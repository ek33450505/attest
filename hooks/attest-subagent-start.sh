#!/usr/bin/env bash
# attest-subagent-start.sh — SubagentStart hook shim for Attest
# Hook event: SubagentStart
#
# Fires when a subagent is spawned via the Agent tool. Snapshots the git
# working tree so attest-subagent-stop.sh can compute the delta after the
# agent completes.
#
# Exit code: always 0 (detect-and-print mode — never blocks in Phase 1b).
#
# ── Installation (add to ~/.claude/settings.json) ────────────────────────────
#   "SubagentStart": [
#     {
#       "hooks": [
#         {
#           "type": "command",
#           "command": "bash /path/to/attest/hooks/attest-subagent-start.sh"
#         }
#       ]
#     }
#   ]
# ─────────────────────────────────────────────────────────────────────────────
#
# Environment variables:
#   ATTEST_STATE_DB   — path to the attest state SQLite DB
#                       (default: ~/.attest/state.db)
#   ATTEST_CAPTURE    — set to 1 to dump payload + transcript to fixtures/captured/
#   ATTEST_PYTHON     — override the python3 binary (default: python3)

# Never fail loudly — a broken hook must not interrupt the parent session.
set +e

# ── Error logging ─────────────────────────────────────────────────────────────
mkdir -p "${HOME}/.claude/logs" 2>/dev/null || true
_log_error() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR attest-subagent-start.sh: $1" \
    >> "${HOME}/.claude/logs/attest-errors.log" 2>/dev/null || true
}

# ── Read stdin once ───────────────────────────────────────────────────────────
INPUT="$(cat 2>/dev/null || true)"
if [ -z "$INPUT" ]; then
  exit 0
fi

# ── Locate python3 ────────────────────────────────────────────────────────────
PYTHON="${ATTEST_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  _log_error "python3 not found on PATH — skipping SubagentStart"
  exit 0
fi

# ── Locate the attest package ─────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || dirname "$0")"
ATTEST_REPO="$(dirname "$SCRIPT_DIR")"

# ── Dispatch to the Python hook handler ──────────────────────────────────────
echo "$INPUT" | PYTHONPATH="$ATTEST_REPO:${PYTHONPATH:-}" \
  "$PYTHON" -m attest.hook start 2>/dev/null || \
  _log_error "attest.hook start failed (exit $?)"

exit 0
