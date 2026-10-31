"""flow 测试用的假 context / 控制器 / 时钟。

FakeContext 默认加载真实 Pipeline 资源（含 DungeonProfiles），选项载体按测试参数覆盖，
因此 reset 走的是真实的配置解析与 profile 注入路径。
"""

from __future__ import annotations

import copy
import json
import re
from types import SimpleNamespace

from ._loader import RESOURCE_BASE


def strip_comments(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_jsonc(path):
    return json.loads(strip_comments(path.read_text(encoding="utf-8-sig")))


def load_pipeline() -> dict:
    nodes = {}
    for path in sorted((RESOURCE_BASE / "pipeline").rglob("*.json")):
        data = load_jsonc(path)
        if isinstance(data, dict):
            nodes.update(data)
    return nodes


_PIPELINE = None


def pipeline() -> dict:
    global _PIPELINE
    if _PIPELINE is None:
        _PIPELINE = load_pipeline()
    return _PIPELINE


class Job:
    def __init__(self, value=None, succeeded=True, raises=None):
        self.value = value
        self.succeeded = succeeded
        self._raises = raises

    def wait(self):
        if self._raises is not None:
            raise self._raises
        return self

    def get(self):
        return self.value


class FakeController:
    def __init__(self):
        self.events: list[tuple] = []
        self.cached_image = "frame"
        self.fail_click = False  # 抛异常
        self.click_status = True  # 作业状态
        self.key_down_status = True
        self.key_down_raises = None
        self.key_up_status = True
        self.screencap_ok = True

    def post_screencap(self):
        self.events.append(("screencap",))
        return Job(self.cached_image if self.screencap_ok else None, succeeded=self.screencap_ok)

    def post_click(self, x, y):
        if self.fail_click:
            raise RuntimeError("simulated click failure")
        self.events.append(("click", x, y))
        return Job(succeeded=self.click_status)

    def post_key_down(self, vk):
        self.events.append(("down", vk))
        return Job(succeeded=self.key_down_status, raises=self.key_down_raises)

    def post_key_up(self, vk):
        self.events.append(("up", vk))
        return Job(succeeded=self.key_up_status)

    def count(self, kind):
        return sum(1 for e in self.events if e[0] == kind)

    def clicks(self):
        return [(e[1], e[2]) for e in self.events if e[0] == "click"]


class FakeTasker:
    def __init__(self, controller):
        self.controller = controller
        self.stopping = False


def ocr(*items):
    """构造多结果识别值：ocr(([x, y, w, h], "文本"), ...)。"""
    return [SimpleNamespace(box=list(rect), text=text) for rect, text in items]


class FakeContext:
    """nodes：真实 Pipeline + 选项载体覆盖；recognize：{节点名: 值或 callable}。

    识别值：None/False = 未命中；True = 命中；str = 命中且文本；list = 多结果（见 ocr()）。
    """

    def __init__(self, options=None, recognize=None, use_pipeline=True):
        self.controller = FakeController()
        self.tasker = FakeTasker(self.controller)
        self.nodes = copy.deepcopy(pipeline()) if use_pipeline else {}
        for name, attach in (options or {}).items():
            node = self.nodes.setdefault(name, {"enabled": False})
            if attach is None:
                self.nodes.pop(name, None)
            else:
                node["attach"] = attach
        self.recognize = recognize or {}
        self.overrides: list[tuple[str, list[str]]] = []
        self.pipeline_overrides: list[dict] = []
        self.recognitions: list[str] = []
        self.override_ok = True

    def get_node_data(self, name):
        node = self.nodes.get(name)
        return None if node is None else copy.deepcopy(node)

    def override_pipeline(self, override):
        self.pipeline_overrides.append(copy.deepcopy(override))
        for name, node in override.items():
            self.nodes[name] = copy.deepcopy(node)
        return self.override_ok

    def run_recognition(self, entry, image, pipeline_override=None):
        self.recognitions.append(entry)
        value = self.recognize.get(entry)
        if callable(value):
            value = value()
        if value is None or value is False or value == []:
            return SimpleNamespace(hit=False, best_result=None, filtered_results=[], box=None)
        if isinstance(value, list):
            return SimpleNamespace(hit=True, best_result=value[0], filtered_results=value, box=value[0].box)
        text = value if isinstance(value, str) else ""
        best = SimpleNamespace(box=[0, 0, 10, 10], text=text)
        return SimpleNamespace(hit=True, best_result=best, filtered_results=[best], box=best.box)

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


def options(runs="0", stage="clock_trick", reserve=None, reward=None, plan=None, **extra):
    """任务选项载体。reserve/reward/plan 为 None 时不提供该载体（模拟旧配置迁移）。"""
    opts = {
        "DungeonFarm_RunsOption": {"runs": runs},
        "DungeonFarm_StageOption": {"name": stage},
    }
    if reserve is not None:
        opts["DungeonFarm_PlanOption"] = {"mode": plan or "reserve"}
        opts["DungeonFarm_ReserveOption"] = {"reserve": str(reserve)}
    elif plan is not None:
        opts["DungeonFarm_PlanOption"] = {"mode": plan}
    if reward is not None:
        opts["DungeonFarm_RewardOption"] = {"reward_mode": reward}
    opts.update(extra)
    return opts


def combat_result(reason):
    return SimpleNamespace(reason=reason, success=reason in ("cleared", "timeout", "hud_lost"))
