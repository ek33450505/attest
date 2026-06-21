# fixtures/

Test fixtures for the Attest hook system.

## Files

### `subagent_stop_payload.json`
Synthetic SubagentStop stdin payload matching the confirmed Phase 0 schema
(verified 2026-06-20 against live `cast-subagent-stop-hook.sh` parsing code).

**Confirmed-real fields** (verified against `cast-subagent-stop-hook.sh` lines ~104-145):
- `agent_type` — primary identity field (Claude Code sends this, NOT `agent_name`)
- `agent_id` — unique invocation ID (present on managed-agent dispatches)
- `session_id` — parent session identifier
- `stop_reason` — `"end_turn"` | `"max_turns"` | `"error"`
- `agent_response.content[].type` — `"text"` blocks contain the agent's final output
- `last_assistant_message`, `output`, `body` — flat fallback fields for older dispatch paths

**Synthetic/unconfirmed fields** (schema reasonable but not captured live):
- `transcript_path` — expected per docs but not yet confirmed in real payload
- `stop_hook_active` — documented as guard against blocking loops
- `cwd` — confirmed in transcript JSONL lines but not confirmed in stop payload
- `duration_ms`, `tool_use_count` — observed in CAST enrichment, not raw Claude Code payload

### `transcript_sample.jsonl`
Minimal representative JSONL transcript constructed from the confirmed real shape
(structure verified by reading real transcripts at
`~/.claude/projects/-Users-edkubiak-Projects-personal/da27b414-f9f1-4c91-bd50-1a6096555066/subagents/`).

**Confirmed-real structure** (verified 2026-06-20 against `agent-ad53bc683c2bae23a.jsonl`):
- Each line is a JSON object
- Top-level keys: `parentUuid`, `isSidechain`, `agentId` (camelCase), `type`, `message`,
  `uuid`, `timestamp`, `cwd`, `sessionId`, `gitBranch`, `slug`, `requestId`, `attributionAgent`
- `type` values seen: `"user"`, `"assistant"`, `"attachment"`
- `message.role`: `"user"` or `"assistant"`
- `message.content[]` block types seen: `"text"`, `"thinking"`, `"tool_use"`, `"tool_result"`
- Final assistant message (stop_reason=end_turn): `content = [{"type": "text", "text": "..."}]`

**Synthetic content** (representative shape, not copied verbatim from real sessions):
- File paths are `/tmp/attest-test-repo/...` (disposable)
- Agent IDs, session IDs, UUIDs are synthetic
- Thinking block signatures are placeholder strings
- The `## Handoff` block in the final message mirrors the CAST convention

**Pending** (live capture still needed):
- A real `SubagentStop` raw stdin payload has NOT yet been captured. Set
  `ATTEST_CAPTURE=1` and install the hooks to get a live capture into
  `fixtures/captured/` on the next real subagent dispatch.

## `captured/` directory

Created automatically when `ATTEST_CAPTURE=1` is set and the hooks are installed.
Populated with real payloads and transcripts from live Claude Code sessions.
**Do NOT commit files from `captured/` — they may contain sensitive session content.**
The `captured/` subdirectory is in `.gitignore`.
