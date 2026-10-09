"""Write the eval-runner results JSON from a Decision Index scores.json.

The runner marks a run FAILED unless ${EVAL_RUNNER_OUTPUT_DIR}/${eval_run_id}_results.json
exists and metrics.main.value is a number. This module is that writer.
"""

import argparse
import csv
import html
import json
import os
from pathlib import Path

from decision_index.suite.io import read_jsonl


def _num(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def load_scores(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text())


def attempt_usage(results_path):
    path = Path(results_path) if results_path else None
    attempts = []
    if path is None or not path.exists():
        return attempts
    for row in read_jsonl(path, complete_lines_only=True):
        usage = ((row.get("response") or {}).get("usage")) or {}
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        measured = isinstance(inp, int) and not isinstance(inp, bool) and isinstance(out, int) and not isinstance(out, bool)
        attempts.append({
            "run_id": row.get("run_id"),
            "status": row.get("status"),
            "measured": measured,
            "input_tokens": inp if measured else None,
            "output_tokens": out if measured else None,
        })
    return attempts


def token_report(attempts, input_price, output_price):
    observed = len(attempts)
    measured_rows = [a for a in attempts if a["measured"]]
    measured = len(measured_rows)
    input_tokens = sum(a["input_tokens"] for a in measured_rows) if measured else None
    output_tokens = sum(a["output_tokens"] for a in measured_rows) if measured else None
    prices = {}
    note = None
    if input_price is None and output_price is None:
        note = "token-only; no custom prices were set"
    elif input_price is None or output_price is None:
        missing = "input_token_price" if input_price is None else "output_token_price"
        note = "token-only; " + missing + " was not set, so custom cost was not computed"
    else:
        prices = {"input_token_price": input_price, "output_token_price": output_price}
        note = "custom cost uses measured tokens only and is a lower bound when coverage is incomplete"
    cost = None
    if prices and input_tokens is not None:
        cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    by_status = {}
    for row in attempts:
        bucket = by_status.setdefault(row.get("status") or "unknown", {"attempts": 0, "measured": 0, "input_tokens": 0, "output_tokens": 0})
        bucket["attempts"] += 1
        if row["measured"]:
            bucket["measured"] += 1
            bucket["input_tokens"] += row["input_tokens"]
            bucket["output_tokens"] += row["output_tokens"]
    return {
        "observed_attempts": observed,
        "measured_attempts": measured,
        "coverage_pct": round(100.0 * measured / observed, 2) if observed else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": None if input_tokens is None else input_tokens + output_tokens,
        "custom_cost_usd": cost,
        "prices": prices,
        "by_status": by_status,
        "note": note,
        "methodology": "One attempt is one Decision Index request. Token totals sum API usage on responses that reported both input and output tokens. Unmeasured attempts stay in the denominator and contribute no tokens. Per-request rows stay in the run directory results.jsonl, which is not uploaded.",
    }


def _flat_score_metrics(scores):
    """Headline scalars from scores.json for dashboard secondary columns (flat only)."""
    out = {}
    nested = scores.get("scores") or {}
    for key in ("balanced_skill", "balanced_raw", "breadth_skill"):
        if key in nested and not isinstance(nested[key], bool) and isinstance(nested[key], (int, float)):
            out[key] = float(nested[key])
    latency = scores.get("latency_ms") or {}
    if isinstance(latency, dict):
        for src, dst in (("median", "latency_median_ms"), ("p95", "latency_p95_ms"), ("mean", "latency_mean_ms")):
            if src in latency and not isinstance(latency[src], bool) and isinstance(latency[src], (int, float)):
                out[dst] = float(latency[src])
    if "coverage" in scores and not isinstance(scores["coverage"], bool) and isinstance(scores["coverage"], (int, float)):
        out["coverage"] = float(scores["coverage"])
    if scores.get("panel_id"):
        out["panel_id"] = str(scores["panel_id"])
    return out


def metrics_document(scores, meta, usage):
    index = scores.get("decision_index")
    if isinstance(index, bool) or not isinstance(index, (int, float)):
        raise ValueError("scores.json decision_index is not a number")
    raw = scores.get("raw_index")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raw = (scores.get("scores") or {}).get("balanced_raw")
    counts = scores.get("counts") or {}
    secondary = {
        "decision_index": float(index),
        "raw_index": _num(raw),
        "completed": _int(scores.get("completed")),
        "complete": 1 if scores.get("complete") else 0,
        "edition": str(scores.get("edition") or meta.get("edition") or ""),
    }
    secondary.update(_flat_score_metrics(scores))
    for key in ("ok", "error", "unsupported", "abstained"):
        if key in counts:
            secondary[key] = _int(counts[key])
    areas = []
    for area in scores.get("areas") or []:
        if isinstance(area, dict):
            areas.append({k: area.get(k) for k in ("id", "label", "skill", "raw", "coverage", "n", "benchmarks") if k in area})
    additional = {
        "status": "scored",
        "engine": meta.get("engine"),
        "model": meta.get("model"),
        "task_range": meta.get("task_range") or "",
        "row_start": meta.get("row_start"),
        "row_end": meta.get("row_end"),
        "range_note": meta.get("range_note") or "",
        "base_url": meta.get("base_url") or "",
        "delay_s": meta.get("delay_s"),
        "run_rc": meta.get("run_rc"),
        "ended_abruptly": meta.get("ended_abruptly") or 0,
        "stop_reason": meta.get("stop_reason") or "",
        "attempted_ok_rate": meta.get("attempted_ok_rate"),
        "last_errors": meta.get("last_errors") or [],
        "areas": areas,
        "counts": counts,
        "note": scores.get("note"),
        "methodology": {
            "grid": "Chat adapter: one completion per request, choice mass on the selected key. Not the board's /v1/systemone protocol.",
            "http": "POST /v1/systemone. The server owns the probability distribution.",
            "random": "Uniform baseline. A pipeline check, not a model score.",
        }.get(meta.get("engine"), "Engine " + str(meta.get("engine"))),
    }
    if isinstance(scores.get("latency_ms"), dict):
        additional["latency_ms"] = scores["latency_ms"]
    if isinstance(scores.get("scores"), dict):
        additional["scores"] = scores["scores"]
    if isinstance(scores.get("suite"), dict):
        additional["suite"] = scores["suite"]
    if scores.get("panel_id"):
        additional["panel_id"] = scores["panel_id"]
    if isinstance(scores.get("index_benchmarks"), dict):
        additional["index_benchmarks"] = scores["index_benchmarks"]
    if usage:
        additional["token_usage"] = {k: usage[k] for k in ("observed_attempts", "measured_attempts", "coverage_pct", "input_tokens", "output_tokens", "total_tokens", "custom_cost_usd", "note")}
    return {"metrics": {"main": {"name": "Decision Index", "value": float(index)}, "secondary": secondary, "additional": additional}}


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_token_artifacts(directory, report):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / "token_usage.json", json.dumps(report, indent=2) + "\n")
    rows = [
        ["observed_attempts", report["observed_attempts"]],
        ["measured_attempts", report["measured_attempts"]],
        ["coverage_pct", report["coverage_pct"]],
        ["input_tokens", report["input_tokens"]],
        ["output_tokens", report["output_tokens"]],
        ["total_tokens", report["total_tokens"]],
        ["custom_cost_usd", report["custom_cost_usd"]],
    ]
    csv_path = directory / "token_usage.csv"
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)
    os.replace(tmp, csv_path)
    lines = ["# Token usage", "", report["methodology"], "", "| metric | value |", "|---|---|"]
    for name, value in rows:
        lines.append("| " + name + " | " + ("—" if value is None else str(value)) + " |")
    if report.get("note"):
        lines.extend(["", report["note"]])
    atomic_write(directory / "token_usage.md", "\n".join(lines) + "\n")
    cells = "".join("<tr><td>" + html.escape(name) + "</td><td>" + html.escape("—" if value is None else str(value)) + "</td></tr>" for name, value in rows)
    page = "<!DOCTYPE html><meta charset=utf-8><title>Token usage</title><h1>Token usage</h1><p>" + html.escape(report["methodology"]) + "</p><table>" + cells + "</table><p>" + html.escape(report.get("note") or "") + "</p>\n"
    atomic_write(directory / "token_usage.html", page)


def load_status(path):
    path = Path(path) if path else None
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def merge_stop_meta(meta, status):
    if not status:
        return meta
    out = dict(meta)
    if status.get("ended_abruptly"):
        out["ended_abruptly"] = status.get("ended_abruptly")
        out["stop_reason"] = status.get("stop_reason") or ""
        out["last_errors"] = status.get("last_errors") or []
        if status.get("ok_rate") is not None:
            out["attempted_ok_rate"] = status.get("ok_rate")
    return out


def emit(scores_path, results_path, out_path, meta, input_price, output_price, token_dir, status_path=None):
    scores = load_scores(scores_path)
    attempts = attempt_usage(results_path)
    report = token_report(attempts, input_price, output_price)
    if token_dir and attempts:
        write_token_artifacts(token_dir, report)
    document = metrics_document(scores, merge_stop_meta(meta, load_status(status_path)), report if attempts else None)
    atomic_write(out_path, json.dumps(document, indent=2) + "\n")
    return document


def _optional_price(value):
    if value is None or value == "":
        return None
    return float(value)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="decision_index.dashboard_emit")
    parser.add_argument("--scores", required=True)
    parser.add_argument("--results-jsonl")
    parser.add_argument("--out", required=True)
    parser.add_argument("--token-dir")
    parser.add_argument("--model", default="")
    parser.add_argument("--engine", default="")
    parser.add_argument("--edition", default="")
    parser.add_argument("--task-range", default="")
    parser.add_argument("--row-start", default="")
    parser.add_argument("--row-end", default="")
    parser.add_argument("--range-note", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--delay-s", default="")
    parser.add_argument("--status")
    parser.add_argument("--run-rc", type=int, default=0)
    parser.add_argument("--input-token-price", default=None)
    parser.add_argument("--output-token-price", default=None)
    args = parser.parse_args(argv)
    row_start = int(args.row_start) if args.row_start != "" else None
    row_end = int(args.row_end) if args.row_end != "" else None
    delay_s = float(args.delay_s) if args.delay_s != "" else None
    emit(
        args.scores,
        args.results_jsonl,
        args.out,
        {
            "model": args.model,
            "engine": args.engine,
            "edition": args.edition,
            "task_range": args.task_range,
            "row_start": row_start,
            "row_end": row_end,
            "range_note": args.range_note,
            "base_url": args.base_url,
            "delay_s": delay_s,
            "run_rc": args.run_rc,
        },
        _optional_price(args.input_token_price),
        _optional_price(args.output_token_price),
        args.token_dir,
        status_path=args.status,
    )


if __name__ == "__main__":
    main()
