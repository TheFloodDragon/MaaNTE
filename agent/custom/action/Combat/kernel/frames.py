"""图像基础工具：截图转换、ROI 裁剪/缩放、快速颜色与模板匹配。

所有函数从 ``pinkpaw_core3`` 原样搬移，行为逐一对齐：

- ``as_bgr_image``      <- ``_as_bgr_image``
- ``crop_roi``          <- ``_crop_roi``
- ``scale_roi``         <- ``_scale_roi``
- ``fast_color_match``  <- ``_fast_color_match``
- ``fast_template_match`` <- ``_fast_template_match``
- ``TemplateCache``     <- ``_load_fast_template`` + ``_FAST_TEMPLATE_CACHE``

与 core3 的唯一结构性差异：模板目录由调用方注入，而不是硬编码
``PinkPawHeist``；粉爪侧传入原目录，所以取值不变。
"""

from __future__ import annotations

from pathlib import Path

from .constants import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    FAST_TEMPLATE_SAMPLE_LIMIT,
)

try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover - 运行环境缺依赖时降级
    np = None
    Image = None

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


def as_bgr_image(image):
    """把 MAA 截图转换为 OpenCV 使用的 BGR 三通道图。"""
    if np is None or not isinstance(image, np.ndarray):
        return None
    if image.ndim != 3 or image.shape[2] < 3 or image.size == 0:
        return None
    return image[:, :, :3]


def crop_roi(image, roi):
    """按给定坐标裁剪截图区域，并自动处理越界。"""
    bgr = as_bgr_image(image)
    if bgr is None:
        return None
    x, y, w, h = [int(v) for v in roi]
    ih, iw = bgr.shape[:2]
    x1 = max(0, min(iw, x))
    y1 = max(0, min(ih, y))
    x2 = max(x1, min(iw, x + w))
    y2 = max(y1, min(ih, y + h))
    if x2 <= x1 or y2 <= y1:
        return None
    return bgr[y1:y2, x1:x2]


def scale_roi(roi, image):
    """把以 1280x720 为基准的 ROI 缩放到当前截图尺寸。"""
    bgr = as_bgr_image(image)
    if bgr is None:
        return roi
    ih, iw = bgr.shape[:2]
    sx = iw / DEFAULT_WIDTH
    sy = ih / DEFAULT_HEIGHT
    x, y, w, h = roi
    return [
        int(round(x * sx)),
        int(round(y * sy)),
        max(1, int(round(w * sx))),
        max(1, int(round(h * sy))),
    ]


def crop_scaled_roi(image, roi):
    """先把 720p 基准 ROI 缩放到截图尺寸，再裁剪。"""
    return crop_roi(image, scale_roi(roi, image))


def fast_color_match(image, cfg):
    """在本地用 OpenCV 做颜色点数量检测，替代对应颜色识别节点。"""
    roi = crop_roi(image, cfg["roi"])
    if roi is None:
        return False
    stride = max(1, int(cfg.get("stride", 1)))
    if stride > 1:
        roi = roi[::stride, ::stride]
    lower = np.asarray(cfg["lower_bgr"], dtype=np.uint8)
    upper = np.asarray(cfg["upper_bgr"], dtype=np.uint8)
    mask = np.all((roi >= lower) & (roi <= upper), axis=2)
    count = max(1, int(cfg.get("count", 1)) // (stride * stride))
    return int(mask.sum()) >= count


class TemplateCache:
    """按目录缓存快速模板匹配用的预处理结果。

    预处理步骤与 ``pinkpaw_core3._load_fast_template`` 完全一致。
    """

    def __init__(self, image_dir):
        self._dir = Path(image_dir)
        self._cache: dict = {}

    @property
    def image_dir(self) -> Path:
        return self._dir

    def load(self, name):
        """读取并缓存 OpenCV 模板图，供快速模板匹配复用。"""
        if np is None or Image is None:
            return None
        if name in self._cache:
            return self._cache[name]
        path = self._dir / name
        if not path.exists():
            self._cache[name] = None
            return None
        rgba = np.asarray(Image.open(path).convert("RGBA"), dtype=np.uint8)
        alpha = rgba[:, :, 3]
        rgb = rgba[:, :, :3]
        brightness = rgb.max(axis=2)
        saturation = brightness - rgb.min(axis=2)
        mask = (alpha >= 128) & ((brightness >= 80) | (saturation >= 40))
        if int(mask.sum()) == 0:
            mask = alpha >= 128
        coords = np.argwhere(mask)
        if coords.size == 0:
            self._cache[name] = None
            return None
        if len(coords) > FAST_TEMPLATE_SAMPLE_LIMIT:
            scores = (
                brightness[coords[:, 0], coords[:, 1]].astype(np.int32)
                + saturation[coords[:, 0], coords[:, 1]].astype(np.int32) * 2
            )
            indices = np.argsort(scores)[-FAST_TEMPLATE_SAMPLE_LIMIT:]
            coords = coords[indices]
        gray = (
            rgb[:, :, 0].astype(np.float32) * 0.299
            + rgb[:, :, 1].astype(np.float32) * 0.587
            + rgb[:, :, 2].astype(np.float32) * 0.114
        )
        bgr = rgb[:, :, ::-1].astype(np.float32)
        cv_bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        cv_mask = np.ascontiguousarray((alpha >= 128).astype(np.uint8) * 255)
        values = gray[coords[:, 0], coords[:, 1]].astype(np.float32)
        bgr_values = bgr[coords[:, 0], coords[:, 1]].astype(np.float32)
        template = {
            "name": name,
            "cv_bgr": cv_bgr,
            "cv_mask": cv_mask,
            "coords": coords.astype(np.int32),
            "bgr_values": bgr_values,
            "height": gray.shape[0],
            "width": gray.shape[1],
        }
        anchor_index = int(np.argmax(values))
        anchor_bgr = bgr_values[anchor_index]
        template["anchor_y"] = int(template["coords"][anchor_index, 0])
        template["anchor_x"] = int(template["coords"][anchor_index, 1])
        template["anchor_channel"] = int(np.argmax(anchor_bgr))
        template["anchor_value"] = int(anchor_bgr[template["anchor_channel"]])
        self._cache[name] = template
        return template


def fast_template_match(image, cfg, cache: TemplateCache):
    """在本地用 OpenCV 做模板匹配，减少频繁调用 MAA 节点的延迟。"""
    if cv2 is None:
        return None
    roi = crop_roi(image, cfg["roi"])
    if roi is None:
        return None
    threshold = float(cfg.get("cv_threshold", cfg["threshold"]))
    roi = np.ascontiguousarray(roi)
    for name in cfg["templates"]:
        template = cache.load(name)
        if template is None:
            continue
        templ = template["cv_bgr"]
        mask = template["cv_mask"]
        if roi.shape[0] < templ.shape[0] or roi.shape[1] < templ.shape[1]:
            continue
        scores = cv2.matchTemplate(roi, templ, cv2.TM_CCORR_NORMED, mask=mask)
        finite_scores = scores[np.isfinite(scores)]
        if finite_scores.size == 0:
            continue
        best = float(np.max(finite_scores))
        if best >= threshold:
            return True
    return None


def is_hit(result) -> bool:
    """兼容 MAA 不同返回结构，统一判断识别或任务是否成功命中。"""
    if result is None:
        return False
    status = getattr(result, "status", None)
    succeeded = getattr(status, "succeeded", None)
    if succeeded is not None:
        return bool(succeeded)
    if status is not None:
        return status == 0
    return bool(getattr(result, "hit", True))


def screencap(ctx):
    """通过控制器截取当前游戏画面。"""
    controller = getattr(getattr(ctx, "tasker", None), "controller", None)
    if controller is None:
        return None
    return controller.post_screencap().wait().get()
