# CLAUDE.md

## Repo

`omnivia-core`

## Claude role

You are the implementation agent for scoped tasks in this repo.

Codex is the PM/orchestrator and owns planning, review, verification, commits and pushes.

## Foundational principle

Prefer the simplest viable approach.

Do not add abstractions, frameworks, extra concepts or extra files unless required by the task or existing architecture.

## Primary rule

Work only inside the target repo/worktree for the task unless Codex explicitly authorises a multi-repo task.

## Naming rule

Use short names by default:

- Apps
- Dev
- Pro
- Core
- Platform
- Cloud

Use **Module** for installable product sets such as Apps, Dev and Pro.

Use **Component** for reusable parts inside Apps.

Do not use deprecated terms unless quoting old docs.

## This repo owns

- graph primitives
- memory/context primitives
- public contracts
- manifest schemas
- provenance primitives
- repo-local reference implementations that currently live here for memory,
  persistence, ingestion, search, and graph assembly

## This repo does not own

- desktop shell
- licensing
- Harness implementation
- paid Modules

The runtime-oriented implementations that ship here are transitional. Treat them
as local code in this repo, not as a claim that Core is the final long-term
owner of those boundaries.

## Before coding

Read the task packet from Codex and confirm:

- target repo
- allowed files/folders
- files/folders not to modify
- acceptance criteria
- verification commands
- relevant contracts

## Pull request and CI discipline

Canonical text lives in `AGENTS.md`. In short:

- Never push directly to `main`. Work on a branch or a worktree.
- Run `./scripts/preflight` and get a clean pass before opening the pull request.
- Open the pull request when the change is ready; merge only when the latest
  validation on it is green.
- A ruleset requires `Core acceptance` and the three `Phase 2 platform (<os>)`
  rows by exact name. Never rename those jobs or change that matrix.

## During coding

- Keep changes narrow.
- Use existing contracts and schemas.
- Do not invent cross-repo dependencies.
- Do not modify unrelated files.
- Add or update tests where requested.
- If another repo needs changes, report the dependency instead of making unauthorised changes.

## Return format

Return:

1. Summary of changes.
2. Files changed.
3. Tests run.
4. Results.
5. Known issues or follow-up work.
6. Architecture concerns.
7. Whether Codex should update PM docs or ADRs.
