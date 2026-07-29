"""Lifecycle transition rules for memory approval workflow.

These rules implement the governance model where AI-created knowledge
must be reviewed before becoming approved business truth.
"""

from __future__ import annotations

from enum import Enum

from omnivia_core.lifecycle.models import LifecycleState


class CreatedBy(str, Enum):
    """Who or what created a memory.

    Used to determine the initial lifecycle state:
    - human: Created directly by a human, starts as approved
    - agent: Created by an AI agent, starts as proposed
    """

    HUMAN = "human"
    AGENT = "agent"


class LifecycleRules:
    """Rules for memory lifecycle transitions.

    Implements the governance model where:
    - Human-created memories start as approved (trusted source)
    - Agent-created memories start as proposed (need review)
    """

    @staticmethod
    def get_initial_state(created_by: CreatedBy) -> LifecycleState:
        """Get the initial lifecycle state for a new memory.

        Human-created memories are trusted and start approved.
        Agent-created memories need verification and start proposed.

        Args:
            created_by: Whether a human or agent created the memory

        Returns:
            The appropriate initial lifecycle state
        """
        if created_by == CreatedBy.HUMAN:
            return LifecycleState.APPROVED
        return LifecycleState.PROPOSED

    @staticmethod
    def can_transition(
        current_state: LifecycleState,
        target_state: LifecycleState,
    ) -> bool:
        """Check if a lifecycle transition is allowed.

        Args:
            current_state: The current state of the memory
            target_state: The desired target state

        Returns:
            True if the transition is allowed
        """
        return current_state.can_transition_to(target_state)

    @staticmethod
    def requires_human_approval(current_state: LifecycleState) -> bool:
        """Check if human approval is required to reach approved state.

        Args:
            current_state: The current state of the memory

        Returns:
            True if a human must explicitly approve to reach approved state
        """
        return current_state.requires_human_approval_to_approve()

    @staticmethod
    def is_approved_state(state: LifecycleState) -> bool:
        """Check if a state is approved.

        Args:
            state: The state to check

        Returns:
            True if the state is approved
        """
        return state.is_approved()

    @staticmethod
    def is_terminal_state(state: LifecycleState) -> bool:
        """Check if a state is terminal (no further transitions possible).

        Args:
            state: The state to check

        Returns:
            True if the state is terminal
        """
        return state.is_terminal()
