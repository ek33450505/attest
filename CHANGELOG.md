# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- GitHub Actions CI running the full 290-test suite (270 Python + 20 BATS) on every push and pull request, plus a CI status badge in the README.
- Project docs: CONTRIBUTING.md, SECURITY.md, CHANGELOG.md, and GitHub issue/PR templates.

### Fixed

- Corrected a stale comment in `hooks/attest-subagent-stop.sh` that wrongly described enforce-mode blocking as "exit 2". Blocks are delivered via a stdout JSON `{"decision":"block"}` payload with exit code 0; the hook always exits 0 (fail-open).

## [0.1.0] - 2026-06-21

### Added

- Initial public release. SubagentStart/SubagentStop hooks that verify a subagent's "Status: DONE" / "## Handoff" claim against the real git working-tree delta.
- Detect mode (default, print-only) and opt-in enforce mode (`ATTEST_ENFORCE=1`) that blocks a proven false DONE via stdout JSON, with a loop-safety retry cap.
- Conservative claim parser (prose never becomes a claimed file). Stdlib-only Python, zero network, fail-open on every doubt.
- Claude Code plugin (`.claude-plugin/`) and Homebrew formula (`Formula/attest.rb`).
- 290 tests (270 Python unittest + 20 BATS) validated against real Claude Code v2.1.170; full evidence dossier in `docs/VALIDATION.md`.

[Unreleased]: https://github.com/ek33450505/attest/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ek33450505/attest/releases/tag/v0.1.0
