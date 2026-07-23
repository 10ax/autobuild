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
    max_concurrency: int = 3
    per_project_timeout_min: int = 30
    opus_escalation: bool = True
    weekly_reserve_enabled: bool = False
    weekly_reserve_frac: float = 0.12
    weekly_target_usd: float = 0.0
    stack: str = "typescript"
    default_model: str = "sonnet"
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
    cfg = Config(
        root=Path(root).resolve() if root else path.resolve().parent.parent,
        timezone=hours.get("timezone", "Europe/Rome"),
        quiet_from=hours.get("quiet_from", "08:00"),
        quiet_to=hours.get("quiet_to", "19:00"),
        max_concurrency=int(pace.get("max_concurrency", 3)),
        per_project_timeout_min=int(pace.get("per_project_timeout_min", 30)),
        opus_escalation=bool(pace.get("opus_escalation", True)),
        weekly_reserve_enabled=bool(pace.get("weekly_reserve_enabled", False)),
        weekly_reserve_frac=float(pace.get("weekly_reserve_frac", 0.12)),
        weekly_target_usd=float(pace.get("weekly_target_usd", 0.0)),
        stack=build.get("stack", "typescript"),
        default_model=build.get("default_model", "sonnet"),
        telegram_script=notify.get("telegram_script", "~/.claude/notify-telegram.sh"),
        notify_on=list(notify.get("notify_on", ["done", "needs-review", "paused", "crash"])),
    )
    if cfg.max_concurrency < 1:
        raise ConfigError("pace.max_concurrency must be >= 1")
    if cfg.default_model not in ("sonnet", "opus"):
        raise ConfigError("build.default_model must be sonnet or opus")
    return cfg
