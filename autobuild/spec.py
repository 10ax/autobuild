from __future__ import annotations
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TIERS = {"script", "library", "service", "docs", "quality"}
STATUSES = {"pending", "building", "done", "needs-review"}
MODELS = {"auto", "sonnet", "opus"}
# Lanes: "build" creates a new project under projects/<slug>; "document" writes a doc set
# into an existing repo named by the brief's `repo` key (see AUTODOC.md); "improve" brings an
# existing repo up to a quality standard — tests, CI, docs, skills — inside the paths the
# brief allows, gated by the verify commands the brief names (see IMPROVE.md); "implement"
# carries out an implementation plan that already exists in the repo, and is the only lane
# allowed to change what the code does (see IMPLEMENT.md).
MODES = {"build", "document", "improve", "implement"}
REPO_MODES = {"document", "improve", "implement"}
# The contract lanes. Both state their own guarantee in the brief's front-matter rather than
# in a fixed file list, and both are gated by it; what differs is what they may do inside
# those lines, not how the lines are drawn.
CONTRACT_MODES = {"improve", "implement"}
# Contract front-matter lists: writable path globs, globs that must match ≥1 file,
# pre-existing files whose human text may be replaced, and the shell commands that must pass.
IMPROVE_LISTS = ("allow", "require", "rewrite", "verify")
SPEC_SECTIONS = ["Intent", "Ubiquitous Language", "Domain Model", "Requirements",
                 "Interfaces", "Acceptance Criteria", "Non-Goals", "Constraints"]
BRIEF_SECTIONS = ["Intent", "Acceptance Criteria"]

_FRONT = re.compile(r"^\+\+\+\s*\n(.*?)\n\+\+\+\s*\n(.*)$", re.DOTALL)
_PLACEHOLDER = re.compile(r"^(tbd|todo|\.\.\.|xxx|n/a)\b", re.IGNORECASE)
_H2 = re.compile(r"^##\s+(.*\S)\s*$")
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass
class SpecDoc:
    meta: dict
    sections: dict[str, str]
    raw: str
    path: Path | None = None


def parse_spec(text: str) -> SpecDoc:
    m = _FRONT.match(text.lstrip("﻿").lstrip())
    if not m:
        return SpecDoc(meta={}, sections={}, raw=text)
    try:
        meta = tomllib.loads(m.group(1))
    except tomllib.TOMLDecodeError:
        return SpecDoc(meta={}, sections={}, raw=text)  # unparseable → empty meta (won't be selected)
    sections: dict[str, str] = {}
    cur, buf = None, []
    for line in m.group(2).splitlines():
        h = _H2.match(line)
        if h:
            if cur is not None:
                sections[cur] = "\n".join(buf).strip()
            cur, buf = h.group(1).strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        sections[cur] = "\n".join(buf).strip()
    return SpecDoc(meta=meta, sections=sections, raw=text)


def validate_spec(doc: SpecDoc, level: str = "spec") -> list[str]:
    errors: list[str] = []
    meta = doc.meta
    for k in ["spec_version", "slug", "title", "tier", "priority", "status"]:
        if k not in meta:
            errors.append(f"missing front-matter key: {k}")
    if meta.get("tier") not in TIERS:
        errors.append(f"tier must be one of {sorted(TIERS)}")
    if meta.get("status") not in STATUSES:
        errors.append(f"status must be one of {sorted(STATUSES)}")
    if meta.get("model", "auto") not in MODELS:
        errors.append(f"model must be one of {sorted(MODELS)}")
    if not _SLUG.match(str(meta.get("slug", ""))):
        errors.append("slug must be kebab-case")
    mode = meta.get("mode", "build")
    if mode not in MODES:
        errors.append(f"mode must be one of {sorted(MODES)}")
    repo = meta.get("repo")
    if mode in REPO_MODES:
        if not repo:
            errors.append(f"mode={mode} requires a repo path in the front-matter")
        else:
            target = Path(str(repo)).expanduser()
            if not target.is_dir():
                errors.append(f"repo does not exist: {target}")
            elif not (target / ".git").exists():
                errors.append(f"repo is not a git repo: {target}")
    elif repo is not None:
        errors.append(f"repo is only used with mode in {sorted(REPO_MODES)}")
    # The implement lane carries out a plan that already exists in the repo, so the plan is
    # the one thing worth resolving here: a mistyped path costs a whole overnight window,
    # and the run would otherwise discover it with no human awake to fix it.
    plan_rel = meta.get("plan")
    if mode == "implement":
        if not plan_rel:
            errors.append("mode=implement requires a plan path in the front-matter")
        elif repo:
            plan_path = Path(str(repo)).expanduser() / str(plan_rel)
            if not plan_path.is_file():
                errors.append(f"plan does not exist in the repo: {plan_rel}")
            elif not plan_path.read_text(errors="replace").strip():
                errors.append(f"plan is empty: {plan_rel}")
    elif plan_rel is not None:
        errors.append("plan is only used with mode=implement")
    if meta.get("tier") == "docs" and mode != "document":
        errors.append('tier "docs" requires mode = "document"')
    if meta.get("tier") == "quality" and mode != "improve":
        errors.append('tier "quality" requires mode = "improve"')
    for k in IMPROVE_LISTS:
        v = meta.get(k)
        if v is None:
            continue
        if mode not in CONTRACT_MODES:
            errors.append(f"{k} is only used with mode in {sorted(CONTRACT_MODES)}")
        elif not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            errors.append(f"{k} must be a list of non-empty strings")
    if mode in CONTRACT_MODES:
        for k in ("allow", "verify"):
            if not meta.get(k):
                errors.append(f"mode={mode} requires a non-empty {k} list")
    required = BRIEF_SECTIONS if level == "brief" else SPEC_SECTIONS
    for s in required:
        v = doc.sections.get(s, "").strip()
        if not v:
            errors.append(f"section empty/missing: {s}")
        elif _PLACEHOLDER.match(v):
            errors.append(f"section is placeholder: {s}")
    return errors
