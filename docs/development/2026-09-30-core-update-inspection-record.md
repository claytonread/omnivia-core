# Core update inspection record (spec v0.4 §16.1, Core half)

- Date: 2026-09-30
- Actor: Codex-planned; Claude-inspected on main `531b8074` + this branch
- Scope: the §16.1 inspection evidence for the **Core** update path only. The
  Platform and Crok Pot halves require their own repository inspections
  (PR-U4's first step) and are explicitly not covered here.

## Installation

| Evidence | Finding |
|---|---|
| Canonical target | The installation is the Python environment the five first-party wheels are installed into (`omnivia-core`, `-runtime`, `-client`, `-cli`, `-mcp`); the standard candidate installs them offline from its wheelhouse (`scripts/build-standard-candidate.py`). |
| Supported layouts | (1) wheel install (the standard candidate's offline `pip install --no-index --find-links`), (2) source/editable checkout (developer-owned; v0.4 §11.3 fallback), (3) externally managed environments (defer to their owner). |
| Installer/update mechanism | The installation environment's own `pip` from **staged, verified wheels** — no new installation technology, per §9.1's reuse rule. |
| Current package assets | `build-standard-candidate.py` emits wheels + wheelhouse + `checksums-sha256.txt` + SBOM + licence material + journey/lifecycle evidence. |

## Version and metadata

| Evidence | Finding |
|---|---|
| Installed identity | `importlib.metadata` for the five first-party distributions; `direct_url.json` distinguishes wheel installs from editable/source checkouts (`editable: true` or a `file://` URL). |
| Running identity | Same process serves CLI and MCP; installed and running versions coincide for the standard install. Kept distinct in the result document where both are known. |
| Version comparator | Core's established dotted-integer ordering (the same rule the contract-compatibility windows and runtime `semver` gates use). Full PEP 440 pre/post segments are deliberately out of scope until a channel needs them — recorded as a conscious scope decision, switchable to `packaging.version` later. |
| Feed format | `omnivia-core-update.v1` (v0.4 §4.2): one mutable recommendation (`release` object or `null`) + the five-package version map. No existing consumers (this specification introduces the first); the 29 September design brief's append-only shape is superseded. |

## Lifecycle

| Evidence | Finding |
|---|---|
| Safe-stop owner | The managed service lifecycle (`omnivia service start/stop/status`) plus the workspace lease/fencing machinery. The update coordinator must account for **all workspaces served by the installation**, not one. |
| Restart inhibition | The bootstrap/managed-start path re-launches the service; the update coordinator must prevent a race between stop and automatic restart (U2 concern, A07). |
| Readiness | The service probe (`service readiness`) is the post-restart verification surface. |
| Processes | One Core service process per installation serves all workspaces; MCP servers are client-side processes and do not need stopping for a Core update (they reconnect). |

## Trust and recovery

| Evidence | Finding |
|---|---|
| Checksum enforcement | The standard candidate ships `checksums-sha256.txt` covering every wheel; the update path verifies staged wheels against it before mutation (D-1's HTTPS-plus-checksums trust label — §8.3 keeps it explicitly limited). |
| Signing | `sign-runtime-payload.py` + `trusted_runtime` (Ed25519, windows) exist for runtime payloads; model-manifest verification (#141) is merged. Neither signs `channel.json` yet — D-2/D-4 outstanding, and §8.3 forbids treating D-1 as their completion. |
| Recovery | Managed crash recovery is journey-proven (`evidence/standalone-journey-result.json` in the standard candidate); interruption during mutation follows the installer/product recovery procedure (§12). |

## CLI surface impact

`service update-check` joins the lifecycle administrative family (start/stop/
status → four commands). The surface bijection test renames accordingly; the
lifecycle adapter document gains the update-check frame. The command requires
no service connection and no workspace: it is installation-scoped discovery.

## Feed location decision

`channel.json` is hosted in a dedicated **`omnivia-core-updates`** repository
on GitHub, served over raw HTTPS (the Ora two-repo separation: channel
updates never touch release assets). The first real channel entry is
published with PR-U3's workflow after PR-U2's update path is qualified.
