#!/usr/bin/env python3
"""Seed the quality lane: one brief per repo listed in config/improve-targets.toml.

The docs lane could discover its own targets, because documenting a repo is harmless. This
lane writes tests, CI and config into repos the user works in every day, so there is no
discovery: the target list is written by hand, and each entry states the contract the daemon
will enforce (`allow`, `require`, `verify`, `rewrite`). If you cannot say which paths a repo's
quality run may touch, it is not ready to be in the list.

    bin/seed-improve-briefs.py --seed              # dry run: what would be written
    bin/seed-improve-briefs.py --seed --write      # write the briefs
    bin/seed-improve-briefs.py --systemd           # the ReadWritePaths drop-in for the unit
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / "config" / "improve-targets.toml"
BACKLOG = ROOT / "backlog"

REQUIRED = ("path", "slug", "priority", "allow", "require", "verify", "note")
LIST_KEYS = ("allow", "require", "verify", "rewrite")

BRIEF = '''+++
spec_version = "1.0"
slug     = "{slug}"
title    = "Quality: {name}"
tier     = "quality"
mode     = "improve"
repo     = "{path}"
model    = "opus"
priority = {priority}
created  = "{today}"
status   = "needs-review"
allow    = {allow}
require  = {require}
rewrite  = {rewrite}
verify   = {verify}
+++
## Intent
{note}

Bring this repo up to the standard: a test suite that pins down what the code does today, a
GitHub Actions workflow that runs it, a README and a CLAUDE.md that tell the truth, an
operating guide for the day it breaks, and two or three skills for the workflows this repo
actually repeats. Behaviour must not change — refactor only where it is what makes a test
possible, and write down anything broken instead of fixing it.

{traps}

Follow {root}/IMPROVE.md exactly. Write only inside the `allow` globs above; the daemon
rejects the run otherwise, and it runs the `verify` commands itself before committing.

## Acceptance Criteria
A1. Every path in `require` exists, is non-empty, is not gitignored, and carries no
    placeholder line: {require_list}.
A2. Nothing outside the `allow` globs is added, changed or deleted in the worktree.
A3. Every pre-existing Markdown file{rewrite_clause} keeps its human text byte for byte:
    generated prose appears only between `<!-- autodoc:begin -->` and `<!-- autodoc:end -->`.
A4. These commands all exit 0 when run from the worktree root: {verify_list}.
A5. Every command, env var and endpoint the docs publish is defined somewhere in this repo —
    nothing invented — and no secret, token, channel id, private host or personal datum is
    copied into them.
A6. `.github/workflows/*.yml` pass `actionlint`, run only deterministic steps (no API keys,
    no model calls), trigger on push and pull_request, and use the versions this repo pins.
    An existing workflow is extended or joined by a sibling, never deleted or rewritten.
A7. `docs/TROUBLESHOOTING.md` covers every trap named above in symptom → check → cause → fix
    form, and ends with a Known issues section naming what is broken and left unfixed.
'''


def _fmt_list(v) -> str:
    return "[" + ", ".join(f'"{x}"' for x in v) + "]"


def check_targets(rows: list[dict]) -> list[dict]:
    seen = set()
    for t in rows:
        for k in REQUIRED:
            if not t.get(k):
                sys.exit(f"target missing '{k}': {t.get('slug', t.get('path', '?'))}")
        for k in LIST_KEYS:
            v = t.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
                sys.exit(f"{t['slug']}: '{k}' must be a list of non-empty strings")
        if t["slug"] in seen:
            sys.exit(f"duplicate slug: {t['slug']}")
        seen.add(t["slug"])
        p = Path(t["path"]).expanduser()
        if not (p / ".git").exists():
            sys.exit(f"not a git repo: {p} ({t['slug']})")
    return rows


def load_targets() -> list[dict]:
    if not TARGETS.exists():
        sys.exit(f"no target list at {TARGETS} — copy "
                 f"{TARGETS.with_name('improve-targets.example.toml').name} to "
                 f"{TARGETS.name} and edit it (it stays local: the repo ignores it).")
    return check_targets(tomllib.loads(TARGETS.read_text()).get("target", []))


def render(t: dict, today: str, root: Path) -> str:
    path = Path(t["path"]).expanduser()
    traps = t.get("traps") or []
    trap_text = ("Traps this repo already taught its owner — each one belongs in\n"
                 "`docs/TROUBLESHOOTING.md`:\n\n"
                 + "\n".join(f"- {x}" for x in traps)) if traps else \
                ("This repo has no recorded traps yet; mine `git log` for `fix:` commits and\n"
                 "reverts, and write down what they were about.")
    rewrite = t.get("rewrite") or []
    rewrite_clause = (" except " + ", ".join(f"`{x}`" for x in rewrite)
                      + " (boilerplate: replace it wholesale, inside one block)") if rewrite else ""
    return BRIEF.format(
        slug=t["slug"], name=path.name, path=path, priority=t["priority"], today=today,
        allow=_fmt_list(t["allow"]), require=_fmt_list(t["require"]),
        rewrite=_fmt_list(rewrite), verify=_fmt_list(t["verify"]),
        note=t["note"].strip(), traps=trap_text, root=root,
        require_list=", ".join(f"`{x}`" for x in t["require"]),
        verify_list=", ".join(f"`{x}`" for x in t["verify"]),
        rewrite_clause=rewrite_clause)


def seed(write: bool) -> int:
    today = date.today().isoformat()
    n = 0
    for t in load_targets():
        out = BACKLOG / f"{t['slug']}.md"
        if out.exists():
            print(f"skip   {t['slug']:34s} brief already in backlog/")
            continue
        if write:
            out.write_text(render(t, today, ROOT))
        print(f"{'write ' if write else 'would '} {t['slug']:34s} -> {out.name}")
        n += 1
    if not write:
        print("\n(dry run — pass --write to create the briefs)")
    return n


def systemd_dropin(rows: list[dict], home: Path | None = None) -> str:
    home = Path(home or Path.home())
    paths = []
    for t in rows:
        p = Path(t["path"]).expanduser().resolve()
        try:
            rel = p.relative_to(home)
        except ValueError:
            paths.append(f"-{p}")
            continue
        if str(rel).split("/")[0] in ("autobuild", ".claude"):
            continue                        # already in the unit's own ReadWritePaths
        paths.append(f"-%h/{rel}")
    # The lane builds a repo's toolchain inside the worktree: uv downloads interpreters into
    # its share dir and caches wheels, pnpm/npm into their stores. Without these, a verify
    # command dies against ProtectSystem=strict instead of failing honestly.
    paths += ["-%h/.local/share/uv", "-%h/.local/share/pnpm", "-%h/.cache/uv"]
    return ("# ~/.config/systemd/user/autobuild.service.d/improve-targets.conf\n"
            "# ReadWritePaths is additive across drop-ins; '-' tolerates a path that is gone.\n"
            "[Service]\n"
            "ReadWritePaths=" + " ".join(sorted(set(paths))) + "\n")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="seed-improve-briefs")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--write", action="store_true", help="with --seed: actually write")
    ap.add_argument("--systemd", action="store_true")
    args = ap.parse_args(argv)
    if args.systemd:
        print(systemd_dropin(load_targets()), end="")
    if args.seed:
        seed(args.write)
    if not (args.seed or args.systemd):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
