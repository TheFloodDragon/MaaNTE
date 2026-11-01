"""赠礼动作离线回归：真实 catalog/state/actions，合成界面不代表实机 OCR 验收。

虚拟界面按实机结构建模：赠礼页签、侧栏 5 个头像槽、5×2 礼物格、名称框、
「当前选择」、「赠送n/3」按钮和滚轮。只有生产动作的实际输入才会改变界面。
"""

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
    affection: str | None = None
    limit_prompt: str | None = None
    send_box: tuple | None = None
    controls: dict = field(default_factory=dict)
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
C1, C2, C3, C4, C5, C6, C7, C8 = CHARACTERS[:8]
ICON_GIFTS = tuple(key for key, record in CATALOG.gifts.items() if record["image"])
# A/B/C 有 Wiki 图标；D 无图标（只能逐格点选核对）；U 为不占次数礼物（无图标）。
A, B, C = ICON_GIFTS[:3]
D = next(
    key
    for key, record in CATALOG.gifts.items()
    if record["image"] is None and not record["unlimited"]
)
U = next(
    key
    for key, record in CATALOG.gifts.items()
    if record["unlimited"] and record["exclusive_character"] is None
)
X, X_OWNER = next(
    (key, record["exclusive_character"])
    for key, record in CATALOG.gifts.items()
    if record["exclusive_character"] is not None
)

# 虚拟界面几何；只在本测试内使用，生产坐标由视觉层提供。
SLOTS = tuple((1180, 100 + i * 100, 60, 60) for i in range(5))
CELLS = tuple(
    (600 + column * 100, 300 + row * 100, 80, 60)
    for row in range(2)
    for column in range(5)
)
COLUMNS = 5
SIDEBAR_POINT = (1210, 330)
GRID_POINT = (790, 380)
TAB_BOX = (900, 60, 100, 40)
SEND_BOX = (760, 600, 160, 40)


def center(box):
    x, y, width, height = box
    return x + width // 2, y + height // 2


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
    """可观察的虚拟赠礼界面；生产动作的实际输入才会切换角色、礼物和滚动位置。"""

    def __init__(
        self, *, remaining=3, stock=None, items=None, sidebar=None, page="bond"
    ):
        self.clock = Clock()
        self.page = page
        self.sidebar = list(sidebar or (C1, C2, C3, C4, C5, C6))
        self.sidebar_offset = 0
        # 打开赠礼页时默认选中侧栏第一位。
        self.character_id = self.sidebar[0] if self.sidebar else None
        self.remaining = (
            dict(remaining)
            if isinstance(remaining, dict)
            else dict.fromkeys(CHARACTERS, remaining)
        )
        self.items = list(items or (A, B, C))
        self.stock = dict.fromkeys(self.items, 20)
        if stock is not None:
            self.stock.update(stock)
        self.grid_row = 0
        self.gift_id = None
        self.unlimited = {
            key for key in CATALOG.gifts if CATALOG.gifts[key]["unlimited"]
        }
        self.badged = set()
        self.icon_misses = set()
        self.icon_alias = {}
        self.unreadable_gifts = set()
        self.unreadable_slots = set()
        self.slot_alias = {}
        self.dead_cells = set()
        self.refusing = set()
        self.receipt = "immediate"
        self.receipt_delay = 6
        self.pending_receipt = None
        self.toast = None
        self.toast_until = 0
        self.prompt_until = 0
        self.global_limit = False
        self.view_overrides = {}
        self.scroll_moves = True
        self.auto_select_next = False
        self.grid_rows_per_step = 1
        self.tab_switches = True
        self.fail_inputs = set()
        self.raise_inputs = set()
        self.inputs = []
        self.sends = []
        self.captures = 0
        self.snapshots = {}
        self.navigation = 0
        self.cursor = None
        self.before_capture = None
        self.after_inspect = None
        self.before_send = None
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)

    def stocked(self):
        """格子只显示持有的礼物；送完的礼物从格子中消失，后面的前移。"""
        return [gift for gift in self.items if self.stock.get(gift, 0) > 0]

    def max_grid_row(self):
        rows = -(-len(self.stocked()) // COLUMNS)
        return max(0, rows - 2)

    def max_sidebar_offset(self):
        return max(0, len(self.sidebar) - len(SLOTS))

    def visible_cells(self):
        items = self.stocked()
        start = self.grid_row * COLUMNS
        return [
            items[start + i] if start + i < len(items) else None
            for i in range(len(CELLS))
        ]

    def visible_slots(self):
        start = self.sidebar_offset
        return [
            self.sidebar[start + i] if start + i < len(self.sidebar) else None
            for i in range(len(SLOTS))
        ]

    def view(self):
        if self.global_limit:
            return FrameView(page="global_limit", global_limit=True)
        known = self.page in {"bond", "gifts"}
        gifts = self.page == "gifts"
        character = (
            self.character_id
            if known and self.character_id not in self.unreadable_slots
            else None
        )
        gift = (
            self.gift_id
            if gifts and self.gift_id not in self.unreadable_gifts
            else None
        )
        view = FrameView(
            page=self.page,
            character_id=character,
            identity_verified=character is not None,
            gift_id=gift,
            gift_verified=gift is not None,
            selected=gift is not None,
            remaining=self.remaining.get(self.character_id) if gifts else None,
            total=3 if gifts else None,
            affection=self.toast if self.captures <= self.toast_until else None,
            limit_prompt="character" if self.captures <= self.prompt_until else None,
            send_box=SEND_BOX if gifts else None,
            controls={"gift_tab": TAB_BOX} if known else {},
        )
        return replace(view, **self.view_overrides)

    def screencap(self):
        self.captures += 1
        self.clock.now += 0.1
        if self.before_capture is not None:
            self.before_capture(self)
        if self.pending_receipt is not None:
            when, character_id, gift_id = self.pending_receipt
            if self.captures >= when:
                self.pending_receipt = None
                self._apply(character_id, gift_id)
        cells = self.visible_cells()
        slots = self.visible_slots()
        candidates = {}
        for index, gift in enumerate(cells):
            if gift is None:
                continue
            if CATALOG.gifts[gift]["image"] and gift not in self.icon_misses:
                candidates.setdefault(gift, []).append(CELLS[index])
            for target, alias in self.icon_alias.items():
                if alias == gift:
                    candidates.setdefault(target, []).append(CELLS[index])
        self.snapshots[self.captures] = SimpleNamespace(
            view=self.view(),
            slots=[
                None if item is None else ("slot", item, item == self.character_id)
                for item in slots
            ],
            cells=[
                (
                    None
                    if item is None
                    else ("cell", item, self.stock[item], item == self.gift_id)
                )
                for item in cells
            ],
            sidebar=tuple(slots),
            grid=tuple((item, self.stock.get(item)) for item in cells),
            candidates=candidates,
            badges=[
                item is not None and (item in self.unlimited or item in self.badged)
                for item in cells
            ],
        )
        marker = self.captures
        self.image[0, 0] = (marker & 255, (marker >> 8) & 255, (marker >> 16) & 255)
        return self.image.copy()

    def click(self, x, y):
        target = None
        if (x, y) == center(TAB_BOX) and self.page in {"bond", "gifts"}:
            kind = "tab"
        elif (x, y) == center(SEND_BOX) and self.page == "gifts":
            kind = "send"
        elif self.page == "gifts" and (x, y) in [center(box) for box in SLOTS]:
            kind = "slot"
            target = self.visible_slots()[[center(box) for box in SLOTS].index((x, y))]
        elif self.page == "gifts" and (x, y) in [center(box) for box in CELLS]:
            kind = "cell"
            index = [center(box) for box in CELLS].index((x, y))
            target = self.visible_cells()[index]
        else:
            raise AssertionError("未授权的虚拟界面点击: %r" % ((self.page, x, y),))
        if kind in {"slot", "cell"} and target is None:
            raise AssertionError("点击了空槽位/空格子: %r" % ((kind, x, y),))
        self.inputs.append((self.captures, kind, target))
        if kind == "send":
            if self.before_send is not None:
                self.before_send()
            self.sends.append((self.character_id, self.gift_id))
        if kind in self.raise_inputs:
            raise RuntimeError("模拟控制器异常")
        if kind in self.fail_inputs:
            return False
        if kind == "tab":
            if self.tab_switches:
                self.page = "gifts"
        elif kind == "slot":
            self.character_id = self.slot_alias.get(target, target)
        elif kind == "cell":
            if index not in self.dead_cells:
                self.gift_id = target
        else:
            self._send()
        return True

    def _send(self):
        character_id, gift_id = self.character_id, self.gift_id
        if character_id in self.refusing:
            self.prompt_until = self.captures + 3
        elif self.receipt == "popup":
            self.page = "unknown"
        elif self.receipt == "global":
            self.global_limit = True
        elif self.receipt == "jump":
            self.remaining[character_id] -= 2
        elif self.receipt == "delayed":
            self.pending_receipt = (
                self.captures + self.receipt_delay,
                character_id,
                gift_id,
            )
        elif self.receipt != "none":
            self._apply(character_id, gift_id, toast=self.receipt != "no_toast")

    def _apply(self, character_id, gift_id, *, toast=True):
        if gift_id not in self.unlimited:
            self.remaining[character_id] -= 1
        self.stock[gift_id] -= 1
        if toast:
            self.toast = character_id
            self.toast_until = self.captures + 4
        if self.stock[gift_id] <= 0:
            stocked = self.stocked()
            # 实机可能清空选择，也可能自动选中下一件礼物。
            self.gift_id = stocked[0] if self.auto_select_next and stocked else None
            self.grid_row = min(self.grid_row, self.max_grid_row())

    def touch_move(self, x, y):
        self.cursor = (x, y)
        return "move" not in self.fail_inputs

    def scroll(self, dx, dy):
        # MaaFramework 在 target 框内随机取点；只接受落在滚动点附近的光标。
        region = None
        for name, (x, y) in (("sidebar", SIDEBAR_POINT), ("grid", GRID_POINT)):
            if (
                self.cursor
                and abs(self.cursor[0] - x) <= 6
                and abs(self.cursor[1] - y) <= 6
            ):
                region = name
        if region is None or dx != 0 or dy == 0 or self.page != "gifts":
            raise AssertionError("未授权的滚轮输入: %r" % ((self.cursor, dx, dy),))
        down = dy < 0
        self.inputs.append(
            (self.captures, "scroll", (region, "down" if down else "up"))
        )
        if "scroll" in self.fail_inputs:
            return False
        if not self.scroll_moves:
            return True
        if region == "sidebar":
            step = 1 if down else -len(self.sidebar)
            self.sidebar_offset = min(
                self.max_sidebar_offset(), max(0, self.sidebar_offset + step)
            )
        else:
            step = self.grid_rows_per_step if down else -99
            self.grid_row = min(self.max_grid_row(), max(0, self.grid_row + step))
        return True

    def enter_bond(self):
        self.navigation += 1
        self.page = "bond"
        return True


class MarkerVision:
    """按截图左上角像素找回当时的虚拟界面快照，实现生产视觉 API 契约。"""

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

    def gift_candidates(self, image, gift_id):
        return list(self.snapshot(image).candidates.get(gift_id, []))

    def infinite_at(self, image, cell):
        return self.snapshot(image).badges[CELLS.index(tuple(cell))]

    @staticmethod
    def sidebar_slots():
        return list(SLOTS)

    @staticmethod
    def sidebar_scroll_point():
        return SIDEBAR_POINT

    def sidebar_signature(self, image):
        return self.snapshot(image).sidebar

    def slot_signature(self, image, index):
        return self.snapshot(image).slots[index]

    @staticmethod
    def grid_cells():
        return list(CELLS)

    @staticmethod
    def grid_columns():
        return COLUMNS

    @staticmethod
    def cell_index(box):
        return CELLS.index(tuple(box)) if tuple(box) in CELLS else None

    @staticmethod
    def grid_scroll_point():
        return GRID_POINT

    def grid_signature(self, image):
        return self.snapshot(image).grid

    def cell_signature(self, image, index):
        return self.snapshot(image).cells[index]

    @staticmethod
    def same(before, after):
        return before is not None and after is not None and before == after


@contextmanager
def patched_actions(world):
    messages = []
    with (
        patch.object(actions, "_runtime", None),
        patch.object(
            actions, "GiftVision", lambda context, catalog: MarkerVision(world)
        ),
        # 全库 PNG 校验较慢；已在模块加载时校验过一次，流程测试复用同一对象。
        patch.object(actions.Catalog, "load", classmethod(lambda cls: CATALOG)),
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


class UnitContext:
    def __init__(self, world, config):
        self.world = world
        self.tasker = SimpleNamespace(stopping=False, controller=UnitController(world))
        self.config = config
        self.routes = {}
        self.read_nodes = []
        self.actions = []

    def run_action(self, entry, box=(0, 0, 0, 0), reco_detail="", pipeline_override={}):
        """模拟 MaaFramework 的 Scroll 动作：先把光标移到 target 中心，再滚轮。"""
        if entry != "GiftScrollWheel" or PIPELINE[entry]["action"]["type"] != "Scroll":
            raise AssertionError("未授权的动作节点: %s" % entry)
        param = pipeline_override[entry]["action"]["param"]
        self.actions.append((entry, copy.deepcopy(param)))
        x, y, width, height = param["target"]
        moved = self.world.touch_move(x + width // 2, y + height // 2)
        success = moved and self.world.scroll(0, param["dy"])
        return SimpleNamespace(success=bool(success))

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
        self.task_failed = False

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
            if self.world.page not in {"bond", "gifts"}:
                self.world.enter_bond()
            self.next_node = "GiftBondReady"
        elif node == "GiftBondReady":
            self.next_node = "GiftOpenTab"
        elif node == "GiftFailed":
            # fail_task：MaaFramework 把该节点记为失败并结束任务。
            self.task_failed = True
            self.next_node = None
        else:
            result = self.invoke(node)
            if result.success:
                nodes = self.context.routes.get(node, PIPELINE[node].get("next", []))
            else:
                self.failed.append(node)
                nodes = PIPELINE[node].get("on_error", [])
                if not nodes:
                    self.task_failed = True
            self.next_node = nodes[0] if nodes else None
        return self.next_node

    def until(self, node):
        for _ in range(6000):
            if self.next_node == node:
                return
            if self.next_node is None:
                raise AssertionError(
                    "流程未到达 %s；trace=%s" % (node, self.trace[-12:])
                )
            self.step()
        raise AssertionError("流程未有限结束: %s" % self.trace[-12:])

    def finish(self):
        self.until(None)
        return self

    def states(self):
        return {state.character_id: state for state in self.runtime.session.states}

    def summary(self):
        return [item[1:] for item in self.messages if item[0] == "gift.summary"][-1]

    def kinds(self, kind):
        return [target for _, item, target in self.world.inputs if item == kind]


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
            self.assertEqual(len(run.runtime.catalog.gifts), 256)
            self.assertEqual(len(run.runtime.catalog.characters), 24)
            self.assertEqual(run.runtime.session.current.recipient.gifts, (A, B))
            self.assertIn("GiftConfig_" + C1, run.context.read_nodes)

    def test_bond_page_opens_gift_tab_then_sends_to_preselected_character(self):
        with flow() as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, A)] * 3)
            self.assertEqual(run.kinds("tab"), [None])
            self.assertEqual(run.kinds("slot"), [])
            self.assertEqual(run.states()[C1].confirmed, 3)
            self.assertEqual(run.summary(), (3, 0, 0, 0, 0))
            self.assertFalse(run.task_failed)
            self.assertEqual(run.failed, [])


class SidebarTests(unittest.TestCase):
    def test_gift_tab_that_never_opens_is_not_replayed_and_fails_task(self):
        world = GiftWorld()
        world.tab_switches = False
        with flow(world) as run:
            run.finish()
            self.assertEqual(run.kinds("tab"), [None])
            self.assertEqual(world.sends, [])
            self.assertIn("GiftWaitGiftPage", run.failed)
            self.assertTrue(run.task_failed)

    def test_leaving_gift_tab_mid_run_reopens_it_at_most_once_per_character(self):
        world = GiftWorld()

        def drop_to_bond(item):
            # 每次截图都把画面打回羁遇首页：页签点击始终“生效后又退出”。
            if item.page == "gifts" and any(
                kind == "tab" for _, kind, _ in item.inputs
            ):
                item.page = "bond"

        world.before_capture = drop_to_bond
        with flow(world, make_config([(C1, (A,), 1), (C2, (A,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertLessEqual(len(run.kinds("tab")), 3)
            self.assertLess(len(run.trace), 400)
            self.assertTrue(run.task_failed)

    def test_flickering_page_classification_is_bounded_without_extra_input(self):
        world = GiftWorld(page="gifts")

        def flicker(item):
            # 识别在 bond/gifts 之间来回跳：不能在两个节点间无限互跳。
            if item.captures > 1:
                item.page = "bond" if item.page == "gifts" else "gifts"

        world.before_capture = flicker
        with flow(world, make_config([(C3, (A,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertLess(len(run.trace), 400)
            self.assertTrue(run.task_failed)

    def test_clicks_sidebar_slots_until_name_box_shows_recipient(self):
        with flow(config=make_config([(C3, (A,), 1)])) as run:
            run.finish()
            self.assertEqual(run.kinds("slot"), [C1, C2, C3])
            self.assertEqual(run.kinds("scroll"), [("sidebar", "up")])
            self.assertEqual(run.world.sends, [(C3, A)])
            self.assertFalse(run.task_failed)

    def test_scrolls_sidebar_to_reach_recipient_below_first_screen(self):
        world = GiftWorld(sidebar=(C1, C2, C3, C4, C5, C6, C7, C8))
        with flow(world, make_config([(C7, (A,), 1)])) as run:
            run.finish()
            self.assertEqual(run.kinds("slot")[-1], C7)
            self.assertIn(("sidebar", "down"), run.kinds("scroll"))
            self.assertEqual(world.sends, [(C7, A)])
            self.assertFalse(run.task_failed)

    def test_recipient_missing_from_sidebar_warns_after_bottom_and_fails(self):
        world = GiftWorld(sidebar=(C1, C2, C3, C4, C5, C6, C8))
        with flow(world, make_config([(C7, (A,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertEqual(world.sidebar_offset, world.max_sidebar_offset())
            self.assertEqual(run.states()[C7].reason, "character_not_found")
            self.assertTrue(any(m[0] == "gift.warning" for m in run.messages))
            self.assertTrue(run.task_failed)

    def test_unreadable_slot_is_remembered_and_not_clicked_again(self):
        world = GiftWorld(sidebar=(C1, "npc", C3, C4))
        world.unreadable_slots.add("npc")
        config = make_config([(C3, (A,), 1), (C4, (A,), 1)])
        with flow(world, config) as run:
            run.finish()
            self.assertEqual(run.kinds("slot").count("npc"), 1)
            self.assertEqual(world.sends, [(C3, A), (C4, A)])
            self.assertFalse(run.task_failed)

    def test_slot_that_opens_another_character_never_sends_to_wrong_one(self):
        world = GiftWorld(sidebar=(C2, C1, C3))
        world.character_id = C2
        world.slot_alias[C1] = C3
        with flow(world, make_config([(C1, (A,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertEqual(run.states()[C1].status, "skipped")
            self.assertTrue(run.task_failed)


class GiftSearchTests(unittest.TestCase):
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
                self.assertNotIn(B, run.kinds("cell"))
                self.assertEqual(run.summary()[0], expected)
                self.assertFalse(run.task_failed)

    def test_exhausted_first_gift_falls_back_only_after_full_check(self):
        with flow(GiftWorld(stock={A: 1, B: 5})) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, A), (C1, B), (C1, B)])
            self.assertEqual(run.states()[C1].confirmed, 3)
            self.assertTrue(any(m[0] == "gift.exhausted" for m in run.messages))
            self.assertFalse(run.task_failed)

    def test_selection_jumping_to_next_gift_after_last_item_is_still_a_receipt(self):
        world = GiftWorld(stock={A: 1, B: 5})
        world.auto_select_next = True
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, A), (C1, B), (C1, B)])
            self.assertEqual(run.states()[C1].confirmed, 3)
            self.assertFalse(run.task_failed)

    def test_preferred_gift_on_second_page_beats_visible_fallback(self):
        items = (B, C, *ICON_GIFTS[3:12], A)
        with flow(GiftWorld(items=items)) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, A)] * 3)
            self.assertIn(("grid", "down"), run.kinds("scroll"))
            self.assertNotIn(B, run.kinds("cell"))

    def test_absent_preferred_gift_checks_every_cell_before_fallback(self):
        items = (B, C, *ICON_GIFTS[3:13])
        with flow(GiftWorld(items=items)) as run:
            run.finish()
            self.assertEqual(run.world.sends, [(C1, B)] * 3)
            first_send = next(
                i for i, item in enumerate(run.world.inputs) if item[1] == "send"
            )
            checked = {
                target
                for _, kind, target in run.world.inputs[:first_send]
                if kind == "cell"
            }
            # 退到备选前，A 不在的结论必须来自逐格点选核对过的每一个礼物。
            self.assertEqual(checked, set(items))
            self.assertFalse(run.task_failed)

    def test_icon_miss_still_finds_gift_by_cell_by_cell_check(self):
        world = GiftWorld(items=(B, C, A))
        world.icon_misses.add(A)
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, A)] * 3)
            self.assertFalse(run.task_failed)

    def test_icon_false_positive_is_corrected_by_current_selection(self):
        world = GiftWorld(items=(B, C, A))
        world.icon_alias[A] = B
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, A)] * 3)
            self.assertNotIn((C1, B), world.sends)

    def test_gift_without_icon_is_found_by_reading_current_selection(self):
        world = GiftWorld(items=(A, B, D))
        with flow(world, make_config([(C1, (D,), 2)])) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, D)] * 2)
            self.assertIn(D, run.kinds("cell"))
            self.assertFalse(run.task_failed)


class AbsenceSafetyTests(unittest.TestCase):
    """证明“没有该礼物”之前不能退到备选礼物。"""

    def assert_no_fallback(self, run):
        self.assertEqual(run.world.sends, [])
        self.assertTrue(run.task_failed)
        self.assertTrue(any(m[0] == "gift.warning" for m in run.messages))

    def test_unreadable_cell_name_never_proves_absence(self):
        world = GiftWorld(items=(B, C))
        world.unreadable_gifts.add(C)
        with flow(world) as run:
            run.finish()
            self.assert_no_fallback(run)

    def test_scroll_that_skips_a_row_never_proves_absence(self):
        world = GiftWorld(items=(B, C, *ICON_GIFTS[3:22]))
        world.grid_rows_per_step = 2
        with flow(world) as run:
            run.finish()
            self.assert_no_fallback(run)

    def test_full_page_that_never_scrolls_is_not_treated_as_the_end(self):
        world = GiftWorld(items=(B, C, *ICON_GIFTS[3:11], A))
        world.scroll_moves = False
        with flow(world) as run:
            run.finish()
            self.assert_no_fallback(run)

    def test_partial_single_page_without_target_does_fall_back(self):
        world = GiftWorld(items=(B, C))
        world.scroll_moves = False
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, B)] * 3)
            self.assertFalse(run.task_failed)

    def test_click_that_leaves_previous_name_is_ambiguous_not_identity(self):
        world = GiftWorld(items=(B, C, D))
        world.icon_misses.add(A)
        world.dead_cells.add(2)
        with flow(world, make_config([(C1, (A, D), 1)])) as run:
            run.finish()
            self.assert_no_fallback(run)

    def test_dead_click_on_target_cell_never_sends(self):
        world = GiftWorld(items=(A, B, C))
        world.dead_cells.add(0)
        with flow(world) as run:
            run.finish()
            self.assert_no_fallback(run)


class SendAndReceiptTests(unittest.TestCase):
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
            self.assertFalse(run.task_failed)

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
            {"inventory": 0},
            {"inventory": -1},
            {"inventory": True},
            {"global_limit": True},
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
                self.assertIn("GiftSendOne", run.failed)
                self.assertTrue(run.task_failed)

    def test_infinite_badge_on_regular_gift_blocks_consumption(self):
        world = GiftWorld()
        world.badged.add(A)
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertEqual(run.states()[C1].reason, "infinite")
            self.assertTrue(run.task_failed)

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


class ReceiptFailureTests(unittest.TestCase):
    def test_missing_or_conflicting_receipt_locks_character_without_replay(self):
        for mode in ("none", "jump", "popup"):
            world = GiftWorld()
            world.receipt = mode
            with self.subTest(mode=mode), flow(world) as run:
                run.finish()
                self.assertEqual(world.sends, [(C1, A)])
                self.assertEqual(run.states()[C1].confirmed, 0)
                self.assertEqual(run.states()[C1].status, "uncertain")
                self.assertIsNone(run.runtime.session.pending)
                self.assertLess(world.captures, 300)
                self.assertTrue(run.task_failed)
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

    def test_refusal_prompt_skips_only_that_character_without_replay(self):
        world = GiftWorld()
        world.refusing.add(C1)
        config = make_config([(C1, (A,), 3), (C2, (A,), 1)])
        with flow(world, config) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, A), (C2, A)])
            self.assertEqual(run.states()[C1].reason, "character_limit")
            self.assertEqual(run.states()[C2].confirmed, 1)
            self.assertEqual(run.summary(), (1, 0, 0, 1, 0))
            self.assertTrue(run.task_failed)

    def test_global_limit_blocks_later_characters_without_extra_input(self):
        with flow(config=make_config([(C1, (A,), 3), (C2, (B,), 3)])) as run:
            run.until("GiftSendOne")
            count = len(run.world.inputs)
            run.world.global_limit = True
            run.finish()
            self.assertEqual(run.world.sends, [])
            self.assertEqual(len(run.world.inputs), count)
            self.assertEqual(run.summary(), (0, 0, 0, 2, 0))
            self.assertTrue(run.task_failed)

    def test_global_limit_after_send_is_not_counted_as_success(self):
        world = GiftWorld()
        world.receipt = "global"
        with flow(world, make_config([(C1, (A,), 3), (C2, (B,), 3)])) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, A)])
            self.assertEqual(run.states()[C1].confirmed, 0)
            self.assertEqual(run.summary()[0], 0)
            self.assertTrue(run.task_failed)


class UnlimitedGiftTests(unittest.TestCase):
    """不占次数礼物：计数不变，同帧好感提示点名当前角色才算送出。"""

    def test_unlimited_gift_confirms_by_affection_prompt_and_counts_to_target(self):
        world = GiftWorld(items=(A, B, U))
        with flow(world, make_config([(C1, (U,), 3)])) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, U)] * 3)
            self.assertEqual(world.remaining[C1], 3)
            self.assertEqual(run.states()[C1].confirmed, 3)
            self.assertEqual(run.states()[C1].reason, "target_reached")
            self.assertFalse(run.task_failed)

    def test_unlimited_gift_without_prompt_is_uncertain_and_never_resent(self):
        world = GiftWorld(items=(A, U))
        world.receipt = "no_toast"
        with flow(world, make_config([(C1, (U,), 3)])) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, U)])
            self.assertEqual(run.states()[C1].status, "uncertain")
            self.assertEqual(run.states()[C1].confirmed, 0)
            self.assertTrue(run.task_failed)

    def test_prompt_naming_another_character_is_not_a_receipt(self):
        world = GiftWorld(items=(A, U))
        world.view_overrides["affection"] = C2
        with flow(world, make_config([(C1, (U,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [(C1, U)])
            self.assertEqual(run.states()[C1].status, "uncertain")
            self.assertTrue(run.task_failed)

    def test_exclusive_gift_is_rejected_for_other_characters(self):
        other = next(character for character in CHARACTERS if character != X_OWNER)
        world = GiftWorld(items=(A, X), sidebar=(other, X_OWNER))
        with flow(world, make_config([(other, (X,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertEqual(run.summary(), (0, 0, 0, 1, 0))
            self.assertTrue(run.task_failed)
        world = GiftWorld(items=(A, X), sidebar=(X_OWNER, other))
        with flow(world, make_config([(X_OWNER, (X,), 1)])) as run:
            run.finish()
            self.assertEqual(world.sends, [(X_OWNER, X)])
            self.assertFalse(run.task_failed)


class BudgetAndStopTests(unittest.TestCase):
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
                    self.assertFalse(run.task_failed)
                else:
                    self.assertTrue(run.task_failed)

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


class StopAndRecoveryTests(unittest.TestCase):
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

    def test_unknown_page_before_selection_stops_input_and_warns_everyone(self):
        with flow(config=make_config([(C1, (A,), 3), (C2, (B,), 3)])) as run:
            run.until("GiftChooseCharacter")
            count = len(run.world.inputs)
            run.world.page = "unknown"
            run.finish()
            self.assertEqual(len(run.world.inputs), count)
            self.assertEqual(run.world.sends, [])
            self.assertEqual(
                len([m for m in run.messages if m[0] == "gift.warning"]), 2
            )
            self.assertEqual(run.summary(), (0, 0, 0, 2, 0))
            self.assertTrue(run.task_failed)

    def test_capture_failure_or_wrong_dimensions_never_clicks(self):
        for fault in ("job", "shape"):
            with self.subTest(fault=fault), flow() as run:
                run.until("GiftOpenTab")
                if fault == "job":
                    run.context.tasker.controller.capture_succeeds = False
                else:
                    run.world.image = np.zeros((360, 640, 3), dtype=np.uint8)
                run.finish()
                self.assertEqual(run.world.inputs, [])
                self.assertEqual(run.world.sends, [])
                self.assertIn("GiftOpenTab", run.failed)
                self.assertTrue(run.task_failed)

    def test_scroll_input_failure_never_turns_into_absence(self):
        world = GiftWorld(items=(B, C))
        world.fail_inputs.add("scroll")
        with flow(world) as run:
            run.finish()
            self.assertEqual(world.sends, [])
            self.assertIn("GiftScroll", run.failed)
            self.assertTrue(run.task_failed)


class ConfigurationTests(unittest.TestCase):
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
                self.assertTrue(run.task_failed)

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
                # 没选任何角色只是提示；选了但配置无效则任务失败。
                self.assertEqual(run.task_failed, mode != "empty")

    def test_invalid_dry_run_does_not_default_to_sending(self):
        for value in (None, "false", 0, 1):
            with self.subTest(value=value), flow(
                config=make_config(dry_run=value)
            ) as run:
                run.finish()
                self.assertEqual(run.world.inputs, [])
                self.assertEqual(run.world.sends, [])
                self.assertIn("GiftEntry", run.failed)
                self.assertTrue(run.task_failed)

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


class UiConfigurationTests(unittest.TestCase):
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


class UiRowTests(unittest.TestCase):
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
            # 别的角色的专属礼物（UI 不会列出，但手动/旧配置可能带入）。
            {
                "primary_gift": next(
                    key
                    for key, record in CATALOG.gifts.items()
                    if record["exclusive_character"] not in (None, C1)
                )
            },
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
            self.assertFalse(run.task_failed)


if __name__ == "__main__":
    unittest.main()
