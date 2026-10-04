from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, date, time
from autobuild.config import Config
from autobuild.ledger import GovernorState
from autobuild.usage import UsageSignal

WINDOW_SECONDS = 5 * 3600


@dataclass
class Pace:
    level: str          # pause | low | moderate | high
    concurrency: int
    subagents: bool
    model: str          # sonnet | opus


_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _parse_day_hm(s: str):
    """'fri 19:00' -> (weekday_int, time). Returns None if empty/malformed."""
    parts = (s or "").strip().lower().split()
    if len(parts) != 2 or parts[0][:3] not in _DAYS:
        return None
    try:
        return _DAYS[parts[0][:3]], _parse_hm(parts[1])
    except (ValueError, IndexError):
        return None


def in_weekend_window(now: datetime, cfg: Config) -> bool:
    """True inside the recurring weekly continuous-work window (default Fri 19:00 → Mon 08:00).
    Handles the wrap across the Sun→Mon week boundary. Disabled if either bound is unset."""
    fr = _parse_day_hm(getattr(cfg, "weekend_from", "") or "")
    to = _parse_day_hm(getattr(cfg, "weekend_to", "") or "")
    if not fr or not to:
        return False
    def wk_minutes(day, t):  # minutes since Monday 00:00
        return day * 1440 + t.hour * 60 + t.minute
    now_min = now.weekday() * 1440 + now.hour * 60 + now.minute
    start, end = wk_minutes(*fr), wk_minutes(*to)
    return start <= now_min < end if start <= end else (now_min >= start or now_min < end)


def in_quiet_hours(now: datetime, cfg: Config) -> bool:
    # Weekends run continuously — never quiet inside the weekend window.
    if in_weekend_window(now, cfg):
        return False
    t = now.timetz().replace(tzinfo=None)
    a, b = _parse_hm(cfg.quiet_from), _parse_hm(cfg.quiet_to)
    return a <= t < b if a <= b else (t >= a or t < b)


def next_active_time(now: datetime, cfg: Config) -> datetime:
    if not in_quiet_hours(now, cfg):
        return now
    b = _parse_hm(cfg.quiet_to)
    cand = now.replace(hour=b.hour, minute=b.minute, second=0, microsecond=0)
    return cand if cand > now else cand + timedelta(days=1)


def weekly_headroom(cfg: Config, state: GovernorState) -> float:
    if not cfg.weekly_reserve_enabled or cfg.weekly_target_usd <= 0:
        return 1.0
    return max(0.0, 1.0 - state.weekly_spend_usd / cfg.weekly_target_usd)


def update_ceiling_ema(state: GovernorState, spend_at_429: float, alpha: float = 0.3) -> GovernorState:
    if state.learned_ceiling_usd <= 0:
        state.learned_ceiling_usd = spend_at_429
    else:
        state.learned_ceiling_usd = (1 - alpha) * state.learned_ceiling_usd + alpha * spend_at_429
    return state


def anchor_window(state: GovernorState, reset_epoch: float) -> GovernorState:
    state.window_start = reset_epoch
    state.window_spend_usd = 0.0
    return state


def roll_day(state: GovernorState, now: datetime, signal: UsageSignal | None) -> GovernorState:
    """Maintain the per-day weekly-% baseline used by the daily cap. The baseline is the LOWEST
    live 7d% seen on the current calendar day (cfg-tz `now`); it re-baselines at midnight and
    whenever the weekly meter drops (i.e. the weekly window reset). No-op without a live 7d%."""
    wk7 = signal.used_pct_7d if signal is not None else None
    if wk7 is None:
        return state
    today = now.date().isoformat()
    if state.day_start != today or state.day_start_pct is None or wk7 < state.day_start_pct:
        state.day_start = today
        state.day_start_pct = wk7
    return state


def record_spend(state: GovernorState, cost_usd: float, now: datetime) -> GovernorState:
    now_ts = now.timestamp()
    if state.window_start is None or now_ts - state.window_start >= WINDOW_SECONDS:
        state.window_start = now_ts
        state.window_spend_usd = 0.0
    state.window_spend_usd += cost_usd
    today = now.date()
    ws = date.fromisoformat(state.week_start) if state.week_start else today
    if (today - ws).days >= 7 or (today - ws).days < 0:
        ws = today
        state.weekly_spend_usd = 0.0
    state.week_start = ws.isoformat()
    state.weekly_spend_usd += cost_usd
    return state


def compute_pace(now: datetime, cfg: Config, state: GovernorState,
                 signal: UsageSignal | None = None) -> Pace:
    if in_quiet_hours(now, cfg):
        return Pace("pause", 0, False, cfg.default_model)
    wk = weekly_headroom(cfg, state)
    if cfg.weekly_reserve_enabled and cfg.weekly_target_usd > 0 and wk < cfg.weekly_reserve_frac:
        return Pace("pause", 0, False, cfg.default_model)
    now_ts = now.timestamp()
    # On a metered provider the weekly quota does not exist, so the guard built around it
    # must not run: its ceiling, its daily cap and its fail-closed rule all describe a
    # subscription nobody is billed for here, and nothing feeds it a 7d% for such a
    # backend — leaving it on would stall the daemon forever. Cost becomes the budget
    # instead, enforced below by the learned ceiling. The clock still gates above.
    if not cfg.metered:
        # Live hard stop: the API currently rejects the 5h window (and overage is disabled/
        # out-of-credits, so there is no spill zone). Trust it over any inference.
        if signal is not None and signal.status == "rejected":
            return Pace("pause", 0, False, cfg.default_model)
        # Weekly usage budget (live 7d %): reserve headroom for the user's own daytime work and
        # cap the daemon's own draw per day. The 7d % is statusline-only, so when the guard is on
        # but the % is unavailable we FAIL CLOSED (pause) rather than run blind past the budget.
        if cfg.weekly_guard_enabled and signal is not None:
            wk7 = signal.used_pct_7d
            if wk7 is None:
                return Pace("pause", 0, False, cfg.default_model)
            # Use-it-or-lose-it: within burst_before_reset_h of the weekly reset the reserved
            # headroom would just expire at the reset, so spend it — suspend the ceiling and the
            # daily cap. Quiet hours (above) and the live 5h limit (above/below) still gate, so this
            # only bursts inside an active window and never past the API's real rate limit. Needs a
            # known reset time; without it we cannot tell we're near a reset, so the caps stand.
            reset7 = signal.reset_at_7d
            bursting = (cfg.burst_before_reset_h > 0 and reset7 is not None
                        and reset7 - now_ts <= cfg.burst_before_reset_h * 3600)
            if not bursting:
                if wk7 >= cfg.weekly_ceiling_pct:                   # reserve the rest for the user
                    return Pace("pause", 0, False, cfg.default_model)
                if state.day_start_pct is not None and wk7 - state.day_start_pct >= cfg.daily_cap_pct:
                    return Pace("pause", 0, False, cfg.default_model)   # per-day draw cap reached
    # Rate-limit cooldown: after a 429 the window is anchored to its FUTURE reset
    # (see anchor_window). Until the clock reaches it we are blocked, so pause — even
    # though window_spend is 0, which would otherwise read as full headroom.
    if state.window_start is not None and now_ts < state.window_start:
        return Pace("pause", 0, False, cfg.default_model)
    # A fully-elapsed window is a fresh window for pacing even if no spend event has
    # reset it yet — this is what lets the governor recover from a rate-limit pause
    # (record_spend, the only other reset path, is not called while paused).
    within_window = (state.window_start is not None
                     and 0 <= now_ts - state.window_start < WINDOW_SECONDS)
    # Headroom: prefer a fresh live 5h percentage (exact); else the EMA-learned ceiling;
    # else we are uncalibrated with no live signal → run conservatively.
    live_pct = signal.used_pct_5h if signal is not None else None
    if live_pct is not None:
        wh = max(0.0, 1.0 - live_pct / 100.0)
    elif state.learned_ceiling_usd > 0:
        effective_spend = state.window_spend_usd if within_window else 0.0
        wh = max(0.0, 1.0 - effective_spend / state.learned_ceiling_usd)
    else:
        return Pace("low", 1, False, "sonnet")
    # Late-in-window clamp: never start a wide fan-out that will die at the cap.
    late = within_window and (now_ts - state.window_start) / WINDOW_SECONDS > 0.8
    if wh <= 0.05:
        return Pace("pause", 0, False, cfg.default_model)
    if wh < 0.30 or late:
        return Pace("low", 1, False, "sonnet")
    if wh < 0.60:
        return Pace("moderate", min(2, cfg.max_concurrency), False, "sonnet")
    model = "opus" if (cfg.opus_escalation and (not cfg.weekly_reserve_enabled or wk > 0.40)) else "sonnet"
    return Pace("high", cfg.max_concurrency, True, model)
