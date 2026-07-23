import unittest
from autobuild.spec import parse_spec, validate_spec

VALID_BRIEF = '''+++
spec_version = "1.0"
slug = "pomodoro-cli"
title = "Pomodoro Timer CLI"
tier = "script"
priority = 3
status = "pending"
+++
## Intent
A CLI Pomodoro timer.

## Acceptance Criteria
A1. `pomo start 25` runs a 25-minute timer.
'''

class TestSpec(unittest.TestCase):
    def test_parse_front_matter_and_sections(self):
        doc = parse_spec(VALID_BRIEF)
        self.assertEqual(doc.meta["slug"], "pomodoro-cli")
        self.assertIn("Intent", doc.sections)
        self.assertTrue(doc.sections["Acceptance Criteria"].startswith("A1."))

    def test_valid_brief_passes_brief_level(self):
        self.assertEqual(validate_spec(parse_spec(VALID_BRIEF), level="brief"), [])

    def test_brief_missing_full_sections_fails_spec_level(self):
        errs = validate_spec(parse_spec(VALID_BRIEF), level="spec")
        self.assertTrue(any("Domain Model" in e for e in errs))

    def test_bad_tier_and_placeholder_flagged(self):
        bad = VALID_BRIEF.replace('tier = "script"', 'tier = "huge"') \
                         .replace("A CLI Pomodoro timer.", "TBD")
        errs = validate_spec(parse_spec(bad), level="brief")
        self.assertTrue(any("tier" in e for e in errs))
        self.assertTrue(any("Intent" in e for e in errs))
