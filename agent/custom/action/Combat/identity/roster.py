"""队伍编成：把"槽位 -> 角色"的映射交给用户显式声明。

这是对 ``matcher`` 能力缺口的**务实补位**：既然战斗中还认不出侧栏头像，
就让用户在脚本里写清楚自己的队伍站位::

    "roster": {"1": "mint", "2": "skia", "3": "zero", "4": "hotori"}

于是 ``{"character": "mint"}`` 这类条件可以基于"当前槽位 + 用户声明的编成"
求值，不依赖头像识别。槽位识别本身是可靠的（复用粉爪已验证的高亮打分）。

代价与前提要写清楚：**用户声明与实际队伍不一致时，条件会按错误身份求值。**
所以 ``Roster`` 只在用户显式配置时才启用；没配置就退回"身份未知"，
让 character 条件恒为假，而不是瞎猜一个默认编成。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..kernel.constants import CURRENT_CHAR_SLOT_COUNT
from . import catalog


@dataclass(frozen=True)
class Roster:
    """槽位到角色键名的映射。

    ``slots`` 用 0-based 索引（与内核槽位索引一致），但用户在 JSON 里
    习惯写 1-based 的按键号，解析时负责换算。
    """

    slots: tuple[str | None, ...] = (None,) * CURRENT_CHAR_SLOT_COUNT
    issues: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        """是否至少声明了一个槽位。"""
        return any(item is not None for item in self.slots)

    def character_at(self, slot: int) -> str | None:
        """返回指定槽位的角色键名；未声明或越界返回 None。"""
        if not 0 <= slot < len(self.slots):
            return None
        return self.slots[slot]

    def slot_of(self, name: str) -> int:
        """返回指定角色所在槽位；未声明返回 -1。"""
        entry = catalog.resolve(name)
        if entry is None:
            return -1
        for index, key in enumerate(self.slots):
            if key == entry.key:
                return index
        return -1

    def key_for(self, name: str) -> str | None:
        """返回切到指定角色需要按的键（1-based 字符串）。"""
        slot = self.slot_of(name)
        if slot < 0:
            return None
        return str(slot + 1)

    def describe(self) -> str:
        parts = []
        for index, key in enumerate(self.slots):
            label = catalog.display_name(key) if key else "?"
            parts.append(f"{index + 1}:{label}")
        return " ".join(parts)


EMPTY_ROSTER = Roster()


def parse_roster(raw) -> Roster:
    """解析用户声明的队伍编成。

    接受两种写法：
    - 对象：``{"1": "mint", "3": "zero"}``（键是 1-based 按键号）
    - 数组：``["mint", "skia", "zero", "hotori"]``（按槽位顺序）

    无法解析的角色名会被忽略并记录 issue，不影响其它槽位。
    """
    issues: list[str] = []
    slots: list[str | None] = [None] * CURRENT_CHAR_SLOT_COUNT

    if raw is None:
        return Roster(slots=tuple(slots))

    if isinstance(raw, (list, tuple)):
        if len(raw) > CURRENT_CHAR_SLOT_COUNT:
            issues.append(
                f"roster 数组长度 {len(raw)} 超过槽位数 "
                f"{CURRENT_CHAR_SLOT_COUNT}，多余项被忽略"
            )
        for index, item in enumerate(raw[:CURRENT_CHAR_SLOT_COUNT]):
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            entry = catalog.resolve(item)
            if entry is None:
                issues.append(f"roster[{index}]: 未知角色 {item!r}")
                continue
            slots[index] = entry.key
    elif isinstance(raw, dict):
        for raw_key, item in raw.items():
            try:
                position = int(str(raw_key).strip())
            except (TypeError, ValueError):
                issues.append(f"roster: 槽位号非法 {raw_key!r}")
                continue
            if not 1 <= position <= CURRENT_CHAR_SLOT_COUNT:
                issues.append(
                    f"roster: 槽位号 {position} 超出 1~{CURRENT_CHAR_SLOT_COUNT}"
                )
                continue
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            entry = catalog.resolve(item)
            if entry is None:
                issues.append(f"roster[{raw_key}]: 未知角色 {item!r}")
                continue
            index = position - 1
            if slots[index] is not None:
                issues.append(f"roster: 槽位 {position} 被重复声明")
            slots[index] = entry.key
    else:
        issues.append(f"roster 必须是对象或数组，得到 {type(raw).__name__}")

    # 同一角色出现在多个槽位几乎肯定是配置错误
    seen: dict[str, int] = {}
    for index, key in enumerate(slots):
        if key is None:
            continue
        if key in seen:
            issues.append(
                f"roster: 角色 {catalog.display_name(key)} 同时出现在槽位 "
                f"{seen[key] + 1} 和 {index + 1}"
            )
        else:
            seen[key] = index

    return Roster(slots=tuple(slots), issues=tuple(issues))


def resolve_identity(roster: Roster, slot: int) -> str | None:
    """由槽位推出角色身份。

    这是编成路径的核心：槽位识别可靠 + 用户声明可信 => 身份可信。
    任一环节缺失都返回 None，让 character 条件退回"未知即不满足"。
    """
    if slot < 0:
        return None
    if not roster.configured:
        return None
    return roster.character_at(slot)
