# tests/test_seed_improve.py — the quality lane's brief generator.
# A brief the daemon rejects is a night wasted, so the generator's output is round-tripped
# through the REAL validator rather than eyeballed, and the sandbox drop-in is checked to
# actually name the repos the lane will write in.
import importlib.util
import tempfile
import unittest
from pathlib import Path

from autobuild.spec import parse_spec, validate_spec

_spec = importlib.util.spec_from_file_location(
    "seed_improve", Path(__file__).resolve().parent.parent / "bin" / "seed-improve-briefs.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def _repo(name="repo") -> Path:
    d = Path(tempfile.mkdtemp()) / name
    (d / ".git").mkdir(parents=True)
    return d


def _target(path, **kw):
    t = {"path": str(path), "slug": "quality-x", "priority": 50,
         "allow": ["README.md", "docs/**", "tests/**"],
         "require": ["docs/TROUBLESHOOTING.md"],
         "verify": ["pytest -q"],
         "note": "A repo that does a thing.",
         "traps": ["yt-dlp goes stale", "the hook is missing in a fresh clone"]}
    t.update(kw)
    return t


class TestRender(unittest.TestCase):
    def test_rendered_brief_passes_the_real_validator(self):
        text = seed.render(_target(_repo()), today="2026-09-16", root=Path("/home/x/autobuild"))
        self.assertEqual(validate_spec(parse_spec(text), level="brief"), [])

    def test_front_matter_carries_the_contract(self):
        meta = parse_spec(seed.render(_target(_repo()), today="2026-09-16",
                                      root=Path("/r"))).meta
        self.assertEqual(meta["mode"], "improve")
        self.assertEqual(meta["tier"], "quality")
        self.assertEqual(meta["model"], "opus")
        self.assertEqual(meta["status"], "needs-review")   # parked until a human flips it
        self.assertEqual(meta["allow"], ["README.md", "docs/**", "tests/**"])
        self.assertEqual(meta["verify"], ["pytest -q"])
        self.assertEqual(meta["rewrite"], [])

    def test_traps_reach_the_agent_in_the_intent(self):
        doc = parse_spec(seed.render(_target(_repo()), today="2026-09-16", root=Path("/r")))
        self.assertIn("yt-dlp goes stale", doc.sections["Intent"])
        self.assertIn("A repo that does a thing.", doc.sections["Intent"])

    def test_acceptance_criteria_name_the_verify_commands(self):
        doc = parse_spec(seed.render(_target(_repo(), verify=["pytest -q", "ruff check ."]),
                                     today="2026-09-16", root=Path("/r")))
        self.assertIn("pytest -q", doc.sections["Acceptance Criteria"])
        self.assertIn("ruff check .", doc.sections["Acceptance Criteria"])

    def test_rewrite_is_explained_when_present(self):
        doc = parse_spec(seed.render(_target(_repo(), rewrite=["README.md"]),
                                     today="2026-09-16", root=Path("/r")))
        self.assertIn("README.md", doc.sections["Acceptance Criteria"])


class TestValidation(unittest.TestCase):
    def test_missing_key_is_refused(self):
        bad = _target(_repo())
        del bad["verify"]
        with self.assertRaises(SystemExit):
            seed.check_targets([bad])

    def test_non_git_path_is_refused(self):
        plain = Path(tempfile.mkdtemp())
        with self.assertRaises(SystemExit):
            seed.check_targets([_target(plain)])

    def test_duplicate_slug_is_refused(self):
        a, b = _repo("a"), _repo("b")
        with self.assertRaises(SystemExit):
            seed.check_targets([_target(a), _target(b)])


class TestSystemd(unittest.TestCase):
    def test_dropin_names_the_repos_and_the_uv_cache(self):
        home = Path(tempfile.mkdtemp())
        repo = home / "Personal" / "code" / "thing"
        (repo / ".git").mkdir(parents=True)
        out = seed.systemd_dropin([_target(repo)], home=home)
        self.assertIn("ReadWritePaths=", out)
        self.assertIn("-%h/Personal/code/thing", out)
        # uv puts downloaded interpreters here; without it a `uv venv --python 3.13` in the
        # worktree dies against ProtectSystem=strict
        self.assertIn("uv", out)

    def test_paths_outside_home_are_absolute(self):
        home = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp()) / "elsewhere"
        (outside / ".git").mkdir(parents=True)
        out = seed.systemd_dropin([_target(outside)], home=home)
        self.assertIn(f"-{outside}", out)


if __name__ == "__main__":
    unittest.main()
