"""The implement lane: carry out an implementation plan that already exists in a repo.

This is the only lane allowed to change what the code *does*. The docs lane guarantees that
nothing but four doc files moved; the quality lane guarantees that behaviour did not change.
Neither guarantee can hold here, because the whole point is to ship the feature a plan
describes — so the guarantee is the plan itself plus the same contract the quality lane uses:

1. **The plan is the authority.** The brief names a plan file inside the repo, `spec.py`
   resolves it at validation time, and the agent's job is to execute it task by task rather
   than to design anything. A plan that does not say what to do is a plan to fix, not a gap
   for an unattended agent to fill.
2. **Containment, presence, verification, hygiene** — `verify_improve`, unchanged. The
   contract lists in the brief draw the lines; the daemon runs the repo's own test suite
   itself, because an agent saying the tests pass is not evidence that they do.

The one place the two lanes genuinely differ is human prose. The quality lane freezes it,
which is right when behaviour must not change. Shipping a feature legitimately adds a row to
an env-var table or a section to a reference document — so an implement brief names those
specific files in its `rewrite` list. That keeps the escape narrow, explicit and visible in
review, instead of switching the check off for a whole lane.

Everything else — throwaway worktree, the daemon owning every git write, WIP commit and a
kept worktree on a red run — is the quality lane's choreography under this lane's name.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from autobuild import autodoc, improve
from autobuild.autodoc import AutodocError, AutodocPlan

__all__ = ["AutodocError", "AutodocPlan", "prepare_worktree", "verify_implement",
           "commit_implement", "remove_worktree", "changed_paths", "BRANCH_PREFIX"]

BRANCH_PREFIX = "implement"


def prepare_worktree(repo: Path, slug: str, worktrees_dir: Path, date: str,
                     runner=subprocess.run) -> AutodocPlan:
    """A worktree on `implement/<date>`, never pushed — review it with `git diff`."""
    return autodoc.prepare_worktree(repo, slug, worktrees_dir, date, runner=runner,
                                    prefix=BRANCH_PREFIX)


# The gate is the quality lane's, used as-is: a feature landing inside the brief's `allow`
# list, delivering its `require` files and passing its `verify` commands is exactly what
# "this plan was carried out" means. Sharing it means one gate to trust, not two to keep
# in step.
verify_implement = improve.verify_improve
commit_implement = improve.commit_improve
changed_paths = improve.changed_paths
remove_worktree = autodoc.remove_worktree
