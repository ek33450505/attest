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
# DB path resolution
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Return the expanded path to the attest state DB."""
    raw = os.environ.get('ATTEST_STATE_DB', '~/.attest/state.db')
    return os.path.expanduser(raw)


def _open_db() -> sqlite3.Connection:
    """Open (and initialise) the state DB, creating directories as needed."""
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS snapshots (
            agent_key   TEXT PRIMARY KEY,
            snapshot    TEXT NOT NULL,
            repo        TEXT NOT NULL DEFAULT '',
            session_id  TEXT NOT NULL DEFAULT '',
            meta        TEXT NOT NULL DEFAULT '{}',
            saved_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    ''')
    conn.commit()
    return conn


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
    try:
        conn = _open_db()
        conn.execute(
            '''
            INSERT OR REPLACE INTO snapshots
                (agent_key, snapshot, repo, session_id, meta)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (
                key,
                json.dumps(snapshot),
                repo or '',
                session_id or '',
                json.dumps(meta or {}),
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def load_snapshot(key: str) -> Optional[dict]:
    """Load the stored snapshot for the given agent key.

    Returns the snapshot dict, or None if not found.
    """
    try:
        conn = _open_db()
        cur = conn.execute(
            'SELECT snapshot FROM snapshots WHERE agent_key = ?',
            (key,),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        return None


def clear(key: str) -> None:
    """Delete the stored snapshot for the given key (idempotent).

    Silently ignores errors.
    """
    try:
        conn = _open_db()
        conn.execute('DELETE FROM snapshots WHERE agent_key = ?', (key,))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Optional CAST cast.db mirror
# ---------------------------------------------------------------------------

def mirror_to_cast_db(record: dict) -> None:
    """Best-effort write to the CAST cast.db attestations table.

    Silently no-ops if cast_db is not importable or cast.db does not exist.
    The ``attestations`` table is created if not present (idempotent).

    Args:
        record: arbitrary dict to store as JSON in the ``payload`` column.
    """
    try:
        cast_db_path = os.path.expanduser(
            os.environ.get('CAST_DB_PATH', '~/.claude/cast.db')
        )
        if not os.path.isfile(cast_db_path):
            return

        conn = sqlite3.connect(cast_db_path, timeout=3)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS attestations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_key   TEXT,
                false_done  INTEGER DEFAULT 0,
                payload     TEXT,
                created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
        ''')
        conn.execute(
            'INSERT INTO attestations (agent_key, false_done, payload) VALUES (?, ?, ?)',
            (
                record.get('agent_key', ''),
                1 if record.get('false_done') else 0,
                json.dumps(record),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass
