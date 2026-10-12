import csv
import io
import json

from decision_index.analysis import analyze, read_attempts, run_health, timeline
from decision_index.report import CSV_COLUMNS, CSV_FILE, HTML_FILE, JSON_FILE, MD_FILE, benchmarks_csv, main, render_html, render_md, write_reports


def make_run(tmp_path, benchmarks=3):
    run = tmp_path / "runs" / "r1"
    run.mkdir(parents=True)
    scores = {
        "edition": "0.2.1",
        "decision_index": 41.7,
        "raw_index": 55.2,
        "completed": 5,
        "complete": False,
        "counts": {"ok": 3, "error": 2},
        "scores": {"balanced_skill": 41.7, "breadth_skill": 30.1},
        "suite": {"scoreable": 119898, "added_requests": 30419},
        "areas": [{"id": "tools", "label": "Tools & Automation", "skill": 0.42, "raw": 0.55, "coverage": 0.9, "n": benchmarks, "benchmarks": list(range(1, benchmarks + 1))}],
        "benchmarks": {
            str(n): {"catalog_id": n, "dataset": f"Bench{n}", "requests": 10, "answered": 0 if n == 2 else 10, "errors": 0, "unsupported": 1 if n == 3 else 0, "abstained": 0, "pending": 0, "metric": "accuracy", "score": 0.5, "median_ms": 400.0}
            for n in range(1, benchmarks + 1)
        },
        "index_benchmarks": {str(n): {"raw": 0.5, "skill": 0.0 if n == 3 else 0.4, "coverage": 0.9 if n == 3 else 1.0, "random": 0.25, "in_index": True} for n in range(1, benchmarks + 1)},
        "note": "test note",
    }
    (run / "scores.json").write_text(json.dumps(scores))
    (run / "environment.json").write_text(
        json.dumps({"engine": "http", "engine_options": {"model": "jev-latest", "base_url": "https://grid.example", "timeout": 600, "systemone_path": "/v1/systemone"}, "concurrency": 20, "delay_s": 0, "frozen_corpus_sha256": "abc"})
    )
    t = "2026-10-12T00:{}+00:00".format
    lines = [
        {"run_id": "1:a", "catalog_id": 1, "dataset": "Bench1", "status": "ok", "started_utc": t("00:00"), "completed_utc": t("00:01"), "total_wall_ms": 300.0},
        {"run_id": "1:b", "catalog_id": 1, "dataset": "Bench1", "status": "ok", "retries": ["http_429", "http_429"], "started_utc": t("00:01"), "completed_utc": t("00:02"), "total_wall_ms": 700.0},
        {"run_id": "3:a", "catalog_id": 3, "dataset": "Bench3", "status": "error", "exception": "ReadTimeout", "error": "timed out", "started_utc": t("00:02"), "completed_utc": t("00:03"), "total_wall_ms": 50.0},
        {"run_id": "3:a", "catalog_id": 3, "dataset": "Bench3", "status": "ok", "started_utc": t("01:00"), "completed_utc": t("01:00.500000"), "total_wall_ms": 1500.0},
        {"run_id": "3:b", "catalog_id": 3, "dataset": "Bench3", "status": "error", "exception": "HTTPStatusError", "error": "Server error '502 Bad Gateway' for url 'https://grid.example/v1/systemone'", "started_utc": t("01:00"), "completed_utc": t("01:01"), "total_wall_ms": 40.0},
        {"run_id": "1:c", "catalog_id": 1, "dataset": "Bench1", "status": "error", "exception": "HttpStop", "http_status": 401, "error": "HTTP 401: Received API Key = sk-abcdef123456, Bearer abc.def", "started_utc": t("01:01"), "completed_utc": t("01:02"), "total_wall_ms": 20.0},
    ]
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines) + '{"run_id": "half-writ')
    return run


def test_analyze_merges_scores_and_results(tmp_path):
    a = analyze(make_run(tmp_path), run_id="r1")
    assert a["run_id"] == "r1" and a["status"]["state"] == "partial"
    assert a["status"]["expected"] == 150317 and a["status"]["completed"] == 5
    assert a["failures"]["final_counts"] == {"ok": 3, "error": 2}
    kinds = {e["kind"]: e for e in a["failures"]["errors"]}
    assert set(kinds) == {"HTTPStatusError 502", "HttpStop 401"}
    assert kinds["HTTPStatusError 502"]["benchmarks"] == ["Bench3"]
    assert a["failures"]["retries"] == {"rows": 1, "recovered": 1, "still_failed": 0, "by_reason": {"http_429": 2}}
    b = {x["catalog_id"]: x for x in a["benchmarks"]}
    assert b[2]["flags"] == ["no answers"]
    assert b[3]["flags"] == ["below chance", "low coverage"]
    assert b[1]["area"] == "tools" and b[1]["p95_ms"] == 700.0 and b[1]["chance"] == 0.25
    assert a["throughput"]["attempts"] == 6 and a["throughput"]["wall_seconds"] == 62.0
    assert a["run"]["concurrency"] == 20 and a["run"]["model"] == "jev-latest" and a["run"]["systemone_path"] == "/v1/systemone"
    assert a["latency"]["histogram"][1] == {"label": "250–500ms", "count": 1}
    assert a["strongest"][0] == 1 and a["weakest"][0] == 3


def test_error_samples_redact_keys(tmp_path):
    a = analyze(make_run(tmp_path))
    sample = {e["kind"]: e for e in a["failures"]["errors"]}["HttpStop 401"]["sample"]
    assert "sk-***" in sample and "Bearer ***" in sample
    assert "abcdef123456" not in sample and "abc.def" not in sample


def test_analyze_tolerates_an_empty_run_dir(tmp_path):
    a = analyze(tmp_path / "missing", run_id="x")
    assert a["benchmarks"] == [] and a["areas"] == [] and a["failures"]["final_counts"] == {}
    assert a["throughput"]["counts"] == [] and a["throughput"]["wall_seconds"] is None
    assert a["status"]["state"] == "partial" and a["tokens"] is None


def test_status_reflects_early_stop_and_emitted_meta(tmp_path):
    run = make_run(tmp_path)
    (run / "status.json").write_text(json.dumps({"event": "stopped", "ended_abruptly": 1, "stop_reason": "http_429"}))
    results_json = tmp_path / "r1_results.json"
    results_json.write_text(json.dumps({"metrics": {"additional": {"engine": "http", "systemone_model": "jev-latest", "dashboard_model": "glm-5.3-flash", "task_range": "0-399", "token_usage": {"total_tokens": 99, "coverage_pct": 100.0}}}}))
    a = analyze(run, results_json)
    assert a["status"]["state"] == "stopped" and a["status"]["stop_reason"] == "http_429"
    assert a["run"]["dashboard_model"] == "glm-5.3-flash" and a["run"]["task_range"] == "0-399"
    assert a["tokens"]["total_tokens"] == 99


def test_run_health_and_timeline_caps_bars():
    spans = [(i * 60.0, i * 60.0 + 0.5) for i in range(24 * 60)]
    t = timeline(spans)
    assert t["bucket_seconds"] == 1800 and len(t["counts"]) <= 60 and sum(t["counts"]) == len(spans)
    final = {"a": {"status": "ok", "retries": ["http_502"]}, "b": {"status": "error", "retries": ["http_429"] * 3}, "c": {"status": "ok", "retries": []}}
    health = run_health(final, spans)
    assert health["attempts"] == 1440 and health["retried_rows"] == 2 and health["retries_exhausted"] == 1
    assert health["requests_per_minute"] == round(60 * 1440 / (1439 * 60 + 0.5), 1)


def test_read_attempts_ignores_a_half_written_last_line(tmp_path):
    final, spans = read_attempts(make_run(tmp_path) / "results.jsonl")
    assert len(spans) == 6 and "half-writ" not in final


def test_html_is_dashboard_safe_and_small_at_suite_scale(tmp_path):
    page = render_html(analyze(make_run(tmp_path, benchmarks=45)))
    head = page.split("</head>")[0]
    assert page.startswith("<!DOCTYPE html>") and "<style>" in head
    for banned in ("<script", "<svg", "href=", "<link", "<iframe"):
        assert banned not in page.lower()
    assert len(page) < 200_000
    assert "41.7" in page and "Tools &amp; Automation" in page and "Bench45" in page


def test_html_escapes_untrusted_text(tmp_path):
    a = analyze(make_run(tmp_path))
    a["benchmarks"][0]["dataset"] = "<img src=x onerror=alert(1)>"
    a["failures"]["errors"][0]["sample"] = "<b>boom</b>"
    page = render_html(a)
    assert "<img" not in page and "<b>boom" not in page and "&lt;img" in page


def test_html_shows_stop_reason_and_empty_sections(tmp_path):
    run = make_run(tmp_path)
    (run / "status.json").write_text(json.dumps({"ended_abruptly": 1, "stop_reason": "http_429"}))
    assert "stopped: http_429" in render_html(analyze(run))
    empty = render_html(analyze(tmp_path / "missing", run_id="x"))
    assert "No benchmark scores" in empty and "No timed requests" in empty


def test_markdown_and_csv_cover_every_benchmark(tmp_path):
    a = analyze(make_run(tmp_path, benchmarks=45))
    md = render_md(a)
    assert sum(line.startswith("| Bench") and not line.startswith("| Benchmark") for line in md.splitlines()) == 45
    rows = list(csv.reader(io.StringIO(benchmarks_csv(a))))
    assert rows[0] == list(CSV_COLUMNS) and len(rows) == 46
    assert rows[3][CSV_COLUMNS.index("flags")] == "below chance;low coverage"


def test_write_reports_and_main_write_four_files(tmp_path):
    run = make_run(tmp_path)
    paths = write_reports(analyze(run, run_id="r1"), tmp_path / "out")
    assert sorted(p.name for p in paths) == sorted([HTML_FILE, MD_FILE, JSON_FILE, CSV_FILE])
    assert json.loads((tmp_path / "out" / JSON_FILE).read_text(encoding="utf-8"))["run_id"] == "r1"
    main(["--run-dir", str(run), "--out-dir", str(tmp_path / "cli"), "--run-id", "r2"])
    assert json.loads((tmp_path / "cli" / JSON_FILE).read_text(encoding="utf-8"))["run_id"] == "r2"
