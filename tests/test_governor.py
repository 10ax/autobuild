import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState
from autobuild.governor import (in_quiet_hours, compute_pace, record_spend,
                                update_ceiling_ema, anchor_window, WINDOW_SECONDS)
from autobuild.usage import UsageSignal

TZ = ZoneInfo("Europe/Rome")
def _cfg(**kw):
    return Config(root=".", **kw)
def _at(h):  # a WEEKDAY (Thu 2026-07-23) at hour h, Rome
    return datetime(2026, 7, 23, h, 0, tzinfo=TZ)
def _dt(d, h, m=0):  # 2026-07-{d} (24=Fri, 25=Sat, 26=Sun, 27=Mon) at h:m, Rome
    return datetime(2026, 7, d, h, m, tzinfo=TZ)

class TestGovernor(unittest.TestCase):
    def test_daytime_pauses(self):
        p = compute_pace(_at(12), _cfg(), GovernorState())
        self.assertEqual(p.level, "pause")

    def test_night_uncalibrated_is_conservative(self):
        p = compute_pace(_at(23), _cfg(), GovernorState())
        self.assertEqual((p.concurrency, p.model, p.subagents), (1, "sonnet", False))

    # --- weekend-continuous policy (Fri 19:00 → Mon 08:00) ---
    def test_weekday_daytime_still_quiet(self):
        self.assertTrue(in_quiet_hours(_at(12), _cfg()))          # Thu 12:00 → weekday quiet

    def test_friday_evening_starts_weekend(self):
        self.assertTrue(in_quiet_hours(_dt(24, 18), _cfg()))      # Fri 18:00 → still weekday quiet
        self.assertFalse(in_quiet_hours(_dt(24, 20), _cfg()))     # Fri 20:00 → weekend, active

    def test_saturday_and_sunday_daytime_active(self):
        self.assertFalse(in_quiet_hours(_dt(25, 12), _cfg()))     # Sat noon → active
        self.assertFalse(in_quiet_hours(_dt(26, 3), _cfg()))      # Sun 03:00 → active
        self.assertFalse(in_quiet_hours(_dt(26, 15), _cfg()))     # Sun 15:00 → active

    def test_monday_morning_ends_weekend(self):
        self.assertFalse(in_quiet_hours(_dt(27, 7), _cfg()))      # Mon 07:00 → still weekend
        self.assertTrue(in_quiet_hours(_dt(27, 9), _cfg()))       # Mon 09:00 → weekday quiet resumes

    def test_saturday_daytime_pace_is_not_pause(self):
        # the whole point: uncalibrated Sat noon should WORK (conservative), not pause
        self.assertEqual(compute_pace(_dt(25, 12), _cfg(), GovernorState()).level, "low")

    def test_weekend_disabled_falls_back_to_daily(self):
        self.assertTrue(in_quiet_hours(_dt(25, 12), _cfg(weekend_from="", weekend_to="")))

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

    # --- live UsageSignal integration ---
    def test_live_rejected_status_pauses(self):
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=0.0,
                           window_start=_at(23).timestamp())
        p = compute_pace(_at(23), _cfg(max_concurrency=3), st, signal=UsageSignal(status="rejected"))
        self.assertEqual(p.level, "pause")

    def test_live_percentage_drives_headroom_when_uncalibrated(self):
        # no EMA ceiling, but a fresh live % → the governor still ramps on real headroom
        p = compute_pace(_at(23), _cfg(max_concurrency=3), GovernorState(),
                         signal=UsageSignal(used_pct_5h=10.0))
        self.assertEqual(p.level, "high")

    def test_live_percentage_overrides_ema(self):
        # EMA alone (ceiling 10, spent 9) would clamp to "low"; live % says only 20% used → high
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=9.0,
                           window_start=_at(23).timestamp())
        p = compute_pace(_at(23), _cfg(max_concurrency=3), st, signal=UsageSignal(used_pct_5h=20.0))
        self.assertEqual(p.level, "high")

    def test_live_percentage_near_cap_pauses(self):
        p = compute_pace(_at(23), _cfg(), GovernorState(), signal=UsageSignal(used_pct_5h=98.0))
        self.assertEqual(p.level, "pause")
