#!/usr/bin/env python3
"""
claim.py — Two-tier claim parser for agent completion claims.

Tier 1: Parse ## Handoff block (structured, preferred).
        Mirrors cast_handoff_parser._HANDOFF_RE and _parse_kv exactly so CAST
        handoff blocks parse identically here and in the upstream parser.

Tier 2: Natural-language fallback (regex-based, when no Handoff block found).
        Status: extracted ONLY when "Status:" appears at start-of-line (after
        optional leading whitespace). Files changed: extracted ONLY from an
        explicit key at start-of-line. Separator anchoring (/ ; |) was removed
        in BUG-4b hardening — it produced false-positives on ordinary prose
        such as "The previous implementation; status: DONE was broken."

        Accepted trade-off: a single-line "Status: DONE / Files changed: a.txt"
        will detect status but NOT extract the file. Use multi-line format or a
        ## Handoff block to have files included in the verified claim.

        Free-prose scraping (backtick paths, verb-path patterns, bare DONE) is
        intentionally absent — it was a false-positive source in live capture
        (BUG-4).

CRITICAL RULE: a MISSING or unparseable claim returns status=None, source="none".
               It MUST NEVER be treated as a false DONE downstream.

Return value of parse_claim():
  {
    "status":        str | None,       # DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT | None
    "files_changed": list[str],        # normalized file paths (empty list when none)
    "blockers":      str | None,       # blockers text, or None when absent/none
    "ran_tests":     bool,             # True if claim text mentions running tests (informational only)
    "source":        str,              # "handoff" | "nl" | "none"
    "raw":           str,              # first 500 chars of the parsed source block
  }
"""
import re
from typing import Optional

# Matches lines that open or close a fenced code block (``` or ~~~).
# Used in _parse_nl() to skip Status/files detection while inside a fence.
_FENCE_RE = re.compile(r'^[ \t]*(```|~~~)')

# ---------------------------------------------------------------------------
# Tier 1 — ## Handoff block (mirrors cast_handoff_parser.py exactly)
# ---------------------------------------------------------------------------

# Regex from cast_handoff_parser._HANDOFF_RE — do not change without syncing upstream.
_HANDOFF_RE = re.compile(r'## Handoff\s*\n([\s\S]+?)(?=\n## |\Z)')

# Status values recognized in structured Handoff blocks (uppercase comparison).
_STATUS_VALUES = frozenset({'DONE', 'DONE_WITH_CONCERNS', 'BLOCKED', 'NEEDS_CONTEXT'})

# ---------------------------------------------------------------------------
# Tier 2 — anchored natural-language patterns
#
# REMOVED (BUG-4 hardening — false-positive sources):
#   _BACKTICK_PATH_RE  — backtick-wrapped tokens contributed ghost file paths
#   _VERB_PATH_RE      — "created foo.py" patterns fired on prose descriptions
#   _BARE_DONE_RE      — bare "DONE" anywhere caused false blocks on honest prose
#
# REMOVED (BUG-4b hardening — separator anchoring produced false-positives):
#   _NL_STATUS_SEP_RE  — "Status:" after / ; | fired on ordinary prose
#                        e.g. "The previous implementation; status: DONE was broken."
#   _FILES_KEY_SEP_RE  — files-key after / ; | produced phantom file claims
#                        e.g. "Status: DONE / files_changed: ghost.py" -> ['ghost.py']
#
# Replacement (start-of-line ONLY): Status: and files-changed keys are now
# recognised ONLY at start-of-line (after optional whitespace). A path mentioned
# in prose MUST NOT become a claimed file. Accepted trade-off: a single-line
# "Status: DONE / Files changed: a.txt" will detect status but NOT extract the
# file; use multi-line format or a ## Handoff block to have files verified.
# ---------------------------------------------------------------------------

# "Status: DONE" anchored at start of a line (use with re.match() on each line).
_NL_STATUS_SOL_RE = re.compile(
    r'[ \t]*Status\s*:\s*(DONE_WITH_CONCERNS|DONE|BLOCKED|NEEDS_CONTEXT)\b',
    re.IGNORECASE,
)

# Files key at start of a line (use with re.match() on each line).
# Keys: files_changed, files changed, changed files, modified files, files modified.
# Value terminator: \n | ; only — NOT / because file paths contain directory slashes.
_FILES_KEY_SOL_RE = re.compile(
    r'[ \t]*(?:files[_ ]changed|changed[_ ]files|modified[_ ]files|files[_ ]modified)'
    r'\s*:\s*([^\n|;]*)',
    re.IGNORECASE,
)

# Test-run heuristics (informational only; does not gate blocking).
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
    """Parse files_changed value: comma/and-separated list; literal 'none' → []."""
    if not raw:
        return []
    stripped = raw.strip()
    if stripped.lower() == 'none':
        return []
    # Split on commas and the word 'and' (optional Oxford-style separator)
    parts = re.split(r',|\band\b', stripped, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


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

    Detects Status: <value> ONLY when anchored at start-of-line (after optional
    leading whitespace). Extracts files_changed ONLY from an explicit files-key
    at start-of-line. Separator anchoring (/ ; |) was removed in BUG-4b hardening
    because it fired on ordinary prose (e.g. "impl; Status: DONE was broken.").

    Intentionally absent (BUG-4 / BUG-4b hardening):
      - Backtick-token path scraping
      - Verb-then-path scraping ("created foo.py")
      - Bare "DONE" anywhere fallback
      - Status: or files-key after a field separator (/ ; |)

    A path mentioned in prose (e.g. \"the bug is in `src/auth.py`\") MUST NOT
    become a claimed file.
    """
    status: Optional[str] = None
    files: list = []
    seen: set = set()
    in_code_block: bool = False

    for line in text.splitlines():
        # ---- Code-fence tracking — skip detection inside fences (I1/I4) -----
        if _FENCE_RE.match(line):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # ---- Status detection (start-of-line ONLY) --------------------------
        sm = _NL_STATUS_SOL_RE.match(line)
        if sm:
            candidate = sm.group(1).upper()
            if candidate in _STATUS_VALUES and status is None:
                status = candidate

        # ---- Files extraction (start-of-line ONLY) --------------------------
        fm = _FILES_KEY_SOL_RE.match(line)
        if fm:
            for f in _parse_files_changed(fm.group(1)):
                if f not in seen:
                    files.append(f)
                    seen.add(f)

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
      2. Natural-language Status + anchored files extraction (fallback).

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
