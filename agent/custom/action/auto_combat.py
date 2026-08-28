"""自动战斗的 MAA 自定义动作入口。

只做参数解析、脚本加载、依赖装配与结果返回；决策逻辑在 ``Combat.script``，
底层机制在 ``Combat.kernel``，会话驱动在 ``Combat.runtime``。

脚本来源（按优先级）：

1. ``custom_action_param`` 里的 ``script``（内联对象，便于 Pipeline 直接写）
2. ``preset``：内置预设名，读 ``assets/resource/base/combat/<name>.json``
3. ``script_path``：用户自定义脚本的路径

三者都没给时，报错并返回失败——**不提供隐式默认脚本**。让用户明确
知道自己在跑什么，而不是被一个来源不明的动作序列驱动。
"""

from __future__ import annotations

import json
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from agent.custom.action.Combat.kernel import CombatKernel
    from agent.custom.action.Combat.runtime import CombatSession, SessionConfig
    from agent.custom.action.Combat.script import parse_script
except ImportError:
    from .Combat.kernel import CombatKernel
    from .Combat.runtime import CombatSession, SessionConfig
    from .Combat.script import parse_script

try:
    from agent.custom.action.pinkpaw.pinkpaw_common import (
        _parse_bool,
        _parse_custom_action_param,
    )
except ImportError:
    from .pinkpaw.pinkpaw_common import _parse_bool, _parse_custom_action_param

LOG_PREFIX = "[Combat]"
NODE_PREFIX = "Combat"

# 界面选项的载体节点。每个选项覆盖各自独立的节点，Python 侧再把它们的
# ``attach`` 合并成参数字典。
#
# 为什么不让选项直接改 ``AutoCombatMain`` 的 ``custom_action_param``：
# GUI 合并多个 option 的 pipeline_override 时，``custom_action_param``
# 是**整体替换**而不是逐键合并。五个选项都改这一个字段时只有最后一个能活下来，
# 实机表现为战斗预设永远是 ``basic_attack``、``duration`` 回落到代码默认值、
# 两个开关（脱战结束、大世界守卫）完全关不掉。
#
# 改成每个选项写各自的节点就不存在互相覆盖——字段路径本来就不同。
# 这套机制与 ``DungeonFarm_*`` / ``PinkPawHeist_AutoResizeGameWindowConfig``
# 相同，已经在刷本任务上验证过。
OPTION_NODES = (
    "AutoCombat_PresetOption",
    "AutoCombat_DurationOption",
    "AutoCombat_StopWhenNotInTeamOption",
    "AutoCombat_GuardOpenWorldOption",
    "AutoCombat_LogDecisionsOption",
)

# 内置预设目录。两种布局都要支持：
# - 开发仓库：``<root>/assets/resource/base/combat``
# - 发布包：  ``<root>/resource/base/combat``（打包时 assets/ 这一层被剥掉）
# 只认第一种会让所有内置预设在发布包里加载失败，而这恰好是测试覆盖不到的
# 布局——因此这里必须显式枚举，不能依赖单一相对路径。
PRESET_SUBDIRS = (
    ("assets", "resource", "base", "combat"),
    ("resource", "base", "combat"),
)


def _project_root() -> Path:
    """定位项目根，用于查找预设脚本。"""
    return Path(__file__).resolve().parents[3]


def _preset_search_bases():
    """返回候选根目录，覆盖开发/发布/dev 模式三种 cwd 情况。

    dev 模式下 ``agent/main.py`` 会把 cwd 切到 ``<root>/assets``，
    因此 ``cwd.parent`` 也要作为候选，否则只能靠 ``_project_root()`` 兜。
    """
    cwd = Path.cwd()
    bases = [cwd, cwd.parent, _project_root()]
    seen = set()
    result = []
    for base in bases:
        key = str(base)
        if key not in seen:
            seen.add(key)
            result.append(base)
    return result


def _preset_dir() -> Path:
    """返回内置预设目录；找不到时返回开发布局路径以便报错信息可读。"""
    for base in _preset_search_bases():
        for subdir in PRESET_SUBDIRS:
            candidate = base.joinpath(*subdir)
            if candidate.is_dir():
                return candidate
    return _project_root().joinpath(*PRESET_SUBDIRS[0])


def _load_script_source(params: dict):
    """按优先级取脚本原始数据，返回 ``(raw, 来源描述)``。"""
    inline = params.get("script")
    if inline:
        return inline, "custom_action_param.script"

    preset = params.get("preset")
    if preset:
        name = str(preset).strip()
        # 防目录穿越：预设名只允许简单文件名
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None, f"预设名非法: {preset!r}"
        path = _preset_dir() / f"{name}.json"
        if not path.exists():
            available = sorted(p.stem for p in _preset_dir().glob("*.json"))
            return None, (
                f"预设不存在: {name}（可用预设: {available or '无'}）"
            )
        try:
            return json.loads(path.read_text(encoding="utf-8")), f"preset:{name}"
        except (OSError, ValueError) as exc:
            return None, f"预设读取失败 {path}: {exc}"

    script_path = params.get("script_path")
    if script_path:
        path = Path(str(script_path))
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return None, f"脚本文件不存在: {path}"
        try:
            return json.loads(path.read_text(encoding="utf-8")), f"file:{path}"
        except (OSError, ValueError) as exc:
            return None, f"脚本读取失败 {path}: {exc}"

    return None, (
        "未指定脚本。请提供 script（内联）、preset（内置预设）"
        "或 script_path（自定义文件）之一"
    )


def _describe_params(params: dict) -> str:
    """把收到的参数摘要成一行，专门用于诊断「参数没传下来」。

    只打键和值的类型/是否为空，不打全文：内联 script 可能很长，
    刷到日志里会把真正有用的信息顶掉。
    """
    if not params:
        return "（空字典）"
    parts = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            parts.append(f"{key}=null")
        elif isinstance(value, str):
            parts.append(f"{key}={'（空字符串）' if not value else value!r}")
        elif isinstance(value, dict):
            parts.append(f"{key}=<对象,{len(value)}键>")
        else:
            parts.append(f"{key}={value!r}")
    return "{" + ", ".join(parts) + "}"


def _collect_option_params(context) -> dict:
    """把界面选项载体节点的 ``attach`` 合并成参数字典。

    空值（``None`` / 空字符串）一律跳过：它们表示「这个选项没有给出有效值」。
    输入框类选项的占位符替换失败时下发的正是 ``null``，跳过它才能回落到
    ``custom_action_param`` 里的默认值，而不是拿着 ``None`` 去当预设名。

    读不到节点不算错误：脚本直接调用、或某个 Client 不下发选项时都会这样，
    此时应当由 ``custom_action_param`` 提供默认值。
    """
    merged: dict = {}
    failed: list[str] = []
    for node in OPTION_NODES:
        try:
            data = context.get_node_data(node)
        except Exception:
            # 节点读不到是正常情况（脚本直接调用时资源里没加载这些载体节点），
            # 汇总成一条日志而不是每个节点刷一行。
            failed.append(node)
            continue
        attach = data.get("attach") if isinstance(data, dict) else None
        if not isinstance(attach, dict):
            continue
        for key, value in attach.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            merged[key] = value
    if failed and len(failed) < len(OPTION_NODES):
        # 只有部分读不到才值得提醒：全都读不到通常是「没经过界面」，
        # 而只缺几个说明 pipeline 里的载体节点定义不全。
        print(f"{LOG_PREFIX}[WARN] 这些选项载体节点读不到: {failed}")
    return merged


@AgentServer.custom_action("AutoCombat")
class AutoCombat(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        # 先把原始参数原样打出来。一旦界面选项没把值下发（GUI 对 option 的
        # 合并方式各家不同），后面每一条异常行为都只是下游表现，看不出起因。
        # 这一行日志是排查那类问题唯一可靠的证据。
        raw_param = getattr(argv, "custom_action_param", None)
        print(f"{LOG_PREFIX} 收到参数: {raw_param!r}")

        # 界面选项走载体节点，pipeline / 内联调用走 custom_action_param。
        # 选项优先：它代表用户在界面上的显式选择，而 custom_action_param
        # 里的是资源自带的默认值。
        inline = _parse_custom_action_param(argv, log_prefix=LOG_PREFIX)
        from_options = _collect_option_params(context)
        if from_options:
            print(f"{LOG_PREFIX} 界面选项: {_describe_params(from_options)}")
        params = {**inline, **from_options}

        raw, source = _load_script_source(params)
        if raw is None:
            print(f"{LOG_PREFIX}[ERROR] {source}")
            return CustomAction.RunResult(success=False)

        script = parse_script(raw)
        # issue 一律打出来：脚本不生效是最常见的用户困惑，
        # 与其让用户猜，不如把解析期发现的问题全部告知。
        for issue in script.issues:
            print(f"{LOG_PREFIX}[WARN] 脚本问题: {issue}")

        if not script.rules and not script.fallback:
            print(
                f"{LOG_PREFIX}[ERROR] 脚本 {source} 没有可执行内容，放弃运行"
            )
            return CustomAction.RunResult(success=False)

        config = SessionConfig(
            duration=float(params.get("duration", 60.0) or 60.0),
            tick_interval=float(params.get("tick_interval", 0.05) or 0.05),
            stop_when_not_in_team=_parse_bool(
                params.get("stop_when_not_in_team"), True
            ),
            stop_when_menu_open=_parse_bool(
                params.get("stop_when_menu_open"), True
            ),
            guard_open_world=_parse_bool(params.get("guard_open_world"), True),
            max_ticks=int(params.get("max_ticks", 0) or 0),
            log_decisions=_parse_bool(params.get("log_decisions"), False),
        )

        kernel = CombatKernel(
            context,
            timing_scale=float(params.get("timing_scale", 1.0) or 1.0),
            direct_input=_parse_bool(params.get("direct_input"), True),
            node_prefix=NODE_PREFIX,
            log_prefix=LOG_PREFIX,
            stop_message="AutoCombat stopped by Maa tasker.",
        )

        roster = getattr(script, "roster", None)
        kernel.log_info(
            f"开始自动战斗: {script.name}（来源 {source}）"
            f" 规则 {len(script.rules)} 条"
            + (f" 编成 [{roster.describe()}]" if roster and roster.configured else "")
        )

        session = CombatSession(kernel, script, config=config)
        result = session.run()
        kernel.log_info(f"战斗结束: {result.describe()}")
        return CustomAction.RunResult(success=result.success)
