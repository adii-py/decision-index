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
