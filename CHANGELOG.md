# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-23

### Security

- Capture mode (`ATTEST_CAPTURE=1`, opt-in) now sanitizes `agent_id` before using it in capture filenames and validates that `transcript_path` resolves under `$HOME` before copying it, closing a path-traversal write and an arbitrary-file-copy vector. Both remain best-effort and fail-open.
- `_sha256_file` is now symlink-safe: it uses `lstat` + `O_NOFOLLOW` and fingerprints symlinks by metadata instead of following them, removing a TOCTOU / FIFO-stall risk on the synchronous hook.
- Large-file fingerprints (over `ATTEST_MAX_HASH_BYTES`) now include a first+last-4 KB partial content hash, so a content edit that preserves size and mtime is still detected.
- Documented accepted local-trust limitations (absolute-path block suppression, env-var-driven paths, residual symlink TOCTOU) in `docs/LIMITATIONS.md`.

### Changed

- Hot-path performance on the synchronous `SubagentStop` hook: the sqlite schema / `PRAGMA` / permission setup now runs once per process instead of on every state operation; the `cast.db` mirror DDL is likewise guarded; `delta()` reuses the repo root resolved by `snapshot()` to drop a redundant `git rev-parse` (2 git subprocesses per stop instead of 3); and the transcript reader scans from the end for the last assistant turn instead of reading the whole JSONL forward.

### Fixed

- The claim parser no longer treats a `Status:` / `files_changed:` line inside a fenced code block (` ``` ` or `~~~`) as a real claim — preventing a false block in enforce mode when an agent's final message documents an example Status block.
- `parse_payload` never raises on non-string input (returns safe defaults), and `stop_hook_active` is parsed strictly as a JSON boolean, so a string `"false"` no longer suppresses a block.
- `_build_reason` no longer reports "Claim matches observed delta" when a claim without a `Status:` line lists files absent from the delta (corrected detect-mode / observability output).
- A negative `ATTEST_MAX_HASH_BYTES` no longer silently degrades every file to metadata fingerprinting (it is clamped to the default).
- The start hook shim now logs Python stderr to the error log instead of discarding it to `/dev/null`, and both shims create the log directory before redirecting stderr to it (repairs a fresh-system regression where the hook could not run).
- Removed dead `_print_report` (a latent enforce-mode stdout-contract risk) and an unreachable `except NotAGitRepo` branch in the CLI.

### Tests

- Pinned previously-untested safety branches: the suppressed-detection observability log, `DONE_WITH_CONCERNS` + enforce end-to-end, the counter-persist fail-open valve, the `parse_payload` never-raises contract, the negative-env clamp, and a shim missing-log-dir regression. Suite is now 325 tests (304 Python + 21 BATS).

## [0.1.1] - 2026-06-22

### Security

- `state.db` and its parent directory are no longer world-readable. `_open_db()` now creates the directory `0700` and the database file `0600` (best-effort, fail-open). Previously both inherited the umask (typically `0644`), exposing session IDs, repo paths, and agent keys to other local users on shared machines.

### Added

- GitHub Actions CI now runs a Python **3.9–3.13** matrix plus a ShellCheck job on every push and pull request, alongside the existing test suite and CI badge.
- `make test` Makefile runner (`test` / `test-py` / `test-bats`).
- Project docs: CONTRIBUTING.md, SECURITY.md, CHANGELOG.md, and GitHub issue/PR templates.
- README "What the report looks like" sample-output block, and a Windows/WSL note in `docs/INSTALL.md`.
- Documented minimum supported Python version (3.9+).

### Changed

- Large files (over `ATTEST_MAX_HASH_BYTES`, default 10 MB) are now fingerprinted by metadata (`size` + `mtime`) instead of a full content hash, so the synchronous `SubagentStop` hook is never stalled by a large uncommitted binary. Change detection is preserved (any write changes size or mtime) with no false OK / false block.
- Hook-shim header comments now defer to the canonical environment-variable table; removed the stale "Phase 1b" label.

### Fixed

- Guarded the `ATTEST_MAX_HASH_BYTES` parse so a malformed override falls back to the default instead of raising `ValueError` on the hot path (fail-open).
- Corrected a stale comment in `hooks/attest-subagent-stop.sh` that wrongly described enforce-mode blocking as "exit 2". Blocks are delivered via a stdout JSON `{"decision":"block"}` payload with exit code 0; the hook always exits 0 (fail-open).
- Silenced a ShellCheck SC2317 false positive on the trap-invoked cleanup handler in `scripts/live-capture-test.sh`.

## [0.1.0] - 2026-06-21

### Added

- Initial public release. SubagentStart/SubagentStop hooks that verify a subagent's "Status: DONE" / "## Handoff" claim against the real git working-tree delta.
- Detect mode (default, print-only) and opt-in enforce mode (`ATTEST_ENFORCE=1`) that blocks a proven false DONE via stdout JSON, with a loop-safety retry cap.
- Conservative claim parser (prose never becomes a claimed file). Stdlib-only Python, zero network, fail-open on every doubt.
- Claude Code plugin (`.claude-plugin/`) and Homebrew formula (`Formula/attest.rb`).
- 290 tests (270 Python unittest + 20 BATS) validated against real Claude Code v2.1.170; full evidence dossier in `docs/VALIDATION.md`.

[Unreleased]: https://github.com/ek33450505/attest/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ek33450505/attest/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/ek33450505/attest/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ek33450505/attest/releases/tag/v0.1.0
