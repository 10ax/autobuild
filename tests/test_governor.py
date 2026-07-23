import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState
from autobuild.governor import (in_quiet_hours, compute_pace, record_spend,
                                update_ceiling_ema, WINDOW_SECONDS)

TZ = ZoneInfo("Europe/Rome")
def _cfg(**kw):
    return Config(root=".", **kw)
def _at(h):  # today at hour h, Rome
    return datetime(2026, 7, 23, h, 0, tzinfo=TZ)

class TestGovernor(unittest.TestCase):
    def test_daytime_pauses(self):
        p = compute_pace(_at(12), _cfg(), GovernorState())
        self.assertEqual(p.level, "pause")

    def test_night_uncalibrated_is_conservative(self):
        p = compute_pace(_at(23), _cfg(), GovernorState())
        self.assertEqual((p.concurrency, p.model, p.subagents), (1, "sonnet", False))

    def test_night_ample_headroom_ramps_and_escalates(self):
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=0.5,
                           window_start=_at(23).timestamp())
        p = compute_pace(_at(23), _cfg(max_concurrency=3), st)
        self.assertEqual(p.level, "high")
        self.assertEqual(p.concurrency, 3)
        self.assertTrue(p.subagents)
        self.assertEqual(p.model, "opus")

    def test_near_ceiling_pauses(self):
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=9.8,
                           window_start=_at(23).timestamp())
        self.assertEqual(compute_pace(_at(23), _cfg(), st).level, "pause")

    def test_ema_calibrates_from_first_429(self):
        st = update_ceiling_ema(GovernorState(), 8.0)
        self.assertEqual(st.learned_ceiling_usd, 8.0)
        st = update_ceiling_ema(st, 12.0, alpha=0.5)
        self.assertEqual(st.learned_ceiling_usd, 10.0)

    def test_record_spend_accumulates_window_and_week(self):
        st = record_spend(GovernorState(), 1.5, _at(23))
        self.assertAlmostEqual(st.window_spend_usd, 1.5)
        self.assertAlmostEqual(st.weekly_spend_usd, 1.5)
        self.assertIsNotNone(st.window_start)
