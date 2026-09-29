"""Production handler for ``analysis.start`` (structured-data milestone 1).

`analysis.start` is a refusal contract in this milestone (D-0028, CO-4): the
handler classifies the request through the contract package's strict
classifier and renders the typed outcome. It never enqueues work, never
returns a job reference or queue state, never touches a source adapter,
credential, analytical worker or SQL compiler, and never publishes a result.
Recognising a request means the contract is understood -- it is not a
statement that the referenced analysis is authorised, resolvable or
executable, and every admitted ``use_class`` receives exactly the same
refusal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    classify_analysis_start_request,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError

ANALYSIS_START_OPERATION: Final = "analysis.start"

_MESSAGE_DEPENDENCY: Final = (
    "the analysis request is understood, but no analytical executor is "
    "admitted in this milestone; no job was started"
)
_MESSAGE_INVALID: Final = (
    "the analysis request payload is not valid for this contract version"
)

#: Retry classes per outcome code, mirroring the frozen error catalogue: a
#: dependency refusal is delay-retryable (the contract may gain an executor),
#: while the request-shaped refusals are non-retryable because the identical
#: request fails identically.
_RETRY_CLASSES: Final = {
    ERROR_CODE_DEPENDENCY_UNAVAILABLE: "retryable_after_delay",
}


def analysis_start(context: OperationContext) -> Mapping[str, Any]:
    """Classify one `analysis.start` request and render the typed outcome.

    Zero side effects by construction: the classifier is a pure function over
    the request document, and the only statement this handler makes is the
    typed error. A valid-shape request and a malformed one differ solely in
    the code and retry class they receive.
    """
    code, _detail = classify_analysis_start_request(context.request.input)
    if code == ERROR_CODE_DEPENDENCY_UNAVAILABLE:
        raise OperationError(
            code,
            _MESSAGE_DEPENDENCY,
            retry_class=_RETRY_CLASSES[code],
        )
    raise OperationError(code, _MESSAGE_INVALID)
