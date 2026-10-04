from __future__ import annotations
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class Config:
    root: Path
    timezone: str = "Europe/Rome"
    quiet_from: str = "08:00"
    quiet_to: str = "19:00"
    # Weekends run continuously (no daytime quiet). "day HH:MM"; set both "" to disable.
    weekend_from: str = "fri 19:00"
    weekend_to: str = "mon 08:00"
    max_concurrency: int = 3
    per_project_timeout_min: int = 30
    opus_escalation: bool = True
    weekly_reserve_enabled: bool = False
    weekly_reserve_frac: float = 0.12
    weekly_target_usd: float = 0.0
    # Live weekly-usage guard (uses the statusline 7d %). Daemon pauses at weekly_ceiling_pct
    # (reserve the rest for the user) and adds at most daily_cap_pct weekly-% points per day.
    weekly_guard_enabled: bool = True
    weekly_ceiling_pct: float = 50.0
    daily_cap_pct: float = 10.0
    # Use-it-or-lose-it: within this many hours of the weekly (7d) reset, the reserved headroom
    # would just expire, so the guard spends it — ceiling + daily cap suspended (quiet hours and
    # the live 5h limit still apply). 0 disables the burst.
    burst_before_reset_h: float = 24.0
    stack: str = "typescript"
    default_model: str = "sonnet"
    # Which provider a build runs on. "claude" is the subscription seat (a weekly quota);
    # "opencode" is a metered provider billed per token. See runner.py.
    provider: str = "claude"
    # Metered backends bill per token, so an unattended loop spends real money. Off by
    # default: the daemon refuses to build until this is explicitly turned on.
    allow_metered: bool = False
    # Model ids for the metered backend. The governor escalates a "tier" (sonnet/opus),
    # which are Claude's names; these map a tier to a real id per provider.
    opencode_model: str = "opencode-go/deepseek-v4.1-flash"
    opencode_model_high: str = "opencode-go/deepseek-v4-pro"
    # Set by the daemon from the resolved runner. NOT read from the toml: a flag that
    # could be typed by hand is a flag that can disagree with the backend actually used.
    metered: bool = False
    telegram_script: str = "~/.claude/notify-telegram.sh"
    notify_on: list[str] = field(
        default_factory=lambda: ["done", "needs-review", "paused", "crash"]
    )


def load_config(path: Path, root: Path | None = None) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    data = tomllib.loads(path.read_text())
    hours, pace = data.get("hours", {}), data.get("pace", {})
    build, notify = data.get("build", {}), data.get("notify", {})
    runner_section, safety = data.get("runner", {}), data.get("safety", {})
    cfg = Config(
        root=Path(root).resolve() if root else path.resolve().parent.parent,
        timezone=hours.get("timezone", "Europe/Rome"),
        quiet_from=hours.get("quiet_from", "08:00"),
        quiet_to=hours.get("quiet_to", "19:00"),
        weekend_from=hours.get("weekend_from", "fri 19:00"),
        weekend_to=hours.get("weekend_to", "mon 08:00"),
        max_concurrency=int(pace.get("max_concurrency", 3)),
        per_project_timeout_min=int(pace.get("per_project_timeout_min", 30)),
        opus_escalation=bool(pace.get("opus_escalation", True)),
        weekly_reserve_enabled=bool(pace.get("weekly_reserve_enabled", False)),
        weekly_reserve_frac=float(pace.get("weekly_reserve_frac", 0.12)),
        weekly_target_usd=float(pace.get("weekly_target_usd", 0.0)),
        weekly_guard_enabled=bool(pace.get("weekly_guard_enabled", True)),
        weekly_ceiling_pct=float(pace.get("weekly_ceiling_pct", 50.0)),
        daily_cap_pct=float(pace.get("daily_cap_pct", 10.0)),
        burst_before_reset_h=float(pace.get("burst_before_reset_h", 24.0)),
        stack=build.get("stack", "typescript"),
        default_model=build.get("default_model", "sonnet"),
        provider=str(runner_section.get("provider", "claude")),
        allow_metered=bool(safety.get("allow_metered", False)),
        telegram_script=notify.get("telegram_script", "~/.claude/notify-telegram.sh"),
        notify_on=list(notify.get("notify_on", ["done", "needs-review", "paused", "crash"])),
    )
    if cfg.max_concurrency < 1:
        raise ConfigError("pace.max_concurrency must be >= 1")
    if cfg.default_model not in ("sonnet", "opus"):
        raise ConfigError("build.default_model must be sonnet or opus")
    from autobuild.runner import _BY_NAME as _RUNNERS
    if cfg.provider not in _RUNNERS:
        raise ConfigError(
            f"runner.provider must be one of {', '.join(sorted(_RUNNERS))}, got {cfg.provider!r}"
        )
    return cfg
