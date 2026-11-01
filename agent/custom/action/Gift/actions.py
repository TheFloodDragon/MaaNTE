"""Pipeline 驱动的逐件赠礼动作；每次调用最多一次输入，等待节点只观察。

按游戏真实界面工作：在赠礼页签内依次点侧栏头像、读名称框确认角色；侧栏用
滚轮翻页，滚动后画面不变即到底。礼物按优先级查找：有图标先做图标匹配，否则
逐格点选并读「当前选择」；礼物格同样滚动到底才能证明缺货。每次点「赠送」只送
1 件，由回执确认：常规礼物计数恰好减 1，不占次数礼物计数不变且出现好感提示。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils.i18n import T
from utils.logger import logger
from utils.maafocus import PrintT

from .catalog import Catalog, CatalogError
from .state import Evidence, GiftSafetyError, GiftSession, Recipient
from .vision import FrameView, GiftVision

# 静态 next 为安全默认值；这里只允许在已审查的节点集合之间动态分派。
_ROUTES = {
    "GiftEntry": {"GiftEnsureBond", "GiftSummary"},
    "GiftOpenTab": {"GiftWaitGiftPage", "GiftChooseCharacter"},
    "GiftWaitGiftPage": {"GiftWaitGiftPage", "GiftChooseCharacter"},
    "GiftChooseCharacter": {
        "GiftChooseCharacter",
        "GiftWaitCharacter",
        "GiftScroll",
        "GiftCheckQuota",
        "GiftOpenTab",
        "GiftNextCharacter",
    },
    "GiftWaitCharacter": {
        "GiftWaitCharacter",
        "GiftChooseCharacter",
        "GiftCheckQuota",
        "GiftNextCharacter",
    },
    "GiftScroll": {"GiftWaitScroll"},
    "GiftWaitScroll": {
        "GiftWaitScroll",
        "GiftChooseCharacter",
        "GiftFindGift",
        "GiftNextCharacter",
    },
    "GiftCheckQuota": {"GiftFindGift", "GiftNextCharacter"},
    "GiftFindGift": {
        "GiftFindGift",
        "GiftVerifyGift",
        "GiftScroll",
        "GiftNextCharacter",
    },
    "GiftVerifyGift": {"GiftVerifyGift", "GiftFindGift", "GiftSendOne"},
    "GiftSendOne": {"GiftWaitReceipt", "GiftNextCharacter"},
    "GiftWaitReceipt": {
        "GiftWaitReceipt",
        "GiftCheckQuota",
        "GiftNextCharacter",
        "GiftWarnCurrent",
    },
    "GiftReceiptFailed": {"GiftWarnCurrent"},
    "GiftWarnCurrent": {"GiftRecoverCurrent", "GiftNextCharacter", "GiftSummary"},
    "GiftRecoverCurrent": {"GiftRecoverCurrent", "GiftNextCharacter", "GiftSummary"},
    "GiftNextCharacter": {"GiftChooseCharacter", "GiftNextCharacter", "GiftSummary"},
    "GiftNavigationFailed": {"GiftSummary"},
    "GiftSummary": {None, "GiftFailed"},
}

_REASON_CODES = {
    "invalid_config",
    "no_recipients",
    "unknown_character",
    "character_not_found",
    "character_limit",
    "identity",
    "gift_not_found",
    "gift_identity",
    "inventory",
    "quota",
    "quantity",
    "infinite",
    "global_limit",
    "budget",
    "page",
    "navigation",
    "recovery",
    "ambiguous",
    "scan",
    "receipt",
    "receipt_timeout",
    "controller",
    "stopping",
    "dry_run",
    "exhausted",
    "catalog",
    "internal",
}

_STATE_REASONS = {
    "invalid_inventory": "inventory",
    "invalid_quantity": "quantity",
    "invalid_remaining": "quota",
    "invalid_total": "quota",
    "counter_changed": "quota",
    "infinite_quantity": "infinite",
    "send_budget": "budget",
    "stopped": "stopping",
    "identity_changed": "identity",
    "counter_jump": "receipt",
    "inventory_conflict": "receipt",
    "invalid_affection": "receipt",
    "receipt_timeout": "receipt_timeout",
    "confirmation_timeout": "receipt_timeout",
    "gifts_exhausted": "exhausted",
    "wrong_character": "identity",
    "wrong_gift": "gift_identity",
    "unverified_page_verified": "page",
    "unverified_identity_verified": "identity",
    "unverified_gift_verified": "gift_identity",
    "unverified_selected": "gift_identity",
}

# 等待参数：点击/滚动后至少等 SETTLE 秒且连续两帧一致才采信画面。
SETTLE = 0.8
PAGE_WAIT = 8.0
CHARACTER_WAIT = 6.0
SELECTION_WAIT = 5.0
RECEIPT_WAIT = 10.0
SCROLL_WAIT = 5.0
MAX_WAIT_FRAMES = 120
POLL = 0.1
# 翻页上限：超出即保守报错，绝不把“没翻完”当成“没有”。
MAX_SIDEBAR_PAGES = 12
MAX_GRID_PAGES = 30
MAX_TOP_SCROLLS = 6
# 点击上限：防止签名误配导致在同几个格子/头像之间来回点。
MAX_SLOT_CLICKS = MAX_SIDEBAR_PAGES * 5 + 5
MAX_CELL_CLICKS = MAX_GRID_PAGES * 10 + 5
MAX_TAB_REOPENS = 2
# 滚轮步长（MaaFramework 约定：正值向上、负值向下，120 为一格）；待实机校准。
SIDEBAR_STEP = -360
GRID_STEP = -240
TOP_STEP = 1200


class _FlowError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class _Search:
    """当前收礼角色在侧栏中的查找进度；每位角色都从侧栏顶部重新查找。"""

    top: bool = False
    top_scrolls: int = 0
    queue: list[int] = field(default_factory=list)
    filled: bool = False
    pages: int = 0
    clicks: int = 0
    # 选角色时发现不在赠礼页、回到页签检查的次数；防止识别抖动导致两节点无限互跳。
    reopens: int = 0


@dataclass
class _Scan:
    """当前候选礼物在礼物格中的遍历进度；赠送或换候选后重建。"""

    gift_id: str
    # 第一遍只按图标逐页查找（有图标的礼物）；到底仍没找到再逐格点选核对。
    sweep: bool = False
    top: bool = False
    top_scrolls: int = 0
    fast_tried: bool = False
    filled: bool = False
    queue: list[int] = field(default_factory=list)
    signatures: dict[int, Any] = field(default_factory=dict)
    # 格子序号 -> 点选后读到的礼物 ID（None＝读不出名称，不能证明缺货）。
    known: dict[int, str | None] = field(default_factory=dict)
    previous: dict[int, tuple[Any, str | None]] = field(default_factory=dict)
    unknown: bool = False
    pages: int = 0
    clicks: int = 0


@dataclass
class _Runtime:
    catalog: Catalog | None = None
    session: GiftSession = field(default_factory=lambda: GiftSession(()))
    vision: GiftVision | None = None
    frame_id: int = 0
    reason: str = "internal"
    invalid: list[tuple[str, str]] = field(default_factory=list)
    notified: set[str] = field(default_factory=set)
    wait_kind: str = ""
    deadline: float = 0.0
    wait_frames: int = 0
    armed_at: float = 0.0
    settle_value: Any = None
    settle_count: int = 0
    # 侧栏头像签名 -> 点选后确认的角色 ID（None＝不在目录中）；整轮复用。
    known_slots: list[tuple[Any, str | None]] = field(default_factory=list)
    search: _Search | None = None
    scan: _Scan | None = None
    scroll_kind: str = ""
    scroll_direction: str = ""
    scroll_before: Any = None
    pending_slot: int | None = None
    pending_signature: Any = None
    pending_cell: int | None = None
    previous_character: str | None = None
    previous_gift: str | None = None
    selected_cell: int | None = None
    hint_cell: int | None = None
    announced_gift: str | None = None
    announced_character: str | None = None
    blocked: bool = False
    task_failed: bool = False
    summary_emitted: bool = False


_runtime: _Runtime | None = None


def _rt() -> _Runtime:
    if _runtime is None:
        raise _FlowError("internal")
    return _runtime


def _stop(context: Context) -> None:
    if context.tasker.stopping:
        if _runtime is not None:
            _runtime.session.cancel()
        raise _FlowError("stopping")


def _name(character_id: str | None) -> str:
    runtime = _rt()
    record = runtime.catalog.characters.get(character_id, {}) if runtime.catalog else {}
    return record.get("names", {}).get("zh_cn", character_id or "—")


def _gift_name(gift_id: str | None) -> str:
    runtime = _rt()
    record = runtime.catalog.gifts.get(gift_id, {}) if runtime.catalog else {}
    return record.get("names", {}).get("zh_cn", gift_id or "—")


def _reason(code: str) -> str:
    code = _STATE_REASONS.get(code, code)
    if (
        code.startswith("scan_")
        or code.startswith("invalid_scan_")
        or code == "stale_scan"
    ):
        return "scan"
    return code if code in _REASON_CODES else "internal"


def _warn(context: Context, character_id: str | None, code: str) -> None:
    runtime = _rt()
    key = character_id or "task"
    if key in runtime.notified:
        return
    runtime.notified.add(key)
    logger.warning("赠礼警告 | character=%s reason=%s", character_id, code)
    PrintT(
        context, "gift.warning", _name(character_id), T("gift.reason." + _reason(code))
    )


def _capture(context: Context) -> tuple[np.ndarray, FrameView]:
    _stop(context)
    runtime = _rt()
    controller = context.tasker.controller
    if controller is None or not controller.post_screencap().wait().succeeded:
        raise _FlowError("controller")
    _stop(context)
    frame = controller.cached_image
    if not isinstance(frame, np.ndarray) or frame.shape[:2] != (720, 1280):
        # 不将未归一化截图坐标发送给720p控制器，也不静默猜测缩放比例。
        raise _FlowError("controller")
    runtime.frame_id += 1
    view = runtime.vision.inspect(frame)
    logger.debug(
        "赠礼识别 | frame=%d page=%s character=%s gift=%s remaining=%s/%s "
        "limit=%s affection=%s errors=%s",
        runtime.frame_id,
        view.page,
        view.character_id,
        view.gift_id,
        view.remaining,
        view.total,
        view.limit_prompt,
        view.affection,
        view.errors,
    )
    return frame, view


def _evidence(view: FrameView, *, infinite: bool | None = None) -> Evidence:
    runtime = _rt()
    current = runtime.session.current
    gift = runtime.catalog.gifts.get(current.gift_id) if current else None
    return Evidence(
        frame_id=runtime.frame_id,
        character_id=view.character_id,
        gift_id=view.gift_id,
        remaining=view.remaining,
        total=view.total,
        inventory=view.inventory,
        quantity=view.quantity,
        page_verified=view.page == "gifts",
        identity_verified=view.identity_verified,
        gift_verified=view.gift_verified,
        selected=view.selected,
        infinite=view.infinite if infinite is None else infinite,
        global_limit=view.global_limit,
        unlimited=bool(gift and gift.get("unlimited")),
        # 好感提示必须点名当前角色，不接受上一位角色残留的提示。
        affection=bool(current) and view.affection == current.character_id,
    )


def _page(view: FrameView, *, identity: bool = True) -> None:
    runtime = _rt()
    if view.global_limit:
        runtime.session.block_remaining("global_limit")
        runtime.blocked = True
        raise _FlowError("global_limit")
    if view.page not in {"bond", "gifts"}:
        raise _FlowError("page")
    if identity and (
        not view.identity_verified
        or runtime.session.current is None
        or view.character_id != runtime.session.current.character_id
    ):
        raise _FlowError("identity")


def _box_center(box: Any) -> tuple[int, int]:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise _FlowError("page")
    if any(type(value) is not int for value in box):
        raise _FlowError("page")
    x, y, width, height = box
    if (
        x < 0
        or y < 0
        or width < 1
        or height < 1
        or x + width > 1280
        or y + height > 720
    ):
        raise _FlowError("page")
    return x + width // 2, y + height // 2


def _click(context: Context, box: Any, *, sending: bool = False, intent=None) -> None:
    _stop(context)
    runtime = _rt()
    x, y = _box_center(box)
    if sending:
        if intent is None or not runtime.session.is_pending(intent):
            raise _FlowError("receipt")
    else:
        if runtime.session.pending is not None:
            raise _FlowError("receipt")
        runtime.session.invalidate("navigation", frame_id=runtime.frame_id)
    # pending 在进入控制器前已建立；失败/异常也不能回放。
    _stop(context)
    if not context.tasker.controller.post_click(x, y).wait().succeeded:
        raise _FlowError("controller")
    _stop(context)


def _scroll(context: Context, point: tuple[int, int], dy: int) -> None:
    """在滚动区域滚轮一次。

    直接 post_scroll 会先把光标移回 (0,0)；管线 Scroll 动作带 target 时才先把
    光标移到目标再滚动，所以经由 GiftScrollWheel 节点执行。
    """
    _stop(context)
    runtime = _rt()
    if runtime.session.pending is not None:
        raise _FlowError("receipt")
    runtime.session.invalidate("scroll", frame_id=runtime.frame_id)
    runtime.selected_cell = None
    x, y = point
    override = {
        "GiftScrollWheel": {
            "action": {"param": {"target": [x - 4, y - 4, 8, 8], "dy": dy}}
        }
    }
    _stop(context)
    detail = context.run_action("GiftScrollWheel", pipeline_override=override)
    if detail is None or not detail.success:
        raise _FlowError("controller")
    _stop(context)


def _arm(kind: str, seconds: float) -> None:
    runtime = _rt()
    runtime.wait_kind = kind
    runtime.armed_at = time.monotonic()
    runtime.deadline = runtime.armed_at + seconds
    runtime.wait_frames = 0
    runtime.settle_value = None
    runtime.settle_count = 0


def _waiting(kind: str, error: str) -> None:
    runtime = _rt()
    runtime.wait_frames += 1
    if (
        runtime.wait_kind != kind
        or time.monotonic() >= runtime.deadline
        or runtime.wait_frames > MAX_WAIT_FRAMES
    ):
        raise _FlowError(error)


def _settled(value: Any, same, seconds: float = SETTLE) -> bool:
    """至少等待 seconds 秒，且连续两帧观测一致，才认为界面已稳定。"""
    runtime = _rt()
    if runtime.settle_count and same(runtime.settle_value, value):
        runtime.settle_count += 1
    else:
        runtime.settle_value = value
        runtime.settle_count = 1
    return runtime.settle_count >= 2 and time.monotonic() - runtime.armed_at >= seconds


def _pause(context: Context) -> None:
    """等待节点之间短暂休眠，避免空转截图；替身时钟没有 sleep 时直接返回。"""
    _stop(context)
    sleep = getattr(time, "sleep", None)
    if sleep is not None:
        sleep(POLL)


def _equal(left: Any, right: Any) -> bool:
    return left == right


def _gift_priority(catalog: Catalog, config: dict, character_id: str) -> tuple:
    mode = config.get("gift_mode", "manual")
    if mode == "manual":
        gifts = catalog.parse_priority(config.get("gifts"))
    elif mode == "choices":
        primary = config.get("primary_gift")
        backup = config.get("backup_gift", "")
        extra = config.get("extra_gifts", "")
        if (
            not isinstance(primary, str)
            or not primary.strip()
            or not isinstance(backup, str)
            or not isinstance(extra, str)
        ):
            raise _FlowError("invalid_config")
        parts = [primary]
        if backup.strip():
            parts.append(backup)
        if extra.strip():
            parts.append(extra)
        # UI 下拉与自由输入共用严格目录解析；跨字段重复项保序去重。
        gifts = catalog.parse_priority(" > ".join(parts))
    else:
        raise _FlowError("invalid_config")
    # 专属礼物只能送给对应角色；手动输入了别人的专属礼物视为配置无效。
    if any(not catalog.gift_allowed(gift, character_id) for gift in gifts):
        raise _FlowError("invalid_config")
    return gifts


def _recipient_order(context: Context, catalog: Catalog, settings: dict):
    mode = settings.get("recipient_mode", "legacy")
    if mode == "legacy":
        return list(catalog.characters), []
    if mode != "rows":
        raise _FlowError("invalid_config")
    count = settings.get("plan_count")
    maximum = sum(record["giftable"] for record in catalog.characters.values())
    if type(count) is not int or not 1 <= count <= maximum:
        raise _FlowError("invalid_config")
    order, invalid = [], []
    for index in range(1, count + 1):
        _stop(context)
        # attach 的字典合并仅保留平级字段，不能将各行放进会被替换的嵌套对象。
        character_id = settings.get("recipient_%d" % index)
        if character_id == "":
            continue
        if not isinstance(character_id, str):
            invalid.append(("plan_%d" % index, "invalid_config"))
        elif (
            character_id not in catalog.characters
            or not catalog.characters[character_id]["giftable"]
        ):
            invalid.append((character_id, "unknown_character"))
        elif character_id not in order:
            order.append(character_id)
        else:
            # 同一角色的多个编辑入口共用配置，仅按首次出现的位置执行一次。
            logger.debug(
                "赠礼方案重复角色已去重 | row=%d character=%s", index, character_id
            )
    return order, invalid


def _config(
    context: Context, catalog: Catalog
) -> tuple[list[Recipient], list[tuple[str, str]], bool]:
    node = context.get_node_data("GiftSettings") or {}
    settings = node.get("attach", {})
    if not isinstance(settings, dict) or type(settings.get("dry_run")) is not bool:
        raise _FlowError("invalid_config")
    dry_run = settings["dry_run"]
    order, invalid = _recipient_order(context, catalog, settings)
    rows = settings.get("recipient_mode", "legacy") == "rows"
    recipients = []
    for character_id in order:
        _stop(context)
        record = catalog.characters[character_id]
        node = context.get_node_data("GiftConfig_" + character_id) or {}
        config = node.get("attach", {})
        if not rows and (
            not config or isinstance(config, dict) and config.get("selected") is False
        ):
            continue
        if not isinstance(config, dict) or config.get("selected") is not True:
            invalid.append((character_id, "invalid_config"))
            continue
        try:
            if not record.get("giftable") or config.get("character_id") != character_id:
                raise _FlowError("unknown_character")
            gifts = _gift_priority(catalog, config, character_id)
            recipients.append(Recipient(character_id, gifts, config.get("limit")))
        except (CatalogError, GiftSafetyError, _FlowError, TypeError) as exc:
            logger.warning("赠礼配置无效 | character=%s error=%s", character_id, exc)
            invalid.append((character_id, "invalid_config"))
    return recipients, invalid, dry_run


class _Action(CustomAction):
    def execute(self, context: Context) -> str | None:
        raise NotImplementedError

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        try:
            _stop(context)
            node = self.execute(context)
            if node not in _ROUTES.get(argv.node_name, set()):
                raise _FlowError("internal")
            if not context.override_next(
                argv.node_name, [] if node is None else [node]
            ):
                raise _FlowError("internal")
            return CustomAction.RunResult(success=True)
        except (GiftSafetyError, _FlowError, CatalogError) as exc:
            code = getattr(exc, "code", "catalog")
            if _runtime is not None:
                _runtime.reason = _reason(code)
            logger.warning("赠礼步骤拒绝 | node=%s reason=%s", argv.node_name, code)
        except Exception as exc:
            if _runtime is not None:
                _runtime.reason = "internal"
            logger.exception("赠礼步骤异常 | node=%s error=%s", argv.node_name, exc)
        return CustomAction.RunResult(success=False)


@AgentServer.custom_action("gift_reset")
class GiftReset(_Action):
    def execute(self, context):
        global _runtime
        _runtime = _Runtime()
        catalog = Catalog.load()
        _runtime.catalog = catalog
        recipients, _runtime.invalid, dry_run = _config(context, catalog)
        _runtime.session = GiftSession(recipients, dry_run=dry_run, send_budget=10)
        _runtime.vision = GiftVision(context, catalog)
        PrintT(
            context,
            "gift.started",
            len(recipients) + len(_runtime.invalid),
            T("gift.mode.dry_run" if dry_run else "gift.mode.execute"),
        )
        for character_id, reason in _runtime.invalid:
            _warn(context, character_id, reason)
        if not recipients:
            if not _runtime.invalid:
                _warn(context, None, "no_recipients")
            return "GiftSummary"
        return "GiftEnsureBond"


@AgentServer.custom_action("gift_open_tab")
class GiftOpenTab(_Action):
    """进入羁遇后切到「赠礼」页签；之后在页签内切换角色，不再回到羁遇首页。"""

    def execute(self, context):
        _, view = _capture(context)
        _page(view, identity=False)
        if view.page == "gifts":
            return "GiftChooseCharacter"
        if not view.controls.get("gift_tab"):
            raise _FlowError("navigation")
        _click(context, view.controls["gift_tab"])
        _arm("gift_page", PAGE_WAIT)
        return "GiftWaitGiftPage"


@AgentServer.custom_action("gift_wait_gift_page")
class GiftWaitGiftPage(_Action):
    def execute(self, context):
        _, view = _capture(context)
        if view.global_limit:
            _page(view)
        if view.page == "gifts":
            return "GiftChooseCharacter"
        _waiting("gift_page", "navigation")
        _pause(context)
        return "GiftWaitGiftPage"


def _known_slot(signature: Any) -> tuple[bool, str | None]:
    """头像签名是否已对应过某个角色（None＝点过但不在目录中）。"""
    runtime = _rt()
    for known, character_id in runtime.known_slots:
        if runtime.vision.same(known, signature):
            return True, character_id
    return False, None


def _request_scroll(kind: str, direction: str) -> str:
    runtime = _rt()
    runtime.scroll_kind = kind
    runtime.scroll_direction = direction
    return "GiftScroll"


def _click_slot(context: Context, index: int, signature: Any, view: FrameView) -> str:
    runtime = _rt()
    if runtime.search is None:
        runtime.search = _Search()
    runtime.search.clicks += 1
    if runtime.search.clicks > MAX_SLOT_CLICKS:
        raise _FlowError("character_not_found")
    runtime.pending_slot = index
    runtime.pending_signature = signature
    runtime.previous_character = view.character_id
    _click(context, runtime.vision.sidebar_slots()[index])
    _arm("character", CHARACTER_WAIT)
    return "GiftWaitCharacter"


@AgentServer.custom_action("gift_choose_character")
class GiftChooseCharacter(_Action):
    """在赠礼页签内依次点侧栏头像，直到名称框显示目标角色。"""

    def execute(self, context):
        runtime = _rt()
        current = runtime.session.current
        if current is None or current.status != "ready":
            return "GiftNextCharacter"
        frame, view = _capture(context)
        _page(view, identity=False)
        search = runtime.search
        if search is None:
            search = runtime.search = _Search()
        if view.page != "gifts":
            search.reopens += 1
            if search.reopens > MAX_TAB_REOPENS:
                raise _FlowError("page")
            return "GiftOpenTab"
        if view.identity_verified and view.character_id == current.character_id:
            runtime.search = None
            return "GiftCheckQuota"
        vision = runtime.vision
        slots = range(len(vision.sidebar_slots()))
        signatures = {index: vision.slot_signature(frame, index) for index in slots}
        # 已在本轮见过目标头像且它就在当前页：直接点，免去从顶部重新查找。
        for index, signature in signatures.items():
            if signature is not None and _known_slot(signature) == (
                True,
                current.character_id,
            ):
                return _click_slot(context, index, signature, view)
        if not search.top:
            return _request_scroll("sidebar", "up")
        if not search.filled:
            search.queue = list(slots)
            search.filled = True
        while search.queue:
            index = search.queue.pop(0)
            signature = signatures[index]
            if signature is None:
                continue  # 空槽位
            known, _ = _known_slot(signature)
            if known:
                continue  # 已确认是其他角色（或不在目录中的角色）
            return _click_slot(context, index, signature, view)
        if search.pages >= MAX_SIDEBAR_PAGES:
            raise _FlowError("character_not_found")
        return _request_scroll("sidebar", "down")


@AgentServer.custom_action("gift_wait_character")
class GiftWaitCharacter(_Action):
    """只观察名称框直到稳定；绝不重放点击。"""

    def execute(self, context):
        runtime = _rt()
        _, view = _capture(context)
        _page(view, identity=False)
        if view.page != "gifts":
            raise _FlowError("page")
        identity = view.character_id if view.identity_verified else None
        # 名称变了只需常规稳定时间；没变则可能本就选中该角色或点击未生效，多等一倍。
        changed = identity != runtime.previous_character
        if not _settled(identity, _equal, SETTLE if changed else SETTLE * 2):
            _waiting("character", "identity")
            _pause(context)
            return "GiftWaitCharacter"
        signature = runtime.pending_signature
        if signature is not None:
            known, expected = _known_slot(signature)
            if not known:
                runtime.known_slots.append((signature, identity))
            elif expected is not None and identity is not None and expected != identity:
                # 同一头像签名点出了不同角色：签名不可靠，停止点击该角色。
                raise _FlowError("ambiguous")
        runtime.pending_slot = None
        runtime.pending_signature = None
        current = runtime.session.current
        if identity is not None and identity == current.character_id:
            runtime.search = None
            return "GiftCheckQuota"
        return "GiftChooseCharacter"


@AgentServer.custom_action("gift_scroll")
class GiftScroll(_Action):
    """在侧栏或礼物格滚轮一次；不以画面不动之外的任何东西推断到底。"""

    def execute(self, context):
        runtime = _rt()
        frame, view = _capture(context)
        grid = runtime.scroll_kind == "grid"
        _page(view, identity=grid)
        if view.page != "gifts" or runtime.scroll_direction not in {"up", "down"}:
            raise _FlowError("scan")
        vision = runtime.vision
        if grid:
            runtime.scroll_before = vision.grid_signature(frame)
            point = vision.grid_scroll_point()
            step = GRID_STEP
        else:
            runtime.scroll_before = vision.sidebar_signature(frame)
            point = vision.sidebar_scroll_point()
            step = SIDEBAR_STEP
        _scroll(context, point, TOP_STEP if runtime.scroll_direction == "up" else step)
        _arm("scroll", SCROLL_WAIT)
        return "GiftWaitScroll"


@AgentServer.custom_action("gift_wait_scroll")
class GiftWaitScroll(_Action):
    """滚动后等画面稳定；与滚动前相比不变＝已到顶/到底，变了＝翻到新一页。"""

    def execute(self, context):
        runtime = _rt()
        frame, view = _capture(context)
        _page(view, identity=False)
        if view.page != "gifts":
            raise _FlowError("page")
        vision = runtime.vision
        grid = runtime.scroll_kind == "grid"
        signature = (
            vision.grid_signature(frame) if grid else vision.sidebar_signature(frame)
        )
        if not _settled(signature, vision.same):
            _waiting("scroll", "scan")
            _pause(context)
            return "GiftWaitScroll"
        moved = not vision.same(runtime.scroll_before, signature)
        state = runtime.scan if grid else runtime.search
        if state is None:
            raise _FlowError("scan")
        if moved:
            # 新一页：本页的格子/槽位须重新识别。向下翻页时保留上一页结果，
            # 用于核对两页重叠（证明没有跳过整行）；向上回顶不做核对。
            if grid:
                state.previous = (
                    {
                        index: (state.signatures.get(index), gift_id)
                        for index, gift_id in state.known.items()
                    }
                    if runtime.scroll_direction == "down"
                    else {}
                )
                state.signatures, state.known = {}, {}
                state.fast_tried = False
            state.filled, state.queue = False, []
        if runtime.scroll_direction == "up":
            if moved:
                state.top_scrolls += 1
                if state.top_scrolls > MAX_TOP_SCROLLS:
                    raise _FlowError("scan")
            else:
                state.top = True
            return "GiftFindGift" if grid else "GiftChooseCharacter"
        if not moved:
            # 已到底：侧栏没有目标角色；礼物格整列已逐格核对，目标确实不在。
            if not grid:
                raise _FlowError("character_not_found")
            if state.sweep:
                # 图标逐页没找到：回到顶部逐格点选核对名称，才能证明缺货。
                state.sweep = False
                state.top, state.top_scrolls, state.pages = False, 0, 0
                state.known, state.signatures, state.previous = {}, {}, {}
                state.unknown = False
                state.filled, state.queue = False, []
                return "GiftFindGift"
            return _absent(context)
        state.pages += 1
        return "GiftFindGift" if grid else "GiftChooseCharacter"


def _absent(context: Context) -> str:
    """礼物格从顶到底逐格核对后没有目标：视为未持有，换下一候选。"""
    runtime = _rt()
    scan = runtime.scan
    if scan is None or scan.unknown or not scan.top or scan.sweep:
        # 有格子读不出名称，或没从顶部开始：不能证明缺货，保守放弃该角色。
        raise _FlowError("scan")
    if scan.pages == 0 and all(
        signature is not None for signature in scan.signatures.values()
    ):
        # 滚轮从未让礼物格动过、且这一页格子全满：可能是滚动没生效而不是到底。
        raise _FlowError("scan")
    gift_id = runtime.session.current.gift_id
    runtime.session.exhaust_unavailable(runtime.frame_id)
    PrintT(context, "gift.exhausted", _gift_name(gift_id))
    runtime.scan = None
    runtime.hint_cell = None
    runtime.announced_gift = None
    return (
        "GiftFindGift"
        if runtime.session.current.status == "ready"
        else "GiftNextCharacter"
    )


@AgentServer.custom_action("gift_check_quota")
class GiftCheckQuota(_Action):
    def execute(self, context):
        runtime = _rt()
        _, view = _capture(context)
        _page(view)
        if view.page != "gifts":
            raise _FlowError("page")
        target = runtime.session.check_quota(_evidence(view))
        current = runtime.session.current
        if runtime.announced_character != current.character_id:
            PrintT(
                context, "gift.character", _name(current.character_id), current.limit
            )
            runtime.announced_character = current.character_id
        if target == 0:
            PrintT(context, "gift.already_done", _name(current.character_id))
            return "GiftNextCharacter"
        return "GiftFindGift"


def _select(context: Context, index: int, view: FrameView) -> str:
    """点选礼物格；身份只由随后「当前选择」的名称确认。"""
    runtime = _rt()
    scan = runtime.scan
    scan.clicks += 1
    if scan.clicks > MAX_CELL_CLICKS:
        raise _FlowError("scan")
    runtime.pending_cell = index
    runtime.previous_gift = view.gift_id if view.gift_verified else None
    _click(context, runtime.vision.grid_cells()[index])
    _arm("gift_selection", SELECTION_WAIT)
    return "GiftVerifyGift"


def _fill(frame: np.ndarray, scan: _Scan) -> None:
    """记录本页格子签名；向下翻页后须与上一页按整行错位重叠，否则不能证明没跳行。"""
    vision = _rt().vision
    count = len(vision.grid_cells())
    columns = vision.grid_columns()
    scan.signatures = {
        index: vision.cell_signature(frame, index) for index in range(count)
    }
    if scan.previous:
        matched = None
        for shift in range(1, count // columns):
            compared = hits = 0
            carried = {}
            for index in range(count - shift * columns):
                old_signature, old_gift = scan.previous.get(
                    index + shift * columns, (None, None)
                )
                new_signature = scan.signatures[index]
                if old_signature is None or new_signature is None:
                    continue
                compared += 1
                if vision.same(old_signature, new_signature):
                    hits += 1
                    carried[index] = old_gift
            # 允许一处不一致：被点选的格子带高亮边框，签名会变化。
            if compared and hits >= max(1, compared - 1):
                matched = carried
                break
        if matched is None:
            raise _FlowError("scan")
        for index, gift_id in matched.items():
            scan.known[index] = gift_id
            if gift_id is None:
                scan.unknown = True
    scan.previous = {}
    scan.queue = [
        index
        for index in range(count)
        if scan.signatures[index] is not None and index not in scan.known
    ]
    scan.filled = True


@AgentServer.custom_action("gift_find_gift")
class GiftFindGift(_Action):
    """按优先级找当前候选礼物：先图标匹配，再从顶部逐格点选核对名称。"""

    def execute(self, context):
        runtime = _rt()
        current = runtime.session.current
        if current is None or current.status != "ready":
            return "GiftNextCharacter"
        frame, view = _capture(context)
        _page(view)
        if view.page != "gifts":
            raise _FlowError("page")
        target = current.gift_id
        if runtime.announced_gift != target:
            PrintT(context, "gift.candidate", _gift_name(target))
            runtime.announced_gift = target
        scan = runtime.scan
        if scan is None or scan.gift_id != target:
            record = runtime.catalog.gifts.get(target) or {}
            scan = runtime.scan = _Scan(target, sweep=bool(record.get("image")))
        vision = runtime.vision
        # 刚送出一件：原格子大概率仍是同一礼物，先重新点选它刷新「当前选择」。
        hint, runtime.hint_cell = runtime.hint_cell, None
        if hint is not None and vision.cell_signature(frame, hint) is not None:
            return _select(context, hint, view)
        if not scan.fast_tried:
            scan.fast_tried = True
            cells = {
                vision.cell_index(box) for box in vision.gift_candidates(frame, target)
            }
            if len(cells) == 1 and None not in cells:
                index = next(iter(cells))
                if index not in scan.known:
                    return _select(context, index, view)
        if not scan.top:
            return _request_scroll("grid", "up")
        if scan.sweep:
            if scan.pages >= MAX_GRID_PAGES:
                raise _FlowError("scan")
            return _request_scroll("grid", "down")
        if not scan.filled:
            _fill(frame, scan)
        for index, gift_id in scan.known.items():
            if gift_id == target and vision.cell_signature(frame, index) is not None:
                return _select(context, index, view)
        if scan.queue:
            return _select(context, scan.queue.pop(0), view)
        if scan.pages >= MAX_GRID_PAGES:
            raise _FlowError("scan")
        return _request_scroll("grid", "down")


@AgentServer.custom_action("gift_verify_gift")
class GiftVerifyGift(_Action):
    """点选后只观察「当前选择」直到稳定；记录该格是什么礼物，不发送。"""

    def execute(self, context):
        runtime = _rt()
        _, view = _capture(context)
        _page(view, identity=False)
        if view.page != "gifts":
            raise _FlowError("page")
        selection = view.gift_id if view.gift_verified else None
        changed = selection != runtime.previous_gift
        if not _settled(selection, _equal, SETTLE if changed else SETTLE * 2):
            _waiting("gift_selection", "gift_identity")
            _pause(context)
            return "GiftVerifyGift"
        scan, index = runtime.scan, runtime.pending_cell
        runtime.pending_cell = None
        if scan is None or index is None:
            raise _FlowError("gift_identity")
        if selection is not None and any(
            other != index and gift_id == selection
            for other, gift_id in scan.known.items()
        ):
            # 两个格子读出同一礼物：说明点击未生效或名称残留，不能据此判断。
            raise _FlowError("ambiguous")
        scan.known[index] = selection
        if selection is None:
            scan.unknown = True
        if selection != scan.gift_id:
            return "GiftFindGift"
        runtime.selected_cell = index
        return "GiftSendOne"


@AgentServer.custom_action("gift_send_one")
class GiftSendOne(_Action):
    """新截图通过全部安全门后先建立 pending，再且仅再点一次「赠送」。"""

    def execute(self, context):
        runtime = _rt()
        frame, view = _capture(context)
        _page(view)
        current = runtime.session.current
        index = runtime.selected_cell
        if view.page != "gifts" or index is None:
            raise _FlowError("gift_identity")
        if not (view.gift_verified and view.gift_id == current.gift_id):
            raise _FlowError("gift_identity")
        _box_center(view.send_box)
        # 选中格有 ∞ 徽记但目录说是常规礼物：与目录不符，拒绝消耗。
        unlimited = bool(runtime.catalog.gifts[current.gift_id].get("unlimited"))
        badge = runtime.vision.infinite_at(frame, runtime.vision.grid_cells()[index])
        try:
            intent = runtime.session.authorize(
                _evidence(view, infinite=badge and not unlimited),
                stopping=context.tasker.stopping,
            )
        except GiftSafetyError as exc:
            if exc.code == "dry_run":
                PrintT(
                    context,
                    "gift.dry_run",
                    _name(current.character_id),
                    _gift_name(current.gift_id),
                )
                runtime.session.skip_current("dry_run")
                return "GiftNextCharacter"
            if exc.code == "target_reached":
                PrintT(context, "gift.already_done", _name(current.character_id))
                return "GiftNextCharacter"
            raise
        logger.info(
            "赠礼提交意图 | character=%s gift=%s attempt=%d remaining=%d unlimited=%s frame=%d",
            intent.character_id,
            intent.gift_id,
            intent.attempt,
            intent.remaining,
            intent.unlimited,
            intent.frame_id,
        )
        # 送出后格子可能变化：下一件必须重新点选并核对名称，原格子只作提示。
        runtime.hint_cell = index
        runtime.selected_cell = None
        runtime.scan = None
        _arm("receipt", RECEIPT_WAIT)
        _click(context, view.send_box, sending=True, intent=intent)
        return "GiftWaitReceipt"


@AgentServer.custom_action("gift_wait_receipt")
class GiftWaitReceipt(_Action):
    """只观察，不发送：常规礼物计数恰好减 1，不占次数礼物计数不变且出现好感提示。"""

    def execute(self, context):
        runtime = _rt()
        _, view = _capture(context)
        intent = runtime.session.pending
        if intent is None:
            raise _FlowError("receipt")
        if (
            view.limit_prompt == "character"
            and view.page == "gifts"
            and view.character_id == intent.character_id
            and view.remaining == intent.remaining
        ):
            # 游戏明确拒收且计数未变：没有消耗，跳过该角色，不重发。
            runtime.session.refuse_pending("character_limit")
            return "GiftNextCharacter"
        result = runtime.session.confirm(
            _evidence(view), stopping=context.tasker.stopping
        )
        logger.debug(
            "赠礼回执 | attempt=%d status=%s remaining=%s affection=%s",
            intent.attempt,
            result,
            view.remaining,
            view.affection,
        )
        if result == "confirmed":
            current = runtime.session.current
            PrintT(
                context,
                "gift.sent",
                _name(intent.character_id),
                _gift_name(intent.gift_id),
                current.confirmed,
                view.remaining,
            )
            return (
                "GiftCheckQuota" if current.status == "ready" else "GiftNextCharacter"
            )
        if result != "waiting":
            runtime.reason = "global_limit" if result == "global_limit" else "receipt"
            runtime.blocked = result == "global_limit"
            return "GiftWarnCurrent"
        _waiting("receipt", "receipt_timeout")
        _pause(context)
        return "GiftWaitReceipt"


@AgentServer.custom_action("gift_receipt_failed")
class GiftReceiptFailed(_Action):
    def execute(self, context):
        runtime = _rt()
        if runtime.session.pending is not None:
            runtime.session.timeout_pending()
        if runtime.reason == "internal":
            runtime.reason = "receipt_timeout"
        return "GiftWarnCurrent"


@AgentServer.custom_action("gift_warn_current")
class GiftWarnCurrent(_Action):
    def execute(self, context):
        runtime = _rt()
        current = runtime.session.current
        if current is None:
            _warn(context, None, runtime.reason)
            return "GiftSummary"
        if runtime.session.pending is not None:
            runtime.session.timeout_pending()
        if runtime.session.current.status == "ready":
            runtime.session.skip_current(runtime.reason)
        _warn(context, current.character_id, runtime.reason)
        if runtime.blocked:
            return "GiftNextCharacter"
        _arm("recovery", PAGE_WAIT)
        return "GiftRecoverCurrent"


@AgentServer.custom_action("gift_recover_current")
class GiftRecoverCurrent(_Action):
    def execute(self, context):
        runtime = _rt()
        _, view = _capture(context)
        if view.global_limit:
            runtime.session.block_remaining("global_limit")
            runtime.blocked = True
            return "GiftNextCharacter"
        if view.page in {"bond", "gifts"}:
            # 仍在已知的羁遇/赠礼页面：直接换下一角色；不点击泛化的“确定”。
            return "GiftNextCharacter"
        if view.page == "loading":
            _waiting("recovery", "recovery")
            _pause(context)
            return "GiftRecoverCurrent"
        # 未识别页面不发送任何恢复输入，更不以 Esc/确定试探。
        runtime.session.block_remaining("recovery")
        runtime.blocked = True
        runtime.task_failed = True
        return "GiftSummary"


@AgentServer.custom_action("gift_next_character")
class GiftNextCharacter(_Action):
    def execute(self, context):
        runtime = _rt()
        current = runtime.session.current
        if (
            current is not None
            and current.status in {"skipped", "uncertain"}
            and current.reason not in {"target_reached", "dry_run"}
        ):
            _warn(context, current.character_id, current.reason or runtime.reason)
        current = runtime.session.next_recipient()
        runtime.search = None
        runtime.scan = None
        runtime.hint_cell = None
        runtime.selected_cell = None
        runtime.pending_cell = None
        runtime.pending_slot = None
        runtime.announced_gift = None
        runtime.reason = "internal"
        if current is None:
            return "GiftSummary"
        return (
            "GiftChooseCharacter" if current.status == "ready" else "GiftNextCharacter"
        )


@AgentServer.custom_action("gift_navigation_failed")
class GiftNavigationFailed(_Action):
    def execute(self, context):
        runtime = _rt()
        if runtime.reason == "internal":
            runtime.reason = "navigation"
        runtime.session.block_remaining(runtime.reason)
        runtime.blocked = True
        runtime.task_failed = True
        if not runtime.session.states:
            _warn(context, None, runtime.reason)
        return "GiftSummary"


@AgentServer.custom_action("gift_summary")
class GiftSummary(_Action):
    """汇总；有角色无法完成或结果不明时转到 GiftFailed，让任务显示失败。"""

    def execute(self, context):
        runtime = _rt()
        if runtime.summary_emitted:
            return None
        # 所有无法执行项均逐项通知；部分/全部跳过不能伪称任务送礼成功。
        for state in runtime.session.states:
            if state.status in {"skipped", "uncertain"} and state.reason not in {
                "target_reached",
                "dry_run",
            }:
                _warn(context, state.character_id, state.reason or runtime.reason)
        states = runtime.session.states
        confirmed = sum(state.confirmed for state in states)
        already = sum(
            state.reason == "target_reached" and state.confirmed == 0
            for state in states
        )
        dry_run = sum(state.reason == "dry_run" for state in states)
        uncertain = sum(state.status == "uncertain" for state in states)
        unable = len(runtime.invalid) + sum(
            state.status in {"ready", "skipped"}
            and state.reason not in {"target_reached", "dry_run"}
            for state in states
        )
        PrintT(context, "gift.summary", confirmed, already, dry_run, unable, uncertain)
        logger.info(
            "赠礼汇总 | confirmed=%d already_done=%d dry_run=%d unable=%d uncertain=%d "
            "attempts=%d failed=%s",
            confirmed,
            already,
            dry_run,
            unable,
            uncertain,
            runtime.session.attempts,
            runtime.task_failed,
        )
        runtime.summary_emitted = True
        if runtime.task_failed or unable or uncertain:
            return "GiftFailed"
        return None
