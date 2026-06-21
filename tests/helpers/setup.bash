# tests/helpers/setup.bash — Shared BATS setup helpers for Attest tests
#
# Mirrors the sentinel + guard pattern from:
#   ~/Projects/personal/claude-agent-team/tests/helpers/setup.bash
#
# HARD RULE: Any test touching $HOME MUST use setup_temp_home/teardown_temp_home.
# Tests operating on the real $HOME can destroy the live runtime.

setup_temp_home() {
  export ORIG_HOME="$HOME"
  # Use $TMPDIR for sandbox compatibility (macOS sandbox restricts /tmp and /var/folders)
  local base="${TMPDIR:-/tmp}"
  local tmp_home="${base}/attest-test-home-$$-${RANDOM}"
  mkdir -p "$tmp_home"
  export HOME="$tmp_home"
  # Sentinel: teardown_temp_home will refuse to delete any HOME lacking this marker
  touch "$HOME/.attest-test-home"
  export ATTEST_TEST_HOME="$HOME"
}

teardown_temp_home() {
  local target="$HOME"

  # Guard (a): sentinel marker must exist
  if [[ ! -f "$target/.attest-test-home" ]]; then
    echo "FATAL [teardown_temp_home]: refusing to delete '$target' — not a verified test fixture (missing .attest-test-home)" >&2
    export HOME="$ORIG_HOME"
    return 1
  fi

  # Guard (b): path must begin with a known temp prefix
  # Also allow $TMPDIR subtrees (sandbox may redirect /tmp to /tmp/claude-NNN/...)
  local is_tmp=0
  local tmpdir_base="${TMPDIR:-}"
  case "$target" in
    /tmp/*)                  is_tmp=1 ;;
    /private/tmp/*)          is_tmp=1 ;;
    /var/folders/*)          is_tmp=1 ;;
    /private/var/folders/*)  is_tmp=1 ;;
  esac
  # Also allow if it's under $TMPDIR (sandbox-specific temp path)
  if [[ "$is_tmp" -eq 0 && -n "$tmpdir_base" && "$target" == "$tmpdir_base"* ]]; then
    is_tmp=1
  fi
  if [[ "$is_tmp" -eq 0 ]]; then
    echo "FATAL [teardown_temp_home]: refusing to delete '$target' — not a verified temp dir" >&2
    export HOME="$ORIG_HOME"
    return 1
  fi

  # Guard (c): must not equal the invoking user's real home
  local real_home="${ORIG_HOME:-}"
  if [[ -n "$real_home" && "$target" = "$real_home" ]]; then
    echo "FATAL [teardown_temp_home]: refusing to delete '$target' — matches ORIG_HOME" >&2
    export HOME="$ORIG_HOME"
    return 1
  fi
  if [[ -z "$real_home" ]]; then
    case "$target" in
      /Users/*)
        echo "FATAL [teardown_temp_home]: refusing to delete '$target' — looks like a real home (ORIG_HOME unset)" >&2
        export HOME="$ORIG_HOME"
        return 1
        ;;
    esac
  fi

  rm -rf "$target"
  export HOME="$ORIG_HOME"
}

# setup_git_repo <path>
# Initialise a disposable git repo with one commit at <path>.
setup_git_repo() {
  local path="$1"
  mkdir -p "$path"
  git init "$path" >/dev/null 2>&1
  git -C "$path" config user.email "test@attest.bats"
  git -C "$path" config user.name  "Attest BATS"
  echo "# test" > "$path/README.md"
  git -C "$path" add . >/dev/null 2>&1
  git -C "$path" commit -m "init" >/dev/null 2>&1
}

# shim_notifications
# Install no-op stubs for notification/GUI-surface commands per the HARD RULE
# (tests must never fire real desktop notifications).
shim_notifications() {
  # Use $HOME (isolated temp) for notification shims so they're within sandbox-writable space
  local bin_dir="${HOME}/shims"
  mkdir -p "$bin_dir"

  for cmd in osascript terminal-notifier notify-send open; do
    printf '#!/bin/sh\n# no-op notification shim\nexit 0\n' > "$bin_dir/$cmd"
    chmod +x "$bin_dir/$cmd"
  done

  export PATH="$bin_dir:$PATH"
}
