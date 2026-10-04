# autobuild/daemon.py
from __future__ import annotations
import argparse
import subprocess
import sys
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
from autobuild.runner import build_runner
from autobuild import autodoc as _autodoc
from autobuild import improve as _improve
from autobuild import implement as _implement
from autobuild.autodoc import AutodocError
from autobuild.notify import notify
from autobuild.safety import metered_guard
from autobuild.spec import validate_spec
from autobuild.usage import read_signal, write_oracle

# Shared live-signal files. The statusline (any interactive session) writes the snapshot;
# the standalone oracle timer AND this daemon's own builds write the oracle. Freshest wins.
_CLAUDE_DIR = Path.home() / ".claude"

# Lanes that work on a repo which already exists: the daemon prepares a throwaway worktree,
# the agent writes only inside it, and the daemon owns every git write.
_WORKTREE_LANES = ("document", "improve", "implement")


def _target(cfg: Config, item) -> tuple[str, Path]:
    """The lane an item runs in and the repo it works on. `build` items own a fresh dir
    under projects/; `document` and `improve` items point at a repo that already exists."""
    mode = item.meta.get("mode", "build")
    if mode in _WORKTREE_LANES:
        return mode, Path(str(item.meta.get("repo", ""))).expanduser()
    return mode, cfg.root / "projects" / item.meta["slug"]


def _commit_message(item, plan, wip: bool = False) -> str:
    head = "docs(autodoc) WIP:" if wip else "docs(autodoc):"
    return (f"{head} README, CLAUDE.md, working guide and code map\n\n"
            f"Written by autobuild's autodoc lane from {item.path.name}, "
            f"on top of {plan.base_sha[:8]}.\n\n"
            "Co-Authored-By: Claude <noreply@anthropic.com>")


def _quality_commit_message(item, plan, wip: bool = False) -> str:
    head = "chore(quality) WIP:" if wip else "chore(quality):"
    return (f"{head} tests, CI, docs and skills\n\n"
            f"Written by autobuild's quality lane from {item.path.name}, "
            f"on top of {plan.base_sha[:8]}.\n\n"
            "Co-Authored-By: Claude <noreply@anthropic.com>")


def _implement_commit_message(item, plan, wip: bool = False) -> str:
    head = "feat(implement) WIP:" if wip else "feat(implement):"
    title = str(item.meta.get("title") or item.meta.get("slug", "")).strip()
    return (f"{head} {title}\n\n"
            f"Carried out by autobuild's implement lane from {item.path.name}, following "
            f"{item.meta.get('plan', '(plan unset)')}, on top of {plan.base_sha[:8]}.\n\n"
            "Co-Authored-By: Claude <noreply@anthropic.com>")


def _contract(item) -> dict:
    """A contract lane's contract, as the brief states it: which paths may change, which
    files must exist, whose prose may be replaced, and what has to pass."""
    return {k: list(item.meta.get(k, []) or []) for k in ("allow", "verify", "require", "rewrite")}


def _finish_document(item, repo: Path, plan, res: BuildResult, autodoc_ops,
                     runner) -> tuple[str, dict, dict]:
    """Verify the doc set, commit it, and tear the worktree down when green.

    Returns (status, extra ledger fields, extra notification fields). A red run keeps its
    worktree: the WIP commit says what the agent produced, the worktree says what it saw.
    """
    if plan is None:
        reason = str((res.raw or {}).get("autodoc_error", "worktree preparation failed"))
        return "needs-review", {"mode": "document", "repo": str(repo)}, {"reason": reason}

    errs = [] if res.is_error else list(autodoc_ops.verify_docs(plan, runner=runner))
    extra = {"mode": "document", "repo": str(repo), "branch": plan.branch}
    if (not res.is_error) and not errs:
        autodoc_ops.commit_docs(plan, _commit_message(item, plan), runner=runner)
        autodoc_ops.remove_worktree(plan, runner=runner)
        return "done", extra, {"branch": plan.branch}

    autodoc_ops.commit_docs(plan, _commit_message(item, plan, wip=True), runner=runner,
                            wip=True)
    reason = "; ".join(errs[:3]) or str((res.raw or {}).get("error", "agent run failed"))
    extra |= {"worktree": str(plan.worktree), "reason": reason}
    return "needs-review", extra, {"branch": plan.branch, "worktree": str(plan.worktree),
                                   "reason": reason}


def _finish_improve(item, repo: Path, plan, res: BuildResult, improve_ops,
                    runner) -> tuple[str, dict, dict]:
    """Verify the quality run against the brief's contract, commit what it was allowed to
    change, and tear the worktree down when green. Same shape as `_finish_document`: a red
    run keeps its worktree so a human can see what the agent actually did."""
    if plan is None:
        reason = str((res.raw or {}).get("autodoc_error", "worktree preparation failed"))
        return "needs-review", {"mode": "improve", "repo": str(repo)}, {"reason": reason}

    contract = _contract(item)
    errs = ([] if not res.is_error
            else ["agent run failed before the contract could be verified"])
    if not errs:
        errs = list(improve_ops.verify_improve(plan, runner=runner, **contract))
    extra = {"mode": "improve", "repo": str(repo), "branch": plan.branch}
    allow = contract["allow"]
    if (not res.is_error) and not errs:
        improve_ops.commit_improve(plan, _quality_commit_message(item, plan), allow=allow,
                                   runner=runner)
        improve_ops.remove_worktree(plan, runner=runner)
        return "done", extra, {"branch": plan.branch}

    improve_ops.commit_improve(plan, _quality_commit_message(item, plan, wip=True),
                               allow=allow, runner=runner, wip=True)
    reason = "; ".join(errs[:3]) or str((res.raw or {}).get("error", "agent run failed"))
    extra |= {"worktree": str(plan.worktree), "reason": reason}
    return "needs-review", extra, {"branch": plan.branch, "worktree": str(plan.worktree),
                                   "reason": reason}


def _finish_implement(item, repo: Path, plan, res: BuildResult, implement_ops,
                      runner) -> tuple[str, dict, dict]:
    """Verify the implement run against the brief's contract and commit what it was allowed
    to change. Same shape and same gate as `_finish_improve` — this lane is allowed to change
    behaviour, so the plan and the repo's own suite are what say it went right, not a fixed
    file list. A red run keeps its worktree and its WIP commit: an overnight feature that got
    halfway is evidence to read, not damage to undo."""
    if plan is None:
        reason = str((res.raw or {}).get("autodoc_error", "worktree preparation failed"))
        return "needs-review", {"mode": "implement", "repo": str(repo)}, {"reason": reason}

    contract = _contract(item)
    errs = ([] if not res.is_error
            else ["agent run failed before the contract could be verified"])
    if not errs:
        errs = list(implement_ops.verify_implement(plan, runner=runner, **contract))
    extra = {"mode": "implement", "repo": str(repo), "branch": plan.branch}
    allow = contract["allow"]
    if (not res.is_error) and not errs:
        implement_ops.commit_implement(plan, _implement_commit_message(item, plan), allow=allow,
                                       runner=runner)
        implement_ops.remove_worktree(plan, runner=runner)
        return "done", extra, {"branch": plan.branch}

    implement_ops.commit_implement(plan, _implement_commit_message(item, plan, wip=True),
                                   allow=allow, runner=runner, wip=True)
    reason = "; ".join(errs[:3]) or str((res.raw or {}).get("error", "agent run failed"))
    extra |= {"worktree": str(plan.worktree), "reason": reason}
    return "needs-review", extra, {"branch": plan.branch, "worktree": str(plan.worktree),
                                   "reason": reason}


def run_once(cfg: Config, now: datetime, state: GovernorState, state_dir: Path,
             runner=subprocess.run, verifier=verify_repo, builder=run_build,
             oracle_path=None, snapshot_path=None, notifier=notify,
             autodoc_ops=_autodoc, improve_ops=_improve,
             implement_ops=_implement) -> dict:
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

    # Metered safety: a per-token backend plus an unattended overnight loop is a bill nobody
    # is watching. Checked before any work is taken, so a blocked run leaves the backlog
    # untouched rather than marking items building and stalling them. `metered` is derived
    # from the runner rather than trusted from cfg, so this cannot fail open if a caller
    # forgets to stamp the config field.
    blocked = metered_guard(cfg)
    if blocked:
        return {"action": "blocked", "reason": blocked,
                "pace": Pace("pause", 0, False, cfg.default_model)}

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
        mode, repo = _target(cfg, it)
        set_status(it, "building")
        entry = {"slug": slug, "repo": str(repo), "started_at": now.isoformat(),
                 "model": pace.model, "mode": mode}
        plan = None
        if mode in _WORKTREE_LANES:
            # The daemon owns every git write in these lanes; the agent only writes files.
            ops = {"document": autodoc_ops, "improve": improve_ops,
                   "implement": implement_ops}[mode]
            try:
                plan = ops.prepare_worktree(repo, slug, state_dir / "worktrees",
                                            now.strftime("%Y-%m-%d"), runner=runner)
            except AutodocError as e:
                add_lock(lock_path, entry)
                return it, mode, repo, None, BuildResult(
                    is_error=True, cost_usd=0.0, raw={"autodoc_error": str(e)})
            entry["branch"] = plan.branch
        add_lock(lock_path, entry)
        extra = {"mode": mode, "work_dir": plan.worktree} if plan else {}
        res = builder(it.path, repo, pace.model, pace, cfg, runner=runner, **extra)
        return it, mode, repo, plan, res

    results = []
    with ThreadPoolExecutor(max_workers=max(1, pace.concurrency)) as ex:
        for fut in as_completed([ex.submit(_one, it) for it in items]):
            results.append(fut.result())

    for it, mode, repo, plan, res in results:
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
            # Never bank a cost the backend could not report. On a metered provider that
            # means the run was already flagged errored (build.run_build refuses to invent
            # a number); recording 0.0 here would tell the learned ceiling that a real
            # spend was free, so the budget would never advance.
            if not res.raw.get("cost_unreadable"):
                record_spend(state, res.cost_usd, now)
            # Per-build capture: keep the shared oracle warm from a benign (allowed) event.
            if res.rate_status:
                write_oracle(oracle_path, res.rate_status, res.rate_reset_at, "five_hour", now)
            if mode == "document":
                status, extra, note = _finish_document(it, repo, plan, res, autodoc_ops,
                                                       runner)
            elif mode == "improve":
                status, extra, note = _finish_improve(it, repo, plan, res, improve_ops,
                                                      runner)
            elif mode == "implement":
                status, extra, note = _finish_implement(it, repo, plan, res, implement_ops,
                                                        runner)
            else:
                green = (not res.is_error) and verifier(repo, runner=runner)
                status = "done" if green else "needs-review"
                extra, note = {}, {"tests": "green" if green else "red"}
            set_status(it, status)
            append_ledger(ledger_path, {"slug": slug, "status": status,
                                        "cost_usd": res.cost_usd, "duration_s": res.duration_s,
                                        "at": now.isoformat(), **extra})
            notifier(cfg, status, slug=slug, repo=str(repo), cost=res.cost_usd,
                     secs=res.duration_s, runner=runner, **note)
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
    # Resolve the backend once, at startup: an unknown provider or a backend that cannot
    # run is a reason to refuse to start, not to fail every build for a week. `metered`
    # is stamped from the runner rather than read from the file, so the guard can never
    # disagree with the backend actually in use.
    runner = build_runner(cfg.provider, cfg)
    reason = runner.preflight()
    if reason:
        print(f"autobuild: cannot run on provider {cfg.provider!r}: {reason}", file=sys.stderr)
        return 1
    cfg.metered = runner.metered
    if runner.metered:
        print(f"autobuild: provider {cfg.provider!r} is metered; "
              f"allow_metered={cfg.allow_metered}", file=sys.stderr)
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
