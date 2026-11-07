"""统一进入游戏任务的控制器分流与 Win32 输入适配。"""

from functools import wraps

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from game_entry import EntryError, login_timeout_seconds, run_world_entry
from utils.logger import logger
from utils.maafocus import PrintT
from utils.pienv import controller_name

__all__ = ["EnterGameDispatch", "EnterGameWorld", "EnterGameFail"]
CLIENT_CONTROLLERS = ("Win32", "Win32-Front", "Win32-Background")


def _guarded(run):
    @wraps(run)
    def wrapper(self, context, argv):
        try:
            if context.tasker.stopping:
                raise EntryError("enter_game.stopped", "stopping")
            success = bool(run(self, context, argv))
        except EntryError as exc:
            logger.warning(
                "进入游戏操作未完成 | action=%s | reason=%s",
                run.__qualname__,
                exc.reason,
            )
            if not context.tasker.stopping:
                PrintT(context, exc.key, *exc.values)
            success = False
        except Exception as exc:
            logger.error(
                "进入游戏操作异常 | action=%s | error=%s",
                run.__qualname__,
                type(exc).__name__,
            )
            if not context.tasker.stopping:
                PrintT(context, "enter_game.failed")
            success = False
        return CustomAction.RunResult(success=success)

    return wrapper


@AgentServer.custom_action("enter_game_dispatch")
class EnterGameDispatch(CustomAction):
    @_guarded
    def run(self, context, argv):
        name = controller_name()
        if name not in CLIENT_CONTROLLERS + ("CloudGame-Front",):
            raise EntryError("enter_game.wrong_controller", "unsupported_controller")
        login_timeout_seconds(context)
        target = (
            "CloudGameStartEntrance" if name == "CloudGame-Front" else "EnterGameWorld"
        )
        logger.info("进入游戏分流 | controller=%s | target=%s", name, target)
        if not context.override_next(argv.node_name, [target]):
            raise EntryError("enter_game.invalid_config", "dispatch_failed")
        return True


def _wait_input(job):
    status = job.wait()
    if status is None or not status.succeeded:
        raise EntryError("enter_game.input_rejected", "controller_input_failed")
    return True


@AgentServer.custom_action("enter_game_world")
class EnterGameWorld(CustomAction):
    @_guarded
    def run(self, context, argv):
        if controller_name() not in CLIENT_CONTROLLERS:
            raise EntryError(
                "enter_game.wrong_controller", "unsupported_world_controller"
            )
        controller = context.tasker.controller

        def capture():
            status = controller.post_screencap().wait()
            if (
                status is None
                or not status.succeeded
                or controller.cached_image is None
            ):
                raise EntryError("enter_game.invalid_frame", "screencap_failed")
            return controller.cached_image

        def click(x, y):
            return _wait_input(controller.post_click(round(x), round(y)))

        def key(name):
            return _wait_input(controller.post_click_key(27))

        return run_world_entry(context, capture, click, key)


@AgentServer.custom_action("enter_game_fail")
class EnterGameFail(CustomAction):
    def run(self, context, argv):
        logger.error("进入游戏任务失败 | stopping=%s", context.tasker.stopping)
        return CustomAction.RunResult(success=False)
