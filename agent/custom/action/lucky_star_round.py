import json
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils.logger import logger
from utils.task_control import action_succeeded

_round_count = 0
_total_rounds = 5


def _load_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        nested_value = value.get("custom_action_param")
        if nested_value is not None:
            return _load_params(nested_value)
        nested_action = value.get("action")
        if isinstance(nested_action, dict):
            nested_param = nested_action.get("param")
            if isinstance(nested_param, dict):
                nested_value = nested_param.get("custom_action_param")
                if nested_value is not None:
                    return _load_params(nested_value)
        return value
    if isinstance(value, list):
        for item in reversed(value):
            params = _load_params(item)
            if params:
                return params
        return {}
    if isinstance(value, (int, float)):
        return {"repeat_count": value}
    if isinstance(value, str) and value:
        try:
            params = json.loads(value)
        except json.JSONDecodeError:
            return {"repeat_count": value}
        if isinstance(params, (int, float)):
            return {"repeat_count": params}
        return params if isinstance(params, dict) else {}
    return {}


def _set_next(context: Context, node_name: str, next_nodes: list[str]) -> bool:
    if context.tasker.stopping:
        return False
    if not context.override_next(node_name, next_nodes):
        logger.error(
            "Failed to override LuckyStar next: node=%s next=%s", node_name, next_nodes
        )
        return False
    return not context.tasker.stopping


@AgentServer.custom_action("lucky_star_reset")
class LuckyStarReset(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        global _round_count, _total_rounds
        if context.tasker.stopping:
            return CustomAction.RunResult(success=False)
        _round_count = 0
        _total_rounds = 5
        # Overrides belong to the current Context; also support re-entering it.
        restored = context.override_pipeline(
            {
                "PukaLandLuckyStarRound": {
                    "next": ["PukaLandLuckyStarStart", "PukaLandLuckyStarCountdown"]
                },
                "PukaLandLuckyStarResultGate": {"next": ["PukaLandLuckyStarRestarted"]},
            }
        )
        if not restored:
            logger.error("Failed to restore LuckyStar routing during reset")
        return CustomAction.RunResult(success=restored and not context.tasker.stopping)


@AgentServer.custom_action("lucky_star_round_gate")
class LuckyStarRoundGate(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        global _round_count, _total_rounds
        if context.tasker.stopping:
            return CustomAction.RunResult(success=False)

        params = _load_params(argv.custom_action_param)
        try:
            total_rounds = max(0, int(params.get("repeat_count", 5)))
        except (TypeError, ValueError, OverflowError):
            total_rounds = 5
        _total_rounds = total_rounds

        if _round_count >= total_rounds:
            next_nodes = ["PukaLandLuckyStarExit"]
        elif _round_count == 0:
            next_nodes = ["PukaLandLuckyStarStart"]
        else:
            next_nodes = ["PukaLandLuckyStarCountdown"]

        if not _set_next(context, argv.node_name, next_nodes):
            return CustomAction.RunResult(success=False)
        if _round_count < total_rounds:
            _round_count += 1
        logger.debug(
            "LuckyStar round gate: round=%d total=%d next=%s",
            _round_count,
            total_rounds,
            next_nodes,
        )
        return CustomAction.RunResult(success=not context.tasker.stopping)


@AgentServer.custom_action("lucky_star_retry_or_finish")
class LuckyStarRetryOrFinish(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        if context.tasker.stopping:
            return CustomAction.RunResult(success=False)
        if _round_count >= _total_rounds:
            logger.debug(
                "LuckyStar finished: round=%d total=%d; stopping before retry",
                _round_count,
                _total_rounds,
            )
            return CustomAction.RunResult(
                success=_set_next(context, argv.node_name, ["PukaLandLuckyStarExit"])
            )

        if argv.reco_detail is None or argv.box is None:
            logger.warning("LuckyStar retry button recognition has no result box")
            return CustomAction.RunResult(success=False)
        if not _set_next(context, argv.node_name, ["PukaLandLuckyStarRestarted"]):
            return CustomAction.RunResult(success=False)

        # Run only a Click action in a cloned context, preserving the calling
        # node's Custom action and using its current recognition box.
        result = context.run_action(
            argv.node_name,
            box=argv.box,
            pipeline_override={
                argv.node_name: {"action": {"type": "Click", "param": {"target": True}}}
            },
        )
        return CustomAction.RunResult(
            success=action_succeeded(result) and not context.tasker.stopping
        )
