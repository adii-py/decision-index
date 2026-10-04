#!/usr/bin/env bash
# run.sh — Decision Index entrypoint for the xyne-eval-ops-dashboard.
# Args: [GRID_AI_KEY] EVAL_RUN_ID --flags
#
# engine=http (default): POST {base_url}/v1/systemone with model_alpha; the dashboard chat
# model is ignored. base_url defaults to https://grid.ai.juspay.net; trailing /v1 or
# /v1/systemone is stripped. Early stop on HTTP 401/403/429 or 3 consecutive request errors
# completes with partial results (ended_abruptly=1). Only a run that never starts is FAILED.

if [ -z "${STDBUF_APPLIED:-}" ] && command -v stdbuf >/dev/null 2>&1; then
    export STDBUF_APPLIED=1
    _SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    exec stdbuf -oL -eL bash "$_SELF" "$@"
fi

set -uo pipefail
export PYTHONUNBUFFERED=1
export NO_COLOR=1
export HF_HUB_DISABLE_XET=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log_info() { echo "[run][INFO]  $*"; }
log_ok()   { echo "[run][OK]    $*"; }
log_warn() { echo "[run][WARN]  $*" >&2; }
log_err()  { echo "[run][ERR]   $*" >&2; }
log_step() { echo; echo "===== $* ====="; }

normalize_base_url() {
    local url="${1%/}"
    case "$url" in
        */v1/systemone) url="${url%/v1/systemone}" ;;
        */v1) url="${url%/v1}" ;;
    esac
    printf '%s' "$url"
}

API_KEY=""
EVAL_RUN_ID=""
if [ -n "${1:-}" ] && [ "${1#--}" = "$1" ] && [ -n "${2:-}" ] && [ "${2#--}" = "$2" ]; then
    API_KEY="$1"
    EVAL_RUN_ID="$2"
    shift 2
elif [ -n "${1:-}" ] && [ "${1#--}" = "$1" ]; then
    EVAL_RUN_ID="$1"
    shift
else
    EVAL_RUN_ID="local_$(date +%Y%m%d_%H%M%S)"
fi
if [ -z "$API_KEY" ]; then
    API_KEY="${GRID_AI_API:-}"
fi

MODEL=""
MODEL_ALPHA=""
BASE_URL="https://grid.ai.juspay.net"
TASK_RANGE=""
ENGINE="http"
EDITION="0.2.1"
TIMEOUT="600"
DELAY_S="0.5"
RESUME="true"
SUITE_DATASET=""
INPUT_TOKEN_PRICE=""
OUTPUT_TOKEN_PRICE=""
NO_VERIFY="false"
PRICE_INPUT_SET=0
PRICE_OUTPUT_SET=0

while [ $# -gt 0 ]; do
    case "$1" in
        --model) MODEL="${2:-}"; shift 2 ;;
        --model-alpha) MODEL_ALPHA="${2:-}"; shift 2 ;;
        --base-url) BASE_URL="${2:-}"; shift 2 ;;
        --task-range) TASK_RANGE="${2:-}"; shift 2 ;;
        --engine) ENGINE="${2:-}"; shift 2 ;;
        --edition) EDITION="${2:-}"; shift 2 ;;
        --timeout) TIMEOUT="${2:-}"; shift 2 ;;
        --delay-s) DELAY_S="${2:-}"; shift 2 ;;
        --resume) RESUME="${2:-true}"; shift 2 ;;
        --suite-dataset) SUITE_DATASET="${2:-}"; shift 2 ;;
        --no-verify) NO_VERIFY="true"; shift ;;
        --input-token-price) INPUT_TOKEN_PRICE="${2:-}"; PRICE_INPUT_SET=1; shift 2 ;;
        --output-token-price) OUTPUT_TOKEN_PRICE="${2:-}"; PRICE_OUTPUT_SET=1; shift 2 ;;
        --*)
            log_warn "ignoring unknown flag: $1"
            if [ $# -ge 2 ] && [ "${2#--}" = "$2" ]; then
                shift 2
            else
                shift
            fi
            ;;
        *)
            log_warn "ignoring unexpected argument: $1"
            shift
            ;;
    esac
done

ORIGIN="$(normalize_base_url "$BASE_URL")"
case "$ENGINE" in
    http)
        ENGINE_BASE="$ORIGIN"
        ;;
    grid)
        ENGINE_BASE="${ORIGIN}/v1"
        ;;
    random)
        ENGINE_BASE="$ORIGIN"
        ;;
esac

if [ -n "$API_KEY" ]; then
    export GRID_AI_API_KEY="$API_KEY"
    export LITE_LLM_API_KEY="$API_KEY"
    export OPENAI_API_KEY="$API_KEY"
    export OPENAI_BASE_URL="${ORIGIN}/v1"
    export ANTHROPIC_API_KEY="$API_KEY"
    export ANTHROPIC_AUTH_TOKEN="$API_KEY"
    export ANTHROPIC_BASE_URL="$ORIGIN"
    export GEMINI_API_KEY="$API_KEY"
    export DECISION_INDEX_API_KEY="$API_KEY"
    export DECISION_INDEX_BASE_URL="$ORIGIN"
fi

OUTPUT_ROOT="${EVAL_RUNNER_OUTPUT_DIR:-${SCRIPT_DIR}/output}"
mkdir -p "$OUTPUT_ROOT"
RESULTS_FILE="${OUTPUT_ROOT}/${EVAL_RUN_ID}_results.json"
RUN_DIR="${SCRIPT_DIR}/runs/${EVAL_RUN_ID}"
LOGS_DIR="${SCRIPT_DIR}/logs/decision-index/${EVAL_RUN_ID}"
mkdir -p "$RUN_DIR" "$LOGS_DIR"

if [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
    PY="${SCRIPT_DIR}/.venv/bin/python"
else
    PY="$(command -v python3)"
fi
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:$PYTHONPATH}"

FAIL_REASON=""
HB_PID=""
RUN_RC=0
ENDED_ABRUPTLY=0
STOP_REASON=""
ATTEMPTED_OK_RATE=""

write_fallback_results() {
    [ -f "$RESULTS_FILE" ] && return 0
    local reason="${1:-unknown}"
    REASON="$reason" "$PY" - "$RESULTS_FILE" <<'PY'
import json, os, sys
path = sys.argv[1]
doc = {
    "metrics": {
        "main": {"name": "Decision Index", "value": 0},
        "secondary": {"decision_index": 0, "raw_index": 0, "completed": 0, "complete": 0},
        "additional": {"status": "no-results", "reason": os.environ.get("REASON", "unknown")},
    }
}
tmp = path + ".tmp"
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, indent=2)
    handle.write("\n")
os.replace(tmp, path)
PY
    log_warn "wrote fallback zero-metric results (${reason})"
}

on_exit() {
    local rc=$?
    if [ -n "${HB_PID}" ]; then
        kill "${HB_PID}" 2>/dev/null || true
    fi
    write_fallback_results "${FAIL_REASON:-exit ${rc}}"
}
trap on_exit EXIT

ROW_START=""
ROW_END=""
RANGE_NOTE=""
if [ -n "$TASK_RANGE" ]; then
    if [[ ! "$TASK_RANGE" =~ ^[0-9]+-[0-9]+$ ]]; then
        log_warn "task_range '${TASK_RANGE}' is not START-END; running the full suite"
        TASK_RANGE=""
    else
        ROW_START="${TASK_RANGE%-*}"
        ROW_END="${TASK_RANGE#*-}"
        if [ "$ROW_END" -lt "$ROW_START" ]; then
            log_warn "task_range ${TASK_RANGE} has end < start; running the full suite"
            TASK_RANGE=""
            ROW_START=""
            ROW_END=""
        else
            RANGE_NOTE="task_range ${TASK_RANGE} is inclusive (rows ${ROW_START}-${ROW_END})"
            log_info "$RANGE_NOTE"
        fi
    fi
fi

if [ -z "${EVAL_RUNNER_WORK_DIR:-}" ] && [ "${DECISION_INDEX_STUB:-}" = "fallback" ]; then
    FAIL_REASON="stub fallback"
    exit 1
fi

case "$ENGINE" in
    http)
        RUN_MODEL="${MODEL_ALPHA:-$MODEL}"
        if [ -z "$RUN_MODEL" ]; then
            FAIL_REASON="model_alpha is required for engine=http (dashboard chat model is ignored)"
            log_err "$FAIL_REASON"
            exit 1
        fi
        ;;
    *)
        if [ -z "$MODEL" ]; then
            FAIL_REASON="model is required"
            log_err "$FAIL_REASON"
            exit 1
        fi
        RUN_MODEL="$MODEL"
        ;;
esac

log_info "eval_run_id=${EVAL_RUN_ID} engine=${ENGINE} edition=${EDITION} model=${RUN_MODEL}"
log_info "base_url=${ENGINE_BASE} origin=${ORIGIN} timeout=${TIMEOUT} delay_s=${DELAY_S} resume=${RESUME} row_range=${ROW_START:-full}-${ROW_END:-full}"

case "$ENGINE" in
    grid|http|random) ;;
    *)
        FAIL_REASON="unsupported engine ${ENGINE}"
        log_err "$FAIL_REASON"
        exit 1
        ;;
esac

if [ -z "${EVAL_RUNNER_WORK_DIR:-}" ] && [ "${DECISION_INDEX_STUB:-}" = "success" ]; then
    log_step "Stub success"
    "$PY" - "$RUN_DIR/scores.json" <<'PY'
import json, sys
json.dump({"decision_index": 42, "raw_index": 40, "completed": 2, "complete": False, "edition": "0.2.1", "counts": {"ok": 2}}, open(sys.argv[1], "w"), indent=2)
PY
else
    case "$EDITION" in
        0.1) SUITE_DIR="${SCRIPT_DIR}/suite" ;;
        0.2|0.2.1) SUITE_DIR="${SCRIPT_DIR}/suite-0.2" ;;
        *)
            FAIL_REASON="unsupported edition ${EDITION}"
            log_err "$FAIL_REASON"
            exit 1
            ;;
    esac

    suite_missing=0
    for f in selected-rows.jsonl.gz excluded-questions.json manifest.json; do
        [ -f "${SUITE_DIR}/${f}" ] || suite_missing=1
    done
    if [ "$EDITION" != "0.1" ] && [ ! -f "${SUITE_DIR}/added-rows.jsonl.gz" ]; then
        suite_missing=1
    fi
    if [ "$suite_missing" -eq 1 ]; then
        log_warn "suite not present under ${SUITE_DIR}."
        if [ -n "${EVAL_RUNNER_WORK_DIR:-}" ]; then
            if [ -z "$SUITE_DATASET" ]; then
                FAIL_REASON="suite_dataset is required on dashboard runs (private HF dataset, see README Eval-ops)"
                log_err "$FAIL_REASON"
                exit 1
            fi
            if [ -z "${HF_TOKEN:-}" ] && [ -z "${HUGGING_FACE_HUB_TOKEN:-}" ]; then
                FAIL_REASON="HF_TOKEN must be set on the eval runner to download suite_dataset"
                log_err "$FAIL_REASON"
                exit 1
            fi
        else
            log_warn "Build locally: suite rebuild + suite import, or upload with scripts/prepare_hub_upload.py."
            if [ -z "$SUITE_DATASET" ] && [ -z "${HF_TOKEN:-}" ] && [ -z "${HUGGING_FACE_HUB_TOKEN:-}" ]; then
                log_warn "No --suite-dataset and no HF_TOKEN; download will likely fail."
            fi
        fi
    fi

    FRESH=()
    if [ "$RESUME" = "false" ]; then
        FRESH=(--fresh)
    fi
    RANGE_ARGS=()
    if [ -n "$ROW_START" ] && [ -n "$ROW_END" ]; then
        RANGE_ARGS=(--row-start "$ROW_START" --row-end "$ROW_END")
    fi
    DATASET_ARGS=()
    if [ -n "$SUITE_DATASET" ]; then
        DATASET_ARGS=(--suite-dataset "$SUITE_DATASET")
    fi
    VERIFY_ARGS=()
    if [ "$NO_VERIFY" = "true" ]; then
        VERIFY_ARGS=(--no-verify)
    fi
    OPTION_ARGS=(--option "base_url=${ENGINE_BASE}" --option "timeout=${TIMEOUT}")

    log_step "Running eval"
    (
        while true; do
            sleep 60
            log_info "heartbeat: eval still running eval_run_id=${EVAL_RUN_ID}"
        done
    ) &
    HB_PID=$!

    set +e
    "$PY" -m decision_index run \
        --engine "$ENGINE" \
        --model "$RUN_MODEL" \
        --edition "$EDITION" \
        --suite-dir "$SUITE_DIR" \
        ${DATASET_ARGS[@]+"${DATASET_ARGS[@]}"} \
        --out "$RUN_DIR" \
        --compact \
        --delay-s "$DELAY_S" \
        --logs-dir "$LOGS_DIR" \
        ${VERIFY_ARGS[@]+"${VERIFY_ARGS[@]}"} \
        ${FRESH[@]+"${FRESH[@]}"} \
        ${RANGE_ARGS[@]+"${RANGE_ARGS[@]}"} \
        "${OPTION_ARGS[@]}"
    RUN_RC=$?
    set -e
    kill "$HB_PID" 2>/dev/null || true
    wait "$HB_PID" 2>/dev/null || true
    HB_PID=""
    log_info "run exit ${RUN_RC}"

    if [ ! -s "${RUN_DIR}/results.jsonl" ]; then
        FAIL_REASON="no results.jsonl (run exit ${RUN_RC})"
        log_err "$FAIL_REASON"
        exit 1
    fi

    if [ -f "${RUN_DIR}/status.json" ]; then
        read -r ENDED_ABRUPTLY STOP_REASON ATTEMPTED_OK_RATE < <(
            "$PY" - "${RUN_DIR}/status.json" <<'PY'
import json, sys
status = json.load(open(sys.argv[1]))
print(
    status.get("ended_abruptly", 0),
    status.get("stop_reason", "") or "",
    status.get("ok_rate", "") if status.get("ok_rate") is not None else "",
)
PY
        )
        if [ "$ENDED_ABRUPTLY" = "1" ]; then
            log_warn "run ended early: stop_reason=${STOP_REASON} ok_rate=${ATTEMPTED_OK_RATE}"
        fi
    fi

    log_step "Scoring"
    set +e
    "$PY" -m decision_index score \
        --results "${RUN_DIR}/results.jsonl" \
        --suite-dir "$SUITE_DIR" \
        --edition "$EDITION" \
        --engine "$ENGINE" \
        --out "$RUN_DIR"
    SCORE_RC=$?
    set -e
    if [ "$SCORE_RC" -ne 0 ] || [ ! -f "${RUN_DIR}/scores.json" ]; then
        FAIL_REASON="scoring failed (exit ${SCORE_RC})"
        log_err "$FAIL_REASON"
        exit 1
    fi
fi

log_step "Writing results"
EMIT_ARGS=(
    -m decision_index.dashboard_emit
    --scores "${RUN_DIR}/scores.json"
    --out "$RESULTS_FILE"
    --token-dir "$OUTPUT_ROOT"
    --model "$RUN_MODEL"
    --engine "$ENGINE"
    --edition "$EDITION"
    --task-range "$TASK_RANGE"
    --range-note "$RANGE_NOTE"
    --run-rc "${RUN_RC}"
    --delay-s "$DELAY_S"
    --base-url "$ORIGIN"
)
if [ -f "${RUN_DIR}/results.jsonl" ]; then
    EMIT_ARGS+=(--results-jsonl "${RUN_DIR}/results.jsonl")
fi
if [ -n "$ROW_START" ]; then
    EMIT_ARGS+=(--row-start "$ROW_START")
fi
if [ -n "$ROW_END" ]; then
    EMIT_ARGS+=(--row-end "$ROW_END")
fi
if [ -f "${RUN_DIR}/status.json" ]; then
    EMIT_ARGS+=(--status "${RUN_DIR}/status.json")
fi
if [ "$PRICE_INPUT_SET" -eq 1 ]; then
    EMIT_ARGS+=(--input-token-price "$INPUT_TOKEN_PRICE")
fi
if [ "$PRICE_OUTPUT_SET" -eq 1 ]; then
    EMIT_ARGS+=(--output-token-price "$OUTPUT_TOKEN_PRICE")
fi
"$PY" "${EMIT_ARGS[@]}" || {
    FAIL_REASON="results emit failed"
    log_err "$FAIL_REASON"
    exit 1
}

trap - EXIT
if [ -n "${HB_PID}" ]; then
    kill "${HB_PID}" 2>/dev/null || true
fi
log_ok "Decision Index results at ${RESULTS_FILE}"
log_ok "per-case logs at ${LOGS_DIR}"
"$PY" - "$RESULTS_FILE" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
main = doc["metrics"]["main"]
print("[run][OK]    scoreboard", main["name"], "=", main["value"])
add = doc["metrics"]["additional"]
if add.get("ended_abruptly"):
    print("[run][WARN]  ended_abruptly=1 stop_reason=", add.get("stop_reason"))
PY
exit 0
