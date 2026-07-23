import unittest
from autobuild.config import Config
from autobuild.notify import format_message, notify

class TestNotify(unittest.TestCase):
    def test_format_done(self):
        msg = format_message("done", slug="pomodoro-cli", tests="green", cost=0.4, mins=7, repo="/r/p")
        self.assertIn("pomodoro-cli", msg)
        self.assertIn("green", msg)

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
