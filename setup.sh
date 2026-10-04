#!/usr/bin/env bash
# setup.sh — Decision Index on the xyne-eval-ops-dashboard.
# Runs as root inside the Debian eval-runner container. No args. cwd = repo root.
# Non-agentic API eval: no Docker, no Artifact Registry.

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
log "setup complete"
