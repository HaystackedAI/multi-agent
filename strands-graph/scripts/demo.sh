#!/usr/bin/env bash
# Three-minute local demo of Chaser.
#
#   ./scripts/demo.sh            # seed, run one weekly close, print status, start the UI on :8000
#   ./scripts/demo.sh --dry-run  # print the commands without running the model (no AWS needed)
#
# Needs Bedrock credentials for the real run (see README "Run locally").
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-.venv/bin/python}
PORT=${PORT:-8000}
export DEMO_TODAY=${DEMO_TODAY:-2026-09-12}

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

if [[ "${1:-}" == "--dry-run" ]]; then
  cat <<EOF
Demo plan (DEMO_TODAY=$DEMO_TODAY):
  1. $PY -m chaser.cli seed                  # fresh demo books: 7 clients, 14 invoices, 30 days of bank activity
  2. $PY -m chaser.cli sweep                 # weekly close: reconciler -> bookkeeper + collector -> reporter
  3. $PY -m chaser.cli status                # counts, pending approvals, last report
  4. $PY -m uvicorn app.server:app --port $PORT
     open http://localhost:$PORT             # review the approvals queue, approve/skip, ask a question
Nothing was executed (--dry-run).
EOF
  exit 0
fi

step "Seeding demo data"
"$PY" -m chaser.cli seed

step "Running the weekly close (this calls Bedrock; ~1-2 minutes)"
"$PY" -m chaser.cli sweep

step "Status"
"$PY" -m chaser.cli status

step "Starting the web UI on http://localhost:$PORT (Ctrl-C to stop)"
SWEEP_INTERVAL_SECONDS=${SWEEP_INTERVAL_SECONDS:-0} "$PY" -m uvicorn app.server:app --port "$PORT"
