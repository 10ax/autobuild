import tempfile, unittest
from pathlib import Path
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


DOC_BRIEF = '''+++
spec_version = "1.0"
slug = "autodoc-x"
title = "Autodoc: x"
tier = "docs"
mode = "document"
repo = "{repo}"
priority = 9
status = "pending"
+++
## Intent
Document the x repo.

## Acceptance Criteria
A1. The doc set exists and every code-map anchor resolves.
'''


def _git_repo():
    d = Path(tempfile.mkdtemp())
    (d / ".git").mkdir()
    return d


class TestDocumentBrief(unittest.TestCase):
    def test_valid_document_brief_passes(self):
        doc = parse_spec(DOC_BRIEF.format(repo=_git_repo()))
        self.assertEqual(validate_spec(doc, level="brief"), [])

    def test_document_brief_requires_repo(self):
        text = DOC_BRIEF.format(repo=_git_repo())
        text = "\n".join(l for l in text.splitlines() if not l.startswith("repo ="))
        errs = validate_spec(parse_spec(text), level="brief")
        self.assertTrue(any("repo" in e for e in errs), errs)

    def test_document_repo_must_exist(self):
        errs = validate_spec(parse_spec(DOC_BRIEF.format(repo="/nope/not/here")), level="brief")
        self.assertTrue(any("repo" in e for e in errs), errs)

    def test_document_repo_must_be_a_git_repo(self):
        plain = Path(tempfile.mkdtemp())          # a directory, but not a git repo
        errs = validate_spec(parse_spec(DOC_BRIEF.format(repo=plain)), level="brief")
        self.assertTrue(any("git" in e for e in errs), errs)

    def test_repo_key_expands_tilde(self):
        doc = parse_spec(DOC_BRIEF.format(repo="~"))   # $HOME exists but isn't a git repo
        errs = validate_spec(doc, level="brief")
        self.assertFalse(any("does not exist" in e for e in errs), errs)

    def test_unknown_mode_flagged(self):
        bad = DOC_BRIEF.format(repo=_git_repo()).replace('mode = "document"', 'mode = "wat"')
        errs = validate_spec(parse_spec(bad), level="brief")
        self.assertTrue(any("mode" in e for e in errs), errs)

    def test_build_brief_may_not_carry_repo(self):
        bad = VALID_BRIEF.replace('status = "pending"', 'status = "pending"\nrepo = "/tmp"')
        errs = validate_spec(parse_spec(bad), level="brief")
        self.assertTrue(any("repo" in e for e in errs), errs)

    def test_docs_tier_requires_document_mode(self):
        bad = VALID_BRIEF.replace('tier = "script"', 'tier = "docs"')
        errs = validate_spec(parse_spec(bad), level="brief")
        self.assertTrue(any("docs" in e for e in errs), errs)

    def test_legacy_brief_without_mode_still_valid(self):
        self.assertEqual(validate_spec(parse_spec(VALID_BRIEF), level="brief"), [])
