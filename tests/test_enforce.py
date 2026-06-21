"""
tests/test_enforce.py — Unit tests for the pure enforcement policy layer.

Covers every row of the Phase-2 decision truth-table, the config readers
(strict ATTEST_ENFORCE, clamped int parsing), and the block-reason builder
(names every file; JSON-safe).
"""
import json
import unittest

from attest import enforce


def _block_eligible(**overrides) -> dict:
    """Baseline kwargs that decide() should resolve to BLOCK; override one field."""
    kwargs = dict(
        enforce=True,
        false_done=True,
        reliable=True,
        ambiguous=False,
        agent_id_present=True,
        stop_hook_active=False,
        block_count=0,
        max_retries=1,
        session_blocks=0,
        session_ceiling=10,
    )
    kwargs.update(overrides)
    return kwargs


class TestDecideTruthTable(unittest.TestCase):
    def test_baseline_blocks(self) -> None:
        d = enforce.decide(**_block_eligible())
        self.assertEqual(d['action'], 'block')
        self.assertTrue(d['increment'])
        self.assertTrue(d['keep_state'])
        self.assertEqual(d['reason_code'], 'BLOCK_FALSE_DONE')

    def test_not_enforcing_allows(self) -> None:
        d = enforce.decide(**_block_eligible(enforce=False))
        self.assertEqual(d['action'], 'allow')
        self.assertFalse(d['increment'])
        self.assertFalse(d['keep_state'])
        self.assertEqual(d['reason_code'], 'ALLOW_NOT_ENFORCING')

    def test_no_agent_id_allows(self) -> None:
        d = enforce.decide(**_block_eligible(agent_id_present=False))
        self.assertEqual(d['action'], 'allow')
        self.assertEqual(d['reason_code'], 'ALLOW_NO_AGENT_ID')

    def test_not_false_done_allows(self) -> None:
        d = enforce.decide(**_block_eligible(false_done=False))
        self.assertEqual(d['action'], 'allow')
        self.assertEqual(d['reason_code'], 'ALLOW_NOT_FALSE_DONE')

    def test_unreliable_delta_allows(self) -> None:
        d = enforce.decide(**_block_eligible(reliable=False))
        self.assertEqual(d['action'], 'allow')
        self.assertEqual(d['reason_code'], 'ALLOW_DELTA_UNRELIABLE')

    def test_ambiguous_allows(self) -> None:
        d = enforce.decide(**_block_eligible(ambiguous=True))
        self.assertEqual(d['action'], 'allow')
        self.assertEqual(d['reason_code'], 'ALLOW_AMBIGUOUS')

    def test_retry_cap_reached_allows(self) -> None:
        d = enforce.decide(**_block_eligible(block_count=1, max_retries=1))
        self.assertEqual(d['action'], 'allow')
        self.assertEqual(d['reason_code'], 'ALLOW_RETRY_CAP')

    def test_session_ceiling_reached_allows(self) -> None:
        d = enforce.decide(**_block_eligible(session_blocks=10, session_ceiling=10))
        self.assertEqual(d['action'], 'allow')
        self.assertEqual(d['reason_code'], 'ALLOW_SESSION_CEILING')

    def test_stop_hook_active_allows(self) -> None:
        d = enforce.decide(**_block_eligible(stop_hook_active=True))
        self.assertEqual(d['action'], 'allow')
        self.assertEqual(d['reason_code'], 'ALLOW_STOP_HOOK_ACTIVE')

    def test_max_retries_zero_never_blocks(self) -> None:
        # Kill-switch: enforcement on but cap 0 -> 0 < 0 is False -> allow.
        d = enforce.decide(**_block_eligible(block_count=0, max_retries=0))
        self.assertEqual(d['action'], 'allow')
        self.assertEqual(d['reason_code'], 'ALLOW_RETRY_CAP')

    def test_max_retries_two_blocks_first_two(self) -> None:
        self.assertEqual(enforce.decide(**_block_eligible(block_count=0, max_retries=2))['action'], 'block')
        self.assertEqual(enforce.decide(**_block_eligible(block_count=1, max_retries=2))['action'], 'block')
        self.assertEqual(enforce.decide(**_block_eligible(block_count=2, max_retries=2))['action'], 'allow')


class TestConfigReaders(unittest.TestCase):
    def test_enforcement_enabled_strict(self) -> None:
        self.assertTrue(enforce.enforcement_enabled({'ATTEST_ENFORCE': '1'}))
        self.assertTrue(enforce.enforcement_enabled({'ATTEST_ENFORCE': ' 1 '}))
        for val in ('', '0', 'true', 'yes', 'on', 'TRUE'):
            self.assertFalse(enforce.enforcement_enabled({'ATTEST_ENFORCE': val}), val)
        self.assertFalse(enforce.enforcement_enabled({}))

    def test_max_retries_parsing(self) -> None:
        self.assertEqual(enforce.max_retries({}), 1)              # default
        self.assertEqual(enforce.max_retries({'ATTEST_MAX_RETRIES': '0'}), 0)
        self.assertEqual(enforce.max_retries({'ATTEST_MAX_RETRIES': '3'}), 3)
        self.assertEqual(enforce.max_retries({'ATTEST_MAX_RETRIES': 'abc'}), 1)  # invalid -> default
        self.assertEqual(enforce.max_retries({'ATTEST_MAX_RETRIES': ''}), 1)
        self.assertEqual(enforce.max_retries({'ATTEST_MAX_RETRIES': '-2'}), 0)   # negative -> 0

    def test_session_ceiling_parsing(self) -> None:
        self.assertEqual(enforce.session_ceiling({}), 10)
        self.assertEqual(enforce.session_ceiling({'ATTEST_SESSION_BLOCK_CEILING': '25'}), 25)
        self.assertEqual(enforce.session_ceiling({'ATTEST_SESSION_BLOCK_CEILING': 'x'}), 10)


class TestBuildBlockReason(unittest.TestCase):
    def test_names_single_file(self) -> None:
        r = enforce.build_block_reason(['src/ghost.py'], 'DONE')
        self.assertIn('src/ghost.py', r)
        self.assertIn('DONE', r)

    def test_names_all_files(self) -> None:
        r = enforce.build_block_reason(['a.py', 'b.py', 'c.py'], 'DONE')
        for f in ('a.py', 'b.py', 'c.py'):
            self.assertIn(f, r)

    def test_caps_long_lists_with_more(self) -> None:
        files = [f'f{i}.py' for i in range(30)]
        r = enforce.build_block_reason(files, 'DONE')
        self.assertIn('more', r)  # "(+N more)"

    def test_empty_is_defensive_not_crash(self) -> None:
        r = enforce.build_block_reason([], 'DONE')
        self.assertTrue(r)  # non-empty defensive string

    def test_reason_is_json_safe(self) -> None:
        # Paths with quotes/backticks/newlines must survive json.dumps round-trip.
        reason = enforce.build_block_reason(['weird "name".py', "tick`s.py"], 'DONE')
        envelope = json.dumps({'decision': 'block', 'reason': reason})
        parsed = json.loads(envelope)
        self.assertEqual(parsed['decision'], 'block')
        self.assertEqual(parsed['reason'], reason)


if __name__ == '__main__':
    unittest.main()
