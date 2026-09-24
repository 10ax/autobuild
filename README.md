# autobuild

Autonomous overnight builder: seed a brief in `backlog/`, and outside 08:00–19:00
Europe/Rome the runner writes a spec, TDD-builds a TypeScript repo under
`projects/<slug>/`, verifies it, commits, and pings telegram.

## Four lanes
A brief's `mode` picks the lane:

- `build` (default) — the above: write a spec, TDD-build a new project under `projects/<slug>/`,
  verify with `pnpm test` + `tsc` (the manager is read off the repo's lockfile, so
  the six projects built before the switch still verify with npm). Playbook: `CLAUDE.md`.
- `document` — write a doc set (README, CLAUDE.md, `docs/WORKING-ON-THIS.md`,
  `docs/CODE-MAP.md`) into a repo that **already exists**, named by the brief's `repo` key.
  Playbook: `AUTODOC.md`.
- `improve` — give a repo that already exists a test suite, a CI workflow, an operating guide
  and a couple of skills. Playbook: `IMPROVE.md`.
- `implement` — carry out an implementation plan that already exists in a repo, named by the
  brief's `plan` key. The only lane allowed to change what the code does. Playbook:
  `IMPLEMENT.md`.

The docs lane never touches your checkout: the agent writes in a `git worktree` under
`state/worktrees/<slug>`, and the daemon commits only the doc set on a branch
`autodoc/<date>` (never pushed). `verify_docs` gates that commit — it fails the run if a
source file moved, if hand-written prose outside the `autodoc:begin/end` markers changed, or
if a single `file:line` anchor in the code map does not resolve. A red run keeps its worktree
so you can see what the agent saw.

```bash
bin/seed-autodoc-briefs.py --discover      # repos with commits attributed to Claude
bin/seed-autodoc-briefs.py --seed          # dry run; --write to create the briefs
bin/seed-autodoc-briefs.py --systemd       # the ReadWritePaths drop-in for the unit
```
Targets live in `config/autodoc-targets.toml` — that file is the authority, not discovery,
and it stays local (it names your repos); `config/autodoc-targets.example.toml` is the
template.
Review a finished item with `git -C <repo> diff master..autodoc/<date>`.

## The quality lane

The docs lane's gate is a fixed file list, which is exactly wrong for a lane whose job is to
write tests. So an `improve` brief states its own contract in the front-matter, and the
daemon enforces it:

| key | meaning |
|---|---|
| `allow` | path globs the run may change — anything outside fails the whole run |
| `require` | globs that must each match a non-empty, non-gitignored file when it ends |
| `rewrite` | pre-existing files whose human prose may be replaced (an empty or boilerplate README) |
| `verify` | shell commands that must exit 0 in the worktree |

`verify_improve` checks containment, then presence, then that untouched Markdown kept its
human text byte for byte, then **runs the `verify` commands itself** — an agent saying the
tests pass is not evidence that they do — then `actionlint` on changed workflows and
`gitleaks` over the diff. Branches land on `quality/<date>` and are never pushed; a red run
keeps its worktree and commits WIP, same as the docs lane.

```bash
bin/seed-improve-briefs.py --seed          # dry run; --write to create the briefs
bin/seed-improve-briefs.py --systemd       # the ReadWritePaths drop-in for the unit
```
There is no discovery here: `config/improve-targets.toml` is written by hand, because every
entry must say which paths its repo's run may touch. It stays local;
`config/improve-targets.example.toml` is the template.
Review a finished item with `git -C <repo> diff <default-branch>..quality/<date>`.

Because this lane builds each repo's toolchain inside the worktree, the sandbox needs the uv
and pnpm caches writable as well as the repos — `--systemd` emits both. One trap worth
knowing: a `uv venv` has **no pip inside it**, so install with
`VIRTUAL_ENV=.venv uv pip install -r ...`, never `.venv/bin/python -m pip`.

## The implement lane

The quality lane's rule is "behaviour must not change". This one exists for the opposite
case: a plan that a human wrote and approved, sitting in the repo, waiting to be carried out.
The brief adds one key — `plan`, a repo-relative path — and `spec.py` resolves it at
validation time, because a mistyped path should cost a second at seeding, not a whole
overnight window.

It reuses the quality lane's gate **unchanged**: same containment, same `require`, same
frozen human prose, same "the daemon runs your verify commands itself". Shipping a feature
does legitimately touch documentation, so a brief names those specific files in `rewrite` —
narrow, explicit, and visible in review — rather than the lane switching the check off.

The agent does not design. `IMPLEMENT.md` tells it the plan is the authority, to work the
tasks in order, to run the verify commands after **every** task, and to stop at the first
one it cannot finish green: seven tasks done and verified is a good night, nine half-done is
a mess someone has to unpick. It also tells it to skip the plan's own `git commit` steps —
plans are written for humans, and in this lane the daemon owns git.

Branches land on `implement/<date>` and are never pushed. Review with
`git -C <repo> diff <default-branch>..implement/<date>`.

```toml
mode = "implement"
repo = "~/Personal/code/<repo>"
plan = "docs/superpowers/plans/<date>-<feature>.md"
allow   = [...]   # what the run may change
require = [...]   # what must exist when it ends
rewrite = [...]   # the docs this feature is allowed to edit, named one by one
verify  = [...]   # what the daemon runs itself
```

## Seed work
Copy `templates/brief.template.md` into `backlog/NNNN-slug.md`, fill Intent +
Acceptance Criteria, set `status = "pending"`.

## Run
- One tick: `python -m autobuild --once`
- Daemon: `bash deploy/install.sh` (systemd user service, survives reboot)
- Logs: `journalctl --user -u autobuild -f` and `state/ledger.jsonl`

## Tune
Edit `config/runner.toml` — quiet hours, `max_concurrency`, `opus_escalation`,
and the optional weekly reserve (`weekly_reserve_enabled` + `weekly_target_usd`).

## Operator notes
- **Containment.** `--permission-mode bypassPermissions --add-dir ~/autobuild`
  only *scopes* which directory `claude` is allowed to touch — it is not a
  sandbox. The real containment is the systemd sandbox in
  `deploy/autobuild.service` (`ProtectSystem=strict`, `NoNewPrivileges=yes`,
  `PrivateTmp=yes`, etc.), which makes the OS enforce a read-only filesystem
  outside `ReadWritePaths`. On first supervised run, confirm `claude` can
  authenticate and that `pnpm install`/`tsc` work end-to-end; if a tool needs to
  write to a path not already in `ReadWritePaths`, add it there. Note the
  `claude` CLI writes `~/.claude.json` (a file *beside* the `~/.claude/`
  directory) plus a `.claude.json.bak` — both are already listed, but if a
  future CLI version writes other files in `$HOME` root you may see permission
  errors. If `claude` still can't persist config under `ProtectSystem=strict`,
  fall back to `ProtectSystem=true` (protects only `/usr`,`/boot`,`/etc`,
  leaving `$HOME` writable while keeping `NoNewPrivileges` + the kernel
  protections).
- **Interpreter.** `ExecStart` uses `/usr/bin/python3`, which must be ≥3.11
  (the runner uses `tomllib`/`zoneinfo`). Adjust the `ExecStart` path if the
  host's default `python3` is older or lives elsewhere.
- **PATH.** The unit's `Environment=PATH=...` currently lists
  `~/.local/bin:~/.cargo/bin:/usr/local/bin:/usr/bin:/bin`. Make sure
  `node`/`pnpm` resolve on that `PATH` — add the node bin dir (e.g. an
  nvm install path) if the build/verify steps can't find them.
