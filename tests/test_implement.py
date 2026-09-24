# tests/test_implement.py — the implement lane.
# This is the only lane allowed to change what the code does, so the questions it has to
# answer are different from the other two. The docs lane proves a fixed file list; the
# quality lane proves behaviour did NOT change. Here the guarantee is "a plan a human
# approved was carried out inside the lines the brief drew" — so the plan has to resolve
# before the run starts, the branch has to be its own, and the gate has to be the very same
# one the quality lane is judged by rather than a softer copy of it.
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from autobuild import implement as _implement
from autobuild import improve as _improve
from autobuild.autodoc import AutodocPlan
from autobuild.backlog import scan_backlog
from autobuild.build import BuildResult, _lane_prompt
from autobuild.config import Config
from autobuild.implement import prepare_worktree as prepare_implement, verify_implement
from autobuild.ledger import GovernorState, read_ledger
from autobuild.spec import parse_spec, validate_spec

TZ = ZoneInfo("Europe/Rome")
NIGHT = datetime(2026, 7, 23, 23, tzinfo=TZ)

PLAN_REL = "docs/plans/2026-09-24-thing.md"
PLAN_BODY = "# Thing Implementation Plan\n\n### Task 1: do the thing\n\n- [ ] Step 1: test\n"

BRIEF = '''+++
spec_version = "1.0"
slug = "implement-target"
title = "Implement: the thing"
tier = "service"
mode = "implement"
repo = "{repo}"
plan = "{plan}"
priority = 9
status = "pending"
allow = ["app/**", "tests/**", "docs/**"]
require = ["tests/test_*.py"]
rewrite = ["docs/REFERENCE.md"]
verify = [".venv/bin/pytest -q"]
+++
## Intent
Carry out the plan, tasks 1 to 7.

## Acceptance Criteria
A1. The verify commands pass in the worktree.
'''


def _target_repo(with_plan: bool = True, plan_body: str = PLAN_BODY) -> Path:
    """A repo shaped just enough for validate_spec: a .git marker and the plan file."""
    d = Path(tempfile.mkdtemp()) / "target"
    (d / ".git").mkdir(parents=True)
    if with_plan:
        plan = d / PLAN_REL
        plan.parent.mkdir(parents=True)
        plan.write_text(plan_body)
    return d


def _brief(repo: Path) -> str:
    return BRIEF.format(repo=repo, plan=PLAN_REL)


def _errors(text: str) -> list[str]:
    return validate_spec(parse_spec(text), level="brief")


# ---- validation: the plan resolves before the night starts ---------------------------

class TestImplementBrief(unittest.TestCase):
    def test_a_well_formed_implement_brief_validates(self):
        self.assertEqual(_errors(_brief(_target_repo())), [])

    def test_a_brief_with_no_plan_is_rejected(self):
        text = "\n".join(l for l in _brief(_target_repo()).splitlines()
                         if not l.startswith("plan ="))
        self.assertIn("mode=implement requires a plan path in the front-matter",
                      _errors(text))

    def test_a_plan_that_is_not_in_the_repo_is_rejected(self):
        """Catching this at validation costs a second; catching it at 03:00 costs a night."""
        errs = _errors(_brief(_target_repo(with_plan=False)))
        self.assertIn(f"plan does not exist in the repo: {PLAN_REL}", errs)

    def test_an_empty_plan_file_is_rejected(self):
        errs = _errors(_brief(_target_repo(plan_body="   \n\n")))
        self.assertIn(f"plan is empty: {PLAN_REL}", errs)

    def test_a_plan_key_on_another_lane_is_rejected(self):
        text = _brief(_target_repo()).replace('mode = "implement"', 'mode = "document"')
        self.assertIn("plan is only used with mode=implement", _errors(text))

    def test_implement_still_needs_a_contract(self):
        text = "\n".join(l for l in _brief(_target_repo()).splitlines()
                         if not l.startswith(("allow =", "verify =")))
        errs = _errors(text)
        self.assertIn("mode=implement requires a non-empty allow list", errs)
        self.assertIn("mode=implement requires a non-empty verify list", errs)

    def test_the_quality_tier_is_still_reserved_for_the_improve_lane(self):
        text = _brief(_target_repo()).replace('tier = "service"', 'tier = "quality"')
        self.assertIn('tier "quality" requires mode = "improve"', _errors(text))


# ---- the lane's own branch, and the gate it shares -----------------------------------

def _real_repo() -> Path:
    d = Path(tempfile.mkdtemp()) / "repo"
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(d)], check=True)
    for k, v in (("user.name", "Repo Owner"), ("user.email", "owner@example.com")):
        subprocess.run(["git", "-C", str(d), "config", k, v], check=True)
    (d / "app").mkdir()
    (d / "app" / "svc.py").write_text("def f():\n    return 1\n")
    (d / "README.md").write_text("# repo\n\nHand-written prose that must survive.\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "initial"], check=True)
    return d


class TestWorktreeBranch(unittest.TestCase):
    def test_the_implement_lane_branches_under_its_own_prefix(self):
        plan = prepare_implement(_real_repo(), "implement-x",
                                 Path(tempfile.mkdtemp()) / "wt", "2026-09-24")
        self.assertEqual(plan.branch, "implement/2026-09-24")
        self.assertTrue(plan.worktree.is_dir())

    def test_the_quality_prefix_is_untouched(self):
        plan = _improve.prepare_worktree(_real_repo(), "quality-x",
                                         Path(tempfile.mkdtemp()) / "wt", "2026-09-24")
        self.assertEqual(plan.branch, "quality/2026-09-24")


class TestSharedGate(unittest.TestCase):
    def test_the_gate_is_the_quality_lane_s_gate_not_a_copy(self):
        """One gate to trust. A forked copy would drift, and the drift would be silent."""
        self.assertIs(verify_implement, _improve.verify_improve)
        self.assertIs(_implement.commit_implement, _improve.commit_improve)

    def test_a_change_outside_allow_fails_through_the_implement_alias(self):
        plan = prepare_implement(_real_repo(), "implement-x",
                                 Path(tempfile.mkdtemp()) / "wt", "2026-09-24")
        (plan.worktree / "sneaky.py").write_text("x = 1\n")
        errs = verify_implement(plan, allow=["app/**"], verify=[], which=lambda n: None)
        self.assertIn("changed a path outside allow: sneaky.py", errs)

    def test_a_change_inside_allow_passes(self):
        plan = prepare_implement(_real_repo(), "implement-x",
                                 Path(tempfile.mkdtemp()) / "wt", "2026-09-24")
        (plan.worktree / "app" / "catalog.py").write_text("def g():\n    return 2\n")
        errs = verify_implement(plan, allow=["app/**"], verify=[], which=lambda n: None)
        self.assertEqual(errs, [])

    def test_human_prose_is_still_frozen_unless_the_brief_names_it(self):
        """The lane may change behaviour; that is not licence to reword someone's README."""
        plan = prepare_implement(_real_repo(), "implement-x",
                                 Path(tempfile.mkdtemp()) / "wt", "2026-09-24")
        (plan.worktree / "README.md").write_text("# repo\n\nReworded by a machine.\n")
        errs = verify_implement(plan, allow=["README.md"], verify=[], which=lambda n: None)
        self.assertTrue(any("human text" in e for e in errs), errs)

        errs = verify_implement(plan, allow=["README.md"], verify=[], rewrite=["README.md"],
                                which=lambda n: None)
        self.assertEqual(errs, [])


# ---- the prompt the lane sends ------------------------------------------------------

class TestLanePrompt(unittest.TestCase):
    def test_the_prompt_names_the_implement_playbook_and_the_worktree(self):
        cfg = Config(root=Path("/root"))
        prompt = _lane_prompt("implement", Path("/root/backlog/b.md"), Path("/repo"),
                              Path("/wt"), cfg)
        self.assertIn("IMPLEMENT.md", prompt)
        self.assertIn("/wt", prompt)
        self.assertIn("/repo", prompt)
        self.assertNotIn("IMPROVE.md", prompt)


# ---- the daemon's choreography ------------------------------------------------------

def _root():
    d = Path(tempfile.mkdtemp())
    (d / "backlog").mkdir(); (d / "projects").mkdir(); (d / "state").mkdir()
    target = _target_repo()
    (d / "backlog" / "i.md").write_text(_brief(target))
    return d, target


def _sig(root):
    return {"oracle_path": root / "state" / "usage-oracle.json",
            "snapshot_path": root / "state" / "usage-snapshot.json"}


def _C(root, **kw):
    return Config(root=root, **{"weekly_guard_enabled": False, **kw})


def _run(*a, **kw):
    from autobuild.daemon import run_once
    notes = kw.pop("notes", None)
    sink = notes if notes is not None else []
    kw["notifier"] = lambda cfg, event, runner=None, **k: sink.append((event, k))
    return run_once(*a, **kw)


def _capture_builder(seen, **result_kw):
    def builder(brief, repo, model, pace, cfg, mode="build", work_dir=None, runner=None):
        seen.append({"repo": Path(repo), "mode": mode, "work_dir": work_dir})
        return BuildResult(**{"is_error": False, "cost_usd": 0.3, **result_kw})
    return builder


class _IOps:
    """Stand-in for autobuild.implement — records the choreography and the contract the
    daemon hands the gate (allow/require/rewrite/verify come from the brief, not the daemon)."""

    def __init__(self, verify_errors=None):
        self.calls, self.verify_errors = [], list(verify_errors or [])

    def prepare_worktree(self, repo, slug, worktrees_dir, date, runner=None):
        self.calls.append(("prepare", str(repo), slug, date))
        return AutodocPlan(repo=Path(repo), worktree=Path(worktrees_dir) / slug,
                           branch=f"implement/{date}", base_sha="deadbeef", slug=slug)

    def verify_implement(self, plan, allow, verify, require=(), rewrite=(), runner=None):
        self.calls.append(("verify", plan.slug, list(allow), list(verify), list(require),
                           list(rewrite)))
        return list(self.verify_errors)

    def commit_implement(self, plan, message, allow, runner=None, wip=False):
        self.calls.append(("commit", plan.slug, wip, list(allow), message))
        return True

    def remove_worktree(self, plan, runner=None):
        self.calls.append(("remove", plan.slug))


class TestImplementLane(unittest.TestCase):
    def test_an_implement_item_runs_in_its_own_worktree_and_hands_over_the_contract(self):
        root, target = _root()
        ops, seen, notes = _IOps(), [], []
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder(seen), implement_ops=ops, notes=notes, **_sig(root))

        self.assertEqual(seen[0]["repo"], target)
        self.assertEqual(seen[0]["mode"], "implement")
        self.assertEqual(seen[0]["work_dir"],
                         root / "state" / "worktrees" / "implement-target")
        self.assertEqual([c[0] for c in ops.calls], ["prepare", "verify", "commit", "remove"])

        verify = [c for c in ops.calls if c[0] == "verify"][0]
        self.assertEqual(verify[2], ["app/**", "tests/**", "docs/**"])
        self.assertEqual(verify[3], [".venv/bin/pytest -q"])
        self.assertEqual(verify[4], ["tests/test_*.py"])
        self.assertEqual(verify[5], ["docs/REFERENCE.md"])

        commit = [c for c in ops.calls if c[0] == "commit"][0]
        self.assertFalse(commit[2])
        self.assertIn("feat(implement)", commit[4])
        self.assertIn(PLAN_REL, commit[4])          # the commit says which plan it followed

        item = scan_backlog(root / "backlog")[0]
        self.assertEqual(item.meta["status"], "done")
        row = read_ledger(root / "state" / "ledger.jsonl")[0]
        self.assertEqual(row["mode"], "implement")
        self.assertEqual(row["branch"], "implement/2026-07-23")
        self.assertEqual(notes[0][0], "done")

    def test_a_red_gate_keeps_the_worktree_and_commits_wip(self):
        root, _ = _root()
        ops = _IOps(verify_errors=["verify failed: `.venv/bin/pytest -q` (exit 1) — boom"])
        notes = []
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder([]), implement_ops=ops, notes=notes, **_sig(root))

        self.assertEqual([c[0] for c in ops.calls], ["prepare", "verify", "commit"])
        self.assertTrue([c for c in ops.calls if c[0] == "commit"][0][2])
        self.assertIn("WIP", [c for c in ops.calls if c[0] == "commit"][0][4])
        item = scan_backlog(root / "backlog")[0]
        self.assertEqual(item.meta["status"], "needs-review")
        self.assertIn("pytest", notes[0][1]["reason"])
        self.assertIn("worktrees/implement-target", notes[0][1]["worktree"])

    def test_an_agent_error_is_needs_review_without_running_the_gate(self):
        root, _ = _root()
        ops = _IOps()
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder([], is_error=True), implement_ops=ops, **_sig(root))

        self.assertNotIn("verify", [c[0] for c in ops.calls])
        item = scan_backlog(root / "backlog")[0]
        self.assertEqual(item.meta["status"], "needs-review")


if __name__ == "__main__":
    unittest.main()
