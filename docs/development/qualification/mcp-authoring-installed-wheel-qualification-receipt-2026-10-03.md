# MCP authoring installed-wheel qualification receipt

**Date:** 2026-10-03
**Result:** PASS
**Evidence type:** local installed-wheel qualification; not real-host evidence
**Source base:** `7406badb33e00c4d753331d767a3cac1c2bb52ef` plus the uncommitted Phase 8 implementation under review

## Command

```bash
python scripts/build-standard-candidate.py \
  --output <temporary-candidate-directory> \
  --allow-dirty
```

The builder created all five first-party wheels, resolved the reviewed
third-party closure, installed only from that wheelhouse into a new virtual
environment, ran the restricted journey, ran the authoring journey, validated
the authoring result against the closed schema, and ran the lifecycle journey.

## Reviewed dependency pins

- `mcp==2.0.0`
- `mcp-types==2.0.0`

## First-party wheel SHA-256

| Wheel | SHA-256 |
|---|---|
| `omnivia_core-0.1.0-py3-none-any.whl` | `7be609e0ace0616351801ccd27871c79880e8c2ca46041124fdb0db2a29ed309` |
| `omnivia_core_runtime-0.1.0-py3-none-any.whl` | `7d0eaa48d98d4a4f7a0e2e5982dca61c6b20dc2ed113ee1663a965abe556e8f9` |
| `omnivia_core_client-0.1.0-py3-none-any.whl` | `dadb793535d0a942ac688d849790380bee7bf079121b996cd5d257b771921b6c` |
| `omnivia_core_cli-0.1.0-py3-none-any.whl` | `61d9fa22462cfc378d4a9d1cc52d5451ac1f094f89f730d5455d78efb70a1e4a` |
| `omnivia_core_mcp-0.1.0-py3-none-any.whl` | `bdfb57fa6b1dba2d8dbe9d6d1a5526dabbf98a7251f4a1f177bb1114712aaf7d` |

These digests bind this local diagnostic candidate only. They must be replaced
by the exact clean-tip artifacts before release closeout.

## Retained record

`docs/development/qualification/mcp-authoring-installed-wheel-qualification-2026-10-03.json`

The record validates against
`docs/distribution/schemas/mcp-authoring-qualification-record-v1.schema.json`.
It contains no credential, grant, private path, private identifier, submitted
content, prompt, transcript, endpoint, process identifier, stdout/stderr or
model response.

## Scope and limitation

This evidence directly proves the installed-wheel authoring journey and the
reviewed SDK pins on macOS arm64. It does not prove that Claude Code or Codex CLI
executed the workflow. Real-host qualification remains a separate Phase 8 gate.
