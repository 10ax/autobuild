from __future__ import annotations
import subprocess
from pathlib import Path
from autobuild.config import Config


def format_message(event: str, **kw) -> str:
    if event == "done":
        return (f"✅ autobuild: {kw.get('slug')} done — tests {kw.get('tests')}, "
                f"${kw.get('cost', 0):.2f}, {kw.get('mins', 0)}m\n{kw.get('repo', '')}")
    if event == "needs-review":
        return (f"⚠️ autobuild: {kw.get('slug')} needs review — "
                f"{kw.get('reason', 'verification failed')}\n{kw.get('repo', '')}")
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
