#!/usr/bin/env bats
# tests/install.bats — BATS tests for install.sh
#
# Tests the manual hook installer with full temp-HOME isolation so the real
# ~/.claude/settings.json is NEVER touched.
#
# Hard rules honoured:
#   - setup_temp_home / teardown_temp_home isolate every test's $HOME
#   - ATTEST_SETTINGS overrides the settings.json path to the temp fixture
#   - shim_notifications prevents any real GUI side-effects
#   - All assertions use python3 stdlib to verify JSON correctness

load "helpers/setup.bash"

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"

setup() {
  setup_temp_home
  shim_notifications
  # Point install.sh at the temp home's settings file — never the real one.
  export ATTEST_SETTINGS="$HOME/.claude/settings.json"
  mkdir -p "$HOME/.claude"
}

teardown() {
  teardown_temp_home
}

# ── Test 1: fresh install creates settings.json and registers both hooks ──────

@test "fresh install creates settings.json and registers both hooks with async:false" {
  # Ensure no settings.json exists to start
  rm -f "$ATTEST_SETTINGS"

  run bash "$INSTALL_SH"
  [ "$status" -eq 0 ]

  # File must now exist
  [ -f "$ATTEST_SETTINGS" ]

  # Verify JSON structure via python3 (no jq dependency)
  python3 -c "
import json, os
path = os.environ['ATTEST_SETTINGS']
data = json.load(open(path))
hooks = data.get('hooks', {})
for event in ('SubagentStart', 'SubagentStop'):
    arr = hooks.get(event, [])
    assert len(arr) == 1, f'{event}: expected 1 entry, got {len(arr)}'
    inner = arr[0]['hooks'][0]
    assert inner.get('async') == False, f'{event}: async must be False, got {inner.get(\"async\")}'
    assert inner.get('timeout') == 30, f'{event}: timeout must be 30'
    assert inner.get('type') == 'command', f'{event}: type must be command'
    assert 'attest-subagent' in inner.get('command', ''), f'{event}: command missing shim path'
print('assertions passed')
"
}

# ── Test 2: idempotent — running twice produces exactly one entry per event ───

@test "install is idempotent — running twice produces no duplicates" {
  run bash "$INSTALL_SH"
  [ "$status" -eq 0 ]

  run bash "$INSTALL_SH"
  [ "$status" -eq 0 ]

  python3 -c "
import json, os
path = os.environ['ATTEST_SETTINGS']
data = json.load(open(path))
hooks = data.get('hooks', {})
for event in ('SubagentStart', 'SubagentStop'):
    arr = hooks.get(event, [])
    assert len(arr) == 1, f'{event}: expected 1 entry after 2 installs, got {len(arr)}'
print('assertions passed')
"
}

# ── Test 3: pre-existing unrelated SubagentStop hook is preserved ─────────────

@test "install preserves a pre-existing unrelated SubagentStop hook" {
  # Pre-populate settings.json with an unrelated SubagentStop entry
  python3 -c "
import json, os
path = os.environ['ATTEST_SETTINGS']
data = {
  'hooks': {
    'SubagentStop': [
      {
        'hooks': [
          {
            'type': 'command',
            'command': 'bash /some/other/tool/hook.sh',
            'async': False,
            'timeout': 10
          }
        ]
      }
    ]
  }
}
with open(path, 'w') as fh:
    json.dump(data, fh, indent=2)
"

  run bash "$INSTALL_SH"
  [ "$status" -eq 0 ]

  python3 -c "
import json, os
path = os.environ['ATTEST_SETTINGS']
data = json.load(open(path))
hooks = data.get('hooks', {})
arr = hooks.get('SubagentStop', [])
cmds = [h.get('command', '') for e in arr for h in e.get('hooks', [])]
assert any('/some/other/tool/hook.sh' in c for c in cmds), \
    f'unrelated hook was removed! commands: {cmds}'
assert any('attest-subagent-stop.sh' in c for c in cmds), \
    f'attest stop hook missing! commands: {cmds}'
assert len(arr) == 2, f'expected 2 SubagentStop entries, got {len(arr)}'
print('assertions passed')
"
}

# ── Test 4: --uninstall removes only Attest entries, leaves unrelated ones ────

@test "uninstall removes only Attest entries and leaves the unrelated hook intact" {
  # Start with an unrelated SubagentStop hook
  python3 -c "
import json, os
path = os.environ['ATTEST_SETTINGS']
data = {
  'hooks': {
    'SubagentStop': [
      {
        'hooks': [
          {
            'type': 'command',
            'command': 'bash /some/other/tool/hook.sh',
            'async': False,
            'timeout': 10
          }
        ]
      }
    ]
  }
}
with open(path, 'w') as fh:
    json.dump(data, fh, indent=2)
"

  # Install Attest hooks (now we have 2 SubagentStop entries + 1 SubagentStart)
  run bash "$INSTALL_SH"
  [ "$status" -eq 0 ]

  # Uninstall
  run bash "$INSTALL_SH" --uninstall
  [ "$status" -eq 0 ]

  python3 -c "
import json, os
path = os.environ['ATTEST_SETTINGS']
data = json.load(open(path))
hooks = data.get('hooks', {})

# SubagentStop: unrelated entry must remain; Attest entry must be gone
stop_arr = hooks.get('SubagentStop', [])
stop_cmds = [h.get('command', '') for e in stop_arr for h in e.get('hooks', [])]
assert any('/some/other/tool/hook.sh' in c for c in stop_cmds), \
    f'unrelated hook was removed! commands: {stop_cmds}'
assert not any('attest-subagent-stop.sh' in c for c in stop_cmds), \
    f'attest stop hook still present! commands: {stop_cmds}'

# SubagentStart: Attest entry must be gone (was the only entry)
start_arr = hooks.get('SubagentStart', [])
start_cmds = [h.get('command', '') for e in start_arr for h in e.get('hooks', [])]
assert not any('attest-subagent-start.sh' in c for c in start_cmds), \
    f'attest start hook still present! commands: {start_cmds}'

print('assertions passed')
"
}

# ── Test 5: missing settings.json is created with valid JSON ──────────────────

@test "install on missing settings.json creates it with parseable JSON" {
  rm -f "$ATTEST_SETTINGS"

  run bash "$INSTALL_SH"
  [ "$status" -eq 0 ]

  [ -f "$ATTEST_SETTINGS" ]

  # File must be valid JSON with both hook arrays present
  python3 -c "
import json, os
path = os.environ['ATTEST_SETTINGS']
data = json.load(open(path))
assert isinstance(data, dict), 'settings.json root must be a JSON object'
hooks = data.get('hooks', {})
assert 'SubagentStart' in hooks, 'SubagentStart key missing'
assert 'SubagentStop' in hooks, 'SubagentStop key missing'
print('assertions passed')
"
}
