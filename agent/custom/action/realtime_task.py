import json
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

from utils.logger import logger
from utils.task_control import run_task_checked

HOLDER_NODE_NAME = "__RealTimeTaskAction_Holder"
IDLE_NODE_NAME = "RealTimeSleep"


def _parse_nodes(custom_action_param: Any) -> list[str]:
    params = (
        json.loads(custom_action_param)
        if isinstance(custom_action_param, str)
        else custom_action_param
    )
    if not isinstance(params, dict):
        raise ValueError("RealTimeTaskAction: parameters must be an object")
    nodes = params.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("RealTimeTaskAction: 'nodes' must be a nonempty list")
    if any(not isinstance(node, str) or not node.strip() for node in nodes):
        raise ValueError("RealTimeTaskAction: every node must have a name")
    if any(node != node.strip() or node.startswith("[") for node in nodes):
        raise ValueError("RealTimeTaskAction: nodes must be plain pipeline names")
    if HOLDER_NODE_NAME in nodes or "RealTimeTaskMain" in nodes:
        raise ValueError("RealTimeTaskAction: recursive dispatch is not allowed")
    # The idle branch represents a recognition miss, not a task failure.
    return list(dict.fromkeys(node for node in nodes if node != IDLE_NODE_NAME)) + [
        IDLE_NODE_NAME
    ]


def _build_pipeline_override(nodes: list[str]) -> dict:
    return {HOLDER_NODE_NAME: {"next": nodes}}


@AgentServer.custom_action("RealTimeTaskAction")
class RealTimeTaskAction(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        if context.tasker.stopping:
            return CustomAction.RunResult(success=False)
        try:
            nodes = _parse_nodes(argv.custom_action_param)
            for node in nodes:
                if context.get_node_data(node) is None:
                    raise ValueError("RealTimeTaskAction: unknown node: %s" % node)
            idle = context.get_node_data(IDLE_NODE_NAME)
            if not idle.get("enabled", True):
                raise ValueError("RealTimeTaskAction: idle fallback must be enabled")
        except (TypeError, ValueError) as exc:
            logger.error("RealTimeTaskAction configuration invalid: %s", exc)
            return CustomAction.RunResult(success=False)

        # Run exactly one cycle. The pipeline schedules the next cycle, so that
        # cancellation and action failures stay visible to the framework.
        success = run_task_checked(
            context, HOLDER_NODE_NAME, _build_pipeline_override(nodes)
        )
        return CustomAction.RunResult(success=success)
