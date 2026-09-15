"""Model providers.

Two implementations exist so that extraction accuracy, latency and cost can be
compared between a hosted frontier model and a local open one. Both satisfy the
same protocol, so the worker and the evaluation harness are indifferent to
which is in use.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from .config import Config
from .ratelimit import TokenBucket


@dataclass
class Completion:
    """One model response plus what it cost to get it."""
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    model: str = ""


class ProviderError(RuntimeError):
    """Raised when a provider call fails.

    ``retryable`` separates a transient failure -- a timeout, a 503, a rate
    limit -- from a permanent one such as a malformed request. Only the former
    is worth retrying; retrying the latter just burns quota and delays the
    message reaching the dead-letter queue where a human can see it.
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class RateLimited(ProviderError):
    """The provider refused because of a quota limit."""

    def __init__(self, retry_after: float):
        super().__init__(f"rate limited; retry after {retry_after:.1f}s", retryable=True)
        self.retry_after = retry_after


def _retry_after_seconds(resp: requests.Response) -> float:
    """Extract a retry delay from a 429 response, defaulting to 30 seconds.

    Google returns the delay inside the error payload as a RetryInfo detail
    rather than in the standard Retry-After header, so both are checked.
    """
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            delay = detail.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                return float(delay[:-1])
    except (ValueError, AttributeError, TypeError):
        pass
    return 30.0


class Provider(Protocol):
    name: str

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> Completion: ...

    def embed(self, text: str) -> list[float]: ...


class GeminiProvider:
    """Google Gemini via the native generative-language API.

    Uses the API's own structured-output support (``responseSchema``) rather
    than asking for JSON in the prompt and hoping. Constrained decoding means
    the response is schema-valid by construction, which removes an entire class
    of parse-and-repair logic.
    """

    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        # Extraction and embedding are limited independently by the provider,
        # so they get independent buckets.
        self.extract_limiter = TokenBucket(cfg.extraction_rpm)
        self.embed_limiter = TokenBucket(cfg.embedding_rpm)

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> Completion:
        self.extract_limiter.acquire()
        url = f"{self.BASE}/models/{self.cfg.extraction_model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                # Deterministic decoding. Attribute extraction has one right
                # answer, and a benchmark that changes between runs cannot be
                # used to tell an improvement from noise.
                "temperature": 0.0,
            },
        }
        started = time.monotonic()
        response = self._post(url, payload)
        elapsed = int((time.monotonic() - started) * 1000)

        try:
            text = response["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            # A response with no candidate usually means the prompt was
            # blocked by a safety filter, which no amount of retrying fixes.
            raise ProviderError(f"no candidate in response: {str(response)[:300]}", retryable=False) from exc

        usage = response.get("usageMetadata", {})
        return Completion(
            text=text,
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
            latency_ms=elapsed,
            model=response.get("modelVersion", self.cfg.extraction_model),
        )

    def embed(self, text: str) -> list[float]:
        self.embed_limiter.acquire()
        url = f"{self.BASE}/models/{self.cfg.embedding_model}:embedContent"
        payload = {
            "model": f"models/{self.cfg.embedding_model}",
            "content": {"parts": [{"text": text}]},
            # Requested explicitly so the vector always matches the column
            # width declared in the database schema.
            "outputDimensionality": self.cfg.embedding_dims,
        }
        response = self._post(url, payload)
        values = response.get("embedding", {}).get("values")
        if not values:
            raise ProviderError(f"no embedding in response: {str(response)[:200]}", retryable=False)
        return values

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self.session.post(
                url,
                params={"key": self.cfg.gemini_api_key},
                json=payload,
                timeout=self.cfg.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"network error: {exc}") from exc

        if resp.status_code == 429:
            # The provider tells us how long to wait; honouring it is far more
            # effective than guessing with exponential backoff.
            raise RateLimited(retry_after=_retry_after_seconds(resp))
        if resp.status_code >= 500:
            raise ProviderError(f"provider returned {resp.status_code}: {resp.text[:200]}", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError(f"provider returned {resp.status_code}: {resp.text[:300]}", retryable=False)

        return resp.json()


class OllamaProvider:
    """A locally hosted open model, used as the comparison arm in the benchmark.

    Ollama supports JSON-schema-constrained output through its ``format``
    parameter, so the same schema drives both providers. Embeddings come from
    the same host, and are padded or truncated to the configured width because
    open embedding models rarely produce exactly 768 dimensions.
    """

    name = "ollama"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> Completion:
        started = time.monotonic()
        try:
            resp = self.session.post(
                f"{self.cfg.ollama_host}/api/generate",
                json={
                    "model": self.cfg.ollama_model,
                    "prompt": prompt,
                    "format": _to_json_schema(schema),
                    "stream": False,
                    "options": {"temperature": 0.0},
                },
                timeout=self.cfg.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"ollama unreachable: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(f"ollama returned {resp.status_code}: {resp.text[:200]}",
                                retryable=resp.status_code >= 500)

        body = resp.json()
        return Completion(
            text=body.get("response", ""),
            prompt_tokens=body.get("prompt_eval_count", 0),
            completion_tokens=body.get("eval_count", 0),
            latency_ms=int((time.monotonic() - started) * 1000),
            model=self.cfg.ollama_model,
        )

    def embed(self, text: str) -> list[float]:
        try:
            resp = self.session.post(
                f"{self.cfg.ollama_host}/api/embeddings",
                json={"model": self.cfg.ollama_model, "prompt": text},
                timeout=self.cfg.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"ollama unreachable: {exc}") from exc

        values = resp.json().get("embedding", [])
        if not values:
            raise ProviderError("ollama returned no embedding", retryable=False)

        width = self.cfg.embedding_dims
        if len(values) >= width:
            return values[:width]
        return values + [0.0] * (width - len(values))


def _to_json_schema(gemini_schema: dict[str, Any]) -> dict[str, Any]:
    """Translate the Gemini schema dialect (uppercase types) to plain JSON Schema."""
    out: dict[str, Any] = {}
    for key, value in gemini_schema.items():
        if key == "type":
            out["type"] = str(value).lower()
        elif key == "properties":
            out["properties"] = {k: _to_json_schema(v) for k, v in value.items()}
        else:
            out[key] = value
    return out


def build(cfg: Config) -> Provider:
    if cfg.provider == "ollama":
        return OllamaProvider(cfg)
    cfg.require_key()
    return GeminiProvider(cfg)
