"""离线赠礼目录：只解析已随资源分发的数据，不访问网络或注册 Maa 动作。

礼物清单、名称、好感值、不占次数与专属角色以 gifts.json 内嵌的游戏表快照
（NTEData 固定提交）为唯一依据；Wiki 只补图标，按游戏 ID 对齐。
目录、名称、图片均不能独立授权送礼。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import unicodedata
from urllib.parse import urlsplit


class CatalogError(ValueError):
    """目录缺失、损坏、名称未知或不唯一。"""


GIFT_SCHEMA = 2
CHARACTER_SCHEMA = 1
# 游戏 EItemType → 目录 kind；出现未知类型时拒绝猜测，需维护者确认。
GAME_KINDS = {
    "ITEM_TYPE_COOK_FOOD": "food",
    "ITEM_TYPE_FURNITURE": "furniture",
    "ITEM_TYPE_FIGURES": "figure",
    "ITEM_TYPE_FLOWER": "flower",
    "ITEM_TYPE_CONSUMABLE": "consumable",
    "ITEM_TYPE_SHOWCASE": "showcase",
    "ITEM_TYPE_LIKEABILITY_GIFT": "gift",
}
GAME_EXCLUSIONS = ("name_missing", "exclusive_role_unmapped", "unreleased")
WIKI_EXCLUSIONS = ("not_in_game_gift_table",) + GAME_EXCLUSIONS
DAILY_LIMITS = {"per_character": 3, "total": 10}


def normalize_name(value: str) -> str:
    """只做 NFKC 和去空白；不纠错、不改大小写、不做子串匹配。"""
    if not isinstance(value, str):
        raise CatalogError("名称必须是字符串")
    return "".join(unicodedata.normalize("NFKC", value).split())


def default_base() -> Path:
    """兼容仓库 assets/resource/base 和发行包 resource/base。"""
    root = Path(__file__).resolve().parents[4]
    candidates = (
        root / "assets" / "resource" / "base",
        root / "resource" / "base",
        Path.cwd() / "resource" / "base",
        Path.cwd() / "assets" / "resource" / "base",
    )
    for candidate in candidates:
        if any(
            (candidate / "data" / name).is_file()
            for name in ("gifts.json", "characters.json")
        ):
            return candidate
    raise CatalogError("找不到赠礼目录，请指定包含 data 和 image 的资源 base 目录")


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("JSON 存在重复键: %s" % key)
        result[key] = value
    return result


def _read_document(path: Path, version: int) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            document = json.load(stream, object_pairs_hook=_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("无法读取 %s: %s" % (path.name, exc)) from exc
    if not isinstance(document, dict):
        raise CatalogError("%s 的根节点必须为对象" % path.name)
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != version
    ):
        raise CatalogError("不支持的目录 schema_version")
    return document


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("%s 必须为非空字符串" % label)
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CatalogError("%s 必须为不小于 %d 的整数" % (label, minimum))
    return value


def _url(value: object, label: str) -> str:
    value = _text(value, label)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise CatalogError("%s 不是有效的来源 URL" % label) from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise CatalogError("%s 不是有效的来源 URL" % label)
    return value


def _hash(value: object, length: int, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
        raise CatalogError("%s 哈希格式无效" % label)
    return value


def _record_source(record: dict) -> dict:
    source = record.get("source")
    if not isinstance(source, dict):
        raise CatalogError("条目缺少 source")
    _text(source.get("title"), "source.title")
    _integer(source.get("pageid"), "source.pageid", 1)
    _integer(source.get("revid"), "source.revid", 1)
    _text(source.get("timestamp"), "source.timestamp")
    _url(source.get("url"), "source.url")
    return source


def _validate_image(base: Path, record: dict, kind: str, seen: dict) -> None:
    image = record.get("image")
    if not isinstance(image, dict):
        raise CatalogError("条目缺少 image")
    relative = _text(image.get("path"), "image.path")
    path = PurePosixPath(relative)
    directory = "Gifts" if kind == "gifts" else "Characters"
    if (
        path.is_absolute()
        or "\\" in relative
        or ":" in relative
        or any(part in ("", ".", "..") for part in relative.split("/"))
        or path.parts[:2] != ("Gift", directory)
        or path.suffix.lower() != ".png"
    ):
        raise CatalogError("图片路径越界或不是展示 PNG: %s" % relative)
    root = (base / "image").resolve()
    actual = (root / relative).resolve()
    if not root.is_relative_to(base.resolve()) or not actual.is_relative_to(root):
        raise CatalogError("图片符号链接越界: %s" % relative)
    digest = _hash(image.get("sha256"), 64, "image.sha256")
    _hash(image.get("source_sha1"), 40, "image.source_sha1")
    _url(image.get("source_url"), "image.source_url")
    width = _integer(image.get("width"), "image.width", 1)
    height = _integer(image.get("height"), "image.height", 1)
    if max(width, height) > 256:
        raise CatalogError("展示图任一边不能超过 256px")
    if image.get("purpose") not in ("display_only", "display_and_candidate_only"):
        raise CatalogError("图片只能用于展示或候选定位，不能作为送礼授权")
    if image.get("can_authorize_gift", False) is not False:
        raise CatalogError("图片不能授权送礼")
    expected = (digest, width, height)
    if actual in seen:
        if seen[actual] != expected:
            raise CatalogError("同一图片路径存在冲突的清单记录")
        return
    try:
        if actual.stat().st_size > 2 * 1024 * 1024:
            raise CatalogError("展示 PNG 体积异常")
        payload = actual.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise CatalogError("图片 SHA-256 不匹配: %s" % relative)
        # Pillow 是项目已有依赖；只解码本地、已限制大小的展示图。
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as preview:
            if preview.format != "PNG" or preview.size != (width, height):
                raise CatalogError("图片格式或尺寸与清单不符: %s" % relative)
            if getattr(preview, "n_frames", 1) != 1:
                raise CatalogError("展示图不能包含动画帧")
            preview.verify()
        with Image.open(io.BytesIO(payload)) as preview:
            preview.load()
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError("图片缺失或无法解码: %s" % relative) from exc
    seen[actual] = expected


def _category_members(source: dict) -> dict[int, str]:
    _url(source.get("url"), "source.url")
    _text(source.get("collected_at"), "source.collected_at")
    license_info = source.get("license")
    if not isinstance(license_info, dict) or not license_info:
        raise CatalogError("目录缺少素材许可与归属说明")
    if license_info.get("redistribution_requires_permission") is not True:
        raise CatalogError("Wiki 素材再分发许可尚未确认")
    categories = source.get("categories")
    if not isinstance(categories, list) or not categories:
        raise CatalogError("目录缺少分类收录证据")
    members = {}
    for category in categories:
        if not isinstance(category, dict):
            raise CatalogError("分类收录证据必须为对象")
        _text(category.get("title"), "category.title")
        _url(category.get("url"), "category.url")
        pages = category.get("members")
        if not isinstance(pages, list):
            raise CatalogError("分类缺少完整 members 快照")
        if _integer(category.get("page_count"), "category.page_count") != len(pages):
            raise CatalogError("分类页数与 members 快照不符")
        category_ids = set()
        for page in pages:
            if not isinstance(page, dict):
                raise CatalogError("分类成员不是对象")
            pageid = _integer(page.get("pageid"), "member.pageid", 1)
            title = _text(page.get("title"), "member.title")
            if pageid in category_ids:
                raise CatalogError("分类存在重复 pageid")
            if pageid in members and members[pageid] != title:
                raise CatalogError("分类 pageid 对应不同标题")
            category_ids.add(pageid)
            members[pageid] = title
    return members


def _validate_coverage(source: dict, records: dict) -> None:
    """角色库：分类快照必须与条目一一对应。"""
    members = _category_members(source)
    actual = {
        record["source"]["pageid"]: record["source"]["title"]
        for record in records.values()
    }
    if len(actual) != len(records):
        raise CatalogError("多个目录条目重复使用同一 Wiki 页面")
    if members != actual:
        raise CatalogError("目录未完整收录分类快照，或包含未列入分类的条目")


def _page_list(value: object, label: str, *, reason: bool) -> dict[int, dict]:
    if not isinstance(value, list):
        raise CatalogError("%s 必须为数组" % label)
    pages = {}
    for page in value:
        if not isinstance(page, dict):
            raise CatalogError("%s 条目必须为对象" % label)
        pageid = _integer(page.get("pageid"), label + ".pageid", 1)
        _text(page.get("title"), label + ".title")
        _text(page.get("id"), label + ".id")
        if reason and page.get("reason") not in WIKI_EXCLUSIONS:
            raise CatalogError("%s 缺少已知排除原因" % label)
        if pageid in pages:
            raise CatalogError("%s 存在重复 pageid" % label)
        pages[pageid] = page
    return pages


def _validate_gift_coverage(source: dict, records: dict, excluded: dict) -> None:
    """礼物库：Wiki 分类每页要么补了某个游戏礼物的图标，要么写明排除原因。"""
    members = _category_members(source)
    lookups = _page_list(source.get("wiki_lookups"), "wiki_lookups", reason=False)
    skipped = _page_list(source.get("wiki_excluded"), "wiki_excluded", reason=True)
    for pageid, page in lookups.items():
        if pageid in members:
            raise CatalogError("按标题补查的页面不能同时属于分类快照")
    used = {}
    for identifier, record in records.items():
        wiki = record["source"]
        if wiki is None:
            continue
        pageid = wiki["pageid"]
        if pageid in used:
            raise CatalogError("多个目录条目重复使用同一 Wiki 页面")
        title = members.get(pageid)
        if title is None and pageid in lookups:
            if lookups[pageid]["id"] != identifier:
                raise CatalogError("补查页面与礼物 ID 不一致: %s" % identifier)
            title = lookups[pageid]["title"]
        if title is None or title != wiki["title"]:
            raise CatalogError("Wiki 来源不在分类快照或补查记录中: %s" % identifier)
        used[pageid] = identifier
    for pageid, page in skipped.items():
        if members.get(pageid) != page["title"]:
            raise CatalogError("排除页面不在分类快照中: %s" % page["title"])
        if pageid in used or page["id"] in records:
            raise CatalogError("被排除的 Wiki 页面不能同时作为礼物来源")
        if page["reason"] != excluded.get(page["id"], "not_in_game_gift_table"):
            raise CatalogError("Wiki 排除原因与游戏表快照不符: %s" % page["id"])
    if set(members) != set(used) - set(lookups) | set(skipped):
        raise CatalogError("目录未完整处理分类快照，或包含未列入分类的条目")
    if set(lookups) - set(used):
        raise CatalogError("补查页面未被任何礼物使用")


def _role_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise CatalogError("%s 不是有效的游戏角色 ID" % label)
    return value


def _game_snapshot(game: object) -> dict:
    """结构校验；派生关系由 derive_gift_records 统一判定。"""
    if not isinstance(game, dict):
        raise CatalogError("礼物库缺少游戏表快照 source.game")
    _url(game.get("repository"), "game.repository")
    _hash(game.get("commit"), 40, "game.commit")
    _text(game.get("version"), "game.version")
    files = game.get("files")
    if not isinstance(files, list) or not files:
        raise CatalogError("游戏表快照缺少文件清单")
    paths = set()
    for item in files:
        if not isinstance(item, dict):
            raise CatalogError("游戏表文件记录必须为对象")
        path = _text(item.get("path"), "game.files.path")
        if path in paths:
            raise CatalogError("游戏表文件重复: %s" % path)
        paths.add(path)
        _hash(item.get("sha256"), 64, "game.files.sha256")
        _url(item.get("url"), "game.files.url")
    if game.get("daily_limits") != DAILY_LIMITS:
        raise CatalogError("游戏每日赠礼次数与流程假设（单角色 3、合计 10）不一致")
    roles = game.get("roles")
    if not isinstance(roles, list):
        raise CatalogError("游戏表快照缺少角色映射")
    seen_roles = set()
    for role in roles:
        if not isinstance(role, dict):
            raise CatalogError("游戏角色映射必须为对象")
        role_id = _role_id(role.get("role_id"), "game.roles.role_id")
        if role_id in seen_roles:
            raise CatalogError("游戏角色映射重复: %s" % role_id)
        seen_roles.add(role_id)
        # name 为 null 表示该 RoleId 不在 DT_LikeabilityRoleData 中。
        if role.get("name") is not None:
            _text(role["name"], "game.roles.name")
        if role.get("character_id") is not None:
            _text(role["character_id"], "game.roles.character_id")
    rows = game.get("rows")
    if not isinstance(rows, list) or not rows:
        raise CatalogError("游戏表快照缺少礼物行")
    seen_rows = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CatalogError("游戏礼物行必须为对象")
        identifier = _text(row.get("id"), "game.rows.id")
        if not re.fullmatch(r"[A-Za-z0-9_]+", identifier) or identifier in seen_rows:
            raise CatalogError("游戏礼物 ID 无效或重复: %s" % identifier)
        seen_rows.add(identifier)
        if row.get("name") is not None:
            _text(row["name"], "game.rows.name")
        _text(row.get("name_table"), "game.rows.name_table")
        _text(row.get("name_key"), "game.rows.name_key")
        if row.get("item_type") not in GAME_KINDS:
            raise CatalogError("未知的游戏物品类型: %s" % row.get("item_type"))
        _integer(row.get("default_likeability"), "game.rows.default_likeability")
        if type(row.get("unlimited")) is not bool:
            raise CatalogError("game.rows.unlimited 必须为布尔值")
        if row.get("exclusive_role") is not None:
            _role_id(row["exclusive_role"], "game.rows.exclusive_role")
        special = row.get("special")
        if not isinstance(special, list):
            raise CatalogError("game.rows.special 必须为数组")
        for item in special:
            if not isinstance(item, dict):
                raise CatalogError("game.rows.special 条目必须为对象")
            _role_id(item.get("role_id"), "game.rows.special.role_id")
            _integer(item.get("likeability"), "game.rows.special.likeability")
    unreleased = game.get("unreleased")
    if (
        not isinstance(unreleased, list)
        or len(set(unreleased)) != len(unreleased)
        or any(item not in seen_rows for item in unreleased)
    ):
        raise CatalogError("game.unreleased 必须是游戏礼物行 ID 的无重复数组")
    return game


def role_character(name: str | None, characters: dict[str, dict]) -> str | None:
    """游戏角色名与角色库可赠礼角色的中文全名严格一一对应时才映射。"""
    if name is None:
        return None
    matches = [
        identifier
        for identifier, record in characters.items()
        if record.get("giftable") is True
        and normalize_name(record["names"]["zh_cn"]) == normalize_name(name)
    ]
    if len(matches) > 1:
        raise CatalogError("多个可赠礼角色同名: %s" % name)
    return matches[0] if matches else None


def derive_gift_records(
    game: dict, characters: dict[str, dict], *, verify_excluded: bool = True
) -> tuple[dict[str, dict], dict[str, str]]:
    """由游戏表快照推导应收录的礼物字段与排除原因；sync 与运行时共用。

    sync 首次生成快照时传 verify_excluded=False，再把返回的排除原因写回快照。
    """
    _game_snapshot(game)
    roles = {}
    for role in game["roles"]:
        expected = role_character(role.get("name"), characters)
        if role.get("character_id") != expected:
            raise CatalogError("游戏角色映射与角色库不一致: %s" % role["role_id"])
        roles[role["role_id"]] = expected
    records, excluded = {}, {}
    for row in game["rows"]:
        identifier = row["id"]
        exclusive = row.get("exclusive_role")
        referenced = [item["role_id"] for item in row["special"]]
        if exclusive is not None:
            referenced.append(exclusive)
        for role_id in referenced:
            if role_id not in roles:
                raise CatalogError("礼物引用了未快照的游戏角色: %s" % role_id)
        if row.get("name") is None:
            excluded[identifier] = "name_missing"
            continue
        if identifier in game["unreleased"]:
            # 游戏表里有正式名称、但维护者确认尚未上线的礼物。
            excluded[identifier] = "unreleased"
            continue
        if exclusive is not None and roles[exclusive] is None:
            excluded[identifier] = "exclusive_role_unmapped"
            continue
        special = []
        for item in row["special"]:
            character = roles[item["role_id"]]
            if character is None or character in {x["character_id"] for x in special}:
                raise CatalogError("礼物喜好引用未知或重复的角色: %s" % identifier)
            special.append(
                {"character_id": character, "likeability": item["likeability"]}
            )
        records[identifier] = {
            "names": {"zh_cn": row["name"]},
            "kind": GAME_KINDS[row["item_type"]],
            "default_likeability": row["default_likeability"],
            "special_gifts": special,
            "unlimited": row["unlimited"],
            "exclusive_character": None if exclusive is None else roles[exclusive],
        }
    if verify_excluded and game.get("excluded") != excluded_list(excluded):
        raise CatalogError("游戏表快照的排除清单与派生结果不一致")
    return records, excluded


def excluded_list(excluded: dict[str, str]) -> list[dict]:
    return [{"id": key, "reason": value} for key, value in excluded.items()]


class Catalog:
    """保持库顺序的完整目录；未知数据不补猜测值。"""

    def __init__(self, base: Path, documents: dict[str, dict]) -> None:
        self.base = base
        self.documents = documents
        self.gifts: dict[str, dict] = {}
        self.characters: dict[str, dict] = {}
        seen_images: dict = {}
        self._load_characters(documents["characters"], seen_images)
        self._load_gifts(documents["gifts"], seen_images)
        snapshots = [doc["source"].get("snapshot_id") for doc in documents.values()]
        if any(snapshots) and (not all(snapshots) or len(set(snapshots)) != 1):
            raise CatalogError("礼物库与角色库不属于同一个完整同步快照")
        self._gift_index = self._name_index(self.gifts)
        self._character_index = self._name_index(self.characters)

    @staticmethod
    def _rows(document: dict, kind: str) -> list:
        rows = document.get(kind)
        if not isinstance(rows, list) or not rows:
            raise CatalogError("%s 必须为非空数组" % kind)
        source = document.get("source")
        if not isinstance(source, dict):
            raise CatalogError("目录缺少 source")
        return rows

    @staticmethod
    def _identity(record: object, records: dict, kind: str) -> str:
        if not isinstance(record, dict):
            raise CatalogError("目录条目必须为对象")
        identifier = _text(record.get("id"), "id")
        if identifier in records:
            raise CatalogError("重复的 %s ID: %s" % (kind, identifier))
        names = record.get("names")
        if not isinstance(names, dict) or "zh_cn" not in names:
            raise CatalogError("每个条目必须包含 names.zh_cn")
        for language, name in names.items():
            _text(language, "语言")
            if not normalize_name(_text(name, "名称")):
                raise CatalogError("名称不能只有空白")
        return identifier

    def _load_characters(self, document: dict, seen_images: dict) -> None:
        for record in self._rows(document, "characters"):
            identifier = self._identity(record, self.characters, "characters")
            source = _record_source(record)
            if identifier != "char_%d" % source["pageid"]:
                raise CatalogError("角色必须使用独立的 char_<Wiki pageid> 库 ID")
            if type(record.get("giftable")) is not bool:
                raise CatalogError("角色 giftable 必须为布尔值")
            if source["title"] == "异能者·零" and record["giftable"]:
                raise CatalogError("主角不能标为可赠礼角色")
            if record["giftable"] and not source.get("giftability_evidence"):
                raise CatalogError("可赠礼角色缺少 Wiki 证据")
            _validate_image(self.base, record, "characters", seen_images)
            self.characters[identifier] = record
        _validate_coverage(document["source"], self.characters)

    def _load_gifts(self, document: dict, seen_images: dict) -> None:
        rows = self._rows(document, "gifts")
        expected, excluded = derive_gift_records(
            document["source"].get("game"), self.characters
        )
        for record in rows:
            identifier = self._identity(record, self.gifts, "gifts")
            derived = expected.get(identifier)
            if derived is None:
                raise CatalogError(
                    "礼物不在游戏礼物表快照中或已被排除: %s" % identifier
                )
            for key, value in derived.items():
                if key not in record or record[key] != value:
                    raise CatalogError(
                        "%s 的 %s 与游戏表快照不一致" % (identifier, key)
                    )
            if "source" not in record or "image" not in record:
                raise CatalogError("礼物缺少 source/image；没有 Wiki 图标时应为 null")
            if record["source"] is None:
                if record["image"] is not None:
                    raise CatalogError(
                        "没有 Wiki 来源的礼物不能携带图标: %s" % identifier
                    )
            else:
                _record_source(record)
                _validate_image(self.base, record, "gifts", seen_images)
            self.gifts[identifier] = record
        if set(self.gifts) != set(expected):
            missing = sorted(set(expected) - set(self.gifts))
            raise CatalogError(
                "礼物库未完整收录游戏礼物表: %s" % ", ".join(missing[:5])
            )
        _validate_gift_coverage(document["source"], self.gifts, excluded)

    @classmethod
    def load(cls, base: Path | None = None) -> Catalog:
        location = Path(base) if base is not None else default_base()
        documents = {
            kind: _read_document(location / "data" / (kind + ".json"), version)
            for kind, version in (
                ("gifts", GIFT_SCHEMA),
                ("characters", CHARACTER_SCHEMA),
            )
        }
        return cls(location.resolve(), documents)

    def gift_allowed(self, gift_id: str, character_id: str) -> bool:
        """专属礼物只能配给对应角色；其他礼物不限角色。"""
        if gift_id not in self.gifts:
            raise CatalogError("未知目录 ID: %s" % gift_id)
        exclusive = self.gifts[gift_id]["exclusive_character"]
        return exclusive is None or exclusive == character_id

    @staticmethod
    def _name_index(records: dict[str, dict]) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for identifier, record in records.items():
            for name in record["names"].values():
                matches = index.setdefault(normalize_name(name), [])
                if identifier not in matches:
                    matches.append(identifier)
        return index

    @staticmethod
    def _resolve(token: str, records: dict, index: dict, kind: str) -> str:
        normalized = normalize_name(token)
        if not normalized:
            raise CatalogError("%s名称不能为空" % kind)
        if token.strip() in records:
            return token.strip()
        matches = index.get(normalized, [])
        if not matches:
            raise CatalogError("未知%s: %s" % (kind, token))
        if len(matches) != 1:
            raise CatalogError("%s名称有歧义，请改用稳定 ID: %s" % (kind, token))
        return matches[0]

    def resolve_gift(self, token: str) -> str:
        return self._resolve(token, self.gifts, self._gift_index, "礼物")

    def parse_priority(self, text: str) -> tuple[str, ...]:
        """按 > 解析礼物优先序；只接受稳定礼物 ID 或完整礼物名。"""
        if not isinstance(text, str):
            raise CatalogError("礼物优先级必须是字符串")
        result = []
        for token in text.split(">"):
            identifier = self.resolve_gift(token)
            if identifier not in result:
                result.append(identifier)
        return tuple(result)

    @staticmethod
    def _names(identifier: str, records: dict) -> tuple[str, ...]:
        if not isinstance(identifier, str) or identifier not in records:
            raise CatalogError("未知目录 ID: %s" % identifier)
        return tuple(dict.fromkeys(records[identifier]["names"].values()))

    def character_names(self, identifier: str) -> tuple[str, ...]:
        return self._names(identifier, self.characters)

    def gift_names(self, identifier: str) -> tuple[str, ...]:
        return self._names(identifier, self.gifts)
