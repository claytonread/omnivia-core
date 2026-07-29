"""Memory lifecycle states and rules.

Lifecycle states control how AI-created knowledge becomes approved fact.
Agent-created memories start as "proposed" so they cannot silently turn
unverified observations into approved business truth.

State transitions:
    proposed -> observed (automatic on source verification)
    proposed -> approved (human approval required)
    observed -> approved (human approval required)
    any -> rejected (human decision)
"""

from __future__ import annotations

from enum import Enum


class LifecycleState(str, Enum):
    """The approval state of a memory.

    Memoires move through these states based on verification and human approval:

    - proposed: AI-created, unverified. Default for agent-created memories.
    - observed: Partially validated, needs more evidence or human review.
    - approved: Validated by human or sufficient source evidence.
    - rejected: Explicitly disproved or removed from consideration.
    """

    PROPOSED = "proposed"
    OBSERVED = "observed"
    APPROVED = "approved"
    REJECTED = "rejected"

    def can_transition_to(self, target: LifecycleState) -> bool:
        """Check if transition from current state to target is valid.

        Args:
            target: The state to transition to

        Returns:
            True if the transition is allowed under lifecycle rules
        """
        # Rejected is a terminal state - nothing can leave it
        if self == LifecycleState.REJECTED:
            return False

        # From rejected, only terminal (staying rejected) is valid, which is
        # handled above, so no valid transitions from rejected

        # From proposed, can go to observed, approved, or rejected
        if self == LifecycleState.PROPOSED:
            return target in (
                LifecycleState.OBSERVED,
                LifecycleState.APPROVED,
                LifecycleState.REJECTED,
            )

        # From observed, can go to approved or rejected
        if self == LifecycleState.OBSERVED:
            return target in (
                LifecycleState.APPROVED,
                LifecycleState.REJECTED,
            )

        # From approved, can only go to rejected (retraction)
        if self == LifecycleState.APPROVED:
            return target == LifecycleState.REJECTED

        return False

    def is_terminal(self) -> bool:
        """Check if this is a terminal state (no further transitions)."""
        return self == LifecycleState.REJECTED

    def is_approved(self) -> bool:
        """Check if this state represents approved knowledge."""
        return self == LifecycleState.APPROVED

    def requires_human_approval_to_approve(self) -> bool:
        """Check if human approval is required to reach approved state.

        Returns True for proposed->approved, False for observed->approved
        which may happen automatically after additional evidence.
        """
        return self == LifecycleState.PROPOSED
