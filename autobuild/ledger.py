from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


def append_ledger(path: Path, entry: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_ledger(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # tolerate a torn final line from a crash mid-append
    return rows


@dataclass
class GovernorState:
    week_start: str = ""            # ISO date; "" means uninitialised
    window_start: float | None = None  # epoch seconds of current 5h window
    window_spend_usd: float = 0.0
    learned_ceiling_usd: float = 0.0   # 0 => not yet calibrated
    weekly_spend_usd: float = 0.0
    day_start: str = ""                # ISO date of the current daily-cap day
    day_start_pct: float | None = None  # weekly-% baseline at day start (lowest 7d% seen today)


def load_governor_state(path: Path) -> GovernorState:
    path = Path(path)
    if not path.exists():
        return GovernorState()
    try:
        return GovernorState(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError, ValueError):
        return GovernorState()  # corrupt/incompatible → fresh state (overwritten on next save)


def save_governor_state(path: Path, state: GovernorState) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, path)  # atomic
