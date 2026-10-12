# Parallel runner, 429 backoff, and run reports — design

Date: 2026-10-12 · Status: draft v3 for review · Repo: `decision-index`

## 1. Problem

On the eval-ops dashboard, a full edition 0.2.1 run (150,759 requests) never finishes inside the runtime limit. The runner sends one request at a time and sleeps `delay_s` (0.5s) after each one.

| Run | Rows done | Eval time | Request time | Sleep time |
|---|---|---|---|---|
| 6141db1c (jev-latest) | 24,501 (16%) | 7.29h | 3.88h | 3.40h |
| f8950ba7 | 86,916 (58%) | 23.99h | 11.91h | 12.07h |

- jev-latest latency: p50 500ms, p90 776ms, mean 571ms.
- A full run as shipped takes about 45h.

Two more gaps:
- **Any 429 ends the run.**
- **The only report in `output/` is a tiny `token_usage.html`.** Nothing shows where the index came from, what failed, or how the run behaved.

## 2. Goals and success criteria

1. **Parallel requests.** A `concurrency` input in `input_param.json`, from 1 to 20.
   - At 20, a full run takes about 1.2h.
   - `concurrency=1` behaves as today, except for the retry in goal 2.
2. **Survive 429s.** A 429 is retried up to 3 times with backoff before the run gives up on it.
   - Backoff time never counts as model latency.
3. **Reports.** Every scored run writes a self-contained HTML report plus JSON, CSV and Markdown siblings to `output/`. They're built for this benchmark: index, areas, benchmarks, coverage, failures, latency, throughput.
   - Generating them can never fail the run.
4. Nothing already sent is lost when a run stops early. Resume and scoring are unchanged.

## 3. Non-goals

- An asyncio rewrite, multiple processes, or GPU batching.
- Scoring or reporting when the runtime limit hits. Today `run.sh` writes a zero fallback (§11).
- Concurrency in `pipeline` and `hf-job`.
- Calibration analysis (§11).

## 4. Approach

A thread pool with a bounded in-flight window, in `runner.py`.
- The engines are synchronous httpx clients. Sharing one `httpx.Client` across threads is safe: httpcore's sync pool changes its state under a thread lock (`httpcore/_sync/connection_pool.py:126`).
- The main thread stays the only writer, so `results.jsonl`, the per-case logs and the ledger need no locking.

Rejected:
- **asyncio:** needs async engines alongside the sync transformers engine, and gives no gain at ≤20 in flight.
- **Sharding into N runs and merging:** N copies of setup, a merge step, and the dashboard scores each run on its own.

## 5. Parallel runner (`decision_index/runner.py`)

### 5.1 Loop

Add a parameter `concurrency=1` and split the loop body in two:

- **`attempt(row_index, row) -> result`** runs on a worker thread.
  - It is today's per-row body (`runner.py:136-173`) plus the retry loop in §6.
  - It mutates no shared state.
- **`record(result)`** runs on the main thread.
  - It is today's bookkeeping (`runner.py:174-204`): `results.jsonl`, the per-case JSON, the ledger, counts, progress events.
  - It also takes over the stop decisions currently made inside the `except` branches (`runner.py:151-171`): HttpStop, 3 consecutive errors, device error, no success. `attempt` puts the classification on the result; `record` decides from it.

```
for each row (same range / keep / resume-skip logic as today):
    while in_flight >= concurrency and not stopped: record(next finished future)
    if stopped: break
    submit attempt(row_index, row); submitted += 1
    if limit and submitted >= limit: break
drain: record every remaining future
```

- **Order of processing:** when several futures finish together, they are recorded in `row_index` order.
- **Order in `results.jsonl`:** lines appear in completion order. That's safe because:
  - `load_results` keys by `run_id` (`scoring/report.py:16-22`);
  - resume keys by `run_id` (`runner.py:94-96`);
  - per-case files and the ledger carry `row_index`.
- **Stopping:** once a stop condition fires, nothing new is submitted. Up to N−1 in-flight rows are drained and recorded, then the `stopped` event is written. Rows that fail during the drain are `error` and are retried on resume, as today.
- **Consecutive errors** are counted in completion order.
- **`limit`:** counts submissions, so exactly `limit` rows run.
- **Provenance:** `environment.json` gains `concurrency`, the effective value.
- **SIGTERM** kills the process as today. At most N in-flight rows are lost; recorded rows are already on disk, flushed line by line.

### 5.2 Engines

- **Thread-safety flag.** `Engine.thread_safe = False` becomes the base-class default. `HttpSystemOne` and `GridChat` set `thread_safe = True`. For any other engine the runner clamps `concurrency` to 1, logs a warning line, and records the effective value:
  - `transformers` has GPU and KV-cache state;
  - `random` uses one seeded RNG, so parallel calls would make results depend on completion order.
- **Connection pool.** httpx keeps up to 20 idle connections alive by default (`httpx/_config.py:247`), which matches the cap of 20, so the clients don't change.
  - The CLI accepts higher values.
  - Above 20, the extra connections are closed after each request and reopened. That's slower but still correct, and the doc for the field says so.

### 5.3 `delay_s`

- Its meaning doesn't change: a pause after each request. It now applies **per worker**: the worker sleeps after its request, outside the timed window, before freeing its slot. `concurrency=1` timing is unchanged.
- The dashboard and `run.sh` default changes from 0.5 to **0**. The CLI default is already 0.

### 5.4 Main-thread load

At 20 workers and 571ms latency the main thread records about 35 rows/s, roughly 70 small file writes per second. That's well within what local disk handles, and the GIL is idle while workers wait on the network. A test runs a fake engine at `concurrency=20` and asserts all 20 are in flight at once.

## 6. Retries and backoff

### 6.1 What is retried

| Failure | Retried? | Why |
|---|---|---|
| HTTP 429, from `HttpStop(429)` or `HTTPStatusError` 429 | yes | rate limit; asked for explicitly |
| HTTP 502 / 503 / 504 | yes | at 20 in flight, a single gateway blip fails many requests at once and would trip the "3 consecutive errors" stop (the logs show 11–27 502s per run) |
| `ConnectError`, `RemoteProtocolError` | yes | transient transport failures, seen in f8950ba7 |
| `ReadTimeout` (600s) | no | three more 10-minute waits for one row would stall a slot for 30 minutes |
| 401 / 403 | no | stop at once, as today |
| everything else | no | unchanged |

Classification is duck-typed, so `runner.py` doesn't import httpx:
- the HTTP status comes from `exc.status_code` (HttpStop) or `exc.response.status_code` (HTTPStatusError);
- transport errors are matched by exception class name.

### 6.2 Backoff

- **Up to `RETRY_LIMIT = 3` retries per row**, so a row gets at most 4 attempts.
- **Wait before retry *n*:**
  - the response's `Retry-After` in seconds, capped at 60, if present;
  - otherwise `RETRY_BACKOFF_S = (5, 10, 20)[n]` plus up to `RETRY_JITTER_S = 1.0` of jitter.
  - That's about 35s of backoff per row before giving up, enough for a per-minute rate window to roll over.
- **The worker holds its slot while it backs off.** If a key starts returning 429s, every worker ends up waiting and new submissions stop on their own, so the run doesn't hammer the key. No shared pause state is needed.
- **Each attempt is timed separately.**
  - `total_wall_ms`, `model_request_wall_ms` and `started_utc` describe the final attempt, so backoff never enters the latency metric (`scoring/report.py:223-224`).
  - A row that was retried gets `retries: ["http_429", "http_502", …]`, one entry per attempt that was retried. The final attempt's own failure, if any, is in `error`.
- **Once retries run out, today's rules apply.** An http-engine 429 stops the run with `stop_reason=http_429`. A 5xx is recorded as `error` and counts toward the consecutive-errors rule.
- `HttpStop` gains `retry_after`, parsed in `HttpSystemOne.__call__`. `GridChat` doesn't change: its 429/5xx already raise `HTTPStatusError`, which carries the response.

## 7. Run reports

### 7.1 Viewing constraints (from the dashboard code)

- `output/` files are previewed in an `<iframe sandbox="">` after DOMPurify with `USE_PROFILES: {html: true}` and `WHOLE_DOCUMENT` (`eval-dashboard-frontend/src/components/file-preview.tsx:40-55, 191-196`).
- Allowed: `<style>` in `<head>`, `style` attributes, `<details>/<summary>`, tables.
- Stripped:
  - `script`, `iframe`, `object`, `embed`, `link`, `base`, `form`, `meta`;
  - every `href`;
  - all SVG, because the SVG profile is not enabled.
- The CSP blocks remote requests.
- The preview cuts off past 200,000 characters (`file-preview.tsx:12`).

So the report is one static HTML file:
- inline CSS, with dark mode via `prefers-color-scheme`;
- charts drawn as CSS bars (div widths);
- no scripts, no links, no SVG, no external fonts;
- under about 150k characters. A test checks the 200k cap with a realistic suite size of 45 benchmarks.

### 7.2 Files (in `output/`, all small)

| File | Contents |
|---|---|
| `decision_index_report.html` | visual report (§7.3) |
| `decision_index_report.md` | the same sections as Markdown (the dashboard renders it) |
| `decision_index_analysis.json` | every number in the report, machine-readable |
| `decision_index_benchmarks.csv` | one row per benchmark |

The existing `token_usage.{json,csv,md,html}` stay as they are. The report's token section reads from the token usage block in the results JSON.

### 7.3 Report sections

1. **Header.** Systemone model, engine, edition, run id, and a status pill: `complete`, `partial n%`, or `stopped: <reason>`.
2. **Headline tiles.**
   - Decision Index (chance-corrected) and raw index;
   - `balanced_skill`, `breadth_skill`;
   - coverage (completed / expected requests);
   - ok / error / unsupported / abstained / pending;
   - wall time, achieved RPM, latency p50 / p95, tokens.
3. **Areas.** One bar per area showing skill (0–100), with the raw score marked and coverage and benchmark count beside it. Sourced from `scores.areas`.
4. **Benchmarks, grouped by area.**
   - Columns: dataset, metric, score, chance, skill (bar), coverage, answered/requests, errors, unsupported, median ms.
   - Flags: **below chance** (skill = 0), **low coverage** (<95%), **not in index**.
   - Sourced from `scores.benchmarks` and `scores.index_benchmarks`.
5. **Strongest and weakest.** Top and bottom 8 in-index benchmarks by skill.
6. **Failures.**
   - Final status counts (last attempt per `run_id`, as `load_results` does).
   - An error taxonomy: exception class plus HTTP status, with count, the benchmarks affected, and one sample message cut to 200 characters.
   - Unsupported counts by benchmark.
   - A retry summary: retried rows by reason, recovered vs exhausted.
7. **Latency.**
   - A histogram with buckets <250ms, 250–500ms, 0.5–1s, 1–2s, 2–5s, 5–10s, 10–30s, >30s.
   - The 10 slowest benchmarks by median and p95.
   - A note that latency was measured at `concurrency = N`.
8. **Throughput timeline.**
   - Completed requests per bucket, up to 60 CSS bars over the run's wall time (first `started_utc` to last `completed_utc`).
   - Stalls show up as gaps: gateway outages, 429 backoff.
   - Beside it: configured `concurrency` and `delay_s`, achieved requests/s and RPM, and the stop reason.
9. **Run configuration.** Engine, model, `base_url`, `systemone_path`, edition, `concurrency`, `delay_s`, timeout, `task_range`, corpus sha256 (from `environment.json`).
10. **Method notes,** in a collapsed `<details>`: how the index is chance-corrected (the README formula) and what each status means.

### 7.4 Code

- **`decision_index/analysis.py`:** `analyze(run_dir, results_json) -> dict`. Pure data.
  - Reads `scores.json`, `environment.json` and `status.json` from the run dir, and the emitted `<id>_results.json` for metadata and token usage.
  - Streams `results.jsonl` once for:
    - final status per `run_id`;
    - the error taxonomy;
    - retries;
    - latency per benchmark (p50/p95);
    - histogram buckets;
    - throughput buckets.
  - Missing files leave empty sections, never an exception.
- **`decision_index/report.py`:**
  - `render_html(analysis) -> str`, `render_md(analysis) -> str`, `write_csv(analysis, path)`;
  - a `main()` for `python -m decision_index.report --run-dir … --results-json … --out-dir …`, the same pattern as `dashboard_emit`.
- **`dashboard_emit.py`:** adds `additional.concurrency` and `additional.run_health` (`wall_seconds`, `requests_per_minute`, `retried_rows`, `retries_exhausted`) so the key numbers appear on the dashboard results page without opening the HTML. It computes `run_health` from the `--results-jsonl` it already reads for the token report, using the same helper as `analysis.py`.
- **`run.sh`:** after the results JSON is written, it runs
  ```
  "$PY" -m decision_index.report ... || log_warn "report generation failed (non-fatal)"
  ```
  This follows the onboarding guide's rule that post-run reporting must never fail a valid run (token-usage section, `ONBOARDING_A_NEW_EVAL.md:1456`).

## 8. Interfaces

| Surface | Change |
|---|---|
| `input_param.json` | New field `concurrency`: number, default 20, min 1, max 20. Description: "Requests in flight at once. 20 ≈ 1.2h for the full suite at ~570ms latency; lower it if the server or key can't take it." `delay_s` default changes 0.5 → 0, with its description updated to say it applies per worker. |
| `run.sh` | `--concurrency` flag, default 20; passed to `run` and to `dashboard_emit`; shown in the `[run][INFO]` line; header comment updated for 429/5xx retry; report step added |
| `cli.py run` | `--concurrency` (int ≥ 1, default 1) |
| `dashboard_emit.py` | `--concurrency`; `run_health` is computed from the existing `--results-jsonl` |
| `docs/dashboard.md:107`, README | Replace "use delay_s 0.5 (≈120 RPM)" with the table in §9; list the report files |

## 9. Sizing guide (150,759 rows, 571ms mean latency, `delay_s=0`)

| `concurrency` | Requests/s | ≈ RPM on the key | Full run | Dashboard Runtime to set |
|---|---|---|---|---|
| 1 | 1.75 | 105 | ~24h | 30h |
| 5 | 8.8 | 525 | ~4.8h | 7h |
| 10 | 17.5 | 1,050 | ~2.4h | 4h |
| 20 | 35 | 2,100 | ~1.2h | 3h |

These figures assume latency holds under load. The smoke run (§10) measures what actually happens.

## 10. Testing and rollout

**Runner** (`tests/test_runner.py`, fake engine marked thread-safe, injected by monkeypatching `runner.load_engine`, with `RETRY_BACKOFF_S` and `RETRY_JITTER_S` patched to zero):
1. `concurrency=4`: peak in-flight is 4; every row is written once; the ledger is complete; `environment.json` shows concurrency 4.
2. `concurrency=20` over 60 rows: peak in-flight is 20 and all rows are written.
3. `random` with `concurrency=4` is clamped to 1, and its responses match a serial run exactly.
4. A 401 under concurrency stops the run, and rows written equal engine calls.
5. `limit` is exact under concurrency.
6. `delay_s` applies per worker, and `total_wall_ms` excludes the pause.
7. Two 429s then success: the row is `ok`, `retries == ["http_429", "http_429"]`, and `total_wall_ms` covers the final attempt only.
8. 429 retries exhausted: the run stops with `http_429`, and engine calls equal `RETRY_LIMIT + 1`.
9. A 502 then success: the row is `ok`. A `ReadTimeout` is not retried.
10. `Retry-After` is honoured and capped at 60s.

**Reports** (`tests/test_report.py`, using a synthetic run dir):
- `analyze` gets right:
  - status counts, using the last attempt per `run_id`;
  - the error taxonomy (e.g. `HTTPStatusError 502`);
  - retry counts;
  - latency percentiles;
  - throughput RPM;
  - the per-benchmark merge.
- It also survives missing files.
- `render_html` output:
  - has a `<style>` in `<head>`;
  - contains no `<script`, `<svg`, `href=` or `<link`;
  - is under 200,000 characters for 45 benchmarks;
  - shows the headline index.
- The Markdown has the benchmark table, and the CSV row count equals the number of benchmarks.

**Contract** (`tests/test_dashboard_contract.py`):
- `run.sh --concurrency` is accepted and logged;
- `input_param.json` has `concurrency` with range 1–20 and `delay_s` defaulting to 0;
- `emit` writes `concurrency` and `run_health`;
- the stub run (`DECISION_INDEX_STUB=success`, which has no `results.jsonl`) still exits 0 and writes all four report files, because the report tolerates missing inputs. The `|| log_warn` guard in `run.sh` covers any real failure.

The existing 69 tests stay green.

**Rollout:**
1. Implement with tests. Nothing is committed without explicit approval.
2. **Dashboard smoke:** `task_range=0-399`, `concurrency=20`. Check:
   - the four report files appear in `output/` and the HTML renders in the dashboard preview;
   - achieved RPM is ≈ `20 / latency`;
   - latency on those rows compares with serial run 6141db1c (BFCL rows).
3. Full run, Runtime 3h.

## 11. Follow-ups (out of scope)

- **Score and report when the runtime limit hits.** The `run.sh` EXIT trap writes a zero today, and the runner gives 3s from SIGTERM to SIGKILL (`eval-runner/src/executor/job_executor.rs:653`). The fix is to score periodically, or emit quickly on exit.
- **Calibration** (ECE and reliability bins from choice probabilities). Needs the scorer's per-benchmark correctness rules exposed per field.
- **A transient Grid 401** ("write conflict or a deadlock") ends the run.
- **Resume across dashboard runs.** The run dir is keyed by `EVAL_RUN_ID`.
