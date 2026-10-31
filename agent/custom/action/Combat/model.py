"""战斗数据类型。未知信号不等于阴性，停止输出不等于确认清场。"""

from __future__ import annotations

from dataclasses import dataclass


class EndReason:
    CLEARED = "cleared"
    DISENGAGED = "disengaged"
    TARGET_LOST = "target_lost"
    RETREATED = "retreated"
    DEFEATED = "defeated"
    TIMEOUT = "timeout"
    HUD_LOST = "hud_lost"
    OBSERVATION_LOST = "observation_lost"
    NO_COMBAT = "no_combat"
    STOPPED = "stopped"
    SCRIPT_FINISHED = "script_finished"
    ERROR = "error"
    SCHEME_ERROR = "scheme_error"


SUCCESS_REASONS = frozenset({EndReason.CLEARED})
CONFIRMED_REASONS = frozenset(
    {EndReason.CLEARED, EndReason.DISENGAGED, EndReason.RETREATED, EndReason.DEFEATED}
)
END_REASONS = frozenset(
    value for key, value in vars(EndReason).items() if key.isupper()
)
TOGGLE_ACTIONS = ("ultimate", "skill", "arc", "dodge", "jump", "switch")


class Phase:
    WAITING = "waiting"
    ACTIVE = "active"
    UNCERTAIN = "uncertain"
    VERIFYING = "verifying"
    SUSPENDED = "suspended"
    ENDED = "ended"


@dataclass(frozen=True)
class CombatResult:
    success: bool
    reason: str
    duration: float = 0.0
    entered: bool = False
    confirmed_end: bool = False
    evidence: tuple[tuple[str, bool | None], ...] = ()

    @classmethod
    def of(
        cls, reason: str, duration: float = 0.0, *, entered: bool = False, evidence=()
    ) -> "CombatResult":
        return cls(
            success=reason in SUCCESS_REASONS,
            reason=reason,
            duration=duration,
            entered=entered,
            confirmed_end=reason in CONFIRMED_REASONS,
            evidence=tuple(evidence),
        )


@dataclass(frozen=True)
class ActionResult:
    """sent 仅表示控制器接受输入，不表示技能释放/命中成功。"""

    status: str
    executed: int = 0
    skipped: int = 0
    controls: int = 0

    @property
    def success(self) -> bool:
        """sent 已发送 / unchanged 已是目标角色 / configured 仅改了节奏 / completed 默认轮转跑完。"""
        return self.status in {"sent", "unchanged", "configured", "completed"}


@dataclass(frozen=True)
class Snapshot:
    """一张截图的证据；所有识别共享此图，None 表示未知或未配置。"""

    at: float
    valid: bool
    in_hud: bool | None = None
    black: bool | None = None
    slot: int = 0
    enemy: bool | None = None
    health_bar: bool | None = None
    enemy_level: bool | None = None
    boss: bool | None = None
    active: bool | None = None
    cleared: bool | None = None
    disengaged: bool | None = None
    retreated: bool | None = None
    defeated: bool | None = None
    suspended: bool | None = False
    retreat_prompt: bool | None = False
    evidence: tuple[tuple[str, bool | None], ...] = ()

    @property
    def hostile(self) -> bool | None:
        if any(
            value is True
            for value in (
                self.enemy,
                self.health_bar,
                self.enemy_level,
                self.boss,
                self.active,
            )
        ):
            return True
        return self.enemy

    @property
    def terminal_signals(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in ("cleared", "disengaged", "retreated", "defeated")
            if getattr(self, name) is True
        )

    @property
    def terminal_reason(self) -> str | None:
        signals = self.terminal_signals
        # 同帧仍有敌人，或者多个互斥终局同时命中，均不擅自宣布胜利。
        if (
            self.valid
            and self.black is False
            and self.suspended is False
            and self.retreat_prompt is False
            and self.hostile is not True
            and len(signals) == 1
        ):
            return signals[0]
        return None

    @property
    def ready(self) -> bool:
        return bool(
            self.valid
            and self.in_hud is True
            and self.black is False
            and self.suspended is False
            and self.retreat_prompt is False
            and not self.terminal_signals
        )


INVALID_SNAPSHOT = Snapshot(at=0.0, valid=False)


class CombatStopped(Exception):
    """用户停止任务。"""


class CombatEnded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InputSuspended(Exception):
    """画面不可操作，放弃当前动作的剩余部分，不将槽位标记为阵亡。"""


class SwitchFailed(Exception):
    def __init__(self, slot: int):
        super().__init__(slot)
        self.slot = slot
