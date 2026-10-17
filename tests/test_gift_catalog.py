"""赠礼目录的纯离线回归：真实全库只读，变异与发布测试仅写临时目录。"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gift_sync_tested", ROOT / "tools" / "sync_gift_catalog.py"
)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)
Catalog = sync.Catalog
CatalogError = sync.CatalogError


def source(title, pageid):
    return {
        "title": title,
        "pageid": pageid,
        "revid": pageid + 10000,
        "url": sync.wiki_url(title),
        "timestamp": "2026-09-20T00:00:00Z",
    }


def fixture(base, color=(64, 128, 192, 200)):
    """生成可重放的微型来源快照，测试不依赖 Wiki 或运行时 Maa。"""
    image_stream = io.BytesIO()
    Image.new("RGBA", (12, 16), color).save(image_stream, format="PNG")
    original = image_stream.getvalue()
    preview, size = sync.preview_bytes(original)
    characters = {}
    gallery_rows = []
    seeds = [
        (14, "异能者·零", "異能者·零", "이능력자·제로", "Esper Zero"),
        (129, "安魂曲", "レクイエム", "라크리모사", "Lacrimosa"),
        (142, "娜娜莉", "ナナリ", "나나리", "Nanally"),
        (240, "白藏", "白藏", "백장", "Baicang"),
    ]
    for pageid, zh, ja, ko, en in seeds:
        raw = (
            "{{角色信息|姓名=%s（中）<br>%s（日）<br>%s（韩）<br>%s（英）|图片1=[[File:Large.png|300px]]}}"
            % (zh, ja, ko, en)
        )
        evidence = [] if pageid == 14 else ["| 收到赠礼·Ⅰ || 谢谢。"]
        record, template = sync.parse_character(raw, zh, pageid, evidence)
        portrait = {
            "img": "https://www.ntestation.com/images/a/ab/Character_%d.png" % pageid,
            "name": zh,
            "link": zh,
        }
        gallery_rows.append(portrait)
        record["source"] = {
            **source(zh, pageid),
            "template": template.raw,
            "template_sha256": sync.sha256(template.raw.encode()),
            "giftability_evidence": evidence,
            "image_file": "File:Character_%d.png" % pageid,
            "portrait_reference": portrait,
        }
        characters[record["id"]] = record
    gifts = []
    gift_seeds = [
        (
            1154,
            "{{食物|id=Food_003|name=冰点冷萃|icon=Food_0003.png|default_likeability=50|special_gifts=白藏:100}}",
        ),
        (
            1795,
            "{{家具|id=Furniture_draw_004|name=9331|icon=Furniture_draw_004_256.png|default_likeability=100|special_gifts=}}",
        ),
    ]
    for pageid, raw in gift_seeds:
        record, template, image_file = sync.parse_gift(raw, characters)
        record["source"] = {
            **source(record["names"]["zh_cn"], pageid),
            "template": template.raw,
            "template_sha256": sync.sha256(template.raw.encode()),
            "image_file": image_file,
        }
        gifts.append(record)
    documents = {}
    for kind, rows in (("gifts", gifts), ("characters", list(characters.values()))):
        for record in rows:
            url = "https://www.ntestation.com/images/a/ab/" + record["id"] + ".png"
            record["source"]["imageinfo"] = {
                "url": url,
                "sha1": hashlib.sha1(original).hexdigest(),
                "title": record["source"]["image_file"],
                "pageid": record["source"]["pageid"] + 20000,
                "width": 12,
                "height": 16,
                "size": len(original),
                "mime": "image/png",
                "descriptionurl": sync.wiki_url(record["source"]["image_file"]),
                "timestamp": "2026-09-20T00:00:00Z",
            }
            relative = sync.preview_path(kind, record["id"])
            record["image"] = {
                "path": relative,
                "sha256": sync.sha256(preview),
                "source_url": url,
                "source_sha1": hashlib.sha1(original).hexdigest(),
                "width": size[0],
                "height": size[1],
                "purpose": "display_and_candidate_only",
                "can_authorize_gift": False,
                "source_description_url": record["source"]["imageinfo"][
                    "descriptionurl"
                ],
                "source_width": 12,
                "source_height": 16,
                "recipe": sync.RECIPE,
            }
            target = base / "image" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(preview)
        category = "Category:礼物" if kind == "gifts" else "Category:S级角色"
        documents[kind] = {
            "schema_version": 1,
            "source": {
                "url": sync.API_URL,
                "collected_at": "2026-09-20T00:00:00Z",
                "license": sync.source_license({"url": "", "text": ""}),
                "generation": {"recipe": sync.RECIPE, "runtime_network": False},
                "categories": [
                    {
                        "title": category,
                        "url": sync.wiki_url(category),
                        "page_count": len(rows),
                        "members": [
                            {
                                "pageid": record["source"]["pageid"],
                                "title": record["source"]["title"],
                            }
                            for record in rows
                        ],
                    }
                ],
            },
            kind: rows,
        }
    documents["characters"]["source"]["portrait_gallery"] = {
        "url": "https://www.ntestation.com" + sync.GALLERY_PATH,
        "rows": gallery_rows,
        "snapshot_sha256": sync.sha256(sync.canonical_json(gallery_rows)),
    }
    identifier = sync.snapshot_id(documents)
    for document in documents.values():
        document["source"]["snapshot_id"] = identifier
    write_documents(base, documents)
    return documents, original


def write_documents(base, documents):
    (base / "data").mkdir(parents=True, exist_ok=True)
    for kind, document in documents.items():
        (base / "data" / (kind + ".json")).write_bytes(sync.document_json(document))


def legacy_fixture(base):
    """模拟发布过的 PNG9 / 缩进主库，不依赖网络重新获取原图。"""
    documents, original = fixture(base)
    recipe = "rgba-lanczos-256-png9-v1"
    for kind, document in documents.items():
        document["source"]["generation"]["recipe"] = recipe
        for record in document[kind]:
            image = record["image"]
            previous = base / "image" / image["path"]
            relative = Path(image["path"]).with_suffix(".png").as_posix()
            stream = io.BytesIO()
            with Image.open(io.BytesIO(previous.read_bytes())) as pixels:
                pixels.save(stream, format="PNG", compress_level=9)
            payload = stream.getvalue()
            (base / "image" / relative).write_bytes(payload)
            previous.unlink()
            image.update(path=relative, sha256=sync.sha256(payload), recipe=recipe)
    with patch.object(sync, "RECIPE", recipe):
        identifier = sync.snapshot_id(documents)
    for kind, document in documents.items():
        document["source"]["snapshot_id"] = identifier
        (base / "data" / (kind + ".json")).write_bytes(sync.canonical_json(document))
    return documents, original


def fingerprint(base):
    return {
        str(path.relative_to(base)): (
            path.stat().st_mtime_ns,
            sync.sha256(path.read_bytes()),
        )
        for path in base.rglob("*")
        if path.is_file()
    }


class WikitextTests(unittest.TestCase):
    def setUp(self):
        self.characters = {
            "char_240": {"names": {"zh_cn": "白藏"}},
            "char_142": {"names": {"zh_cn": "娜娜莉", "en_us": "Nanally"}},
        }

    def test_nested_template_links_pipes_and_equal_signs(self):
        raw = "{{食物|id=Food_007|name={{nowrap|[[冰点冷萃|冰点冷萃]]}}|description={{说明|x=[[A|B=C]]|y={{{参数|a|b}}}}}|icon=[[File:Food_007.png|64px]]|default_likeability=50|special_gifts=[[白藏|{{nowrap|白藏}}]]:{{small|100}}；{{nowrap|娜娜莉}}:200}}"
        record, template, image = sync.parse_gift(raw, self.characters)
        self.assertEqual(record["names"], {"zh_cn": "冰点冷萃"})
        self.assertEqual(record["id"], "Food_007")
        self.assertEqual(
            record["special_gifts"],
            [
                {"character_id": "char_240", "likeability": 100},
                {"character_id": "char_142", "likeability": 200},
            ],
        )
        self.assertIn("{{{参数|a|b}}}", template.params["description"])
        self.assertEqual(image, "File:Food_007.png")

    def test_nowiki_comments_and_html_attributes_do_not_split_fields(self):
        raw = "name=<span title='a|b'>完整名称</span>|description=<nowiki>x|y={{z}}</nowiki><!-- | fake=yes -->|id=Food_1"
        parts = sync.split_top_level(raw)
        self.assertEqual(len(parts), 3)
        self.assertEqual(sync.plain_text(parts[0].split("=", 1)[1]), "完整名称")

    def test_encoded_icon_pipe_and_nested_infobox(self):
        text = "{{Hide|外层|{{家具|id=F_1|name=[[9331]]|icon=[[File:Furniture_256.png&#124;64px]]|default_likeability=0|special_gifts=}}}}"
        record, _, image = sync.parse_gift(text, self.characters)
        self.assertEqual(image, "File:Furniture_256.png")
        self.assertEqual(record["default_likeability"], 0)
        self.assertIsNone(record["special_gifts"])

    def test_empty_preferences_are_unknown_not_zero_or_empty_list(self):
        for value in ("", " \n ", "<!-- 未录入 -->"):
            self.assertIsNone(sync.parse_special_gifts(value, self.characters))
            self.assertIsNone(sync.optional_integer(value))
        self.assertEqual(sync.optional_integer("0"), 0)

    def test_invalid_preferences_are_not_silently_dropped(self):
        for value in (
            "未知角色:100",
            "白藏:",
            "白藏:10；白藏:20",
            "白藏:abc",
            "白藏",
            "白藏:-1",
            ";;",
        ):
            with self.subTest(value=value), self.assertRaises(sync.SyncError):
                sync.parse_special_gifts(value, self.characters)

    def test_unbalanced_and_duplicate_templates_fail_closed(self):
        for value in (
            "{{家具|id=x",
            "{{家具|id=x|id=y}}",
            "{{家具|name=[[缺失}}",
            "<!-- 缺失",
        ):
            with self.subTest(value=value), self.assertRaises(sync.SyncError):
                list(sync.templates(value))
        with self.assertRaises(sync.SyncError):
            sync.plain_text("{{未知逻辑|这个不是可确认名称}}")

    def test_character_names_do_not_follow_filename_guesses(self):
        raw = "{{角色信息|姓名=安魂曲（中）<br>レクイエム（日）<br>라크리모사（韩）<br>Lacrimosa（英）|图片1=[[File:Requiem Normal.png|300px]]}}"
        record, _ = sync.parse_character(raw, "安魂曲", 129, ["| 收到赠礼·Ⅰ || 谢谢。"])
        self.assertEqual(record["id"], "char_129")
        self.assertEqual(record["names"]["en_us"], "Lacrimosa")
        self.assertNotIn("zh_tw", record["names"])
        self.assertTrue(record["giftable"])
        record, _ = sync.parse_character(raw, "安魂曲", 129, [])
        self.assertFalse(record["giftable"])

    def test_original_sha1_leading_zero_and_small_preview(self):
        self.assertEqual(sync.normalized_sha1("f" * 39), "0" + "f" * 39)
        image = io.BytesIO()
        Image.new("RGBA", (800, 400), (10, 20, 30, 80)).save(image, format="PNG")
        preview, dimensions = sync.preview_bytes(image.getvalue())
        self.assertEqual(dimensions, (256, 128))
        self.assertEqual(sync.preview_bytes(preview)[0], preview)
        with Image.open(io.BytesIO(preview)) as decoded:
            self.assertEqual(decoded.mode, "RGBA")
        with self.assertRaises(sync.SyncError):
            sync.preview_bytes(b"this is not an image")


class APITests(unittest.TestCase):
    @staticmethod
    def client(responses):
        requests = []

        def opener(request, timeout):
            requests.append(parse_qs(urlsplit(request.full_url).query))
            value = responses[len(requests) - 1]
            return io.BytesIO(json.dumps(value, ensure_ascii=False).encode())

        return sync.MediaWikiClient(opener=opener), requests

    def test_category_continue_parameters_are_fully_encoded(self):
        token = "冰|文,+="
        client, requests = self.client(
            [
                {
                    "query": {
                        "categorymembers": [{"pageid": 1, "ns": 0, "title": "冰点冷萃"}]
                    },
                    "continue": {"cmcontinue": token, "continue": "-||"},
                },
                {
                    "query": {
                        "categorymembers": [
                            {"pageid": 2, "ns": 0, "title": "家具|测试"}
                        ]
                    }
                },
            ]
        )
        members, batches = client.category_members("Category:礼物")
        self.assertEqual([row["pageid"] for row in members], [1, 2])
        self.assertEqual(batches, 2)
        self.assertEqual(requests[0]["cmtitle"], ["Category:礼物"])
        self.assertEqual(requests[1]["cmcontinue"], [token])
        self.assertEqual(requests[1]["continue"], ["-||"])
        self.assertEqual(requests[1]["action"], ["query"])

    def test_repeated_continue_or_page_is_an_error(self):
        response = {
            "query": {"categorymembers": [{"pageid": 1, "ns": 0, "title": "一"}]},
            "continue": {"cmcontinue": "same"},
        }
        client, _ = self.client([response, response])
        with self.assertRaises(sync.SyncError):
            client.category_members("Category:礼物")
        client, _ = self.client([{"query": {}, "continue": {"continue": "same"}}] * 3)
        with self.assertRaises(sync.SyncError):
            list(client.continued({"meta": "siteinfo"}))

    def test_revision_category_pagination_is_merged(self):
        client, _ = self.client(
            [
                {
                    "query": {
                        "pages": [
                            {
                                "pageid": 1,
                                "title": "一",
                                "revisions": [{"revid": 5}],
                                "categories": [{"title": "分类:礼物"}],
                            }
                        ]
                    },
                    "continue": {"clcontinue": "1|另一类", "continue": "||"},
                },
                {
                    "query": {
                        "pages": [
                            {
                                "pageid": 1,
                                "title": "一",
                                "categories": [{"title": "分类:食物"}],
                            }
                        ]
                    }
                },
            ]
        )
        page = client.pages([1], by_ids=True)[1]
        self.assertEqual(len(page["categories"]), 2)
        self.assertEqual(page["revisions"][0]["revid"], 5)

    def test_imageinfo_normalization_uses_returned_original_url(self):
        url = "https://www.ntestation.com/images/1/12/Original.png"
        client, requests = self.client(
            [
                {
                    "query": {
                        "normalized": [
                            {"from": "File:food_0003.png", "to": "文件:Food 0003.png"}
                        ],
                        "pages": [
                            {
                                "pageid": 17,
                                "title": "文件:Food 0003.png",
                                "imageinfo": [{"url": url, "sha1": "f" * 40}],
                            }
                        ],
                    }
                }
            ]
        )
        page = client.pages(["File:food_0003.png"], imageinfo=True)[
            "File:food_0003.png"
        ]
        self.assertEqual(page["imageinfo"][0]["url"], url)
        self.assertEqual(requests[0]["prop"], ["imageinfo"])
        self.assertIn("sha1", requests[0]["iiprop"][0])

    def test_revision_requests_are_batched_without_losing_order(self):
        requests = []

        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            ids = list(map(int, query["pageids"][0].split("|")))
            requests.append(ids)
            pages = [
                {
                    "pageid": value,
                    "title": str(value),
                    "revisions": [{"revid": value + 100}],
                }
                for value in reversed(ids)
            ]
            return io.BytesIO(json.dumps({"query": {"pages": pages}}).encode())

        client = sync.MediaWikiClient(opener=opener)
        values = list(range(1, 52))
        self.assertEqual(list(client.pages(values, by_ids=True)), values)
        self.assertEqual(list(map(len, requests)), [25, 25, 1])

    def test_missing_pages_api_errors_and_foreign_urls_fail(self):
        for response in (
            {"error": {"code": "badvalue"}},
            {"query": {"pages": [{"title": "丢失", "missing": True}]}},
        ):
            client, _ = self.client([response])
            with self.assertRaises(sync.SyncError):
                client.pages(["丢失"])
        client = sync.MediaWikiClient(
            opener=Mock(side_effect=AssertionError("不得联网"))
        )
        for url in (
            "http://www.ntestation.com/x",
            "https://example.com/x",
            "https://user@www.ntestation.com/x",
        ):
            with self.subTest(url=url), self.assertRaises(sync.SyncError):
                client.read_url(url)
        client.opener.assert_not_called()


class PreviewTests(unittest.TestCase):
    def test_document_storage_does_not_change_source_hash_contract(self):
        value = {"name": "白藏", "rows": [1, None]}
        self.assertEqual(
            sync.document_json(value), '{"name":"白藏","rows":[1,null]}\n'.encode()
        )
        self.assertEqual(
            sync.canonical_json(value),
            (json.dumps(value, ensure_ascii=False, indent=4) + "\n").encode(),
        )
        self.assertEqual(json.loads(sync.document_json(value)), value)
        with self.assertRaises(ValueError):
            sync.document_json({"invalid": float("nan")})

    def test_webp_is_exact_including_transparent_rgb_and_is_idempotent(self):
        source_image = Image.new("RGBA", (17, 19))
        source_image.putdata(
            [
                ((index * 17) % 256, (index * 37) % 256, index % 256, index % 4 * 85)
                for index in range(17 * 19)
            ]
        )
        source = io.BytesIO()
        source_image.save(source, format="PNG", compress_level=9)
        original = source.getvalue()
        preview, size = sync.preview_bytes(original)
        self.assertEqual(size, source_image.size)
        with Image.open(io.BytesIO(preview)) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertEqual(image.convert("RGBA").tobytes(), source_image.tobytes())
        self.assertEqual(
            cv2.imdecode(
                np.frombuffer(preview, np.uint8), cv2.IMREAD_UNCHANGED
            ).tobytes(),
            cv2.imdecode(
                np.frombuffer(original, np.uint8), cv2.IMREAD_UNCHANGED
            ).tobytes(),
        )
        self.assertEqual(sync.preview_bytes(preview), (preview, size))
        self.assertIn(b"VP8L", preview)

    def test_pixel_verifier_rejects_changes_and_failed_opencv_decode(self):
        streams = []
        for color in ((23, 41, 67, 0), (23, 41, 68, 0)):
            stream = io.BytesIO()
            Image.new("RGBA", (2, 2), color).save(stream, format="PNG")
            streams.append(stream.getvalue())
        with self.assertRaisesRegex(sync.SyncError, "RGBA"):
            sync.verify_lossless_pixels(*streams)
        with patch.object(cv2, "imdecode", return_value=None):
            with self.assertRaisesRegex(sync.SyncError, "OpenCV"):
                sync.preview_bytes(streams[0])
        with patch.object(
            cv2, "imdecode", return_value=np.zeros((2, 2, 4), dtype=np.uint8)
        ):
            with self.assertRaisesRegex(sync.SyncError, "BGRA"):
                sync.preview_bytes(streams[0])

    def test_source_dimension_guard_precedes_decoding(self):
        source = Mock(width=4097, height=4097, n_frames=1)
        opened = Mock()
        opened.__enter__ = Mock(return_value=source)
        opened.__exit__ = Mock(return_value=False)
        with patch.object(sync.Image, "open", return_value=opened):
            with self.assertRaisesRegex(sync.SyncError, "太大"):
                sync.preview_bytes(b"large image header")
        source.verify.assert_not_called()
        source.convert.assert_not_called()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gift-catalog-test-")
        self.base = Path(self.temporary.name) / "base"
        self.documents, self.original = fixture(self.base)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self):
        write_documents(self.base, self.documents)

    def test_nfkc_whitespace_only_normalization(self):
        self.assertEqual(sync.normalize_name(" ＡＢＣ　奶 茶\t\n"), "ABC奶茶")
        self.assertNotEqual(sync.normalize_name("ABC"), sync.normalize_name("abc"))
        with self.assertRaises(CatalogError):
            sync.normalize_name(None)

    def test_exact_ids_full_names_and_stable_order(self):
        catalog = Catalog.load(self.base)
        self.assertEqual(list(catalog.gifts), ["Food_003", "Furniture_draw_004"])
        self.assertEqual(catalog.resolve_gift(" 冰 点冷萃\n"), "Food_003")
        self.assertEqual(catalog.resolve_gift("Food_003"), "Food_003")
        for name in ("", "冷萃", "冰点冷翠", "food_003", "不存在"):
            with self.subTest(name=name), self.assertRaises(CatalogError):
                catalog.resolve_gift(name)
        self.assertEqual(catalog.gift_names("Food_003"), ("冰点冷萃",))
        self.assertEqual(
            catalog.character_names("char_240"), ("白藏", "백장", "Baicang")
        )
        with self.assertRaises(CatalogError):
            catalog.character_names("240")

    def test_priority_any_length_deduplicates_in_order(self):
        catalog = Catalog.load(self.base)
        self.assertEqual(
            catalog.parse_priority(
                "9331 > 冰点冷萃 > Furniture_draw_004 > Food_003 > 9331 > 冰点冷萃"
            ),
            ("Furniture_draw_004", "Food_003"),
        )
        for value in (
            "",
            "冰点冷萃>>9331",
            "冰点冷萃>",
            ">冰点冷萃",
            "冰点冷萃>错别字",
            "白藏",
            "char_142",
            "冰点冷萃>娜娜莉",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                catalog.parse_priority(value)

    def test_empty_preferences_remain_none(self):
        self.documents["gifts"]["gifts"][1]["default_likeability"] = None
        self.write()
        record = Catalog.load(self.base).gifts["Furniture_draw_004"]
        self.assertIsNone(record["default_likeability"])
        self.assertIsNone(record["special_gifts"])

    def test_duplicate_ids_and_json_keys_are_rejected(self):
        self.documents["gifts"]["gifts"].append(
            copy.deepcopy(self.documents["gifts"]["gifts"][0])
        )
        self.write()
        with self.assertRaisesRegex(CatalogError, "重复"):
            Catalog.load(self.base)
        path = self.base / "data" / "gifts.json"
        path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "重复键"):
            Catalog.load(self.base)

    def test_ambiguous_names_require_stable_ids(self):
        self.documents["gifts"]["gifts"][1]["names"]["zh_cn"] = "冰 点冷萃"
        self.write()
        catalog = Catalog.load(self.base)
        with self.assertRaisesRegex(CatalogError, "歧义"):
            catalog.resolve_gift("冰点冷萃")
        with self.assertRaisesRegex(CatalogError, "歧义"):
            catalog.parse_priority("Food_003 > 冰点冷萃")
        self.assertEqual(catalog.resolve_gift("Food_003"), "Food_003")
        self.assertEqual(
            catalog.parse_priority("Food_003 > Furniture_draw_004 > Food_003"),
            ("Food_003", "Furniture_draw_004"),
        )

    def test_unknown_character_reference_and_invalid_integer(self):
        record = self.documents["gifts"]["gifts"][0]
        record["special_gifts"][0]["character_id"] = "char_999999"
        self.write()
        with self.assertRaises(CatalogError):
            Catalog.load(self.base)
        record["special_gifts"] = None
        for value in (True, -1, "100"):
            record["default_likeability"] = value
            self.write()
            with self.subTest(value=value), self.assertRaises(CatalogError):
                Catalog.load(self.base)

    def test_protagonist_and_unproven_giftability_fail_closed(self):
        protagonist = self.documents["characters"]["characters"][0]
        protagonist["giftable"] = True
        self.write()
        with self.assertRaises(CatalogError):
            Catalog.load(self.base)
        protagonist["giftable"] = False
        self.documents["characters"]["characters"][1]["source"][
            "giftability_evidence"
        ] = []
        self.write()
        with self.assertRaises(CatalogError):
            Catalog.load(self.base)

    def test_path_traversal_absolute_windows_and_wrong_directory(self):
        image = self.documents["gifts"]["gifts"][0]["image"]
        for path in (
            "../outside.png",
            "Gift/Gifts/../outside.png",
            "/absolute.png",
            "C:/outside.png",
            "Gift\\Gifts\\bad.png",
            "Gift/Characters/mix.png",
            "Gift//Gifts/bad.png",
            "Gift/Gifts/bad.gif",
            "Gift/Gifts/./bad.webp",
            "Gift/Gifts/../bad.webp",
            "Gift/Characters/mix.webp",
            "Gift\\Gifts\\bad.webp",
            "Gift//Gifts/bad.webp",
            "Gift/Gifts/C:bad.webp",
        ):
            image["path"] = path
            self.write()
            with self.subTest(path=path), self.assertRaises(CatalogError):
                Catalog.load(self.base)

    def test_symlink_cannot_escape_image_root(self):
        image = self.documents["gifts"]["gifts"][0]["image"]
        path = self.base / "image" / image["path"]
        outside = Path(self.temporary.name) / "outside.png"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("当前 Windows 策略未允许创建符号链接")
        with self.assertRaises(CatalogError):
            Catalog.load(self.base)

    def test_missing_bad_and_hash_mismatched_images(self):
        image = self.documents["gifts"]["gifts"][0]["image"]
        path = self.base / "image" / image["path"]
        path.unlink()
        with self.assertRaises(CatalogError):
            Catalog.load(self.base)
        path.write_bytes(b"bad png")
        with self.assertRaisesRegex(CatalogError, "SHA-256"):
            Catalog.load(self.base)
        image["sha256"] = sync.sha256(b"bad png")
        self.write()
        with self.assertRaisesRegex(CatalogError, "解码"):
            Catalog.load(self.base)

    def test_oversized_wrong_dimensions_and_authorizing_images_rejected(self):
        image = self.documents["gifts"]["gifts"][0]["image"]
        for key, value in (
            ("width", 257),
            ("width", 11),
            ("can_authorize_gift", True),
            ("purpose", "recognition_authorizes_gift"),
        ):
            original = image[key]
            image[key] = value
            self.write()
            with self.subTest(key=key, value=value), self.assertRaises(CatalogError):
                Catalog.load(self.base)
            image[key] = original

    def test_category_coverage_and_cross_library_snapshot(self):
        records = self.documents["gifts"]["gifts"]
        removed = records.pop()
        self.write()
        with self.assertRaisesRegex(CatalogError, "完整收录"):
            Catalog.load(self.base)
        records.append(removed)
        self.documents["gifts"]["source"]["snapshot_id"] = "b" * 64
        self.write()
        with self.assertRaisesRegex(CatalogError, "同一个"):
            Catalog.load(self.base)

    def test_check_replays_templates_and_does_not_trust_changed_fields(self):
        record = self.documents["gifts"]["gifts"][0]
        record["default_likeability"] = 999
        self.write()
        with self.assertRaisesRegex(sync.SyncError, "重现"):
            sync.check_catalog(self.base)
        record["default_likeability"] = 50
        record["source"]["template_sha256"] = "b" * 64
        self.write()
        with self.assertRaisesRegex(sync.SyncError, "模板快照"):
            sync.check_catalog(self.base)

    def test_check_is_offline_and_does_not_write_or_reencode_files(self):
        before = fingerprint(self.base)
        forbidden = AssertionError("--check 不得联网或写文件")
        with (
            patch.object(sync.MediaWikiClient, "read_url", side_effect=forbidden),
            patch("socket.socket", side_effect=forbidden),
            patch.object(Path, "write_bytes", side_effect=forbidden),
            patch.object(Path, "write_text", side_effect=forbidden),
            patch.object(Path, "mkdir", side_effect=forbidden),
            patch.object(Path, "unlink", side_effect=forbidden),
            patch.object(os, "replace", side_effect=forbidden),
        ):
            self.assertEqual(sync.main(["--check", "--base", str(self.base)]), 0)
        self.assertEqual(fingerprint(self.base), before)

    def test_cache_reuses_verified_small_images_without_network(self):
        stage = Path(self.temporary.name) / "stage"
        cache = Catalog.load(self.base)
        client = Mock()
        client.read_url.side_effect = AssertionError("缓存命中时不应下载")
        documents = copy.deepcopy(self.documents)
        sync.stage_images(client, documents, stage, cache)
        client.read_url.assert_not_called()
        self.assertEqual(
            documents["gifts"]["gifts"][0]["image"]["sha256"],
            self.documents["gifts"]["gifts"][0]["image"]["sha256"],
        )

    def test_failed_publish_rolls_back_images_and_both_libraries(self):
        stage = Path(self.temporary.name) / "stage"
        documents, _ = fixture(stage, color=(250, 80, 30, 255))
        before = fingerprint(self.base)
        real_replace = os.replace

        def replace(src, dst):
            if Path(src) == stage / "data" / "characters.json":
                raise OSError("模拟第二主库写入失败")
            return real_replace(src, dst)

        with patch.object(sync.os, "replace", side_effect=replace):
            with self.assertRaises(OSError):
                sync.publish_stage(stage, self.base, documents)
        self.assertEqual(fingerprint(self.base), before)
        sync.check_catalog(self.base)

    def test_failed_first_publish_does_not_leave_half_a_library(self):
        stage = Path(self.temporary.name) / "stage"
        documents, _ = fixture(stage)
        empty = Path(self.temporary.name) / "empty"
        empty.mkdir()
        real_replace = os.replace

        def replace(src, dst):
            if Path(src) == stage / "data" / "characters.json":
                raise OSError("模拟首次发布中途失败")
            return real_replace(src, dst)

        with patch.object(sync.os, "replace", side_effect=replace):
            with self.assertRaises(OSError):
                sync.publish_stage(stage, empty, documents)
        self.assertEqual(fingerprint(empty), {})

    def test_bad_stage_is_validated_before_touching_existing_catalog(self):
        stage = Path(self.temporary.name) / "stage"
        documents, _ = fixture(stage)
        documents["gifts"]["gifts"][0]["default_likeability"] = 999
        write_documents(stage, documents)
        before = fingerprint(self.base)
        with self.assertRaises(sync.SyncError):
            sync.publish_stage(stage, self.base, documents)
        self.assertEqual(fingerprint(self.base), before)

    def test_sync_download_failure_removes_temporary_data_and_preserves_old(self):
        documents = copy.deepcopy(self.documents)
        documents["gifts"]["gifts"][0]["source"]["imageinfo"]["sha1"] = "f" * 40
        client = Mock()
        client.read_url.return_value = b"not expected image"
        before = fingerprint(self.base)
        with patch.object(sync, "collect_documents", return_value=documents):
            with self.assertRaisesRegex(sync.SyncError, "SHA-1"):
                sync.synchronize(self.base, client, data_only=True)
        self.assertEqual(fingerprint(self.base), before)
        self.assertFalse(list(self.base.glob(".gift-sync-*")))

    def test_maintenance_updates_generated_config_only_after_complete_validation(self):
        root = Path(self.temporary.name) / "package"
        base = root / "assets" / "resource" / "base"
        documents, _ = fixture(base)
        helper_file = root / "tools" / "gift_task_config.py"
        helper_file.parent.mkdir(parents=True)
        helper_file.write_text("# 临时替身，不执行。\n", encoding="utf-8")

        def update(project, *, characters, gifts):
            self.assertEqual(project, root)
            self.assertEqual(characters["schema_version"], 1)
            self.assertEqual(gifts["schema_version"], 1)
            sync.check_catalog(base, check_generated=False)
            return []

        helper = Mock()
        helper.update.side_effect = update
        helper.check.return_value = []
        client = Mock()
        client.read_url.side_effect = AssertionError("应复用缓存，不下载")
        with (
            patch.object(sync, "ROOT", root),
            patch.object(
                sync, "collect_documents", return_value=copy.deepcopy(documents)
            ),
            patch.object(sync, "_load_module", return_value=helper),
        ):
            for operation in (
                lambda data_only: sync.synchronize(base, client, data_only=data_only),
                lambda data_only: sync.optimize(base, data_only=data_only),
            ):
                operation(False)
                helper.update.assert_called_once()
                helper.check.assert_called_once()
                helper.reset_mock()
                operation(True)
                helper.update.assert_not_called()
                helper.check.assert_not_called()

    def test_failed_rollback_preserves_image_only_backup_for_recovery(self):
        def fail_publish(stage, base, documents):
            backup = stage / "previous" / "image" / "old.png"
            backup.parent.mkdir(parents=True)
            backup.write_bytes(b"must preserve")
            raise sync.RecoveryError("模拟外部文件锁阻止回滚")

        with (
            patch.object(
                sync, "collect_documents", return_value=copy.deepcopy(self.documents)
            ),
            patch.object(sync, "publish_stage", side_effect=fail_publish),
        ):
            for mode, operation in (
                ("sync", lambda: sync.synchronize(self.base, Mock(), data_only=True)),
                ("optimize", lambda: sync.optimize(self.base, data_only=True)),
            ):
                with self.subTest(mode=mode), self.assertRaises(sync.RecoveryError):
                    operation()
                stages = list(self.base.glob(".gift-%s-*" % mode))
                self.assertEqual(len(stages), 1)
                self.assertEqual(
                    (stages[0] / "previous" / "image" / "old.png").read_bytes(),
                    b"must preserve",
                )

    def test_png_catalog_remains_runtime_compatible(self):
        documents, _ = legacy_fixture(self.base)
        loaded = Catalog.load(self.base)
        self.assertEqual(loaded.documents, documents)
        self.assertEqual(
            loaded.parse_priority("冰点冷萃 > 9331"), ("Food_003", "Furniture_draw_004")
        )

    def test_suffix_must_match_decoded_format_for_png_and_webp(self):
        image = self.documents["gifts"]["gifts"][0]["image"]
        webp = (self.base / "image" / image["path"]).read_bytes()
        for suffix, payload in ((".png", webp), (".webp", self.original)):
            image["path"] = Path(image["path"]).with_suffix(suffix).as_posix()
            image["sha256"] = sync.sha256(payload)
            (self.base / "image" / image["path"]).write_bytes(payload)
            self.write()
            with (
                self.subTest(suffix=suffix),
                self.assertRaisesRegex(CatalogError, "格式"),
            ):
                Catalog.load(self.base)

    def test_truncated_and_animated_images_rejected_with_matching_hash(self):
        image = self.documents["gifts"]["gifts"][0]["image"]
        for image_format, suffix in (("PNG", ".png"), ("WEBP", ".webp")):
            image["path"] = Path(image["path"]).with_suffix(suffix).as_posix()
            path = self.base / "image" / image["path"]
            valid = io.BytesIO()
            animated = io.BytesIO()
            first = Image.new("RGBA", (12, 16), (255, 0, 0, 255))
            first.save(valid, format=image_format, lossless=True, exact=True)
            first.save(
                animated,
                format=image_format,
                save_all=True,
                append_images=[Image.new("RGBA", (12, 16), (0, 255, 0, 255))],
                lossless=True,
                exact=True,
                duration=100,
                loop=0,
            )
            for payload in (valid.getvalue()[:-12], animated.getvalue()):
                path.write_bytes(payload)
                image["sha256"] = sync.sha256(payload)
                self.write()
                with self.subTest(format=image_format, size=len(payload)):
                    with self.assertRaises(CatalogError):
                        Catalog.load(self.base)
                    with self.assertRaises(sync.SyncError):
                        sync.preview_bytes(payload)

    def test_file_size_guard_rejects_even_hash_matching_data(self):
        image = self.documents["gifts"]["gifts"][0]["image"]
        path = self.base / "image" / image["path"]
        payload = path.read_bytes() + bytes(2 * 1024 * 1024)
        path.write_bytes(payload)
        image["sha256"] = sync.sha256(payload)
        self.write()
        with self.assertRaisesRegex(CatalogError, "体积异常"):
            Catalog.load(self.base)

    def test_check_rejects_changed_image_recipe_and_source_evidence(self):
        image = self.documents["gifts"]["gifts"][0]["image"]
        for key, value in (
            ("recipe", "lossy-webp"),
            ("source_url", "https://www.ntestation.com/images/other.png"),
            ("source_sha1", "f" * 40),
            ("source_description_url", sync.wiki_url("File:Other.png")),
            ("source_width", 13),
            ("source_height", 17),
        ):
            original = image[key]
            image[key] = value
            self.write()
            with self.subTest(key=key), self.assertRaises(sync.SyncError):
                sync.check_catalog(self.base, check_generated=False)
            image[key] = original


class OptimizeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gift-optimize-test-")
        self.base = Path(self.temporary.name) / "base"
        self.documents, _ = legacy_fixture(self.base)

    def tearDown(self):
        self.temporary.cleanup()

    def test_optimize_is_offline_exact_preserves_metadata_and_is_idempotent(self):
        original = copy.deepcopy(self.documents)
        payloads = {
            record["id"]: (self.base / "image" / record["image"]["path"]).read_bytes()
            for kind, document in original.items()
            for record in document[kind]
        }
        forbidden = AssertionError("离线优化不得联网")
        with (
            patch.object(sync, "collect_documents", side_effect=forbidden),
            patch.object(sync.MediaWikiClient, "read_url", side_effect=forbidden),
            patch("socket.socket", side_effect=forbidden),
        ):
            self.assertEqual(
                sync.main(["--optimize", "--data-only", "--base", str(self.base)]), 0
            )
        optimized = sync.check_catalog(self.base, check_generated=False)
        normalized = copy.deepcopy(optimized.documents)
        for kind, document in normalized.items():
            self.assertEqual(document["source"]["generation"]["recipe"], sync.RECIPE)
            self.assertEqual(
                document["source"]["snapshot_id"], sync.snapshot_id(optimized.documents)
            )
            self.assertNotEqual(
                document["source"]["snapshot_id"],
                original[kind]["source"]["snapshot_id"],
            )
            document["source"]["generation"]["recipe"] = original[kind]["source"][
                "generation"
            ]["recipe"]
            document["source"]["snapshot_id"] = original[kind]["source"]["snapshot_id"]
            for previous, record in zip(original[kind][kind], document[kind]):
                image = record["image"]
                path = self.base / "image" / image["path"]
                self.assertEqual(image["path"], sync.preview_path(kind, record["id"]))
                self.assertEqual(image["recipe"], sync.RECIPE)
                self.assertEqual(image["sha256"], sync.sha256(path.read_bytes()))
                with (
                    Image.open(io.BytesIO(payloads[record["id"]])) as before,
                    Image.open(path) as after,
                ):
                    self.assertEqual(before.size, after.size)
                    self.assertEqual(
                        before.convert("RGBA").tobytes(),
                        after.convert("RGBA").tobytes(),
                    )
                for key in ("path", "sha256", "recipe"):
                    image[key] = previous["image"][key]
        self.assertEqual(normalized, original)
        self.assertFalse(list((self.base / "image").rglob("*.png")))
        self.assertEqual(
            len(list((self.base / "image").rglob("*.webp"))), len(payloads)
        )
        before = fingerprint(self.base)
        sync.optimize(self.base, data_only=True)
        self.assertEqual(fingerprint(self.base), before)
        self.assertEqual(
            sync.main(["--check", "--data-only", "--base", str(self.base)]), 0
        )
        self.assertEqual(fingerprint(self.base), before)
        self.assertFalse(list(self.base.glob(".gift-optimize-*")))

    def test_failed_pixel_validation_leaves_original_library_and_no_stage(self):
        before = fingerprint(self.base)
        with (
            patch.object(
                sync, "verify_lossless_pixels", side_effect=sync.SyncError("像素不一致")
            ),
            patch.object(sync, "_update_generated_config") as update,
        ):
            with self.assertRaisesRegex(sync.SyncError, "像素"):
                sync.optimize(self.base)
            update.assert_not_called()
        self.assertEqual(fingerprint(self.base), before)
        self.assertFalse(list(self.base.glob(".gift-optimize-*")))

    def test_bad_local_input_is_rejected_before_staging(self):
        record = self.documents["gifts"]["gifts"][0]
        path = self.base / "image" / record["image"]["path"]
        path.write_bytes(b"bad png")
        record["image"]["sha256"] = sync.sha256(path.read_bytes())
        write_documents(self.base, self.documents)
        before = fingerprint(self.base)
        with self.assertRaises(CatalogError):
            sync.optimize(self.base, data_only=True)
        self.assertEqual(fingerprint(self.base), before)
        self.assertFalse(list(self.base.glob(".gift-optimize-*")))

    def test_bad_source_replay_keeps_old_png_and_both_libraries(self):
        self.documents["gifts"]["gifts"][0]["default_likeability"] = 999
        write_documents(self.base, self.documents)
        before = fingerprint(self.base)
        with self.assertRaisesRegex(sync.SyncError, "重现"):
            sync.optimize(self.base, data_only=True)
        self.assertEqual(fingerprint(self.base), before)
        self.assertFalse(list(self.base.glob(".gift-optimize-*")))

    def test_failure_removing_old_png_rolls_back_new_images_and_both_libraries(self):
        paths = [
            self.base / "image" / record["image"]["path"]
            for record in self.documents["gifts"]["gifts"]
        ]
        before = fingerprint(self.base)
        real_replace = os.replace
        removed = []

        def replace(src, dst):
            if Path(src) in paths:
                removed.append(Path(src))
                if len(removed) == 2:
                    raise OSError("模拟清理第二张旧图失败")
            return real_replace(src, dst)

        with (
            patch.object(sync.os, "replace", side_effect=replace),
            patch.object(sync, "_update_generated_config") as update,
        ):
            with self.assertRaisesRegex(OSError, "清理"):
                sync.optimize(self.base)
            update.assert_not_called()
        self.assertEqual(len(removed), 2)
        self.assertEqual(fingerprint(self.base), before)
        self.assertFalse(list(self.base.glob(".gift-optimize-*")))
        Catalog.load(self.base)


class CompleteLibraryTests(unittest.TestCase):
    def test_real_gift_priority_accepts_full_names_and_rejects_characters(self):
        catalog = Catalog.load(ROOT / "assets" / "resource" / "base")
        self.assertEqual(
            catalog.parse_priority(
                "溶月茶具 > 时令寿司船 > Food_003 > 9331 > 溶月茶具"
            ),
            ("Furniture_Ornament_026", "Food_034", "Food_003", "Furniture_draw_004"),
        )
        self.assertEqual(
            catalog.parse_priority(" > ".join(catalog.gifts)), tuple(catalog.gifts)
        )
        for text in ("白藏", "娜娜莉", "char_14", "溶月茶具 > 白藏", "溶月"):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(CatalogError, "未知礼物"),
            ):
                catalog.parse_priority(text)

    def test_checked_in_catalog_covers_every_captured_page_and_is_offline(self):
        base = ROOT / "assets" / "resource" / "base"
        with patch("socket.socket", side_effect=AssertionError("全库检查不得联网")):
            catalog = sync.check_catalog(base)
        self.assertGreaterEqual(len(catalog.gifts), 214)
        self.assertGreaterEqual(len(catalog.characters), 24)
        self.assertFalse(catalog.characters["char_14"]["giftable"])
        self.assertEqual(catalog.gifts["Food_003"]["names"]["zh_cn"], "冰点冷萃")
        self.assertEqual(catalog.characters["char_129"]["names"]["en_us"], "Lacrimosa")
        self.assertTrue(
            any(record["special_gifts"] is None for record in catalog.gifts.values())
        )
        self.assertEqual(
            sum(
                category["page_count"]
                for category in catalog.documents["gifts"]["source"]["categories"]
            ),
            len(catalog.gifts),
        )
        self.assertTrue(
            all(
                record["image"]["can_authorize_gift"] is False
                for record in [*catalog.gifts.values(), *catalog.characters.values()]
            )
        )

    def test_default_and_packaged_base_resolution(self):
        catalog = Catalog.load()
        self.assertGreaterEqual(len(catalog.gifts), 214)
        with tempfile.TemporaryDirectory(prefix="gift-package-test-") as temporary:
            package = Path(temporary)
            base = package / "resource" / "base"
            fixture(base)
            pretend_module = (
                package / "agent" / "custom" / "action" / "Gift" / "catalog.py"
            )
            with patch.object(sync._catalog, "__file__", str(pretend_module)):
                self.assertEqual(Catalog.load().base, base.resolve())


if __name__ == "__main__":
    unittest.main()
