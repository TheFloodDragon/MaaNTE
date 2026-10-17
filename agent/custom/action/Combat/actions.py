"""战斗系统的 MaaFramework 注册入口（依赖 maa，其余模块不依赖）。"""

from __future__ import annotations

import json

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from utils.logger import logger

from .api import run_combat
from .perception import as_bgr, in_hud, is_black, current_slot, scale_roi, HUD_ROI

__all__ = ["CombatRunAction", "CombatInHudRecognition"]


def _parse_param(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("combat param is not valid JSON: %r (%s)", raw, exc)
        return {}
    return value if isinstance(value, dict) else {}


@AgentServer.custom_action("combat_run")
class CombatRunAction(CustomAction):
    """执行一场战斗。参数见 docs/zh_cn/develop/combat.md：

    custom_action_param: {"wait_timeout": 30, "max_duration": 300, "scheme": "...",
                          "custom_scheme": "...", "option_nodes": ["MyTask_CombatOption"]}
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = _parse_param(argv.custom_action_param)
        option_nodes = params.get("option_nodes") or []
        if not isinstance(option_nodes, list):
            option_nodes = []
        result = run_combat(context, overrides=params, option_nodes=[str(n) for n in option_nodes])
        logger.debug("combat_run finished: %s", result)
        return CustomAction.RunResult(success=result.success)


@AgentServer.custom_recognition("combat_in_hud")
class CombatInHudRecognition(CustomRecognition):
    """命中 = 处于可操作的战斗/大世界界面（底部血条可见且非黑屏）。detail.slot 为当前槽位（0 未知）。"""

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> CustomRecognition.AnalyzeResult:
        image = as_bgr(argv.image)
        if image is None or is_black(image) or not in_hud(image):
            return CustomRecognition.AnalyzeResult(box=None, detail={})
        box = list(scale_roi(HUD_ROI, image.shape))
        return CustomRecognition.AnalyzeResult(box=box, detail={"slot": current_slot(image)})
