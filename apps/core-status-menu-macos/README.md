# OmniVia Core Status Menu for macOS

This Swift Package is the native macOS companion for the headless OmniVia Core
Service. It remains a thin client for lifecycle controls, but it now also owns
the native document-intake boundary: reviewed file/folder selection,
security-scoped grants, durable local capture publication, monitoring,
review-first App Intents, and the Finder Action/Share extension handoff. Core's
workspace, lease and service lifecycle remain outside this process. Stopping
Core must not remove the control that can start Core again.

The source target is macOS 13 or later. That baseline provides the AppKit and SF
Symbols behavior used for a native dark-mode/Retina menu-bar icon while keeping
the package dependency-free.

## Build and test

From the Core repository root:

```bash
swift test --package-path apps/core-status-menu-macos
swift build --package-path apps/core-status-menu-macos
```

Build the release companion and both extension executables. The extensions must
be linked with `_NSExtensionMain` and extension-safe API checking:

```bash
xcrun swiftc -O -target arm64-apple-macos13.0 -application-extension \
  -parse-as-library -emit-executable \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/NativeHandoffMailbox.swift \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/NativePeerRequirement.swift \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/NativePeerSocket.swift \
  apps/core-status-menu-macos/Extensions/Shared/ExtensionSupport.swift \
  apps/core-status-menu-macos/Extensions/FinderAction/FinderActionViewController.swift \
  -o /absolute/path/to/OmniViaFinderAction -framework AppKit -framework Security \
  -Xlinker -e -Xlinker _NSExtensionMain

xcrun swiftc -O -target arm64-apple-macos13.0 -application-extension \
  -parse-as-library -emit-executable \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/NativeHandoffMailbox.swift \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/NativePeerRequirement.swift \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/NativePeerSocket.swift \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/OwnedSnapshotStore.swift \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/ShareRepresentationPolicy.swift \
  apps/core-status-menu-macos/Sources/DocumentIntakeMacOS/ShareInboxStore.swift \
  apps/core-status-menu-macos/Extensions/Shared/ExtensionSupport.swift \
  apps/core-status-menu-macos/Extensions/Share/ShareViewController.swift \
  -o /absolute/path/to/OmniViaShare -framework AppKit -framework Security \
  -Xlinker -e -Xlinker _NSExtensionMain
```

The extension sources are not Swift Package targets, so `swift build` does not
cover them. Replace `-emit-executable -o …` with `-typecheck` to check them
without linking; both extensions must typecheck under `-application-extension`.

Package a deterministic qualification bundle from those three executables:

```bash
python scripts/build-core-companion-app.py \
  --executable /absolute/path/to/omnivia-core-status-menu \
  --finder-extension-executable /absolute/path/to/OmniViaFinderAction \
  --share-extension-executable /absolute/path/to/OmniViaShare \
  --output /absolute/path/to/new-candidate-directory \
  --version 0.6.5
```

The result embeds `Add to OmniVia Core.appex` and `Share to OmniVia Core.appex`
under `Contents/PlugIns`, and `companion-manifest.json` beside the app binds all
three as one component set: bundle identifiers, marketing and build version,
minimum macOS, executable digests, each component's single service or extension
registration, the component-set compatibility identifier and the update-owner
declaration.

## Packaging the document runtime

Section 11.2 packages the RTF reader and the OCR engine with the distribution.
Add the trusted engine inputs — all of them or none — and the same build stages
them into `Contents/Resources/omnivia-runtime` and declares every byte in the
same manifest:

```bash
python scripts/build-core-companion-app.py \
  --executable /absolute/path/to/omnivia-core-status-menu \
  --finder-extension-executable /absolute/path/to/OmniViaFinderAction \
  --share-extension-executable /absolute/path/to/OmniViaShare \
  --pandoc /absolute/path/to/pandoc \
  --tesseract /absolute/path/to/tesseract \
  --tessdata-eng /absolute/path/to/eng.traineddata \
  --tessdata-osd /absolute/path/to/osd.traineddata \
  --license /absolute/path/to/pandoc-COPYING.md \
  --license /absolute/path/to/tesseract-LICENSE \
  --output /absolute/path/to/new-candidate-directory \
  --version 0.6.5
```

Every input is an absolute regular file the packager is handed; nothing is
discovered on `PATH`, in a package-manager prefix or over the network, and a
symlink is refused because the bytes the manifest digests have to be the bytes
the loader opens. The build then stages the recursive closure of each engine's
non-system Mach-O dependencies into `omnivia-runtime/lib`, rewrites every load
command to `@loader_path`-relative form, deletes every `LC_RPATH` so nothing is
resolved by search path at all, and re-seals each rewritten file ad hoc —
without which a relocated Mach-O will not execute on Apple silicon. Apple's own
libraries and frameworks stay external. Two different builds of one library name
are a refusal, not a last-write-wins. The digests go into the manifest *after*
all of that, so what is pinned is what runs.

The layout, the manifest section and the verification of both live in
`omnivia_core_runtime.distribution.packaged_runtime`, which the packaging script
and the service share: production resolves the payload from
`<installation-state>/companion`, re-digests every declared byte, and hands the
adapters absolute paths. An absent, incomplete or altered payload resolves to
nothing, and then no engine is invoked and no model hash is claimed.

The ad-hoc seal is not a release signature and is never reported as one: the
candidate stays `qualification_only`, `signature_status` stays `unsigned`, and
`release_eligible` stays false. A signing owner re-signs these same files later.

Qualify a real payload on a real host, which is the DOC-13 lane:

```bash
python scripts/check-doc13-packaged-runtime.py produce \
  --installation-state /absolute/path/to/installation-state \
  --output /absolute/path/to/evidence \
  --version 0.6.5 \
  --companion-executable … --finder-extension-executable … \
  --share-extension-executable … \
  --pandoc … --tesseract … --tessdata-eng … --tessdata-osd … --license …
```

It assembles the candidate, verifies its bytes, checks that every staged Mach-O
resolves inside the payload, starts both engines from an environment holding
nothing but the operating system's own `PATH`, recognises a generated English
image and reads an RTF through the confined worker, and finally requires a
tampered packaged byte to be refused. `verify --record FILE` re-reads a produced
record offline and refuses it unless every gate passed. There is no second updater, and the manifest names no owner for the
app's update, because no repository authority binds one: it records
`app_update_owner: externally_unbound`, that the existing update channel does
not cover this component set, and that the installed-product installer is owned
outside this repository (WP-00 binding ledger row 13).

Verify an assembled candidate against that manifest before it goes anywhere:

```bash
python scripts/verify-core-companion-bundle.py /absolute/path/to/candidate
```

It is fail-closed: a missing or extra managed extension, a duplicate bundle
identifier or service registration, any version, build-version or minimum-macOS
skew between the companion and either extension, a tampered executable, a
symlink, a declared path outside the set, a top-level identity, digest or status
field that disagrees with the component set, a manifest field this schema does
not bind, or an unknown manifest schema all exit non-zero. An unknown schema is
refused whole and left exactly as written.

That is the *coherence* verdict, and it is the useful one for an unsigned
qualification build: a clean unsigned candidate exits `0` with `coherence_ok`
true and `release_eligible` false.

Signing and notarisation are never read from a file, and the expected identity
is never taken from the caller. There is no flag for a Team ID or a designated
requirement: an identity the invoking caller chooses verifies nothing, so the
only identity the gate checks against is `REPOSITORY_SIGNING_AUTHORITY` in
`scripts/build-core-companion-app.py` — reviewed configuration owned by this
repository, with the provenance of the binding recorded beside it.

That constant is `None` today, because no designated signing identity exists in
this repository (WP-00 binding ledger row 13). So **signing verification is
unavailable**: the platform authorities are not run at all, the gate reports
`signature_status: unavailable`, and no candidate is release eligible. Binding
it is the same reviewed change, citing the same repository authority, that binds
`UPDATE_OWNER`; while the app's update owner stays `externally_unbound`, signing
verification stays unavailable even if the constant were populated on its own.

Once both are bound by that authority, the gate runs the platform authorities
over the exact candidate: per component `codesign --verify --deep --strict`,
`codesign --verify -R=<requirement>` and `codesign --display --verbose=4` (whose
`TeamIdentifier` must equal the expected one), then `spctl --assess` and `xcrun
stapler validate` on the containing app. Only all of those succeeding reports
`verified`. Off macOS, or with any of those tools missing, each one fails and the
candidate stays `unverified`.

Release eligibility is therefore unreachable here today, by design:

```bash
python scripts/verify-core-companion-bundle.py /absolute/path/to/candidate \
  --require-release-eligible
```

`--require-release-eligible` turns each remaining blocker — the unavailable
signing verification and the unbound update owner — into a finding and a
non-zero exit. That is the same gate release qualification runs.

The bundle remains explicitly `qualification_only` until each
extension and the containing app are signed with the entitlements under
`Packaging/`, then notarized by the production release process. Ad-hoc signing
can launch the app for local UI checks, but it is not evidence that macOS has
registered App Group-backed extensions. The app does not embed the Core runtime
or CLI; it addresses the explicit shared installation and Workspace supplied at
launch.

Run the development executable with an explicit Core selection and the fixed
`omnivia` executable lookup:

```bash
swift run --package-path apps/core-status-menu-macos omnivia-core-status-menu \
  --installation-state /absolute/path/to/installation-state \
  --workspace-id ws-example
```

For a reviewed development checkout, explicit absolute overrides are available:

```bash
swift run --package-path apps/core-status-menu-macos omnivia-core-status-menu \
  --cli /absolute/path/to/omnivia \
  --installation-state /absolute/path/to/installation-state \
  --workspace-id ws-example
```

The executable resolution order is:

1. explicit absolute `--cli` override;
2. `Contents/Resources/omnivia` in a packaged app;
3. `omnivia` beside the companion executable;
4. `/opt/homebrew/bin/omnivia` and `/usr/local/bin/omnivia`;
5. the fixed `omnivia` name on the operating system `PATH`.

The Core repository intentionally forbids OmniVia-specific environment variables
from redirecting state paths or executable names, so the companion does not read
`OMNIVIA_HOME`, `OMNIVIA_CLI`, or equivalents. The installation-state root,
workspace id, and optional `--cli` are explicit,
reviewable process arguments and are intended primarily for development and
packaging verification.

## Behavior

- Starts at **Checking…** and probes live Core readiness immediately.
- Polls every five seconds without overlapping lifecycle commands.
- Shows **Starting…** and **Stopping…** while bounded subprocess operations run.
- Keeps Refresh, Start Service, Stop Service, Show Service Log, and Quit Status
  Menu in stable menu positions.
- Reveals the selected installation's `run/service.log` without creating state.
- Quitting the status menu leaves the Core Service unchanged.

## Native intake routes

Every route opens a review draft. None of them queues anything, and none reads
a Finder selection or the general clipboard.

- **Finder Service** — *Add to OmniVia Core…* takes files and folders from the
  pasteboard the Service is handed and from nowhere else. It is advertised only
  once a review draft can be opened, so an unready companion shows no menu item.
- **Status-item drop** — a transparent child of the system status button, which
  keeps its own click, menu and accessibility. It returns *copy* only, decides
  on declared pasteboard types and the pasteboard's own item count alone (no
  bytes are read while a drag hovers), and admits each drag session once. After
  400 ms of hover it tints the icon and shows a compact affordance — *Drop to
  add to Core* and the declared item count — as a nonactivating floating panel
  that ignores mouse events and is clamped to the visible screen, so it takes
  no key, activates no application, and leaves the drag, click, menu and
  accessibility paths alone. It is ordered out on exit, end and drop. File URLs
  are reviewed where they are; a promised file, a dropped link and dropped text
  have no original to keep, so each becomes a captured copy this companion owns.

### Promised files

AppKit offers no way to reserve a promised file's bytes before a drag source
delivers them, so nothing about a promise is pre-admitted beyond the file count
it declares. The bytes are judged at the callback, not at the drop:

- Delivery goes into a fresh `0700` directory this process makes, and that
  directory is removed on every path out — delivered, refused or timed out.
- A source that goes quiet is bounded by
  `StatusItemDragPolicy.promisedDeliveryTimeout` (30 s). Expiry abandons the
  drag whole and ignores every late callback; nothing is retried, because a
  partly delivered promise is not the selection the person dragged.
- Each delivered file's facts are read with `lstat` before anything is staged:
  it must be a regular file lying directly in that directory, so a symlink is
  refused rather than followed, and its length must fit
  `OwnedSnapshotStore.maximumItemBytes` with the whole delivery fitting
  `maximumSelectionBytes`. One refusal refuses the drag whole.
- The copies then go to review like any other captured copy. Native aggregate
  qualification still applies to them downstream: this boundary bounds the
  companion's own staging, not the pipeline behind it.
- **Native pickers** — files and folders use the two fixed `NSOpenPanel`
  policies in `CaptureIntakePolicy.swift`, so neither picker can drift.
- **Finder Quick Action** — a viewer Action extension. It returns the same
  `NSExtensionItem` objects it was given, unmodified, and completes only after
  the companion acknowledges adopting every offered slot.
- **Share extension** — captures copies, durably, without needing the
  companion to be open. See *Shared content* below.
- **Shortcuts** — exactly two review-first App Intents. See *Shortcuts* below.

### Selected folders

The Quick Action's activation rule admits folders as well as files, so each
loaded selection URL is classified from the file system — not from its declared
types — and offered as its own closed source kind:

- A regular file is offered as `original_bookmark` and adopted as a read-only
  grant on that file (`original_file` candidate).
- A plain directory is offered as `folder_bookmark` and adopted as a
  `NativeAccessStore` root grant under this candidate's own owner reference
  (`folder_root` candidate). The candidate is the root itself: it is never
  represented as a direct file, and the handoff lists, reads and captures
  nothing inside it.
- A package, a symlink, or anything that is neither fails visibly, so the
  action shows its retry message rather than silently narrowing the selection.

One selection is an ordered mix of both, established slot by slot in the order
the person selected them and written adopted in one atomic write, so nothing is
acknowledged until every slot holds what its record names.

A folder's scope is reviewed separately, and only when the person asks for it:
no plan is made, no directory is walked and nothing is queued during the
handoff. A folder candidate's root grant is bound at the adopted commit point
together with its exact generation, and review resolves the folder only at that
generation — so a grant that was rolled back and re-adopted cannot be mistaken
for the one the candidate created.

Every way a folder row leaves review releases it: Remove, Discard, quit, a
failed handoff, orphan recovery, and a folder plan that was successfully
submitted. Releasing is owner scoped, so the grant itself survives while a
submitted bulk run or watched folder still owns it — that work adopts its own
root grant under its own owner, never this candidate's.

One selection is bounded by what one review draft holds
(`CaptureIntakePolicy.maximumSelectionItems`), and the Quick Action's bounded
activation rule carries the same bound. An item that is not a supported document
stays listed as refused until it is removed, and nothing is published while one
is listed.

### Shared content

Share is captured content by default, and only captured content. There is no
path by which a share becomes a durable original: `inPlace` is never requested,
a provider's file URL is copied rather than bookmarked, and nothing a share
delivers is ever monitored. Linking an original is a separate action — *Link an
original…* — which asks the companion to open its own reviewed chooser and says
so, rather than reporting a success it does not have.

`ShareRepresentationPolicy` chooses one representation per logical slot from a
fixed order — a file URL, then an image, then a web link, then plain text, then
any other data — and keeps, within one extension item, only the attachments of
that item's best kind. So a host that sends one logical share as alternative
representations (a link and its title text) becomes one slot, while a multi-file
share stays one slot per file in the order it was given. An item this build
cannot capture refuses the whole selection: a partly captured selection is not
what the person shared.

A shared link is captured as the text of the link. Only `http`/`https` links
with a host, at note length, are admitted, and nothing is ever requested from
one. That is structural rather than a promise: the Share extension is sandboxed
with no network entitlement at all (`Packaging/Share.entitlements`).

Per-item and per-selection bounds (`50 MiB`, `200 MiB`, 10 items) are enforced
while copying, and each provider's temporary file is checked with `lstat` first
— a regular file, never a symlink. Every byte is written owner-only into a
directory this extension owns. A provider that does not answer within 30
seconds is abandoned as one whole selection; a late callback cannot revive it.

### The durable Share spool

`ShareInboxStore` is a second, snapshot-only area of the App Group, separate
from the grant-bearing handoff mailbox. It exists so a share works while the
companion is closed, so it deliberately does **not** reuse or extend that
mailbox's 120-second transfer window, carries no bookmark, grant or access
reference, and claims no authentication. It names no destination, workspace or
caller either.

- A request is written into `staging/<id>/` and becomes visible only when that
  whole directory is renamed to `pending/<id>/` — one atomic step, taken after
  every byte and the manifest are closed and validated. A crash before it
  leaves nothing to adopt; startup reconciliation clears staging directories no
  live share can still be writing.
- Ids are opaque, the schema is closed and versioned, and a manifest carrying a
  schema or a field this build does not know is left **byte for byte** as it is
  so a newer companion can still adopt it. It still counts against the bounds.
- Total unsent native-owned content is bounded by count and by bytes. Nothing
  already durable is dropped to make room; a new share is refused instead.
  Staging writers reserve their worst-case size, and an in-process plus
  cross-process lock makes concurrent quota decisions exact.
- The extension may attempt one fixed companion activation. Whether the
  companion comes forward or not, the request stands, and the wording is
  *Queued on this Mac. Open OmniVia Core to send.* — never *Added*, and never
  naming a destination.
- Cancelling before the publish drops the bytes. Completion or host dismissal
  after it does not: the request is the person's now.

Payloads, manifest and publication directories are flushed before the atomic
rename is acknowledged. The companion adopts a request by copying every one of its bytes into its own
owned-copy storage, proving each copy against the manifest's length and digest,
handing the copies to the same review coordinator every other intake surface
uses, and only then dropping the request. That drop is the acknowledgement, so a
lost one re-copies the same bytes onto the same candidate key review already
holds rather than producing a second candidate, and a crash before it re-adopts
on the next start. Bytes that do not match what the manifest bound are never
shown and the request is dropped rather than retried forever. Nothing is queued,
delivered or watched by an adoption.

If review and its bounded waiting list are full, that is not an
acknowledgement: the temporary companion copy is rolled back and the durable
inbox request remains for a later reconciliation pass.

### Shortcuts

Exactly two App Intents, both review-first:

- **Open Add to Core** opens the capture panel and nothing else. It reads no
  file, no clipboard and no Finder selection.
- **Review Files in OmniVia Core** owns a copy of every supplied `IntentFile`
  representation before `perform` returns — a data representation and a file URL
  alike, with any security scope balanced — under the same item, selection and
  count bounds as every other captured copy, in the order supplied. It reads no
  path string, clipboard or Finder selection, never adds or watches anything,
  and reports *Ready to review*, never *Added*.

Both declare `openAppWhenRun`, which is what macOS 13 and 14 understand, and
`supportedModes = .foreground(.immediate)` where the SDK has it. Neither raises
the package's macOS 13 minimum.

A second run is a second selection: it enters the existing waiting-selection
behavior behind the one being reviewed rather than merging into it.

## Lifecycle adapter version 2

The companion uses an owner-only per-installation lock and Unix activation
socket. A second launch forwards the fixed `refresh` intent and exits; it cannot
send an arbitrary action or payload. Quitting the companion releases that lock
and removes the socket without issuing a Core lifecycle command.

The companion reads `"lifecycle_adapter_version": 2` and nothing else: version 1
published a raw `service` object and a free-form `reason`, and a version 1
document — like any other version — is refused rather than read.

A version 2 document carries `action`, `ok`, `outcome`, a required `code` from
the adapter's closed set, and an optional `safe_status`. `safe_status` is a
canonical `CoreSafeStatusV1` (ADR-038), mirrored here as closed Swift enums and
bounded scalars in `CoreSafeStatus.swift`. The document is accepted only when:

- the adapter version is exactly 2;
- every required field is present and every closed value — target kind and
  management, the four normalized states, warning codes, permitted actions, the
  lifecycle `code` — is one this build knows;
- `code`, `action`, `outcome` and `ok` form one of the adapter's exact valid
  tuples: a code that merely belongs to the right action is not enough, so a
  `status_not_running` reporting `failed`, or a `stop_stopped` reporting
  `ok: false`, is refused;
- the status and its target agree on `contract_version`, and it is exactly the
  Application Contract version this build mirrors (`1.3`) — another minor is
  refused as readily as another major;
- every object carries only the keys its canonical schema declares — the
  envelope, the safe status, and its nested target are all closed;
- every scalar matches its declared value domain and bounds (`Identifier` and
  `WorkspaceId` patterns, `ContractVersion`, `ReleaseVersion`, a 1–256 character
  `display_name`);
- `warning_codes` and `permitted_actions` are within their caps and repeat
  nothing;
- `start`/`stop`/`restart` appear only for a `local`, `locally_managed` target.

Anything else fails closed: the whole document is refused and the menu shows a
fixed *status unavailable* line with **no controls**. `safe_status` may also be
legitimately absent — when the adapter could form no valid target — which also
means no controls.

## The privacy boundary

The companion is the least-privileged consumer Core has: it has authenticated
nothing, so it is shown, and shows, only what a safe status may carry.

- Menu state and every rendered word come from the safe status alone. Start and
  Stop are enabled **only** by `permitted_actions`, never by the lifecycle
  phase. There is no Restart item; a status that offers `restart` still moves no
  control. A remote or externally-managed target can therefore never present a
  local process control.
- Nothing the adapter wrote is ever displayed. No stderr, no `reason`, no
  decoder complaint, no `localizedDescription`, no path, endpoint, pid, or
  credential. The adapter's stderr is drained only so a chatty command cannot
  deadlock on a full pipe, and is discarded unread.
- Every degraded, unknown or failure phrase is a fixed local string chosen from
  a closed enum — a `CoreSafeWarningCode`, a normalized state, or one of the
  companion's own closed runner failures. `display_name` is validated but never
  rendered; the menu's words are this build's own.

## Remaining distribution boundaries

- no service auto-start or auto-restart;
- no launch-at-login, LaunchAgent, daemon, or reboot persistence;
- no port scanning, process-name scanning, or pid-based controls;
- no telemetry or network calls are introduced by native intake;
- production signing, notarization, installer ownership, and Platform cutover
  remain external release qualifications.

The companion package now owns native intake implementation and qualification
packaging. Production signing identities and the installation/update owner are
still required to close WP-14 and the installed portions of WP-15.
