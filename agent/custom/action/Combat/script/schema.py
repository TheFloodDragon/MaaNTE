"""战斗脚本的数据模型与解析。

设计取向：**脚本是数据，不是代码**。

用户写的 JSON 只描述"什么条件下按什么键"，解析后得到不可变的
``CombatScript``。解析阶段就把所有非法输入处理掉（回退默认 + 收集
``issues``），这样引擎在战斗中永远不需要处理畸形数据——战斗中抛异常
等于角色站着挨打。

与项目其它模块一致：非法值回退默认而不是抛错，但**同时记录 issue**，
由 ``tools/check_combat_assets.py`` 在 CI 阶段把问题暴露出来。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import Any

# --- 限制：防止用户配置把战斗拖垮 ---
MAX_ACTIONS_PER_RULE = 16
MAX_RULES = 64
MAX_SEQUENCE_DURATION = 10.0
MIN_ACTION_DURATION = 0.0
MAX_ACTION_DURATION = 5.0
MAX_COOLDOWN = 600.0
DEFAULT_PRIORITY = 100

# 允许在脚本里出现的按键。刻意做白名单：
# 打错一个键名应该在解析期被发现，而不是运行时静默不发键。
ALLOWED_KEYS = frozenset(
    {
        "w", "a", "s", "d",
        "q", "e", "r", "t", "f", "g", "h",
        "x", "c", "v", "z",
        "space", "lshift", "shift", "ctrl", "lctrl", "alt", "tab",
        "1", "2", "3", "4", "5", "6",
        "esc", "m",
    }
)

ALLOWED_MOUSE = frozenset({"left", "right", "middle"})

# 动作类型
ACTION_KEY = "key"
ACTION_HOLD = "hold"
ACTION_RELEASE = "release"
ACTION_CLICK = "click"
ACTION_MOUSE_DOWN = "mouse_down"
ACTION_MOUSE_UP = "mouse_up"
ACTION_WAIT = "wait"
ACTION_TYPES = frozenset(
    {
        ACTION_KEY,
        ACTION_HOLD,
        ACTION_RELEASE,
        ACTION_CLICK,
        ACTION_MOUSE_DOWN,
        ACTION_MOUSE_UP,
        ACTION_WAIT,
    }
)


@dataclass(frozen=True)
class Action:
    """一个原子动作。

    ``duration`` 的含义随类型变化：
    - ``key`` / ``click``：按住多久后松开
    - ``wait``：等待多久
    - ``hold`` / ``release`` / ``mouse_*``：忽略
    """

    type: str
    key: str = ""
    duration: float = 0.0

    def describe(self) -> str:
        if self.type == ACTION_WAIT:
            return f"wait({self.duration:g}s)"
        if self.duration > 0:
            return f"{self.type}({self.key},{self.duration:g}s)"
        return f"{self.type}({self.key})"


@dataclass(frozen=True)
class Rule:
    """一条规则：条件满足时执行动作序列。

    ``cooldown`` 是规则自身的冷却（秒），用来表达技能 CD；
    ``priority`` 越小越优先；``once`` 表示整场战斗只触发一次。
    """

    name: str
    condition: Any  # conditions.Condition，避免循环导入所以不标注具体类型
    actions: tuple[Action, ...]
    cooldown: float = 0.0
    priority: int = DEFAULT_PRIORITY
    once: bool = False
    interruptible: bool = True

    @property
    def total_duration(self) -> float:
        """动作序列的名义总时长，用于估算规则占用时间。"""
        return sum(a.duration for a in self.actions)


@dataclass(frozen=True)
class CombatScript:
    """一份完整的战斗脚本。"""

    name: str = "unnamed"
    rules: tuple[Rule, ...] = ()
    # 兜底动作：所有规则都不满足时执行（通常是普攻）
    fallback: tuple[Action, ...] = ()
    fallback_interval: float = 0.0
    # 队伍编成（identity.Roster）。战斗中认不出侧栏头像，所以 character
    # 条件依赖用户在这里显式声明站位；未声明时 character 条件恒为假。
    roster: Any = None
    # 解析过程中发现的问题，供 CI 与日志使用；不影响运行
    issues: tuple[str, ...] = ()

    def with_issues(self, extra) -> CombatScript:
        return replace(self, issues=tuple(self.issues) + tuple(extra))

    @property
    def sorted_rules(self) -> tuple[Rule, ...]:
        """按优先级升序排列；同优先级保持声明顺序（稳定排序）。"""
        return tuple(sorted(self.rules, key=lambda r: r.priority))


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------


def _to_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _to_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return int(round(parsed))


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return default


def _clamp(value, low, high):
    return max(low, min(high, value))


def parse_action(raw: Any, issues: list[str], where: str) -> Action | None:
    """解析单个动作。

    支持两种写法：
    - 简写字符串：``"e"``（点按 E）、``"wait:0.5"``、``"hold:w"``、``"click:left"``
    - 完整对象：``{"type": "key", "key": "e", "duration": 0.1}``

    非法动作返回 ``None`` 并记录 issue —— 宁可少发一个键，也不要在战斗中
    发出一个含义不明的输入。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            issues.append(f"{where}: 空动作")
            return None
        if ":" in text:
            head, _, tail = text.partition(":")
            head = head.strip().lower()
            tail = tail.strip()
            if head == ACTION_WAIT:
                duration = _to_float(tail, -1.0)
                if duration < 0:
                    issues.append(f"{where}: wait 时长非法 {tail!r}")
                    return None
                return Action(
                    type=ACTION_WAIT,
                    duration=_clamp(duration, MIN_ACTION_DURATION, MAX_ACTION_DURATION),
                )
            if head in ACTION_TYPES:
                return parse_action(
                    {"type": head, "key": tail}, issues, where
                )
            issues.append(f"{where}: 未知动作类型 {head!r}")
            return None
        # 裸键名 -> 点按
        return parse_action({"type": ACTION_KEY, "key": text}, issues, where)

    if not isinstance(raw, dict):
        issues.append(f"{where}: 动作必须是字符串或对象，得到 {type(raw).__name__}")
        return None

    action_type = str(raw.get("type", ACTION_KEY)).strip().lower()
    if action_type not in ACTION_TYPES:
        issues.append(f"{where}: 未知动作类型 {action_type!r}")
        return None

    duration = _clamp(
        _to_float(raw.get("duration", 0.0), 0.0),
        MIN_ACTION_DURATION,
        MAX_ACTION_DURATION,
    )

    if action_type == ACTION_WAIT:
        if duration <= 0:
            issues.append(f"{where}: wait 时长必须大于 0")
            return None
        return Action(type=ACTION_WAIT, duration=duration)

    key = str(raw.get("key", "")).strip().lower()
    if not key:
        issues.append(f"{where}: {action_type} 缺少 key")
        return None

    if action_type in {ACTION_CLICK, ACTION_MOUSE_DOWN, ACTION_MOUSE_UP}:
        if key not in ALLOWED_MOUSE:
            issues.append(
                f"{where}: 鼠标键 {key!r} 不在允许列表 {sorted(ALLOWED_MOUSE)}"
            )
            return None
    elif key not in ALLOWED_KEYS:
        issues.append(f"{where}: 按键 {key!r} 不在允许列表")
        return None

    return Action(type=action_type, key=key, duration=duration)


def parse_actions(raw: Any, issues: list[str], where: str) -> tuple[Action, ...]:
    """解析动作序列，并对总时长做上限保护。"""
    if raw is None:
        return ()
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        issues.append(f"{where}: actions 必须是数组")
        return ()

    actions: list[Action] = []
    for index, item in enumerate(raw):
        if len(actions) >= MAX_ACTIONS_PER_RULE:
            issues.append(
                f"{where}: 动作数超过上限 {MAX_ACTIONS_PER_RULE}，已截断"
            )
            break
        action = parse_action(item, issues, f"{where}.actions[{index}]")
        if action is not None:
            actions.append(action)

    total = sum(a.duration for a in actions)
    if total > MAX_SEQUENCE_DURATION:
        issues.append(
            f"{where}: 动作序列总时长 {total:.2f}s 超过上限 "
            f"{MAX_SEQUENCE_DURATION}s，可能导致长时间无法响应"
        )
    return tuple(actions)


def parse_rule(raw: Any, issues: list[str], index: int):
    """解析一条规则。缺条件视为恒真，缺动作则丢弃该规则。"""
    from .conditions import ALWAYS, bind_every_keys, parse_condition

    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        issues.append(f"{where}: 规则必须是对象")
        return None

    name = str(raw.get("name", f"rule{index}")).strip() or f"rule{index}"
    where = f"rules[{index}]({name})"

    actions = parse_actions(raw.get("actions", raw.get("action")), issues, where)
    if not actions:
        issues.append(f"{where}: 没有可执行动作，规则被忽略")
        return None

    condition_raw = raw.get("when", raw.get("condition"))
    if condition_raw is None:
        condition = ALWAYS
    else:
        condition = parse_condition(condition_raw, issues, where)
        if condition is None:
            issues.append(f"{where}: 条件非法，规则被忽略")
            return None
    # every 条件需要知道自己属于哪条规则，才能查到上次触发时间
    condition = bind_every_keys(condition, name)

    cooldown = _clamp(_to_float(raw.get("cooldown", 0.0), 0.0), 0.0, MAX_COOLDOWN)
    priority = _to_int(raw.get("priority", DEFAULT_PRIORITY), DEFAULT_PRIORITY)

    return Rule(
        name=name,
        condition=condition,
        actions=actions,
        cooldown=cooldown,
        priority=priority,
        once=_to_bool(raw.get("once"), False),
        interruptible=_to_bool(raw.get("interruptible"), True),
    )


def parse_script(raw: Any) -> CombatScript:
    """把 JSON 数据（或已解析的 dict）转成 CombatScript。"""
    issues: list[str] = []

    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            return CombatScript(issues=(f"脚本 JSON 解析失败: {exc}",))

    if raw is None:
        return CombatScript(issues=("脚本为空",))
    if not isinstance(raw, dict):
        return CombatScript(issues=(f"脚本根节点必须是对象，得到 {type(raw).__name__}",))

    name = str(raw.get("name", "unnamed")).strip() or "unnamed"

    raw_rules = raw.get("rules", [])
    if not isinstance(raw_rules, (list, tuple)):
        issues.append("rules 必须是数组")
        raw_rules = []
    if len(raw_rules) > MAX_RULES:
        issues.append(f"规则数超过上限 {MAX_RULES}，已截断")
        raw_rules = list(raw_rules)[:MAX_RULES]

    rules: list[Rule] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw_rules):
        rule = parse_rule(item, issues, index)
        if rule is None:
            continue
        if rule.name in seen_names:
            issues.append(f"规则名重复: {rule.name!r}（后者仍会生效，但日志会混淆）")
        seen_names.add(rule.name)
        rules.append(rule)

    fallback = parse_actions(raw.get("fallback"), issues, "fallback")
    fallback_interval = _clamp(
        _to_float(raw.get("fallback_interval", 0.0), 0.0), 0.0, MAX_COOLDOWN
    )

    # 队伍编成：character 条件要靠它把槽位翻译成角色身份
    from ..identity.roster import parse_roster

    roster = parse_roster(raw.get("roster"))
    issues.extend(roster.issues)

    # 用了 character 条件但没声明编成 -> 条件永远不会成立，必须提醒
    if not roster.configured and _uses_character_condition(rules):
        issues.append(
            "脚本用了 character 条件但没有声明 roster，"
            "战斗中无法确定角色身份，这些条件将永远不成立；"
            "请在脚本里加 \"roster\": {\"1\": \"角色名\", ...}"
        )

    if not rules and not fallback:
        issues.append("脚本既没有有效规则也没有兜底动作，引擎将无事可做")

    return CombatScript(
        name=name,
        rules=tuple(rules),
        fallback=fallback,
        fallback_interval=fallback_interval,
        roster=roster,
        issues=tuple(issues),
    )


def _uses_character_condition(rules) -> bool:
    """检查规则里是否用到了 character 条件（需要递归进组合条件）。"""
    from .conditions import All, Any_, IsCharacter, Not

    def walk(condition) -> bool:
        if isinstance(condition, IsCharacter):
            return True
        if isinstance(condition, Not):
            return walk(condition.inner)
        if isinstance(condition, (All, Any_)):
            return any(walk(item) for item in condition.items)
        return False

    return any(walk(rule.condition) for rule in rules)
