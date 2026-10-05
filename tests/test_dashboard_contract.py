import json
import os
import subprocess
from pathlib import Path

from decision_index.dashboard_emit import emit, token_report
from decision_index.engines.grid import answers_from, extract_json
from decision_index.engines.http import grid_chat_base_url, normalize_systemone_base_url

ROOT = Path(__file__).resolve().parents[1]


def run_sh(tmp_path, *args, stub="success"):
    env = os.environ.copy()
    env["DECISION_INDEX_STUB"] = stub
    env["EVAL_RUNNER_OUTPUT_DIR"] = str(tmp_path / "out")
    env.pop("EVAL_RUNNER_WORK_DIR", None)
    proc = subprocess.run(["bash", str(ROOT / "run.sh"), *args], cwd=ROOT, env=env, text=True, capture_output=True)
    return proc


def test_normalize_systemone_base_url_strips_v1_suffixes():
    assert normalize_systemone_base_url("https://grid.ai.juspay.net/v1") == "https://grid.ai.juspay.net"
    assert normalize_systemone_base_url("https://grid.ai.juspay.net/v1/systemone") == "https://grid.ai.juspay.net"
    assert grid_chat_base_url("https://grid.ai.juspay.net") == "https://grid.ai.juspay.net/v1"


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
    scores.write_text(json.dumps({"decision_index": 42, "raw_index": 7, "completed": 2, "complete": False, "counts": {"ok": 2}}))
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
    assert saved["metrics"]["secondary"]["complete"] == 0
    assert saved["metrics"]["additional"]["row_start"] == 0
    assert saved["metrics"]["additional"]["row_end"] == 49


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
    assert "suite_dataset" not in by_name
    assert by_name["engine"]["default"] == "http"
    assert by_name["resume"]["default"] == "false"
    assert "delay_s" in by_name
    assert "input_token_price" in by_name and "output_token_price" in by_name
