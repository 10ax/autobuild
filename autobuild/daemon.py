# autobuild/daemon.py
from __future__ import annotations
import argparse
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from autobuild.config import Config, load_config
from autobuild.ledger import (GovernorState, load_governor_state, save_governor_state,
                              append_ledger)
from autobuild.governor import (Pace, compute_pace, record_spend, update_ceiling_ema,
                                anchor_window, roll_day, next_active_time)
from autobuild.backlog import scan_backlog, select_pending, set_status, add_lock, clear_lock, read_lock
from autobuild.build import run_build, verify_repo, BuildResult
from autobuild.notify import notify
from autobuild.spec import validate_spec
from autobuild.usage import read_signal, write_oracle

# Shared live-signal files. The statusline (any interactive session) writes the snapshot;
# the standalone oracle timer AND this daemon's own builds write the oracle. Freshest wins.
_CLAUDE_DIR = Path.home() / ".claude"


def run_once(cfg: Config, now: datetime, state: GovernorState, state_dir: Path,
             runner=subprocess.run, verifier=verify_repo, builder=run_build,
             oracle_path=None, snapshot_path=None, notifier=notify) -> dict:
    state_dir = Path(state_dir)
    lock_path = state_dir / "current.lock"
    ledger_path = state_dir / "ledger.jsonl"
    gov_path = state_dir / "governor.json"
    oracle_path = Path(oracle_path) if oracle_path else _CLAUDE_DIR / "usage-oracle.json"
    snapshot_path = Path(snapshot_path) if snapshot_path else _CLAUDE_DIR / "usage-snapshot.json"

    # Manual pause: the web console drops a state/pause flag. Honor it even inside an active
    # window — nothing builds until the flag is removed. Distinct from a governor/rate-limit
    # pause (the "manual" marker lets callers and the console tell them apart).
    if (state_dir / "pause").exists():
        return {"action": "pause", "pace": Pace("pause", 0, False, cfg.default_model), "manual": True}

    signal = read_signal(oracle_path, snapshot_path, now)
    roll_day(state, now, signal)          # maintain the per-day weekly-% baseline
    pace = compute_pace(now, cfg, state, signal)
    if pace.level == "pause":
        return {"action": "pause", "pace": pace}

    # Crash recovery first: resume any in-flight slugs before taking new work.
    inflight = {e["slug"] for e in read_lock(lock_path)}
    all_items = scan_backlog(cfg.root / "backlog")
    if inflight:
        items = [i for i in all_items if i.meta.get("slug") in inflight]
    else:
        items = select_pending(all_items, pace.concurrency)
        validated = []
        for it in items:
            errs = validate_spec(it.doc, level="brief")
            if errs:
                ident = it.meta.get("slug") or it.path.stem
                set_status(it, "needs-review")
                append_ledger(ledger_path, {"slug": ident, "status": "needs-review",
                                            "reason": "invalid brief: " + "; ".join(errs),
                                            "at": now.isoformat()})
                notifier(cfg, "needs-review", slug=ident, repo="",
                         reason="invalid brief: " + "; ".join(errs[:3]), runner=runner)
                continue
            validated.append(it)
        items = validated
    if not items:
        return {"action": "idle", "pace": pace}

    def _one(it):
        slug = it.meta["slug"]
        repo = cfg.root / "projects" / slug
        set_status(it, "building")
        add_lock(lock_path, {"slug": slug, "repo": str(repo),
                             "started_at": now.isoformat(), "model": pace.model})
        res = builder(it.path, repo, pace.model, pace, cfg, runner=runner)
        return it, repo, res

    results = []
    with ThreadPoolExecutor(max_workers=max(1, pace.concurrency)) as ex:
        for fut in as_completed([ex.submit(_one, it) for it in items]):
            results.append(fut.result())

    for it, repo, res in results:
        slug = it.meta["slug"]
        if res.rate_limited:
            # Calibrate the ceiling from the spend at the 429 BEFORE anchoring resets it.
            update_ceiling_ema(state, state.window_spend_usd or res.cost_usd)
            # Prefer the structured rate_limit_event reset; anchor so we pause until it reopens.
            reset = res.rate_reset_at or res.reset_at
            if reset:
                anchor_window(state, reset)
            # Persist the live status so the very next tick's signal reflects it immediately.
            write_oracle(oracle_path, res.rate_status or "rejected", reset, "five_hour", now)
            set_status(it, "pending")
            reset_str = (", resets " + datetime.fromtimestamp(reset, now.tzinfo).strftime("%a %H:%M")
                         if reset else "")
            notifier(cfg, "paused", reason=f"rate limited on {slug}{reset_str}", runner=runner)
        else:
            record_spend(state, res.cost_usd, now)
            # Per-build capture: keep the shared oracle warm from a benign (allowed) event.
            if res.rate_status:
                write_oracle(oracle_path, res.rate_status, res.rate_reset_at, "five_hour", now)
            green = (not res.is_error) and verifier(repo, runner=runner)
            status = "done" if green else "needs-review"
            set_status(it, status)
            append_ledger(ledger_path, {"slug": slug, "status": status,
                                        "cost_usd": res.cost_usd, "duration_s": res.duration_s,
                                        "at": now.isoformat()})
            notifier(cfg, "done" if green else "needs-review", slug=slug, repo=str(repo),
                     tests="green" if green else "red", cost=res.cost_usd,
                     secs=res.duration_s, runner=runner)
        clear_lock(lock_path, slug)

    save_governor_state(gov_path, state)
    return {"action": "built", "pace": pace, "results": results}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="autobuild")
    ap.add_argument("--config", default="config/runner.toml")
    ap.add_argument("--once", action="store_true", help="run one tick and exit")
    ap.add_argument("--poll", type=int, default=300, help="seconds between ticks")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    tz = ZoneInfo(cfg.timezone)
    state_dir = cfg.root / "state"

    while True:
        state = load_governor_state(state_dir / "governor.json")
        now = datetime.now(tz)
        try:
            res = run_once(cfg, now, state, state_dir)
        except Exception as e:  # never die silently
            notify(cfg, "crash", error=str(e))
            if args.once:
                return 1
            time.sleep(args.poll)
            continue
        if args.once:
            return 0
        if res["action"] == "pause":
            sleep_until = next_active_time(now, cfg)
            time.sleep(max(args.poll, min(3600, (sleep_until - now).total_seconds())))
        else:
            time.sleep(args.poll)
