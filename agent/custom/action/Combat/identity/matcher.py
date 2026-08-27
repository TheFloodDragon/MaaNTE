"""角色身份识别。

## 身份从哪里来（已定方案）

战斗脚本想表达"当前操作角色是薄荷时才放这个技能"，需要在战斗界面知道
当前角色是谁。本项目采用**槽位识别 + 用户声明编成**的组合：

1. 侧栏槽位位置 —— 内核 ``CURRENT_CHAR_MARKER_ROI`` ``[1168,164,68,36]``
   + 88px 槽距，来自粉爪，已在真机验证；
2. 槽位里是谁 —— 用户在脚本 ``roster`` 字段里显式声明（见 roster.py）。

于是身份链路是：高亮打分 -> 槽位索引 -> roster 查表 -> 角色键名。
两个环节都不依赖新增图片资源，现在即可使用。

## 为什么不做头像自动认人

仓库里的角色图只有 ``Character_UI/Character_Pic/*.png``，那是 200×210 的
**角色详情页立绘**，用在 ROI ``[390,80,200,210]``。它和 68×36 的战斗侧栏
槽位既不同尺寸也不同用途：立绘是半身构图，侧栏是头部特写加边框，
缩放后匹配并不可靠。

``PortraitIdentifier`` 因此保留为**未启用的扩展点**：结构和 ROI 计算都在，
但没有侧栏模板时 ``available`` 为 ``False``，识别一律返回未知。它刻意
**不会**退化成"用立绘凑合匹配"——错误的角色身份比没有身份更危险，
因为脚本会按错误前提放技能，而用户很难从行为反推出"识别错了"。

将来若补齐侧栏头像模板（放到 ``Character_UI/Team_Slot/``），本类会自动
发现并启用，无需改动调用方。

## 身份未知时的行为

三条路径都认不出身份时，``CombatState.character`` 保持 ``None``，
``character`` 条件按"未知即不满足"恒为假（见 conditions.py）——
脚本退回兜底动作，而不是按错误身份乱放技能。
"""


from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..kernel import team as kernel_team
from ..kernel.constants import CURRENT_CHAR_SLOT_COUNT
from . import catalog

# 侧栏头像模板的预期存放位置。这是一个**未启用的扩展点**：
# 项目已决定用 roster 显式声明代替头像自动认人（见模块文档）。
# 若将来要启用，把裁好的 68x36 侧栏图放进这里，文件名沿用立绘名
# （Mint.png 等），PortraitIdentifier 会自动发现。
SIDEBAR_TEMPLATE_DIR = "Character_UI/Team_Slot"

# 侧栏识别的最低置信度。比详情页立绘的 0.8 更严：
# 侧栏图小、边框干扰多，宁可判未知也不要判错人。
SIDEBAR_THRESHOLD = 0.88


@dataclass(frozen=True)
class SlotIdentity:
    """一个槽位的识别结果。

    ``character`` 为 ``None`` 表示"没识别出是谁"，与"识别出无人"不同——
    后者用 ``occupied=False`` 表达。区分这两者是为了让日志能说清
    到底是资源缺失还是槽位真的空着。
    """

    slot: int
    character: str | None = None
    confidence: float = 0.0
    occupied: bool = True

    @property
    def display(self) -> str:
        if self.character is None:
            return "未知"
        return catalog.display_name(self.character)


class SlotIdentifier:
    """判断当前操作的是哪个槽位。

    完全复用内核已验证的高亮打分逻辑，不引入新的识别方式，
    因此可靠性等同于粉爪已上线的切人确认。
    """

    def current_slot(self, image) -> int:
        """返回当前高亮槽位索引；无法可靠判断时返回 -1。"""
        if image is None:
            return -1
        return kernel_team.current_slot_index(image)

    def is_slot_active(self, image, slot: int) -> bool:
        """判断指定槽位是否为当前操作角色。"""
        if image is None:
            return False
        return kernel_team.is_slot_active(image, slot)

    def slot_scores(self, image) -> list[int]:
        """暴露原始打分，便于诊断"为什么没认出槽位"。"""
        if image is None:
            return [0] * CURRENT_CHAR_SLOT_COUNT
        return kernel_team.slot_scores(image)


class PortraitIdentifier:
    """从侧栏头像自动认人。**未启用的扩展点**。

    项目已决定用 ``Roster`` 显式声明代替头像自动认人（见模块文档），
    因此正常运行路径不会用到本类。保留它是因为结构与 ROI 计算已经写好，
    将来若补齐侧栏模板可以直接启用。

    没有模板时本类不会退化成"用立绘凑合匹配"，而是明确报告不可用。
    理由：错误的角色身份比没有身份更危险——脚本会按错误前提放技能，
    而用户很难从行为反推出"识别错了"。
    """

    def __init__(self, image_dir=None, threshold=SIDEBAR_THRESHOLD):
        self._threshold = float(threshold)
        self._dir = Path(image_dir) if image_dir else None
        self._templates: dict[str, Path] = {}
        self._scan()

    def _scan(self):
        """发现可用的侧栏头像模板。"""
        self._templates.clear()
        if self._dir is None or not self._dir.exists():
            return
        for entry in catalog.available_portraits():
            path = self._dir / entry.portrait
            if path.exists():
                self._templates[entry.key] = path

    @property
    def available(self) -> bool:
        """是否具备识别能力（至少有一个侧栏模板）。"""
        return bool(self._templates)

    @property
    def known_characters(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    def missing_reason(self) -> str | None:
        """不可用的原因，用于日志与静态校验；可用时返回 None。"""
        if self.available:
            return None
        if self._dir is None:
            return (
                "未配置侧栏头像目录；战斗中无法识别角色身份，"
                f"character 条件将恒为假。请把裁好的模板放到 {SIDEBAR_TEMPLATE_DIR}/"
            )
        if not self._dir.exists():
            return (
                f"侧栏头像目录不存在: {self._dir}；"
                "战斗中无法识别角色身份，character 条件将恒为假"
            )
        return (
            f"侧栏头像目录 {self._dir} 中没有任何可用模板；"
            "战斗中无法识别角色身份，character 条件将恒为假"
        )

    def identify(self, image, slot: int) -> SlotIdentity:
        """识别指定槽位是哪个角色。

        模板缺失或置信度不足时返回 ``character=None``，绝不返回猜测值。
        """
        if not self.available or image is None:
            return SlotIdentity(slot=slot, character=None, confidence=0.0)

        from ..kernel.frames import TemplateCache, crop_scaled_roi, fast_template_match

        roi = self._slot_roi(slot)
        crop = crop_scaled_roi(image, roi)
        if crop is None:
            return SlotIdentity(slot=slot, character=None, confidence=0.0)

        cache = TemplateCache(self._dir)
        best_key, best_score = None, 0.0
        for key, path in self._templates.items():
            cfg = {
                "roi": roi,
                "templates": [path.name],
                "threshold": self._threshold,
                "cv_threshold": self._threshold,
            }
            hit = fast_template_match(image, cfg, cache)
            if hit:
                # fast_template_match 只回布尔，够用：命中即视为达到阈值。
                # 想要真实分数需要扩展内核接口，等模板到位后再做。
                best_key, best_score = key, self._threshold
                break
        return SlotIdentity(
            slot=slot,
            character=best_key,
            confidence=best_score,
            occupied=True,
        )

    @staticmethod
    def _slot_roi(slot: int) -> list[int]:
        """按槽位算出侧栏 ROI，沿用内核的槽距。"""
        from ..kernel.constants import (
            CURRENT_CHAR_MARKER_ROI,
            CURRENT_CHAR_SLOT_SPACING,
        )

        roi = list(CURRENT_CHAR_MARKER_ROI)
        roi[1] += CURRENT_CHAR_SLOT_SPACING * int(slot)
        return roi
