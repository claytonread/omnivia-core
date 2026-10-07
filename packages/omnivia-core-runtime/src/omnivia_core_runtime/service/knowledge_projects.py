"""The server-held Project bindings for cross-Project knowledge sharing (DEV-REQ-081).

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

    {
      "schema": "omnivia.knowledge-projects.v1",
      "projects": [
        {
          "workspace_id": "ws-one",
          "project_id": "project-source",
          "domain_scope": "product.core",
          "owners": ["local-user", "owner-two"],
          "members": ["reader"]
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
from omnivia_core_runtime.storage.backup import InstallationLayout

__all__ = [
    "KNOWLEDGE_PROJECTS_FILE",
    "KNOWLEDGE_PROJECTS_SCHEMA",
    "KnowledgeProjectsRefused",
    "load_project_authorities",
]

KNOWLEDGE_PROJECTS_SCHEMA: Final = "omnivia.knowledge-projects.v1"

#: The document's name inside the installation's catalogue directory.
KNOWLEDGE_PROJECTS_FILE: Final = "knowledge-projects.json"

#: Bounds that keep the read a small, fixed-cost one. Each is far above any real installation and far
#: below what a document could otherwise hold.
_MAX_DOCUMENT_BYTES: Final = 65_536
_MAX_PROJECTS: Final = 64
_MAX_PRINCIPALS: Final = 64

_DOCUMENT_MEMBERS: Final = frozenset({"schema", "projects"})
_PROJECT_MEMBERS: Final = frozenset(
    {"workspace_id", "project_id", "domain_scope", "owners", "members"}
)

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


class KnowledgeProjectsRefused(Exception):
    """The Project document exists and cannot be used. Its message is one fixed sentence."""


def load_project_authorities(installation_root: Path) -> Mapping[str, ProjectAuthority]:
    """Each workspace's bound Projects, keyed by workspace id. Empty when no document is configured."""
    raw = _read(
        InstallationLayout(root=installation_root).catalogue / KNOWLEDGE_PROJECTS_FILE
    )
    if raw is None:
        return {}
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


def _authorities(document: dict[str, Any]) -> dict[str, ProjectAuthority]:
    """Validate every member of the document, then build one authority per workspace."""
    projects = document.get("projects")
    if (
        set(document) != _DOCUMENT_MEMBERS
        or document["schema"] != KNOWLEDGE_PROJECTS_SCHEMA
        or not isinstance(projects, list)
        or len(projects) > _MAX_PROJECTS
    ):
        raise KnowledgeProjectsRefused(_REASON_MALFORMED)
    by_workspace: dict[str, list[ProjectBinding]] = {}
    try:
        for entry in projects:
            if not isinstance(entry, dict) or set(entry) != _PROJECT_MEMBERS:
                raise ValueError("a binding is outside its closed shape")
            workspace_id = _text(entry["workspace_id"])
            if not is_workspace_id(workspace_id):
                raise ValueError("a binding names a malformed workspace")
            by_workspace.setdefault(workspace_id, []).append(
                ProjectBinding(
                    project_id=_text(entry["project_id"]),
                    domain_scope=_text(entry["domain_scope"]),
                    owners=_principals(entry["owners"]),
                    members=_principals(entry["members"]),
                )
            )
        return {
            workspace: ProjectAuthority.of(bindings)
            for workspace, bindings in by_workspace.items()
        }
    except (TypeError, ValueError):
        # `ProjectBinding` and `ProjectAuthority` state their own reasons, which are not repeated: a
        # value from the document must not reach an operator's terminal through this path. The `try`
        # returns only when every binding is valid, so reaching this line means the handler ran.
        pass
    raise KnowledgeProjectsRefused(_REASON_MALFORMED)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("a binding member is not text")
    return value


def _principals(value: object) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > _MAX_PRINCIPALS:
        raise ValueError("a principal list is outside its bounds")
    principals = [_text(item) for item in value]
    if len(set(principals)) != len(principals):
        raise ValueError("a principal is listed twice")
    return frozenset(principals)
