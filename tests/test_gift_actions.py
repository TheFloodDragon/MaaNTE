"""赠礼动作离线回归：真实 catalog/state/actions，合成视觉不代表实机 OCR 验收。"""

import copy
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import importlib.util
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

ROOT = Path(__file__).resolve().parents[1]
GIFT_DIR = ROOT / "agent/custom/action/Gift"
PIPELINE = json.loads(
    (ROOT / "assets/resource/base/pipeline/Gift/Gift.json").read_text(encoding="utf-8")
)
CONFIG = json.loads(
    (ROOT / "assets/resource/base/pipeline/Gift/GiftConfig.json").read_text(
        encoding="utf-8"
    )
)
TASK = json.loads(
    (ROOT / "assets/resource/tasks/Gift.json").read_text(encoding="utf-8")
)


@dataclass(frozen=True)
class FrameView:
    """仅模拟视觉 API 契约；没有调用或验收真实视觉算法。"""

    page: str = "unknown"
    character_id: str | None = None
    identity_verified: bool = False
    gift_id: str | None = None
    gift_verified: bool = False
    selected: bool = False
    remaining: int | None = None
    total: int | None = None
    inventory: int | None = None
    quantity: int | None = None
    infinite: bool = False
    global_limit: bool = False
    send_box: tuple | None = None
    controls: dict = field(default_factory=dict)
    errors: tuple = ()


@dataclass(frozen=True)
class ScanInfo:
    category_verified: bool = False
    start_verified: bool = False
    end_verified: bool = False
    page_key: str = ""
    progress_evidence: tuple = ()
    single_page_verified: bool = False
    absence_verified: bool = False
    scroll_box: tuple | None = None
    visible_ids: tuple = ()
    errors: tuple = ()


def load_actions():
    """独立包加载真实相对导入，退出后恢复全部 sys.modules 与装饰器。"""
    package_name = "_gift_actions_test_subject"
    package = ModuleType(package_name)
    package.__path__ = [str(GIFT_DIR)]
    modules = {
        name: ModuleType(name)
        for name in ("utils", "utils.logger", "utils.maafocus", "utils.i18n")
    }
    logger = logging.getLogger("gift-actions-offline")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    modules["utils.logger"].logger = logger
    modules["utils.maafocus"].PrintT = lambda *args: None
    modules["utils.i18n"].T = lambda key, *args: key
    vision = ModuleType(package_name + ".vision")
    vision.FrameView = FrameView
    vision.GiftVision = None
    modules[package_name] = package
    modules[vision.__name__] = vision
    spec = importlib.util.spec_from_file_location(
        package_name + ".actions", GIFT_DIR / "actions.py"
    )
    module = importlib.util.module_from_spec(spec)
    modules[spec.name] = module
    registered = {}

    def register(name):
        def decorate(cls):
            registered[name] = cls
            return cls

        return decorate

    with patch.dict(sys.modules, modules), patch.object(
        AgentServer, "custom_action", register
    ):
        spec.loader.exec_module(module)
    return module, registered


actions, ACTIONS = load_actions()
CATALOG = actions.Catalog.load()
CHARACTERS = tuple(
    key for key, record in CATALOG.characters.items() if record["giftable"]
)
C1, C2, C3, C4 = CHARACTERS[:4]
A, B, C = tuple(CATALOG.gifts)[:3]
SEND_BOX = (1060, 610, 160, 50)
TAB_BOX = (820, 110, 100, 45)
LIST_BOX = (1060, 70, 120, 45)
CLOSE_BOX = (1180, 50, 70, 50)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def make_config(recipients=None, *, dry_run=False):
    data = copy.deepcopy(CONFIG)
    data["GiftSettings"]["attach"]["dry_run"] = dry_run
    for character_id, gifts, limit in (
        [(C1, (A, B), 3)] if recipients is None else recipients
    ):
        data.setdefault("GiftConfig_" + character_id, {}).setdefault(
            "attach", {}
        ).update(
            character_id=character_id,
            selected=True,
            gifts=">".join(gifts) if not isinstance(gifts, str) else gifts,
            limit=limit,
        )
    return data


def merge(left, right):
    """模拟多选项 pipeline_override 的深合并，不覆盖其他角色的 attach。"""
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            merge(left[key], value)
        else:
            left[key] = copy.deepcopy(value)
    return left


def ui_config(values=None, *, overrides=None):
    """沿客户端 select/switch 活跃分支应用真实任务选项，隐藏值不产生覆盖。"""
    values = values or {}
    data = copy.deepcopy(CONFIG)
    visible = []

    def apply(override):
        if overrides is not None and override:
            overrides.append(copy.deepcopy(override))
        merge(data, override)

    def substitute(value, variables):
        if isinstance(value, str):
            return variables.get(value, value)
        if isinstance(value, dict):
            return {key: substitute(item, variables) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item, variables) for item in value]
        return value

    def visit(key):
        option = TASK["option"][key]
        visible.append(key)
        if option["type"] == "input":
            fields = values.get(key, {})
            variables = {}
            for field in option["inputs"]:
                value = fields.get(field["name"], field.get("default", ""))
                if field.get("pipeline_type") == "int":
                    value = int(value)
                variables["{" + field["name"] + "}"] = value
            apply(substitute(option.get("pipeline_override", {}), variables))
            return
        if option["type"] not in {"select", "switch"}:
            raise AssertionError("当前客户端不展开checkbox子项: " + key)
        selected = values.get(key, option["default_case"])
        case = next(case for case in option["cases"] if case["name"] == selected)
        apply(case.get("pipeline_override", {}))
        for child in case.get("option", []):
            visit(child)

    for key in TASK["task"][0]["option"]:
        visit(key)
    return data, visible


class GiftWorld:
    """可观察的虚拟 UI；生产动作的实际输入才会切换角色、礼物和页码。"""

    def __init__(self, *, remaining=3, stock=None, gift_pages=None, page="characters"):
        self.clock = Clock()
        self.page = page
        self.character_id = None
        self.gift_id = None
        self.remaining = (
            dict(remaining)
            if isinstance(remaining, dict)
            else dict.fromkeys(CHARACTERS, remaining)
        )
        self.stock = dict.fromkeys((A, B, C), 20)
        if stock is not None:
            self.stock.update(stock)
        self.gift_pages = gift_pages or ((A, B, C),)
        self.character_pages = (CHARACTERS[:4],)
        self.indices = {"gifts": 0, "characters": 0}
        self.scroll_generation = 0
        self.scroll_moves = True
        self.scroll_step = 1
        self.view_overrides = {}
        self.scan_overrides = {}
        self.character_redirect = {}
        self.gift_redirect = {}
        self.duplicate_candidates = set()
        self.hidden_candidates = set()
        self.list_close_changes_page = True
        self.selection_keeps_list = False
        self.receipt = "immediate"
        self.receipt_delay = 6
        self.pending_receipt = None
        self.fail_inputs = set()
        self.raise_inputs = set()
        self.inputs = []
        self.sends = []
        self.captures = 0
        self.snapshots = {}
        self.navigation = 0
        self.before_capture = None
        self.after_inspect = None
        self.before_send = None
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)

    @staticmethod
    def center(box):
        x, y, width, height = box
        return x + width // 2, y + height // 2

    def visible(self, kind):
        pages = self.gift_pages if kind == "gifts" else self.character_pages
        return tuple(pages[self.indices[kind]])

    def boxes(self, kind):
        return {
            identifier: (70 + i * 150, 330 if kind == "gifts" else 180, 100, 65)
            for i, identifier in enumerate(self.visible(kind))
        }

    def view(self):
        known = self.page in {"bond", "gifts"}
        gift_page = self.page == "gifts"
        view = FrameView(
            page=self.page,
            character_id=self.character_id if known else None,
            identity_verified=known and self.character_id is not None,
            gift_id=self.gift_id if gift_page else None,
            gift_verified=gift_page and self.gift_id is not None,
            selected=gift_page and self.gift_id is not None,
            remaining=self.remaining.get(self.character_id) if gift_page else None,
            total=3 if gift_page else None,
            inventory=self.stock.get(self.gift_id) if gift_page else None,
            quantity=1 if gift_page else None,
            send_box=SEND_BOX if gift_page else None,
            controls={"gift_tab": TAB_BOX, "list_button": LIST_BOX} if known else {},
        )
        return replace(view, **self.view_overrides)

    def scan(self, kind, target_id):
        pages = self.gift_pages if kind == "gifts" else self.character_pages
        index = self.indices[kind]
        visible = self.visible(kind)
        info = ScanInfo(
            category_verified=self.page == kind,
            start_verified=index == 0,
            end_verified=index == len(pages) - 1,
            page_key=kind + ":" + str(index),
            progress_evidence=("page:第%d页/共%d页" % (index + 1, len(pages)),),
            single_page_verified=len(pages) == 1,
            absence_verified=target_id not in visible,
            scroll_box=(40, 280, 700, 300) if kind == "gifts" else (40, 150, 1100, 450),
            visible_ids=visible,
        )
        return replace(info, **self.scan_overrides.get((kind, index), {}))

    def _receipt(self, character_id, gift_id):
        if self.receipt == "popup":
            self.page = "unknown"
        elif self.receipt == "global":
            self.view_overrides["global_limit"] = True
        else:
            decrement = 2 if self.receipt == "jump" else 1
            self.remaining[character_id] -= decrement
            self.stock[gift_id] -= (
                2 if self.receipt == "inventory_conflict" else decrement
            )

    def screencap(self):
        self.captures += 1
        self.clock.now += 0.1
        if self.before_capture is not None:
            self.before_capture(self)
        if self.pending_receipt is not None:
            when, character_id, gift_id = self.pending_receipt
            if self.captures >= when:
                self.pending_receipt = None
                self._receipt(character_id, gift_id)
        snapshot = SimpleNamespace(
            view=self.view(),
            characters=(
                self.boxes("characters")
                if self.page in {"characters", "bond", "gifts"}
                else {}
            ),
            gifts=self.boxes("gifts") if self.page == "gifts" else {},
            scans={
                (kind, target): self.scan(kind, target)
                for kind, targets in (
                    ("gifts", (A, B, C)),
                    ("characters", CHARACTERS[:4]),
                )
                for target in targets
            },
        )
        self.snapshots[self.captures] = snapshot
        marker = self.captures
        self.image[0, 0] = (marker & 255, (marker >> 8) & 255, (marker >> 16) & 255)
        return self.image.copy()

    def click(self, x, y):
        target = None
        if (x, y) == self.center(SEND_BOX) and self.page == "gifts":
            kind = "send"
        elif (x, y) == self.center(TAB_BOX) and self.page == "bond":
            kind = "tab"
        elif (x, y) == self.center(LIST_BOX) and self.page in {"bond", "gifts"}:
            kind = "list"
        elif (x, y) == self.center(CLOSE_BOX) and self.page == "characters":
            kind = "close_list"
        else:
            kind = ""
            for candidate_kind in ("characters", "gifts"):
                for identifier, box in self.boxes(candidate_kind).items():
                    if (x, y) == self.center(box):
                        kind, target = candidate_kind, identifier
            if not kind:
                raise AssertionError("未授权的虚拟界面点击: %r" % ((self.page, x, y),))
        self.inputs.append((self.captures, kind, target))
        if kind == "send":
            if self.before_send is not None:
                self.before_send()
            self.sends.append((self.character_id, self.gift_id))
        if kind in self.raise_inputs:
            raise RuntimeError("模拟控制器异常")
        if kind in self.fail_inputs:
            return False
        if kind == "characters":
            self.character_id = self.character_redirect.get(target, target)
            self.page = "characters" if self.selection_keeps_list else "bond"
            self.gift_id = None
            self.indices["gifts"] = 0
        elif kind == "gifts":
            self.gift_id = self.gift_redirect.get(target, target)
        elif kind == "tab":
            self.page = "gifts"
        elif kind == "list":
            self.page = "characters"
        elif kind == "close_list":
            if self.list_close_changes_page:
                self.page = "bond"
                self.view_overrides.clear()
        elif self.receipt == "delayed":
            self.pending_receipt = (
                self.captures + self.receipt_delay,
                self.character_id,
                self.gift_id,
            )
        elif self.receipt != "none":
            self._receipt(self.character_id, self.gift_id)
        return True

    def swipe(self, x1, y1, x2, y2, duration):
        kind = "gifts" if self.page == "gifts" else "characters"
        self.inputs.append((self.captures, "swipe", (kind, y1 > y2)))
        if "swipe" in self.fail_inputs:
            return False
        if self.scroll_moves:
            pages = self.gift_pages if kind == "gifts" else self.character_pages
            self.indices[kind] = min(
                len(pages) - 1,
                max(0, self.indices[kind] + self.scroll_step * (1 if y1 > y2 else -1)),
            )
            self.scroll_generation += 1
        return True

    def enter_bond(self):
        self.navigation += 1
        self.page = "characters"
        return True


class MarkerVision:
    def __init__(self, world):
        self.world = world

    def snapshot(self, image):
        blue, green, red = map(int, image[0, 0])
        return self.world.snapshots[blue | (green << 8) | (red << 16)]

    def inspect(self, image):
        result = self.snapshot(image).view
        if self.world.after_inspect is not None:
            self.world.after_inspect()
        return result

    def character_candidates(self, image, identifier):
        box = self.snapshot(image).characters.get(identifier)
        return (
            [box] * (2 if identifier in self.world.duplicate_candidates else 1)
            if box
            else []
        )

    def gift_candidates(self, image, identifier):
        box = self.snapshot(image).gifts.get(identifier)
        return (
            [box] * (2 if identifier in self.world.duplicate_candidates else 1)
            if box
            else []
        )

    def scan_info(self, image, *, kind, target_id):
        return self.snapshot(image).scans[(kind, target_id)]


@contextmanager
def patched_actions(world):
    messages = []
    with (
        patch.object(actions, "_runtime", None),
        patch.object(
            actions, "GiftVision", lambda context, catalog: MarkerVision(world)
        ),
        patch.object(actions, "time", world.clock),
        patch.object(
            actions, "PrintT", lambda context, key, *args: messages.append((key, *args))
        ),
    ):
        yield messages


class UnitController:
    def __init__(self, world):
        self.world = world
        self.cached_image = None
        self.capture_succeeds = True

    @staticmethod
    def job(succeeded):
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=succeeded))

    def post_screencap(self):
        self.cached_image = self.world.screencap()
        return self.job(self.capture_succeeds)

    def post_click(self, x, y):
        return self.job(self.world.click(x, y))

    def post_swipe(self, *args):
        return self.job(self.world.swipe(*args))


class UnitContext:
    def __init__(self, world, config):
        self.tasker = SimpleNamespace(stopping=False, controller=UnitController(world))
        self.config = config
        self.routes = {}
        self.read_nodes = []

    def get_node_data(self, name):
        self.read_nodes.append(name)
        return copy.deepcopy(self.config.get(name))

    def override_next(self, name, nodes):
        self.routes[name] = nodes
        return True


def arg(node, params=None):
    return SimpleNamespace(node_name=node, custom_action_param=json.dumps(params or {}))


class Flow:
    """按真实 JSON next/on_error 驱动单测；原生调度由 native suite 另行验证。"""

    def __init__(self, world, config, messages):
        self.world = world
        self.context = UnitContext(world, config)
        self.messages = messages
        self.next_node = "GiftEntry"
        self.trace = []
        self.failed = []

    @property
    def runtime(self):
        return actions._runtime

    def invoke(self, node):
        name = PIPELINE[node]["action"]["param"]["custom_action"]
        return ACTIONS[name]().run(self.context, arg(node))

    def step(self):
        node = self.next_node
        self.trace.append(node)
        if node == "GiftEnsureBond":
            if self.world.page not in {"characters", "bond", "gifts"}:
                self.world.enter_bond()
            self.next_node = "GiftBondReady"
        elif node == "GiftBondReady":
            self.next_node = "GiftChooseCharacter"
        else:
            result = self.invoke(node)
            if result.success:
                nodes = self.context.routes.get(node, PIPELINE[node].get("next", []))
            else:
                self.failed.append(node)
                nodes = PIPELINE[node].get("on_error", [])
            self.next_node = nodes[0] if nodes else None
        return self.next_node

    def until(self, node):
        for _ in range(1500):
            if self.next_node == node:
                return
            if self.next_node is None:
                raise AssertionError("流程未到达 %s；trace=%s" % (node, self.trace))
            self.step()
        raise AssertionError("流程未有限结束: %s" % self.trace[-12:])

    def finish(self):
        self.until(None)
        return self

    def states(self):
        return {state.character_id: state for state in self.runtime.session.states}

    def summary(self):
        return [item[1:] for item in self.messages if item[0] == "gift.summary"][-1]


@contextmanager
def flow(world=None, config=None):
    world = world or GiftWorld()
    with patched_actions(world) as messages:
        yield Flow(world, make_config() if config is None else config, messages)


class GiftActionTests(unittest.TestCase):
    def test_isolated_import_restores_modules_and_registration(self):
        names = (
            "utils",
            "utils.logger",
            "utils.i18n",
            "utils.maafocus",
            "_gift_actions_test_subject",
        )
        before = {name: sys.modules.get(name) for name in names}
        decorator = AgentServer.custom_action
        loaded, registered = load_actions()
        self.assertEqual(before, {name: sys.modules.get(name) for name in names})
        self.assertEqual(AgentServer.custom_action, decorator)
        self.assertIn("gift_send_one", registered)
        self.assertIsNot(loaded, actions)
        # 先在隔离域外导入 Maa：patch.dict 退出不能卸载真实回调使用的类型。
        # 否则后续 native suite 重导入同名类，会拒绝旧类的 RunResult。
        from maa.custom_action import CustomAction as NativeCustomAction

        self.assertIs(loaded.CustomAction, NativeCustomAction)
        self.assertIs(actions.CustomAction, CustomAction)

    def test_reset_loads_real_catalog_and_uses_independent_config_nodes(self):
        with flow() as run:
            self.assertTrue(run.invoke("GiftEntry").success)
            self.assertEqual(len(run.runtime.catalog.gifts), 214)
            self.assertEqual(len(run.runtime.catalog.characters), 24)
            self.assertEqual(run.runtime.session.current.recipient.gifts, (A, B))
            self.assertIn("GiftConfig_" + C1, run.context.read_nodes)

    def test_gift_page_opens_verified_list_for_offscreen_character(self):
        world = GiftWorld(page="gifts")
        world.character_id = C2
        world.character_pages = ((C2,), (C1,))
        with flow(world) as run:
            run.until("GiftChooseCharacter")
            run.step()
            self.assertEqual(run.next_node, "GiftWaitCharacterList")
            self.assertEqual([kind for _, kind, _ in world.inputs], ["list"])
            self.assertEqual(world.sends, [])

    def test_verified_character_list_closes_before_using_underlying_gift_tab(self):
        for already_selected in (False, True):
            with self.subTest(already_selected=already_selected):
                world = GiftWorld()
                world.selection_keeps_list = True
                if already_selected:
                    world.character_id = C1

                def selected_list_view(current):
                    if current.page == "characters" and current.character_id == C1:
                        current.view_overrides.update(
                            character_id=C1,
                            identity_verified=True,
                            controls={"close_list": CLOSE_BOX, "gift_tab": TAB_BOX},
                        )

                world.before_capture = selected_list_view
                with flow(world) as run:
                    run.finish()
                    kinds = [kind for _, kind, _ in world.inputs]
                    self.assertEqual(kinds.count("close_list"), 1)
                    self.assertLess(kinds.index("close_list"), kinds.index("tab"))
                    self.assertEqual(world.sends, [(C1, A)] * 3)
                    self.assertEqual(run.failed, [])

    def test_character_list_close_is_not_replayed_when_page_does_not_change(self):
        world = GiftWorld()
        world.character_id = C1
        world.list_close_changes_page = False
        world.view_overrides.update(
            character_id=C1,
            identity_verified=True,
            controls={"close_list": CLOSE_BOX, "gift_tab": TAB_BOX},
        )
        with flow(world) as run:
            run.finish()
            self.assertEqual([kind for _, kind, _ in world.inputs], ["close_list"])
            self.assertEqual(world.sends, [])
            self.assertIn("GiftWaitCharacter", run.failed)
            self.assertEqual(run.summary()[0], 0)

    def test_character_list_needs_identity_and_close_control_before_input(self):
        world = GiftWorld()
        world.character_id = C1
        world.view_overrides.update(
            character_id=C1, identity_verified=True, controls={"gift_tab": TAB_BOX}
        )
        with flow(world) as run:
            run.invoke("GiftEntry")
            self.assertFalse(run.invoke("GiftChooseCharacter").success)
            self.assertEqual(world.inputs, [])
        for identity in ({"identity_verified": False}, {"character_id": C2}):
            with self.subTest(identity=identity):
                world = GiftWorld()
                world.view_overrides.update(
                    character_id=C1,
                    identity_verified=True,
                    controls={"close_list": CLOSE_BOX},
                )
                world.view_overrides.update(identity)
                with flow(world) as run:
                    run.invoke("GiftEntry")
                    actions._arm("character")
                    self.assertTrue(run.invoke("GiftWaitCharacter").success)
                    self.assertEqual(world.inputs, [])
                    self.assertEqual(world.sends, [])

    def test_daily_quota_and_priority_do_not_restart_at_three(self):
        for remaining, limit, expected in (
            (3, 3, 3),
            (2, 3, 2),
            (0, 3, 0),
            (2, 1, 0),
            (3, 1, 1),
        ):
            with self.subTest(remaining=remaining, limit=limit), flow(
                GiftWorld(remaining=remaining), make_config([(C1, (A, B), limit)])
            ) as run:
                run.finish()
                self.assertEqual(run.world.sends, [(C1, A)] * expected)
                self.assertEqual(run.states()[C1].confirmed, expected)
                self.assertFalse(
                    any(
                        kind == "gifts" and target == B
                        for _, kind, target in run.world.inputs
                    )
                )
                self.assertEqual(run.summary()[0], expected)
                self.assertEqual(run.failed, [])

    def test_exhausted_first_gift_falls_back_only_after_verified_zero(self):
        with flow(GiftWorld(stock={A: 1, B: 5})) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, A), (C1, B), (C1, B)])
            self.assertEqual(run.states()[C1].confirmed, 3)
            self.assertEqual(run.failed, [])

    def test_preferred_gift_on_second_page_beats_visible_fallback(self):
        with flow(GiftWorld(gift_pages=((B,), (A,)))) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, A)] * 3)
            self.assertEqual(sum(kind == "swipe" for _, kind, _ in run.world.inputs), 1)
            self.assertFalse(
                any(
                    kind == "gifts" and target == B
                    for _, kind, target in run.world.inputs
                )
            )

    def test_absence_requires_complete_multiframe_scan_before_fallback(self):
        with flow(GiftWorld(gift_pages=((B,), (C,)))) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, B)] * 3)
            self.assertEqual(run.failed, [])
            self.assertEqual(sum(kind == "swipe" for _, kind, _ in run.world.inputs), 2)
            first_selection = next(
                i for i, item in enumerate(run.world.inputs) if item[1] == "gifts"
            )
            self.assertTrue(
                any(item[1] == "swipe" for item in run.world.inputs[:first_selection])
            )

    def test_low_confidence_or_unproven_absence_never_falls_back(self):
        faults = (
            (0, {"category_verified": False}),
            (0, {"absence_verified": False}),
            (0, {"errors": ("low_score",)}),
            (1, {"progress_evidence": ()}),
            (1, {"end_verified": False}),
        )
        for index, override in faults:
            with self.subTest(override=override):
                world = GiftWorld(gift_pages=((B,), (C,)))
                world.scan_overrides[("gifts", index)] = override
                with flow(world) as run:
                    run.finish()
                    self.assertEqual(world.sends, [])
                    self.assertFalse(
                        any(
                            kind == "gifts" and target == B
                            for _, kind, target in world.inputs
                        )
                    )
                    self.assertTrue(
                        any(item[0] == "gift.warning" for item in run.messages)
                    )

    def test_no_scroll_progress_is_bounded_and_not_replayed(self):
        world = GiftWorld(gift_pages=((B,), (A,)))
        world.scroll_moves = False
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertEqual(sum(kind == "swipe" for _, kind, _ in world.inputs), 1)
            self.assertIn("GiftWaitScroll", run.failed)
            self.assertLess(world.captures, 150)

    def test_changed_fingerprint_without_scroll_evidence_never_falls_back(self):
        world = GiftWorld(gift_pages=((B,), (C,)))
        world.scroll_moves = False

        def animate_after_input(item):
            if any(kind == "swipe" for _, kind, _ in item.inputs):
                item.scan_overrides[("gifts", 0)] = {
                    "page_key": "animation:" + str(item.captures)
                }

        world.before_capture = animate_after_input
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertEqual(sum(kind == "swipe" for _, kind, _ in world.inputs), 1)
            self.assertIn("GiftWaitScroll", run.failed)
            self.assertFalse(
                any(kind == "gifts" and target == B for _, kind, target in world.inputs)
            )

    def test_skipping_middle_page_cannot_prove_preferred_gift_absent(self):
        world = GiftWorld(gift_pages=((B,), (A,), (C,)))
        world.scroll_step = 2
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertIn("GiftWaitScroll", run.failed)
            self.assertEqual(sum(kind == "swipe" for _, kind, _ in world.inputs), 1)
            self.assertFalse(
                any(kind == "gifts" and target == B for _, kind, target in world.inputs)
            )

    def test_inertial_page_change_before_swipe_cannot_skip_preferred_gift(self):
        world = GiftWorld(gift_pages=((B,), (A,), (C,)))
        with flow(world) as run:
            run.until("GiftScrollList")
            # FindGift 只扫了第一页；实际输入前惯性已滑到含 A 的第二页。
            world.indices["gifts"] = 1
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertFalse(any(kind == "swipe" for _, kind, _ in world.inputs))
            self.assertFalse(
                any(kind == "gifts" and target == B for _, kind, target in world.inputs)
            )
            self.assertIn("GiftScrollList", run.failed)

    def test_single_page_verified_absence_needs_no_scroll_box(self):
        world = GiftWorld(gift_pages=((B,),))
        world.scan_overrides[("gifts", 0)] = {"scroll_box": None}
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, B)] * 3)
            self.assertEqual(run.failed, [])
            self.assertFalse(any(kind == "swipe" for _, kind, _ in world.inputs))

    def test_single_page_without_complete_boundary_proof_cannot_fallback(self):
        world = GiftWorld(gift_pages=((B,),))
        world.scan_overrides[("gifts", 0)] = {"single_page_verified": False}
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertFalse(
                any(kind == "gifts" and target == B for _, kind, target in world.inputs)
            )

    def test_multiple_characters_have_separate_gifts_quotas_and_receipts(self):
        config = make_config([(C1, (A, B), 1), (C2, (B, A), 3), (C3, (C,), 2)])
        world = GiftWorld(remaining={C1: 3, C2: 2, C3: 3})
        with flow(world, config) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, A), (C2, B), (C2, B), (C3, C), (C3, C)])
            self.assertEqual(
                [state.confirmed for state in run.runtime.session.states], [1, 2, 2]
            )
            self.assertEqual(run.summary()[0], 5)

    def test_wrong_first_character_warns_then_second_character_succeeds(self):
        world = GiftWorld()
        world.character_redirect[C1] = C2
        with flow(world, make_config([(C1, (A,), 1), (C2, (B,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [(C2, B)])
            self.assertIn("GiftWaitCharacter", run.failed)
            self.assertEqual(run.states()[C1].confirmed, 0)
            self.assertEqual(run.states()[C2].confirmed, 1)
            self.assertEqual(run.summary()[0], 1)
            self.assertEqual(run.summary()[3], 1)

    def test_unknown_recovery_warns_every_remaining_character_without_input(self):
        with flow(config=make_config([(C1, (A,), 3), (C2, (B,), 3)])) as run:
            run.until("GiftChooseCharacter")
            run.world.page = "unknown"
            run.finish()
            self.assertEqual(run.world.inputs, [])
            self.assertEqual(run.world.sends, [])
            self.assertEqual(
                len([m for m in run.messages if m[0] == "gift.warning"]), 2
            )
            self.assertEqual(run.summary(), (0, 0, 0, 2, 0))

    def test_send_gate_rechecks_all_evidence_on_a_fresh_frame(self):
        faults = (
            {"character_id": C2},
            {"identity_verified": False},
            {"gift_id": B},
            {"gift_verified": False},
            {"selected": False},
            {"quantity": 2},
            {"quantity": 0},
            {"quantity": True},
            {"quantity": None},
            {"infinite": True},
            {"global_limit": True},
            {"inventory": None},
            {"inventory": -1},
            {"inventory": True},
            {"total": 4},
            {"remaining": 4},
            {"page": "unknown"},
            {"send_box": (-10, 600, 100, 40)},
            {"send_box": None},
        )
        for override in faults:
            with self.subTest(override=override), flow() as run:
                run.until("GiftSendOne")
                previous_frame = run.runtime.frame_id
                run.world.view_overrides.update(override)
                run.finish()
                self.assertGreater(run.runtime.frame_id, previous_frame)
                self.assertEqual(run.world.sends, [])
                self.assertEqual(run.states()[C1].confirmed, 0)
                self.assertTrue(run.failed)

    def test_wrong_gift_selection_never_uses_icon_candidate_as_identity(self):
        world = GiftWorld()
        world.gift_redirect[A] = B
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertIn("GiftVerifyGift", run.failed)
            self.assertEqual(sum(kind == "gifts" for _, kind, _ in world.inputs), 1)

    def test_ambiguous_character_or_gift_candidates_are_not_clicked(self):
        for identifier in (C1, A):
            world = GiftWorld()
            world.duplicate_candidates.add(identifier)
            with self.subTest(identifier=identifier), flow(world) as run:
                run.finish()
                self.assertEqual(world.sends, [])
                self.assertFalse(
                    any(target == identifier for _, _, target in world.inputs)
                )

    def test_dry_run_and_zero_budget_never_submit(self):
        for mode in ("dry_run", "budget"):
            with self.subTest(mode=mode), flow(
                config=make_config(dry_run=mode == "dry_run")
            ) as run:
                run.until("GiftSendOne")
                if mode == "budget":
                    run.runtime.session._send_budget = 0
                run.finish()
                self.assertEqual(run.world.sends, [])
                self.assertEqual(run.runtime.session.attempts, 0)
                self.assertEqual(run.states()[C1].confirmed, 0)
                if mode == "dry_run":
                    self.assertEqual(run.summary(), (0, 0, 1, 0, 0))

    def test_real_ten_attempt_budget_caps_four_characters_without_eleventh_send(self):
        config = make_config([(character, (A,), 3) for character in (C1, C2, C3, C4)])
        with flow(config=config) as run:
            run.finish()
            self.assertEqual(len(run.world.sends), 10)
            self.assertEqual(run.runtime.session.attempts, 10)
            self.assertEqual(
                [state.confirmed for state in run.runtime.session.states], [3, 3, 3, 1]
            )
            self.assertIsNone(run.runtime.session.pending)
            self.assertEqual(run.summary()[0], 10)

    def test_global_limit_blocks_later_characters_without_extra_input(self):
        with flow(config=make_config([(C1, (A,), 3), (C2, (B,), 3)])) as run:
            run.until("GiftSendOne")
            count = len(run.world.inputs)
            run.world.view_overrides["global_limit"] = True
            run.finish()
            self.assertEqual(run.world.sends, [])
            self.assertEqual(len(run.world.inputs), count)
            self.assertEqual(run.summary(), (0, 0, 0, 2, 0))

    def test_confirmed_click_is_submitted_once_and_pending_precedes_controller(self):
        with flow(config=make_config([(C1, (A,), 1)])) as run:
            seen = []
            run.world.before_send = lambda: seen.append(run.runtime.session.pending)
            run.finish()
            self.assertEqual(run.world.sends, [(C1, A)])
            self.assertEqual(len(seen), 1)
            self.assertIsNotNone(seen[0])
            self.assertEqual(run.states()[C1].confirmed, 1)
            self.assertIsNone(run.runtime.session.pending)

    def test_delayed_decrement_is_observed_without_replaying_send(self):
        world = GiftWorld()
        world.receipt = "delayed"
        with flow(world, make_config([(C1, (A,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, A)])
            self.assertGreater(run.trace.count("GiftWaitReceipt"), 1)
            self.assertEqual(run.states()[C1].confirmed, 1)
            self.assertEqual(run.failed, [])

    def test_missing_or_conflicting_receipt_locks_character_without_replay(self):
        for mode in ("none", "jump", "inventory_conflict", "popup"):
            world = GiftWorld()
            world.receipt = mode
            with self.subTest(mode=mode), flow(world) as run:
                run.finish()
                self.assertEqual(world.sends, [(C1, A)])
                self.assertEqual(run.states()[C1].confirmed, 0)
                self.assertEqual(run.states()[C1].status, "uncertain")
                self.assertIsNone(run.runtime.session.pending)
                self.assertLess(world.captures, 160)
                if mode in {"none", "popup"}:
                    self.assertIn("GiftReceiptFailed", run.trace)

    def test_send_input_failure_or_exception_never_replays(self):
        for exception in (False, True):
            world = GiftWorld()
            (world.raise_inputs if exception else world.fail_inputs).add("send")
            with self.subTest(exception=exception), flow(world) as run:
                run.finish()
                self.assertEqual(world.sends, [(C1, A)])
                self.assertEqual(run.states()[C1].confirmed, 0)
                self.assertEqual(run.states()[C1].status, "uncertain")
                self.assertIn("GiftSendOne", run.failed)
                self.assertIn("GiftReceiptFailed", run.trace)
                self.assertIsNone(run.runtime.session.pending)

    def test_wait_receipt_is_bounded_even_when_monotonic_clock_does_not_advance(self):
        world = GiftWorld()
        world.receipt = "none"
        with flow(world) as run:
            run.until("GiftWaitReceipt")
            world.before_capture = lambda item: setattr(item.clock, "now", 0.0)
            run.finish()
            self.assertEqual(world.sends, [(C1, A)])
            self.assertEqual(run.trace.count("GiftWaitReceipt"), 121)
            self.assertIn("GiftWaitReceipt", run.failed)

    def test_stopping_at_each_action_boundary_prevents_capture_and_input(self):
        for node in actions._ROUTES:
            with self.subTest(node=node), flow() as run:
                self.assertTrue(run.invoke("GiftEntry").success)
                run.context.tasker.stopping = True
                before = (run.world.captures, list(run.world.inputs))
                self.assertFalse(run.invoke(node).success)
                self.assertEqual((run.world.captures, run.world.inputs), before)

    def test_stop_after_screencap_or_authorization_never_sends(self):
        for phase in ("capture", "inspect"):
            with self.subTest(phase=phase), flow() as run:
                run.until("GiftSendOne")
                stop = lambda: setattr(run.context.tasker, "stopping", True)
                if phase == "capture":
                    run.world.before_capture = lambda _: stop()
                else:
                    run.world.after_inspect = stop
                self.assertFalse(run.invoke("GiftSendOne").success)
                self.assertEqual(run.world.sends, [])
                self.assertIsNone(run.runtime.session.pending)

    def test_stop_with_pending_cancels_it_without_accepting_unobserved_success(self):
        with flow() as run:
            run.until("GiftWaitReceipt")
            run.context.tasker.stopping = True
            self.assertFalse(run.invoke("GiftWaitReceipt").success)
            self.assertEqual(run.world.sends, [(C1, A)])
            self.assertEqual(run.states()[C1].confirmed, 0)
            self.assertIsNone(run.runtime.session.pending)

    def test_invalid_configuration_is_local_to_one_character(self):
        faults = (
            {"gifts": ""},
            {"gifts": "unknown-gift"},
            {"gifts": []},
            {"limit": 0},
            {"limit": 4},
            {"limit": True},
            {"limit": "3"},
            {"selected": "true"},
            {"character_id": "not-a-character"},
        )
        for override in faults:
            config = make_config([(C1, (A,), 3), (C2, (B,), 1)])
            config["GiftConfig_" + C1]["attach"].update(override)
            with self.subTest(override=override), flow(config=config) as run:
                run.finish()
                self.assertEqual(run.world.sends, [(C2, B)])
                self.assertEqual(run.summary(), (1, 0, 0, 1, 0))
                self.assertTrue(any(item[0] == "gift.warning" for item in run.messages))

    def test_no_selection_or_only_invalid_config_finishes_with_zero_input(self):
        for mode in ("empty", "invalid", "nongiftable"):
            config = make_config([])
            if mode == "invalid":
                config = make_config([(C1, "", 3)])
            if mode == "nongiftable":
                identifier = next(
                    key
                    for key, item in CATALOG.characters.items()
                    if not item["giftable"]
                )
                config = make_config([(identifier, (A,), 3)])
            with self.subTest(mode=mode), flow(config=config) as run:
                run.finish()
                self.assertEqual(run.world.inputs, [])
                self.assertEqual(run.world.sends, [])
                self.assertEqual(run.summary()[0], 0)
                self.assertTrue(any(item[0] == "gift.warning" for item in run.messages))

    def test_invalid_dry_run_does_not_default_to_sending(self):
        for value in (None, "false", 0, 1):
            with self.subTest(value=value), flow(
                config=make_config(dry_run=value)
            ) as run:
                run.finish()
                self.assertEqual(run.world.inputs, [])
                self.assertEqual(run.world.sends, [])
                self.assertIn("GiftEntry", run.failed)

    def test_visible_row_options_keep_each_character_configuration_and_row_order(self):
        data, visible = ui_config(
            {
                "GiftPlanCount": "2",
                "GiftRecipient_1": C2,
                "GiftRecipient_2": C1,
                "GiftPrimary_" + C2: B,
                "GiftPrimary_" + C1: A,
                "GiftLimit_" + C2: "1",
                "GiftLimit_" + C1: "2",
            }
        )
        for character in (C1, C2):
            self.assertIn("GiftPrimary_" + character, visible)
            self.assertIn("GiftBackup_" + character, visible)
            self.assertIn("GiftExtra_" + character, visible)
            self.assertIn("GiftLimit_" + character, visible)
        with flow(config=data) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C2, B), (C1, A), (C1, A)])
            self.assertEqual(run.states()[C1].recipient.gifts, (A,))
            self.assertEqual(run.states()[C2].recipient.gifts, (B,))

    def test_ui_list_choices_follow_primary_backup_and_unbounded_extra_order(self):
        data, _ = ui_config(
            {
                "GiftRecipient_1": C1,
                "GiftPrimary_" + C1: A,
                "GiftBackup_" + C1: B,
                "GiftExtra_" + C1: {"extra_gifts": "%s > %s > %s" % (C, A, B)},
            }
        )
        with flow(GiftWorld(stock={A: 1, B: 1, C: 5}), config=data) as run:
            run.finish()
            self.assertEqual(run.states()[C1].recipient.gifts, (A, B, C))
            self.assertEqual(run.world.sends, [(C1, A), (C1, B), (C1, C)])

    def test_ui_manual_mode_uses_saved_text_and_ignores_hidden_choice_values(self):
        data, visible = ui_config(
            {
                "GiftRecipient_1": C1,
                "GiftPriorityMode_" + C1: "Custom",
                "GiftPriority_" + C1: {"gifts": B + " > " + A},
                "GiftPrimary_" + C1: "invalid-hidden-gift",
                "GiftLimit_" + C1: "1",
            }
        )
        self.assertIn("GiftPriority_" + C1, visible)
        self.assertNotIn("GiftPrimary_" + C1, visible)
        with flow(config=data) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, B)])
            self.assertEqual(run.states()[C1].recipient.gifts, (B, A))

    def test_ui_empty_roles_or_missing_primary_never_choose_a_default_gift(self):
        for values in ({}, {"GiftRecipient_1": C1}):
            with self.subTest(values=values):
                data, _ = ui_config(values)
                with flow(config=data) as run:
                    run.finish()
                    self.assertEqual(run.world.inputs, [])
                    self.assertEqual(run.world.sends, [])
                    self.assertEqual(run.runtime.session.attempts, 0)

    def test_ui_duplicate_role_is_processed_once_without_increasing_daily_limit(self):
        data, _ = ui_config(
            {
                "GiftPlanCount": "2",
                "GiftRecipient_1": C1,
                "GiftRecipient_2": C1,
                "GiftPrimary_" + C1: A,
                "GiftLimit_" + C1: "1",
            }
        )
        with flow(config=data) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, A)])
            self.assertEqual(len(run.runtime.session.states), 1)

    def test_ui_hidden_rows_and_unselected_character_overrides_do_not_send(self):
        data, _ = ui_config(
            {
                "GiftPlanCount": "1",
                "GiftRecipient_1": C1,
                "GiftRecipient_2": C2,
                "GiftPrimary_" + C1: A,
                "GiftPrimary_" + C2: B,
                "GiftLimit_" + C1: "1",
            }
        )
        # 即使外部输入意外遗留旧selected，行模式只执行当前可见方案。
        data["GiftConfig_" + C2]["attach"].update(selected=True, gifts=B)
        data["GiftSettings"]["attach"]["recipient_2"] = C2
        with flow(config=data) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, A)])
            self.assertNotIn(C2, run.states())

    def test_ui_invalid_gift_choice_only_skips_its_recipient(self):
        faults = (
            {"primary_gift": ""},
            {"primary_gift": None},
            {"primary_gift": "unknown-gift"},
            {"backup_gift": False},
            {"backup_gift": "unknown-gift"},
            {"extra_gifts": []},
            {"extra_gifts": " > "},
            {"gift_mode": "unknown"},
        )
        original, _ = ui_config(
            {
                "GiftPlanCount": "2",
                "GiftRecipient_1": C1,
                "GiftRecipient_2": C2,
                "GiftPrimary_" + C1: A,
                "GiftPrimary_" + C2: B,
                "GiftLimit_" + C2: "1",
            }
        )
        for fault in faults:
            data = copy.deepcopy(original)
            data["GiftConfig_" + C1]["attach"].update(fault)
            with self.subTest(fault=fault), flow(config=data) as run:
                run.finish()
                self.assertEqual(run.world.sends, [(C2, B)])
                self.assertEqual(run.summary(), (1, 0, 0, 1, 0))

    def test_ui_invalid_row_count_never_defaults_to_legacy_selected_recipients(self):
        original, _ = ui_config({"GiftRecipient_1": C1, "GiftPrimary_" + C1: A})
        for value in (0, True, "1", 24, None):
            data = copy.deepcopy(original)
            data["GiftSettings"]["attach"]["plan_count"] = value
            with self.subTest(value=value), flow(config=data) as run:
                run.finish()
                self.assertEqual(run.world.inputs, [])
                self.assertEqual(run.runtime.session.attempts, 0)

    def test_ui_unknown_row_character_does_not_prevent_other_valid_rows(self):
        data, _ = ui_config(
            {
                "GiftPlanCount": "2",
                "GiftRecipient_1": C1,
                "GiftRecipient_2": C2,
                "GiftPrimary_" + C1: A,
                "GiftPrimary_" + C2: B,
                "GiftLimit_" + C2: "1",
            }
        )
        data["GiftSettings"]["attach"]["recipient_1"] = "unknown-character"
        with flow(config=data) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C2, B)])
            self.assertEqual(run.summary(), (1, 0, 0, 1, 0))

    def test_ui_dry_run_cannot_submit_even_with_all_list_choices_configured(self):
        data, _ = ui_config(
            {
                "GiftRecipient_1": C1,
                "GiftPrimary_" + C1: A,
                "GiftDryRun": "Yes",
            }
        )
        with flow(config=data) as run:
            run.finish()
            self.assertEqual(run.world.sends, [])
            self.assertEqual(run.runtime.session.attempts, 0)
            self.assertEqual(run.summary(), (0, 0, 1, 0, 0))

    def test_reset_discards_previous_receipt_quota_and_configuration(self):
        with flow(config=make_config([(C1, (A,), 3)])) as run:
            run.finish()
            previous = run.runtime
            self.assertEqual(len(run.world.sends), 3)
            run.context.config = make_config([(C1, (B,), 3)])
            run.world.remaining[C1] = 2
            run.next_node = "GiftEntry"
            run.finish()
            self.assertIsNot(run.runtime, previous)
            self.assertEqual(run.world.sends, [(C1, A)] * 3 + [(C1, B)] * 2)
            self.assertEqual(run.states()[C1].confirmed, 2)
            self.assertEqual(run.runtime.session.attempts, 2)
            self.assertIsNone(run.runtime.session.pending)

    def test_capture_failure_or_wrong_dimensions_never_clicks(self):
        for fault in ("job", "shape"):
            with self.subTest(fault=fault), flow() as run:
                run.until("GiftChooseCharacter")
                if fault == "job":
                    run.context.tasker.controller.capture_succeeds = False
                else:
                    run.world.image = np.zeros((360, 640, 3), dtype=np.uint8)
                run.finish()
                self.assertEqual(run.world.inputs, [])
                self.assertEqual(run.world.sends, [])
                self.assertIn("GiftChooseCharacter", run.failed)


if __name__ == "__main__":
    unittest.main()
