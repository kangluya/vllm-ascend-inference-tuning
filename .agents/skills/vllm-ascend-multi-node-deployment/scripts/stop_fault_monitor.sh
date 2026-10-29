#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST="${1:-$SCRIPT_DIR/monitor-manifest.json}"

exec python3 "$SCRIPT_DIR/vllm_fault_monitor.py" --manifest "$MANIFEST" --stop
