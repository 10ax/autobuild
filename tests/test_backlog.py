import tempfile, unittest
from pathlib import Path
from autobuild.backlog import (scan_backlog, select_pending, set_status,
                               add_lock, read_lock, clear_lock)

BRIEF = '''+++
spec_version = "1.0"
slug = "{slug}"
title = "{slug}"
tier = "script"
priority = {prio}
status = "{status}"
+++
## Intent
x
## Acceptance Criteria
A1. x
'''

def _backlog():
    d = Path(tempfile.mkdtemp()) / "backlog"
    d.mkdir()
    (d / ".gitkeep").write_text("")
    (d / "a.md").write_text(BRIEF.format(slug="alpha", prio=1, status="pending"))
    (d / "b.md").write_text(BRIEF.format(slug="beta", prio=5, status="pending"))
    (d / "c.md").write_text(BRIEF.format(slug="gamma", prio=9, status="done"))
    return d

class TestBacklog(unittest.TestCase):
    def test_scan_skips_gitkeep(self):
        self.assertEqual(len(scan_backlog(_backlog())), 3)

    def test_select_pending_orders_by_priority_desc(self):
        picked = select_pending(scan_backlog(_backlog()), 2)
        self.assertEqual([i.meta["slug"] for i in picked], ["beta", "alpha"])

    def test_set_status_persists(self):
        items = scan_backlog(_backlog())
        target = [i for i in items if i.meta["slug"] == "alpha"][0]
        set_status(target, "building")
        again = [i for i in scan_backlog(target.path.parent) if i.meta["slug"] == "alpha"][0]
        self.assertEqual(again.meta["status"], "building")

    def test_lock_roundtrip(self):
        p = Path(tempfile.mkdtemp()) / "current.lock"
        add_lock(p, {"slug": "alpha", "repo": "/r/alpha"})
        add_lock(p, {"slug": "beta", "repo": "/r/beta"})
        self.assertEqual({e["slug"] for e in read_lock(p)}, {"alpha", "beta"})
        clear_lock(p, "alpha")
        self.assertEqual([e["slug"] for e in read_lock(p)], ["beta"])
