"""Guards that decide whether the daemon may start working at all.

These are deliberately separate from the governor. The governor paces work inside a
running daemon against the seat's weekly quota; these decide whether that daemon should be
running unattended in the first place.
"""
from __future__ import annotations

from autobuild.config import Config


def metered_guard(cfg: Config, metered: bool, allow_metered: bool,
                  provider: str | None = None) -> str | None:
    """None when the daemon may build, else the reason it must not.

    A metered backend bills per token. The daemon builds unattended, typically overnight,
    so starting it on a metered provider is how a loop nobody is watching turns into a
    bill nobody expected. The seat has a weekly quota instead of a bill, so it is always
    allowed through here.
    """
    if not metered:
        return None
    if allow_metered:
        return None
    who = provider or "the configured provider"
    return (
        f"{who} is a metered backend (billed per token) and this daemon builds unattended. "
        f"Refusing to start work. Set [safety] allow_metered = true in runner.toml to accept "
        f"the cost, or run it supervised."
    )
