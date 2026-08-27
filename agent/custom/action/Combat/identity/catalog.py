"""角色名录：英文键名 <-> 中文显示名 <-> 立绘模板。

数据来源是 ``SyncCharacterAbilityCityAbility``（已上线、已验证的角色识别），
这里只做**复用与集中**，不新增角色数据，避免两处各写一份导致漂移。

刻意不含"槽位 -> 角色"的映射：那需要在战斗界面识别侧栏头像，
而仓库目前缺少侧栏头像模板（见 ``matcher.py`` 的说明）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 立绘模板目录（相对 image 根），与 SyncCharacterAbilityCityAbility 一致
PORTRAIT_DIR = "Character_UI/Character_Pic"

# 角色详情页立绘的 ROI 与阈值，来自
# pipeline/UserInfo/SyncCharacterAbilityCityAbility.json 的
# SyncCharacterAbilityCityAbilityMatchCharacter 节点。
PORTRAIT_ROI = (390, 80, 200, 210)
PORTRAIT_THRESHOLD = 0.8
PORTRAIT_GREEN_MASK = True

# 角色详情页角色名 OCR 的 ROI 与纠错规则（同一 JSON 的 OCRName 节点）。
NAME_OCR_ROI = (865, 120, 210, 40)
NAME_OCR_REPLACE = (
    (r"[一二三—#-]", ""),
    (r"小(咬|岐).*", "小吱"),
    (r"^[黯霸]$", "翳"),
    (r"^[海涛]$", "浔"),
)


@dataclass(frozen=True)
class Character:
    """一个角色的名录条目。

    ``key`` 是脚本里书写的稳定标识（小写英文），``display`` 是中文名，
    ``portrait`` 是立绘文件名；``has_portrait`` 为假表示仓库暂无该角色立绘
    （角色已在游戏里但项目还没放图）。
    """

    key: str
    display: str
    portrait: str
    has_portrait: bool = True

    @property
    def portrait_path(self) -> str:
        """给 MAA TemplateMatch 用的相对路径。"""
        return f"{PORTRAIT_DIR}/{self.portrait}"


# 与 SyncCharacterAbilityCityAbility._TEMPLATE_TO_NAME 保持同步。
# has_portrait=False 的条目在那里也标注了"没抽到/没开池子，没放图"。
_ENTRIES = (
    Character("adler", "阿德勒", "Adler.png"),
    Character("aurelia", "海月", "Aurelia.png"),
    Character("baicang", "白藏", "Baicang.png", has_portrait=False),
    Character("chaos", "卡厄斯", "Chaos.png", has_portrait=False),
    Character("chiz", "小吱", "Chiz.png"),
    Character("daffodill", "达芙蒂尔", "Daffodill.png"),
    Character("edgar", "埃德嘉", "Edgar.png"),
    Character("fadia", "法帝娅", "Fadia.png", has_portrait=False),
    Character("haniel", "哈尼娅", "Haniel.png"),
    Character("hathor", "哈索尔", "Hathor.png"),
    Character("hotori", "浔", "Hotori.png"),
    Character("jiuyuan", "九原", "Jiuyuan.png"),
    Character("lacrimosa", "安魂曲", "Lacrimosa.png"),
    Character("mint", "薄荷", "Mint.png"),
    Character("nanally", "娜娜莉", "Nanally.png"),
    Character("sakiri", "早雾", "Sakiri.png"),
    Character("skia", "翳", "Skia.png"),
    Character("zero", "零", "Zero.png"),
)

CHARACTERS: dict[str, Character] = {entry.key: entry for entry in _ENTRIES}

_BY_DISPLAY = {entry.display: entry for entry in _ENTRIES}
_BY_PORTRAIT = {entry.portrait: entry for entry in _ENTRIES}


def resolve(name: str) -> Character | None:
    """把用户写的角色名解析成名录条目。

    接受英文键名、中文名或立绘文件名，大小写与首尾空白无关。
    解析不了返回 ``None`` —— 由调用方决定是报 issue 还是忽略。
    """
    if not name:
        return None
    text = str(name).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in CHARACTERS:
        return CHARACTERS[lowered]
    if text in _BY_DISPLAY:
        return _BY_DISPLAY[text]
    if text in _BY_PORTRAIT:
        return _BY_PORTRAIT[text]
    # 允许写不带扩展名的立绘名，例如 "Mint"
    for entry in _ENTRIES:
        if entry.portrait.rsplit(".", 1)[0].lower() == lowered:
            return entry
    return None


def display_name(name: str) -> str:
    """返回中文显示名；解析不了时原样返回，便于日志排查。"""
    entry = resolve(name)
    return entry.display if entry is not None else str(name)


def available_portraits() -> tuple[Character, ...]:
    """返回仓库中确实有立绘文件的角色。"""
    return tuple(entry for entry in _ENTRIES if entry.has_portrait)


def portrait_template_list() -> list[str]:
    """按 MAA TemplateMatch 需要的顺序返回模板相对路径。"""
    return [entry.portrait_path for entry in available_portraits()]
