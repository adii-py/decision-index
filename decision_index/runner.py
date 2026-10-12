import collections
import hashlib
import json
import random
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from decision_index import constants as C
from decision_index.engines import NativeAbstention, Unsupported, load_engine, validate
from decision_index.engines.http import HttpStop
from decision_index.suite.io import atomic_json, dumps, read_jsonl

# Transient failures are retried inside the worker, which keeps its slot while it waits, so a
# rate-limited key throttles the whole pool instead of being hammered. Each attempt is timed on
# its own: backoff never reaches total_wall_ms.
RETRY_LIMIT = 3
RETRY_BACKOFF_S = (5, 10, 20)
RETRY_JITTER_S = 1.0
RETRY_AFTER_CAP_S = 60
RETRY_HTTP_STATUSES = frozenset({429, 502, 503, 504})
RETRY_TRANSPORT_ERRORS = frozenset({"ConnectError", "RemoteProtocolError"})


def stamp():
    return datetime.now(timezone.utc).isoformat()


def iter_rows(rows_path, keep=None):
    paths = rows_path if isinstance(rows_path, (list, tuple)) else [rows_path]
    for path in paths:
        for row in read_jsonl(path):
            if keep is None or keep(row["_evaluation"]):
                yield row


def _safe_name(value):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value))


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
            status = http_status(exc)
            if status is not None:
                result["http_status"] = status
        elapsed = (time.perf_counter() - t) * 1000
        result.update(completed_utc=stamp(), total_wall_ms=elapsed, model_request_wall_ms=elapsed)
        return result, failure

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
