# Parallel runner, 429 backoff, and run reports — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a dashboard run send up to 20 Decision Index requests at once, retry rate limits and gateway errors with backoff, and write a self-contained HTML report (plus MD/JSON/CSV) next to the results JSON.

**Architecture:**
- `runner.run` keeps one main-thread writer and hands each row's engine call to a `ThreadPoolExecutor` worker. The in-flight window is bounded by `concurrency`.
- Retries happen inside the worker, so each attempt is timed on its own and a worker holds its slot while it backs off.
- `analysis.py` turns a run directory into plain data. `report.py` renders that data into dashboard-safe HTML/MD/CSV/JSON. `run.sh` calls it after the results JSON is written, as a non-fatal step.

**Tech Stack:**
- Python ≥3.10, stdlib `concurrent.futures`;
- httpx (already a dependency), pytest;
- bash (`run.sh`).

**Spec:** `docs/superpowers/specs/2026-10-12-parallel-runner-design.md`

**Test command used below:** `PYTEST` means `python -m pytest -q -p no:cacheprovider` run from the repo root, with an interpreter that has `httpx`, `huggingface_hub` and `pytest`.
- On the dashboard image that's `.venv/bin/python`.
- In this session it's `/private/tmp/claude-502/-Users-vaibhav-nohwar-tempPush/224b45fa-c9e1-47da-8a5c-86c4a0122e74/scratchpad/venv/bin/python`.

## Global Constraints

- No new dependencies (`pyproject.toml` stays `huggingface_hub`, `httpx`).
- `concurrency`:
  - `input_param.json` range 1–20, default 20;
  - `run.sh` default 20;
  - `cli.py run` default 1.
  - Values below 1 are rejected by `runner.run`.
- `delay_s`: a pause after each request, applied per worker, outside the timed window. Default 0 in `input_param.json` and `run.sh`; the CLI default stays 0.
- Retries:
  - `RETRY_LIMIT = 3`, `RETRY_BACKOFF_S = (5, 10, 20)`, `RETRY_JITTER_S = 1.0`, `Retry-After` honoured and capped at 60s.
  - Retried: HTTP 429/502/503/504, `ConnectError`, `RemoteProtocolError`.
  - Never retried: `ReadTimeout`, 401, 403.
- After retries run out: an http-engine 429 stops the run with `http_429`; a 5xx stays `error` and counts toward 3-consecutive-errors.
- `total_wall_ms` / `model_request_wall_ms` / `started_utc` describe the final attempt only. A retried row carries `retries: [reason, …]`, one entry per attempt that was retried.
- Report files in `output/`: `decision_index_report.html`, `decision_index_report.md`, `decision_index_analysis.json`, `decision_index_benchmarks.csv`.
- HTML constraints:
  - no `<script`, `<svg`, `href=`, `<link`, `<iframe`;
  - CSS only inside `<style>` in `<head>` or `style=` attributes;
  - under 200,000 characters at 45 benchmarks.
- Reporting never fails a run: `run.sh` uses `|| log_warn`, and `analyze` returns empty sections for missing inputs.
- Secrets: error samples in reports redact `sk-…` and `Bearer …` tokens.
- Do not `git commit`. The user authorises commits explicitly; every task ends with a test checkpoint instead.

## Review Focus

1. **A run killed mid-write** leaves a half line at the end of `results.jsonl`. Analysis and `run_health` must ignore it, as `read_jsonl(complete_lines_only=True)` does. Test in Task 4.
2. **A 401 body can echo the API key** (LiteLLM "Received API Key = sk-…"). Report samples must redact it. Test in Task 4.
3. **Dataset names or error text containing HTML** must be escaped in the report, never rendered. Test in Task 5.
4. **The dashboard may pass `concurrency` as `20.0` or junk.** `run.sh` must normalise it to an integer or fall back to 1 with a warning, never crash. Test in Task 6.
5. **Resume appends a second line for a retried `run_id`.** Status counts and the error taxonomy must use the last line per `run_id`, while throughput counts every attempt. Test in Task 4.

---

## File Structure

| File | Responsibility |
|---|---|
| `decision_index/engines/base.py` | `Engine.thread_safe = False` default |
| `decision_index/engines/http.py` | `HttpStop.retry_after`; `HttpSystemOne.thread_safe = True` |
| `decision_index/engines/grid.py` | `GridChat.thread_safe = True` |
| `decision_index/runner.py` | Bounded thread pool, single writer, retry/backoff |
| `decision_index/analysis.py` (new) | Run dir → plain-data analysis dict; `read_attempts`, `run_health`, `timeline` |
| `decision_index/report.py` (new) | Analysis → HTML/MD/CSV/JSON files; `python -m decision_index.report` |
| `decision_index/dashboard_emit.py` | `additional.concurrency`, `additional.run_health` |
| `decision_index/cli.py` | `run --concurrency` |
| `run.sh`, `input_param.json` | Flag, defaults, report step |
| `docs/dashboard.md`, `README.md` | Sizing and report notes |
| `tests/test_report_and_engine.py`, `tests/test_runner.py`, `tests/test_report.py` (new), `tests/test_dashboard_contract.py` | Tests |

---

### Task 1: Engine contract — `thread_safe` flag and `HttpStop.retry_after`

**Files:**
- Modify: `decision_index/engines/base.py` (class `Engine`)
- Modify: `decision_index/engines/http.py:21-27` (`HttpStop`), `:101-157` (`HttpSystemOne`)
- Modify: `decision_index/engines/grid.py:69-71` (`GridChat` class attrs)
- Test: `tests/test_report_and_engine.py`

**Interfaces:**
- Produces:
  - `Engine.thread_safe: bool` (False by default; True on `HttpSystemOne` and `GridChat`);
  - `HttpStop(status_code, message="", retry_after=None)` with attribute `.retry_after: str | None`, the raw `Retry-After` header.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_report_and_engine.py`)

```python
def test_network_engines_are_marked_thread_safe():
    from decision_index.engines.base import Engine, RandomEngine
    from decision_index.engines.grid import GridChat
    from decision_index.engines.http import HttpSystemOne

    assert Engine.thread_safe is False and RandomEngine.thread_safe is False
    assert HttpSystemOne.thread_safe is True and GridChat.thread_safe is True


def test_systemone_429_carries_retry_after():
    import httpx

    from decision_index.engines.http import HttpStop, HttpSystemOne

    engine = HttpSystemOne(base_url="https://example.test", model="jev-latest")
    engine.client = httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"retry-after": "7"}, text="slow down")),
    )
    with pytest.raises(HttpStop) as caught:
        engine("state", {"q": {"type": "choice", "criteria": {"a": "a", "b": "b"}}})
    assert caught.value.status_code == 429 and caught.value.retry_after == "7"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTEST tests/test_report_and_engine.py -k "thread_safe or retry_after"`
Expected: FAIL with `AttributeError: type object 'Engine' has no attribute 'thread_safe'`.

- [ ] **Step 3: Implement**

In `decision_index/engines/base.py`, class `Engine`, add after the `latency = …` line:

```python
    thread_safe = False
```

In `decision_index/engines/http.py`, replace class `HttpStop` with:

```python
class HttpStop(RuntimeError):
    """HTTP response that should end the run early but keep partial results."""

    def __init__(self, status_code, message="", retry_after=None):
        self.status_code = int(status_code)
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code}: {message[:500]}")
```

In `HttpSystemOne`, add `thread_safe = True` under `name = "http"`, and in `__call__` replace `raise HttpStop(r.status_code, r.text)` with:

```python
            raise HttpStop(r.status_code, r.text, retry_after=r.headers.get("retry-after"))
```

In `decision_index/engines/grid.py`, class `GridChat`, add `thread_safe = True` under `name = "grid"`.

- [ ] **Step 4: Run to verify pass**

Run: `PYTEST tests/test_report_and_engine.py`
Expected: all pass.

- [ ] **Step 5: Checkpoint (no commit)** — `PYTEST` (the full suite) is green. Do not commit.

---

### Task 2: Parallel runner — bounded thread pool with a single writer

**Files:**
- Modify: `decision_index/runner.py` (whole `run` function; new imports)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `Engine.thread_safe` (Task 1).
- Produces:
  - `run(..., concurrency=1)`;
  - `environment.json["concurrency"]`, the effective value;
  - nested helpers `attempt_once(row_index, e, payload) -> (result: dict, failure: Exception | None)` and `attempt(row_index, row) -> dict`, which Task 3 changes.

- [ ] **Step 1: Write the failing tests** (add imports at the top of `tests/test_runner.py`, and the rest at the end)

Imports to add (keep the existing `gzip`, `json`, `run`, `load_results`):

```python
import collections
import threading
import time

import pytest

from decision_index import runner
from decision_index.engines.base import Engine
from decision_index.engines.http import HttpStop
```

Append:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTEST tests/test_runner.py`
Expected: the new tests FAIL with `TypeError: run() got an unexpected keyword argument 'concurrency'`. The existing `test_run_resumes_and_keeps_row_format` passes.

- [ ] **Step 3: Implement** — in `decision_index/runner.py`:

Add the import:

```python
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
```

Replace the whole `run` function with:

```python
def run(
    engine_name,
    engine_options,
    rows_path,
    out_dir,
    limit=None,
    row_start=None,
    row_end=None,
    delay_s=0.0,
    logs_dir=None,
    compact=False,
    resume=True,
    warm=True,
    seed=C.RUN_SEED,
    corpus_sha256=None,
    halt_on_device_error=True,
    log=print,
    keep=None,
    concurrency=1,
):
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    logs_path = Path(logs_dir) if logs_dir else None
    if logs_path:
        logs_path.mkdir(parents=True, exist_ok=True)

    def event(**kw):
        record = {"time": stamp(), **kw}
        log(dumps(record))
        atomic_json(out / "status.json", record, indent=None)

    try:
        import torch

        torch.manual_seed(seed)
    except ImportError:
        pass
    t = time.perf_counter()
    event(event="loading", engine=engine_name)
    engine = load_engine(engine_name, **engine_options)
    engine.synchronize()
    if concurrency > 1 and not getattr(engine, "thread_safe", False):
        log(dumps({"time": stamp(), "event": "warning", "engine": engine_name, "message": f"{engine_name} is not thread-safe; running serially instead of concurrency={concurrency}"}))
        concurrency = 1
    atomic_json(
        out / "environment.json",
        {
            "engine": engine_name,
            "engine_options": engine_options,
            "model_source": engine.provenance,
            **engine.runtime(),
            "loaded_seconds": time.perf_counter() - t,
            "frozen_corpus_sha256": corpus_sha256,
            "rows_path": [str(p) for p in rows_path] if isinstance(rows_path, (list, tuple)) else str(rows_path),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "latency": engine.latency,
            "row_start": row_start,
            "row_end": row_end,
            "delay_s": delay_s,
            "concurrency": concurrency,
        },
    )
    if warm:
        engine.warmup()
        engine.synchronize()
    event(event="ready", engine=engine_name)
    previous = {}
    results_path = out / "results.jsonl"
    if resume and results_path.exists():
        for r in read_jsonl(results_path, complete_lines_only=True):
            previous[r["run_id"]] = r["status"]
        if previous:
            text = results_path.read_text(encoding="utf-8")
            if not text.endswith("\n"):
                results_path.write_text(text[: text.rfind("\n") + 1], encoding="utf-8")
    elif results_path.exists():
        results_path.unlink()
    counts = collections.Counter()
    start = time.perf_counter()
    finished = 0
    completed = set(previous)
    stop_reason = None
    last_errors = []
    ended_abruptly = 0
    consecutive_errors = 0

    def stop_now(reason, error=None):
        nonlocal stop_reason, ended_abruptly
        stop_reason = reason
        ended_abruptly = 1
        if error:
            last_errors.append(error)
            if len(last_errors) > 5:
                last_errors.pop(0)

    def attempt_once(row_index, e, payload):
        # Worker thread: one engine call, timed on its own. Touches no shared run state.
        failure = None
        t = time.perf_counter()
        engine.synchronize()
        result = {**e, "started_utc": stamp(), "engine": engine_name, "row_index": row_index}
        if not compact:
            result["payload"] = payload
        try:
            response, raw = engine(**payload)
            engine.synchronize()
            validate(payload["questions"], response)
            result.update(status="ok", response=response)
            if not compact:
                result["raw_output"] = raw
        except HttpStop as exc:
            failure = exc
            result.update(status="error", error=str(exc), exception="HttpStop", http_status=exc.status_code)
        except NativeAbstention as exc:
            result.update(status="abstained", error=str(exc))
            if not compact:
                result["raw_output"] = exc.raw
        except Unsupported as exc:
            result.update(status="unsupported", error=str(exc))
        except Exception as exc:
            failure = exc
            result.update(status="error", error=str(exc), exception=type(exc).__name__, traceback=traceback.format_exc())
        elapsed = (time.perf_counter() - t) * 1000
        result.update(completed_utc=stamp(), total_wall_ms=elapsed, model_request_wall_ms=elapsed)
        return result, failure

    def attempt(row_index, row):
        payload = {"state": row["state"], "questions": row["questions"]}
        result, _ = attempt_once(row_index, row["_evaluation"], payload)
        if delay_s:
            time.sleep(delay_s)
        return result

    def record(result, logf):
        # Main thread only: the single writer for results.jsonl, per-case logs and stop decisions.
        nonlocal finished, consecutive_errors
        rid = result["run_id"]
        if result.get("exception") == "HttpStop":
            stop_now(f"http_{result['http_status']}", {"run_id": rid, "status_code": result["http_status"], "error": result["error"][:500]})
        elif result["status"] == "error":
            consecutive_errors += 1
            last_errors.append({"run_id": rid, "exception": result["exception"], "error": result["error"][:500]})
            if len(last_errors) > 5:
                last_errors.pop(0)
            if consecutive_errors >= 3:
                stop_now("consecutive_errors", last_errors[-1])
        else:
            consecutive_errors = 0
            last_errors.clear()
        logf.write(dumps(result) + "\n")
        logf.flush()
        counts[result["status"]] += 1
        finished += 1
        completed.add(rid)
        if logs_path:
            case_path = logs_path / f"{result['row_index']:06d}_{_safe_name(rid)}.json"
            atomic_json(case_path, result, indent=None)
            with (logs_path / "ledger.jsonl").open("a", encoding="utf-8") as ledger:
                ledger.write(
                    dumps(
                        {
                            "row_index": result["row_index"],
                            "run_id": rid,
                            "catalog_id": result.get("catalog_id"),
                            "status": result["status"],
                            "total_wall_ms": result["total_wall_ms"],
                        }
                    )
                    + "\n"
                )
        if finished % 10 == 0:
            event(event="progress", engine=engine_name, completed=len(completed), counts=dict(counts), elapsed_seconds=round(time.perf_counter() - start, 1))
        if halt_on_device_error and (result.get("exception") in ("OutOfMemoryError", "AcceleratorError") or "device-side assert" in result.get("error", "")):
            stop_now("device_error", {"run_id": rid, "error": result.get("error")})
        if counts["error"] >= 5 and counts["ok"] == 0 and finished >= 5:
            stop_now("no_success", {"error": "five failed requests without a success"})

    with results_path.open("a", encoding="utf-8") as logf, ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="decision-index") as pool:
        in_flight = set()

        def collect():
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            in_flight.difference_update(done)
            for future in sorted(done, key=lambda f: f.result()["row_index"]):
                record(future.result(), logf)

        submitted = 0
        for row_index, row in enumerate(iter_rows(rows_path, keep)):
            if row_end is not None and row_index > row_end:
                break
            if row_start is not None and row_index < row_start:
                continue
            rid = row["_evaluation"]["run_id"]
            if rid in previous and previous[rid] != "error":
                completed.add(rid)
                continue
            while len(in_flight) >= concurrency and not ended_abruptly:
                collect()
            if ended_abruptly:
                break
            in_flight.add(pool.submit(attempt, row_index, row))
            submitted += 1
            if limit and submitted >= limit:
                break
        while in_flight:
            collect()
    engine.close()
    attempted = counts["ok"] + counts["error"] + counts["unsupported"] + counts["abstained"]
    ok_rate = (counts["ok"] / attempted) if attempted else 0.0
    if finished == 0 and len(completed) == 0:
        event(event="failed", engine=engine_name, completed=0, counts=dict(counts), error="run never started")
        raise RuntimeError("Run never started")
    if ended_abruptly:
        final = dict(
            event="stopped",
            engine=engine_name,
            completed=len(completed),
            counts=dict(counts),
            elapsed_seconds=round(time.perf_counter() - start, 1),
            ended_abruptly=1,
            stop_reason=stop_reason,
            last_errors=last_errors,
            attempted=attempted,
            ok_rate=round(ok_rate, 4),
        )
    else:
        final = dict(event="complete", engine=engine_name, completed=len(completed), counts=dict(counts), elapsed_seconds=round(time.perf_counter() - start, 1), attempted=attempted, ok_rate=round(ok_rate, 4))
    event(**final)
    return final
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTEST tests/test_runner.py`
Expected: all 8 tests pass.

- [ ] **Step 5: Checkpoint (no commit)** — the full `PYTEST` suite is green.

---

### Task 3: Retry and backoff for 429 / 5xx / dropped connections

**Files:**
- Modify: `decision_index/runner.py` (module constants and helpers; the `attempt` helper; the generic `except` in `attempt_once`)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `HttpStop.retry_after` (Task 1); `attempt_once` / `attempt` (Task 2).
- Produces:
  - module constants `RETRY_LIMIT`, `RETRY_BACKOFF_S`, `RETRY_JITTER_S`, `RETRY_AFTER_CAP_S`;
  - functions `http_status(exc) -> int | None`, `retry_reason(exc) -> str | None`, `parse_retry_after(value) -> float | None`, `retry_wait(exc, retry: int) -> float`;
  - result rows gain `retries: list[str]` (only when retried) and `http_status` for `HTTPStatusError`.
  - Task 4 reads `retries` and `http_status`.

- [ ] **Step 1: Write the failing tests** (add `import httpx` to the imports of `tests/test_runner.py`, then append)

```python
@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(runner, "RETRY_BACKOFF_S", (0, 0, 0))
    monkeypatch.setattr(runner, "RETRY_JITTER_S", 0.0)


def http_error(status, headers=None):
    request = httpx.Request("POST", "https://example.test/v1/systemone")
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
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTEST tests/test_runner.py -k "retr or timeout or gateway"`
Expected: FAIL — `AttributeError: module 'decision_index.runner' has no attribute 'RETRY_BACKOFF_S'` (fixture) and `RETRY_LIMIT`.

- [ ] **Step 3: Implement** — in `decision_index/runner.py`:

Add `import random` to the imports. Then add after the imports:

```python
# Transient failures are retried inside the worker, which keeps its slot while it waits, so a
# rate-limited key throttles the whole pool instead of being hammered. Each attempt is timed on
# its own: backoff never reaches total_wall_ms.
RETRY_LIMIT = 3
RETRY_BACKOFF_S = (5, 10, 20)
RETRY_JITTER_S = 1.0
RETRY_AFTER_CAP_S = 60
RETRY_HTTP_STATUSES = frozenset({429, 502, 503, 504})
RETRY_TRANSPORT_ERRORS = frozenset({"ConnectError", "RemoteProtocolError"})
```

Add these module-level functions after `_safe_name`:

```python
def http_status(exc):
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def retry_reason(exc):
    status = http_status(exc)
    if status in RETRY_HTTP_STATUSES:
        return f"http_{status}"
    if type(exc).__name__ in RETRY_TRANSPORT_ERRORS:
        return type(exc).__name__
    return None


def parse_retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_S)


def retry_wait(exc, retry):
    header = getattr(exc, "retry_after", None)
    if header is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        header = headers.get("retry-after") if headers is not None else None
    seconds = parse_retry_after(header)
    if seconds is not None:
        return seconds
    return RETRY_BACKOFF_S[min(retry, len(RETRY_BACKOFF_S) - 1)] + random.uniform(0, RETRY_JITTER_S)
```

In `attempt_once`, replace the generic `except Exception as exc:` branch with:

```python
        except Exception as exc:
            failure = exc
            result.update(status="error", error=str(exc), exception=type(exc).__name__, traceback=traceback.format_exc())
            status = http_status(exc)
            if status is not None:
                result["http_status"] = status
```

Replace `attempt` with:

```python
    def attempt(row_index, row):
        payload = {"state": row["state"], "questions": row["questions"]}
        retries = []
        while True:
            result, failure = attempt_once(row_index, row["_evaluation"], payload)
            reason = retry_reason(failure) if failure is not None else None
            if reason is None or len(retries) >= RETRY_LIMIT:
                break
            retries.append(reason)
            time.sleep(retry_wait(failure, len(retries) - 1))
        if retries:
            result["retries"] = retries
        if delay_s:
            time.sleep(delay_s)
        return result
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTEST tests/test_runner.py`
Expected: all 13 tests pass.

- [ ] **Step 5: Checkpoint (no commit)** — the full `PYTEST` suite is green.

---

### Task 4: `analysis.py` — run directory to report data

**Files:**
- Create: `decision_index/analysis.py`
- Test: `tests/test_report.py` (new)

**Interfaces:**
- Consumes:
  - `results.jsonl` rows: `run_id`, `catalog_id`, `dataset`, `status`, `total_wall_ms`, `started_utc`, `completed_utc`, `exception`, `http_status`, `error`, `retries` (Task 3);
  - `scores.json` (`pipeline.score_run_v02` shape);
  - `environment.json` (`concurrency` from Task 2);
  - `status.json`;
  - the emitted `<id>_results.json` (`metrics.additional`).
- Produces:
  - `analyze(run_dir, results_json=None, run_id=None) -> dict`, with keys `run_id, generated_utc, run, status, headline, areas, benchmarks, strongest, weakest, failures, latency, throughput, tokens, notes`;
  - `read_attempts(results_path) -> (dict[run_id, slim_row], list[(start_epoch|None, end_epoch|None)])`;
  - `run_health(final, spans) -> {"attempts", "wall_seconds", "requests_per_minute", "retried_rows", "retries_exhausted"}`;
  - `timeline(spans) -> {"bucket_seconds", "counts", "first_start_utc"}`.
- Benchmark row keys: `catalog_id, dataset, area, area_label, metric, score, chance, raw, skill, coverage, in_index, requests, answered, errors, unsupported, abstained, pending, median_ms, p95_ms, flags`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_report.py`:

```python
import json

from decision_index.analysis import analyze, read_attempts, run_health, timeline


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
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTEST tests/test_report.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_index.analysis'`.

- [ ] **Step 3: Implement** — create `decision_index/analysis.py`:

```python
"""Numbers behind the Decision Index run reports: scored files plus one pass over results.jsonl.

Pure data. A missing or unreadable input leaves its section empty instead of raising, because
reporting runs after the results JSON is written and must never fail a scored run.
"""

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from decision_index.scoring.metrics import percentile
from decision_index.suite.io import read_jsonl

LATENCY_BUCKETS_MS = (
    (250, "<250ms"),
    (500, "250–500ms"),
    (1000, "0.5–1s"),
    (2000, "1–2s"),
    (5000, "2–5s"),
    (10000, "5–10s"),
    (30000, "10–30s"),
    (math.inf, ">30s"),
)
TIMELINE_STEPS_S = (10, 30, 60, 120, 300, 600, 900, 1800, 3600)
TIMELINE_MAX_BARS = 60
LOW_COVERAGE = 0.95
SAMPLE_CHARS = 200
EXTREMES = 8
SLOWEST = 10
_STATUS_IN_MESSAGE = re.compile(r"'(\d{3}) [^']*'")
_SECRET = re.compile(r"(sk-|Bearer\s+)[A-Za-z0-9._\-]+")


def load_json(path):
    path = Path(path) if path else None
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _number(value):
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else value


def _epoch(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _catalog(key):
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def _slim(row):
    error = _SECRET.sub(r"\1***", str(row.get("error") or ""))
    status = _number(row.get("http_status"))
    if status is None and row.get("status") == "error":
        match = _STATUS_IN_MESSAGE.search(error)
        status = int(match.group(1)) if match else None
    return {
        "catalog_id": row.get("catalog_id"),
        "dataset": row.get("dataset"),
        "status": row.get("status") or "unknown",
        "ms": _number(row.get("total_wall_ms")),
        "exception": row.get("exception"),
        "http_status": status,
        "error": error[:SAMPLE_CHARS],
        "retries": list(row.get("retries") or []),
    }


def read_attempts(results_path):
    """Return (last row per run_id, (start, end) epoch per attempt line).

    Resume appends a new line when an errored row is retried, so the last line per run_id is its
    final status, while every line is an attempt that took wall time.
    """
    final, spans = {}, []
    path = Path(results_path) if results_path else None
    if path is None or not path.is_file():
        return final, spans
    for row in read_jsonl(path, complete_lines_only=True):
        final[row.get("run_id")] = _slim(row)
        spans.append((_epoch(row.get("started_utc")), _epoch(row.get("completed_utc"))))
    return final, spans


def _wall(spans):
    starts = [s for s, _ in spans if s is not None]
    ends = [e for _, e in spans if e is not None]
    if not starts or not ends or max(ends) <= min(starts):
        return None, None
    return min(starts), max(ends)


def run_health(final, spans):
    first, last = _wall(spans)
    wall = None if first is None else last - first
    retried = [r for r in final.values() if r.get("retries")]
    return {
        "attempts": len(spans),
        "wall_seconds": None if wall is None else round(wall, 1),
        "requests_per_minute": None if not wall else round(60 * len(spans) / wall, 1),
        "retried_rows": len(retried),
        "retries_exhausted": sum(1 for r in retried if r.get("status") != "ok"),
    }


def timeline(spans):
    first, last = _wall(spans)
    if first is None:
        return {"bucket_seconds": None, "counts": [], "first_start_utc": None}
    wall = last - first
    step = next((s for s in TIMELINE_STEPS_S if wall / s <= TIMELINE_MAX_BARS), math.ceil(wall / TIMELINE_MAX_BARS))
    counts = [0] * min(TIMELINE_MAX_BARS, int(wall // step) + 1)
    for _, end in spans:
        if end is not None:
            counts[min(len(counts) - 1, int((end - first) // step))] += 1
    return {"bucket_seconds": step, "counts": counts, "first_start_utc": datetime.fromtimestamp(first, timezone.utc).isoformat(timespec="seconds")}


def latency(final):
    ok = sorted(r["ms"] for r in final.values() if r["status"] == "ok" and r["ms"] is not None)
    histogram = [{"label": label, "count": 0} for _, label in LATENCY_BUCKETS_MS]
    by_benchmark = defaultdict(list)
    for r in final.values():
        if r["status"] == "ok" and r["ms"] is not None:
            histogram[next(i for i, (edge, _) in enumerate(LATENCY_BUCKETS_MS) if r["ms"] < edge)]["count"] += 1
            by_benchmark[r["catalog_id"]].append(r["ms"])
    summary = {
        "p50_ms": percentile(ok, 0.5),
        "p95_ms": percentile(ok, 0.95),
        "mean_ms": statistics.fmean(ok) if ok else None,
        "max_ms": ok[-1] if ok else None,
        "histogram": histogram,
    }
    per = {n: {"p50_ms": percentile(v, 0.5), "p95_ms": percentile(v, 0.95)} for n, v in by_benchmark.items()}
    return summary, per


def failures(final):
    kinds = {}
    for r in final.values():
        if r["status"] != "error":
            continue
        kind = (r["exception"] or "error") + (f" {r['http_status']}" if r["http_status"] else "")
        entry = kinds.setdefault(kind, {"kind": kind, "count": 0, "benchmarks": set(), "sample": r["error"]})
        entry["count"] += 1
        if r["dataset"]:
            entry["benchmarks"].add(r["dataset"])
    retried = [r for r in final.values() if r["retries"]]
    return {
        "final_counts": dict(Counter(r["status"] for r in final.values())),
        "errors": sorted(({**e, "benchmarks": sorted(e["benchmarks"])} for e in kinds.values()), key=lambda e: (-e["count"], e["kind"])),
        "retries": {
            "rows": len(retried),
            "recovered": sum(1 for r in retried if r["status"] == "ok"),
            "still_failed": sum(1 for r in retried if r["status"] != "ok"),
            "by_reason": dict(Counter(reason for r in retried for reason in r["retries"])),
        },
    }


def benchmark_rows(scores, per_latency, datasets):
    native = scores.get("benchmarks") or {}
    index = scores.get("index_benchmarks") or {}
    area_of = {}
    for area in scores.get("areas") or []:
        for n in area.get("benchmarks") or []:
            area_of[_catalog(n)] = (area.get("id"), area.get("label") or area.get("id"))
    rows = []
    for n in sorted({_catalog(k) for k in (*native, *index)} - {None}):
        b, ib = native.get(str(n)) or {}, index.get(str(n)) or {}
        requests, answered = _number(b.get("requests")), _number(b.get("answered"))
        coverage = _number(ib.get("coverage"))
        if coverage is None and requests:
            coverage = (answered or 0) / requests
        in_index = bool(ib.get("in_index", b.get("in_index", False)))
        skill = _number(ib.get("skill"))
        flags = []
        if not answered:
            flags.append("no answers")
        elif in_index and skill == 0:
            flags.append("below chance")
        if answered and coverage is not None and coverage < LOW_COVERAGE:
            flags.append("low coverage")
        if not in_index:
            flags.append("not in index")
        lat = per_latency.get(n) or {}
        area_id, area_label = area_of.get(n, (None, None))
        rows.append(
            {
                "catalog_id": n,
                "dataset": b.get("dataset") or datasets.get(n) or f"#{n}",
                "area": area_id,
                "area_label": area_label,
                "metric": b.get("metric"),
                "score": _number(b.get("score")),
                "chance": _number(ib.get("random", b.get("chance"))),
                "raw": _number(ib.get("raw")),
                "skill": skill,
                "coverage": coverage,
                "in_index": in_index,
                "requests": requests,
                "answered": answered,
                "errors": _number(b.get("errors")),
                "unsupported": _number(b.get("unsupported")),
                "abstained": _number(b.get("abstained")),
                "pending": _number(b.get("pending")),
                "median_ms": _number(b.get("median_ms")) if b.get("median_ms") is not None else lat.get("p50_ms"),
                "p95_ms": lat.get("p95_ms"),
                "flags": flags,
            }
        )
    return rows


def analyze(run_dir, results_json=None, run_id=None):
    run_dir = Path(run_dir)
    scores = load_json(run_dir / "scores.json")
    env = load_json(run_dir / "environment.json")
    status = load_json(run_dir / "status.json")
    meta = (load_json(results_json).get("metrics") or {}).get("additional") or {}
    options = env.get("engine_options") or {}
    final, spans = read_attempts(run_dir / "results.jsonl")
    lat, per = latency(final)
    datasets = {r["catalog_id"]: r["dataset"] for r in final.values() if r["dataset"]}
    benchmarks = benchmark_rows(scores, per, datasets)
    ranked = sorted((b for b in benchmarks if b["in_index"] and b["answered"] and b["skill"] is not None), key=lambda b: (-b["skill"], b["catalog_id"]))
    lat["slowest"] = [b["catalog_id"] for b in sorted((b for b in benchmarks if b["median_ms"] is not None), key=lambda b: -b["median_ms"])[:SLOWEST]]
    suite = scores.get("suite") or {}
    expected = (_number(suite.get("scoreable")) or 0) + (_number(suite.get("added_requests")) or 0) or None
    completed = _number(scores.get("completed"))
    if status.get("ended_abruptly"):
        state = "stopped"
    elif scores.get("complete"):
        state = "complete"
    else:
        state = "partial"
    return {
        "run_id": run_id or run_dir.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": {
            "engine": meta.get("engine") or env.get("engine"),
            "model": meta.get("systemone_model") or meta.get("model") or options.get("model"),
            "dashboard_model": meta.get("dashboard_model") or None,
            "edition": scores.get("edition") or meta.get("edition"),
            "base_url": meta.get("base_url") or options.get("base_url"),
            "systemone_path": options.get("systemone_path"),
            "timeout_s": options.get("timeout"),
            "concurrency": env.get("concurrency", meta.get("concurrency")),
            "delay_s": env.get("delay_s", meta.get("delay_s")),
            "task_range": meta.get("task_range") or None,
            "corpus_sha256": env.get("frozen_corpus_sha256"),
        },
        "status": {
            "state": state,
            "stop_reason": status.get("stop_reason") or None,
            "completed": completed,
            "expected": expected,
            "completion": (completed / expected) if completed is not None and expected else None,
        },
        "headline": {
            "decision_index": _number(scores.get("decision_index")),
            "raw_index": _number(scores.get("raw_index")),
            "scores": scores.get("scores") or {},
            "counts": scores.get("counts") or {},
        },
        "areas": [{k: a.get(k) for k in ("id", "label", "skill", "raw", "coverage", "n")} for a in scores.get("areas") or []],
        "benchmarks": benchmarks,
        "strongest": [b["catalog_id"] for b in ranked[:EXTREMES]],
        "weakest": [b["catalog_id"] for b in ranked[::-1][:EXTREMES]],
        "failures": failures(final),
        "latency": lat,
        "throughput": {**run_health(final, spans), **timeline(spans)},
        "tokens": meta.get("token_usage"),
        "notes": {"index": scores.get("note")},
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTEST tests/test_report.py`
Expected: 6 tests pass.

- [ ] **Step 5: Checkpoint (no commit)** — the full `PYTEST` suite is green.

---

### Task 5: `report.py` — dashboard-safe HTML, Markdown, CSV and JSON

**Files:**
- Create: `decision_index/report.py`
- Test: `tests/test_report.py` (append)

**Interfaces:**
- Consumes: the `analyze()` dict (Task 4).
- Produces:
  - `render_html(a) -> str`, `render_md(a) -> str`, `benchmarks_csv(a) -> str`;
  - `write_reports(a, out_dir) -> list[Path]`;
  - constants `HTML_FILE`, `MD_FILE`, `JSON_FILE`, `CSV_FILE`, `CSV_COLUMNS`;
  - `main(argv)` for `python -m decision_index.report --run-dir D --out-dir O [--results-json F] [--run-id ID]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_report.py`)

```python
import csv
import io

from decision_index.report import CSV_COLUMNS, CSV_FILE, HTML_FILE, JSON_FILE, MD_FILE, benchmarks_csv, main, render_html, render_md, write_reports


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
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTEST tests/test_report.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_index.report'`.

- [ ] **Step 3: Implement** — create `decision_index/report.py`:

```python
"""Write the Decision Index run reports next to the dashboard results JSON.

The eval-ops dashboard previews output/ files in a sandboxed iframe after DOMPurify (html profile):
scripts, links, SVG and remote assets are stripped, and anything past 200,000 characters is cut.
So the page is static: all styling lives in one <style> in <head>, every chart is a CSS bar, and
every value from the run is HTML-escaped.
"""

import argparse
import csv
import html
import io
import json
import os
from pathlib import Path

from decision_index.analysis import analyze

HTML_FILE = "decision_index_report.html"
MD_FILE = "decision_index_report.md"
JSON_FILE = "decision_index_analysis.json"
CSV_FILE = "decision_index_benchmarks.csv"
CSV_COLUMNS = ("catalog_id", "dataset", "area", "metric", "score", "chance", "raw", "skill", "coverage", "in_index", "requests", "answered", "errors", "unsupported", "abstained", "pending", "median_ms", "p95_ms", "flags")

STYLE = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#5b6470;--line:#e3e6ea;--card:#f6f7f9;--accent:#2f6fde;--ok:#1f8a4c;--warn:#b7791f;--bad:#c53030}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e6e8eb;--muted:#9aa3ad;--line:#2a3038;--card:#1c2026;--accent:#6b9cf0;--ok:#48bb78;--warn:#ecc94b;--bad:#fc8181}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
h3{font-size:12px;margin:12px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.sub,.note,.empty{color:var(--muted)}.note{font-size:12px;margin:0 0 8px}
.pill{display:inline-block;margin-top:8px;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;background:var(--card);border:1px solid var(--line)}
.pill.complete{color:var(--ok)}.pill.partial{color:var(--warn)}.pill.stopped{color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;margin-top:18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile .label{font-size:12px;color:var(--muted)}.tile .value{font-size:20px;font-weight:650;margin:2px 0}.tile .note{margin:0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.w{width:20%;min-width:90px}
tr.group td{background:var(--card);font-weight:600}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin-top:5px}
.bar span{display:block;height:100%;background:var(--accent)}
.flag{display:inline-block;margin-left:6px;padding:0 6px;border-radius:4px;font-size:11px;background:var(--card);border:1px solid var(--line);color:var(--muted)}
.flag.below-chance,.flag.no-answers{color:var(--bad)}.flag.low-coverage{color:var(--warn)}
.id{color:var(--muted);font-size:11px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}
.spark{display:flex;align-items:flex-end;gap:2px;height:90px;padding:4px;background:var(--card);border:1px solid var(--line);border-radius:6px}
.spark span{flex:1;min-height:1px;background:var(--accent);border-radius:2px 2px 0 0}
.sample{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--muted);word-break:break-word}
table.kv th{width:200px}
details summary{cursor:pointer;font-weight:600}
ol{margin:0;padding-left:20px}
"""

BENCH_HEAD = (
    '<thead><tr><th>Benchmark</th><th>Metric</th><th class="n">Score</th><th class="n">Chance</th>'
    '<th>Skill</th><th class="n"></th><th class="n">Coverage</th><th class="n">Answered</th>'
    '<th class="n">Errors</th><th class="n">Unsup.</th><th class="n">p50</th></tr></thead>'
)


def _esc(value):
    return html.escape("—" if value is None else str(value))


def pct(x, digits=1):
    return "—" if x is None else f"{100 * x:.{digits}f}%"


def num(x, digits=1):
    if x is None:
        return "—"
    if isinstance(x, int) and not isinstance(x, bool):
        return f"{x:,}"
    return f"{x:,.{digits}f}"


def ms(x):
    if x is None:
        return "—"
    return f"{x / 1000:.2f}s" if x >= 1000 else f"{x:.0f}ms"


def duration(seconds):
    if seconds is None:
        return "—"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


def bar(fraction):
    width = 0.0 if fraction is None else max(0.0, min(1.0, fraction)) * 100
    return f'<div class="bar"><span style="width:{width:.1f}%"></span></div>'


def status_text(a):
    st = a["status"]
    if st["state"] == "stopped":
        return f"stopped: {st.get('stop_reason') or 'unknown'}"
    if st["state"] == "partial":
        return f"partial {pct(st.get('completion'))}"
    return "complete"


def _section(title, body, note=None):
    return f"<section><h2>{_esc(title)}</h2>" + (f'<p class="note">{_esc(note)}</p>' if note else "") + body + "</section>"


def _tile(label, value, note=""):
    return f'<div class="tile"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div><div class="note">{_esc(note)}</div></div>'


def _header(a):
    run = a["run"]
    sub = " · ".join(x for x in (f"engine {run['engine']}" if run.get("engine") else None, f"edition {run['edition']}" if run.get("edition") else None, f"run {a['run_id']}") if x)
    return (
        f'<header><h1>Decision Index — {_esc(run.get("model") or "unknown model")}</h1>'
        f'<p class="sub">{_esc(sub)}</p><span class="pill {_esc(a["status"]["state"])}">{_esc(status_text(a))}</span></header>'
    )


def _tiles(a):
    h, st, t, lat = a["headline"], a["status"], a["throughput"], a["latency"]
    tokens = a.get("tokens") or {}
    counts = h.get("counts") or {}
    tiles = [
        _tile("Decision Index", num(h.get("decision_index")), "chance-corrected, 0–100"),
        _tile("Raw index", num(h.get("raw_index")), "before chance correction"),
        _tile("Breadth skill", num((h.get("scores") or {}).get("breadth_skill")), "penalises weak areas"),
        _tile("Coverage", pct(st.get("completion")), f"{num(st.get('completed'))} of {num(st.get('expected'))} requests"),
        _tile("Answered", num(counts.get("ok", 0)), f"{num(counts.get('error', 0))} errors · {num(counts.get('unsupported', 0))} unsupported · {num(counts.get('abstained', 0))} abstained"),
        _tile("Wall time", duration(t.get("wall_seconds")), f"{num(t.get('requests_per_minute'), 0)} requests/min"),
        _tile("Latency p50 / p95", f"{ms(lat.get('p50_ms'))} / {ms(lat.get('p95_ms'))}", f"at concurrency {num(a['run'].get('concurrency'))}"),
        _tile("Tokens", num(tokens.get("total_tokens")), f"{num(tokens.get('coverage_pct'))}% of attempts measured" if tokens else "not reported"),
    ]
    return '<section class="tiles">' + "".join(tiles) + "</section>"


def _areas(a):
    if not a["areas"]:
        return _section("Areas", '<p class="empty">No area scores in scores.json.</p>')
    rows = "".join(
        f'<tr><td>{_esc(x.get("label"))}</td><td class="w">{bar(x.get("skill"))}</td><td class="n">{pct(x.get("skill"))}</td>'
        f'<td class="n">{pct(x.get("raw"))}</td><td class="n">{pct(x.get("coverage"))}</td><td class="n">{num(x.get("n"))}</td></tr>'
        for x in a["areas"]
    )
    head = '<thead><tr><th>Area</th><th>Skill</th><th class="n"></th><th class="n">Raw</th><th class="n">Coverage</th><th class="n">Benchmarks</th></tr></thead>'
    return _section("Areas", f"<table>{head}<tbody>{rows}</tbody></table>", "Skill is chance-corrected: 0 means no better than guessing.")


def _benchmark_row(b):
    flags = "".join(f'<span class="flag {_esc(f.replace(" ", "-"))}">{_esc(f)}</span>' for f in b["flags"])
    return (
        f'<tr><td>{_esc(b["dataset"])} <span class="id">#{_esc(b["catalog_id"])}</span>{flags}</td>'
        f'<td>{_esc(b.get("metric"))}</td><td class="n">{num(b.get("score"), 3)}</td><td class="n">{num(b.get("chance"), 3)}</td>'
        f'<td class="w">{bar(b.get("skill"))}</td><td class="n">{pct(b.get("skill"))}</td><td class="n">{pct(b.get("coverage"))}</td>'
        f'<td class="n">{num(b.get("answered"))}/{num(b.get("requests"))}</td><td class="n">{num(b.get("errors"))}</td>'
        f'<td class="n">{num(b.get("unsupported"))}</td><td class="n">{ms(b.get("median_ms"))}</td></tr>'
    )


def _by_skill(items):
    return sorted(items, key=lambda b: (b["skill"] is None, -(b["skill"] or 0), b["catalog_id"]))


def _benchmarks(a):
    if not a["benchmarks"]:
        return _section("Benchmarks", '<p class="empty">No benchmark scores in scores.json.</p>')
    body = ""
    for area in a["areas"]:
        members = [b for b in a["benchmarks"] if b["area"] == area.get("id")]
        if members:
            body += f'<tr class="group"><td colspan="11">{_esc(area.get("label"))} · skill {pct(area.get("skill"))}</td></tr>'
            body += "".join(_benchmark_row(b) for b in _by_skill(members))
    rest = [b for b in a["benchmarks"] if b["area"] is None]
    if rest:
        body += '<tr class="group"><td colspan="11">Not in an index area</td></tr>' + "".join(_benchmark_row(b) for b in _by_skill(rest))
    note = "Score is the benchmark's native metric on complete, supported case groups; chance is its random baseline; skill is chance-corrected and coverage-weighted."
    return _section("Benchmarks", f"<table>{BENCH_HEAD}<tbody>{body}</tbody></table>", note)


def _extremes(a):
    if not a["strongest"]:
        return ""
    by_id = {b["catalog_id"]: b for b in a["benchmarks"]}

    def listing(ids):
        return "<ol>" + "".join(f'<li>{_esc(by_id[i]["dataset"])} — {pct(by_id[i]["skill"])}</li>' for i in ids if i in by_id) + "</ol>"

    body = f'<div class="cols"><div><h3>Strongest</h3>{listing(a["strongest"])}</div><div><h3>Weakest</h3>{listing(a["weakest"])}</div></div>'
    return _section("Strongest and weakest", body, "In-index benchmarks with answers, ranked by chance-corrected skill.")


def _failures(a):
    f = a["failures"]
    counts = " · ".join(f"{_esc(k)} {num(v)}" for k, v in sorted(f["final_counts"].items()))
    parts = [f"<p>Final status per request: {counts or 'no results.jsonl'}</p>"]
    if f["errors"]:
        rows = "".join(
            f'<tr><td>{_esc(e["kind"])}</td><td class="n">{num(e["count"])}</td>'
            f'<td>{_esc(", ".join(e["benchmarks"][:6]) + (" …" if len(e["benchmarks"]) > 6 else ""))}</td>'
            f'<td class="sample">{_esc(e["sample"])}</td></tr>'
            for e in f["errors"]
        )
        parts.append(f'<table><thead><tr><th>Error</th><th class="n">Rows</th><th>Benchmarks</th><th>Sample</th></tr></thead><tbody>{rows}</tbody></table>')
    else:
        parts.append('<p class="empty">No failed requests.</p>')
    unsupported = sorted((b for b in a["benchmarks"] if b.get("unsupported")), key=lambda b: -b["unsupported"])
    if unsupported:
        parts.append("<h3>Unsupported by benchmark</h3><p>" + " · ".join(f'{_esc(b["dataset"])} {num(b["unsupported"])}' for b in unsupported) + "</p>")
    r = f["retries"]
    reasons = " · ".join(f"{_esc(k)} {num(v)}" for k, v in sorted(r["by_reason"].items()))
    parts.append(
        f'<h3>Retries</h3><p>{num(r["rows"])} requests retried: {num(r["recovered"])} recovered, {num(r["still_failed"])} still failed.'
        + (f" Retried attempts by reason: {reasons}." if reasons else "")
        + "</p>"
    )
    return _section("Failures", "".join(parts))


def _latency(a):
    lat = a["latency"]
    top = max((h["count"] for h in lat["histogram"]), default=0)
    dist = "".join(f'<tr><td>{_esc(h["label"])}</td><td class="w">{bar(h["count"] / top if top else 0)}</td><td class="n">{num(h["count"])}</td></tr>' for h in lat["histogram"])
    by_id = {b["catalog_id"]: b for b in a["benchmarks"]}
    slow = "".join(f'<tr><td>{_esc(by_id[i]["dataset"])}</td><td class="n">{ms(by_id[i]["median_ms"])}</td><td class="n">{ms(by_id[i]["p95_ms"])}</td></tr>' for i in lat.get("slowest", []) if i in by_id)
    summary = f'p50 {ms(lat.get("p50_ms"))} · p95 {ms(lat.get("p95_ms"))} · mean {ms(lat.get("mean_ms"))} · max {ms(lat.get("max_ms"))}'
    body = (
        f'<p>{_esc(summary)}</p><div class="cols"><div><h3>Distribution</h3><table><tbody>{dist}</tbody></table></div>'
        f'<div><h3>Slowest benchmarks</h3><table><thead><tr><th>Benchmark</th><th class="n">p50</th><th class="n">p95</th></tr></thead><tbody>{slow}</tbody></table></div></div>'
    )
    note = f"Successful requests only, timed per final attempt (retry backoff excluded), measured at concurrency {a['run'].get('concurrency') or 1}."
    return _section("Latency", body, note)


def _throughput(a):
    t = a["throughput"]
    counts = t.get("counts") or []
    if not counts:
        return _section("Throughput", '<p class="empty">No timed requests in results.jsonl.</p>')
    top = max(counts) or 1
    bars = "".join(f'<span style="height:{100 * c / top:.1f}%"></span>' for c in counts)
    run = a["run"]
    facts = f'{num(t.get("attempts"))} attempts in {duration(t.get("wall_seconds"))} · {num(t.get("requests_per_minute"), 0)} requests/min · concurrency {num(run.get("concurrency"))} · delay_s {num(run.get("delay_s"), 2)}'
    stop = a["status"].get("stop_reason")
    if stop:
        facts += f" · stopped: {stop}"
    note = f'Completed requests per {duration(t.get("bucket_seconds"))} from {t.get("first_start_utc")}. Gaps are stalls: gateway outages or rate-limit backoff.'
    return _section("Throughput", f'<p>{_esc(facts)}</p><div class="spark">{bars}</div><p class="note">{_esc(note)}</p>')


def _config(a):
    run = a["run"]
    items = (
        ("Engine", run.get("engine")),
        ("Model", run.get("model")),
        ("Dashboard model", run.get("dashboard_model")),
        ("Edition", run.get("edition")),
        ("Base URL", run.get("base_url")),
        ("Systemone path", run.get("systemone_path")),
        ("Request timeout (s)", run.get("timeout_s")),
        ("Concurrency", run.get("concurrency")),
        ("delay_s", run.get("delay_s")),
        ("Task range", run.get("task_range") or "full suite"),
        ("Corpus sha256", run.get("corpus_sha256")),
    )
    rows = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in items)
    return _section("Run configuration", f'<table class="kv"><tbody>{rows}</tbody></table>')


def _method(a):
    note = a["notes"].get("index")
    return (
        "<section><details><summary>How these numbers are computed</summary>"
        "<p>Each benchmark is scored with its native metric on complete, supported case groups. Raw is that score times coverage. "
        "Skill is chance-corrected: clip((raw − chance) / (1 − chance), 0, 1), so 0 means no better than the benchmark's random baseline. "
        "The Decision Index aggregates area skills as defined by the edition's panel; the raw index applies the same aggregation without chance correction.</p>"
        "<p>Statuses: ok = answered and validated; error = request failed after retries; unsupported = the engine reported the case exceeds its capacity; "
        "abstained = the model chose its native abstention outcome; pending = never attempted.</p>"
        + (f"<p>{_esc(note)}</p>" if note else "")
        + "</details></section>"
    )


def render_html(a):
    title = f"Decision Index · {a['run'].get('model') or 'unknown model'}"
    return "".join(
        (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>',
            _esc(title),
            "</title><style>",
            STYLE,
            "</style></head><body>",
            _header(a),
            _tiles(a),
            _areas(a),
            _benchmarks(a),
            _extremes(a),
            _failures(a),
            _latency(a),
            _throughput(a),
            _config(a),
            _method(a),
            "</body></html>\n",
        )
    )


def _md(value):
    return ("—" if value is None else str(value)).replace("|", "\\|").replace("\n", " ")


def render_md(a):
    run, h, st, t, lat, f = a["run"], a["headline"], a["status"], a["throughput"], a["latency"], a["failures"]
    lines = [
        f"# Decision Index — {_md(run.get('model') or 'unknown model')}",
        "",
        f"Run `{a['run_id']}` · engine {_md(run.get('engine'))} · edition {_md(run.get('edition'))} · **{status_text(a)}**",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Decision Index | {num(h.get('decision_index'))} |",
        f"| Raw index | {num(h.get('raw_index'))} |",
        f"| Breadth skill | {num((h.get('scores') or {}).get('breadth_skill'))} |",
        f"| Coverage | {pct(st.get('completion'))} ({num(st.get('completed'))} of {num(st.get('expected'))}) |",
        f"| Wall time | {duration(t.get('wall_seconds'))} ({num(t.get('requests_per_minute'), 0)} requests/min) |",
        f"| Latency p50 / p95 | {ms(lat.get('p50_ms'))} / {ms(lat.get('p95_ms'))} at concurrency {num(run.get('concurrency'))} |",
        "",
    ]
    if a["areas"]:
        lines += ["## Areas", "", "| Area | Skill | Raw | Coverage | Benchmarks |", "|---|---:|---:|---:|---:|"]
        lines += [f"| {_md(x.get('label'))} | {pct(x.get('skill'))} | {pct(x.get('raw'))} | {pct(x.get('coverage'))} | {num(x.get('n'))} |" for x in a["areas"]]
        lines.append("")
    if a["benchmarks"]:
        lines += ["## Benchmarks", "", "| Benchmark | Area | Metric | Score | Chance | Skill | Coverage | Answered | Errors | p50 | Flags |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
        lines += [
            f"| {_md(b['dataset'])} | {_md(b['area_label'])} | {_md(b.get('metric'))} | {num(b.get('score'), 3)} | {num(b.get('chance'), 3)} | {pct(b.get('skill'))} | "
            f"{pct(b.get('coverage'))} | {num(b.get('answered'))}/{num(b.get('requests'))} | {num(b.get('errors'))} | {ms(b.get('median_ms'))} | {_md(', '.join(b['flags']))} |"
            for b in a["benchmarks"]
        ]
        lines.append("")
    lines += ["## Failures", ""]
    if f["errors"]:
        lines += ["| Error | Rows | Benchmarks | Sample |", "|---|---:|---|---|"]
        lines += [f"| {_md(e['kind'])} | {num(e['count'])} | {_md(', '.join(e['benchmarks'][:6]))} | {_md(e['sample'])} |" for e in f["errors"]]
    else:
        lines.append("No failed requests.")
    r = f["retries"]
    lines += ["", f"Retries: {num(r['rows'])} requests retried, {num(r['recovered'])} recovered, {num(r['still_failed'])} still failed.", ""]
    lines += ["## Run configuration", ""] + [f"- {k}: {_md(v)}" for k, v in run.items()]
    return "\n".join(lines) + "\n"


def benchmarks_csv(a):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for b in a["benchmarks"]:
        writer.writerow([";".join(b["flags"]) if c == "flags" else ("" if b.get(c) is None else b.get(c)) for c in CSV_COLUMNS])
    return buffer.getvalue()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_reports(analysis, out_dir):
    out = Path(out_dir)
    files = {
        HTML_FILE: render_html(analysis),
        MD_FILE: render_md(analysis),
        JSON_FILE: json.dumps(analysis, indent=2, ensure_ascii=False) + "\n",
        CSV_FILE: benchmarks_csv(analysis),
    }
    for name, text in files.items():
        _write(out / name, text)
    return [out / name for name in files]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="decision_index.report")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--results-json")
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    for path in write_reports(analyze(args.run_dir, args.results_json, args.run_id), args.out_dir):
        print(f"[report] wrote {path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTEST tests/test_report.py`
Expected: 11 tests pass.

- [ ] **Step 5: Checkpoint (no commit)** — the full `PYTEST` suite is green.

---

### Task 6: Wiring — CLI flag, `dashboard_emit`, `run.sh`, `input_param.json`, docs

**Files:**
- Modify: `decision_index/cli.py` (`cmd_run`, `run` sub-parser)
- Modify: `decision_index/dashboard_emit.py` (`metrics_document`, `emit`, `main`)
- Modify: `run.sh` (header comment, defaults, flag parse, validation, info line, run and emit args, report step)
- Modify: `input_param.json` (`delay_s`, new `concurrency`)
- Modify: `docs/dashboard.md:101-107`, `README.md:205`
- Test: `tests/test_dashboard_contract.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: `run(..., concurrency)` (Task 2); `read_attempts`, `run_health` (Task 4); `python -m decision_index.report` (Task 5).
- Produces:
  - `additional.concurrency: int | None`, `additional.run_health: dict` (when `results.jsonl` has rows);
  - `run.sh --concurrency N`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:

```python
def test_cli_run_accepts_concurrency():
    from decision_index.cli import build_parser

    assert build_parser().parse_args(["run", "--engine", "random", "--concurrency", "8"]).concurrency == 8
    assert build_parser().parse_args(["run", "--engine", "random"]).concurrency == 1
```

Append to `tests/test_dashboard_contract.py`:

```python
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
```

In `test_input_param_schema_is_json`, append:

```python
    assert by_name["concurrency"]["default"] == 20
    assert by_name["concurrency"]["constraints"] == {"min": 1, "max": 20}
    assert by_name["delay_s"]["default"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTEST tests/test_dashboard_contract.py tests/test_runner.py -k "concurrency or run_health"`
Expected: FAIL — `KeyError: 'concurrency'` (emit and schema), the unknown flag warning in `run.sh`, and argparse rejecting `--concurrency`.

- [ ] **Step 3: Implement**

`decision_index/cli.py`:
- in `cmd_run`'s call to `run(...)`, add `concurrency=args.concurrency,` after `delay_s=args.delay_s,`;
- in the `run` sub-parser, after the `--delay-s` line, add:

```python
    p.add_argument("--concurrency", type=int, default=1, help="requests in flight at once (thread-safe engines: http, grid)")
```

`decision_index/dashboard_emit.py`:
- add the import `from decision_index.analysis import read_attempts, run_health`;
- change the signature to `def metrics_document(scores, meta, usage, health=None):`;
- in the `additional = {...}` literal, after `"delay_s": meta.get("delay_s"),`, add `"concurrency": meta.get("concurrency"),`;
- before `if usage:`, add:

```python
    if health:
        additional["run_health"] = health
```

In `emit`, replace the `document = metrics_document(...)` line with:

```python
    health = None
    if results_path:
        try:
            final, spans = read_attempts(results_path)
            health = run_health(final, spans) if spans else None
        except (OSError, ValueError):
            health = None
    document = metrics_document(scores, merge_stop_meta(meta, load_status(status_path)), report if attempts else None, health)
```

In `main`:
- add `parser.add_argument("--concurrency", default="")` after `--delay-s`;
- add `concurrency = int(float(args.concurrency)) if args.concurrency != "" else None` after the `delay_s = …` line;
- add `"concurrency": concurrency,` after `"delay_s": delay_s,` in the meta dict.

`run.sh`:
1. Replace header lines 8–9 with:

```bash
# Requests run --concurrency at a time (default 20). HTTP 429/502/503/504 and dropped
# connections are retried 3 times with backoff; after that 401/403/429 or 3 consecutive
# request errors end the run early with partial results (ended_abruptly=1). Only a run
# that never starts is FAILED. Reports (decision_index_report.html and siblings) are
# written next to the results JSON and never fail the run.
```

2. Change `DELAY_S="0.5"` to `DELAY_S="0"`, and add `CONCURRENCY="20"` on the next line.

3. In the flag `case`, after the `--delay-s)` line, add:

```bash
        --concurrency) CONCURRENCY="${2:-}"; shift 2 ;;
```

4. After `ORIGIN="$(normalize_base_url "$BASE_URL")"`, add:

```bash
CONCURRENCY="${CONCURRENCY%%.*}"
if ! [[ "$CONCURRENCY" =~ ^[0-9]+$ ]] || [ "$CONCURRENCY" -lt 1 ]; then
    log_warn "concurrency '${CONCURRENCY}' is not a positive integer; using 1"
    CONCURRENCY=1
fi
```

5. In the `log_info "base_url=…"` line, change `delay_s=${DELAY_S} resume=` to `delay_s=${DELAY_S} concurrency=${CONCURRENCY} resume=`.

6. In the `"$PY" -m decision_index run \` block, after `--delay-s "$DELAY_S" \`, add `--concurrency "$CONCURRENCY" \`.

7. In `EMIT_ARGS=(…)`, after `--delay-s "$DELAY_S"`, add `--concurrency "$CONCURRENCY"`.

8. Insert the report step immediately before the existing `trap - EXIT` line (the line right after the `"$PY" "${EMIT_ARGS[@]}" || { … }` block):

```bash
log_step "Writing reports"
"$PY" -m decision_index.report \
    --run-dir "$RUN_DIR" \
    --results-json "$RESULTS_FILE" \
    --run-id "$EVAL_RUN_ID" \
    --out-dir "$OUTPUT_ROOT" \
    || log_warn "report generation failed (non-fatal); the results JSON is unaffected"

```

`input_param.json`: replace the `delay_s` field and add `concurrency` after it:

```json
    {
      "name": "delay_s",
      "type": "number",
      "label": "Pause after each request (seconds)",
      "description": "Pause after each request, per parallel worker. 0 = no pause; use concurrency to set throughput.",
      "required": false,
      "default": 0,
      "constraints": {"min": 0, "max": 60}
    },
    {
      "name": "concurrency",
      "type": "number",
      "label": "Parallel requests",
      "description": "Requests in flight at once (1-20). 20 finishes the full suite in about 1.2h at ~570ms latency (set Runtime to 3h); lower it if the server or key cannot take the load. HTTP 429/502/503/504 are retried 3 times with backoff.",
      "required": false,
      "default": 20,
      "constraints": {"min": 1, "max": 20}
    },
```

In `docs/dashboard.md`:
- in the `additional` row of the metrics table, change `token usage, run meta` to `token usage, \`concurrency\`, \`run_health\`, run meta`;
- replace the line `Clear \`task_range\`. Expect ~150k requests; use \`delay_s\` 0.5 (≈120 RPM).` with:

```markdown
Clear `task_range`. Expect ~150k requests. `concurrency` (default 20, max 20) sets parallel requests: at ~570ms latency 20 finishes in ~1.2h (set Runtime to 3h), 10 in ~2.4h, 1 in ~24h. HTTP 429/502/503/504 and dropped connections are retried 3 times with backoff (`Retry-After`, else 5s, 10s, 20s).

Reports in `output/` (non-fatal, written after the results JSON): `decision_index_report.html` (index, areas, per-benchmark skill and coverage, failures, latency, throughput timeline, run configuration), `decision_index_report.md`, `decision_index_analysis.json`, `decision_index_benchmarks.csv`, plus `token_usage.*`.
```

In `README.md` line 205, after `…writes \`${EVAL_RUNNER_OUTPUT_DIR}/${eval_run_id}_results.json\`.`, insert:

```markdown
 `--concurrency` (default 20) sets parallel requests, and the HTML/MD/JSON/CSV run reports land next to the results JSON.
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTEST`
Expected: the full suite passes (69 existing plus all new tests). Also run `bash -n run.sh` and expect no output.

- [ ] **Step 5: Checkpoint (no commit)** — the full suite is green, `bash -n run.sh` is clean, and `python -c 'import json; json.load(open("input_param.json"))'` succeeds.

---

## Done when

- The full `PYTEST` suite is green.
- **Real-data sanity check.** The per-case JSON files under `Logs Decision Index/artifacts (3)/repo/logs/decision-index/6141db1c-…/` are full result rows. Concatenate them, sorted by file name, into `<scratchpad>/real-run/results.jsonl`, then run `python -m decision_index.report --run-dir <scratchpad>/real-run --out-dir <scratchpad>/real-out`. The HTML should render, with:
  - a throughput timeline of about 56 requests/min;
  - an error table led by `HTTPStatusError 502`;
  - empty score sections, because `scores.json` isn't in those logs.
- Dashboard smoke (spec §10) is the user's step after review.
