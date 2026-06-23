#!/usr/bin/env python3
"""
state.py — Snapshot store for per-agent git tree state.

Backend: stdlib sqlite3 at ATTEST_STATE_DB (default ~/.attest/state.db).

Public API:
  agent_key(payload: dict) -> str
      Returns the canonical key for a parsed payload dict.
      Prefers agent_id; falls back to "{agent_type}:{session_id}".

  save_snapshot(agent_key, snapshot, *, repo, session_id, meta) -> bool
      Persist a snapshot dict for the given key. Returns True on success.

  load_snapshot(agent_key) -> dict | None
      Return the stored snapshot dict, or None if not found.

  clear(agent_key) -> None
      Delete the stored snapshot for the given key (idempotent).

Optional CAST cast.db mirror: if the cast_db module is importable AND the
cast.db file exists, verdicts are also written to the `attestations` table.
The mirror is always best-effort; any failure is silently ignored.
"""
import json
import os
import sqlite3
from typing import Optional


# ---------------------------------------------------------------------------
# Module-level schema-init guards (keyed by resolved db path)
# ---------------------------------------------------------------------------
# WAL mode, DDL, and chmod persist on the db file/dir once set, so repeating
# them on every connection wastes 8–24 sqlite ops per SubagentStop invocation.
# We re-initialize whenever the resolved path changes (e.g. a different
# ATTEST_STATE_DB in tests) so tests with isolated tmpdir DBs always get a
# fresh schema pass without needing to reload the module.
_schema_initialized_path: Optional[str] = None
_cast_db_initialized_path: Optional[str] = None


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Return the expanded path to the attest state DB."""
    raw = os.environ.get('ATTEST_STATE_DB', '~/.attest/state.db')
    return os.path.expanduser(raw)


def _open_db() -> sqlite3.Connection:
    """Open (and initialise) the state DB, creating directories as needed.

    Security: the parent directory is chmoded to 0o700 and the DB file to 0o600
    so neither is world-readable.  Both chmod calls are best-effort — a failure
    (e.g. on a read-only or foreign-owned filesystem) degrades to a no-op and
    NEVER raises out of this function.  The WAL sidecars (-wal/-shm) are
    protected implicitly by the 0o700 directory.

    Performance: WAL mode, DDL, and chmod persist on the db file after first
    initialization.  The module-level ``_schema_initialized_path`` flag skips
    the 8–24 repeated sqlite ops on every subsequent call within the same
    process.  The guard is keyed by resolved path so tests that point
    ATTEST_STATE_DB at a fresh tmpdir always get a proper schema pass.
    """
    global _schema_initialized_path
    path = _db_path()
    dirname = os.path.dirname(path)
    # makedirs is always run — it is idempotent and sqlite3.connect() requires
    # the directory to exist regardless of whether schema init is needed.
    try:
        os.makedirs(dirname, exist_ok=True)
    except OSError:
        pass

    need_init = (_schema_initialized_path != path)

    if need_init:
        # Harden the directory — also fixes pre-existing dirs that makedirs skips
        # when exist_ok=True (e.g. ~/.attest already existed world-readable).
        try:
            os.chmod(dirname, 0o700)
        except OSError:
            pass

    conn = sqlite3.connect(path, timeout=5)

    if need_init:
        # Harden the DB file itself (WAL sidecars inherit directory protection).
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS snapshots (
                agent_key   TEXT PRIMARY KEY,
                snapshot    TEXT NOT NULL,
                repo        TEXT NOT NULL DEFAULT '',
                session_id  TEXT NOT NULL DEFAULT '',
                meta        TEXT NOT NULL DEFAULT '{}',
                block_count INTEGER NOT NULL DEFAULT 0,
                saved_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
        ''')
        # Idempotent migration for pre-Phase-2 DBs created without block_count.
        # Without this, a SELECT/UPDATE of block_count on an old DB raises
        # OperationalError, which (swallowed) would read back as 0 forever — the
        # classic "counter never increments -> infinite block" failure. See python.md.
        try:
            conn.execute('ALTER TABLE snapshots ADD COLUMN block_count INTEGER NOT NULL DEFAULT 0')
        except sqlite3.OperationalError:
            pass  # column already exists
        # Session-scoped backstop counter (independent of agent_key stability). Keyed on
        # the confirmed-real (session_id, repo); bounds any runaway even if agent_id churns.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS session_blocks (
                session_id  TEXT NOT NULL,
                repo        TEXT NOT NULL,
                n           INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                PRIMARY KEY (session_id, repo)
            )
        ''')
        conn.commit()
        _schema_initialized_path = path

    return conn


def _safe_close(conn) -> None:
    """Close a sqlite connection if open, ignoring errors. For finally blocks so a
    connection is never leaked on an exception path (a hook runs as a short-lived
    process, but long-lived test/caller processes must not accumulate handles)."""
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def agent_key(payload: dict) -> str:
    """Derive the canonical agent key from a parsed payload dict.

    Prefers ``payload["agent_id"]``; falls back to
    ``"{agent_type}:{session_id}"`` when agent_id is absent or empty.
    """
    aid = (payload.get('agent_id') or '').strip()
    if aid:
        return aid
    atype = (payload.get('agent_type') or 'unknown').strip()
    sid = (payload.get('session_id') or '').strip()
    return f'{atype}:{sid}'


def save_snapshot(
    key: str,
    snapshot: dict,
    *,
    repo: str = '',
    session_id: str = '',
    meta: Optional[dict] = None,
) -> bool:
    """Persist a snapshot dict for the given agent key.

    Args:
        key:        canonical agent key (from agent_key()).
        snapshot:   output of gitdelta.snapshot().
        repo:       path to the git repository.
        session_id: session identifier for correlation.
        meta:       optional extra context dict.

    Returns:
        True on success, False on error.
    """
    conn = None
    try:
        conn = _open_db()
        snap_json = json.dumps(snapshot)
        meta_json = json.dumps(meta or {})
        # INSERT-OR-IGNORE then UPDATE (not INSERT OR REPLACE) so an existing row's
        # block_count is PRESERVED, never reset. REPLACE would delete+reinsert and
        # silently zero the per-agent loop counter — a re-fired SubagentStart mid-retry
        # could then defeat the cap. New rows get block_count=0 via the column default.
        conn.execute(
            '''
            INSERT OR IGNORE INTO snapshots
                (agent_key, snapshot, repo, session_id, meta)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (key, snap_json, repo or '', session_id or '', meta_json),
        )
        conn.execute(
            '''
            UPDATE snapshots
               SET snapshot = ?, repo = ?, session_id = ?, meta = ?,
                   saved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
             WHERE agent_key = ?
            ''',
            (snap_json, repo or '', session_id or '', meta_json, key),
        )
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        _safe_close(conn)


def load_snapshot(key: str) -> Optional[dict]:
    """Load the stored snapshot for the given agent key.

    Returns the snapshot dict, or None if not found.
    """
    conn = None
    try:
        conn = _open_db()
        cur = conn.execute(
            'SELECT snapshot FROM snapshots WHERE agent_key = ?',
            (key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        return None
    finally:
        _safe_close(conn)


def clear(key: str) -> None:
    """Delete the stored snapshot for the given key (idempotent).

    Drops the per-agent row, which discards both the snapshot and its
    ``block_count`` atomically. The session-scoped counter is intentionally NOT
    cleared here (it spans agents for the life of the session).

    Silently ignores errors.
    """
    conn = None
    try:
        conn = _open_db()
        conn.execute('DELETE FROM snapshots WHERE agent_key = ?', (key,))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _safe_close(conn)


# ---------------------------------------------------------------------------
# Enforcement counters (Phase 2)
# ---------------------------------------------------------------------------

def get_block_count(key: str) -> int:
    """Return the per-agent block counter for ``key`` (0 if absent or on error)."""
    conn = None
    try:
        conn = _open_db()
        cur = conn.execute(
            'SELECT block_count FROM snapshots WHERE agent_key = ?', (key,)
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])
    except Exception:  # noqa: BLE001
        return 0
    finally:
        _safe_close(conn)


def increment_block_count(key: str) -> Optional[int]:
    """Atomically increment the per-agent block counter and return the new value.

    Returns the durably-committed new count, or ``None`` if the row does not
    exist or the write fails. The caller MUST treat ``None`` as "do not block"
    (fail open): emitting a block without a persisted increment is what creates
    an unbounded loop, so an unconfirmable write must suppress the block.
    """
    conn = None
    try:
        conn = _open_db()
        cur = conn.execute(
            'UPDATE snapshots SET block_count = block_count + 1 WHERE agent_key = ?',
            (key,),
        )
        if cur.rowcount < 1:
            return None  # no such row — cannot confirm the increment
        conn.commit()
        cur2 = conn.execute(
            'SELECT block_count FROM snapshots WHERE agent_key = ?', (key,)
        )
        row = cur2.fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])
    except Exception:  # noqa: BLE001
        return None
    finally:
        _safe_close(conn)


def get_session_blocks(session_id: str, repo: str) -> int:
    """Return the session-scoped block counter for (session_id, repo); 0 if absent."""
    conn = None
    try:
        conn = _open_db()
        cur = conn.execute(
            'SELECT n FROM session_blocks WHERE session_id = ? AND repo = ?',
            (session_id or '', repo or ''),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])
    except Exception:  # noqa: BLE001
        return 0
    finally:
        _safe_close(conn)


def increment_session_blocks(session_id: str, repo: str) -> Optional[int]:
    """Atomically increment the session backstop counter; return the new value.

    Returns the durably-committed new count, or ``None`` on failure (caller fails
    open). Uses INSERT-OR-IGNORE then UPDATE for compatibility with older sqlite
    (no UPSERT dependency).
    """
    sid = session_id or ''
    rp = repo or ''
    conn = None
    try:
        conn = _open_db()
        conn.execute(
            'INSERT OR IGNORE INTO session_blocks (session_id, repo, n) VALUES (?, ?, 0)',
            (sid, rp),
        )
        conn.execute(
            'UPDATE session_blocks SET n = n + 1, '
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            'WHERE session_id = ? AND repo = ?',
            (sid, rp),
        )
        conn.commit()
        cur = conn.execute(
            'SELECT n FROM session_blocks WHERE session_id = ? AND repo = ?',
            (sid, rp),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])
    except Exception:  # noqa: BLE001
        return None
    finally:
        _safe_close(conn)


# ---------------------------------------------------------------------------
# Optional CAST cast.db mirror
# ---------------------------------------------------------------------------

def mirror_to_cast_db(record: dict) -> None:
    """Best-effort write to the CAST cast.db attestations table.

    Silently no-ops if cast_db is not importable or cast.db does not exist.
    The ``attestations`` table is created if not present — but only ONCE per
    process per resolved cast.db path (``_cast_db_initialized_path`` guard)
    to avoid repeating the CREATE TABLE on every SubagentStop invocation.

    Args:
        record: arbitrary dict to store as JSON in the ``payload`` column.
    """
    global _cast_db_initialized_path
    conn = None
    try:
        cast_db_path = os.path.expanduser(
            os.environ.get('CAST_DB_PATH', '~/.claude/cast.db')
        )
        if not os.path.isfile(cast_db_path):
            return

        conn = sqlite3.connect(cast_db_path, timeout=3)
        if _cast_db_initialized_path != cast_db_path:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS attestations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_key   TEXT,
                    false_done  INTEGER DEFAULT 0,
                    payload     TEXT,
                    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                )
            ''')
            conn.commit()
            _cast_db_initialized_path = cast_db_path
        conn.execute(
            'INSERT INTO attestations (agent_key, false_done, payload) VALUES (?, ?, ?)',
            (
                record.get('agent_key', ''),
                1 if record.get('false_done') else 0,
                json.dumps(record),
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _safe_close(conn)
