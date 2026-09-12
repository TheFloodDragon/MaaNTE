"""Combat policy implementations."""

from __future__ import annotations

import time
from typing import Optional, Protocol

from .models import (
    ActionIntent,
    CombatSession,
    CombatSnapshot,
    Phase,
    SoundEvent,
)


class CombatPolicy(Protocol):
    """Protocol for combat decision policies."""

    def decide(
        self, session: CombatSession, snapshot: CombatSnapshot
    ) -> Optional[ActionIntent]:
        """Decide next action based on current state.

        Returns:
            ActionIntent if an action should be taken, None to wait for new observation.
        """
        ...


class BasicCombatPolicy:
    """Basic policy: normal attack with current character only."""

    def __init__(self, settings: dict):
        self.attack_interval = settings.get("attack_interval_ms", 800) / 1000.0
        self.last_attack = 0.0

    def decide(
        self, session: CombatSession, snapshot: CombatSnapshot
    ) -> Optional[ActionIntent]:
        """Basic attack with rate limiting."""
        if not snapshot.valid or not snapshot.combat:
            return None

        now = time.monotonic()
        if now - self.last_attack < self.attack_interval:
            return None

        self.last_attack = now
        return ActionIntent(action="normal_attack")


class BalancedCombatPolicy:
    """Balanced policy: ultimate > skill > normal attack priority."""

    def __init__(self, settings: dict):
        self.use_ultimate = settings.get("use_ultimate", True)
        self.use_skill = settings.get("use_skill", True)
        self.attack_interval = settings.get("attack_interval_ms", 800) / 1000.0
        self.last_attack = 0.0

    def decide(
        self, session: CombatSession, snapshot: CombatSnapshot
    ) -> Optional[ActionIntent]:
        """Priority-based attack selection."""
        if not snapshot.valid or not snapshot.combat:
            return None

        # Ultimate if ready
        if self.use_ultimate and snapshot.ultimate_ready:
            return ActionIntent(action="ultimate")

        # Skill if ready
        if self.use_skill and snapshot.skill_ready:
            return ActionIntent(action="skill")

        # Normal attack with rate limit
        now = time.monotonic()
        if now - self.last_attack < self.attack_interval:
            return None

        self.last_attack = now
        return ActionIntent(action="normal_attack")


class CustomCombatPolicy:
    """Custom template-based policy with step execution and cycle tracking."""

    def __init__(self, template, settings: dict):
        """Initialize policy with loaded template.

        Args:
            template: CustomTemplate instance
            settings: Combat settings including role slot mappings
        """
        self.template = template
        self.settings = settings

        # Execution state
        self.current_step = 0
        self.waiting_for_confirmation = False
        self.step_started_at: Optional[float] = None

        # Role slot resolution
        self.role_slots = {}
        for role_name, config_slot in template.roles.items():
            # Allow settings to override template role assignments
            self.role_slots[role_name] = settings.get(f"role_{role_name}_slot", config_slot)

    def decide(
        self, session: CombatSession, snapshot: CombatSnapshot
    ) -> Optional[ActionIntent]:
        """Execute template steps in order with cycle management."""
        if not snapshot.valid:
            return None

        # Check if we're waiting for pending action confirmation
        if session.pending:
            return None  # Wait for confirmation

        # Check if template is complete
        if self.current_step >= len(self.template.steps):
            return self._handle_completion(session)

        # Execute current step
        step = self.template.steps[self.current_step]
        intent = self._build_intent(step, snapshot)

        if intent is None:
            # Condition not met, check if we should skip or wait
            if step.get("when") and snapshot.combat is None:
                # Unknown state, wait for better observation
                return None
            else:
                # Condition explicitly not met, skip to next step
                self.current_step += 1
                return self.decide(session, snapshot)  # Try next step immediately

        # Mark step start time
        if self.step_started_at is None:
            self.step_started_at = time.monotonic()

        return intent

    def _build_intent(self, step: dict, snapshot: CombatSnapshot) -> Optional[ActionIntent]:
        """Build ActionIntent from template step.

        Returns:
            ActionIntent if step should execute, None if condition not met.
        """
        action = step["action"]

        # Check condition
        condition = step.get("when")
        if condition and not self._check_condition(condition, snapshot):
            return None

        # Build intent based on action type
        if action == "switch":
            role = step["role"]
            slot = self.role_slots.get(role)
            if slot is None:
                # Role not configured, fail if required
                if step.get("required", False):
                    return ActionIntent(
                        action="invalid",
                        required=True,
                    )
                return None  # Skip non-required switch with missing role

            return ActionIntent(
                action="switch",
                slot=slot,
                required=step.get("required", False),
                condition=condition,
            )

        elif action == "charged_attack":
            return ActionIntent(
                action="charged_attack",
                duration_ms=step.get("duration_ms", 1000),
                required=step.get("required", False),
                condition=condition,
            )

        elif action == "wait":
            # Wait action: just return None to continue observing
            timeout = step.get("timeout_ms", 1000) / 1000.0
            if self.step_started_at and time.monotonic() - self.step_started_at >= timeout:
                self.current_step += 1
                self.step_started_at = None
            return None

        else:
            # Normal attack, skill, ultimate, dodge
            return ActionIntent(
                action=action,
                required=step.get("required", False),
                condition=condition,
            )

    def _check_condition(self, condition: str, snapshot: CombatSnapshot) -> bool:
        """Check if condition is met.

        Returns:
            True if met, False if explicitly not met or unknown.
        """
        if condition == "always":
            return True
        elif condition == "skill_ready":
            return snapshot.skill_ready is True
        elif condition == "ultimate_ready":
            return snapshot.ultimate_ready is True
        elif condition == "in_combat":
            return snapshot.combat is True
        elif condition == "slot_alive":
            # Would need slot parameter, simplified for now
            return True
        return False

    def _handle_completion(self, session: CombatSession) -> Optional[ActionIntent]:
        """Handle template completion based on on_finish setting."""
        if self.template.on_finish == "finish":
            # Mark session as complete
            session.finish(Phase.ENDED, "template_completed")
            return None

        elif self.template.on_finish == "reobserve":
            # Check cycle limit
            if session.cycles_completed >= self.template.max_cycles:
                session.finish(Phase.ENDED, "cycle_limit_reached")
                return None

            # Reset for next cycle
            self.current_step = 0
            self.step_started_at = None
            session.cycles_completed += 1
            return None  # Get new observation before starting next cycle

        return None

    def on_action_result(self, intent: ActionIntent, succeeded: bool) -> None:
        """Handle action result and advance step if appropriate.

        Args:
            intent: The action that was executed
            succeeded: Whether it succeeded
        """
        if succeeded:
            # Move to next step
            self.current_step += 1
            self.step_started_at = None
        elif intent.required:
            # Required step failed, policy cannot continue
            # Session will be marked as failed by coordinator
            pass


def update_session_phase(
    session: CombatSession, snapshot: CombatSnapshot, settings: dict
) -> None:
    """Update session phase based on current snapshot and timers.

    Args:
        session: Combat session to update
        snapshot: Current observation
        settings: Combat settings with timeout values
    """
    now = time.monotonic()

    # Timeout values from settings
    enter_timeout = settings.get("enter_combat_timeout_ms", 10000) / 1000.0
    clear_confirm_time = settings.get("clear_confirm_ms", 3000) / 1000.0
    uncertain_timeout = settings.get("uncertain_timeout_ms", 15000) / 1000.0
    max_combat_duration = settings.get("max_combat_duration_ms", 300000) / 1000.0

    if session.terminal:
        return  # Already finished

    # WAITING -> ACTIVE or timeout
    if session.phase == Phase.WAITING:
        if snapshot.valid and snapshot.combat is True:
            session.phase = Phase.ACTIVE
            session.entered_at = now
            session.uncertain_since = None
            session.clear_since = None
        elif now - session.started_at > enter_timeout:
            session.finish(Phase.FAILED, "enter_timeout")
        return

    # ACTIVE combat phase
    if session.phase == Phase.ACTIVE:
        if not snapshot.valid:
            # Invalid snapshot, enter uncertain
            if session.uncertain_since is None:
                session.uncertain_since = now
            session.phase = Phase.UNCERTAIN
            return

        if snapshot.combat is True:
            # Still in combat, reset clear timer
            session.clear_since = None
            session.uncertain_since = None

            # Check max duration
            if session.entered_at and now - session.entered_at > max_combat_duration:
                session.finish(Phase.FAILED, "max_duration_exceeded")
            return

        if snapshot.combat is False:
            # Combat clear detected
            if session.clear_since is None:
                session.clear_since = now
            elif now - session.clear_since >= clear_confirm_time:
                # Confirmed clear
                session.finish(Phase.ENDED, "combat_ended")
            return

        # snapshot.combat is None (unknown)
        if session.uncertain_since is None:
            session.uncertain_since = now
        session.phase = Phase.UNCERTAIN
        return

    # UNCERTAIN state
    if session.phase == Phase.UNCERTAIN:
        if snapshot.valid and snapshot.combat is True:
            # Recovered, back to active
            session.phase = Phase.ACTIVE
            session.uncertain_since = None
            session.clear_since = None
            return

        if snapshot.valid and snapshot.combat is False:
            # Detected clear while uncertain
            session.finish(Phase.ENDED, "combat_ended")
            return

        # Still uncertain
        if session.uncertain_since and now - session.uncertain_since > uncertain_timeout:
            session.finish(Phase.FAILED, "uncertain_timeout")
        return
