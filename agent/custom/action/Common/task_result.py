from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


@AgentServer.custom_action("fail_task")
class FailTask(CustomAction):
    """Keep a pipeline failure visible after displaying its error message."""

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        return CustomAction.RunResult(success=False)
