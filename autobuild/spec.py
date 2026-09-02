from __future__ import annotations
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TIERS = {"script", "library", "service", "docs"}
STATUSES = {"pending", "building", "done", "needs-review"}
MODELS = {"auto", "sonnet", "opus"}
# Lanes: "build" creates a new project under projects/<slug>; "document" writes a doc set
# into an existing repo named by the brief's `repo` key (see AUTODOC.md).
MODES = {"build", "document"}
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
    if mode == "document":
        if not repo:
            errors.append("mode=document requires a repo path in the front-matter")
        else:
            target = Path(str(repo)).expanduser()
            if not target.is_dir():
                errors.append(f"repo does not exist: {target}")
            elif not (target / ".git").exists():
                errors.append(f"repo is not a git repo: {target}")
    elif repo is not None:
        errors.append("repo is only allowed with mode=document")
    if meta.get("tier") == "docs" and mode != "document":
        errors.append('tier "docs" requires mode = "document"')
    required = BRIEF_SECTIONS if level == "brief" else SPEC_SECTIONS
    for s in required:
        v = doc.sections.get(s, "").strip()
        if not v:
            errors.append(f"section empty/missing: {s}")
        elif _PLACEHOLDER.match(v):
            errors.append(f"section is placeholder: {s}")
    return errors
