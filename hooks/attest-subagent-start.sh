#!/usr/bin/env bash
# attest-subagent-start.sh — SubagentStart hook shim for Attest
# Hook event: SubagentStart
#
# Fires when a subagent is spawned via the Agent tool. Snapshots the git
# working tree so attest-subagent-stop.sh can compute the delta after the
# agent completes.
#
# Exit code: always 0. Always exits cleanly; never blocks a subagent by design.
# This shim only snapshots the working tree — detect mode by default.
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
#   See README.md "Enforcement (opt-in)" table or docs/INSTALL.md
#   "Configuration (environment variables)" for the full canonical set.
#   This shim reads ATTEST_PYTHON directly (default: python3); all other
#   vars are passed through to the Python handler at runtime.

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
# shellcheck disable=SC2015  # Intentional A && B || C: pwd cannot fail after a
# successful cd, so the `dirname "$0"` fallback runs only when cd itself fails.
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || dirname "$0")"
ATTEST_REPO="$(dirname "$SCRIPT_DIR")"

# ── Dispatch to the Python hook handler ──────────────────────────────────────
printf '%s\n' "$INPUT" | PYTHONPATH="$ATTEST_REPO:${PYTHONPATH:-}" \
  "$PYTHON" -m attest.hook start 2>/dev/null || \
  _log_error "attest.hook start failed (exit $?)"

exit 0
