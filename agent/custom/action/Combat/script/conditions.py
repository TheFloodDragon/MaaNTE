"""战斗条件的解析与求值。

条件是**纯函数**：``evaluate(state) -> bool``，其中 ``state`` 是一个只读
的 ``CombatState`` 快照。这样做的理由：

1. 可离线测试 —— 不需要游戏、不需要截图，构造 state 即可断言；
2. 求值无副作用 —— 引擎可以放心地对同一帧反复求值多个条件；
3. 出错可定位 —— 每个条件都能 ``describe()`` 自己，日志里看得懂。

支持的条件写法（JSON）::

    "always"                          # 恒真
    {"hp_below": 0.5}                 # 自身血量低于 50%
    {"enemy_visible": true}           # 视野内有敌人
    {"elapsed_above": 3.0}            # 本场战斗已进行超过 3 秒
    {"every": 5.0}                    # 每 5 秒满足一次
    {"all": [...]}                    # 与
    {"any": [...]}                    # 或
    {"not": {...}}                    # 非

刻意**不支持**任意表达式求值（没有 eval），因为脚本来自用户配置文件，
执行任意代码是安全问题；而且受限的条件集合更容易做静态校验。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CombatState:
    """引擎每帧交给条件求值的只读快照。

    字段刻意保持"能从画面直接读出来"的粒度。识别不到的量用 ``None``
    表示未知，而不是猜一个默认值——条件求值时把"未知"当作不满足，
    这样识别失效只会让脚本退回兜底动作，不会误触发大招。
    """

    # 时间
    elapsed: float = 0.0  # 本场战斗已进行秒数
    now: float = 0.0  # 单调时钟读数

    # 血量（0.0~1.0），None 表示未识别到
    self_hp: float | None = None
    boss_hp: float | None = None

    # 敌情
    enemy_visible: bool = False
    enemy_count: int = 0

    # 角色身份（identity 层填充）
    character: str | None = None
    slot: int = -1

    # 界面状态
    in_team: bool = True
    black_screen: bool = False
    # ESC 菜单是否打开。菜单打开时继续按键会点到菜单项上，
    # 因此这是一个必须让会话立刻停手的信号。
    menu_open: bool = False

    # 引擎运行时统计，供 every / count 类条件使用
    tick: int = 0
    rule_fire_counts: dict = field(default_factory=dict)
    rule_last_fired: dict = field(default_factory=dict)

    def hp_of(self, target: str) -> float | None:
        if target == "boss":
            return self.boss_hp
        return self.self_hp


class Condition:
    """条件基类。子类必须实现 ``evaluate`` 与 ``describe``。"""

    def evaluate(self, state: CombatState) -> bool:  # pragma: no cover - 抽象
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - 抽象
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.describe()}>"


class _Always(Condition):
    def evaluate(self, state):
        return True

    def describe(self):
        return "always"


class _Never(Condition):
    def evaluate(self, state):
        return False

    def describe(self):
        return "never"


ALWAYS = _Always()
NEVER = _Never()


@dataclass(frozen=True)
class Not(Condition):
    inner: Condition

    def evaluate(self, state):
        return not self.inner.evaluate(state)

    def describe(self):
        return f"not({self.inner.describe()})"


@dataclass(frozen=True)
class All(Condition):
    items: tuple[Condition, ...]

    def evaluate(self, state):
        return all(item.evaluate(state) for item in self.items)

    def describe(self):
        return "all(" + ", ".join(i.describe() for i in self.items) + ")"


@dataclass(frozen=True)
class Any_(Condition):
    items: tuple[Condition, ...]

    def evaluate(self, state):
        return any(item.evaluate(state) for item in self.items)

    def describe(self):
        return "any(" + ", ".join(i.describe() for i in self.items) + ")"


@dataclass(frozen=True)
class HpCompare(Condition):
    """血量比较。识别不到血量（None）时一律返回 False。"""

    target: str  # "self" | "boss"
    threshold: float
    below: bool

    def evaluate(self, state):
        value = state.hp_of(self.target)
        if value is None:
            return False
        return value < self.threshold if self.below else value > self.threshold

    def describe(self):
        op = "<" if self.below else ">"
        return f"{self.target}_hp{op}{self.threshold:g}"


@dataclass(frozen=True)
class EnemyVisible(Condition):
    expected: bool

    def evaluate(self, state):
        return bool(state.enemy_visible) == self.expected

    def describe(self):
        return f"enemy_visible=={self.expected}"


@dataclass(frozen=True)
class EnemyCount(Condition):
    threshold: int
    at_least: bool

    def evaluate(self, state):
        if self.at_least:
            return state.enemy_count >= self.threshold
        return state.enemy_count <= self.threshold

    def describe(self):
        op = ">=" if self.at_least else "<="
        return f"enemy_count{op}{self.threshold}"


@dataclass(frozen=True)
class ElapsedCompare(Condition):
    threshold: float
    above: bool

    def evaluate(self, state):
        return (
            state.elapsed > self.threshold
            if self.above
            else state.elapsed < self.threshold
        )

    def describe(self):
        op = ">" if self.above else "<"
        return f"elapsed{op}{self.threshold:g}"


@dataclass(frozen=True)
class IsCharacter(Condition):
    """当前操作角色是否为指定角色（identity 层提供）。"""

    names: tuple[str, ...]

    def evaluate(self, state):
        if state.character is None:
            return False
        return state.character.lower() in self.names

    def describe(self):
        return f"character in {list(self.names)}"


@dataclass(frozen=True)
class IsSlot(Condition):
    slot: int

    def evaluate(self, state):
        return state.slot == self.slot

    def describe(self):
        return f"slot=={self.slot}"


@dataclass(frozen=True)
class Every(Condition):
    """周期性条件：距离上次触发满 ``interval`` 秒才成立。

    这个条件有状态语义，但状态存在 ``CombatState.rule_last_fired`` 里
    （由引擎维护），条件自身仍是纯函数。``key`` 由引擎在绑定规则时注入。
    """

    interval: float
    key: str = ""

    def evaluate(self, state):
        last = state.rule_last_fired.get(self.key)
        if last is None:
            return True
        return (state.now - last) >= self.interval

    def describe(self):
        return f"every({self.interval:g}s)"


@dataclass(frozen=True)
class InTeam(Condition):
    expected: bool

    def evaluate(self, state):
        return bool(state.in_team) == self.expected

    def describe(self):
        return f"in_team=={self.expected}"


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


def _parse_ratio(value: Any, issues: list[str], where: str) -> float | None:
    """解析 0~1 的比例；也接受 0~100 的百分数写法。"""
    parsed = _to_float(value, float("nan"))
    if not math.isfinite(parsed):
        issues.append(f"{where}: 比例值非法 {value!r}")
        return None
    if parsed > 1.0:
        if parsed <= 100.0:
            parsed = parsed / 100.0
        else:
            issues.append(f"{where}: 比例值 {value!r} 超出范围，已钳制到 1.0")
            parsed = 1.0
    if parsed < 0.0:
        issues.append(f"{where}: 比例值 {value!r} 为负，已钳制到 0")
        parsed = 0.0
    return parsed


_SIMPLE = {
    "always": ALWAYS,
    "never": NEVER,
}


def parse_condition(raw: Any, issues: list[str], where: str) -> Condition | None:
    """把 JSON 条件转成 Condition 对象；非法返回 None 并记录 issue。"""
    if raw is None:
        return None

    if isinstance(raw, bool):
        return ALWAYS if raw else NEVER

    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _SIMPLE:
            return _SIMPLE[text]
        issues.append(f"{where}: 未知条件 {raw!r}")
        return None

    if isinstance(raw, (list, tuple)):
        # 数组视为 all
        return parse_condition({"all": list(raw)}, issues, where)

    if not isinstance(raw, dict):
        issues.append(f"{where}: 条件必须是对象/字符串，得到 {type(raw).__name__}")
        return None

    if len(raw) > 1:
        # 多键对象视为 all，避免用户误以为 {"a":..,"b":..} 是或关系
        parts = []
        for key, value in raw.items():
            part = parse_condition({key: value}, issues, where)
            if part is None:
                return None
            parts.append(part)
        return All(items=tuple(parts))

    if not raw:
        issues.append(f"{where}: 空条件对象")
        return None

    key, value = next(iter(raw.items()))
    key = str(key).strip().lower()

    if key == "not":
        inner = parse_condition(value, issues, f"{where}.not")
        return None if inner is None else Not(inner=inner)

    if key in {"all", "any"}:
        if not isinstance(value, (list, tuple)):
            issues.append(f"{where}.{key}: 必须是数组")
            return None
        if not value:
            issues.append(f"{where}.{key}: 数组为空")
            return None
        items = []
        for index, item in enumerate(value):
            parsed = parse_condition(item, issues, f"{where}.{key}[{index}]")
            if parsed is None:
                return None
            items.append(parsed)
        return All(tuple(items)) if key == "all" else Any_(tuple(items))

    if key in {"hp_below", "hp_above", "self_hp_below", "self_hp_above"}:
        ratio = _parse_ratio(value, issues, f"{where}.{key}")
        if ratio is None:
            return None
        return HpCompare(
            target="self", threshold=ratio, below=key.endswith("below")
        )

    if key in {"boss_hp_below", "boss_hp_above"}:
        ratio = _parse_ratio(value, issues, f"{where}.{key}")
        if ratio is None:
            return None
        return HpCompare(target="boss", threshold=ratio, below=key.endswith("below"))

    if key == "enemy_visible":
        return EnemyVisible(expected=bool(value))

    if key in {"enemy_count_at_least", "enemy_count_at_most"}:
        count = _to_int(value, -1)
        if count < 0:
            issues.append(f"{where}.{key}: 数量非法 {value!r}")
            return None
        return EnemyCount(threshold=count, at_least=key.endswith("at_least"))

    if key in {"elapsed_above", "elapsed_below"}:
        seconds = _to_float(value, -1.0)
        if seconds < 0:
            issues.append(f"{where}.{key}: 秒数非法 {value!r}")
            return None
        return ElapsedCompare(threshold=seconds, above=key.endswith("above"))

    if key in {"character", "character_in"}:
        if isinstance(value, str):
            names = (value.strip().lower(),)
        elif isinstance(value, (list, tuple)):
            names = tuple(
                str(item).strip().lower() for item in value if str(item).strip()
            )
        else:
            issues.append(f"{where}.{key}: 角色名必须是字符串或数组")
            return None
        if not names:
            issues.append(f"{where}.{key}: 角色名为空")
            return None
        return IsCharacter(names=names)

    if key == "slot":
        slot = _to_int(value, -99)
        if not 0 <= slot <= 3:
            issues.append(f"{where}.slot: 槽位必须是 0~3，得到 {value!r}")
            return None
        return IsSlot(slot=slot)

    if key == "every":
        interval = _to_float(value, -1.0)
        if interval <= 0:
            issues.append(f"{where}.every: 间隔必须大于 0")
            return None
        return Every(interval=interval)

    if key == "in_team":
        return InTeam(expected=bool(value))

    issues.append(f"{where}: 未知条件键 {key!r}")
    return None


def bind_every_keys(condition: Condition, rule_name: str) -> Condition:
    """给条件树里的 ``Every`` 注入规则名，使其能查到自己的上次触发时间。

    解析阶段不知道规则名（条件可能被复用），所以在规则构造完成后统一绑定。
    """
    if isinstance(condition, Every):
        return Every(interval=condition.interval, key=rule_name)
    if isinstance(condition, Not):
        return Not(inner=bind_every_keys(condition.inner, rule_name))
    if isinstance(condition, All):
        return All(
            items=tuple(bind_every_keys(i, rule_name) for i in condition.items)
        )
    if isinstance(condition, Any_):
        return Any_(
            items=tuple(bind_every_keys(i, rule_name) for i in condition.items)
        )
    return condition
