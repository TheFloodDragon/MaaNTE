"""赠礼的单帧视觉证据；不截图、不点击、不等待，也不隐式导航。

按游戏真实界面识别：计数按钮「赠送n/3」（合并或拆分 OCR 均可）、名称框里的
完整角色名、点选礼物后「当前选择」处的完整礼物名、选中格左上角的 ∞ 徽记。
Wiki 图标只用于定位礼物格候选，礼物身份以「当前选择」名称为准。
布局常量取自 ok-nte 实机跑通的版本，待本项目实机复核。
"""

from __future__ import annotations

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
# 以下布局取自 ok-nte 实机跑通的 GiftTask/layout（换算到 1280x720），待本项目实机复核。
GRID_FIRST = [682, 345, 66, 27]
GRID_COLUMNS, GRID_ROWS = 5, 2
GRID_STEP_X, GRID_STEP_Y = 83.3, 97.3
GRID_ROI = [660, 280, 450, 215]
GRID_SCROLL_POINT = (880, 410)
SIDEBAR_X = 1211
SIDEBAR_SLOTS_Y = (127, 235, 340, 449, 556)
SIDEBAR_COMPARE = [1198, 105, 45, 43]
SIDEBAR_SCROLL_POINT = (1212, 360)
# 灰度小图逐像素平均差低于该值视为“画面未变化”（滚动到底）。
REGION_SAME_THRESHOLD = 4.0
# 灰度标准差低于该值视为空槽位/空格子。
BLANK_STD = 6.0


def grid_cells() -> list[Box]:
    x, y, w, h = GRID_FIRST
    return [
        [x + round(column * GRID_STEP_X), y + round(row * GRID_STEP_Y), w, h]
        for row in range(GRID_ROWS)
        for column in range(GRID_COLUMNS)
    ]


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
    # 同帧提示「【角色】的好感度提升了N点。」中点名的角色 ID（不占次数礼物的回执证据）。
    affection: str | None = None
    # 游戏限制提示："character"＝对方今日无法接受更多/需提升羁遇等级。
    limit_prompt: str | None = None
    send_box: Box | None = None
    controls: dict[str, Box] = field(default_factory=dict)
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
    # 游戏按钮文字为「赠送{n}/{3}」（ST_Common:ui_likeability_gift_times）；
    # OCR 可能整串返回，也可能只返回 n/3。仍拒绝 13/3、3/4 等截断或越界值。
    match = re.fullmatch(r"(?:赠送|贈送)?([0-3])/3", normalize_text(text))
    return (int(match[1]), 3) if match else None


def quota_hit(hits: list[TextHit]) -> tuple[tuple[int, int], Box] | None:
    """计数区：合并「赠送n/3」或拆分「赠送」+「n/3」都接受，返回额度与按钮框。"""
    parsed = [
        (parse_quota(hit.text), hit)
        for hit in hits
        if math.isfinite(hit.score) and hit.score >= OCR_MIN_SCORE
    ]
    parsed = [(quota, hit) for quota, hit in parsed if quota is not None]
    if len(parsed) != 1:
        return None
    quota, hit = parsed[0]
    boxes = [hit.box] + [
        other.box
        for other in hits
        if normalize_text(other.text) in {"赠送", "贈送"}
        and math.isfinite(other.score)
        and other.score >= OCR_MIN_SCORE
    ]
    x = min(box[0] for box in boxes)
    y = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    return quota, [x, y, right - x, bottom - y]


def name_in_box(
    hits: list[TextHit], records: dict[str, dict]
) -> tuple[str, Box] | None:
    """名称框里可能还有等级、称号等文字：恰好一个高分结果是完整目录名才算识别。"""
    found = {}
    for hit in hits:
        if not math.isfinite(hit.score) or hit.score < OCR_MIN_SCORE:
            continue
        result = unique_name([hit], records)
        if result is not None:
            found.setdefault(result[0], result[1])
    return next(iter(found.items())) if len(found) == 1 else None


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


def _validate_frame(frame: np.ndarray) -> None:
    if (
        not isinstance(frame, np.ndarray)
        or frame.dtype != np.uint8
        or frame.shape not in ((720, 1280, 3), (720, 1280, 4))
    ):
        raise VisionError("unsupported_frame_size_or_format")


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
            {"今日赠礼次数已用完", "今日贈禮次數已用完"},
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
        if gift_tab is None:
            return "unknown", {}
        controls["gift_tab"] = gift_tab
        # 赠礼页签内只有一个「赠送n/3」按钮：合并或拆分 OCR 都能判成 gifts。
        quota = quota_hit(self._texts("GiftQuotaOCR", frame))
        if quota is not None:
            controls["send"] = quota[1]
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
        # 游戏没有「已选择」侧栏标记：名称框里恰好一个完整角色名即为当前角色。
        identity = name_in_box(
            self._texts("GiftCharacterNameOCR", frame), self.catalog.characters
        )
        character_id = identity[0] if identity else None
        if character_id is None:
            errors.append("character_identity_unverified")
        limit_prompt = self._limit_prompt(frame)
        if page != "gifts":
            return FrameView(
                page=page,
                character_id=character_id,
                identity_verified=character_id is not None,
                limit_prompt=limit_prompt,
                controls=controls,
                errors=tuple(errors),
            )
        quota = quota_hit(self._texts("GiftQuotaOCR", frame))
        if quota is None:
            errors.append("quota_unverified")
        # 点选礼物后「当前选择」处显示完整名称；这是礼物身份的唯一授权证据。
        selection = name_in_box(
            self._texts("GiftCurrentSelectionOCR", frame), self.catalog.gifts
        )
        gift_id = selection[0] if selection else None
        if gift_id is None:
            errors.append("gift_selection_unverified")
        # ∞ 徽记只对选中格检测（infinite_at），整片礼物格里有别的不限量礼物不影响。
        infinite = False
        affection = self._affection(frame)
        return FrameView(
            page=page,
            character_id=character_id,
            identity_verified=character_id is not None,
            gift_id=gift_id,
            gift_verified=gift_id is not None,
            selected=gift_id is not None,
            remaining=quota[0][0] if quota else None,
            total=quota[0][1] if quota else None,
            infinite=infinite,
            affection=affection,
            limit_prompt=limit_prompt,
            send_box=quota[1] if quota else None,
            controls=controls,
            errors=tuple(errors),
        )

    def _affection(self, frame: np.ndarray) -> str | None:
        """提示「【{角色}】的好感度提升了{n}点。」；返回其中点名的唯一角色 ID。"""
        named = set()
        for hit in self._texts("GiftAffectionOCR", frame):
            if not math.isfinite(hit.score) or hit.score < OCR_MIN_SCORE:
                continue
            match = re.fullmatch(
                r"【(.+)】的好感度提升了[1-9][0-9]*点。?", normalize_text(hit.text)
            )
            if match is None:
                continue
            result = unique_name(
                [TextHit(match[1], hit.score, hit.box)], self.catalog.characters
            )
            named.add(result[0] if result else None)
        return next(iter(named)) if len(named) == 1 else None

    def _limit_prompt(self, frame: np.ndarray) -> str | None:
        """单角色/羁遇等级限制提示返回 character；全角色次数用完由 _page 判为 global_limit。"""
        texts = {
            normalize_text(hit.text)
            for hit in self._texts("GiftLimitPromptOCR", frame)
            if math.isfinite(hit.score) and hit.score >= OCR_MIN_SCORE
        }
        if texts & {
            "对方今日无法接受更多的礼物",
            "對方今日無法接受更多的禮物",
            "提升羁遇等级后才能继续赠送礼物",
            "提升羈遇等級後才能繼續贈送禮物",
        }:
            return "character"
        return None

    def gift_candidates(self, frame: np.ndarray, gift_id: str) -> list[Box]:
        """有 Wiki 图标的礼物在礼物格区域内做多尺度匹配；无图标返回空列表。"""
        _validate_frame(frame)
        record = self.catalog.gifts.get(gift_id)
        if record is None or not record.get("image"):
            return []
        return self._candidates(frame, gift_id, "gifts", GRID_ROI)

    def infinite_at(self, frame: np.ndarray, cell: Box) -> bool:
        """只在指定礼物格左上角的徽记区检测 ∞，不受其他格子影响。"""
        _validate_frame(frame)
        detail = self._recognize("GiftInfiniteMarker", frame, badge_box(cell))
        return bool(detail is not None and _get(detail, "hit", False))

    # —— 侧栏与礼物格几何；签名只回答“两帧同一区域是否相同”，不授权任何身份。——
    @staticmethod
    def sidebar_slots() -> list[Box]:
        return [slot_box(y) for y in SIDEBAR_SLOTS_Y]

    @staticmethod
    def sidebar_scroll_point() -> tuple[int, int]:
        return SIDEBAR_SCROLL_POINT

    @staticmethod
    def sidebar_signature(frame: np.ndarray) -> np.ndarray:
        return region(frame, SIDEBAR_COMPARE)

    @staticmethod
    def slot_signature(frame: np.ndarray, index: int) -> np.ndarray | None:
        signature = region(frame, slot_box(SIDEBAR_SLOTS_Y[index]))
        return None if blank(signature) else signature

    @staticmethod
    def grid_cells() -> list[Box]:
        return grid_cells()

    @staticmethod
    def grid_columns() -> int:
        return GRID_COLUMNS

    @staticmethod
    def cell_index(box: Box) -> int | None:
        return cell_at(box)

    @staticmethod
    def grid_scroll_point() -> tuple[int, int]:
        return GRID_SCROLL_POINT

    @staticmethod
    def grid_signature(frame: np.ndarray) -> np.ndarray:
        return region(frame, GRID_ROI)

    @staticmethod
    def cell_signature(frame: np.ndarray, index: int) -> np.ndarray | None:
        signature = region(frame, grid_cells()[index])
        return None if blank(signature) else signature

    @staticmethod
    def same(before: np.ndarray | None, after: np.ndarray | None) -> bool:
        return same_region(before, after)


def slot_box(y: int) -> Box:
    """侧栏头像比较区；与 ok-nte 侧栏比较框同尺寸，按槽位纵坐标平移。"""
    x, top, w, h = SIDEBAR_COMPARE
    return [x, y - (SIDEBAR_SLOTS_Y[0] - top), w, h]


def cell_at(box: Box) -> int | None:
    """把图标匹配框映射到礼物格序号；中心离任何格子超过半个步长则不映射。"""
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    best = None
    for index, (x, y, w, h) in enumerate(grid_cells()):
        dx, dy = abs(cx - (x + w / 2)), abs(cy - (y + h / 2))
        if dx <= GRID_STEP_X / 2 and dy <= GRID_STEP_Y / 2:
            if best is None or dx + dy < best[0]:
                best = (dx + dy, index)
    return None if best is None else best[1]


def badge_box(cell: Box) -> Box:
    """ok-nte 实测：徽记区 = 格子左移 10% 宽、上移 74% 高、宽度减 45%。"""
    x, y, w, h = cell
    left = max(0, x - round(w * 0.10))
    top = max(0, y - round(h * 0.74))
    return [left, top, max(2, w - round(w * 0.45)), h]


def region(frame: np.ndarray, box: Box) -> np.ndarray:
    """取灰度小图用于“滚动后画面是否变化”的比较；JPEG 噪声下不能用哈希。"""
    _validate_frame(frame)
    x, y, w, h = box
    gray = cv2.cvtColor(frame[y : y + h, x : x + w, :3], cv2.COLOR_BGR2GRAY)
    size = (max(8, w // 4), max(8, h // 4))
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA)


def blank(signature: np.ndarray) -> bool:
    """近乎单色的格子/槽位视为空位，不点击。"""
    return float(np.std(signature)) < BLANK_STD


def same_region(before: np.ndarray | None, after: np.ndarray | None) -> bool:
    if before is None or after is None or before.shape != after.shape:
        return False
    diff = np.abs(before.astype(np.int16) - after.astype(np.int16))
    return float(np.mean(diff)) < REGION_SAME_THRESHOLD
