"""Focused acceptance for ``omnivia_core.control_plane.effects``.

RT-201: the smallest closed mapping from the control-plane ``SideEffect``
taxonomy to the Host Contract runtime effect classes. This proves exhaustive
enum coverage, the exact required mapping, that every mapped value belongs to
``HOST_OPERATION_EFFECT_CLASSES``, immutability of the mapping table, and
fail-closed refusal of unknown or malformed input.
"""

from __future__ import annotations

import subprocess
import sys
from types import MappingProxyType
from typing import Any

import pytest

from omnivia_core.control_plane.effects import (
    SIDE_EFFECT_TO_HOST_EFFECT_CLASS,
    SideEffectMappingError,
    host_effect_class_for_side_effect,
)
from omnivia_core.control_plane.models import SideEffect
from omnivia_core.host_contract.v1.codec import HOST_OPERATION_EFFECT_CLASSES

REQUIRED_MAPPING = {
    SideEffect.NONE: "read",
    SideEffect.READ: "read",
    SideEffect.LOCAL_WRITE: "local_mutation",
    SideEffect.EXTERNAL_WRITE: "consequential_external",
    SideEffect.SEND: "consequential_external",
    SideEffect.PUBLISH: "consequential_external",
    SideEffect.DESTRUCTIVE: "consequential_external",
    SideEffect.FINANCIAL: "consequential_external",
    SideEffect.ADMIN: "consequential_external",
}


def test_mapping_covers_every_side_effect_member_exactly() -> None:
    assert set(SIDE_EFFECT_TO_HOST_EFFECT_CLASS) == set(SideEffect)
    assert set(REQUIRED_MAPPING) == set(SideEffect)


def test_mapping_matches_required_table_exactly() -> None:
    assert dict(SIDE_EFFECT_TO_HOST_EFFECT_CLASS) == REQUIRED_MAPPING


def test_mapping_values_belong_to_host_operation_effect_classes() -> None:
    assert set(SIDE_EFFECT_TO_HOST_EFFECT_CLASS.values()) <= set(HOST_OPERATION_EFFECT_CLASSES)


def test_mapping_table_is_immutable() -> None:
    assert type(SIDE_EFFECT_TO_HOST_EFFECT_CLASS) is MappingProxyType
    with pytest.raises(TypeError):
        SIDE_EFFECT_TO_HOST_EFFECT_CLASS[SideEffect.READ] = "local_mutation"  # type: ignore[index]


@pytest.mark.parametrize(("side_effect", "expected"), sorted(REQUIRED_MAPPING.items(), key=str))
def test_host_effect_class_for_side_effect_enum_member(
    side_effect: SideEffect, expected: str
) -> None:
    assert host_effect_class_for_side_effect(side_effect) == expected


@pytest.mark.parametrize(("side_effect", "expected"), sorted(REQUIRED_MAPPING.items(), key=str))
def test_host_effect_class_for_side_effect_string_value(
    side_effect: SideEffect, expected: str
) -> None:
    assert host_effect_class_for_side_effect(side_effect.value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "unknown_effect",
        "",
        "READ",
        None,
        42,
        1.0,
        ["read"],
        {"value": "read"},
    ],
)
def test_host_effect_class_for_side_effect_fails_closed_on_unknown_input(
    value: Any,
) -> None:
    with pytest.raises(SideEffectMappingError):
        host_effect_class_for_side_effect(value)


@pytest.mark.parametrize(
    "value",
    [
        "unknown_effect_with_a_secret_token_abc123",
        {"value": "read", "secret": "token"},
    ],
)
def test_error_message_does_not_echo_malformed_value(value: Any) -> None:
    with pytest.raises(SideEffectMappingError) as exc_info:
        host_effect_class_for_side_effect(value)
    message = str(exc_info.value)
    assert "secret" not in message
    assert "abc123" not in message
    assert repr(value) not in message


def test_module_import_and_fail_closed_error_path_survive_optimized_execution() -> None:
    """Exercise the module under ``python -O`` in a subprocess.

    Proves the module-level exhaustiveness guard still runs on import (it is
    an explicit ``if ...: raise``, not an ``assert``, which ``-O`` strips)
    and that the runtime fail-closed error path still raises. Uses an
    explicit ``if ...: raise SystemExit(...)`` rather than ``assert`` for the
    success-path check, since that check itself runs under ``-O``.
    """
    script = (
        "from omnivia_core.control_plane.effects import host_effect_class_for_side_effect, "
        "SideEffectMappingError\n"
        "if host_effect_class_for_side_effect('admin') != 'consequential_external':\n"
        "    raise SystemExit('unexpected effect class for admin')\n"
        "try:\n"
        "    host_effect_class_for_side_effect('not_a_real_side_effect')\n"
        "except SideEffectMappingError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('expected SideEffectMappingError under -O')\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
