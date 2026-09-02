#!/usr/bin/env python3
"""Seed the docs lane: find repos Claude has worked in, then write one brief per target.

Discovery only ever *proposes*. `config/autodoc-targets.toml` is the authority, because
"Claude touched it" and "it is worth documenting" are not the same question — a clone of
someone else's project carries their Claude commits, not yours.

    bin/seed-autodoc-briefs.py --discover          # what is out there, and what is skipped
    bin/seed-autodoc-briefs.py --seed              # dry run: what would be written
    bin/seed-autodoc-briefs.py --seed --write      # write the briefs
    bin/seed-autodoc-briefs.py --systemd           # the ReadWritePaths drop-in for the unit
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / "config" / "autodoc-targets.toml"
BACKLOG = ROOT / "backlog"

# Commit trailers Claude Code leaves behind.
CLAUDE_MARKS = ("co-authored-by: claude", "generated with [claude code]")
# Emails that mean "this repo is mine", not "I cloned someone's repo that used Claude".
MINE = ("systemceramics.com", "10ax", "tenax@")
SKIP_PARTS = ("/node_modules/", "/.cache/", "/.nvm/", "/vendor/", "/.venv/",
              "/.claude/plugins/", "/.claude/remote/", "/.local/share/", "/aur/")

BRIEF = '''+++
spec_version = "1.0"
slug     = "{slug}"
title    = "Autodoc: {name}"
tier     = "docs"
mode     = "document"
repo     = "{path}"
model    = "auto"
priority = {priority}
created  = "{today}"
status   = "pending"
+++
## Intent
{note}

Leave this repo understandable to its owner and to the next agent that opens it: a README
that tells the truth about what runs, a CLAUDE.md an agent can work from, an operating
guide for humans and LLMs alike, and a code map that points at the lines where the business
logic actually lives. Follow {root}/AUTODOC.md exactly.

## Acceptance Criteria
A1. `README.md`, `CLAUDE.md`, `docs/WORKING-ON-THIS.md` and `docs/CODE-MAP.md` all exist,
    are non-empty, and contain no placeholder line.
A2. Nothing outside those four files is added, changed or deleted in the worktree.
A3. Every pre-existing file keeps its human text byte for byte: generated prose appears
    only between `<!-- autodoc:begin -->` and `<!-- autodoc:end -->`.
A4. `docs/CODE-MAP.md` carries 5 to 40 anchors shaped `path/file.ext:LINE` (`symbol`), every
    one resolving to a real line in this repo, and stays under 250 lines.
A5. Every command the docs publish is defined somewhere in the repo (script, Makefile, unit
    file, compose file, package manifest) — no invented commands, env vars or endpoints.
A6. The docs are written in the language of the repo's existing documentation; English when
    it has none.
'''


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return cp.stdout if cp.returncode == 0 else ""


def _counts(repo: Path) -> tuple[int, int]:
    """(commits attributed to Claude, commits authored by me)."""
    claude = sum(1 for line in _git(repo, "log", "--all", "--format=%H%x09%b").lower()
                 .splitlines() if any(m in line for m in CLAUDE_MARKS))
    mine = sum(1 for e in _git(repo, "log", "--all", "--format=%ae").lower().splitlines()
               if any(m in e for m in MINE))
    return claude, mine


def discover(home: Path) -> tuple[list[dict], list[dict]]:
    keep, skip = [], []
    for gitdir in sorted(home.glob("**/.git")):
        p = str(gitdir)
        if any(s in p for s in SKIP_PARTS) or len(gitdir.relative_to(home).parts) > 6:
            continue
        repo = gitdir.parent
        claude, mine = _counts(repo)
        built_here = ROOT / "projects" in repo.parents
        row = {"path": repo, "claude": claude, "mine": mine}
        if (claude or built_here) and (mine or built_here):
            keep.append(row)
        elif claude:
            row["why"] = "Claude commits, but none of yours — someone else's repo"
            skip.append(row)
    keep.sort(key=lambda r: -r["claude"])
    return keep, skip


def load_targets() -> list[dict]:
    if not TARGETS.exists():
        sys.exit(f"no target list at {TARGETS} — copy "
                 f"{TARGETS.with_name('autodoc-targets.example.toml').name} to "
                 f"{TARGETS.name} and edit it (it stays local: the repo ignores it).")
    data = tomllib.loads(TARGETS.read_text())
    rows = data.get("target", [])
    seen = set()
    for t in rows:
        for k in ("path", "slug", "priority"):
            if k not in t:
                sys.exit(f"target missing '{k}': {t}")
        if t["slug"] in seen:
            sys.exit(f"duplicate slug: {t['slug']}")
        seen.add(t["slug"])
        p = Path(t["path"]).expanduser()
        if not (p / ".git").exists():
            sys.exit(f"not a git repo: {p} ({t['slug']})")
    return rows


def seed(write: bool) -> int:
    today = date.today().isoformat()
    n = 0
    for t in load_targets():
        path = Path(t["path"]).expanduser()
        out = BACKLOG / f"{t['slug']}.md"
        if out.exists():
            print(f"skip   {t['slug']:32s} brief already in backlog/")
            continue
        text = BRIEF.format(slug=t["slug"], name=path.name, path=path,
                            priority=t["priority"], today=today, root=ROOT,
                            note=t.get("note", "").strip() or f"Document {path.name}.")
        if write:
            out.write_text(text)
        print(f"{'write ' if write else 'would '} {t['slug']:32s} -> {out.name}  ({path})")
        n += 1
    if not write:
        print("\n(dry run — pass --write to create the briefs)")
    return n


def systemd() -> None:
    home = Path.home()
    paths = []
    for t in load_targets():
        p = Path(t["path"]).expanduser().resolve()
        try:
            rel = p.relative_to(home)
        except ValueError:
            paths.append(f"-{p}")
            continue
        root = Path(f"%h/{rel}")
        # already covered by the unit's own ReadWritePaths
        if str(rel).split("/")[0] in ("autobuild", ".claude"):
            continue
        paths.append(f"-{root}")
    print("# ~/.config/systemd/user/autobuild.service.d/autodoc-targets.conf")
    print("# ReadWritePaths is additive across drop-ins; '-' tolerates a path that is gone.")
    print("[Service]")
    print("ReadWritePaths=" + " ".join(sorted(set(paths))))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="seed-autodoc-briefs")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--write", action="store_true", help="with --seed: actually write")
    ap.add_argument("--systemd", action="store_true")
    ap.add_argument("--home", default=str(Path.home()))
    args = ap.parse_args(argv)
    if args.discover:
        keep, skip = discover(Path(args.home))
        print(f"{'repo':58s} claude  mine")
        for r in keep:
            print(f"{str(r['path']):58s} {r['claude']:6d} {r['mine']:5d}")
        if skip:
            print("\nnot proposed:")
            for r in skip:
                print(f"  {str(r['path']):56s} {r['why']}")
    if args.systemd:
        systemd()
    if args.seed:
        seed(args.write)
    if not (args.discover or args.seed or args.systemd):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
