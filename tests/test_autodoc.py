# tests/test_autodoc.py — the docs lane's git plumbing and its verify gate.
# These tests drive REAL git in a tmpdir: the containment guarantees this lane sells
# (never touch source, never touch human prose) are only worth as much as git says.
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from autobuild.autodoc import (BEGIN, DOC_SET, END, AutodocError, commit_docs,
                               prepare_worktree, remove_worktree, verify_docs)

SRC = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(10))       # ~30 lines
HUMAN_README = "# repo\n\nHand-written prose that must survive untouched.\n"


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
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "initial")
    return d


def _plan(repo, slug="autodoc-x", date="2026-09-03"):
    return prepare_worktree(repo, slug, Path(tempfile.mkdtemp()) / "worktrees", date)


def _block(body: str) -> str:
    return f"{BEGIN}\n{body}\n{END}\n"


def _write_doc_set(wt: Path, anchors: int = 6, code_map_extra: str = "") -> None:
    """A doc set that should pass every gate."""
    (wt / "README.md").write_text(HUMAN_README + "\n" + _block("## Overview\nWhat it is."))
    (wt / "CLAUDE.md").write_text(_block("## Stack\nPython. See docs/CODE-MAP.md."))
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "WORKING-ON-THIS.md").write_text(_block("## Dev loop\nRun the tests."))
    rows = "\n".join(f"| capability {i} | `src/app.py:{i + 1}` (`f{i}`) | does {i} |"
                     for i in range(anchors))
    (wt / "docs" / "CODE-MAP.md").write_text(
        _block("| capability | location | note |\n|---|---|---|\n" + rows + code_map_extra))


class TestWorktree(unittest.TestCase):
    def test_prepare_creates_branch_and_worktree_at_head(self):
        repo = _repo()
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        plan = _plan(repo)
        self.assertTrue(plan.worktree.is_dir())
        self.assertEqual(plan.branch, "autodoc/2026-09-03")
        self.assertEqual(plan.base_sha, head)
        self.assertTrue((plan.worktree / "src" / "app.py").exists())
        # the user's own checkout is untouched
        self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")
        self.assertEqual(_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "master")

    def test_prepare_is_idempotent_and_reuses_branch(self):
        repo = _repo()
        plan = _plan(repo)
        _write_doc_set(plan.worktree)
        commit_docs(plan, "docs: autodoc")
        again = prepare_worktree(repo, plan.slug, plan.worktree.parent, "2026-09-03")
        self.assertEqual(again.branch, plan.branch)
        # reusing the worktree keeps the committed docs, so base_sha follows the branch tip
        self.assertEqual(again.base_sha,
                         _git(repo, "rev-parse", plan.branch).stdout.strip())
        self.assertTrue((again.worktree / "docs" / "CODE-MAP.md").exists())

    def test_reused_worktree_reports_the_branch_it_is_actually_on(self):
        # A red run leaves its worktree behind. Days later the retry must not claim a new
        # branch name while committing onto the old one — the ledger would be lying.
        repo = _repo()
        first = _plan(repo, date="2026-09-03")
        again = prepare_worktree(repo, first.slug, first.worktree.parent, "2026-09-10")
        self.assertEqual(again.branch, "autodoc/2026-09-03")
        self.assertEqual(_git(again.worktree, "rev-parse", "--abbrev-ref", "HEAD")
                         .stdout.strip(), again.branch)

    def test_prepare_rejects_a_foreign_directory_at_the_worktree_path(self):
        repo = _repo()
        wtdir = Path(tempfile.mkdtemp()) / "worktrees"
        (wtdir / "autodoc-x").mkdir(parents=True)
        (wtdir / "autodoc-x" / "junk.txt").write_text("not a worktree")
        with self.assertRaises(AutodocError):
            prepare_worktree(repo, "autodoc-x", wtdir, "2026-09-03")

    def test_remove_worktree_keeps_the_branch(self):
        repo = _repo()
        plan = _plan(repo)
        _write_doc_set(plan.worktree)
        commit_docs(plan, "docs: autodoc")
        remove_worktree(plan)
        self.assertFalse(plan.worktree.exists())
        files = _git(repo, "show", "--name-only", "--format=", plan.branch).stdout.split()
        self.assertIn("docs/CODE-MAP.md", files)


class TestVerify(unittest.TestCase):
    def test_valid_doc_set_is_green(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        self.assertEqual(verify_docs(plan), [])

    def test_missing_file_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / "docs" / "WORKING-ON-THIS.md").unlink()
        errs = verify_docs(plan)
        self.assertTrue(any("WORKING-ON-THIS" in e for e in errs), errs)

    def test_dangling_anchor_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        cm = plan.worktree / "docs" / "CODE-MAP.md"
        cm.write_text(cm.read_text().replace("src/app.py:2", "src/ghost.py:2"))
        errs = verify_docs(plan)
        self.assertTrue(any("ghost.py" in e for e in errs), errs)

    def test_anchor_past_end_of_file_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        cm = plan.worktree / "docs" / "CODE-MAP.md"
        cm.write_text(cm.read_text().replace("src/app.py:2", "src/app.py:9999"))
        errs = verify_docs(plan)
        self.assertTrue(any("9999" in e for e in errs), errs)

    def test_too_few_anchors_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree, anchors=2)
        errs = verify_docs(plan)
        self.assertTrue(any("anchor" in e for e in errs), errs)

    def test_modified_source_file_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / "src" / "app.py").write_text("# helpfully refactored\n" + SRC)
        errs = verify_docs(plan)
        self.assertTrue(any("src/app.py" in e for e in errs), errs)

    def test_deleted_source_file_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / "src" / "app.py").unlink()
        errs = verify_docs(plan)
        self.assertTrue(any("src/app.py" in e for e in errs), errs)

    def test_human_prose_outside_markers_must_be_intact(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        r = plan.worktree / "README.md"
        r.write_text(r.read_text().replace("Hand-written prose", "Rewritten prose"))
        errs = verify_docs(plan)
        self.assertTrue(any("README.md" in e for e in errs), errs)

    def test_whitespace_only_change_outside_markers_is_tolerated(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        r = plan.worktree / "README.md"
        r.write_text(r.read_text().replace("untouched.\n", "untouched.  \n\n"))
        self.assertEqual(verify_docs(plan), [])

    def test_placeholder_line_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        c = plan.worktree / "CLAUDE.md"
        c.write_text(c.read_text().replace("Python. See docs/CODE-MAP.md.", "TBD"))
        errs = verify_docs(plan)
        self.assertTrue(any("placeholder" in e for e in errs), errs)

    def test_empty_file_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / "CLAUDE.md").write_text("   \n")
        errs = verify_docs(plan)
        self.assertTrue(any("CLAUDE.md" in e for e in errs), errs)

    def test_oversized_code_map_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree, code_map_extra="\n" + "\n".join(f"note {i}"
                                                                     for i in range(300)))
        errs = verify_docs(plan)
        self.assertTrue(any("250" in e or "too long" in e for e in errs), errs)

    def test_ephemeral_tool_output_does_not_break_containment(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / ".tokensave").mkdir()
        (plan.worktree / ".tokensave" / "tokensave.db").write_text("index")
        (plan.worktree / "__pycache__").mkdir()
        (plan.worktree / "__pycache__" / "x.pyc").write_text("x")
        self.assertEqual(verify_docs(plan), [])

    def test_plugin_telemetry_written_into_the_worktree_is_ignored(self):
        # The operator's Claude Code plugins write into the session cwd, i.e. the worktree.
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / "observability").mkdir()
        (plan.worktree / "observability" / "affordance-invocations.json").write_text("[]")
        self.assertEqual(verify_docs(plan), [])

    def test_a_real_new_file_in_that_directory_is_still_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / "observability").mkdir()
        (plan.worktree / "observability" / "notes.md").write_text("invented")
        errs = verify_docs(plan)
        self.assertTrue(any("observability/notes.md" in e for e in errs), errs)

    def test_stray_new_file_is_red(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / "NOTES.md").write_text("a file nobody asked for")
        errs = verify_docs(plan)
        self.assertTrue(any("NOTES.md" in e for e in errs), errs)


class TestCommit(unittest.TestCase):
    def test_commits_only_allowed_paths(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        (plan.worktree / ".tokensave").mkdir()
        (plan.worktree / ".tokensave" / "db").write_text("x")
        self.assertTrue(commit_docs(plan, "docs: autodoc"))
        files = sorted(_git(plan.repo, "show", "--name-only", "--format=",
                            plan.branch).stdout.split())
        self.assertEqual(files, sorted(DOC_SET))
        self.assertEqual(_git(plan.worktree, "status", "--porcelain", "--untracked-files=no")
                         .stdout, "")

    def test_commit_uses_the_target_repos_identity(self):
        plan = _plan(_repo())
        _write_doc_set(plan.worktree)
        env = dict(os.environ)
        os.environ.update({"GIT_AUTHOR_NAME": "10ax", "GIT_AUTHOR_EMAIL": "10ax@example.com",
                           "GIT_COMMITTER_NAME": "10ax",
                           "GIT_COMMITTER_EMAIL": "10ax@example.com"})
        try:
            commit_docs(plan, "docs: autodoc")
        finally:
            os.environ.clear()
            os.environ.update(env)
        who = _git(plan.repo, "log", "-1", "--format=%an <%ae> %cn", plan.branch).stdout
        self.assertIn("owner@example.com", who)
        self.assertNotIn("10ax", who)

    def test_commit_returns_false_when_there_is_nothing_to_commit(self):
        plan = _plan(_repo())
        self.assertFalse(commit_docs(plan, "docs: autodoc"))

    def test_wip_commit_of_a_partial_doc_set(self):
        plan = _plan(_repo())
        (plan.worktree / "CLAUDE.md").write_text(_block("## Stack\nhalf-finished"))
        self.assertTrue(commit_docs(plan, "docs: autodoc WIP", wip=True))
        files = _git(plan.repo, "show", "--name-only", "--format=", plan.branch).stdout.split()
        self.assertEqual(files, ["CLAUDE.md"])
