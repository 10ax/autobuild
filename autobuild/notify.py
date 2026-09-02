from __future__ import annotations
import subprocess
from pathlib import Path
from autobuild.config import Config


def _fmt_dur(seconds) -> str:
    """Human build duration: '45s' under a minute, else '7m03s'."""
    s = int(round(seconds or 0))
    if s < 60:
        return f"{s}s"
    m, r = divmod(s, 60)
    return f"{m}m{r:02d}s"


def format_message(event: str, **kw) -> str:
    # A `branch` means the docs lane: there are no tests to report, but there is a branch to
    # review and — when it went red — a worktree left standing to look inside.
    branch = kw.get("branch")
    if event == "done":
        what = f"branch {branch}" if branch else f"tests {kw.get('tests')}"
        return (f"✅ autobuild: {kw.get('slug')} done — {what}, "
                f"${kw.get('cost', 0):.2f}, {_fmt_dur(kw.get('secs'))}\n{kw.get('repo', '')}")
    if event == "needs-review":
        dur = f" ({_fmt_dur(kw['secs'])})" if kw.get("secs") else ""
        tail = f"\n{kw.get('repo', '')}"
        if branch:
            tail += f" @ {branch}"
        if kw.get("worktree"):
            tail += f"\nworktree: {kw['worktree']}"
        return (f"⚠️ autobuild: {kw.get('slug')} needs review — "
                f"{kw.get('reason', 'verification failed')}{dur}{tail}")
    if event == "paused":
        return f"⏸ autobuild paused — {kw.get('reason', '')}"
    if event == "crash":
        return f"💥 autobuild crashed — {kw.get('error', '')}"
    return f"autobuild: {event}"


def notify(cfg: Config, event: str, runner=subprocess.run, **kw) -> None:
    if event not in cfg.notify_on:
        return
    script = Path(cfg.telegram_script).expanduser()
    msg = format_message(event, **kw)
    try:
        runner([str(script), msg], capture_output=True, text=True, timeout=30)
    except Exception:
        pass  # notification must never crash the daemon
