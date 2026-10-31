"""战斗系统的 MaaFramework 注册入口（依赖 maa，其余模块不依赖）。"""

from __future__ import annotations

import json

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from utils.logger import logger

from .api import run_combat
from .perception import (
    HUD_ROI,
    as_bgr,
    current_slot,
    find_health_bars,
    in_hud,
    is_black,
    scale_roi,
)
from .settings import parse_accepted_reasons

__all__ = ["CombatRunAction", "CombatInHudRecognition", "CombatEnemyBarRecognition"]

_bar_failed = False


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
                          "option_nodes": ["MyTask_CombatOption"],
                          "signal_nodes": {"cleared": ["MyTask_Victory"]},
                          "accepted_reasons": ["cleared", "disengaged"]}

    accepted_reasons 只决定本动作是否成功（走 next 还是 on_error），默认仅 cleared；
    它不改变 CombatResult.success。
    """

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        params = _parse_param(argv.custom_action_param)
        option_nodes = params.get("option_nodes") or []
        if not isinstance(option_nodes, list):
            logger.warning("combat option_nodes must be a list: %r", option_nodes)
            option_nodes = []
        accepted, warnings = parse_accepted_reasons(params.get("accepted_reasons"))
        for warning in warnings:
            logger.warning("combat %s", warning)
        result = run_combat(
            context,
            overrides=params,
            option_nodes=[str(n) for n in option_nodes],
            signal_nodes=params.get("signal_nodes"),
        )
        logger.debug("combat_run finished: %s accepted=%s", result, sorted(accepted))
        return CustomAction.RunResult(success=result.reason in accepted)


@AgentServer.custom_recognition("combat_in_hud")
class CombatInHudRecognition(CustomRecognition):
    """命中 = 处于可操作的战斗/大世界界面（底部血条可见且非黑屏）。detail.slot 为当前槽位（0 未知）。"""

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> CustomRecognition.AnalyzeResult:
        image = as_bgr(argv.image)
        if image is None or is_black(image) or not in_hud(image):
            return CustomRecognition.AnalyzeResult(box=None, detail={})
        box = list(scale_roi(HUD_ROI, image.shape))
        return CustomRecognition.AnalyzeResult(
            box=box, detail={"slot": current_slot(image)}
        )


@AgentServer.custom_recognition("combat_enemy_bar")
class CombatEnemyBarRecognition(CustomRecognition):
    """命中 = 画面中有细长的红色敌人血条（容差颜色 + 形状），补充精确颜色匹配漏掉的变色/短血条。"""

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> CustomRecognition.AnalyzeResult:
        global _bar_failed
        try:
            boxes = find_health_bars(argv.image)
        except Exception as exc:  # 缺少 OpenCV 等：只记一次，之后静默未命中
            if not _bar_failed:
                _bar_failed = True
                logger.warning("combat_enemy_bar unavailable: %s", exc)
            return CustomRecognition.AnalyzeResult(box=None, detail={})
        if not boxes:
            return CustomRecognition.AnalyzeResult(box=None, detail={})
        best = max(boxes, key=lambda b: b[2])
        return CustomRecognition.AnalyzeResult(
            box=list(best), detail={"bars": [list(b) for b in boxes]}
        )
