"""
tests/test_state.py — Unit tests for attest.state

Covers:
  - agent_key() derivation (agent_id preferred, fallback to type:session)
  - save_snapshot() + load_snapshot() round-trip
  - load_snapshot() returns None when key absent
  - clear() removes the snapshot (idempotent)
  - save_snapshot() returns False on unwritable path (error path)
  - mirror_to_cast_db() no-ops gracefully when cast.db absent
"""
import json
import os
import tempfile
import unittest


class TestAgentKey(unittest.TestCase):
    def setUp(self) -> None:
        from attest import state
        self.state = state

    def test_agent_id_preferred(self) -> None:
        payload = {'agent_id': 'abc123', 'agent_type': 'code-writer', 'session_id': 'sess1'}
        self.assertEqual(self.state.agent_key(payload), 'abc123')

    def test_fallback_to_type_session(self) -> None:
        payload = {'agent_id': '', 'agent_type': 'code-writer', 'session_id': 'sess1'}
        self.assertEqual(self.state.agent_key(payload), 'code-writer:sess1')

    def test_missing_agent_id_key(self) -> None:
        payload = {'agent_type': 'debugger', 'session_id': 'sess2'}
        self.assertEqual(self.state.agent_key(payload), 'debugger:sess2')

    def test_all_missing(self) -> None:
        result = self.state.agent_key({})
        self.assertEqual(result, 'unknown:')


class TestSnapshotStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'state.db')
        os.environ['ATTEST_STATE_DB'] = self.db_path
        from attest import state
        self.state = state

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']

    def test_save_and_load_round_trip(self) -> None:
        snap = {'src/foo.py': 'deadbeef01', 'src/bar.py': 'cafebabe02'}
        ok = self.state.save_snapshot('agent-001', snap, repo='/tmp/repo', session_id='sess-A')
        self.assertTrue(ok)
        loaded = self.state.load_snapshot('agent-001')
        self.assertEqual(loaded, snap)

    def test_load_missing_key_returns_none(self) -> None:
        result = self.state.load_snapshot('no-such-key')
        self.assertIsNone(result)

    def test_clear_removes_snapshot(self) -> None:
        snap = {'a.py': 'hash1'}
        self.state.save_snapshot('to-clear', snap)
        self.assertIsNotNone(self.state.load_snapshot('to-clear'))
        self.state.clear('to-clear')
        self.assertIsNone(self.state.load_snapshot('to-clear'))

    def test_clear_nonexistent_key_is_idempotent(self) -> None:
        # Should not raise
        self.state.clear('ghost-key')

    def test_save_replaces_existing(self) -> None:
        snap1 = {'a.py': 'hash1'}
        snap2 = {'b.py': 'hash2'}
        self.state.save_snapshot('replace-test', snap1)
        self.state.save_snapshot('replace-test', snap2)
        loaded = self.state.load_snapshot('replace-test')
        self.assertEqual(loaded, snap2)

    def test_snapshot_with_error_key_stored(self) -> None:
        snap = {'_error': 'Not a git repository: /nope'}
        ok = self.state.save_snapshot('err-key', snap)
        self.assertTrue(ok)
        loaded = self.state.load_snapshot('err-key')
        self.assertEqual(loaded, snap)

    def test_meta_persisted(self) -> None:
        meta = {'agent_type': 'code-writer', 'agent_id': 'abc'}
        self.state.save_snapshot('meta-test', {}, meta=meta)
        # Meta is stored but load_snapshot only returns the snapshot dict.
        # Verify the save succeeded.
        loaded = self.state.load_snapshot('meta-test')
        self.assertEqual(loaded, {})

    def test_mirror_to_cast_db_noop_when_absent(self) -> None:
        # Should not raise even when cast.db doesn't exist
        self.state.mirror_to_cast_db({'agent_key': 'k', 'false_done': False})


class TestSnapshotStoreBadPath(unittest.TestCase):
    """Test save_snapshot graceful failure on unwritable path."""

    def setUp(self) -> None:
        # Point to a path where the parent dir cannot be created
        os.environ['ATTEST_STATE_DB'] = '/dev/null/cannot/write/state.db'
        from attest import state
        self.state = state

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']

    def test_save_returns_false_on_error(self) -> None:
        ok = self.state.save_snapshot('key', {'a': 'b'})
        self.assertFalse(ok)

    def test_load_returns_none_on_error(self) -> None:
        result = self.state.load_snapshot('key')
        self.assertIsNone(result)


class TestBlockCounter(unittest.TestCase):
    """Phase-2 per-agent block counter (the loop guard)."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'state.db')
        os.environ['ATTEST_STATE_DB'] = self.db_path
        from attest import state
        self.state = state

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']

    def test_fresh_snapshot_count_is_zero(self) -> None:
        self.state.save_snapshot('agent-1', {'a.py': 'h'})
        self.assertEqual(self.state.get_block_count('agent-1'), 0)

    def test_increment_returns_committed_value(self) -> None:
        self.state.save_snapshot('agent-2', {'a.py': 'h'})
        self.assertEqual(self.state.increment_block_count('agent-2'), 1)
        self.assertEqual(self.state.increment_block_count('agent-2'), 2)
        self.assertEqual(self.state.get_block_count('agent-2'), 2)

    def test_increment_missing_row_returns_none(self) -> None:
        # No snapshot saved for this key -> cannot confirm a durable increment.
        self.assertIsNone(self.state.increment_block_count('never-saved'))

    def test_clear_resets_counter(self) -> None:
        self.state.save_snapshot('agent-3', {'a.py': 'h'})
        self.state.increment_block_count('agent-3')
        self.state.clear('agent-3')
        self.assertEqual(self.state.get_block_count('agent-3'), 0)

    def test_resave_preserves_counter(self) -> None:
        self.state.save_snapshot('agent-4', {'a.py': 'h'})
        self.state.increment_block_count('agent-4')
        self.assertEqual(self.state.get_block_count('agent-4'), 1)
        # A re-fired start-snapshot must PRESERVE block_count (not reset it), so a
        # re-fired SubagentStart cannot silently defeat the per-agent loop cap.
        self.state.save_snapshot('agent-4', {'b.py': 'h2'})
        self.assertEqual(self.state.get_block_count('agent-4'), 1)
        # ...but the snapshot payload itself is refreshed.
        self.assertEqual(self.state.load_snapshot('agent-4'), {'b.py': 'h2'})

    def test_fresh_key_starts_at_zero(self) -> None:
        # A brand-new key (no prior row) starts at 0 via the column default.
        self.state.save_snapshot('agent-fresh', {'a.py': 'h'})
        self.assertEqual(self.state.get_block_count('agent-fresh'), 0)

    def test_get_count_missing_key_is_zero(self) -> None:
        self.assertEqual(self.state.get_block_count('ghost'), 0)


class TestSessionBlocks(unittest.TestCase):
    """Phase-2 session-scoped backstop counter."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        os.environ['ATTEST_STATE_DB'] = os.path.join(self.tmpdir, 'state.db')
        from attest import state
        self.state = state

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']

    def test_session_counter_increments_and_persists(self) -> None:
        self.assertEqual(self.state.get_session_blocks('sess-A', '/repo'), 0)
        self.assertEqual(self.state.increment_session_blocks('sess-A', '/repo'), 1)
        self.assertEqual(self.state.increment_session_blocks('sess-A', '/repo'), 2)
        self.assertEqual(self.state.get_session_blocks('sess-A', '/repo'), 2)

    def test_session_counter_scoped_by_session_and_repo(self) -> None:
        self.state.increment_session_blocks('sess-A', '/repo1')
        self.assertEqual(self.state.get_session_blocks('sess-A', '/repo2'), 0)
        self.assertEqual(self.state.get_session_blocks('sess-B', '/repo1'), 0)

    def test_session_counter_survives_agent_clear(self) -> None:
        # clear() must NOT touch the session counter.
        self.state.save_snapshot('agent-x', {'a.py': 'h'})
        self.state.increment_session_blocks('sess-A', '/repo')
        self.state.clear('agent-x')
        self.assertEqual(self.state.get_session_blocks('sess-A', '/repo'), 1)


class TestSchemaMigration(unittest.TestCase):
    """An existing pre-Phase-2 DB (no block_count column) must migrate, not loop."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'old.db')
        # Build a DB with the OLD schema (no block_count, no session_blocks).
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            CREATE TABLE snapshots (
                agent_key   TEXT PRIMARY KEY,
                snapshot    TEXT NOT NULL,
                repo        TEXT NOT NULL DEFAULT '',
                session_id  TEXT NOT NULL DEFAULT '',
                meta        TEXT NOT NULL DEFAULT '{}',
                saved_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
        ''')
        conn.execute(
            "INSERT INTO snapshots (agent_key, snapshot) VALUES ('legacy', '{}')"
        )
        conn.commit()
        conn.close()
        os.environ['ATTEST_STATE_DB'] = self.db_path
        from attest import state
        self.state = state

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']

    def test_migration_adds_block_count_and_works(self) -> None:
        # Triggers _open_db -> idempotent ALTER. The legacy row gets default 0.
        self.assertEqual(self.state.get_block_count('legacy'), 0)
        self.assertEqual(self.state.increment_block_count('legacy'), 1)

    def test_migration_adds_session_blocks_table(self) -> None:
        self.assertEqual(self.state.increment_session_blocks('sess', '/r'), 1)
