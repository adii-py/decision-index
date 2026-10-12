import os
import re

from decision_index.engines.base import Engine, Unsupported

CAPACITY_MARKERS = (
    "options per choice",
    "a choice needs at least two options",
    "a score takes 2 to 10 levels",
    "the canvas holds",
    "maximum context length",
    "maximum model length",
    "longer than the maximum model length",
    "context window",
    "too many tokens",
)

STOP_STATUS_CODES = frozenset({401, 403, 429})


class HttpStop(RuntimeError):
    """HTTP response that should end the run early but keep partial results."""

    def __init__(self, status_code, message="", retry_after=None):
        self.status_code = int(status_code)
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code}: {message[:500]}")


DECISIONS_PATH = "/v1/decisions"


def normalize_decisions_base_url(base_url):
    base_url = (base_url or "").strip().rstrip("/")
    for suffix in (DECISIONS_PATH, "/v1"):
        if base_url.endswith(suffix):
            return base_url[: -len(suffix)]
    return base_url


def grid_chat_base_url(base_url):
    origin = normalize_decisions_base_url(base_url)
    return origin + "/v1"


def normalize_http_response(raw):
    if isinstance(raw, dict) and isinstance(raw.get("answers"), dict):
        return {"answers": raw["answers"]}
    raise ValueError("unexpected /v1/decisions response shape")


class HttpDecisions(Engine):
    name = "http"
    thread_safe = True
    latency = "HTTP request wall time against the configured /v1/decisions endpoint, including server-side prompt construction and inference; excludes server startup."

    def __init__(
        self,
        base_url=None,
        model="default",
        token_env="DECISION_INDEX_API_KEY",
        timeout=600,
        extra=None,
        decisions_path=None,
        **options,
    ):
        super().__init__(**options)
        import httpx

        base_url = normalize_decisions_base_url(base_url or os.environ.get("DECISION_INDEX_BASE_URL") or os.environ.get("OPENAI_BASE_URL"))
        if not base_url:
            raise ValueError("HttpDecisions needs base_url (or DECISION_INDEX_BASE_URL)")
        headers = {}
        token = os.environ.get(token_env)
        if token:
            headers["Authorization"] = "Bearer " + token
        self.model = model
        self.extra = extra or {}
        self.decisions_path = decisions_path or DECISIONS_PATH
        self.client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)
        self.provenance = {
            "kind": "http",
            "base_url": base_url,
            "model": model,
            "decisions_path": self.decisions_path,
            "request_options": self.extra,
            "policy": f"POST {self.decisions_path} with unmodified state and questions; explicit capacity rejections (HTTP 422 with a known marker) are unsupported.",
        }

    def __call__(self, state, questions):
        body = {"model": self.model, "questions": questions, **self.extra, "state": state}
        r = self.client.post(self.decisions_path, json=body)
        if r.status_code in STOP_STATUS_CODES:
            raise HttpStop(r.status_code, r.text, retry_after=r.headers.get("retry-after"))
        if r.status_code in (400, 413, 422):
            message = r.text
            if any(s in message for s in CAPACITY_MARKERS):
                raise Unsupported(message)
        r.raise_for_status()
        raw = r.json()
        return normalize_http_response(raw), raw

    def close(self):
        self.client.close()
