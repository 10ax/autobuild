"""Which systemd units belong to which provider — deploy facts, in one place.

`autobuild.service` and the web console are needed by any backend. The rate-limit oracle
and the usage feed are not: they exist only to keep a live reading of the *subscription's*
weekly quota, which a metered provider does not have. Keeping that distinction here means
`install.sh` does not have to know it, and a test can hold it.
"""
from __future__ import annotations

from pathlib import Path

_DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"

# Needed whichever backend runs.
COMMON_UNITS = ("autobuild.service", "autobuild-web.service")

# Only meaningful against the seat's weekly quota: the oracle pings the API for a live
# 5h/7d reset, and the feed keeps an interactive agent session alive to refresh the 7d %.
# Nothing reads either on a metered provider.
_SEAT_UNITS = ("usage-feed.service", "ratelimit-oracle.service", "ratelimit-oracle.timer")

_PROVIDERS = {"claude", "opencode"}


def seat_units() -> tuple[str, ...]:
    """The units that only exist to meter the subscription."""
    return _SEAT_UNITS


def units_for_provider(provider: str) -> tuple[str, ...]:
    """Every unit to install for a backend, metering dropped when there is nothing to meter."""
    if provider not in _PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of "
                         f"{', '.join(sorted(_PROVIDERS))}")
    if provider == "claude":
        return COMMON_UNITS + _SEAT_UNITS
    return COMMON_UNITS


def unit_files(name: str) -> Path:
    """Where a unit's source file lives in the repo."""
    return _DEPLOY_DIR / name
