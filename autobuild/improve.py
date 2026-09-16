"""The quality lane: bring a repo that already exists up to a standard — tests, CI, docs,
skills — provably inside the lines the brief drew.

The docs lane sells one guarantee: nothing but four doc files changes. This lane cannot,
because its whole point is to write test files, workflows and a little test-enabling
refactoring into a repo the user works in every day. So the guarantee moves from a fixed
file list to a contract the brief states and git checks:

1. **Containment.** Every path the agent modified, created or deleted matches one of the
   brief's `allow` globs. Anything else is a red run, however good the change.
2. **Presence.** Every `require` glob matches at least one non-empty, non-ignored file — the
   deliverables the brief demanded are actually there.
3. **Human prose survives.** Any pre-existing Markdown file not listed in `rewrite` keeps its
   text outside the `autodoc:begin/end` markers byte for byte (same rule as the docs lane).
4. **The repo's own truth.** The brief's `verify` commands — the repo's test suite, type
   check, lint — run in the worktree and must all exit 0. The daemon runs them, not the agent.
5. **Hygiene.** Workflows pass `actionlint`, the diff passes `gitleaks`, no placeholder lines.

As in the docs lane, the agent writes in a throwaway worktree and the daemon does every git
write, so the user's checkout is never touched and a red run leaves evidence, not damage.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath

from autobuild import autodoc
from autobuild.autodoc import (AutodocError, AutodocPlan, _changed, _commit_env, _git,
                               _outside_markers)
from autobuild.spec import _PLACEHOLDER

__all__ = ["AutodocError", "AutodocPlan", "prepare_worktree", "verify_improve",
           "commit_improve", "remove_worktree", "changed_paths"]

BRANCH_PREFIX = "quality"
# Untracked leftovers of installing and running a repo's own toolchain inside the worktree.
# They die with the worktree; they are neither "invented files" nor ever committed.
IGNORABLE = autodoc.IGNORABLE + (".venv/", "venv/", "coverage/", ".coverage", "htmlcov/",
                                 ".husky/_/", ".claude/settings.local.json", "out/",
                                 ".ruff_cache/", ".eslintcache", ".turbo/", ".vite/")
PROSE_SUFFIXES = (".md",)
VERIFY_TIMEOUT_S = 1200
LINT_TIMEOUT_S = 180
TAIL = 400


def prepare_worktree(repo: Path, slug: str, worktrees_dir: Path, date: str,
                     runner=subprocess.run) -> AutodocPlan:
    """A worktree on `quality/<date>` — the docs lane's choreography under this lane's name."""
    return autodoc.prepare_worktree(repo, slug, worktrees_dir, date, runner=runner,
                                    prefix=BRANCH_PREFIX)


remove_worktree = autodoc.remove_worktree


def _ignorable(rel: str) -> bool:
    return any(rel.startswith(p) for p in IGNORABLE)


def _allowed(rel: str, allow) -> bool:
    return any(PurePosixPath(rel).full_match(g) for g in allow)


def changed_paths(plan: AutodocPlan, runner=subprocess.run) -> tuple[list[str], list[str], list[str]]:
    """(modified tracked, deleted tracked, new untracked) relative paths in the worktree,
    with toolchain leftovers already filtered out of the untracked set."""
    tracked, deleted, others = _changed(plan, runner)
    modified = [t for t in tracked if t not in set(deleted)]
    new = [o for o in others if not _ignorable(o)]
    return modified, deleted, new


def _tail(cp) -> str:
    out = ((getattr(cp, "stdout", "") or "") + "\n" + (getattr(cp, "stderr", "") or "")).strip()
    return out[-TAIL:].replace("\n", " ⏎ ")


def _placeholder_line(text: str) -> str | None:
    for line in text.splitlines():
        bare = line.lstrip("#*->| \t")
        if bare and _PLACEHOLDER.match(bare):
            return line.strip()[:60]
    return None


def _matches(wt: Path, pattern: str) -> list[str]:
    hits = []
    for p in wt.glob(pattern):
        if not p.is_file():
            continue
        rel = p.relative_to(wt).as_posix()
        if rel.startswith(".git/") or _ignorable(rel):
            continue
        hits.append(rel)
    return sorted(hits)


def verify_improve(plan: AutodocPlan, allow, verify, require=(), rewrite=(),
                   runner=subprocess.run, which=shutil.which,
                   timeout: int = VERIFY_TIMEOUT_S) -> list[str]:
    """[] when the run stayed inside `allow`, delivered every `require`, kept human prose,
    passes the brief's `verify` commands and the linters. Otherwise, why not — in order."""
    errors: list[str] = []
    wt = plan.worktree
    rewrite = set(rewrite or ())

    modified, deleted, new = changed_paths(plan, runner)
    for rel in modified + new:
        if not _allowed(rel, allow):
            errors.append(f"changed a path outside allow: {rel}")
    for rel in deleted:
        if not _allowed(rel, allow):
            errors.append(f"deleted a tracked file outside allow: {rel}")

    for pattern in require or ():
        hits = _matches(wt, pattern)
        if not hits:
            errors.append(f"require unmet: no file matches {pattern}")
            continue
        if all((wt / h).stat().st_size == 0 or not (wt / h).read_text(errors="replace").strip()
               for h in hits):
            errors.append(f"require unmet: {pattern} matches only empty files")
            continue
        ignored = _git(wt, "check-ignore", "--", *hits, runner=runner, check=False).stdout.split()
        if len(ignored) == len(hits):
            errors.append(f"require unmet: every file matching {pattern} is gitignored in this "
                          f"repo — the lane cannot commit it (fix .gitignore, which is in allow)")

    for rel in modified + new:
        if rel.endswith(PROSE_SUFFIXES) and (wt / rel).is_file():
            hit = _placeholder_line((wt / rel).read_text(errors="replace"))
            if hit:
                errors.append(f"placeholder line in {rel}: {hit}")

    for rel in modified:
        if not rel.endswith(PROSE_SUFFIXES) or rel in rewrite:
            continue
        base = _git(wt, "show", f"{plan.base_sha}:{rel}", runner=runner, check=False)
        if base.returncode != 0:
            continue
        now = (wt / rel).read_text(errors="replace") if (wt / rel).is_file() else ""
        if _outside_markers(now) != _outside_markers(base.stdout):
            errors.append(f"human text outside the autodoc markers changed in {rel}")

    for cmd in verify or ():
        try:
            cp = runner(cmd, shell=True, cwd=str(wt), capture_output=True, text=True,
                        timeout=timeout)
        except subprocess.TimeoutExpired:
            errors.append(f"verify timed out after {timeout}s: `{cmd}`")
            break
        except OSError as e:
            errors.append(f"verify could not run `{cmd}`: {e}")
            break
        if cp.returncode != 0:
            errors.append(f"verify failed: `{cmd}` (exit {cp.returncode}) — {_tail(cp)}")
            break

    workflows = [rel for rel in modified + new
                 if rel.startswith(".github/workflows/") and rel.endswith((".yml", ".yaml"))
                 and (wt / rel).is_file()]
    lint = which("actionlint") if workflows else None
    if lint:
        cp = runner([lint, *workflows], cwd=str(wt), capture_output=True, text=True,
                    timeout=LINT_TIMEOUT_S)
        if cp.returncode != 0:
            errors.append(f"actionlint: {_tail(cp)}")

    leaks = which("gitleaks") if (modified or new) else None
    if leaks:
        diff = _git(wt, "diff", "HEAD", "--", *modified, runner=runner, check=False).stdout \
            if modified else ""
        for rel in new:
            f = wt / rel
            if f.is_file():
                diff += f"\n--- /dev/null\n+++ b/{rel}\n" + "".join(
                    "+" + line for line in f.read_text(errors="replace").splitlines(keepends=True))
        if diff.strip():
            cp = runner([leaks, "stdin", "--no-banner", "--redact"], input=diff, cwd=str(wt),
                        capture_output=True, text=True, timeout=LINT_TIMEOUT_S)
            if cp.returncode != 0:
                errors.append(f"gitleaks found a possible secret in the diff: {_tail(cp)}")
    return errors


def commit_improve(plan: AutodocPlan, message: str, allow, runner=subprocess.run,
                   wip: bool = False) -> bool:
    """Commit every allowed change on the lane's branch. True when a commit was made.

    Paths outside `allow` are never staged — they stay in the worktree as evidence for the
    reviewer (a red run keeps the worktree). `wip` only changes what the caller says in the
    message; the staging rule is the same, because a half-finished allowed change is still
    the honest thing to show a human.
    """
    del wip
    modified, deleted, new = changed_paths(plan, runner)
    paths = [p for p in modified + deleted + new if _allowed(p, allow)]
    if not paths:
        return False
    _git(plan.worktree, "add", "-A", "--", *paths, runner=runner)
    staged = _git(plan.worktree, "diff", "--cached", "--quiet", runner=runner, check=False)
    if staged.returncode == 0:
        return False
    _git(plan.worktree, "commit", "--no-verify", "-m", message, runner=runner,
         env=_commit_env())
    return True
