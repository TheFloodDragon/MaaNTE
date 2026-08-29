"""把一帧截图变成 ``LauncherScreen``（启动器界面的 OCR 结果）。

单独成模块的原因：``enter.py`` 的状态机要能离线测试，不能依赖真实 OCR 模型。
所以状态机只接受一个 ``ocr_screen(image) -> LauncherScreen`` 回调，真实实现
放在这里，测试时换成造好的假数据。

## 为什么用 run_recognition_direct 而不是 pipeline 节点

启动器界面的文本是动态的（剩余时长、倒计时秒数），而 pipeline 的 OCR 节点靠
``expected`` 匹配固定文案，还要跟 5 个 locale 文件同步。这里需要的是「把这一帧
的所有文本连坐标读回来」，用 ``run_recognition_direct`` + ``JOCR`` 最直接，
也不引入 i18n 负担。仓库里 ``auto_tetris.py`` 用的是同一套做法。

## only_rec 的坑

``JOCR(only_rec=True)`` 是**跳过文本检测、把整个 ROI 当作一行去识别**。
对整屏用会返回一条低分垃圾（实测得到 ``"口"`` / 0.19）。要拿到画面里的多条
文本必须用 ``only_rec=False``（默认），让它先检测再逐块识别——实测整屏能
稳定读回 19 条，分数普遍 0.98~1.00。
"""

from __future__ import annotations

from .launcher_ui import LauncherScreen, TextHit

# 低于这个分数的 OCR 结果丢弃：实测有效文本普遍 ≥0.9，
# 而画面装饰、logo 残影会产生 0.2 左右的噪声条目。
MIN_SCORE = 0.6


def make_ocr_screen(context, logger=None):
    """返回一个 ``ocr_screen(image) -> LauncherScreen`` 回调。"""

    def ocr_screen(image) -> LauncherScreen:
        hits = _ocr_all_text(context, image, logger=logger)
        return LauncherScreen(hits=hits)

    return ocr_screen


def _ocr_all_text(context, image, logger=None) -> list[TextHit]:
    """整屏 OCR，返回所有文本条目。"""
    try:
        from maa.pipeline import JOCR, JRecognitionType
    except ImportError as exc:  # pragma: no cover - 仅在缺少 maafw 时触发
        if logger:
            logger(f"导入 OCR 类型失败: {exc}")
        return []

    height, width = image.shape[0], image.shape[1]
    try:
        detail = context.run_recognition_direct(
            JRecognitionType.OCR,
            # only_rec 必须是 False：True 会把整屏当一行读，得到垃圾结果
            JOCR(roi=[0, 0, width, height], only_rec=False),
            image,
        )
    except Exception as exc:
        if logger:
            logger(f"启动器界面 OCR 失败: {exc}")
        return []

    if detail is None:
        return []

    hits: list[TextHit] = []
    for item in getattr(detail, "all_results", None) or []:
        text = getattr(item, "text", "") or ""
        if not text.strip():
            continue
        score = float(getattr(item, "score", 0.0) or 0.0)
        if score < MIN_SCORE:
            continue
        box = getattr(item, "box", None)
        hits.append(TextHit(text=text, box=_normalize_box(box), score=score))
    return hits


def _normalize_box(box) -> tuple[int, int, int, int]:
    """box 可能是 ``[x, y, w, h]``，也可能是带属性的对象。"""
    if box is None:
        return (0, 0, 0, 0)
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    try:
        return (int(box.x), int(box.y), int(box.w), int(box.h))
    except Exception:
        return (0, 0, 0, 0)


__all__ = ["MIN_SCORE", "make_ocr_screen"]
