"""刷本配置快照：只读选项载体与副本目录，不截图、不输入、不修改 Context。

reset 调用 load_config(context) 一次，得到本次任务的冻结快照；任何结构、取值或资源引用错误
都以 ValueError 报告，调用方在输入游戏前失败。

- 选项：每个界面选项写入各自独立的禁用载体节点（*_Option.attach），互不覆盖。
  缺少新增载体时按旧行为迁移（钟表把戏 / 仅40 / 用完预算 / 沿用当前队伍）；
  载体存在但字段缺失或取值非法时绝不静默默认。
- 目录：DungeonFarmProfiles.attach.profiles 保存有来源的副本资料。资料存在不等于可执行：
  只有 executable=true 且绑定完整的 profile 可被选中。
- 绑定：profile.bindings 把运行节点（DungeonStatus / DungeonFarm 中的识别与路线节点）
  映射到该副本独立的禁用模板节点。overrides 是模板的完整节点定义（仅 enabled 改为 true），
  交给 context.override_pipeline 整节点替换，不依赖也不保留上一次任务留下的字段。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

RUNS_NODE = "DungeonFarm_RunsOption"
STAGE_NODE = "DungeonFarm_StageOption"
TYPE_NODE = "DungeonFarm_TypeOption"
ENTRANCE_NODE = "DungeonFarm_EntranceOption"
PLAN_NODE = "DungeonFarm_PlanOption"
RESERVE_NODE = "DungeonFarm_ReserveOption"
REWARD_NODE = "DungeonFarm_RewardOption"
TEAM_NODE = "DungeonFarm_TeamOption"
PROFILES_NODE = "DungeonFarmProfiles"

# 载体 → (必需字段与旧行为默认值)。缺载体才用默认值。
OPTION_DEFAULTS = {
    RUNS_NODE: {"runs": "0"},
    STAGE_NODE: {"name": "clock_trick"},
    TYPE_NODE: {"type_id": "anomaly_domain"},
    ENTRANCE_NODE: {"entrance_id": "rabbithole"},
    PLAN_NODE: {"mode": "exhaust"},
    RESERVE_NODE: {"reserve": "0"},
    REWARD_NODE: {"reward_mode": "40"},
    TEAM_NODE: {"team_mode": "current"},
}
OPTION_NODES = tuple(OPTION_DEFAULTS)

MAX_RUNS = 999
MAX_RESERVE = 999999
PLAN_MODES = ("exhaust", "reserve")
REWARD_MODES = ("40", "80", "auto")
TEAM_MODES = ("current",)  # 不识别 / 创建 / 匹配队伍；预设队伍未取得界面证据，拒绝
REWARD_COSTS = (40, 80)

# 运行识别节点：模板只能识别、不能输入。
RECO_BINDINGS = (
    "DungeonEntrancePrompt",
    "DungeonStageListTitle",
    "DungeonStageEntry",
    "DungeonStageDetail",
    "DungeonEnterButton",
    "DungeonEntryBalance",
    "DungeonEnemyCounter",
    "DungeonRewardPrompt",
    "DungeonRewardPopup",
    "DungeonRewardOptions",
    "DungeonBalance",
    "DungeonClaimButton",
    "DungeonClaim80Button",
    "DungeonSettlement",
    "DungeonChallengeAgain",
    "DungeonRepeatBalance",
)
# 运行路线节点 → 唯一允许的自定义动作。
ROUTE_BINDINGS = {
    "DungeonFarmTeleport": "map_teleport_to_point",
    "DungeonFarmWalkToEntrance": "dungeon_walk_until",
    "DungeonFarmAdvance": "dungeon_walk_until",
}
BINDINGS = frozenset(RECO_BINDINGS) | frozenset(ROUTE_BINDINGS)
TEMPLATE_PREFIX = "DungeonProfile"
# 身份识别节点 → 必须能识别出的 profile 文本字段，且不得同时识别同目录的其他同级名称。
IDENTITY_FIELDS = {
    "DungeonStageEntry": "name",
    "DungeonStageDetail": "name",
    "DungeonEntrancePrompt": "entrance_name",
    "DungeonStageListTitle": "type_name",
}
_NODE_PREFIX = re.compile(r"^(\[[A-Za-z]+\])+")
_WALK_REQUIRED = ("key", "until", "guard", "timeout")
_WALK_REFS = ("until", "guard")


@dataclass(frozen=True)
class DungeonConfig:
    runs: int  # 成功领奖轮数上限，0 = 不限（仍受预算 / 保留线约束）
    reserve: int  # 至少保留的本性像素；plan=exhaust 时为 0
    reward_mode: str  # "40" / "80" / "auto"
    team_mode: str  # 目前只有 "current"
    profile: dict  # 选中 profile 的副本（含 bindings）
    overrides: dict  # {运行节点: 完整模板节点}，供 context.override_pipeline


def _node(context, name: str, *, optional: bool = False) -> dict | None:
    try:
        value = context.get_node_data(name)
    except Exception as exc:  # 资源接口异常同样是配置无法确认
        raise ValueError("cannot read node %s" % name) from exc
    if value is None and optional:
        return None
    if not isinstance(value, dict):
        raise ValueError("missing or invalid node %s" % name)
    return deepcopy(value)


def _option(context, name: str) -> dict:
    defaults = OPTION_DEFAULTS[name]
    node = _node(context, name, optional=True)
    if node is None:  # 旧资源没有该载体：迁移为旧行为
        return dict(defaults)
    attach = node.get("attach")
    if not isinstance(attach, dict) or not set(defaults) <= set(attach):
        raise ValueError("invalid option carrier %s" % name)
    return attach


def _integer(value, name: str, maximum: int) -> int:
    """与界面 verify 一致：0 或无前导零的十进制；也接受 pipeline_type=int 下发的整数。"""
    if isinstance(value, bool):
        raise ValueError("invalid %s" % name)
    if isinstance(value, str):
        if len(value) > len(str(maximum)) or not re.fullmatch(r"0|[1-9][0-9]*", value):
            raise ValueError("invalid %s" % name)
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError("invalid %s" % name)
    return value


def _choice(value, choices, name: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError("invalid %s" % name)
    return value


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing profile %s" % name)
    return value


def _recognition(node: dict) -> dict:
    value = node.get("recognition")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):  # v1 写法：参数平铺在节点上
        return {"type": value, "param": node}
    if value is None:
        return {"type": "DirectHit", "param": {}}
    raise ValueError("invalid recognition")


def _action(node: dict) -> tuple[str, str | None, dict]:
    """返回 (动作类型, custom_action, custom_action_param)。"""
    action = node.get("action")
    if isinstance(action, dict):
        params = action.get("param") or {}
        kind = action.get("type", "DoNothing")
    elif isinstance(action, str):
        params, kind = node, action
    elif action is None:
        params, kind = {}, "DoNothing"
    else:
        raise ValueError("invalid action")
    custom = params.get("custom_action_param") or {}
    if isinstance(custom, str):
        custom = json.loads(custom) if custom.strip() else {}
    if not isinstance(custom, dict):
        raise ValueError("invalid custom_action_param")
    return kind, params.get("custom_action"), custom


def _ref_name(item) -> str:
    """节点列表项 → 节点名：兼容资源中的 "[JumpBack]Name" 与 get_node_data 导出的 {"name": ...}。"""
    if isinstance(item, dict):
        if item.get("anchor"):
            raise ValueError("anchor references are not allowed in profile templates")
        item = item.get("name")
    if not isinstance(item, str) or not item:
        raise ValueError("invalid node reference")
    return _NODE_PREFIX.sub("", item)


def references(node: dict) -> list[str]:
    """节点直接引用的其他节点：next / on_error / And-Or 子识别 / 本任务动作参数中的节点名。"""
    refs = [_ref_name(i) for key in ("next", "on_error") for i in (node.get(key) or [])]
    param = _recognition(node).get("param") or {}
    for key in ("all_of", "any_of"):
        for item in param.get(key) or []:
            if not isinstance(item, str):  # 模板只允许引用命名节点，不允许内联识别
                raise ValueError("inline sub-recognition is not allowed in profile templates")
            refs.append(_ref_name(item))
    _, _, custom = _action(node)
    for key in ("until", "guard", "balance_node", "popup_node", "options_node", "button_node"):
        if custom.get(key):
            refs.append(_ref_name(custom[key]))
    for key in ("abort_nodes", "continue", "finish"):
        refs += [_ref_name(i) for i in custom.get(key) or []]
    buttons = custom.get("buttons")
    if isinstance(buttons, dict):
        refs += [_ref_name(i) for i in buttons.values()]
    return refs


def _teleport_points() -> dict[str, dict]:
    """与 MapTeleport 相同的开发 / 发行包查找顺序；只读 JSON，不导入图像与 Maa 依赖。"""
    for parent in Path(__file__).resolve().parents:
        for base in (parent / "assets" / "resource" / "base", parent / "resource" / "base"):
            if not base.exists():
                continue
            path = base / "map_teleport" / "teleport_points.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                return {p["id"]: p for p in data["teleport_points"]}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError("invalid teleport resource") from exc
    raise ValueError("teleport resource not found")


def _expected(reco: dict) -> list[str]:
    expected = (reco.get("param") or {}).get("expected")
    if isinstance(expected, str):
        expected = [expected]
    if not isinstance(expected, list) or not expected or not all(isinstance(p, str) and p for p in expected):
        raise ValueError("missing identity recognition text")
    return expected


def _identifies(expected: list[str], text: str) -> bool:
    # OCR expected 是正则搜索语义；这里用同样语义核对模板确实能认出该文本。
    return any(re.search(pattern, text) for pattern in expected)


def _check_identity(target: str, reco: dict, profile: dict, catalog: list[dict]) -> None:
    field = IDENTITY_FIELDS[target]
    expected = _expected(reco)
    if not _identifies(expected, profile[field]):
        raise ValueError("%s does not recognize profile %s" % (target, field))
    others = {p.get(field) for p in catalog if isinstance(p.get(field), str)} - {profile[field]}
    for other in others:
        if _identifies(expected, other):
            raise ValueError("%s also recognizes sibling %s" % (target, other))


def _check_profile_entry(profile: dict) -> None:
    """目录中每一项（包括不可执行项）的基本结构。"""
    if not isinstance(profile, dict):
        raise ValueError("invalid profile catalog entry")
    pid = _text(profile.get("id"), "id")
    if profile.get("content_id") != pid:
        raise ValueError("profile content_id must equal id")
    for key in ("name", "entrance_id", "entrance_name"):
        _text(profile.get(key), key)
    for key in ("type_id", "type_name", "teleport_point_id"):
        if profile.get(key) is not None:
            _text(profile.get(key), key)
    for key in ("executable", "calibrated"):
        if type(profile.get(key)) is not bool:
            raise ValueError("profile %s must be boolean" % key)
    costs = profile.get("supported_costs")
    if (
        not isinstance(costs, list)
        or not costs
        or any(type(c) is not int or c not in REWARD_COSTS for c in costs)
        or len(set(costs)) != len(costs)
    ):
        raise ValueError("invalid supported costs")


def _bindings(context, profile: dict, catalog: list[dict]) -> dict:
    bindings = profile.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != BINDINGS:
        raise ValueError("incomplete profile bindings")
    sources = list(bindings.values())
    if len(set(sources)) != len(sources):
        raise ValueError("profile bindings must use distinct templates")
    points = _teleport_points()
    overrides = {}
    for target in sorted(bindings):
        source = bindings[target]
        if not isinstance(source, str) or not source.startswith(TEMPLATE_PREFIX) or source in BINDINGS:
            raise ValueError("profile must reference independent %s* templates" % TEMPLATE_PREFIX)
        _node(context, target)  # 运行节点必须已存在：拼写错误不能凭空造出新节点
        node = _node(context, source)
        if node.get("enabled") is not False:
            raise ValueError("profile template %s must stay disabled" % source)
        if node.get("inverse"):
            raise ValueError("inverse profile template %s" % source)
        reco = _recognition(node)
        kind, custom_action, custom = _action(node)
        if target in ROUTE_BINDINGS:
            if kind != "Custom" or custom_action != ROUTE_BINDINGS[target]:
                raise ValueError("invalid route action %s" % target)
            if target == "DungeonFarmTeleport":
                point_id = custom.get("teleport_point_id")
                if point_id != profile["teleport_point_id"]:
                    raise ValueError("teleport binding does not match profile")
                point = points[point_id]
                if profile.get("type_name") is not None and point.get("selectionName") != profile["type_name"]:
                    raise ValueError("teleport point type does not match profile")
            else:
                if not all(custom.get(k) for k in _WALK_REQUIRED) or not custom.get("abort_nodes"):
                    raise ValueError("incomplete bounded route %s" % target)
                for key in _WALK_REFS:  # 路线停止条件 / 守护场景只能用识别节点
                    if _NODE_PREFIX.sub("", str(custom[key])).startswith(TEMPLATE_PREFIX):
                        raise ValueError("route must reference runtime nodes, not templates")
        else:
            if reco.get("type") in (None, "DirectHit") or kind != "DoNothing":
                raise ValueError("recognition template %s must recognize and must not act" % source)
            if node.get("next") or node.get("on_error"):
                raise ValueError("recognition template %s must not route" % source)
            if target in IDENTITY_FIELDS:
                _check_identity(target, reco, profile, catalog)
        for ref in references(node):
            if ref.startswith("__ScenePrivate"):
                raise ValueError("private scene reference is forbidden")
            if ref.startswith(TEMPLATE_PREFIX):
                raise ValueError("templates must not reference other templates")
            _node(context, ref)
        # 完整替换：资源导出的节点已含全部字段，只把禁用模板转为启用的运行节点。
        node["enabled"] = True
        node.setdefault("next", [])
        node.setdefault("on_error", [])
        overrides[target] = node
    return overrides


def _load_config(context) -> DungeonConfig:
    options = {name: _option(context, name) for name in OPTION_NODES}
    runs = _integer(options[RUNS_NODE]["runs"], "runs", MAX_RUNS)
    stage = options[STAGE_NODE]
    if "cost" in stage and (type(stage["cost"]) is not int or stage["cost"] != 40):
        raise ValueError("legacy stage cost must be 40")  # 旧字段只是普通领取兼容值
    type_id = _text(options[TYPE_NODE]["type_id"], "type option")  # 与目录核对见下方
    entrance_id = _text(options[ENTRANCE_NODE]["entrance_id"], "entrance option")
    mode = _choice(options[PLAN_NODE]["mode"], PLAN_MODES, "plan")
    reserve = _integer(options[RESERVE_NODE]["reserve"], "reserve", MAX_RESERVE)
    reward = _choice(options[REWARD_NODE]["reward_mode"], REWARD_MODES, "reward mode")
    team = _choice(options[TEAM_NODE]["team_mode"], TEAM_MODES, "team mode")

    catalog = (_node(context, PROFILES_NODE).get("attach") or {}).get("profiles")
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("invalid profile catalog")
    for entry in catalog:
        _check_profile_entry(entry)
    ids = [p["id"] for p in catalog]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate profile id")
    selected = _choice(stage["name"], ids, "stage")
    profile = deepcopy(next(p for p in catalog if p["id"] == selected))
    if profile["type_id"] != type_id or profile["entrance_id"] != entrance_id:
        raise ValueError("type / entrance / content mismatch")
    if profile["executable"] is not True:
        raise ValueError("profile %s is catalog data only, not executable" % selected)
    for key in ("type_id", "type_name", "teleport_point_id"):
        _text(profile.get(key), key)
    if profile.get("difficulty_mode") != "current":
        raise ValueError("unsupported difficulty selection")
    if profile.get("team_modes") != list(TEAM_MODES) or team not in profile["team_modes"]:
        raise ValueError("unsupported team selection")
    if reward != "auto" and int(reward) not in profile["supported_costs"]:
        raise ValueError("reward mode is not supported by profile")
    if profile["teleport_point_id"] not in _teleport_points():
        raise ValueError("unknown teleport point")
    overrides = _bindings(context, profile, catalog)
    return DungeonConfig(runs, reserve if mode == "reserve" else 0, reward, team, profile, overrides)


def load_config(context) -> DungeonConfig:
    """解析本次任务的配置快照。结构、取值或资源引用错误一律抛 ValueError。"""
    try:
        return _load_config(context)
    except ValueError:
        raise
    except (TypeError, KeyError, AttributeError, OverflowError, re.error) as exc:
        raise ValueError("invalid dungeon configuration structure: %s" % exc) from exc
