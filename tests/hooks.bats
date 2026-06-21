#!/usr/bin/env bats
# tests/hooks.bats — End-to-end BATS tests for Attest hook shims
#
# Tests the bash shims (hooks/attest-subagent-start.sh + hooks/attest-subagent-stop.sh)
# end-to-end, with:
#   - temp-HOME isolation (setup_temp_home / teardown_temp_home)
#   - disposable git repo fixtures (setup_git_repo)
#   - notification shims per the HARD RULE (shim_notifications)
#
# Hard rules:
#   - All destructive ops are within $BATS_TEST_TMPDIR or $HOME (isolated temp)
#   - ATTEST_STATE_DB is always set to a temp path
#   - Exit 0 is always asserted (Phase 1b: detect-and-print, never blocking)
#   - Notification shims are wired in setup()

load "helpers/setup.bash"

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
START_HOOK="$REPO_ROOT/hooks/attest-subagent-start.sh"
STOP_HOOK="$REPO_ROOT/hooks/attest-subagent-stop.sh"

setup() {
  setup_temp_home
  shim_notifications

  # Use $HOME (isolated temp from setup_temp_home) for all test artifacts
  export TEST_WORK_DIR="$HOME/work"
  mkdir -p "$TEST_WORK_DIR"

  # Isolated state DB in temp dir
  export ATTEST_STATE_DB="$TEST_WORK_DIR/attest-state.db"

  # Ensure the attest package is importable from the repo root
  export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"

  # Create a disposable git repo for tests
  export TEST_REPO="$TEST_WORK_DIR/git-repo"
  setup_git_repo "$TEST_REPO"
}

teardown() {
  teardown_temp_home
}

# ── Helpers ────────────────────────────────────────────────────────────────────

make_start_payload() {
  local agent_id="${1:-test-agent-001}"
  local session_id="${2:-sess-bats-001}"
  local cwd="${3:-$TEST_REPO}"
  printf '{"agent_type":"code-writer","agent_id":"%s","session_id":"%s","cwd":"%s","stop_reason":"","transcript_path":"","stop_hook_active":false}' \
    "$agent_id" "$session_id" "$cwd"
}

make_stop_payload() {
  local agent_id="${1:-test-agent-001}"
  local session_id="${2:-sess-bats-001}"
  local cwd="${3:-$TEST_REPO}"
  local claim="${4:-}"
  printf '{"agent_type":"code-writer","agent_id":"%s","session_id":"%s","cwd":"%s","stop_reason":"end_turn","transcript_path":"","stop_hook_active":false,"last_assistant_message":"%s"}' \
    "$agent_id" "$session_id" "$cwd" "$claim"
}

# ── Tests ──────────────────────────────────────────────────────────────────────

@test "start hook exits 0 with valid payload" {
  local payload
  payload="$(make_start_payload)"
  run bash "$START_HOOK" <<< "$payload"
  [ "$status" -eq 0 ]
}

@test "stop hook exits 0 with no prior snapshot" {
  local payload
  payload="$(make_stop_payload)"
  run bash "$STOP_HOOK" <<< "$payload"
  [ "$status" -eq 0 ]
}

@test "stop hook exits 0 with empty stdin" {
  run bash "$STOP_HOOK" <<< ""
  [ "$status" -eq 0 ]
}

@test "start hook exits 0 with empty stdin" {
  run bash "$START_HOOK" <<< ""
  [ "$status" -eq 0 ]
}

@test "stop hook exits 0 with invalid JSON" {
  run bash "$STOP_HOOK" <<< "{not valid json}"
  [ "$status" -eq 0 ]
}

@test "start-then-stop with truthful claim prints OK and exits 0" {
  local agent_id="bats-truthful-001"
  local session_id="sess-bats-truth"
  local start_payload
  start_payload="$(make_start_payload "$agent_id" "$session_id" "$TEST_REPO")"

  # Run start hook
  run bash "$START_HOOK" <<< "$start_payload"
  [ "$status" -eq 0 ]

  # "Agent" writes a real file into the repo
  mkdir -p "$TEST_REPO/src"
  echo "def feature(): pass" > "$TEST_REPO/src/feature.py"

  # Stop with truthful claim: claims src/feature.py (which was written)
  local claim
  claim="$(printf '## Handoff\\nfiles_changed: src/feature.py\\nstatus: DONE\\nblockers: none\\n')"
  local stop_payload
  stop_payload="$(printf '{"agent_type":"code-writer","agent_id":"%s","session_id":"%s","cwd":"%s","stop_reason":"end_turn","transcript_path":"","stop_hook_active":false,"last_assistant_message":"%s"}' \
    "$agent_id" "$session_id" "$TEST_REPO" \
    "$(printf '## Handoff\nfiles_changed: src/feature.py\nstatus: DONE\nblockers: none\n' | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read())[1:-1])')" \
  )"

  run bash "$STOP_HOOK" <<< "$stop_payload"
  [ "$status" -eq 0 ]
  [[ "$output" == *"OK"* ]] || [[ "$output" == *"start"* ]]
}

@test "start-then-stop with FALSE claim prints MISMATCH and still exits 0" {
  local agent_id="bats-false-001"
  local session_id="sess-bats-false"
  local start_payload
  start_payload="$(make_start_payload "$agent_id" "$session_id" "$TEST_REPO")"

  # Run start hook to snapshot
  run bash "$START_HOOK" <<< "$start_payload"
  [ "$status" -eq 0 ]

  # Agent claims ghost.py but does NOT write it
  local stop_payload
  stop_payload="$(printf '{"agent_type":"code-writer","agent_id":"%s","session_id":"%s","cwd":"%s","stop_reason":"end_turn","transcript_path":"","stop_hook_active":false,"last_assistant_message":"%s"}' \
    "$agent_id" "$session_id" "$TEST_REPO" \
    "$(printf '## Handoff\nfiles_changed: src/ghost.py\nstatus: DONE\nblockers: none\n' | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read())[1:-1])')" \
  )"

  run bash "$STOP_HOOK" <<< "$stop_payload"
  [ "$status" -eq 0 ]
  # MISMATCH must be printed
  [[ "$output" == *"MISMATCH"* ]]
}

@test "stop hook exits 0 when snapshot missing (start never ran)" {
  local payload
  payload="$(make_stop_payload "agent-nostart-bats" "sess-nostart" "$TEST_REPO")"
  run bash "$STOP_HOOK" <<< "$payload"
  [ "$status" -eq 0 ]
  [[ "$output" == *"no snapshot found"* ]] || [[ "$output" == *"skipping"* ]]
}

@test "stop hook exits 0 for non-git directory" {
  local non_git="$TEST_WORK_DIR/not-a-repo"
  mkdir -p "$non_git"

  local start_payload
  start_payload="$(make_start_payload "bats-nongit" "sess-nongit" "$non_git")"
  run bash "$START_HOOK" <<< "$start_payload"
  [ "$status" -eq 0 ]

  local stop_payload
  stop_payload="$(make_stop_payload "bats-nongit" "sess-nongit" "$non_git")"
  run bash "$STOP_HOOK" <<< "$stop_payload"
  [ "$status" -eq 0 ]
}

@test "stop hook with claim source=none never prints MISMATCH" {
  local agent_id="bats-noclaim"
  local session_id="sess-noclaim"

  # Start snapshot
  local start_payload
  start_payload="$(make_start_payload "$agent_id" "$session_id" "$TEST_REPO")"
  run bash "$START_HOOK" <<< "$start_payload"
  [ "$status" -eq 0 ]

  # Stop with completely empty claim text
  local stop_payload
  stop_payload="$(printf '{"agent_type":"code-writer","agent_id":"%s","session_id":"%s","cwd":"%s","stop_reason":"end_turn","transcript_path":"","stop_hook_active":false}' \
    "$agent_id" "$session_id" "$TEST_REPO")"

  run bash "$STOP_HOOK" <<< "$stop_payload"
  [ "$status" -eq 0 ]
  [[ "$output" != *"MISMATCH"* ]]
}

@test "hooks use transcript_path when payload_text absent" {
  local agent_id="bats-transcript"
  local session_id="sess-transcript"
  local fixture_transcript="$REPO_ROOT/fixtures/transcript_sample.jsonl"

  # Only run if fixture exists
  [ -f "$fixture_transcript" ] || skip "fixture transcript not found"

  # Start snapshot
  local start_payload
  start_payload="$(make_start_payload "$agent_id" "$session_id" "$TEST_REPO")"
  run bash "$START_HOOK" <<< "$start_payload"
  [ "$status" -eq 0 ]

  # The fixture claims src/feature.py and tests/test_feature.py but they don't exist
  # in the repo — so this should produce a MISMATCH
  local stop_payload
  stop_payload="$(printf '{"agent_type":"code-writer","agent_id":"%s","session_id":"%s","cwd":"%s","stop_reason":"end_turn","transcript_path":"%s","stop_hook_active":false}' \
    "$agent_id" "$session_id" "$TEST_REPO" "$fixture_transcript")"

  run bash "$STOP_HOOK" <<< "$stop_payload"
  [ "$status" -eq 0 ]
  # Either MISMATCH (files not in repo) or OK/SCOPE_CREEP — either way exit 0
}
