# autobuild

Autonomous overnight builder: seed a brief in `backlog/`, and outside 08:00–19:00
Europe/Rome the runner writes a spec, TDD-builds a TypeScript repo under
`projects/<slug>/`, verifies it, commits, and pings telegram.

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
