"""MaaFramework action bridge for the combat session loop.

Pipeline contract (see ``pipeline/Combat/Combat.json``):

    CombatMain (CombatInitAction)
      -> CombatLoop (CombatStepAction, one observe/decide/execute cycle)
           -> CombatSessionEnded (custom recognition ``session_terminal``)
                -> CombatFinalizeAction
           -> CombatLoop

The step action never reports failure just because the session ended; the
recognition branch decides when to leave the loop. Reporting failure would
route into ``on_error`` instead of the finalize node.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils.logger import logger
from utils.maafocus import PrintT

from .executor import CombatExecutor
from .models import (
    ActionIntent,
    CombatSession,
    CombatSnapshot,
    Outcome,
    PendingAction,
    Phase,
)
from .observer import CombatObserver
from .policies import (
    BalancedCombatPolicy,
    BasicCombatPolicy,
    CustomCombatPolicy,
    update_session_phase,
)
from .sound_events import SoundEventSource
from .templates import TemplateError, load_template

CONFIG_NODE = "CombatMain"
DODGE_SAMPLE = "dodge.wav"

# One session per agent process; the pipeline runs a single combat at a time.
_session: Optional[CombatSession] = None
_observer: Optional[CombatObserver] = None
_executor: Optional[CombatExecutor] = None
_policy: Any = None
_sound: Optional[SoundEventSource] = None
_sound_started = False
_sound_disabled_reason = ""


def active_session() -> Optional[CombatSession]:
    return _session


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
    return default


def _as_number(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_settings(context: Context, argv: CustomAction.RunArg) -> dict:
    """Merge node ``attach`` (task option overrides) with custom_action_param."""
    settings: dict = {}
    try:
        node_data = context.get_node_data(CONFIG_NODE) or {}
        attach = node_data.get("attach") if isinstance(node_data, dict) else None
        if isinstance(attach, dict):
            settings.update(attach)
    except Exception as exc:
        logger.warning("Combat: failed to read %s attach: %s", CONFIG_NODE, exc)

    raw = getattr(argv, "custom_action_param", None)
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                settings.update(parsed)
        except (TypeError, ValueError) as exc:
            logger.warning("Combat: invalid custom_action_param %r: %s", raw, exc)

    # Normalise user-facing values into the units used internally.
    settings["use_ultimate"] = _as_bool(settings.get("use_ultimate"), True)
    settings["use_skill"] = _as_bool(settings.get("use_skill"), True)
    settings["enable_sound"] = _as_bool(settings.get("enable_sound"), False)
    settings["attack_interval_ms"] = int(_as_number(settings.get("attack_interval_ms"), 800))
    settings["sound_threshold"] = _as_number(settings.get("sound_threshold"), 0.13)
    settings["confirm_timeout_ms"] = int(_as_number(settings.get("confirm_timeout_ms"), 2000))
    duration_s = _as_number(settings.get("max_combat_duration_s"), 300)
    settings["max_combat_duration_ms"] = int(duration_s * 1000)
    return settings


# ----------------------------------------------------------------------
# Session lifecycle
# ----------------------------------------------------------------------
def _build_policy(settings: dict):
    policy_type = str(settings.get("policy_type", "balanced")).lower()
    if policy_type == "basic":
        return BasicCombatPolicy(settings)
    if policy_type == "balanced":
        return BalancedCombatPolicy(settings)
    if policy_type == "custom":
        template_name = str(settings.get("custom_template", "custom_example.json"))
        template = load_template(template_name)
        return CustomCombatPolicy(template, settings)
    raise ValueError("unknown policy_type: %s" % policy_type)


def _start_session(context: Context, settings: dict) -> CombatSession:
    global _session, _observer, _executor, _policy, _sound, _sound_started, _sound_disabled_reason

    _cleanup(context)

    session = CombatSession(
        session_id=uuid.uuid4().hex[:8],
        settings=settings,
        started_at=time.monotonic(),
    )
    _observer = CombatObserver(context)
    _executor = CombatExecutor(context)
    _policy = _build_policy(settings)
    _sound = SoundEventSource() if settings["enable_sound"] else None
    _sound_started = False
    _sound_disabled_reason = ""
    _session = session
    return session


def _cleanup(context: Optional[Context]) -> None:
    """Release inputs and stop audio. Safe to call repeatedly."""
    global _session, _observer, _executor, _policy, _sound, _sound_started

    if _executor is not None:
        try:
            _executor.release_all()
        except Exception as exc:
            logger.error("Combat: release_all failed: %s", exc)

    if _sound is not None:
        ok, reason = _sound.stop()
        _sound.clear_events()
        if not ok:
            logger.error("Combat: sound listener stop failed: %s", reason)
            if context is not None:
                PrintT(context, "combat.sound_health_degraded", reason)

    _session = None
    _observer = None
    _executor = None
    _policy = None
    _sound = None
    _sound_started = False


def _ensure_sound(context: Context, session: CombatSession) -> None:
    """Start the listener once combat is active; degrade loudly on failure."""
    global _sound_started, _sound_disabled_reason

    if _sound is None or _sound_disabled_reason:
        return

    if not _sound_started:
        ok, reason = _sound.start(
            session_id=session.session_id,
            sample_name=DODGE_SAMPLE,
            counter_name=None,
            threshold=session.settings["sound_threshold"],
            counter_threshold=0.12,
            stop_check=lambda: bool(context.tasker.stopping),
        )
        if ok:
            _sound_started = True
        else:
            _sound_disabled_reason = reason
            logger.warning("Combat: sound defense disabled: %s", reason)
            PrintT(context, "combat.sound_health_degraded", reason)
        return

    if not _sound.is_healthy():
        _sound_disabled_reason = "listener_died"
        _sound.clear_events()
        logger.warning("Combat: sound listener died during session")
        PrintT(context, "combat.sound_health_degraded", _sound_disabled_reason)


def _handle_sound_event(session: CombatSession, snapshot: CombatSnapshot) -> None:
    """Execute at most one still-valid dodge event for this session."""
    if _sound is None or not _sound_started or _sound_disabled_reason:
        return
    event = _sound.poll_event()
    if event is None or event.session_id != session.session_id:
        return
    if not snapshot.valid or snapshot.combat is not True:
        return
    if session.pending is not None:
        return
    result = _executor.execute(ActionIntent(action="dodge", required=False))
    logger.debug("Combat: dodge on sound event -> %s (%s)", result.outcome.value, result.reason)


# ----------------------------------------------------------------------
# Pending-action confirmation
# ----------------------------------------------------------------------
def _confirm_pending(session: CombatSession, snapshot: CombatSnapshot) -> Optional[bool]:
    """Return True/False once the pending action is confirmed/refuted, None to keep waiting."""
    pending = session.pending
    if pending is None or not snapshot.valid:
        return None
    if snapshot.frame_id <= pending.issued_frame:
        return None  # need a frame captured after the input was sent

    intent = pending.intent
    if intent.action == "switch":
        if snapshot.current_slot is None:
            return None
        return snapshot.current_slot == intent.slot
    if intent.action == "skill":
        if snapshot.skill_ready is None:
            return None
        return snapshot.skill_ready is False  # went on cooldown
    if intent.action == "ultimate":
        if snapshot.ultimate_ready is None:
            return None
        return snapshot.ultimate_ready is False
    return True


def _resolve_pending(session: CombatSession, snapshot: CombatSnapshot) -> bool:
    """Advance the pending action. Returns False when the session must stop."""
    pending = session.pending
    if pending is None:
        return True

    confirmed = _confirm_pending(session, snapshot)
    if confirmed is None:
        if time.monotonic() < pending.deadline:
            return True  # keep waiting for a decisive observation
        confirmed = False
        reason = "confirm_timeout"
    else:
        reason = "confirmed" if confirmed else "refuted"

    session.pending = None
    logger.debug("Combat: pending %s -> %s", pending.intent.action, reason)

    if hasattr(_policy, "on_action_result"):
        _policy.on_action_result(pending.intent, succeeded=confirmed)

    if not confirmed and pending.intent.required:
        session.finish(Phase.FAILED, "required_action_failed:%s:%s" % (pending.intent.action, reason))
        return False
    return True


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------
@AgentServer.custom_action("CombatInitAction")
class CombatInitAction(CustomAction):
    """Create the combat session from task options and validate the template."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        settings = _read_settings(context, argv)
        try:
            session = _start_session(context, settings)
        except TemplateError as exc:
            logger.error("Combat: template rejected: %s", exc)
            PrintT(context, "combat.template_invalid", str(exc))
            return CustomAction.RunResult(success=False)
        except Exception as exc:
            logger.error("Combat: session init failed: %s", exc)
            PrintT(context, "combat.template_invalid", str(exc))
            return CustomAction.RunResult(success=False)

        logger.info(
            "Combat: session %s policy=%s sound=%s",
            session.session_id,
            settings.get("policy_type", "balanced"),
            settings["enable_sound"],
        )
        PrintT(context, "combat.session_start")
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("CombatStepAction")
class CombatStepAction(CustomAction):
    """Run one observe -> decide -> execute cycle."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        session = _session
        if session is None or _observer is None or _executor is None or _policy is None:
            logger.error("Combat: step called without an active session")
            return CustomAction.RunResult(success=False)

        try:
            self._step(context, session)
        except Exception as exc:
            logger.error("Combat: step failed: %s", exc)
            session.finish(Phase.FAILED, "step_exception")
            _executor.release_all()
        # Loop exit is decided by the CombatSessionEnded recognition branch.
        return CustomAction.RunResult(success=True)

    def _step(self, context: Context, session: CombatSession) -> None:
        if session.terminal:
            return

        if context.tasker.stopping:
            session.finish(Phase.STOPPED, "stopped")
            _executor.release_all()
            return

        snapshot = _observer.observe()
        update_session_phase(session, snapshot, session.settings)
        if session.terminal:
            return

        if session.phase != Phase.ACTIVE:
            return  # waiting / uncertain: observe only, never attack blind

        _ensure_sound(context, session)

        if not _resolve_pending(session, snapshot):
            return
        if session.pending is not None:
            return

        _handle_sound_event(session, snapshot)
        if session.pending is not None:
            return

        intent = _policy.decide(session, snapshot)
        if intent is None or session.terminal:
            return

        if intent.action == "invalid":
            session.finish(Phase.FAILED, "template_role_unresolved")
            return

        result = _executor.execute(intent)
        logger.debug("Combat: %s -> %s (%s)", intent.action, result.outcome.value, result.reason)

        if result.outcome is Outcome.PENDING:
            now = time.monotonic()
            session.pending = PendingAction(
                intent=intent,
                sent_at=now,
                deadline=now + session.settings["confirm_timeout_ms"] / 1000.0,
                issued_frame=snapshot.frame_id,
                issued_slot=snapshot.current_slot,
            )
            return

        if result.outcome is Outcome.BLOCKED:
            session.finish(Phase.STOPPED, result.reason)
            return

        succeeded = result.outcome is Outcome.SUCCEEDED
        if hasattr(_policy, "on_action_result"):
            _policy.on_action_result(intent, succeeded=succeeded)
        if not succeeded and intent.required:
            session.finish(Phase.FAILED, "required_action_failed:%s:%s" % (intent.action, result.reason))


@AgentServer.custom_action("CombatFinalizeAction")
class CombatFinalizeAction(CustomAction):
    """Release inputs, stop audio and report how the session ended."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        session = _session
        if session is None:
            _cleanup(context)
            return CustomAction.RunResult(success=True)

        if not session.terminal:
            session.finish(Phase.STOPPED, "finalized_early")

        reason = session.exit_reason
        phase = session.phase
        _cleanup(context)

        logger.info("Combat: session ended phase=%s reason=%s", phase.value, reason)
        PrintT(context, "combat.session_end", reason)
        return CustomAction.RunResult(success=phase is Phase.ENDED)
