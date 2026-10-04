from __future__ import annotations
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from autobuild.config import Config
from autobuild.governor import Pace
from autobuild.result import BuildResult
from autobuild.runner import build_runner

_RESET = re.compile(r'(?:reset_at|resetsAt|"reset")\D{0,4}(\d{10})')
_RL_MARKERS = ("rate limit", "rate_limit", "usage limit", "429", "exceeded your")
# For the structured api_error_status subtree, match descriptive phrases only — never a
# bare "429" substring, which would false-positive on ids/tokens that merely contain it
# (a false positive there costs a needless multi-hour pause). Exact "429" status is caught
# numerically below. "429" stays in _RL_MARKERS for the error-gated whole-object fallback.
_RL_TEXT = ("rate limit", "rate_limit", "usage limit", "exceeded your")
_RESET_KEYS = ("reset_at", "resets_at", "resetAt", "resetsAt", "reset")
_MSG_FIELDS = ("result", "error", "subtype", "stop_reason", "terminal_reason")
_STATUS_KEYS = ("status", "code", "status_code", "statusCode", "http_status", "httpStatus")


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


def _looks_rate_limited(val) -> bool:
    """Whether `api_error_status` (shape unconfirmed) indicates a rate/usage limit.

    Applied ONLY to the small, error-specific api_error_status subtree — never the
    whole result — so a build whose *content* mentions "rate limit" is not misread.
    """
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return int(val) == 429
    if isinstance(val, str):
        low = val.lower()
        return val.strip() == "429" or any(m in low for m in _RL_TEXT)
    if isinstance(val, dict):
        for k in _STATUS_KEYS:
            v = val.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) == 429:
                return True
            if isinstance(v, str) and v.strip() == "429":
                return True
        return any(_looks_rate_limited(v) for v in val.values())
    if isinstance(val, (list, tuple)):
        return any(_looks_rate_limited(v) for v in val)
    return False


def _extract_reset(val) -> float | None:
    """Pull a 10-digit epoch reset time out of a structured api_error_status subtree."""
    if isinstance(val, dict):
        for k in _RESET_KEYS:
            v = val.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 1_000_000_000:
                return float(v)
            if isinstance(v, str) and v.strip().isdigit() and len(v.strip()) >= 10:
                return float(v.strip())
        for v in val.values():
            r = _extract_reset(v)
            if r is not None:
                return r
    elif isinstance(val, (list, tuple)):
        for v in val:
            r = _extract_reset(v)
            if r is not None:
                return r
    return None


# The worktree lanes' prompts. `prompts/<lane>.md` is the source of truth when present; these
# constants keep a lane working (and unit-testable) if the file is missing.
_LANE_PROMPTS = {
    "document": (
        "Document the existing repo: {REPO}. Read and write ONLY inside the worktree "
        "{WORKTREE} (a git worktree of that repo — cd there first). Brief: {BRIEF_PATH}. "
        "Follow the process in {ROOT}/AUTODOC.md exactly."
    ),
    "improve": (
        "Bring the existing repo {REPO} up to the quality standard in the brief {BRIEF_PATH}. "
        "Read and write ONLY inside the worktree {WORKTREE} (a git worktree of that repo — "
        "cd there first). Follow the process in {ROOT}/IMPROVE.md exactly."
    ),
    "implement": (
        "Carry out the implementation plan named by the brief {BRIEF_PATH}, in the existing "
        "repo {REPO}. Read and write ONLY inside the worktree {WORKTREE} (a git worktree of "
        "that repo — cd there first). Follow the process in {ROOT}/IMPLEMENT.md exactly."
    ),
}
_LANE_PROMPT_FILES = {"document": "autodoc.md", "improve": "improve.md",
                      "implement": "implement.md"}


def _lane_prompt(mode: str, brief_path: Path, repo_root: Path, work_dir: Path,
                 cfg: Config) -> str:
    template = _LANE_PROMPTS[mode]
    f = Path(cfg.root) / "prompts" / _LANE_PROMPT_FILES[mode]
    try:
        text = f.read_text().strip()
        if text:
            template = text
    except OSError:
        pass
    return (template.replace("{REPO}", str(repo_root))
                    .replace("{WORKTREE}", str(work_dir))
                    .replace("{BRIEF_PATH}", str(brief_path))
                    .replace("{ROOT}", str(Path(cfg.root))))


def _build_prompt(brief_path: Path, repo_root: Path, cfg: Config, mode: str = "build",
                  work_dir: Path | None = None) -> str:
    """The prompt for one run. A lane's prompts/<lane>.md is the source of truth when
    present; the constants keep a lane working (and unit-testable) if it is missing."""
    if mode in _LANE_PROMPTS:
        return _lane_prompt(mode, brief_path, repo_root, Path(work_dir or repo_root), cfg)
    return (f"Build backlog item: {brief_path}. Target repo dir: {repo_root}. "
            f"Follow the process in {Path(cfg.root)}/CLAUDE.md exactly.")


def build_argv(brief_path: Path, repo_root: Path, model: str, cfg: Config,
               mode: str = "build", work_dir: Path | None = None) -> list[str]:
    """Kept for the seat path and its existing tests; the provider seam is runner.py."""
    prompt = _build_prompt(brief_path, repo_root, cfg, mode=mode, work_dir=work_dir)
    return build_runner(getattr(cfg, "provider", "claude"), cfg).argv(
        prompt, str(repo_root), model, work_dir=str(work_dir) if work_dir else None)


def _iter_json(stdout: str):
    """Yield JSON objects from build stdout: a batch object (compact or pretty) as one, or
    stream-json as one object per line."""
    s = stdout.strip()
    if not s:
        return
    try:
        yield json.loads(s)     # batch --output-format json (single object)
        return
    except json.JSONDecodeError:
        pass
    for line in s.splitlines():  # stream-json: one event per line
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def parse_result(stdout: str) -> BuildResult:
    events = [e for e in _iter_json(stdout) if isinstance(e, dict)]
    if not events:
        return BuildResult(is_error=True, cost_usd=0.0, raw={})

    # The final result: an explicit stream `result` event, else (batch) the object carrying
    # the result fields.
    obj = next((e for e in events if e.get("type") == "result"), None)
    if obj is None:
        obj = next((e for e in reversed(events)
                    if "total_cost_usd" in e or "is_error" in e), {})

    # Real-time limit status from any stream `rate_limit_event` (prefer the five_hour window).
    rate_status = None
    rate_reset_at = None
    for e in events:
        if e.get("type") == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            typ = info.get("rateLimitType")
            if rate_status is None or typ == "five_hour":
                if info.get("status") is not None:
                    rate_status = info.get("status")
                rt = info.get("resetsAt")
                if isinstance(rt, (int, float)) and not isinstance(rt, bool):
                    rate_reset_at = float(rt)

    is_error = bool(obj.get("is_error", False))
    api_error_status = obj.get("api_error_status")

    # Primary signals: a rejected rate_limit_event, or the structured api_error_status field.
    rate_limited = (rate_status == "rejected") or _looks_rate_limited(api_error_status)
    # Fallback for older/unknown shapes: scan only the human MESSAGE fields (never ids or
    # the whole object), and only on an errored run — so neither a benign id containing
    # "429" nor a *successful* build whose content mentions "rate limit" is misread.
    if not rate_limited and is_error:
        msg = " ".join(str(obj.get(k, "")) for k in _MSG_FIELDS).lower()
        rate_limited = any(m in msg for m in _RL_MARKERS)

    # Reset time: prefer the structured rate_limit_event, then api_error_status, then a regex.
    reset_at = rate_reset_at
    if reset_at is None:
        reset_at = _extract_reset(api_error_status)
    if reset_at is None:
        m = _RESET.search(json.dumps(obj))
        reset_at = float(m.group(1)) if m else None

    return BuildResult(
        is_error=is_error,
        cost_usd=float(obj.get("total_cost_usd", 0.0) or 0.0),
        usage=obj.get("usage", {}) or {},
        session_id=obj.get("session_id"),
        rate_limited=rate_limited,
        reset_at=reset_at,
        api_error_status=api_error_status,
        rate_status=rate_status,
        rate_reset_at=rate_reset_at,
        raw=obj,
    )


def run_build(brief_path: Path, repo_root: Path, model: str, pace: Pace,
              cfg: Config, mode: str = "build", work_dir: Path | None = None,
              runner=subprocess.run, clock=time.monotonic,
              provider_runner=None) -> BuildResult:
    # `runner` is the injectable process runner tests stand in for; `provider_runner` is
    # the backend (seat vs metered). Keeping them separate means every existing test that
    # fakes the process call keeps working, and the backend stays swappable.
    if provider_runner is None:
        provider_runner = build_runner(getattr(cfg, "provider", "claude"), cfg)
    prompt = _build_prompt(brief_path, repo_root, cfg, mode=mode, work_dir=work_dir)
    argv = provider_runner.argv(prompt, str(repo_root), model,
                                work_dir=str(work_dir) if work_dir else None)
    # AUTOBUILD_NO_NOTIFY marks THIS build's headless agent (and any subagents) so its Stop hook
    # stays quiet — the daemon sends its own richer ✅/⚠️. Scoped to the build env only, never the
    # daemon, so the daemon's own notify path is unaffected.
    env = dict(os.environ, PACE=("high" if pace.subagents else "low"), AUTOBUILD_NO_NOTIFY="1")
    start = clock()
    try:
        cp = runner(argv, capture_output=True, text=True,
                    timeout=cfg.per_project_timeout_min * 60, env=env,
                    **({"cwd": str(work_dir)} if work_dir else {}))
    except subprocess.TimeoutExpired as e:
        # A killed build's captured stdout may still carry cost / a rate_limit_event — recover what
        # we can (keeps weekly spend + the oracle warm) but the run is incomplete, so flag error.
        partial = e.stdout or e.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        result = provider_runner.parse(partial)
        result.is_error = True
        result.duration_s = clock() - start
        result.raw = {**(result.raw or {}), "timeout": True}
        return result
    except OSError as e:
        return BuildResult(is_error=True, cost_usd=0.0,
                           duration_s=clock() - start, raw={"error": str(e)})
    result = provider_runner.parse(cp.stdout or "")
    result.duration_s = clock() - start
    rc = getattr(cp, "returncode", 0)
    if rc != 0:
        result.is_error = True
        result.raw = {**(result.raw or {}), "returncode": rc}
    # A metered backend prints no cost, so read it back from the finished session; the
    # governor's budget is built on that number. An unreadable cost must NEVER be reported
    # as 0.0: record_spend would bank a free build and the learned ceiling would never
    # advance, so real money could be spent while the governor believed nothing was. Treat
    # it as an errored run and say so, rather than inventing a number.
    if provider_runner.metered:
        usage = None
        if result.session_id:
            usage = getattr(provider_runner, "session_usage", lambda *_: None)(result.session_id)
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            result.is_error = True
            result.raw = {**(result.raw or {}),
                          "cost_unreadable": (usage if usage is None else "not a number")}
            return result
        provider_runner.apply_usage(usage)
        result.cost_usd = float(cost)
        tokens = usage.get("tokens")
        if isinstance(tokens, dict):
            result.usage = dict(tokens)
    return result


# The verify commands, per package manager. pnpm is what the playbook scaffolds with
# now; npm stays for any repo that still carries a package-lock.json (the six projects
# built before the switch were moved to pnpm on 2026-09-25), because verifying one of
# those with pnpm fails on a lockfile it will not read — an infrastructure failure
# reported as a broken build.
_VERIFY_CMDS = {
    "pnpm": (["pnpm", "test"], ["pnpm", "exec", "tsc", "--noEmit"]),
    "npm": (["npm", "test", "--silent"], ["npx", "tsc", "--noEmit"]),
}


def package_manager(repo_root: Path) -> str:
    """Which package manager this repo speaks, read off its lockfile.

    pnpm wins when both are present: that is a migration in progress, and the pnpm
    lockfile is the newer intent. No lockfile at all means a build died before its
    first install, and pnpm is what the next one will produce.
    """
    if (repo_root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_root / "package-lock.json").exists():
        return "npm"
    return "pnpm"


def verify_repo(repo_root: Path, runner=subprocess.run, timeout: int = 600) -> bool:
    for cmd in _VERIFY_CMDS[package_manager(repo_root)]:
        try:
            cp = runner(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return False  # missing tool or hung test → treat as failed verify
        if cp.returncode != 0:
            return False
    return True
