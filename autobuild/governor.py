from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState

WINDOW_SECONDS = 5 * 3600


@dataclass
class Pace:
    level: str          # pause | low | moderate | high
    concurrency: int
    subagents: bool
    model: str          # sonnet | opus


def _parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, cfg: Config) -> bool:
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


def compute_pace(now: datetime, cfg: Config, state: GovernorState) -> Pace:
    if in_quiet_hours(now, cfg):
        return Pace("pause", 0, False, cfg.default_model)
    wk = weekly_headroom(cfg, state)
    if cfg.weekly_reserve_enabled and cfg.weekly_target_usd > 0 and wk < cfg.weekly_reserve_frac:
        return Pace("pause", 0, False, cfg.default_model)
    # Uncalibrated: run conservatively while we learn the ceiling.
    if state.learned_ceiling_usd <= 0:
        return Pace("low", 1, False, "sonnet")
    wh = max(0.0, 1.0 - state.window_spend_usd / state.learned_ceiling_usd)
    # Late-in-window clamp: never start a wide fan-out that will die at the cap.
    late = (state.window_start is not None
            and (now.timestamp() - state.window_start) / WINDOW_SECONDS > 0.8)
    if wh <= 0.05:
        return Pace("pause", 0, False, cfg.default_model)
    if wh < 0.30 or late:
        return Pace("low", 1, False, "sonnet")
    if wh < 0.60:
        return Pace("moderate", min(2, cfg.max_concurrency), False, "sonnet")
    model = "opus" if (cfg.opus_escalation and (not cfg.weekly_reserve_enabled or wk > 0.40)) else "sonnet"
    return Pace("high", cfg.max_concurrency, True, model)
