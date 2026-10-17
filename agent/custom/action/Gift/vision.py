"""赠礼的单帧视觉证据；不截图、不点击、不等待，也不隐式导航。

Wiki 美术只定位候选。完整名称、独立的显式选中标记、数量、额度必须在
同一帧各自得到正面证据。上游仅提供羁遇/无限徽记小图，没有完整详情、
选中边框或无数量控件布局；因此不猜高亮颜色，也不把缺少数量控件当作 1。
识别 ROI 是校准起点，不是可点击的固定坐标。详见 Gift/UI/source.json。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from utils.logger import logger

Box = list[int]
OCR_MIN_SCORE = 0.90
IMAGE_MIN_SCORE = 0.94
CHARACTER_ROI = [1120, 75, 155, 535]
GIFT_ROI = [650, 250, 475, 285]
DETAIL_ROI = [560, 165, 565, 365]


@dataclass(frozen=True)
class FrameView:
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
    send_box: Box | None = None
    controls: dict[str, Box] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanInfo:
    category_verified: bool = False
    start_verified: bool = False
    end_verified: bool = False
    page_key: str | None = None
    progress_evidence: tuple[str, ...] = ()
    single_page_verified: bool = False
    absence_verified: bool = False
    scroll_box: Box | None = None
    visible_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class TextHit:
    text: str
    score: float
    box: Box


class VisionError(ValueError):
    """图像/识别失败是未知，不是零库存或扫描完成。"""


def normalize_text(text: str) -> str:
    """仅 NFKC 和去空白；不改字、去标点、忽略大小写或模糊匹配。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def parse_quota(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([0-3])/3", normalize_text(text))
    return (int(match[1]), 3) if match else None


def parse_inventory(text: str) -> int | None:
    match = re.fullmatch(
        r"(?:(?:持有数量|拥有数量|库存)[:：]?|[xX×])?(0|[1-9][0-9]{0,8})",
        normalize_text(text),
    )
    return int(match[1]) if match else None


def parse_quantity(text: str) -> int | None:
    # 必须有数量语义；任意位置出现的 1 可能是库存、等级或额度。
    match = re.fullmatch(
        r"(?:赠送数量|贈送數量|数量|數量)[:：]?([1-9][0-9]{0,3})", normalize_text(text)
    )
    return int(match[1]) if match else None


def _box(value: Any) -> Box | None:
    if value is None:
        return None
    try:
        values = (
            list(value)
            if isinstance(value, (list, tuple, np.ndarray))
            else [value.x, value.y, value.w, value.h]
        )
        if len(values) != 4 or any(
            not isinstance(v, (int, np.integer)) for v in values
        ):
            return None
        x, y, w, h = [int(v) for v in values]
        return (
            [x, y, w, h] if 0 <= x < x + w <= 1280 and 0 <= y < y + h <= 720 else None
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return (
        obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    )


def _rows(detail: Any) -> list:
    # all_results 保留被 threshold/expected 筛掉的冲突，不能只看 best_result。
    rows = _get(detail, "all_results")
    if rows:
        return list(rows)
    raw = _get(detail, "raw_detail", {}) or {}
    rows = raw.get("detail", {}).get("all") if isinstance(raw, dict) else None
    return list(rows or _get(detail, "filtered_results", []) or [])


def text_hits(detail: Any) -> list[TextHit]:
    hits = []
    for row in _rows(detail):
        text, box = _get(row, "text"), _box(_get(row, "box"))
        score = _get(row, "score", 0.0)
        if isinstance(text, str) and normalize_text(text) and box is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = float("nan")
            hits.append(TextHit(text, score, box))
    return hits


def unique_name(
    hits: list[TextHit], records: dict[str, dict]
) -> tuple[str, Box] | None:
    if (
        len(hits) != 1
        or not math.isfinite(hits[0].score)
        or hits[0].score < OCR_MIN_SCORE
    ):
        return None
    text = normalize_text(hits[0].text)
    identifiers = {
        identifier
        for identifier, record in records.items()
        if text
        in {
            normalize_text(name)
            for name in record.get("names", {}).values()
            if isinstance(name, str)
        }
    }
    return (next(iter(identifiers)), hits[0].box) if len(identifiers) == 1 else None


def _one_text(hits: list[TextHit], expected: set[str] | None = None) -> TextHit | None:
    if expected is not None:
        hits = [hit for hit in hits if normalize_text(hit.text) in expected]
    if (
        len(hits) != 1
        or not math.isfinite(hits[0].score)
        or hits[0].score < OCR_MIN_SCORE
    ):
        return None
    return hits[0]


def _inside(inner: Box, outer: Box, margin: int = 0) -> bool:
    x, y, w, h = inner
    a, b, c, d = outer
    return (
        x >= a - margin
        and y >= b - margin
        and x + w <= a + c + margin
        and y + h <= b + d + margin
    )


def _overlap(a: Box, b: Box) -> float:
    intersection = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(
        0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    )
    return intersection / max(1, min(a[2] * a[3], b[2] * b[3]))


def _selected_box(marker: TextHit | None, candidates: list[Box]) -> Box | None:
    if marker is None:
        return None
    # 标记必须落在同一卡片之内或紧贴其下方，不接受另一行的“已选择”。
    nearby = [box for box in candidates if _inside(marker.box, box, margin=18)]
    return nearby[0] if len(nearby) == 1 else None


def _validate_frame(frame: np.ndarray) -> None:
    if (
        not isinstance(frame, np.ndarray)
        or frame.dtype != np.uint8
        or frame.shape not in ((720, 1280, 3), (720, 1280, 4))
    ):
        raise VisionError("unsupported_frame_size_or_format")


def _rectangles(frame: np.ndarray, roi: Box) -> list[Box]:
    """只返回画面中实际闭合的矩形，不由槽位常量构造按钮/边界。"""
    x, y, w, h = roi
    gray = cv2.cvtColor(frame[y : y + h, x : x + w, :3], cv2.COLOR_BGR2GRAY)
    contours, _ = cv2.findContours(
        cv2.Canny(gray, 60, 150), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    boxes = []
    for contour in contours:
        hull = cv2.convexHull(contour)
        approximation = cv2.approxPolyDP(
            hull, min(1.0, 0.02 * cv2.arcLength(hull, True)), True
        )
        if len(approximation) != 4 or not cv2.isContourConvex(approximation):
            continue
        a, b, c, d = cv2.boundingRect(approximation)
        corners = {(a, b), (a + c - 1, b), (a, b + d - 1), (a + c - 1, b + d - 1)}
        if any(
            not any(
                abs(int(point[0][0]) - p) <= 2 and abs(int(point[0][1]) - q) <= 2
                for p, q in corners
            )
            for point in approximation
        ):
            continue
        box = [x + a, y + b, c, d]
        if c < 2 or d < 8 or a <= 1 or b <= 1 or a + c >= w - 1 or b + d >= h - 1:
            continue
        if any(
            all(abs(v - u) <= 3 for v, u in zip(box, existing)) for existing in boxes
        ):
            continue
        boxes.append(box)
    return boxes


def _scrollbar(rectangles: list[Box], cells: list[Box]) -> tuple[Box, Box] | None:
    """正面检测嵌套的轨道和滑块；没有滑块/轨道绝不意味着单页或到底。"""
    if not cells:
        return None
    right = max(box[0] + box[2] for box in cells)
    pairs = []
    for track in rectangles:
        if not (
            3 <= track[2] <= 18 and track[3] >= 70 and right < track[0] <= right + 40
        ):
            continue
        for thumb in rectangles:
            if thumb == track or not _inside(thumb, track, margin=1):
                continue
            if 2 <= thumb[2] <= track[2] and 8 <= thumb[3] < track[3] - 5:
                pairs.append((track, thumb))
    return pairs[0] if len(pairs) == 1 else None


class GiftVision:
    def __init__(self, context: Any, catalog: Any):
        self.context = context
        self.catalog = catalog
        self._images: dict[str, np.ndarray] = {}

    def _recognize(self, name: str, frame: np.ndarray, roi: Box | None = None):
        override = (
            {name: {"recognition": {"param": {"roi": roi}}}} if roi is not None else {}
        )
        try:
            return self.context.run_recognition(name, frame, pipeline_override=override)
        except Exception as exc:
            logger.warning(
                "赠礼视觉识别异常: node=%s error=%s", name, type(exc).__name__
            )
            raise VisionError("recognition_error:" + name) from exc

    def _texts(
        self, name: str, frame: np.ndarray, roi: Box | None = None
    ) -> list[TextHit]:
        hits = text_hits(self._recognize(name, frame, roi))
        return [hit for hit in hits if roi is None or _inside(hit.box, roi)]

    def _control(self, name: str, frame: np.ndarray, expected: set[str]) -> Box | None:
        hit = _one_text(self._texts(name, frame), expected)
        return hit.box if hit else None

    def _template_boxes(self, name: str, frame: np.ndarray) -> list[Box]:
        detail = self._recognize(name, frame)
        if detail is None or not _get(detail, "hit", False):
            return []
        boxes = []
        for row in _get(detail, "filtered_results", []) or []:
            try:
                score = float(_get(row, "score", 0.0))
            except (TypeError, ValueError):
                continue
            box = _box(_get(row, "box"))
            if box and math.isfinite(score) and score >= 0.90:
                boxes.append(box)
        return boxes

    def _template_box(self, name: str, frame: np.ndarray) -> Box | None:
        boxes = self._template_boxes(name, frame)
        return boxes[0] if len(boxes) == 1 else None

    def _image(self, record: dict) -> np.ndarray:
        relative = record.get("image", {}).get("path")
        if not isinstance(relative, str):
            raise VisionError("missing_candidate_image")
        root = (Path(self.catalog.base) / "image").resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise VisionError("candidate_image_path_escape")
        key = str(path)
        if key not in self._images:
            try:
                image = cv2.imdecode(
                    np.frombuffer(path.read_bytes(), dtype=np.uint8),
                    cv2.IMREAD_UNCHANGED,
                )
            except OSError as exc:
                raise VisionError("candidate_image_read_failed") from exc
            if image is None or image.ndim != 3 or image.shape[2] not in (3, 4):
                raise VisionError("candidate_image_decode_failed")
            if image.shape[2] == 4:
                points = cv2.findNonZero((image[:, :, 3] > 200).astype(np.uint8))
                if points is None:
                    raise VisionError("candidate_image_empty_alpha")
                x, y, w, h = cv2.boundingRect(points)
                image = image[y : y + h, x : x + w]
            self._images[key] = image
        return self._images[key]

    def _candidates(
        self, frame: np.ndarray, identifier: str, kind: str, roi: Box
    ) -> list[Box]:
        records = (
            self.catalog.characters if kind == "characters" else self.catalog.gifts
        )
        if identifier not in records:
            return []
        image = self._image(records[identifier])
        x, y, w, h = roi
        search = frame[y : y + h, x : x + w, :3]
        # Wiki 缩略图与游戏中的显示尺寸可能不同；多尺度仅用于定位候选。
        edges = sorted({40, 48, 56, 64, 72, 80, 88, 96, max(image.shape[:2])})
        matches = []
        for edge in edges:
            if getattr(getattr(self.context, "tasker", None), "stopping", False):
                raise VisionError("stopping")
            scale = edge / max(image.shape[:2])
            tw, th = max(2, round(image.shape[1] * scale)), max(
                2, round(image.shape[0] * scale)
            )
            if tw > w or th > h:
                continue
            resized = cv2.resize(
                image,
                (tw, th),
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
            )
            template = resized[:, :, :3]
            mask = (
                (resized[:, :, 3] > 200).astype(np.uint8) * 255
                if resized.shape[2] == 4
                else np.full((th, tw), 255, dtype=np.uint8)
            )
            if np.count_nonzero(mask) < 30 or float(np.std(template[mask > 0])) < 5.0:
                continue
            scores = cv2.matchTemplate(
                search, template, cv2.TM_SQDIFF_NORMED, mask=mask
            )
            scores = np.where(np.isfinite(scores), 1.0 - scores, -1.0)
            # 极值抑制只合并同一物理卡片的多尺度命中，不抹掉相邻的歧义候选。
            for _ in range(32):
                _, score, _, location = cv2.minMaxLoc(scores)
                if score < IMAGE_MIN_SCORE:
                    break
                a, b = location
                matches.append((score, [x + a, y + b, tw, th]))
                scores[
                    max(0, b - th // 2) : b + th // 2 + 1,
                    max(0, a - tw // 2) : a + tw // 2 + 1,
                ] = -1.0
        kept = []
        for _, box in sorted(matches, key=lambda match: match[0], reverse=True):
            if not any(_overlap(box, existing) > 0.55 for existing in kept):
                kept.append(box)
        return sorted(kept, key=lambda box: (box[1], box[0]))

    def _page(self, frame: np.ndarray) -> tuple[str, dict[str, Box]]:
        global_limit = self._control(
            "GiftGlobalLimitOCR",
            frame,
            {
                "今日赠礼总次数已达上限",
                "今日所有角色赠礼次数已达上限",
                "今日已达到赠礼总次数上限",
            },
        )
        if global_limit:
            return "global_limit", {}
        dialog_actions = {
            "确定",
            "確定",
            "确认",
            "確認",
            "购买",
            "購買",
            "解锁",
            "解鎖",
            "提交",
            "确认赠送",
            "確認贈送",
            "是否赠送",
            "是否贈送",
            "是否购买",
            "是否購買",
        }
        if any(
            normalize_text(hit.text) in dialog_actions
            for hit in self._texts("GiftUnexpectedDialogOCR", frame)
        ):
            # 识别到危险对话框词即阻断，即使低分也不能以底层页面授权消耗。
            raise VisionError("unexpected_dialog")
        if self._template_box("GiftLoadingMarker", frame):
            return "loading", {}
        if not self._template_box("GiftBondMarker", frame):
            return "unknown", {}
        controls = {}
        gift_tab = self._control("GiftTabOCR", frame, {"赠礼", "贈禮"})
        if gift_tab:
            controls["gift_tab"] = gift_tab
        list_title = self._control(
            "GiftCharacterListTitleOCR", frame, {"角色列表", "羁遇列表", "羈遇列表"}
        )
        if list_title:
            close = self._template_box("GiftCloseButton", frame)
            if close:
                controls["close_list"] = close
            return "characters", controls
        if gift_tab is None:
            return "unknown", {}
        list_button = self._template_box("GiftListButton", frame)
        if list_button:
            controls["list_button"] = list_button
        send = self._control("GiftSendOCR", frame, {"赠送", "贈送", "送礼", "送禮"})
        counter = _one_text(self._texts("GiftQuotaOCR", frame))
        if gift_tab and (
            send is not None or (counter and parse_quota(counter.text) is not None)
        ):
            return "gifts", controls
        return "bond", controls

    def inspect(self, frame: np.ndarray) -> FrameView:
        try:
            _validate_frame(frame)
            return self._inspect(frame)
        except (VisionError, cv2.error) as exc:
            logger.debug("赠礼单帧证据不可用: error=%s", str(exc))
            return FrameView(errors=(str(exc),))

    def _inspect(self, frame: np.ndarray) -> FrameView:
        page, controls = self._page(frame)
        if page in {"unknown", "loading", "global_limit"}:
            return FrameView(page=page, global_limit=page == "global_limit")
        errors = []
        identity = unique_name(
            self._texts("GiftCharacterNameOCR", frame), self.catalog.characters
        )
        character_id = identity[0] if identity else None
        identity_verified = False
        if character_id:
            marker = _one_text(
                self._texts("GiftCharacterSelectedOCR", frame),
                {"已选择", "已選擇", "当前角色", "當前角色"},
            )
            selected_avatar = _selected_box(
                marker,
                self._candidates(frame, character_id, "characters", CHARACTER_ROI),
            )
            if selected_avatar:
                conflicts = [
                    identifier
                    for identifier in self.catalog.characters
                    if identifier != character_id
                    and any(
                        _overlap(candidate, selected_avatar) > 0.55
                        for candidate in self._candidates(
                            frame, identifier, "characters", CHARACTER_ROI
                        )
                    )
                ]
                identity_verified = not conflicts
        if not identity_verified:
            errors.append("character_identity_or_selection_unverified")
        if page != "gifts":
            return FrameView(
                page=page,
                character_id=character_id,
                identity_verified=identity_verified,
                controls=controls,
                errors=tuple(errors),
            )

        quota_hit = _one_text(self._texts("GiftQuotaOCR", frame))
        quota = parse_quota(quota_hit.text) if quota_hit else None
        if quota is None:
            errors.append("quota_unverified")
        detail_name = unique_name(
            self._texts("GiftDetailNameOCR", frame), self.catalog.gifts
        )
        gift_id = detail_name[0] if detail_name else None
        selected = False
        gift_verified = False
        inventory = None
        quantity = None
        detail_title = self._control(
            "GiftDetailTitleOCR",
            frame,
            {"礼物详情", "禮物詳情", "道具详情", "道具詳情"},
        )
        if gift_id and detail_title:
            art = self._candidates(frame, gift_id, "gifts", DETAIL_ROI)
            selected_marker = _one_text(
                self._texts("GiftSelectedOCR", frame),
                {"已选择", "已選擇", "已选中", "已選中"},
            )
            selected_gift = _selected_box(selected_marker, art)
            selected = selected_gift is not None and len(art) == 1
            gift_verified = selected and _inside(detail_name[1], DETAIL_ROI)
            inventory_hit = _one_text(self._texts("GiftInventoryOCR", frame))
            quantity_hit = _one_text(self._texts("GiftQuantityOCR", frame))
            if gift_verified:
                inventory = (
                    parse_inventory(inventory_hit.text) if inventory_hit else None
                )
                quantity = parse_quantity(quantity_hit.text) if quantity_hit else None
            close = self._template_box("GiftCloseButton", frame)
            cancel = self._control("GiftCancelOCR", frame, {"取消"})
            if close:
                controls["close_detail"] = close
            if cancel:
                controls["cancel"] = cancel
        if not gift_verified:
            errors.append("gift_detail_or_selection_unverified")
        if inventory is None:
            errors.append("inventory_unverified")
        if quantity is None:
            errors.append("quantity_unverified_no_single_item_layout_evidence")
        infinite_ocr = self._texts("GiftInfiniteOCR", frame)
        # 任一通道可靠命中即拒绝消耗；OCR 缺失绝不折算成库存 0。
        infinite = bool(self._template_boxes("GiftInfiniteMarker", frame)) or any(
            math.isfinite(hit.score)
            and hit.score >= OCR_MIN_SCORE
            and normalize_text(hit.text) == "∞"
            for hit in infinite_ocr
        )
        send_box = self._control("GiftSendOCR", frame, {"赠送", "贈送", "送礼", "送禮"})
        return FrameView(
            page=page,
            character_id=character_id,
            identity_verified=identity_verified,
            gift_id=gift_id,
            gift_verified=gift_verified,
            selected=selected,
            remaining=quota[0] if quota else None,
            total=quota[1] if quota else None,
            inventory=inventory,
            quantity=quantity,
            infinite=infinite,
            send_box=send_box,
            controls=controls,
            errors=tuple(errors),
        )

    def character_candidates(self, frame: np.ndarray, character_id: str) -> list[Box]:
        _validate_frame(frame)
        page, _ = self._page(frame)
        if page not in {"bond", "gifts", "characters"}:
            return []
        if page == "characters":
            # 列表完整名字是可点击候选，不会因一次 OCR 就授予身份。
            candidates = []
            for hit in self._texts("GiftVisibleNameOCR", frame, [70, 95, 1150, 510]):
                result = unique_name([hit], self.catalog.characters)
                if result and result[0] == character_id:
                    candidates.append(hit.box)
            return candidates
        return self._candidates(frame, character_id, "characters", CHARACTER_ROI)

    def gift_candidates(self, frame: np.ndarray, gift_id: str) -> list[Box]:
        _validate_frame(frame)
        page, _ = self._page(frame)
        return (
            self._candidates(frame, gift_id, "gifts", GIFT_ROI)
            if page == "gifts"
            else []
        )

    def scan_info(
        self, frame: np.ndarray, kind: str = "gifts", target_id: str | None = None
    ) -> ScanInfo:
        try:
            _validate_frame(frame)
            if kind not in {"gifts", "characters"}:
                raise VisionError("unknown_scan_kind")
            return self._scan_info(frame, kind, target_id)
        except (VisionError, cv2.error) as exc:
            return ScanInfo(errors=(str(exc),))

    def _scan_info(
        self, frame: np.ndarray, kind: str, target_id: str | None
    ) -> ScanInfo:
        page, _ = self._page(frame)
        if page != kind:
            return ScanInfo(errors=("wrong_scan_page",))
        roi = GIFT_ROI if kind == "gifts" else [70, 95, 1150, 510]
        category_text = (
            {"当前分类:全部礼物", "当前分类:全部", "當前分類:全部禮物", "當前分類:全部"}
            if kind == "gifts"
            else {
                "当前分类:全部角色",
                "当前分类:全部",
                "當前分類:全部角色",
                "當前分類:全部",
            }
        )
        category = self._control("GiftCategoryOCR", frame, category_text)
        if category is None:
            return ScanInfo(errors=("selected_list_category_unverified",))
        records = self.catalog.gifts if kind == "gifts" else self.catalog.characters
        if target_id is not None and target_id not in records:
            return ScanInfo(errors=("unknown_scan_target",))
        x, y, w, h = roi
        crop = frame[y : y + h, x : x + w, :3]
        page_key = hashlib.sha256(
            cv2.resize(
                crop, (min(w, 128), min(h, 72)), interpolation=cv2.INTER_AREA
            ).tobytes()
        ).hexdigest()
        rectangles = _rectangles(frame, roi)
        cells = [
            box for box in rectangles if 40 <= box[2] <= 160 and 40 <= box[3] <= 180
        ]
        # 包含关系表示内框/图标，而非两个可见槽位。只保留外层卡片。
        cells = [
            box
            for box in cells
            if not any(
                box != other and _inside(box, other, margin=1) for other in cells
            )
        ]
        count_hit = _one_text(self._texts("GiftVisibleCountOCR", frame))
        count_pattern = (
            r"(?:当前显示|當前顯示)(0|[1-9][0-9]{0,2})(?:件礼物|件禮物)"
            if kind == "gifts"
            else r"(?:当前显示|當前顯示)(0|[1-9][0-9]{0,2})名角色"
        )
        count_match = (
            re.fullmatch(count_pattern, normalize_text(count_hit.text))
            if count_hit
            else None
        )
        visible_count = int(count_match[1]) if count_match else None
        empty = _one_text(
            self._texts("GiftEmptyListOCR", frame, roi),
            {"暂无礼物", "暫無禮物"} if kind == "gifts" else {"暂无角色", "暫無角色"},
        )
        if empty:
            if cells or visible_count not in (None, 0):
                return ScanInfo(
                    category_verified=True,
                    page_key=page_key,
                    errors=("empty_list_conflicts_with_visible_cells",),
                )
            return ScanInfo(
                category_verified=True,
                start_verified=True,
                end_verified=True,
                page_key=page_key,
                progress_evidence=("explicit_empty_list",),
                single_page_verified=True,
                absence_verified=target_id is not None,
            )
        if not cells:
            return ScanInfo(
                category_verified=True,
                page_key=page_key,
                errors=("visible_list_bounds_unverified",),
            )
        # 局部轮廓检测会漏格；只有显式当前可见数量与完整卡片数相等才证明覆盖。
        # 这个标签同样未实机校准，缺失时拒绝给出完整缺货证明。
        coverage_verified = visible_count == len(cells)
        errors = (
            [] if coverage_verified else ["visible_count_or_complete_grid_unverified"]
        )
        visible = []
        all_identified = True
        for cell in cells:
            name = unique_name(self._texts("GiftVisibleNameOCR", frame, cell), records)
            art = self._candidates(frame, name[0], kind, cell) if name else []
            if name is None or len(art) != 1 or not _inside(art[0], cell):
                all_identified = False
            else:
                visible.append(name[0])
        pair = _scrollbar(rectangles, cells)
        start = end = single_page = False
        scroll_box = None
        progress = []
        if pair:
            track, thumb = pair
            start = abs(thumb[1] - track[1]) <= 3
            end = abs(thumb[1] + thumb[3] - track[1] - track[3]) <= 3
            progress.append(
                "scrollbar:%d:%d:%d:%d" % (track[1], track[3], thumb[1], thumb[3])
            )
            a, b = min(box[0] for box in cells), min(box[1] for box in cells)
            c, d = max(box[0] + box[2] for box in cells), max(
                box[1] + box[3] for box in cells
            )
            scroll_box = [a, b, c - a, d - b]
        pagination = _one_text(self._texts("GiftPaginationOCR", frame))
        # 无滚动条不是“仅一页”；只有完整页码才可提供额外的边界证据。
        if pagination:
            match = re.fullmatch(
                r"第([1-9][0-9]{0,3})页/共([1-9][0-9]{0,3})页",
                normalize_text(pagination.text),
            )
            if match and int(match[1]) <= int(match[2]):
                page_start, page_end = int(match[1]) == 1, match[1] == match[2]
                if pair and (start != page_start or end != page_end):
                    errors.append("pagination_scrollbar_conflict")
                    start = end = False
                    scroll_box = None
                else:
                    start, end = page_start, page_end
                    single_page = start and end
                progress.append("page:" + normalize_text(pagination.text))
        if not all_identified:
            errors.append("not_all_visible_cells_positively_identified")
        if not pair and not single_page:
            errors.append("scrollbar_boundaries_unverified")
        return ScanInfo(
            category_verified=True,
            start_verified=start,
            end_verified=end,
            page_key=page_key,
            progress_evidence=tuple(progress),
            single_page_verified=single_page,
            absence_verified=bool(
                target_id is not None
                and coverage_verified
                and not errors
                and all_identified
                and len(visible) == len(cells)
                and target_id not in visible
            ),
            scroll_box=scroll_box,
            visible_ids=tuple(visible),
            errors=tuple(errors),
        )
