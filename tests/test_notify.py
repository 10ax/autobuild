import unittest
from autobuild.config import Config
from autobuild.notify import format_message, notify

class TestNotify(unittest.TestCase):
    def test_format_done(self):
        msg = format_message("done", slug="pomodoro-cli", tests="green", cost=0.4, secs=423, repo="/r/p")
        self.assertIn("pomodoro-cli", msg)
        self.assertIn("green", msg)
        self.assertIn("7m", msg)            # 423s → 7m03s, real duration (not the old 0m)
        self.assertNotIn("0m0", msg)

    def test_format_done_subminute_duration(self):
        msg = format_message("done", slug="x", tests="green", cost=0.1, secs=45, repo="/r/x")
        self.assertIn("45s", msg)           # sub-minute builds read as seconds, not "0m"

    def test_notify_skips_events_not_in_notify_on(self):
        calls = []
        notify(Config(root="/tmp", notify_on=["done"]), "paused",
               runner=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_notify_calls_runner_for_enabled_event(self):
        calls = []
        notify(Config(root="/tmp", notify_on=["done"]), "done", slug="x",
               runner=lambda *a, **k: calls.append(a))
        self.assertEqual(len(calls), 1)
