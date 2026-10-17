"""Pipeline 驱动的逐件赠礼动作；每次调用最多一次输入，等待节点只观察。"""

from __future__ import annotations

import re
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
from .state import (
    Evidence,
    GiftSafetyError,
    GiftSession,
    Recipient,
    ScanEvidence,
    ScanTracker,
)
from .vision import FrameView, GiftVision

# 静态 next 为安全默认值；这里只允许在已审查的节点集合之间动态分派。
_ROUTES = {
    "GiftEntry": {"GiftEnsureBond", "GiftSummary"},
    "GiftChooseCharacter": {
        "GiftWaitCharacter",
        "GiftWaitCharacterList",
        "GiftOpenTab",
        "GiftCheckQuota",
        "GiftScrollList",
        "GiftNextCharacter",
    },
    "GiftWaitCharacter": {"GiftWaitCharacter", "GiftOpenTab", "GiftCheckQuota"},
    "GiftWaitCharacterList": {"GiftWaitCharacterList", "GiftChooseCharacter"},
    "GiftOpenTab": {"GiftWaitGiftPage", "GiftCheckQuota"},
    "GiftWaitGiftPage": {"GiftWaitGiftPage", "GiftCheckQuota"},
    "GiftCheckQuota": {"GiftFindGift", "GiftNextCharacter"},
    "GiftFindGift": {
        "GiftVerifyGift",
        "GiftScrollList",
        "GiftFindGift",
        "GiftNextCharacter",
    },
    "GiftScrollList": {"GiftWaitScroll"},
    "GiftWaitScroll": {"GiftWaitScroll", "GiftFindGift", "GiftChooseCharacter"},
    "GiftVerifyGift": {"GiftVerifyGift", "GiftSendOne"},
    "GiftSendOne": {"GiftWaitReceipt", "GiftFindGift", "GiftNextCharacter"},
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
    "GiftSummary": {None},
}

_REASON_CODES = {
    "invalid_config",
    "no_recipients",
    "unknown_character",
    "character_not_found",
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
    "gift_changed": "gift_identity",
    "counter_jump": "receipt",
    "inventory_conflict": "receipt",
    "receipt_timeout": "receipt_timeout",
    "gifts_exhausted": "exhausted",
    "wrong_character": "identity",
    "wrong_gift": "gift_identity",
    "unverified_page_verified": "page",
    "unverified_identity_verified": "identity",
    "unverified_gift_verified": "gift_identity",
    "unverified_selected": "gift_identity",
    "stock_not_zero": "inventory",
}


class _FlowError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


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
    scan: ScanTracker | None = None
    scan_key: str | None = None
    scan_evidence: tuple[str, ...] = ()
    scroll_kind: str = ""
    scroll_direction: str = ""
    scroll_before: str = ""
    scroll_evidence: tuple[str, ...] = ()
    scan_progress: bool = False
    scroll_steps: int = 0
    character_top: bool = False
    list_close_requested: bool = False
    announced_gift: str | None = None
    blocked: bool = False
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
        "赠礼识别 | frame=%d page=%s character=%s gift=%s remaining=%s/%s stock=%s errors=%s",
        runtime.frame_id,
        view.page,
        view.character_id,
        view.gift_id,
        view.remaining,
        view.total,
        view.inventory,
        view.errors,
    )
    return frame, view


def _evidence(view: FrameView) -> Evidence:
    return Evidence(
        frame_id=_rt().frame_id,
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
        infinite=view.infinite,
        global_limit=view.global_limit,
    )


def _page(view: FrameView, *, identity: bool = True) -> None:
    runtime = _rt()
    if view.global_limit:
        runtime.session.block_remaining("global_limit")
        runtime.blocked = True
        raise _FlowError("global_limit")
    if view.page not in {"bond", "gifts", "characters"}:
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


def _close_character_list(context: Context, view: FrameView) -> None:
    runtime = _rt()
    _page(view)
    if (
        view.page != "characters"
        or runtime.list_close_requested
        or not view.controls.get("close_list")
    ):
        raise _FlowError("navigation")
    runtime.list_close_requested = True
    _click(context, view.controls["close_list"])


def _arm(kind: str, seconds: float = 8.0) -> None:
    runtime = _rt()
    runtime.wait_kind = kind
    runtime.deadline = time.monotonic() + seconds
    runtime.wait_frames = 0


def _waiting(kind: str, error: str) -> None:
    runtime = _rt()
    runtime.wait_frames += 1
    if (
        runtime.wait_kind != kind
        or time.monotonic() >= runtime.deadline
        or runtime.wait_frames > 120
    ):
        raise _FlowError(error)


def _config(
    context: Context, catalog: Catalog
) -> tuple[list[Recipient], list[tuple[str, str]], bool]:
    settings = context.get_node_data("GiftSettings") or {}
    dry_run = settings.get("attach", {}).get("dry_run")
    if type(dry_run) is not bool:
        raise _FlowError("invalid_config")
    recipients, invalid = [], []
    for character_id, record in catalog.characters.items():
        _stop(context)
        node = context.get_node_data("GiftConfig_" + character_id) or {}
        config = node.get("attach", {})
        if config.get("selected") is False or not config:
            continue
        if config.get("selected") is not True:
            invalid.append((character_id, "invalid_config"))
            continue
        try:
            if not record.get("giftable") or config.get("character_id") != character_id:
                raise _FlowError("unknown_character")
            gifts = catalog.parse_priority(config.get("gifts"))
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


def _request_scroll(kind: str, direction: str) -> str:
    runtime = _rt()
    runtime.scroll_kind = kind
    runtime.scroll_direction = direction
    return "GiftScrollList"


def _scroll_progress(
    before: tuple[str, ...], after: tuple[str, ...], direction: str
) -> bool:
    """边界观测不等于进展：拒绝纯动画、反向滚动和跳过一整屏的翻页。"""
    if direction not in {"up", "down"}:
        return False
    sign = -1 if direction == "up" else 1
    proofs = []
    for pattern, kind in (
        (r"page:第([1-9][0-9]*)页/共([1-9][0-9]*)页", "page"),
        (r"scrollbar:([0-9]+):([1-9][0-9]*):([0-9]+):([1-9][0-9]*)", "bar"),
    ):
        old = [re.fullmatch(pattern, item) for item in before]
        new = [re.fullmatch(pattern, item) for item in after]
        old, new = [item for item in old if item], [item for item in new if item]
        if not old and not new:
            continue
        if len(old) != 1 or len(new) != 1:
            return False
        previous = tuple(map(int, old[0].groups()))
        current = tuple(map(int, new[0].groups()))
        if kind == "page":
            page, total = previous
            next_page, next_total = current
            proofs.append(
                total == next_total
                and 1 <= next_page <= total
                and next_page - page == sign
            )
        else:
            y, height, thumb_y, thumb_height = previous
            ny, nh, next_y, nth = current
            proofs.append(
                y == ny
                and height == nh
                and thumb_height == nth
                and y <= thumb_y <= y + height - thumb_height
                and ny <= next_y <= ny + nh - nth
                and 0 < (next_y - thumb_y) * sign <= thumb_height
            )
    return bool(proofs) and all(proofs)


@AgentServer.custom_action("gift_choose_character")
class GiftChooseCharacter(_Action):
    def execute(self, context):
        runtime = _rt()
        current = runtime.session.current
        if current is None or current.status != "ready":
            return "GiftNextCharacter"
        frame, view = _capture(context)
        _page(view, identity=False)
        if view.character_id == current.character_id and view.identity_verified:
            if view.page == "characters":
                _close_character_list(context, view)
                _arm("character")
                return "GiftWaitCharacter"
            return "GiftCheckQuota" if view.page == "gifts" else "GiftOpenTab"
        candidates = runtime.vision.character_candidates(frame, current.character_id)
        if len(candidates) > 1:
            raise _FlowError("ambiguous")
        if candidates:
            _click(context, candidates[0])
            _arm("character")
            return "GiftWaitCharacter"
        if view.page in {"bond", "gifts"} and view.controls.get("list_button"):
            _click(context, view.controls["list_button"])
            _arm("character_list")
            return "GiftWaitCharacterList"
        info = runtime.vision.scan_info(
            frame, kind="characters", target_id=current.character_id
        )
        if not info.category_verified or not info.scroll_box:
            raise _FlowError("character_not_found")
        if not runtime.character_top:
            if info.start_verified:
                runtime.character_top = True
            else:
                return _request_scroll("characters", "up")
        if info.end_verified:
            raise _FlowError("character_not_found")
        return _request_scroll("characters", "down")


@AgentServer.custom_action("gift_wait_character")
class GiftWaitCharacter(_Action):
    def execute(self, context):
        _, view = _capture(context)
        if view.global_limit:
            _page(view)
        if (
            view.identity_verified
            and view.character_id == _rt().session.current.character_id
            and view.page in {"bond", "gifts"}
        ):
            return "GiftCheckQuota" if view.page == "gifts" else "GiftOpenTab"
        _waiting("character", "identity")
        if (
            view.page == "characters"
            and view.identity_verified
            and view.character_id == _rt().session.current.character_id
            and not _rt().list_close_requested
        ):
            _close_character_list(context, view)
        return "GiftWaitCharacter"


@AgentServer.custom_action("gift_wait_character_list")
class GiftWaitCharacterList(_Action):
    def execute(self, context):
        _, view = _capture(context)
        if view.global_limit:
            _page(view)
        if view.page == "characters":
            return "GiftChooseCharacter"
        _waiting("character_list", "navigation")
        return "GiftWaitCharacterList"


@AgentServer.custom_action("gift_open_tab")
class GiftOpenTab(_Action):
    def execute(self, context):
        _, view = _capture(context)
        _page(view)
        if view.page == "gifts":
            return "GiftCheckQuota"
        if not view.controls.get("gift_tab"):
            raise _FlowError("navigation")
        _click(context, view.controls["gift_tab"])
        _arm("gift_page")
        return "GiftWaitGiftPage"


@AgentServer.custom_action("gift_wait_gift_page")
class GiftWaitGiftPage(_Action):
    def execute(self, context):
        _, view = _capture(context)
        if view.global_limit:
            _page(view)
        if (
            view.page == "gifts"
            and view.identity_verified
            and view.character_id == _rt().session.current.character_id
        ):
            return "GiftCheckQuota"
        _waiting("gift_page", "page")
        return "GiftWaitGiftPage"


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
        PrintT(context, "gift.character", _name(current.character_id), current.limit)
        if target == 0:
            PrintT(context, "gift.already_done", _name(current.character_id))
            return "GiftNextCharacter"
        return "GiftFindGift"


def _exhaust(context: Context, evidence) -> str:
    runtime = _rt()
    gift_id = runtime.session.current.gift_id
    runtime.session.exhaust_current_gift(evidence)
    PrintT(context, "gift.exhausted", _gift_name(gift_id))
    runtime.scan = None
    runtime.announced_gift = None
    runtime.scroll_steps = 0
    return (
        "GiftFindGift"
        if runtime.session.current.status == "ready"
        else "GiftNextCharacter"
    )


@AgentServer.custom_action("gift_find_gift")
class GiftFindGift(_Action):
    def execute(self, context):
        runtime = _rt()
        current = runtime.session.current
        if current.status != "ready":
            return "GiftNextCharacter"
        frame, view = _capture(context)
        _page(view)
        if view.page != "gifts":
            raise _FlowError("page")
        if runtime.announced_gift != current.gift_id:
            PrintT(context, "gift.candidate", _gift_name(current.gift_id))
            runtime.announced_gift = current.gift_id
        if view.gift_id == current.gift_id and view.gift_verified and view.selected:
            runtime.scan = None
            runtime.scan_progress = False
            if view.infinite:
                raise _FlowError("infinite")
            if type(view.inventory) is int and view.inventory == 0:
                return _exhaust(context, _evidence(view))
            _arm("gift_selection")
            return "GiftVerifyGift"
        candidates = runtime.vision.gift_candidates(frame, current.gift_id)
        if len(candidates) > 1:
            raise _FlowError("ambiguous")
        if candidates:
            runtime.scan = None
            runtime.scan_progress = False
            _click(context, candidates[0])
            _arm("gift_selection")
            return "GiftVerifyGift"
        info = runtime.vision.scan_info(frame, kind="gifts", target_id=current.gift_id)
        if not info.category_verified or info.errors:
            raise _FlowError("scan")
        if runtime.scan is None:
            if not info.start_verified:
                return _request_scroll("gifts", "up")
            runtime.scan = ScanTracker(current.character_id, current.gift_id)
        absent = runtime.scan.observe(
            ScanEvidence(
                frame_id=runtime.frame_id,
                character_id=current.character_id,
                gift_id=current.gift_id,
                page_key=info.page_key,
                page_verified=True,
                identity_verified=view.identity_verified,
                category_verified=info.category_verified,
                start_verified=info.start_verified,
                end_verified=info.end_verified,
                progress_verified=runtime.scan_progress
                and tuple(info.progress_evidence) == runtime.scroll_evidence,
                no_errors=not bool(info.errors),
                absence_verified=info.absence_verified,
                found=False,
                single_page_verified=info.single_page_verified,
            )
        )
        runtime.scan_key = info.page_key
        runtime.scan_evidence = tuple(info.progress_evidence)
        runtime.scan_progress = False
        if absent:
            return _exhaust(context, runtime.scan.proof())
        return _request_scroll("gifts", "down")


@AgentServer.custom_action("gift_scroll_list")
class GiftScrollList(_Action):
    def execute(self, context):
        runtime = _rt()
        frame, view = _capture(context)
        _page(view, identity=runtime.scroll_kind == "gifts")
        target_id = (
            runtime.session.current.gift_id
            if runtime.scroll_kind == "gifts"
            else runtime.session.current.character_id
        )
        info = runtime.vision.scan_info(
            frame, kind=runtime.scroll_kind, target_id=target_id
        )
        if (
            not info.category_verified
            or not info.page_key
            or not info.scroll_box
            or info.errors
            or not info.progress_evidence
        ):
            raise _FlowError("scan")
        if (
            runtime.scroll_kind == "gifts"
            and runtime.scan is not None
            and (
                runtime.scroll_direction != "down"
                or info.page_key != runtime.scan_key
                or tuple(info.progress_evidence) != runtime.scan_evidence
                or not info.absence_verified
                or target_id in info.visible_ids
            )
        ):
            # 扫描后、输入前又发生惯性翻页时，不能把新页当作已检查过。
            # 本轮保守退出，绝不能用缺页 proof 推进到低优先礼物。
            raise _FlowError("scan")
        if runtime.scroll_steps >= 30:
            raise _FlowError("scan")
        _box_center(info.scroll_box)
        x, y, width, height = info.scroll_box
        if height < 60 or width < 20:
            raise _FlowError("scan")
        upper, lower = y + height // 4, y + height * 3 // 4
        start, end = (
            (upper, lower) if runtime.scroll_direction == "up" else (lower, upper)
        )
        runtime.scroll_before = info.page_key
        runtime.scroll_evidence = tuple(info.progress_evidence)
        runtime.scan_progress = False
        runtime.scroll_steps += 1
        if runtime.session.pending is not None:
            raise _FlowError("receipt")
        # 列表扫描跨多个导航帧。保持起点证明的时序，不将自己的翻页
        # 误判为旧扫描；每个输入/观察仍由 _capture 生成全新帧。
        runtime.session.invalidate("scroll")
        _stop(context)
        if (
            not context.tasker.controller.post_swipe(
                x + width // 2, start, x + width // 2, end, 300
            )
            .wait()
            .succeeded
        ):
            raise _FlowError("controller")
        _stop(context)
        _arm("scroll")
        return "GiftWaitScroll"


@AgentServer.custom_action("gift_wait_scroll")
class GiftWaitScroll(_Action):
    def execute(self, context):
        runtime = _rt()
        frame, view = _capture(context)
        _page(view, identity=runtime.scroll_kind == "gifts")
        target_id = (
            runtime.session.current.gift_id
            if runtime.scroll_kind == "gifts"
            else runtime.session.current.character_id
        )
        info = runtime.vision.scan_info(
            frame, kind=runtime.scroll_kind, target_id=target_id
        )
        if (
            info.category_verified
            and info.page_key
            and info.page_key != runtime.scroll_before
            and not info.errors
            and _scroll_progress(
                runtime.scroll_evidence,
                tuple(info.progress_evidence),
                runtime.scroll_direction,
            )
        ):
            runtime.scroll_evidence = tuple(info.progress_evidence)
            runtime.scan_progress = True
            return (
                "GiftFindGift"
                if runtime.scroll_kind == "gifts"
                else "GiftChooseCharacter"
            )
        _waiting("scroll", "scan")
        return "GiftWaitScroll"


@AgentServer.custom_action("gift_verify_gift")
class GiftVerifyGift(_Action):
    def execute(self, context):
        runtime = _rt()
        _, view = _capture(context)
        _page(view)
        if (
            view.page == "gifts"
            and view.gift_verified
            and view.selected
            and view.gift_id == runtime.session.current.gift_id
        ):
            if view.infinite:
                raise _FlowError("infinite")
            return "GiftSendOne"
        _waiting("gift_selection", "gift_identity")
        return "GiftVerifyGift"


@AgentServer.custom_action("gift_send_one")
class GiftSendOne(_Action):
    def execute(self, context):
        runtime = _rt()
        _, view = _capture(context)
        _page(view)
        _box_center(view.send_box)
        if (
            view.gift_id == runtime.session.current.gift_id
            and view.gift_verified
            and view.selected
            and type(view.inventory) is int
            and view.inventory == 0
        ):
            return _exhaust(context, _evidence(view))
        try:
            intent = runtime.session.authorize(
                _evidence(view), stopping=context.tasker.stopping
            )
        except GiftSafetyError as exc:
            if exc.code == "dry_run":
                PrintT(
                    context,
                    "gift.dry_run",
                    _name(runtime.session.current.character_id),
                    _gift_name(runtime.session.current.gift_id),
                )
                runtime.session.skip_current("dry_run")
                return "GiftNextCharacter"
            if exc.code == "target_reached":
                PrintT(
                    context,
                    "gift.already_done",
                    _name(runtime.session.current.character_id),
                )
                return "GiftNextCharacter"
            raise
        logger.info(
            "赠礼提交意图 | character=%s gift=%s attempt=%d remaining=%d stock=%d frame=%d",
            intent.character_id,
            intent.gift_id,
            intent.attempt,
            intent.remaining,
            intent.inventory,
            intent.frame_id,
        )
        _arm("receipt", 10.0)
        _click(context, view.send_box, sending=True, intent=intent)
        return "GiftWaitReceipt"


@AgentServer.custom_action("gift_wait_receipt")
class GiftWaitReceipt(_Action):
    def execute(self, context):
        runtime = _rt()
        _, view = _capture(context)
        intent = runtime.session.pending
        if intent is None:
            raise _FlowError("receipt")
        result = runtime.session.confirm(
            _evidence(view), stopping=context.tasker.stopping
        )
        logger.debug(
            "赠礼回执 | attempt=%d status=%s remaining=%s stock=%s",
            intent.attempt,
            result,
            view.remaining,
            view.inventory,
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
        _arm("recovery")
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
        if view.page in {"bond", "gifts", "characters"}:
            # 在已知非确认页面直接切换到下一角色；不点击泛化的“确定”。
            return "GiftNextCharacter"
        if view.page == "loading":
            _waiting("recovery", "recovery")
            return "GiftRecoverCurrent"
        # 未识别页面不发送任何恢复输入，更不以Esc/确定试探。
        runtime.session.block_remaining("recovery")
        runtime.blocked = True
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
        runtime.scan = None
        runtime.announced_gift = None
        runtime.character_top = False
        runtime.list_close_requested = False
        runtime.scroll_steps = 0
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
        if not runtime.session.states:
            _warn(context, None, runtime.reason)
        return "GiftSummary"


@AgentServer.custom_action("gift_summary")
class GiftSummary(_Action):
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
            "赠礼汇总 | confirmed=%d already_done=%d dry_run=%d unable=%d uncertain=%d attempts=%d",
            confirmed,
            already,
            dry_run,
            unable,
            uncertain,
            runtime.session.attempts,
        )
        runtime.summary_emitted = True
        return None
