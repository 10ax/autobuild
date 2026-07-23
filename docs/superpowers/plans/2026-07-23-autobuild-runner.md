# Autobuild Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dumb controller daemon that autonomously turns a user-seeded backlog of idea briefs into TDD-built, tokensave-wired TypeScript repos, paced by an adaptive bidirectional rate-limit governor.

**Architecture:** A Python-stdlib controller (`autobuild/` package) sequences backlog items and spawns one fresh headless `claude -p` per project. All state lives in files + git — no accumulated context, no workflow engine. A governor learns the 5-hour window ceiling from 429s and scales concurrency / in-run subagents / model (Sonnet→Opus) up when headroom is ample, down near the cap, and pauses during owner daytime hours.

**Tech Stack:** Python 3.14 (stdlib only: `tomllib`, `zoneinfo`, `json`, `subprocess`, `dataclasses`, `concurrent.futures`, `re`, `unittest`), Node 22 / `tsc` (built projects), `claude` CLI 2.1.218, `tokensave`, existing `~/.claude/notify-telegram.sh`.

## Global Constraints

- **Zero pip dependencies** — controller runtime AND tests use Python stdlib only (`unittest`, not pytest).
- **All commands run from repo root** `~/autobuild` unless stated; tests: `python -m unittest tests.<module> -v`.
- **Fixed stack** for built projects: TypeScript / Node.
- **Front-matter is TOML** (`+++`-delimited), read with `tomllib`; never write TOML — mutate the `status` line by regex, persist all other state as JSON/JSONL.
- **Default model** `sonnet`; escalate to `opus` only when window *and* weekly headroom are ample.
- **Timezone** `Europe/Rome`; quiet hours `08:00`–`19:00` (runner paused).
- **Safety:** headless runs use `--permission-mode bypassPermissions --add-dir ~/autobuild`; **no auto-push to remotes**; no Docker (v1).
- **Now-injection:** every time-dependent function takes an explicit `now: datetime` (tz-aware) — never call `datetime.now()` inside logic, so it's testable.
- **All dataclasses** `from __future__ import annotations` at top of each module.

---

## File Structure

```
autobuild/                 # Python package (controller)
  __init__.py
  __main__.py              # `python -m autobuild` entrypoint
  config.py                # Config dataclass + load_config
  spec.py                  # SpecDoc, parse_spec, validate_spec, constants
  ledger.py                # append/read ledger + GovernorState persistence
  governor.py              # Pace, quiet-hours, ceiling EMA, compute_pace, record_spend
  backlog.py               # Item, scan/select, set_status, lock file
  build.py                 # build_argv, parse_result, run_build, verify_repo
  notify.py                # format_message, notify (telegram wrapper)
  daemon.py                # run_once (one tick) + main (loop, argparse)
tests/
  test_config.py test_spec.py test_ledger.py test_governor.py
  test_backlog.py test_build.py test_notify.py test_daemon.py
config/runner.toml
schema/spec.schema.json
templates/spec.template.md  templates/brief.template.md
prompts/build.md
CLAUDE.md
deploy/autobuild.service
backlog/0001-example.md
README.md
```

**Dependency order:** config → spec → ledger → governor → backlog → build → notify → daemon → build-assets → deploy.

---

### Task 1: Config loader

**Files:**
- Create: `autobuild/__init__.py` (empty), `autobuild/config.py`
- Test: `tests/__init__.py` (empty), `tests/test_config.py`

**Interfaces:**
- Produces: `Config` dataclass (fields below); `load_config(path: Path, root: Path | None = None) -> Config`; `ConfigError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import tempfile, unittest
from pathlib import Path
from autobuild.config import load_config, ConfigError

def _write(text):
    d = Path(tempfile.mkdtemp())
    (d / "config").mkdir()
    p = d / "config" / "runner.toml"
    p.write_text(text)
    return p, d

class TestConfig(unittest.TestCase):
    def test_defaults_when_empty(self):
        p, d = _write("")
        cfg = load_config(p, root=d)
        self.assertEqual(cfg.timezone, "Europe/Rome")
        self.assertEqual(cfg.max_concurrency, 3)
        self.assertFalse(cfg.weekly_reserve_enabled)
        self.assertEqual(cfg.default_model, "sonnet")

    def test_overrides(self):
        p, d = _write('[pace]\nmax_concurrency = 2\n[build]\ndefault_model = "opus"\n')
        cfg = load_config(p, root=d)
        self.assertEqual(cfg.max_concurrency, 2)
        self.assertEqual(cfg.default_model, "opus")

    def test_invalid_model_raises(self):
        p, d = _write('[build]\ndefault_model = "gpt"\n')
        with self.assertRaises(ConfigError):
            load_config(p, root=d)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_config -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobuild.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# autobuild/config.py
from __future__ import annotations
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class Config:
    root: Path
    timezone: str = "Europe/Rome"
    quiet_from: str = "08:00"
    quiet_to: str = "19:00"
    max_concurrency: int = 3
    per_project_timeout_min: int = 30
    opus_escalation: bool = True
    weekly_reserve_enabled: bool = False
    weekly_reserve_frac: float = 0.12
    weekly_target_usd: float = 0.0
    stack: str = "typescript"
    default_model: str = "sonnet"
    telegram_script: str = "~/.claude/notify-telegram.sh"
    notify_on: list[str] = field(
        default_factory=lambda: ["done", "needs-review", "paused", "crash"]
    )


def load_config(path: Path, root: Path | None = None) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    data = tomllib.loads(path.read_text())
    hours, pace = data.get("hours", {}), data.get("pace", {})
    build, notify = data.get("build", {}), data.get("notify", {})
    cfg = Config(
        root=Path(root).resolve() if root else path.resolve().parent.parent,
        timezone=hours.get("timezone", "Europe/Rome"),
        quiet_from=hours.get("quiet_from", "08:00"),
        quiet_to=hours.get("quiet_to", "19:00"),
        max_concurrency=int(pace.get("max_concurrency", 3)),
        per_project_timeout_min=int(pace.get("per_project_timeout_min", 30)),
        opus_escalation=bool(pace.get("opus_escalation", True)),
        weekly_reserve_enabled=bool(pace.get("weekly_reserve_enabled", False)),
        weekly_reserve_frac=float(pace.get("weekly_reserve_frac", 0.12)),
        weekly_target_usd=float(pace.get("weekly_target_usd", 0.0)),
        stack=build.get("stack", "typescript"),
        default_model=build.get("default_model", "sonnet"),
        telegram_script=notify.get("telegram_script", "~/.claude/notify-telegram.sh"),
        notify_on=list(notify.get("notify_on", ["done", "needs-review", "paused", "crash"])),
    )
    if cfg.max_concurrency < 1:
        raise ConfigError("pace.max_concurrency must be >= 1")
    if cfg.default_model not in ("sonnet", "opus"):
        raise ConfigError("build.default_model must be sonnet or opus")
    return cfg
```

Also create empty `autobuild/__init__.py` and `tests/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_config -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add autobuild/__init__.py autobuild/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: config loader with TOML + defaults + validation"
```

---

### Task 2: Standardized spec model (schema + parser + validator)

**Files:**
- Create: `autobuild/spec.py`, `schema/spec.schema.json`, `templates/spec.template.md`
- Test: `tests/test_spec.py`

**Interfaces:**
- Produces: `SpecDoc(meta: dict, sections: dict[str, str], raw: str, path: Path | None = None)`;
  `parse_spec(text: str) -> SpecDoc`; `validate_spec(doc: SpecDoc, level: str = "spec") -> list[str]`;
  constants `TIERS`, `STATUSES`, `MODELS`, `SPEC_SECTIONS`, `BRIEF_SECTIONS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spec.py
import unittest
from autobuild.spec import parse_spec, validate_spec

VALID_BRIEF = '''+++
spec_version = "1.0"
slug = "pomodoro-cli"
title = "Pomodoro Timer CLI"
tier = "script"
priority = 3
status = "pending"
+++
## Intent
A CLI Pomodoro timer.

## Acceptance Criteria
A1. `pomo start 25` runs a 25-minute timer.
'''

class TestSpec(unittest.TestCase):
    def test_parse_front_matter_and_sections(self):
        doc = parse_spec(VALID_BRIEF)
        self.assertEqual(doc.meta["slug"], "pomodoro-cli")
        self.assertIn("Intent", doc.sections)
        self.assertTrue(doc.sections["Acceptance Criteria"].startswith("A1."))

    def test_valid_brief_passes_brief_level(self):
        self.assertEqual(validate_spec(parse_spec(VALID_BRIEF), level="brief"), [])

    def test_brief_missing_full_sections_fails_spec_level(self):
        errs = validate_spec(parse_spec(VALID_BRIEF), level="spec")
        self.assertTrue(any("Domain Model" in e for e in errs))

    def test_bad_tier_and_placeholder_flagged(self):
        bad = VALID_BRIEF.replace('tier = "script"', 'tier = "huge"') \
                         .replace("A CLI Pomodoro timer.", "TBD")
        errs = validate_spec(parse_spec(bad), level="brief")
        self.assertTrue(any("tier" in e for e in errs))
        self.assertTrue(any("Intent" in e for e in errs))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_spec -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobuild.spec'`

- [ ] **Step 3: Write minimal implementation**

```python
# autobuild/spec.py
from __future__ import annotations
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TIERS = {"script", "library", "service"}
STATUSES = {"pending", "building", "done", "needs-review"}
MODELS = {"auto", "sonnet", "opus"}
SPEC_SECTIONS = ["Intent", "Ubiquitous Language", "Domain Model", "Requirements",
                 "Interfaces", "Acceptance Criteria", "Non-Goals", "Constraints"]
BRIEF_SECTIONS = ["Intent", "Acceptance Criteria"]

_FRONT = re.compile(r"^\+\+\+\s*\n(.*?)\n\+\+\+\s*\n(.*)$", re.DOTALL)
_PLACEHOLDER = re.compile(r"^(tbd|todo|\.\.\.|xxx|n/a)\b", re.IGNORECASE)
_H2 = re.compile(r"^##\s+(.*\S)\s*$")
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass
class SpecDoc:
    meta: dict
    sections: dict[str, str]
    raw: str
    path: Path | None = None


def parse_spec(text: str) -> SpecDoc:
    m = _FRONT.match(text.lstrip("﻿").lstrip())
    if not m:
        return SpecDoc(meta={}, sections={}, raw=text)
    meta = tomllib.loads(m.group(1))
    sections: dict[str, str] = {}
    cur, buf = None, []
    for line in m.group(2).splitlines():
        h = _H2.match(line)
        if h:
            if cur is not None:
                sections[cur] = "\n".join(buf).strip()
            cur, buf = h.group(1).strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        sections[cur] = "\n".join(buf).strip()
    return SpecDoc(meta=meta, sections=sections, raw=text)


def validate_spec(doc: SpecDoc, level: str = "spec") -> list[str]:
    errors: list[str] = []
    meta = doc.meta
    for k in ["spec_version", "slug", "title", "tier", "priority", "status"]:
        if k not in meta:
            errors.append(f"missing front-matter key: {k}")
    if meta.get("tier") not in TIERS:
        errors.append(f"tier must be one of {sorted(TIERS)}")
    if meta.get("status") not in STATUSES:
        errors.append(f"status must be one of {sorted(STATUSES)}")
    if meta.get("model", "auto") not in MODELS:
        errors.append(f"model must be one of {sorted(MODELS)}")
    if not _SLUG.match(str(meta.get("slug", ""))):
        errors.append("slug must be kebab-case")
    required = BRIEF_SECTIONS if level == "brief" else SPEC_SECTIONS
    for s in required:
        v = doc.sections.get(s, "").strip()
        if not v:
            errors.append(f"section empty/missing: {s}")
        elif _PLACEHOLDER.match(v):
            errors.append(f"section is placeholder: {s}")
    return errors
```

- [ ] **Step 4: Write the schema + template artifacts**

```json
// schema/spec.schema.json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Autobuild Spec front-matter",
  "type": "object",
  "required": ["spec_version", "slug", "title", "tier", "priority", "status"],
  "properties": {
    "spec_version": {"const": "1.0"},
    "slug": {"type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"},
    "title": {"type": "string", "minLength": 1},
    "tier": {"enum": ["script", "library", "service"]},
    "priority": {"type": "integer"},
    "status": {"enum": ["pending", "building", "done", "needs-review"]},
    "stack": {"type": "string", "default": "typescript"},
    "model": {"enum": ["auto", "sonnet", "opus"], "default": "auto"},
    "tags": {"type": "array", "items": {"type": "string"}}
  },
  "additionalProperties": true
}
```

```markdown
<!-- templates/spec.template.md -->
+++
spec_version = "1.0"
slug     = "REPLACE-kebab-slug"
title    = "REPLACE Title"
tier     = "script"          # script | library | service
priority = 1
status   = "pending"
stack    = "typescript"
model    = "auto"
tags     = []
+++
## Intent
<one paragraph: the problem and why it matters>

## Ubiquitous Language
- term — definition

## Domain Model
<entities / value objects / invariants; depth scales with tier>

## Requirements
R1. <testable statement>

## Interfaces
<CLI commands / public API / contracts>

## Acceptance Criteria
A1. <executable check that maps to a test>

## Non-Goals
- <out of scope>

## Constraints
- <allowed deps, perf budgets, etc.>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_spec -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add autobuild/spec.py schema/spec.schema.json templates/spec.template.md tests/test_spec.py
git commit -m "feat: standardized spec model — parser, validator, schema, template"
```

---

### Task 3: Ledger + governor-state persistence

**Files:**
- Create: `autobuild/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Produces: `append_ledger(path: Path, entry: dict) -> None`; `read_ledger(path: Path) -> list[dict]`;
  `GovernorState(week_start: str, window_start: float | None, window_spend_usd: float, learned_ceiling_usd: float, weekly_spend_usd: float)`;
  `load_governor_state(path: Path) -> GovernorState`; `save_governor_state(path: Path, state: GovernorState) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger.py
import tempfile, unittest
from pathlib import Path
from autobuild.ledger import (append_ledger, read_ledger, GovernorState,
                              load_governor_state, save_governor_state)

class TestLedger(unittest.TestCase):
    def test_ledger_roundtrip(self):
        p = Path(tempfile.mkdtemp()) / "ledger.jsonl"
        append_ledger(p, {"slug": "a", "cost_usd": 1.0})
        append_ledger(p, {"slug": "b", "cost_usd": 2.0})
        rows = read_ledger(p)
        self.assertEqual([r["slug"] for r in rows], ["a", "b"])

    def test_governor_state_defaults_and_roundtrip(self):
        p = Path(tempfile.mkdtemp()) / "governor.json"
        st = load_governor_state(p)                       # missing file -> defaults
        self.assertEqual(st.weekly_spend_usd, 0.0)
        self.assertIsNone(st.window_start)
        st.learned_ceiling_usd = 12.5
        save_governor_state(p, st)
        self.assertEqual(load_governor_state(p).learned_ceiling_usd, 12.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_ledger -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobuild.ledger'`

- [ ] **Step 3: Write minimal implementation**

```python
# autobuild/ledger.py
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from pathlib import Path


def append_ledger(path: Path, entry: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_ledger(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@dataclass
class GovernorState:
    week_start: str = ""            # ISO date; "" means uninitialised
    window_start: float | None = None  # epoch seconds of current 5h window
    window_spend_usd: float = 0.0
    learned_ceiling_usd: float = 0.0   # 0 => not yet calibrated
    weekly_spend_usd: float = 0.0


def load_governor_state(path: Path) -> GovernorState:
    path = Path(path)
    if not path.exists():
        return GovernorState()
    return GovernorState(**json.loads(path.read_text()))


def save_governor_state(path: Path, state: GovernorState) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_ledger -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add autobuild/ledger.py tests/test_ledger.py
git commit -m "feat: append-only ledger + governor-state persistence"
```

---

### Task 4: Adaptive bidirectional governor

**Files:**
- Create: `autobuild/governor.py`
- Test: `tests/test_governor.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `GovernorState` (Task 3).
- Produces: `Pace(level: str, concurrency: int, subagents: bool, model: str)`;
  `in_quiet_hours(now, cfg) -> bool`; `next_active_time(now, cfg) -> datetime`;
  `weekly_headroom(cfg, state) -> float`; `compute_pace(now, cfg, state) -> Pace`;
  `record_spend(state, cost_usd, now) -> GovernorState`;
  `update_ceiling_ema(state, spend_at_429, alpha=0.3) -> GovernorState`;
  `anchor_window(state, reset_epoch) -> GovernorState`; constant `WINDOW_SECONDS = 18000`.
  `now` is a tz-aware `datetime`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_governor.py
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState
from autobuild.governor import (in_quiet_hours, compute_pace, record_spend,
                                update_ceiling_ema, WINDOW_SECONDS)

TZ = ZoneInfo("Europe/Rome")
def _cfg(**kw):
    return Config(root=".", **kw)
def _at(h):  # today at hour h, Rome
    return datetime(2026, 7, 23, h, 0, tzinfo=TZ)

class TestGovernor(unittest.TestCase):
    def test_daytime_pauses(self):
        p = compute_pace(_at(12), _cfg(), GovernorState())
        self.assertEqual(p.level, "pause")

    def test_night_uncalibrated_is_conservative(self):
        p = compute_pace(_at(23), _cfg(), GovernorState())
        self.assertEqual((p.concurrency, p.model, p.subagents), (1, "sonnet", False))

    def test_night_ample_headroom_ramps_and_escalates(self):
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=0.5,
                           window_start=_at(23).timestamp())
        p = compute_pace(_at(23), _cfg(max_concurrency=3), st)
        self.assertEqual(p.level, "high")
        self.assertEqual(p.concurrency, 3)
        self.assertTrue(p.subagents)
        self.assertEqual(p.model, "opus")

    def test_near_ceiling_pauses(self):
        st = GovernorState(learned_ceiling_usd=10.0, window_spend_usd=9.8,
                           window_start=_at(23).timestamp())
        self.assertEqual(compute_pace(_at(23), _cfg(), st).level, "pause")

    def test_ema_calibrates_from_first_429(self):
        st = update_ceiling_ema(GovernorState(), 8.0)
        self.assertEqual(st.learned_ceiling_usd, 8.0)
        st = update_ceiling_ema(st, 12.0, alpha=0.5)
        self.assertEqual(st.learned_ceiling_usd, 10.0)

    def test_record_spend_accumulates_window_and_week(self):
        st = record_spend(GovernorState(), 1.5, _at(23))
        self.assertAlmostEqual(st.window_spend_usd, 1.5)
        self.assertAlmostEqual(st.weekly_spend_usd, 1.5)
        self.assertIsNotNone(st.window_start)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_governor -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobuild.governor'`

- [ ] **Step 3: Write minimal implementation**

```python
# autobuild/governor.py
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState

WINDOW_SECONDS = 5 * 3600


@dataclass
class Pace:
    level: str          # pause | low | moderate | high
    concurrency: int
    subagents: bool
    model: str          # sonnet | opus


def _parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, cfg: Config) -> bool:
    t = now.timetz().replace(tzinfo=None)
    a, b = _parse_hm(cfg.quiet_from), _parse_hm(cfg.quiet_to)
    return a <= t < b if a <= b else (t >= a or t < b)


def next_active_time(now: datetime, cfg: Config) -> datetime:
    if not in_quiet_hours(now, cfg):
        return now
    b = _parse_hm(cfg.quiet_to)
    cand = now.replace(hour=b.hour, minute=b.minute, second=0, microsecond=0)
    return cand if cand > now else cand + timedelta(days=1)


def weekly_headroom(cfg: Config, state: GovernorState) -> float:
    if not cfg.weekly_reserve_enabled or cfg.weekly_target_usd <= 0:
        return 1.0
    return max(0.0, 1.0 - state.weekly_spend_usd / cfg.weekly_target_usd)


def update_ceiling_ema(state: GovernorState, spend_at_429: float, alpha: float = 0.3) -> GovernorState:
    if state.learned_ceiling_usd <= 0:
        state.learned_ceiling_usd = spend_at_429
    else:
        state.learned_ceiling_usd = (1 - alpha) * state.learned_ceiling_usd + alpha * spend_at_429
    return state


def anchor_window(state: GovernorState, reset_epoch: float) -> GovernorState:
    state.window_start = reset_epoch
    state.window_spend_usd = 0.0
    return state


def record_spend(state: GovernorState, cost_usd: float, now: datetime) -> GovernorState:
    now_ts = now.timestamp()
    if state.window_start is None or now_ts - state.window_start >= WINDOW_SECONDS:
        state.window_start = now_ts
        state.window_spend_usd = 0.0
    state.window_spend_usd += cost_usd
    today = now.date()
    ws = date.fromisoformat(state.week_start) if state.week_start else today
    if (today - ws).days >= 7 or (today - ws).days < 0:
        ws = today
        state.weekly_spend_usd = 0.0
    state.week_start = ws.isoformat()
    state.weekly_spend_usd += cost_usd
    return state


def compute_pace(now: datetime, cfg: Config, state: GovernorState) -> Pace:
    if in_quiet_hours(now, cfg):
        return Pace("pause", 0, False, cfg.default_model)
    wk = weekly_headroom(cfg, state)
    if cfg.weekly_reserve_enabled and cfg.weekly_target_usd > 0 and wk < cfg.weekly_reserve_frac:
        return Pace("pause", 0, False, cfg.default_model)
    # Uncalibrated: run conservatively while we learn the ceiling.
    if state.learned_ceiling_usd <= 0:
        return Pace("low", 1, False, "sonnet")
    wh = max(0.0, 1.0 - state.window_spend_usd / state.learned_ceiling_usd)
    # Late-in-window clamp: never start a wide fan-out that will die at the cap.
    late = (state.window_start is not None
            and (now.timestamp() - state.window_start) / WINDOW_SECONDS > 0.8)
    if wh <= 0.05:
        return Pace("pause", 0, False, cfg.default_model)
    if wh < 0.30 or late:
        return Pace("low", 1, False, "sonnet")
    if wh < 0.60:
        return Pace("moderate", min(2, cfg.max_concurrency), False, "sonnet")
    model = "opus" if (cfg.opus_escalation and (not cfg.weekly_reserve_enabled or wk > 0.40)) else "sonnet"
    return Pace("high", cfg.max_concurrency, True, model)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_governor -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add autobuild/governor.py tests/test_governor.py
git commit -m "feat: adaptive bidirectional governor (quiet hours, ceiling EMA, pace bands)"
```

---

### Task 5: Backlog selection + status + lock

**Files:**
- Create: `autobuild/backlog.py`
- Test: `tests/test_backlog.py`

**Interfaces:**
- Consumes: `parse_spec`, `SpecDoc` (Task 2).
- Produces: `Item(path: Path, meta: dict, doc: SpecDoc)`; `scan_backlog(backlog_dir: Path) -> list[Item]`;
  `select_pending(items: list[Item], n: int) -> list[Item]`; `set_status(item: Item, status: str) -> None`;
  `read_lock(path: Path) -> list[dict]`; `add_lock(path: Path, entry: dict) -> None`; `clear_lock(path: Path, slug: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backlog.py
import tempfile, unittest
from pathlib import Path
from autobuild.backlog import (scan_backlog, select_pending, set_status,
                               add_lock, read_lock, clear_lock)

BRIEF = '''+++
spec_version = "1.0"
slug = "{slug}"
title = "{slug}"
tier = "script"
priority = {prio}
status = "{status}"
+++
## Intent
x
## Acceptance Criteria
A1. x
'''

def _backlog():
    d = Path(tempfile.mkdtemp()) / "backlog"
    d.mkdir()
    (d / ".gitkeep").write_text("")
    (d / "a.md").write_text(BRIEF.format(slug="alpha", prio=1, status="pending"))
    (d / "b.md").write_text(BRIEF.format(slug="beta", prio=5, status="pending"))
    (d / "c.md").write_text(BRIEF.format(slug="gamma", prio=9, status="done"))
    return d

class TestBacklog(unittest.TestCase):
    def test_scan_skips_gitkeep(self):
        self.assertEqual(len(scan_backlog(_backlog())), 3)

    def test_select_pending_orders_by_priority_desc(self):
        picked = select_pending(scan_backlog(_backlog()), 2)
        self.assertEqual([i.meta["slug"] for i in picked], ["beta", "alpha"])

    def test_set_status_persists(self):
        items = scan_backlog(_backlog())
        target = [i for i in items if i.meta["slug"] == "alpha"][0]
        set_status(target, "building")
        again = [i for i in scan_backlog(target.path.parent) if i.meta["slug"] == "alpha"][0]
        self.assertEqual(again.meta["status"], "building")

    def test_lock_roundtrip(self):
        p = Path(tempfile.mkdtemp()) / "current.lock"
        add_lock(p, {"slug": "alpha", "repo": "/r/alpha"})
        add_lock(p, {"slug": "beta", "repo": "/r/beta"})
        self.assertEqual({e["slug"] for e in read_lock(p)}, {"alpha", "beta"})
        clear_lock(p, "alpha")
        self.assertEqual([e["slug"] for e in read_lock(p)], ["beta"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_backlog -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobuild.backlog'`

- [ ] **Step 3: Write minimal implementation**

```python
# autobuild/backlog.py
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from pathlib import Path
from autobuild.spec import parse_spec, SpecDoc

_STATUS_LINE = re.compile(r'(?m)^(status\s*=\s*)".*?"')


@dataclass
class Item:
    path: Path
    meta: dict
    doc: SpecDoc


def scan_backlog(backlog_dir: Path) -> list[Item]:
    items = []
    for p in sorted(Path(backlog_dir).glob("*.md")):
        doc = parse_spec(p.read_text())
        doc.path = p
        items.append(Item(path=p, meta=doc.meta, doc=doc))
    return items


def select_pending(items: list[Item], n: int) -> list[Item]:
    pend = [i for i in items if i.meta.get("status") == "pending"]
    pend.sort(key=lambda i: (-int(i.meta.get("priority", 0)),
                             str(i.meta.get("created", "")), i.path.name))
    return pend[:n]


def set_status(item: Item, status: str) -> None:
    text = item.path.read_text()
    new = _STATUS_LINE.sub(rf'\g<1>"{status}"', text, count=1)
    item.path.write_text(new)
    item.meta["status"] = status


def read_lock(path: Path) -> list[dict]:
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else []


def add_lock(path: Path, entry: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [e for e in read_lock(path) if e.get("slug") != entry.get("slug")]
    rows.append(entry)
    path.write_text(json.dumps(rows, indent=2))


def clear_lock(path: Path, slug: str) -> None:
    path = Path(path)
    rows = [e for e in read_lock(path) if e.get("slug") != slug]
    path.write_text(json.dumps(rows, indent=2))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_backlog -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add autobuild/backlog.py tests/test_backlog.py
git commit -m "feat: backlog scan/select, status mutation, crash-recovery lock"
```

---

### Task 6: Build runner (spawn claude, parse result, detect 429, verify)

**Files:**
- Create: `autobuild/build.py`
- Test: `tests/test_build.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `Pace` (Task 4).
- Produces: `BuildResult(is_error: bool, cost_usd: float, usage: dict, session_id: str | None, rate_limited: bool, reset_at: float | None, raw: dict)`;
  `build_argv(brief_path, repo_root, model, cfg) -> list[str]`; `parse_result(stdout: str) -> BuildResult`;
  `run_build(brief_path, repo_root, model, pace, cfg, runner=subprocess.run) -> BuildResult`;
  `verify_repo(repo_root, runner=subprocess.run) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build.py
import json, unittest
from pathlib import Path
from autobuild.config import Config
from autobuild.governor import Pace
from autobuild.build import build_argv, parse_result, run_build, verify_repo

class _CP:  # fake CompletedProcess
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode

class TestBuild(unittest.TestCase):
    def test_argv_includes_safety_flags_and_model(self):
        argv = build_argv(Path("/b/x.md"), Path("/r/x"), "opus", Config(root="/home/tenax/autobuild"))
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertIn("--add-dir", argv)

    def test_parse_success_result(self):
        out = json.dumps({"is_error": False, "total_cost_usd": 0.42,
                          "usage": {"output_tokens": 100}, "session_id": "s1"})
        r = parse_result(out)
        self.assertFalse(r.is_error)
        self.assertAlmostEqual(r.cost_usd, 0.42)
        self.assertFalse(r.rate_limited)

    def test_parse_detects_rate_limit_and_reset(self):
        out = json.dumps({"is_error": True, "subtype": "error_max_turns",
                          "result": "Usage limit reached. reset_at:1753305600"})
        r = parse_result(out)
        self.assertTrue(r.rate_limited)
        self.assertEqual(r.reset_at, 1753305600.0)

    def test_run_build_uses_injected_runner(self):
        out = json.dumps({"is_error": False, "total_cost_usd": 1.0})
        r = run_build(Path("/b/x.md"), Path("/r/x"), "sonnet",
                      Pace("high", 3, True, "sonnet"), Config(root="/tmp"),
                      runner=lambda *a, **k: _CP(stdout=out))
        self.assertAlmostEqual(r.cost_usd, 1.0)

    def test_verify_repo_gates_on_exit_codes(self):
        self.assertTrue(verify_repo(Path("/r/x"), runner=lambda *a, **k: _CP(returncode=0)))
        self.assertFalse(verify_repo(Path("/r/x"), runner=lambda *a, **k: _CP(returncode=1)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_build -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobuild.build'`

- [ ] **Step 3: Write minimal implementation**

```python
# autobuild/build.py
from __future__ import annotations
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from autobuild.config import Config
from autobuild.governor import Pace

_RESET = re.compile(r'(?:reset_at|resetsAt|"reset")\D{0,4}(\d{10})')
_RL_MARKERS = ("rate limit", "rate_limit", "usage limit", "429", "exceeded your")


@dataclass
class BuildResult:
    is_error: bool
    cost_usd: float
    usage: dict = field(default_factory=dict)
    session_id: str | None = None
    rate_limited: bool = False
    reset_at: float | None = None
    raw: dict = field(default_factory=dict)


def build_argv(brief_path: Path, repo_root: Path, model: str, cfg: Config) -> list[str]:
    prompt = (f"Build backlog item: {brief_path}. Target repo dir: {repo_root}. "
              f"Follow the process in {Path(cfg.root)}/CLAUDE.md exactly.")
    return ["claude", "-p", prompt, "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(cfg.root), "--model", model]


def _load_json(stdout: str) -> dict:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        for line in reversed(stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def parse_result(stdout: str) -> BuildResult:
    obj = _load_json(stdout)
    if not obj:
        return BuildResult(is_error=True, cost_usd=0.0, raw={})
    text = json.dumps(obj).lower()
    rate_limited = any(m in text for m in _RL_MARKERS)
    m = _RESET.search(json.dumps(obj))
    reset_at = float(m.group(1)) if m else None
    return BuildResult(
        is_error=bool(obj.get("is_error", False)),
        cost_usd=float(obj.get("total_cost_usd", 0.0) or 0.0),
        usage=obj.get("usage", {}) or {},
        session_id=obj.get("session_id"),
        rate_limited=rate_limited,
        reset_at=reset_at,
        raw=obj,
    )


def run_build(brief_path: Path, repo_root: Path, model: str, pace: Pace,
              cfg: Config, runner=subprocess.run) -> BuildResult:
    argv = build_argv(brief_path, repo_root, model, cfg)
    env = dict(os.environ, PACE=("high" if pace.subagents else "low"))
    try:
        cp = runner(argv, capture_output=True, text=True,
                    timeout=cfg.per_project_timeout_min * 60, env=env)
    except subprocess.TimeoutExpired:
        return BuildResult(is_error=True, cost_usd=0.0, raw={"timeout": True})
    return parse_result(cp.stdout or "")


def verify_repo(repo_root: Path, runner=subprocess.run) -> bool:
    for cmd in (["npm", "test", "--silent"], ["npx", "tsc", "--noEmit"]):
        cp = runner(cmd, cwd=str(repo_root), capture_output=True, text=True)
        if cp.returncode != 0:
            return False
    return True
```

> **Implementation note:** the exact rate-limit message / reset field emitted by `claude -p --output-format json` must be confirmed against a real 429 during execution. Capture one real error payload, add it as a fixture in `tests/test_build.py`, and adjust `_RESET` / `_RL_MARKERS` if the real shape differs.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_build -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add autobuild/build.py tests/test_build.py
git commit -m "feat: headless build runner — argv, result/429 parsing, verify gate"
```

---

### Task 7: Telegram notifier

**Files:**
- Create: `autobuild/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: `Config` (Task 1).
- Produces: `format_message(event: str, **kw) -> str`; `notify(cfg, event, runner=subprocess.run, **kw) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notify.py
import unittest
from autobuild.config import Config
from autobuild.notify import format_message, notify

class TestNotify(unittest.TestCase):
    def test_format_done(self):
        msg = format_message("done", slug="pomodoro-cli", tests="green", cost=0.4, mins=7, repo="/r/p")
        self.assertIn("pomodoro-cli", msg)
        self.assertIn("green", msg)

    def test_notify_skips_events_not_in_notify_on(self):
        calls = []
        notify(Config(root="/tmp", notify_on=["done"]), "paused",
               runner=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_notify_calls_runner_for_enabled_event(self):
        calls = []
        notify(Config(root="/tmp", notify_on=["done"]), "done", slug="x",
               runner=lambda *a, **k: calls.append(a))
        self.assertEqual(len(calls), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_notify -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobuild.notify'`

- [ ] **Step 3: Write minimal implementation**

```python
# autobuild/notify.py
from __future__ import annotations
import subprocess
from pathlib import Path
from autobuild.config import Config


def format_message(event: str, **kw) -> str:
    if event == "done":
        return (f"✅ autobuild: {kw.get('slug')} done — tests {kw.get('tests')}, "
                f"${kw.get('cost', 0):.2f}, {kw.get('mins', 0)}m\n{kw.get('repo', '')}")
    if event == "needs-review":
        return (f"⚠️ autobuild: {kw.get('slug')} needs review — "
                f"{kw.get('reason', 'verification failed')}\n{kw.get('repo', '')}")
    if event == "paused":
        return f"⏸ autobuild paused — {kw.get('reason', '')}"
    if event == "crash":
        return f"💥 autobuild crashed — {kw.get('error', '')}"
    return f"autobuild: {event}"


def notify(cfg: Config, event: str, runner=subprocess.run, **kw) -> None:
    if event not in cfg.notify_on:
        return
    script = Path(cfg.telegram_script).expanduser()
    msg = format_message(event, **kw)
    try:
        runner([str(script), msg], capture_output=True, text=True, timeout=30)
    except Exception:
        pass  # notification must never crash the daemon
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_notify -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add autobuild/notify.py tests/test_notify.py
git commit -m "feat: telegram notifier with per-event gating"
```

---

### Task 8: Daemon loop (`run_once` + `main`)

**Files:**
- Create: `autobuild/daemon.py`, `autobuild/__main__.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `run_once(cfg, now, state, state_dir, runner=subprocess.run, verifier=verify_repo, builder=run_build) -> dict`;
  `main(argv: list[str]) -> int`. `run_once` returns `{"action": "pause"|"idle"|"built", "pace": Pace, "results": [...]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon.py
import tempfile, unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from autobuild.config import Config
from autobuild.ledger import GovernorState, GovernorState as GS, read_ledger
from autobuild.governor import Pace
from autobuild.build import BuildResult
from autobuild.backlog import scan_backlog
from autobuild.daemon import run_once

TZ = ZoneInfo("Europe/Rome")
BRIEF = '''+++
spec_version = "1.0"
slug = "alpha"
title = "alpha"
tier = "script"
priority = 5
status = "pending"
+++
## Intent
x
## Acceptance Criteria
A1. x
'''

def _root():
    d = Path(tempfile.mkdtemp())
    (d / "backlog").mkdir(); (d / "projects").mkdir(); (d / "state").mkdir()
    (d / "backlog" / "a.md").write_text(BRIEF)
    return d

class TestDaemon(unittest.TestCase):
    def test_daytime_returns_pause_and_builds_nothing(self):
        root = _root()
        res = run_once(Config(root=root), datetime(2026, 7, 23, 12, tzinfo=TZ),
                       GovernorState(), root / "state")
        self.assertEqual(res["action"], "pause")

    def test_night_builds_and_marks_done_on_green(self):
        root = _root()
        res = run_once(
            Config(root=root, max_concurrency=1),
            datetime(2026, 7, 23, 23, tzinfo=TZ), GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
            verifier=lambda *a, **k: True,
        )
        self.assertEqual(res["action"], "built")
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "done")
        self.assertEqual(read_ledger(root / "state" / "ledger.jsonl")[0]["status"], "done")

    def test_red_verify_marks_needs_review(self):
        root = _root()
        run_once(
            Config(root=root, max_concurrency=1),
            datetime(2026, 7, 23, 23, tzinfo=TZ), GovernorState(), root / "state",
            builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5),
            verifier=lambda *a, **k: False,
        )
        item = [i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha"][0]
        self.assertEqual(item.meta["status"], "needs-review")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_daemon -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobuild.daemon'`

- [ ] **Step 3: Write minimal implementation**

```python
# autobuild/daemon.py
from __future__ import annotations
import argparse
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from autobuild.config import Config, load_config
from autobuild.ledger import (GovernorState, load_governor_state, save_governor_state,
                              append_ledger)
from autobuild.governor import (Pace, compute_pace, record_spend, update_ceiling_ema,
                                next_active_time)
from autobuild.backlog import scan_backlog, select_pending, set_status, add_lock, clear_lock, read_lock
from autobuild.build import run_build, verify_repo, BuildResult
from autobuild.notify import notify


def run_once(cfg: Config, now: datetime, state: GovernorState, state_dir: Path,
             runner=subprocess.run, verifier=verify_repo, builder=run_build) -> dict:
    state_dir = Path(state_dir)
    lock_path = state_dir / "current.lock"
    ledger_path = state_dir / "ledger.jsonl"
    gov_path = state_dir / "governor.json"

    pace = compute_pace(now, cfg, state)
    if pace.level == "pause":
        return {"action": "pause", "pace": pace}

    # Crash recovery first: resume any in-flight slugs before taking new work.
    inflight = {e["slug"] for e in read_lock(lock_path)}
    all_items = scan_backlog(cfg.root / "backlog")
    if inflight:
        items = [i for i in all_items if i.meta.get("slug") in inflight]
    else:
        items = select_pending(all_items, pace.concurrency)
    if not items:
        return {"action": "idle", "pace": pace}

    def _one(it):
        slug = it.meta["slug"]
        repo = cfg.root / "projects" / slug
        set_status(it, "building")
        add_lock(lock_path, {"slug": slug, "repo": str(repo),
                             "started_at": now.isoformat(), "model": pace.model})
        res = builder(it.path, repo, pace.model, pace, cfg, runner=runner)
        return it, repo, res

    results = []
    with ThreadPoolExecutor(max_workers=max(1, pace.concurrency)) as ex:
        for fut in as_completed([ex.submit(_one, it) for it in items]):
            results.append(fut.result())

    for it, repo, res in results:
        slug = it.meta["slug"]
        if res.rate_limited:
            update_ceiling_ema(state, state.window_spend_usd or res.cost_usd)
            set_status(it, "pending")
            notify(cfg, "paused", reason=f"rate limited on {slug}", runner=runner)
        else:
            record_spend(state, res.cost_usd, now)
            green = (not res.is_error) and verifier(repo, runner=runner)
            status = "done" if green else "needs-review"
            set_status(it, status)
            append_ledger(ledger_path, {"slug": slug, "status": status,
                                        "cost_usd": res.cost_usd, "at": now.isoformat()})
            notify(cfg, "done" if green else "needs-review", slug=slug, repo=str(repo),
                   tests="green" if green else "red", cost=res.cost_usd, runner=runner)
        clear_lock(lock_path, slug)

    save_governor_state(gov_path, state)
    return {"action": "built", "pace": pace, "results": results}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="autobuild")
    ap.add_argument("--config", default="config/runner.toml")
    ap.add_argument("--once", action="store_true", help="run one tick and exit")
    ap.add_argument("--poll", type=int, default=300, help="seconds between ticks")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    tz = ZoneInfo(cfg.timezone)
    state_dir = cfg.root / "state"

    while True:
        state = load_governor_state(state_dir / "governor.json")
        now = datetime.now(tz)
        try:
            res = run_once(cfg, now, state, state_dir)
        except Exception as e:  # never die silently
            notify(cfg, "crash", error=str(e))
            if args.once:
                return 1
            time.sleep(args.poll)
            continue
        if args.once:
            return 0
        if res["action"] == "pause":
            sleep_until = next_active_time(now, cfg)
            time.sleep(max(args.poll, min(3600, (sleep_until - now).total_seconds())))
        else:
            time.sleep(args.poll)
```

```python
# autobuild/__main__.py
import sys
from autobuild.daemon import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_daemon -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS (all tasks 1–8)

- [ ] **Step 6: Commit**

```bash
git add autobuild/daemon.py autobuild/__main__.py tests/test_daemon.py
git commit -m "feat: daemon loop — run_once tick, concurrency, crash recovery, main"
```

---

### Task 9: Build-process assets (`CLAUDE.md`, prompt, brief template, example)

These are the instructions every headless run follows. Test = they must round-trip through the validators from Tasks 2 & 5.

**Files:**
- Create: `CLAUDE.md`, `prompts/build.md`, `templates/brief.template.md`, `backlog/0001-example.md`
- Test: `tests/test_assets.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_assets.py
import unittest
from pathlib import Path
from autobuild.spec import parse_spec, validate_spec

ROOT = Path(__file__).resolve().parent.parent

class TestAssets(unittest.TestCase):
    def test_claude_md_names_the_lifecycle(self):
        text = (ROOT / "CLAUDE.md").read_text()
        for marker in ["tokensave init", "tokensave install", "docs/spec.md",
                       "test-driven-development", "verification-before-completion",
                       "PROGRESS.md", "needs-review"]:
            self.assertIn(marker, text, f"CLAUDE.md missing: {marker}")

    def test_example_backlog_is_a_valid_brief(self):
        doc = parse_spec((ROOT / "backlog" / "0001-example.md").read_text())
        self.assertEqual(validate_spec(doc, level="brief"), [])

    def test_brief_template_is_valid_brief(self):
        doc = parse_spec((ROOT / "templates" / "brief.template.md").read_text())
        self.assertEqual(validate_spec(doc, level="brief"), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_assets -v`
Expected: FAIL — `FileNotFoundError: .../CLAUDE.md`

- [ ] **Step 3: Write `CLAUDE.md`**

```markdown
# Autobuild — build process for one backlog item

You are running headless and unattended. There is NO human to ask. Produce a
complete, tested TypeScript/Node project from the given backlog brief, or leave a
clearly-flagged `needs-review` branch. Never push to any remote.

## Environment signal
`$PACE` is `high` or `low`. When `high`, you MAY spawn subagents (Agent tool) to
build independent modules in parallel. When `low`, work single-threaded.

## Exploration rule (mandatory)
Use `tokensave_context` as the ONLY code-exploration tool. Never grep/Read files
for code research. After scaffolding, keep the graph current with `tokensave sync`.

## Lifecycle (do these in order)
1. **Expand the brief → full spec.** Read the brief. Write `docs/spec.md` in the
   canonical format (front-matter + sections: Intent, Ubiquitous Language, Domain
   Model, Requirements, Interfaces, Acceptance Criteria, Non-Goals, Constraints).
   No `TBD`/placeholder sections. This is the contract for everything below.
2. **Scaffold** the repo at the target dir: `npm init -y`, add `typescript`,
   `tsx`/`vitest` (or `node --test`), `tsconfig.json` with `strict: true`,
   `git init`, initial commit. Then:
   ```
   tokensave init
   tokensave install
   ```
3. **Architect by tier** (from `docs/spec.md` front-matter `tier`):
   - `script`: ubiquitous language + one PURE core module; thin CLI/FS adapters.
   - `library`: value objects + entities + a clean public API (the port); domain
     logic pure and isolated from adapters.
   - `service`: full tactical DDD — aggregates, repository interfaces, domain
     services, layered/hexagonal (domain / application / infrastructure), optional
     domain events.
   TDD applies to every tier.
4. **TDD implement** (use the test-driven-development skill): for each Acceptance
   Criterion `A1..An`, write a failing test → minimal code → pass → commit. Run
   `tokensave sync` after each batch.
5. **Verify** (use the verification-before-completion skill): `npm test` and
   `npx tsc --noEmit` must BOTH pass. Do not claim success without green output.
6. **Progress + finish.** Keep `PROGRESS.md` updated (what's done, what's next) so
   a killed run resumes cleanly. If a `$PACE`/rate-limit interruption stopped you
   mid-build, on the next run continue from `PROGRESS.md`.
   - All green → final commit on the default branch.
   - Not green after reasonable attempts → commit WIP on a `needs-review` branch
     and stop.
7. Your final message is the JSON `--output-format` result (the controller reads
   `total_cost_usd`, `usage`, and error state). Do not print anything after it.
```

- [ ] **Step 4: Write `prompts/build.md`, `templates/brief.template.md`, `backlog/0001-example.md`**

```markdown
<!-- prompts/build.md -->
Build backlog item: {BRIEF_PATH}. Target repo dir: {REPO_ROOT}.
Follow the process in {ROOT}/CLAUDE.md exactly. $PACE={PACE}.
```

```markdown
<!-- templates/brief.template.md -->
+++
spec_version = "1.0"
slug     = "REPLACE-kebab-slug"
title    = "REPLACE Title"
tier     = "script"
priority = 1
status   = "pending"
+++
## Intent
Describe the problem and why it matters, in one paragraph.

## Acceptance Criteria
A1. A concrete, executable check that proves it works.
```

```markdown
<!-- backlog/0001-example.md -->
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_assets -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md prompts/build.md templates/brief.template.md backlog/0001-example.md tests/test_assets.py
git commit -m "feat: build-process assets — CLAUDE.md lifecycle, templates, example brief"
```

---

### Task 10: Deployment (config file, systemd unit, README, smoke test)

**Files:**
- Create: `config/runner.toml`, `deploy/autobuild.service`, `deploy/install.sh`, `README.md`
- Test: manual verification steps below (no unit test — this is ops wiring).

- [ ] **Step 1: Write `config/runner.toml`**

```toml
[hours]
timezone   = "Europe/Rome"
quiet_from = "08:00"
quiet_to   = "19:00"

[pace]
max_concurrency = 3
per_project_timeout_min = 30
opus_escalation = true
weekly_reserve_enabled = false
weekly_reserve_frac = 0.12
weekly_target_usd = 0.0

[build]
stack = "typescript"
default_model = "sonnet"

[notify]
telegram_script = "~/.claude/notify-telegram.sh"
notify_on = ["done", "needs-review", "paused", "crash"]
```

- [ ] **Step 2: Write the systemd user unit**

```ini
# deploy/autobuild.service  ->  ~/.config/systemd/user/autobuild.service
[Unit]
Description=Autobuild runner (autonomous overnight builder)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/autobuild
Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 -m autobuild --poll 300
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

- [ ] **Step 3: Write `deploy/install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/.config/systemd/user"
cp "$HOME/autobuild/deploy/autobuild.service" "$HOME/.config/systemd/user/autobuild.service"
loginctl enable-linger "$USER"          # run the user service without an active login
systemctl --user daemon-reload
systemctl --user enable --now autobuild.service
systemctl --user status autobuild.service --no-pager
```

- [ ] **Step 4: Verify the unit file parses**

Run: `systemd-analyze --user verify deploy/autobuild.service`
Expected: no output (valid). (Warnings about `%h` paths not existing are acceptable.)

- [ ] **Step 5: Smoke-test one tick without touching Claude**

Run (daytime → must pause, proving the governor + wiring work end-to-end):
```bash
cd ~/autobuild && python -m autobuild --once --config config/runner.toml; echo "exit=$?"
```
Expected: `exit=0`. During quiet hours it does nothing; outside them with the
example backlog present it will attempt a real build — only run outside quiet
hours once you're ready for a live build.

- [ ] **Step 6: Write `README.md`**

```markdown
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
```

- [ ] **Step 7: Commit**

```bash
git add config/runner.toml deploy/ README.md
git commit -m "feat: deployment — runner.toml, systemd user unit, install script, README"
```

---

## Self-Review

**Spec coverage** (spec §→task):
- §5 spec model → Task 2 (schema, parser, validator, template) ✓
- §6 controller loop (governor gate, crash recovery, select, spawn, independent verify, notify, 429, ledger) → Tasks 4, 5, 6, 8 ✓
- §7 headless build run (expand, scaffold, tokensave init/install/sync, TDD, verify, PROGRESS, JSON result, subagents) → Task 9 `CLAUDE.md` + Task 6 `build_argv`/PACE env ✓
- §8 governor (learned ceiling EMA, headroom, 3 speed-up levers, pace bands, calibration, weekly reserve) → Task 4 ✓
- §9 TDD universal + DDD tiered → Task 9 `CLAUDE.md` step 3 ✓
- §10 observability + safety → Task 7 (telegram) + Task 6 (bypassPermissions/add-dir) + Task 3 (ledger) ✓
- §11 config → Tasks 1 + 10 ✓
- §4 directory layout → skeleton exists + created across all tasks ✓

**Placeholder scan:** No `TBD`/"add error handling"/"similar to Task N" in code steps; every code step has complete code. The `REPLACE-*` tokens live only inside `templates/` (that is their purpose) and the asset test asserts the *example* brief validates. ✓

**Type consistency:** `Config`, `SpecDoc`, `GovernorState`, `Pace`, `Item`, `BuildResult` field names and signatures are used identically in Tasks 4–8 as defined in Tasks 1–6. `run_build(brief_path, repo_root, model, pace, cfg, runner=...)` signature matches the `builder=` injection point in `run_once` and the daemon test's lambda. `verify_repo(repo_root, runner=...)` matches the `verifier=` injection. ✓

**One flagged real-world unknown:** the exact 429 payload shape from `claude -p` (Task 6 note) — must be confirmed against a live rate-limit and the regex adjusted, with the real payload captured as a test fixture.

---

## Notes carried from the spec (§13 open items → resolved here)

- `governor.json` schema → `GovernorState` (Task 3); EMA formula → `update_ceiling_ema` (Task 4).
- `spec.schema.json` + stdlib validator → Task 2.
- `CLAUDE.md` per-tier wording → Task 9 step 3.
- systemd unit + `enable-linger` → Task 10.
- 429 detection/parsing → Task 6 (`parse_result`, `_RESET`), flagged for live confirmation.
