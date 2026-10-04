# Onboarding a New Eval to the Xyne Eval-Ops Dashboard — The Universal Guide

> **The guide we never got.** This document is the standalone, end-to-end manual for taking any evaluation benchmark — agentic or not — and making it run on the `xyne-eval-ops-dashboard`. It is written to be read by a human onboarding for the first time **and** to be handed verbatim to an LLM/coding-agent that must do the integration with full ownership.
>
> It is deliberately exhaustive. Everything here is grounded in the real dashboard code (`eval-runner`, `eval-scheduler`, `eval-dashboard-backend`) and in four production eval integrations: **`swe-auto-eval`**, **`swe-atlas-artificial-analysis`**, **`swe-verified`**, and **`terminal-bench-v2-agentic`**. Where a rule exists, it exists because we hit the bug. The failure catalog (§13) is the graveyard of ~5 hours-of-work-lost incidents so you never repeat them.

---

## Table of contents

- **§0 — How to use this document** (humans + agents; the **Agent Quickstart** decision procedure; notation; the four reference repos)
- **§1 — Mental model**: what the dashboard is; the run lifecycle; the architecture diagram; agentic vs non-agentic
- **§2 — The hard rules** (invariants: verify everything; don't touch shared infra; secrets; disk; …)
- **§3 — The execution environment** (COS + Debian runner + DooD; root-no-sudo; SA/metadata; filesystem layout)
- **§4 — The eval-runner contract** (files; args; env vars; the 3 dirs; the results JSON; log-sync; cancellation)
- **§5 — Registering the eval** (evals row/form/API; repo access public-vs-org-token; machine_type)
- **§6 — `input_params` deep dive** (schema; field types; constraints; flag mapping; the 4 real schemas)
- **§7 — Authoring `setup.sh`** (canonical annotated template + every convention + why)
- **§8 — Authoring `run.sh`** (canonical annotated template + every convention + why)
- **§9 — Docker under DooD** (health-check netns fix; harbor `mounted=False` fix; `.pth` vs `sitecustomize`; the image + build-cache **reaper**; OOM/ENOSPC prevention)
- **§10 — Artifact Registry** (decision tree; the correct vs cluttered format; the seeder; **eval-vm2** access; the pull path; when NOT to push)
- **§11 — Logging & artifacts discipline** (output-vs-logs-vs-never-synced; immediate/atomic save; per-agent capture; token-usage reporting; secrets)
- **§12 — Models, providers, Grid** (endpoint; bare-vs-`Grid/`; credential fan-out; opencode; the LLM judge)
- **§13 — The failure catalog** (every bug: symptom → root cause → fix → prevention)
- **§14 — End-to-end runbooks** (A: non-agentic/API · B: harbor agentic · C: SWE-bench-style with seeded images)
- **§15 — Testing & verification + master checklists**
- **§16 — Appendices** (env table · 4 results schemas · 4 input_params · file-tree map · glossary · command cheat-sheet · the one-paragraph contract)

---

## Section 0 — How to use this document

### 0.1 If you are a human

- **First time?** Read §1 (mental model), §2 (hard rules), §3 (environment), §4 (the contract) in order. That is the "why" and the non-negotiables. Then jump to the runbook in §14 that matches your benchmark type and follow it top-to-bottom, dipping into §5–§12 for detail.
- **Already onboarded one eval?** Skim §2 (hard rules — read every time), then go straight to your runbook in §14 and the checklists in §15.
- **Stuck / hit a bug mid-run?** Go to §13 (failure catalog). It is indexed by symptom. Almost every failure mode we have ever seen is there with root cause and fix.

### 0.2 If you are an LLM/agent executing this integration

- This document is your **specification**. Treat every block prefixed `RULE:` as a hard constraint you must not violate, every `DECISION:` as a branch you must evaluate against the actual repo, every `TEMPLATE:` as canonical code you may copy and adapt, and every `CHECKLIST:` as an acceptance gate you must satisfy before claiming done.
- **Do not assume. Verify.** This project has one overriding rule (§2.1): never assert how something behaves from reasoning — read the real code, the real logs, or add a diagnostic and run it. If a fact you need is not verified, say so and go verify it before you act.
- **Do not touch shared infrastructure** (`eval-runner`, `eval-scheduler`, the backend) to make one benchmark work (§2.2). Everything you need to fix lives in the benchmark's own `setup.sh` / `run.sh` / helper scripts.
- Start at **§0.3 (the Agent Quickstart)**. It is a linear decision procedure that routes you to the right runbook and the right sections.

### 0.3 Agent Quickstart — the linear decision procedure

Execute these steps in order. Each step tells you what to produce and where to read.

```
STEP 1  Classify the benchmark.
        Answer three questions about the benchmark you are onboarding:
          Q1. Is it AGENTIC? (a coding/CLI agent explores and edits, vs. a single
              model call that is scored)                       -> see §1.4
          Q2. Does it run per-task/per-instance DOCKER IMAGES that would otherwise
              be pulled from the public internet (Docker Hub / GHCR) at run time?
                                                                -> see §10.1 decision tree
          Q3. Does it use the HARBOR harness, a SWE-bench harness, or a bespoke
              runner?                                           -> §14 picks the runbook

STEP 2  Pick your runbook (§14):
          - Non-agentic OR agentic-but-no-task-images, pure API scoring -> Runbook A (§14.1)
          - Agentic on HARBOR (like swe-atlas / terminal-bench)         -> Runbook B (§14.2)
          - SWE-bench-style with per-instance images (like swe-verified)-> Runbook C (§14.3)

STEP 3  Internalize the contract (§4). Your repo MUST provide:
          - setup.sh at repo root (installs everything; NO args passed to it)
          - run.sh at repo root (runs the eval; receives [grid_key?] eval_run_id --flags)
          - run.sh MUST write ${EVAL_RUNNER_OUTPUT_DIR}/${eval_run_id}_results.json
            in the canonical metrics shape (§4.5). If it does not, the run is FAILED.

STEP 4  Author setup.sh (§7 template) and run.sh (§8 template). Copy the canonical
        templates and adapt. Do NOT reinvent the conventions — every one of them
        (stdbuf, set -u not set -e, SUDO="" wrapper, docker-cli-only, .pth patches,
        EXIT-trap fallback results) exists because omitting it broke a real run (§13).

STEP 5  If DECISION in §10.1 = "needs images": build the seeder (§10.5), get access
        to eval-vm2 (§10.6), seed the images to Artifact Registry in the CORRECT
        single-package-per-instance-tags format (§10.3). Do NOT use the cluttered
        per-image format (§10.4).

STEP 6  Decide input params (§6). Write input_params (the {fields:[...]} schema).
        These become CLI --flags on run.sh.

STEP 7  Register the eval on the dashboard (§5): create the evals row with repo_url,
        branch/commit pin, machine_type, and input_params. Ensure the repo is public
        OR in the Juspay org and reachable by the dashboard's GITHUB_TOKEN.

STEP 8  Verify BEFORE a real run (§15): bash -n, shellcheck, local stub smoke test,
        .pth dual-gate self-check. Then run a SMOKE range (e.g. task_range 0-9) on the
        dashboard first. Only after that goes green do a full run.

STEP 9  If anything fails, go to §13 (failure catalog), find the symptom, apply the
        fix in your OWN scripts. Re-run the smoke range. Never widen scope to shared infra.
```

### 0.4 Notation used throughout

- **`RULE:`** — a hard constraint. Violating it breaks the run or the dashboard. Non-negotiable.
- **`DECISION:`** — a branch point. Evaluate the condition against the actual repo you are onboarding and take the matching path.
- **`TEMPLATE:`** — canonical, copy-pasteable code. Adapt the marked placeholders; keep the structure.
- **`CHECKLIST:`** — an acceptance gate. Every item must be satisfied before the phase is "done".
- **`WHY:`** — the reason a rule exists, usually a specific incident. Read these; they are the difference between cargo-culting and understanding.
- **`file:line`** citations point into the dashboard repo or one of the four reference eval repos. They were accurate at the time of writing; re-verify against current code before asserting (per §2.1).

### 0.5 The four reference repos (your worked examples)

| Repo | Type | Harness | Task images? | What it teaches best |
|---|---|---|---|---|
| `swe-verified` | Agentic (OpenHands SWE-bench Verified) | OpenHands SDK | **Yes**, per-instance | The **correct** AR format; the image + build-cache **reaper**; the DooD health-check fix; results reconciliation |
| `swe-atlas-artificial-analysis` | Agentic (rubric-judged QnA) | harbor 0.6.6 (vendored) | Yes, shared per-split | `input_param.json`; harbor DooD mount fix; complex agent answer-capture; clean AR format |
| `terminal-bench-v2-agentic` | Agentic (test-verified terminal tasks) | harbor 0.13.1 (PyPI) | Yes, per-task | harbor DooD mount fix; simple agent capture; the **cluttered** AR anti-pattern |
| `swe-auto-eval` | Agentic (SWE-bench multipass, custom runner) | forked swe-bench | Yes (pulled+deleted) | Native-workspace agents; immediate patch/prediction save; multipass; the **cluttered** AR anti-pattern; per-agent log formats |

Absolute paths (in this workspace):
- `github-folder/swe-verified`, `github-folder/swe-atlas-artificial-analysis`, `github-folder/terminal-bench-v2-agentic`, `github-folder/swe-auto-eval`
- Dashboard: `workspace/Vaibhav/xyne-eval-ops-dashboard`

---

## Section 1 — Mental model: what the dashboard is and how a run actually executes

### 1.1 One sentence

The dashboard is a system that, when you press "Run", **spins up a fresh Google Cloud Batch VM, clones your benchmark's git repo onto it, runs your `setup.sh` then your `run.sh`, streams their logs to a web UI, and reads back one small JSON file of metrics** that it stores and puts on a leaderboard.

Everything you author is in service of that: make `setup.sh` install what you need, make `run.sh` run the eval and emit the metrics JSON, and keep both alive inside a constrained container.

### 1.2 The components (only the parts you touch or depend on)

- **`eval-dashboard-frontend`** — the web UI. Where a human creates an "eval" (registers your repo) and presses "Run Eval" (fills your input params and starts a run). You rarely edit this.
- **`eval-dashboard-backend`** — the API + Postgres. Stores evals, runs, results, the leaderboard. Validates your input params and your results JSON. You never edit this; you conform to it.
- **`eval-scheduler`** — the Rust service that turns a run into a **Google Cloud Batch** job: it picks the machine type, attaches the service account, injects secrets/env, and launches the runner container. **Shared infra. Do not modify it** (§2.2).
- **`eval-runner`** — the Rust binary that runs *inside* the Batch VM's container. It clones your repo, executes `setup.sh` then `run.sh`, syncs logs/artifacts to GCS, heartbeats the backend, and parses your results JSON. **Shared infra. Do not modify it** (§2.2). This is the thing whose *contract* (§4) you must satisfy.
- **Your benchmark repo** — a git repo containing (at minimum) `setup.sh` and `run.sh` at the root, plus whatever harness/adapter/scripts your eval needs. **This is the only thing you write.**

### 1.3 The run lifecycle (what happens end to end)

```mermaid
flowchart TD
    A["User creates eval<br/>(repo_url + input_params schema)"] --> B["Backend: evals row"]
    B --> C["User clicks Run Eval<br/>(fills input params)"]
    C --> D["Backend: evaluation_runs row"]
    D --> E["eval-scheduler builds a<br/>Google Cloud Batch job"]
    E --> F["Batch boots a COS VM, runs the<br/>Debian eval-runner container,<br/>binds /var/run/docker.sock (DooD)"]
    F --> G["git clone your repo into work/repo"]
    G --> H["bash setup.sh — no args<br/>(streams to setup.log)"]
    H --> I["bash run.sh — key? id --flags<br/>(streams to runner.log)"]
    I --> J{"Did run.sh write<br/>output/ID_results.json ?"}
    J -- yes --> K["Runner parses metrics,<br/>POSTs to backend → COMPLETED"]
    J -- no --> L["Run marked FAILED"]
    K --> M["Leaderboard upsert if higher,<br/>artifacts.zip, final log flush"]
    I -. every 30s .-> S1[("GCS: runner_logs — full")]
    I -. every 150s .-> S2[("GCS: repo/output + repo/logs — incremental")]
    style L stroke:#d5443a,stroke-width:2px
    style K stroke:#2e9e5b,stroke-width:2px
    style M stroke:#2e9e5b,stroke-width:2px
```

The same flow in detail (plain-text):

```
[User in frontend] --- creates eval (repo_url, input_params schema) --> [Backend: evals row]
[User] --- "Run Eval" (fills input params) --------------------------> [Backend: evaluation_runs row]
        |
        v
[eval-scheduler] --- builds a Google Cloud Batch job:
        - machine_type (n2-standard-8 default)
        - task container image = the eval-runner image (from Artifact Registry)
        - bind-mount: /var/run/docker.sock  (Docker-outside-of-Docker)
        - task service account (grants Artifact Registry + GCS + Secret Manager)
        - env: EVAL_RUN_ID, BACKEND_URL, GCS__*, secrets GRID_AI_API, GITHUB_TOKEN
        v
[Google Cloud Batch] --- boots a VM whose OS is COS (Container-Optimized OS),
                         runs the eval-runner Debian container on it
        v
[eval-runner, inside the Debian container] does, in order:
        1. git clone <your repo_url> [--branch <b>] into  <work>/repo     (cwd base)
        2. chmod +x setup.sh; run  `bash setup.sh`  (NO args)             --> setup.log
        3. chmod +x run.sh;   run  `bash run.sh <grid_key?> <eval_run_id> <--flags>` --> runner.log
        4. every 30s: upload runner_logs/ (full);  every 150s: upload repo/output + repo/logs (incremental)
        5. on run.sh exit: read  repo/output/<eval_run_id>_results.json
        6. POST metrics to backend  ->  COMPLETED (or FAILED if no results file)
        7. zip artifacts, final log flush
```

**RULE:** The runner runs `setup.sh` and `run.sh` as **subprocesses inside the Debian eval-runner container**, with the current working directory set to the cloned repo root. It is not the COS host. (`eval-runner/src/executor/job_executor.rs:390,417`.)

### 1.4 "Agentic" vs "non-agentic" — why it matters

- **Non-agentic eval**: a prompt goes to a model, one (or a few) completions come back, and a scorer grades them. No sandbox, no tools, no per-task containers. Simpler: usually no Docker, no Artifact Registry, no harness. (Runbook A, §14.1.)
- **Agentic eval**: a coding/CLI agent (claude-code, opencode, xyne-cli, pi, …) runs in a loop, explores a codebase or a terminal, calls tools, edits files, and its *trajectory/outcome* is graded — often by running tests inside a per-task Docker container. More moving parts: Docker under DooD, a harness (harbor / swe-bench), per-instance images, agent session capture. (Runbooks B and C.)

All four reference repos are agentic because agentic evals are the hard case and exercise every constraint in this guide. A non-agentic eval is a strict subset: satisfy the contract (§4), skip the Docker/AR/harness chapters.

### 1.5 The architecture diagram (keep this in your head)

```
  Google Cloud Batch VM
  +--------------------------------------------------------------+
  |  Host OS: COS (Container-Optimized OS)                        |
  |  - read-only root fs, minimal, no apt, no package manager     |
  |  - the real dockerd runs HERE (fully capable, CAP_SYS_ADMIN)  |
  |  - /var/run/docker.sock  <---------------------------+        |
  |                                                      |        |
  |   +----------------------------------------------+   | bind   |
  |   |  eval-runner container  (Debian)             |   | mount  |
  |   |  - runs as ROOT, but NO sudo binary          |   |        |
  |   |  - your setup.sh / run.sh run HERE           |   |        |
  |   |  - cwd = /.../<run_id>/repo                   |   |        |
  |   |  - /var/run/docker.sock bind-mounted in  ----+---+        |
  |   |    => `docker ...` talks to the HOST daemon (DooD)         |
  |   |  - NO host-shared filesystem (only the socket)            |
  |   |  - reaches GCS/Artifact Registry via the VM service       |
  |   |    account (metadata server / ADC) — no creds handed to you|
  |   +----------------------------------------------+            |
  |                                                               |
  |   Task/agent containers you `docker run` become SIBLINGS of   |
  |   the runner (children of the host dockerd), NOT children of  |
  |   the runner container.                                       |
  +--------------------------------------------------------------+
```

**This single picture explains ~40% of the bugs in this guide.** Because Docker is DooD, containers you start are siblings on the host, ports you publish land on the *host* network namespace, and any path you bind-mount is resolved by the *host* daemon — not visible inside the runner. Sections §9 and §13 are almost entirely consequences of this diagram.

---

## Section 2 — The hard rules (invariants you must never break)

These are project-level laws. They override convenience, cleverness, and "it would be easier if…". They were each written in blood.

### 2.1 RULE: Never assume. Verify everything.

`WHY:` This is the single most-repeated instruction on this project. Guessing how the container is set up, which env vars exist, how creds are reached, or what a flag does has burned hours and destroyed trust. (`memory: no-assumptions-verify-everything`.)

How to comply:
- When a fact is not verified, **do not proceed on a guess**. Read the actual source, or **add a diagnostic log/print, run it, and read the logs** to establish ground truth (env vars, SA identity, endpoint reachability, HTTP codes), then fix from evidence.
- Treat any fix built on an unverified premise as a *hypothesis* and label it as such until logs confirm it.
- Cross-check every method/flag against the real code of the thing you are integrating (harbor, swe-bench, the eval-runner), not memory or analogy. `file:line` in this doc were true when written — re-verify.
- Thoroughness and correctness outrank token/context economy. It is always cheaper to verify than to lose a 3-hour VM run.

### 2.2 RULE: Never modify shared infrastructure to fix one benchmark.

`WHY:` `eval-runner` and `eval-scheduler` (and the backend) serve *every* benchmark. A change that helps yours can silently break all the others. A `batch.rs` host-path-mount fix was once applied to solve a single benchmark's problem and had to be fully reverted on pushback. (`memory: tbench-batch-scheduler-mount-fix`.)

How to comply:
- Every fix goes into **your benchmark's own** `setup.sh` / `run.sh` / helper scripts / `.pth` patches.
- If it genuinely seems like the platform must change, **present options and ask first** — do not unilaterally edit shared services.
- The harness (harbor, swe-bench) is patched **from your setup.sh at install time** (e.g. `.pth` site-packages shims, §9.3), never by editing shared infra.

### 2.3 RULE: The user owns git. You do not commit or push unless told.

`WHY:` Several of these repos are shared eval infra. On this project the human handles all `git add/commit/push`. Your job is to make the working tree correct, verify it (`bash -n`, shellcheck, local stubs), and stop.

How to comply: make edits, verify them, report exactly what changed and why. Do not run `git commit`/`git push` unless explicitly asked.

### 2.4 RULE: Present options before structural changes.

`WHY:` Structural fixes (changing the run topology, the output layout, the image strategy) have blast radius. The user wants to choose. How to comply: when a fix is more than a local script tweak, lay out the options with trade-offs and let the human pick before you implement.

### 2.5 RULE: A run that exits 0 but writes no results file is a FAILED run.

`WHY:` The runner marks the run FAILED if `run.sh` returns success but no `<eval_run_id>_results.json` exists (`eval-runner/src/main.rs:281-283`). So a "successful" script that forgot to write metrics looks like a hard failure on the dashboard.

How to comply: **always** write a results file, even on error — use the EXIT-trap zero-metric fallback (§8, §4.5). Disarm it only after the real results are written.

### 2.6 RULE: Secrets never appear in logs or artifacts.

`WHY:` Everything under `runner_logs/`, `repo/output/`, `repo/logs/` is uploaded to GCS. An API key echoed into a command that the harness logs, or written into a synced config, leaks it. We shipped a fix because `models.json` with a grid key was echoed into `trial.log`. (`memory: swe-atlas-xyne-v030-capture-break`.)

How to comply: pass secrets via the process `env` dict or `printf %s` into files with `600` perms — never interpolate a key into a shell command string, and never write a key into a synced directory in plaintext.

### 2.7 RULE: Keep large/transient data out of the synced directories.

`WHY:` Only `runner_logs/`, `repo/output/`, `repo/logs/` are uploaded, and `repo/output/` is walked in full on the final pass. Putting 100k-file git-clone workspaces under `output/` made a single 30s sync tick take *tens of minutes*, starving the live log stream (the "Cannot stat" flood). (`memory: xyne-logsync-cancel-fixes`.)

How to comply: put big/transient working data (checkouts, venvs, container scratch, build trees) somewhere in the repo tree that is **not** `output/` or `logs/` (e.g. a `workspaces/` or `cache/` dir), and add it to `.gitignore`. Put only small result/artifact files in `output/` and verbose-but-bounded logs in `logs/`. (§11.2.)

### 2.8 RULE: Disk is the enemy on long agentic runs. Reap continuously.

`WHY:` Per-instance image pulls plus per-instance agent-server image builds plus BuildKit build cache filled a 200GB disk and OOM/ENOSPC-killed VMs — losing ~5 hours of work. The build cache was the *dominant* leak and neither `docker rmi` nor `docker image prune` touch it. (`memory: swe-verified-dashboard-integration`.)

How to comply: if your eval builds or pulls images per instance, run the **pressure-triggered reaper** (§9.4) that prunes build cache every tick and panics below a free-space floor. If your eval never builds/pulls per instance (native workspaces, or harness that deletes each image), you may not need it — but know why.

### 2.9 RULE: Do not pin an internal agent CLI to a stale version, but do not blindly trust `latest` either.

`WHY:` `xyne-cli` is developed at head and must be tested at `latest` (user directive), but npm `latest` silently jumped to v0.3.0 which turned the CLI into a TTY app and broke stdout capture → every task scored 0. (`memory: swe-atlas-xyne-v030-capture-break`.)

How to comply: fetch `latest` for internal agents *and* make your capture **version-adaptive and self-diagnosing** (multiple capture sources, a loud health rollup when capture yields nothing) so a CLI contract change is a visible warning, not a silent all-zero. For third-party pinnable tools (harbor, a specific claude-code version), pin deliberately.

### 2.10 RULE: When you change the run topology, re-smoke-test on a tiny range first.

`WHY:` Full runs are expensive and slow. A smoke range (e.g. `0-9`) surfaces contract/auth/topology bugs in minutes instead of hours. Every reference integration was validated on a small range before a full run.

How to comply: always do a `task_range 0-9` (or equivalent) dashboard run after any structural change, read the logs, and only then scale up.

---

## Section 3 — The execution environment in depth

Everything your scripts do happens inside one specific, constrained place. Internalize its properties; most "why doesn't this work" questions are answered here.

### 3.1 COS host + Debian runner container

- The Batch VM's operating system is **COS (Container-Optimized OS)**: a minimal, largely **read-only root filesystem**, no `apt`, no general package manager, designed only to run containers. You do **not** run your scripts on the COS host.
- On that host, Batch runs the **`eval-runner` container, which is Debian-based**. Your `setup.sh` and `run.sh` execute as subprocesses *inside that Debian container*. This is where you have `apt`, can install tools, etc. (`memory: batch-vm-runs-in-debian-container`.)

`RULE:` Anything you install goes into the Debian runner container's filesystem (ephemeral, per-run). Do not expect to write to the COS host root fs. In particular, `/var/lib/docker` on the COS host is *bind-mounted-adjacent* to Docker but not a general scratch space you own — always write-probe before using it (§7.5).

### 3.2 You run as root, but there is no `sudo`

- The scripts run as **UID 0 (root)** — so you generally don't *need* `sudo`.
- But the Debian runner image **does not ship the `sudo` binary**. A script that calls `sudo apt-get …` dies with `sudo: command not found` (exit 127). This was `swe-verified`'s very first failure at `setup.sh:13`. (`memory: swe-verified-dashboard-integration`.)

`RULE:` Never call `sudo` unconditionally. Use the `SUDO=""` wrapper (§7.4): use `sudo` only when you are *not* root **and** `sudo` exists; otherwise run bare.

```bash
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
$SUDO apt-get update -qq   # runs bare as root on Batch; uses sudo on a dev VM
```

### 3.3 Docker is Docker-outside-of-Docker (DooD), not Docker-in-Docker (DinD)

- The real `dockerd` runs on the **COS host**. The runner container gets the host's socket bind-mounted at `/var/run/docker.sock` (`eval-scheduler/src/dispatcher/batch.rs:151`).
- So when your script runs `docker build` / `docker run` / `docker pull`, it is talking to the **host daemon**. Containers you start are **siblings** of the runner (children of host dockerd), sharing the host's `overlay2` storage. (`DIND_PERFORMANCE_ANALYSIS.md`.)
- **DinD does not work here.** An in-container dockerd fails at `unshare(CLONE_NEWNS)` without `CAP_SYS_ADMIN`. That is *why* the platform chose DooD. Do not try to start your own dockerd.

`RULE:` Install the **Docker CLI only** (`docker-ce-cli` + `docker-buildx-plugin` if you build), never the engine (`docker-ce`/`containerd.io`) when a socket is present. Pin the CLI to the **24.x** line to stay protocol-compatible with the COS host daemon (Docker 24.0.9). (§7.6; verified in all four repos, e.g. `swe-verified/setup.sh`, `swe-auto-eval/setup.sh:1034-1047`.)

Consequences you must design around (each expanded in §9):
- **Published ports land on the host netns.** A container that publishes `-p 8000` is reachable on the *host's* localhost, not the runner's. Health checks that poll `127.0.0.1:<port>` from inside the runner time out even though the server is up. (§9.2.)
- **Bind mounts are resolved by the host daemon.** If you `-v /some/path:/x` and expect to read files back from `/some/path` inside the runner, you can't — the host resolved it. This is exactly why harbor's default "read verifier output from the host bind source" fails under DooD. (§9.3.)
- **There is no host-shared filesystem.** The *only* thing shared between the runner and the host is the docker socket. A DooD probe on a real run proved there is no shared path. (`memory: tbench-batch-scheduler-mount-fix`.)

### 3.4 Credentials: the VM service account via metadata / ADC — nothing is handed to you

- The Batch VM has a **service account** attached that grants access to **Artifact Registry**, **GCS**, and **Secret Manager**.
- Your scripts are **not** given a GCP key file. You reach GCP the same way the runner does: via **Application Default Credentials / the metadata server**. (`eval-runner/src/storage/gcs.rs:97-100` uses ADC when no key path is set; `.env.example` documents `GCS__CREDENTIALS_PATH` as optional "uses ADC if not set".)
- The reliable, install-free way to authenticate Docker to Artifact Registry from inside the container is to fetch an OAuth token from the metadata server and `docker login` with it:

```bash
TOKEN=$(curl -s -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
echo "$TOKEN" | docker login -u oauth2accesstoken --password-stdin https://us-central1-docker.pkg.dev
```

`RULE:` Prefer the **metadata token → `docker login`** path for Artifact Registry auth. It works on any GCP VM regardless of whether `gcloud` is installed. Installing `gcloud` is a belt-and-suspenders extra (and must be the **tarball** install, because COS root fs is read-only for apt — see §7.7), not the primary path.

`WHY:` We verified (by probing on a real run, not assuming) that the metadata server is reachable from inside the container and the SA token authorizes AR + GCS. When the metadata *hostname* failed on one host, the metadata *IP* `169.254.169.254` worked — so if `metadata.google.internal` resolution is flaky, hit the IP directly. (`memory: swe-atlas-xyne-v030-capture-break`, `batch-vm-runs-in-debian-container`.)

### 3.5 Network

- The Batch VMs have **external internet** (no `noExternalIpAddress`): git clone from GitHub, reach the public backend URL, `googleapis.com`, npm, PyPI, Docker Hub, GHCR all work (`batch.rs:111-125`).
- But **do not rely on public image registries at run time** for per-task images — rate limits and egress cost. Mirror them into Artifact Registry once and pull from AR (§10). Run-time public pulls are a reliability risk, not a hard block.

### 3.6 Secrets and identity injected into the runner container

The scheduler injects these into the runner container's environment (`batch.rs:94-109`). Your scripts inherit them (the child process is not `env_clear`'d):

| Var | Source | Use |
|---|---|---|
| `EVAL_RUN_ID` | scheduler | the run's UUID (also passed as a positional arg to `run.sh`) |
| `RUNNER_TOKEN` | scheduler | per-job JWT for the runner→backend API (not a GCP cred) |
| `BACKEND_URL` | scheduler | dashboard backend base URL |
| `STORAGE_PROVIDER`, `GCS__PROJECT_ID`, `GCS__BUCKET` | scheduler | GCS target for artifact upload |
| `GRID_AI_API` | Secret Manager | the Grid AI API key (also delivered as `run.sh`'s optional positional `$1`, or per-run via a `X-Grid-Ai-Key` header) |
| `GITHUB_TOKEN` | Secret Manager | used by the runner to clone private repos; available to you if you need authenticated GitHub access |

`RULE:` Treat `GRID_AI_API`/`GITHUB_TOKEN` as secrets (§2.6). The canonical way `run.sh` receives the model key is the positional `$1` (§4.2); the env var is a fallback.

### 3.7 Filesystem layout the runner sets up

For a run with id `<run_id>`, the runner creates (`eval-runner/src/executor/job_executor.rs:104-157`):

```
<WORKER__WORK_DIR_BASE>/<run_id>/
├── runner_logs/          # runner's own logs (setup.log, runner.log, git_clone.log). == EVAL_RUNNER_LOGS_DIR
└── repo/                 # your cloned benchmark repo. == EVAL_RUNNER_WORK_DIR == cwd of setup.sh/run.sh
    ├── setup.sh          # you author
    ├── run.sh            # you author
    ├── output/           # == EVAL_RUNNER_OUTPUT_DIR. Put results.json + small artifacts here
    ├── logs/             # optional; verbose bounded logs go here (synced every 150s)
    └── ... your repo ...
```

- `EVAL_RUNNER_WORK_DIR` = `<run_id>/repo` (the repo root, and the cwd). Note: it is the **repo dir**, not the parent.
- `EVAL_RUNNER_LOGS_DIR` = `<run_id>/runner_logs` (outside the repo). You *may* drop extra logs here; it is synced every 30s in full.
- `EVAL_RUNNER_OUTPUT_DIR` = `<run_id>/repo/output`. **Where `run.sh` must write `<eval_run_id>_results.json`.**

### 3.8 Disk

- The VM has a finite disk (historically ~200GB usable on the machine types we used). Multi-GB per-instance images and unbounded BuildKit cache fill it fast on agentic runs. When disk hits 100%, not only does the run die — the **EXIT-trap fallback can't write** either, so you get *no* results at all. (§2.8, §9.4.)
- Pick `machine_type` with enough disk headroom for your parallelism, and reap aggressively (§9.4).

### 3.9 CHECKLIST: environment assumptions to (re)verify on your first run

`CHECKLIST:` Before trusting any environmental assumption, prove it on a smoke run by logging it from `setup.sh`/`run.sh`:
- [ ] `id -u` prints `0` and `command -v sudo` is empty (root, no sudo).
- [ ] `[ -S /var/run/docker.sock ]` is true and `docker info` succeeds (DooD works).
- [ ] `[ -f /.dockerenv ]` is true (you are in a container) — used as a DooD heuristic.
- [ ] the metadata token fetch returns a non-empty `access_token`.
- [ ] `docker login` to `us-central1-docker.pkg.dev` succeeds with that token.
- [ ] `EVAL_RUNNER_WORK_DIR`, `EVAL_RUNNER_LOGS_DIR`, `EVAL_RUNNER_OUTPUT_DIR` are all set and point where §3.7 says.
- [ ] a write-probe to `/var/lib/docker` either succeeds or your fallback base (`$HOME/.gcloud-eval`) is used.

---

## Section 4 — The eval-runner contract (the specification)

This is the exact, verified contract between your repo and the platform. If your repo satisfies §4, it runs. Everything else in this guide is about satisfying §4 without breaking under the environment's constraints.

### 4.1 Files the runner expects

`RULE:` Your repo root MUST contain two executable scripts:
- **`setup.sh`** — run first, once, to install/provision everything the eval needs.
- **`run.sh`** — run second, to execute the eval and emit results.

The runner `chmod 0755`s each before running and invokes it as `bash <script>` with cwd = repo root (`job_executor.rs:390-417`). They do not need a shebang to be executed by the runner, but include `#!/usr/bin/env bash` anyway for local runs.

There is a **legacy** architecture in the dashboard repo (`eval-dashboard-benchmark/` with per-benchmark `Dockerfile` + `entrypoint.sh` + `result_parsers.py`, "benchmark API on port 8000"). **Do not model your integration on it.** The current contract is `setup.sh`/`run.sh`/`_results.json`. (Dashboard research, `README.md:101`.)

### 4.2 Invocation and arguments

`RULE:` The runner invokes your scripts like this (`job_executor.rs:164-198`):

```
bash setup.sh                                   # NO arguments
bash run.sh  [GRID_AI_KEY]  <eval_run_id>  --field1 val1  --field2 val2  ...
```

- **`setup.sh` gets no arguments.** Do not read `$1` in setup.sh for run parameters.
- **`run.sh`'s argument layout is conditional on whether a Grid AI key exists:**
  - If a Grid AI key is present: `$1` = the key, `$2` = `eval_run_id`, `$3..` = the `--flags`.
  - If no Grid AI key: `$1` = `eval_run_id`, `$2..` = the `--flags`.
- The Grid AI key comes from the env `GRID_AI_API` or a per-run BYOK header (`X-Grid-Ai-Key`). (`job_executor.rs:180-198`, `api_client.rs:182-217`.)

`RULE:` Parse `run.sh` args **robustly** by detecting whether an argument looks like a `--flag`:
```bash
# Canonical positional parse used by all four repos:
if [ -n "${1:-}" ] && [ "${1#--}" = "$1" ] && [ -n "${2:-}" ] && [ "${2#--}" = "$2" ]; then
    API_KEY="$1"; EVAL_RUN_ID="$2"; shift 2      # key + id
elif [ -n "${1:-}" ] && [ "${1#--}" = "$1" ]; then
    EVAL_RUN_ID="$1"; shift                        # id only (no key)
else
    EVAL_RUN_ID="local_$(date +%Y%m%d_%H%M%S)"     # manual/local run: synthesize an id
fi
```

### 4.3 How input params become `--flags`

The eval-runner translates your run's `input_params` object into CLI flags (`parse_input_params`, `job_executor.rs:463-501`):

| Input param value | Emitted on the run.sh command line |
|---|---|
| string / number | `--key value` |
| boolean `true` | `--key` (bare flag, no value) |
| boolean `false` | *(omitted entirely)* |
| array of strings | `--key v1 --key v2 …` (repeated) |
| key `foo_bar` | `--foo-bar` (underscores → hyphens, unless key already starts with `--`) |

`RULE:` Design your `run.sh` flag parser to accept `--kebab-case` flags. Accept unknown flags gracefully (forward them to your harness, or ignore with a warning) so a schema/flag drift does not crash the run. All four repos keep an `EXTRA_FLAGS`/`EXTRA_HARBOR_FLAGS` bucket for unknown flags.

### 4.4 The three guaranteed environment variables

`RULE:` These three are the *only* env vars the runner explicitly sets on your scripts (`job_executor.rs:420-425`). Everything else is inherited from the runner container (§3.6).

| Var | Value | You use it to… |
|---|---|---|
| `EVAL_RUNNER_WORK_DIR` | `<run_id>/repo` (== cwd) | locate your repo root; detect you're under the runner (it's set) |
| `EVAL_RUNNER_LOGS_DIR` | `<run_id>/runner_logs` | optionally drop extra always-synced logs |
| `EVAL_RUNNER_OUTPUT_DIR` | `<run_id>/repo/output` | **write `<eval_run_id>_results.json` and small artifacts here** |

`RULE:` Always resolve your output root as `OUTPUT_ROOT="${EVAL_RUNNER_OUTPUT_DIR:-./output}"` so the script also works for manual/local runs where the var is unset. (Proven pattern in all four repos.)

### 4.5 The results contract (the single most important output)

`RULE:` `run.sh` MUST write a file at exactly:
```
${EVAL_RUNNER_OUTPUT_DIR}/${eval_run_id}_results.json
```
with exactly this JSON shape (`parse_new_format_results`, `job_executor.rs:530-576`):

```json
{
  "metrics": {
    "main":       { "name": "pass@1", "value": 0.873 },
    "secondary":  { "pass@10": 0.91, "resolved": 437, "total_tasks": 500 },
    "additional": { "per_pass": { "...": "..." }, "notes": "anything" }
  }
}
```

Parser rules (enforced; violating them makes the results unparseable):
- `metrics` object is **required**.
- `metrics.main` is **required**; `metrics.main.name` must be a **string**; `metrics.main.value` must be a **number (f64)**.
- `metrics.secondary` and `metrics.additional` are **optional** (default `{}`), and are **opaque JSON** — the dashboard stores and displays them verbatim.

Semantic conventions (follow them for a clean dashboard):
- **`main`** — the single headline metric, e.g. `{"name":"Total Resolved","value":437}` (swe-verified), `{"name":"Score","value":66.3}` (swe-atlas, a percentage), `{"name":"Solved","value":59}` (terminal-bench, a count). Only `main.value` feeds the leaderboard (upsert-if-higher). Pick something monotonic-better-is-higher.
- **`secondary`** — a **flat** map of scalar → number/string, rendered as dashboard columns. No nested objects here. E.g. `{"pass@1":0.87,"resolved":437,"unresolved":63,"total_tasks":500}`.
- **`additional`** — nested detail objects for drill-down. Put per-pass breakdowns, error buckets, methodology strings, per-task detail here.

`RULE:` Write the file **atomically** and **always**. Atomic = write to `.tmp` then `os.replace()` (or `mv`), so a crash mid-write never leaves a half-file. Always = the **EXIT-trap zero-metric fallback** (§8.6): install a `trap` that writes a `{"metrics":{"main":{"name":...,"value":0},...,"additional":{"status":"no-results","reason":...}}}` file if none exists, and disarm it (`trap - EXIT`) only after the real write. Otherwise a crash or a bug that exits 0 without results → the run is marked FAILED (§2.5) and you get nothing.

Failure semantics (exact):
- Results parse is **non-fatal** — a malformed file is logged, not crashed on (`job_executor.rs:115` uses `.ok()`).
- BUT `run.sh` exiting 0 with **no results file at all** → the run is marked **FAILED** with "No results file produced by run.sh" (`main.rs:281-283`).
- The backend additionally validates `main.name` is non-empty before persisting (`dto.rs:772-778`).

Persistence (for context; you don't touch this): the runner POSTs the metrics to `/api/v1/runner/eval-runs/:id/results`; the backend inserts `eval_results`, upserts `leaderboard_entries` (only if the new `main_metric_value` is higher), and flips the run + pod to COMPLETED in one transaction. Columns: `main_metric_name TEXT`, `main_metric_value DOUBLE PRECISION`, `secondary_metrics JSONB`, `additional_metrics JSONB`.

### 4.6 Log sync — what gets uploaded, when, and the bloat rule

The runner uploads to GCS under `evals/{eval_id}/runs/{run_id}/` on two cadences (`eval-runner/src/log_sync.rs`):

| Subtree | Cadence | Mode |
|---|---|---|
| `runner_logs/` | every **30s** (`WORKER__LOG_SYNC_INTERVAL_SECS`) | **full** re-upload every tick — these are the live, small logs the dashboard tails |
| `repo/output/` | every **150s** (interval × `WORKER__LOG_SYNC_REPO_EVERY_N_TICKS`, default 5) | **incremental** — a file is skipped if `(mtime,size)` is unchanged |
| `repo/logs/` | every **150s** | **incremental** |

`RULE:` **Only** `runner_logs/`, `repo/output/`, and `repo/logs/` are ever uploaded. Nothing else in the repo tree is synced. A convenience `artifacts.zip` of these three is produced at the end.

`RULE:` (restating §2.7, because it is the #1 self-inflicted wound) Keep large/transient data **out of `output/` and `logs/`**. Because `runner_logs/` is uploaded *in full every 30s* and the repo subtrees are walked for incremental diffing, a giant tree under `output/` throttles the whole sync loop and starves the live logs. Put workspaces/checkouts/scratch in a non-synced dir and `.gitignore` it (§11.2). This is exactly what `swe-auto-eval` fixed by moving agent workspaces from `output/<run>/workspaces` to a top-level `workspaces/` passed via `--workspaces_root` (`swe-auto-eval/run.sh:262-266`).

`RULE:` To make the dashboard "live", your `run.sh` must **stream progress into a file under `runner_logs/` or its own stdout** (which the runner captures to `runner.log` under `runner_logs/`) as it goes, and **flush line-buffered** (§7.3, the `stdbuf` re-exec). A run that only prints at the end looks hung for hours.

### 4.7 Cancellation — how "Cancel" actually stops your VM

Understanding this prevents the "cancel button does nothing" class of bug and tells you how your `run.sh` should behave on SIGTERM.

1. User cancels → backend sets `evaluation_runs.status='CANCELLED'` and marks `runner_pods` CANCELLED with `cancel_requested_at=now`, **without** setting `runner_pods.completed_at` (it must stay NULL). (`handlers.rs:1431-1514`.) `WHY:` the scheduler's sweep selects on `completed_at IS NULL`; pre-stamping it hides the pod from the sweep and the VM never dies. (`memory: xyne-logsync-cancel-fixes`.)
2. Cooperative path: the runner's heartbeat sees `cancel_requested` in the `/heartbeat` response and trips a cancellation token — equivalent to SIGTERM — so it kills your `run.sh`, does a final log flush + `artifacts.zip`, and exits cleanly.
3. Backstop: the scheduler sweep (`find_cancelled_pods`, 300s grace) finds pods that are `CANCELLED AND completed_at IS NULL` and issues the Batch `:cancel`, then stamps `completed_at`.

`RULE:` Make `run.sh` **crash- and cancel-safe**: save incremental progress (patches, predictions, transcripts) to disk **immediately** as each unit completes, and make your EXIT trap flush a results file. Then a SIGTERM (cancel) or a VM death loses at most the in-flight unit, not the whole run (§11.3).

### 4.8 CHECKLIST: contract compliance

`CHECKLIST:` Your repo satisfies the contract iff:
- [ ] `setup.sh` and `run.sh` exist at repo root and run under `bash` with no shebang dependency.
- [ ] `setup.sh` reads no run parameters from argv.
- [ ] `run.sh` parses `[key] eval_run_id --flags` per §4.2 and tolerates unknown flags.
- [ ] `run.sh` resolves `OUTPUT_ROOT="${EVAL_RUNNER_OUTPUT_DIR:-./output}"`.
- [ ] `run.sh` writes `${OUTPUT_ROOT}/${eval_run_id}_results.json` in the `{"metrics":{"main":{name,value},...}}` shape, atomically.
- [ ] An EXIT trap writes a zero-metric fallback results file if the real one is missing, and is disarmed after the real write.
- [ ] Large/transient data lives outside `output/` and `logs/` and is `.gitignore`d.
- [ ] Progress is streamed line-buffered so the dashboard log tail is live.

---

## Section 5 — Registering the eval on the dashboard

Onboarding is not just writing scripts — the dashboard must *know* about your benchmark. There is **no manifest file, no DB seed script, no config directory** you commit. A benchmark is a **row in the `evals` table**, created by a human via the frontend form or by `POST /api/v1/evals` (`handlers.rs:207-296`, `CreateEvalRequest` `dto.rs:307-333`).

### 5.1 The fields of an eval

| Field | Required | Meaning / constraint |
|---|---|---|
| `name` | yes | display name of the benchmark |
| `description` | no | free text |
| `domain` | no | grouping/category |
| `version` | no | your versioning |
| `metadata` | no | arbitrary JSON |
| `repo_url` | **yes** | the git URL of your benchmark repo |
| `branch` | no | branch to clone (`--single-branch`) |
| `commit_sha` | no | commit to check out after clone (pin) |
| `machine_type` | no | one of `n2-standard-{2,4,8,16,32}`; default `n2-standard-8` |
| `input_params` | no | the `{fields:[...]}` schema (§6) that drives the "Run Eval" form |

`RULE:` Pin your repo with `branch` and/or `commit_sha` for reproducible runs. The runner does `git clone --branch <b> --single-branch` then `git checkout <sha>` (`run_targeted_clone`, `job_executor.rs:293-370`); on any failure it warns and falls back to a plain default-branch clone, so a bad pin degrades to "latest default branch" rather than hard-failing.

### 5.2 Repo access — public vs Juspay org + token

This is a real onboarding gate people miss.

`DECISION:` Is your benchmark repo public?
- **Public** → the runner does a plain `git clone` (`run_plain_clone`, `job_executor.rs:256-291`). Nothing else needed.
- **Private** → the runner rewrites the URL to `https://x-access-token:${GITHUB_TOKEN}@github.com/...` and clones with the injected `GITHUB_TOKEN` secret (`authenticated_repo_url`, `job_executor.rs:242-254`; token scrubbed from logs). **For this to work, the repo must be in the Juspay GitHub org (or otherwise reachable by that token).** A private repo *outside* the token's reach will fail to clone.

`RULE:` Before registering a private repo, confirm it is **either public or added to the Juspay org and reachable by the dashboard's `GITHUB_TOKEN`**. If the create-form's branch/commit dropdowns are empty, the backend (which lists branches/commits via `GITHUB_TOKEN_SEPT`, `evals/mod.rs:17-18`) cannot see your repo — that is your signal the token lacks access. Fix access before proceeding; do not work around it.

`CHECKLIST:` Repo access:
- [ ] Repo is public, **or** in the Juspay org with the dashboard token granted read access.
- [ ] The create-eval form can list your branches/commits (proves token visibility).
- [ ] `branch`/`commit_sha` chosen and known-good.

### 5.3 Choosing `machine_type`

`DECISION:` Pick `machine_type` by your parallelism and disk pressure:
- Non-agentic / light → `n2-standard-2` or `n2-standard-4`.
- Agentic, moderate concurrency, per-instance images → `n2-standard-8` (default) or `n2-standard-16`.
- High concurrency (10+ workers) with multi-GB per-instance image pulls/builds → `n2-standard-16`/`32` **and** the reaper (§9.4). More workers = faster disk fill; size the machine and the free-space panic floor together.

`RULE:` `machine_type` must be one of the allow-listed values (`n2-standard-2/4/8/16/32`); the backend and a DB CHECK reject anything else (`dto.rs:257-283`).

### 5.4 What the frontend "Run Eval" flow does with your schema

- The form reads `input_params.fields` and renders one input per field (`run-eval-dialog.tsx:58-62`), applying `required`/`default`/`options`/`constraints`.
- On submit it coerces number fields via `Number()` and everything else as string, drops empty values, and builds the run's `input_params` object (`buildInputParams`, `run-eval-dialog.tsx:64-72`).
- The backend validates that object against your schema on run creation (`validate_input_params`, `dto.rs:195-237`): required-present, type-correct, min/max, regex, options, HH:MM bounds. A run that violates the schema is rejected before any VM boots.
- The model field is special-cased by the FE to bind to the selected model/version alias (`run-eval-dialog.tsx:52,265-267`).

### 5.5 Registration runbook

`CHECKLIST:` To register:
- [ ] Repo access confirmed (§5.2).
- [ ] `setup.sh`/`run.sh` present and pushed to the branch/commit you'll pin.
- [ ] `input_params` schema authored (§6) and validated to render the form you want.
- [ ] Create the eval (frontend form or `POST /api/v1/evals`) with `name`, `repo_url`, `branch`/`commit_sha`, `machine_type`, `input_params`.
- [ ] Do a **smoke run** with a tiny `task_range` (§2.10) before announcing it.

---

## Section 6 — `input_params` deep dive

The `input_params` schema is the bridge between the "Run Eval" form and your `run.sh` flags. Getting it right means a human can configure a run from the UI and your script receives clean, validated flags.

### 6.1 The format

`input_params` is JSON stored on the eval row. Top-level shape is an object with a `fields` array (a bare top-level array is also accepted) (`dto.rs:195-206`):

```json
{ "fields": [ <InputParamField>, <InputParamField>, ... ] }
```

Some repos ship this as a file in-repo for reference (`swe-atlas-artificial-analysis/input_param.json`, `swe-verified/input_config_schema.json`); others define it only on the dashboard (`terminal-bench-v2-agentic` ships none). **The file in your repo is documentation/convenience — the authoritative copy is the `evals.input_params` column.** Keep them in sync.

### 6.2 The `InputParamField` schema (exact)

Each field (`dto.rs:22-62`):

| Key | Type | Notes |
|---|---|---|
| `name` | string | the param key; becomes `--name-with-hyphens` on run.sh (§4.3) |
| `type` | enum | one of `text`, `email`, `password`, `number`, `select`, `boolean`, `time` (lowercase) |
| `label` | string | UI label (optional; defaults to `name`) |
| `description` | string | UI help text (optional) |
| `required` | bool | default `false` |
| `default` | any | pre-filled value in the form |
| `options` | string[] | enum choices (for `select`, or a text field rendered as a dropdown) |
| `constraints` | object | see below |

`constraints` supports (by type):
- string types (`text`/`email`/`password`): `min_length`, `max_length`, `regex`
- `number`: `min`, `max`
- `time`: `start`, `end` (as `"HH:MM"`)

`RULE:` Use `type: "password"` for anything secret you must expose in the form (rare — the Grid key is delivered out-of-band, §4.2). Use `regex` on free-text fields that must be structured (e.g. `task_range` → `^[0-9]+-[0-9]+$`). Use `min`/`max` on numbers to stop absurd values (workers, timeouts). Use `options` to constrain agent/model/split choices to what your `run.sh` actually supports.

### 6.3 How fields reach `run.sh` (and the limitations)

Restating §4.3 in the input-params frame:
- `foo_bar: "x"` → `--foo-bar x`
- `count: 8` → `--count 8`
- `flag: true` → `--flag` ; `flag: false` → *(omitted)*
- `items: ["a","b"]` → `--items a --items b`

`RULE:` Limitations to design around:
- **Everything arrives as a string on the command line.** Your `run.sh` must parse/validate types itself (numbers, ranges). The FE coerces number fields for storage, but they still land as `--count 8` text.
- **`false` booleans vanish.** If your logic needs an explicit "off", model it as a `select` with `["true","false"]` (text) rather than a `boolean`, so `run.sh` always receives the value. `swe-verified` does exactly this for `resume` (`options:["true","false"]`).
- **No nested/object params.** The schema is a flat list of scalar fields. Anything structured must be encoded as a string (e.g. JSON in a text field) and parsed by `run.sh`, or split into multiple flat fields.
- **Unknown flags must not crash you.** The runner may pass a flag your `run.sh` doesn't know (schema drift). Keep an `EXTRA_FLAGS` catch-all (§4.3).

### 6.4 Conventions we keep (so evals feel consistent)

`RULE:` Standardize these field names/semantics across benchmarks:
- `model` (**required**, text) — the model id/alias. FE binds it to the selected model version.
- `base_url` (text, default `https://grid.ai.juspay.net/v1`) — the OpenAI-compatible endpoint. Note the `/v1` suffix here is for OpenAI-style clients; your `run.sh` strips it for Anthropic-style clients (`${BASE_URL%/v1}`, §12).
- `task_range` / `range` (text, regex `^[0-9]+-[0-9]+$`) — inclusive instance range, e.g. `0-49`. **Beware off-by-one** in how your harness interprets it (§8.7).
- concurrency knob (`num_workers` / `concurrency` / `parallel_instances`, number, min/max) — sized to the machine type.
- iteration/timeout knobs (`max_iterations`, `eval_timeout`, `agent_timeout`, number, min/max).
- `agent` / `coding_agent` (text, `options:[...]`) — for agentic evals, the agent to drive (only list agents your `run.sh` supports).
- `resume` (select `["true","false"]`) — resume a partially-completed run.

### 6.5 The four reference schemas (copy the closest one)

**swe-verified — `input_config_schema.json` (7 fields):**

| name | type | required | default | constraints |
|---|---|---|---|---|
| `model` | text | yes | — | — |
| `base_url` | text | no | `https://grid.ai.juspay.net/v1` | — |
| `num_workers` | number | no | 4 | min 1, max 32 |
| `max_iterations` | number | no | 100 | min 10, max 500 |
| `eval_timeout` | number | no | 7200 | min 600, max 14400 |
| `task_range` | text | no | `""` | regex `^[0-9]+-[0-9]+$` |
| `resume` | text | no | `"false"` | options `["true","false"]` |

**swe-atlas — `input_param.json` (11 fields):**

| name | type | required | default | options / constraints |
|---|---|---|---|---|
| `model` | text | yes | `private-large` | — |
| `judge_model` | text | no | `private-large` | — |
| `agent` | text | yes | `xyne-cli` | options `xyne-cli, claude-code, codex, gemini-cli, opencode` |
| `split` | text | no | `qa` | options `qa, tw, rf` |
| `attempts` | number | no | 3 | min 1, max 5 |
| `concurrency` | number | no | 16 | min 1, max 16 |
| `range` | text | no | `0-123` | regex `^[0-9]+-[0-9]+$` |
| `base_url` | text | no | `https://grid.ai.juspay.net/v1` | — |
| `override_cpus` | number | no | — | min 1, max 32 |
| `agent_timeout` | number | no | — | min 1, max 10 |
| `log_level` | text | no | `info` | options `debug, info, warn, error` |

**terminal-bench — none in-repo.** Defined on the dashboard; `config.yaml` carries the defaults (`agent`, `model`, `concurrency`, `attempts`, `default_task`). If you follow this pattern, document the dashboard schema somewhere in your repo README so it isn't lost.

**swe-auto-eval — dashboard-side**, mapping to run.sh flags: `model` (required), `coding_agent` (required, options), `parallel_instances` (number min 2 max 30), `runner_timeout` (number min 1200 max 10000), plus multipass knobs.

### 6.6 CHECKLIST: input params

`CHECKLIST:`
- [ ] `model` is present and required.
- [ ] Every `options` list matches exactly what `run.sh` supports.
- [ ] Numeric knobs have sane `min`/`max`.
- [ ] `task_range`/`range` has the `^[0-9]+-[0-9]+$` regex.
- [ ] Booleans that need an explicit "off" are modeled as `select ["true","false"]`.
- [ ] The in-repo schema file (if any) matches the `evals.input_params` column.
- [ ] `run.sh` tolerates unknown flags.

---

## Section 7 — Authoring `setup.sh`

`setup.sh` provisions the container: system packages, Docker CLI, language runtimes, the harness, agent binaries, cloud auth, datasets, and any harness patches. It runs once, as root, with no args, cwd = repo root. It must be **idempotent-ish** (safe to re-run) and **soft-failing** (a flaky apt mirror must warn, not abort the whole eval).

### 7.1 The canonical annotated template

`TEMPLATE:` Start from this and delete the parts you don't need. Every block is annotated with the rule it enforces and the incident behind it.

```bash
#!/usr/bin/env bash
# setup.sh — provisioning for <YOUR BENCHMARK> on the xyne-eval-ops-dashboard.
# Runs as root inside the Debian eval-runner container. No args. cwd = repo root.

# --- 7.3 line-buffered logs: re-exec under stdbuf so the dashboard tail is live ---
if [ -z "${STDBUF_APPLIED:-}" ] && command -v stdbuf >/dev/null 2>&1; then
    export STDBUF_APPLIED=1
    exec stdbuf -oL -eL "$0" "$@"
fi

# --- 7.2 error mode: set -u (catch unset vars) but NOT set -e (soft-fail steps) ---
set -u
export DEBIAN_FRONTEND=noninteractive
export TZ=Etc/UTC

log()  { echo "[setup] $*"; }
warn() { echo "[setup] WARNING: $*" >&2; }
die()  { echo "[setup] FATAL: $*" >&2; exit 1; }

# --- 7.4 sudo wrapper: root has no sudo binary on Batch ---
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# --- 7.5 apt: soft-fail so a flaky mirror warns instead of aborting the eval ---
apt_get() { $SUDO apt-get "$@" -y -qq || warn "apt-get $* failed (continuing)"; }
apt_get update
apt_get install ca-certificates curl git jq tar zstd    # add tmux if your agent needs a TTY

# --- 7.6 Docker: CLI ONLY under DooD, pinned to 24.x; NEVER the engine ---
install_docker_cli() {
    if command -v docker >/dev/null 2>&1; then log "docker present"; return 0; fi
    # add docker apt repo (keyrings + sources) here ...
    $SUDO install -m0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null || warn "docker gpg failed"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
        | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
    apt_get update
    if [ -S /var/run/docker.sock ]; then
        # DooD: install client + buildx ONLY (host runs the daemon)
        local CLI_24; CLI_24=$(apt-cache madison docker-ce-cli 2>/dev/null | awk -F'[| ]+' '$3~/^5:24\./{print $3;exit}')
        if [ -n "$CLI_24" ]; then apt_get install "docker-ce-cli=$CLI_24"; else apt_get install docker-ce-cli; fi
        apt_get install docker-buildx-plugin        # ONLY if you `docker build`
    else
        # bare VM (dev): full engine
        apt_get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin
        $SUDO systemctl start docker 2>/dev/null || $SUDO service docker start 2>/dev/null || warn "cannot start dockerd"
    fi
}
install_docker_cli
docker info >/dev/null 2>&1 || warn "docker daemon not reachable yet"

# --- 7.7 Artifact Registry auth: writable base probe + metadata-token docker login ---
ARTIFACT_REGISTRY_HOST="us-central1-docker.pkg.dev"
GCLOUD_BASE="/var/lib/docker"
if ! mkdir -p "$GCLOUD_BASE" 2>/dev/null || ! touch "$GCLOUD_BASE/.wp" 2>/dev/null; then
    GCLOUD_BASE="$HOME/.gcloud-eval"; mkdir -p "$GCLOUD_BASE"
    log "/var/lib/docker not writable — using $GCLOUD_BASE"
else rm -f "$GCLOUD_BASE/.wp"; fi
export DOCKER_CONFIG="$GCLOUD_BASE/docker"; mkdir -p "$DOCKER_CONFIG"

authenticate_ar() {
    # primary: metadata-server OAuth token (install-free, works on any GCP VM)
    local tok
    tok=$(curl -s --max-time 5 -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
        | python3 -c 'import json,sys;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
    if [ -n "$tok" ] && echo "$tok" | docker login -u oauth2accesstoken --password-stdin "https://$ARTIFACT_REGISTRY_HOST" >/dev/null 2>&1; then
        log "AR auth via metadata token OK"; return 0
    fi
    # secondary: gcloud tarball install (COS root fs is read-only for apt) + configure-docker
    warn "metadata-token login failed; trying gcloud (optional)"
    # ... download google-cloud-sdk tarball into $GCLOUD_BASE, then:
    # gcloud auth configure-docker "$ARTIFACT_REGISTRY_HOST" --quiet || warn "configure-docker failed"
}
authenticate_ar
# persist env so run.sh can restore it
cat > "$GCLOUD_BASE/gcloud-env.sh" <<EOF
export DOCKER_CONFIG="$DOCKER_CONFIG"
export PATH="$PATH"
EOF

# --- language runtimes / package manager ---
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
command -v uv >/dev/null 2>&1 || die "uv install failed"      # HARD fail: nothing works without it

# --- 7.8 agent binaries (agentic evals): npm platform siblings, latest-adaptive ---
# e.g. fetch @xyne/xyne-cli latest, then @xyne/xyne-cli-linux-<arch> sibling,
#      extract package/xyne-linux-<arch> into ./binaries/ (see §7.8)

# --- 7.9 harness install: vendored (uv tool install ./harbor) OR PyPI pinned ---
# e.g. uv tool install "harbor==0.13.1" --with-editable ./adapter --reinstall

# --- 7.10 harness DooD patch as a .pth (Debian sitecustomize shadowing trap) ---
# install your _dood.py + .pth into the harness venv site-packages; self-verify gate on/off

# --- 7.11 dataset restore from GCS (if your eval ships a dataset tarball) ---
# scripts/fetch_dataset_tarball.sh   # gs://xyne-eval-dashboard/<bench>/datasets/*.tar.zst + .sha256

# --- final verification summary (warn-only) ---
log "docker: $(command -v docker || echo MISSING)  socket: $([ -S /var/run/docker.sock ] && echo yes || echo no)"
log "uv: $(command -v uv || echo MISSING)"
log "setup complete"
```

### 7.2 `set -u`, not `set -e`

`RULE:` Use `set -u` (fail loudly on unset variables — catches typos and missing env) but **not** `set -e`. `WHY:` package installs and cloud-auth are best-effort; a transient apt mirror failure with `set -e` aborts the *entire* eval before it starts. Instead, soft-fail those steps (`|| warn ...`) and `die`/`exit 1` **explicitly** only on genuinely fatal steps (no `uv`, harness build failed, DooD patch broken in a DooD env). This is the pattern in `swe-verified/setup.sh:17-18`, `swe-atlas`, `terminal-bench`.

### 7.3 The `stdbuf -oL -eL` re-exec

`RULE:` Re-exec the script under `stdbuf -oL -eL` at the very top. `WHY:` the runner redirects your stdout/stderr to a log file; without line buffering, output is block-buffered and the dashboard tail shows nothing until a 4KB buffer flushes — the run looks hung. Line-buffering makes logs live. (All four repos do this.)

### 7.4 The `SUDO=""` wrapper

Covered in §3.2. `RULE:` never call `sudo` unconditionally; the runner is root-without-sudo.

### 7.5 Never assume a path is writable — probe

`RULE:` Before using `/var/lib/docker` (or any host-adjacent path) as a state dir, **write-probe** it and fall back to `$HOME/.gcloud-eval`. `WHY:` `/var/lib/docker` is writable on a bare COS VM but not from inside the runner container in some configs; hardcoding it caused auth-state write failures. (`swe-verified/setup.sh:132-149`.)

### 7.6 Docker CLI only, pinned 24.x

Covered in §3.3. `RULE:` `[ -S /var/run/docker.sock ]` present ⇒ install `docker-ce-cli` (pinned `5:24.*`) + `docker-buildx-plugin` (only if you build), never the engine. `WHY:` a full engine install tries to run a daemon that can't start under DooD; the 24.x pin matches the host daemon's client/server protocol. buildx is required if any code path runs `docker buildx build` (e.g. OpenHands agent-server builds with `FORCE_BUILD=1`). (`swe-verified/setup.sh:48-103`.)

### 7.7 Artifact Registry / gcloud auth

`RULE:` Primary auth = **metadata token → `docker login`** (install-free). Secondary = `gcloud`, and if you install `gcloud` it MUST be the **tarball** install into a writable base, not apt. `WHY:` COS/Debian-runner root fs constraints; apt-installing gcloud is unreliable and slow, the tarball into `$GCLOUD_BASE/google-cloud-sdk` is deterministic. Persist `DOCKER_CONFIG`/`PATH`/gcloud state into `$GCLOUD_BASE/gcloud-env.sh` so `run.sh` can `source` it (the metadata token expires ~hourly; `run.sh` refreshes it before each phase, §8.4). (`swe-verified/setup.sh:151-224`, `swe-auto-eval/setup.sh:1069-1165`.)

### 7.8 Agent binary fetch (agentic evals)

`RULE:` For `xyne-cli`, fetch the linux binary from the **npm platform-sibling packages**, not the main tarball. Since `@xyne/xyne-cli >= 0.1.2` the main tarball no longer ships linux binaries; they live in `@xyne/xyne-cli-linux-x64` / `@xyne/xyne-cli-linux-arm64`, at path `package/xyne-linux-<arch>` inside the sibling. Resolve `latest` (per §2.9, internal agents run at head), then curl the sibling for the matching arch and extract just the binary into `./binaries/`. (`memory: tbench-batch-scheduler-mount-fix`; `swe-atlas/setup.sh:456-539`, `terminal-bench/setup.sh:441-537`.)

`RULE:` (§2.9 restated) Because you fetch `latest`, make capture **version-adaptive** in `run.sh`/the adapter (§11.4) — never let a CLI version bump silently zero the scores.

### 7.9 Harness install — vendored vs pinned

`DECISION:` How do you install the harness?
- **Vendored** (harbor lives in your repo, e.g. `swe-atlas/harbor/`, version 0.6.6): `uv tool install --python 3.12 ./harbor --reinstall`. The vendored source *is* the pin. Use when you must patch harbor internals or the version isn't on PyPI.
- **PyPI pinned** (e.g. `terminal-bench` uses `harbor==0.13.1`): `uv tool install "harbor==0.13.1" --with-editable ./adapter --reinstall`. Use when the published version works and you only add a small editable adapter package.

`RULE:` Whichever you choose, **pin it** (a vendored dir or an exact `==` version). Different harbor majors have **different `harbor run` flag names** (0.6.6: `-p -c -e -m -k -n`; 0.13.1: `--dataset --model --n-attempts --n-concurrent`). Your `run.sh` invocation must match the installed version.

### 7.10 Harness patches as `.pth` files — and the Debian shadowing trap

Some fixes require monkeypatching the harness at import time (the DooD health-check fix, the harbor `mounted=False` flip; §9). You install these from `setup.sh` into the harness venv's `site-packages`.

`RULE:` Install the patch as a **`.pth` file**, not a `sitecustomize.py`. `WHY:` Debian ships `/usr/lib/pythonX.Y/sitecustomize.py`, which **shadows** a venv-local `sitecustomize.py` — your patch never loads. A `.pth` file with an `import` line is executed by `site.py` at every interpreter start regardless. (Caught during local testing; `memory: tbench-batch-scheduler-mount-fix`, `swe-verified/setup.sh`.)

`TEMPLATE:` `.pth` patch install:
```bash
install_pth_patch() {
    local sp; sp=$(uv run python -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')
    cp scripts/my_patch.py "$sp/my_patch.py"
    printf 'import my_patch\n' > "$sp/my_patch.pth"
    # self-verify BOTH gate states (gate OFF => no patch; gate ON => patched)
    uv run python -c 'import my_patch; assert not my_patch.PATCHED' || die "patch active without gate!"
    MY_GATE=1 uv run python -c 'import my_patch; assert my_patch.PATCHED' || die "patch inert with gate!"
}
```

`RULE:` **Env-gate every patch** (e.g. `SWEV_DOOD_HOST_FIX=1`, `TB_HARBOR_UNMOUNTED=1`) so it is inert on native VMs and only engages under DooD, and **self-verify both gate states** in `setup.sh`. If the patch fails to install **and** a DooD env is detected (`[ -S /var/run/docker.sock ] && [ -f /.dockerenv ]`), **hard-fail setup** (`exit 1`) rather than letting the run fail 20 minutes in.

### 7.11 Dataset restore

`RULE:` If your benchmark needs a dataset that isn't in git, restore it from GCS in `setup.sh` (`gs://xyne-eval-dashboard/<bench>/datasets/<name>.tar.zst` + a `.sha256` side-car). Fetch via the metadata token + GCS REST (or `gsutil` if installed). `DECISION:` hard-fail if the dataset is mandatory and missing (swe-atlas), or fall back to the harness's own downloader if it publishes the dataset (terminal-bench's `harbor download`). If metadata hostname resolution is flaky, use the metadata **IP** `169.254.169.254` (§3.4). (`memory: swe-atlas-xyne-v030-capture-break`.)

### 7.12 CHECKLIST: setup.sh

`CHECKLIST:`
- [ ] `stdbuf -oL -eL` re-exec at top.
- [ ] `set -u`, no `set -e`; soft-fail apt/auth; explicit `die` only on fatal steps.
- [ ] `SUDO=""` wrapper; no unconditional `sudo`.
- [ ] Docker: CLI-only + buildx (if building), pinned 24.x, under DooD; engine only on bare VM.
- [ ] AR auth via metadata token; state persisted to `gcloud-env.sh`; gcloud (if any) via tarball into a write-probed base.
- [ ] Agent binaries fetched from npm siblings (agentic); capture is version-adaptive.
- [ ] Harness installed and pinned (vendored dir or `==`); run.sh flags match the version.
- [ ] Harness patches installed as `.pth`, env-gated, dual-gate self-verified, hard-fail if broken in DooD.
- [ ] Dataset restored (if any) with checksum verification.
- [ ] `bash -n setup.sh` clean; shellcheck clean.

---

## Section 8 — Authoring `run.sh`

`run.sh` is the eval entrypoint. It parses args, restores auth, sets up model credentials, runs the eval (possibly under a harness, possibly with a reaper watchdog), streams rich progress, and — always — writes the canonical results JSON. It receives `[grid_key?] eval_run_id --flags` and runs as root, cwd = repo root.

### 8.1 The canonical annotated template

`TEMPLATE:` The skeleton every `run.sh` should follow. Fill in the "run the eval" middle with your harness/runner.

```bash
#!/usr/bin/env bash
# run.sh — <YOUR BENCHMARK> entrypoint for the xyne-eval-ops-dashboard.

# --- line-buffered logs ---
if [ -z "${STDBUF_APPLIED:-}" ] && command -v stdbuf >/dev/null 2>&1; then
    export STDBUF_APPLIED=1; exec stdbuf -oL -eL "$0" "$@"
fi
set -uo pipefail                      # NOT -e: we want the EXIT trap to run our fallback
export PYTHONUNBUFFERED=1 NO_COLOR=1  # keep harness output plain + live

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log_info(){ echo "[run][INFO]  $*"; }
log_ok(){   echo "[run][OK]    $*"; }
log_warn(){ echo "[run][WARN]  $*" >&2; }
log_err(){  echo "[run][ERR]   $*" >&2; }
log_step(){ echo; echo "===== $* ====="; }

# --- 8.2 positional arg parse: [API_KEY] EVAL_RUN_ID --flags ---
API_KEY=""; EVAL_RUN_ID=""
if [ -n "${1:-}" ] && [ "${1#--}" = "$1" ] && [ -n "${2:-}" ] && [ "${2#--}" = "$2" ]; then
    API_KEY="$1"; EVAL_RUN_ID="$2"; shift 2
elif [ -n "${1:-}" ] && [ "${1#--}" = "$1" ]; then
    EVAL_RUN_ID="$1"; shift
else
    EVAL_RUN_ID="local_$(date +%Y%m%d_%H%M%S)"
fi
[ -z "$API_KEY" ] && API_KEY="${GRID_AI_API:-}"     # fall back to the injected env secret

# --- 8.3 flag parse: known flags -> vars; unknown -> forwarded ---
MODEL="private-large"; BASE_URL="https://grid.ai.juspay.net/v1"; TASK_RANGE=""
declare -a EXTRA_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --model)       MODEL="$2"; shift 2;;
    --base-url)    BASE_URL="$2"; shift 2;;
    --task-range)  TASK_RANGE="$2"; shift 2;;
    --*)           EXTRA_FLAGS+=("$1" "${2:-}"); shift 2;;   # forward unknowns
    *)             shift;;
  esac
done

# --- 8.4 restore auth persisted by setup.sh; refresh the ~hourly AR token ---
for f in /var/lib/docker/gcloud-env.sh "$HOME/.gcloud-eval/gcloud-env.sh"; do
  [ -f "$f" ] && . "$f"
done
refresh_docker_auth() {
  local tok; tok=$(curl -s --max-time 5 -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
  [ -n "$tok" ] && echo "$tok" | docker login -u oauth2accesstoken --password-stdin \
    https://us-central1-docker.pkg.dev >/dev/null 2>&1 || log_warn "AR token refresh failed"
}

# --- 8.5 model credential fan-out (agentic evals; §12) ---
export GRID_AI_API_KEY="$API_KEY" LITE_LLM_API_KEY="$API_KEY"
export ANTHROPIC_AUTH_TOKEN="$API_KEY" ANTHROPIC_BASE_URL="${BASE_URL%/v1}"
export OPENAI_API_KEY="$API_KEY" OPENAI_BASE_URL="$BASE_URL"
export GEMINI_API_KEY="$API_KEY"

# --- 8.6 output paths + always-armed EXIT-trap fallback results ---
OUTPUT_ROOT="${EVAL_RUNNER_OUTPUT_DIR:-${SCRIPT_DIR}/output}"; mkdir -p "$OUTPUT_ROOT"
RESULTS_FILE="${OUTPUT_ROOT}/${EVAL_RUN_ID}_results.json"
write_fallback_results() {
  [ -f "$RESULTS_FILE" ] && return 0
  local reason="${1:-unknown}"
  cat > "$RESULTS_FILE" <<JSON
{ "metrics": { "main": {"name":"<MAIN_METRIC>","value":0},
  "secondary": {"resolved":0,"total_tasks":0},
  "additional": {"status":"no-results","reason":"${reason}"} } }
JSON
  log_warn "wrote fallback zero-metric results (${reason})"
}
on_exit() { local rc=$?; stop_reaper 2>/dev/null || true; write_fallback_results "exit ${rc}"; }
trap on_exit EXIT

# --- 8.7 DooD gate + task range ---
DOOD_GATE=0
if [ -S /var/run/docker.sock ] && { [ -f /.dockerenv ] || [ -n "${EVAL_RUNNER_WORK_DIR:-}" ]; }; then
    DOOD_GATE=1; log_info "DooD environment detected"
fi
# task_range "0-49" -> N (inclusive). Beware your harness's semantics (§8.8).

# --- run the eval (harness / runner) with rich progress. e.g.:
log_step "Running eval"
# start_reaper            # §9.4, only if you build/pull images per instance
# <invoke harbor / swebench-infer / your runner here>, streaming progress

# --- 8.9 write the REAL results atomically, then disarm the trap ---
log_step "Writing results"
python3 - "$RESULTS_FILE" <<'PY'
import json, sys, os
# ... compute resolved/total/etc from the run's outputs ...
results = {"metrics": {
  "main": {"name": "<MAIN_METRIC>", "value": 0},
  "secondary": {"resolved": 0, "total_tasks": 0},
  "additional": {}
}}
p = sys.argv[1]; tmp = p + ".tmp"
open(tmp, "w").write(json.dumps(results, indent=2)); os.replace(tmp, p)   # atomic
PY
trap - EXIT                         # disarm the fallback; real results exist
log_ok "results at $RESULTS_FILE"
```

### 8.2 Positional args

Covered in §4.2. `RULE:` detect key-vs-id-vs-flags by the `--` prefix; synthesize `local_<ts>` for manual runs; fall back to `GRID_AI_API` env if no positional key.

### 8.3 Flag parsing

`RULE:` map known `--kebab-flags` to variables; collect unknowns into `EXTRA_FLAGS` and forward them to your harness/runner. Never `exit 1` on an unrecognized flag — schema drift must degrade gracefully (§4.3).

### 8.4 Restore + refresh auth

`RULE:` `source` the `gcloud-env.sh` that `setup.sh` wrote (try both base locations). The AR OAuth token expires ~hourly, so define `refresh_docker_auth` and call it **before each phase that pulls/pushes images** (inference, scoring). On long runs, an un-refreshed token causes mid-run pull failures. (`swe-verified/run.sh:56-104`.)

### 8.5 Model credential fan-out

Covered fully in §12. `RULE:` one key must be exported under every name the agents/judge read: `GRID_AI_API_KEY`, `LITE_LLM_API_KEY`, `ANTHROPIC_AUTH_TOKEN` (+ `ANTHROPIC_BASE_URL="${BASE_URL%/v1}"`), `OPENAI_API_KEY`/`OPENAI_BASE_URL`, `GEMINI_API_KEY`, and any `XYNE_*` your adapter reads. `WHY:` built-in harbor agents read creds from the process env, not from CLI flags — miss one and that agent fails auth in 150ms with zero patches (§12.3).

### 8.6 The EXIT-trap fallback (the anti-FAILED insurance)

`RULE:` (restating §2.5/§4.5) Install `trap on_exit EXIT` early, where `on_exit` writes a zero-metric results file **iff** one doesn't already exist, then disarm with `trap - EXIT` right after the real atomic write. `WHY:` any crash, cancel, or bug that exits before the real write → the run is marked FAILED and you get nothing; the fallback converts that into a visible zero-with-reason. All four repos do this; `swe-verified/run.sh:364-386` is the reference.

Set `pipefail` and `set -u` but **not** `set -e`: with `set -e`, a nonzero from any command jumps out before your trap logic can distinguish real-vs-fallback (the trap still runs, but you lose the chance to log context). The trap + explicit checks is the controlled path.

### 8.7 DooD gate

`RULE:` compute a `DOOD_GATE` boolean from `[ -S /var/run/docker.sock ]` AND (`[ -f /.dockerenv ]` OR `EVAL_RUNNER_WORK_DIR` set). Export the harness patch gates (`SWEV_DOOD_HOST_FIX`, `TB_HARBOR_UNMOUNTED`, `SA_HARBOR_UNMOUNTED`) **only** when `DOOD_GATE=1`, and **only on the specific command** that needs them (e.g. inline `SWEV_DOOD_HOST_FIX=1 uv run swebench-infer ...`), so native VM behavior is byte-identical. For harbor evals, run an actual `dood_shared_check` probe (start a throwaway container with a bind mount, see if the marker is visible to run.sh) to *prove* the topology before flipping the gate — don't just assume (§9.3). (`swe-atlas/run.sh:410-450`, `swe-verified/run.sh:468-476`.)

### 8.8 Task range — mind the off-by-one

`RULE:` A `task_range` like `0-49` is **inclusive** and means 50 tasks. If your harness takes a "first-N" limit (`--n-limit`), you must pass `END+1` (50), not `END` (49). Ranges not starting at 0 may not be expressible as a first-N limit — warn and degrade to `0-END`, or expand the range to explicit ids. `WHY:` `swe-verified` passed `--n-limit 49` for `0-49` and silently ran 49 instead of 50 until fixed to `END+1` (`swe-verified/run.sh:478-491`). Know exactly how *your* harness interprets the range and log it.

### 8.9 Writing results (atomic) + reconciliation

`RULE:` compute metrics from the run's real outputs, write via `.tmp` + `os.replace()`, then `trap - EXIT`. Keep `secondary` flat, `additional` nested (§4.5).

`RULE:` **Reconcile against attempt files, not just the final report**, when your harness silently drops errored instances. `WHY:` in `swe-verified`, `aggregate_results` skips rows whose final attempt errored, so a run where 3/4 instances died on a budget error reported `1/1 pass@1=1.0` instead of `1/4`. The fix reads the per-attempt stream files (`output.critic_attempt_*.jsonl`) to recover *every attempted* instance, computes `total = attempted`, `infer_errors = attempted − submitted`, and reports the honest denominator (`swe-verified/run.sh:566-674`). If your harness has an "errored instances vanish" behavior, reconcile the same way.

### 8.10 Rich logging for a live dashboard

The dashboard's value is watching a run progress. Make `run.sh` narrate.

`RULE:` Emit structured, greppable progress:
- **Helpers**: `log_info/log_ok/log_warn/log_err/log_step` (consistent prefixes, timestamps optional).
- **Heartbeat** (agentic/harbor): harbor's TUI goes dark on a non-TTY, so back-ground a `heartbeat()` loop that polls the jobs dir and logs one line per trial state transition (`started`, `finished reward=…`) plus a periodic progress summary; kill it when the harness returns. `WHY:` without it, a 3-hour harbor run prints nothing and looks hung. (`swe-atlas/run.sh:474-527`.)
- **Trial-artifacts rollup**: after the run, per-trial print `reward=… | verifier/:yes|MISSING | agent/:yes|MISSING`. `WHY:` this instantly distinguishes a *real* task failure from the DooD orphan bug (verifier dir missing = topology problem, not a model failure). (§9.3.)
- **Answer-capture health** (free-text-graded evals): tally `answer_source[...]` and loudly warn `N/M trials produced NO answer` with reasons, so a CLI contract change is a visible run-level warning, not a silent all-zero (§2.9, §11.4).
- **Scoreboard**: a final human-readable summary of the headline number.

### 8.11 CHECKLIST: run.sh

`CHECKLIST:`
- [ ] `stdbuf` re-exec; `set -uo pipefail` (not `-e`); `PYTHONUNBUFFERED=1`.
- [ ] Positional parse `[key] id --flags`; unknown flags forwarded, never fatal.
- [ ] Auth restored from `gcloud-env.sh`; token refreshed before each image phase.
- [ ] Model creds fanned out to every name the agents/judge read (§12).
- [ ] `OUTPUT_ROOT="${EVAL_RUNNER_OUTPUT_DIR:-./output}"`; EXIT-trap fallback armed early, disarmed after real write.
- [ ] DooD gate computed; harness patch gates exported only under DooD, only on the needed command.
- [ ] Task range semantics verified and logged (off-by-one handled).
- [ ] Results written atomically; totals reconciled against attempt files if the harness drops errored rows.
- [ ] Rich logging: helpers + heartbeat + trial rollup + capture health + scoreboard.
- [ ] Reaper started (if you build/pull per-instance images) and stopped before scoring (§9.4).
- [ ] `bash -n run.sh` clean; shellcheck clean; a local stub run exercises both the fallback and success paths.

---

## Section 9 — Docker under DooD: the deep chapter

If your eval touches Docker (agent sandboxes, per-task images, harness environments), this chapter is where most of your pain will and won't happen. Every issue here is a direct consequence of the architecture diagram in §1.5.

### 9.1 Recap: why DooD changes everything

- Your `docker` commands hit the **host** daemon. Containers are **siblings**, ports publish on the **host** netns, bind mounts resolve on the **host**, and the runner shares **only the socket** — no filesystem.
- So three things break that "just work" on a normal machine: (a) health checks that poll `localhost:<published_port>`, (b) reading files back from a host bind mount, (c) disk accounting (the host's overlay2 fills, not "your" disk — but it's the same physical disk, and it kills you).

### 9.2 The health-check netns split (and the candidate-ladder fix)

**Symptom:** an agent-server / service container starts fine ("Uvicorn running on http://0.0.0.0:8000") but the harness's health check times out for the full window (e.g. 120s) and the instance is declared unhealthy.

**Root cause (proven, not assumed):** the SDK/harness hardcodes the health URL to `http://127.0.0.1:<host_port>` and polls it from *inside the runner*. But `-p <host_port>:8000` publishes on the **COS host** netns. `127.0.0.1` inside the runner is not the host — so the poll hits a dead address while the server is up on the host. Container logs proved the server was up at T+11s while the poll failed for 109 more seconds. (`memory: swe-verified-dashboard-integration`, run 739ac5ae.)

**Fix (script-only, `.pth` monkeypatch): a candidate ladder.** Wrap the SDK's `_wait_for_health` so it probes, in order, until one returns 2xx on `/health`:
1. `http://127.0.0.1:<host_port>` — native/localhost-published (works on bare VMs).
2. `http://<default_gateway_ip>:<host_port>` — via the docker-proxy on the gateway.
3. `http://<agent_container_ip>:8000` — the container's own IP on each attached network.
4. After ~30s of failures: `docker network connect` the agent container onto the runner's own network(s), then retry its new IP.

On success, **rewrite the workspace's `host` field** (`object.__setattr__(self,"host",winner)`) so *all* downstream agent traffic uses the reachable address — not just the health check. On timeout, run a topology-independent `docker exec` probe *inside* the container (server-up proof), dump the network topology, then re-raise the **byte-identical original error** so the harness's retry classifier is unchanged. Gate the whole thing on `SWEV_DOOD_HOST_FIX=1`, installed as a `.pth` (§7.10). Reference implementation: `swe-verified/scripts/swev_dood_hostfix.py` (289 lines), installed by `swe-verified/setup.sh`.

`RULE:` If your harness health-checks a published port from inside the runner, you will hit this. Port the candidate-ladder pattern; do not try to fix it by changing the platform's network mode (§2.2).

### 9.3 The harbor mount fix (`capabilities.mounted=False`)

**Symptom:** every harbor trial no-grades — the reward/verifier/answer files come back empty even though the agent ran.

**Root cause:** harbor bind-mounts each trial dir into the task container and, by default, reads the verifier output and agent logs back **from the host bind source**. Under DooD the host daemon resolves that source on the COS host, not in the runner, so the read-back finds an empty orphan. Harbor's `DockerEnvironment.capabilities` hardcodes `mounted=True` (`harbor .../environments/docker/docker.py:238-242`); the read-back path branches on it (`verifier.py:190`, `trial.py:447-459`).

**Fix (script-only, `.pth` monkeypatch): flip `mounted` to `False`.** harbor already ships a DooD-safe path for its *cloud* backends (Modal/E2B/GKE): when `capabilities.mounted == False`, it fetches the verifier dir and agent logs via `docker compose cp` (socket-streamed, topology-independent) instead of reading the host bind source. The patch simply forces `DockerEnvironment` onto that path:

```python
# _tb_harbor_dood.py / _sa_harbor_dood.py  (installed as a .pth, env-gated)
import os
if os.environ.get("TB_HARBOR_UNMOUNTED") == "1":       # or SA_HARBOR_UNMOUNTED
    from harbor.environments.docker.docker import DockerEnvironment
    _orig = DockerEnvironment.capabilities.fget
    def _unmounted(self):
        return _orig(self).model_copy(update={"mounted": False})
    DockerEnvironment.capabilities = property(_unmounted)
```

`run.sh` sets the gate from an actual probe (`dood_shared_check`): start a throwaway container with a bind mount, write a marker inside, and check whether `run.sh` can see it. Not visible ⇒ no host-shared FS ⇒ `export TB_HARBOR_UNMOUNTED=1`. Inconclusive ⇒ fall back to the `[ -f /.dockerenv ]` heuristic. Host-shared (native VM) ⇒ leave unset (behavior identical to upstream). Reference: `terminal-bench/setup.sh:301-380` + `run.sh:345-386`, `swe-atlas/setup.sh:317-399` + `run.sh:410-450`. (`memory: tbench-batch-scheduler-mount-fix`.)

`RULE:` If you use harbor's `DockerEnvironment` under DooD, you need this patch. Trial bind-mounts still get created (harmless orphans inside containers — `docker compose cp` reads the container view). Verify locally in a scratch venv that the patch engages **only** with the env var and harbor still boots.

### 9.4 The disk / OOM saga and the pressure-triggered reaper

This is the chapter that cost the most hours. Read all of it if your eval builds or pulls images per instance.

**The failure modes we hit, in order:**
1. **ENOSPC from images.** Each instance pulled a multi-GB base image (twice: the AR tag + a `docker.io/swebench` re-tag) and built a per-instance agent-server image. Nothing deleted them (`--rm` covers containers, not images). ~3GB/instance × 66 ≈ 200GB → disk full at ~66 instances in 3h10m. Disk-full also killed the EXIT-trap fallback → the run produced **no results at all**.
2. **Per-instance-key reaping doesn't work.** A first reaper that grepped for each instance's id failed: the SDK tags each agent-server image **three** times and one variant **truncates the instance id and appends random hex** (`...django-1143_tag_latest-f83ac7d09257-source-minimal`) — an instance-key grep can never match it, so that tag pins the whole 5GB chain forever. Dangling images were 0 throughout; `docker image prune` freed ~nothing.
3. **Build cache is the *dominant* leak.** After images were tamed (~19.7GB), disk still filled to 193GB/100% in ~3h. `docker system df` showed Images 19.71GB but **Build Cache 154.1GB / 601 objects**. `FORCE_BUILD=1` rebuilds every instance, and each BuildKit build deposits cache layers that `docker rmi` **and** `docker image prune -f` **leave behind** — only `docker builder prune` removes them.
4. **Time-triggered reaping loses a race → VM OOM/ENOSPC crash.** A 180s-polling reaper is a race: at ~50GB/hr growth (10 workers) the disk fills 100% inside one 180s gap and the VM dies before the next pass — losing ~5 hours of work.

**The fix: a 30s pressure-triggered watchdog + repository-sweep reaper.** (`swe-verified/run.sh:106-271`.) Design:
- **`docker builder prune -f` on EVERY 30s tick** — inactive build cache never accumulates toward danger (active/in-progress builds are daemon-protected, so this is safe live).
- **Free-space panic**: every tick, `df` the working dir; if free < `REAPER_PANIC_FREE_KB` (40 GiB), immediately fire emergency `docker builder prune -af` + `docker image prune -f`, independent of the reap cadence. The excess above the working set (running containers + active builds, which prune skips) is always recoverable, so `-af` recovers or it's a real capacity problem (→ fewer workers).
- **Repository-sweep** (never per-instance-key) every ~180s: reap all `eval-agent-server` repo tags (catches the truncated `_tag_latest`) unless a container references them or they're younger than a 30-min age guard; then base images (`sweb.eval|<your-package>` minus the agent repo) with the same guards; then `docker image prune -f` + `docker builder prune -f`.

`TEMPLATE:` The reaper (adapt names/thresholds):
```bash
WATCHDOG_INTERVAL_SECONDS=30
REAPER_TICKS=6                                   # full image-reap pass ~every 180s
REAPER_MIN_AGE_SECONDS=1800                      # 30-min age guard
REAPER_PANIC_FREE_KB=$((40*1024*1024))           # emergency prune below 40 GiB free
REAPER_PID=""

# age check: skip if any container references it, or it's younger than the guard.
# NOTE: parse timestamps with `date -u -d "${stamp%%.*}"` — the %%.* trims sub-second
# + zone text so GNU date parses both Go formats; plain `date -d` mis-ages on non-UTC hosts.
image_reaper_eligible() {  # $1=image  $2=inspect time format (e.g. '{{.Metadata.LastTagTime}}')
  [ -n "$(docker ps -aq --filter "ancestor=$1" 2>/dev/null)" ] && return 1
  local stamp epoch; stamp=$(docker image inspect -f "$2" "$1" 2>/dev/null) || return 1
  epoch=$(date -u -d "${stamp%%.*}" +%s 2>/dev/null) || return 1
  [ $(( $(date +%s) - epoch )) -ge "$REAPER_MIN_AGE_SECONDS" ]
}
image_reaper_pass() {
  # 1) agent-server children (use {{.Created}} for freshness)
  for img in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -F 'eval-agent-server'); do
    image_reaper_eligible "$img" '{{.Created}}' && docker rmi "$img" >/dev/null 2>&1 || true
  done
  # 2) base images (use {{.Metadata.LastTagTime}} — .Created is the months-old upstream date, useless)
  for img in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'sweb\.eval|<YOUR_PACKAGE>' | grep -v 'eval-agent-server'); do
    image_reaper_eligible "$img" '{{.Metadata.LastTagTime}}' && docker rmi "$img" >/dev/null 2>&1 || true
  done
  docker image prune -f >/dev/null 2>&1 || true
  docker builder prune -f >/dev/null 2>&1 || true
}
image_reaper_loop() {
  set +e; local tick=0 avail
  while true; do
    sleep "$WATCHDOG_INTERVAL_SECONDS"; tick=$((tick+1))
    docker builder prune -f >/dev/null 2>&1 || true                      # cheap, every tick
    avail=$(df -Pk . 2>/dev/null | awk 'NR==2{print $4}')
    if [ -n "$avail" ] && [ "$avail" -lt "$REAPER_PANIC_FREE_KB" ]; then  # emergency
      echo "[reaper] LOW DISK — emergency prune"
      docker builder prune -af >/dev/null 2>&1 || true
      docker image prune -f    >/dev/null 2>&1 || true
    fi
    [ $((tick % REAPER_TICKS)) -eq 0 ] && image_reaper_pass               # age-guarded full pass
  done
}
start_reaper(){ image_reaper_loop & REAPER_PID=$!; }
stop_reaper(){ [ -n "$REAPER_PID" ] && kill "$REAPER_PID" 2>/dev/null || true; REAPER_PID=""; }
```

`RULE:` Use **plain `docker builder prune -f`**, never `--keep-storage 5GB`. `WHY:` that flag is deprecated/version-gated (newer docker → `--max-used-space`); on rejection the command errors and a trailing `|| true` swallows it into a **silent no-op** — the exact recurring bug. Plain `-f` is universally supported.

`RULE:` Start the reaper before the image-producing phase; **stop it before the scoring phase** (scoring needs its own freshly-pulled images and short-lived containers). Between phases, a one-shot bulk cleanup (`cleanup_step1_images`) removes the leftover per-instance chain with no age guard.

`RULE:` **Do you even need the reaper?** `DECISION:`
- Agent runs in **native git workspaces** (no per-instance agent container/image) AND the harness deletes each pulled eval image after grading → you likely **don't** need it. This is why `swe-auto-eval` (300 instances) never leaked: agents run as subprocesses in plain checkouts, and its forked swe-bench harness `docker rmi`s each instance image in `run_instance`'s `finally` (`swe-auto-eval/.../run_evaluation.py:433-440`).
- Harness **builds or pulls an image per instance** (OpenHands agent-server, per-task task images kept around) → you **need** the reaper.

**The live-VM bleeder** (operational escape hatch, not part of run.sh): if a run is filling disk and you're SSH'd into the VM, run a `nohup` loop that every 30s does `docker builder prune -f` and, when free < ~40GiB, `docker builder prune -af` + reaps `eval-agent-server|sweb.eval|<your-package>` images, with a `while pgrep -f <your-infer-proc>` auto-stop. `RULE:` the bleeder MUST include `docker builder prune -f` each tick — a bleeder that only reaps images refills to 154GB while "correctly" reaping. (`memory: swe-verified-dashboard-integration`.)

### 9.5 CHECKLIST: DooD

`CHECKLIST:`
- [ ] Docker CLI-only, pinned 24.x (§7.6); no attempt to start an in-container daemon.
- [ ] If health-checking a published port from inside the runner → candidate-ladder `.pth` fix (§9.2), gated + dual-verified.
- [ ] If using harbor `DockerEnvironment` → `mounted=False` `.pth` fix (§9.3), gated by a real `dood_shared_check` probe.
- [ ] If building/pulling images per instance → pressure-triggered reaper (§9.4): builder prune every tick, panic `-af` below the free floor, repo-sweep with age guards, `date -u -d "${stamp%%.*}"`.
- [ ] Reaper stopped before scoring; between-phase bulk cleanup present.
- [ ] Chose `machine_type` with disk headroom for your worker count.

---

## Section 10 — Artifact Registry: when and how to store images

Agentic evals often need per-task/per-instance Docker images. Pulling those from the public internet (Docker Hub / GHCR) on every run is a reliability and cost risk (rate limits, egress, arch mismatches, disappearing tags). The pattern is: **mirror the images into Google Artifact Registry once (seed), then pull from AR on every run.** This section is the single most-emphasized "get it exactly right" part of onboarding, because a sloppy format makes the GCP console unusable for everyone.

### 10.1 DECISION: does my eval need Artifact Registry at all?

`DECISION:` Walk this tree:
- Does the benchmark run **per-task/per-instance Docker images** (a sandbox/test image per task)?
  - **No** (pure API scoring, non-agentic, or the agent runs in a native git checkout with no per-task container) → **you do NOT need AR.** Skip to §10.9. Examples: a non-agentic eval; `swe-auto-eval`'s agent phase runs in native workspaces (its *eval* phase pulls swe-bench images, but from the forked harness, and deletes them immediately).
  - **Yes** → continue.
- Are those images already reliably hosted somewhere you control with no rate limits?
  - Rarely. Assume **you should mirror them into AR** for reproducibility and to avoid run-time public pulls. → §10.2+.
- Special case: **digest-pinned images** (`ref@sha256:...`) that are content-addressed and stable (e.g. swe-atlas `rf` split pulls GHCR-direct by digest) can be pulled directly and skipped from seeding. Detect and skip them in the seeder.

Note: a **dataset** (tasks, prompts) is different from **images**. Datasets go to GCS as tarballs (§7.11), not to Artifact Registry.

### 10.2 The shared registry

`RULE:` All benchmarks share ONE Artifact Registry repository:
```
us-central1-docker.pkg.dev/xyne-dev-461113/eval-dashboard
```
- host/region: `us-central1-docker.pkg.dev`
- GCP project: `xyne-dev-461113`
- AR repo: `eval-dashboard`

This is overridable via `DOCKER_REGISTRY_URL` but you should not change it. Because it is shared, **naming discipline is mandatory** — your images sit next to every other benchmark's.

### 10.3 The CORRECT format — one package, per-instance tags (FOLLOW THIS)

`RULE:` Store your benchmark's images as **ONE Artifact Registry package** named for your benchmark, with **one tag per instance/task**. In the GCP console this shows as a single package with N tags — clean, greppable, obviously yours, and trivially differentiated from other benchmarks.

Reference (swe-verified, verified identical across seeder, inference pull, scoring adapter, and a unit test):
```
docker.io/swebench/sweb.eval.x86_64.django_1776_django-12345:latest
  ->  us-central1-docker.pkg.dev/xyne-dev-461113/eval-dashboard/
      sweverified-swebench-images : sweb.eval.x86_64.django_1776_django-12345
      \_________ ONE package ____/   \____ per-instance TAG (official basename) ___/
```
- Package name: `<benchmark>-<something>-images` (e.g. `sweverified-swebench-images`). swe-atlas uses `sweatlas-scaleapi-swe-atlas` (one package, per-task tags like `swe_atlas_QnA_<org>_<repo>_<ver>`). Both are the "package-plus-instance-tags" style.
- Tag = the source image's basename (lowercased; Docker tags can't contain `__`, so SWE-bench maps `__` → `_1776_`). Tags must match `^[a-z0-9_][a-z0-9_.-]{0,127}$`.
- Do **not** upload a second "agent-server" image — that derivative is built locally at inference time; only the canonical base/task images are persisted.

### 10.4 The CLUTTERED anti-pattern (DO NOT REPLICATE)

`RULE:` Do **not** create one AR *package* per instance/task. This produces hundreds of near-identical flat package names dumped into the shared `eval-dashboard` repo, impossible to differentiate at a glance in the console, and impossible to tell apart from other benchmarks' images.

Two real examples of what to avoid:
- **swe-auto-eval**: the vendored swe-bench fork builds the AR name by collapsing the namespace separator — `local_key.replace('/', '-', 1)` — yielding `eval-dashboard/swebench-sweb.eval.x86_64.<id>:latest`. So ~300 instances become ~300 **separate flat packages** all tagged `:latest`. (`swe-auto-eval/swe-bench/swebench/harness/docker_build.py:520-522`, `constants/__init__.py:26`.)
- **terminal-bench**: `_build_gar_ref` sanitizes each source image name into its own package — `alexgshaw/<task>:<datetag>` → `eval-dashboard/tbench-alexgshaw-<task>:<datetag>`. So 89 tasks → **89 distinct package names**. (`terminal-bench/scripts/seed_gar_images.py:199-206`, `config.yaml:149-152`.)

`WHY:` The user's explicit directive: follow swe-verified's storage format, **not** swe-auto-eval's or terminal-bench's, because the latter "create a lot of clutter and are hard to differentiate when being on the Google console." One package + many tags is scannable; many packages is noise.

`RULE:` If you are adapting a repo that already uses the cluttered scheme, change the AR-name construction to the one-package-many-tags form (override `DOCKER_REGISTRY_URL` handling and the name builder so the instance id becomes a **tag** under a single fixed package, not a package). Verify with a unit test that asserts the produced reference is `<registry>/<one-package>:<instance-tag>` (swe-verified ships exactly such a test: `tests/test_swebench_registry_layout.py`).

### 10.5 The seeder script (how images get INTO Artifact Registry)

`TEMPLATE:` The seeder mirrors source images into the one package (adapted from `swe-verified/scripts/seed_artifact_registry.sh`, the reference):
```bash
REGISTRY_URL="${DOCKER_REGISTRY_URL:-us-central1-docker.pkg.dev/xyne-dev-461113/eval-dashboard}"
IMAGE_PACKAGE="${MY_REGISTRY_IMAGE_PACKAGE:-<benchmark>-images}"   # ONE package

# auth: metadata token -> docker login (works on the seeding VM = eval-vm2)
authenticate() {
  local host="${REGISTRY_URL%%/*}" tok
  tok=$(curl -sf --max-time 5 -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("access_token",""))')
  echo "$tok" | docker login -u oauth2accesstoken --password-stdin "https://$host"
}
# source basename -> single-package tag
to_registry_image() {           # $1 = docker.io/ns/sweb.eval.x86_64.foo:latest
  local name="${1%:*}"; local tag="${name##*/}"; tag=$(printf '%s' "$tag" | tr 'A-Z' 'a-z')
  echo "${REGISTRY_URL%/}/${IMAGE_PACKAGE}:${tag}"
}
push_one() {                    # idempotent: skip if the tag already exists
  local src="$1" dst; dst=$(to_registry_image "$src")
  docker manifest inspect "$dst" >/dev/null 2>&1 && { echo "skip $dst"; return 0; }
  docker pull --platform linux/amd64 "$src"
  docker tag "$src" "$dst"
  docker push "$dst"
  docker rmi "$src" "$dst" >/dev/null 2>&1 || true   # don't fill the seeding VM's disk
}
```
Key properties to preserve:
- **Idempotent**: `docker manifest inspect` skips already-seeded tags, so interrupted/repeated runs resume safely.
- **Batched**: `--limit N` and `--parallel N` (seed 200 at a time, e.g. 3 passes for 500).
- **Platform-pinned**: `--platform linux/amd64` (and, for stronger guarantees, re-pull and assert `RootFS.Layers` diff_ids match, as swe-atlas/terminal-bench do).
- **Uses the docker CLI**, not docker-py: `WHY:` docker-py 7.x swallows credHelper errors as false 404s (`ImageNotFound`), masking auth failures. All the seed/pull scripts use CLI `subprocess` for this reason.

### 10.6 Pushing images: the eval-vm2 workflow

`RULE:` You push seed images from **`eval-vm2`**, a GCE VM in the **`xyne-dev`** GCP project that has push access to the `eval-dashboard` Artifact Registry.

`WHY:` The seeder needs a machine with (a) the docker CLR + daemon to pull/tag/push multi-GB images, (b) a service account authorized to push to AR, and (c) enough disk/CPU. `eval-vm2` is that machine. The Batch runner VMs typically have *read* (pull) access via their SA; *push* (seed) is done deliberately from `eval-vm2`.

Workflow:
1. **Get access to `eval-vm2`** (xyne-dev project). Ask whoever administers the xyne-dev GCP project to grant you SSH/IAP access to the `eval-vm2` instance. (If you need to run it interactively yourself, ask the human to `! gcloud compute ssh eval-vm2 --project xyne-dev ...` since interactive login runs in their session.)
2. On `eval-vm2`, clone your benchmark repo (which contains the seeder + a manifest of source images / a dataset it can enumerate).
3. Authenticate Docker to AR: the metadata-token `docker login` (§10.5 `authenticate`) — the VM's SA does the rest. (`gcloud auth configure-docker us-central1-docker.pkg.dev` works too if gcloud is present.)
4. Run the seeder in batches, e.g.:
   ```bash
   SEED_PULL_TIMEOUT=1800 SEED_TIMEOUT=1800 ./scripts/seed_artifact_registry.sh --limit 200 --parallel 8
   ```
   Repeat until all instances are seeded (`--limit 0` does them all in one pass; the idempotency check makes reruns cheap). Use `--dry-run` first to print the full source→destination mapping and eyeball the naming.
5. Verify in the GCP console: you should see **one package** for your benchmark with N tags, not N packages.

`CHECKLIST:` seeding:
- [ ] You have access to `eval-vm2` (xyne-dev project).
- [ ] Seeder produces the one-package-many-tags format (dry-run verified).
- [ ] Docker authed to AR via metadata token.
- [ ] All instances/tasks seeded; idempotent reruns show "already seeded".
- [ ] Console shows a single, clearly-named package.

### 10.7 The per-run pull path (how images come OUT of AR at run time)

On each dashboard run, `run.sh`/the harness pulls each needed image from AR and re-tags it to the name the harness/agent expects locally, so the harness's `pull_policy: missing` finds it and never contacts the public registry.

`TEMPLATE:` per-run pull (adapted from `swe-atlas/scripts/pull_atlas_images.py`, `terminal-bench/scripts/pull_tb_images.py`):
```
for each needed image:
    gar_ref = to_registry_image(source_ref)         # same mapping as the seeder
    docker pull  <gar_ref>
    docker tag   <gar_ref>  <source_ref>            # so the harness finds it locally by its expected name
```
`RULE:` Re-use the **exact same name-mapping function** in the seeder and the pull path so they can never drift. Refresh the AR token before pulling (it expires ~60min; use a single-flight `GarAuth` helper so parallel pulls don't stampede the metadata server). On Batch VMs where a committed manifest is absent, re-derive the GAR ref from the task definition with the identical sanitizer.

### 10.8 The scoring/inference registry adapter

`RULE:` The *harness* (swe-bench, OpenHands) may compute image names internally. Point it at your AR package by overriding its name-resolution — via an env-configured namespace/package and, if needed, a small monkeypatch of its `instance_image_key`/`namespace`. swe-verified does this three converging ways (inference base-pull, scoring `--namespace`, and a `sitecustomize`-loaded `TestSpec.instance_image_key` patch), all producing the identical `<registry>/<package>:<tag>`. Keep them all consistent with the seeder mapping, and unit-test the produced reference.

### 10.9 When you do NOT push images

`RULE:` Skip Artifact Registry entirely when:
- The eval is **non-agentic** / pure API scoring (no containers).
- The agent runs in a **native git workspace** with no per-task container image (like swe-auto-eval's agent phase).
- The eval's images are **digest-pinned and stable** and you accept pulling them directly (detect and skip in any seeder).
- You are still in bring-up and pulling from the public registry at run time is acceptable for smoke tests (but mirror before any real/large run).

In these cases your only "registry" concern is authenticating Docker for whatever ephemeral pulls the harness does (metadata token) — no seeding, no package.

---

## Section 11 — Logging & artifacts discipline

The dashboard is only as good as what your run writes. This section is about *what* to write, *where*, and *when* — so a run is live, debuggable, and crash-survivable, without flooding the sync loop.

### 11.1 Rich logs for live dashboard updates

`RULE:` `run.sh` must narrate its progress continuously and line-buffered (§7.3 stdbuf). The runner captures `run.sh`'s stdout/stderr to `runner_logs/runner.log`, which is uploaded **in full every 30s** — so anything you print shows up on the dashboard within ~30s.

Standard log vocabulary (all four repos):
- `log_info` / `log_ok` / `log_warn` / `log_err` / `log_step` — consistent, greppable prefixes.
- **Heartbeat** for long agentic/harbor runs: a backgrounded loop that logs one line per trial state transition + a periodic progress summary; kill it when the harness returns. `WHY:` harbor's TUI goes dark on a non-TTY; without a heartbeat, hours pass with no output and the run looks hung (§8.10).
- **Per-phase banners** (`===== Step 1: Inference =====`) so the log is skimmable.
- **A final scoreboard** restating the headline metric.

### 11.2 output/ vs logs/ vs never-synced — the placement rules

`RULE:` Three-way placement discipline (this is the workspace-bloat fix, §2.7):

| Put here | What | Why |
|---|---|---|
| `${EVAL_RUNNER_OUTPUT_DIR}` (`repo/output/`) | `<eval_run_id>_results.json`; small, dashboard-visible artifacts (a compact report, per-task scores JSON, metadata) | synced incrementally every 150s; keep it SMALL so the walk is fast |
| `repo/logs/` | verbose-but-bounded logs you want retained (scoring logs, agent session transcripts, generated patches) | synced incrementally every 150s |
| a non-synced dir (e.g. `repo/workspaces/`, `repo/cache/`, `~/.cache/...`) | git-clone checkouts, venvs, container scratch, build trees, 100k-file working data | NEVER synced; keep it out of `output/` and `logs/` or you throttle the whole sync loop |

`RULE:` `.gitignore` the non-synced dirs (`output/`, `workspaces/`, `cache/`) so they never get committed either. If your harness defaults to writing workspaces under `output/`, redirect it with a flag (like swe-auto-eval's `--workspaces_root ${SCRIPT_DIR}/workspaces`).

`RULE:` Do NOT copy bulky intermediate files (full agent conversation archives, the harness's own `output.jsonl` with embedded histories, multi-GB logs) into `output/`. Copy only the compact, useful subset (metadata, report, error summary). swe-verified deliberately copies `metadata.json output.swebench.jsonl output.report.json cost_report.jsonl ERROR_LOGS.txt` into `logs/artifacts/` and *not* `output.jsonl`.

### 11.3 Immediate + atomic save (survive a VM crash / cancel)

`RULE:` Save every unit of progress to disk **the moment it completes**, atomically, so a VM crash, OOM, or cancel loses at most the in-flight unit — not the whole run. `WHY:` VMs died mid-run repeatedly; the runs that survived did so because patches/predictions/transcripts were already on disk (and synced within 150s).

What to save immediately:
- **Generated patches / predictions**: write each instance's patch + prediction row the instant it's produced, under `flock` if parallel, with `f.flush()`. swe-auto-eval writes `results/predictions.jsonl` per instance under `LOCK_EX` and `results/patches/<inst>/<inst>_..._.diff` immediately (`custom_runner/runner.py:432-580`), then deletes the workspace clone to reclaim disk.
- **Agent session transcripts**: capture as they stream. terminal-bench symlinks `~/.xyne/agent/sessions → /logs/agent/sessions` so transcripts survive a timeout-cancel.
- **A per-instance status log**: an authoritative, agent-agnostic record written atomically (temp file + rename) after **every** event — carrying `final_status` (`completed`/`no_patch`/`error`), `termination_reason`, timings, errors. swe-auto-eval's `instance_logger.py` does exactly this (`<inst>_instance_log.json`), and that file — not the per-agent stream — is the authoritative verdict.

`RULE:` The patch/prediction extraction itself must be robust: `git add -A` (to include untracked/new files) + `git diff --cached` + `git reset`, run in the agent's checkout. Empty diff ⇒ `no_patch`. Sanity-check with `git apply --check` (non-fatal). (`swe-auto-eval/custom_runner/patch_extractor.py:52-154`.) `WHY:` "edited-but-no-patch" is usually the agent writing outside the repo tree or a git-revert, and `git add -A` is the only way to catch new files.

### 11.4 Per-agent capture formats (agentic evals) and their traps

Different agents write different on-disk artifacts. Know what each produces so you capture it (and so your analyzer can read it). From `swe-auto-eval/custom_runner/agents/*`:

| Agent | On-disk artifact (per instance) | Capture | Trap |
|---|---|---|---|
| `claude-v3` | `{id}_stream.jsonl` (Anthropic stream-json) | `... --output-format stream-json --verbose 2>&1 | tee` | rate-limits appear as `api_retry` 429 events; the run can still END on a trailing terminal 400 after retries exhaust — prefer the 429 signal when there are ≥3 retries |
| `opencode` | `{id}_stream.jsonl` (opencode events) | `... | tee` | needs `Grid/{MODEL}` model + provider config (§12) |
| `pi-mono` | `{id}_stream.jsonl` (JSONL session events) | `... | tee` | **version skew**: event type strings differ across pi versions (`tool_call_*` vs `tool_execution_*`) — match both families + fall back to `message.content` |
| `xyne` / `xyne-agent-cli` | `{id}_xyne.log` (raw tmux TUI, ANSI not JSON) | `xyne 2>&1 | tee` | it's a TUI capture, not structured; needs escape-stripping to read |
| `xyne-local` | structured `.log` envelope (`=== XYNE CLI SESSION/... ===`) | direct subprocess | `=== END ===` written only after the subprocess returns → its absence = timed-out mid-run |
| `cline` | **nothing** (only `{id}_prompt.txt`) | no `tee`; in-memory capture discarded | patch is recovered purely from the workspace — not a gap, but know it writes no stream |

`RULE:` For **free-text-graded** evals (a rubric judge reads a final answer), capture the answer from multiple sources in priority order and normalize it: (A) an agent-written answer file the prompt asks for, (B) the newest session-transcript JSONL's last assistant message, (C) escape-stripped stdout. Normalize to whatever the grader expects (swe-atlas wraps in double `<<FINAL_ANSWER>>` markers because `evaluate_answer.py` takes the text after the first marker). Write a `capture.log` health file per trial (source used, byte count, `EMPTY_ANSWER reason=...`) and a run-level rollup that **loudly warns** when any trial produced no answer. `WHY:` this is how a CLI contract change (xyne v0.3.0) becomes a visible warning instead of a silent all-zero (§2.9). (`swe-atlas/agents/xyne_cli_swe_atlas.py`, `scripts/recover_answers.py`.)

`RULE:` For **test-verified** evals (terminal-bench), there's no answer file to capture — the in-container tests write `verifier/reward.txt`. Capture is simpler (`xyne prompt --tools=… | tee`), but you still symlink the session dir into `/logs` so transcripts survive a cancel.

### 11.5 Secrets hygiene in logs/artifacts

`RULE:` (restating §2.6) Never let a key reach a synced file or a logged command:
- Pass secrets to subprocesses via the `env=` dict, not interpolated into the command string (harness logs the command, not the env).
- Write secret config files with `printf %s "$SECRET"` (no echo of the value into the log) and `chmod 600`.
- Never write a key into `output/`, `logs/`, or `runner_logs/` in plaintext.
`WHY:` `models.json` with a grid key was echoed into `trial.log` and leaked into artifacts; the fix passed the JSON via the exec `env=` and wrote it with `printf %s`. (`memory: swe-atlas-xyne-v030-capture-break`.)

### 11.6 Token-usage tracking — the portable contract

Token usage is not part of the eval-runner's required results contract, but it is a valuable standard artifact for any eval that drives an LLM or coding agent. The implementation cannot be identical across benchmarks: agents expose different session formats, harnesses disagree about what an "attempt" is, and retry policies change the meaning of cost-per-success. The portable contract is therefore about **what must be measured and communicated**, not one parser or one universal formula.

The two current references are:

- `swe-auto-eval/analytics/token_usage.py` + `run.sh`: a multipass SWE benchmark that stops retrying an instance after it resolves and reads agent-native session artifacts for Claude, Pi, OpenCode, Xyne, and Cline.
- `terminal-bench-v2-agentic/analysis/token_usage.py` + `run.sh`: a Harbor benchmark that runs every configured trial independently, including later trials for tasks already solved, and supports Xyne, Claude Code, OpenCode, Pi, Aider, Goose, and Codex.

`RULE:` Treat those as worked examples, not copy-paste templates. Reuse their reporting contract and safety properties; adapt collection and success semantics to your benchmark's actual artifacts and retry topology.

#### 11.6.1 The pipeline and its failure boundary

The intended flow is:

```text
agent/harness artifacts
  -> agent-aware usage parser
  -> normalized per-attempt records
  -> benchmark-aware aggregation
  -> token_usage.json / .csv / .md / .html
  -> optional compact headline in metrics.additional
```

`RULE:` Token accounting is **post-run reporting**, not grading. A parser, pricing, or HTML failure must not turn an otherwise valid benchmark run into a failed run. Run it after the authoritative attempt artifacts exist, before the final results aggregation if that aggregator wants to copy a token headline, and isolate it with an explicit warning path:

```bash
python3 analysis/token_usage.py \
  --run-dir "$RUN_DIR" \
  --out-dir "$OUTPUT_DIR" \
  "${TOKEN_PRICE_FLAGS[@]}" \
  || log_warn "token-usage reporting failed (non-fatal) — the eval result still counts"
```

`WHY:` Token reporting has more schema variability than grading. Coupling the two means an agent log-format change can erase a valid score.

#### 11.6.2 Collect authoritative usage; never manufacture zeroes

For each attempt/trial, normalize enough information to answer these questions:

| Field | Meaning |
|---|---|
| task/instance id | Stable benchmark identity, separate from a random trial-directory suffix |
| attempt/pass number | The independent trial or sequential pass that produced the record |
| outcome | Benchmark-native result such as resolved/solved, unresolved/unsolved, no-patch/no-grade, or error |
| measured | Whether a supported artifact contained usable token telemetry |
| input tokens | Total measured input charged at the input rate; include any input subcategories emitted separately |
| output tokens | Total measured generated output |
| upstream billed cost | Provider/harness-reported cost, if present; secondary telemetry only |
| source and measurement error | Which artifact/parser supplied the usage, or why the attempt is unmeasured |
| agent/version | The selected agent and the actual resolved version when available |

`RULE:` Prefer the agent's authoritative structured artifact over terminal text. Use Harbor's `TrialResult.agent_result` or another harness aggregate as a fallback only after verifying its semantics for that agent and version. Do not sum a cumulative total with the per-event records that produced it.

`RULE:` Missing telemetry is **unmeasured**, not zero usage. Keep the attempt in denominators, set its token/cost values absent or null, attach a diagnostic reason, and expose measurement coverage. A report that says `0 tokens` for a timed-out session is confidently wrong; a report that says `430/433 attempts measured` is honest and actionable.

`DECISION:` Choose the parser from the selected agent when `run.sh` already knows it; use content/schema detection only as a fallback for manual report regeneration. Capture the agent's native session into a per-attempt artifact directory during execution (§11.4), because no post-run analyzer can recover a transcript that was never persisted.

Cache-read/cache-write counters may remain in raw JSON/CSV/Markdown when an agent emits them and they are useful for diagnostics, but they are not user-configurable pricing inputs in the current contract. Fold them into the measured input total exactly once. The visual report should present input/output totals rather than exposing speculative cache economics.

#### 11.6.3 Canonical `input_params` names for optional pricing

When an eval accepts custom token prices from the dashboard, use these names everywhere at the external schema boundary:

```json
{
  "fields": [
    {
      "name": "input_token_price",
      "label": "Input Token Price (USD per 1M tokens)",
      "type": "number",
      "required": false,
      "constraints": { "min": 0 }
    },
    {
      "name": "output_token_price",
      "label": "Output Token Price (USD per 1M tokens)",
      "type": "number",
      "required": false,
      "constraints": { "min": 0 }
    }
  ]
}
```

The eval-runner maps these to `--input-token-price` and `--output-token-price` (§4.3). `run.sh` may then translate them to an analyzer's internal flags, such as `--price-input` / `--price-output`; internal CLI spelling does not need to leak into the dashboard schema.

`RULE:` The external names are exactly `input_token_price` and `output_token_price`. Do not invent per-eval permutations such as `price_input`, `token_price_input`, or `input_price`, because dashboard presets and cloned run configurations need a stable cross-eval contract.

`RULE:` Do not add cache-price or assumed-cache-hit-rate fields. The current cost model is deliberately simple:

```text
custom_cost_usd = (
    total_input_tokens * input_token_price
  + output_tokens      * output_token_price
) / 1,000,000
```

Require a complete input/output pair before enabling custom-priced views. With neither price, emit a token-only report. With a partial pair, remain token-only and state which value is missing rather than half-pricing the run. If a schema uses numeric `0` as an empty/default value, explicitly test how `run.sh` and the analyzer interpret it; do not accidentally label an unpriced run as free or a deliberate zero-cost run as missing.

`RULE:` The in-repo `input_params.json` is a reference copy; `evals.input_params` in the dashboard database remains authoritative (§6.1). Update both together. During a field migration, `run.sh` may consume and ignore retired flags so queued or cloned runs cannot forward an unknown option into the harness; remove that compatibility sink only after the stored schemas have converged.

#### 11.6.4 Always produce the report, with or without prices

For a run with at least one recognized attempt, the normal artifact set is:

| Artifact | Purpose |
|---|---|
| `token_usage.json` | Exact per-attempt records, metadata, coverage, normalized aggregates, nullable monetary values |
| `token_usage.csv` | Flat exact data for spreadsheets and external analysis |
| `token_usage.md` | Exact human-readable tables and methodology |
| `token_usage.html` | Compact visual summary for artifact browsing |

`RULE:` HTML generation must not depend on custom pricing. In token-only mode, use measured tokens as the primary unit and omit custom-cost comparisons. In priced mode, show **both cost and the corresponding token value** in headline cards, success-efficiency blocks, and attempt/pass economics. Provider-reported/as-billed cost may still appear, but only as explicitly labelled secondary telemetry; it must never silently drive custom-price charts.

For readability, HTML may format `12,350` as `12.35K` and `1,450,000` as `1.45M`. Keep exact values in JSON, CSV, and Markdown. Every visual report should make coverage visible and label incomplete totals as lower bounds.

`DECISION:` Add a compact token headline to `metrics.additional` only when the dashboard benefits from it. Preserve the eval's official `metrics.main` score and existing secondary metrics. Token reporting explains resource use; it does not redefine benchmark correctness.

#### 11.6.5 Aggregate according to the benchmark's retry topology

Some metrics are portable:

- total measured input, output, and combined tokens;
- measured attempts / observed attempts and a coverage percentage;
- token/cost totals by benchmark-native outcome;
- average tokens/cost per measured attempt;
- successful-trial totals when the benchmark has an unambiguous success signal;
- agent/version/source/measurement-quality diagnostics.

Success-efficiency metrics are **not** portable without defining their population and denominator. The current references intentionally differ:

| View | SWE multipass (`swe-auto-eval`) | Independent repeated trials (`terminal-bench`) |
|---|---|---|
| Benchmark-wide | All measured usage / instances resolved at least once | All measured trial usage / tasks solved at least once |
| Resolved chain / including failures | Successful attempt plus earlier failed passes for each instance that later resolved | Every trial for tasks solved at least once, including failed and post-solve trials |
| Winning attempt | First successful attempt for a resolved instance; SWE stops scheduling it afterward | First successful trial for each solved task |
| Successful-attempt average | Measured attempts that actually resolved | Every independently successful trial, even when the same task succeeds more than once |
| Attempt/pass view | Later passes contain only still-unresolved instances | Attempt rounds contain the full selected task population when Harbor reruns every task |

`RULE:` Document the population in the metric label or methodology. Never call `all usage / solved tasks` an "average successful trial"—failed trials are included in the numerator. Conversely, never add failed trials to the successful-trial average: independent retries do not transfer context or token spend to the trial that succeeded.

Use null/absent values (rendered as `—`) when a denominator is zero or the relevant telemetry is unmeasured. Do not convert undefined economics into `$0.00` or `0 tokens`.

#### 11.6.6 Implementation sequence and acceptance checks

`TEMPLATE:` A practical implementation order:

1. Inventory every supported agent's actual session/result artifact using a real smoke run.
2. Implement one parser per distinct artifact schema and fixture-test it, including truncated files and missing usage.
3. Normalize attempts without applying benchmark policy inside the parsers.
4. Add benchmark-aware outcome grouping, retry/success views, and explicit denominator tests.
5. Emit exact JSON/CSV/Markdown first; then build HTML from the same aggregate object so formats cannot disagree.
6. Wire the analyzer into `run.sh` as non-fatal reporting and place outputs under the synced output directory (§11.2).
7. Test priced, unpriced, partial-price, zero-denominator, incomplete-coverage, multiple-attempt, and multiple-agent fixtures.
8. Regenerate a report from a real artifact set and compare headline totals back to the source records before the smoke run is accepted.

`CHECKLIST:`
- [ ] Every supported agent has a verified structured source or is explicitly reported as unmeasured; no parser silently substitutes zero.
- [ ] Per-attempt identity, outcome, pass/trial index, source, agent version, and measurement error survive normalization.
- [ ] Coverage is visible in every summary and incomplete totals are labelled lower bounds.
- [ ] Dashboard price fields, when present, are exactly `input_token_price` and `output_token_price`; no cache-pricing controls.
- [ ] No-price and partial-price runs still emit JSON, CSV, Markdown, and HTML with token metrics.
- [ ] Priced HTML pairs each cost-first success/economics metric with its token value; exact values remain outside HTML.
- [ ] Upstream billed cost is labelled secondary and never used as the custom-pricing fallback.
- [ ] Retry-inclusive, winning-attempt, and successful-trial metrics match the benchmark's actual scheduling policy and tested formulas.
- [ ] Reporting failure is non-fatal and cannot replace or suppress the official result JSON.
- [ ] A real smoke artifact set reproduces expected totals, not only synthetic fixtures.

### 11.7 CHECKLIST: logging & artifacts

`CHECKLIST:`
- [ ] Line-buffered, narrated progress (helpers + heartbeat + banners + scoreboard).
- [ ] `output/` holds only the results JSON + small artifacts; verbose logs in `logs/`; workspaces/scratch in a non-synced, gitignored dir.
- [ ] Patches/predictions/transcripts saved immediately + atomically; an authoritative per-instance status log.
- [ ] Robust patch extraction (`git add -A` + `diff --cached` + `reset`).
- [ ] Per-agent capture matched to each agent's real on-disk format; free-text evals have multi-source capture + a loud health rollup.
- [ ] Token reporting (when applicable) follows §11.6: agent-aware source, explicit coverage, benchmark-aware retry semantics, always-on HTML, and optional input/output pricing.
- [ ] No secret ever reaches a logged command or a synced file.

---

## Section 12 — Models, providers, and the Grid gateway

Almost every eval here drives models through Juspay's **Grid** gateway. Getting the endpoint, model id, and credential fan-out right is the difference between a run that scores and a run that fails auth in 150ms.

### 12.1 The Grid endpoint

- Base gateway URL: `https://grid.ai.juspay.net`.
- For **OpenAI-compatible** clients, the base URL usually includes `/v1`: `https://grid.ai.juspay.net/v1` (this is the `base_url` default in the input schemas).
- For **Anthropic-style** clients (claude-code), the client appends `/v1` itself, so you must pass `ANTHROPIC_BASE_URL="${BASE_URL%/v1}"` (strip the `/v1`). Grid does serve the Anthropic Messages API.
- For the **LLM-judge / analyzer** direct calls, use the bare base `https://grid.ai.juspay.net` — the analyzer appends `/chat/completions` itself (no `/v1`). (`memory: grid-judge-config`.)

### 12.2 Model ids: bare (FREE) vs `Grid/`-prefixed (PAID)

`RULE:` Use **bare** model ids — `glm-latest`, `kimi-latest`, `private-large`, etc. These route to the **free internal** tier.

`RULE:` Do **NOT** prefix a model with `Grid/` unless you specifically intend the external **paid** tier. `Grid/glm-latest` routes to the paid tier and returns **HTTP 400 `budget_exceeded`** ("Current spend $0.00, Limit $0.00") on an unfunded account — a hard failure that looks like a model error. The one legitimate `Grid/{MODEL}` use is **opencode**, whose provider format requires a `provider/model` split (§12.4). (`memory: grid-judge-config`.)

`WHY:` We shipped fixes because analyzer/judge calls defaulted to a `Grid/`-prefixed id and got `budget_exceeded`; and because nemotron runs billed as EXTERNAL PAID with a $5 cap died on `budget_exceeded` mid-run.

### 12.3 Credential fan-out: built-in agents read the process env

`RULE:` Export the one API key under **every** name the agents and harness read (§8.5). `WHY:` built-in harbor agents do **not** take the key as a CLI flag — they read it from the harbor **process env** (verified in harbor source):
- claude-code: `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL` (`claude_code.py:1024-1027`).
- codex: `OPENAI_API_KEY`, `OPENAI_BASE_URL` (`codex.py:756,764`).
- gemini-cli: `GEMINI_API_KEY`, `GOOGLE_*` (`gemini_cli.py:667-672`).
- opencode: parses `provider, model = model_name.split("/", 1)` → **requires a `/` in the model** (`opencode.py:375`).
- Your custom agent (xyne-cli): whatever env your adapter reads (`XYNE_API_KEY`/`LITE_LLM_*`), passed via `--agent-env`/`--ae`.

Miss one of these and that agent fails authentication in ~150ms with `apiKeySource:none` / "Not logged in", produces no edits, and every task no-patches. (`memory: swe-atlas-xyne-v030-capture-break`, the claude-code all-zero.)

### 12.4 opencode's `Grid/{MODEL}` + provider config

`RULE:` For opencode, set the model to `Grid/{MODEL}` (the split gives provider `Grid`, model `{MODEL}`) **and** inject a provider config via `--ak opencode_config=<json>` that defines the `Grid` provider as an `@ai-sdk/openai-compatible` endpoint pointing at the Grid base URL with the key. This is the one place the `Grid/` prefix is correct (it's an opencode provider name, not the paid-tier router). (`swe-atlas`/`terminal-bench` run.sh + config.yaml.)

### 12.5 The LLM-as-judge / analyzer config

If your eval (or its post-run analyzer) uses an LLM judge:
`RULE:` endpoint = `https://grid.ai.juspay.net` (no `/v1`); model = **bare** `glm-latest` (never `Grid/glm-latest`); temp 0 + seed for determinism; bounded concurrency (e.g. 4) with retry that treats 429/5xx/network as transient (backoff + honor `Retry-After`) but fails fast on other 4xx (bad key/model/budget). Surface the exact `HTTP <code> <reason> :: <body>` on failure so a misconfig is diagnosable, not silent. Prefer the strong-but-context-starved judge with **compact, pre-digested evidence** (a few booleans + one short snippet), never raw streams. (`memory: grid-judge-config`, `nopatch-analysis-glm-wrong-stream`.)

### 12.6 CHECKLIST: models/grid

`CHECKLIST:`
- [ ] `base_url` default `https://grid.ai.juspay.net/v1`; `ANTHROPIC_BASE_URL="${BASE_URL%/v1}"`.
- [ ] Model ids are **bare** (free tier); no accidental `Grid/` prefix except opencode.
- [ ] Key fanned out to `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `LITE_LLM_*`, `GRID_AI_API_KEY`, `XYNE_*`.
- [ ] opencode (if used) gets `Grid/{MODEL}` + provider config.
- [ ] Judge (if any) uses bare `glm-latest` at the no-`/v1` base, temp 0, transient-vs-fatal retry split, loud HTTP errors.

---

## Section 13 — The failure catalog (symptom → root cause → fix → prevention)

This is the graveyard. Every entry is a real incident from one of the four integrations. When a run misbehaves, **search this section by symptom first** — the odds are it's here. Each entry: what you see, why, the fix (in your scripts), and how to never hit it again.

### 13.1 Setup / environment failures

**F1 — `sudo: command not found` (exit 127) in setup.sh.**
- Root cause: the runner runs as root with no `sudo` binary.
- Fix: the `SUDO=""` wrapper (§3.2/§7.4); use `sudo` only when non-root AND it exists.
- Prevention: never call `sudo` unconditionally in any script.

**F2 — Docker install hangs / fails trying to start a daemon.**
- Root cause: installing the full `docker-ce` engine under DooD; the daemon can't start (no `CAP_SYS_ADMIN`).
- Fix: install `docker-ce-cli` (pinned `5:24.*`) + `docker-buildx-plugin` only when a socket is present (§7.6).
- Prevention: branch on `[ -S /var/run/docker.sock ]`; engine only on bare VMs.

**F3 — docker CLI/daemon protocol mismatch errors.**
- Root cause: an unpinned CLI newer than the COS host's Docker 24.0.9.
- Fix: pin the CLI to the 24.x line.
- Prevention: always pin `docker-ce-cli=5:24.*`.

**F4 — gcloud/AR auth state write fails; `/var/lib/docker` not writable.**
- Root cause: hardcoding `/var/lib/docker` as a writable base; it isn't, inside the runner.
- Fix: write-probe `/var/lib/docker`, fall back to `$HOME/.gcloud-eval` (§7.5).
- Prevention: never assume a path is writable — probe.

**F5 — gcloud won't apt-install on COS/Debian runner.**
- Root cause: read-only/constrained root fs; apt path for gcloud is unreliable.
- Fix: install gcloud from the **tarball** into the writable base; but prefer the metadata-token `docker login` as the primary AR-auth path (§7.7).
- Prevention: don't depend on gcloud; metadata token is install-free.

**F6 — the harness patch (`.pth`) silently doesn't apply.**
- Root cause: a venv-local `sitecustomize.py` is shadowed by Debian's `/usr/lib/pythonX.Y/sitecustomize.py`.
- Fix: install the patch as a `.pth` file (`import my_patch`) instead (§7.10).
- Prevention: never use `sitecustomize.py` for venv patches on Debian; dual-gate self-verify in setup.sh; hard-fail if broken in a DooD env.

**F7 — agent binary missing after install.**
- Root cause: `@xyne/xyne-cli >= 0.1.2` no longer ships linux binaries in the main tarball.
- Fix: fetch from npm platform siblings `@xyne/xyne-cli-linux-{x64,arm64}`, extract `package/xyne-linux-<arch>` (§7.8).
- Prevention: know where each tool ships its linux binary; verify it exists after install.

### 13.2 Contract / results failures

**F8 — run shows FAILED on the dashboard despite run.sh "succeeding".**
- Root cause: `run.sh` exited 0 but wrote no `<eval_run_id>_results.json` (`main.rs:281-283`).
- Fix: the always-armed EXIT-trap zero-metric fallback; disarm only after the real atomic write (§8.6).
- Prevention: every code path (success, error, cancel, crash) leaves a results file.

**F9 — results parse error / metrics not displayed.**
- Root cause: wrong JSON shape (flat metrics, `main.value` a string, missing `metrics`/`main`).
- Fix: exact `{"metrics":{"main":{"name":str,"value":number},"secondary":{},"additional":{}}}` shape; `secondary` flat, `additional` nested (§4.5).
- Prevention: unit-test the results writer; validate `main.value` is numeric.

**F10 — half-written / corrupt results file after a crash.**
- Root cause: writing the results file in place.
- Fix: write `.tmp` then `os.replace()`/`mv` (atomic).
- Prevention: always atomic-write outputs.

**F11 — output dir / image tag "not found", empty predictions.**
- Root cause: the results/output path was hardcoded (a stale SHA, a guessed dir) and didn't match what the harness actually builds.
- Fix: derive the path from the harness's own function as a single source of truth (`swe-verified` queries `construct_eval_output_dir` via `uv run python -c`); remove hardcoded tag prefixes (§8, F-note: an `IMAGE_TAG_PREFIX` override that once "fixed" this later became **harmful** after an upstream fix — never carry stale overrides).
- Prevention: never hardcode a path the harness computes; ask the harness.

**F12 — task_range `0-49` runs 49 tasks, not 50.**
- Root cause: passing `--n-limit 49` for an inclusive range; `--n-limit` is first-N.
- Fix: pass `END+1`; warn/degrade for ranges not starting at 0 (§8.8).
- Prevention: verify and log your harness's range semantics.

**F13 — metrics under-report; errored instances vanish.**
- Root cause: the harness's aggregate drops rows whose final attempt errored, so the denominator is wrong (reported 1/1 instead of 1/4).
- Fix: reconcile against per-attempt stream files to recover every attempted instance; report honest `total`/`infer_errors` (§8.9).
- Prevention: know whether your harness silently drops errored rows; reconcile if so.

**F14 — pass@2/@3 "no_patch" lists include already-resolved instances.**
- Root cause: SWE-bench grades the full dataset every pass and marks every non-submitted instance `empty_patch`; from pass≥2 that includes earlier-resolved and out-of-scope ids.
- Fix: scope-filter the reporting: `incomplete = (raw ∩ scope) − cumulative_resolved` before writing nopatch lists (`swe-auto-eval/run.sh:710-718`).
- Prevention: for multipass, always de-contaminate per-pass failure buckets by subtracting cumulative resolved.

### 13.3 DooD / Docker failures

**F15 — container is up but health check times out; instance unhealthy.**
- Root cause: health poll hits `127.0.0.1:<host_port>` inside the runner, but `-p` publishes on the COS host netns.
- Fix: the candidate-ladder `_wait_for_health` `.pth` patch (localhost → gateway → container-IP → `docker network connect`), rewrite `workspace.host` on success (§9.2).
- Prevention: any in-runner health check of a published port needs this; gate + dual-verify.

**F16 — every harbor trial no-grades; verifier/answer files empty.**
- Root cause: harbor reads verifier output from the host bind source, which DooD resolves on the host (invisible to the runner).
- Fix: `.pth` patch flipping `DockerEnvironment.capabilities.mounted=False` → `docker compose cp` retrieval; gate from a real `dood_shared_check` probe (§9.3).
- Prevention: use harbor's cloud-backend path under DooD; verify with the probe, don't assume topology.

**F17 — ENOSPC; disk fills mid-run; run dies with no results.**
- Root cause: per-instance image pulls + agent-server builds, nothing deleted; disk-full also kills the EXIT-trap fallback.
- Fix: the pressure-triggered reaper (§9.4); size `machine_type` for the worker count.
- Prevention: reap continuously if you build/pull per instance.

**F18 — reaper "removes tags" but frees ~0 GB.**
- Root cause: per-instance-key grep can't match the SDK's truncated `_tag_latest` tag variant, which pins the whole image chain.
- Fix: repository-sweep reaping (all `eval-agent-server` repo tags), never per-instance-key (§9.4).
- Prevention: reap by repo, with age + container guards.

**F19 — disk still fills after images are tamed; `docker system df` shows huge Build Cache.**
- Root cause: `FORCE_BUILD=1` deposits BuildKit cache that `docker rmi`/`docker image prune` do NOT touch (grew to 154GB).
- Fix: `docker builder prune -f` every watchdog tick; emergency `-af` on low disk (§9.4).
- Prevention: prune build cache continuously; the bleeder loop must include it too.

**F20 — VM OOM/ENOSPC-crashes despite a reaper.**
- Root cause: a time-triggered (180s poll) reaper loses the race — disk fills 100% inside one gap at ~50GB/hr.
- Fix: pressure-triggered 30s watchdog with a free-space panic floor (§9.4).
- Prevention: make reaping pressure-triggered, not (only) time-triggered.

**F21 — images mis-aged; reaper deletes too eagerly or never.**
- Root cause: parsing docker timestamps with plain `date -d` on a non-UTC host; using `.Created` (the months-old upstream build date) for local freshness.
- Fix: `date -u -d "${stamp%%.*}"`; use `.Metadata.LastTagTime` for base-image freshness (§9.4).
- Prevention: always parse UTC; use LastTagTime for local pull/tag age.

**F22 — a `|| true` on a prune flag silently no-ops.**
- Root cause: `docker builder prune --keep-storage 5GB` is deprecated/version-gated; on rejection it errors and `|| true` swallows it.
- Fix: plain `docker builder prune -f` (universally supported) (§9.4).
- Prevention: avoid version-gated flags behind `|| true`; that pattern hides the failure.

### 13.4 Artifact Registry failures

**F23 — hundreds of near-identical images clutter the GCP console.**
- Root cause: one AR package per instance/task (`swebench-sweb.eval.*` ×300; `tbench-alexgshaw-*` ×89).
- Fix: one package + per-instance tags (the swe-verified format, §10.3/§10.4).
- Prevention: never make the instance id a package; make it a tag.

**F24 — `docker manifest inspect` / pull returns false 404 despite valid creds.**
- Root cause: docker-py 7.x swallows credHelper errors as `ImageNotFound`.
- Fix: use the docker CLI (`subprocess`), not docker-py, for pull/tag/push/inspect.
- Prevention: seed/pull scripts use the CLI.

**F25 — AR pull fails mid-run on a long sweep.**
- Root cause: the metadata OAuth token expired (~60min).
- Fix: refresh the token before each image phase; single-flight `GarAuth` for parallel pulls (§8.4/§10.7).
- Prevention: never assume a token lives the whole run.

### 13.5 Model / capture / scoring failures

**F26 — claude-code (or any built-in agent) fails auth in ~150ms; all tasks no-patch.**
- Root cause: creds wired only for the custom agent; built-ins read provider creds from the harbor process env, which run.sh never exported.
- Fix: export `ANTHROPIC_AUTH_TOKEN`+`ANTHROPIC_BASE_URL`, `OPENAI_*`, `GEMINI_*` (§12.3).
- Prevention: fan the key out to every provider env name.

**F27 — HTTP 400 `budget_exceeded` (Current spend $0.00, Limit $0.00).**
- Root cause: a `Grid/`-prefixed model id routed to the paid tier.
- Fix: use bare model ids (`glm-latest`); only opencode uses `Grid/{MODEL}` deliberately (§12.2/§12.4).
- Prevention: never prefix `Grid/` except opencode's provider config.

**F28 — every task scores 0; answer file empty after a CLI version bump.**
- Root cause: npm `latest` jumped to xyne-cli v0.3.0 (a TUI app); the old `xyne prompt | tee` capture broke; without a TTY, tool calls are blocked ("Cannot prompt for confirmation (no TTY)").
- Fix: `--yolo` + multi-source capture (agent answer file → session JSONL → escape-stripped stdout) + `<<FINAL_ANSWER>>` normalization + a loud capture-health rollup (§11.4).
- Prevention: version-adaptive, self-diagnosing capture; never let a silent all-zero pass.

**F29 — an API key leaks into `trial.log` / synced artifacts.**
- Root cause: a secret interpolated into a logged command (`models.json` echoed with the key).
- Fix: pass secrets via the exec `env=` dict; write config with `printf %s` + `chmod 600` (§11.5).
- Prevention: no secret ever in a command string or a synced file.

**F30 — a "resolved" instance shows as no_patch in an analysis; wrong log stream read.**
- Root cause: selecting streams by a buggy `nopatch_ids` list (which counts earlier-resolved instances) and reading the wrong pass's stream.
- Fix: select by the authoritative signal — the per-instance `instance_log.json` `final_status`, or `(session exists) ∧ (not in that pass's predictions)`.
- Prevention: use the harness's authoritative per-instance verdict, not derived summary lists; separate infra failures from capability failures.

**F34 — a typed-decision benchmark is wired at Grid `/v1` and every request 404s.**
- Root cause: the kit's `http` engine posts `{origin}/v1/systemone`. Grid's OpenAI base is already `…/v1`, so that path does not exist, and a chat model does not speak systemone.
- Fix: `engine=grid` posts `{base_url}/chat/completions` and parses a JSON map of question key → option key or noul probability (`decision_index/engines/grid.py`). Keep `engine=http` only when `base_url` is a systemone origin.
- Prevention: the dashboard schema's engine field lists `grid` (default), `http`, and `random`, and the base_url description says which shape each engine expects.

**F31 — cline produces a patch but "writes no logs".**
- Root cause: `cline` writes no stream file (no `tee`); its in-memory capture is discarded by design.
- Fix: recover the patch from the workspace (patch extractor); don't expect a stream.
- Prevention: know each agent's on-disk contract (§11.4 table); `cline` = workspace-only.

### 13.6 Log-sync / dashboard failures

**F32 — dashboard logs stale/frozen despite a live run; "Cannot stat" WARN flood.**
- Root cause: 100k-file git-clone workspaces under synced `output/`; a 30s tick takes tens of minutes, starving `runner_logs`.
- Fix: move workspaces out of `output/` into a non-synced dir (`--workspaces_root`) (§2.7/§11.2).
- Prevention: keep large/transient trees out of `output/` and `logs/`.

**F33 — "Cancel" button doesn't kill the VM.**
- Root cause: the backend pre-stamped `runner_pods.completed_at`, but the scheduler sweep filters `completed_at IS NULL` — so the pod was never swept.
- Fix (platform-side, already shipped): don't pre-stamp `completed_at` on cancel (§4.7). As an eval author: make `run.sh` cancel-safe (save progress immediately) so a delayed kill still preserved work.
- Prevention: understand the cancel path; don't rely on instant teardown.

### 13.7 How to triage a new failure

`RULE:` When you hit something not in this catalog:
1. Read the actual logs first (`runner_logs/runner.log`, the harness's own logs) — never guess (§2.1).
2. Reproduce the smallest failing unit (one instance, a smoke range).
3. Add a diagnostic print/log at the suspected point, run, and read it — establish ground truth (env var, HTTP code, path, topology).
4. Fix in **your** scripts only (§2.2). Re-smoke-test.
5. Write the new war story into this section (and a memory file) so the next person doesn't rediscover it.

---

## Section 14 — End-to-end runbooks

Three complete, ordered walkthroughs. Pick the one matching your benchmark (§0.3 STEP 2) and execute top to bottom. Each references the deeper sections for detail.

### 14.1 Runbook A — Non-agentic / API-scored eval (the simple case)

Use when: a model is prompted and scored, no agent loop, no per-task Docker, no harness. This is the strict subset of the contract.

```
A1. Repo skeleton:
      repo/
        setup.sh
        run.sh
        eval/            # your prompting + scoring code
        input_param.json # (optional, mirror of the dashboard schema)
        .gitignore       # ignore output/, cache/
A2. setup.sh (§7): stdbuf re-exec; set -u no set -e; SUDO="" wrapper;
      apt install python/curl/jq; install uv or pip deps. NO docker, NO gcloud,
      NO harness needed. Hard-fail only if the runtime won't install.
A3. run.sh (§8): parse [key] id --flags; export OPENAI_API_KEY/BASE_URL +
      ANTHROPIC_* (§12) from the key; OUTPUT_ROOT="${EVAL_RUNNER_OUTPUT_DIR:-./output}";
      arm the EXIT-trap fallback; run your prompting/scoring streaming progress logs;
      write ${OUTPUT_ROOT}/${id}_results.json atomically; disarm the trap.
A4. Models (§12): bare model ids; base_url https://grid.ai.juspay.net/v1;
      ANTHROPIC_BASE_URL="${BASE_URL%/v1}".
A5. input_params (§6): model (required), base_url, task_range, any sampling knobs.
A6. Register (§5): create the eval (repo_url, branch/sha, machine_type n2-standard-2/4,
      input_params). Public repo or Juspay-org+token.
A7. Verify (§15): bash -n, shellcheck, local stub run (both fallback + success paths).
A8. Smoke run task_range 0-9 on the dashboard; read logs; then full run.
```

You can skip: §9 (DooD), §10 (Artifact Registry), the reaper, per-agent capture.

### 14.2 Runbook B — Agentic eval on harbor (like swe-atlas / terminal-bench)

Use when: a coding/CLI agent runs per task inside harbor-managed Docker containers, graded by in-container tests or a rubric judge.

```
B1. Repo skeleton:
      repo/
        setup.sh
        run.sh
        harbor/              # vendored harbor  (OR rely on PyPI pin)
        adapter/ or agents/  # your custom agent import-path package
        scripts/             # seed_gar_images.py, pull_*_images.py, dataset fetch,
                             #   _harbor_dood.py (.pth patch), recover_answers.py
        config.yaml          # models/agents/dataset/gar constants
        input_param.json
        .gitignore           # output/, logs/big, ~/.cache
B2. setup.sh (§7):
      - stdbuf; set -u; SUDO="" ; apt: git curl jq tar zstd (+ tmux if the agent needs a TTY)
      - docker-ce-cli 24.x + buildx (§7.6)
      - AR auth: metadata token docker login; persist gcloud-env.sh (§7.7)
      - uv; Python floor (3.11/3.12 per harbor version)
      - harbor: vendored `uv tool install ./harbor` (0.6.6) OR `uv tool install harbor==0.13.1
        --with-editable ./adapter` (§7.9). PIN it.
      - xyne-cli binary from npm siblings (§7.8)
      - install the harbor DooD `.pth` patch (mounted=False), env-gated, dual-verified (§7.10/§9.3)
      - restore dataset tarball from GCS (§7.11)
B3. Seed images to AR (§10) — ONE-TIME, on eval-vm2:
      - build a seeder (one package + per-task tags; §10.3/§10.5) — do NOT use the cluttered
        per-image format
      - on eval-vm2: metadata-token docker login; ./scripts/seed_gar_images.py --limit 0 (batched)
      - verify one clean package in the console
B4. run.sh (§8):
      - parse [key] id --flags; source gcloud-env; refresh AR token
      - key fan-out to ANTHROPIC_AUTH_TOKEN/OPENAI_*/GEMINI_*/XYNE_* (§12.3); opencode -> Grid/{MODEL}+--ak
      - dood_shared_check probe -> export TB_HARBOR_UNMOUNTED/SA_HARBOR_UNMOUNTED (§9.3)
      - pull needed images from AR and retag to source names (§10.7)
      - background a heartbeat (harbor TUI is dark on non-TTY) (§8.10)
      - invoke `harbor run` with the version-correct flags; capture agent output per §11.4
      - for free-text grading: recover_answers + <<FINAL_ANSWER>> normalization + capture-health rollup
      - aggregate -> results JSON (main/secondary/additional), atomic; trap - EXIT
B5. input_params (§6): model, agent (options), split/dataset, attempts, concurrency,
      range, base_url, timeouts, log_level.
B6. Register (§5): machine_type n2-standard-8/16; pin branch/sha; input_params.
B7. Verify (§15): bash -n; shellcheck; scratch-venv .pth dual-gate check; harbor boots
      and logs the patch line; local smoke of 1 task if possible.
B8. Smoke run range 0-9 on the dashboard. Read the trial-artifacts rollup: verifier/:yes
      means grading works; MISSING means the DooD mount fix didn't engage -> fix and re-smoke.
      Then full run.
```

Reaper: usually NOT needed for harbor task images that are pulled-and-reused (they don't multiply per instance the way OpenHands agent-server builds do) — but if your agent builds an image per task, add the reaper (§9.4).

### 14.3 Runbook C — SWE-bench-style eval with per-instance images (like swe-verified)

Use when: each instance has its own multi-GB base image, an agent-server is built per instance, and scoring runs tests in per-instance containers. This is the heaviest case — all of §9 and §10 apply.

```
C1. Repo skeleton (OpenHands-style monorepo or a forked swe-bench):
      repo/
        setup.sh
        run.sh
        scripts/
          seed_artifact_registry.sh   # ONE package + per-instance tags (the reference)
          swev_dood_hostfix.py        # candidate-ladder health .pth patch
        sitecustomize.py              # loads the AR registry-layout scoring adapter
        benchmarks/ or swe-bench/     # the harness (vendored/forked)
        input_config_schema.json
        tests/test_registry_layout.py # asserts one-package-per-instance-tag naming
        .gitignore                    # eval_outputs/, output/, workspaces/
C2. setup.sh (§7):
      - stdbuf; set -u; SUDO="" ; docker-ce-cli 24.x + buildx (buildx REQUIRED: agent-server builds)
      - AR-auth base-dir write-probe (/var/lib/docker -> $HOME/.gcloud-eval); metadata token login;
        persist gcloud-env.sh
      - uv; `make build` (submodules + uv sync) HARD-fail on error
      - install scripts/swev_dood_hostfix.py as a .pth, env-gated SWEV_DOOD_HOST_FIX, dual-verify;
        HARD-fail if broken AND DooD detected (§7.10/§9.2)
C3. Seed images to AR (§10), ONE-TIME on eval-vm2:
      - ./scripts/seed_artifact_registry.sh --dry-run --limit 0   # eyeball naming
      - SEED_PULL_TIMEOUT=1800 SEED_TIMEOUT=1800 ./scripts/seed_artifact_registry.sh --limit 200 --parallel 8
        (repeat for the full set; idempotent)
      - package = <benchmark>-swebench-images ; tags = sweb.eval.x86_64.<repo>_1776_<name>
C4. run.sh (§8, three phases):
      - source gcloud-env; refresh AR token; export the AR package env
        (SWEBENCH_REGISTRY_IMAGE_PACKAGE) and DOCKER_REGISTRY_URL
      - derive the output dir from the harness (construct_eval_output_dir), NOT a hardcode (§13 F11)
      - DooD gate -> SWEV_DOOD_HOST_FIX applied inline only on the inference command (§8.7)
      - task_range -> --n-limit END+1 (§8.8)
      - START THE REAPER (§9.4)
      - Step 1 inference (swebench-infer): builds agent-server per instance, pulls base from AR
      - STOP THE REAPER; between-phase bulk image cleanup
      - Step 2 scoring (swebench-eval): symlink the harness run_evaluation log dir into logs/ for
        live scoring logs; the sitecustomize AR adapter points the harness at the AR package
      - Step 3 results: reconcile against output.critic_attempt_*.jsonl (errored rows dropped by
        aggregate_results) -> honest total/pass@1/infer_errors; write metrics JSON atomically; trap - EXIT
C5. input_params (§6): model, base_url, num_workers, max_iterations, eval_timeout,
      task_range, resume (select true/false).
C6. Register (§5): machine_type n2-standard-16/32 (disk headroom!); pin branch/sha.
C7. Verify (§15): bash -n; shellcheck; stub-uv functional test of run.sh (exercise the trap
      fallback + the success path + range 0-49 -> 50 + EVAL_RUNNER_OUTPUT_DIR honored);
      run the registry-layout unit test.
C8. Smoke run range 0-9. Watch: images build, containers become healthy (host fix engaged),
      disk stays bounded (reaper working: `docker system df` Build Cache small), results reconciled.
      Then a 0-49 run, then full.
```

`RULE:` For Runbook C, the two things that most often bite on the first real run are (a) the DooD health-check fix not engaging (containers "unhealthy" though up — §9.2) and (b) disk filling from build cache (§9.4/F19). Confirm both explicitly in the smoke run before scaling.

---

## Section 15 — Testing & verification before you ship

`RULE:` Never let the dashboard be your first test. A Batch run costs money and time; a bug that a 2-second `bash -n` would catch should never reach a VM. Verify locally, then smoke-test on a tiny range, then scale.

### 15.1 Static checks (seconds, run on every edit)

```bash
bash -n setup.sh && bash -n run.sh                 # syntax
shellcheck setup.sh run.sh scripts/*.sh            # lint (fix or justify every warning)
python3 -c 'import json,sys; json.load(open("input_param.json"))'   # schema is valid JSON
```

### 15.2 Local stub / dry-run tests (minutes)

`RULE:` Exercise both failure and success paths without a real model or a real VM:
- **Results-writer test**: stub the eval so it produces a tiny fake output, run `run.sh`, assert `${id}_results.json` exists and has `metrics.main.value` numeric.
- **EXIT-trap fallback test**: make the eval `exit 1` partway; assert a fallback zero-metric results file was written with a `reason`.
- **`EVAL_RUNNER_OUTPUT_DIR` honored**: set it to a temp dir; assert results land there.
- **Range math**: assert `task_range 0-49` yields the harness's "50" (or your equivalent) — catches the off-by-one (F12).
- **Manual-run path**: run `run.sh` with no args; assert it synthesizes `local_<ts>` and still writes results.
- **`.pth` dual-gate check** (if you patch a harness): in a scratch venv, import the patch module with the gate off (assert inert) and with the gate on (assert patched); confirm the harness CLI still boots and logs the patch line.

swe-verified did exactly this with a "stub-uv" that replaced the real `uv run swebench-infer` to exercise `run.sh`'s trap fallback, the nested output dir handling, range 0-49→50, and `EVAL_RUNNER_OUTPUT_DIR` honoring — all before any Batch run.

### 15.3 Seeder dry-run (if you use Artifact Registry)

```bash
./scripts/seed_artifact_registry.sh --dry-run --limit 0   # prints every source -> dest mapping
```
`RULE:` Eyeball the mapping: it must be `<registry>/<one-package>:<instance-tag>`, not one package per instance (§10.3/§10.4). If you have a layout unit test (swe-verified does), run it.

### 15.4 The smoke run (first dashboard run — always)

`RULE:` The first dashboard run is always a **smoke range** (e.g. `task_range 0-9`), not the full set. Then read the logs and confirm, explicitly:
- `setup.sh` completed (no `sudo`, docker CLI installed, AR auth OK, patch installed).
- `run.sh` progressed live (heartbeat lines, per-trial logs) — not silent.
- For agentic/DooD: containers became healthy (health fix engaged), grading produced `verifier/:yes` (mount fix engaged), disk stayed bounded (reaper working — `docker system df` build cache small).
- A results JSON was written with a sane `main.value`.
Only after a clean smoke run do you scale to a partial (0-49) then full run.

### 15.5 CHECKLIST: master go-live checklist

`CHECKLIST:` Do not announce the eval as ready until every box is checked.

Contract:
- [ ] `setup.sh` + `run.sh` at repo root; run under `bash`.
- [ ] `run.sh` writes `${EVAL_RUNNER_OUTPUT_DIR}/${id}_results.json` in the canonical shape, atomically.
- [ ] EXIT-trap fallback armed early, disarmed after real write.
- [ ] Unknown `run.sh` flags tolerated; manual-run path works.

Environment/scripts:
- [ ] `stdbuf` re-exec; `set -u` no `set -e` (setup); `set -uo pipefail` (run).
- [ ] `SUDO=""` wrapper; no unconditional `sudo`.
- [ ] Docker CLI-only + buildx (if building), pinned 24.x, under DooD.
- [ ] AR auth via metadata token; state persisted + refreshed.
- [ ] Harness pinned; patches installed as `.pth`, gated, dual-verified, hard-fail if broken in DooD.
- [ ] Large/transient data outside `output/`+`logs/`, gitignored.
- [ ] Progress streamed live; heartbeat for harbor; secrets never logged/synced.

Docker/disk (agentic):
- [ ] DooD health-check fix (if applicable) engaged in smoke run.
- [ ] harbor mount fix (if applicable) engaged (`verifier/:yes`).
- [ ] Reaper (if building/pulling per instance): build cache pruned every tick, panic floor set, repo-sweep, correct timestamp parsing.
- [ ] `machine_type` sized for worker count + disk.

Artifact Registry (if applicable):
- [ ] Images seeded from eval-vm2 in the one-package-per-instance-tag format.
- [ ] Console shows a single clean package.
- [ ] Per-run pull re-tags to expected names; token refreshed.

Models:
- [ ] Bare model ids (free tier); no stray `Grid/` prefix (except opencode).
- [ ] Key fanned out to all provider env names.

Registration:
- [ ] Repo public or Juspay-org + token-reachable; branch/commit listed in the form.
- [ ] Eval row created with repo_url, pin, machine_type, input_params.

Verification:
- [ ] `bash -n` + shellcheck clean; local stub tests pass (fallback + success).
- [ ] Seeder dry-run mapping correct; layout test passes.
- [ ] Clean smoke run (0-9) on the dashboard; then partial; then full.

Handoff:
- [ ] New war stories written into §13 (and a memory file).
- [ ] README documents the input_params schema (especially if dashboard-external).

---

## Section 16 — Appendices (quick reference)

### Appendix A — The environment-variable reference

| Variable | Set by | Available to | Meaning |
|---|---|---|---|
| `EVAL_RUNNER_WORK_DIR` | runner (explicit) | setup.sh, run.sh | cloned repo root == cwd |
| `EVAL_RUNNER_LOGS_DIR` | runner (explicit) | setup.sh, run.sh | `runner_logs/` (synced full every 30s) |
| `EVAL_RUNNER_OUTPUT_DIR` | runner (explicit) | setup.sh, run.sh | `repo/output/` — write `<id>_results.json` here |
| `EVAL_RUN_ID` | scheduler | inherited | the run UUID (also run.sh positional arg) |
| `RUNNER_TOKEN` | scheduler | inherited | runner→backend JWT (not a GCP cred) |
| `BACKEND_URL` | scheduler | inherited | dashboard backend base URL |
| `STORAGE_PROVIDER`, `GCS__PROJECT_ID`, `GCS__BUCKET` | scheduler | inherited | GCS artifact upload target |
| `GRID_AI_API` | Secret Manager | inherited | Grid AI key (also run.sh positional `$1`) |
| `GITHUB_TOKEN` | Secret Manager | inherited | private-repo clone; authenticated GitHub |
| `DOCKER_REGISTRY_URL` | your scripts (default) | your scripts | `us-central1-docker.pkg.dev/xyne-dev-461113/eval-dashboard` |
| `SWEV_DOOD_HOST_FIX` / `TB_HARBOR_UNMOUNTED` / `SA_HARBOR_UNMOUNTED` | your run.sh | harness patch | DooD patch gates (§9) |

Env vars **you** typically export in run.sh for models (§12): `GRID_AI_API_KEY`, `LITE_LLM_API_KEY`, `LITE_LLM_URL`, `LITE_LLM_MODEL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `GEMINI_API_KEY`, `XYNE_API_KEY`, `XYNE_BASE_URL`.

### Appendix B — The canonical results JSON (and the four real shapes)

Contract (must-hold): `metrics` object; `metrics.main.name` string; `metrics.main.value` number; `secondary`/`additional` optional opaque JSON.

```json
{ "metrics": {
    "main":       { "name": "<headline>", "value": <number> },
    "secondary":  { "<flat_scalar>": <number|string>, "...": "..." },
    "additional": { "<nested>": { "...": "..." } }
} }
```

- swe-verified: `main {"Total Resolved", <int>}`; secondary `{pass@1, resolved, unresolved, total_tasks}`; additional `{pass@1:{generated,resolved,unresolved,errors,empty_patches,infer_errors,submitted}}`.
- swe-atlas: `main {"Score", <pct>}`; secondary `{score_pct, tasks_total, tasks_judged, no_grade, attempts_per_task}`; additional `{methodology, agent, model, judge_model, split, harbor_exit_code, per_task_score, per_task_detail}`.
- terminal-bench: `main {"Solved", <count>}`; secondary `{solved, unsolved, no_grade, total, solve_rate_pct}`; additional `{agent, model, dataset, attempts_per_task, harbor_exit_code, solved_tasks, ...}`.
- swe-auto-eval: `main {"Total Resolved", <unique across passes>}`; secondary `{pass@1, pass@2, ...}` (flat per-pass resolved); additional `{pass@n:{generated,unresolved,errors,no_patch}}`.

### Appendix C — The four input_params schemas (see §6.5 for full tables)

- swe-verified `input_config_schema.json`: `model`(req), `base_url`, `num_workers`, `max_iterations`, `eval_timeout`, `task_range`, `resume`.
- swe-atlas `input_param.json`: `model`(req), `judge_model`, `agent`(req, options), `split`(options), `attempts`, `concurrency`, `range`, `base_url`, `override_cpus`, `agent_timeout`, `log_level`.
- terminal-bench: none in-repo (dashboard-defined; defaults in `config.yaml`).
- swe-auto-eval: dashboard-side — `model`(req), `coding_agent`(req, options), `parallel_instances`, `runner_timeout`, multipass knobs.

### Appendix D — Where things live (file-tree map)

Dashboard repo (`xyne-eval-ops-dashboard`, shared infra — read, don't edit):
```
eval-runner/src/executor/job_executor.rs   # clone + run setup.sh/run.sh; env vars; results parse
eval-runner/src/log_sync.rs                # what/when uploads (runner_logs 30s, repo subtrees 150s)
eval-runner/src/storage/gcs.rs             # GCS via ADC/metadata
eval-scheduler/src/dispatcher/batch.rs     # Batch job; docker.sock mount; env/secrets; machine_type
eval-dashboard-backend/src/evals/          # evals CRUD; input_params validation; results persistence
docs/plans/2026-07-06-eval-result-analyzer-design.md   # closest existing contract doc
DIND_PERFORMANCE_ANALYSIS.md               # DooD rationale
```
A benchmark repo (yours) — the parts the runner cares about:
```
setup.sh                      # provisioning (no args)
run.sh                        # entrypoint ([key] id --flags) -> writes results.json
output/<id>_results.json      # THE deliverable
logs/                         # verbose bounded logs (synced 150s)
workspaces/ or cache/         # large/transient — NOT synced, gitignored
scripts/                      # seeders, pull scripts, .pth patches, dataset fetch
input_param.json              # mirror of the dashboard input schema (optional)
config.yaml                   # models/agents/dataset/gar constants (harbor evals)
```

### Appendix E — Glossary

- **COS** — Container-Optimized OS; the Batch VM's minimal, read-only-root host OS.
- **DooD** — Docker-outside-of-Docker; the runner uses the host's docker via a bind-mounted socket; containers are siblings.
- **DinD** — Docker-in-Docker; a daemon inside the container. Does NOT work here (no `CAP_SYS_ADMIN`).
- **eval-runner** — the Rust binary in the Batch container that runs your scripts and syncs logs.
- **eval-scheduler** — the Rust service that launches Batch jobs. Shared infra.
- **harbor** — the agentic test harness used by swe-atlas (0.6.6 vendored) and terminal-bench (0.13.1 PyPI).
- **AR / GAR** — (Google) Artifact Registry; the shared `eval-dashboard` repo for images.
- **eval-vm2** — the GCE VM in the `xyne-dev` project used to push (seed) images to AR.
- **Grid** — Juspay's model gateway at `https://grid.ai.juspay.net`; bare ids = free, `Grid/`-prefix = paid.
- **the reaper** — the pressure-triggered disk watchdog that prunes images + build cache (§9.4).
- **seeder** — the script that mirrors source images into AR (one package + per-instance tags).
- **smoke run** — a tiny (e.g. `task_range 0-9`) first dashboard run to catch contract/topology bugs cheaply.

### Appendix F — Command cheat-sheet

```bash
# metadata OAuth token
curl -s -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])'
# (if the hostname is flaky, use the IP 169.254.169.254 instead)

# authenticate docker to Artifact Registry
echo "$TOKEN" | docker login -u oauth2accesstoken --password-stdin https://us-central1-docker.pkg.dev
# or: gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

# seed images (on eval-vm2), batched + idempotent
SEED_PULL_TIMEOUT=1800 SEED_TIMEOUT=1800 ./scripts/seed_artifact_registry.sh --limit 200 --parallel 8
./scripts/seed_artifact_registry.sh --dry-run --limit 0     # print all source->dest mappings

# disk / cache triage on a live VM
docker system df                       # Images vs Build Cache (the latter is the silent killer)
docker builder prune -f                # free INACTIVE build cache (rmi/image-prune do NOT)
docker builder prune -af               # emergency: all inactive build cache
docker image prune -f                  # dangling images

# static verification
bash -n setup.sh && bash -n run.sh
shellcheck setup.sh run.sh scripts/*.sh
python3 -c 'import json;json.load(open("input_param.json"))'
```

### Appendix G — The one-paragraph contract (memorize this)

A benchmark is a git repo with an executable `setup.sh` (runs first, no args, cwd = repo root, installs everything) and `run.sh` (runs second; args `[grid_key?] eval_run_id --field value …` from the eval's `input_params`). Both run as **root without sudo** in a **Debian container on a COS host**, with Docker as **DooD** (socket-only, no host-shared FS), reaching GCS/Artifact Registry via the **VM service account (metadata/ADC)** — no creds handed to you. `run.sh` MUST write `${EVAL_RUNNER_OUTPUT_DIR}/${eval_run_id}_results.json` = `{"metrics":{"main":{"name":<str>,"value":<number>},"secondary":{…},"additional":{…}}}`, **atomically and always** (EXIT-trap fallback) — or the run is FAILED. Only `runner_logs/`, `repo/output/`, `repo/logs/` upload, so keep big/transient data elsewhere. If it uses per-task Docker images, mirror them into the **one shared AR repo** as **one package + per-instance tags** (never per-image), seeded from **eval-vm2**, pulled per run. Patch the harness for DooD via **`.pth`** shims (never `sitecustomize.py`), gated + self-verified. Reap **build cache every tick** if you build/pull per instance. Use **bare** Grid model ids. Register the eval as a row (repo public or Juspay-org+token-reachable). **Verify locally, smoke-run `0-9`, then scale. Never touch shared infra. Never assume — verify.**

---

*End of guide. When you learn something new onboarding your eval, add it to §13 and write a memory file so the next person inherits it. Pass the torch.*
