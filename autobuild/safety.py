"""Guards that decide whether the daemon may start working at all.

These are deliberately separate from the governor. The governor paces work inside a
running daemon against the seat's weekly quota; these decide whether that daemon should be
running unattended in the first place.
"""
from __future__ import annotations

from autobuild.config import Config


def metered_guard(cfg: Config, metered: bool | None = None, allow_metered: bool = False,
                  provider: str | None = None) -> str | None:
    """None when the daemon may build, else the reason it must not.

    A metered backend bills per token. The daemon builds unattended, typically overnight,
    so starting it on a metered provider is how a loop nobody is watching turns into a
    bill nobody expected. The seat has a weekly quota instead of a bill, so it is always
    allowed through here.

    `metered` defaults to None meaning "derive it": asking the runner registry is the only
    way to be sure the flag agrees with the backend actually about to run. A caller that
    passes a stale `cfg.metered` is the failure mode this guards against, so it is not the
    default.
    """
    provider = provider or getattr(cfg, "provider", "claude")
    if metered is None:
        from autobuild.runner import build_runner
        metered = build_runner(provider, cfg).metered
    if not metered:
        return None
    if allow_metered or getattr(cfg, "allow_metered", False):
        return None
    return (
        f"{provider} is a metered backend (billed per token) and this daemon builds unattended. "
        f"Refusing to start work. Set [safety] allow_metered = true in runner.toml to accept "
        f"the cost, or run it supervised."
    )
