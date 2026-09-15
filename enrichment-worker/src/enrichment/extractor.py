"""Turns a raw seller listing into validated, structured catalog data."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import taxonomy
from .config import Config
from .providers import Completion, Provider, ProviderError, RateLimited

log = logging.getLogger(__name__)

PROMPT = """You are cataloguing a product listing for an Indian online marketplace.

Seller title: {title}
Seller description: {description}

Extract structured catalogue data.

Rules:
- Pick the single best category from the allowed list. If nothing fits, use "unknown".
- Fill an attribute only if the listing states or clearly implies it. Leave it out otherwise.
  Guessing a plausible value is worse than omitting it, because a wrong attribute
  silently corrupts search filters.
- normalized_title: a clean, factual title under 80 characters. Remove promotional
  noise ("BEST QUALITY", "free shipping", "100% guarantee"), fix obvious spelling
  errors, keep the product facts.
- description: two neutral factual sentences. Do not invent features, materials,
  measurements or guarantees that the seller did not state.
- pack_size is the number of units sold together. "combo of 2", "2pcs", "pack of 2"
  all mean 2. Default to 1 only when the listing implies a single item.
"""


@dataclass
class Enrichment:
    """The result of enriching one listing."""
    category: str
    normalized_title: str
    description: str
    attributes: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1


class PermanentFailure(RuntimeError):
    """Raised when a listing cannot be enriched and should go to the DLQ."""


class Extractor:
    def __init__(self, provider: Provider, cfg: Config):
        self.provider = provider
        self.cfg = cfg
        self.schema = taxonomy.response_schema()

    def enrich(self, title: str, description: str | None) -> Enrichment:
        """Extract structured data, retrying transient provider failures.

        Retries use exponential backoff. They are bounded because the message
        is not acknowledged until this returns: retrying forever would block
        the partition behind one poisonous listing, which is a far worse
        outcome than one listing reaching the dead-letter queue.
        """
        prompt = PROMPT.format(title=title, description=description or "(none)")
        last_error: Exception | None = None

        for attempt in range(1, self.cfg.max_attempts + 1):
            try:
                completion = self.provider.complete_json(prompt, self.schema)
                return self._parse(completion, attempt)
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable:
                    raise PermanentFailure(f"provider rejected the request: {exc}") from exc
                if attempt < self.cfg.max_attempts:
                    # A rate limit carries the provider's own recommended wait,
                    # which beats an exponential guess: too short and the retry
                    # is refused again, too long and throughput collapses.
                    if isinstance(exc, RateLimited):
                        delay = max(exc.retry_after, self.cfg.backoff_base_seconds)
                    else:
                        delay = self.cfg.backoff_base_seconds * (2 ** (attempt - 1))
                    log.warning("attempt %d/%d failed (%s); retrying in %.1fs",
                                attempt, self.cfg.max_attempts, exc, delay)
                    time.sleep(delay)
            except json.JSONDecodeError as exc:
                # Constrained decoding should make this impossible. If it
                # happens the provider is misbehaving, so it is treated as
                # retryable once rather than assumed permanent.
                last_error = exc
                log.warning("attempt %d returned unparseable JSON: %s", attempt, exc)

        raise PermanentFailure(f"exhausted {self.cfg.max_attempts} attempts: {last_error}")

    def _parse(self, completion: Completion, attempts: int) -> Enrichment:
        raw = json.loads(completion.text)

        category = str(raw.get("category", taxonomy.UNKNOWN_CATEGORY)).strip().lower()
        attributes, problems = taxonomy.validate(category, raw)

        title = str(raw.get("normalized_title", "")).strip()
        description = str(raw.get("description", "")).strip()

        if not title:
            problems.append("model returned an empty normalized_title")
        elif len(title) > 120:
            # Truncation rather than rejection: an over-long title is a
            # cosmetic problem, and flagging it lets a reviewer decide.
            problems.append(f"normalized_title was {len(title)} chars, truncated to 120")
            title = title[:120].rstrip()

        return Enrichment(
            category=category,
            normalized_title=title,
            description=description,
            attributes=attributes,
            problems=problems,
            model=completion.model,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            latency_ms=completion.latency_ms,
            attempts=attempts,
        )

    def embedding_text(self, enrichment: Enrichment, raw_title: str) -> str:
        """Build the text that gets embedded for duplicate detection.

        The normalized title plus category plus sorted attributes, rather than
        the raw seller title. Two sellers describing the same product write
        wildly different promotional text, but their normalized forms converge
        -- which is exactly the property duplicate detection needs. Attributes
        are sorted so that the same product always produces the same string.
        """
        parts = [enrichment.normalized_title or raw_title, enrichment.category]
        parts.extend(f"{k}={v}" for k, v in sorted(enrichment.attributes.items()))
        return " | ".join(str(p) for p in parts if p)
