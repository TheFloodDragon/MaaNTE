"""Combat state observer that generates snapshots from controller screenshots."""

from __future__ import annotations

import time
from typing import Optional

from .models import CombatSnapshot

# Public recognition node defined in pipeline/Combat/CombatStatus.json.
ENEMY_HEALTH_BAR_NODE = "CombatEnemyHealthBar"


class CombatObserver:
    """Observes game state and generates snapshots with evidence.

    Unknown states are reported as ``None`` rather than ``False`` so that
    policies never treat "could not tell" as "confirmed absent".
    """

    def __init__(self, context):
        self.context = context
        self.frame_counter = 0

    # ------------------------------------------------------------------
    # Screenshot acquisition
    # ------------------------------------------------------------------
    def observe(self) -> CombatSnapshot:
        """Capture a fresh frame from the controller and analyze it."""
        self.frame_counter += 1
        captured_at = time.monotonic()

        controller = getattr(getattr(self.context, "tasker", None), "controller", None)
        if controller is None:
            return self._invalid(captured_at, "no_controller")

        try:
            job = controller.post_screencap()
            job.wait()
            if not getattr(job, "succeeded", False):
                return self._invalid(captured_at, "screencap_failed")
            image = job.get()
        except Exception as exc:  # controller errors must not kill the loop
            return self._invalid(captured_at, "screencap_error", str(exc))

        if image is None or getattr(image, "size", 0) == 0:
            return self._invalid(captured_at, "screencap_empty")

        return self.analyze(image, captured_at=captured_at)

    def analyze(self, image, captured_at: Optional[float] = None) -> CombatSnapshot:
        """Analyze an already captured image (used by custom recognition)."""
        if captured_at is None:
            self.frame_counter += 1
            captured_at = time.monotonic()

        evidence: list[str] = []
        combat = self._check_combat(image, evidence)

        return CombatSnapshot(
            frame_id=self.frame_counter,
            captured_at=captured_at,
            valid=True,
            combat=combat,
            # The following signals are not implemented yet and are reported
            # as unknown on purpose; see docs for the acceptance checklist.
            in_team=None,
            focused=None,
            loading=False,
            defeated=False,
            current_slot=None,
            alive={},
            skill_ready=None,
            ultimate_ready=None,
            evidence=tuple(evidence),
        )

    # ------------------------------------------------------------------
    # Individual signals
    # ------------------------------------------------------------------
    def _check_combat(self, image, evidence: list[str]) -> Optional[bool]:
        """Detect enemy health bars; None when recognition itself fails."""
        try:
            result = self.context.run_recognition(ENEMY_HEALTH_BAR_NODE, image)
        except Exception as exc:
            evidence.append("combat:error:%s" % exc)
            return None
        if result is None:
            evidence.append("combat:unknown")
            return None
        if getattr(result, "hit", False):
            evidence.append("combat:enemy_healthbar")
            return True
        evidence.append("combat:no_enemy")
        return False

    def _invalid(self, captured_at: float, *evidence: str) -> CombatSnapshot:
        return CombatSnapshot(
            frame_id=self.frame_counter,
            captured_at=captured_at,
            valid=False,
            evidence=tuple(evidence),
        )
