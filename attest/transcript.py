#!/usr/bin/env python3
"""
transcript.py — Extract the final assistant text from a Claude Code JSONL transcript.

Public API:
  last_assistant_text(transcript_path: str) -> str

Confirmed transcript JSONL structure (Phase 0 verified 2026-06-20):
  Each line is a JSON object with:
    "type"    : "user" | "assistant" | "attachment" | ...
    "message" : { "role": "user"|"assistant", "content": [ {type, text|...} ] }
    "agentId" : str  (camelCase)
    "sessionId": str
    "cwd"     : str
    "gitBranch": str
    "uuid"    : str
    "parentUuid": str | null
    "isSidechain": bool

  content block types found in real transcripts:
    "text"       — the actual text content (has "text" key)
    "thinking"   — internal reasoning (drop)
    "tool_use"   — tool invocation (drop)
    "tool_result"— tool output (drop)

  Strategy: scan ALL lines, track the last line with type=="assistant" and
  message.role=="assistant", then concat all content[].type=="text" blocks.
  Unknown content block types are silently skipped (future-proofing).
"""
import json
import os


def last_assistant_text(transcript_path: str) -> str:
    """Extract the final assistant message text from a JSONL transcript file.

    Returns:
        Concatenated text from all ``content[].type=="text"`` blocks in the
        last assistant turn, with double newlines between blocks.
        Returns "" if the file is missing, empty, unreadable, or has no
        assistant turns.

    Tolerates:
        - Missing or inaccessible file.
        - Malformed JSON lines (skipped individually).
        - Unknown content block types (skipped silently).
    """
    if not transcript_path:
        return ''

    expanded = os.path.expanduser(transcript_path)

    try:
        with open(expanded, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
    except (FileNotFoundError, PermissionError, OSError):
        return ''

    # Iterate in reverse so the first assistant turn we encounter is the last
    # one in the file.  We break (via return) as soon as we find a turn that
    # produces text blocks, avoiding a full forward scan of the whole transcript.
    for raw_line in reversed(lines):
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if not isinstance(obj, dict):
            continue

        # Only process "assistant" typed lines
        if obj.get('type') != 'assistant':
            continue

        message = obj.get('message')
        if not isinstance(message, dict):
            continue

        if message.get('role') != 'assistant':
            continue

        content = message.get('content')
        if not isinstance(content, list):
            continue

        # Extract only "text" blocks; drop thinking/tool_use/tool_result/etc.
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text':
                text = block.get('text', '')
                if text:
                    texts.append(text)

        if texts:
            # This is the last assistant turn that produced text — return immediately.
            return '\n\n'.join(texts)
        # Assistant turn found but no text blocks (e.g. thinking-only) — keep
        # scanning backwards to find the last turn that does have text.

    return ''
