+++
spec_version = "1.0"
slug     = "pomodoro-cli"
title    = "Pomodoro Timer CLI"
tier     = "script"
priority = 1
status   = "pending"
tags     = ["cli", "productivity"]
+++
## Intent
A small command-line Pomodoro timer so I can time focus blocks from the terminal
without a GUI app.

## Acceptance Criteria
A1. `pomo start 25` starts a 25-minute timer and prints a desktop notification at zero.
A2. `pomo start 25 --break 5` chains a 5-minute break after the focus block.
A3. The timer state machine (idle → focus → break → idle) is unit-tested.
