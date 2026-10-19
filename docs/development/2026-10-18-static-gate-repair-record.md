# Static gate repair record (P10 inherited blockers)

- Date: 2026-10-18
- Branch: `codex/core-tech-gate-repair`
- Baseline: `be327ce1` ("docs(core): record the update trust and staging
  integration evidence")
- Scope: exactly the twelve inherited static-analysis findings listed in §4 of
  `2026-10-18-update-trust-integration-record.md`. No update, semantic,
  migration, native Settings or public contract behaviour was touched.

This record uses the verification levels of
`OMNIVIA-CORE-TECH-SPECS-IMPLEMENTATION-PLAN-001` §10. It repairs two
merge-blocking static gates and one fail-closed publication defect the type
error was hiding. It is **not** release qualification and raises no
A-scenario, work-ID or acceptance-case status. The `L5`/`L6` position of the
document-intake vertical is exactly where the Waves 1-4 and update-trust
records left it.

All commands below ran from this worktree on macOS 27.0.0 (Darwin, arm64)
against a worktree-local `.venv` provisioned per the `scripts/preflight`
header: CPython 3.11.15, ruff 0.16.1, mypy 2.3.0, pytest 9.1.1. Those are the
exact versions `services/omnivia-memory/pyproject.toml` pins for CI.

## 1. The twelve findings and what each repair was

### Ruff `SIM117` x2 -- `tests/test_packaged_runtime.py:705,717`

Each was `with pytest.raises(...)` wrapped around `with
<catalogue>.frozen_resources(...)`. Both are now one parenthesised `with`
holding the two managers in the same order.

Order is the semantics here, and it is unchanged: `pytest.raises` is entered
first, so a `CaptureNotExecutable` raised by `frozen_resources.__enter__`
still propagates out of the second manager and into the first. A manager whose
`__enter__` raised is never exited, in the combined form exactly as in the
nested one, so the test's cleanup and error path are the ones it had.

### Strict mypy x10

| # | Where | Code | Repair |
|---|---|---|---|
| 1 | `ooxml_extractors.py` `_Package._checked_xml` | `valid-type` | return type spelled `builtins.bytes` |
| 2-6 | `ooxml_extractors.py` `_Package.xml_with_prefixes` | `var-annotated`, `func-returns-value`, `misc` x2, `return-value` | method deleted |
| 7-9 | `ooxml_extractors.py` `_DocxWalker.inventory_note` x2, `inventory_applicability` | `arg-type` | `False` -> `"current"` |
| 10 | `document_extractors.py` `_InertHTMLParser.handle_starttag` | `dict-item` | `placement: dict[str, object]` declared |

**1.** `_Package` defines a reader called `bytes`, so from that method onwards
the class namespace binds the name to the reader and `-> bytes` no longer
names the builtin. The annotation now says `builtins.bytes`, which is the type
the function has always returned. The method was not renamed: several Waves
1-4 tests patch `_Package.bytes` as the single witness that a member's content
was read, and renaming it is a change to delivered surface this packet does not
own.

**2-6.** `xml_with_prefixes` had no caller, no test and no reference anywhere
in the tree, in any file type, from its introduction at `c6eb3621`
(2026-10-14) to this commit. It was also incapable of working:
`xml.etree.ElementTree.XMLPullParser.close()` returns `None` by documented
design ("Unlike XMLParser, does not return the root element"), so the method
returned `(None, prefixes)` where its signature promised an `Element`. That is
what mypy reported as five of the ten findings -- the unsolved element type,
the valueless `close()`, the two unpack errors over `read_events()` and the
return type. Repairing it would have meant giving dead surface a second parse
and a test of its own; the live markup-compatibility path resolves
`mc:AlternateContent` by local name and needs no prefix map. Deleted instead,
and recoverable from `c6eb3621` if a later packet wants the helper wired up.

**7-9.** See §2: this one is a real defect, not a type artefact.

**10.** A placement starts as table or element ordinals (`int`) and, where the
element declares a hidden style, carries the claim list beside them. mypy
inferred `dict[str, int]` from the first branch. The variable is now declared
`dict[str, object]`, which is what `_place` and `_locator` already accept on
either side of it. No `Any` was introduced.

## 2. The defect behind findings 7-9 (L3)

`_DocxWalker._inventory` takes `state: str` and three call sites passed
`False`:

- `inventory_note` -- a footnote/endnote separator, continuation separator or
  continuation notice (`noteSeparator`);
- `inventory_note` -- a comment's author, date, initials and thread claims
  (`commentMetadata`);
- `inventory_applicability` -- every `(section, kind)` a header or footer part
  stands for (`headerFooter`).

`storage/document_intake.py` closes the state over
`SEGMENT_REVISIONS = {"current", "deleted", "inserted", "none"}`, and
`service/document_intake.py::_validated_segments` refuses any segment outside
it. `False` is in neither set, so **before this repair every record of those
three kinds was refused at publication**, and because the refusal is raised
over the segment tuple it took the whole document's extraction with it. Any
DOCX carrying a header, a footer, a note separator or a comment with an author
was affected.

Observed directly against the real validator:

```
SEGMENT_REVISIONS         : ['current', 'deleted', 'inserted', 'none']
False in SEGMENT_REVISIONS: False
revision_state=False      -> refused: an extraction segment has an unknown revision state
revision_state="current"  -> admitted
```

**Why `"current"` and not `"none"`.** `_DocxWalker.walk` seeds its own
unrevised baseline as `state="current"`, every other extractor (XLSX, PPTX,
HTML, delimited, RTF for undeleted content) leaves the `ExtractedSegment`
default of `"current"`, and `"none"` has no producer anywhere in the tree and
no consumer that reads it. `"current"` is this repository's existing spelling
of "not standing in a revision"; `"none"` would have invented a distinction
nothing downstream understands.

**Visibility is unchanged.** `_revised_visibility` branches only on
`state == "deleted"`, and neither `False` nor `"current"` is that, so each of
the three records keeps the visibility it had (`annotation`, `annotation`,
`visible`).

Two focused regression tests pin it, both of which fail on the baseline with
`assert {False} == {'current'}`:

- `test_docx_walker.py::test_note_and_comment_furniture_stands_in_an_admitted_revision_state`
- `test_docx_headers_footers.py::test_an_applicability_record_stands_in_an_admitted_revision_state`

Each also asserts every segment of its document stands in a state
`SEGMENT_REVISIONS` admits, so the next producer that invents one is caught
where it is written rather than at publication.

## 3. Verification

| Level | Check | Command | Result |
|---|---|---|---|
| L1 | owned Ruff file | `python -m ruff check tests/test_packaged_runtime.py` | `All checks passed!` |
| L1 | full repository Ruff target (exact `Run Ruff` step) | extracted from `core-acceptance.yml` | `ruff 0.16.1` / `All checks passed!` |
| L1 | owned strict-mypy modules | `python -m mypy --strict .../ooxml_extractors.py .../document_extractors.py` | `Success: no issues found in 2 source files` |
| L1 | full strict-mypy target (exact `Run strict mypy` step) | extracted from `core-acceptance.yml` | `mypy 2.3.0` / `Success: no issues found in 421 source files` |
| L3 | OOXML/document extractor suites (48 files: DOCX, XLSX, PPTX, HTML, delimited, RTF, raster, package validation) | `pytest` | 2409 passed |
| L3 | packaged runtime | `pytest tests/test_packaged_runtime.py` | 32 passed |
| L3 | document intake, worker, knowledge and processing suites | `pytest` | 335 passed, 1 skipped |
| L1 | whitespace | `git diff --check` and `git diff --check be327ce1` | clean |
| L1 | migration allocations | `python scripts/check-migration-allocations.py` | `Migration allocation check passed.` |

The baseline numbers the repair is measured against were taken on `be327ce1`
before any edit: Ruff reported exactly the two `SIM117` findings and strict
mypy exactly the ten above -- the same twelve §4 names.

## 4. Environment evidence: the four CLI lifecycle failures

`packages/omnivia-core-cli/tests/test_lifecycle.py` **passes 4/4 in this
worktree**, both with `.venv/bin` first on `PATH` and without it. The §4
failures did not reproduce here in either configuration, so nothing was
changed to accommodate them.

The hazard §4 named is still present and still foreign:
`~/.local/bin/omnivia-core-service` is a symlink into a uv-managed CPython
whose script imports
`/Users/claytonread/Projects/worktree-omnivia-core-preflight/packages/omnivia-core-runtime/src/omnivia_core_runtime/__init__.py`.
With `.venv/bin` first on `PATH` this worktree's own
`omnivia-core-service` wins the lookup. No production lifecycle code was
touched for it.

## 5. What this record does not claim

- No `L5`/`L6` evidence. Nothing was built as a signed or notarised package,
  installed, activated or run against a live installation in this lane.
- No acceptance-case, work-ID or A-scenario status moves. The
  `2026-10-01-update-qualification-record.md` ledger and the WP-15
  qualification ledger are untouched.
- Finding 2-6 removed dead code; it delivered no capability, so no coverage
  claim changes with it.
- The three record kinds are proven admissible against the real validator in
  isolation (`L3`). No `L4` run in this lane put a DOCX carrying a header
  through a live Runtime and a shipped transport, so the end-to-end
  consequence of the §2 defect having been present is unmeasured here.
- A clean local preflight is local evidence only. The required check
  `Core acceptance` is reported only by the run on the pull request.

## 6. Summary line

The two merge-blocking static gates `Run Ruff` and `Run strict mypy` are clean
on the exact pinned toolchain -- 0 findings against the full CI target lists,
where the baseline had 12. Nine of the twelve were type or style defects over
correct behaviour; three were the visible edge of a fail-closed publication
refusal that would have rejected every DOCX carrying a header, footer, note
separator or authored comment, now repaired and pinned by two tests that fail
without it. Per §10 of the plan, this lane is `L3 component` and no higher.
