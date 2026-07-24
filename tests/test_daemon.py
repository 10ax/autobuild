# tests/test_daemon.py
import json, tempfile, unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState, read_ledger
from autobuild.build import BuildResult
from autobuild.backlog import scan_backlog
from autobuild.daemon import run_once

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


class TestDaemon(unittest.TestCase):
    def test_daytime_returns_pause_and_builds_nothing(self):
        root = _root()
        res = run_once(Config(root=root), datetime(2026, 7, 23, 12, tzinfo=TZ),
                       GovernorState(), root / "state", **_sig(root))
        self.assertEqual(res["action"], "pause")

    def test_night_builds_and_marks_done_on_green(self):
        root = _root()
        res = run_once(
            Config(root=root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
            verifier=lambda *a, **k: True, **_sig(root),
        )
        self.assertEqual(res["action"], "built")
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "done")
        self.assertEqual(read_ledger(root / "state" / "ledger.jsonl")[0]["status"], "done")

    def test_red_verify_marks_needs_review(self):
        root = _root()
        run_once(
            Config(root=root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
            verifier=lambda *a, **k: False, **_sig(root),
        )
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "needs-review")

    def test_rate_limited_anchors_window_and_requeues(self):
        root = _root()
        reset = NIGHT.timestamp() + 3600
        st = GovernorState()
        run_once(
            Config(root=root, max_concurrency=1), NIGHT, st, root / "state",
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
        res = run_once(
            Config(root=root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
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
        run_once(
            Config(root=root, max_concurrency=1), NIGHT, st, root / "state",
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
        run_once(
            Config(root=root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
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
        run_once(
            Config(root=root, max_concurrency=1), NIGHT, GovernorState(), root / "state",
            builder=lambda *a, **k: built.append(1) or BuildResult(is_error=False, cost_usd=0.0),
            verifier=lambda *a, **k: True, **_sig(root),
        )
        self.assertEqual(built, [])  # builder never called on an invalid brief
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "needs-review")
