#!/usr/bin/env bash
# install.sh — Manual installer for Attest SubagentStart/SubagentStop hooks
#
# Wires hooks/attest-subagent-start.sh and hooks/attest-subagent-stop.sh into
# ~/.claude/settings.json for users not using the Claude Code plugin system.
#
# IMPORTANT: SubagentStop is registered with "async": false so that its stdout
# {"decision":"block",...} payload is honored by Claude Code v2.1.170+.
# (The official docs are wrong about SubagentStop being unable to block;
#  synchronous registration is empirically required and verified.)
#
# Usage:
#   bash install.sh              Install Attest hooks (default)
#   bash install.sh --uninstall  Remove only Attest hook entries; preserve others
#   bash install.sh --help       Show this help
#
# Env overrides:
#   ATTEST_SETTINGS   Path to settings.json  (default: ~/.claude/settings.json)
#
# Exit codes: 0 = success, 1 = error

set -euo pipefail

# ── Resolve the absolute directory that contains this script ──────────────────
ATTEST_REPO="$(cd "$(dirname "$0")" && pwd)"

START_CMD="bash ${ATTEST_REPO}/hooks/attest-subagent-start.sh"
STOP_CMD="bash ${ATTEST_REPO}/hooks/attest-subagent-stop.sh"
SETTINGS_FILE="${ATTEST_SETTINGS:-${HOME}/.claude/settings.json}"
ACTION="install"

# ── Argument parsing ──────────────────────────────────────────────────────────
for arg in "$@"; do
	case "$arg" in
	--install) ACTION="install" ;;
	--uninstall) ACTION="uninstall" ;;
	--help | -h) ACTION="help" ;;
	*)
		echo "attest install: unknown argument: ${arg}" >&2
		echo "Usage: bash install.sh [--install|--uninstall|--help]" >&2
		exit 1
		;;
	esac
done

# ── Help ──────────────────────────────────────────────────────────────────────
if [[ "$ACTION" == "help" ]]; then
	cat <<'EOF'
attest install.sh — wire Attest SubagentStart/SubagentStop hooks

Usage:
  bash install.sh              Install hooks into ~/.claude/settings.json
  bash install.sh --uninstall  Remove only Attest hook entries (preserves others)
  bash install.sh --help       Show this help

Env overrides:
  ATTEST_SETTINGS   Path to settings.json  (default: ~/.claude/settings.json)

Runtime env (set in shell profile after install):
  ATTEST_ENFORCE    Set to 1 to enable enforcement mode (blocks false DONEs)
  ATTEST_CAPTURE    Set to 1 to capture raw payloads for debugging
EOF
	exit 0
fi

# ── Detect python3 ────────────────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
	echo "attest install: python3 not found on PATH — please install Python 3." >&2
	exit 1
fi

# ── Ensure the settings directory exists ──────────────────────────────────────
_settings_dir="$(dirname "$SETTINGS_FILE")"
mkdir -p "$_settings_dir"

# ── Create an empty settings.json if none exists ──────────────────────────────
if [[ ! -f "$SETTINGS_FILE" ]]; then
	printf '{}' >"$SETTINGS_FILE"
	echo "attest install: created ${SETTINGS_FILE}"
fi

# ── Back up before any write ──────────────────────────────────────────────────
cp "$SETTINGS_FILE" "${SETTINGS_FILE}.attest.bak"

# ── Read / modify / write JSON with python3 (stdlib only) ────────────────────
# Variables are injected via env so no shell quoting risk inside the Python body.
ATTEST_SETTINGS_FILE="$SETTINGS_FILE" \
	ATTEST_START_CMD="$START_CMD" \
	ATTEST_STOP_CMD="$STOP_CMD" \
	ATTEST_ACTION="$ACTION" \
	python3 - <<'PYEOF'
import json
import os

settings_file = os.environ["ATTEST_SETTINGS_FILE"]
start_cmd = os.environ["ATTEST_START_CMD"]
stop_cmd = os.environ["ATTEST_STOP_CMD"]
action = os.environ["ATTEST_ACTION"]


def _cmd_in_entry(entry: dict, cmd: str) -> bool:
    """Return True if any inner hook in this entry carries the given command."""
    for h in entry.get("hooks", []):
        if isinstance(h, dict) and h.get("command") == cmd:
            return True
    return False


def _make_entry(cmd: str) -> dict:
    """Build a single hook-group entry for the given command."""
    return {
        "hooks": [
            {
                "type": "command",
                "command": cmd,
                "async": False,
                "timeout": 30,
            }
        ]
    }


with open(settings_file, "r") as fh:
    try:
        data = json.load(fh)
    except json.JSONDecodeError:
        data = {}

if not isinstance(data, dict):
    data = {}

if not isinstance(data.get("hooks"), dict):
    data["hooks"] = {}

hooks = data["hooks"]

for event, cmd in [("SubagentStart", start_cmd), ("SubagentStop", stop_cmd)]:
    if not isinstance(hooks.get(event), list):
        hooks[event] = []
    arr = hooks[event]

    if action == "install":
        # Idempotent: append only when no existing entry already carries this command.
        if not any(_cmd_in_entry(e, cmd) for e in arr):
            arr.append(_make_entry(cmd))
    elif action == "uninstall":
        # Remove only entries whose inner hooks reference the Attest shim.
        hooks[event] = [e for e in arr if not _cmd_in_entry(e, cmd)]

with open(settings_file, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PYEOF

# ── Post-action output ────────────────────────────────────────────────────────
if [[ "$ACTION" == "install" ]]; then
	echo ""
	echo "attest install: hooks registered in ${SETTINGS_FILE}"
	echo ""
	echo "Post-install notes:"
	echo "  (a) Attest runs in DETECT mode by default — subagents are never blocked."
	echo "  (b) Enable enforcement (block false DONEs):  export ATTEST_ENFORCE=1"
	echo "  (c) Capture raw payloads for debugging:      export ATTEST_CAPTURE=1"
	echo "  (d) IMPORTANT: Restart your Claude Code session for hook changes to"
	echo "      take effect."
	echo "  (e) Alternative: use the Claude Code plugin (hooks/hooks.json) which"
	echo "      wires hooks automatically via \${CLAUDE_PLUGIN_ROOT}."
	echo ""
elif [[ "$ACTION" == "uninstall" ]]; then
	echo "attest install: Attest hook entries removed from ${SETTINGS_FILE}"
fi
