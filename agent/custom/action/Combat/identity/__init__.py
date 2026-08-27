"""角色身份识别。

三条路径，可靠性递减，能力边界写在类型里：

1. ``SlotIdentifier``  —— "当前在哪个槽位"。复用内核已验证的高亮打分，
   现在就可用。
2. ``Roster``          —— "槽位里是谁"，由用户在脚本里显式声明。
   槽位识别可靠 + 声明可信 => 身份可信。
3. ``PortraitIdentifier`` —— 从侧栏头像自动认人。**需要仓库补齐侧栏头像
   模板**，当前不可用（见 matcher.py 顶部说明）。

三条路径都认不出身份时，``CombatState.character`` 保持 ``None``，
``character`` 条件按"未知即不满足"恒为假 —— 脚本退回兜底动作，
而不是按错误身份放技能。
"""

from __future__ import annotations

from .catalog import (
    CHARACTERS,
    NAME_OCR_ROI,
    PORTRAIT_ROI,
    PORTRAIT_THRESHOLD,
    Character,
    available_portraits,
    display_name,
    portrait_template_list,
    resolve,
)
from .matcher import (
    SIDEBAR_TEMPLATE_DIR,
    SIDEBAR_THRESHOLD,
    PortraitIdentifier,
    SlotIdentifier,
    SlotIdentity,
)
from .roster import EMPTY_ROSTER, Roster, parse_roster, resolve_identity

__all__ = [
    "CHARACTERS",
    "Character",
    "EMPTY_ROSTER",
    "NAME_OCR_ROI",
    "PORTRAIT_ROI",
    "PORTRAIT_THRESHOLD",
    "PortraitIdentifier",
    "Roster",
    "SIDEBAR_TEMPLATE_DIR",
    "SIDEBAR_THRESHOLD",
    "SlotIdentifier",
    "SlotIdentity",
    "available_portraits",
    "display_name",
    "parse_roster",
    "portrait_template_list",
    "resolve",
    "resolve_identity",
]
