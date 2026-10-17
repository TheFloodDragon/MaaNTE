"""离线赠礼目录：只解析已随资源分发的数据，不访问网络或注册 Maa 动作。

Wiki 图标只能展示或定位候选；目录、名称、图片均不能独立授权送礼。
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


def _read_document(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            document = json.load(stream, object_pairs_hook=_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("无法读取 %s: %s" % (path.name, exc)) from exc
    if not isinstance(document, dict):
        raise CatalogError("%s 的根节点必须为对象" % path.name)
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
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
    image_format = {".png": "PNG", ".webp": "WEBP"}.get(path.suffix.lower())
    if (
        path.is_absolute()
        or "\\" in relative
        or ":" in relative
        or any(part in ("", ".", "..") for part in relative.split("/"))
        or path.parts[:2] != ("Gift", directory)
        or image_format is None
    ):
        raise CatalogError("图片路径越界或不是展示 PNG/WebP: %s" % relative)
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
            raise CatalogError("展示图片体积异常")
        payload = actual.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise CatalogError("图片 SHA-256 不匹配: %s" % relative)
        # Pillow 是项目已有依赖；只解码本地、已限制大小的展示图。
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as preview:
            if preview.format != image_format or preview.size != (width, height):
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


def _validate_coverage(source: dict, records: dict) -> None:
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
    actual = {
        record["source"]["pageid"]: record["source"]["title"]
        for record in records.values()
    }
    if len(actual) != len(records):
        raise CatalogError("多个目录条目重复使用同一 Wiki 页面")
    if members != actual:
        raise CatalogError("目录未完整收录分类快照，或包含未列入分类的条目")


class Catalog:
    """保持库顺序的完整目录；未知数据不补猜测值。"""

    def __init__(self, base: Path, documents: dict[str, dict]) -> None:
        self.base = base
        self.documents = documents
        self.gifts: dict[str, dict] = {}
        self.characters: dict[str, dict] = {}
        seen_images: dict = {}
        for kind, records in (("characters", self.characters), ("gifts", self.gifts)):
            document = documents[kind]
            rows = document.get(kind)
            if not isinstance(rows, list) or not rows:
                raise CatalogError("%s 必须为非空数组" % kind)
            for record in rows:
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
                source = _record_source(record)
                if kind == "characters":
                    if identifier != "char_%d" % source["pageid"]:
                        raise CatalogError(
                            "角色必须使用独立的 char_<Wiki pageid> 库 ID"
                        )
                    if type(record.get("giftable")) is not bool:
                        raise CatalogError("角色 giftable 必须为布尔值")
                    if source["title"] == "异能者·零" and record["giftable"]:
                        raise CatalogError("主角不能标为可赠礼角色")
                    if record["giftable"] and not source.get("giftability_evidence"):
                        raise CatalogError("可赠礼角色缺少 Wiki 证据")
                else:
                    value = record.get("default_likeability")
                    if "default_likeability" not in record:
                        raise CatalogError(
                            "礼物缺少 default_likeability；未知应为 null"
                        )
                    if value is not None:
                        _integer(value, "default_likeability")
                    if "special_gifts" not in record:
                        raise CatalogError("礼物缺少 special_gifts；未知应为 null")
                    special = record["special_gifts"]
                    if special is not None:
                        if not isinstance(special, list):
                            raise CatalogError("special_gifts 必须为数组或 null")
                        seen_characters = set()
                        for preference in special:
                            if not isinstance(preference, dict):
                                raise CatalogError("特殊喜好必须为对象")
                            character = preference.get("character_id")
                            if (
                                not isinstance(character, str)
                                or character not in self.characters
                            ):
                                raise CatalogError("特殊喜好引用未知角色")
                            if character in seen_characters:
                                raise CatalogError("同一礼物重复记录角色喜好")
                            seen_characters.add(character)
                            _integer(preference.get("likeability"), "likeability")
                _validate_image(base, record, kind, seen_images)
                records[identifier] = record
            source = document.get("source")
            if not isinstance(source, dict):
                raise CatalogError("目录缺少 source")
            _validate_coverage(source, records)
        snapshots = [doc["source"].get("snapshot_id") for doc in documents.values()]
        if any(snapshots) and (not all(snapshots) or len(set(snapshots)) != 1):
            raise CatalogError("礼物库与角色库不属于同一个完整同步快照")
        self._gift_index = self._name_index(self.gifts)
        self._character_index = self._name_index(self.characters)

    @classmethod
    def load(cls, base: Path | None = None) -> Catalog:
        location = Path(base) if base is not None else default_base()
        documents = {
            kind: _read_document(location / "data" / (kind + ".json"))
            for kind in ("gifts", "characters")
        }
        return cls(location.resolve(), documents)

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
