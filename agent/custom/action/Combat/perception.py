"""战斗感知：把画面变成 ``CombatState``。

## 每个信号的来源与可靠性

本层刻意**只使用已在真机验证过的识别方式**，不自己发明阈值：

| 信号 | 来源 | 可靠性 |
|---|---|---|
| 敌人可见 | 粉爪 ``PinkPawHeist_CheckMonsterOnce`` 的红色血条签名 | 已上线验证 |
| 在队伍界面 | 内核 ``team.is_in_team``（粉爪切人确认在用） | 已上线验证 |
| 黑屏/加载 | 内核 ``team.is_black_screen`` | 已上线验证 |
| 当前槽位 | 内核 ``team.current_slot_index`` | 已上线验证 |
| ESC 菜单打开 | 场景管理器公共节点 ``InEscMenu`` | 已上线验证 |
| 是否在大世界 | 场景管理器公共节点 ``InWorld`` | 已上线验证 |
| 角色身份 | 槽位 + 用户声明的 ``roster`` | 取决于用户声明是否正确 |
| 自身血量 | **未实现**，恒为 ``None`` | 见下 |
| Boss 血量 | **未实现**，恒为 ``None`` | 见下 |

## 为什么血量是 None

读血量需要知道血条的 ROI 与颜色区间。仓库里没有玩家血条/Boss 血条的
识别节点，粉爪也不读血量（它只用"有没有红色血条"判断有没有怪）。
我没有游戏环境去标定这些 ROI，凭空写一个阈值是在制造一个看起来能用、
实际会误判的功能。

因此 ``self_hp`` / ``boss_hp`` 保持 ``None``，``hp_below`` 这类条件按
"未知即不满足"恒为假。脚本解析期会对此发出 issue 提醒用户。
补齐方式见 docs/zh_cn/develop/combat-engine.md 的"扩展感知"一节。
"""

from __future__ import annotations

from dataclasses import dataclass

from .kernel import team as kernel_team
from .kernel.frames import is_hit
from .identity.roster import Roster, resolve_identity
from .script.conditions import CombatState

# 敌人检测节点。直接复用粉爪已验证的红色血条颜色签名，
# 不新增阈值。ROI 覆盖整个战斗区域。
ENEMY_NODE = "PinkPawHeist_CheckMonsterOnce"

# ESC 菜单检测节点。来自场景管理器的公共接口（Interface/Scene/Status.json），
# 是已验证的 OCR 判定，不是我新写的识别。
# 用途：菜单打开时立刻停手——否则脚本会把攻击键按到菜单项上，
# 可能误点「退出副本」之类的危险选项。
MENU_NODE = "InEscMenu"

# 大世界检测节点。同样来自场景管理器公共接口，判据是同时存在
# ESC 手机按钮与任务菜单按钮（两者都有现成模板与 ROI）。
# 用途：启动前守卫——用户在大世界误点「自动战斗」时，
# 与其对着空气乱按技能键，不如直接拒绝运行并说明原因。
WORLD_NODE = "InWorld"

# 敌人检测的节流间隔（秒）。每 tick 都跑一次识别会拖慢按键节奏，
# 而敌人出现/消失本身不是毫秒级事件。
ENEMY_CHECK_INTERVAL = 0.2

# 菜单检测间隔。比敌人检测更宽松：菜单是用户主动操作的结果，
# 半秒内响应足够，而且 OCR 比颜色匹配贵。
MENU_CHECK_INTERVAL = 0.5


@dataclass
class PerceptionConfig:
    """感知层的开关与节流参数。"""

    detect_enemy: bool = True
    enemy_check_interval: float = ENEMY_CHECK_INTERVAL
    detect_slot: bool = True
    enemy_node: str = ENEMY_NODE
    detect_menu: bool = True
    menu_check_interval: float = MENU_CHECK_INTERVAL
    menu_node: str = MENU_NODE
    world_node: str = WORLD_NODE


class Perception:
    """从截图构建 ``CombatState``。

    识别失败一律降级为"未知"而不是猜测：``enemy_visible`` 保持上一次
    结果（避免单帧抖动导致技能乱放），``character`` 直接置 ``None``。
    """

    def __init__(self, kernel, roster: Roster | None = None,
                 config: PerceptionConfig | None = None):
        self.kernel = kernel
        self.roster = roster
        self.config = config or PerceptionConfig()
        self._last_enemy_check = 0.0
        self._enemy_visible = False
        self._enemy_checked_once = False
        self._last_menu_check = 0.0
        self._menu_open = False
        self._menu_checked_once = False

    def reset(self):
        """开始新一场战斗时清空缓存。"""
        self._last_enemy_check = 0.0
        self._enemy_visible = False
        self._enemy_checked_once = False
        self._last_menu_check = 0.0
        self._menu_open = False
        self._menu_checked_once = False

    def _recognize(self, node: str, image) -> bool:
        """跑一次识别节点，异常时返回 None 交调用方决定降级策略。"""
        return is_hit(self.kernel.ctx.run_recognition(node, image))

    def _check_enemy(self, image, now: float) -> bool:
        """按节流间隔检测敌人；间隔内沿用上次结果。"""
        if not self.config.detect_enemy:
            return False
        if (
            self._enemy_checked_once
            and (now - self._last_enemy_check) < self.config.enemy_check_interval
        ):
            return self._enemy_visible
        self._last_enemy_check = now
        self._enemy_checked_once = True
        try:
            self._enemy_visible = self._recognize(self.config.enemy_node, image)
        except Exception as exc:
            # 识别异常时保持上次结果，不因单次失败改变行为
            self.kernel.log_warning(f"敌人检测失败，沿用上次结果: {exc}")
        return self._enemy_visible

    def _check_menu(self, image, now: float) -> bool:
        """按节流间隔检测 ESC 菜单是否打开。

        与敌人检测的降级策略相反：识别失败时返回 ``False``（认为菜单没开）。
        因为"误判菜单已开"会让战斗白白停下，而菜单真开了最多多按几下键，
        下一次检测就会发现。宁可漏报一拍，不要凭识别抖动中断战斗。
        """
        if not self.config.detect_menu:
            return False
        if (
            self._menu_checked_once
            and (now - self._last_menu_check) < self.config.menu_check_interval
        ):
            return self._menu_open
        self._last_menu_check = now
        self._menu_checked_once = True
        try:
            self._menu_open = self._recognize(self.config.menu_node, image)
        except Exception as exc:
            self.kernel.log_warning(f"菜单检测失败，视为未打开: {exc}")
            self._menu_open = False
        return self._menu_open

    def sees_enemy_once(self, image) -> bool:
        """不走节流地做一次敌人检测，供启动守卫使用。

        与 ``_check_enemy`` 分开是刻意的：守卫在会话开始前连续探几帧，
        不能被 ``enemy_check_interval`` 的节流缓存干扰，也不该污染
        战斗中的 ``_enemy_visible`` 状态。

        识别失败时返回 ``False``（视为没看到敌人）。这不会导致误拒——
        守卫还要求同时命中 ``InWorld`` 才会拒绝。
        """
        if image is None or not self.config.detect_enemy:
            return False
        try:
            return self._recognize(self.config.enemy_node, image)
        except Exception as exc:
            self.kernel.log_warning(f"守卫敌人检测失败，视为未发现敌人: {exc}")
            return False

    def looks_like_open_world(self, image) -> bool:
        """判断当前画面是否命中大世界判据（ESC 手机按钮 + 任务菜单按钮）。

        **这不等于"不在战斗"**：NTE 的野外战斗就发生在大世界，战斗中这两个
        按钮同样存在。因此调用方必须结合敌人证据一起判断，不能单独用它
        拒绝运行（见 ``runtime`` 的启动守卫）。

        识别失败时返回 ``False`` —— 宁可放行让用户自己看结果，
        也不要因为识别抖动把一次合法的战斗挡掉。
        """
        if image is None:
            return False
        try:
            return self._recognize(self.config.world_node, image)
        except Exception as exc:
            self.kernel.log_warning(f"大世界检测失败，视为不在大世界: {exc}")
            return False

    def observe(self, image, *, now: float, elapsed: float, tick: int) -> CombatState:
        """构建当前帧的状态快照。"""
        if image is None:
            # 截图失败：只能给出时间信息，其余全部未知
            return CombatState(
                elapsed=elapsed, now=now, tick=tick, in_team=True
            )

        in_team = kernel_team.is_in_team(image)
        black = kernel_team.is_black_screen(image)

        # 黑屏时不做任何节点识别：画面没内容，结果无意义
        menu_open = False if black else self._check_menu(image, now)

        slot = -1
        character = None
        if self.config.detect_slot and in_team and not black and not menu_open:
            slot = kernel_team.current_slot_index(image)
            if self.roster is not None:
                character = resolve_identity(self.roster, slot)

        enemy = False if black or menu_open else self._check_enemy(image, now)

        return CombatState(
            elapsed=elapsed,
            now=now,
            tick=tick,
            self_hp=None,  # 见模块文档：缺少已验证的血条 ROI
            boss_hp=None,
            enemy_visible=enemy,
            enemy_count=1 if enemy else 0,
            character=character,
            slot=slot,
            in_team=in_team,
            black_screen=black,
            menu_open=menu_open,
        )
