# AGENTS.md

## Repo

`omnivia-core`

## Role

Public core, graph, memory, contracts, schemas and public docs.

## Foundational principle

Prefer the simplest viable approach.

Choose fewer concepts, fewer moving parts, clearer ownership and easier verification unless complexity is clearly justified.

## Operating model

Codex is the PM, orchestrator, reviewer and integration controller.

Claude is the implementation agent.

For coding tasks:

**Claude builds. Codex manages.**

## Pull request and CI discipline

GitHub Free, one founder, several agents. Validation runs on pull requests, so a
change that never reaches a pull request is never validated.

- Never push directly to `main`.
- Work on a branch or a worktree.
- Run `./scripts/preflight` and get a clean pass before opening the pull request.
- Open the pull request when the change is ready, not to watch CI run.
- Merge only when the latest GitHub validation on the pull request is green.
  Green on an earlier commit of the same branch is not green.

This repository is public, and a ruleset on `main` requires four checks by exact
name: `Core acceptance`, and `Phase 2 platform (ubuntu-latest)`,
`(macos-latest)`, `(windows-latest)`. Renaming a job or changing that matrix
silently removes the rule that references it. Do not.

Every workflow keeps `workflow_dispatch`. Use it to re-validate without pushing
an empty commit.

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

## This repo does not own

- desktop shell
- licensing
- Harness implementation
- paid Modules

## Shared OmniVia vocabulary

- Module: installable product set such as Apps, Dev or Pro.
- Apps: Module for creating and running custom business Apps.
- Dev: Module for developer tools.
- Pro: Module for premium local features.
- App: custom hosted business application.
- Component: reusable part inside an App.
- Harness: controlled runtime boundary.
- Module Manifest: definition file for an installable Module.

## Deprecated terms

Avoid unless quoting old docs:

- Extension
- Capability
- Pack
- Add-on
- Layer
- Space
- Surface
- Graph App
- Brick
- Block
- Studio

## Repo boundary rules

- Do not make changes outside this repo unless Codex explicitly created a cross-repo integration task.
- Do not add paid Module implementation code to the base platform.
- Do not bypass Harness APIs.
- Do not access connector credentials directly.
- If the task requires another repo, stop and report the required cross-repo change.

## Required PM context

Read the operating model in:

`/Users/claytonread/Projects/omnivia-pm/docs/operating-model/codex-claude-multirepo-workflow.md`

Use the Claude implementation task template in:

`/Users/claytonread/Projects/omnivia-pm/prompts/claude-implementation-task-template.md`
