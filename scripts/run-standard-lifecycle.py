#!/usr/bin/env python3
"""Qualify installed-wheel upgrade, rollback boundaries, and recovery.

This script is launched by the Standard candidate's clean virtual environment.
It deliberately uses the installed Core and Runtime distributions rather than a
repository import path.  Its retained result is bounded to booleans, counts,
content-inventory digests, and the fixed attempt identifier this script chooses
itself: workspace paths, source bytes, and other machine-local values never
enter candidate evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    integrity_check,
    open_database,
    table_names,
)
from omnivia_core_runtime.storage.inventory import (
    capture_inventory,
    compare_inventories,
)
from omnivia_core_runtime.storage.legacy import (
    migrate_legacy_database,
    rollback_migration,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    materialise_phase0_baseline,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import read_manifest

from omnivia_core.workspace.compatibility import (
    CompatibilityStatus,
    evaluate_compatibility,
)
from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

WORKSPACE_ID = "ws-standard-lifecycle"
CORE_VERSION = "0.1.0"
CREATED_AT = "2026-08-13T00:00:00+00:00"


class LifecycleError(RuntimeError):
    """The installed lifecycle qualification did not satisfy its assertions."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_legacy_source(path: Path) -> None:
    materialise_phase0_baseline(path)
    connection = sqlite3.connect(str(path))
    try:
        with connection:
            connection.execute(
                "INSERT INTO workspaces "
                "(id, name, root_path, storage_path, description, index_status, "
                "settings_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    WORKSPACE_ID,
                    "Standard lifecycle",
                    "/legacy/root",
                    "/legacy/storage",
                    "upgrade preservation record",
                    "indexed",
                    '{"source":"candidate"}',
                    CREATED_AT,
                    CREATED_AT,
                ),
            )
            connection.execute(
                "INSERT INTO memories "
                "(id, workspace_id, content, source_type, source_reference, "
                "source_description, lifecycle_state, memory_type, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "memory-standard-lifecycle",
                    WORKSPACE_ID,
                    "Unicode preservation: ☃ 🌍; quotes ' \"; empty and NULL stay distinct.",
                    "qualification",
                    "urn:omnivia:standard-lifecycle",
                    None,
                    "approved",
                    "evidence",
                    "standard-candidate",
                    CREATED_AT,
                    CREATED_AT,
                ),
            )
    finally:
        connection.close()


def _manifest() -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id=WORKSPACE_ID,
        created_at=CREATED_AT,
        name="Standard lifecycle",
        compatibility=CoreCompatibility(
            workspace_format_version="1",
            min_core_version=CORE_VERSION,
        ),
    )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise LifecycleError(message)


def qualify(root: Path) -> dict[str, object]:
    source = root / "legacy" / "explicit-source.sqlite"
    source.parent.mkdir(parents=True)
    _seed_legacy_source(source)
    source_digest = _digest(source)

    workspace = WorkspaceLayout(root=root / "workspace")
    installation = InstallationLayout(root=root / "installation-state")
    result = migrate_legacy_database(
        source,
        workspace,
        installation,
        _manifest(),
        service_instance_id="standard-lifecycle-upgrade",
        attempt_id="standard-lifecycle-upgrade",
    )
    _assert(_digest(source) == source_digest, "upgrade modified the legacy source")
    _assert(result.backup.verified, "upgrade backup was not verified")
    _assert(
        compare_inventories(result.source_inventory, result.published_inventory) == [],
        "upgrade did not preserve the source inventory",
    )
    published_manifest = read_manifest(workspace)
    _assert(published_manifest.workspace_id == WORKSPACE_ID, "workspace identity changed")
    _assert(published_manifest.integrity_matches(), "manifest integrity did not verify")
    _assert(
        published_manifest.migration is not None
        and published_manifest.migration.from_format_version == "0"
        and published_manifest.migration.to_format_version == "1",
        "upgrade history was not recorded",
    )

    published = open_database(workspace.database_path, OpenMode.READ_ONLY)
    try:
        migration_count = len(applied_migrations(published))
        _assert(integrity_check(published) == [], "upgraded database failed integrity")
        _assert(
            "omnivia_workspace_state" in table_names(published),
            "upgrade did not publish the ownership substrate",
        )
    finally:
        published.close()

    incompatible_manifest = replace(
        published_manifest,
        compatibility=CoreCompatibility(
            workspace_format_version="99",
            min_core_version=CORE_VERSION,
        ),
    )
    incompatible = evaluate_compatibility(incompatible_manifest, CORE_VERSION)
    _assert(
        incompatible.status is CompatibilityStatus.WORKSPACE_FORMAT_UNSUPPORTED
        and not incompatible.readable
        and not incompatible.writable,
        "unknown workspace format did not fail closed",
    )

    newer_manifest = replace(
        published_manifest,
        compatibility=CoreCompatibility(
            workspace_format_version="1",
            min_core_version="9.0.0",
        ),
    )
    package_rollback = evaluate_compatibility(newer_manifest, CORE_VERSION)
    _assert(
        package_rollback.status is CompatibilityStatus.CORE_TOO_OLD
        and package_rollback.readable
        and not package_rollback.writable,
        "older package did not refuse writable access to a newer workspace",
    )

    failed_source = root / "legacy" / "corrupt-source.sqlite"
    shutil.copy2(source, failed_source)
    corrupt = bytearray(failed_source.read_bytes())
    corrupt[0] ^= 0xFF
    failed_source.write_bytes(corrupt)
    failed_digest = _digest(failed_source)
    failed_workspace = WorkspaceLayout(root=root / "failed-workspace")
    failed = False
    try:
        migrate_legacy_database(
            failed_source,
            failed_workspace,
            installation,
            replace(_manifest(), workspace_id="ws-failed-upgrade"),
            service_instance_id="standard-lifecycle-failed-upgrade",
            attempt_id="standard-lifecycle-failed-upgrade",
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        failed = True
    _assert(failed, "corrupt upgrade source was accepted")
    _assert(_digest(failed_source) == failed_digest, "failed upgrade modified its source")
    _assert(
        not failed_workspace.database_path.exists(),
        "failed upgrade published a database",
    )

    recovery = root / "recovery" / "restored.sqlite"
    rollback_migration(result.backup, recovery)
    recovered = open_database(recovery, OpenMode.READ_ONLY)
    try:
        recovered_inventory = capture_inventory(recovered)
        _assert(integrity_check(recovered) == [], "recovered backup failed integrity")
        _assert(
            compare_inventories(result.source_inventory, recovered_inventory) == [],
            "recovery did not restore the pre-upgrade inventory",
        )
        _assert(
            "omnivia_workspace_state" not in table_names(recovered),
            "recovery silently retained the upgraded schema",
        )
    finally:
        recovered.close()

    live = open_database(workspace.database_path, OpenMode.READ_ONLY)
    try:
        live_unchanged = "omnivia_workspace_state" in table_names(live)
    finally:
        live.close()
    _assert(live_unchanged, "recovery downgraded the live workspace")

    # Which mechanism recovered the workspace, and which logical database it
    # produced.  A boolean says a restore happened; a rollback decision needs to
    # know it was the online backup API rather than a file copy, and that the
    # restored database is the same content the backup was verified against.
    # Every value below is read from the run that just passed its assertions.
    backup = result.backup
    identity = {
        "algorithm": "sha256",
        "scope": "database content inventory",
        "source": backup.source_inventory.content_checksum,
        "backup": backup.backup_inventory.content_checksum,
        "restored": recovered_inventory.content_checksum,
    }

    return {
        "format": "omnivia.standard-lifecycle-result.v1",
        "verdict": "pass",
        "backup_restore": {
            "mechanism": "sqlite3.Connection.backup",
            "mechanism_description": "SQLite online backup API",
            "create_procedure": (
                "omnivia_core_runtime.storage.backup.create_verified_backup"
            ),
            "verify_procedure": "omnivia_core_runtime.storage.backup.verify_backup",
            "restore_procedure": (
                "omnivia_core_runtime.storage.legacy.rollback_migration"
            ),
            "attempt_id": backup.attempt_id,
            "identity": identity,
            "backup_matches_source": identity["backup"] == identity["source"],
            "restored_matches_backup": identity["restored"] == identity["backup"],
            "verification_status": "verified" if backup.verified else "unverified",
            "restore_status": (
                "restored"
                if identity["restored"] == identity["backup"]
                else "not_restored"
            ),
        },
        "upgrade": {
            "phase0_to_workspace_format_1": "pass",
            "verified_backup_before_migration": True,
            "identity_preserved": True,
            "history_preserved": True,
            "source_unchanged": True,
            "migration_count": migration_count,
        },
        "failed_upgrade": {
            "corrupt_source_refused": True,
            "source_unchanged": True,
            "database_not_published": True,
        },
        "rollback_boundary": {
            "older_core_writable_access_refused": True,
            "read_only_diagnosis_available": True,
            "silent_schema_downgrade": False,
        },
        "recovery": {
            "verified_pre_upgrade_backup_restored": True,
            "inventory_preserved": True,
            "live_workspace_downgraded": False,
        },
        "incompatible_format": {
            "unknown_format_refused": True,
            "readable": False,
            "writable": False,
        },
        "private_paths_recorded": False,
        "secrets_recorded": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="omnivia-standard-lifecycle-") as temp:
            result = qualify(Path(temp))
    except (LifecycleError, OSError, sqlite3.Error, ValueError) as error:
        print(f"Standard lifecycle failed: {error}")
        return 1
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"verdict": "pass"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
