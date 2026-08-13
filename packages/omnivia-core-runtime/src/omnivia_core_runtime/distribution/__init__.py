"""Shared, consumer-neutral Core distribution bookkeeping."""

from omnivia_core_runtime.distribution.shared_runtime import (
    COMPANION_BUNDLE_ID,
    CandidateRecord,
    ConsumerReceipt,
    DistributionError,
    SharedRuntimeInstallation,
    canonical_macos_paths,
)

__all__ = [
    "COMPANION_BUNDLE_ID",
    "CandidateRecord",
    "ConsumerReceipt",
    "DistributionError",
    "SharedRuntimeInstallation",
    "canonical_macos_paths",
]
