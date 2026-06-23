"""
tests/test_claim.py — Unit tests for attest.claim.parse_claim()

Covers:
  - Tier 1 (## Handoff block): happy path, files_changed "none", ran_tests, blockers
  - Tier 2 (NL fallback — anchored): Status: at start-of-line or after separator
  - Tier 2 positive: explicit files-key extraction (anchored only)
  - Tier 2 safety/negative: prose paths, backtick paths, verb-paths, bare DONE
    MUST NOT produce file claims (BUG-4 regression suite)
  - Edge cases: empty text, no claim found, partial block, source rules
"""
import json
import os
import unittest

from attest.claim import parse_claim


class TestHandoffParsing(unittest.TestCase):
    """Tier 1: ## Handoff block parsing. ALL tests here must remain passing."""

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
    """Tier 2: natural-language fallback — status detection and misc."""

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

    def test_nl_bare_done_not_detected(self) -> None:
        """Bare 'DONE' token without 'Status:' prefix must NOT set status (BUG-4)."""
        text = "All changes are complete. DONE\n"
        result = parse_claim(text)
        # No anchored Status: → status must be None, source must be 'none'
        self.assertIsNone(result['status'])
        self.assertEqual(result['source'], 'none')

    def test_nl_backtick_path_not_extracted(self) -> None:
        """Backtick-wrapped paths in prose must NOT appear in files_changed (BUG-4)."""
        text = "Updated `src/utils.py` and `lib/helpers.ts`.\nStatus: DONE\n"
        result = parse_claim(text)
        # Status is detected (on its own line), but paths must NOT be extracted
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], [])

    def test_nl_backtick_path_extension_not_extracted(self) -> None:
        """Backtick token with only extension (no slash) must NOT appear in files_changed."""
        text = "Edited `app.py`.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], [])

    def test_nl_verb_created_path_not_extracted(self) -> None:
        """'Created foo.py' in prose must NOT produce a file claim (BUG-4)."""
        text = "Created src/new_module.py for the feature.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], [])

    def test_nl_verb_modified_path_not_extracted(self) -> None:
        text = "Modified tests/test_core.py to cover edge cases.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], [])

    def test_nl_verb_added_inline_status_not_detected(self) -> None:
        """Inline 'Status: DONE' (not at start of line) must NOT set status."""
        text = "Added config/settings.json. Status: DONE\n"
        result = parse_claim(text)
        # Status: DONE is not at start of line; no anchored status → source='none'
        self.assertIsNone(result['status'])
        self.assertEqual(result['files_changed'], [])
        self.assertEqual(result['source'], 'none')

    def test_nl_verb_wrote_inline_status_not_detected(self) -> None:
        text = "Wrote bin/deploy.sh. Status: DONE"
        result = parse_claim(text)
        self.assertIsNone(result['status'])
        self.assertEqual(result['files_changed'], [])

    def test_nl_verb_updated_inline_status_not_detected(self) -> None:
        text = "Updated README.md. Status: DONE"
        result = parse_claim(text)
        self.assertIsNone(result['status'])
        self.assertEqual(result['files_changed'], [])

    def test_nl_no_files_from_prose_paths(self) -> None:
        """Files mentioned inline alongside verb are not extracted."""
        text = "Updated `src/a.py` and also updated src/a.py.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], [])

    def test_nl_ran_tests_detected(self) -> None:
        text = "I ran the tests and they passed.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertTrue(result['ran_tests'])

    def test_nl_tests_passed(self) -> None:
        text = "Tests passed.\nStatus: DONE\n"
        result = parse_claim(text)
        self.assertTrue(result['ran_tests'])

    def test_nl_blockers_not_parsed(self) -> None:
        """NL fallback never sets blockers (only Handoff block does)."""
        text = "Status: BLOCKED\nBlockers: some issue"
        result = parse_claim(text)
        self.assertIsNone(result['blockers'])

    def test_nl_non_path_backtick_tokens_ignored(self) -> None:
        """Backtick tokens are never extracted as files regardless of content."""
        text = "`DONE` `foo` `bar` – Status: DONE"
        result = parse_claim(text)
        # Status: DONE is not at start of line (preceded by backtick tokens and dash)
        # files_changed must be empty
        self.assertNotIn('DONE', result['files_changed'])
        self.assertNotIn('foo', result['files_changed'])
        self.assertNotIn('bar', result['files_changed'])


class TestNLAnchored(unittest.TestCase):
    """Tier 2 POSITIVE tests: anchored Status: and files-key extraction."""

    def test_status_and_files_on_separate_lines(self) -> None:
        """Status: DONE on one line, Files changed: on the next."""
        text = "Status: DONE\nFiles changed: a.py, b.py\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], ['a.py', 'b.py'])
        self.assertEqual(result['source'], 'nl')

    def test_files_changed_key_standalone_line(self) -> None:
        """A bare 'files_changed: x.py' line is extracted even without Status:."""
        text = "files_changed: x.py\n"
        result = parse_claim(text)
        self.assertIn('x.py', result['files_changed'])

    def test_status_and_files_separator_delimited(self) -> None:
        """Status: DONE / Files changed: a.txt — status detected, files NOT extracted.

        Separator anchoring was removed in BUG-4b hardening. Files after / are not
        extracted. Use multi-line format or ## Handoff block to have files verified.
        """
        text = "Status: DONE / Files changed: a.txt\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], [])

    def test_modified_files_key(self) -> None:
        """'Modified files:' key is recognized at start of line."""
        text = "Modified files: src/x.py, tests/y.py\n"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], ['src/x.py', 'tests/y.py'])

    def test_changed_files_key(self) -> None:
        """'Changed files:' key is recognized."""
        text = "Status: DONE\nChanged files: lib/a.ts\n"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], ['lib/a.ts'])

    def test_files_and_keyword_split(self) -> None:
        """'and' as separator in file list is handled."""
        text = "Status: DONE\nFiles changed: a.py and b.py\n"
        result = parse_claim(text)
        self.assertIn('a.py', result['files_changed'])
        self.assertIn('b.py', result['files_changed'])

    def test_files_changed_literal_none(self) -> None:
        text = "Status: DONE\nFiles changed: none\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], [])

    def test_status_after_separator_not_detected(self) -> None:
        """Status: after a semicolon is NOT detected (separator anchoring removed BUG-4b).

        Only start-of-line Status: is recognized; mid-line occurrences are ignored.
        """
        text = "Work complete; Status: DONE\n"
        result = parse_claim(text)
        self.assertIsNone(result['status'])
        self.assertEqual(result['source'], 'none')

    def test_files_after_separator_not_extracted(self) -> None:
        """files_changed after a semicolon is NOT extracted (separator anchoring removed BUG-4b).

        Even when Status: is at start-of-line, separator-delimited files are not extracted.
        Use multi-line format or ## Handoff block to have files verified.
        """
        text = "Status: DONE; files_changed: x.py\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['files_changed'], [])

    def test_leading_whitespace_status(self) -> None:
        """Leading whitespace before Status: is allowed."""
        text = "  Status: DONE\n"
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')

    def test_files_underscore_variant(self) -> None:
        """files_changed (underscore) key at start of line."""
        text = "files_changed: src/main.py, src/util.py\n"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], ['src/main.py', 'src/util.py'])


class TestNLSafety(unittest.TestCase):
    """Tier 2 NEGATIVE/SAFETY: prose must NEVER yield blockable false-done claims."""

    def test_regression_fixture_false_done(self) -> None:
        """BUG-4 regression: honest prose must not produce files_changed or blockable DONE.

        Fixture: real SubagentStop payload where an honest subagent created nothing
        and explained in prose why it couldn't comply with the handoff template.
        The prose contains backtick paths, 'files_changed: ghost.py' inside quotes,
        and 'status: DONE' inside backticks — none of these may become a claim.
        """
        fixture_path = os.path.join(
            os.path.dirname(__file__), '..', 'fixtures', 'subagent_stop_false_done.json'
        )
        with open(fixture_path) as f:
            d = json.load(f)
        result = parse_claim(d['last_assistant_message'])
        self.assertEqual(result['files_changed'], [])
        # Must not be a blockable DONE
        self.assertIsNone(result['status'])
        self.assertEqual(result['source'], 'none')

    def test_prose_with_backtick_path_no_claim(self) -> None:
        """'The bug is in `src/auth.py`' must yield no claimed files."""
        text = "I'm done; the bug is in `src/auth.py`"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])

    def test_prose_quoting_template_no_claim(self) -> None:
        """Quoting 'files_changed: ghost.py' and 'status: DONE' in prose must not extract them."""
        text = "The template asserts `files_changed: ghost.py` and `status: DONE`"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])
        self.assertIsNone(result['status'])

    def test_inline_separator_files_without_anchored_status(self) -> None:
        """'DONE / files_changed: ghost.py' in prose with no anchored Status: yields nothing."""
        text = 'so emitting a "DONE / files_changed: ghost.py" handoff would be inaccurate.'
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])
        self.assertIsNone(result['status'])

    def test_done_in_prose_not_status(self) -> None:
        """The word 'done' in prose without 'Status:' prefix must not set status."""
        text = "The task is done. I finished everything.\n"
        result = parse_claim(text)
        self.assertIsNone(result['status'])
        self.assertEqual(result['source'], 'none')

    def test_backtick_status_not_extracted(self) -> None:
        """`status: DONE` inside backticks in mid-sentence must not set status."""
        text = "The agent is supposed to emit `status: DONE` but did not.\n"
        result = parse_claim(text)
        self.assertIsNone(result['status'])

    def test_quoted_files_changed_in_prose(self) -> None:
        """'files_changed: x.py' quoted mid-sentence must not be extracted."""
        text = 'The output template includes "files_changed: x.py" but I did not create x.py.\n'
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])

    def test_word_preceded_files_key_not_extracted(self) -> None:
        """'myfiles_changed: x.py' must not match (word char before key)."""
        text = "myfiles_changed: x.py\nStatus: DONE\n"
        result = parse_claim(text)
        # 'myfiles_changed' starts with 'my' so SOL regex won't match (it starts with 'my')
        self.assertNotIn('x.py', result['files_changed'])


class TestNLSeparatorProseSafety(unittest.TestCase):
    """BUG-4b regression: separator-anchored Status: in ordinary prose must NEVER
    produce phantom files or block honest agents.

    All strings below previously triggered false-positive status or phantom file
    claims when separator anchoring (/ ; |) was active. After BUG-4b hardening
    (start-of-line ONLY), files_changed MUST be [] for all of them.

    Per the task spec: Status MAY be None or a value (we assert None since none of
    these have a SOL "Status:"), but files MUST be [] so they can never be blocked.
    """

    def test_prose_semicolon_status_done_no_files(self) -> None:
        """'status: DONE' after semicolon in prose -> status=None, files=[]."""
        text = "The previous implementation; status: DONE was broken."
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])
        self.assertIsNone(result['status'])

    def test_prose_pipe_status_done_no_files(self) -> None:
        """'Status: DONE' after pipe in prose -> status=None, files=[]."""
        text = "Applied grep to filter logs | Status: DONE is pending."
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])
        self.assertIsNone(result['status'])

    def test_prose_semicolon_status_blocked_no_files(self) -> None:
        """'Status: BLOCKED' after semicolon in prose -> status=None, files=[]."""
        text = "The design review stalled; Status: BLOCKED indefinitely..."
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])
        self.assertIsNone(result['status'])

    def test_prose_slash_status_done_no_files(self) -> None:
        """'Status: DONE' after slash in prose -> status=None, files=[]."""
        text = "Ran the tests / Status: DONE but there are warnings"
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])
        self.assertIsNone(result['status'])

    def test_block_causing_phantom_file_no_files(self) -> None:
        """Block-causing string: slash-separated Status AND files_changed -> files=[].

        Previously produced status=DONE AND files_changed=['refactor.py would be next']
        — a phantom file that would have caused a false block on an honest agent.
        After BUG-4b hardening, files MUST be [] regardless of status.
        """
        text = (
            "I considered the plan / Status: DONE / "
            "files_changed: refactor.py would be next."
        )
        result = parse_claim(text)
        self.assertEqual(result['files_changed'], [])

    def test_source_none_when_no_sol_status(self) -> None:
        """All four prose separator strings yield source='none' — not blockable."""
        strings = [
            "The previous implementation; status: DONE was broken.",
            "Applied grep to filter logs | Status: DONE is pending.",
            "The design review stalled; Status: BLOCKED indefinitely...",
            "Ran the tests / Status: DONE but there are warnings",
        ]
        for text in strings:
            with self.subTest(text=text[:50]):
                result = parse_claim(text)
                self.assertEqual(result['source'], 'none',
                                 f"Expected source='none' for: {text!r}")
                self.assertEqual(result['files_changed'], [],
                                 f"Expected no files for: {text!r}")


class TestCodeFenceBlindspot(unittest.TestCase):
    """Fix: code-fence lines inside ``` or ~~~ must NOT be parsed as claims (I1/I4)."""

    def test_status_inside_backtick_fence_yields_source_none(self) -> None:
        """Status: and files_changed: ONLY inside a ``` fence → source='none', no claim."""
        text = (
            "Here is an example block:\n"
            "```\n"
            "Status: DONE\n"
            "files_changed: phantom.py\n"
            "```\n"
            "That is what the output looks like.\n"
        )
        result = parse_claim(text)
        self.assertIsNone(result['status'],
                          "Status: inside a fence must not produce a parsed status")
        self.assertEqual(result['files_changed'], [],
                         "files_changed: inside a fence must not produce claimed files")
        self.assertEqual(result['source'], 'none',
                         "No real claim outside the fence → source='none'")

    def test_status_inside_tilde_fence_yields_source_none(self) -> None:
        """Status: inside a ~~~ fence must not be parsed."""
        text = (
            "~~~\n"
            "Status: DONE\n"
            "files_changed: ghost.py\n"
            "~~~\n"
        )
        result = parse_claim(text)
        self.assertIsNone(result['status'])
        self.assertEqual(result['files_changed'], [])
        self.assertEqual(result['source'], 'none')

    def test_real_status_after_fence_is_detected(self) -> None:
        """A Status: outside the fence (after closing ```) IS detected normally."""
        text = (
            "```\n"
            "Status: DONE\n"
            "files_changed: phantom.py\n"
            "```\n"
            "\n"
            "Status: DONE\n"
            "files_changed: real.py\n"
        )
        result = parse_claim(text)
        self.assertEqual(result['status'], 'DONE')
        self.assertIn('real.py', result['files_changed'])
        self.assertNotIn('phantom.py', result['files_changed'])

    def test_indented_fence_still_tracked(self) -> None:
        """An indented ``` fence line is still recognized as a fence boundary."""
        text = (
            "  ```\n"
            "  Status: DONE\n"
            "  files_changed: secret.py\n"
            "  ```\n"
        )
        result = parse_claim(text)
        self.assertIsNone(result['status'])
        self.assertEqual(result['files_changed'], [])


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
