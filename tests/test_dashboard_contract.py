import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from decision_index.dashboard_emit import emit, token_report
from decision_index.engines import validate
from decision_index.engines.grid import answers_from, extract_json
from decision_index.engines.http import (
    HttpDecisions,
    grid_chat_base_url,
    normalize_decisions_base_url,
    normalize_http_response,
)

ROOT = Path(__file__).resolve().parents[1]


def run_sh(tmp_path, *args, stub="success"):
    env = os.environ.copy()
    env["DECISION_INDEX_STUB"] = stub
    env["EVAL_RUNNER_OUTPUT_DIR"] = str(tmp_path / "out")
    env.pop("EVAL_RUNNER_WORK_DIR", None)
    proc = subprocess.run(["bash", str(ROOT / "run.sh"), *args], cwd=ROOT, env=env, text=True, capture_output=True)
    return proc


def test_normalize_decisions_base_url_strips_v1_suffixes():
    assert normalize_decisions_base_url("https://grid.ai.juspay.net/v1") == "https://grid.ai.juspay.net"
    assert normalize_decisions_base_url("https://grid.ai.juspay.net/v1/decisions") == "https://grid.ai.juspay.net"
    assert normalize_decisions_base_url("https://grid.ai.juspay.net/") == "https://grid.ai.juspay.net"
    assert grid_chat_base_url("https://grid.ai.juspay.net/v1/decisions") == "https://grid.ai.juspay.net/v1"


def test_decisions_engine_posts_model_state_and_questions(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(host=request.url.host, path=request.url.path, auth=request.headers.get("authorization"), body=json.loads(request.content))
        answers = {
            "when": {"type": "choice", "choice": "later", "probabilities": {"now": 0.2, "later": 0.8}},
            "luggage": {"type": "noul", "noul": 0.97},
        }
        return httpx.Response(200, json={"model": "xor-latest", "answers": answers, "usage": {"input_tokens": 120, "output_tokens": 2}})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("DECISION_INDEX_API_KEY", "sk-test")
    state = {"transcript": "Book me a cab from Koramangala 5th Block to Majestic tomorrow morning for 3 people, we have two big bags", "current_time": "2026-10-05T10:00:00"}
    questions = {
        "when": {"type": "choice", "instructions": "Now or scheduled?", "criteria": {"now": "Now", "later": "Scheduled"}},
        "luggage": {"type": "noul", "instructions": "Does the rider mention bags or luggage?"},
    }
    engine = HttpDecisions(base_url="https://grid.ai.juspay.net/v1/decisions", model="xor-latest")
    response, raw = engine(state, questions)
    validate(questions, response)
    assert seen == {"host": "grid.ai.juspay.net", "path": "/v1/decisions", "auth": "Bearer sk-test", "body": {"model": "xor-latest", "questions": questions, "state": state}}
    assert response["answers"]["when"]["choice"] == "later" and raw["usage"]["output_tokens"] == 2
    assert response["model"] == "xor-latest" and response["usage"] == {"input_tokens": 120, "output_tokens": 2}
    assert engine.provenance["decisions_path"] == "/v1/decisions"


def test_decisions_path_override_and_unexpected_response_shape():
    assert HttpDecisions(base_url="https://grid.example", model="xor-latest", decisions_path="/v2/decisions").decisions_path == "/v2/decisions"
    with pytest.raises(ValueError, match="/v1/decisions response"):
        normalize_http_response({"result": {"q": "a"}})


def test_grid_reply_parses_fenced_choice_and_noul():
    questions = {
        "q": {"type": "choice", "criteria": {"A": "a", "B": "b"}},
        "yes": {"type": "noul"},
    }
    payload = extract_json("```json\n{\"answers\": {\"q\": \"B\", \"yes\": 0.25}}\n```")
    answers = answers_from(payload, questions)
    assert answers["q"]["choice"] == "B"
    assert answers["q"]["probabilities"] == {"A": 0.0, "B": 1.0}
    assert answers["yes"]["noul"] == 0.25


def test_grid_reply_rejects_unknown_choice():
    try:
        answers_from({"q": "nope"}, {"q": {"type": "choice", "criteria": {"A": "a"}}})
    except ValueError as exc:
        assert "invalid choice" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_token_report_stays_unpriced_when_one_price_is_missing():
    attempts = [{"run_id": "a", "status": "ok", "measured": True, "input_tokens": 10, "output_tokens": 4}]
    report = token_report(attempts, 1.5, None)
    assert report["custom_cost_usd"] is None
    assert report["input_tokens"] == 10
    assert "output_token_price" in report["note"]


def test_emit_requires_numeric_index(tmp_path):
    scores = tmp_path / "scores.json"
    scores.write_text(
        json.dumps(
            {
                "decision_index": 42,
                "raw_index": 7,
                "completed": 2,
                "complete": False,
                "counts": {"ok": 2},
                "latency_ms": {"median": 152.9, "p95": 603.8, "mean": 235.1},
                "scores": {"balanced_skill": 42.0, "balanced_raw": 7.0, "breadth_skill": 40.0},
                "panel_id": "decision-index-0.2.1",
                "suite": {"edition": "release-v2.1", "scoreable": 119898},
                "areas": [{"id": "tools", "label": "Tools", "skill": 0.1, "raw": 0.2, "coverage": 1.0, "n": 5, "benchmarks": [1]}],
            }
        )
    )
    out = tmp_path / "id_results.json"
    doc = emit(
        scores,
        None,
        out,
        {
            "engine": "http",
            "model": "xor-1.2",
            "edition": "0.2.1",
            "task_range": "0-49",
            "row_start": 0,
            "row_end": 49,
            "range_note": "ok",
            "run_rc": 0,
            "base_url": "https://grid.ai.juspay.net",
            "delay_s": 0.5,
        },
        None,
        None,
        None,
    )
    assert doc["metrics"]["main"] == {"name": "Decision Index", "value": 42.0}
    saved = json.loads(out.read_text())
    sec = saved["metrics"]["secondary"]
    add = saved["metrics"]["additional"]
    assert sec["complete"] == 0
    assert sec["balanced_skill"] == 42.0
    assert sec["balanced_raw"] == 7.0
    assert sec["breadth_skill"] == 40.0
    assert sec["latency_median_ms"] == 152.9
    assert sec["latency_p95_ms"] == 603.8
    assert sec["latency_mean_ms"] == 235.1
    assert sec["panel_id"] == "decision-index-0.2.1"
    assert add["latency_ms"] == {"median": 152.9, "p95": 603.8, "mean": 235.1}
    assert add["scores"]["breadth_skill"] == 40.0
    assert add["suite"]["edition"] == "release-v2.1"
    assert add["areas"][0]["label"] == "Tools"
    assert add["row_start"] == 0
    assert add["row_end"] == 49


def test_emit_includes_early_stop_from_status(tmp_path):
    scores = tmp_path / "scores.json"
    scores.write_text(json.dumps({"decision_index": 10, "raw_index": 9, "completed": 3, "complete": False, "counts": {"ok": 2, "error": 1}}))
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "event": "stopped",
                "ended_abruptly": 1,
                "stop_reason": "http_429",
                "last_errors": [{"run_id": "a", "status_code": 429}],
                "ok_rate": 0.6667,
            }
        )
    )
    out = tmp_path / "id_results.json"
    doc = emit(scores, None, out, {"engine": "http", "model": "m", "edition": "0.2.1", "run_rc": 0}, None, None, None, status_path=status)
    add = doc["metrics"]["additional"]
    assert add["ended_abruptly"] == 1
    assert add["stop_reason"] == "http_429"
    assert add["attempted_ok_rate"] == 0.6667


def test_http_run_defaults_decisions_model_when_model_alpha_missing(tmp_path):
    proc = run_sh(
        tmp_path,
        "xor-default",
        "--engine",
        "http",
        "--model",
        "private-large",
        "--task-range",
        "0-0",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "decisions_model=xor-latest" in proc.stdout
    assert "decisions_path=/v1/decisions" in proc.stdout
    assert "dashboard_model=private-large" in proc.stdout
    doc = json.loads((tmp_path / "out" / "xor-default_results.json").read_text())
    assert doc["metrics"]["additional"]["decisions_model"] == "xor-latest"
    assert doc["metrics"]["additional"]["dashboard_model"] == "private-large"
    assert doc["metrics"]["additional"]["model"] == "xor-latest"


def test_run_sh_success_honors_output_dir_and_inclusive_range(tmp_path):
    proc = run_sh(tmp_path, "run-1", "--model", "stub-model", "--task-range", "0-49", "--not-a-flag", "x")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    doc = json.loads((tmp_path / "out" / "run-1_results.json").read_text())
    assert doc["metrics"]["main"]["value"] == 42
    assert doc["metrics"]["additional"]["row_start"] == 0
    assert doc["metrics"]["additional"]["row_end"] == 49
    assert doc["metrics"]["additional"]["status"] == "scored"
    assert "ignoring unknown flag" in proc.stderr
    assert not (ROOT / "output" / "run-1_results.json").exists()


def test_run_sh_range_not_starting_at_zero_uses_inclusive_slice(tmp_path):
    proc = run_sh(tmp_path, "run-2", "--model", "stub-model", "--task-range", "5-9")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    doc = json.loads((tmp_path / "out" / "run-2_results.json").read_text())
    assert doc["metrics"]["additional"]["row_start"] == 5
    assert doc["metrics"]["additional"]["row_end"] == 9
    assert "does not start at 0" not in proc.stderr


def test_run_sh_fallback_and_manual_id(tmp_path):
    failed = run_sh(tmp_path, "run-3", stub="fallback")
    assert failed.returncode != 0
    doc = json.loads((tmp_path / "out" / "run-3_results.json").read_text())
    assert doc["metrics"]["main"]["value"] == 0
    assert doc["metrics"]["additional"]["reason"] == "stub fallback"

    manual = run_sh(tmp_path, "--model", "stub-model")
    assert manual.returncode == 0, manual.stderr + manual.stdout
    written = list((tmp_path / "out").glob("local_*_results.json"))
    assert len(written) == 1
    assert json.loads(written[0].read_text())["metrics"]["main"]["value"] == 42


def test_input_param_schema_is_json():
    schema = json.loads((ROOT / "input_param.json").read_text())
    by_name = {field["name"]: field for field in schema["fields"]}
    assert by_name["model"]["required"] is True
    assert by_name["model_alpha"]["required"] is True
    assert by_name["model_alpha"]["default"] == "xor-latest"
    assert by_name["model_alpha"]["options"] == ["xor-latest"]
    assert "suite_dataset" not in by_name
    assert by_name["engine"]["default"] == "http"
    assert by_name["resume"]["default"] == "false"
    assert "delay_s" in by_name
    assert "input_token_price" in by_name and "output_token_price" in by_name
    assert by_name["concurrency"]["default"] == 20
    assert by_name["concurrency"]["constraints"] == {"min": 1, "max": 20}
    assert by_name["delay_s"]["default"] == 0


REPORT_FILES = ("decision_index_report.html", "decision_index_report.md", "decision_index_analysis.json", "decision_index_benchmarks.csv")


def test_emit_reports_concurrency_and_run_health(tmp_path):
    scores = tmp_path / "scores.json"
    scores.write_text(json.dumps({"decision_index": 10, "completed": 2, "counts": {"ok": 2}}))
    results = tmp_path / "results.jsonl"
    rows = [
        {"run_id": "a", "status": "ok", "started_utc": "2026-10-12T00:00:00+00:00", "completed_utc": "2026-10-12T00:00:30+00:00", "total_wall_ms": 500.0},
        {"run_id": "b", "status": "ok", "retries": ["http_429"], "started_utc": "2026-10-12T00:00:30+00:00", "completed_utc": "2026-10-12T00:01:00+00:00", "total_wall_ms": 400.0},
    ]
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    meta = {"engine": "http", "model": "m", "edition": "0.2.1", "run_rc": 0, "concurrency": 20}
    doc = emit(scores, results, tmp_path / "id_results.json", meta, None, None, None)
    add = doc["metrics"]["additional"]
    assert add["concurrency"] == 20
    assert add["run_health"] == {"attempts": 2, "wall_seconds": 60.0, "requests_per_minute": 2.0, "retried_rows": 1, "retries_exhausted": 0}


def test_run_sh_accepts_concurrency_and_writes_reports(tmp_path):
    proc = run_sh(tmp_path, "run-c", "--model", "stub-model", "--concurrency", "12", "--task-range", "0-9")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "ignoring unknown flag: --concurrency" not in proc.stderr
    assert "concurrency=12" in proc.stdout
    doc = json.loads((tmp_path / "out" / "run-c_results.json").read_text())
    assert doc["metrics"]["additional"]["concurrency"] == 12
    for name in REPORT_FILES:
        assert (tmp_path / "out" / name).is_file(), name


def test_run_sh_normalizes_dashboard_concurrency_values(tmp_path):
    as_float = run_sh(tmp_path, "run-d", "--model", "stub-model", "--concurrency", "20.0")
    assert as_float.returncode == 0 and "concurrency=20 " in as_float.stdout
    junk = run_sh(tmp_path, "run-e", "--model", "stub-model", "--concurrency", "zero")
    assert junk.returncode == 0 and "concurrency=1 " in junk.stdout and "not a positive integer" in junk.stderr


def test_dashboard_entrypoints_no_longer_reference_systemone():
    for name in ("input_param.json", "run.sh", "scripts/dashboard_defaults.sh", "scripts/verify_dashboard_config.sh"):
        text = (ROOT / name).read_text().lower()
        assert "systemone" not in text and "jev-trained" not in text, name


def test_decisions_response_keeps_model_and_usage_for_token_report():
    answers = {"q": {"type": "noul", "noul": 0.4}}
    openai_style = {"model": "xor-latest", "answers": answers, "usage": {"prompt_tokens": 210, "completion_tokens": 3}}
    assert normalize_http_response(openai_style) == {"model": "xor-latest", "answers": answers, "usage": {"input_tokens": 210, "output_tokens": 3}}
    assert normalize_http_response({"answers": answers}) == {"answers": answers}
