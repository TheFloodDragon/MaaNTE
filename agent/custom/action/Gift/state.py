"""赠礼的纯业务状态机：只消费明确证据，不识图、导航、等待或发送输入。"""

from dataclasses import dataclass, replace
from typing import Iterable


class GiftSafetyError(ValueError):
    """拒绝危险或不完整的状态转换；code 可供动作层记录原因。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise GiftSafetyError(code)


def _integer(value: object, minimum: int, maximum: int | None = None) -> bool:
    return (
        type(value) is int
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _identifier(value: object) -> bool:
    return type(value) is str and bool(value.strip()) and value == value.strip()


def _counter(remaining: object, total: object) -> None:
    _require(type(total) is int and total == 3, "invalid_total")
    _require(_integer(remaining, 0, 3), "invalid_remaining")


@dataclass(frozen=True)
class Recipient:
    character_id: str
    gifts: tuple[str, ...]
    limit: int = 3

    def __post_init__(self) -> None:
        _require(_identifier(self.character_id), "invalid_character_id")
        _require(type(self.gifts) is tuple and bool(self.gifts), "invalid_gifts")
        _require(all(_identifier(gift) for gift in self.gifts), "invalid_gift_id")
        _require(len(set(self.gifts)) == len(self.gifts), "duplicate_gift")
        _require(_integer(self.limit, 1, 3), "invalid_limit")


@dataclass(frozen=True)
class Evidence:
    frame_id: int = -1
    character_id: str | None = None
    gift_id: str | None = None
    remaining: int | None = None
    total: int | None = None
    inventory: int | None = None
    quantity: int | None = None
    page_verified: bool = False
    identity_verified: bool = False
    gift_verified: bool = False
    selected: bool = False
    infinite: bool = False
    global_limit: bool = False


@dataclass(frozen=True)
class ScanEvidence:
    frame_id: int = -1
    character_id: str | None = None
    gift_id: str | None = None
    page_key: str | None = None
    page_verified: bool = False
    identity_verified: bool = False
    category_verified: bool = False
    start_verified: bool = False
    end_verified: bool = False
    progress_verified: bool = False
    no_errors: bool = False
    absence_verified: bool = False
    found: bool = False
    single_page_verified: bool = False


@dataclass(frozen=True)
class ScanProof:
    """完整缺失凭据；起终点与实际滚动均必须有独立依据。"""

    character_id: str | None = None
    gift_id: str | None = None
    first_frame_id: int = -1
    last_frame_id: int = -1
    pages: int = 0
    page_verified: bool = False
    identity_verified: bool = False
    category_verified: bool = False
    start_verified: bool = False
    end_verified: bool = False
    progress_verified: bool = False
    no_errors: bool = False
    absence_verified: bool = False
    single_page_verified: bool = False


class ScanTracker:
    """有限页数扫描；静止、低置信、分类错误都不等于列表末尾。"""

    def __init__(self, character_id: str, gift_id: str, max_pages: int = 20):
        _require(_identifier(character_id), "invalid_character_id")
        _require(_identifier(gift_id), "invalid_gift_id")
        _require(_integer(max_pages, 1, 100), "invalid_scan_budget")
        self.character_id = character_id
        self.gift_id = gift_id
        self.max_pages = max_pages
        self._status = "scanning"
        self._page_keys: set[str] = set()
        self._first_frame = -1
        self._last_frame = -1
        self._single_page = False

    @property
    def status(self) -> str:
        return self._status

    @property
    def pages_seen(self) -> int:
        return len(self._page_keys)

    def observe(self, evidence: ScanEvidence) -> bool:
        _require(self._status == "scanning", "scan_closed")
        try:
            self._observe(evidence)
        except GiftSafetyError:
            self._status = "uncertain"
            raise
        return self._status == "absent"

    def _observe(self, evidence: ScanEvidence) -> None:
        _require(isinstance(evidence, ScanEvidence), "invalid_scan_evidence")
        _require(_integer(evidence.frame_id, 0), "invalid_frame")
        _require(evidence.frame_id > self._last_frame, "stale_frame")
        _require(evidence.character_id == self.character_id, "wrong_character")
        _require(evidence.gift_id == self.gift_id, "wrong_gift")
        for name in (
            "page_verified",
            "identity_verified",
            "category_verified",
            "no_errors",
        ):
            _require(getattr(evidence, name) is True, "scan_" + name)
        for name in (
            "start_verified",
            "end_verified",
            "progress_verified",
            "absence_verified",
            "found",
            "single_page_verified",
        ):
            _require(type(getattr(evidence, name)) is bool, "invalid_scan_" + name)
        _require(evidence.found != evidence.absence_verified, "scan_ambiguous")
        _require(_identifier(evidence.page_key), "invalid_page_key")
        _require(evidence.page_key not in self._page_keys, "scan_no_progress")
        _require(self.pages_seen < self.max_pages, "scan_budget")
        if self.pages_seen:
            _require(evidence.progress_verified, "scan_progress_unverified")
            _require(not evidence.start_verified, "scan_restarted")
        elif not evidence.found:
            _require(evidence.start_verified, "scan_start_unverified")
        if not self.pages_seen:
            self._first_frame = evidence.frame_id
        self._last_frame = evidence.frame_id
        self._page_keys.add(evidence.page_key)
        if evidence.found:
            self._status = "found"
        elif evidence.end_verified:
            if self.pages_seen == 1:
                _require(evidence.single_page_verified, "scan_single_page_unverified")
                self._single_page = True
            self._status = "absent"
        else:
            _require(self.pages_seen < self.max_pages, "scan_budget")

    def proof(self) -> ScanProof:
        _require(self._status == "absent", "scan_incomplete")
        return ScanProof(
            character_id=self.character_id,
            gift_id=self.gift_id,
            first_frame_id=self._first_frame,
            last_frame_id=self._last_frame,
            pages=self.pages_seen,
            page_verified=True,
            identity_verified=True,
            category_verified=True,
            start_verified=True,
            end_verified=True,
            progress_verified=self.pages_seen > 1,
            no_errors=True,
            absence_verified=True,
            single_page_verified=self._single_page,
        )


@dataclass(frozen=True)
class Pending:
    character_id: str
    gift_id: str
    frame_id: int
    remaining: int
    total: int
    inventory: int
    quantity: int
    attempt: int


@dataclass(frozen=True)
class RecipientState:
    recipient: Recipient
    gift_index: int = 0
    confirmed: int = 0
    attempts: int = 0
    status: str = "ready"
    reason: str | None = None
    last_remaining: int | None = None

    @property
    def character_id(self) -> str:
        return self.recipient.character_id

    @property
    def gift_id(self) -> str | None:
        gifts = self.recipient.gifts
        return gifts[self.gift_index] if self.gift_index < len(gifts) else None

    @property
    def limit(self) -> int:
        return self.recipient.limit


class GiftSession:
    """Pipeline 每步调用一次；pending 从意图建立到明确回执之间不可回放。"""

    def __init__(
        self,
        recipients: Iterable[Recipient],
        dry_run: bool = False,
        send_budget: int = 10,
    ):
        # 空选择是任务 UI 的安全默认值：允许汇总，但 _ready 始终拒绝授权。
        configured = tuple(recipients)
        _require(
            all(isinstance(item, Recipient) for item in configured), "invalid_recipient"
        )
        identifiers = [item.character_id for item in configured]
        _require(len(set(identifiers)) == len(identifiers), "duplicate_recipient")
        _require(type(dry_run) is bool, "invalid_dry_run")
        _require(_integer(send_budget, 0, 10), "invalid_send_budget")
        self._states = [RecipientState(item) for item in configured]
        self._index = 0
        self._dry_run = dry_run
        self._send_budget = send_budget
        self._attempts = 0
        self._pending: Pending | None = None
        self._last_frame = -1
        self._stopped = False
        self._blocked_reason: str | None = None

    @property
    def current(self) -> RecipientState | None:
        return self._states[self._index] if self._index < len(self._states) else None

    @property
    def states(self) -> tuple[RecipientState, ...]:
        return tuple(self._states)

    @property
    def pending(self) -> Pending | None:
        return self._pending

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def _replace_current(self, **changes: object) -> None:
        _require(self.current is not None, "no_recipient")
        self._states[self._index] = replace(self.current, **changes)

    def _ready(self) -> RecipientState:
        _require(self._pending is None, "pending_exists")
        _require(not self._stopped, "stopped")
        _require(self._blocked_reason is None, self._blocked_reason or "blocked")
        current = self.current
        _require(current is not None, "no_recipient")
        _require(current.status == "ready", "recipient_closed")
        return current

    def _frame(self, frame_id: int) -> None:
        _require(_integer(frame_id, 0), "invalid_frame")
        _require(frame_id > self._last_frame, "stale_frame")
        self._last_frame = frame_id

    def _identity(self, evidence: Evidence, selected: bool = False) -> None:
        current = self.current
        _require(evidence.character_id == current.character_id, "wrong_character")
        _require(evidence.gift_id == current.gift_id, "wrong_gift")
        names = ["page_verified", "identity_verified", "gift_verified"]
        if selected:
            names.append("selected")
        for name in names:
            _require(getattr(evidence, name) is True, "unverified_" + name)

    def _flags(self, evidence: Evidence) -> None:
        _require(type(evidence.infinite) is bool, "invalid_infinite")
        _require(type(evidence.global_limit) is bool, "invalid_global_limit")
        if evidence.global_limit:
            self.block_remaining("global_limit")
            raise GiftSafetyError("global_limit")
        _require(not evidence.infinite, "infinite_quantity")

    def _known_counter(self, remaining: int, total: int) -> int:
        target = self.remaining_target(remaining, total)
        if (
            self.current.last_remaining is not None
            and remaining != self.current.last_remaining
        ):
            self._replace_current(status="uncertain", reason="counter_changed")
            raise GiftSafetyError("counter_changed")
        self._replace_current(last_remaining=remaining)
        return target

    def remaining_target(self, remaining: int, total: int) -> int:
        _require(self.current is not None, "no_recipient")
        _counter(remaining, total)
        return max(0, self.current.limit - (total - remaining))

    def check_quota(self, evidence: Evidence) -> int:
        """未选礼物时也可核验当日目标；页面/身份必须是全新正面证据。"""
        current = self._ready()
        _require(isinstance(evidence, Evidence), "invalid_evidence")
        self._frame(evidence.frame_id)
        self._flags(evidence)
        _require(evidence.page_verified is True, "unverified_page_verified")
        _require(evidence.identity_verified is True, "unverified_identity_verified")
        _require(evidence.character_id == current.character_id, "wrong_character")
        target = self._known_counter(evidence.remaining, evidence.total)
        if target == 0:
            self._replace_current(status="completed", reason="target_reached")
        return target

    def authorize(self, evidence: Evidence, *, stopping: bool = False) -> Pending:
        _require(type(stopping) is bool, "invalid_stopping")
        if stopping:
            self.cancel()
            raise GiftSafetyError("stopped")
        current = self._ready()
        _require(isinstance(evidence, Evidence), "invalid_evidence")
        self._frame(evidence.frame_id)
        self._flags(evidence)
        self._identity(evidence, selected=True)
        _counter(evidence.remaining, evidence.total)
        _require(
            type(evidence.quantity) is int and evidence.quantity == 1,
            "invalid_quantity",
        )
        _require(_integer(evidence.inventory, 1), "invalid_inventory")
        if not self._known_counter(evidence.remaining, evidence.total):
            self._replace_current(status="completed", reason="target_reached")
            raise GiftSafetyError("target_reached")
        _require(not self._dry_run, "dry_run")
        if self._attempts >= self._send_budget:
            self.block_remaining("send_budget")
            raise GiftSafetyError("send_budget")
        self._attempts += 1
        self._pending = Pending(
            character_id=current.character_id,
            gift_id=current.gift_id,
            frame_id=evidence.frame_id,
            remaining=evidence.remaining,
            total=evidence.total,
            inventory=evidence.inventory,
            quantity=evidence.quantity,
            attempt=self._attempts,
        )
        self._replace_current(status="pending", attempts=current.attempts + 1)
        return self._pending

    def _uncertain(self, reason: str) -> str:
        self._pending = None
        self._replace_current(status="uncertain", reason=reason)
        self._enforce_budget()
        return "uncertain"

    def _enforce_budget(self) -> None:
        if self._pending is None and self._attempts >= self._send_budget:
            self.block_remaining("send_budget")

    def confirm(self, evidence: Evidence, *, stopping: bool = False) -> str:
        _require(type(stopping) is bool, "invalid_stopping")
        _require(self._pending is not None, "no_pending")
        if stopping:
            self.cancel()
            return "uncertain"
        _require(isinstance(evidence, Evidence), "invalid_evidence")
        self._frame(evidence.frame_id)
        pending = self._pending
        for name in (
            "page_verified",
            "identity_verified",
            "gift_verified",
            "selected",
            "infinite",
            "global_limit",
        ):
            if type(getattr(evidence, name)) is not bool:
                return self._uncertain("invalid_" + name)
        if evidence.global_limit:
            self.block_remaining("global_limit")
            return "global_limit"
        if (
            evidence.character_id is not None
            and evidence.character_id != pending.character_id
        ):
            return self._uncertain("identity_changed")
        if evidence.gift_id is not None and evidence.gift_id != pending.gift_id:
            return self._uncertain("gift_changed")
        if evidence.infinite:
            return self._uncertain("infinite_quantity")
        if evidence.total is not None and (
            type(evidence.total) is not int or evidence.total != 3
        ):
            return self._uncertain("invalid_total")
        if evidence.remaining is not None and not _integer(evidence.remaining, 0, 3):
            return self._uncertain("invalid_remaining")
        if evidence.inventory is not None and not _integer(evidence.inventory, 0):
            return self._uncertain("invalid_inventory")
        if evidence.quantity is not None and (
            type(evidence.quantity) is not int or evidence.quantity != 1
        ):
            return self._uncertain("invalid_quantity")
        decrement = (
            None
            if evidence.remaining is None
            else pending.remaining - evidence.remaining
        )
        if decrement is not None and decrement not in (0, 1):
            return self._uncertain("counter_jump")
        if evidence.inventory is not None:
            expected = (
                (pending.inventory, pending.inventory - 1)
                if decrement is None
                else (pending.inventory - decrement,)
            )
            if evidence.inventory not in expected:
                return self._uncertain("inventory_conflict")
        if not (
            evidence.page_verified
            and evidence.identity_verified
            and evidence.gift_verified
            and evidence.character_id == pending.character_id
            and evidence.gift_id == pending.gift_id
            and evidence.remaining is not None
            and evidence.total is not None
        ):
            return "waiting"
        if decrement == 0:
            return "waiting"
        self._pending = None
        done = self.remaining_target(evidence.remaining, evidence.total) == 0
        self._replace_current(
            confirmed=self.current.confirmed + 1,
            last_remaining=evidence.remaining,
            status="completed" if done else "ready",
            reason="target_reached" if done else None,
        )
        self._enforce_budget()
        return "confirmed"

    def timeout_pending(self) -> str:
        _require(self._pending is not None, "no_pending")
        return self._uncertain("confirmation_timeout")

    def invalidate(self, reason: str, *, frame_id: int | None = None) -> None:
        """页面/身份切换失效意图；传入切换帧还能淘汰未消费的旧截图。"""
        _require(_identifier(reason), "invalid_reason")
        if self._pending is not None:
            self._uncertain(reason)
        if frame_id is not None:
            _require(_integer(frame_id, 0), "invalid_frame")
            _require(frame_id >= self._last_frame, "stale_frame")
            self._last_frame = frame_id

    def is_pending(self, intent: Pending) -> bool:
        """仅原始、未撤销的意图有效；动作层仍须紧邻输入检查 stopping。"""
        return self._pending is intent and intent is not None

    def exhaust_current_gift(self, evidence: Evidence | ScanProof) -> RecipientState:
        current = self._ready()
        if isinstance(evidence, Evidence):
            self._frame(evidence.frame_id)
            self._flags(evidence)
            self._identity(evidence)
            _require(
                type(evidence.inventory) is int and evidence.inventory == 0,
                "stock_not_zero",
            )
            if not self._known_counter(evidence.remaining, evidence.total):
                self._replace_current(status="completed", reason="target_reached")
                return self.current
        else:
            _require(isinstance(evidence, ScanProof), "invalid_exhaustion_proof")
            _require(evidence.character_id == current.character_id, "wrong_character")
            _require(evidence.gift_id == current.gift_id, "wrong_gift")
            for name in (
                "page_verified",
                "identity_verified",
                "category_verified",
                "start_verified",
                "end_verified",
                "no_errors",
                "absence_verified",
            ):
                _require(getattr(evidence, name) is True, "scan_" + name)
            _require(_integer(evidence.pages, 1, 100), "invalid_scan_pages")
            _require(_integer(evidence.first_frame_id, 0), "invalid_frame")
            _require(
                _integer(evidence.last_frame_id, evidence.first_frame_id),
                "invalid_frame",
            )
            _require(evidence.first_frame_id > self._last_frame, "stale_scan")
            if evidence.pages == 1:
                _require(
                    evidence.first_frame_id == evidence.last_frame_id,
                    "invalid_scan_frames",
                )
                _require(
                    evidence.single_page_verified is True, "scan_single_page_unverified"
                )
            else:
                _require(
                    evidence.last_frame_id - evidence.first_frame_id
                    >= evidence.pages - 1,
                    "invalid_scan_frames",
                )
                _require(evidence.progress_verified is True, "scan_progress_unverified")
            self._frame(evidence.last_frame_id)
        index = current.gift_index + 1
        exhausted = index == len(current.recipient.gifts)
        self._replace_current(
            gift_index=index,
            status="skipped" if exhausted else "ready",
            reason="gifts_exhausted" if exhausted else None,
        )
        return self.current

    def skip_current(self, reason: str) -> None:
        _require(_identifier(reason), "invalid_reason")
        _require(self._pending is None, "pending_exists")
        _require(self.current is not None, "no_recipient")
        if self.current.status == "ready":
            self._replace_current(status="skipped", reason=reason)

    def next_recipient(self) -> RecipientState | None:
        _require(self._pending is None, "pending_exists")
        if self.current is not None:
            _require(self.current.status != "ready", "recipient_unfinished")
            self._index += 1
        while self.current is not None and self.current.status != "ready":
            self._index += 1
        return self.current

    def block_remaining(self, reason: str) -> None:
        _require(_identifier(reason), "invalid_reason")
        self._blocked_reason = reason
        if self._pending is not None:
            self._pending = None
            self._replace_current(status="uncertain", reason=reason)
        for index in range(self._index, len(self._states)):
            if self._states[index].status == "ready":
                self._states[index] = replace(
                    self._states[index], status="skipped", reason=reason
                )

    def cancel(self, reason: str = "stopped") -> None:
        self.block_remaining(reason)
        self._stopped = True

    def summary(self) -> dict:
        return {
            "attempts": self._attempts,
            "confirmed": sum(state.confirmed for state in self._states),
            "pending": self._pending is not None,
            "dry_run": self._dry_run,
            "send_budget": self._send_budget,
            "stopped": self._stopped,
            "blocked_reason": self._blocked_reason,
            "recipients": [
                {
                    "character_id": state.character_id,
                    "gift_id": state.gift_id,
                    "limit": state.limit,
                    "attempts": state.attempts,
                    "confirmed": state.confirmed,
                    "status": state.status,
                    "reason": state.reason,
                }
                for state in self._states
            ],
        }
