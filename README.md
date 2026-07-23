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
