from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

from utils.logger import logger
from utils.maafocus import PrintT
from utils.task_control import run_task_checked

FURNITURE_RECOG_NODES = [
    ("FurnitureHamsterBall", "仓鼠球", "hamster_ball"),
    ("FurnitureFluff", "棉棉", "fluff"),
    ("FurnitureDamagedCrate", "破损的木箱", "damaged_crate"),
    ("FurnitureIntactCrate", "完整的木箱", "intact_crate"),
]


@AgentServer.custom_action("furniture_claim")
class FurnitureClaim(CustomAction):
    def run(
        self, context: Context, _argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        controller = context.tasker.controller
        for node_name, name, msg_key in FURNITURE_RECOG_NODES:
            if context.tasker.stopping:
                return CustomAction.RunResult(success=False)
            job = controller.post_screencap().wait()
            if context.tasker.stopping or not job.succeeded:
                return CustomAction.RunResult(success=False)
            image = job.get()
            if context.tasker.stopping or image is None:
                return CustomAction.RunResult(success=False)
            result = context.run_recognition(node_name, image)
            if context.tasker.stopping:
                return CustomAction.RunResult(success=False)
            if result is None or not result.hit or not result.box:
                logger.debug("未识别到 %s", name)
                continue

            box = result.box
            if isinstance(box, (list, tuple)):
                roi = list(box)
            else:
                roi = [box.x, box.y, box.w, box.h]
            claim_override = {"FurnitureClaim": {"roi": roi}}
            result = context.run_recognition(
                "FurnitureClaim", image, pipeline_override=claim_override
            )
            if context.tasker.stopping:
                return CustomAction.RunResult(success=False)
            if result is None or not result.hit:
                logger.debug("识别到但无法领取 %s", name)
                continue

            if not run_task_checked(
                context, "FurnitureClaim", pipeline_override=claim_override
            ):
                return CustomAction.RunResult(success=False)
            if context.tasker.stopping:
                return CustomAction.RunResult(success=False)
            PrintT(context, f"furniture.claimed.{msg_key}")

        return CustomAction.RunResult(success=not context.tasker.stopping)
