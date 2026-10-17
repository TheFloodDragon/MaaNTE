"""显式维护 Wiki 赠礼库；--check 完全离线、只读，运行时不使用本工具。

用现有环境执行：python tools/sync_gift_catalog.py --sync / --check
同步前应关闭使用此资源的任务。所有原图仅在内存解码；最终只保存 <=256px
展示 PNG。没有开放素材许可，本工具不发布、不替权利人授予再分发许可。
"""

from __future__ import annotations

import sys

# 包括 --check 动态导入的模块在内，禁止留下 __pycache__ 等隐式写入。
sys.dont_write_bytecode = True

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Iterator
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from PIL import Image, __version__ as PILLOW_VERSION

ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://www.ntestation.com/api.php"
GIFT_CATEGORY = "Category:礼物"
CHARACTER_CATEGORIES = ("Category:S级角色", "Category:A级角色")
GALLERY_PATH = "/images/character_data.json"
RECIPE = "rgba-lanczos-256-png9-v1"
MAX_DOWNLOAD = 16 * 1024 * 1024
LOG = logging.getLogger("gift_catalog_sync")


def _load_module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载离线校验模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_catalog = _load_module(
    "_maante_gift_catalog", ROOT / "agent" / "custom" / "action" / "Gift" / "catalog.py"
)
Catalog = _catalog.Catalog
CatalogError = _catalog.CatalogError
normalize_name = _catalog.normalize_name


class SyncError(ValueError):
    """来源不完整、不支持或无法安全发布本次目录。"""


class RecoveryError(SyncError):
    """系统文件锁阻止回滚，必须保留临时备份等待人工恢复。"""


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=4, allow_nan=False) + "\n"
    ).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def wiki_url(title: str) -> str:
    return "https://www.ntestation.com/" + quote(title.replace(" ", "_"), safe=":")


# 不用 split('|') 或正则截取模板：链接、嵌套模板、三重大括号和 nowiki 都可含 |。
_OPAQUE = re.compile(r"<(nowiki|pre|source|syntaxhighlight|ref)\b[^>]*>", re.I)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def _scan(
    text: str, separators: str = ""
) -> tuple[list[int], list[tuple[str, int, int]]]:
    stack: list[tuple[str, str, int]] = []
    delimiters = []
    spans = []
    index = 0
    while index < len(text):
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            if end < 0:
                raise SyncError("Wikitext 注释未闭合")
            index = end + 3
            continue
        opaque = _OPAQUE.match(text, index)
        if opaque:
            if opaque.group(0).rstrip().endswith("/>"):
                index = opaque.end()
            else:
                close = re.search(
                    r"</%s\s*>" % opaque.group(1), text[opaque.end() :], re.I
                )
                if close is None:
                    raise SyncError("Wikitext %s 标签未闭合" % opaque.group(1))
                index = opaque.end() + close.end()
            continue
        tag = _TAG.match(text, index)
        if tag:
            index = tag.end()
            continue
        if stack and text.startswith(stack[-1][0], index):
            closing, kind, start = stack.pop()
            index += len(closing)
            spans.append((kind, start, index))
            continue
        token = None
        for opening, closing, kind in (
            ("{{{", "}}}", "argument"),
            ("{{", "}}", "template"),
            ("[[", "]]", "link"),
        ):
            if text.startswith(opening, index):
                token = (opening, closing, kind)
                break
        if token is not None:
            opening, closing, kind = token
            stack.append((closing, kind, index))
            index += len(opening)
            continue
        if text.startswith(("[https://", "[http://", "[//"), index):
            stack.append(("]", "external", index))
            index += 1
            continue
        if text.startswith(("}}", "]]"), index):
            raise SyncError("Wikitext 存在不匹配的闭合标记")
        if not stack and text[index] in separators:
            delimiters.append(index)
        index += 1
    if stack:
        raise SyncError("Wikitext 存在未闭合的模板或链接")
    return delimiters, sorted(spans, key=lambda span: span[1])


def split_top_level(text: str, separators: str = "|", maxsplit: int = -1) -> list[str]:
    positions, _ = _scan(text, separators)
    if maxsplit >= 0:
        positions = positions[:maxsplit]
    result = []
    start = 0
    for position in positions:
        result.append(text[start:position])
        start = position + 1
    result.append(text[start:])
    return result


@dataclass(frozen=True)
class WikiTemplate:
    name: str
    params: dict[str, str]
    raw: str


def parse_template(raw: str) -> WikiTemplate:
    parts = split_top_level(raw[2:-2])
    name = parts[0].strip()
    name = re.sub(r"^(?:Template|模板):", "", name, flags=re.I)
    params = {}
    positional = 1
    for part in parts[1:]:
        pair = split_top_level(part, "=", 1)
        if len(pair) == 2:
            key, value = (piece.strip() for piece in pair)
        else:
            key, value = str(positional), part.strip()
            positional += 1
        if not key or key in params:
            raise SyncError("模板 %s 存在空参数或重复参数: %s" % (name, key))
        params[key] = value
    return WikiTemplate(name, params, raw)


def templates(text: str) -> Iterator[WikiTemplate]:
    _, spans = _scan(text)
    for kind, start, end in spans:
        if kind == "template":
            yield parse_template(text[start:end])


def one_template(text: str, names: tuple[str, ...]) -> WikiTemplate:
    selected = [template for template in templates(text) if template.name in names]
    if len(selected) != 1:
        raise SyncError(
            "需要唯一的 %s 模板，实际 %d 个" % ("/".join(names), len(selected))
        )
    return selected[0]


def plain_text(value: str) -> str:
    """只展开可确认的展示包装，遇到未知模板拒绝猜测权威字段。"""
    value = re.sub(r"<!--[\s\S]*?-->", "", value)
    value = re.sub(r"<ref\b[^>]*(?:/>|>[\s\S]*?</ref\s*>)", "", value, flags=re.I)
    _, spans = _scan(value)
    result = []
    end = 0
    for kind, start, stop in spans:
        if start < end:
            continue
        result.append(value[end:start])
        raw = value[start:stop]
        if kind == "link":
            fields = split_top_level(raw[2:-2])
            result.append(plain_text(fields[-1]))
        elif kind == "template":
            template = parse_template(raw)
            if template.name == "!" and not template.params:
                result.append("|")
            elif template.name.lower() in (
                "nowrap",
                "nobr",
                "small",
                "big",
                "lang",
                "color",
            ):
                positional = [v for k, v in template.params.items() if k.isdecimal()]
                if not positional:
                    raise SyncError("展示包装模板没有文本")
                result.append(plain_text(positional[-1]))
            else:
                raise SyncError("权威字段包含未知模板: %s" % template.name)
        elif kind == "external":
            fields = raw[1:-1].split(None, 1)
            if len(fields) != 2:
                raise SyncError("权威字段只有外部链接、没有名称")
            result.append(plain_text(fields[1]))
        else:
            raise SyncError("权威字段含未求值的模板参数")
        end = stop
    result.append(value[end:])
    value = "".join(result)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = _TAG.sub("", value).replace("'''", "").replace("''", "")
    return html.unescape(value).strip()


def optional_integer(value: str) -> int | None:
    text = plain_text(value)
    if not text:
        return None
    if not re.fullmatch(r"[0-9]+", text):
        raise SyncError("喜好数值不是非负整数: %s" % text)
    return int(text)


def file_reference(value: str) -> str:
    value = html.unescape(value).strip()
    if value.startswith("[[") and value.endswith("]]"):
        value = split_top_level(value[2:-2])[0].strip()
    value = re.sub(r"^(?:File|Image|文件|檔案):", "", value, flags=re.I).strip()
    if not value or any(char in value for char in ("/", "\\", "|", "\n", "\r", "\x00")):
        raise SyncError("无效的 Wiki 文件引用")
    return "File:" + value


def name_index(characters: dict[str, dict]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for identifier, character in characters.items():
        for name in character["names"].values():
            matches = result.setdefault(normalize_name(name), [])
            if identifier not in matches:
                matches.append(identifier)
    return result


def parse_special_gifts(value: str, characters: dict[str, dict]) -> list[dict] | None:
    if not plain_text(value):
        return None  # 空字段表示来源未知，绝不等价于「没有额外偏好」。
    index = name_index(characters)
    entries = split_top_level(re.sub(r"<br\s*/?>", ";", value, flags=re.I), ";；\n")
    result = []
    seen = set()
    for entry in entries:
        if not entry.strip():
            continue
        pair = split_top_level(entry, ":：", 1)
        if len(pair) != 2:
            raise SyncError("special_gifts 缺少角色/数值分隔符")
        name = plain_text(pair[0])
        matches = index.get(normalize_name(name), [])
        if len(matches) != 1:
            raise SyncError("喜好引用未知或有歧义的角色: %s" % name)
        identifier = matches[0]
        likeability = optional_integer(pair[1])
        if likeability is None or identifier in seen:
            raise SyncError("喜好数值缺失或角色重复: %s" % name)
        seen.add(identifier)
        result.append({"character_id": identifier, "likeability": likeability})
    if not result:
        raise SyncError("special_gifts 有内容但无法提取喜好")
    return result


def parse_gift(
    text: str, characters: dict[str, dict]
) -> tuple[dict, WikiTemplate, str]:
    template = one_template(text, ("食物", "家具"))
    params = template.params
    identifier = plain_text(params.get("id", ""))
    name = plain_text(params.get("name", ""))
    if not identifier or not name:
        raise SyncError("礼物模板缺少原始 id 或完整 name")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise SyncError("尚未支持此原始礼物 ID 的安全文件名: %s" % identifier)
    record = {
        "id": identifier,
        "names": {"zh_cn": name},
        "kind": "food" if template.name == "食物" else "furniture",
        "default_likeability": optional_integer(params.get("default_likeability", "")),
        "special_gifts": parse_special_gifts(
            params.get("special_gifts", ""), characters
        ),
    }
    return record, template, file_reference(params.get("icon", ""))


def parse_character(
    text: str, title: str, pageid: int, evidence: list[str]
) -> tuple[dict, WikiTemplate]:
    template = one_template(text, ("角色信息",))
    names = {}
    languages = {"中": "zh_cn", "日": "ja_jp", "韩": "ko_kr", "英": "en_us"}
    for line in re.split(r"<br\s*/?>", template.params.get("姓名", ""), flags=re.I):
        line = plain_text(line)
        if not line:
            continue
        match = re.fullmatch(r"(.+?)[（(]([中日韩英])[）)]", line)
        if match is None:
            raise SyncError("未识别的多语姓名字段: %s" % line)
        name, marker = match.groups()
        language = languages[marker]
        if language in names:
            raise SyncError("同一角色重复提供语言名称")
        names[language] = name.strip()
    if not names.get("zh_cn") or normalize_name(names["zh_cn"]) != normalize_name(
        title
    ):
        raise SyncError("角色中文全名与来源标题不一致: %s" % title)
    protagonist = title == "异能者·零"
    return {
        "id": "char_%d" % pageid,
        "names": names,
        "giftable": bool(evidence) and not protagonist,
        "protagonist": protagonist,
    }, template


class MediaWikiClient:
    """编码所有参数、完整跟随 continue；网络失败直接终止，不猜测或静默跳页。"""

    def __init__(self, api_url: str = API_URL, opener: Callable = urlopen):
        self.api_url = api_url
        self.opener = opener
        self.requests = 0

    def read_url(self, url: str) -> bytes:
        parsed, origin = urlsplit(url), urlsplit(self.api_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != origin.netloc
            or parsed.username
        ):
            raise SyncError("拒绝非 Wiki 同源 HTTPS 下载")
        request = Request(
            url,
            headers={"User-Agent": "MaaNTE-gift-catalog/1.0 (explicit maintenance)"},
        )
        try:
            with self.opener(request, timeout=60) as response:
                payload = response.read(MAX_DOWNLOAD + 1)
        except (OSError, TimeoutError) as exc:
            raise SyncError("Wiki 请求失败: %s" % exc) from exc
        if len(payload) > MAX_DOWNLOAD:
            raise SyncError("来源响应超过内存下载上限")
        return payload

    def query(self, params: dict) -> dict:
        encoded = urlencode(
            {"action": "query", "format": "json", "formatversion": 2, **params}
        )
        payload = self.read_url(self.api_url + "?" + encoded)
        self.requests += 1
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise SyncError("Wiki API 未返回有效 JSON") from exc
        if not isinstance(result, dict) or "error" in result or "query" not in result:
            raise SyncError("Wiki API 返回错误或不完整数据: %s" % str(result)[:300])
        return result

    def continued(self, params: dict) -> Iterator[dict]:
        continuation = {}
        seen = set()
        while True:
            result = self.query({**params, **continuation})
            yield result
            if "continue" not in result:
                return
            continuation = result["continue"]
            if not isinstance(continuation, dict) or not continuation:
                raise SyncError("无效的 API continue")
            fingerprint = json.dumps(continuation, sort_keys=True)
            if fingerprint in seen:
                raise SyncError("Wiki API 重复返回同一 continue")
            seen.add(fingerprint)

    def category_members(self, category: str) -> tuple[list[dict], int]:
        members = {}
        batches = 0
        for result in self.continued(
            {
                "list": "categorymembers",
                "cmtitle": category,
                "cmprop": "ids|title",
                "cmnamespace": 0,
                "cmtype": "page",
                "cmlimit": 50,
            }
        ):
            batches += 1
            rows = result["query"].get("categorymembers")
            if not isinstance(rows, list):
                raise SyncError("分类响应缺少 categorymembers")
            for row in rows:
                if (
                    row.get("ns") != 0
                    or type(row.get("pageid")) is not int
                    or not row.get("title")
                ):
                    raise SyncError("分类中出现非预期页面")
                if row["pageid"] in members:
                    raise SyncError("分类分页出现重复页面")
                members[row["pageid"]] = {
                    "pageid": row["pageid"],
                    "title": row["title"],
                }
        if not members:
            raise SyncError("分类为空: %s" % category)
        return sorted(members.values(), key=lambda row: row["pageid"]), batches

    def pages(
        self, values: list, *, by_ids: bool = False, imageinfo: bool = False
    ) -> dict:
        output = {}
        for offset in range(0, len(values), 25):
            batch = values[offset : offset + 25]
            params = {"pageids" if by_ids else "titles": "|".join(map(str, batch))}
            if imageinfo:
                params.update(
                    prop="imageinfo", iiprop="url|sha1|size|timestamp|mime", iilimit=1
                )
            else:
                params.update(
                    prop="revisions|categories",
                    rvprop="ids|timestamp|content",
                    rvslots="main",
                    cllimit="max",
                )
            pages = {}
            aliases = {}
            for result in self.continued(params):
                query = result["query"]
                for group in ("normalized", "converted", "redirects"):
                    for alias in query.get(group, []):
                        aliases[alias["from"]] = alias["to"]
                rows = query.get("pages", [])
                if isinstance(rows, dict):
                    rows = list(rows.values())
                for page in rows:
                    if "missing" in page or "invalid" in page or not page.get("pageid"):
                        raise SyncError("来源页面或图片缺失: %s" % page.get("title"))
                    key = page["pageid"] if by_ids else page["title"]
                    if key not in pages:
                        pages[key] = page
                    else:
                        previous = pages[key]
                        if previous.get("revisions") and page.get("revisions"):
                            if (
                                previous["revisions"][0]["revid"]
                                != page["revisions"][0]["revid"]
                            ):
                                raise SyncError(
                                    "分页时来源修订发生改变，请重新维护同步"
                                )
                        categories = {
                            row["title"]: row for row in previous.get("categories", [])
                        }
                        categories.update(
                            {row["title"]: row for row in page.get("categories", [])}
                        )
                        previous.update(
                            {
                                key: value
                                for key, value in page.items()
                                if key != "categories"
                            }
                        )
                        previous["categories"] = list(categories.values())
            for value in batch:
                normalized = value
                visited = set()
                while normalized in aliases:
                    if normalized in visited:
                        raise SyncError("Wiki 标题归一化存在循环")
                    visited.add(normalized)
                    normalized = aliases[normalized]
                if normalized not in pages:
                    raise SyncError("API 未返回请求的页面: %s" % value)
                page = pages[normalized]
                if imageinfo and not page.get("imageinfo"):
                    raise SyncError("Wiki 未提供原图 imageinfo: %s" % value)
                if not imageinfo and not page.get("revisions"):
                    raise SyncError("Wiki 未提供页面修订: %s" % value)
                output[value] = page
        return output


def revision_text(page: dict) -> str:
    try:
        text = page["revisions"][0]["slots"]["main"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SyncError("页面修订没有 main wikitext 内容") from exc
    if not isinstance(text, str):
        raise SyncError("页面 wikitext 类型无效")
    return text


def page_source(page: dict) -> dict:
    revision = page["revisions"][0]
    return {
        "title": page["title"],
        "pageid": page["pageid"],
        "revid": revision["revid"],
        "url": wiki_url(page["title"]),
        "timestamp": revision["timestamp"],
    }


def normalized_sha1(value: str) -> str:
    # MediaWiki API 有些 SHA-1 省略前导零，不能把 39 位值误当成坏文件。
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{1,40}", value):
        raise SyncError("imageinfo 的 SHA-1 格式无效")
    return value.zfill(40)


def preview_bytes(payload: bytes) -> tuple[bytes, tuple[int, int]]:
    try:
        with Image.open(io.BytesIO(payload)) as source:
            if (
                source.width * source.height > 16_000_000
                or getattr(source, "n_frames", 1) != 1
            ):
                raise SyncError("来源图片太大或包含动画")
            source.verify()
        with Image.open(io.BytesIO(payload)) as source:
            preview = source.convert("RGBA")
        preview.thumbnail((256, 256), Image.Resampling.LANCZOS, reducing_gap=3.0)
        output = io.BytesIO()
        preview.save(output, format="PNG", optimize=False, compress_level=9)
        return output.getvalue(), preview.size
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError("来源图片无法完整解码") from exc


def preview_path(kind: str, identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise SyncError("无法生成安全的图片路径")
    return "Gift/%s/%s.png" % ("Gifts" if kind == "gifts" else "Characters", identifier)


def gallery_snapshot(client: MediaWikiClient) -> dict:
    widget = client.pages(["Widget:CharacterGallery"])["Widget:CharacterGallery"]
    content = revision_text(widget)
    pattern = (
        r"var\s+DATA_URL\s*=\s*['\"]("
        + re.escape(GALLERY_PATH)
        + r")\?t=['\"]\s*\+\s*Date\.now\(\);"
    )
    reference = re.search(pattern, content)
    if reference is None:
        raise SyncError("角色图鉴未引用已核实的小头像数据，拒绝推测图名")
    url = "https://" + urlsplit(client.api_url).netloc + GALLERY_PATH
    payload = client.read_url(url)
    try:
        rows = json.loads(payload)
    except ValueError as exc:
        raise SyncError("角色图鉴数据不是 JSON") from exc
    if not isinstance(rows, list) or not rows:
        raise SyncError("角色图鉴映射为空")
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(key), str) and row[key]
            for key in ("img", "link", "name")
        ):
            raise SyncError("角色图鉴缺少明确的角色/图像映射")
        url_parts = urlsplit(row["img"])
        if url_parts.hostname != urlsplit(
            client.api_url
        ).hostname or not url_parts.path.startswith("/images/"):
            raise SyncError("角色图鉴存在未核实的图片站点")
    return {
        "url": url,
        "response_sha256": sha256(payload),
        "snapshot_sha256": sha256(canonical_json(rows)),
        "widget": page_source(widget),
        "reference": reference.group(0),
        "rows": rows,
    }


def choose_portrait(title: str, gallery: dict) -> tuple[str, dict]:
    rows = [row for row in gallery["rows"] if row["link"] == title]
    if not rows:
        raise SyncError("图鉴没有角色头像映射: %s" % title)
    if len(rows) != 1 and title != "异能者·零":
        raise SyncError("角色存在无法解释的多个头像映射: %s" % title)
    # 主角两种性别共享一个不可赠礼记录，展示图保留来源顺序的第一张。
    row = rows[0]
    filename = unquote(urlsplit(row["img"]).path.rsplit("/", 1)[-1])
    return file_reference(filename), row


def snapshot_id(documents: dict[str, dict]) -> str:
    items = {
        kind: [
            [record["id"], record["source"]["revid"], record["image"]["source_sha1"]]
            for record in documents[kind][kind]
        ]
        for kind in ("gifts", "characters")
    }
    return sha256(canonical_json({"recipe": RECIPE, "items": items}))


def source_license(rightsinfo: dict) -> dict:
    return {
        "status": "permission_not_confirmed",
        "game_artwork": "《异环》（Neverness to Everness）游戏美术归游戏及各素材原权利人所有。",
        "wiki_content": "页面文本由异环信息站贡献者提供；来源可追溯不等于取得再分发许可。",
        "api_rightsinfo": rightsinfo,
        "redistribution_requires_permission": True,
        "permission_confirmed": False,
        "notice": "未确认开放授权；这些素材不适用本项目代码的 AGPL 许可。再分发前须向相应权利人确认许可。本次仅本地维护，不发布。",
    }


def collect_documents(client: MediaWikiClient) -> dict[str, dict]:
    categories = {}
    category_pages = client.pages([GIFT_CATEGORY, *CHARACTER_CATEGORIES])
    for category in (GIFT_CATEGORY, *CHARACTER_CATEGORIES):
        members, batches = client.category_members(category)
        categories[category] = {
            "title": category,
            "url": wiki_url(category),
            "page_count": len(members),
            "query_batches": batches,
            "revision": page_source(category_pages[category]),
            "members": members,
        }
        LOG.info("分类 %s: %d 页，%d 次分页响应", category, len(members), batches)
    gallery = gallery_snapshot(client)
    character_members = {}
    for category in CHARACTER_CATEGORIES:
        for row in categories[category]["members"]:
            character_members[row["pageid"]] = row
    all_members = {row["pageid"]: row for row in categories[GIFT_CATEGORY]["members"]}
    all_members.update(character_members)
    pages = client.pages(sorted(all_members), by_ids=True)
    for category, snapshot in categories.items():
        expected_category = category.replace("Category:", "分类:", 1)
        for member in snapshot["members"]:
            page = pages[member["pageid"]]
            if page["title"] != member["title"] or expected_category not in {
                entry["title"] for entry in page.get("categories", [])
            }:
                raise SyncError("来源页面与分类快照不一致，请重新维护同步")
    characters = {}
    for pageid in sorted(character_members):
        page = pages[pageid]
        text = revision_text(page)
        evidence = re.findall(r"^\|\s*收到赠礼[^\n]*", text, re.M)
        record, template = parse_character(text, page["title"], pageid, evidence)
        image_file, portrait = choose_portrait(page["title"], gallery)
        record["source"] = {
            **page_source(page),
            "template": template.raw,
            "template_sha256": sha256(template.raw.encode("utf-8")),
            "giftability_evidence": evidence,
            "image_file": image_file,
            "portrait_reference": portrait,
        }
        characters[record["id"]] = record
    gifts = {}
    for member in categories[GIFT_CATEGORY]["members"]:
        page = pages[member["pageid"]]
        record, template, image_file = parse_gift(revision_text(page), characters)
        if record["id"] in gifts:
            raise SyncError("Wiki 礼物原始 ID 重复: %s" % record["id"])
        record["source"] = {
            **page_source(page),
            "template": template.raw,
            "template_sha256": sha256(template.raw.encode("utf-8")),
            "image_file": image_file,
        }
        gifts[record["id"]] = record
    image_files = list(
        dict.fromkeys(
            record["source"]["image_file"]
            for record in [*gifts.values(), *characters.values()]
        )
    )
    images = client.pages(image_files, imageinfo=True)
    for record in [*gifts.values(), *characters.values()]:
        page = images[record["source"]["image_file"]]
        info = page["imageinfo"][0]
        normalized_sha1(info.get("sha1"))
        if info.get("mime") not in ("image/png", "image/jpeg", "image/webp"):
            raise SyncError("不支持的原图 MIME 类型")
        record["source"]["imageinfo"] = {
            "title": page["title"],
            "pageid": page["pageid"],
            **info,
        }
    # 网络取数期间若分类发生变化，不把「部分新+部分旧」冒充完整同步。
    for category, snapshot in categories.items():
        current, _ = client.category_members(category)
        if current != snapshot["members"]:
            raise SyncError("采集期间分类发生变动，请重新执行维护同步")
    rights = client.query({"meta": "siteinfo", "siprop": "rightsinfo"})["query"][
        "rightsinfo"
    ]
    collected_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    common = {
        "url": client.api_url,
        "collected_at": collected_at,
        "license": source_license(rights),
        "generation": {
            "tool": "tools/sync_gift_catalog.py",
            "recipe": RECIPE,
            "pillow": PILLOW_VERSION,
            "max_image_edge": 256,
            "runtime_network": False,
        },
    }
    return {
        "gifts": {
            "schema_version": 1,
            "source": {**common, "categories": [categories[GIFT_CATEGORY]]},
            "gifts": [gifts[key] for key in sorted(gifts)],
        },
        "characters": {
            "schema_version": 1,
            "source": {
                **common,
                "categories": [categories[key] for key in CHARACTER_CATEGORIES],
                "portrait_gallery": gallery,
            },
            "characters": list(characters.values()),
        },
    }


def stage_images(
    client: MediaWikiClient, documents: dict, stage: Path, cache: Catalog | None
) -> None:
    count = 0
    cached_count = 0
    for kind in ("gifts", "characters"):
        previous = getattr(cache, kind) if cache is not None else {}
        for record in documents[kind][kind]:
            info = record["source"]["imageinfo"]
            expected_sha1 = normalized_sha1(info["sha1"])
            relative = preview_path(kind, record["id"])
            target = stage / "image" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            old = previous.get(record["id"])
            cache_recipe = (
                cache.documents[kind]["source"].get("generation", {}).get("recipe")
                if cache
                else None
            )
            if (
                old
                and old["image"]["source_sha1"] == expected_sha1
                and cache_recipe == RECIPE
            ):
                existing = cache.base / "image" / old["image"]["path"]
                # NTFS 硬链接复用已校验的小图，避免缓存图产生额外磁盘副本。
                os.link(existing, target)
                cached_count += 1
                preview = target.read_bytes()
                dimensions = (old["image"]["width"], old["image"]["height"])
            else:
                payload = client.read_url(info["url"])
                if hashlib.sha1(payload).hexdigest() != expected_sha1:
                    raise SyncError("原图 SHA-1 与 imageinfo 不符: %s" % info["title"])
                with Image.open(io.BytesIO(payload)) as image:
                    if image.size != (info["width"], info["height"]):
                        raise SyncError("原图尺寸与 imageinfo 不符")
                preview, dimensions = preview_bytes(payload)
                target.write_bytes(preview)
            record["image"] = {
                "path": relative,
                "sha256": sha256(preview),
                "source_url": info["url"],
                "source_sha1": expected_sha1,
                "width": dimensions[0],
                "height": dimensions[1],
                "purpose": "display_and_candidate_only",
                "can_authorize_gift": False,
                "source_description_url": info["descriptionurl"],
                "source_width": info["width"],
                "source_height": info["height"],
                "recipe": RECIPE,
            }
            count += 1
            if count % 25 == 0:
                LOG.info("已准备 %d 张展示小图", count)
    LOG.info(
        "图片准备完成：%d 张新派生、%d 张复用现有小图",
        count - cached_count,
        cached_count,
    )
    identifier = snapshot_id(documents)
    for document in documents.values():
        document["source"]["snapshot_id"] = identifier


def _check_generated_config(base: Path, catalog: Catalog) -> None:
    path = ROOT / "tools" / "gift_task_config.py"
    if (
        path.is_file()
        and base.resolve() == (ROOT / "assets" / "resource" / "base").resolve()
    ):
        generator = _load_module("_maante_gift_task_config", path)
        problems = generator.check(
            ROOT,
            characters=catalog.documents["characters"],
            gifts=catalog.documents["gifts"],
        )
        if problems:
            raise SyncError(
                "任务配置生成结果不一致: %s" % "; ".join(map(str, problems))
            )


def check_catalog(base: Path, *, check_generated: bool = True) -> Catalog:
    """只读校验全库、PNG、来源快照和能离线重放的生成字段。"""
    catalog = Catalog.load(base)
    documents = catalog.documents
    gallery = documents["characters"]["source"].get("portrait_gallery")
    if not isinstance(gallery, dict) or sha256(
        canonical_json(gallery.get("rows"))
    ) != gallery.get("snapshot_sha256"):
        raise SyncError("角色图鉴映射快照损坏")
    expected_snapshot = snapshot_id(documents)
    for kind in ("gifts", "characters"):
        document = documents[kind]
        if (base / "data" / (kind + ".json")).read_bytes() != canonical_json(document):
            raise SyncError("%s.json 不是可重现的规范 JSON" % kind)
        if document["source"].get("snapshot_id") != expected_snapshot:
            raise SyncError("同步快照校验失败")
        if document["source"].get("generation", {}).get("recipe") != RECIPE:
            raise SyncError("未知的小图生成规则")
        for record in document[kind]:
            source = record["source"]
            template = source.get("template", "")
            if sha256(template.encode("utf-8")) != source.get("template_sha256"):
                raise SyncError("来源模板快照损坏")
            if kind == "gifts":
                regenerated, _, image_file = parse_gift(template, catalog.characters)
            else:
                regenerated, _ = parse_character(
                    template,
                    source["title"],
                    source["pageid"],
                    source["giftability_evidence"],
                )
                image_file, reference = choose_portrait(source["title"], gallery)
                if reference != source["portrait_reference"]:
                    raise SyncError("头像与已核实的角色映射不一致")
            for key, value in regenerated.items():
                if record.get(key) != value:
                    raise SyncError(
                        "%s 字段无法从来源快照重现: %s" % (record["id"], key)
                    )
            if source["image_file"] != image_file:
                raise SyncError("图片引用无法从来源重现")
            image = record["image"]
            info = source["imageinfo"]
            if image["path"] != preview_path(kind, record["id"]):
                raise SyncError("展示图路径不符合生成规则")
            if image["source_url"] != info["url"] or image[
                "source_sha1"
            ] != normalized_sha1(info["sha1"]):
                raise SyncError("原图 URL/SHA-1 与 imageinfo 证据不一致")
            if (image.get("source_width"), image.get("source_height")) != (
                info["width"],
                info["height"],
            ):
                raise SyncError("原图尺寸证据不一致")
            if image["width"] > info["width"] or image["height"] > info["height"]:
                raise SyncError("展示图不能放大原图")
            payload = (base / "image" / image["path"]).read_bytes()
            canonical, _ = preview_bytes(payload)
            if canonical != payload:
                raise SyncError("展示图不是可重复生成的规范 PNG")
    if check_generated:
        _check_generated_config(base, catalog)
    return catalog


def publish_stage(stage: Path, base: Path, documents: dict) -> None:
    """先完整校验；替换失败时反向恢复库与小图，不留下半套主库。"""
    check_catalog(stage, check_generated=False)
    relatives = [
        Path("image") / record["image"]["path"]
        for kind in ("gifts", "characters")
        for record in documents[kind][kind]
    ] + [Path("data/gifts.json"), Path("data/characters.json")]
    journal = []
    try:
        for relative in relatives:
            incoming, target = stage / relative, base / relative
            if not target.resolve().is_relative_to(base.resolve()):
                raise SyncError("目标资源目录含越界符号链接")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and incoming.read_bytes() == target.read_bytes():
                continue
            backup = stage / "previous" / relative
            had_previous = target.exists()
            if had_previous:
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
            journal.append((target, backup, had_previous))
            os.replace(incoming, target)
    except BaseException:
        recovery_errors = []
        for target, backup, had_previous in reversed(journal):
            try:
                if target.exists():
                    target.unlink()
                if had_previous:
                    os.replace(backup, target)
            except OSError as exc:
                recovery_errors.append(str(exc))
        if recovery_errors:
            raise RecoveryError(
                "回滚被文件锁阻止；请保留临时目录 %s 并人工恢复: %s"
                % (stage, "; ".join(recovery_errors))
            )
        raise


def synchronize(
    base: Path, client: MediaWikiClient | None = None, *, data_only: bool = False
) -> Catalog:
    client = client or MediaWikiClient()
    documents = collect_documents(client)
    cache = None
    if all(
        (base / "data" / (kind + ".json")).is_file() for kind in ("gifts", "characters")
    ):
        try:
            cache = Catalog.load(base)
        except CatalogError:
            LOG.warning("旧目录未通过校验，本次不复用缓存图")
    base.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".gift-sync-", dir=base))
    preserve_stage = False
    try:
        stage_images(client, documents, stage, cache)
        (stage / "data").mkdir(parents=True, exist_ok=True)
        for kind, document in documents.items():
            (stage / "data" / (kind + ".json")).write_bytes(canonical_json(document))
        publish_stage(stage, base, documents)
    except RecoveryError:
        preserve_stage = True
        raise
    finally:
        if not preserve_stage:
            shutil.rmtree(stage)
    catalog = check_catalog(base, check_generated=False)
    if (
        not data_only
        and base.resolve() == (ROOT / "assets" / "resource" / "base").resolve()
    ):
        helper = ROOT / "tools" / "gift_task_config.py"
        if not helper.is_file():
            raise SyncError(
                "两库已完整同步；配置生成器缺失，可使用 --data-only 仅维护数据"
            )
        generator = _load_module("_maante_gift_task_config", helper)
        generator.update(
            ROOT,
            characters=catalog.documents["characters"],
            gifts=catalog.documents["gifts"],
        )
        _check_generated_config(base, catalog)
    return catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="完全离线，只读校验主库、小图与派生配置"
    )
    mode.add_argument(
        "--sync", action="store_true", help="显式联网维护，整体验证后替换两库和小图"
    )
    parser.add_argument(
        "--base", type=Path, default=ROOT / "assets" / "resource" / "base"
    )
    parser.add_argument(
        "--data-only",
        action="store_true",
        help="仅与 --sync 使用，跳过任务配置派生文件更新",
    )
    args = parser.parse_args(argv)
    if args.check and args.data_only:
        parser.error("--data-only 仅可与 --sync 同时使用")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        catalog = (
            check_catalog(args.base)
            if args.check
            else synchronize(args.base, data_only=args.data_only)
        )
    except (CatalogError, SyncError, OSError, ValueError) as exc:
        LOG.error("目录维护失败: %s", exc)
        return 2
    LOG.info(
        "%s完成：%d 礼物、%d 角色（%d 可赠礼）；运行时离线，图片不授权送礼",
        "只读校验" if args.check else "同步",
        len(catalog.gifts),
        len(catalog.characters),
        sum(character["giftable"] for character in catalog.characters.values()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
