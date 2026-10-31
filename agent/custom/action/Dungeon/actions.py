"""刷本任务的 MaaFramework 注册入口（依赖 maa，其余模块不依赖）。

custom_action 与 flow.py 中的同名函数一一对应，参数见各类的 docstring。
"""

from __future__ import annotations

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils.logger import logger

from . import flow

__all__ = [
    "DungeonResetAction",
    "DungeonGateAction",
    "DungeonEnterAction",
    "DungeonBeginRoundAction",
    "DungeonClaimAction",
    "DungeonConfirmAction",
    "DungeonWalkUntilAction",
    "DungeonFightAction",
    "DungeonFinishAction",
    "DungeonFailAction",
]


def _result(name: str, run) -> CustomAction.RunResult:
    try:
        ok = bool(run())
    except Exception as exc:  # 任何异常都按失败处理，不让 Pipeline 继续推进
        logger.exception("dungeon %s raised: %s", name, exc)
        ok = False
    return CustomAction.RunResult(success=ok)


def _box(argv):
    return list(argv.box) if getattr(argv, "box", None) is not None else None


@AgentServer.custom_action("dungeon_reset")
class DungeonResetAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        return _result("reset", lambda: flow.reset(context))


@AgentServer.custom_action("dungeon_gate")
class DungeonGateAction(CustomAction):
    """custom_action_param: {"phase": "entry|repeat|claim", "balance_node": "...",
    "continue": [...], "finish": [...]}；phase=claim 另需 popup_node / options_node / buttons。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = flow.parse_param(argv.custom_action_param)
        return _result("gate", lambda: flow.gate(context, argv.node_name, params))


@AgentServer.custom_action("dungeon_enter")
class DungeonEnterAction(CustomAction):
    """custom_action_param: {"phase": "entry|repeat", "button_node": "..."}"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = flow.parse_param(argv.custom_action_param)
        box = _box(argv)
        return _result("enter", lambda: flow.enter(context, params, box))


@AgentServer.custom_action("dungeon_begin_round")
class DungeonBeginRoundAction(CustomAction):
    """custom_action_param: {"round_timeout": 600}"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = flow.parse_param(argv.custom_action_param)
        return _result("begin_round", lambda: flow.begin_round(context, params))


@AgentServer.custom_action("dungeon_claim")
class DungeonClaimAction(CustomAction):
    """custom_action_param 与 phase=claim 的闸门相同；按钮在新截图中重新定位，不使用 argv.box。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = flow.parse_param(argv.custom_action_param)
        return _result("claim", lambda: flow.claim(context, params))


@AgentServer.custom_action("dungeon_confirm")
class DungeonConfirmAction(CustomAction):
    """custom_action_param: {"balance_node": "", "continue": [...], "finish": [...]}"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = flow.parse_param(argv.custom_action_param)
        return _result("confirm", lambda: flow.confirm(context, argv.node_name, params))


@AgentServer.custom_action("dungeon_walk_until")
class DungeonWalkUntilAction(CustomAction):
    """custom_action_param: {"key": "W", "until": "...", "guard": "...", "abort_nodes": [...], "timeout": 10}"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = flow.parse_param(argv.custom_action_param)
        return _result("walk_until", lambda: flow.walk_until(context, params))


@AgentServer.custom_action("dungeon_fight")
class DungeonFightAction(CustomAction):
    """custom_action_param: {"wait_timeout": 5}；本轮时限由 dungeon_begin_round 设定。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = flow.parse_param(argv.custom_action_param)
        return _result("fight", lambda: flow.fight(context, params))


@AgentServer.custom_action("dungeon_finish")
class DungeonFinishAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        return _result("finish", lambda: flow.finish(context))


@AgentServer.custom_action("dungeon_fail")
class DungeonFailAction(CustomAction):
    """custom_action_param: {"reason": "unsafe_start"}（可选）"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = flow.parse_param(argv.custom_action_param)
        return _result("fail", lambda: flow.fail(context, params))
