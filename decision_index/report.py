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
        ("Decisions path", run.get("decisions_path")),
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
