import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState
from autobuild.governor import (in_quiet_hours, compute_pace, record_spend,
                                update_ceiling_ema, anchor_window, WINDOW_SECONDS)

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

    def test_late_in_window_clamps_to_low(self):
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=0.1,
                           window_start=_at(23).timestamp() - 0.9 * WINDOW_SECONDS)
        p = compute_pace(_at(23), _cfg(max_concurrency=3), st)
        self.assertEqual(p.level, "low")       # ample headroom would be "high", but late clamps it
        self.assertEqual(p.concurrency, 1)
        self.assertFalse(p.subagents)

    def test_record_spend_resets_expired_window(self):
        old_start = _at(23).timestamp() - (WINDOW_SECONDS + 10)
        st = GovernorState(window_start=old_start, window_spend_usd=5.0,
                           week_start=_at(23).date().isoformat())
        st = record_spend(st, 1.0, _at(23))
        self.assertAlmostEqual(st.window_spend_usd, 1.0)              # reset to 0 then +1.0
        self.assertAlmostEqual(st.window_start, _at(23).timestamp())

    def test_record_spend_rolls_week_after_7_days(self):
        old_week = (_at(23) - timedelta(days=8)).date().isoformat()
        st = GovernorState(week_start=old_week, weekly_spend_usd=50.0)
        st = record_spend(st, 2.0, _at(23))
        self.assertAlmostEqual(st.weekly_spend_usd, 2.0)             # reset to 0 then +2.0
        self.assertEqual(st.week_start, _at(23).date().isoformat())

    def test_anchor_window_sets_start_and_zeroes_spend(self):
        st = anchor_window(GovernorState(window_spend_usd=5.0, window_start=123.0),
                           9_999_999_999.0)
        self.assertEqual(st.window_start, 9_999_999_999.0)
        self.assertEqual(st.window_spend_usd, 0.0)

    def test_rate_limit_cooldown_pauses_until_reset(self):
        # window anchored to a FUTURE reset (post-429) → blocked until the window opens,
        # even though window_spend is 0 (which would otherwise read as full headroom).
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=0.0,
                           window_start=_at(23).timestamp() + 3600)
        self.assertEqual(compute_pace(_at(23), _cfg(max_concurrency=3), st).level, "pause")

    def test_expired_window_recovers_to_fresh(self):
        # a fully-elapsed window with stale high spend computes as fresh, so the governor
        # recovers from a rate-limit pause without needing a spend event to reset it.
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=9.9,
                           window_start=_at(23).timestamp() - (WINDOW_SECONDS + 10))
        self.assertEqual(compute_pace(_at(23), _cfg(max_concurrency=3), st).level, "high")
