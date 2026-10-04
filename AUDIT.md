# Audit — find where one existing repo can get better

You are running headless and unattended, in the `improve` lane, under a brief whose `allow`
list names **one report and nothing else**. This page replaces the "The deliverables" section
of `IMPROVE.md`. Everything else there still binds you: Environment, Exploration, Language,
Markers, "The verify commands run again", Self-check, Fidelity, Finish.

You are not fixing anything. You are producing the evidence a human needs to decide what the
next `improve` or `implement` brief for this repo should be. A finding without evidence is an
opinion; leave it out.

## The one deliverable

`docs/audit/<YYYY-MM-DD>-quality.md` (today's date), one autodoc block, stamped. Sections, in
this order and with these exact H2 headings — the daemon's `verify` greps for them:

```
## Baseline
## Drift since the last quality run
## Findings
## Proposed briefs
## Not examined
```

### `## Baseline` — does the repo still meet the standard, from clean?

Run every check the repo's CI runs, **from a clean toolchain** (`uv venv --clear`, a fresh
`--frozen-lockfile` install), exactly as CI would on a fresh runner. Record, as a table:
command, exit code, wall time, and the one line that matters (test count, lint count, the
first error). A check that is green on the owner's machine but red from clean is the most
valuable thing this section can find — say which dependency moved and to what version.

Also record, when the tool exists or can be fetched with `uvx` / `pnpm dlx`:
- dependency advisories: `pnpm audit --prod` / `uvx pip-audit -r requirements.txt` — group by
  package, with the lowest fixed version;
- outdated direct dependencies (`pnpm outdated`, `uv pip list --outdated`), majors separately;
- `actionlint .github/workflows/*.yml`.

If the network is down, say so and move on — do not guess versions.

### `## Drift since the last quality run`

Find the last `chore(quality)` commit (`git log --grep='chore(quality)' -1`). From there to
HEAD:
- source files added or substantially changed with **no test that exercises them**
  (`tokensave tool test_map`, `test_coverage`, `test_risk`);
- statements in `README.md`, `CLAUDE.md`, `docs/TROUBLESHOOTING.md` and
  `.claude/skills/*/SKILL.md` that are now false — a command, file, package manager, version
  or flag that no longer exists. Quote the line and the evidence;
- Known issues listed in `docs/TROUBLESHOOTING.md` that have since been fixed (name the
  commit) or that are still open;
- CI steps that no longer mirror what the repo actually uses.

### `## Findings`

Use `tokensave tool <name>` for the structural pass — `health`, `hotspots`, `complexity`,
`dead_code`, `unused_imports`, `god_class`, `circular`, `unsafe_patterns`, `todos`,
`doc_coverage` — then read the code the numbers point at. A metric is a reason to look, never
a finding by itself.

Weigh, in this order: correctness of the code that decides something irreversible (deletes,
overwrites, publishes, pays); tests that pass without proving anything (asserting a mock was
called, depending on the machine, order or clock); supply chain and CI health (unpinned
ranges that silently change, deprecated runtimes, workflow permissions); then maintainability.

One H3 per finding:

```
### F<n> — <one-line claim>
- **Severity:** high | medium | low — and why, in one clause
- **Evidence:** file:line, commit sha, or the command and the output line
- **Change:** what to do, in one or two sentences
- **Touches:** the path globs a brief fixing it would need in `allow`
- **Proves it:** the command that would go from red to green
- **Effort:** S (<1h agent) | M | L
```

Five sharp findings beat twenty soft ones. Order by severity, then by effort ascending.

### `## Proposed briefs`

Group the findings into at most three follow-up briefs, each small enough for one night:
slug, lane (`improve` for tests/CI/docs, `implement` only when there is a written plan),
the findings it closes, `allow`, `require`, `verify`. These are proposals for the owner — do
not create files in `backlog/`.

### `## Not examined`

What you could not check and why (no network, needs a secret, needs hardware). An honest gap
beats a silent one.

## Rules specific to this lane

- The brief's `allow` is only `docs/audit/**`. Do not "just fix" the one-line bug you found —
  the run fails, and the finding is worth more written down.
- Never start a line with `TODO`, `TBD`, `N/A`, `XXX` or `...` — not even in a table cell.
  The daemon's placeholder check reads those as an unfinished document and fails the run.
  Write "none", "not applicable", or say why.
- No secret, token, channel id, private host, personal datum, or `.env` value in the report —
  the report is committed to the repo.
