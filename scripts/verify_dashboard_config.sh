#!/usr/bin/env bash
# Local preflight for eval-ops dashboard registration (Runbook A).
# Checks the repo scripts and schema; prints what to paste into the dashboard UI.
# Does not call the dashboard API — run this before updating the eval row.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ok()  { echo "[verify] OK   $*"; }
bad() { echo "[verify] FAIL $*" >&2; FAIL=1; }

FAIL=0

echo "===== Repo scripts ====="
for f in setup.sh run.sh input_param.json scripts/dashboard_defaults.sh; do
    [ -f "$f" ] || bad "missing $f"
done
bash -n setup.sh && ok "setup.sh syntax"
bash -n run.sh && ok "run.sh syntax"
# shellcheck disable=SC1091
. scripts/dashboard_defaults.sh
ok "suite dataset default=${DECISION_INDEX_SUITE_DATASET_DEFAULT} edition=${DECISION_INDEX_SUITE_EDITION_DEFAULT}"

echo
echo "===== input_param.json ====="
if .venv/bin/python -c 'import json; json.load(open("input_param.json"))' 2>/dev/null; then
    ok "input_param.json parses"
else
    python3 -c 'import json; json.load(open("input_param.json"))' && ok "input_param.json parses"
fi
if .venv/bin/python -m pytest tests/test_dashboard_contract.py -q 2>/dev/null; then
    ok "dashboard contract tests"
else
    bad "dashboard contract tests (run: .venv/bin/python -m pytest tests/test_dashboard_contract.py)"
fi

echo
echo "===== Hugging Face suite (optional) ====="
if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python3
fi
if "$PY" -m decision_index suite download --edition "${DECISION_INDEX_SUITE_EDITION_DEFAULT}" --dir /tmp/di-verify-suite --dataset "${DECISION_INDEX_SUITE_DATASET_DEFAULT}" >/tmp/di-verify-download.json 2>/tmp/di-verify-download.err; then
    ok "suite download from ${DECISION_INDEX_SUITE_DATASET_DEFAULT}"
    rm -rf /tmp/di-verify-suite /tmp/di-verify-download.json /tmp/di-verify-download.err
else
    bad "suite download failed (private dataset? add HF_TOKEN to .env for local verify)"
    tail -3 /tmp/di-verify-download.err >&2 || true
fi

echo
echo "===== Pin this commit on the eval row ====="
git rev-parse HEAD
git log -1 --oneline

echo
echo "===== Dashboard UI (manual — not checked from here) ====="
cat <<EOF
Register / update eval:
  repo_url:     https://github.com/adii-py/decision-index
  commit_sha:   $(git rev-parse HEAD)
  machine_type: n2-standard-4   (smoke)
  input_params: paste contents of input_param.json (authoritative copy is the DB column)

Suite hosting is NOT a dashboard field:
  dataset:  ${DECISION_INDEX_SUITE_DATASET_DEFAULT}  (scripts/dashboard_defaults.sh)
  download: setup.sh on EVAL_RUNNER_WORK_DIR runs

First smoke run (form):
  model:       any Validate alias (ignored when engine=http)
  model_alpha: your systemone id (e.g. xor-1.2)
  engine:      http
  task_range:  0-9
  resume:      false

Pass: \${EVAL_RUNNER_OUTPUT_DIR}/<eval_run_id>_results.json with numeric Decision Index
      and additional.status == scored
EOF

[ "$FAIL" -eq 0 ] || exit 1
echo
ok "preflight complete"
