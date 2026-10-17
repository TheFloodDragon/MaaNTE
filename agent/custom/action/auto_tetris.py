import re

import cv2
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context
from maa.pipeline import JOCR, JRecognitionType

from .Common.utils import load_params
from .Tetris.feats.play import TetrisGamePlayer
from utils.logger import logger
from utils.maafocus import PrintT
from utils.task_control import interruptible_wait

_round_count = 0
_target_round = 0
_single_shot_done = False
_allow_speed_drop = False
_task_started = False
_task_config = {}

_VITALITY_NEXT = [
    "[JumpBack]TetrisDismissResult",
    "[JumpBack]TetrisHandleLoading",
    "[JumpBack]TetrisHandleWorldWithPrompt",
    "[JumpBack]TetrisSceneDispatch",
    "TetrisExit",
]


def _game_next(use_all_vitality: bool) -> list[str]:
    if use_all_vitality:
        return ["[JumpBack]TetrisCheckVitality", "TetrisExit"]
    return ["TetrisDismissResult", "TetrisExit"]


def _set_next(context: Context, node_name: str, next_nodes: list[str]) -> bool:
    if context.tasker.stopping:
        return False
    if not context.override_next(node_name, next_nodes):
        logger.error(
            "Failed to override Tetris next: node=%s next=%s", node_name, next_nodes
        )
        return False
    return not context.tasker.stopping


def _press_escape(context: Context) -> bool:
    if context.tasker.stopping:
        return False
    controller = context.tasker.controller
    try:
        try:
            if not controller.post_key_down(27).wait().succeeded:
                return False
            waited = interruptible_wait(context, 0.05)
        finally:
            released = controller.post_key_up(27).wait().succeeded
    except Exception as exc:
        logger.error("Tetris result cleanup failed: %s", exc)
        return False
    return waited and released and not context.tasker.stopping


def _finish(context: Context, node_name: str) -> bool:
    # StopTask must run on this pipeline so pending JumpBack entries are discarded.
    return _press_escape(context) and _set_next(context, node_name, ["TetrisExit"])


@AgentServer.custom_action("tetris_reset_context")
class TetrisResetContext(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        global _round_count, _target_round, _single_shot_done, _allow_speed_drop
        global _task_started, _task_config
        if context.tasker.stopping:
            return CustomAction.RunResult(success=False)

        _round_count = 0
        _target_round = 0
        _single_shot_done = False
        _allow_speed_drop = False
        _task_started = False
        _task_config = {}

        params = load_params(argv.custom_action_param)
        _allow_speed_drop = params.get("allow_speed_drop", False)

        # Context overrides last until this task ends. Restore routing on re-entry,
        # retaining the task option that selects the vitality check after a round.
        game_node = context.get_node_data("TetrisHandleGameActive")
        if game_node is None:
            logger.error("Tetris game node is missing during reset")
            return CustomAction.RunResult(success=False)
        action = game_node.get("action")
        action_params = (
            action.get("param", {}) if isinstance(action, dict) else game_node
        )
        game_params = load_params(action_params.get("custom_action_param"))
        restored = context.override_pipeline(
            {
                "TetrisHandleGameActive": {
                    "next": _game_next(game_params.get("use_all_vitality", False))
                },
                "TetrisCheckVitality": {"next": _VITALITY_NEXT},
            }
        )
        if not restored:
            logger.error("Failed to restore Tetris routing during reset")
        return CustomAction.RunResult(success=restored and not context.tasker.stopping)


@AgentServer.custom_action("auto_tetris")
class AutoTetris(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        global _round_count, _target_round, _single_shot_done
        global _task_started, _task_config

        tasker = context.tasker
        if tasker.stopping:
            return CustomAction.RunResult(success=False)
        controller = tasker.controller
        params = load_params(argv.custom_action_param)

        mode = params.get("mode", "single")
        use_all_vitality = params.get("use_all_vitality", False)
        allow_speed_drop = params.get("allow_speed_drop", _allow_speed_drop)
        rc = params.get("repeat_count", 1)
        try:
            new_target = int(rc) if rc else 0
        except (ValueError, TypeError):
            new_target = 0

        if new_target > 0 and _target_round != new_target:
            _round_count = 0
            _target_round = new_target
            _single_shot_done = False

        if not _task_started:
            _task_started = True
            _task_config = {
                "mode": mode,
                "use_all_vitality": use_all_vitality,
                "allow_speed_drop": allow_speed_drop,
                "target_round": new_target if new_target > 0 else 1,
            }
            mode_label = "单人" if mode == "single" else "多人"
            parts = [f"模式: {mode_label}"]
            if new_target > 1:
                parts.append(f"连打: {new_target}轮")
            if use_all_vitality:
                parts.append("消耗所有活力: 是")
            if allow_speed_drop:
                parts.append("允许速降: 是")
            PrintT(context, "tetris.task_started", " | ".join(parts))

        if not use_all_vitality and _single_shot_done:
            return CustomAction.RunResult(success=_finish(context, argv.node_name))

        if not _set_next(context, argv.node_name, _game_next(use_all_vitality)):
            return CustomAction.RunResult(success=False)

        player = TetrisGamePlayer()
        player.context = context
        player.mode = mode
        player.fast_drop = allow_speed_drop
        player.debug = params.get("debug", False)
        success = player.play_round(controller, tasker)
        if tasker.stopping:
            return CustomAction.RunResult(success=False)
        if not success:
            _round_count = 0
            PrintT(context, "tetris.task_failed")
            return CustomAction.RunResult(success=False)

        if not use_all_vitality:
            _round_count += 1
            PrintT(context, "tetris.progress", _round_count, _target_round)

            if _round_count >= _target_round:
                _single_shot_done = True
                if not _finish(context, argv.node_name):
                    return CustomAction.RunResult(success=False)
                PrintT(context, "tetris.task_done")

        return CustomAction.RunResult(success=not tasker.stopping)


@AgentServer.custom_action("tetris_check_vitality_action")
class TetrisCheckVitalityAction(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        if context.tasker.stopping:
            return CustomAction.RunResult(success=False)
        roi = [451, 290, 371, 20]
        controller = context.tasker.controller
        if not controller.post_screencap().wait().succeeded or context.tasker.stopping:
            return CustomAction.RunResult(success=False)
        frame = controller.cached_image

        vitality = None
        if frame is not None and frame.size > 0:
            if len(frame.shape) == 3 and frame.shape[2] == 4:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

            detail = context.run_recognition_direct(
                JRecognitionType.OCR, JOCR(roi=roi), frame
            )
            if context.tasker.stopping:
                return CustomAction.RunResult(success=False)
            if detail is not None and detail.hit:
                for result in detail.filtered_results:
                    numbers = re.findall(r"\d+", getattr(result, "text", ""))
                    if numbers:
                        vitality = int(numbers[-1])

        if vitality is None:
            logger.warning("Tetris vitality OCR did not recognize a number")
            return CustomAction.RunResult(success=False)

        if vitality == 0:
            if not _finish(context, argv.node_name):
                return CustomAction.RunResult(success=False)
            PrintT(context, "tetris.vitality_exhausted")
        elif not _set_next(
            context, argv.node_name, _VITALITY_NEXT
        ) or not _press_escape(context):
            return CustomAction.RunResult(success=False)

        return CustomAction.RunResult(success=not context.tasker.stopping)
