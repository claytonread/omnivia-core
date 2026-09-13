#!/usr/bin/env python3
"""Stage exactly one verified import descriptor, for real-host qualification only.

R004 section 8.3 puts staging outside the MCP milestone: `import_start` names a
handle "produced by an installed, trusted Core path" and accepts no archive,
path or URL it could produce one from. Section 13.D then asks a real host to
start an import from such a handle. Something has to write the handle, and it
may not be the host, the MCP surface, or anything a user is told to run.

**This is that something, and it is not a product surface.** It is a
qualification fixture with a script's shape: not packaged into any wheel, not
named by any installed console script, not documented as an ingestion path, and
useless for ingesting anything a caller chooses -- every value it writes is a
fixed constant below, and there is no input for content, path, URL, checksum or
media type. The one thing a caller supplies is which temporary workspace to
write into. `scripts/run-host-qualification.py` is its only caller.

**It reaches the runtime, and only the installed one.** The three-table shape a
`verified` staging must have -- a blob object, the integrity event that verified
it, and the staging row naming both -- is a storage invariant of migration
0008, not an API, so it is written the way the repository's own runtime tests
write it: `acquire_lease`, `open_guard`, one `fenced_transaction`. To keep the
qualification claim honest, `--installed-prefix` is required and every OmniVia
module this process imports must resolve beneath it; a source tree on
`sys.path` or a stray `PYTHONPATH` fails here rather than silently qualifying
the wrong code.

**It runs while nothing owns the workspace.** `OpenMode.SERVICE_OWNED` plus a
fresh lease is the service's own authority sequence, so this must run before
the Core service is started for the journey -- never beside it. The lease and
guard it takes are replaced by the next generation the real service acquires.

Output is the canonical `ImportSourceDescriptor` as JSON on stdout, which is
exactly what the qualification prompt hands the host and nothing more.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: Every value the staged descriptor carries. Fixed, synthetic and obviously
#: so: a repeated-nibble digest can be printed in evidence and read in a log
#: without being a fingerprint of anybody's content.
STAGED_SOURCE_REF = "stg-hostqual-1"
SOURCE_KIND = "archive"
CONTENT_CHECKSUM = "sha256:" + "e" * 64
CONTENT_LENGTH_BYTES = 1024
MEDIA_TYPE = "application/zip"

#: Fixed identity for the throwaway lease. Never a real service instance.
_INSTANCE = "svc-hostqual-stage-1"
_INSTALLATION = "inst-hostqual"
_METADATA_JSON = '{"kind":"archive"}'
_METADATA_DIGEST = "sha256:" + "c" * 64
_BASE_US = 1_700_000_000_000_000

_BLOBS = "omnivia_blob_objects"
_INTEGRITY = "omnivia_blob_integrity_events"
_STAGED = "omnivia_staged_sources"

#: The OmniVia distributions this process is allowed to have imported once it
#: has done its work. Checked by path, so an editable source tree fails.
_OMNIVIA_PREFIX = "omnivia_"


def descriptor() -> dict[str, Any]:
    """The canonical `ImportSourceDescriptor` this helper stages.

    `source_version` is deliberately absent: the staged row leaves that column
    NULL and `require_staged_import_source` matches it with `IS ?`, so a
    descriptor that carried one would not resolve.
    """
    return {
        "staged_source_ref": STAGED_SOURCE_REF,
        "source_kind": SOURCE_KIND,
        "content_checksum": CONTENT_CHECKSUM,
        "content_length_bytes": CONTENT_LENGTH_BYTES,
        "media_type": MEDIA_TYPE,
    }


def _assert_installed(prefix: Path) -> list[str]:
    """Prove every imported OmniVia module came from `prefix`.

    Returns the modules it checked so the caller can record that the check had
    something to check. An empty result is a failure, not a pass: it would mean
    the runtime was never imported and this function proved nothing.
    """
    checked = []
    for name, module in sorted(sys.modules.items()):
        if not name.startswith(_OMNIVIA_PREFIX):
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        resolved = Path(origin).resolve()
        if prefix not in resolved.parents:
            raise SystemExit(
                f"qualification staging imported {name} from outside the installed "
                "prefix; the installed wheel is not what ran"
            )
        checked.append(name)
    if not checked:
        raise SystemExit("qualification staging imported no OmniVia module")
    return checked


def stage(workspace: Path, installed_prefix: Path) -> dict[str, Any]:
    """Write the one staged source, and return the descriptor that names it."""
    from omnivia_core_runtime.ownership.fencing import fenced_transaction, open_guard
    from omnivia_core_runtime.ownership.identity import (
        ProcessEvidence,
        ServiceInstanceIdentity,
        SystemClock,
    )
    from omnivia_core_runtime.ownership.lease import acquire_lease
    from omnivia_core_runtime.storage.connection import OpenMode, open_database
    from omnivia_core_runtime.workspace.layout import WorkspaceLayout
    from omnivia_core_runtime.workspace.manifest_store import read_manifest

    # Prove the imports before taking a lease or mutating the fixture.  A source
    # leak must fail closed without leaving even the throwaway workspace changed.
    _assert_installed(installed_prefix)
    layout = WorkspaceLayout(root=workspace)
    workspace_id = read_manifest(layout).workspace_id
    connection = open_database(layout.database_path, OpenMode.SERVICE_OWNED)
    identity = ServiceInstanceIdentity(
        service_instance_id=_INSTANCE,
        installation_id=_INSTALLATION,
        process=ProcessEvidence(
            pid=1, start_time="0", boot_id="boot-hostqual", os_principal="hostqual"
        ),
    )
    try:
        lease = acquire_lease(
            connection,
            identity,
            clock=SystemClock(),
            workspace_id=workspace_id,
            holds_storage_lock=True,
            lock_mechanism="flock",
        )
        open_guard(
            connection,
            identity,
            clock=SystemClock(),
            workspace_id=workspace_id,
            fencing_generation=lease.fencing_generation,
        )
        with fenced_transaction(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=lease.fencing_generation,
        ):
            connection.execute(
                f"INSERT INTO {_BLOBS} (workspace_id, content_digest, "
                "content_length_bytes, created_at_us, verified_at_us) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    CONTENT_CHECKSUM,
                    CONTENT_LENGTH_BYTES,
                    _BASE_US,
                    _BASE_US + 1,
                ),
            )
            connection.execute(
                f"INSERT INTO {_INTEGRITY} (integrity_event_id, workspace_id, "
                "content_digest, integrity_sequence, outcome, checked_at_us) "
                "VALUES (?, ?, ?, 1, 'verified', ?)",
                (
                    f"bie-{STAGED_SOURCE_REF}",
                    workspace_id,
                    CONTENT_CHECKSUM,
                    _BASE_US + 2,
                ),
            )
            connection.execute(
                f"INSERT INTO {_STAGED} (staged_source_ref, workspace_id, source_kind, "
                "declared_checksum, content_length_bytes, media_type, computed_checksum, "
                "original_metadata_json, original_metadata_digest, staging_outcome, "
                "blob_workspace_id, blob_content_digest, recorded_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?)",
                (
                    STAGED_SOURCE_REF,
                    workspace_id,
                    SOURCE_KIND,
                    CONTENT_CHECKSUM,
                    CONTENT_LENGTH_BYTES,
                    MEDIA_TYPE,
                    CONTENT_CHECKSUM,
                    _METADATA_JSON,
                    _METADATA_DIGEST,
                    workspace_id,
                    CONTENT_CHECKSUM,
                    _BASE_US + 3,
                ),
            )
    finally:
        connection.close()
    return descriptor()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        allow_abbrev=False, description=__doc__.splitlines()[0]
    )
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--installed-prefix", required=True, type=Path)
    arguments = parser.parse_args(argv)
    for name, value in (
        ("--workspace", arguments.workspace),
        ("--installed-prefix", arguments.installed_prefix),
    ):
        if not value.is_absolute():
            raise SystemExit(f"{name} must be an absolute path")
    staged = stage(arguments.workspace.resolve(), arguments.installed_prefix.resolve())
    sys.stdout.write(json.dumps(staged, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
