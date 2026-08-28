#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ============================================================
# Configuration (override via env vars or command-line args)
# ============================================================
MODEL="${BENCHMARK_MODEL:-gpt-5}"
MAX_DAYS="${BENCHMARK_MAX_DAYS:-365}"
MAX_TURNS="${BENCHMARK_MAX_LLM_CALLS:-4000}"
RUNS="${BENCHMARK_RUNS:-1}"
BASE_LOG_DIR="${BENCHMARK_LOG_DIR:-${SCRIPT_DIR}/log}"
PLOT_INTERVAL="${BENCHMARK_PLOT_INTERVAL:-180}"

# ---- Misc ----
export API_MAX_RETRIES="${API_MAX_RETRIES:-100}"

# ---- Create session directory ----
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SAFE_MODEL="$(echo "${MODEL}" | sed 's/[^a-zA-Z0-9._-]/_/g')"
SESSION_DIR="${BASE_LOG_DIR}/${TIMESTAMP}_${SAFE_MODEL}"
mkdir -p "${SESSION_DIR}"

# ============================================================
echo "=== E-Commerce Bench ==="
echo "Model:          ${MODEL}"
echo "Max Days:       ${MAX_DAYS}"
echo "Max Turns:      ${MAX_TURNS}"
echo "Runs:           ${RUNS}"
echo "Session Dir:    ${SESSION_DIR}"
echo "Plot Interval:  ${PLOT_INTERVAL}s"
echo "Tokenizer:      ${TOKENIZER_PATH:-}"
echo "GPT Base URL:   ${GPT_BASE_URL:-}"
echo "========================"

# ---- Start background plotting ----
(
    while true; do
        sleep "${PLOT_INTERVAL}"
        python "${SCRIPT_DIR}/evaluation/plot_daily_balance.py" \
            "${SESSION_DIR}"/balance/run_*_daily_balance.csv \
            --output-dir "${SESSION_DIR}/plots" \
            --overlay-name "${SAFE_MODEL}_${TIMESTAMP}" 2>/dev/null || true
    done
) &
PLOT_PID=$!

# ---- Clean teardown on Ctrl+C / kill / normal exit ----
# Why this is needed: SIGINT (Ctrl+C) only reaches run.py's main thread as a
# KeyboardInterrupt, but its non-daemon ThreadPoolExecutor workers stay blocked
# in network retries (API_MAX_RETRIES backoff), so the python process — and this
# plot loop — would otherwise survive as orphans after this shell exits. We run
# run.py in the background and `wait` on it so this trap can fire *mid-run* (a
# foreground command would defer the trap until it returns, which never happens
# when it's hung), then SIGTERM the children. SIGTERM has no Python handler, so
# the interpreter terminates immediately instead of hanging on the thread join.
RUN_PID=""
cleanup() {
    trap - INT TERM EXIT
    [ -n "${RUN_PID}" ] && kill "${RUN_PID}" 2>/dev/null || true
    kill "${PLOT_PID}" 2>/dev/null || true
    pkill -P "${PLOT_PID}" 2>/dev/null || true   # the sleep/plot child the loop is in
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

python "${SCRIPT_DIR}/run.py" \
    --model "${MODEL}" \
    --max-days "${MAX_DAYS}" \
    --max-turns "${MAX_TURNS}" \
    --runs "${RUNS}" \
    --log-dir "${SESSION_DIR}" \
    "$@" &
RUN_PID=$!
wait "${RUN_PID}"

# ---- Final plot after run completes ----
# Reuse the live plot's name so the complete (all-runs-finished) figure
# overwrites the last stale background snapshot instead of leaving a second file.
python "${SCRIPT_DIR}/evaluation/plot_daily_balance.py" \
    "${SESSION_DIR}"/balance/run_*_daily_balance.csv \
    --output-dir "${SESSION_DIR}/plots" \
    --overlay-name "${SAFE_MODEL}_${TIMESTAMP}" 2>/dev/null || true
