"""战斗画面感知。

HUD / 当前槽位 / 黑屏阈值移植自 pinkpaw/pinkpaw_core3.py（已实机校准），坐标基于 1280x720，
按截图实际尺寸等比缩放。敌人血条、等级文字、撤离提示等走 Pipeline 节点，由 observe_frame
通过注入的 recognize 回调执行；find_health_bars 是 combat_enemy_bar 识别（CombatEnemyBarShape 节点）的实现，
需要 OpenCV，战斗内核直接调用它，不经 run_recognition。
未实机校准的部分：血条形状阈值、等级文字区域（参考 ok-NTE 换算到 720p）。
"""

from __future__ import annotations

from dataclasses import dataclass
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
    return [
        _roi_score(image, _slot_roi(SLOT_ROI, i), i) + SLOT_SCORE_BONUS[i]
        for i in range(4)
    ]


def core_scores(image: np.ndarray) -> list[int]:
    return [
        _roi_score(image, _slot_roi(SLOT_CORE_ROI, i), i) * SLOT_CORE_WEIGHT
        for i in range(4)
    ]


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


@dataclass(frozen=True)
class SignalConfig:
    """同帧识别的节点组；空组未配置，不作为已观察的阴性。"""

    enemy: tuple[str, ...] = ("CombatEnemyHealthBar",)  # 与 api.ENEMY_NODE 一致
    level: tuple[str, ...] = ()
    boss: tuple[str, ...] = ()
    retreat_prompt: tuple[str, ...] = ()
    caller: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def nodes(self, kind: str) -> tuple[str, ...]:
        return dict(self.caller).get(kind, ())


SIGNAL_KINDS = ("active", "cleared", "disengaged", "retreated", "defeated", "suspended")
_TERMINAL_KINDS = ("cleared", "disengaged", "retreated", "defeated")


def any_hit(recognize: Callable[[str], bool | None], names) -> bool | None:
    """任一命中即 True；全部明确未命中为 False；否则（含识别失败）为 None。未配置返回 None。"""
    if not names:
        return None
    unknown = False
    for name in names:
        hit = recognize(name)
        if hit is True:
            return True
        if hit is None:
            unknown = True
    return None if unknown else False


def observe_frame(
    image,
    recognize: Callable[[str, object], bool | None],
    signals: SignalConfig | None = None,
) -> Snapshot:
    """同一张图获取独立证据。HUD 隐藏仍识别撤离/终局；黑屏一律未知。

    血条阳性时省略等级 OCR；否则全部已配置的敌人证据均阴性才能判目标暂失。
    识别异常/缺失不冒充阴性，任一阳性（含调用方 active）优先。
    """
    bgr = as_bgr(image)
    if bgr is None:
        return INVALID_SNAPSHOT
    signals = signals or SignalConfig()
    cache: dict[str, bool | None] = {}

    def recognize_once(name):
        if name not in cache:
            try:
                cache[name] = recognize(name, image)
            except Exception:
                cache[name] = None
        return cache[name]

    def hit(names):
        return any_hit(recognize_once, names)

    black = is_black(bgr)
    hud = not black and in_hud(bgr)
    caller = {
        kind: None if black else hit(signals.nodes(kind)) for kind in SIGNAL_KINDS
    }
    retreat = (
        (None if black else hit(signals.retreat_prompt))
        if signals.retreat_prompt
        else False
    )
    slot = current_slot(bgr) if hud else 0
    health = hit(signals.enemy) if hud else None
    level = boss = None
    if hud and health is not True:
        level, boss = hit(signals.level), hit(signals.boss)
    hostile = [health]
    if signals.level:
        hostile.append(level)
    if signals.boss:
        hostile.append(boss)
    if signals.nodes("active"):
        hostile.append(caller["active"])
    enemy = (
        True
        if any(v is True for v in hostile)
        else False if all(v is False for v in hostile) else None
    )
    evidence = (
        ("hud", hud),
        ("health_bar", health),
        ("enemy_level", level),
        ("boss", boss),
        ("retreat_prompt", retreat),
        *((kind, caller[kind]) for kind in SIGNAL_KINDS if signals.nodes(kind)),
    )
    return Snapshot(
        at=0.0,
        valid=True,
        in_hud=hud,
        black=black,
        slot=slot,
        enemy=enemy,
        health_bar=health,
        enemy_level=level,
        boss=boss,
        active=caller["active"],
        cleared=caller["cleared"],
        disengaged=caller["disengaged"],
        retreated=caller["retreated"],
        defeated=caller["defeated"],
        suspended=caller["suspended"] if signals.nodes("suspended") else False,
        retreat_prompt=retreat,
        evidence=evidence,
    )


# ---- 敌人血条形状（combat_enemy_bar），与 CombatEnemyHealthBar 的精确颜色匹配互补 ----
# 颜色与区域参考 ok-NTE CombatCheck._find_red_health_bar（2560x1440，提交 893525d）换算到 720p：
# HSV S>=190、V>=175 后再取 R 210~255 / G 20~80 / B 20~100；区域去掉左侧 UI、底部自身血条与右侧队伍栏。
# 与上游不同：最小宽度从 50px 放宽到 6px，残血短条也能识别；但仍要求长宽比 >= 2.5 以排除红色粒子，
# 更短的残血条由精确颜色节点（count=3）和等级/Boss 证据覆盖。未在 MaaNTE 实机校准。
BAR_ROI = (126, 0, 1021, 644)
BAR_HSV_LOWER = (0, 190, 175)
BAR_HSV_UPPER = (179, 255, 255)
BAR_BGR_LOWER = (20, 20, 210)
BAR_BGR_UPPER = (100, 80, 255)
BAR_MIN_W, BAR_MAX_W = 6, 100
BAR_MIN_H, BAR_MAX_H = 2, 6
BAR_MIN_ASPECT = 2.5
BAR_MIN_FILL = 0.7


def find_health_bars(image) -> list[tuple[int, int, int, int]]:
    """返回疑似敌人血条的框（截图坐标）。只接受细长、填充饱满的红色连通块，排除大片红色场景。"""
    import cv2

    bgr = as_bgr(image)
    if bgr is None:
        return []
    x, y, w, h = scale_roi(BAR_ROI, bgr.shape)
    region = np.ascontiguousarray(bgr[y : y + h, x : x + w])
    if region.size == 0:
        return []
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, BAR_HSV_LOWER, BAR_HSV_UPPER) & cv2.inRange(
        region, BAR_BGR_LOWER, BAR_BGR_UPPER
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    sx, sy = bgr.shape[1] / BASE_WIDTH, bgr.shape[0] / BASE_HEIGHT
    boxes = []
    for i in range(1, count):
        bx, by, bw, bh, area = (int(v) for v in stats[i])
        w720, h720 = bw / sx, bh / sy
        if not (BAR_MIN_W <= w720 <= BAR_MAX_W and BAR_MIN_H <= h720 <= BAR_MAX_H):
            continue
        if w720 / max(h720, 1e-6) < BAR_MIN_ASPECT:
            continue
        if area / float(bw * bh) < BAR_MIN_FILL:
            continue
        boxes.append((x + bx, y + by, bw, bh))
    return boxes
