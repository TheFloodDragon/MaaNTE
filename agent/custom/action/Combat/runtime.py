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

启动前还有一道场景守卫，用来拦住"在大世界空地上误启动"。

## 结束条件

按优先级：超时 / tick 上限 -> 脱战（离开队伍界面够久）-> 清怪
（``stop_when_no_enemy``，默认关）-> ESC 菜单打开够久。

清怪判定是为刷本加的：副本里打完怪队伍 UI 还在，脱战判据永远不成立，
不看血条就只能干等满 ``duration``。它要求**先见过敌人**才生效，
否则开局那几帧还没识别到血条就会被判成"打完了"。

注意这道守卫**不能只看 ``InWorld``**：NTE 的战斗就发生在大世界里，
``InWorld`` 的判据是 ESC 手机按钮 + 任务菜单按钮，战斗中同样成立。
只凭它拒绝运行，会把每一次合法战斗都挡掉（这正是守卫最初的 bug）。

因此判据改成"缺少战斗证据"：只有在**整个探测窗口内**都满足
``InWorld`` 命中且始终看不到敌人血条时才拒绝。敌人血条
（``PinkPawHeist_CheckMonsterOnce``）是仓库里唯一已验证的直接战斗信号，
一旦出现就立即放行。黑屏、加载等过渡帧不作为拒绝依据。

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
REASON_NO_ENEMY = "no_enemy"

# 默认参数
DEFAULT_TICK_INTERVAL = 0.05
DEFAULT_DURATION = 60.0
MAX_DURATION = 3600.0
# 离开队伍界面多久算脱战。给足黑屏/加载的时间，避免误判结束。
DEFAULT_LEAVE_GRACE = 3.0
# ESC 菜单持续打开多久就结束会话。给一点宽限，避免用户手滑开一下菜单
# 就把整场战斗中断；但也不能太长，菜单开着时脚本是完全无用的。
DEFAULT_MENU_GRACE = 1.0
# 清怪后多久算「这波打完了」。刷本时用它替代干等 duration。
# 给足 4 秒是因为血条会因为镜头转动、技能特效遮挡而短暂消失，
# 判太快会在敌人还活着时收兵，下一轮进副本就会带着残怪。
DEFAULT_NO_ENEMY_GRACE = 4.0

# 启动守卫的探测窗口。战斗中怪物血条可能被遮挡或恰好不在 ROI 内，
# 单帧判断会误拒；多探几帧再决定，代价只有不到一秒。
DEFAULT_GUARD_PROBES = 6
DEFAULT_GUARD_PROBE_INTERVAL = 0.12


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
    # 敌人血条消失够久就结束会话。默认关闭：普通「自动战斗」任务里
    # 用户可能只是想让脚本一直挂着出招，不该替他决定何时收手。
    # 刷本会打开它——否则每一轮都要白等满 duration 才肯进下一轮。
    #
    # 只有在**这场战斗里至少见过一次敌人**之后才会生效：否则开局第一帧
    # 还没识别到血条就会被判成"打完了"，战斗直接空转结束。
    stop_when_no_enemy: bool = False
    no_enemy_grace: float = DEFAULT_NO_ENEMY_GRACE
    # 启动前如果确认"在大世界且没有任何战斗迹象"就拒绝运行。默认开启：
    # 本任务只负责战斗中出招，在空地上跑只会对着空气乱按键。
    # 判据见模块文档——必须同时缺少敌人证据，不能只看 InWorld。
    guard_open_world: bool = True
    guard_probes: int = DEFAULT_GUARD_PROBES
    guard_probe_interval: float = DEFAULT_GUARD_PROBE_INTERVAL
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
        no_enemy_since: float | None = None
        seen_enemy = False
        reason = REASON_TIMEOUT
        success = True

        self.engine.reset()
        self.perception.reset()

        # 启动前守卫：只有确认"在大世界且完全没有战斗迹象"才拒绝。
        # 这个检查只做一次，因为它的目的是拦住"误启动"，
        # 而不是在战斗中反复判断场景。
        if self.config.guard_open_world and self._lacks_combat_evidence():
            self.kernel.log_warning(
                "当前像是在大世界空地上（未发现任何敌人），自动战斗不会执行。"
                "本任务只负责战斗中出招，请先自行进入战斗再启动；"
                "若确认要在无敌人场景下跑脚本，请关闭“启动前检查是否在战斗中”。"
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

                # 清怪判定：见过敌人之后血条持续消失，说明这波打完了。
                # 顺序放在脱战判定之后、菜单判定之前——脱战是更强的信号
                # （已经离开战斗界面），不该被"没看见血条"抢先报成清怪。
                if self.config.stop_when_no_enemy:
                    if state.enemy_visible:
                        seen_enemy = True
                        no_enemy_since = None
                    elif seen_enemy and not state.black_screen:
                        if no_enemy_since is None:
                            no_enemy_since = now
                        elif (now - no_enemy_since) >= self.config.no_enemy_grace:
                            reason = REASON_NO_ENEMY
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

    def _lacks_combat_evidence(self) -> bool:
        """启动守卫判据：是否可以确认"当前不在战斗"。

        返回 ``True``（拒绝运行）的唯一条件是：探测窗口内每一帧都既命中
        ``InWorld`` 又看不到敌人血条。任何一帧出现敌人、或任何一帧不在
        大世界，都立即放行——宁可放行让用户看到结果，也不要把合法战斗挡掉。

        黑屏/加载帧不构成拒绝依据：它们既不算"有敌人"也不算"在大世界"，
        循环会继续探测下一帧。
        """
        probes = max(1, int(self.config.guard_probes))
        for attempt in range(probes):
            self.kernel.ah.raise_if_stopped()
            frame = self.kernel.screencap()
            if frame is None:
                return False
            if self.perception.sees_enemy_once(frame):
                return False
            if not self.perception.looks_like_open_world(frame):
                return False
            if attempt + 1 < probes and self.config.guard_probe_interval > 0:
                self.kernel.sleep(
                    self.config.guard_probe_interval,
                    allow_slow_poll=False,
                    scaled=False,
                )
        return True

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
