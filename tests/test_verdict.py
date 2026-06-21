"""
tests/test_verdict.py — Unit tests for attest.verdict.evaluate()

Covers:
  - claimed_but_unchanged: files claimed but not in observed delta
  - observed_but_unclaimed: changed files not mentioned in claim
  - false_done: DONE + claimed_but_unchanged + source != "none"
  - false_done NOT set when source == "none" (CRITICAL RULE)
  - abs-vs-relative path normalization (both sides)
  - ambiguous flag propagation
  - reason string presence and correctness
  - non-DONE status does not trigger false_done
"""
import os
import unittest

from attest.verdict import evaluate


# ---------------------------------------------------------------------------
# Helpers to build fixture claim / observed dicts
# ---------------------------------------------------------------------------

def _claim(
    status: str = 'DONE',
    files_changed: list = None,
    source: str = 'handoff',
    blockers: str = None,
) -> dict:
    return {
        'status': status,
        'files_changed': files_changed or [],
        'blockers': blockers,
        'ran_tests': False,
        'source': source,
        'raw': '',
    }


def _observed(changed: set = None, ambiguous: bool = False) -> dict:
    return {
        'changed': changed or set(),
        'ambiguous': ambiguous,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClaimedButUnchanged(unittest.TestCase):

    def test_all_claimed_files_in_delta(self) -> None:
        """No claimed_but_unchanged when every claimed file is in the delta."""
        c = _claim(files_changed=['a.py', 'b.py'])
        o = _observed(changed={'a.py', 'b.py'})
        v = evaluate(c, o)
        self.assertEqual(v['claimed_but_unchanged'], [])
        self.assertFalse(v['false_done'])

    def test_one_claimed_file_missing_from_delta(self) -> None:
        """A claimed file absent from the delta → claimed_but_unchanged."""
        c = _claim(files_changed=['a.py', 'b.py'])
        o = _observed(changed={'a.py'})  # b.py never changed
        v = evaluate(c, o)
        self.assertIn('b.py', v['claimed_but_unchanged'])
        self.assertNotIn('a.py', v['claimed_but_unchanged'])

    def test_all_claimed_files_missing(self) -> None:
        """All claimed files absent → claimed_but_unchanged is full list."""
        c = _claim(files_changed=['x.py', 'y.py'])
        o = _observed(changed=set())
        v = evaluate(c, o)
        self.assertCountEqual(v['claimed_but_unchanged'], ['x.py', 'y.py'])

    def test_no_files_claimed_no_claimed_but_unchanged(self) -> None:
        """Empty files_changed list → claimed_but_unchanged is always empty."""
        c = _claim(files_changed=[])
        o = _observed(changed={'a.py'})
        v = evaluate(c, o)
        self.assertEqual(v['claimed_but_unchanged'], [])


class TestObservedButUnclaimed(unittest.TestCase):

    def test_observed_but_unclaimed_file(self) -> None:
        """A file in delta not mentioned in claim → observed_but_unclaimed."""
        c = _claim(files_changed=['a.py'])
        o = _observed(changed={'a.py', 'b.py'})
        v = evaluate(c, o)
        self.assertIn('b.py', v['observed_but_unclaimed'])
        self.assertNotIn('a.py', v['observed_but_unclaimed'])

    def test_all_observed_unclaimed(self) -> None:
        c = _claim(files_changed=[])
        o = _observed(changed={'x.py', 'y.py'})
        v = evaluate(c, o)
        self.assertCountEqual(v['observed_but_unclaimed'], ['x.py', 'y.py'])

    def test_perfect_match_no_unclaimed(self) -> None:
        c = _claim(files_changed=['a.py'])
        o = _observed(changed={'a.py'})
        v = evaluate(c, o)
        self.assertEqual(v['observed_but_unclaimed'], [])


class TestFalseDone(unittest.TestCase):

    def test_false_done_when_done_and_claimed_but_unchanged(self) -> None:
        c = _claim(status='DONE', files_changed=['a.py', 'b.py'])
        o = _observed(changed={'a.py'})  # b.py never changed
        v = evaluate(c, o)
        self.assertTrue(v['false_done'])

    def test_not_false_done_when_source_is_none(self) -> None:
        """CRITICAL RULE: missing claim (source='none') can NEVER be a false DONE."""
        c = _claim(status='DONE', files_changed=['a.py'], source='none')
        o = _observed(changed=set())
        v = evaluate(c, o)
        self.assertFalse(v['false_done'])

    def test_not_false_done_when_status_is_not_done(self) -> None:
        """BLOCKED status with unclaimed files is not a false DONE."""
        c = _claim(status='BLOCKED', files_changed=['a.py'])
        o = _observed(changed=set())
        v = evaluate(c, o)
        self.assertFalse(v['false_done'])

    def test_not_false_done_when_status_is_done_with_concerns(self) -> None:
        """DONE_WITH_CONCERNS: only exact 'DONE' triggers false_done."""
        c = _claim(status='DONE_WITH_CONCERNS', files_changed=['a.py'])
        o = _observed(changed=set())
        v = evaluate(c, o)
        self.assertFalse(v['false_done'])

    def test_not_false_done_when_claimed_but_unchanged_is_empty(self) -> None:
        """DONE + all files changed → no false_done."""
        c = _claim(status='DONE', files_changed=['a.py'])
        o = _observed(changed={'a.py'})
        v = evaluate(c, o)
        self.assertFalse(v['false_done'])

    def test_not_false_done_when_status_is_none(self) -> None:
        """None status (missing) → never false_done."""
        c = _claim(status=None, files_changed=['a.py'])
        o = _observed(changed=set())
        v = evaluate(c, o)
        self.assertFalse(v['false_done'])

    def test_false_done_with_nl_source(self) -> None:
        """NL source CAN trigger false_done (source != 'none')."""
        c = _claim(status='DONE', files_changed=['a.py'], source='nl')
        o = _observed(changed=set())
        v = evaluate(c, o)
        self.assertTrue(v['false_done'])


class TestPathNormalization(unittest.TestCase):
    """Abs-vs-relative path matching via repo_root."""

    _REPO = '/home/user/myrepo'

    def test_abs_claimed_vs_relative_observed(self) -> None:
        """Absolute claimed path is matched to repo-relative observed path."""
        c = _claim(files_changed=['/home/user/myrepo/src/foo.py'])
        o = _observed(changed={'src/foo.py'})
        v = evaluate(c, o, repo_root=self._REPO)
        # foo.py is in both (normalized) → not claimed_but_unchanged
        self.assertEqual(v['claimed_but_unchanged'], [])
        self.assertFalse(v['false_done'])

    def test_dotslash_prefix_stripped(self) -> None:
        """'./src/foo.py' and 'src/foo.py' are treated as equal."""
        c = _claim(files_changed=['./src/foo.py'])
        o = _observed(changed={'src/foo.py'})
        v = evaluate(c, o)
        self.assertEqual(v['claimed_but_unchanged'], [])

    def test_abs_claimed_outside_repo_treated_as_unknown(self) -> None:
        """Absolute path outside the repo root is not normalized → mismatch."""
        c = _claim(files_changed=['/other/repo/file.py'])
        o = _observed(changed={'file.py'})
        v = evaluate(c, o, repo_root=self._REPO)
        # /other/repo/file.py does not map into _REPO → stays absolute → mismatch
        self.assertIn('/other/repo/file.py', v['claimed_but_unchanged'])

    def test_relative_claimed_matches_relative_observed(self) -> None:
        c = _claim(files_changed=['src/bar.py'])
        o = _observed(changed={'src/bar.py'})
        v = evaluate(c, o)
        self.assertEqual(v['claimed_but_unchanged'], [])


class TestAmbiguous(unittest.TestCase):

    def test_ambiguous_propagated_from_observed(self) -> None:
        c = _claim(files_changed=['a.py'])
        o = _observed(changed={'a.py'}, ambiguous=True)
        v = evaluate(c, o)
        self.assertTrue(v['ambiguous'])

    def test_not_ambiguous_when_observed_is_not(self) -> None:
        c = _claim(files_changed=['a.py'])
        o = _observed(changed={'a.py'}, ambiguous=False)
        v = evaluate(c, o)
        self.assertFalse(v['ambiguous'])


class TestReasonString(unittest.TestCase):

    def test_reason_present(self) -> None:
        c = _claim(files_changed=['a.py'])
        o = _observed(changed={'a.py'})
        v = evaluate(c, o)
        self.assertIsInstance(v['reason'], str)
        self.assertTrue(v['reason'])

    def test_reason_mentions_file_for_false_done(self) -> None:
        c = _claim(status='DONE', files_changed=['missing.py'])
        o = _observed(changed=set())
        v = evaluate(c, o)
        self.assertIn('missing.py', v['reason'])

    def test_reason_matches_delta_on_success(self) -> None:
        c = _claim(status='DONE', files_changed=['a.py'])
        o = _observed(changed={'a.py'})
        v = evaluate(c, o)
        self.assertIn('match', v['reason'].lower())

    def test_reason_no_claim(self) -> None:
        c = _claim(status=None, files_changed=[], source='none')
        o = _observed(changed=set())
        v = evaluate(c, o)
        self.assertIn('no claim', v['reason'].lower())

    def test_reason_ambiguous(self) -> None:
        c = _claim(status='DONE', files_changed=['a.py'])
        o = _observed(changed={'a.py'}, ambiguous=True)
        v = evaluate(c, o)
        # Claim matches but ambiguous flag is present — reason may mention ambiguous
        # or match; both are acceptable depending on evaluation order.
        self.assertIsInstance(v['reason'], str)


if __name__ == '__main__':
    unittest.main()
