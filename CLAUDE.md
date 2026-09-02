# Autobuild — build process for one backlog item

*This is the **build** lane's playbook (`mode = "build"`, the default): create a new project
from a brief. Items with `mode = "document"` follow `AUTODOC.md` instead — they document a
repo that already exists and may not change code at all.*

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
