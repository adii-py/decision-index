import collections
import hashlib
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from decision_index import constants as C
from decision_index.engines import NativeAbstention, Unsupported, load_engine, validate
from decision_index.engines.http import HttpStop
from decision_index.suite.io import atomic_json, dumps, read_jsonl


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
):
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
    row_index = -1
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

    with results_path.open("a", encoding="utf-8") as logf:
        for row in iter_rows(rows_path, keep):
            row_index += 1
            if row_end is not None and row_index > row_end:
                break
            if row_start is not None and row_index < row_start:
                continue
            e = row["_evaluation"]
            rid = e["run_id"]
            if rid in previous and previous[rid] != "error":
                completed.add(rid)
                continue
            if delay_s and finished > 0:
                time.sleep(delay_s)
            payload = {"state": row["state"], "questions": row["questions"]}
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
                consecutive_errors = 0
                last_errors.clear()
            except HttpStop as exc:
                result.update(status="error", error=str(exc), exception="HttpStop", http_status=exc.status_code)
                stop_now(f"http_{exc.status_code}", {"run_id": rid, "status_code": exc.status_code, "error": str(exc)[:500]})
            except NativeAbstention as exc:
                result.update(status="abstained", error=str(exc))
                if not compact:
                    result["raw_output"] = exc.raw
                consecutive_errors = 0
                last_errors.clear()
            except Unsupported as exc:
                result.update(status="unsupported", error=str(exc))
                consecutive_errors = 0
                last_errors.clear()
            except Exception as exc:
                result.update(status="error", error=str(exc), exception=type(exc).__name__, traceback=traceback.format_exc())
                consecutive_errors += 1
                last_errors.append({"run_id": rid, "exception": type(exc).__name__, "error": str(exc)[:500]})
                if len(last_errors) > 5:
                    last_errors.pop(0)
                if consecutive_errors >= 3:
                    stop_now("consecutive_errors", last_errors[-1])
            elapsed = (time.perf_counter() - t) * 1000
            result.update(completed_utc=stamp(), total_wall_ms=elapsed, model_request_wall_ms=elapsed)
            logf.write(dumps(result) + "\n")
            logf.flush()
            counts[result["status"]] += 1
            finished += 1
            completed.add(rid)
            if logs_path:
                case_path = logs_path / f"{row_index:06d}_{_safe_name(rid)}.json"
                atomic_json(case_path, result, indent=None)
                with (logs_path / "ledger.jsonl").open("a", encoding="utf-8") as ledger:
                    ledger.write(
                        dumps(
                            {
                                "row_index": row_index,
                                "run_id": rid,
                                "catalog_id": e.get("catalog_id"),
                                "status": result["status"],
                                "total_wall_ms": elapsed,
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
            if limit and finished >= limit:
                break
            if ended_abruptly:
                break
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
