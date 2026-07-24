from __future__ import annotations
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from autobuild.config import Config
from autobuild.governor import Pace

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


def build_argv(brief_path: Path, repo_root: Path, model: str, cfg: Config) -> list[str]:
    prompt = (f"Build backlog item: {brief_path}. Target repo dir: {repo_root}. "
              f"Follow the process in {Path(cfg.root)}/CLAUDE.md exactly.")
    return ["claude", "-p", prompt, "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(cfg.root), "--model", model]


def _load_json(stdout: str) -> dict:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        for line in reversed(stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def parse_result(stdout: str) -> BuildResult:
    obj = _load_json(stdout)
    if not obj:
        return BuildResult(is_error=True, cost_usd=0.0, raw={})
    is_error = bool(obj.get("is_error", False))
    api_error_status = obj.get("api_error_status")

    # Primary signal: the structured api_error_status field from headless `claude -p`.
    rate_limited = _looks_rate_limited(api_error_status)
    # Fallback for older/unknown shapes: scan only the human MESSAGE fields (never ids or
    # the whole object), and only on an errored run — so neither a benign id containing
    # "429" nor a *successful* build whose content mentions "rate limit" is misread.
    if not rate_limited and is_error:
        msg = " ".join(str(obj.get(k, "")) for k in _MSG_FIELDS).lower()
        rate_limited = any(m in msg for m in _RL_MARKERS)

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
        raw=obj,
    )


def run_build(brief_path: Path, repo_root: Path, model: str, pace: Pace,
              cfg: Config, runner=subprocess.run) -> BuildResult:
    argv = build_argv(brief_path, repo_root, model, cfg)
    env = dict(os.environ, PACE=("high" if pace.subagents else "low"))
    try:
        cp = runner(argv, capture_output=True, text=True,
                    timeout=cfg.per_project_timeout_min * 60, env=env)
    except subprocess.TimeoutExpired:
        return BuildResult(is_error=True, cost_usd=0.0, raw={"timeout": True})
    except OSError as e:
        return BuildResult(is_error=True, cost_usd=0.0, raw={"error": str(e)})
    result = parse_result(cp.stdout or "")
    rc = getattr(cp, "returncode", 0)
    if rc != 0:
        result.is_error = True
        result.raw = {**(result.raw or {}), "returncode": rc}
    return result


def verify_repo(repo_root: Path, runner=subprocess.run, timeout: int = 600) -> bool:
    for cmd in (["npm", "test", "--silent"], ["npx", "tsc", "--noEmit"]):
        try:
            cp = runner(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return False  # missing tool or hung test → treat as failed verify
        if cp.returncode != 0:
            return False
    return True
