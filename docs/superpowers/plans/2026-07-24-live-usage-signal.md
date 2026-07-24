# Live usage signal — implementation plan

**Branch:** `feat/live-usage-signal`. **Goal:** feed the governor the REAL rate-limit signal
(not just EMA-inferred), sourced three ways (hybrid, per the user's choice):
1. **Per-build** — daemon builds switch to `--output-format stream-json --verbose`; capture the
   `rate_limit_event` each build (zero extra usage).
2. **Oracle heartbeat** — a systemd user timer fires a tiny Haiku stream-json ping every ~7 min,
   writes `~/.claude/usage-oracle.json`.
3. **Statusline % snapshot** — `statusline-command.sh` writes `~/.claude/usage-snapshot.json`
   (five_hour/seven_day `used_percentage` + resets) on render (graduated headroom, daytime).

## The signal shapes (confirmed via live probe 2026-07-24)
- stream-json emits `rate_limit_event`:
  `{rate_limit_info:{status:"allowed"|"rejected", resetsAt:<epoch>, rateLimitType:"five_hour"|"seven_day",
    overageStatus, overageDisabledReason:"out_of_credits", isUsingOverage}}`.
  → gives real-time **status** + exact **resetsAt**, but NO used_percentage.
- statusline stdin JSON has `rate_limits.{five_hour,seven_day}.{used_percentage(0-100),resets_at}`.
  → the only source of the **percentage**. Interactive-only → stale overnight → age-gate.
- Batch `--output-format json` result has NEITHER (rl:null) — hence the switch to stream-json.
- overage is disabled/out_of_credits → the plan cap is a HARD wall (no spill zone).

## Files & tasks (TDD each; keep all existing 56 tests green)
- **T1 build.py** — stream-json aware `parse_result` (JSONL: use the `result` event for
  is_error/cost/usage/api_error_status; collect `rate_limit_event` → new BuildResult fields
  `rate_status`, `rate_reset_at`). MUST still parse a single batch JSON object (existing tests).
  build_argv → add `--output-format stream-json --verbose`. Keep api_error_status detection.
- **T2 usage.py (new)** — `UsageSignal` dataclass + `read_signal(paths, now)` that reads
  usage-oracle.json + usage-snapshot.json, age-gates (stale → fields dropped), returns the
  freshest-per-field merged signal. Pure, now-injected.
- **T3 ledger.py** — GovernorState gains `rate_status`, `api_reset_at`, `used_pct_5h`,
  `used_pct_7d`, `signal_at` (persisted; self-healing preserved).
- **T4 governor.py** — compute_pace consumes a UsageSignal: `status=="rejected"` (fresh) → pause;
  fresh `used_pct_5h` → exact `wh = 1 - pct/100` (overrides EMA estimate); `resetsAt`/`api_reset_at`
  → exact window anchoring. Graceful degrade to EMA when no fresh signal. Keep the bands.
- **T5 daemon.py** — each tick assemble the signal (per-build event stored in state ⊕ files via
  usage.read_signal), pass to compute_pace; on a build's rate_limit_event, update state +
  anchor_window(reset). On rejected/near-limit, pause + anchor.
- **T6 deploy/** — `ratelimit-oracle.sh` (haiku stream-json ping → extract rate_limit_event →
  atomic write usage-oracle.json) + `ratelimit-oracle.service` + `.timer` (OnUnitActiveSec=7min).
  NOT run by me; user installs.
- **T7 statusline** — extend `~/.claude/statusline-command.sh` to atomically write
  usage-snapshot.json when rate_limits present (additive; must not change/slow the display).

## Status log
- branch created; plan written.
- T1 build.py: stream-json argv + JSONL-aware parse_result + rate_limit_event capture (rate_status,
  rate_reset_at). DONE, tests green (still parses batch json).
- T2 usage.py: UsageSignal + read_signal (age-gated merge of oracle+snapshot) + write_oracle. DONE, 7 tests.
- T3 ledger: NOT NEEDED — signal comes from files (usage.read_signal), not persisted state.
- T4 governor: compute_pace(signal=) — rejected→pause, live used_pct_5h→exact wh (overrides EMA),
  degrades to EMA. DONE, tests green.
- T5 daemon: read_signal each tick → compute_pace; on build rate_limit_event write_oracle +
  anchor(rate_reset_at||reset_at). Existing tests made hermetic. DONE, 8 tests.
- T6 deploy/ratelimit-oracle.{sh,service,timer}: VALIDATED LIVE — script wrote a correct
  ~/.claude/usage-oracle.json, read_signal round-tripped it.
- T7 statusline: ~/.claude/statusline-command.sh extended (backup: .bak-usage) to atomic-write
  usage-snapshot.json. VALIDATED — toolbar unchanged, snapshot parses, no-rl case writes nothing.
- Full suite: 74 tests pass.

## Caveats / follow-ups
- Per-build leg buffers a build's full stream-json stdout (potentially tens of MB). Acceptable v1;
  streaming (Popen line-reader in run_build) is the optimization if memory bites.
- rate_limit_event carries status+resetsAt but NO %; the % is statusline-only (age-gated, daytime).
- Unverified on a REAL long build that stream-json+--verbose completes cleanly & emits result +
  rate_limit_event (probe was a trivial 'ok'). Watch the first supervised build.

## Host-level installs the user must do (NOT done by me)
- Oracle timer: cp deploy/ratelimit-oracle.{service,timer} → ~/.config/systemd/user/;
  `systemctl --user daemon-reload && systemctl --user enable --now ratelimit-oracle.timer`.
- Statusline edit ALREADY applied to ~/.claude/statusline-command.sh (revert: restore .bak-usage).
- After merge: `systemctl --user restart autobuild` so the daemon loads the new code.
