import collections
import gzip
import json
import threading
import time

import httpx
import pytest

from decision_index import runner
from decision_index.engines.base import Engine
from decision_index.engines.http import HttpStop
from decision_index.runner import run
from decision_index.scoring.report import load_results


def write_rows(path, n):
    with gzip.open(path, "wt") as f:
        for i in range(n):
            row = {"id": f"x:{i}", "family": "x", "split": "t", "state": {"i": i}, "questions": {"q1": {"type": "choice", "instructions": "pick", "criteria": {"A": "a", "B": "b"}}}, "expected": {"q1": "A"}, "_evaluation": {"run_id": f"24:x:{i}", "catalog_id": 24, "dataset": "MMLU", "group_id": f"x:{i}", "track": "x", "source_path": "x", "payload_sha256": "0", "proxy_tokens": 1, "benchmark_origin": "test"}}
            f.write(json.dumps(row) + "\n")


def test_run_resumes_and_keeps_row_format(tmp_path):
    rows = tmp_path / "rows.jsonl.gz"
    write_rows(rows, 5)
    out = tmp_path / "run"
    first = run("random", {"seed": 3}, rows, out, limit=2, log=lambda s: None)
    assert first["completed"] == 2
    second = run("random", {"seed": 3}, rows, out, log=lambda s: None)
    assert second["completed"] == 5
    results = load_results(out / "results.jsonl")
    assert len(results) == 5
    raw = [json.loads(l) for l in (out / "results.jsonl").open()]
    keys = {"run_id", "catalog_id", "dataset", "group_id", "track", "source_path", "payload_sha256", "proxy_tokens", "benchmark_origin", "payload", "started_utc", "engine", "status", "response", "raw_output", "completed_utc", "total_wall_ms", "model_request_wall_ms"}
    assert keys <= set(raw[0])
    assert (out / "status.json").exists() and (out / "environment.json").exists()


class FakeHttpEngine(Engine):
    """Thread-safe stand-in for an HTTP engine: records overlap and per-thread request starts."""

    name = "fake"
    thread_safe = True

    def __init__(self, sleep=0.02, fail=None):
        super().__init__()
        self.sleep = sleep
        self.fail = {k: list(v) for k, v in (fail or {}).items()}
        self.lock = threading.Lock()
        self.active = self.peak = self.calls = 0
        self.starts = []

    def warmup(self):
        pass

    def __call__(self, state, questions):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
            self.starts.append((threading.current_thread().name, time.monotonic()))
            pending = self.fail.get(state["i"])
            exc = pending.pop(0) if pending else None
        try:
            time.sleep(self.sleep)
            if exc:
                raise exc
            return {"answers": {"q1": {"type": "choice", "choice": "A", "probabilities": {"A": 1.0, "B": 0.0}}}}, None
        finally:
            with self.lock:
                self.active -= 1


def run_fake(monkeypatch, tmp_path, engine, n=12, **kw):
    rows = tmp_path / "rows.jsonl.gz"
    write_rows(rows, n)
    monkeypatch.setattr(runner, "load_engine", lambda name, **options: engine)
    out = tmp_path / "run"
    final = run("fake", {}, rows, out, log=lambda s: None, **kw)
    return final, out


def test_concurrent_run_overlaps_requests_and_writes_every_row(monkeypatch, tmp_path):
    engine = FakeHttpEngine()
    final, out = run_fake(monkeypatch, tmp_path, engine, n=12, concurrency=4, logs_dir=tmp_path / "logs")
    assert engine.peak == 4
    assert final["event"] == "complete" and final["completed"] == 12 and final["counts"] == {"ok": 12}
    assert set(load_results(out / "results.jsonl")) == {f"24:x:{i}" for i in range(12)}
    assert len((tmp_path / "logs" / "ledger.jsonl").read_text().splitlines()) == 12
    assert json.loads((out / "environment.json").read_text())["concurrency"] == 4


def test_twenty_workers_are_all_in_flight(monkeypatch, tmp_path):
    engine = FakeHttpEngine(sleep=0.05)
    final, _ = run_fake(monkeypatch, tmp_path, engine, n=60, concurrency=20)
    assert engine.peak == 20 and final["completed"] == 60


def test_engine_without_thread_safety_runs_serially_and_deterministically(tmp_path):
    rows = tmp_path / "rows.jsonl.gz"
    write_rows(rows, 8)
    serial = run("random", {"seed": 3}, rows, tmp_path / "a", log=lambda s: None)
    asked = run("random", {"seed": 3}, rows, tmp_path / "b", concurrency=4, log=lambda s: None)
    assert serial["counts"] == asked["counts"] == {"ok": 8}
    assert json.loads((tmp_path / "b" / "environment.json").read_text())["concurrency"] == 1
    a, b = load_results(tmp_path / "a" / "results.jsonl"), load_results(tmp_path / "b" / "results.jsonl")
    assert {k: v["response"] for k, v in a.items()} == {k: v["response"] for k, v in b.items()}


def test_http_stop_keeps_every_in_flight_result(monkeypatch, tmp_path):
    engine = FakeHttpEngine(sleep=0.02, fail={0: [HttpStop(401, "bad key")]})
    final, out = run_fake(monkeypatch, tmp_path, engine, n=40, concurrency=4)
    assert final["ended_abruptly"] == 1 and final["stop_reason"] == "http_401"
    written = (out / "results.jsonl").read_text().splitlines()
    assert len(written) == engine.calls < 40


def test_limit_is_exact_under_concurrency(monkeypatch, tmp_path):
    engine = FakeHttpEngine()
    final, out = run_fake(monkeypatch, tmp_path, engine, n=12, concurrency=4, limit=3)
    assert final["completed"] == 3 and engine.calls == 3
    assert len((out / "results.jsonl").read_text().splitlines()) == 3


def test_delay_applies_per_worker_and_is_not_latency(monkeypatch, tmp_path):
    engine = FakeHttpEngine(sleep=0)
    _, out = run_fake(monkeypatch, tmp_path, engine, n=8, concurrency=2, delay_s=0.05)
    by_thread = collections.defaultdict(list)
    for name, started in engine.starts:
        by_thread[name].append(started)
    gaps = [b - a for starts in by_thread.values() for a, b in zip(starts, starts[1:])]
    assert gaps and min(gaps) >= 0.045
    assert all(r["total_wall_ms"] < 40 for r in load_results(out / "results.jsonl").values())


def test_concurrency_below_one_is_rejected(tmp_path):
    rows = tmp_path / "rows.jsonl.gz"
    write_rows(rows, 1)
    with pytest.raises(ValueError, match="concurrency"):
        run("random", {}, rows, tmp_path / "run", concurrency=0, log=lambda s: None)


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(runner, "RETRY_BACKOFF_S", (0, 0, 0))
    monkeypatch.setattr(runner, "RETRY_JITTER_S", 0.0)


def http_error(status, headers=None):
    request = httpx.Request("POST", "https://example.test/v1/decisions")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"Server error '{status} Bad Gateway' for url '{request.url}'", request=request, response=response)


def raw_row(out, run_id):
    return next(json.loads(line) for line in (out / "results.jsonl").open() if json.loads(line)["run_id"] == run_id)


def test_rate_limit_is_retried_and_backoff_is_not_latency(monkeypatch, tmp_path):
    limited = [HttpStop(429, "slow down", retry_after="0.2"), HttpStop(429, "slow down", retry_after="0.2")]
    engine = FakeHttpEngine(sleep=0.01, fail={2: limited})
    final, out = run_fake(monkeypatch, tmp_path, engine, n=5, concurrency=2)
    assert final["event"] == "complete" and final["counts"] == {"ok": 5}
    row = raw_row(out, "24:x:2")
    assert row["status"] == "ok" and row["retries"] == ["http_429", "http_429"]
    assert row["total_wall_ms"] < 150


def test_rate_limit_stops_the_run_once_retries_are_exhausted(monkeypatch, tmp_path, no_backoff):
    limited = [HttpStop(429, "slow down") for _ in range(runner.RETRY_LIMIT + 1)]
    engine = FakeHttpEngine(sleep=0, fail={0: limited})
    final, out = run_fake(monkeypatch, tmp_path, engine, n=5)
    assert final["ended_abruptly"] == 1 and final["stop_reason"] == "http_429"
    assert engine.calls == runner.RETRY_LIMIT + 1
    assert raw_row(out, "24:x:0")["retries"] == ["http_429"] * runner.RETRY_LIMIT


def test_gateway_errors_are_retried_and_keep_their_status(monkeypatch, tmp_path, no_backoff):
    engine = FakeHttpEngine(sleep=0, fail={1: [http_error(502)], 3: [http_error(503)] * (runner.RETRY_LIMIT + 1)})
    final, out = run_fake(monkeypatch, tmp_path, engine, n=5)
    assert raw_row(out, "24:x:1")["status"] == "ok" and raw_row(out, "24:x:1")["retries"] == ["http_502"]
    failed = raw_row(out, "24:x:3")
    assert failed["status"] == "error" and failed["http_status"] == 503 and len(failed["retries"]) == runner.RETRY_LIMIT
    assert final["counts"] == {"ok": 4, "error": 1} and not final.get("ended_abruptly")


def test_read_timeout_is_not_retried(monkeypatch, tmp_path, no_backoff):
    timeout = httpx.ReadTimeout("The read operation timed out", request=httpx.Request("POST", "https://example.test"))
    engine = FakeHttpEngine(sleep=0, fail={1: [timeout]})
    _, out = run_fake(monkeypatch, tmp_path, engine, n=3)
    row = raw_row(out, "24:x:1")
    assert row["status"] == "error" and row["exception"] == "ReadTimeout" and "retries" not in row
    assert engine.calls == 3


def test_retry_after_is_parsed_and_capped(monkeypatch):
    monkeypatch.setattr(runner, "RETRY_JITTER_S", 0.0)
    assert runner.parse_retry_after("7") == 7.0
    assert runner.parse_retry_after("120") == runner.RETRY_AFTER_CAP_S == 60
    assert runner.parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert runner.parse_retry_after(None) is None and runner.parse_retry_after("-3") is None
    assert runner.retry_wait(HttpStop(429, retry_after="3"), 0) == 3.0
    assert runner.retry_wait(http_error(429, {"retry-after": "4"}), 0) == 4.0
    assert runner.retry_wait(HttpStop(429), 1) == runner.RETRY_BACKOFF_S[1]


def test_cli_run_accepts_concurrency():
    from decision_index.cli import build_parser

    assert build_parser().parse_args(["run", "--engine", "random", "--concurrency", "8"]).concurrency == 8
    assert build_parser().parse_args(["run", "--engine", "random"]).concurrency == 1
