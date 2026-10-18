"""C08 outcome-admission authority: the v2 Project document and the pure decisions over it.

Every case loads a document from disk through `load_project_documents`, the same path the service uses, so
the file safety and the single parse are exercised together with the decisions. The decisions are pure: no
request principal, handler, storage or context generation is involved.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.service import knowledge_projects
from omnivia_core_runtime.service.knowledge_projects import (
    KNOWLEDGE_PROJECTS_FILE,
    KNOWLEDGE_PROJECTS_SCHEMA,
    KNOWLEDGE_PROJECTS_SCHEMA_V2,
    KnowledgeProjectsRefused,
    load_outcome_admission_authorities,
    load_project_authorities,
    load_project_documents,
)
from omnivia_core_runtime.service.outcome_admission import (
    ACTION_READ,
    ACTION_REVIEW,
    ACTION_SUBMIT,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ARCHIVED,
    LIFECYCLE_PAUSED,
    REFUSAL_REASONS,
    AccountableRoles,
    AdmissionSourceBinding,
    AdmittedOutcome,
    DeclaredRoles,
    OutcomeAdmissionAuthority,
    OutcomeAdmissionRefused,
    is_source_target,
)

WS = "ws-one"
PROJECT = "project-source"
OWNER = "owner-one"
EXECUTOR = "owner-two"
REVIEWER = "reader"
ROLES = DeclaredRoles(owner=OWNER, executor=EXECUTOR, reviewer=REVIEWER)
_ABSENT = object()


# --- documents ---------------------------------------------------------------------


def _work(work_id: str = "work-one") -> dict[str, Any]:
    return {
        "work_id": work_id,
        "sources": [
            {"target": "docs", "revisions": ["rev-1", "rev-2"]},
            {"target": "code", "revisions": ["rev-9"]},
        ],
    }


def _project(**changes: Any) -> dict[str, Any]:
    """One version 2 Project. Keys given as `_ABSENT` are removed, so a test can drop a member."""
    project: dict[str, Any] = {
        "workspace_id": WS,
        "project_id": PROJECT,
        "domain_scope": "product.core",
        "owners": [OWNER, EXECUTOR],
        "members": [REVIEWER],
        "lifecycle": LIFECYCLE_ACTIVE,
        "works": [_work()],
        "requested_scopes": ["read", "prepare"],
        "accountable_roles": {
            "owner": [OWNER],
            "executor": [EXECUTOR],
            "reviewer": [REVIEWER],
        },
    }
    for key, value in changes.items():
        if value is _ABSENT:
            del project[key]
        else:
            project[key] = value
    return project


def _document(*projects: Any, schema: str = KNOWLEDGE_PROJECTS_SCHEMA_V2) -> str:
    return json.dumps({"schema": schema, "projects": list(projects)})


def _installation(tmp_path: Path) -> Path:
    root = tmp_path / "installation-state"
    (root / "catalogue").mkdir(parents=True)
    return root


def _write(root: Path, payload: str | bytes, *, mode: int = 0o600) -> Path:
    path = root / "catalogue" / KNOWLEDGE_PROJECTS_FILE
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    path.chmod(mode)
    return path


def _admission(tmp_path: Path, payload: str) -> OutcomeAdmissionAuthority:
    root = _installation(tmp_path)
    _write(root, payload)
    return load_outcome_admission_authorities(root)[WS]


def _admit(authority: OutcomeAdmissionAuthority, **changes: Any) -> AdmittedOutcome:
    request: dict[str, Any] = {
        "project_id": PROJECT,
        "action": ACTION_SUBMIT,
        "work_id": "work-one",
        "target": "docs",
        "revision": "rev-1",
        "scopes": ["read"],
        "roles": ROLES,
    }
    request.update(changes)
    return authority.admit(**request)


# --- v1 and v2 loading --------------------------------------------------------------


def _v1_project() -> dict[str, Any]:
    return {
        "workspace_id": WS,
        "project_id": PROJECT,
        "domain_scope": "product.core",
        "owners": [OWNER, EXECUTOR],
        "members": [REVIEWER],
    }


def test_a_version_one_document_binds_sharing_and_no_admission(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    _write(root, _document(_v1_project(), schema=KNOWLEDGE_PROJECTS_SCHEMA))
    sharing, admission = load_project_documents(root)
    assert sharing[WS].project(PROJECT) is not None
    assert admission == {}
    assert load_outcome_admission_authorities(root) == {}


def test_one_version_two_document_binds_the_same_sharing_membership(
    tmp_path: Path,
) -> None:
    v1_root = _installation(tmp_path / "v1")
    _write(v1_root, _document(_v1_project(), schema=KNOWLEDGE_PROJECTS_SCHEMA))
    v2_root = _installation(tmp_path / "v2")
    _write(v2_root, _document(_project()))

    sharing_v2 = load_project_authorities(v2_root)
    assert sharing_v2 == load_project_authorities(v1_root)
    assert sharing_v2[WS].project(PROJECT) is not None
    admission = load_outcome_admission_authorities(v2_root)[WS]
    assert admission.project(PROJECT, ACTION_SUBMIT).owners == frozenset(
        {OWNER, EXECUTOR}
    )


def test_one_parse_supplies_both_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _installation(tmp_path)
    _write(root, _document(_project()))
    parses: list[bytes] = []
    parse = knowledge_projects._parse

    def counting(raw: bytes) -> dict[str, Any]:
        parses.append(raw)
        return parse(raw)

    monkeypatch.setattr(knowledge_projects, "_parse", counting)
    sharing, admission = load_project_documents(root)
    assert len(parses) == 1
    assert sharing[WS].project(PROJECT) is not None
    assert admission[WS].project(PROJECT, ACTION_READ).project_id == PROJECT


def test_a_version_two_document_with_no_projects_binds_nothing(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    _write(root, _document())
    assert load_project_documents(root) == ({}, {})


# --- closed shapes, bounds and duplicates --------------------------------------------


def _first_work(project: dict[str, Any]) -> dict[str, Any]:
    return project["works"][0]


def _first_source(project: dict[str, Any]) -> dict[str, Any]:
    return project["works"][0]["sources"][0]


def _roles(project: dict[str, Any]) -> dict[str, Any]:
    return project["accountable_roles"]


_MALFORMED: list[Any] = [
    pytest.param(lambda p: p.update(surprise=1), id="project-extra-member"),
    pytest.param(lambda p: p.pop("lifecycle"), id="lifecycle-missing"),
    pytest.param(lambda p: p.update(lifecycle="frozen"), id="lifecycle-unknown"),
    pytest.param(lambda p: p.update(lifecycle=5), id="lifecycle-not-text"),
    pytest.param(lambda p: p.update(works=[]), id="no-work"),
    pytest.param(lambda p: p.update(works="work-one"), id="works-not-a-list"),
    pytest.param(
        lambda p: p.update(works=[_work(f"work-{n}") for n in range(65)]),
        id="too-many-works",
    ),
    pytest.param(lambda p: p["works"].append(_work()), id="work-id-listed-twice"),
    pytest.param(lambda p: _first_work(p).update(surprise=1), id="work-extra-member"),
    pytest.param(lambda p: _first_work(p).pop("sources"), id="work-missing-sources"),
    pytest.param(
        lambda p: _first_work(p).update(work_id="work one!"), id="work-id-malformed"
    ),
    pytest.param(lambda p: _first_work(p).update(sources=[]), id="no-source"),
    pytest.param(
        lambda p: _first_work(p)["sources"].append(_first_source(p)),
        id="target-listed-twice",
    ),
    pytest.param(
        lambda p: _first_work(p).update(
            sources=[{"target": f"t{n}", "revisions": ["r"]} for n in range(17)]
        ),
        id="too-many-sources",
    ),
    pytest.param(
        lambda p: _first_source(p).update(surprise=1), id="source-extra-member"
    ),
    pytest.param(lambda p: _first_source(p).update(revisions=[]), id="no-revision"),
    pytest.param(
        lambda p: _first_source(p).update(revisions=["rev-1", "rev-1"]),
        id="revision-listed-twice",
    ),
    pytest.param(
        lambda p: _first_source(p).update(revisions=[f"rev-{n}" for n in range(65)]),
        id="too-many-revisions",
    ),
    pytest.param(
        lambda p: _first_source(p).update(revisions=["rev 1!"]),
        id="revision-malformed",
    ),
    pytest.param(lambda p: _first_source(p).update(target=7), id="target-not-text"),
    pytest.param(
        lambda p: _first_source(p).update(target=" docs"), id="target-leading-space"
    ),
    pytest.param(
        lambda p: _first_source(p).update(target="services\nmemory"),
        id="target-line-break",
    ),
    pytest.param(lambda p: p.update(requested_scopes=[]), id="no-scope"),
    pytest.param(
        lambda p: p.update(requested_scopes=["write"]), id="scope-outside-vocabulary"
    ),
    pytest.param(
        lambda p: p.update(requested_scopes=["read", "read"]),
        id="scope-listed-twice",
    ),
    pytest.param(lambda p: p.update(requested_scopes=[["read"]]), id="scope-not-text"),
    pytest.param(
        lambda p: p.update(requested_scopes=["read"] * 9), id="too-many-scopes"
    ),
    pytest.param(
        lambda p: p.update(accountable_roles="owner-one"), id="roles-not-an-object"
    ),
    pytest.param(lambda p: _roles(p).update(surprise=[OWNER]), id="roles-extra-member"),
    pytest.param(lambda p: _roles(p).pop("reviewer"), id="roles-missing-reviewer"),
    pytest.param(lambda p: _roles(p).update(executor=[]), id="role-without-principal"),
    pytest.param(
        lambda p: _roles(p).update(reviewer=[REVIEWER, REVIEWER]),
        id="role-principal-listed-twice",
    ),
    pytest.param(
        lambda p: _roles(p).update(owner=[REVIEWER]), id="owner-role-outside-owners"
    ),
    pytest.param(
        lambda p: _roles(p).update(executor=["stranger"]),
        id="executor-outside-owners-and-members",
    ),
    pytest.param(
        lambda p: _roles(p).update(reviewer=["stranger"]),
        id="reviewer-outside-owners-and-members",
    ),
    pytest.param(lambda p: p.update(owners=[OWNER, OWNER]), id="owner-listed-twice"),
    pytest.param(
        lambda p: p.update(members=[REVIEWER, REVIEWER]), id="member-listed-twice"
    ),
]


@pytest.mark.parametrize("mutate", _MALFORMED)
def test_a_malformed_version_two_project_refuses_with_the_fixed_version_two_reason(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], object]
) -> None:
    project = _project()
    mutate(project)
    root = _installation(tmp_path)
    _write(root, _document(project))
    with pytest.raises(
        KnowledgeProjectsRefused, match="not a valid version 2 document"
    ):
        load_project_documents(root)


def test_a_project_bound_twice_in_version_two_refuses(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    _write(root, _document(_project(), _project(domain_scope="product.other")))
    with pytest.raises(KnowledgeProjectsRefused, match="version 2"):
        load_project_documents(root)


def test_a_version_two_document_of_an_unknown_version_refuses(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    _write(root, _document(_project(), schema="omnivia.knowledge-projects.v3"))
    with pytest.raises(KnowledgeProjectsRefused, match="version 1 document"):
        load_project_documents(root)


def test_the_work_bound_can_be_exactly_at_its_limits(tmp_path: Path) -> None:
    project = _project(
        works=[
            {
                "work_id": f"work-{n}",
                "sources": [{"target": "docs", "revisions": ["rev-1"]}],
            }
            for n in range(64)
        ]
    )
    authority = _admission(tmp_path, _document(project))
    assert authority.project(PROJECT, ACTION_READ).work("work-63") is not None


# --- file safety, unchanged for version two -----------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_version_two_document_other_principals_can_write_is_refused(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path)
    _write(root, _document(_project()), mode=0o664)
    with pytest.raises(KnowledgeProjectsRefused, match="writable by other principals"):
        load_project_documents(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_version_two_document_reached_through_a_symlink_is_refused(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(_document(_project()), encoding="utf-8")
    target.chmod(0o600)
    (root / "catalogue" / KNOWLEDGE_PROJECTS_FILE).symlink_to(target)
    with pytest.raises(KnowledgeProjectsRefused, match="cannot be read"):
        load_project_documents(root)


def test_an_oversized_version_two_document_is_refused_before_it_is_parsed(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path)
    _write(root, _document(_project()) + " " * 70_000)
    with pytest.raises(KnowledgeProjectsRefused, match="too large"):
        load_project_documents(root)


def test_a_version_two_document_with_a_duplicate_member_refuses(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    payload = (
        '{"schema": "'
        + KNOWLEDGE_PROJECTS_SCHEMA_V2
        + '", "projects": [], "projects": []}'
    )
    _write(root, payload)
    # The document cannot be parsed far enough to read its schema, so it takes the shared parse refusal.
    with pytest.raises(KnowledgeProjectsRefused, match="not a valid version"):
        load_project_documents(root)


# --- lifecycle ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lifecycle", "action", "admitted"),
    [
        pytest.param(LIFECYCLE_ACTIVE, ACTION_REVIEW, True, id="active-review"),
        pytest.param(LIFECYCLE_ACTIVE, ACTION_SUBMIT, True, id="active-submit"),
        pytest.param(LIFECYCLE_ACTIVE, ACTION_READ, True, id="active-read"),
        pytest.param(LIFECYCLE_PAUSED, ACTION_REVIEW, True, id="paused-review"),
        pytest.param(LIFECYCLE_PAUSED, ACTION_SUBMIT, False, id="paused-submit"),
        pytest.param(LIFECYCLE_PAUSED, ACTION_READ, True, id="paused-read"),
        pytest.param(LIFECYCLE_ARCHIVED, ACTION_REVIEW, False, id="archived-review"),
        pytest.param(LIFECYCLE_ARCHIVED, ACTION_SUBMIT, False, id="archived-submit"),
        pytest.param(LIFECYCLE_ARCHIVED, ACTION_READ, True, id="archived-read"),
    ],
)
def test_lifecycle_decides_which_actions_a_project_admits(
    tmp_path: Path, lifecycle: str, action: str, admitted: bool
) -> None:
    authority = _admission(tmp_path, _document(_project(lifecycle=lifecycle)))
    if admitted:
        assert authority.project(PROJECT, action).project_id == PROJECT
        assert _admit(authority, action=action).project_id == PROJECT
    else:
        with pytest.raises(OutcomeAdmissionRefused) as refused:
            authority.project(PROJECT, action)
        assert refused.value.reason == "lifecycle_closed"
        with pytest.raises(OutcomeAdmissionRefused) as refused:
            _admit(authority, action=action)
        assert refused.value.reason == "lifecycle_closed"


# --- decisions -----------------------------------------------------------------------


def test_an_admitted_decision_returns_the_canonical_projection(tmp_path: Path) -> None:
    authority = _admission(tmp_path, _document(_project()))
    outcome = _admit(authority, scopes=["prepare", "read"])
    assert outcome == AdmittedOutcome(
        project_id=PROJECT,
        work_id="work-one",
        target="docs",
        revision="rev-1",
        scopes=("read", "prepare"),
        owner=OWNER,
        executor=EXECUTOR,
        reviewer=REVIEWER,
    )


def test_a_decision_projection_carries_no_principal_grant_fence_or_permission() -> None:
    fields = {field.name for field in dataclasses.fields(AdmittedOutcome)}
    assert fields == {
        "project_id",
        "work_id",
        "target",
        "revision",
        "scopes",
        "owner",
        "executor",
        "reviewer",
    }


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param(
            {"project_id": "project-missing"}, "unknown_project", id="project"
        ),
        pytest.param({"work_id": "work-missing"}, "unknown_work", id="work"),
        pytest.param({"target": "wiki"}, "unknown_source", id="source"),
        pytest.param({"revision": "rev-404"}, "unknown_revision", id="revision"),
        pytest.param(
            {"target": "code", "revision": "rev-1"},
            "unknown_revision",
            id="other-target",
        ),
        pytest.param({"scopes": ["execute"]}, "disallowed_scope", id="scope-outside"),
        pytest.param({"scopes": []}, "disallowed_scope", id="scope-empty"),
        pytest.param(
            {"scopes": ["read", "read"]}, "disallowed_scope", id="scope-repeated"
        ),
        pytest.param(
            {"roles": DeclaredRoles(OWNER, OWNER, REVIEWER)},
            "duplicate_role",
            id="duplicate-role",
        ),
        pytest.param(
            {"roles": DeclaredRoles(EXECUTOR, OWNER, REVIEWER)},
            "misassigned_role",
            id="owner-is-not-the-owner-role",
        ),
        pytest.param(
            {"roles": DeclaredRoles(OWNER, OWNER, REVIEWER)},
            "duplicate_role",
            id="owner-and-executor-the-same",
        ),
        pytest.param(
            {"roles": DeclaredRoles(OWNER, EXECUTOR, EXECUTOR)},
            "duplicate_role",
            id="executor-and-reviewer-the-same",
        ),
        pytest.param(
            {"roles": DeclaredRoles(OWNER, OWNER + "-x", REVIEWER)},
            "misassigned_role",
            id="executor-not-assigned",
        ),
        pytest.param(
            {"roles": DeclaredRoles(OWNER, EXECUTOR, EXECUTOR + "-x")},
            "misassigned_role",
            id="reviewer-not-a-member",
        ),
        pytest.param(
            {"roles": DeclaredRoles(OWNER, EXECUTOR, OWNER)},
            "duplicate_role",
            id="reviewer-is-the-owner",
        ),
        pytest.param({"scopes": "read"}, "disallowed_scope", id="scope-text"),
        pytest.param({"scopes": ["r", "e"]}, "disallowed_scope", id="scope-characters"),
        pytest.param({"scopes": None}, "disallowed_scope", id="scope-none"),
        pytest.param({"scopes": 5}, "disallowed_scope", id="scope-not-iterable"),
        pytest.param({"scopes": [["read"]]}, "disallowed_scope", id="scope-unhashable"),
        pytest.param({"scopes": [1]}, "disallowed_scope", id="scope-not-text"),
        pytest.param(
            {"roles": (OWNER, EXECUTOR, REVIEWER)},
            "misassigned_role",
            id="roles-tuple",
        ),
        pytest.param({"roles": None}, "misassigned_role", id="roles-none"),
        pytest.param(
            {"roles": {"owner": OWNER, "executor": EXECUTOR, "reviewer": REVIEWER}},
            "misassigned_role",
            id="roles-mapping",
        ),
    ],
)
def test_an_inadmissible_request_refuses_with_a_stable_reason(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    authority = _admission(tmp_path, _document(_project()))
    with pytest.raises(OutcomeAdmissionRefused) as refused:
        _admit(authority, **changes)
    assert refused.value.reason == reason
    assert reason in REFUSAL_REASONS


def test_a_role_assigned_to_the_wrong_role_refuses(tmp_path: Path) -> None:
    authority = _admission(tmp_path, _document(_project()))
    with pytest.raises(OutcomeAdmissionRefused) as refused:
        _admit(authority, roles=DeclaredRoles(OWNER, REVIEWER, EXECUTOR))
    assert refused.value.reason == "misassigned_role"


def test_the_decision_is_pure_and_repeatable(tmp_path: Path) -> None:
    authority = _admission(tmp_path, _document(_project()))
    assert _admit(authority) == _admit(authority)


def test_an_empty_authority_admits_nothing() -> None:
    with pytest.raises(OutcomeAdmissionRefused) as refused:
        OutcomeAdmissionAuthority().project(PROJECT, ACTION_READ)
    assert refused.value.reason == "unknown_project"


# --- source targets and direct construction -----------------------------------------


_ACCEPTED_TARGETS: list[Any] = [
    pytest.param("docs", id="plain"),
    pytest.param("services/omnivia-memory-dev", id="service-path"),
    pytest.param("repo://omnivia/dev", id="repo-uri"),
    pytest.param("release notes", id="inner-space"),
    pytest.param("docs/überblick", id="non-ascii"),
    pytest.param("x" * 512, id="exactly-512-bytes"),
    pytest.param("é" * 256, id="exactly-512-bytes-multibyte"),
]
_REFUSED_TARGETS: list[Any] = [
    pytest.param("", id="empty"),
    pytest.param(" docs", id="leading-space"),
    pytest.param("docs ", id="trailing-space"),
    pytest.param("\tdocs", id="leading-tab"),
    pytest.param("do\x00cs", id="nul"),
    pytest.param("do\x1fcs", id="c0-control"),
    pytest.param("do\x7fcs", id="delete"),
    pytest.param("do\x85cs", id="c1-next-line"),
    pytest.param("do\ncs", id="line-feed"),
    pytest.param("do\rcs", id="carriage-return"),
    pytest.param("do cs", id="line-separator"),
    pytest.param("do cs", id="paragraph-separator"),
    pytest.param("\ud800docs", id="lone-surrogate"),
    pytest.param("x" * 513, id="513-bytes"),
    pytest.param("é" * 257, id="514-bytes-multibyte"),
    pytest.param(b"docs", id="bytes"),
    pytest.param(None, id="none"),
    pytest.param(7, id="number"),
]


@pytest.mark.parametrize("target", _ACCEPTED_TARGETS)
def test_a_well_formed_source_target_is_an_opaque_binding(target: str) -> None:
    assert is_source_target(target)
    binding = AdmissionSourceBinding(target=target, revisions=("rev-1",))
    assert binding.target == target
    assert binding.revisions == ("rev-1",)


@pytest.mark.parametrize("target", _REFUSED_TARGETS)
def test_a_malformed_source_target_refuses_on_direct_construction(target: Any) -> None:
    assert not is_source_target(target)
    with pytest.raises(ValueError):
        AdmissionSourceBinding(target=target, revisions=("rev-1",))


def test_a_slash_bearing_target_is_admitted_from_the_document(tmp_path: Path) -> None:
    work = {
        "work_id": "work-one",
        "sources": [
            {"target": "services/omnivia-memory-dev", "revisions": ["rev-1"]},
            {"target": "repo://omnivia/dev", "revisions": ["rev-2"]},
        ],
    }
    authority = _admission(tmp_path, _document(_project(works=[work])))
    outcome = _admit(authority, target="repo://omnivia/dev", revision="rev-2")
    assert outcome.target == "repo://omnivia/dev"
    assert outcome.revision == "rev-2"
    assert _admit(authority, target="services/omnivia-memory-dev").target == (
        "services/omnivia-memory-dev"
    )


@pytest.mark.parametrize(
    ("revisions", "error"),
    [
        pytest.param("rev-1", TypeError, id="text-not-a-collection"),
        pytest.param(5, TypeError, id="not-iterable"),
        pytest.param([], ValueError, id="empty"),
        pytest.param(["rev-1", "rev-1"], ValueError, id="repeated"),
        pytest.param(["rev 1"], ValueError, id="malformed"),
        pytest.param([1], ValueError, id="not-text"),
        pytest.param([["rev-1"]], ValueError, id="unhashable"),
    ],
)
def test_a_malformed_source_binding_refuses_on_direct_construction(
    revisions: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        AdmissionSourceBinding(target="docs", revisions=revisions)


@pytest.mark.parametrize("field", ["owner", "executor", "reviewer"])
@pytest.mark.parametrize(
    ("value", "error"),
    [
        pytest.param("owner-one", TypeError, id="text-not-a-collection"),
        pytest.param(5, TypeError, id="not-iterable"),
        pytest.param([], ValueError, id="empty"),
        pytest.param(frozenset(), ValueError, id="empty-frozenset"),
        pytest.param(["owner-one", "owner-one"], ValueError, id="repeated"),
        pytest.param(["bad principal"], ValueError, id="malformed"),
        pytest.param([7], ValueError, id="not-text"),
        pytest.param([["owner-one"]], ValueError, id="unhashable"),
    ],
)
def test_a_malformed_accountable_role_refuses_on_direct_construction(
    field: str, value: Any, error: type[Exception]
) -> None:
    roles: dict[str, Any] = {
        "owner": {OWNER},
        "executor": {EXECUTOR},
        "reviewer": {REVIEWER},
    }
    roles[field] = value
    with pytest.raises(error):
        AccountableRoles(**roles)


def test_accountable_roles_normalise_each_role_to_a_frozenset() -> None:
    roles = AccountableRoles(
        owner=[OWNER], executor={EXECUTOR}, reviewer=(REVIEWER, EXECUTOR)
    )
    assert roles.owner == frozenset({OWNER})
    assert roles.executor == frozenset({EXECUTOR})
    assert roles.reviewer == frozenset({REVIEWER, EXECUTOR})
    assert all(
        isinstance(role, frozenset)
        for role in (roles.owner, roles.executor, roles.reviewer)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("owner", 7, id="owner-not-text"),
        pytest.param("executor", "", id="executor-empty"),
        pytest.param("reviewer", "reader one", id="reviewer-malformed"),
        pytest.param("owner", [OWNER], id="owner-a-collection"),
        pytest.param("reviewer", None, id="reviewer-none"),
    ],
)
def test_declared_roles_refuse_a_value_outside_identifiers(
    field: str, value: Any
) -> None:
    declared: dict[str, Any] = {
        "owner": OWNER,
        "executor": EXECUTOR,
        "reviewer": REVIEWER,
    }
    declared[field] = value
    with pytest.raises(ValueError):
        DeclaredRoles(**declared)
