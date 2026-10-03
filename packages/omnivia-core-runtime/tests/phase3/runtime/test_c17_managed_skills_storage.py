"""C17 acceptance for the managed Skills registry writer and its bounded reads.

*Identity is content, and versions are append-only.* A published version is one `manifest_id`
over the canonical manifest. The same `(skill_name, version)` is published once: identical
content and different content each refuse, as their own exception. Changed content is a new
version with a new id.

*Authorship, publication and installation are separate acts.* Each is written on its own row
with its own actor, and nothing in one implies another: a draft is not a version, a version
is not installed, and an installed version is not bound to any Run.

*Resolution is deterministic and bounded.* An explicit manifest reference wins over the
role-compatibility filter, which wins over the highest compatible version. A deprecated or
uninstalled version is never newly selected. Dependencies are pinned by id and resolve to a
bounded closure; a cycle, a conflict, a missing pin, depth and size are each refused.

*A Run keeps what it was admitted with.* Its bindings are sealed, verified on read, and read
back without consulting install or deprecation state.

Everything runs against a real SQLite workspace migrated to head.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_repository as r102
import test_workflow_runs_migration as m27
from omnivia_core_runtime.storage import managed_skills as store
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1.semantics_skills import (
    MAX_CLOSURE,
    MAX_DEPENDENCY_DEPTH,
    SkillClosureError,
    resolve_closure,
    skill_manifest_id,
)

WORKSPACE_ID = m27.WORKSPACE_ID
BASE = m27.BASE_US + 10_000
ACTOR = "principal-author"
PUBLISHER = "principal-publisher"
OPERATOR = "principal-operator"
EVIDENCE = [{"evidence_id": "evidence-1", "content_digest": "sha256:" + "a" * 64}]
ROLE = "reviewer"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


class Registry:
    """A small driver that numbers every id, instant and audit event it needs."""

    def __init__(self, holder: m1.Owned) -> None:
        self.holder = holder
        self.n = 0

    def tick(self) -> tuple[int, str]:
        self.n += 1
        return BASE + self.n, f"audit-skills-{self.n}"

    def writer(self) -> Any:
        return store.managed_skills_writer(
            self.holder.connection,
            self.holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=self.holder.generation,
        )

    def step(self, action: Any) -> Any:
        at_us, audit_ref = self.tick()
        with self.writer() as w:
            m27.audit(self.holder, audit_ref)
            return action(w, at_us, audit_ref)

    def draft(self, manifest: dict[str, Any], *, draft_id: str | None = None) -> Any:
        draft_id = draft_id or f"draft-{self.n + 1}"
        return self.step(
            lambda w, at, ref: w.create_draft(
                draft_id=draft_id,
                draft_revision_id=f"{draft_id}-r1",
                manifest=manifest,
                source_work_ref=None,
                actor=ACTOR,
                at_us=at,
                audit_ref=ref,
            )
        )

    def submit(self, draft_id: str, revision: int = 1) -> Any:
        return self.step(
            lambda w, at, ref: w.submit_proposal(
                proposal_id=f"proposal-{draft_id}",
                draft_id=draft_id,
                expected_revision=revision,
                evidence=EVIDENCE,
                actor=ACTOR,
                at_us=at,
                audit_ref=ref,
            )
        )

    def publish(self, manifest: dict[str, Any]) -> store.SkillVersion:
        head = self.draft(manifest)
        self.submit(head.draft.draft_id)
        return self.step(
            lambda w, at, ref: w.publish_version(
                proposal_id=f"proposal-{head.draft.draft_id}",
                review_evidence=EVIDENCE,
                actor=PUBLISHER,
                at_us=at,
                audit_ref=ref,
            )
        )

    def install(self, manifest_id: str) -> store.SkillInstallEvent:
        return self.step(
            lambda w, at, ref: w.record_install_event(
                install_event_id=f"install-{self.n + 1}",
                manifest_id=manifest_id,
                event_kind="install",
                actor=OPERATOR,
                at_us=at,
                audit_ref=ref,
            )
        )

    def remove(self, manifest_id: str) -> store.SkillInstallEvent:
        return self.step(
            lambda w, at, ref: w.record_install_event(
                install_event_id=f"remove-{self.n + 1}",
                manifest_id=manifest_id,
                event_kind="remove",
                actor=OPERATOR,
                at_us=at,
                audit_ref=ref,
            )
        )

    def deprecate(self, manifest_id: str) -> store.SkillDeprecation:
        return self.step(
            lambda w, at, ref: w.deprecate_version(
                deprecation_id=f"deprecation-{self.n + 1}",
                manifest_id=manifest_id,
                reason="superseded.by_newer",
                actor=PUBLISHER,
                at_us=at,
                audit_ref=ref,
            )
        )

    def resolve(self, *requests: tuple[str, str | None], role: str = ROLE) -> store.RoleClosure:
        return store.resolve_role_selection(
            self.holder.connection,
            workspace_id=WORKSPACE_ID,
            role_id=role,
            requests=list(requests),
        )


@pytest.fixture
def registry(owned: m1.Owned) -> Registry:
    return Registry(owned)


def manifest(
    name: str = "triage",
    version: str = "1.0.0",
    *,
    dependencies: tuple[tuple[str, str], ...] = (),
    roles: tuple[str, ...] = (ROLE,),
    instructions: str = "Review the change and report what you find.",
) -> dict[str, Any]:
    return {
        "skill_name": name,
        "version": version,
        "description": f"{name} skill",
        "instructions": instructions,
        "references": [],
        "dependencies": [{"skill_name": n, "manifest_id": i} for n, i in dependencies],
        "compatible_roles": list(roles),
        "required_capabilities": ["repo.read"],
    }


# --- drafts, proposals, versions ---------------------------------------------------------


def test_a_draft_proposal_and_version_are_three_separate_rows_with_their_own_actors(
    registry: Registry,
) -> None:
    published = registry.publish(manifest())
    assert published.manifest_id == skill_manifest_id(manifest())
    head = store.read_draft_head(
        registry.holder.connection, workspace_id=WORKSPACE_ID, draft_id=published.draft_id
    )
    assert head is not None and head.proposal is not None
    assert head.draft.created_by == ACTOR
    assert head.proposal.submitted_by == ACTOR
    assert published.published_by == PUBLISHER
    assert published.review_evidence == tuple(EVIDENCE)
    assert (published.draft_id, published.draft_revision) == (head.draft.draft_id, 1)
    # Publishing is not installing: nothing is installed until an operator acts.
    assert (
        store.installed_state(
            registry.holder.connection, workspace_id=WORKSPACE_ID, manifest_id=published.manifest_id
        )
        is None
    )


def test_the_manifest_id_is_the_hash_of_the_canonical_manifest_whatever_the_order(
    registry: Registry,
) -> None:
    first = manifest(roles=("reviewer", "author"))
    second = {**manifest(roles=("author", "reviewer"))}
    assert skill_manifest_id(first) == skill_manifest_id(second)
    assert skill_manifest_id(first) != skill_manifest_id(manifest(instructions="Different."))


def test_an_update_must_be_made_against_the_latest_revision(registry: Registry) -> None:
    head = registry.draft(manifest())
    registry.step(
        lambda w, at, ref: w.revise_draft(
            draft_revision_id="rev-2",
            draft_id=head.draft.draft_id,
            expected_revision=1,
            manifest=manifest(instructions="Second."),
            actor=ACTOR,
            at_us=at,
            audit_ref=ref,
        )
    )
    with pytest.raises(StorageError, match="is at revision 2, not the revision 1"):
        registry.step(
            lambda w, at, ref: w.revise_draft(
                draft_revision_id="rev-3",
                draft_id=head.draft.draft_id,
                expected_revision=1,
                manifest=manifest(instructions="Stale."),
                actor=ACTOR,
                at_us=at,
                audit_ref=ref,
            )
        )


def test_an_update_that_changes_nothing_or_the_skill_name_is_refused(registry: Registry) -> None:
    head = registry.draft(manifest())
    for changed, message in (
        (manifest(), "changes nothing"),
        (manifest(name="other"), "keeps the skill name"),
    ):
        with pytest.raises(StorageError, match=message):
            registry.step(
                lambda w, at, ref, changed=changed, message=message: w.revise_draft(
                    draft_revision_id=f"rev-{message[:4]}",
                    draft_id=head.draft.draft_id,
                    expected_revision=1,
                    manifest=changed,
                    actor=ACTOR,
                    at_us=at,
                    audit_ref=ref,
                )
            )


def test_a_submitted_draft_is_closed_to_revision_and_to_a_second_submission(
    registry: Registry,
) -> None:
    head = registry.draft(manifest())
    registry.submit(head.draft.draft_id)
    with pytest.raises(StorageError, match="closed to revision"):
        registry.step(
            lambda w, at, ref: w.revise_draft(
                draft_revision_id="rev-late",
                draft_id=head.draft.draft_id,
                expected_revision=1,
                manifest=manifest(instructions="Late."),
                actor=ACTOR,
                at_us=at,
                audit_ref=ref,
            )
        )
    with pytest.raises(StorageError, match="already submitted"):
        registry.submit(head.draft.draft_id)


def test_a_proposal_submitted_against_a_stale_revision_is_refused(registry: Registry) -> None:
    head = registry.draft(manifest())
    registry.step(
        lambda w, at, ref: w.revise_draft(
            draft_revision_id="rev-2",
            draft_id=head.draft.draft_id,
            expected_revision=1,
            manifest=manifest(instructions="Second."),
            actor=ACTOR,
            at_us=at,
            audit_ref=ref,
        )
    )
    with pytest.raises(StorageError, match="not the revision 1"):
        registry.submit(head.draft.draft_id, revision=1)
    assert registry.submit(head.draft.draft_id, revision=2).draft_revision == 2


def test_identical_content_under_the_same_version_is_refused_as_identical(
    registry: Registry,
) -> None:
    registry.publish(manifest())
    with pytest.raises(store.SkillVersionConflict, match="identical content") as raised:
        registry.publish(manifest())
    assert raised.value.identical is True


def test_changed_content_under_the_same_version_is_refused_as_a_content_conflict(
    registry: Registry,
) -> None:
    registry.publish(manifest())
    with pytest.raises(store.SkillVersionConflict, match="different content") as raised:
        registry.publish(manifest(instructions="Changed without a new version."))
    assert raised.value.identical is False
    # The same change under a new version is simply a new immutable version.
    changed = registry.publish(manifest("triage", "1.0.1", instructions="Changed."))
    assert changed.manifest_id != skill_manifest_id(manifest())


def test_a_proposal_publishes_once(registry: Registry) -> None:
    head = registry.draft(manifest())
    registry.submit(head.draft.draft_id)
    publish = lambda w, at, ref: w.publish_version(  # noqa: E731
        proposal_id=f"proposal-{head.draft.draft_id}",
        review_evidence=EVIDENCE,
        actor=PUBLISHER,
        at_us=at,
        audit_ref=ref,
    )
    registry.step(publish)
    with pytest.raises(StorageError, match="already published"):
        registry.step(publish)


def test_publication_needs_reviewing_evidence(registry: Registry) -> None:
    head = registry.draft(manifest())
    registry.submit(head.draft.draft_id)
    with pytest.raises(StorageError, match="1 to 8 references"):
        registry.step(
            lambda w, at, ref: w.publish_version(
                proposal_id=f"proposal-{head.draft.draft_id}",
                review_evidence=[],
                actor=PUBLISHER,
                at_us=at,
                audit_ref=ref,
            )
        )


def test_a_dependency_must_be_a_published_manifest_of_another_skill(registry: Registry) -> None:
    unpublished = skill_manifest_id(manifest("library"))
    head = registry.draft(manifest("app", dependencies=(("library", unpublished),)))
    registry.submit(head.draft.draft_id)
    with pytest.raises(store.SkillResolutionError, match="not published") as raised:
        registry.step(
            lambda w, at, ref: w.publish_version(
                proposal_id=f"proposal-{head.draft.draft_id}",
                review_evidence=EVIDENCE,
                actor=PUBLISHER,
                at_us=at,
                audit_ref=ref,
            )
        )
    assert raised.value.code == "dependency_missing"
    library = registry.publish(manifest("library"))
    app = registry.publish(manifest("app", dependencies=(("library", library.manifest_id),)))
    assert app.manifest["dependencies"] == [
        {"skill_name": "library", "manifest_id": library.manifest_id}
    ]


def test_a_stored_manifest_that_was_edited_reads_as_corrupt(registry: Registry) -> None:
    published = registry.publish(manifest())
    # Only the schema's own guards stand between a file edit and a read, so edit past them,
    # from outside the service, the way a damaged or hand-edited file would be.
    connection = r102.tamper(
        registry.holder,
        "DROP TRIGGER omnivia_guard_skill_versions_update",
        "UPDATE omnivia_skill_versions SET manifest_json = "
        "replace(manifest_json, 'Review', 'Skip')",
    )
    with pytest.raises(StorageError, match="corrupt"):
        store.read_skill_version(
            connection, workspace_id=WORKSPACE_ID, manifest_id=published.manifest_id
        )


def test_reads_are_scoped_to_their_workspace(registry: Registry) -> None:
    published = registry.publish(manifest())
    assert (
        store.read_skill_version(
            registry.holder.connection,
            workspace_id="ws-someone-else",
            manifest_id=published.manifest_id,
        )
        is None
    )
    assert (
        store.read_draft_head(
            registry.holder.connection, workspace_id="ws-someone-else", draft_id=published.draft_id
        )
        is None
    )


# --- installation ------------------------------------------------------------------------


def test_install_and_remove_alternate_and_remove_deletes_nothing(registry: Registry) -> None:
    published = registry.publish(manifest())
    registry.install(published.manifest_id)
    registry.remove(published.manifest_id)
    registry.install(published.manifest_id)
    event = store.installed_state(
        registry.holder.connection, workspace_id=WORKSPACE_ID, manifest_id=published.manifest_id
    )
    assert event is not None and (event.event_sequence, event.event_kind) == (3, "install")
    registry.remove(published.manifest_id)
    assert store.read_skill_version(
        registry.holder.connection, workspace_id=WORKSPACE_ID, manifest_id=published.manifest_id
    )
    with pytest.raises(sqlite3.IntegrityError, match="must alternate"):
        registry.remove(published.manifest_id)


def test_a_deprecated_version_cannot_be_installed_and_deprecation_is_once(
    registry: Registry,
) -> None:
    published = registry.publish(manifest())
    registry.deprecate(published.manifest_id)
    with pytest.raises(StorageError, match="deprecated and cannot be installed"):
        registry.install(published.manifest_id)
    with pytest.raises(StorageError, match="already deprecated"):
        registry.deprecate(published.manifest_id)
    read = store.read_skill_version(
        registry.holder.connection, workspace_id=WORKSPACE_ID, manifest_id=published.manifest_id
    )
    assert read is not None and read.deprecation is not None
    assert read.deprecation.deprecated_by == PUBLISHER


def test_installing_or_deprecating_an_unpublished_manifest_is_refused(registry: Registry) -> None:
    absent = skill_manifest_id(manifest("ghost"))
    with pytest.raises(StorageError, match="not published"):
        registry.install(absent)
    with pytest.raises(StorageError, match="not published"):
        registry.deprecate(absent)


def test_installed_versions_of_one_skill_are_bounded(registry: Registry) -> None:
    for minor in range(store.MAX_INSTALLED_PER_SKILL):
        registry.install(registry.publish(manifest("triage", f"1.{minor}.0")).manifest_id)
    overflow = registry.publish(manifest("triage", "2.0.0"))
    with pytest.raises(StorageError, match="remove one first"):
        registry.install(overflow.manifest_id)


# --- resolution --------------------------------------------------------------------------


def test_the_highest_compatible_installed_version_wins_by_integer_order(
    registry: Registry,
) -> None:
    ids = {
        version: registry.publish(manifest("triage", version)).manifest_id
        for version in ("1.2.0", "1.10.0", "1.9.0")
    }
    for manifest_id in ids.values():
        registry.install(manifest_id)
    resolved = registry.resolve(("triage", None))
    assert [(e.skill_name, e.version, e.selection) for e in resolved.entries] == [
        ("triage", "1.10.0", "highest_compatible")
    ]
    assert resolved.entries[0].manifest_id == ids["1.10.0"]


def test_an_explicit_reference_beats_a_higher_version(registry: Registry) -> None:
    old = registry.publish(manifest("triage", "1.0.0"))
    new = registry.publish(manifest("triage", "2.0.0"))
    registry.install(old.manifest_id)
    registry.install(new.manifest_id)
    explicit = registry.resolve(("triage", old.manifest_id))
    assert [(e.version, e.selection) for e in explicit.entries] == [("1.0.0", "explicit")]
    assert registry.resolve(("triage", None)).entries[0].version == "2.0.0"


def test_the_role_filter_runs_before_the_highest_version(registry: Registry) -> None:
    compatible = registry.publish(manifest("triage", "1.0.0", roles=(ROLE,)))
    other_role = registry.publish(manifest("triage", "2.0.0", roles=("planner",)))
    registry.install(compatible.manifest_id)
    registry.install(other_role.manifest_id)
    assert registry.resolve(("triage", None)).entries[0].version == "1.0.0"
    assert registry.resolve(("triage", None), role="planner").entries[0].version == "2.0.0"
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", None), role="unrelated")
    assert raised.value.code == "no_compatible_version"
    # Naming a manifest never widens what a role may use.
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", other_role.manifest_id))
    assert raised.value.code == "role_incompatible"


def test_deprecated_and_uninstalled_versions_are_never_newly_selected(registry: Registry) -> None:
    first = registry.publish(manifest("triage", "1.0.0"))
    second = registry.publish(manifest("triage", "2.0.0"))
    registry.install(first.manifest_id)
    registry.install(second.manifest_id)
    registry.deprecate(second.manifest_id)
    assert registry.resolve(("triage", None)).entries[0].version == "1.0.0"
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", second.manifest_id))
    assert raised.value.code == "skill_deprecated"
    registry.remove(first.manifest_id)
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", None))
    assert raised.value.code == "no_compatible_version"
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", first.manifest_id))
    assert raised.value.code == "skill_not_installed"


def test_a_published_but_never_installed_manifest_is_not_selectable(registry: Registry) -> None:
    published = registry.publish(manifest())
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", published.manifest_id))
    assert raised.value.code == "skill_not_installed"


def test_an_explicit_reference_names_a_manifest_of_that_skill_only(registry: Registry) -> None:
    other = registry.publish(manifest("other"))
    registry.install(other.manifest_id)
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", other.manifest_id))
    assert raised.value.code == "selection_mismatch"
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", skill_manifest_id(manifest("triage", "9.9.9"))))
    assert raised.value.code == "skill_not_found"


def test_a_skill_is_selected_once_and_a_selection_is_bounded(registry: Registry) -> None:
    published = registry.publish(manifest())
    registry.install(published.manifest_id)
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("triage", None), ("triage", published.manifest_id))
    assert raised.value.code == "duplicate_selection"
    with pytest.raises(StorageError, match="between 1 and 16"):
        registry.resolve(*[(f"skill-{i}", None) for i in range(17)])


def test_dependencies_resolve_to_a_closure_with_dependencies_first(registry: Registry) -> None:
    leaf = registry.publish(manifest("leaf"))
    middle = registry.publish(manifest("middle", dependencies=(("leaf", leaf.manifest_id),)))
    top = registry.publish(manifest("top", dependencies=(("middle", middle.manifest_id),)))
    registry.install(top.manifest_id)
    closure = registry.resolve(("top", None))
    assert [(e.skill_name, e.selection) for e in closure.entries] == [
        ("leaf", "dependency"),
        ("middle", "dependency"),
        ("top", "highest_compatible"),
    ]
    # A dependency is resolved by its pin, so it need not be installed itself.
    assert registry.resolve(("top", None)) == closure


def test_two_manifests_of_one_skill_in_one_closure_conflict(registry: Registry) -> None:
    v1 = registry.publish(manifest("library", "1.0.0"))
    v2 = registry.publish(manifest("library", "2.0.0"))
    a = registry.publish(manifest("a", dependencies=(("library", v1.manifest_id),)))
    b = registry.publish(manifest("b", dependencies=(("library", v2.manifest_id),)))
    registry.install(a.manifest_id)
    registry.install(b.manifest_id)
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("a", None), ("b", None))
    assert raised.value.code == "dependency_conflict"


def test_a_dependency_incompatible_with_the_role_refuses_the_closure(registry: Registry) -> None:
    library = registry.publish(manifest("library", roles=("planner",)))
    app = registry.publish(manifest("app", dependencies=(("library", library.manifest_id),)))
    registry.install(app.manifest_id)
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.resolve(("app", None))
    assert raised.value.code == "role_incompatible"


def test_dependency_depth_is_bounded_at_publication_and_by_the_walk(registry: Registry) -> None:
    previous: tuple[str, str] | None = None
    for level in range(MAX_DEPENDENCY_DEPTH + 1):
        published = registry.publish(
            manifest(f"level-{level}", dependencies=(previous,) if previous else ())
        )
        previous = (f"level-{level}", published.manifest_id)
    # Eight dependency edges under the root is the most there may be, and it resolves.
    assert previous is not None
    registry.install(previous[1])
    assert len(registry.resolve((previous[0], None)).entries) == MAX_DEPENDENCY_DEPTH + 1
    # One more edge is refused when it is published, so it never reaches a Run.
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.publish(manifest("too-deep", dependencies=(previous,)))
    assert raised.value.code == "dependency_depth_exceeded"


def test_the_walk_itself_refuses_a_chain_past_the_depth_bound() -> None:
    chain = {
        f"skill-{index:064x}": {
            "skill_name": f"s{index}",
            "version": "1.0.0",
            "compatible_roles": [ROLE],
            "dependencies": (
                [{"skill_name": f"s{index + 1}", "manifest_id": f"skill-{index + 1:064x}"}]
                if index < MAX_DEPENDENCY_DEPTH + 3
                else []
            ),
        }
        for index in range(MAX_DEPENDENCY_DEPTH + 4)
    }
    with pytest.raises(SkillClosureError) as raised:
        resolve_closure([(f"skill-{0:064x}", "explicit")], role_id=ROLE, load=chain.get)
    assert raised.value.code == "dependency_depth_exceeded"


def test_closure_size_is_bounded_at_publication(registry: Registry) -> None:
    pins = [
        (f"leaf-{index:02}", registry.publish(manifest(f"leaf-{index:02}")).manifest_id)
        for index in range(MAX_CLOSURE)
    ]
    # Sixteen is the most one manifest may pin, so thirty-two leaves take two fan-out skills.
    first = registry.publish(manifest("first", dependencies=tuple(pins[:16])))
    second = registry.publish(manifest("second", dependencies=tuple(pins[16:])))
    with pytest.raises(store.SkillResolutionError) as raised:
        registry.publish(
            manifest(
                "root",
                dependencies=(("first", first.manifest_id), ("second", second.manifest_id)),
            )
        )
    assert raised.value.code == "closure_too_large"


def test_a_cycle_is_refused_by_the_walk_even_though_it_cannot_be_published() -> None:
    manifests = {
        "skill-" + "a" * 64: {
            "skill_name": "a",
            "version": "1.0.0",
            "compatible_roles": [ROLE],
            "dependencies": [{"skill_name": "b", "manifest_id": "skill-" + "b" * 64}],
        },
        "skill-" + "b" * 64: {
            "skill_name": "b",
            "version": "1.0.0",
            "compatible_roles": [ROLE],
            "dependencies": [{"skill_name": "a", "manifest_id": "skill-" + "a" * 64}],
        },
    }
    with pytest.raises(SkillClosureError) as raised:
        resolve_closure([("skill-" + "a" * 64, "explicit")], role_id=ROLE, load=manifests.get)
    assert raised.value.code == "dependency_cycle"


# --- run bindings ------------------------------------------------------------------------


def seed_run(registry: Registry) -> tuple[str, int, str]:
    m27.seed_workflow_run(registry.holder)
    return m27.RUN_ID, m27.BASE_US + 20, "aud-job-run-0001"


def bind(registry: Registry, closures: list[store.RoleClosure]) -> store.RunSkillBindings:
    run_id, bound_at, audit_ref = seed_run(registry)
    counter = iter(range(1, 100))
    with registry.writer() as w:
        return w.bind_run(
            run_id=run_id,
            roles=closures,
            bound_at_us=bound_at,
            audit_ref=audit_ref,
            allocate_binding_id=lambda: f"binding-{next(counter)}",
        )


def test_a_run_keeps_exactly_what_it_was_admitted_with(registry: Registry) -> None:
    first = registry.publish(manifest("triage", "1.0.0"))
    registry.install(first.manifest_id)
    bound = bind(registry, [registry.resolve(("triage", None))])
    # A newer version is published and installed, and the old one is deprecated and removed.
    newer = registry.publish(manifest("triage", "2.0.0"))
    registry.install(newer.manifest_id)
    registry.deprecate(first.manifest_id)
    registry.remove(first.manifest_id)
    read = store.read_run_skill_bindings(
        registry.holder.connection, workspace_id=WORKSPACE_ID, run_id=bound.run_id
    )
    assert read is not None
    assert [(b.manifest_id, b.version, b.selection) for b in read.bindings] == [
        (first.manifest_id, "1.0.0", "highest_compatible")
    ]
    assert read.set_digest == bound.set_digest
    assert registry.resolve(("triage", None)).entries[0].manifest_id == newer.manifest_id


def test_a_run_that_bound_nothing_reads_as_none(registry: Registry) -> None:
    run_id, _bound_at, _audit = seed_run(registry)
    assert (
        store.read_run_skill_bindings(
            registry.holder.connection, workspace_id=WORKSPACE_ID, run_id=run_id
        )
        is None
    )


def test_bindings_are_written_only_under_the_run_admission(registry: Registry) -> None:
    published = registry.publish(manifest())
    registry.install(published.manifest_id)
    closure = registry.resolve(("triage", None))
    run_id, bound_at, _audit = seed_run(registry)
    for kwargs, message in (
        ({"bound_at_us": bound_at + 1, "audit_ref": "aud-job-run-0001"}, "run admission"),
        ({"bound_at_us": bound_at, "audit_ref": "audit-skills-1"}, "run admission"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match=message), registry.writer() as w:
            w.bind_run(
                run_id=run_id,
                roles=[closure],
                allocate_binding_id=lambda: "binding-x",
                **kwargs,
            )


def test_a_sealed_set_cannot_grow_and_a_tampered_binding_is_detected(registry: Registry) -> None:
    published = registry.publish(manifest())
    registry.install(published.manifest_id)
    closure = registry.resolve(("triage", None))
    bound = bind(registry, [closure])
    connection = registry.holder.connection
    with pytest.raises(sqlite3.IntegrityError, match="sealed skill run binding generation cannot grow"), registry.writer():
        connection.execute(
            "INSERT INTO omnivia_skill_run_bindings (workspace_id, run_binding_id, run_id, "
            "binding_generation, binding_position, role_id, manifest_id, skill_name, selection, "
            "binding_digest, bound_at_us, audit_ref) VALUES (?, 'binding-late', ?, 1, 2, ?, ?, "
            "'triage', 'explicit', ?, ?, 'aud-job-run-0001')",
            (
                WORKSPACE_ID,
                bound.run_id,
                ROLE,
                published.manifest_id,
                "sha256:" + "0" * 64,
                m27.BASE_US + 20,
            ),
        )
    connection = r102.tamper(
        registry.holder,
        "DROP TRIGGER omnivia_guard_skill_run_bindings_update",
        "UPDATE omnivia_skill_run_bindings SET selection = 'dependency'",
    )
    with pytest.raises(StorageError, match="tampered"):
        store.read_run_skill_bindings(connection, workspace_id=WORKSPACE_ID, run_id=m27.RUN_ID)


def amend(
    registry: Registry,
    closures: list[store.RoleClosure],
    *,
    amendment_id: str = "amendment-1",
) -> store.RunSkillBindings:
    """Open the next generation of the seeded Run through the internal seam, under its own audit."""
    audit_ref = f"aud-{amendment_id}"
    counter = iter(range(1, 100))
    with registry.writer() as w:
        m27.audit(registry.holder, audit_ref)
        return store.append_run_binding_generation(
            w,
            run_id=m27.RUN_ID,
            accepted_amendment_id=amendment_id,
            roles=closures,
            rebound_at_us=m27.BASE_US + 30,
            audit_ref=audit_ref,
            allocate_binding_id=lambda: f"{amendment_id}-binding-{next(counter)}",
        )


def test_an_accepted_amendment_opens_a_generation_and_keeps_the_one_before(
    registry: Registry,
) -> None:
    first = registry.publish(manifest("triage", "1.0.0"))
    registry.install(first.manifest_id)
    admitted = bind(registry, [registry.resolve(("triage", None))])
    newer = registry.publish(manifest("triage", "2.0.0"))
    registry.install(newer.manifest_id)
    amended = amend(registry, [registry.resolve(("triage", None))])
    connection = registry.holder.connection
    assert (amended.binding_generation, amended.amendment_id) == (2, "amendment-1")
    assert [b.version for b in amended.bindings] == ["2.0.0"]
    assert store.read_run_skill_binding_generations(
        connection, workspace_id=WORKSPACE_ID, run_id=m27.RUN_ID
    ) == (1, 2)
    latest = store.read_run_skill_bindings(
        connection, workspace_id=WORKSPACE_ID, run_id=m27.RUN_ID
    )
    assert latest is not None and latest.set_digest == amended.set_digest
    earlier = store.read_run_skill_bindings(
        connection, workspace_id=WORKSPACE_ID, run_id=m27.RUN_ID, binding_generation=1
    )
    assert earlier is not None
    assert (earlier.set_digest, earlier.amendment_id) == (admitted.set_digest, None)
    assert [b.version for b in earlier.bindings] == ["1.0.0"]
    # The amendment itself is append-only, as every row of the registry is.
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), registry.writer():
        connection.execute("UPDATE omnivia_skill_binding_amendments SET accepted_at_us = 1")


def test_the_seam_refuses_to_amend_a_run_with_nothing_sealed(registry: Registry) -> None:
    published = registry.publish(manifest())
    registry.install(published.manifest_id)
    closure = registry.resolve(("triage", None))
    seed_run(registry)
    with pytest.raises(StorageError, match="has no sealed skill bindings to amend"):
        amend(registry, [closure])


def test_an_amendment_cannot_rebind_a_removed_or_deprecated_skill(registry: Registry) -> None:
    published = registry.publish(manifest())
    registry.install(published.manifest_id)
    closure = registry.resolve(("triage", None))
    bind(registry, [closure])
    registry.deprecate(published.manifest_id)
    with pytest.raises(sqlite3.IntegrityError, match="installed and not deprecated"):
        amend(registry, [closure])
    assert store.read_run_skill_binding_generations(
        registry.holder.connection, workspace_id=WORKSPACE_ID, run_id=m27.RUN_ID
    ) == (1,)


def test_a_later_generation_is_written_only_by_the_seam(registry: Registry) -> None:
    published = registry.publish(manifest())
    registry.install(published.manifest_id)
    bind(registry, [registry.resolve(("triage", None))])
    connection = registry.holder.connection
    # Writing the rows of a generation directly is refused: no accepted amendment opens it.
    with pytest.raises(sqlite3.IntegrityError, match="accepted amendment"), registry.writer():
        connection.execute(
            "INSERT INTO omnivia_skill_run_bindings (workspace_id, run_binding_id, run_id, "
            "binding_generation, binding_position, role_id, manifest_id, skill_name, selection, "
            "binding_digest, bound_at_us, audit_ref) VALUES (?, 'binding-direct', ?, 2, 1, ?, ?, "
            "'triage', 'highest_compatible', ?, ?, 'aud-job-run-0001')",
            (
                WORKSPACE_ID,
                m27.RUN_ID,
                ROLE,
                published.manifest_id,
                "sha256:" + "0" * 64,
                m27.BASE_US + 20,
            ),
        )
    # The seam is not a public surface: not exported, and not a method of the writer.
    assert "append_run_binding_generation" not in store.__all__
    assert not hasattr(store.ManagedSkillsWriter, "append_run_binding_generation")
    assert store.read_run_skill_binding_generations(
        connection, workspace_id=WORKSPACE_ID, run_id=m27.RUN_ID
    ) == (1,)


def test_the_seam_refuses_an_amendment_audit_the_workspace_never_recorded(
    registry: Registry,
) -> None:
    published = registry.publish(manifest())
    registry.install(published.manifest_id)
    bind(registry, [registry.resolve(("triage", None))])
    with pytest.raises(StorageError, match="not recorded in this workspace"), registry.writer() as w:
        store.append_run_binding_generation(
            w,
            run_id=m27.RUN_ID,
            accepted_amendment_id="amendment-1",
            roles=[registry.resolve(("triage", None))],
            rebound_at_us=m27.BASE_US + 30,
            audit_ref="aud-never-recorded",
            allocate_binding_id=lambda: "binding-x",
        )
