from __future__ import annotations
import json
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
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@dataclass
class GovernorState:
    week_start: str = ""            # ISO date; "" means uninitialised
    window_start: float | None = None  # epoch seconds of current 5h window
    window_spend_usd: float = 0.0
    learned_ceiling_usd: float = 0.0   # 0 => not yet calibrated
    weekly_spend_usd: float = 0.0


def load_governor_state(path: Path) -> GovernorState:
    path = Path(path)
    if not path.exists():
        return GovernorState()
    return GovernorState(**json.loads(path.read_text()))


def save_governor_state(path: Path, state: GovernorState) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2))
