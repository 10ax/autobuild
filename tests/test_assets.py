import unittest
from pathlib import Path
from autobuild.spec import parse_spec, validate_spec

ROOT = Path(__file__).resolve().parent.parent

class TestAssets(unittest.TestCase):
    def test_claude_md_names_the_lifecycle(self):
        text = (ROOT / "CLAUDE.md").read_text()
        for marker in ["tokensave init", "tokensave install", "docs/spec.md",
                       "test-driven-development", "verification-before-completion",
                       "PROGRESS.md", "needs-review"]:
            self.assertIn(marker, text, f"CLAUDE.md missing: {marker}")

    def test_example_is_a_valid_brief(self):
        doc = parse_spec((ROOT / "examples" / "0001-example.md").read_text())
        self.assertEqual(validate_spec(doc, level="brief"), [])

    def test_brief_template_is_valid_brief(self):
        doc = parse_spec((ROOT / "templates" / "brief.template.md").read_text())
        self.assertEqual(validate_spec(doc, level="brief"), [])
