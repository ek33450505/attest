#!/usr/bin/env python3
"""
gitdelta.py — Git working-tree snapshot and delta computation.

All git operations are READ-ONLY. This module never mutates a repository.

Public API:
  snapshot(repo_dir: str) -> dict
      Returns {relpath: sha256_hex} for every path that differs from HEAD.
      Deleted files map to the _DELETED_SENTINEL constant.
      On error (non-git dir, git not installed, etc.) returns {"_error": "..."}.

  delta(before: dict, repo_dir: str) -> dict
      Returns {"changed": set[str], "ambiguous": bool}.
      "changed" is the set of repo-relative paths whose content-hash differs
      between the before snapshot and the current working tree.
      "ambiguous" is True when before had pre-existing uncommitted changes that
      cannot be cleanly attributed to the agent under evaluation.
"""
import hashlib
import os
import subprocess
from typing import Optional

# Sentinel stored for deleted files in a snapshot.
_DELETED_SENTINEL = '\x00DELETED\x00'

# Files larger than this are fingerprinted by metadata (size + mtime_ns) rather
# than content, so the synchronous stop-hook is never stalled by a large binary.
# A real write changes size or mtime → fingerprint changes → change is still
# detected.  A sha256 hex digest is always 64 lowercase hex chars with no colon,
# so the "meta:" prefix guarantees zero collision with a real hash.
# Override at runtime via the ATTEST_MAX_HASH_BYTES environment variable.
_MAX_HASH_BYTES = 10 * 1024 * 1024  # 10 MB default


class NotAGitRepo(Exception):
    """Raised when a directory is not inside a git repository."""


# ---------------------------------------------------------------------------
# Low-level git helpers
# ---------------------------------------------------------------------------

def _run_git(args: list, cwd: str) -> tuple:
    """Run a git subcommand. Returns (stdout_bytes, stderr_bytes, returncode)."""
    result = subprocess.run(
        ['git'] + args,
        cwd=cwd,
        capture_output=True,
    )
    return result.stdout, result.stderr, result.returncode


def _get_repo_root(repo_dir: str) -> str:
    """Return the absolute git repo root for repo_dir.

    Raises:
        NotAGitRepo: if repo_dir is not inside a git repository.
    """
    stdout, _stderr, rc = _run_git(['rev-parse', '--show-toplevel'], cwd=repo_dir)
    if rc != 0:
        raise NotAGitRepo(f'Not a git repository: {repo_dir}')
    return stdout.decode('utf-8', errors='replace').strip()


def repo_root(repo_dir: str) -> Optional[str]:
    """Resolve the absolute git toplevel for repo_dir, or None if not a git repo.

    Non-raising wrapper around ``_get_repo_root``. Used so callers can normalize
    claimed/observed paths against the *resolved* toplevel (e.g. ``/private/tmp``
    rather than the symlinked ``/tmp``) instead of an unresolved cwd.
    """
    try:
        return _get_repo_root(repo_dir)
    except Exception:  # noqa: BLE001
        return None


def path_on_disk(root: str, path: str, cwd: str = '') -> bool:
    """Return True if a claimed path currently exists on disk.

    Resolves a relative ``path`` against ``root`` (the git toplevel) AND against
    ``cwd`` (the subagent's payload cwd, which may be a subdirectory of the repo —
    so an agent that reported a cwd-relative claim still resolves), and also
    accepts an absolute path. The bias is intentionally toward returning True: a
    claimed file that exists on disk is NOT phantom and must never trigger an
    enforcement block, so a spurious True only ever fails open (skips blocking),
    never the reverse.
    """
    if not path:
        return False
    candidates = []
    if os.path.isabs(path):
        candidates.append(path)
    else:
        if root:
            candidates.append(os.path.join(root, path))
        if cwd:
            candidates.append(os.path.join(cwd, path))
        candidates.append(path)  # process-cwd fallback (last resort)
    for cand in candidates:
        try:
            if os.path.exists(cand):
                return True
        except OSError:
            continue
    return False


def _sha256_file(path: str) -> str:
    """Return the SHA-256 hex digest of the file at path.

    For files whose size exceeds _MAX_HASH_BYTES (default 10 MB, overridable
    via the ATTEST_MAX_HASH_BYTES env var), a metadata fingerprint is returned
    instead of reading the file content:

        ``meta:<size>:<mtime_ns>``

    Semantic safety: any real write changes at least one of size or mtime, so
    the fingerprint changes → the change is still detected.  A sha256 hex digest
    is exactly 64 lowercase hex chars with no colon, so the ``meta:`` prefix
    guarantees no collision with a real hash and no false OK / false block.

    Returns _DELETED_SENTINEL if the file cannot be read (deleted, permission
    denied, etc.).
    """
    try:
        max_bytes = int(os.environ.get('ATTEST_MAX_HASH_BYTES', _MAX_HASH_BYTES))
    except (ValueError, TypeError):
        max_bytes = _MAX_HASH_BYTES
    try:
        st = os.stat(path)
        if st.st_size > max_bytes:
            # Large file: skip content read; fingerprint on size + mtime_ns.
            return f'meta:{st.st_size}:{st.st_mtime_ns}'
    except (IOError, OSError):
        return _DELETED_SENTINEL

    h = hashlib.sha256()
    try:
        with open(path, 'rb') as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except (IOError, OSError):
        return _DELETED_SENTINEL
    return h.hexdigest()


def _parse_porcelain_v1_z(output: bytes) -> list:
    """Parse ``git status --porcelain=v1 -z`` output.

    With ``-z``, entries are NUL-terminated instead of newline-terminated.
    For renames and copies, the format is:
        XY SP NEW_PATH NUL OLD_PATH NUL
    so the OLD_PATH is consumed as a second token.

    Returns a list of repo-relative path strings (new path for renames).
    """
    paths: list = []
    tokens = output.split(b'\x00')
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # Each entry is at least XY + space + one filename char = 4 bytes.
        if len(token) < 4:
            i += 1
            continue
        xy = token[:2].decode('ascii', errors='replace')
        path = token[3:].decode('utf-8', errors='replace')
        x_status = xy[0]

        if x_status in ('R', 'C'):
            # Rename/copy: next NUL-delimited token is the old path; skip it.
            i += 2
        else:
            i += 1

        if path:
            paths.append(path)

    return paths


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def snapshot(repo_dir: str) -> dict:
    """Snapshot the git working-tree state.

    Captures all files that differ from HEAD (modified, added, untracked,
    deleted) and hashes their current content with SHA-256.

    Args:
        repo_dir: path to or inside a git repository.

    Returns:
        A dict mapping repo-relative paths to SHA-256 hex digests.
        Deleted files map to _DELETED_SENTINEL.
        On any error, returns ``{"_error": "<message>"}`` instead of raising.
    """
    try:
        root = _get_repo_root(repo_dir)
    except NotAGitRepo as exc:
        return {'_error': str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {'_error': f'Failed to resolve repo root: {exc}'}

    try:
        stdout, stderr, rc = _run_git(['status', '--porcelain=v1', '-z'], cwd=root)
        if rc != 0:
            err = stderr.decode('utf-8', errors='replace').strip()
            return {'_error': f'git status failed (rc={rc}): {err}'}

        paths = _parse_porcelain_v1_z(stdout)

        result: dict = {}
        for relpath in paths:
            abs_path = os.path.join(root, relpath)

            # git status reports untracked directories as "dirname/" (with trailing
            # slash).  Walk them to include each individual file.
            if relpath.endswith('/') and os.path.isdir(abs_path):
                for dirpath, _dirs, filenames in os.walk(abs_path):
                    for fname in filenames:
                        file_abs = os.path.join(dirpath, fname)
                        file_rel = os.path.relpath(file_abs, root)
                        result[file_rel] = _sha256_file(file_abs)
            elif os.path.isfile(abs_path):
                result[relpath] = _sha256_file(abs_path)
            else:
                result[relpath] = _DELETED_SENTINEL

        return result

    except Exception as exc:  # noqa: BLE001
        return {'_error': f'Snapshot failed: {exc}'}


def delta(before: dict, repo_dir: str) -> dict:
    """Compute what changed between the before snapshot and the current working tree.

    Args:
        before: snapshot dict from a prior call to ``snapshot()``.
        repo_dir: path to the git repository.

    Returns:
        {
            "changed":   set[str],  # repo-relative paths whose hash differs from before
            "ambiguous": bool,      # True when before had pre-existing uncommitted changes
            "reliable":  bool,      # True only when BOTH snapshots computed without error
        }

    Ambiguity: if ``before`` is non-empty and has no ``_error`` key, it means the repo
    had uncommitted changes at snapshot time. Changes from other agents or the user
    cannot be cleanly separated, so ``ambiguous=True`` is set honestly rather than
    asserting false precision (v1 sequential-case behaviour per spec).

    Reliability: ``reliable`` is the explicit, load-bearing signal for enforcement. An
    empty ``changed`` set is ambiguous on its own — it means EITHER "the agent changed
    nothing" OR "git could not be read" (non-git dir, transient ``git status`` failure).
    These must never be confused: the former can be a false DONE, the latter must fail
    open. ``reliable`` is derived ONLY from whether each snapshot carried an ``_error``,
    NEVER inferred from ``len(changed)``.
    """
    # If the start snapshot itself failed (non-git dir / git error at SubagentStart),
    # there is no trustworthy baseline — short-circuit as unreliable. Falling through
    # would treat every current diff-from-HEAD path as "changed" against a null baseline.
    if '_error' in before:
        return {'changed': set(), 'ambiguous': False, 'reliable': False}

    # Ambiguity: before had pre-existing uncommitted changes.
    before_has_changes = bool(
        before
        and len(before) > 0
    )

    after = snapshot(repo_dir)

    if '_error' in after:
        # Cannot determine what changed; empty set, and explicitly UNRELIABLE so
        # enforcement fails open instead of mistaking this for "changed nothing".
        return {'changed': set(), 'ambiguous': before_has_changes, 'reliable': False}

    changed: set = set()

    # Files in after that are new or have a different hash than in before.
    for path, after_hash in after.items():
        if after_hash != before.get(path):
            changed.add(path)

    # Files that were in before but have disappeared from after entirely
    # (e.g., deleted and then staged/committed during the agent run).
    for path in before:
        if path != '_error' and path not in after:
            changed.add(path)

    return {'changed': changed, 'ambiguous': before_has_changes, 'reliable': True}
