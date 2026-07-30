"""Strict-mypy consumer fixture for the Module Manifest compatibility facade.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer: it imports ``ModuleManifest`` and ``validate_module_manifest``
from ``omnivia_memory.module_manifest.*`` (never from ``omnivia_core``) and
proves the facade re-exports usefully typed objects -- the exact canonical
types, not ``Any`` -- via ``typing.assert_type``.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``). If the facade ever stopped
explicitly re-exporting these names, or degraded them to ``Any``, strict mypy
would fail here.
"""

from typing import assert_type

from omnivia_memory.module_manifest.models import (
    Entrypoint,
    Integrity,
    ModuleKind,
    ModuleManifest,
    Permission,
    PublishedTarget,
)
from omnivia_memory.module_manifest.validation import (
    ModuleManifestValidationError,
    validate_module_manifest,
)


def build() -> ModuleManifest:
    """Construct a manifest through the facade's own types."""
    return ModuleManifest(
        manifest_version="1.0.0",
        module_id="com.omnivia.apps",
        display_name="Apps",
        kind=ModuleKind.APPS,
        version="1.0.0",
        compatible_core_versions=[">=0.1,<0.2"],
        compatible_platform_versions=[">=0.1,<0.2"],
        entrypoint=Entrypoint(module="omnivia_apps"),
        permissions=[Permission(name="storage.read")],
        integrity=Integrity(algorithm="sha256", digest="0" * 64),
        published_targets=[PublishedTarget(target_id="t", contract_path="t.json")],
    )


def consume(payload: dict[str, object]) -> str:
    """Validate through the facade and use the result's precise types."""
    try:
        manifest = validate_module_manifest(payload)
    except ModuleManifestValidationError as error:
        return str(error)

    # No `Any` leakage: each attribute keeps its canonical static type through
    # the facade, so these compose without a cast or an ignore.
    assert_type(manifest, ModuleManifest)
    assert_type(manifest.module_id, str)
    assert_type(manifest.kind, ModuleKind)
    assert_type(manifest.entrypoint, Entrypoint)
    assert_type(manifest.integrity, Integrity)
    assert_type(manifest.permissions, list[Permission])
    assert_type(manifest.published_targets, list[PublishedTarget])

    kind: str = manifest.kind.value
    permissions: list[str] = [permission.name for permission in manifest.permissions]
    targets: list[str] = [target.contract_path for target in manifest.published_targets]
    return f"{manifest.module_id}:{kind}:{manifest.entrypoint.module}" + "".join(
        permissions + targets + [manifest.integrity.digest]
    )


def roundtrip() -> str:
    """The two facade paths compose with each other, not just individually."""
    built = build()
    return consume(
        {
            "manifest_version": built.manifest_version,
            "module_id": built.module_id,
            "display_name": built.display_name,
            "kind": built.kind.value,
            "version": built.version,
            "compatible_core_versions": built.compatible_core_versions,
            "compatible_platform_versions": built.compatible_platform_versions,
            "entrypoint": {
                "module": built.entrypoint.module,
                "class_name": built.entrypoint.class_name,
                "config_key": built.entrypoint.config_key,
            },
            "permissions": [
                {"name": permission.name, "description": permission.description}
                for permission in built.permissions
            ],
            "integrity": {
                "algorithm": built.integrity.algorithm,
                "digest": built.integrity.digest,
                "signature": built.integrity.signature,
            },
            "published_targets": [
                {
                    "target_id": target.target_id,
                    "contract_path": target.contract_path,
                }
                for target in built.published_targets
            ],
        }
    )
