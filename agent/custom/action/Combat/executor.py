"""Single input gateway for combat with stop checks and guaranteed release."""

from __future__ import annotations

import time
from typing import Optional

from .models import ActionIntent, ActionResult, Outcome

# Virtual key codes (Win32).
VK_SHIFT = 0xA0
VK_E = 0x45
VK_F = 0x46
VK_SLOT = {1: 0x31, 2: 0x32, 3: 0x33, 4: 0x34}

# Attack target at the 1280x720 baseline; the controller scales it.
ATTACK_POINT = (640, 360)
HOLD_CHUNK_MS = 50


class CombatExecutor:
    """Executes combat intents via the Maa controller.

    Every method checks the tasker stop flag before touching the controller,
    and any input that is held (touch down / key down) is tracked so that
    ``release_all()`` can restore a clean state on exit or error.
    """

    def __init__(self, context):
        self.context = context
        self._held_keys: set[int] = set()
        self._touch_held = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def is_stopping(self) -> bool:
        tasker = getattr(self.context, "tasker", None)
        if tasker is None:
            return False
        stopping = getattr(tasker, "stopping", False)
        if callable(stopping):
            stopping = stopping()
        return bool(stopping)

    def _controller(self):
        tasker = getattr(self.context, "tasker", None)
        return getattr(tasker, "controller", None) if tasker is not None else None

    @staticmethod
    def _run(job) -> bool:
        """Wait for a controller job and report whether it succeeded."""
        if job is None:
            return False
        job.wait()
        return bool(getattr(job, "succeeded", False))

    def _click_key(self, vk: int) -> bool:
        controller = self._controller()
        if controller is None:
            return False
        try:
            return self._run(controller.post_click_key(vk))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.is_stopping():
            return ActionResult(outcome=Outcome.BLOCKED, reason="stopping")
        if self._controller() is None:
            return ActionResult(outcome=Outcome.FAILED, reason="no_controller")

        handler = {
            "switch": self._switch_character,
            "normal_attack": self._normal_attack,
            "charged_attack": self._charged_attack,
            "skill": self._skill,
            "ultimate": self._ultimate,
            "dodge": self._dodge,
        }.get(intent.action)
        if handler is None:
            return ActionResult(outcome=Outcome.FAILED, reason="unknown_action:%s" % intent.action)
        return handler(intent)

    def release_all(self) -> None:
        """Release every input this executor may still be holding."""
        controller = self._controller()
        if controller is None:
            self._held_keys.clear()
            self._touch_held = False
            return

        if self._touch_held:
            try:
                controller.post_touch_up().wait()
            except Exception:
                pass
            self._touch_held = False

        for vk in list(self._held_keys):
            try:
                controller.post_key_up(vk).wait()
            except Exception:
                pass
        self._held_keys.clear()

    # ------------------------------------------------------------------
    # Intents
    # ------------------------------------------------------------------
    def _switch_character(self, intent: ActionIntent) -> ActionResult:
        vk = VK_SLOT.get(intent.slot) if intent.slot is not None else None
        if vk is None:
            return ActionResult(outcome=Outcome.FAILED, reason="invalid_slot")
        if not self._click_key(vk):
            return ActionResult(outcome=Outcome.FAILED, reason="controller_failed")
        # Sending the key proves nothing; confirmation needs a newer frame.
        return ActionResult(outcome=Outcome.PENDING, reason="switch_sent")

    def _normal_attack(self, intent: ActionIntent) -> ActionResult:
        controller = self._controller()
        try:
            ok = self._run(controller.post_click(*ATTACK_POINT))
        except Exception as exc:
            return ActionResult(outcome=Outcome.FAILED, reason="click_error:%s" % exc)
        if not ok:
            return ActionResult(outcome=Outcome.FAILED, reason="click_failed")
        return ActionResult(outcome=Outcome.SUCCEEDED, reason="attack_sent")

    def _charged_attack(self, intent: ActionIntent) -> ActionResult:
        controller = self._controller()
        duration_ms = intent.duration_ms or 600
        x, y = ATTACK_POINT
        try:
            if not self._run(controller.post_touch_down(x, y)):
                return ActionResult(outcome=Outcome.FAILED, reason="touch_down_failed")
            self._touch_held = True

            held = 0
            while held < duration_ms:
                if self.is_stopping():
                    self.release_all()
                    return ActionResult(outcome=Outcome.BLOCKED, reason="stopping")
                step = min(HOLD_CHUNK_MS, duration_ms - held)
                time.sleep(step / 1000.0)
                held += step

            released = self._run(controller.post_touch_up())
            self._touch_held = False
            if not released:
                return ActionResult(outcome=Outcome.FAILED, reason="touch_up_failed")
            return ActionResult(outcome=Outcome.SUCCEEDED, reason="charged_attack_sent")
        except Exception as exc:
            self.release_all()
            return ActionResult(outcome=Outcome.FAILED, reason="charged_error:%s" % exc)

    def _skill(self, intent: ActionIntent) -> ActionResult:
        if not self._click_key(VK_E):
            return ActionResult(outcome=Outcome.FAILED, reason="controller_failed")
        return ActionResult(outcome=Outcome.PENDING, reason="skill_sent")

    def _ultimate(self, intent: ActionIntent) -> ActionResult:
        if not self._click_key(VK_F):
            return ActionResult(outcome=Outcome.FAILED, reason="controller_failed")
        return ActionResult(outcome=Outcome.PENDING, reason="ultimate_sent")

    def _dodge(self, intent: ActionIntent) -> ActionResult:
        if not self._click_key(VK_SHIFT):
            return ActionResult(outcome=Outcome.FAILED, reason="controller_failed")
        time.sleep(0.1)
        if self.is_stopping():
            return ActionResult(outcome=Outcome.BLOCKED, reason="stopping")
        self._click_key(VK_SHIFT)
        return ActionResult(outcome=Outcome.SUCCEEDED, reason="dodge_sent")
