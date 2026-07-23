# tests/test_daemon.py
import tempfile, unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState, GovernorState as GS, read_ledger
from autobuild.governor import Pace
from autobuild.build import BuildResult
from autobuild.backlog import scan_backlog
from autobuild.daemon import run_once

TZ = ZoneInfo("Europe/Rome")
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

class TestDaemon(unittest.TestCase):
    def test_daytime_returns_pause_and_builds_nothing(self):
        root = _root()
        res = run_once(Config(root=root), datetime(2026, 7, 23, 12, tzinfo=TZ),
                       GovernorState(), root / "state")
        self.assertEqual(res["action"], "pause")

    def test_night_builds_and_marks_done_on_green(self):
        root = _root()
        res = run_once(
            Config(root=root, max_concurrency=1),
            datetime(2026, 7, 23, 23, tzinfo=TZ), GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
            verifier=lambda *a, **k: True,
        )
        self.assertEqual(res["action"], "built")
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "done")
        self.assertEqual(read_ledger(root / "state" / "ledger.jsonl")[0]["status"], "done")

    def test_red_verify_marks_needs_review(self):
        root = _root()
        run_once(
            Config(root=root, max_concurrency=1),
            datetime(2026, 7, 23, 23, tzinfo=TZ), GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
            verifier=lambda *a, **k: False,
        )
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "needs-review")

    def test_invalid_brief_diverted_to_needs_review_not_built(self):
        root = _root()
        # overwrite the example with an invalid brief: pending but NO slug
        (root / "backlog" / "a.md").write_text(
            '+++\nspec_version = "1.0"\ntitle = "x"\ntier = "script"\n'
            'priority = 5\nstatus = "pending"\n+++\n## Intent\nx\n## Acceptance Criteria\nA1. x\n')
        built = []
        res = run_once(
            Config(root=root, max_concurrency=1),
            datetime(2026, 7, 23, 23, tzinfo=TZ), GovernorState(), root / "state",
            builder=lambda *a, **k: built.append(1) or BuildResult(is_error=False, cost_usd=0.0),
            verifier=lambda *a, **k: True,
        )
        self.assertEqual(built, [])  # builder never called on an invalid brief
        item = [i for i in scan_backlog(root / "backlog")][0]
        self.assertEqual(item.meta["status"], "needs-review")
