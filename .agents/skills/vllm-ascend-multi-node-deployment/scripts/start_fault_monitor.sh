#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST="${1:-$SCRIPT_DIR/monitor-manifest.json}"
LOG_FILE="${2:-$SCRIPT_DIR/fault-monitor.log}"
PID_FILE="$SCRIPT_DIR/monitor.pid"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "fault monitor is already running with pid $(cat "$PID_FILE")" >&2
  exit 0
fi

nohup python3 "$SCRIPT_DIR/vllm_fault_monitor.py" \
  --manifest "$MANIFEST" \
  >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
echo "fault monitor started with pid $!"
