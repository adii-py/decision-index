import os
import re

from decision_index.engines.base import Engine, Unsupported, text

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

    def __init__(self, status_code, message=""):
        self.status_code = int(status_code)
        self.message = message
        super().__init__(f"HTTP {status_code}: {message[:500]}")


DEFAULT_SYSTEMONE_PATH = "/v1/systemone"
JEV_TRAINED_SYSTEMONE_PATH = "/v1/systemone-custom"

MODEL_SYSTEMONE_PATHS = {
    "jev-latest": DEFAULT_SYSTEMONE_PATH,
    "jev-trained": JEV_TRAINED_SYSTEMONE_PATH,
}


def systemone_path_for_model(model, override=None):
    if override:
        return override
    return MODEL_SYSTEMONE_PATHS.get(model or "", DEFAULT_SYSTEMONE_PATH)


def normalize_systemone_base_url(base_url):
    base_url = (base_url or "").strip().rstrip("/")
    for suffix in (JEV_TRAINED_SYSTEMONE_PATH, DEFAULT_SYSTEMONE_PATH, "/v1"):
        if base_url.endswith(suffix):
            return base_url[: -len(suffix)]
    return base_url


def grid_chat_base_url(base_url):
    origin = normalize_systemone_base_url(base_url)
    return origin + "/v1"


def answers_from_custom_result(result, questions, confidence=None):
    if not isinstance(result, dict):
        raise ValueError("systemone-custom result is not a JSON object")
    confidence = confidence or {}
    answers = {}
    for key, question in questions.items():
        if key not in result:
            raise ValueError("missing answer for " + key)
        value = result[key]
        if question["type"] == "choice":
            if not isinstance(value, str) or value not in question["criteria"]:
                raise ValueError("invalid choice for " + key)
            conf = confidence.get(key) if isinstance(confidence.get(key), dict) else {}
            mean_p = conf.get("mean_p")
            if isinstance(mean_p, (int, float)) and 0 <= float(mean_p) <= 1:
                chosen = float(mean_p)
                rest = 1.0 - chosen
                others = [opt for opt in question["criteria"] if opt != value]
                tail = rest / len(others) if others else 0.0
                probabilities = {opt: (chosen if opt == value else tail) for opt in question["criteria"]}
            else:
                probabilities = {opt: (1.0 if opt == value else 0.0) for opt in question["criteria"]}
            answers[key] = {"type": "choice", "choice": value, "probabilities": probabilities}
        elif question["type"] == "noul":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("invalid noul for " + key)
            probability = float(value)
            if not 0 <= probability <= 1:
                raise ValueError("noul out of range for " + key)
            answers[key] = {"type": "noul", "noul": probability}
        else:
            raise Unsupported("Unsupported question type " + str(question["type"]))
    return answers


def normalize_http_response(raw, questions, systemone_path):
    if isinstance(raw, dict) and isinstance(raw.get("answers"), dict):
        return {"answers": raw["answers"]}
    if systemone_path == JEV_TRAINED_SYSTEMONE_PATH:
        return {"answers": answers_from_custom_result(raw.get("result"), questions, raw.get("confidence"))}
    raise ValueError("unexpected systemone response shape")


class HttpSystemOne(Engine):
    name = "http"
    latency = "HTTP request wall time against the configured /v1/systemone endpoint, including server-side prompt construction and inference; excludes server startup."

    def __init__(
        self,
        base_url=None,
        model="default",
        token_env="DECISION_INDEX_API_KEY",
        timeout=600,
        extra=None,
        systemone_path=None,
        **options,
    ):
        super().__init__(**options)
        import httpx

        base_url = normalize_systemone_base_url(base_url or os.environ.get("DECISION_INDEX_BASE_URL") or os.environ.get("OPENAI_BASE_URL"))
        if not base_url:
            raise ValueError("HttpSystemOne needs base_url (or DECISION_INDEX_BASE_URL)")
        headers = {}
        token = os.environ.get(token_env)
        if token:
            headers["Authorization"] = "Bearer " + token
        self.model = model
        self.extra = extra or {}
        self.systemone_path = systemone_path_for_model(model, systemone_path)
        self.client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)
        self.provenance = {
            "kind": "http",
            "base_url": base_url,
            "model": model,
            "systemone_path": self.systemone_path,
            "request_options": self.extra,
            "policy": f"POST {self.systemone_path} with unmodified state and questions; explicit capacity rejections (HTTP 422 with a known marker) are unsupported.",
        }

    def _request_body(self, state, questions):
        body = {"model": self.model, "questions": questions, **self.extra}
        if self.systemone_path == JEV_TRAINED_SYSTEMONE_PATH:
            body["context"] = text(state)
        else:
            body["state"] = state
        return body

    def __call__(self, state, questions):
        r = self.client.post(self.systemone_path, json=self._request_body(state, questions))
        if r.status_code in STOP_STATUS_CODES:
            raise HttpStop(r.status_code, r.text)
        if r.status_code in (400, 413, 422):
            message = r.text
            if any(s in message for s in CAPACITY_MARKERS):
                raise Unsupported(message)
        r.raise_for_status()
        raw = r.json()
        response = normalize_http_response(raw, questions, self.systemone_path)
        return response, raw

    def close(self):
        self.client.close()
