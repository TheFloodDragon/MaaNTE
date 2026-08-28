"""把 ``Step`` 翻译成 MAA 调用。

这一层刻意做得很薄，理由与 ``Combat/script/primitives.py`` 相同：
``runner`` 负责「按什么顺序做、失败怎么办」（纯逻辑，可离线测试），
本模块只负责「怎么调 MAA」（需要真实环境）。

于是 runner 的测试用一个假 executor 就能跑完整刷本循环，不需要游戏。

## 识别失败的处理

识别类步骤会在 ``timeout`` 内按 ``interval`` 轮询重试。这不是为了「多试
几次总能成」，而是因为界面切换有动画和加载：一次没中不代表位置错了。
超时仍未命中时返回 ``False``，由 runner 按 ``required`` 决定是失败还是跳过。

## ROI 的分辨率适配

配置里的 ROI 一律按 720p 标定（与仓库其它资源一致）。真机截图不一定正好
是 1280x720：窗口大小、云游戏缩放都会变。所以识别前统一用内核的
``scale_roi`` 把 ROI 映射到当前截图尺寸——否则在 1080p 窗口下每个 ROI
都会偏到左上角，而日志只会显示「没识别到」，根本看不出是缩放问题。
"""

from __future__ import annotations

import time

# 探测循环的轮数硬上限。它不是业务参数，而是防死循环的保险：
# ``timeout`` 只在时钟真的推进时有效，而「等待被卡住/被替换掉」是真实故障，
# 那种情况下没有这道上限，角色会一直按着前进键跑下去。
MAX_SEARCH_ROUNDS = 300

try:
    from agent.custom.action.Combat.kernel.frames import scale_roi
    from agent.custom.action.Dungeon.config import (
        STEP_CLICK,
        STEP_HOLD,
        STEP_KEY,
        STEP_NODE,
        STEP_OCR,
        STEP_SEARCH,
        STEP_TEMPLATE,
        STEP_WAIT,
    )
except ImportError:  # pragma: no cover
    from ..Combat.kernel.frames import scale_roi
    from .config import (
        STEP_CLICK,
        STEP_HOLD,
        STEP_KEY,
        STEP_NODE,
        STEP_OCR,
        STEP_SEARCH,
        STEP_TEMPLATE,
        STEP_WAIT,
    )


class StepExecutor:
    """用 MAA context + 战斗内核执行步骤。

    复用 ``CombatKernel`` 而不是自己写按键/截图：内核的按键时序、可中断
    等待、停止检查都是已验证的，重写一遍只会多一处 bug 来源。
    """

    def __init__(self, kernel, context, log=None, template_dir=None, clock=None):
        self.kernel = kernel
        self.ctx = context
        self._log = log or (lambda *a: None)
        self._template_dir = template_dir
        # 时钟可注入，好让「探测循环到底转了几轮、什么时候算超时」能离线断言。
        # 用真实时钟测超时要么很慢，要么只能把 timeout 调到没有意义的小值。
        self._clock = clock or time.monotonic

    # ------------------------------------------------------------------
    # 识别
    # ------------------------------------------------------------------

    def _recognize_ocr(self, step, image):
        """跑一次 OCR。用临时节点而不是 run_recognition_direct，
        与仓库里其它任务保持一致，也让 pipeline_override 的语义可见。
        """
        node = "__DungeonFarmOCR"
        override = {
            node: {
                "recognition": {
                    "type": "OCR",
                    "param": {
                        "roi": self._roi(step.roi, image),
                        "expected": list(step.text),
                        "threshold": step.threshold,
                    },
                },
                "action": {"type": "DoNothing"},
            }
        }
        return self.ctx.run_recognition(node, image, pipeline_override=override)

    def _recognize_template(self, step, image):
        node = "__DungeonFarmTemplate"
        override = {
            node: {
                "recognition": {
                    "type": "TemplateMatch",
                    "param": {
                        "roi": self._roi(step.roi, image),
                        "template": [step.path],
                        "threshold": step.threshold,
                        "green_mask": True,
                    },
                },
                "action": {"type": "DoNothing"},
            }
        }
        return self.ctx.run_recognition(node, image, pipeline_override=override)

    def _roi(self, roi, image):
        """把 720p 基准 ROI 换算到当前截图尺寸。

        截图正好是 1280x720 时 ``scale_roi`` 是恒等变换，所以这层没有代价；
        窗口尺寸不同时它是唯一能让 ROI 落对位置的地方。
        """
        try:
            return [int(v) for v in scale_roi(list(roi), image)]
        except Exception as exc:
            # 缺 numpy 之类的降级路径：宁可用原始 ROI 试一次，也不要整步崩掉。
            self._log(f"ROI 缩放失败，按原值使用 {list(roi)}: {exc}")
            return [int(v) for v in roi]

    def recognize(self, step):
        """做一次识别，命中返回 box（或 True），未命中返回 None。"""
        image = self.kernel.screencap()
        if image is None:
            return None

        if step.type == STEP_NODE:
            result = self.ctx.run_recognition(step.node, image)
            return _hit_box(result)

        if step.type == STEP_OCR:
            return _hit_box(self._recognize_ocr(step, image))

        if step.type == STEP_TEMPLATE:
            return _hit_box(self._recognize_template(step, image))

        return None

    def wait_recognize(self, step):
        """在 timeout 内轮询识别，返回命中的 box 或 None。"""
        deadline = self._clock() + max(step.timeout, 0.0)
        attempt = 0
        while True:
            self.kernel.ah.raise_if_stopped()
            attempt += 1
            try:
                box = self.recognize(step)
            except Exception as exc:
                self._log(f"识别异常（{step.describe()}）: {exc}")
                box = None
            if box is not None:
                return box
            if self._clock() >= deadline:
                return None
            self.kernel.sleep(step.interval, allow_slow_poll=False, scaled=False)

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def click_box(self, box):
        """点击识别结果的中心。"""
        x, y, w, h = box
        self.kernel.click(int(x + w / 2), int(y + h / 2))
        return True

    def click_rect(self, roi):
        """点击配置里写死的 720p 矩形中心。

        与 ``click_box`` 分开是因为来源不同：识别结果已经是当前截图坐标系，
        而配置里的矩形是 720p 基准，必须先换算——否则在非 720p 窗口下会
        点到别的地方，而且看不出任何报错。
        """
        image = self.kernel.screencap()
        target = list(roi)
        if image is not None:
            target = self._roi(roi, image)
        return self.click_box(target)

    def hold(self, step) -> bool:
        """按住一个键固定时长，用于「前进一小段」。

        ``finally`` 里必须松手：探测循环里如果某次等待被停止异常打断而键还
        按着，角色会一直往前走，直到撞墙或掉下去——这类问题在日志里看不到，
        只能在游戏里看到人物发疯。
        """
        self.kernel.send_key_down(step.key)
        try:
            self.kernel.sleep(step.duration, allow_slow_poll=False, scaled=False)
        finally:
            self.kernel.send_key_up(step.key)
        return True

    def run_search(self, step) -> bool:
        """反复执行 ``probe`` 直到 ``until`` 命中，命中后跑 ``on_found``。

        先看一眼再动手：目标可能已经满足（比如战斗刚结束奖励弹窗就自己弹
        出来了），这时候不该先莽一步走开。

        除了 ``timeout``，还有一道轮数上限。时间上限依赖时钟真的在走，而
        「时钟没推进」是一种真实故障（等待被 mock、控制器卡住），这时只靠
        超时判断会让角色一直往前跑。轮数上限保证循环一定会结束。
        """
        deadline = self._clock() + max(step.timeout, 0.0)
        rounds = 0
        while True:
            self.kernel.ah.raise_if_stopped()
            try:
                box = self.recognize(step.until)
            except Exception as exc:
                self._log(f"探测识别异常（{step.until.describe()}）: {exc}")
                box = None

            if box is not None:
                self._log(
                    f"探测命中（第 {rounds} 轮）: {step.until.describe()}"
                )
                return self._run_sequence(step.on_found, f"{step.describe()}.on_found")

            if self._clock() >= deadline:
                self._log(
                    f"探测超时 {step.timeout}s（{rounds} 轮）未命中: "
                    f"{step.until.describe()}"
                )
                return False
            if rounds >= MAX_SEARCH_ROUNDS:
                self._log(
                    f"探测已跑 {rounds} 轮仍未命中且未到超时，判为失败: "
                    f"{step.until.describe()}"
                    "（时钟没有推进，检查等待是否被卡住）"
                )
                return False

            rounds += 1
            if not self._run_sequence(step.probe, f"{step.describe()}.probe"):
                return False
            if (
                step.sweep
                and step.sweep_every > 0
                and rounds % step.sweep_every == 0
            ):
                self._log(f"探测第 {rounds} 轮仍未命中，执行转向动作")
                if not self._run_sequence(step.sweep, f"{step.describe()}.sweep"):
                    return False

    def _run_sequence(self, steps, where: str) -> bool:
        """跑一段子步骤序列，语义与 runner 的阶段执行一致。"""
        for index, item in enumerate(steps):
            self.kernel.ah.raise_if_stopped()
            if self.run_step(item):
                continue
            if item.required:
                self._log(f"[{where}] 第 {index + 1} 步失败: {item.describe()}")
                return False
            self._log(f"[{where}] 可选步骤未命中，跳过: {item.describe()}")
        return True

    def run_step(self, step) -> bool:
        """执行一个步骤，返回是否成功。

        对识别类步骤，"成功" = 命中（且按需点击）。
        """
        if step.type == STEP_WAIT:
            self.kernel.sleep(step.duration, allow_slow_poll=False, scaled=False)
            return True

        if step.type == STEP_KEY:
            self.kernel.send_key(step.key)
            self._after(step)
            return True

        if step.type == STEP_HOLD:
            self.hold(step)
            self._after(step)
            return True

        if step.type == STEP_SEARCH:
            if not self.run_search(step):
                return False
            self._after(step)
            return True

        if step.type == STEP_CLICK:
            self.click_rect(step.roi)
            self._after(step)
            return True

        if step.type == STEP_NODE:
            # 节点步骤直接交给 pipeline 跑（它自带识别与动作）
            self.kernel.ah.raise_if_stopped()
            result = self.ctx.run_task(step.node)
            self._after(step)
            return result is not None

        # OCR / 模板：等命中，再按需点击
        box = self.wait_recognize(step)
        if box is None:
            return False
        if step.click:
            if isinstance(box, (list, tuple)) and len(box) == 4:
                self.click_box(box)
            else:
                # 命中了但拿不到坐标：不能瞎点。这里必须报失败而不是静默成功，
                # 否则流程会在"以为点过了"的前提下继续，后面一连串步骤都会错，
                # 而日志里看不出真正的起因。
                self._log(
                    f"步骤要求点击但识别结果没有坐标，判为失败: {step.describe()}"
                )
                return False
        self._after(step)
        return True

    def _after(self, step):
        if step.post_wait > 0:
            self.kernel.sleep(
                step.post_wait, allow_slow_poll=False, scaled=False
            )


def _hit_box(result):
    """从 MAA 识别结果里取 box；未命中返回 None。

    命中但拿不到 box 时返回 ``True``——调用方据此知道"中了但不知道在哪"，
    不会去点一个瞎猜的位置。
    """
    if result is None:
        return None

    hit = getattr(result, "hit", None)
    if hit is None:
        status = getattr(result, "status", None)
        succeeded = getattr(status, "succeeded", None)
        if succeeded is not None:
            hit = bool(succeeded)
        elif status is not None:
            hit = status == 0
        else:
            hit = True
    if not hit:
        return None

    box = getattr(result, "box", None)
    rect = _as_rect(box)
    if rect is not None:
        return rect

    best = getattr(result, "best_result", None)
    rect = _as_rect(getattr(best, "box", None))
    if rect is not None:
        return rect

    for attr in ("filtered_results", "all_results"):
        for item in getattr(result, attr, None) or []:
            rect = _as_rect(getattr(item, "box", None))
            if rect is not None:
                return rect

    return True


def _as_rect(box):
    if box is None:
        return None
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        try:
            return [int(box[0]), int(box[1]), int(box[2]), int(box[3])]
        except (TypeError, ValueError):
            return None
    if all(hasattr(box, attr) for attr in ("x", "y", "w", "h")):
        try:
            return [int(box.x), int(box.y), int(box.w), int(box.h)]
        except (TypeError, ValueError):
            return None
    return None
