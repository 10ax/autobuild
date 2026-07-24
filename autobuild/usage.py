from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path

# A live signal is "fresh" within this age. The oracle timer fires ~every 7 min, so its
# file stays within this window; the statusline snapshot is interactive-only and often goes
# stale overnight → its percentage fields simply drop out and the governor falls back to EMA.
DEFAULT_MAX_AGE_S = 900


@dataclass
class UsageSignal:
    status: str | None = None          # "allowed" | "rejected" (5h window), freshest source
    reset_at: float | None = None       # 5h-window reset epoch
    used_pct_5h: float | None = None    # 0..100, statusline-only
    used_pct_7d: float | None = None
    reset_at_7d: float | None = None


def _read_json(path) -> dict | None:
    try:
        obj = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _fresh(obj: dict | None, now_ts: float, max_age_s: float) -> bool:
    if not isinstance(obj, dict):
        return False
    wa = obj.get("written_at")
    if not isinstance(wa, (int, float)) or isinstance(wa, bool):
        return False
    return 0 <= now_ts - float(wa) <= max_age_s   # not stale, not from the future


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def read_signal(oracle_path, snapshot_path, now, max_age_s: float = DEFAULT_MAX_AGE_S) -> UsageSignal:
    """Merge the (status+reset) oracle file and the (percentages) statusline snapshot into one
    signal, dropping any source older than max_age_s. Robust to missing/corrupt files."""
    now_ts = now.timestamp()
    sig = UsageSignal()

    o = _read_json(oracle_path)
    if _fresh(o, now_ts, max_age_s):
        if isinstance(o.get("status"), str):
            sig.status = o["status"]
        sig.reset_at = _num(o.get("reset_at"))

    s = _read_json(snapshot_path)
    if _fresh(s, now_ts, max_age_s):
        fh = s.get("five_hour") or {}
        sig.used_pct_5h = _num(fh.get("used_percentage"))
        if sig.reset_at is None:                       # oracle's API reset preferred; snapshot fills in
            sig.reset_at = _num(fh.get("reset_at"))
        sd = s.get("seven_day") or {}
        sig.used_pct_7d = _num(sd.get("used_percentage"))
        sig.reset_at_7d = _num(sd.get("reset_at"))
    return sig


def write_oracle(path, status, reset_at, rate_limit_type, now) -> None:
    """Atomically write the status+reset oracle file (used by the daemon on a build's
    rate_limit_event; the standalone oracle timer writes the same shape)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "reset_at": reset_at,
               "rate_limit_type": rate_limit_type, "written_at": now.timestamp()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)
