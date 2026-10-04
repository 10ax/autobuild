# tests/test_daemon.py
import json, tempfile, unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState, read_ledger
from autobuild.autodoc import AutodocError, AutodocPlan
from autobuild.build import BuildResult
from autobuild.backlog import scan_backlog
from autobuild.daemon import run_once as _run_once

TZ = ZoneInfo("Europe/Rome")
NIGHT = datetime(2026, 7, 23, 23, tzinfo=TZ)
BRIEF = '''+++
spec_version = "1.0"
slug = "alpha"
title = "alpha"
tier = "script"
priority = 5
status = "pending"
+++
## Intent
x
## Acceptance Criteria
A1. x
'''


def _root():
    d = Path(tempfile.mkdtemp())
    (d / "backlog").mkdir(); (d / "projects").mkdir(); (d / "state").mkdir()
    (d / "backlog" / "a.md").write_text(BRIEF)
    return d


def _sig(root):  # hermetic signal paths (default to real ~/.claude, so pin them in tests)
    return {"oracle_path": root / "state" / "usage-oracle.json",
            "snapshot_path": root / "state" / "usage-snapshot.json"}

def _C(root, **kw):  # Config with the weekly guard OFF (build/pace-flow tests aren't about it)
    return Config(root=root, **{"weekly_guard_enabled": False, **kw})


def _run(*a, **kw):
    """run_once with the notifier captured — tests must NEVER shell out to the real telegram
    script. Pass notes=[] to inspect the (event, kwargs) tuples the daemon emitted."""
    notes = kw.pop("notes", None)
    sink = notes if notes is not None else []
    kw["notifier"] = lambda cfg, event, runner=None, **k: sink.append((event, k))
    return _run_once(*a, **kw)


class TestDaemon(unittest.TestCase):
    def test_daytime_returns_pause_and_builds_nothing(self):
        root = _root()
        res = _run(_C(root), datetime(2026, 7, 23, 12, tzinfo=TZ),
                       GovernorState(), root / "state", **_sig(root))
        self.assertEqual(res["action"], "pause")

    def test_night_builds_and_marks_done_on_green(self):
        root = _root()
        res = _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
            verifier=lambda *a, **k: True, **_sig(root),
        )
        self.assertEqual(res["action"], "built")
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "done")
        self.assertEqual(read_ledger(root / "state" / "ledger.jsonl")[0]["status"], "done")


class TestMeteredDaemon(unittest.TestCase):
    """A metered provider must not be able to spend unattended money by accident, and must
    not be paced by a quota it does not have."""

    def test_metered_refuses_to_build_without_the_opt_in(self):
        root = _root()
        res = _run(_C(root, metered=True, provider="opencode"), NIGHT, GovernorState(),
                   root / "state",
                   builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=9.0),
                   verifier=lambda *a, **k: True, **_sig(root))
        self.assertEqual(res["action"], "blocked")
        self.assertIn("metered", res["reason"].lower())
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "pending")   # untouched, not even marked building

    def test_guard_does_not_depend_on_the_metered_flag_being_stamped(self):
        """provider alone is enough to refuse: a caller that forgets to stamp cfg.metered
        must not be able to slip an unattended metered run past the guard."""
        root = _root()
        res = _run(_C(root, provider="opencode"), NIGHT, GovernorState(), root / "state",
                   builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=9.0),
                   verifier=lambda *a, **k: True, **_sig(root))
        self.assertEqual(res["action"], "blocked")

    def test_metered_builds_once_the_opt_in_is_set(self):
        root = _root()
        res = _run(_C(root, metered=True, allow_metered=True, max_concurrency=1), NIGHT,
                   GovernorState(), root / "state",
                   builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=1.5),
                   verifier=lambda *a, **k: True, **_sig(root))
        self.assertEqual(res["action"], "built")

    def test_metered_equipment_is_not_paced_by_the_weekly_quota(self):
        # A 7d% over the ceiling would pause the seat. Nothing feeds a 7d% for a metered
        # backend, so acting on one would stall the daemon permanently.
        root = _root()
        sig = _sig(root)
        sig["oracle_path"].write_text(json.dumps({"status": "allowed", "written_at": 0}))
        res = _run(_C(root, metered=True, allow_metered=True, max_concurrency=1,
                      weekly_guard_enabled=True), NIGHT, GovernorState(), root / "state",
                   builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=1.0),
                   verifier=lambda *a, **k: True, **sig)
        self.assertEqual(res["action"], "built")

    def test_unreadable_cost_is_not_banked_as_free_spend(self):
        """recording 0.0 for a metered run whose cost could not be read would tell the
        learned ceiling that a real spend was free, so the budget would never advance."""
        root = _root()
        _run(_C(root, metered=True, allow_metered=True, max_concurrency=1), NIGHT,
             GovernorState(), root / "state",
             builder=lambda *a, **k: BuildResult(is_error=True, cost_usd=0.0,
                                                 raw={"cost_unreadable": None}),
             verifier=lambda *a, **k: True, **_sig(root))
        gov = json.loads((root / "state" / "governor.json").read_text())
        self.assertEqual(gov.get("weekly_spend_usd", 0.0), 0.0)
        self.assertEqual(gov.get("window_spend_usd", 0.0), 0.0)

    def test_manual_pause_flag_pauses_and_builds_nothing(self):
        # A state/pause file (written by the web console) forces a pause even inside an
        # active build window, and nothing is built until it is removed.
        root = _root(); built = []
        (root / "state" / "pause").write_text('{"at":"2026-07-23T23:00:00","by":"console"}')
        res = _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: built.append(1) or BuildResult(is_error=False, cost_usd=0.1),
            verifier=lambda *a, **k: True, **_sig(root),
        )
        self.assertEqual(res["action"], "pause")
        self.assertTrue(res.get("manual"))   # distinguishable from a governor/rate-limit pause
        self.assertEqual(built, [])

    def test_ledger_records_build_duration(self):
        root = _root()
        _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5, duration_s=137.0),
            verifier=lambda *a, **k: True, **_sig(root),
        )
        row = read_ledger(root / "state" / "ledger.jsonl")[0]
        self.assertEqual(row["duration_s"], 137.0)   # duration persisted for the shift-log

    def test_red_verify_marks_needs_review(self):
        root = _root()
        _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
            verifier=lambda *a, **k: False, **_sig(root),
        )
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "needs-review")

    def test_rate_limited_anchors_window_and_requeues(self):
        root = _root()
        reset = NIGHT.timestamp() + 3600
        st = GovernorState()
        _run(
            _C(root, max_concurrency=1), NIGHT, st, root / "state",
            builder=lambda *a, **k: BuildResult(is_error=True, cost_usd=0.7,
                                                rate_limited=True, reset_at=reset),
            verifier=lambda *a, **k: True, **_sig(root),
        )
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "pending")     # requeued, not consumed
        self.assertEqual(st.window_start, reset)             # window anchored to the reset
        self.assertGreater(st.learned_ceiling_usd, 0.0)      # ceiling calibrated from the hit

    def test_live_rejected_signal_file_pauses_before_building(self):
        root = _root()
        built = []
        sig = _sig(root)
        sig["oracle_path"].write_text(json.dumps(
            {"status": "rejected", "reset_at": NIGHT.timestamp() + 1800,
             "rate_limit_type": "five_hour", "written_at": NIGHT.timestamp() - 60}))
        res = _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: built.append(1) or BuildResult(is_error=False, cost_usd=0.1),
            verifier=lambda *a, **k: True, **sig,
        )
        self.assertEqual(res["action"], "pause")   # live "rejected" → hard stop
        self.assertEqual(built, [])                 # nothing built

    def test_rejected_build_writes_oracle_and_prefers_rate_reset(self):
        root = _root()
        reset = NIGHT.timestamp() + 2400
        st = GovernorState()
        sig = _sig(root)
        _run(
            _C(root, max_concurrency=1), NIGHT, st, root / "state",
            builder=lambda *a, **k: BuildResult(is_error=True, cost_usd=0.6, rate_limited=True,
                                                rate_status="rejected", rate_reset_at=reset,
                                                reset_at=999.0),  # rate_reset_at must win
            verifier=lambda *a, **k: True, **sig,
        )
        self.assertEqual(st.window_start, reset)                    # rate_reset_at preferred over reset_at
        oracle = json.loads(sig["oracle_path"].read_text())
        self.assertEqual(oracle["status"], "rejected")
        self.assertEqual(oracle["reset_at"], reset)

    def test_allowed_build_warms_oracle(self):
        root = _root()
        reset = NIGHT.timestamp() + 3000
        sig = _sig(root)
        _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.4,
                                                rate_status="allowed", rate_reset_at=reset),
            verifier=lambda *a, **k: True, **sig,
        )
        oracle = json.loads(sig["oracle_path"].read_text())
        self.assertEqual(oracle["status"], "allowed")   # per-build capture keeps the oracle warm
        self.assertEqual(oracle["reset_at"], reset)

    def test_invalid_brief_diverted_to_needs_review_not_built(self):
        root = _root()
        (root / "backlog" / "a.md").write_text(
            '+++\nspec_version = "1.0"\ntitle = "x"\ntier = "script"\n'
            'priority = 5\nstatus = "pending"\n+++\n## Intent\nx\n## Acceptance Criteria\nA1. x\n')
        built = []
        _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: built.append(1) or BuildResult(is_error=False, cost_usd=0.0),
            verifier=lambda *a, **k: True, **_sig(root),
        )
        self.assertEqual(built, [])  # builder never called on an invalid brief
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "needs-review")

    # --- notifications (must never shell out in tests; content must be human-readable) ---
    def test_done_emits_single_done_notification(self):
        root = _root(); notes = []
        _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5, duration_s=123.0),
            verifier=lambda *a, **k: True, notes=notes, **_sig(root),
        )
        self.assertEqual([e for e, _ in notes], ["done"])              # exactly one, right event
        self.assertEqual(notes[0][1]["slug"], "alpha")
        self.assertEqual(notes[0][1]["tests"], "green")
        self.assertEqual(notes[0][1]["secs"], 123.0)                   # real build duration flows through

    def test_paused_notification_reset_is_human_readable(self):
        root = _root(); notes = []
        reset = NIGHT.timestamp() + 3600
        _run(
            _C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=True, cost_usd=0.7,
                                                rate_limited=True, reset_at=reset),
            verifier=lambda *a, **k: True, notes=notes, **_sig(root),
        )
        reason = dict(notes)["paused"]["reason"]
        self.assertIn("rate limited on alpha", reason)
        self.assertIn(":", reason)                       # a clock time, not a bare epoch
        self.assertNotIn(str(int(reset)), reason)        # the raw epoch must not leak through

    # --- weekly guard on (production default) ---
    def test_weekly_guard_pauses_over_ceiling(self):
        root = _root(); sig = _sig(root)
        sig["snapshot_path"].write_text(json.dumps(
            {"five_hour": {"used_percentage": 5.0}, "seven_day": {"used_percentage": 60.0},
             "written_at": NIGHT.timestamp() - 30}))
        built = []
        res = _run(
            Config(root=root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: built.append(1) or BuildResult(is_error=False, cost_usd=0.1),
            verifier=lambda *a, **k: True, **sig,
        )
        self.assertEqual(res["action"], "pause")   # weekly 60% ≥ 50% ceiling → reserve for user
        self.assertEqual(built, [])

    def test_weekly_guard_fail_closed_without_7d(self):
        root = _root(); built = []
        res = _run(
            Config(root=root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: built.append(1) or BuildResult(is_error=False, cost_usd=0.1),
            verifier=lambda *a, **k: True, **_sig(root),   # no signal files → no live 7d
        )
        self.assertEqual(res["action"], "pause")   # guard on + no 7d → fail closed
        self.assertEqual(built, [])

    def test_weekly_guard_under_budget_builds(self):
        root = _root(); sig = _sig(root)
        sig["snapshot_path"].write_text(json.dumps(
            {"five_hour": {"used_percentage": 5.0}, "seven_day": {"used_percentage": 20.0},
             "written_at": NIGHT.timestamp() - 30}))
        res = _run(
            Config(root=root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.1),
            verifier=lambda *a, **k: True, **sig,
        )
        self.assertEqual(res["action"], "built")   # 20% < 50%, daily delta 0 → runs
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "done")


# ---- the docs lane ----------------------------------------------------------------
DOC_BRIEF = '''+++
spec_version = "1.0"
slug = "autodoc-target"
title = "Autodoc: target"
tier = "docs"
mode = "document"
repo = "{repo}"
priority = 9
status = "pending"
+++
## Intent
Document the target repo.

## Acceptance Criteria
A1. The doc set exists and every anchor resolves.
'''


def _doc_root():
    """A root whose only backlog item is a document brief pointing at a git repo."""
    d = Path(tempfile.mkdtemp())
    (d / "backlog").mkdir(); (d / "projects").mkdir(); (d / "state").mkdir()
    target = d / "target"
    (target / ".git").mkdir(parents=True)
    (d / "backlog" / "doc.md").write_text(DOC_BRIEF.format(repo=target))
    return d, target


class _Ops:
    """Stand-in for autobuild.autodoc — records the lane's git choreography."""

    def __init__(self, verify_errors=None, prepare_raises=False):
        self.calls, self.verify_errors = [], list(verify_errors or [])
        self.prepare_raises = prepare_raises

    def prepare_worktree(self, repo, slug, worktrees_dir, date, runner=None):
        self.calls.append(("prepare", str(repo), slug, date))
        if self.prepare_raises:
            raise AutodocError("worktree path occupied")
        return AutodocPlan(repo=Path(repo), worktree=Path(worktrees_dir) / slug,
                           branch=f"autodoc/{date}", base_sha="deadbeef", slug=slug)

    def verify_docs(self, plan, runner=None):
        self.calls.append(("verify", plan.slug))
        return list(self.verify_errors)

    def commit_docs(self, plan, message, runner=None, wip=False):
        self.calls.append(("commit", plan.slug, wip))
        return True

    def remove_worktree(self, plan, runner=None):
        self.calls.append(("remove", plan.slug))


def _capture_builder(seen, **result_kw):
    def builder(brief, repo, model, pace, cfg, mode="build", work_dir=None, runner=None):
        seen.append({"repo": Path(repo), "mode": mode, "work_dir": work_dir})
        return BuildResult(**{"is_error": False, "cost_usd": 0.3, **result_kw})
    return builder


class TestDocsLane(unittest.TestCase):
    def test_document_item_targets_the_front_matter_repo(self):
        root, target = _doc_root()
        ops, seen, notes = _Ops(), [], []
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder(seen), autodoc_ops=ops, notes=notes, **_sig(root))
        self.assertEqual(seen[0]["repo"], target)            # not root/projects/<slug>
        self.assertEqual(seen[0]["mode"], "document")
        self.assertEqual(seen[0]["work_dir"],
                         root / "state" / "worktrees" / "autodoc-target")
        self.assertEqual([c[0] for c in ops.calls], ["prepare", "verify", "commit", "remove"])
        self.assertFalse([c for c in ops.calls if c[0] == "commit"][0][2])   # not WIP
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "done")
        row = read_ledger(root / "state" / "ledger.jsonl")[0]
        self.assertEqual(row["mode"], "document")
        self.assertEqual(row["branch"], "autodoc/2026-07-23")
        self.assertEqual(row["repo"], str(target))
        self.assertEqual(notes[0][0], "done")
        self.assertIn("autodoc/2026-07-23", notes[0][1]["branch"])

    def test_failed_verify_keeps_the_worktree_and_commits_wip(self):
        root, target = _doc_root()
        ops, notes = _Ops(verify_errors=["dangling anchor in docs/CODE-MAP.md: x.py:9"]), []
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder([]), autodoc_ops=ops, notes=notes, **_sig(root))
        self.assertEqual([c[0] for c in ops.calls], ["prepare", "verify", "commit"])
        self.assertTrue([c for c in ops.calls if c[0] == "commit"][0][2])     # WIP commit
        self.assertNotIn("remove", [c[0] for c in ops.calls])                 # left to inspect
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "needs-review")
        self.assertEqual(notes[0][0], "needs-review")
        self.assertIn("dangling anchor", notes[0][1]["reason"])
        self.assertIn("worktrees/autodoc-target", notes[0][1]["worktree"])

    def test_unpreparable_worktree_never_runs_the_agent(self):
        root, _ = _doc_root()
        ops, seen = _Ops(prepare_raises=True), []
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder(seen), autodoc_ops=ops, **_sig(root))
        self.assertEqual(seen, [])
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "needs-review")

    def test_rate_limited_document_item_requeues_without_committing(self):
        root, _ = _doc_root()
        ops = _Ops()
        reset = NIGHT.timestamp() + 3600
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder([], is_error=True, rate_limited=True, reset_at=reset),
             autodoc_ops=ops, **_sig(root))
        self.assertNotIn("commit", [c[0] for c in ops.calls])
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "pending")

    def test_build_item_never_touches_the_docs_lane(self):
        root = _root()
        ops = _Ops()
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
             verifier=lambda *a, **k: True, autodoc_ops=ops, **_sig(root))
        self.assertEqual(ops.calls, [])
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "done")


# ---- the quality lane ---------------------------------------------------------------
IMPROVE_BRIEF = '''+++
spec_version = "1.0"
slug = "quality-target"
title = "Quality: target"
tier = "quality"
mode = "improve"
repo = "{repo}"
priority = 9
status = "pending"
allow = ["README.md", "docs/**", "tests/**", ".github/**"]
require = ["docs/TROUBLESHOOTING.md"]
rewrite = ["README.md"]
verify = [".venv/bin/pytest -q"]
+++
## Intent
Bring the target repo up to standard.

## Acceptance Criteria
A1. The verify commands pass in the worktree.
'''


def _improve_root():
    d = Path(tempfile.mkdtemp())
    (d / "backlog").mkdir(); (d / "projects").mkdir(); (d / "state").mkdir()
    target = d / "target"
    (target / ".git").mkdir(parents=True)
    (d / "backlog" / "q.md").write_text(IMPROVE_BRIEF.format(repo=target))
    return d, target


class _QOps:
    """Stand-in for autobuild.improve — records the lane's choreography and the contract
    the daemon hands it (allow/require/rewrite/verify come from the brief, not the daemon)."""

    def __init__(self, verify_errors=None):
        self.calls, self.verify_errors = [], list(verify_errors or [])

    def prepare_worktree(self, repo, slug, worktrees_dir, date, runner=None):
        self.calls.append(("prepare", str(repo), slug, date))
        return AutodocPlan(repo=Path(repo), worktree=Path(worktrees_dir) / slug,
                           branch=f"quality/{date}", base_sha="deadbeef", slug=slug)

    def verify_improve(self, plan, allow, verify, require=(), rewrite=(), runner=None):
        self.calls.append(("verify", plan.slug, list(allow), list(verify), list(require),
                           list(rewrite)))
        return list(self.verify_errors)

    def commit_improve(self, plan, message, allow, runner=None, wip=False):
        self.calls.append(("commit", plan.slug, wip, list(allow), message))
        return True

    def remove_worktree(self, plan, runner=None):
        self.calls.append(("remove", plan.slug))


class TestQualityLane(unittest.TestCase):
    def test_improve_item_runs_in_a_quality_worktree_and_hands_the_contract_to_verify(self):
        root, target = _improve_root()
        ops, seen, notes = _QOps(), [], []
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder(seen), improve_ops=ops, notes=notes, **_sig(root))
        self.assertEqual(seen[0]["repo"], target)
        self.assertEqual(seen[0]["mode"], "improve")
        self.assertEqual(seen[0]["work_dir"], root / "state" / "worktrees" / "quality-target")
        self.assertEqual([c[0] for c in ops.calls], ["prepare", "verify", "commit", "remove"])
        verify = [c for c in ops.calls if c[0] == "verify"][0]
        self.assertEqual(verify[2], ["README.md", "docs/**", "tests/**", ".github/**"])
        self.assertEqual(verify[3], [".venv/bin/pytest -q"])
        self.assertEqual(verify[4], ["docs/TROUBLESHOOTING.md"])
        self.assertEqual(verify[5], ["README.md"])
        commit = [c for c in ops.calls if c[0] == "commit"][0]
        self.assertFalse(commit[2])
        self.assertEqual(commit[3], ["README.md", "docs/**", "tests/**", ".github/**"])
        self.assertIn("quality", commit[4])
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "done")
        row = read_ledger(root / "state" / "ledger.jsonl")[0]
        self.assertEqual(row["mode"], "improve")
        self.assertEqual(row["branch"], "quality/2026-07-23")
        self.assertEqual(notes[0][0], "done")
        self.assertIn("quality/2026-07-23", notes[0][1]["branch"])

    def test_red_verify_keeps_the_worktree_and_commits_wip(self):
        root, _ = _improve_root()
        ops, notes = _QOps(verify_errors=["verify failed: `.venv/bin/pytest -q` (exit 1) — boom"]), []
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder([]), improve_ops=ops, notes=notes, **_sig(root))
        self.assertEqual([c[0] for c in ops.calls], ["prepare", "verify", "commit"])
        self.assertTrue([c for c in ops.calls if c[0] == "commit"][0][2])
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "needs-review")
        self.assertEqual(notes[0][0], "needs-review")
        self.assertIn("pytest", notes[0][1]["reason"])
        self.assertIn("worktrees/quality-target", notes[0][1]["worktree"])

    def test_agent_error_is_needs_review_without_running_verify(self):
        root, _ = _improve_root()
        ops = _QOps()
        _run(_C(root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
             builder=_capture_builder([], is_error=True), improve_ops=ops, **_sig(root))
        self.assertNotIn("verify", [c[0] for c in ops.calls])
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "needs-review")
