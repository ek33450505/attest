#!/usr/bin/env python3
"""
hookio.py — Normalize Claude Code hook stdin payloads.

Public API:
  parse_payload(raw: str) -> dict

Returns a normalized dict:
  {
    "agent_id":              str,   # from agent_id or subagent_id; "" when absent
    "agent_type":            str,   # from agent_type (primary) or legacy name fields
    "session_id":            str,
    "stop_reason":           str,
    "transcript_path":       str,   # from transcript_path or agent_transcript_path (back-compat); "" when absent
    "agent_transcript_path": str,   # from agent_transcript_path; "" when absent
    "cwd":                   str,
    "stop_hook_active":      bool,
    "payload_text":          str,   # best effort text from the payload body
  }

Field precedence (mirrors cast-subagent-stop-hook.sh lines ~104-145):
  agent_id   : payload["agent_id"] > payload["subagent_id"]
  agent_type : payload["agent_type"] > payload["agent_name"] > payload["subagent_name"] > "unknown"
  transcript_path : payload["transcript_path"] > payload["agent_transcript_path"] (back-compat fallback)
  agent_transcript_path : payload["agent_transcript_path"] (the subagent's own jsonl; prefer for claim extraction)
  payload_text : agent_response.content[].type=="text" joined >
                 last_assistant_message > output > body > ""

On any JSON parse failure the function returns a dict with all fields set
to safe defaults rather than raising.
"""
import json
from typing import Any


def _extract_agent_response_text(data: dict) -> str:
    """Extract text from agent_response.content[].type=="text" blocks.

    Returns joined text or "" on any failure.
    """
    try:
        agent_response = data.get('agent_response') or {}
        if not isinstance(agent_response, dict):
            return ''
        content_blocks = agent_response.get('content') or []
        if not isinstance(content_blocks, list):
            return ''
        texts = [
            block.get('text', '')
            for block in content_blocks
            if isinstance(block, dict) and block.get('type') == 'text'
        ]
        return '\n'.join(t for t in texts if t)
    except Exception:  # noqa: BLE001
        return ''


def parse_payload(raw: str) -> dict:
    """Parse and normalize a SubagentStart/SubagentStop stdin JSON payload.

    Args:
        raw: raw stdin string (JSON).

    Returns:
        Normalized dict with stable keys regardless of Claude Code version.
        Never raises; returns safe defaults on parse error.
    """
    _empty: dict[str, Any] = {
        'agent_id': '',
        'agent_type': 'unknown',
        'session_id': '',
        'stop_reason': '',
        'transcript_path': '',
        'agent_transcript_path': '',
        'cwd': '',
        'stop_hook_active': False,
        'payload_text': '',
    }

    if not raw or not raw.strip():
        return _empty.copy()

    try:
        data: dict = json.loads(raw)
    except json.JSONDecodeError:
        return _empty.copy()

    if not isinstance(data, dict):
        return _empty.copy()

    # agent_id: prefer agent_id, then subagent_id
    agent_id: str = (
        data.get('agent_id') or
        data.get('subagent_id') or
        ''
    )

    # agent_type: Claude Code sends agent_type (not agent_name per source comments).
    # Legacy fallbacks for older/enriched payloads.
    agent_type: str = (
        data.get('agent_type') or
        data.get('agent_name') or
        data.get('subagent_name') or
        'unknown'
    )

    session_id: str = data.get('session_id') or ''
    stop_reason: str = data.get('stop_reason') or ''

    # transcript_path: back-compat key — prefers the parent session transcript_path, but
    # falls back to agent_transcript_path so older call-sites that only check transcript_path
    # continue to get a non-empty value even on the real payload format (BUG-2 back-compat).
    transcript_path: str = (
        data.get('transcript_path') or
        data.get('agent_transcript_path') or
        ''
    )

    # agent_transcript_path: the subagent's own jsonl. For SubagentStop claim extraction
    # this is the CORRECT transcript to read (not the parent session transcript_path).
    agent_transcript_path: str = data.get('agent_transcript_path') or ''

    cwd: str = data.get('cwd') or ''

    # stop_hook_active: guard against blocking loops (Phase 2+); detect early
    stop_hook_active_raw = data.get('stop_hook_active')
    stop_hook_active: bool = bool(stop_hook_active_raw) if stop_hook_active_raw is not None else False

    # payload_text: best-effort extraction of the agent's output text
    # Priority: agent_response.content[].text > last_assistant_message > output > body
    payload_text: str = _extract_agent_response_text(data)
    if not payload_text:
        payload_text = (
            data.get('last_assistant_message') or
            data.get('output') or
            data.get('body') or
            ''
        )

    return {
        'agent_id': str(agent_id),
        'agent_type': str(agent_type),
        'session_id': str(session_id),
        'stop_reason': str(stop_reason),
        'transcript_path': str(transcript_path),
        'agent_transcript_path': str(agent_transcript_path),
        'cwd': str(cwd),
        'stop_hook_active': stop_hook_active,
        'payload_text': str(payload_text),
    }
