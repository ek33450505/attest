#!/usr/bin/env bash
# attest-subagent-stop.sh — SubagentStop hook shim for Attest
# Hook event: SubagentStop
#
# Fires when a subagent completes. Loads the start snapshot, computes the git
# delta, parses the agent's completion claim, evaluates the verdict, and prints
# a concise human-readable report.
#
# Exit code: ALWAYS 0. In detect mode the hook only prints a report. In enforce
# mode a proven false DONE is blocked via a stdout JSON {"decision":"block"}
# payload — the exit code stays 0; the block travels through stdout, never via
# exit 2 — so a broken or slow hook can never wedge the parent session (fail-open).
#
# ── Installation (add to ~/.claude/settings.json) ────────────────────────────
#   "SubagentStop": [
#     {
#       "hooks": [
#         {
#           "type": "command",
#           "command": "bash /path/to/attest/hooks/attest-subagent-stop.sh"
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
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR attest-subagent-stop.sh: $1" \
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
  _log_error "python3 not found on PATH — skipping SubagentStop"
  exit 0
fi

# ── Locate the attest package ─────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || dirname "$0")"
ATTEST_REPO="$(dirname "$SCRIPT_DIR")"

# ── Dispatch to the Python hook handler ──────────────────────────────────────
# stdout flows through unchanged — in enforce mode it carries ONLY the
# {"decision":"block",...} JSON that Claude Code reads. Python diagnostics go to
# stderr; route them to the error log (not /dev/null) so enforce-mode decisions
# are observable, and so the hook's stdout stays pure JSON.
ATTEST_LOG="${HOME}/.claude/logs/attest-errors.log"
echo "$INPUT" | PYTHONPATH="$ATTEST_REPO:${PYTHONPATH:-}" \
  "$PYTHON" -m attest.hook stop 2>>"$ATTEST_LOG" || \
  _log_error "attest.hook stop failed (exit $?)"

# Always exit 0: the block signal travels via stdout JSON, never the exit code,
# so a broken/slow hook can never wedge the parent session (fail-open by design).
exit 0
