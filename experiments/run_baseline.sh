#!/usr/bin/env bash
set -euo pipefail

# Baseline run for AURA. Parameters can be set with environment variables or
# overridden positionally: URL DURATION_S WORKERS WINDOW_S WORK_ITERS.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

URL="${1:-${AURA_URL:-http://127.0.0.1:8080/work}}"
DURATION="${2:-${AURA_DURATION_S:-60}}"
WORKERS="${3:-${AURA_WORKERS:-4}}"
WINDOW="${4:-${AURA_WINDOW_S:-10}}"
WORK_ITERS="${5:-${AURA_WORK_ITERS:-1000}}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
JSONL="${AURA_JSONL_OUT:-results/raw/baseline_${STAMP}.jsonl}"
CSV="${AURA_CSV_OUT:-results/raw/baseline_${STAMP}.csv}"
LATENCIES="${AURA_LATENCY_SAMPLES_OUT:-results/raw/baseline_${STAMP}_latencies.csv}"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

ARGS=(--url "$URL" --duration "$DURATION" --workers "$WORKERS" --window "$WINDOW"
      --work-iters "$WORK_ITERS" --scenario baseline --jsonl "$JSONL" --csv "$CSV"
      --latency-samples "$LATENCIES")
if [[ "${AURA_SUBMIT_REPORTS:-0}" == "1" ]]; then
  ARGS+=(--submit-reports)
fi

exec "$PYTHON_BIN" -m aura.client "${ARGS[@]}"
