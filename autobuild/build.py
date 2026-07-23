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


@dataclass
class BuildResult:
    is_error: bool
    cost_usd: float
    usage: dict = field(default_factory=dict)
    session_id: str | None = None
    rate_limited: bool = False
    reset_at: float | None = None
    raw: dict = field(default_factory=dict)


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
    text = json.dumps(obj).lower()
    rate_limited = any(m in text for m in _RL_MARKERS)
    m = _RESET.search(json.dumps(obj))
    reset_at = float(m.group(1)) if m else None
    return BuildResult(
        is_error=bool(obj.get("is_error", False)),
        cost_usd=float(obj.get("total_cost_usd", 0.0) or 0.0),
        usage=obj.get("usage", {}) or {},
        session_id=obj.get("session_id"),
        rate_limited=rate_limited,
        reset_at=reset_at,
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
    return parse_result(cp.stdout or "")


def verify_repo(repo_root: Path, runner=subprocess.run) -> bool:
    for cmd in (["npm", "test", "--silent"], ["npx", "tsc", "--noEmit"]):
        cp = runner(cmd, cwd=str(repo_root), capture_output=True, text=True)
        if cp.returncode != 0:
            return False
    return True
