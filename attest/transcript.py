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

    last_text: str = ''

    for raw_line in lines:
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
            # Update last_text; we keep scanning to find the very last assistant turn
            last_text = '\n\n'.join(texts)

    return last_text
