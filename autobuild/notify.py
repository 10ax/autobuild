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
    if event == "done":
        return (f"✅ autobuild: {kw.get('slug')} done — tests {kw.get('tests')}, "
                f"${kw.get('cost', 0):.2f}, {_fmt_dur(kw.get('secs'))}\n{kw.get('repo', '')}")
    if event == "needs-review":
        dur = f" ({_fmt_dur(kw['secs'])})" if kw.get("secs") else ""
        return (f"⚠️ autobuild: {kw.get('slug')} needs review — "
                f"{kw.get('reason', 'verification failed')}{dur}\n{kw.get('repo', '')}")
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
