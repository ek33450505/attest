#!/usr/bin/env python3
"""
claim.py — Two-tier claim parser for agent completion claims.

Tier 1: Parse ## Handoff block (structured, preferred).
        Mirrors cast_handoff_parser._HANDOFF_RE and _parse_kv exactly so CAST
        handoff blocks parse identically here and in the upstream parser.

Tier 2: Natural-language fallback (regex-based, when no Handoff block found).

CRITICAL RULE: a MISSING or unparseable claim returns status=None, source="none".
               It MUST NEVER be treated as a false DONE downstream.

Return value of parse_claim():
  {
    "status":        str | None,       # DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT | None
    "files_changed": list[str],        # normalized file paths (empty list when none)
    "blockers":      str | None,       # blockers text, or None when absent/none
    "ran_tests":     bool,             # True if claim text mentions running tests
    "source":        str,              # "handoff" | "nl" | "none"
    "raw":           str,              # first 500 chars of the parsed source block
  }
"""
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Tier 1 — ## Handoff block (mirrors cast_handoff_parser.py exactly)
# ---------------------------------------------------------------------------

# Regex from cast_handoff_parser._HANDOFF_RE — do not change without syncing upstream.
_HANDOFF_RE = re.compile(r'## Handoff\s*\n([\s\S]+?)(?=\n## |\Z)')

# Status values recognized in structured Handoff blocks (uppercase comparison).
_STATUS_VALUES = frozenset({'DONE', 'DONE_WITH_CONCERNS', 'BLOCKED', 'NEEDS_CONTEXT'})

# ---------------------------------------------------------------------------
# Tier 2 — natural-language patterns
# ---------------------------------------------------------------------------

# "Status: DONE" and variants (case-insensitive).
_NL_STATUS_RE = re.compile(
    r'\bStatus\s*:\s*(DONE_WITH_CONCERNS|DONE|BLOCKED|NEEDS_CONTEXT)\b',
    re.IGNORECASE,
)

# Bare "DONE" token not preceded by "Status:" (lower-priority fallback).
_BARE_DONE_RE = re.compile(r'(?<!Status:\s)(?<!Status: )\bDONE\b')

# Backtick-wrapped tokens: `some/path.py`
_BACKTICK_PATH_RE = re.compile(r'`([^`\n]+)`')

# Verb-then-path: "created foo/bar.py", "modified tests/test_foo.py", etc.
_VERB_PATH_RE = re.compile(
    r'\b(?:created|added|edited|changed|wrote|modified|updated)\s+([^\s,;:\n]+)',
    re.IGNORECASE,
)

# Test-run heuristics.
_RAN_TESTS_RE = re.compile(
    r'\b(?:ran|run|executed|running)\s+(?:the\s+)?tests?\b'
    r'|\btests?\s+(?:pass(?:ed)?|green|ok)\b'
    r'|\btest\s+suite\b'
    r'|\bunittest\b'
    r'|\bpytest\b'
    r'|\bnpm\s+test\b'
    r'|\bbats\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _looks_like_path(token: str) -> bool:
    """Return True if token plausibly looks like a file path.

    Heuristics: contains '/' (directory separator) OR has a file extension
    (trailing '.XYZ' where XYZ is 1-6 alpha chars).
    """
    if '/' in token:
        return True
    dot = token.rfind('.')
    if 0 < dot < len(token) - 1:
        ext = token[dot + 1:]
        if ext.isalpha() and 1 <= len(ext) <= 6:
            return True
    return False


def _parse_kv(block: str) -> dict:
    """Parse 'key: value' lines from a Handoff block body.

    Mirrors cast_handoff_parser._parse_kv exactly:
    - Keys are lowercased and stripped.
    - Values are stripped.
    - Lines without ':' are silently skipped.
    - Only the FIRST ':' is used as the delimiter (values may contain colons).
    - Empty lines are skipped.
    """
    result: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip().lower()
        value = value.strip()
        if key:
            result[key] = value
    return result


def _parse_files_changed(raw: str) -> list:
    """Parse files_changed value: comma-separated list; literal 'none' → []."""
    if not raw:
        return []
    stripped = raw.strip()
    if stripped.lower() == 'none':
        return []
    parts = [p.strip() for p in stripped.split(',')]
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# Tier 1 parser
# ---------------------------------------------------------------------------

def _parse_handoff(text: str) -> Optional[dict]:
    """Attempt Tier 1 parse: look for a ## Handoff block.

    Returns a claim dict if the block is found, or None if absent.
    An unparseable / empty block returns None (not a false claim — see CRITICAL RULE).
    """
    m = _HANDOFF_RE.search(text)
    if not m:
        return None

    block = m.group(1)
    raw_excerpt = block[:500]

    fields = _parse_kv(block)
    if not fields:
        return None

    raw_status = fields.get('status', '').strip().upper()
    status: Optional[str] = raw_status if raw_status in _STATUS_VALUES else None

    files_changed = _parse_files_changed(fields.get('files_changed', ''))

    blockers_raw = fields.get('blockers', '').strip()
    blockers: Optional[str] = (
        None if not blockers_raw or blockers_raw.lower() == 'none' else blockers_raw
    )

    ran_tests = bool(_RAN_TESTS_RE.search(block))

    return {
        'status': status,
        'files_changed': files_changed,
        'blockers': blockers,
        'ran_tests': ran_tests,
        'source': 'handoff',
        'raw': raw_excerpt,
    }


# ---------------------------------------------------------------------------
# Tier 2 parser
# ---------------------------------------------------------------------------

def _parse_nl(text: str) -> dict:
    """Tier 2 natural-language fallback parser.

    Detects Status: <value> and extracts file paths from backtick tokens and
    verb-then-path patterns. Deduplicates paths (preserves first-seen order).
    """
    # Status detection — prefer explicit "Status: X" form.
    status: Optional[str] = None
    m = _NL_STATUS_RE.search(text)
    if m:
        candidate = m.group(1).upper()
        if candidate in _STATUS_VALUES:
            status = candidate

    # Bare "DONE" fallback if no "Status:" form found.
    if status is None and _BARE_DONE_RE.search(text):
        status = 'DONE'

    # File path extraction — deduplicated, first-seen order.
    files: list = []
    seen: set = set()

    for fm in _BACKTICK_PATH_RE.finditer(text):
        token = fm.group(1).strip()
        if _looks_like_path(token) and token not in seen:
            files.append(token)
            seen.add(token)

    for fm in _VERB_PATH_RE.finditer(text):
        token = fm.group(1).strip().rstrip('.,;:)')
        if _looks_like_path(token) and token not in seen:
            files.append(token)
            seen.add(token)

    ran_tests = bool(_RAN_TESTS_RE.search(text))

    return {
        'status': status,
        'files_changed': files,
        'blockers': None,
        'ran_tests': ran_tests,
        'source': 'nl',
        'raw': text[:500],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_claim(text: str) -> dict:
    """Parse an agent completion claim from text.

    Two-tier:
      1. ## Handoff block (structured, CAST-compatible, preferred).
      2. Natural-language Status + path extraction (fallback).

    CRITICAL RULE: a MISSING or unparseable claim always returns
    ``status=None, source="none"`` and MUST NEVER be treated as a false
    DONE downstream.

    Args:
        text: raw agent output text (the full final message).

    Returns:
        {
            "status":        str | None,
            "files_changed": list[str],
            "blockers":      str | None,
            "ran_tests":     bool,
            "source":        "handoff" | "nl" | "none",
            "raw":           str,
        }
    """
    if not text or not text.strip():
        return {
            'status': None,
            'files_changed': [],
            'blockers': None,
            'ran_tests': False,
            'source': 'none',
            'raw': '',
        }

    # Tier 1
    result = _parse_handoff(text)
    if result is not None:
        return result

    # Tier 2
    nl = _parse_nl(text)

    # If NL found neither status nor files, return source="none" (CRITICAL RULE).
    if nl['status'] is None and not nl['files_changed']:
        return {
            'status': None,
            'files_changed': [],
            'blockers': None,
            'ran_tests': nl['ran_tests'],
            'source': 'none',
            'raw': text[:500],
        }

    return nl
