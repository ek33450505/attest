# fixtures/

Test fixtures for the Attest hook system.

## Real captured payloads (P1 ship-gate, 2026-06-21)

These are **sanitized real** SubagentStart/SubagentStop payloads captured from live
Claude Code **v2.1.170** during the P1 live-capture ship-gate. Capture method: an
isolated headless run — a scratch git repo with project-scoped attest hooks, driven by
`claude -p "<dispatch a subagent>" --setting-sources project --permission-mode
bypassPermissions` (with `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0`). This left the real
`~/.claude/settings.json` untouched and kept other hooks from firing.

Sanitization: the local username and the real `$TMPDIR` repo path were rewritten
(`/tmp/attest-test-repo`, `/Users/dev`); opaque run ids (`agent_id`, `session_id`,
`uuid`s) are kept verbatim — they are ephemeral, not secret.

### Real SubagentStop schema (ground truth)
Fields present in a real SubagentStop payload:
`session_id`, `transcript_path` (the **parent** session jsonl), `agent_transcript_path`
(the **subagent's own** jsonl at `.../subagents/agent-<agent_id>.jsonl`), `cwd`,
`permission_mode`, `agent_id`, `agent_type`, `effort.level`, `hook_event_name`,
`stop_hook_active`, `last_assistant_message` (the subagent's final text = the claim),
`background_tasks`, `session_crons`.

Confirmed facts (replacing prior synthetic assumptions):
- **`agent_id`** is present for plain `general-purpose` Task subagents and is identical
  across SubagentStart → SubagentStop.
- **`stop_hook_active`** is real: `false` on the first stop, **`true`** on the post-block
  re-fire (the loop-safety guard the docs left unconfirmed).
- The claim text rides in **`last_assistant_message`** (the payload fast-path); no
  separate transcript read is needed in the common case.
- There is **no `stop_reason`/`exit_reason`** on a normal-completion stop (the earlier
  synthetic fixture invented `stop_reason: "end_turn"`).
- A SubagentStart payload carries only `session_id`/`transcript_path`/`cwd`/`agent_id`/
  `agent_type`/`hook_event_name` (no `agent_transcript_path`, no `stop_hook_active`).

### Files

| File | What it is |
|------|------------|
| `subagent_start_payload.json` | Real SubagentStart (the snapshot boundary). |
| `subagent_stop_payload.json` | Real SubagentStop — **true DONE**: clean `## Handoff`, `hello.py` actually created. |
| `subagent_stop_false_done.json` | Real SubagentStop — an **honest** subagent that created nothing and explained why in prose. Regression fixture for the parser false-positive: the parser MUST extract `files_changed=[]` from this. |
| `subagent_stop_refire.json` | Real SubagentStop — the **post-block re-fire** of the same `agent_id`: `stop_hook_active=true`, claim corrected to `status: BLOCKED` / `files: none`. Loop-safety fixture. |

`tests/test_real_fixtures.py` pins the parser + hook normalization against these.

### `transcript_sample.jsonl`
Representative JSONL transcript. Its **structure** was verified against real subagent
transcripts (`type`/`message{role,content[]}`/`agentId`/`sessionId`/`cwd`/`gitBranch`/
`uuid`/`parentUuid`/`isSidechain`; block types `text`/`thinking`/`tool_use`/`tool_result`),
but the **content** is synthetic (disposable paths, placeholder ids). The payload
`last_assistant_message` fast-path means this transcript path is only a fallback.

## `captured/` directory
Created automatically when `ATTEST_CAPTURE=1` is set and the hooks are installed.
Populated with both the **normalized** payload and (since BUG-3) a verbatim **raw**
`{event}-raw-*.json` so raw field names can be verified. Set `ATTEST_CAPTURE_DIR` to
redirect writes (used by tests). **Do NOT commit files from `captured/`** — they may
contain session content. The `captured/` subdirectory is in `.gitignore`.
