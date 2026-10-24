"""战斗选项合并。

优先级（后者覆盖前者）：内置默认 < 全局战斗配置载体节点 < 调用方 custom_action_param
< 调用方 option_nodes 载体节点。None 与空字符串视为"未设置"，不覆盖已有值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import Iterable, Mapping

from .model import TOGGLE_ACTIONS
from .scheme import BUILTIN_SCHEMES, DEFAULT_SCHEME

# 全局战斗配置（MXU 设置页「战斗配置」「战斗策略」）写入的载体节点，见 pipeline/Combat/Combat.json。
# 每个选项、每个多选项各写自己的节点，互不覆盖
GLOBAL_SETTING_NODES = (
    "CombatSetting_Scheme",
    "CombatSetting_CustomScheme",
    "CombatSetting_TeamSize",
    *("CombatSetting_Disable%s" % name.capitalize() for name in TOGGLE_ACTIONS),
    "CombatSetting_LockTarget",
    "CombatSetting_SearchTurn",
    "CombatSetting_ClearConfirm",
)
SCHEME_CHOICES = frozenset(BUILTIN_SCHEMES) | {"custom"}


@dataclass(frozen=True)
class CombatOptions:
    scheme: str = DEFAULT_SCHEME
    custom_scheme: str = ""
    wait_timeout: float = 30.0  # 等待进战；0 = 一直等
    max_duration: float = 300.0  # 单场最长；0 = 不限
    clear_confirm: float = 3.0  # 敌人血条持续消失多久判定脱战
    hud_lost_timeout: float = 10.0  # 战斗中 HUD 连续消失多久结束；0 = 不判断
    team_size: int = 4  # 队伍人数，超出的槽位不参与切人
    lock_target: bool = True  # 开战中键锁敌、敌人丢失时重新锁定
    search_turn: bool = False  # 敌人丢失时转动视角寻找
    use_ultimate: bool = True
    use_skill: bool = True
    use_arc: bool = True
    use_dodge: bool = True
    use_jump: bool = True
    use_switch: bool = True

    @property
    def disabled_actions(self) -> tuple[str, ...]:
        """被战斗策略禁用的动作（按 TOGGLE_ACTIONS 顺序）。"""
        return tuple(a for a in TOGGLE_ACTIONS if not getattr(self, "use_" + a))


OPTION_KEYS = frozenset(f.name for f in fields(CombatOptions))
_BOOL_KEYS = frozenset({"lock_target", "search_turn"} | {"use_" + a for a in TOGGLE_ACTIONS})
_INT_BOUNDS = {"team_size": (1, 4)}
# 数值选项范围：(下限, 上限)
_BOUNDS = {
    "wait_timeout": (0.0, 86400.0),
    "max_duration": (0.0, 86400.0),
    "clear_confirm": (0.5, 60.0),
    "hud_lost_timeout": (0.0, 600.0),
}
_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})


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


def _parse(key: str, value):
    if key == "scheme":
        choice = str(value).strip().lower()
        if choice not in SCHEME_CHOICES:
            raise ValueError(value)
        return choice
    if key == "custom_scheme":
        return str(value).strip()
    if key in _BOOL_KEYS:
        return _boolean(value)
    if key in _INT_BOUNDS:
        return _integer(key, value)
    return _number(key, value)


def merge_options(layers: Iterable[Mapping], base: CombatOptions | None = None) -> tuple[CombatOptions, list[str]]:
    """依次合并多层选项，返回 (结果, 警告)。非法值保留上一层的值并记警告，不中断战斗。"""
    options = base or CombatOptions()
    warnings: list[str] = []
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        changes = {}
        for key, value in layer.items():
            if key not in OPTION_KEYS:
                warnings.append("unknown combat option: %s" % key)
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            try:
                changes[key] = _parse(key, value)
            except (TypeError, ValueError, OverflowError):
                warnings.append("invalid combat option %s=%r" % (key, value))
        options = replace(options, **changes)
    return options, warnings


def node_attach(get_node_data, name: str) -> dict:
    """读取载体节点的 attach（被 pipeline_override 覆盖后的当前值）。读取失败返回空。"""
    try:
        data = get_node_data(name) or {}
    except Exception:
        return {}
    attach = data.get("attach") if isinstance(data, dict) else None
    return dict(attach) if isinstance(attach, dict) else {}


def collect_layers(get_node_data, overrides: Mapping | None = None, option_nodes: Iterable[str] = ()) -> list[dict]:
    layers = [node_attach(get_node_data, name) for name in GLOBAL_SETTING_NODES]
    layers.append({k: v for k, v in (overrides or {}).items() if k != "option_nodes"})
    layers.extend(node_attach(get_node_data, name) for name in option_nodes)
    return layers
