"""The checker pass: one more voice on `done`, from a run that wrote nothing.

`verify_repo` proves the tests exit 0. It cannot prove the tests are the right ones, and
the session that wrote both the code and its tests is the worst available judge of its own
work: it convinced itself this approach was correct while generating it, and when it looks
back it sees its own reasoning, not its mistakes.

So the build lane does not get to mark an item done on its own tests alone. After verify
goes green, a fresh run on the opposite model tier reads the brief and the repo and either
accepts or refuses. Missing, unparseable or errored verdicts fail closed: the item goes to
needs-review, because the alternative is promoting work nobody checked.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from autobuild.result import BuildResult

_ACCEPT = {"accept", "accepted", "approve", "approved", "pass", "passed"}
_REVISE = {"revise", "revised", "reject", "rejected", "fail", "failed"}
_OBJECT = re.compile(r"\{[^{}]*\"verdict\"[^{}]*\}", re.S)


@dataclass
class Review:
    """What the checker said. `verdict` is None when it did not say anything usable."""

    verdict: str | None
    reasons: list[str] = field(default_factory=list)
    tier: str = ""
    result: BuildResult | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict == "accept"

    @property
    def reason(self) -> str:
        """One notification-sized line: the checker's reasons, or why there are none."""
        if self.reasons:
            return "; ".join(self.reasons)[:400]
        if self.verdict == "accept":
            return ""
        return "checker returned no usable verdict"


def opposite_tier(build_model: str) -> str:
    """A different model does the checking, so the two do not share their blind spots."""
    return "sonnet" if build_model == "opus" else "opus"


def review_tier(cfg, build_model: str) -> str:
    """The tier that checks. Cheap by default: the point is a reader with no authorship
    stake, and this runs once per build for a whole night. `auto` is the escape hatch for
    a genuinely different model."""
    choice = str(getattr(cfg, "reviewer_tier", "sonnet") or "sonnet").strip().lower()
    return opposite_tier(build_model) if choice == "auto" else choice


def parse_verdict(text: str | None) -> tuple[str | None, list[str]]:
    """The last JSON object carrying a verdict wins; anything unknown counts as no verdict."""
    verdict: str | None = None
    reasons: list[str] = []
    for match in _OBJECT.finditer(text or ""):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        raw = str(obj.get("verdict", "")).strip().lower()
        if raw in _ACCEPT:
            verdict = "accept"
        elif raw in _REVISE:
            verdict = "revise"
        else:
            continue
        found = obj.get("reasons")
        if isinstance(found, list):
            reasons = [str(r) for r in found if str(r).strip()]
        elif isinstance(found, str) and found.strip():
            reasons = [found.strip()]
    return verdict, reasons


def _text(result: BuildResult) -> str:
    """The agent's own words, however this backend happened to store them."""
    raw = result.raw or {}
    for key in ("output", "result", "text", "message", "response"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    try:
        return json.dumps(raw)[-6000:]
    except (TypeError, ValueError):
        return ""


def review_build(brief_path: Path, repo_root: Path, build_model: str, pace, cfg,
                 runner=subprocess.run, builder=None) -> Review:
    """Run the checker and return its verdict. Never raises on a bad verdict: an unattended
    loop must survive a confusing answer by refusing to promote, not by crashing."""
    if builder is None:
        from autobuild.build import run_build
        builder = run_build
    tier = review_tier(cfg, build_model)
    result = builder(Path(brief_path), Path(repo_root), tier, pace, cfg, runner=runner,
                     mode="review", timeout_min=getattr(cfg, "reviewer_timeout_min", 20))
    if result.is_error:
        return Review(verdict=None, reasons=[], tier=tier, result=result)
    verdict, reasons = parse_verdict(_text(result))
    return Review(verdict=verdict, reasons=reasons, tier=tier, result=result)
