"""战斗内核共用的数据类型与异常。本模块不依赖 maa，可直接单测。"""

from __future__ import annotations

from dataclasses import dataclass


class EndReason:
    """一场战斗的结束原因。值同时用作 agent i18n key 的后缀。"""

    CLEARED = "cleared"  # 敌人血条持续消失，判定脱战
    TIMEOUT = "timeout"  # 超过单场最长时长
    HUD_LOST = "hud_lost"  # 战斗中长时间看不到可操作界面（阵亡/剧情/弹窗）
    NO_COMBAT = "no_combat"  # 等待超时仍未进入战斗
    STOPPED = "stopped"  # 用户停止任务
    ERROR = "error"  # 内部异常
    SCHEME_ERROR = "scheme_error"  # 方案文件加载或校验失败


# 视为"正常结束"的原因：把画面交还调用方的 next 继续识别
SUCCESS_REASONS = frozenset({EndReason.CLEARED, EndReason.TIMEOUT, EndReason.HUD_LOST})


@dataclass(frozen=True)
class CombatResult:
    success: bool
    reason: str
    duration: float = 0.0

    @classmethod
    def of(cls, reason: str, duration: float = 0.0) -> "CombatResult":
        return cls(success=reason in SUCCESS_REASONS, reason=reason, duration=duration)


@dataclass(frozen=True)
class Snapshot:
    """一帧感知结果。未知统一用 None，消费方按"未知即不满足"处理。"""

    at: float
    valid: bool
    in_hud: bool | None = None
    black: bool | None = None
    slot: int = 0  # 当前高亮槽位 1-4，0 表示无法判断
    enemy: bool | None = None

    @property
    def ready(self) -> bool:
        """可以接收战斗输入：看得到 HUD 且不是黑屏。"""
        return bool(self.valid and self.in_hud and not self.black)


INVALID_SNAPSHOT = Snapshot(at=0.0, valid=False)


class CombatStopped(Exception):
    """用户停止任务。"""


class CombatEnded(Exception):
    """战斗结束信号，从任意深度的执行流程中跳出。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SwitchFailed(Exception):
    """出招表中的 switch(n) 未能确认，放弃本段剩余指令。"""

    def __init__(self, slot: int):
        super().__init__(slot)
        self.slot = slot
