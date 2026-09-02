# tests/test_seed.py — who gets documented is a safety question, so the discovery filter
# gets a test: a clone of someone else's repo carries THEIR Claude commits, not yours.
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "seed", Path(__file__).resolve().parent.parent / "bin" / "seed-autodoc-briefs.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)

TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"


def _repo(home: Path, name: str, email: str, trailer: bool) -> Path:
    d = home / name
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", email], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "someone"], check=True)
    (d / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    msg = "feat: thing" + (f"\n\n{TRAILER}" if trailer else "")
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", msg], check=True)
    return d


class TestDiscover(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        cls.mine = _repo(cls.home, "mine", "did-69@systemceramics.com", True)
        cls.theirs = _repo(cls.home, "theirs", "stranger@example.com", True)
        cls.plain = _repo(cls.home, "plain", "did-69@systemceramics.com", False)
        cls.vendored = _repo(cls.home, "app/node_modules/pkg", "did-69@systemceramics.com", True)
        cls.keep, cls.skip = seed.discover(cls.home)

    def test_my_repo_with_claude_commits_is_proposed(self):
        self.assertIn(self.mine, [r["path"] for r in self.keep])

    def test_someone_elses_repo_is_not_proposed_and_says_why(self):
        self.assertNotIn(self.theirs, [r["path"] for r in self.keep])
        row = [r for r in self.skip if r["path"] == self.theirs]
        self.assertEqual(len(row), 1)
        self.assertIn("none of yours", row[0]["why"])

    def test_repo_without_claude_commits_is_silent(self):
        self.assertNotIn(self.plain, [r["path"] for r in self.keep])
        self.assertNotIn(self.plain, [r["path"] for r in self.skip])

    def test_vendored_repos_are_never_scanned(self):
        every = [r["path"] for r in self.keep + self.skip]
        self.assertNotIn(self.vendored, every)


class TestLoadTargets(unittest.TestCase):
    def _targets(self, body: str):
        f = Path(tempfile.mkdtemp()) / "t.toml"
        f.write_text(body)
        seed.TARGETS = f
        return f

    def tearDown(self):
        seed.TARGETS = seed.ROOT / "config" / "autodoc-targets.toml"

    def test_real_config_is_valid(self):
        # local content: a fresh clone has only the example template
        if not (seed.ROOT / "config" / "autodoc-targets.toml").exists():
            self.skipTest("no local target list")
        self.assertGreaterEqual(len(seed.load_targets()), 1)

    def test_duplicate_slug_is_fatal(self):
        d = Path(tempfile.mkdtemp())
        (d / ".git").mkdir()
        self._targets(f'''
[[target]]
path = "{d}"
slug = "dup"
priority = 1
[[target]]
path = "{d}"
slug = "dup"
priority = 2
''')
        with self.assertRaises(SystemExit):
            seed.load_targets()

    def test_non_git_path_is_fatal(self):
        self._targets(f'''
[[target]]
path = "{Path(tempfile.mkdtemp())}"
slug = "nope"
priority = 1
''')
        with self.assertRaises(SystemExit):
            seed.load_targets()
