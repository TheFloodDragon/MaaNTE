"""战斗画面感知（纯 numpy）。

阈值移植自 pinkpaw/pinkpaw_core3.py（已在实机校准），坐标基于 1280x720，
按截图实际尺寸等比缩放。敌人血条检测走 Pipeline 节点 CombatEnemyVisible，不在本模块。
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from .model import INVALID_SNAPSHOT, Snapshot

BASE_WIDTH = 1280
BASE_HEIGHT = 720

# 底部血条斜杠附近：亮且低饱和的像素足够多 → 处于可操作的战斗/大世界界面
HUD_ROI = (620, 654, 95, 42)
HUD_BRIGHT_MIN = 175
HUD_SAT_MAX = 95
HUD_MIN_PIXELS = 10

# 右侧队伍栏：当前角色槽位会高亮，槽位间距 88px
SLOT_ROI = (1168, 164, 68, 36)
SLOT_CORE_ROI = (1176, 172, 38, 16)
SLOT_SPACING = 88
SLOT_WHITE_THRESHOLDS = (205, 188, 205, 205)
SLOT_COLORED_THRESHOLDS = (170, 145, 170, 170)
SLOT_SCORE_BONUS = (0, 4, 0, 0)
SLOT_MIN_SCORE = (16, 12, 16, 16)
SLOT_MIN_MARGIN = (5, 2, 5, 5)
SLOT_CORE_WEIGHT = 3
SLOT2_CORE_MIN_SCORE = 6  # 二号位头像偏暗，用核心区兜底
SLOT2_CORE_MIN_MARGIN = 2

# 黑屏/加载：整体很暗且亮像素很少
BLACK_MEAN_MAX = 18
BLACK_BRIGHT_LEVEL = 80
BLACK_BRIGHT_MAX_COUNT = 300


def as_bgr(image) -> np.ndarray | None:
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] < 3:
        return None
    if image.size == 0:
        return None
    return image[:, :, :3]


def scale_roi(roi, shape) -> tuple[int, int, int, int]:
    height, width = shape[:2]
    sx, sy = width / BASE_WIDTH, height / BASE_HEIGHT
    x, y, w, h = roi
    return (
        int(round(x * sx)),
        int(round(y * sy)),
        max(1, int(round(w * sx))),
        max(1, int(round(h * sy))),
    )


def crop(image: np.ndarray, roi) -> np.ndarray | None:
    x, y, w, h = scale_roi(roi, image.shape)
    ih, iw = image.shape[:2]
    x1, y1 = max(0, min(iw, x)), max(0, min(ih, y))
    x2, y2 = max(x1, min(iw, x + w)), max(y1, min(ih, y + h))
    if x2 <= x1 or y2 <= y1:
        return None
    return image[y1:y2, x1:x2]


def _max_and_sat(region: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    region = region.astype(np.int16)
    max_ch = region.max(axis=2)
    return max_ch, max_ch - region.min(axis=2)


def in_hud(image: np.ndarray) -> bool:
    region = crop(image, HUD_ROI)
    if region is None:
        return False
    max_ch, sat = _max_and_sat(region)
    bright = (max_ch >= HUD_BRIGHT_MIN) & (sat <= HUD_SAT_MAX)
    return int(bright.sum()) >= HUD_MIN_PIXELS


def is_black(image: np.ndarray) -> bool:
    step = max(1, int(round(8 * image.shape[1] / BASE_WIDTH)))
    sample = image[::step, ::step]
    if sample.size == 0:
        return False
    max_ch = sample.max(axis=2)
    return (
        float(max_ch.mean()) <= BLACK_MEAN_MAX
        and int((max_ch >= BLACK_BRIGHT_LEVEL).sum()) <= BLACK_BRIGHT_MAX_COUNT
    )


def _slot_roi(base, index: int):
    x, y, w, h = base
    return (x, y + SLOT_SPACING * index, w, h)


def _roi_score(image: np.ndarray, roi, index: int) -> int:
    region = crop(image, roi)
    if region is None:
        return 0
    max_ch, sat = _max_and_sat(region)
    white = (max_ch >= SLOT_WHITE_THRESHOLDS[index]) & (sat <= 65)
    colored = (max_ch >= SLOT_COLORED_THRESHOLDS[index]) & (sat >= 55)
    return int((white | colored).sum())


def slot_scores(image: np.ndarray) -> list[int]:
    return [_roi_score(image, _slot_roi(SLOT_ROI, i), i) + SLOT_SCORE_BONUS[i] for i in range(4)]


def core_scores(image: np.ndarray) -> list[int]:
    return [_roi_score(image, _slot_roi(SLOT_CORE_ROI, i), i) * SLOT_CORE_WEIGHT for i in range(4)]


def _accepted(scores, index: int, min_score, min_margin) -> bool:
    best_other = max(s for i, s in enumerate(scores) if i != index)
    return scores[index] >= min_score and scores[index] - best_other >= min_margin


def current_slot(image: np.ndarray) -> int:
    """返回当前高亮的槽位（1-4），无法可靠判断时返回 0。"""
    scores = slot_scores(image)
    best = max(range(4), key=lambda i: scores[i])
    if _accepted(scores, best, SLOT_MIN_SCORE[best], SLOT_MIN_MARGIN[best]):
        return best + 1
    if _accepted(core_scores(image), 1, SLOT2_CORE_MIN_SCORE, SLOT2_CORE_MIN_MARGIN):
        return 2
    return 0


def observe_frame(image, detect_enemy: Callable[[np.ndarray], bool | None]) -> Snapshot:
    """把一帧截图转换为 Snapshot。HUD 不可见时不判断槽位和敌人，避免菜单/动画误判。"""
    bgr = as_bgr(image)
    if bgr is None:
        return INVALID_SNAPSHOT
    black = is_black(bgr)
    hud = (not black) and in_hud(bgr)
    slot = current_slot(bgr) if hud else 0
    enemy = detect_enemy(image) if hud else None
    return Snapshot(at=0.0, valid=True, in_hud=hud, black=black, slot=slot, enemy=enemy)
