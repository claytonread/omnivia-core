"""Cross-language regression corpus for the public Core endpoint URI policy.

One fixture, four surfaces. The canonical JSON Schema, the generated Python
pattern, the generated TypeScript guard and (in `test_service_semantics`) the
Python semantic validator must all return the same verdict for every case here.
The generated `ServiceEndpointUri` pattern is the single authority: no surface is
allowed a post-check of its own, because a check only one runtime can perform is
exactly the drift this corpus exists to catch.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts.v1.generated import SERVICE_ENDPOINT_URI_PATTERN

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"
FIXTURE_PATH = (
    REPO_ROOT
    / "tests"
    / "contracts"
    / "fixtures"
    / "service-endpoint-uri-policy-v1.json"
)
TYPESCRIPT_PATH = (
    REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"
)


def _fixture() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return document


def _endpoint_uri_validator() -> Draft202012Validator:
    entries: list[tuple[str, Resource[Any]]] = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        resource = Resource.from_contents(
            json.loads(path.read_text(encoding="utf-8"))
        )
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Draft202012Validator(
        {"$ref": f"{BASE_URI}service.schema.json#/$defs/ServiceEndpointUri"},
        registry=Registry().with_resources(entries),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


ENDPOINT_URI_VALIDATOR = _endpoint_uri_validator()


def test_endpoint_policy_fixture_is_deterministic_and_has_unique_cases() -> None:
    document = _fixture()
    assert document["format"] == "omnivia.service-endpoint-uri-policy.v1"
    assert FIXTURE_PATH.read_text(encoding="utf-8") == (
        json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    )
    cases = [*document["accepted"], *document["rejected"]]
    ids = [case["id"] for case in cases]
    uris = [case["endpoint_uri"] for case in cases]
    assert len(ids) == len(set(ids))
    assert len(uris) == len(set(uris))


def test_generated_python_pattern_matches_the_endpoint_policy_fixture() -> None:
    pattern = re.compile(SERVICE_ENDPOINT_URI_PATTERN)
    document = _fixture()
    assert all(pattern.fullmatch(case["endpoint_uri"]) for case in document["accepted"])
    assert all(
        pattern.fullmatch(case["endpoint_uri"]) is None
        for case in document["rejected"]
    )


@pytest.mark.parametrize("case", _fixture()["accepted"], ids=lambda case: case["id"])
def test_canonical_schema_accepts_approved_endpoints(case: dict[str, str]) -> None:
    errors = list(ENDPOINT_URI_VALIDATOR.iter_errors(case["endpoint_uri"]))
    assert not errors, f"schema rejected an approved endpoint: {errors}"


@pytest.mark.parametrize("case", _fixture()["rejected"], ids=lambda case: case["id"])
def test_canonical_schema_rejects_unsafe_endpoints(case: dict[str, str]) -> None:
    assert list(ENDPOINT_URI_VALIDATOR.iter_errors(case["endpoint_uri"]))


def test_generated_typescript_exports_the_same_endpoint_policy_pattern() -> None:
    typescript = TYPESCRIPT_PATH.read_text(encoding="utf-8")
    assert "export type ServiceEndpointUri = string;" in typescript
    assert "export function isServiceEndpointUri(" in typescript
    assert "export function assertServiceEndpointUri(" in typescript
    assert "export function isServiceEndpointDescriptorSemanticallyValid(" in typescript
    assert "export function assertServiceEndpointDescriptorSemantics(" in typescript
    assert "export function assertServiceProbeResultSemantics(" in typescript
    declaration = "export const SERVICE_ENDPOINT_URI_PATTERN: string ="
    start = typescript.index(declaration)
    emitted_lines: list[str] = []
    for line in typescript[start:].splitlines():
        emitted_lines.append(line)
        if line.rstrip().endswith('";'):
            break
    pieces = re.findall(r'"(?:\\.|[^"\\])*"', "\n".join(emitted_lines))
    assert "".join(json.loads(piece) for piece in pieces) == SERVICE_ENDPOINT_URI_PATTERN


def test_generated_typescript_executes_the_shared_policy_and_rejects_untyped_dtos(
    tmp_path: Path,
) -> None:
    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    node = shutil.which("node")
    assert tsc.is_file(), "run npm ci so the pinned TypeScript compiler is available"
    assert node is not None, "node is required for the executable TypeScript parity gate"

    subprocess.run(
        [
            str(tsc),
            "--strict",
            "--skipLibCheck",
            "--target",
            "ES2022",
            "--module",
            "commonjs",
            "--outDir",
            str(tmp_path),
            str(TYPESCRIPT_PATH),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    compiled = tmp_path / "index.js"
    runtime_check = """
const fs = require("node:fs");
const contracts = require(process.argv[1]);
const fixture = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
for (const testCase of fixture.accepted) {
  if (!contracts.isServiceEndpointUri(testCase.endpoint_uri)) process.exit(11);
}
for (const testCase of fixture.rejected) {
  if (contracts.isServiceEndpointUri(testCase.endpoint_uri)) process.exit(12);
}
if (contracts.isServiceEndpointUri("https://host.invalid/" + "a".repeat(2048))) {
  process.exit(13);
}
"""
    subprocess.run(
        [node, "-e", runtime_check, str(compiled), str(FIXTURE_PATH)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    type_gate = tmp_path / "consumer.ts"
    module_specifier = json.dumps(str(TYPESCRIPT_PATH))
    type_gate.write_text(
        "\n".join(
            [
                "import {",
                "  assertServiceEndpointDescriptorSemantics,",
                "  assertServiceProbeResultSemantics,",
                f"}} from {module_specifier};",
                "// @ts-expect-error semantic validation requires a structurally decoded DTO",
                'assertServiceEndpointDescriptorSemantics({ endpoint_uri: "pipe://valid" });',
                "// @ts-expect-error semantic validation requires a structurally decoded result",
                "assertServiceProbeResultSemantics({});",
                "",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(tsc),
            "--strict",
            "--noEmit",
            "--skipLibCheck",
            # The consumer imports the generated source by absolute path, so the
            # specifier ends in `.ts`; without this tsc stops at TS5097 before it
            # ever evaluates the `@ts-expect-error` directives below.
            "--allowImportingTsExtensions",
            "--target",
            "ES2022",
            "--moduleResolution",
            "Bundler",
            "--module",
            "ESNext",
            str(type_gate),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
