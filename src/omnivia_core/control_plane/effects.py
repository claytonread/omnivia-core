"""Mapping from control-plane ``SideEffect`` to Host Contract runtime effect classes.

The control plane and the Host Contract each publish their own closed
vocabulary for the same underlying idea -- how consequential an action is.
This module is the single, explicit translation between them; it defines no
new taxonomy and reuses :data:`HOST_OPERATION_EFFECT_CLASSES` verbatim.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from omnivia_core.control_plane.models import SideEffect
from omnivia_core.host_contract.v1.codec import HOST_OPERATION_EFFECT_CLASSES

__all__ = [
    "SIDE_EFFECT_TO_HOST_EFFECT_CLASS",
    "SideEffectMappingError",
    "host_effect_class_for_side_effect",
]


class SideEffectMappingError(ValueError):
    """Raised when a value cannot be mapped to a Host Contract effect class.

    Fails closed: there is no fallback to a weaker effect class for an
    unknown or malformed input.
    """


SIDE_EFFECT_TO_HOST_EFFECT_CLASS: Final[MappingProxyType[SideEffect, str]] = MappingProxyType(
    {
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
)

if set(SIDE_EFFECT_TO_HOST_EFFECT_CLASS) != set(SideEffect):
    raise SideEffectMappingError("SIDE_EFFECT_TO_HOST_EFFECT_CLASS is not exhaustive over SideEffect")
if not set(SIDE_EFFECT_TO_HOST_EFFECT_CLASS.values()) <= set(HOST_OPERATION_EFFECT_CLASSES):
    raise SideEffectMappingError(
        "SIDE_EFFECT_TO_HOST_EFFECT_CLASS contains a value outside HOST_OPERATION_EFFECT_CLASSES"
    )


def host_effect_class_for_side_effect(side_effect: SideEffect | str) -> str:
    """Return the Host Contract effect class for a control-plane ``SideEffect``.

    Accepts either a :class:`SideEffect` member or its exact string value.
    Anything else -- an unknown string, a wrong type, an unrecognised
    member -- raises :class:`SideEffectMappingError` rather than defaulting
    to ``"read"`` or ``"local_mutation"``.
    """
    if type(side_effect) is SideEffect:
        member = side_effect
    elif type(side_effect) is str:
        try:
            member = SideEffect(side_effect)
        except ValueError:
            raise SideEffectMappingError("unknown SideEffect value") from None
    else:
        raise SideEffectMappingError("unsupported SideEffect input type")
    try:
        return SIDE_EFFECT_TO_HOST_EFFECT_CLASS[member]
    except KeyError:
        raise SideEffectMappingError("SideEffect member is not present in the mapping table") from None
