"""flow 测试用的假 context / 控制器 / 时钟。"""

from __future__ import annotations

from types import SimpleNamespace


class Job:
    succeeded = True

    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class FakeController:
    def __init__(self):
        self.events: list[tuple] = []
        self.cached_image = "frame"
        self.fail_click = False

    def post_screencap(self):
        self.events.append(("screencap",))
        return Job(self.cached_image)

    def post_click(self, x, y):
        if self.fail_click:
            raise RuntimeError("simulated click failure")
        self.events.append(("click", x, y))
        return Job()

    def post_key_down(self, vk):
        self.events.append(("down", vk))
        return Job()

    def post_key_up(self, vk):
        self.events.append(("up", vk))
        return Job()

    def count(self, kind):
        return sum(1 for e in self.events if e[0] == kind)


class FakeTasker:
    def __init__(self, controller):
        self.controller = controller
        self.stopping = False


class FakeContext:
    """nodes: 节点 attach；recognize: {节点名: 结果或 callable}，结果为 None / 文本 / True。"""

    def __init__(self, nodes=None, recognize=None):
        self.controller = FakeController()
        self.tasker = FakeTasker(self.controller)
        self.nodes = nodes or {}
        self.recognize = recognize or {}
        self.overrides: list[tuple[str, list[str]]] = []
        self.recognitions: list[str] = []

    def get_node_data(self, name):
        attach = self.nodes.get(name)
        return None if attach is None else {"attach": attach}

    def run_recognition(self, entry, image, pipeline_override=None):
        self.recognitions.append(entry)
        value = self.recognize.get(entry)
        if callable(value):
            value = value()
        if value is None or value is False:
            return SimpleNamespace(hit=False, best_result=None)
        text = value if isinstance(value, str) else ""
        return SimpleNamespace(hit=True, best_result=SimpleNamespace(text=text))

    def override_next(self, name, next_list):
        self.overrides.append((name, list(next_list)))
        return True


class FakeClock:
    def __init__(self, step=0.0):
        self.t = 0.0
        self.step = step  # 每次读时间前进 step 秒，模拟识别耗时

    def __call__(self):
        self.t += self.step
        return self.t


def options(runs="0", stage="clock_trick", cost=40):
    return {
        "DungeonFarm_RunsOption": {"runs": runs},
        "DungeonFarm_StageOption": {"name": stage, "cost": cost},
    }


def combat_result(reason):
    return SimpleNamespace(reason=reason, success=reason in ("cleared", "timeout", "hud_lost"))
