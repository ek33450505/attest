# Security Policy

Attest is a small, single-maintainer, open-source tool. This policy is
deliberately proportionate to that reality: honest about scope, and honest
about response capacity.

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

Security fixes land on the `0.1.x` line. Older or unreleased revisions are not
maintained.

## Security Posture

Attest is a **local, read-only** Claude Code hook. By design:

- It makes **no network calls** — zero outbound connections, no telemetry.
- It **never executes agent-supplied code**. The subagent's final message is
  *parsed* (a `## Handoff` block, then an anchored `Status:` / `Files changed:`
  fallback), never evaluated, sourced, or shelled out.
- It only **reads** your repository — computing `sha256` of files in the git
  working tree that differ from `HEAD`. The only thing it **writes** is its own
  local SQLite state database (`~/.attest/state.db` by default), where it records
  working-tree snapshots and block counts. It has **no write access to your
  source**.
- It is **fail-open**: on any doubt — a parse error, a missing file, a slow or
  broken run — the hook exits 0 and lets the parent session continue. It blocks
  only on proof, and only in opt-in enforce mode (`ATTEST_ENFORCE=1`).

This narrow surface is the security story: there is little Attest can do *to*
your machine because it does so little in the first place.

## Reporting a Vulnerability

Please report security issues **privately**:

- Use **GitHub's private vulnerability reporting** (Security Advisories) on the
  repository: <https://github.com/ek33450505/attest/security/advisories/new>.
- **Do not open a public issue** for a sensitive report.

This is maintained by one person, so response is **best-effort**. You can expect
acknowledgement and a fix on a timeline that a single maintainer can realistically
sustain. Please include enough detail to reproduce — affected version, OS, and the
exact conditions that trigger the issue.

## In Scope

Vulnerabilities in any of the shipped components are in scope:

- the hook shims (`hooks/attest-subagent-start.sh`, `hooks/attest-subagent-stop.sh`),
- the Python package (`attest/`) and the `bin/attest` CLI,
- `install.sh`,
- the Claude Code plugin manifest (`.claude-plugin/`), and
- the Homebrew formula (`Formula/attest.rb`).

Examples of genuinely in-scope issues:

- a **path-traversal** or **injection** that lets crafted input escape the
  intended read-only file scope,
- any way to make a hook **execute arbitrary code** (e.g. parsed subagent output
  reaching a shell, `eval`, or import),
- a **sha256 mismatch** between the Homebrew formula and the published release
  tarball.

## Out of Scope

- Anything that **requires an already-compromised local machine** (an attacker
  with existing write access to your repo, `$HOME`, `~/.attest/`, the installed
  files, or your shell). Attest trusts the local environment it runs in; if that
  environment is already controlled by an attacker, Attest is not the relevant
  defense.
