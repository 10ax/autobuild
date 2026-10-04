"""What one agent run produced — the shape every layer of the daemon consumes.

This lives on its own so `build.py` (which runs a build) and `runner.py` (which decides how)
can both depend on it without depending on each other.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BuildResult:
    is_error: bool
    cost_usd: float
    usage: dict = field(default_factory=dict)
    session_id: str | None = None
    rate_limited: bool = False
    reset_at: float | None = None
    api_error_status: object = None
    rate_status: str | None = None       # allowed | rejected — from stream rate_limit_event
    rate_reset_at: float | None = None    # 5h-window reset epoch from rate_limit_event
    duration_s: float = 0.0               # wall-clock the build ran (for the notification)
    raw: dict = field(default_factory=dict)
