"""
tests/test_claim.py — Unit tests for attest.claim.parse_claim()

Covers:
  - Tier 1 (## Handoff block): happy path, files_changed "none", ran_tests, blockers
  - Tier 2 (NL fallback): backtick paths, verb-then-path, bare DONE, Status:
  - Edge cases: empty text, no claim found, partial block, source rules
"""
import unittest

from attest.claim import parse_claim


class TestHandoffParsing(unittest.TestCase):
    """Tier 1: ## Handoff block parsing."""

    _FULL_HANDOFF = """\
## Handoff
files_changed: src/foo.py, src/bar.py
status: DONE
blockers: none

## Work Log
- Did stuff
"""

    def test_handoff_happy_path(self) -> None:
        result = parse_claim(self._FULL_HANDOFF)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], ['src/foo.py', 'src/bar.py'])
        self.assertIsNone(result['blockers'])
        self.assertEqual(result['source'], 'handoff')

    def test_handoff_files_none_returns_empty_list(self) -> None:
        text = "## Handoff\nfiles_changed: none\nstatus: DONE\nblockers: none\n"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])
        self.assertEqual(result['source'], 'handoff')

    def test_handoff_files_none_uppercase(self) -> None:
        text = "## Handoff\nfiles_changed: NONE\nstatus: DONE\nblockers: none\n"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])

    def test_handoff_blockers_present(self) -> None:
        text = "## Handoff\nfiles_changed: a.py\nstatus: BLOCKED\nblockers: Missing test fixture\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['blockers'], 'Missing test fixture')

    def test_handoff_blockers_literal_none(self) -> None:
        text = "## Handoff\nfiles_changed: a.py\nstatus: DONE\nblockers: none\n"
        result = parse_claim(text)
        self.assertIsNone(result['blockers'])

    def test_handoff_done_with_concerns_status(self) -> None:
        text = "## Handoff\nfiles_changed: a.py\nstatus: DONE_WITH_CONCERNS\nblockers: none\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE_WITH_CONCERNS')

    def test_handoff_needs_context_status(self) -> None:
        text = "## Handoff\nfiles_changed: none\nstatus: NEEDS_CONTEXT\nblockers: unclear direction\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'NEEDS_CONTEXT')
        self.assertEqual(result['blockers'], 'unclear direction')

    def test_handoff_ran_tests_detected_in_block(self) -> None:
        text = "## Handoff\nfiles_changed: a.py\nstatus: DONE\nblockers: none\nran unittest suite\n"
        result = parse_claim(text)
        self.assertTrue(result['ran_tests'])

    def test_handoff_ran_tests_not_detected(self) -> None:
        text = "## Handoff\nfiles_changed: a.py\nstatus: DONE\nblockers: none\n"
        result = parse_claim(text)
        self.assertFalse(result['ran_tests'])

    def test_handoff_values_with_colons_in_value(self) -> None:
        """Value may contain colons — only the first colon is the delimiter."""
        text = "## Handoff\nfiles_changed: a.py\nstatus: DONE\nblockers: see http://example.com/issue:42\n"
        result = parse_claim(text)
        self.assertEqual(result['blockers'], 'see http://example.com/issue:42')

    def test_handoff_status_case_insensitive_lower(self) -> None:
        """Status value stored as-is but only recognized statuses are surfaced."""
        text = "## Handoff\nfiles_changed: a.py\nstatus: done\nblockers: none\n"
        result = parse_claim(text)
        # "done" uppercased → "DONE" which is in _STATUS_VALUES → status='DONE'
        self.assertEqual(result['status'], 'DONE')

    def test_handoff_unknown_status_returns_none(self) -> None:
        text = "## Handoff\nfiles_changed: a.py\nstatus: MYSTERY\nblockers: none\n"
        result = parse_claim(text)
        # Unknown status value → None (not in _STATUS_VALUES)
        self.assertIsNone(result['status'])

    def test_handoff_empty_block_falls_through_to_nl(self) -> None:
        """A Handoff block with no key:value lines → Tier 2 fallback.

        The regex stops at the next '## ' heading, so 'Status: DONE' must be
        placed AFTER the Handoff block for the body to be colon-free.
        """
        text = (
            "## Handoff\n"
            "This block has no structured fields at all\n"
            "\n"
            "## Work Log\n"
            "Status: DONE\n"
        )
        result = parse_claim(text)
        # Handoff body stops at \n## Work Log, has no ':' → _parse_kv returns {} → Tier 2
        self.assertEqual(result['source'], 'nl')
        self.assertEqual(result['status'], 'DONE')

    def test_handoff_file_paths_trimmed(self) -> None:
        text = "## Handoff\nfiles_changed:  a.py ,  b/c.ts  \nstatus: DONE\nblockers: none\n"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], ['a.py', 'b/c.ts'])

    def test_handoff_raw_is_excerpt_of_block(self) -> None:
        result = parse_claim(self._FULL_HANDOFF)
        self.assertIn('files_changed', result['raw'])

    def test_handoff_source_is_handoff(self) -> None:
        result = parse_claim(self._FULL_HANDOFF)
        self.assertEqual(result['source'], 'handoff')

    def test_handoff_only_block_no_trailing_heading(self) -> None:
        """Handoff at end of string (no trailing ## heading)."""
        text = "## Handoff\nfiles_changed: x.py\nstatus: DONE\nblockers: none"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], ['x.py'])


class TestNLFallback(unittest.TestCase):
    """Tier 2: natural-language fallback parsing."""

    def test_nl_status_done_explicit(self) -> None:
        text = "I've finished the work.\n\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['source'], 'nl')

    def test_nl_status_done_with_concerns(self) -> None:
        text = "Status: DONE_WITH_CONCERNS\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE_WITH_CONCERNS')

    def test_nl_status_blocked(self) -> None:
        text = "Status: BLOCKED\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'BLOCKED')

    def test_nl_status_case_insensitive(self) -> None:
        text = "status: done\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')

    def test_nl_bare_done(self) -> None:
        """Bare 'DONE' token (no 'Status:' prefix) is detected."""
        text = "All changes are complete. DONE\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['source'], 'nl')

    def test_nl_backtick_path_with_slash(self) -> None:
        text = "Updated `src/utils.py` and `lib/helpers.ts`.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertIn('src/utils.py', result['files_changed'])
        self.assertIn('lib/helpers.ts', result['files_changed'])

    def test_nl_backtick_path_with_extension_no_slash(self) -> None:
        text = "Edited `app.py`.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertIn('app.py', result['files_changed'])

    def test_nl_verb_created(self) -> None:
        text = "Created src/new_module.py for the feature.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertIn('src/new_module.py', result['files_changed'])

    def test_nl_verb_modified(self) -> None:
        text = "Modified tests/test_core.py to cover edge cases.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertIn('tests/test_core.py', result['files_changed'])

    def test_nl_verb_added(self) -> None:
        text = "Added config/settings.json. Status: DONE\n"
        result = parse_claim(text)
        self.assertIn('config/settings.json', result['files_changed'])

    def test_nl_verb_wrote(self) -> None:
        text = "Wrote bin/deploy.sh. Status: DONE"
        result = parse_claim(text)
        self.assertIn('bin/deploy.sh', result['files_changed'])

    def test_nl_verb_updated(self) -> None:
        text = "Updated README.md. Status: DONE"
        result = parse_claim(text)
        self.assertIn('README.md', result['files_changed'])

    def test_nl_deduplicated_paths(self) -> None:
        text = "Updated `src/a.py` and also updated src/a.py. Status: DONE"
        result = parse_claim(text)
        # Should appear only once
        self.assertEqual(result['files_changed'].count('src/a.py'), 1)

    def test_nl_ran_tests_detected(self) -> None:
        text = "I ran the tests and they passed. Status: DONE"
        result = parse_claim(text)
        self.assertTrue(result['ran_tests'])

    def test_nl_tests_passed(self) -> None:
        text = "Tests passed. Status: DONE"
        result = parse_claim(text)
        self.assertTrue(result['ran_tests'])

    def test_nl_blockers_not_parsed(self) -> None:
        """NL fallback never sets blockers (only Handoff block does)."""
        text = "Status: BLOCKED\nBlockers: some issue"
        result = parse_claim(text)
        self.assertIsNone(result['blockers'])

    def test_nl_non_path_backtick_tokens_ignored(self) -> None:
        """Backtick tokens without '/' or extension are not paths."""
        text = "`DONE` `foo` `bar` – Status: DONE"
        result = parse_claim(text)
        self.assertNotIn('DONE', result['files_changed'])
        self.assertNotIn('foo', result['files_changed'])
        self.assertNotIn('bar', result['files_changed'])


class TestMissingEmptyClaim(unittest.TestCase):
    """CRITICAL RULE: missing/unparseable claim → status=None, source='none'."""

    def test_empty_string(self) -> None:
        result = parse_claim('')
        self.assertIsNone(result['status'])
        self.assertEqual(result['files_changed'], [])
        self.assertEqual(result['source'], 'none')
        self.assertFalse(result['ran_tests'])

    def test_whitespace_only(self) -> None:
        result = parse_claim('   \n\t\n  ')
        self.assertIsNone(result['status'])
        self.assertEqual(result['source'], 'none')

    def test_no_status_no_paths(self) -> None:
        """Text with no status and no paths → source='none'."""
        text = "The work has been done thoroughly and completely."
        result = parse_claim(text)
        self.assertIsNone(result['status'])
        self.assertEqual(result['source'], 'none')

    def test_source_none_never_false_done(self) -> None:
        """Downstream safety: source='none' should block false_done in verdict."""
        # We test verdict.evaluate() separately; here just confirm source='none'
        # is surfaced correctly.
        result = parse_claim('')
        self.assertEqual(result['source'], 'none')

    def test_only_ran_tests_no_status(self) -> None:
        """ran_tests detected but no status → source='none' (cannot be false DONE)."""
        text = "I ran the unittest suite."
        result = parse_claim(text)
        self.assertIsNone(result['status'])
        self.assertEqual(result['source'], 'none')
        self.assertTrue(result['ran_tests'])

    def test_returns_raw_field(self) -> None:
        """parse_claim always returns a 'raw' key."""
        result = parse_claim('hello')
        self.assertIn('raw', result)


if __name__ == '__main__':
    unittest.main()
