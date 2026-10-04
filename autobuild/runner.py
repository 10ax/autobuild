"""Where a build's work actually runs: one runner per provider backend.

`build.run_build` used to build the `claude` argv and parse its stream inline, so the seat
was the only possible backend and the parser could not be exercised without the CLI. A
runner owns three things and nothing else:

  argv()   -> the command line for one agent run
  parse()  -> stdout -> BuildResult (the shape the daemon already consumes)
  metered  -> whether this backend bills per token

`metered` is the load-bearing flag: the governor's weekly ceiling and per-day cap exist to
ration a *weekly subscription quota*, which a pay-per-token provider does not have. Callers
must not run the seat's weekly guard against a metered backend.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from autobuild.result import BuildResult
from autobuild.config import Config

SEAT = ("claude",)
METERED = ("opencode",)


class Runner:
    """Protocol. Subclasses supply argv/parse and declare whether they bill per token."""

    name = "runner"
    metered = False

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def argv(self, prompt: str, repo_root: str, model: str,
             work_dir: str | None = None) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str) -> BuildResult:
        raise NotImplementedError

    def resolve_model(self, tier: str) -> str:
        """Turn the governor's model tier into this backend's model id.

        The governor escalates between 'sonnet' and 'opus'. Those are Claude's names; a
        backend that does not know them must translate, or every run is handed a model that
        does not exist. A literal model id is passed through untouched.
        """
        return tier

    def apply_usage(self, usage: dict | None) -> None:
        """Attach cost/usage the backend reports out of band. No-op where the CLI
        already prints cost in its own stream."""
        return None

    # --- preflight -------------------------------------------------------------------
    def preflight(self) -> str | None:
        """Return None when this backend is usable, else a human-readable reason.

        Checked before the daemon takes work: an unattended runner that starts and then
        fails every build is worse than one that refuses to start and says why.
        """
        exe = self.argv("ping", ".", "unused")[0]
        if shutil.which(exe) is None:
            return f"{exe} not found in PATH"
        return None


class ClaudeSeatRunner(Runner):
    """Claude Code on the subscription seat — the original, unchanged behaviour."""

    name = "claude"
    metered = False

    def argv(self, prompt, repo_root, model, work_dir=None) -> list[str]:
        # stream-json (+ --verbose, required for it in print mode) so each build also emits the
        # `rate_limit_event` carrying the real limit status + exact resetsAt. The final `result`
        # event still carries everything the batch json result did.
        #
        # --add-dir is the whole filesystem scope of a bypassPermissions build: the autobuild
        # root (playbooks, CLAUDE.md) plus the lane's worktree. `repo_root` is named in the
        # prompt but deliberately NOT granted — a worktree lane works in its worktree, and the
        # user's checkout must stay out of reach.
        argv = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "bypassPermissions", "--add-dir", str(self.cfg.root)]
        if work_dir is not None:
            argv += ["--add-dir", str(work_dir)]
        return argv + ["--model", model]

    def parse(self, stdout: str) -> BuildResult:
        # Imported here, not at module scope: build.py imports build_runner() from this
        # module, so a top-level import of its parser would be circular.
        from autobuild.build import parse_result
        return parse_result(stdout)


class OpenCodeRunner(Runner):
    """OpenCode against a metered provider.

    Two differences from the seat that the rest of the daemon has to know about:

    * `opencode run` prints no cost or token counts, so `parse` cannot fill cost_usd. The
      caller reads the finished session back from the server (`session_usage`) and hands it
      to `apply_usage`.
    * there is no `--add-dir`. `--auto` auto-approves permissions but cannot be scoped to a
      worktree, which is why a metered backend is not allowed to build until containment
      exists (see config.metered_autonomy).
    """

    name = "opencode"
    metered = True

    def argv(self, prompt, repo_root, model, work_dir=None) -> list[str]:
        return ["opencode", "run", prompt, "--format", "json", "--auto",
                "--model", self.resolve_model(model)]

    def resolve_model(self, tier: str) -> str:
        # Only the governor's tiers are aliases; anything else is already a model id.
        if tier == "opus":
            return self.cfg.opencode_model_high
        if tier == "sonnet":
            return self.cfg.opencode_model
        return tier

    def parse(self, stdout: str) -> BuildResult:
        session_id = None
        is_error = True
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict):
                continue
            if e.get("sessionID"):
                session_id = e["sessionID"]
            if e.get("type") in ("text", "step_start", "step_finish"):
                is_error = False
        if not (stdout or "").strip():
            return BuildResult(is_error=True, cost_usd=0.0, raw={})
        # Deliberately never rate_limited: a metered provider has no weekly quota, and the
        # seat's 5h/7d semantics do not transfer. Reporting one would pause the daemon for
        # hours over a signal that means something else entirely.
        return BuildResult(is_error=is_error, cost_usd=0.0, session_id=session_id,
                           raw={"output": (stdout or "")[-2000:]})

    def apply_usage(self, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            usage_out = dict(usage.get("tokens") or {})
            usage_out["cost_usd"] = float(cost)
            self.last_usage = usage_out

    def session_usage(self, session_id: str, runner=subprocess.run) -> dict | None:
        """Read cost/tokens back from the server for a finished session.

        `opencode run` does not print them, but the session record carries them, and the
        governor needs a real number to enforce a spend cap.
        """
        if not session_id:
            return None
        api_exe = shutil.which("opencode")
        if api_exe is None:
            return None
        try:
            cp = runner([api_exe, "api", "get", f"/api/session/{session_id}"],
                        capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if cp.returncode != 0:
            return None
        try:
            obj = json.loads(cp.stdout or "")
        except json.JSONDecodeError:
            return None
        data = obj.get("data") if isinstance(obj, dict) else None
        return data if isinstance(data, dict) else None


_BY_NAME = {"claude": ClaudeSeatRunner, "opencode": OpenCodeRunner}


def build_runner(provider: str, cfg: Config) -> Runner:
    """The runner for a configured provider name. Unknown names are a config error, not a
    silent fallback to the seat — quietly running the wrong backend is how a metered run
    would slip past the guard."""
    try:
        cls = _BY_NAME[provider]
    except KeyError:
        raise ValueError(
            f"unknown runner provider {provider!r}; expected one of "
            f"{', '.join(sorted(_BY_NAME))}"
        ) from None
    return cls(cfg)
