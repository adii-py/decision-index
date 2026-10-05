#!/usr/bin/env bash
# setup.sh — Decision Index on the xyne-eval-ops-dashboard.
# Runs as root inside the Debian eval-runner container. No args. cwd = repo root.
# Non-agentic API eval: no Docker, no Artifact Registry.
#
# On dashboard runs (EVAL_RUNNER_WORK_DIR set), downloads the frozen suite from a
# private Hugging Face dataset using HF_TOKEN into suite-0.2/ (~79 MB, edition 0.2.1).

if [ -z "${STDBUF_APPLIED:-}" ] && command -v stdbuf >/dev/null 2>&1; then
    export STDBUF_APPLIED=1
    _SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    exec stdbuf -oL -eL bash "$_SELF" "$@"
fi

set -u
export DEBIAN_FRONTEND=noninteractive
export TZ=Etc/UTC
export HF_HUB_DISABLE_XET=1

log()  { echo "[setup] $*"; }
warn() { echo "[setup] WARNING: $*" >&2; }
die()  { echo "[setup] FATAL: $*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi

apt_get() { $SUDO apt-get "$@" -y -qq || warn "apt-get $* failed (continuing)"; }
apt_get update
apt_get install ca-certificates curl git python3

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv install failed"
fi
export PATH="${HOME}/.local/bin:${PATH}"
command -v uv >/dev/null 2>&1 || die "uv not on PATH after install"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

uv python install 3.12 || die "uv python install 3.12 failed"
uv venv --python 3.12 .venv || die "uv venv failed"
uv pip install --python .venv/bin/python -e . || die "package install failed"
.venv/bin/python -c 'import decision_index, decision_index.engines.grid, decision_index.dashboard_emit' || die "import check failed"

log "python: $(.venv/bin/python -c 'import sys; print(sys.version.split()[0])')"
log "uv: $(command -v uv)"

SUITE_EDITION="${DECISION_INDEX_SUITE_EDITION:-0.2.1}"
SUITE_DATASET="${SUITE_DATASET:-${DECISION_INDEX_SUITE_DATASET:-adi060/decision-index-suite-0.2}}"
case "$SUITE_EDITION" in
    0.1) SUITE_DIR="${SCRIPT_DIR}/suite" ;;
    0.2|0.2.1) SUITE_DIR="${SCRIPT_DIR}/suite-0.2" ;;
    *) die "unsupported DECISION_INDEX_SUITE_EDITION ${SUITE_EDITION}" ;;
esac

suite_ready() {
    local missing=0
    for f in selected-rows.jsonl.gz excluded-questions.json manifest.json; do
        [ -f "${SUITE_DIR}/${f}" ] || missing=1
    done
    if [ "$SUITE_EDITION" != "0.1" ] && [ ! -f "${SUITE_DIR}/added-rows.jsonl.gz" ]; then
        missing=1
    fi
    [ "$missing" -eq 0 ]
}

download_suite() {
    local token="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"
    if [ -z "$token" ]; then
        return 1
    fi
    export HF_TOKEN="$token"
    log "downloading suite dataset=${SUITE_DATASET} edition=${SUITE_EDITION} -> ${SUITE_DIR}"
    .venv/bin/python -m decision_index suite download \
        --edition "$SUITE_EDITION" \
        --dir "$SUITE_DIR" \
        --dataset "$SUITE_DATASET"
}

if suite_ready; then
    log "suite already present under ${SUITE_DIR}"
elif [ -n "${EVAL_RUNNER_WORK_DIR:-}" ] || [ "${DECISION_INDEX_DOWNLOAD_SUITE:-}" = "1" ]; then
    if download_suite; then
        suite_ready || die "suite incomplete after download"
        log "suite download verified under ${SUITE_DIR}"
    else
        if [ -n "${EVAL_RUNNER_WORK_DIR:-}" ]; then
            die "HF_TOKEN is required on dashboard runs to download ${SUITE_DATASET} (set runner secret HF_TOKEN with read access)"
        fi
        warn "HF_TOKEN not set; skipped suite download (set DECISION_INDEX_DOWNLOAD_SUITE=1 and HF_TOKEN to fetch)"
    fi
else
    warn "suite not present under ${SUITE_DIR}; local runs need a built suite or DECISION_INDEX_DOWNLOAD_SUITE=1 + HF_TOKEN"
fi

log "setup complete"
