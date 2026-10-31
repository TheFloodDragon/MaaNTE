"""战斗选项合并。

优先级（后者覆盖前者）：内置默认 < 全局战斗配置载体节点 < 调用方 custom_action_param
< 调用方 option_nodes 载体节点。None 与空字符串视为"未设置"，不覆盖已有值。

既有的简单选项非法时告警并保留上一层的值；新增的节奏/轮转/槽位选项非法时记为错误，
由 prepare_combat 在第一笔游戏输入前报出，不会静默换成别的出招。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace
from typing import Iterable, Mapping

from .keys import SPEED_MAX, SPEED_MIN
from .model import END_REASONS, EndReason, TOGGLE_ACTIONS
from .scheme import BUILTIN_SCHEMES, DEFAULT_SCHEME, MAX_FIELD_TIME, MAX_ORDER

SLOTS = (1, 2, 3, 4)
# 全局战斗配置（MXU 设置页）写入的载体节点，见 pipeline/Combat/Combat.json。每个选项、每个多选项各写自己的节点
GLOBAL_SETTING_NODES = (
    "CombatSetting_Scheme",
    "CombatSetting_CustomScheme",
    "CombatSetting_TeamSize",
    *("CombatSetting_Disable%s" % name.capitalize() for name in TOGGLE_ACTIONS),
    "CombatSetting_LockTarget",
    "CombatSetting_SearchTurn",
    "CombatSetting_ClearConfirm",
    "CombatSetting_Speed",
    "CombatSetting_AssistDetection",
    "CombatSetting_RotationMode",
    "CombatSetting_RotationOrder",
    "CombatSetting_FieldTime",
    *("CombatSetting_DisableSlot%d" % slot for slot in SLOTS),
    *("CombatSetting_Slot%d" % slot for slot in SLOTS),
)
SCHEME_CHOICES = frozenset(BUILTIN_SCHEMES) | {"custom"}
# custom_action_param 中不属于战斗选项的键
RESERVED_PARAM_KEYS = frozenset({"option_nodes", "signal_nodes", "accepted_reasons"})
_SLOT_FIELDS = ("enabled", "character", "note", "combo", "min_field_time")
MAX_TEXT = 4000


@dataclass(frozen=True)
class SlotOverride:
    """单个槽位的调整。None / 空字符串表示沿用方案。"""

    enabled: bool | None = None
    character: str = ""
    note: str = ""
    combo: str = ""
    min_field_time: float | None = None

    @property
    def empty(self) -> bool:
        return self == SlotOverride()


_EMPTY_SLOTS = tuple(SlotOverride() for _ in SLOTS)


@dataclass(frozen=True)
class CombatOptions:
    scheme: str = DEFAULT_SCHEME
    custom_scheme: str = ""
    wait_timeout: float = 30.0  # 等待进战；0 = 一直等
    max_duration: float = 300.0  # 单场最长；0 = 不限
    clear_confirm: float = 3.0  # 目标持续丢失多久停止本场（不代表确认清场）
    hud_lost_timeout: float = 10.0  # 战斗中 HUD 连续消失多久结束；0 = 不判断
    team_size: int = 4  # 队伍人数，超出的槽位不参与切人
    lock_target: bool = True
    search_turn: bool = False
    use_ultimate: bool = True
    use_skill: bool = True
    use_arc: bool = True
    use_dodge: bool = True
    use_jump: bool = True
    use_switch: bool = True
    combat_speed: float = 1.0  # 输入节奏倍率（不是游戏速度）
    assist_detection: bool = True  # 血条消失时用敌人等级 / Boss 等级文字辅助确认
    rotation_mode: str = ""  # "" 沿用方案，否则 cycle / stay
    rotation_order: tuple[int, ...] = ()  # 空 = 沿用方案
    min_field_time: float | None = None  # None = 沿用方案
    slots: tuple[SlotOverride, ...] = field(default=_EMPTY_SLOTS)

    @property
    def disabled_actions(self) -> tuple[str, ...]:
        """被战斗策略禁用的动作（按 TOGGLE_ACTIONS 顺序）。"""
        return tuple(a for a in TOGGLE_ACTIONS if not getattr(self, "use_" + a))

    @property
    def disabled_slots(self) -> tuple[int, ...]:
        return tuple(s for s, o in zip(SLOTS, self.slots) if o.enabled is False)

    def slot(self, slot: int) -> SlotOverride:
        return self.slots[slot - 1]


OPTION_KEYS = frozenset(f.name for f in fields(CombatOptions))
_BOOL_KEYS = frozenset(
    {"lock_target", "search_turn", "assist_detection"}
    | {"use_" + a for a in TOGGLE_ACTIONS}
)
_INT_BOUNDS = {"team_size": (1, 4)}
_BOUNDS = {
    "wait_timeout": (0.0, 86400.0),
    "max_duration": (0.0, 86400.0),
    "clear_confirm": (0.5, 60.0),
    "hud_lost_timeout": (0.0, 600.0),
    "combat_speed": (SPEED_MIN, SPEED_MAX),
    "min_field_time": (0.0, MAX_FIELD_TIME),
}
# 新增选项：非法值是配置错误，开战前报出
STRICT_KEYS = frozenset(
    {
        "combat_speed",
        "assist_detection",
        "rotation_mode",
        "rotation_order",
        "min_field_time",
        "slots",
    }
)
_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})
_COMMA = str.maketrans({"，": ",", "、": ",", " ": "", "\t": ""})


def _number(key: str, value) -> float:
    if isinstance(value, bool):
        raise ValueError(value)
    number = float(str(value).strip()) if isinstance(value, str) else float(value)
    low, high = _BOUNDS[key]
    if not low <= number <= high:  # NaN 比较恒为 False，同样被拒绝
        raise ValueError(value)
    return number


def _integer(key: str, value) -> int:
    if isinstance(value, bool):
        raise ValueError(value)
    number = float(str(value).strip()) if isinstance(value, str) else float(value)
    low, high = _INT_BOUNDS[key]
    if not math.isfinite(number) or number != int(number) or not low <= number <= high:
        raise ValueError(value)
    return int(number)


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(value)


def parse_order(value) -> tuple[int, ...]:
    """轮转顺序：'1,2,1,3' 或 [1, 2, 1, 3]；1~16 项，每项 1~4。"""
    if isinstance(value, str):
        text = value.translate(_COMMA).strip(",")
        items = [part for part in text.split(",")] if text else []
        if any(not part.isdigit() for part in items):
            raise ValueError(value)
        order = [int(part) for part in items]
    elif isinstance(value, (list, tuple)):
        if any(isinstance(s, bool) or not isinstance(s, int) for s in value):
            raise ValueError(value)
        order = list(value)
    else:
        raise ValueError(value)
    if not 1 <= len(order) <= MAX_ORDER or not all(1 <= s <= 4 for s in order):
        raise ValueError(value)
    return tuple(order)


def _text(value) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ValueError(value)
    return value.strip()


def _merge_slots(current: tuple[SlotOverride, ...], value) -> tuple[SlotOverride, ...]:
    """按槽位、按字段合并。空字符串 / None 沿用；明确的 false 保留。"""
    if not isinstance(value, Mapping):
        raise ValueError(value)
    slots = list(current)
    for raw_key, body in value.items():
        key = str(raw_key).strip()
        if key not in {"1", "2", "3", "4"} or not isinstance(body, Mapping):
            raise ValueError(value)
        index = int(key) - 1
        changes = {}
        for name, item in body.items():
            if name not in _SLOT_FIELDS:
                raise ValueError("slots.%s.%s" % (key, name))
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            if name == "enabled":
                changes[name] = _boolean(item)
            elif name == "min_field_time":
                changes[name] = _number("min_field_time", item)
            else:
                changes[name] = _text(item)
        slots[index] = replace(slots[index], **changes)
    return tuple(slots)


def _parse(key: str, value, options: CombatOptions):
    if key == "scheme":
        choice = str(value).strip().lower()
        if choice not in SCHEME_CHOICES:
            raise ValueError(value)
        return choice
    if key == "custom_scheme":
        return str(value).strip()
    if key == "rotation_mode":
        choice = str(value).strip().lower()
        if choice not in ("cycle", "stay"):
            raise ValueError(value)
        return choice
    if key == "rotation_order":
        return parse_order(value)
    if key == "slots":
        return _merge_slots(options.slots, value)
    if key in _BOOL_KEYS:
        return _boolean(value)
    if key in _INT_BOUNDS:
        return _integer(key, value)
    return _number(key, value)


def merge_options_strict(
    layers: Iterable[Mapping], base: CombatOptions | None = None
) -> tuple[CombatOptions, list[str], list[tuple[str, object]]]:
    """返回 (结果, 警告, 错误)。错误项为 (键, 原值)，只来自 STRICT_KEYS。"""
    options = base or CombatOptions()
    warnings: list[str] = []
    errors: list[tuple[str, object]] = []
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        for key, value in layer.items():
            if key not in OPTION_KEYS:
                warnings.append("unknown combat option: %s" % key)
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if key == "slots" and isinstance(value, Mapping) and not value:
                continue
            try:
                options = replace(options, **{key: _parse(key, value, options)})
            except (TypeError, ValueError, OverflowError):
                if key in STRICT_KEYS:
                    errors.append((key, value))
                else:
                    warnings.append("invalid combat option %s=%r" % (key, value))
    return options, warnings, errors


def merge_options(
    layers: Iterable[Mapping], base: CombatOptions | None = None
) -> tuple[CombatOptions, list[str]]:
    """兼容旧接口：返回 (结果, 警告)，新增选项的错误也作为警告列出。"""
    options, warnings, errors = merge_options_strict(layers, base)
    warnings += ["invalid combat option %s=%r" % item for item in errors]
    return options, warnings


def node_attach(get_node_data, name: str) -> dict:
    """读取载体节点的 attach（被 pipeline_override 覆盖后的当前值）。读取失败返回空。"""
    try:
        data = get_node_data(name) or {}
    except Exception:
        return {}
    attach = data.get("attach") if isinstance(data, dict) else None
    return dict(attach) if isinstance(attach, dict) else {}


def collect_layers(
    get_node_data, overrides: Mapping | None = None, option_nodes: Iterable[str] = ()
) -> list[dict]:
    layers = [node_attach(get_node_data, name) for name in GLOBAL_SETTING_NODES]
    layers.append(
        {k: v for k, v in (overrides or {}).items() if k not in RESERVED_PARAM_KEYS}
    )
    layers.extend(node_attach(get_node_data, name) for name in option_nodes)
    return layers


DEFAULT_ACCEPTED = frozenset({EndReason.CLEARED})


def parse_accepted_reasons(value) -> tuple[frozenset[str], list[str]]:
    """combat_run 的 accepted_reasons：哪些结束原因让 Pipeline 动作视为成功。

    只影响 Pipeline 是否走 next，不改变 CombatResult.success。非法值回退默认（仅 cleared）。
    """
    if value is None:
        return DEFAULT_ACCEPTED, []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        return DEFAULT_ACCEPTED, ["invalid accepted_reasons %r" % (value,)]
    reasons = {str(v).strip() for v in value}
    unknown = reasons - END_REASONS
    if unknown:
        return DEFAULT_ACCEPTED, ["unknown accepted_reasons %s" % sorted(unknown)]
    return frozenset(reasons), []
