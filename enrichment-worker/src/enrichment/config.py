"""Runtime configuration, read from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    kafka_brokers: str = os.getenv("KAFKA_BROKERS", "localhost:9092")
    consumer_group: str = os.getenv("CONSUMER_GROUP", "enrichment-worker")

    topic_created: str = os.getenv("TOPIC_CREATED", "listing.created")
    # The worker publishes its result to a topic only the catalogue consumes.
    # The catalogue re-emits listing.enriched once the result is persisted, so
    # that moderation can never receive a listing the database has not caught up
    # with. See the race documented in docs/DESIGN.md.
    topic_enrichment_result: str = os.getenv("TOPIC_ENRICHMENT_RESULT", "listing.enrichment-result")
    topic_dlq: str = os.getenv("TOPIC_DLQ", "listing.dlq")

    provider: str = os.getenv("LLM_PROVIDER", "gemini")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    extraction_model: str = os.getenv("EXTRACTION_MODEL", "gemini-flash-lite-latest")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

    # The schema declares vector(768); the embedding request asks for exactly
    # this many dimensions so the two can never drift apart silently.
    embedding_dims: int = int(os.getenv("EMBEDDING_DIMS", "768"))

    # Paced below the observed free-tier ceiling so requests are shaped locally
    # rather than refused remotely. Set too low this becomes its own outage: at
    # 12 rpm a batch of thirty listings took longer than the catalogue's stall
    # timeout, and the reconciliation sweep failed listings that were merely
    # queued. Rate limits and liveness timeouts have to be chosen together.
    extraction_rpm: float = float(os.getenv("EXTRACTION_RPM", "30"))
    embedding_rpm: float = float(os.getenv("EMBEDDING_RPM", "60"))

    max_attempts: int = int(os.getenv("MAX_ATTEMPTS", "5"))
    backoff_base_seconds: float = float(os.getenv("BACKOFF_BASE_SECONDS", "1.0"))
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "45"))

    def require_key(self) -> None:
        if self.provider == "gemini" and not self.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")


CONFIG = Config()
