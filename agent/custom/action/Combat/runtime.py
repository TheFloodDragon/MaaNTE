"""战斗会话：进战 -> tick 循环 -> 退出，并保证按键被释放。

## 时序契约

一个 tick 的顺序是固定的，改动会影响行为：

1. 检查停止/超时
2. 截图 -> ``Perception.observe`` -> ``CombatState``
3. ``CombatEngine.decide`` -> ``Decision``
4. ``ActionRunner.run`` 执行动作
5. ``engine.commit`` 记账（**执行后**才记，避免白占冷却）
6. 间隔等待（走内核可中断 sleep，期间仍响应停止）

## 安全保证

无论正常结束、异常、还是 tasker 停止，``run()`` 都会在 ``finally`` 里
释放所有按住的键。战斗中最糟的 bug 是角色卡住一直走或一直按着攻击键。

另外，检测到 ESC 菜单打开时会**立刻停止发键并松手**（不等宽限期）：
菜单开着按键会点到菜单项上，有误触发「退出副本」之类的风险。
宽限期只用来决定要不要结束会话，不用来决定要不要继续按键。

启动前还有一道场景守卫：如果明显处于大世界，直接拒绝运行。
本任务需要用户自己先进战斗，在大世界跑只会对着空气乱按键。

依赖全部通过构造注入（screencap/clock/sleeper），因此可以完全离线跑
闭环仿真，不需要游戏。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .kernel.errors import AbortException, TaskerStoppedException
from .perception import Perception
from .script import ActionRunner, CombatEngine, CombatScript
from .script.engine import REASON_FALLBACK, REASON_IDLE, REASON_RULE

# 结束原因
REASON_TIMEOUT = "timeout"
REASON_STOPPED = "stopped"
REASON_NOT_IN_COMBAT = "not_in_combat"
REASON_ABORTED = "aborted"
REASON_ERROR = "error"
REASON_MAX_TICKS = "max_ticks"
REASON_MENU_OPEN = "menu_open"
REASON_OPEN_WORLD = "open_world"

# 默认参数
DEFAULT_TICK_INTERVAL = 0.05
DEFAULT_DURATION = 60.0
MAX_DURATION = 3600.0
# 离开队伍界面多久算脱战。给足黑屏/加载的时间，避免误判结束。
DEFAULT_LEAVE_GRACE = 3.0
# ESC 菜单持续打开多久就结束会话。给一点宽限，避免用户手滑开一下菜单
# 就把整场战斗中断；但也不能太长，菜单开着时脚本是完全无用的。
DEFAULT_MENU_GRACE = 1.0


@dataclass
class SessionConfig:
    """会话参数。"""

    duration: float = DEFAULT_DURATION
    tick_interval: float = DEFAULT_TICK_INTERVAL
    leave_grace: float = DEFAULT_LEAVE_GRACE
    menu_grace: float = DEFAULT_MENU_GRACE
    # 脱离队伍界面即结束（关掉可用于跑非战斗场景的脚本）
    stop_when_not_in_team: bool = True
    # ESC 菜单打开时结束会话。默认开启：菜单开着还按键有误点风险。
    stop_when_menu_open: bool = True
    # 启动前如果明显在大世界就拒绝运行。默认开启：本任务需要用户
    # 自己先进战斗，在大世界跑只会对着空气乱按键。
    guard_open_world: bool = True
    max_ticks: int = 0  # 0 = 不限
    log_decisions: bool = False


@dataclass
class SessionResult:
    """一次战斗会话的结果摘要。"""

    success: bool
    reason: str
    ticks: int = 0
    elapsed: float = 0.0
    rule_fires: dict = field(default_factory=dict)
    fallback_fires: int = 0
    idle_ticks: int = 0

    def describe(self) -> str:
        fired = ", ".join(f"{k}x{v}" for k, v in sorted(self.rule_fires.items()))
        return (
            f"reason={self.reason} ticks={self.ticks} "
            f"elapsed={self.elapsed:.1f}s rules[{fired}] "
            f"fallback={self.fallback_fires} idle={self.idle_ticks}"
        )


class CombatSession:
    """驱动一场战斗。"""

    def __init__(
        self,
        kernel,
        script: CombatScript,
        config: SessionConfig | None = None,
        perception: Perception | None = None,
        engine: CombatEngine | None = None,
        runner: ActionRunner | None = None,
        clock=time.monotonic,
    ):
        self.kernel = kernel
        self.script = script
        self.config = config or SessionConfig()
        self.engine = engine or CombatEngine(script)
        self.perception = perception or Perception(
            kernel, roster=getattr(script, "roster", None)
        )
        self.runner = runner or ActionRunner(kernel, log=kernel.log_warning)
        self._clock = clock

    def run(self) -> SessionResult:
        """跑一场战斗，直到超时/脱战/被停止。

        返回摘要而不是抛异常（除了 tasker 停止），让调用方能记录统计。
        """
        started = self._clock()
        deadline = started + max(0.0, min(self.config.duration, MAX_DURATION))
        ticks = 0
        rule_fires: dict[str, int] = {}
        fallback_fires = 0
        idle_ticks = 0
        left_team_since: float | None = None
        menu_open_since: float | None = None
        reason = REASON_TIMEOUT
        success = True

        self.engine.reset()
        self.perception.reset()

        # 启动前守卫：明显在大世界就别跑了。
        # 这个检查只做一次，因为它的目的是拦住"误启动"，
        # 而不是在战斗中反复判断场景。
        if self.config.guard_open_world:
            first_frame = self.kernel.screencap()
            if self.perception.looks_like_open_world(first_frame):
                self.kernel.log_warning(
                    "当前处于大世界，自动战斗不会执行。"
                    "本任务只负责战斗中出招，请先自行进入战斗再启动。"
                )
                return SessionResult(
                    success=False,
                    reason=REASON_OPEN_WORLD,
                    elapsed=self._clock() - started,
                )

        try:
            while True:
                now = self._clock()
                if now >= deadline:
                    reason = REASON_TIMEOUT
                    break
                if self.config.max_ticks and ticks >= self.config.max_ticks:
                    reason = REASON_MAX_TICKS
                    break
                self.kernel.ah.raise_if_stopped()

                image = self.kernel.screencap()
                state = self.perception.observe(
                    image, now=now, elapsed=now - started, tick=ticks
                )

                # 脱战判定：连续离开队伍界面超过宽限期才认为结束。
                # 黑屏视为过渡态，不计入脱战。
                if self.config.stop_when_not_in_team:
                    if state.in_team or state.black_screen:
                        left_team_since = None
                    else:
                        if left_team_since is None:
                            left_team_since = now
                        elif (now - left_team_since) >= self.config.leave_grace:
                            reason = REASON_NOT_IN_COMBAT
                            break

                # ESC 菜单打开时**立刻停止发键**，不等宽限期。
                # 宽限期只决定"要不要结束会话"，不决定"要不要继续按键"——
                # 菜单开着按键会点到菜单项上，可能误触发退出副本之类的操作。
                if state.menu_open:
                    if menu_open_since is None:
                        menu_open_since = now
                        self.kernel.log_warning(
                            "检测到 ESC 菜单打开，暂停发键"
                        )
                    # 松开已按住的键，避免菜单里角色仍在移动
                    self.runner.release_all()
                    if (
                        self.config.stop_when_menu_open
                        and (now - menu_open_since) >= self.config.menu_grace
                    ):
                        reason = REASON_MENU_OPEN
                        break
                    ticks += 1
                    idle_ticks += 1
                    if self.config.tick_interval > 0:
                        self.kernel.sleep(
                            self.config.tick_interval,
                            allow_slow_poll=False,
                            scaled=False,
                        )
                    continue
                menu_open_since = None

                decision = self.engine.decide(state)
                if decision.reason == REASON_RULE:
                    self.runner.run(decision.actions)
                    self.engine.commit(decision, self._clock())
                    rule_fires[decision.rule_name] = (
                        rule_fires.get(decision.rule_name, 0) + 1
                    )
                    if self.config.log_decisions:
                        self.kernel.log_info(
                            f"[combat] {decision.rule_name} "
                            f"({decision.rule.condition.describe()})"
                        )
                elif decision.reason == REASON_FALLBACK:
                    self.runner.run(decision.actions)
                    self.engine.commit(decision, self._clock())
                    fallback_fires += 1
                elif decision.reason == REASON_IDLE:
                    idle_ticks += 1

                ticks += 1
                if self.config.tick_interval > 0:
                    self.kernel.sleep(
                        self.config.tick_interval, allow_slow_poll=False, scaled=False
                    )

        except TaskerStoppedException:
            reason, success = REASON_STOPPED, False
        except AbortException as exc:
            self.kernel.log_warning(f"战斗中止: {exc}")
            reason, success = REASON_ABORTED, False
        except Exception as exc:
            self.kernel.log_error(f"战斗异常: {exc}")
            reason, success = REASON_ERROR, False
        finally:
            # 无论怎么结束，都必须松手
            self._release_safely()

        return SessionResult(
            success=success,
            reason=reason,
            ticks=ticks,
            elapsed=self._clock() - started,
            rule_fires=rule_fires,
            fallback_fires=fallback_fires,
            idle_ticks=idle_ticks,
        )

    def _release_safely(self):
        """释放按键。本身不允许抛异常，否则会盖掉真正的失败原因。"""
        try:
            self.runner.release_all()
        except Exception as exc:
            self.kernel.log_error(f"释放脚本按键失败: {exc}")
        try:
            self.kernel.release_held_keys()
        except Exception as exc:
            self.kernel.log_error(f"释放内核按键失败: {exc}")
