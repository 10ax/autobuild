"""The docs lane: write a doc set into a repo that already exists, provably.

The build lane creates a project from nothing, so "did it work?" is `npm test`. This lane
edits repos the user works in every day, so the question is different: *did it stay inside
the lines?* Three properties are what make an unattended run safe here, and each one is
checked by git rather than trusted:

1. **The user's checkout is never touched.** The agent writes in a throwaway `git worktree`
   under `state/worktrees/<slug>`; only objects and refs land in the real repo.
2. **Nothing but documentation changes.** `verify_docs` fails on any tracked source file
   that moved, and on any new file outside the doc set.
3. **Hand-written prose survives.** Generated text lives between `autodoc:begin/end`
   markers; everything outside them must match the base commit.

All git writing happens here, in Python, and never in the agent's hands.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from autobuild.spec import _PLACEHOLDER

DOC_SET = ("README.md", "CLAUDE.md", "docs/WORKING-ON-THIS.md", "docs/CODE-MAP.md")
# AGENTS.md is hand-curated in several repos: the playbook may add a cross-reference line
# inside markers, never rewrite it — so it is commit-allowed but not part of the doc set.
ALLOWED = DOC_SET + ("AGENTS.md",)
CODE_MAP = "docs/CODE-MAP.md"

BEGIN = "<!-- autodoc:begin -->"
END = "<!-- autodoc:end -->"

MIN_ANCHORS, MAX_ANCHORS = 5, 40
MAX_CODE_MAP_LINES = 250

# Untracked leftovers from exploration tooling. They are thrown away with the worktree, so
# they must not read as "the agent invented files", but they are never committed either.
IGNORABLE = (".tokensave/", "node_modules/", "__pycache__/", ".venv/", ".pytest_cache/",
             ".mypy_cache/", ".ruff_cache/", "dist/", "build/", ".next/", "target/",
             # A plugin hook in the operator's Claude Code writes this into the session's
             # cwd — which is the worktree. Never the agent's doing, never committed.
             "observability/affordance-invocations.json")

# A code-map anchor: `path/to/file.ext:123`, or one of the conventional extensionless
# filenames. The leading lookbehind keeps URLs (`https://host/x:80`) out.
_ANCHOR = re.compile(
    r"(?<![\w/.:-])((?:[A-Za-z0-9_.\-]+/)*"
    r"(?:[A-Za-z0-9_.\-]+\.[A-Za-z][A-Za-z0-9]{0,4}"
    r"|Makefile|Dockerfile|Containerfile|PKGBUILD|Justfile|Rakefile|Gemfile|CMakeLists\.txt))"
    r":(\d+)")


class AutodocError(RuntimeError):
    """A git operation the daemon owns failed — the item cannot proceed safely."""


@dataclass
class AutodocPlan:
    repo: Path
    worktree: Path
    branch: str
    base_sha: str
    slug: str


def _git(cwd: Path, *args: str, runner=subprocess.run, check: bool = True, env=None):
    cp = runner(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if check and cp.returncode != 0:
        raise AutodocError(f"git {' '.join(args)} failed in {cwd}: "
                           f"{(cp.stderr or cp.stdout or '').strip()[:300]}")
    return cp


def _commit_env() -> dict:
    """The daemon runs with 10ax's git identity for its own public repo; a doc commit in
    someone else's repo must be authored by that repo's own configured identity."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}


def prepare_worktree(repo: Path, slug: str, worktrees_dir: Path, date: str,
                     runner=subprocess.run) -> AutodocPlan:
    """Put a worktree for `repo` on branch `autodoc/<date>`, creating or reusing both."""
    repo, worktrees_dir = Path(repo).expanduser().resolve(), Path(worktrees_dir)
    if not (repo / ".git").exists():
        raise AutodocError(f"not a git repo: {repo}")
    branch = f"autodoc/{date}"
    worktree = worktrees_dir / slug
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    _git(repo, "worktree", "prune", runner=runner, check=False)
    if worktree.exists():
        if not (worktree / ".git").is_file():
            raise AutodocError(f"{worktree} exists but is not a linked worktree — "
                               "move it aside by hand")
    else:
        has_branch = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                          runner=runner, check=False).returncode == 0
        args = (["worktree", "add", str(worktree), branch] if has_branch
                else ["worktree", "add", "-b", branch, str(worktree), "HEAD"])
        _git(repo, *args, runner=runner)

    base_sha = _git(worktree, "rev-parse", "HEAD", runner=runner).stdout.strip()
    return AutodocPlan(repo=repo, worktree=worktree, branch=branch, base_sha=base_sha,
                       slug=slug)


def _outside_markers(text: str) -> str:
    """The text a human owns: everything outside every autodoc block, whitespace-normalised
    (so an added blank line around a block is not read as an edit to the prose)."""
    out, depth = [], 0
    for line in text.splitlines():
        s = line.strip()
        if s == BEGIN:
            depth += 1
            continue
        if s == END:
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(line.rstrip())
    return "\n".join(out).strip()


def _anchors(text: str) -> list[tuple[str, int]]:
    seen, rows = set(), []
    for path, line in _ANCHOR.findall(text):
        key = (path, int(line))
        if key not in seen:
            seen.add(key)
            rows.append(key)
    return rows


def _changed(plan: AutodocPlan, runner) -> tuple[list[str], list[str], list[str]]:
    """(tracked changes, tracked deletions, untracked files) in the worktree vs its HEAD."""
    tracked = _git(plan.worktree, "diff", "--name-only", "HEAD", runner=runner).stdout.split("\n")
    deleted = _git(plan.worktree, "diff", "--name-only", "--diff-filter=D", "HEAD",
                   runner=runner).stdout.split("\n")
    others = _git(plan.worktree, "ls-files", "--others", "--exclude-standard",
                  runner=runner).stdout.split("\n")
    clean = lambda rows: [r for r in (x.strip() for x in rows) if r]
    return clean(tracked), clean(deleted), clean(others)


def verify_docs(plan: AutodocPlan, runner=subprocess.run) -> list[str]:
    """[] when the doc set is complete, contained, anchored to real code, and additive."""
    errors: list[str] = []
    wt = plan.worktree

    for rel in DOC_SET:
        f = wt / rel
        if not f.is_file():
            errors.append(f"missing: {rel}")
            continue
        text = f.read_text(errors="replace")
        if not text.strip():
            errors.append(f"empty: {rel}")
            continue
        for line in text.splitlines():
            bare = line.lstrip("#*->| \t")
            if bare and _PLACEHOLDER.match(bare):
                errors.append(f"placeholder line in {rel}: {line.strip()[:60]}")
                break

    tracked, deleted, others = _changed(plan, runner)
    for rel in tracked:
        if rel not in ALLOWED:
            errors.append(f"changed a file outside the doc set: {rel}")
    for rel in deleted:
        errors.append(f"deleted a tracked file: {rel}")
    for rel in others:
        if rel not in ALLOWED and not any(rel.startswith(p) for p in IGNORABLE):
            errors.append(f"created a file outside the doc set: {rel}")

    cm = wt / CODE_MAP
    if cm.is_file():
        text = cm.read_text(errors="replace")
        n_lines = len(text.splitlines())
        if n_lines > MAX_CODE_MAP_LINES:
            errors.append(f"{CODE_MAP} is too long: {n_lines} lines (max "
                          f"{MAX_CODE_MAP_LINES}) — it must stay a map, not a manual")
        anchors = _anchors(text)
        if len(anchors) < MIN_ANCHORS:
            errors.append(f"{CODE_MAP} has {len(anchors)} code anchors, needs at least "
                          f"{MIN_ANCHORS}")
        if len(anchors) > MAX_ANCHORS:
            errors.append(f"{CODE_MAP} has {len(anchors)} code anchors, at most "
                          f"{MAX_ANCHORS} keep it synthetic")
        for rel, line in anchors:
            target = wt / rel
            if not target.is_file():
                errors.append(f"dangling anchor in {CODE_MAP}: {rel}:{line} — no such file")
            else:
                have = len(target.read_bytes().splitlines())
                if line < 1 or line > have:
                    errors.append(f"dangling anchor in {CODE_MAP}: {rel}:{line} — file has "
                                  f"{have} lines")

    for rel in ALLOWED:
        base = _git(wt, "show", f"{plan.base_sha}:{rel}", runner=runner, check=False)
        if base.returncode != 0:
            continue                                   # the file is new — nothing to preserve
        f = wt / rel
        now = f.read_text(errors="replace") if f.is_file() else ""
        if _outside_markers(now) != _outside_markers(base.stdout):
            errors.append(f"human text outside the autodoc markers changed in {rel}")
    return errors


def commit_docs(plan: AutodocPlan, message: str, runner=subprocess.run,
                wip: bool = False) -> bool:
    """Commit the doc set on the autodoc branch. True when a commit was made.

    A non-WIP commit carries the whole doc set or nothing: a half-written set is only ever
    committed deliberately, as WIP, for a human to look at.
    """
    present = [rel for rel in ALLOWED if (plan.worktree / rel).is_file()]
    if not wip and any(not (plan.worktree / rel).is_file() for rel in DOC_SET):
        return False
    if not present:
        return False
    _git(plan.worktree, "add", "--", *present, runner=runner)
    staged = _git(plan.worktree, "diff", "--cached", "--quiet", runner=runner, check=False)
    if staged.returncode == 0:
        return False                                   # nothing actually differs from HEAD
    _git(plan.worktree, "commit", "--no-verify", "-m", message,
         runner=runner, env=_commit_env())
    return True


def remove_worktree(plan: AutodocPlan, runner=subprocess.run) -> None:
    """Drop the scratch worktree; the branch and its commits stay in the repo."""
    _git(plan.repo, "worktree", "remove", "--force", str(plan.worktree),
         runner=runner, check=False)
    _git(plan.repo, "worktree", "prune", runner=runner, check=False)
