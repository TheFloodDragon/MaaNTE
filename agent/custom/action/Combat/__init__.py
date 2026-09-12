"""Combat module: session-driven combat with generic and custom templates.

Importing this package registers the Maa custom actions/recognitions, so it
must only be imported from the agent process (``agent/custom/action``).
Pure-logic modules (models, policies, templates, resources) can be loaded
individually by tests without MaaFramework.
"""

from .actions import CombatFinalizeAction, CombatInitAction, CombatStepAction
from .executor import CombatExecutor
from .models import (
    ActionIntent,
    ActionResult,
    CombatSession,
    CombatSnapshot,
    Outcome,
    PendingAction,
    Phase,
    SoundEvent,
)
from .observer import CombatObserver
from .policies import (
    BalancedCombatPolicy,
    BasicCombatPolicy,
    CustomCombatPolicy,
    update_session_phase,
)
from .recognitions import CombatStatusRecognition
from .resources import find_sound_sample, find_template
from .sound_events import SoundEventSource
from .templates import CustomTemplate, TemplateError, load_template

__all__ = [
    "ActionIntent",
    "ActionResult",
    "BalancedCombatPolicy",
    "BasicCombatPolicy",
    "CombatExecutor",
    "CombatFinalizeAction",
    "CombatInitAction",
    "CombatObserver",
    "CombatSession",
    "CombatSnapshot",
    "CombatStatusRecognition",
    "CombatStepAction",
    "CustomCombatPolicy",
    "CustomTemplate",
    "Outcome",
    "PendingAction",
    "Phase",
    "SoundEvent",
    "SoundEventSource",
    "TemplateError",
    "find_sound_sample",
    "find_template",
    "load_template",
    "update_session_phase",
]
