"""Core data models for combat system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Phase(Enum):
    """Combat session phase."""

    WAITING = "waiting"  # Waiting to enter combat
    ACTIVE = "active"  # In combat
    SUSPENDED = "suspended"  # Loading/menu interruption
    UNCERTAIN = "uncertain"  # Unknown state, trying to recover
    ENDED = "ended"  # Successfully ended (combat clear)
    FAILED = "failed"  # Failed or error
    STOPPED = "stopped"  # User stopped


class Outcome(Enum):
    """Action execution outcome."""

    SUCCEEDED = "succeeded"  # Action completed successfully
    PENDING = "pending"  # Action sent, awaiting confirmation
    SKIPPED = "skipped"  # Action skipped due to condition
    BLOCKED = "blocked"  # Action blocked (stopping, etc)
    FAILED = "failed"  # Action failed


@dataclass
class CombatSnapshot:
    """Immutable observation of combat state at a point in time."""

    frame_id: int
    captured_at: float  # monotonic time
    valid: bool = False

    # Combat state
    combat: Optional[bool] = None  # True=in combat, False=clear, None=unknown
    in_team: Optional[bool] = None  # Team HUD visible
    focused: Optional[bool] = None  # Window has focus
    loading: bool = False
    defeated: bool = False

    # Character state
    current_slot: Optional[int] = None  # 0-3
    alive: dict[int, Optional[bool]] = field(default_factory=dict)  # slot -> alive status

    # Ability state
    skill_ready: Optional[bool] = None
    ultimate_ready: Optional[bool] = None

    # Evidence for debugging
    evidence: tuple[str, ...] = ()


@dataclass
class ActionIntent:
    """Requested combat action."""

    action: str  # "switch", "normal_attack", "skill", "ultimate", "dodge", etc
    slot: Optional[int] = None  # For switch actions (1-4)
    duration_ms: Optional[int] = None  # For charged attacks
    required: bool = False  # If False, failure only skips; if True, ends session
    condition: Optional[str] = None  # Condition label for logging


@dataclass
class ActionResult:
    """Result of executing an action."""

    outcome: Outcome
    reason: str = ""
    confirmed_at: Optional[float] = None  # When confirmation was verified


@dataclass
class PendingAction:
    """Action awaiting confirmation from a later observation."""

    intent: ActionIntent
    sent_at: float
    deadline: float
    issued_frame: int = -1  # confirmation needs a frame newer than this
    issued_slot: Optional[int] = None
    result: Optional[ActionResult] = None


@dataclass
class SoundEvent:
    """Sound detection event with session scope and expiry."""

    session_id: str
    detected_at: float
    expires_at: float
    kind: str  # "dodge" or "counter"


@dataclass
class CombatSession:
    """Mutable combat session state."""

    session_id: str
    settings: dict
    started_at: float

    phase: Phase = Phase.WAITING
    entered_at: Optional[float] = None  # When combat became active
    pending: Optional[PendingAction] = None
    cycles_completed: int = 0

    # Timers
    uncertain_since: Optional[float] = None
    clear_since: Optional[float] = None

    # Terminal state
    terminal: bool = False
    exit_reason: str = ""

    def finish(self, phase: Phase, reason: str) -> None:
        """Mark session as finished."""
        self.phase = phase
        self.terminal = True
        self.exit_reason = reason
