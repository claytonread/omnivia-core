"""The server-held Project bindings for cross-Project knowledge sharing (DEV-REQ-081) and C08 outcome admission.

`ProjectAuthority` is composition state. Nothing a request carries, and nothing a caller selects, can add
a Project, an owner or a member to it, so its only source is this document: an operator-owned file in the
installation's catalogue directory. Its location is fixed relative to the installation root that
`--installation-state` names, so no flag, environment value or request chooses it. A managed start's child
is given that same installation root, so it reads the same document its launcher did.

The document is read once, when a service that serves starts, and fully validated before that service
advertises anything. Each workspace has its own bindings, so a Project bound in one workspace grants nothing
in another. A missing document binds nothing, which is the unconfigured posture where every sharing
operation refuses. A document that exists but cannot be used refuses the start: a service that silently
bound less than its operator wrote would be worse than one that does not start.

Owners and members name principals. The local owner is `local-user`. An installed MCP principal is the
`principal_id` its setup reports when it is configured, and a setup holds the sharing grants only through its
profile, so naming a principal here does not give it any operation.

Version 1 binds knowledge sharing only. Version 2 is additive: every Project carries the same v1 fields, and
also its lifecycle, the Works it holds with their target and revision bindings, its requested scopes, and its
accountable owner, executor and reviewer. Both versions bind the same sharing membership. Only version 2
yields outcome-admission authority (`service/outcome_admission.py`).

    {
      "schema": "omnivia.knowledge-projects.v2",
      "projects": [
        {
          "workspace_id": "ws-one",
          "project_id": "project-source",
          "domain_scope": "product.core",
          "owners": ["local-user", "owner-two"],
          "members": ["reader"],
          "lifecycle": "active",
          "works": [
            {
              "work_id": "work-one",
              "sources": [{"target": "docs", "revisions": ["rev-1"]}]
            }
          ],
          "requested_scopes": ["read", "prepare"],
          "accountable_roles": {
            "owner": ["local-user"],
            "executor": ["owner-two"],
            "reviewer": ["reader"]
          }
        }
      ]
    }
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import is_workspace_id
from omnivia_core_runtime.service.knowledge_sharing import (
    ProjectAuthority,
    ProjectBinding,
)
from omnivia_core_runtime.service.outcome_admission import (
    AccountableRoles,
    AdmissionProjectBinding,
    AdmissionSourceBinding,
    AdmissionWorkBinding,
    OutcomeAdmissionAuthority,
)
from omnivia_core_runtime.storage.backup import InstallationLayout

__all__ = [
    "KNOWLEDGE_PROJECTS_FILE",
    "KNOWLEDGE_PROJECTS_SCHEMA",
    "KNOWLEDGE_PROJECTS_SCHEMA_V2",
    "KnowledgeProjectsRefused",
    "load_outcome_admission_authorities",
    "load_project_authorities",
    "load_project_documents",
]

KNOWLEDGE_PROJECTS_SCHEMA: Final = "omnivia.knowledge-projects.v1"
KNOWLEDGE_PROJECTS_SCHEMA_V2: Final = "omnivia.knowledge-projects.v2"
_SCHEMAS: Final = (KNOWLEDGE_PROJECTS_SCHEMA, KNOWLEDGE_PROJECTS_SCHEMA_V2)

#: The document's name inside the installation's catalogue directory.
KNOWLEDGE_PROJECTS_FILE: Final = "knowledge-projects.json"

#: Bounds that keep the read a small, fixed-cost one. Each is far above any real installation and far
#: below what a document could otherwise hold.
_MAX_DOCUMENT_BYTES: Final = 65_536
_MAX_PROJECTS: Final = 64
_MAX_PRINCIPALS: Final = 64
_MAX_WORKS: Final = 64
_MAX_SOURCES: Final = 16
_MAX_REVISIONS: Final = 64
#: The closed scope vocabulary has three entries, so a longer list is malformed before it is compared.
_MAX_SCOPES: Final = 8

_DOCUMENT_MEMBERS: Final = frozenset({"schema", "projects"})
_PROJECT_MEMBERS: Final = frozenset(
    {"workspace_id", "project_id", "domain_scope", "owners", "members"}
)
_PROJECT_MEMBERS_V2: Final = _PROJECT_MEMBERS | {
    "lifecycle",
    "works",
    "requested_scopes",
    "accountable_roles",
}
_WORK_MEMBERS: Final = frozenset({"work_id", "sources"})
_SOURCE_MEMBERS: Final = frozenset({"target", "revisions"})
_ROLE_MEMBERS: Final = frozenset({"owner", "executor", "reviewer"})

#: Every sentence a refusal can carry. Fixed, so no value from the document reaches an operator's
#: terminal and nothing about the document's contents is disclosed by a refusal.
_REASON_UNREADABLE: Final = "the knowledge sharing Project document cannot be read"
_REASON_NOT_A_FILE: Final = (
    "the knowledge sharing Project document is not a regular file"
)
_REASON_WRITABLE: Final = (
    "the knowledge sharing Project document is writable by other principals"
)
_REASON_TOO_LARGE: Final = "the knowledge sharing Project document is too large"
_REASON_MALFORMED: Final = (
    "the knowledge sharing Project document is not a valid version 1 document"
)
_REASON_MALFORMED_V2: Final = (
    "the knowledge sharing Project document is not a valid version 2 document"
)


class KnowledgeProjectsRefused(Exception):
    """The Project document exists and cannot be used. Its message is one fixed sentence."""


def load_project_authorities(installation_root: Path) -> Mapping[str, ProjectAuthority]:
    """Each workspace's bound Projects, keyed by workspace id. Empty when no document is configured."""
    return load_project_documents(installation_root)[0]


def load_outcome_admission_authorities(
    installation_root: Path,
) -> Mapping[str, OutcomeAdmissionAuthority]:
    """Each workspace's outcome-admission authority, from a version 2 document. Empty for version 1 or none."""
    return load_project_documents(installation_root)[1]


def load_project_documents(
    installation_root: Path,
) -> tuple[Mapping[str, ProjectAuthority], Mapping[str, OutcomeAdmissionAuthority]]:
    """Both authorities from one read and one parse of the document, keyed by workspace id."""
    raw = _read(
        InstallationLayout(root=installation_root).catalogue / KNOWLEDGE_PROJECTS_FILE
    )
    if raw is None:
        return {}, {}
    return _authorities(_parse(raw))


def _read(path: Path) -> bytes | None:
    """The document's bytes, or `None` when there is no document at all.

    Opened without following a final symlink, and checked through the descriptor that was opened, so
    the bytes read are the file that was checked. A document other principals can write is refused,
    because whoever can write it can grant Project authority.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    unreadable = False
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        unreadable = True
    if unreadable:
        raise KnowledgeProjectsRefused(_REASON_UNREADABLE)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise KnowledgeProjectsRefused(_REASON_NOT_A_FILE)
        if os.name != "nt" and status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise KnowledgeProjectsRefused(_REASON_WRITABLE)
    except BaseException:
        # Refused before the descriptor is wrapped, so it is closed here rather than by a `with`.
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(_MAX_DOCUMENT_BYTES + 1)
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise KnowledgeProjectsRefused(_REASON_TOO_LARGE)
    return raw


def _parse(raw: bytes) -> dict[str, Any]:
    """The document as a JSON object, refusing duplicate members and non-standard numbers."""
    # UnicodeDecodeError and JSONDecodeError are ValueErrors; a deeply nested document is a RecursionError.
    parsed: Any = None
    malformed = False
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_members,
            parse_constant=_refuse_constant,
        )
    except (ValueError, RecursionError):
        malformed = True
    if malformed or not isinstance(parsed, dict):
        raise KnowledgeProjectsRefused(_REASON_MALFORMED)
    return parsed


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    members: dict[str, Any] = {}
    for key, value in pairs:
        if key in members:
            raise ValueError("a member is repeated")
        members[key] = value
    return members


def _refuse_constant(_name: str) -> Any:
    raise ValueError("a non-standard number is not a Project binding")


def _authorities(
    document: dict[str, Any],
) -> tuple[Mapping[str, ProjectAuthority], Mapping[str, OutcomeAdmissionAuthority]]:
    """Validate every member of the document, then build the authorities per workspace.

    Each Project's owners and members are read once and given to both the sharing binding and, for
    version 2, the admission binding, so the two cannot disagree about who is in a Project.
    """
    schema = document.get("schema")
    projects = document.get("projects")
    if (
        set(document) != _DOCUMENT_MEMBERS
        or schema not in _SCHEMAS
        or not isinstance(projects, list)
        or len(projects) > _MAX_PROJECTS
    ):
        raise KnowledgeProjectsRefused(
            _REASON_MALFORMED_V2
            if schema == KNOWLEDGE_PROJECTS_SCHEMA_V2
            else _REASON_MALFORMED
        )
    version_two = schema == KNOWLEDGE_PROJECTS_SCHEMA_V2
    shape = _PROJECT_MEMBERS_V2 if version_two else _PROJECT_MEMBERS
    sharing: dict[str, list[ProjectBinding]] = {}
    admission: dict[str, list[AdmissionProjectBinding]] = {}
    try:
        for entry in projects:
            if not isinstance(entry, dict) or set(entry) != shape:
                raise ValueError("a binding is outside its closed shape")
            workspace_id = _text(entry["workspace_id"])
            if not is_workspace_id(workspace_id):
                raise ValueError("a binding names a malformed workspace")
            project_id = _text(entry["project_id"])
            owners = _principals(entry["owners"])
            members = _principals(entry["members"])
            sharing.setdefault(workspace_id, []).append(
                ProjectBinding(
                    project_id=project_id,
                    domain_scope=_text(entry["domain_scope"]),
                    owners=owners,
                    members=members,
                )
            )
            if version_two:
                admission.setdefault(workspace_id, []).append(
                    _admission_project(entry, project_id, owners, members)
                )
        return (
            {
                workspace: ProjectAuthority.of(bindings)
                for workspace, bindings in sharing.items()
            },
            {
                workspace: OutcomeAdmissionAuthority.of(bindings)
                for workspace, bindings in admission.items()
            },
        )
    except (TypeError, ValueError):
        # The binding and authority types state their own reasons, which are not repeated: a value from
        # the document must not reach an operator's terminal through this path. The `try` returns only
        # when every binding is valid, so reaching this line means the handler ran.
        pass
    raise KnowledgeProjectsRefused(
        _REASON_MALFORMED_V2 if version_two else _REASON_MALFORMED
    )


def _admission_project(
    entry: dict[str, Any],
    project_id: str,
    owners: frozenset[str],
    members: frozenset[str],
) -> AdmissionProjectBinding:
    roles = _exact(entry["accountable_roles"], _ROLE_MEMBERS)
    return AdmissionProjectBinding(
        project_id=project_id,
        lifecycle=_text(entry["lifecycle"]),
        owners=owners,
        members=members,
        works=tuple(_work(item) for item in _items(entry["works"], _MAX_WORKS)),
        requested_scopes=_texts(entry["requested_scopes"], _MAX_SCOPES),
        roles=AccountableRoles(
            owner=_principals(roles["owner"]),
            executor=_principals(roles["executor"]),
            reviewer=_principals(roles["reviewer"]),
        ),
    )


def _work(value: object) -> AdmissionWorkBinding:
    entry = _exact(value, _WORK_MEMBERS)
    return AdmissionWorkBinding(
        work_id=_text(entry["work_id"]),
        sources=tuple(_source(item) for item in _items(entry["sources"], _MAX_SOURCES)),
    )


def _source(value: object) -> AdmissionSourceBinding:
    entry = _exact(value, _SOURCE_MEMBERS)
    return AdmissionSourceBinding(
        target=_text(entry["target"]),
        revisions=_texts(entry["revisions"], _MAX_REVISIONS),
    )


def _exact(value: object, members: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != members:
        raise ValueError("an object is outside its closed shape")
    return value


def _items(value: object, bound: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > bound:
        raise ValueError("a list is outside its bounds")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("a binding member is not text")
    return value


def _texts(value: object, bound: int) -> tuple[str, ...]:
    return tuple(_text(item) for item in _items(value, bound))


def _principals(value: object) -> frozenset[str]:
    principals = _texts(value, _MAX_PRINCIPALS)
    if len(set(principals)) != len(principals):
        raise ValueError("a principal is listed twice")
    return frozenset(principals)
