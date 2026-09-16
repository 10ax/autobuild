# tests/test_improve.py — the quality lane's gate.
# Unlike the docs lane, this lane is ALLOWED to write tests, CI and config into a repo the
# user works in every day, so the safety question shifts: did it stay inside the paths the
# brief allowed, did the verify commands really pass, did the human prose survive? Every
# one of those is asserted against real git in a tmpdir, never trusted.
import subprocess
import tempfile
import unittest
from pathlib import Path

from autobuild.autodoc import BEGIN, END, prepare_worktree
from autobuild.improve import (commit_improve, prepare_worktree as prepare_quality,
                               verify_improve)

SRC = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(5))
HUMAN_README = "# repo\n\nHand-written prose that must survive untouched.\n"
OLD_DOC = "# old\n\nAn old note.\n"


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], check=check,
                          capture_output=True, text=True)


def _repo() -> Path:
    d = Path(tempfile.mkdtemp()) / "repo"
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(d)], check=True)
    _git(d, "config", "user.name", "Repo Owner")
    _git(d, "config", "user.email", "owner@example.com")
    (d / "src").mkdir()
    (d / "src" / "app.py").write_text(SRC)
    (d / "README.md").write_text(HUMAN_README)
    (d / "docs").mkdir()
    (d / "docs" / "OLD.md").write_text(OLD_DOC)
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "initial")
    return d


def _plan(repo, slug="quality-x", date="2026-09-15"):
    return prepare_quality(repo, slug, Path(tempfile.mkdtemp()) / "worktrees", date)


def _block(body: str) -> str:
    return f"{BEGIN}\n{body}\n{END}\n"


ALLOW = ["README.md", "CLAUDE.md", "docs/**", "tests/**", ".github/**", ".claude/skills/**"]


class _Runner:
    """A subprocess.run stand-in for the verify commands only: git calls pass through to the
    real thing, shell commands are answered from a table and recorded."""

    def __init__(self, answers=None):
        self.answers, self.calls = dict(answers or {}), []

    def __call__(self, argv, **kw):
        if isinstance(argv, list) and argv and argv[0] == "git":
            return subprocess.run(argv, **kw)
        self.calls.append((argv, kw))
        rc, out = self.answers.get(argv, (0, ""))
        if rc == "timeout":
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def _verify(plan, runner=None, which=lambda name: None, **kw):
    args = {"allow": ALLOW, "verify": ["true"], "require": [], "rewrite": []}
    args.update(kw)
    return verify_improve(plan, runner=runner or _Runner(), which=which, **args)


class TestWorktreeBranch(unittest.TestCase):
    def test_quality_lane_branches_under_quality_prefix(self):
        plan = _plan(_repo())
        self.assertEqual(plan.branch, "quality/2026-09-15")
        self.assertTrue(plan.worktree.is_dir())

    def test_autodoc_prefix_is_still_the_default(self):
        repo = _repo()
        plan = prepare_worktree(repo, "autodoc-x", Path(tempfile.mkdtemp()) / "wt", "2026-09-15")
        self.assertEqual(plan.branch, "autodoc/2026-09-15")


class TestContainment(unittest.TestCase):
    def test_changes_inside_allow_pass(self):
        plan = _plan(_repo())
        (plan.worktree / "tests").mkdir()
        (plan.worktree / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n")
        (plan.worktree / "README.md").write_text(HUMAN_README + "\n" + _block("## Quickstart\nRun it."))
        self.assertEqual(_verify(plan), [])

    def test_new_file_outside_allow_fails(self):
        plan = _plan(_repo())
        (plan.worktree / "src" / "other.py").write_text("x = 1\n")
        errs = _verify(plan)
        self.assertTrue(any("outside" in e and "src/other.py" in e for e in errs), errs)

    def test_modified_source_outside_allow_fails(self):
        plan = _plan(_repo())
        (plan.worktree / "src" / "app.py").write_text(SRC + "\ndef extra():\n    return 1\n")
        errs = _verify(plan)
        self.assertTrue(any("outside" in e and "src/app.py" in e for e in errs), errs)

    def test_source_change_passes_when_the_brief_allows_that_path(self):
        plan = _plan(_repo())
        (plan.worktree / "src" / "app.py").write_text(SRC + "\ndef extra():\n    return 1\n")
        self.assertEqual(_verify(plan, allow=ALLOW + ["src/**"]), [])

    def test_deletion_outside_allow_fails(self):
        plan = _plan(_repo())
        (plan.worktree / "src" / "app.py").unlink()
        errs = _verify(plan)
        self.assertTrue(any("deleted" in e and "src/app.py" in e for e in errs), errs)

    def test_deletion_inside_allow_passes(self):
        plan = _plan(_repo())
        (plan.worktree / "docs" / "OLD.md").unlink()
        self.assertEqual(_verify(plan), [])

    def test_tool_leftovers_are_ignored(self):
        plan = _plan(_repo())
        (plan.worktree / ".venv" / "bin").mkdir(parents=True)
        (plan.worktree / ".venv" / "bin" / "pytest").write_text("#!/bin/sh\n")
        (plan.worktree / "node_modules").mkdir()
        (plan.worktree / "node_modules" / "x.js").write_text("")
        (plan.worktree / ".tokensave").mkdir()
        (plan.worktree / ".tokensave" / "db").write_text("")
        self.assertEqual(_verify(plan), [])


class TestRequire(unittest.TestCase):
    def test_missing_required_file_fails(self):
        plan = _plan(_repo())
        errs = _verify(plan, require=["docs/TROUBLESHOOTING.md"])
        self.assertTrue(any("require" in e and "TROUBLESHOOTING" in e for e in errs), errs)

    def test_required_glob_satisfied_by_one_match(self):
        plan = _plan(_repo())
        (plan.worktree / ".claude" / "skills" / "release").mkdir(parents=True)
        (plan.worktree / ".claude" / "skills" / "release" / "SKILL.md").write_text(
            _block("---\nname: release\n---\nHow to release."))
        self.assertEqual(_verify(plan, require=[".claude/skills/*/SKILL.md"]), [])

    def test_empty_required_file_fails(self):
        plan = _plan(_repo())
        (plan.worktree / "docs" / "TROUBLESHOOTING.md").write_text("")
        errs = _verify(plan, require=["docs/TROUBLESHOOTING.md"])
        self.assertTrue(any("empty" in e for e in errs), errs)


class TestProse(unittest.TestCase):
    def test_placeholder_line_in_a_written_doc_fails(self):
        plan = _plan(_repo())
        (plan.worktree / "docs" / "TROUBLESHOOTING.md").write_text(
            _block("## Symptom\nTBD\n"))
        errs = _verify(plan)
        self.assertTrue(any("placeholder" in e for e in errs), errs)

    def test_human_text_changed_outside_markers_fails(self):
        plan = _plan(_repo())
        (plan.worktree / "README.md").write_text("# repo\n\nRewritten by the agent.\n")
        errs = _verify(plan)
        self.assertTrue(any("human text" in e and "README.md" in e for e in errs), errs)

    def test_rewrite_lets_a_listed_file_be_replaced(self):
        plan = _plan(_repo())
        (plan.worktree / "README.md").write_text(_block("# repo\n\nRewritten by the agent."))
        self.assertEqual(_verify(plan, rewrite=["README.md"]), [])

    def test_untouched_docs_with_todos_are_not_scanned(self):
        repo = _repo()
        (repo / "docs" / "PLAN.md").write_text("# plan\n\nTODO later\n")
        _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "plan")
        plan = _plan(repo)
        self.assertEqual(_verify(plan), [])


class TestVerifyCommands(unittest.TestCase):
    def test_commands_run_in_the_worktree_through_a_shell(self):
        plan = _plan(_repo())
        runner = _Runner()
        self.assertEqual(_verify(plan, runner=runner, verify=["pytest -q", "ruff check ."]), [])
        cmds = [c[0] for c in runner.calls]
        self.assertEqual(cmds, ["pytest -q", "ruff check ."])
        for _, kw in runner.calls:
            self.assertTrue(kw.get("shell"))
            self.assertEqual(Path(kw["cwd"]), plan.worktree)
            self.assertIn("timeout", kw)

    def test_failing_command_reports_the_command_and_its_output_tail(self):
        plan = _plan(_repo())
        runner = _Runner({"pytest -q": (1, "x" * 1000 + "FAILED tests/test_a.py::test_b")})
        errs = _verify(plan, runner=runner, verify=["pytest -q", "ruff check ."])
        self.assertEqual(len(errs), 1)
        self.assertIn("pytest -q", errs[0])
        self.assertIn("FAILED tests/test_a.py::test_b", errs[0])
        self.assertLess(len(errs[0]), 600)
        # a red command stops the chain: later commands are not run
        self.assertEqual([c[0] for c in runner.calls], ["pytest -q"])

    def test_hung_command_is_an_error_not_a_crash(self):
        plan = _plan(_repo())
        runner = _Runner({"pytest -q": ("timeout", "")})
        errs = _verify(plan, runner=runner, verify=["pytest -q"])
        self.assertTrue(any("timed out" in e for e in errs), errs)


class TestLinters(unittest.TestCase):
    def test_missing_linters_are_skipped_silently(self):
        plan = _plan(_repo())
        (plan.worktree / ".github" / "workflows").mkdir(parents=True)
        (plan.worktree / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
        runner = _Runner()
        self.assertEqual(_verify(plan, runner=runner, which=lambda n: None, verify=[]), [])
        self.assertEqual(runner.calls, [])

    def test_actionlint_runs_on_workflows_and_its_failure_is_reported(self):
        plan = _plan(_repo())
        (plan.worktree / ".github" / "workflows").mkdir(parents=True)
        (plan.worktree / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
        which = lambda n: "/usr/bin/actionlint" if n == "actionlint" else None
        runner = _ListRunner()
        self.assertEqual(_verify(plan, runner=runner, which=which), [])
        lint = [c for c in runner.calls if isinstance(c[0], list) and c[0][0] == "/usr/bin/actionlint"]
        self.assertEqual(len(lint), 1)
        self.assertIn(".github/workflows/ci.yml", lint[0][0])
        errs = _verify(plan, runner=_ListRunner({"actionlint": (1, "ci.yml:3: bad")}), which=which)
        self.assertTrue(any("actionlint" in e and "ci.yml:3: bad" in e for e in errs), errs)

    def test_actionlint_is_not_run_when_no_workflow_changed(self):
        plan = _plan(_repo())
        (plan.worktree / "docs" / "X.md").write_text(_block("x"))
        runner = _ListRunner()
        which = lambda n: "/usr/bin/actionlint" if n == "actionlint" else None
        self.assertEqual(_verify(plan, runner=runner, which=which, verify=[]), [])
        self.assertEqual(runner.calls, [])

    def test_gitleaks_scans_the_diff_and_its_hit_is_reported(self):
        plan = _plan(_repo())
        (plan.worktree / "docs" / "NOTES.md").write_text(_block("token: AKIA..."))
        which = lambda n: "/usr/bin/gitleaks" if n == "gitleaks" else None
        runner = _ListRunner({"gitleaks": (1, "Finding: AKIA...")})
        errs = _verify(plan, runner=runner, which=which)
        self.assertTrue(any("gitleaks" in e and "AKIA" in e for e in errs), errs)
        scan = [c for c in runner.calls if isinstance(c[0], list) and "gitleaks" in c[0][0]]
        self.assertEqual(len(scan), 1)
        self.assertIn("token: AKIA...", scan[0][1].get("input", ""))   # the diff went in on stdin
        self.assertEqual(_verify(plan, runner=_ListRunner({"gitleaks": (0, "")}), which=which), [])


class _ListRunner(_Runner):
    """Answers list-argv tool calls by the tool's basename (actionlint, gitleaks)."""

    def __call__(self, argv, **kw):
        if isinstance(argv, list) and argv and argv[0] == "git":
            return subprocess.run(argv, **kw)
        self.calls.append((argv, kw))
        key = Path(argv[0]).name if isinstance(argv, list) else argv
        rc, out = self.answers.get(key, (0, ""))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


class TestCommit(unittest.TestCase):
    def test_commits_only_allowed_changes_with_the_repo_identity(self):
        repo = _repo()
        plan = _plan(repo)
        (plan.worktree / "tests").mkdir()
        (plan.worktree / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n")
        (plan.worktree / "docs" / "OLD.md").unlink()
        (plan.worktree / "src" / "stray.py").write_text("x = 1\n")     # not allowed: left behind
        self.assertTrue(commit_improve(plan, "chore(quality): tests", allow=ALLOW))
        files = _git(repo, "show", "--stat", "--format=%ae", plan.branch).stdout
        self.assertIn("owner@example.com", files.splitlines()[0])
        self.assertIn("tests/test_app.py", files)
        self.assertIn("docs/OLD.md", files)
        self.assertNotIn("stray.py", files)
        self.assertTrue((plan.worktree / "src" / "stray.py").exists())
        # the user's checkout never moved
        self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")
        self.assertEqual(_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "master")

    def test_nothing_to_commit_returns_false(self):
        plan = _plan(_repo())
        self.assertFalse(commit_improve(plan, "chore(quality): nothing", allow=ALLOW))

    def test_wip_commit_carries_whatever_allowed_paths_changed(self):
        plan = _plan(_repo())
        (plan.worktree / "docs" / "TROUBLESHOOTING.md").write_text(_block("half done"))
        self.assertTrue(commit_improve(plan, "WIP", allow=ALLOW, wip=True))
        self.assertIn("WIP", _git(plan.worktree, "log", "-1", "--format=%s").stdout)


if __name__ == "__main__":
    unittest.main()
