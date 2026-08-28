"""刷本任务的 MAA 自定义动作入口。

只做参数解析、配置加载、依赖装配与结果返回；循环逻辑在 ``runner``，
步骤执行在 ``steps``，配置校验在 ``config``。

配置来源（按优先级）：

1. ``custom_action_param`` 里的 ``config``（内联对象）
2. ``dungeon``：内置配置名，读 ``resource/base/dungeon/<name>.json``
3. ``config_path``：用户自定义配置文件路径

三者都没给时报错并返回失败——与 ``AutoCombat`` 一致，**不提供隐式默认
配置**。刷本会在游戏里点击和消耗体力，绝不能靠一个来源不明的配置驱动。
"""

from __future__ import annotations

import json
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from agent.custom.action.Combat.kernel import CombatKernel
    from agent.custom.action.Combat.runtime import (
        DEFAULT_NO_ENEMY_GRACE,
        CombatSession,
        SessionConfig,
    )
    from agent.custom.action.Combat.script import parse_script
    from agent.custom.action.Dungeon.config import apply_overrides, parse_config
    from agent.custom.action.Dungeon.runner import DungeonFarmRunner
    from agent.custom.action.Dungeon.steps import StepExecutor
    from agent.custom.action.auto_combat import _load_script_source
    from agent.custom.action.pinkpaw.pinkpaw_common import (
        _parse_bool,
        _parse_custom_action_param,
    )
except ImportError:  # pragma: no cover
    from ..Combat.kernel import CombatKernel
    from ..Combat.runtime import (
        DEFAULT_NO_ENEMY_GRACE,
        CombatSession,
        SessionConfig,
    )
    from ..Combat.script import parse_script
    from ..auto_combat import _load_script_source
    from ..pinkpaw.pinkpaw_common import _parse_bool, _parse_custom_action_param
    from .config import apply_overrides, parse_config
    from .runner import DungeonFarmRunner
    from .steps import StepExecutor

LOG_PREFIX = "[DungeonFarm]"
NODE_PREFIX = "DungeonFarm"

# 界面选项的载体节点。每个选项覆盖各自独立的节点，Python 侧再把它们的
# ``attach`` 合并成参数字典。
#
# 为什么不让选项直接改 ``DungeonFarmMain`` 的 ``custom_action_param``：
# GUI 合并多个 option 的 pipeline_override 时，``custom_action_param``
# 是**整体替换**而不是逐键合并。多个选项都改这一个字段时只有最后一个能活下来，
# 实机日志里表现为 Python 只收到 ``{"log_decisions":true}``，连 pipeline 里
# 写死的 ``dungeon`` 默认值都被冲掉，任务直接以「未指定副本配置」退出。
#
# 改成每个选项写各自的节点就不存在互相覆盖——字段路径本来就不同。
# 这套机制与 ``PinkPawHeist_AutoResizeGameWindowConfig`` 相同，已经上线验证过。
OPTION_NODES = (
    "DungeonFarm_ConfigOption",
    "DungeonFarm_StageOption",
    "DungeonFarm_DifficultyOption",
    "DungeonFarm_RoundsOption",
    "DungeonFarm_TeleportOption",
    "DungeonFarm_CombatPresetOption",
    "DungeonFarm_LogDecisionsOption",
)

# 内置配置目录。两种布局都要支持——发布包没有 assets/ 这一层。
# 这个坑在 AutoCombat 上已经踩过一次（发布版预设全部加载失败），不再重复。
CONFIG_SUBDIRS = (
    ("assets", "resource", "base", "dungeon"),
    ("resource", "base", "dungeon"),
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _config_search_bases():
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


def _config_dir() -> Path:
    for base in _config_search_bases():
        for subdir in CONFIG_SUBDIRS:
            candidate = base.joinpath(*subdir)
            if candidate.is_dir():
                return candidate
    return _project_root().joinpath(*CONFIG_SUBDIRS[0])


def _load_config_source(params: dict):
    """按优先级取配置原始数据，返回 ``(raw, 来源描述)``。"""
    inline = params.get("config")
    if inline:
        return inline, "custom_action_param.config"

    name = params.get("dungeon")
    if name:
        text = str(name).strip()
        # 防目录穿越：配置名只允许简单文件名
        if not text or "/" in text or "\\" in text or text.startswith("."):
            return None, f"配置名非法: {name!r}"
        path = _config_dir() / f"{text}.json"
        if not path.exists():
            available = sorted(
                p.stem for p in _config_dir().glob("*.json")
                if not p.stem.startswith("_")
            )
            return None, (
                f"副本配置不存在: {text}（可用配置: {available or '无'}）。"
                f"配置目录: {_config_dir()}"
            )
        try:
            return json.loads(path.read_text(encoding="utf-8")), f"dungeon:{text}"
        except (OSError, ValueError) as exc:
            return None, f"配置读取失败 {path}: {exc}"

    config_path = params.get("config_path")
    if config_path:
        path = Path(str(config_path))
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return None, f"配置文件不存在: {path}"
        try:
            return json.loads(path.read_text(encoding="utf-8")), f"file:{path}"
        except (OSError, ValueError) as exc:
            return None, f"配置读取失败 {path}: {exc}"

    return None, (
        "未指定副本配置。请提供 config（内联）、dungeon（内置配置名）"
        "或 config_path（自定义文件）之一。"
        f"实际收到的参数: {_describe_params(params)}。"
        "若参数为空、或 dungeon 的值是 null/空字符串，说明界面选项没把值传下来"
        "（GUI 对输入框类选项的占位符替换失败时会这样）——"
        "请把界面上的「副本配置」重新选一次；仍不行请把上一行 "
        "「[DungeonFarm] 收到参数」的日志反馈给开发者"
    )


def _describe_params(params: dict) -> str:
    """把收到的参数摘要成一行，专门用于诊断「参数没传下来」。

    只打键和值的类型/是否为空，不打全文：内联 config 可能很长，
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
    ``custom_action_param`` 里的默认值，而不是拿着 ``None`` 去当配置名。

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
            # 汇总成一条日志而不是每个节点刷一行——七行 WARN 只会盖掉真正的问题。
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


def _make_teleport(context, point_id: str, kernel):
    """返回执行传送的可调用对象；未配置传送点时返回 None。"""
    if not point_id:
        return None

    def do_teleport() -> bool:
        try:
            from agent.custom.action.MapTeleport.teleport_to_point import (
                run_map_teleport_flow,
            )
        except ImportError:
            from ..MapTeleport.teleport_to_point import run_map_teleport_flow

        try:
            return bool(run_map_teleport_flow(context, point_id))
        except Exception as exc:
            kernel.log_error(f"传送失败: {exc}")
            return False

    return do_teleport


def _make_combat(context, combat_params: dict, kernel):
    """返回执行一场战斗的可调用对象。

    直接复用 ``AutoCombat`` 的脚本加载与会话实现，而不是重写一套：
    战斗逻辑只应该有一个来源，否则修了一处漏一处。
    """
    raw, source = _load_script_source(combat_params)
    if raw is None:
        kernel.log_error(f"战斗脚本加载失败: {source}")
        return None, source

    script = parse_script(raw)
    for issue in script.issues:
        kernel.log_warning(f"战斗脚本问题: {issue}")
    if not script.rules and not script.fallback:
        return None, f"战斗脚本 {source} 没有可执行内容"

    session_config = SessionConfig(
        duration=float(combat_params.get("duration", 180.0) or 180.0),
        tick_interval=float(combat_params.get("tick_interval", 0.05) or 0.05),
        stop_when_not_in_team=_parse_bool(
            combat_params.get("stop_when_not_in_team"), True
        ),
        stop_when_menu_open=_parse_bool(
            combat_params.get("stop_when_menu_open"), True
        ),
        # 清怪即收兵。刷本默认开启：副本里打完怪队伍 UI 还在，脱战判据
        # 永远不成立，不看血条就只能每轮干等满 duration。
        stop_when_no_enemy=_parse_bool(
            combat_params.get("stop_when_no_enemy"), True
        ),
        no_enemy_grace=float(
            combat_params.get("no_enemy_grace", DEFAULT_NO_ENEMY_GRACE)
            or DEFAULT_NO_ENEMY_GRACE
        ),
        # 刷本时是脚本自己把角色送进副本的，不需要再拦一道；
        # 但保留开关，默认关闭以免副本内 UI 恰好命中大世界判据。
        guard_open_world=_parse_bool(
            combat_params.get("guard_open_world"), False
        ),
        max_ticks=int(combat_params.get("max_ticks", 0) or 0),
        log_decisions=_parse_bool(combat_params.get("log_decisions"), False),
    )

    def do_combat() -> bool:
        session = CombatSession(kernel, script, config=session_config)
        result = session.run()
        kernel.log_info(f"战斗结束: {result.describe()}")
        return bool(result.success)

    return do_combat, source


@AgentServer.custom_action("DungeonFarm")
class DungeonFarm(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        # 先把原始参数原样打出来。刷本的所有配置都靠这一个入口传进来，
        # 一旦界面选项没把值下发（GUI 对 option 的合并方式各家不同），
        # 后面每一条报错都只是「没配置」的下游表现，看不出真正的起因。
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

        raw, source = _load_config_source(params)
        if raw is None:
            print(f"{LOG_PREFIX}[ERROR] {source}")
            return CustomAction.RunResult(success=False)

        config = parse_config(raw)

        # 任务选项覆盖（轮次、传送点、战斗预设等）。逻辑放在 config 层，
        # 以便离线验证界面开关真的生效。
        for note in apply_overrides(config, params):
            print(f"{LOG_PREFIX}[WARN] {note}")

        if not config.ok:
            print(f"{LOG_PREFIX}[ERROR] 配置 {source} 有 {len(config.errors)} 处错误：")
            for item in config.errors:
                print(f"{LOG_PREFIX}[ERROR]   - {item}")
            return CustomAction.RunResult(success=False)

        kernel = CombatKernel(
            context,
            timing_scale=float(params.get("timing_scale", 1.0) or 1.0),
            direct_input=_parse_bool(params.get("direct_input"), True),
            node_prefix=NODE_PREFIX,
            log_prefix=LOG_PREFIX,
            stop_message="DungeonFarm stopped by Maa tasker.",
        )

        combat_params = dict(config.combat)
        combat, combat_source = _make_combat(context, combat_params, kernel)
        if combat is None:
            print(f"{LOG_PREFIX}[ERROR] {combat_source}")
            return CustomAction.RunResult(success=False)

        kernel.log_info(
            f"开始刷本: {config.describe()}（配置来源 {source}，"
            f"战斗脚本 {combat_source}）"
        )

        executor = StepExecutor(kernel, context, log=kernel.log_warning)
        runner = DungeonFarmRunner(
            executor,
            config,
            teleport=_make_teleport(context, config.teleport_point_id, kernel),
            combat=combat,
            log=kernel.log_info,
            sleep=lambda d: kernel.sleep(
                d, allow_slow_poll=False, scaled=False
            ),
            raise_if_stopped=kernel.ah.raise_if_stopped,
        )

        try:
            result = runner.run()
        finally:
            # 刷本中途停止时必须松手，否则角色会一直按着键
            try:
                kernel.release_controls()
            except Exception as exc:
                kernel.log_error(f"释放按键失败: {exc}")

        kernel.log_info(f"刷本结束: {result.describe()}")
        return CustomAction.RunResult(success=result.success)
