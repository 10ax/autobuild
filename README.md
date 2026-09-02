# autobuild

Autonomous overnight builder: seed a brief in `backlog/`, and outside 08:00–19:00
Europe/Rome the runner writes a spec, TDD-builds a TypeScript repo under
`projects/<slug>/`, verifies it, commits, and pings telegram.

## Two lanes
A brief's `mode` picks the lane:

- `build` (default) — the above: write a spec, TDD-build a new project under `projects/<slug>/`,
  verify with `npm test` + `tsc`. Playbook: `CLAUDE.md`.
- `document` — write a doc set (README, CLAUDE.md, `docs/WORKING-ON-THIS.md`,
  `docs/CODE-MAP.md`) into a repo that **already exists**, named by the brief's `repo` key.
  Playbook: `AUTODOC.md`.

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
  authenticate and that `npm install`/`tsc` work end-to-end; if a tool needs to
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
  `node`/`npm`/`npx` resolve on that `PATH` — add the node bin dir (e.g. an
  nvm install path) if the build/verify steps can't find them.
