"""战斗选项合并。

优先级（后者覆盖前者）：内置默认 < 全局战斗配置载体节点 < 调用方 custom_action_param
< 调用方 option_nodes 载体节点。None 与空字符串视为"未设置"，不覆盖已有值。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Iterable, Mapping

from .scheme import BUILTIN_SCHEMES, DEFAULT_SCHEME

# 全局战斗配置（MXU 设置页「战斗配置」）写入的载体节点，见 pipeline/Combat/Combat.json
GLOBAL_SETTING_NODES = ("CombatSetting_Scheme", "CombatSetting_CustomScheme")
SCHEME_CHOICES = frozenset(BUILTIN_SCHEMES) | {"custom"}

# 数值选项范围：(下限, 上限)
_BOUNDS = {
    "wait_timeout": (0.0, 86400.0),
    "max_duration": (0.0, 86400.0),
    "clear_confirm": (0.5, 60.0),
    "hud_lost_timeout": (0.0, 600.0),
}


@dataclass(frozen=True)
class CombatOptions:
    scheme: str = DEFAULT_SCHEME
    custom_scheme: str = ""
    wait_timeout: float = 30.0  # 等待进战；0 = 一直等
    max_duration: float = 300.0  # 单场最长；0 = 不限
    clear_confirm: float = 3.0  # 敌人血条持续消失多久判定脱战
    hud_lost_timeout: float = 10.0  # 战斗中 HUD 连续消失多久结束；0 = 不判断


OPTION_KEYS = frozenset(f.name for f in fields(CombatOptions))


def _number(key: str, value) -> float:
    if isinstance(value, bool):
        raise ValueError(value)
    number = float(str(value).strip()) if isinstance(value, str) else float(value)
    low, high = _BOUNDS[key]
    if not low <= number <= high:
        raise ValueError(value)
    return number


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
                if key == "scheme":
                    choice = str(value).strip().lower()
                    if choice not in SCHEME_CHOICES:
                        raise ValueError(value)
                    changes[key] = choice
                elif key == "custom_scheme":
                    changes[key] = str(value).strip()
                else:
                    changes[key] = _number(key, value)
            except (TypeError, ValueError):
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
