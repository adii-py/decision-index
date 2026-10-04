import json
import os

from decision_index.engines.base import Engine, Unsupported
from decision_index.engines.http import CAPACITY_MARKERS, grid_chat_base_url

SYSTEM = (
    "You answer typed decision questions. Reply with one JSON object and nothing else. "
    "Keys are the question keys. For a choice question the value is exactly one option key from that question's criteria. "
    "For a noul question the value is a number from 0 to 1, the probability the answer is yes."
)


def extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def answers_from(payload, questions):
    if isinstance(payload, dict) and isinstance(payload.get("answers"), dict):
        payload = payload["answers"]
    if not isinstance(payload, dict):
        raise ValueError("model reply is not a JSON object")
    answers = {}
    for key, question in questions.items():
        if key not in payload:
            raise ValueError("missing answer for " + key)
        value = payload[key]
        if question["type"] == "choice":
            if not isinstance(value, str) or value not in question["criteria"]:
                raise ValueError("invalid choice for " + key)
            answers[key] = {"type": "choice", "choice": value, "probabilities": {opt: (1.0 if opt == value else 0.0) for opt in question["criteria"]}}
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


def usage_from(raw):
    usage = (raw or {}).get("usage") or {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    out = {}
    if isinstance(prompt, int) and not isinstance(prompt, bool):
        out["input_tokens"] = prompt
    if isinstance(completion, int) and not isinstance(completion, bool):
        out["output_tokens"] = completion
    return out or None


class GridChat(Engine):
    name = "grid"
    latency = "HTTP request wall time against the OpenAI-compatible chat completions endpoint; excludes server startup."

    def __init__(self, base_url=None, model="default", token_env="OPENAI_API_KEY", timeout=600, **options):
        super().__init__(**options)
        import httpx

        base_url = grid_chat_base_url(base_url or os.environ.get("OPENAI_BASE_URL") or os.environ.get("DECISION_INDEX_BASE_URL"))
        if not base_url or base_url == "/v1":
            raise ValueError("GridChat needs base_url (or OPENAI_BASE_URL)")
        token = os.environ.get(token_env) or os.environ.get("DECISION_INDEX_API_KEY")
        headers = {"Authorization": "Bearer " + token} if token else {}
        self.model = model
        self.timeout = timeout
        self.client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)
        self.provenance = {
            "kind": "grid-chat",
            "base_url": base_url,
            "model": model,
            "policy": "One chat completion per request. The reply must be JSON mapping each question key to an option key (choice) or a yes-probability (noul). Choice probabilities are 1 on the chosen key and 0 on the others; this engine does not read token logprobs.",
        }

    def __call__(self, state, questions):
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps({"state": state, "questions": questions}, ensure_ascii=False)},
            ],
        }
        response = self.client.post("/chat/completions", json=body)
        if response.status_code in (400, 413, 422):
            message = response.text
            if any(marker in message for marker in CAPACITY_MARKERS):
                raise Unsupported(message)
        response.raise_for_status()
        raw = response.json()
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("chat completion has no message content") from exc
        answers = answers_from(extract_json(content), questions)
        result = {"model": self.model, "answers": answers}
        usage = usage_from(raw)
        if usage:
            result["usage"] = usage
        return result, raw

    def close(self):
        self.client.close()
