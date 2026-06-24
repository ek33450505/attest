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
import stat
import subprocess
from typing import Optional

# Sentinel stored for deleted files in a snapshot.
_DELETED_SENTINEL = '\x00DELETED\x00'

# Files larger than this are fingerprinted by partial content + metadata rather
# than a full content hash, so the synchronous stop-hook is never stalled by a
# large binary.  The fingerprint includes a partial hash of the first+last 4 KB
# so size+mtime-preserving content edits are still detected.
# A sha256 hex digest is always 64 lowercase hex chars with no colon, so the
# "meta:" prefix guarantees zero collision with a real hash.
# Override at runtime via the ATTEST_MAX_HASH_BYTES environment variable.
_MAX_HASH_BYTES = 10 * 1024 * 1024  # 10 MB default

# Bytes read from the head and tail of large files for the partial content hash.
_PARTIAL_HASH_CHUNK = 4096


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


def _partial_content_hash(path: str, size: int) -> str:
    """Return a 16-char hex digest of the first + last 4 KB of a file.

    Opens the file with O_NOFOLLOW to guard against a TOCTOU race where the
    file is swapped to a symlink between the caller's lstat and this open.
    Returns '' on any error so callers can fall back to a plain metadata
    fingerprint (fail-open by design).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ''
    try:
        with os.fdopen(fd, 'rb') as fh:
            head = fh.read(_PARTIAL_HASH_CHUNK)
            tail = b''
            if size > _PARTIAL_HASH_CHUNK:
                # Seek to the start of the last chunk (but not before the
                # current position, in case the file shrank since lstat).
                fh.seek(max(fh.tell(), size - _PARTIAL_HASH_CHUNK))
                tail = fh.read(_PARTIAL_HASH_CHUNK)
    except (IOError, OSError):
        return ''
    h = hashlib.sha256()
    h.update(head + tail)
    return h.hexdigest()[:16]


def _sha256_file(path: str) -> str:
    """Return a content fingerprint for the file at path.

    For regular files whose size is at or below _MAX_HASH_BYTES (default 10 MB,
    overridable via the ATTEST_MAX_HASH_BYTES env var), the full SHA-256 hex
    digest is returned.

    For symlinks, a metadata fingerprint from lstat is returned rather than
    following the target — this prevents a FIFO/device stall via a symlink TOCTOU:

        ``link:<lstat_size>:<lstat_mtime_ns>``

    For large regular files (size > _MAX_HASH_BYTES), a partial-content
    fingerprint is returned so the synchronous stop-hook is never stalled:

        ``meta:<size>:<mtime_ns>:<sha256(first4k+last4k)[:16]>``

    A sha256 hex digest is exactly 64 lowercase hex chars with no colon, so the
    ``meta:`` and ``link:`` prefixes guarantee no collision with a real hash and
    no false OK / false block.  On any read error when computing the partial hash,
    the format falls back to ``meta:<size>:<mtime_ns>`` (still detects size/mtime
    changes).

    Returns _DELETED_SENTINEL if the file cannot be stat'd (deleted, permission
    denied, etc.).
    """
    try:
        max_bytes = int(os.environ.get('ATTEST_MAX_HASH_BYTES', _MAX_HASH_BYTES))
    except (ValueError, TypeError):
        max_bytes = _MAX_HASH_BYTES
    # Clamp negatives: int('-1') succeeds but makes st.st_size > max_bytes
    # always True, silently degrading every file to metadata fingerprinting.
    if max_bytes < 0:
        max_bytes = _MAX_HASH_BYTES

    try:
        lst = os.lstat(path)
    except (IOError, OSError):
        return _DELETED_SENTINEL

    # Symlink: return a lstat-based fingerprint; never follow to the target.
    # A symlink that points to a FIFO or device could stall a blocking read.
    if stat.S_ISLNK(lst.st_mode):
        return f'link:{lst.st_size}:{lst.st_mtime_ns}'

    # Large regular file: partial-content fingerprint (first+last 4 KB).
    if lst.st_size > max_bytes:
        partial = _partial_content_hash(path, lst.st_size)
        if partial:
            return f'meta:{lst.st_size}:{lst.st_mtime_ns}:{partial}'
        # Fall back to plain metadata fingerprint on any read error (fail-open).
        return f'meta:{lst.st_size}:{lst.st_mtime_ns}'

    # Regular file: full SHA-256 with O_NOFOLLOW to prevent a TOCTOU race where
    # the file is swapped to a symlink between lstat and open.
    h = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return _DELETED_SENTINEL
    try:
        with os.fdopen(fd, 'rb') as fh:
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

def _snapshot_with_root(repo_dir: str) -> tuple:
    """Internal helper: snapshot the working tree and return (result_dict, root).

    Resolves the repo root once via ``_get_repo_root`` (1 subprocess) and runs
    ``git status`` once (1 subprocess), for a total of 2 git calls.  The resolved
    root is returned alongside the hash dict so ``delta()`` can include it in its
    own return value without making a redundant third ``rev-parse`` call.

    Returns:
        ``(result_dict, root)`` where ``root`` is the absolute git toplevel.
        On any error, returns ``({'_error': '<message>'}, None)`` — root is None
        so callers can detect the error path without inspecting the dict.

    The public ``snapshot()`` is a thin wrapper that discards ``root`` to keep
    the public API unchanged.  ``delta()`` consumes both fields.
    """
    try:
        root = _get_repo_root(repo_dir)
    except NotAGitRepo as exc:
        return {'_error': str(exc)}, None
    except Exception as exc:  # noqa: BLE001
        return {'_error': f'Failed to resolve repo root: {exc}'}, None

    try:
        stdout, stderr, rc = _run_git(['status', '--porcelain=v1', '-z'], cwd=root)
        if rc != 0:
            err = stderr.decode('utf-8', errors='replace').strip()
            return {'_error': f'git status failed (rc={rc}): {err}'}, None

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

        return result, root

    except Exception as exc:  # noqa: BLE001
        return {'_error': f'Snapshot failed: {exc}'}, None


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
    result, _root = _snapshot_with_root(repo_dir)
    return result


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

    # _snapshot_with_root resolves the repo root via a single rev-parse and runs
    # git status — 2 subprocesses total per delta() call.  The root is threaded
    # back here so hook.py can skip its own repo_root() call (no 3rd rev-parse).
    after, root = _snapshot_with_root(repo_dir)

    if '_error' in after:
        # Cannot determine what changed; empty set, and explicitly UNRELIABLE so
        # enforcement fails open instead of mistaking this for "changed nothing".
        # root is absent on error paths so hook.py's fallback to repo_root() applies.
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

    return {
        'changed': changed,
        'ambiguous': before_has_changes,
        'reliable': True,
        'root': root,  # resolved by _snapshot_with_root; no extra subprocess needed
    }
