"""云异环启动器界面识别与操作。

## 为什么需要这一层

``ready.py`` 只回答「是否已经在游戏里」，中间过程一概不看。但实机的启动器
**不会自动进游戏**：

1. 启动后停在主页，要点右下角「开始游戏」；
2. 随后弹出「本次游戏将使用您的免费时长或计费时长」确认框，
   **左按钮「退出启动」带 30 秒倒计时**，超时就自动放弃启动；
3. 之后才是排队 / 串流加载 / 进入游戏。

也就是说，缺了这一层，`CloudGameLaunch` 只会干等到 `ready_timeout` 然后
报「未能进入游戏」，而真正原因是没人点按钮。

## 识别方式

启动器界面没有模板图，但 OCR 读得很干净（实测分数普遍 0.98~1.00），
所以这里全部走 OCR 文本匹配，不引入任何未经验证的模板与 ROI。

标定依据：``debug/cloudgame/023714_restored.png``（1280x720 实机截图）。
坐标不写死——每次都从当前帧的 OCR 结果里取 box，因此窗口尺寸变化也不受影响。

## 注意

- 所有文本匹配一律用**包含**而非相等：实测「付费时长：」前的图标会被 OCR
  读成 emoji（``😄付费时长：``），相等匹配必然失配。
- 剩余时长文本形如 ``11小时38分钟``；OCR 有时会在数字与单位间插空格，
  所以解析用正则抓数字，不按固定格式切分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# —— 界面文本锚点（实机 OCR 确认过的原文）——————————————————————

# 主页：右下角进入按钮
TEXT_START_GAME = "开始游戏"
# 主页：剩余时长区
TEXT_FREE_TIME = "免费时长"
TEXT_PAID_TIME = "付费时长"
# 主页：账号区，用于判断是否已登录
TEXT_UID = "UID"

# 确认弹窗（用户实机截图确认的原文）
TEXT_CONFIRM_TITLE = "本次游戏将使用您的免费时长或计费时长"
TEXT_CONFIRM_ENTER = "进入游戏"
TEXT_CONFIRM_QUIT = "退出启动"
TEXT_CONFIRM_MUTE = "不再提醒"

# 时长文本：`11小时38分钟` / `0 小时 0 分钟`（OCR 可能插空格）
_DURATION_RE = re.compile(r"(\d+)\s*小时\s*(\d+)\s*分钟")
# 倒计时：`退出启动 (29S)`
_COUNTDOWN_RE = re.compile(r"\((\d+)\s*[Ss]\)")


@dataclass
class TextHit:
    """一条 OCR 文本及其位置。"""

    text: str
    box: tuple[int, int, int, int]
    score: float

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.box
        return x + w // 2, y + h // 2


@dataclass
class LauncherScreen:
    """一帧启动器画面的 OCR 结果与派生判断。"""

    hits: list[TextHit] = field(default_factory=list)

    def find(self, needle: str) -> TextHit | None:
        """返回第一条**包含** ``needle`` 的文本。"""
        for hit in self.hits:
            if needle in hit.text:
                return hit
        return None

    def has(self, needle: str) -> bool:
        return self.find(needle) is not None

    @property
    def all_text(self) -> str:
        return " | ".join(hit.text for hit in self.hits)

    # —— 阶段判断 ————————————————————————————————

    @property
    def is_confirm_dialog(self) -> bool:
        """是否停在「将使用您的免费时长或计费时长」确认框。

        标题可能被 OCR 拆行，所以只要「进入游戏」和「退出启动」同时在，
        就足以判定——这两个按钮只在该弹窗出现。
        """
        if self.has(TEXT_CONFIRM_TITLE):
            return True
        return self.has(TEXT_CONFIRM_ENTER) and self.has(TEXT_CONFIRM_QUIT)

    @property
    def is_home(self) -> bool:
        """是否停在启动器主页（有「开始游戏」按钮且不是确认框）。"""
        return self.has(TEXT_START_GAME) and not self.is_confirm_dialog

    @property
    def is_logged_in(self) -> bool:
        """是否已登录。

        以 UID 可见为准。未登录页尚未实机采集过，所以这里只做**正向**判断，
        不反过来断言「没有 UID 就是未登录」——加载中的空白帧也没有 UID，
        那种情况该继续等而不是报未登录。
        """
        return self.has(TEXT_UID)

    @property
    def confirm_countdown(self) -> int | None:
        """确认框上「退出启动 (29S)」里的剩余秒数；读不到返回 ``None``。"""
        hit = self.find(TEXT_CONFIRM_QUIT)
        if hit is None:
            return None
        match = _COUNTDOWN_RE.search(hit.text)
        return int(match.group(1)) if match else None


def parse_duration_minutes(text: str) -> int | None:
    """把 ``11小时38分钟`` 解析成分钟数；解析不出返回 ``None``。"""
    match = _DURATION_RE.search(text or "")
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def read_playtime(screen: LauncherScreen) -> tuple[int | None, int | None]:
    """读「免费时长」与「付费时长」，返回 ``(免费分钟, 付费分钟)``。

    读不到的那项为 ``None``，调用方必须把 ``None`` 与 ``0`` 区别对待：
    ``0`` 是「确实没时长了」，``None`` 是「没看清」——后者不能当作时长耗尽
    去中止任务，否则 OCR 抖一下就误停。

    时长值与标签是分开的两条 OCR 文本（标签在上、值在下），靠**垂直距离最近**
    配对，不靠固定 ROI，这样窗口尺寸变化也不受影响。
    """
    free = _value_below(screen, TEXT_FREE_TIME)
    paid = _value_below(screen, TEXT_PAID_TIME)
    return free, paid


def _value_below(screen: LauncherScreen, label: str) -> int | None:
    """找到 ``label`` 下方最近的一条时长文本并解析成分钟。"""
    anchor = screen.find(label)
    if anchor is None:
        return None

    # 标签自己那条也可能连着值（OCR 偶尔合并），先就地试一次
    inline = parse_duration_minutes(anchor.text)
    if inline is not None:
        return inline

    ax, ay, _aw, ah = anchor.box
    best: tuple[int, int] | None = None  # (垂直距离, 分钟)
    for hit in screen.hits:
        minutes = parse_duration_minutes(hit.text)
        if minutes is None:
            continue
        hx, hy, _hw, _hh = hit.box
        # 只认标签下方、且水平位置大致对齐的（同一栏）
        if hy < ay + ah // 2:
            continue
        if abs(hx - ax) > 200:
            continue
        distance = hy - ay
        if best is None or distance < best[0]:
            best = (distance, minutes)
    return best[1] if best else None


__all__ = [
    "LauncherScreen",
    "TEXT_CONFIRM_ENTER",
    "TEXT_CONFIRM_MUTE",
    "TEXT_CONFIRM_QUIT",
    "TEXT_CONFIRM_TITLE",
    "TEXT_FREE_TIME",
    "TEXT_PAID_TIME",
    "TEXT_START_GAME",
    "TEXT_UID",
    "TextHit",
    "parse_duration_minutes",
    "read_playtime",
]
