"""Kafka consume/produce loop for the enrichment worker."""
from __future__ import annotations

import json
import logging
import signal
import sys
from typing import Any

from confluent_kafka import Consumer, KafkaError, Producer

from . import providers
from .config import CONFIG, Config
from .extractor import Extractor, PermanentFailure

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, cfg: Config = CONFIG):
        self.cfg = cfg
        self.provider = providers.build(cfg)
        self.extractor = Extractor(self.provider, cfg)
        self.running = True

        self.consumer = Consumer({
            "bootstrap.servers": cfg.kafka_brokers,
            "group.id": cfg.consumer_group,
            "auto.offset.reset": "earliest",
            # Offsets are committed by hand after the result has been produced.
            # Auto-commit would acknowledge a listing the moment it was read,
            # so a crash mid-enrichment would lose it silently.
            "enable.auto.commit": False,
        })
        self.producer = Producer({
            "bootstrap.servers": cfg.kafka_brokers,
            "enable.idempotence": True,
            "acks": "all",
        })

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

        self.consumer.subscribe([self.cfg.topic_created])
        log.info("enrichment worker started: provider=%s model=%s",
                 self.provider.name, self.cfg.extraction_model)

        while self.running:
            message = self.consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                if message.error().code() != KafkaError._PARTITION_EOF:
                    log.error("consumer error: %s", message.error())
                continue

            try:
                self._handle(message.value())
            except Exception:
                # The handler already routes permanent failures to the DLQ, so
                # reaching here means an unexpected bug. The offset is still
                # committed: leaving it uncommitted would replay the same
                # message forever and stall the partition.
                log.exception("unhandled error processing message; committing to avoid a stall")

            self.consumer.commit(message=message, asynchronous=False)

        self.producer.flush(10)
        self.consumer.close()
        log.info("enrichment worker stopped")

    def _handle(self, raw: bytes) -> None:
        event = json.loads(raw)
        listing_id = event.get("listingId")
        if listing_id is None:
            log.warning("event without listingId, skipping: %s", raw[:200])
            return

        title = event.get("rawTitle") or ""
        description = event.get("rawDescription")

        try:
            enrichment = self.extractor.enrich(title, description)
        except PermanentFailure as exc:
            log.error("listing %s failed permanently: %s", listing_id, exc)
            self._produce(self.cfg.topic_dlq, listing_id, {
                "listingId": listing_id,
                "stage": "enrichment",
                "error": str(exc),
                "rawTitle": title,
            })
            return

        try:
            embedding = self.provider.embed(self.extractor.embedding_text(enrichment, title))
        except Exception as exc:
            # A missing embedding costs duplicate detection, not correctness,
            # so the listing still goes forward. Moderation treats an absent
            # embedding as "cannot check for duplicates" rather than "unique".
            log.warning("embedding failed for listing %s: %s", listing_id, exc)
            embedding = []

        payload: dict[str, Any] = {
            "listingId": listing_id,
            "sellerId": event.get("sellerId"),
            # The seller's original text is carried forward untouched. The
            # enrichment prompt strips promotional and non-compliant language,
            # so a listing claiming to cure a disease arrives at moderation
            # already sanitised. Moderation has to see what the seller actually
            # wrote, or the model quietly launders policy violations.
            "rawTitle": title,
            "rawDescription": description,
            "normalizedTitle": enrichment.normalized_title,
            "description": enrichment.description,
            "category": enrichment.category,
            "attributes": enrichment.attributes,
            "extractionProblems": enrichment.problems,
            "embedding": embedding,
            "embeddingModel": self.cfg.embedding_model if embedding else "",
            "extractionModel": enrichment.model,
            "promptTokens": enrichment.prompt_tokens,
            "completionTokens": enrichment.completion_tokens,
            "latencyMs": enrichment.latency_ms,
        }
        self._produce(self.cfg.topic_enrichment_result, listing_id, payload)
        log.info("listing %s enriched: category=%s attrs=%d problems=%d latency=%dms",
                 listing_id, enrichment.category, len(enrichment.attributes),
                 len(enrichment.problems), enrichment.latency_ms)

    def _produce(self, topic: str, key: Any, payload: dict[str, Any]) -> None:
        self.producer.produce(topic, key=str(key).encode(), value=json.dumps(payload).encode())
        self.producer.poll(0)

    def _stop(self, *_: Any) -> None:
        log.info("shutdown signal received")
        self.running = False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )
    try:
        Worker().run()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
