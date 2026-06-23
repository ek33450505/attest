#!/usr/bin/env python3
"""
verdict.py — Evaluate a parsed claim against the observed git delta.

Public API:
  evaluate(claim, observed, repo_root=None) -> dict

Returns:
  {
    "claimed_but_unchanged": list[str],   # claimed files absent from delta
    "observed_but_unclaimed": list[str],  # changed files not mentioned in claim
    "false_done":            bool,        # proven false DONE (contradicted claim)
    "ambiguous":             bool,        # from observed["ambiguous"]
    "reason":                str,         # human-readable one-line explanation
  }
"""
import os
from typing import Optional


def _normalize_path(path: str, repo_root: Optional[str] = None) -> str:
    """Normalize a file path to a consistent repo-relative form for comparison.

    Rules:
    - Absolute path + repo_root provided → convert to repo-relative (if inside repo).
    - Strip leading './'.
    - Everything else is left unchanged (normalize separators handled by os.path).
    """
    path = path.strip()
    if not path:
        return path

    if os.path.isabs(path) and repo_root:
        try:
            rel = os.path.relpath(path, repo_root)
            # Only use the relative form if it doesn't escape the repo root.
            if not rel.startswith('..'):
                return rel
        except ValueError:
            # relpath can raise on Windows when drives differ; fall through.
            pass

    # Strip leading './'
    if path.startswith('./'):
        path = path[2:]

    return path


def evaluate(
    claim: dict,
    observed: dict,
    repo_root: Optional[str] = None,
) -> dict:
    """Evaluate a parsed claim against observed git delta.

    A claim is a "false DONE" if and only if:
      - claim["status"] == "DONE"          (agent asserted completion)
      - claim["source"] != "none"          (claim is present, not missing)
      - claimed_but_unchanged is non-empty  (at least one claimed file never changed)

    Args:
        claim:     output of ``claim.parse_claim()``.
        observed:  output of ``gitdelta.delta()``.
        repo_root: optional absolute path to the repo root; used for
                   abs-vs-relative path normalization.

    Returns:
        {
            "claimed_but_unchanged": list[str],
            "observed_but_unclaimed": list[str],
            "false_done":            bool,
            "ambiguous":             bool,
            "reason":                str,
        }
    """
    claimed_files_raw: list = claim.get('files_changed', [])
    observed_changed: set = observed.get('changed', set())
    ambiguous: bool = observed.get('ambiguous', False)
    status: Optional[str] = claim.get('status')
    source: str = claim.get('source', 'none')

    # Build normalized → original-path maps for comparison.
    claimed_norm: dict = {
        _normalize_path(p, repo_root): p
        for p in claimed_files_raw
    }
    observed_norm: dict = {
        _normalize_path(p, repo_root): p
        for p in observed_changed
    }

    # Files the agent claimed it changed that are NOT in the observed delta.
    claimed_but_unchanged: list = [
        claimed_norm[norm]
        for norm in claimed_norm
        if norm not in observed_norm
    ]

    # Files that changed but the agent never mentioned (scope-creep signal).
    observed_but_unclaimed: list = [
        observed_norm[norm]
        for norm in observed_norm
        if norm not in claimed_norm
    ]

    # A false DONE requires: DONE status + a present (non-"none") claim + unclaimed changes.
    false_done: bool = (
        status == 'DONE'
        and source != 'none'
        and bool(claimed_but_unchanged)
    )

    # Human-readable reason string.
    reason = _build_reason(
        false_done=false_done,
        claimed_but_unchanged=claimed_but_unchanged,
        observed_but_unclaimed=observed_but_unclaimed,
        status=status,
        ambiguous=ambiguous,
        source=source,
    )

    return {
        'claimed_but_unchanged': claimed_but_unchanged,
        'observed_but_unclaimed': observed_but_unclaimed,
        'false_done': false_done,
        'ambiguous': ambiguous,
        'reason': reason,
    }


def _build_reason(
    *,
    false_done: bool,
    claimed_but_unchanged: list,
    observed_but_unclaimed: list,
    status: Optional[str],
    ambiguous: bool,
    source: str,
) -> str:
    """Build a concise human-readable reason string for the verdict."""
    if false_done:
        n = len(claimed_but_unchanged)
        preview = ', '.join(claimed_but_unchanged[:3])
        suffix = f' (+{n - 3} more)' if n > 3 else ''
        verb = 'was' if n == 1 else 'were'
        return f'Claimed DONE but {preview}{suffix} {verb} never modified'

    if claimed_but_unchanged:
        preview = ', '.join(claimed_but_unchanged[:3])
        return f'Claimed {status or "(none)"} but {preview} not in observed delta'

    if observed_but_unclaimed:
        n = len(observed_but_unclaimed)
        return f'{n} file{"s" if n != 1 else ""} changed but not mentioned in claim'

    if ambiguous:
        return 'Delta is ambiguous due to pre-existing uncommitted changes'

    if source == 'none':
        return 'No claim found — cannot verify'

    return 'Claim matches observed delta'
