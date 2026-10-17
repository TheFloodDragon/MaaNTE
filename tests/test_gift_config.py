"""赠礼配置回归：纯离线生成、独立覆盖、五语与共享文件保护。"""

from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import gift_task_config as config

try:
    from jsonschema.validators import validator_for
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
except ImportError:
    validator_for = None


REASONS = {
    "invalid_config",
    "no_recipients",
    "unknown_character",
    "character_not_found",
    "identity",
    "gift_not_found",
    "gift_identity",
    "inventory",
    "quota",
    "quantity",
    "infinite",
    "global_limit",
    "budget",
    "page",
    "navigation",
    "recovery",
    "ambiguous",
    "scan",
    "receipt",
    "receipt_timeout",
    "controller",
    "stopping",
    "dry_run",
    "exhausted",
    "catalog",
    "internal",
}
MESSAGE_FORMATS = {
    "gift.started": ["%d", "%s"],
    "gift.mode.dry_run": [],
    "gift.mode.execute": [],
    "gift.character": ["%s", "%d"],
    "gift.candidate": ["%s"],
    "gift.exhausted": ["%s"],
    "gift.sent": ["%s", "%s", "%d", "%d"],
    "gift.already_done": ["%s"],
    "gift.dry_run": ["%s", "%s"],
    "gift.warning": ["%s", "%s"],
    "gift.summary": ["%d"] * 5,
    **{"gift.reason.%s" % reason: [] for reason in REASONS},
}


def catalogs():
    """Test-only identities; no guessed game translations are added to resources."""
    characters = {
        "schema_version": 1,
        "characters": [
            {
                "id": "char_14",
                "names": {"zh_cn": "测试主角"},
                "source": {"pageid": 14},
                "image": {"path": "Gift/Characters/char_14.png"},
                "giftable": False,
            },
            {
                "id": "char_1951",
                "names": {"zh_cn": "测试甲", "en_us": "Verified test alpha"},
                "source": {"pageid": 1951},
                "image": {"path": "Gift/Characters/char_1951.png"},
                "giftable": True,
            },
            {
                "id": "char_1952",
                "names": {"zh_cn": "测试乙"},
                "source": {"pageid": 1952},
                "image": {"path": "Gift/Characters/char_1952.png"},
                "giftable": True,
            },
        ],
    }
    gifts = {
        "schema_version": 1,
        "gifts": [
            {
                "id": "Food_003",
                "names": {"zh_cn": "测试礼物甲", "en_us": "Verified test gift"},
                "source": {"pageid": 1},
                "image": {"path": "Gift/Gifts/Food_003.png"},
                "default_likeability": 10,
                "special_gifts": [],
            },
            {
                "id": "Furniture_draw_004",
                "names": {"zh_cn": "测试礼物乙"},
                "source": {"pageid": 2},
                "image": {"path": "Gift/Gifts/Furniture_draw_004.png"},
                "default_likeability": None,
                "special_gifts": [],
            },
        ],
    }
    return characters, gifts


def merge(base, override):
    """Model the interface's recursive merge, retaining different node namespaces."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def references(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)
    elif isinstance(value, str) and value.startswith("$"):
        yield value[1:]


def schema_validator(filename):
    """Resolve every schema reference locally; never fetch schemas from the network."""
    directory = ROOT / "deps/tools"
    registry = Registry()
    for name in (
        filename,
        "custom.action.schema.json",
        "custom.recognition.schema.json",
    ):
        path = directory / name
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(
            path.as_uri(),
            Resource.from_contents(schema, default_specification=DRAFT202012),
        )
    path = directory / filename
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["$id"] = path.as_uri()
    cls = validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, registry=registry)


class GiftRenderingTests(unittest.TestCase):
    def setUp(self):
        self.characters, self.gifts = catalogs()
        self.task = config.render_task(self.characters, self.gifts)
        self.nodes = config.render_config(self.characters)

    def test_daily_task_has_no_automatic_selection(self):
        task = self.task["task"][0]
        self.assertEqual(
            (task["name"], task["entry"], task["group"]),
            ("Gift", "GiftEntry", ["Daily"]),
        )
        self.assertNotIn("default_check", task)
        self.assertNotIn("default_selected", task)
        recipients = self.task["option"]["GiftRecipients"]
        self.assertEqual(recipients["type"], "checkbox")
        self.assertEqual(recipients["default_case"], [])
        self.assertEqual(
            [case["name"] for case in recipients["cases"]], ["char_1951", "char_1952"]
        )
        self.assertNotIn("GiftConfig_char_14", self.nodes)

    def test_recipient_options_only_override_their_own_attach(self):
        seen = set()
        for case in self.task["option"]["GiftRecipients"]["cases"]:
            identifier = case["name"]
            node = "GiftConfig_%s" % identifier
            self.assertEqual(
                case["pipeline_override"], {node: {"attach": {"selected": True}}}
            )
            self.assertNotIn(node, seen)
            seen.add(node)
            self.assertEqual(
                case["option"],
                ["GiftPriority_%s" % identifier, "GiftLimit_%s" % identifier],
            )
            self.assertEqual(
                case["icon"], "resource/base/image/Gift/Characters/%s.png" % identifier
            )
            for option in case["option"]:
                override = self.task["option"][option]["pipeline_override"]
                self.assertEqual(set(override), {node})
                self.assertEqual(set(override[node]), {"attach"})

    def test_checkbox_merge_keeps_all_selected_characters_and_their_values(self):
        merged = copy.deepcopy(self.nodes)
        cases = self.task["option"]["GiftRecipients"]["cases"]
        for index, case in enumerate(cases):
            merge(merged, case["pipeline_override"])
            priority = self.task["option"][case["option"][0]]["pipeline_override"]
            limit = self.task["option"][case["option"][1]]["pipeline_override"]
            node = "GiftConfig_%s" % case["name"]
            gift_override = copy.deepcopy(priority)
            gift_override[node]["attach"]["gifts"] = (
                "Food_003 > Furniture_draw_004" if index == 0 else "Food_003"
            )
            limit_override = copy.deepcopy(limit)
            limit_override[node]["attach"]["limit"] = index + 1
            merge(merged, gift_override)
            merge(merged, limit_override)
        self.assertEqual(
            merged["GiftConfig_char_1951"]["attach"],
            {
                "character_id": "char_1951",
                "selected": True,
                "gifts": "Food_003 > Furniture_draw_004",
                "limit": 1,
            },
        )
        self.assertEqual(
            merged["GiftConfig_char_1952"]["attach"],
            {
                "character_id": "char_1952",
                "selected": True,
                "gifts": "Food_003",
                "limit": 2,
            },
        )
        one_selected = copy.deepcopy(self.nodes)
        merge(one_selected, cases[0]["pipeline_override"])
        self.assertFalse(one_selected["GiftConfig_char_1952"]["attach"]["selected"])

    def test_inputs_are_unbounded_priority_and_integer_daily_total(self):
        for case in self.task["option"]["GiftRecipients"]["cases"]:
            priority = self.task["option"][case["option"][0]]
            limit = self.task["option"][case["option"][1]]
            self.assertEqual(len(priority["inputs"]), 1)
            self.assertEqual(priority["inputs"][0]["name"], "gifts")
            self.assertEqual(priority["inputs"][0]["default"], "")
            self.assertEqual(priority["inputs"][0]["pipeline_type"], "string")
            self.assertNotIn("verify", priority["inputs"][0])
            field = limit["inputs"][0]
            self.assertEqual(
                (field["name"], field["default"], field["pipeline_type"]),
                ("limit", "3", "int"),
            )
            self.assertEqual(field["verify"], "^[1-3]$")
            for valid in ("1", "2", "3"):
                self.assertIsNotNone(re.fullmatch(field["verify"], valid))
            for invalid in ("", "0", "4", "03", "-1", "1.5", "3\n"):
                self.assertIsNone(re.fullmatch(field["verify"], invalid))
            node = "GiftConfig_%s" % case["name"]
            self.assertEqual(
                priority["pipeline_override"], {node: {"attach": {"gifts": "{gifts}"}}}
            )
            self.assertEqual(
                limit["pipeline_override"], {node: {"attach": {"limit": "{limit}"}}}
            )

    def test_data_nodes_are_inert_and_start_with_safe_defaults(self):
        self.assertEqual(self.nodes["GiftSettings"]["attach"], {"dry_run": False})
        for name, node in self.nodes.items():
            self.assertEqual(node["recognition"], {"type": "DirectHit"})
            self.assertEqual(node["action"], {"type": "DoNothing"})
            for field in ("rate_limit", "pre_delay", "post_delay"):
                self.assertEqual(node[field], 0)
            for field in ("next", "on_error", "timeout", "max_hit"):
                self.assertNotIn(field, node)
            if name != "GiftSettings":
                self.assertEqual(
                    node["attach"],
                    {
                        "character_id": name.removeprefix("GiftConfig_"),
                        "selected": False,
                        "gifts": "",
                        "limit": 3,
                    },
                )

    def test_dry_run_is_an_independent_boolean_switch(self):
        dry_run = self.task["option"]["GiftDryRun"]
        self.assertEqual((dry_run["type"], dry_run["default_case"]), ("switch", "No"))
        for case, expected in zip(dry_run["cases"], (True, False)):
            self.assertEqual(
                case["pipeline_override"],
                {"GiftSettings": {"attach": {"dry_run": expected}}},
            )

    def test_rendering_never_mutates_catalog_inputs(self):
        originals = copy.deepcopy((self.characters, self.gifts))
        config.render_task(self.characters, self.gifts)
        config.render_config(self.characters)
        config.render_interface_locales(self.characters, self.gifts)
        self.assertEqual((self.characters, self.gifts), originals)

    def test_invalid_catalog_inputs_fail_closed(self):
        invalids = []
        for version in (True, "1", 0, 2):
            invalid = copy.deepcopy(self.characters)
            invalid["schema_version"] = version
            invalids.append(invalid)
        for value in ("yes", None, 1):
            invalid = copy.deepcopy(self.characters)
            invalid["characters"][1]["giftable"] = value
            invalids.append(invalid)
        for path in (
            "../outside.png",
            "/absolute.png",
            "C:/outside.png",
            "Gift\\wrong.png",
            "./image.png",
        ):
            invalid = copy.deepcopy(self.characters)
            invalid["characters"][1]["image"]["path"] = path
            invalids.append(invalid)
        duplicate = copy.deepcopy(self.characters)
        duplicate["characters"].append(copy.deepcopy(duplicate["characters"][1]))
        invalids.append(duplicate)
        unnamed = copy.deepcopy(self.characters)
        unnamed["characters"][1]["names"] = {"en_us": "Not an original name"}
        invalids.append(unnamed)
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    config.render_task(invalid, self.gifts)
                with self.assertRaises(ValueError):
                    config.render_config(invalid)
        duplicate_gift = copy.deepcopy(self.gifts)
        duplicate_gift["gifts"].append(copy.deepcopy(duplicate_gift["gifts"][0]))
        with self.assertRaises(ValueError):
            config.render_task(self.characters, duplicate_gift)

    @unittest.skipUnless(
        validator_for is not None, "需要已有 jsonschema；生成器本身仅用标准库"
    )
    def test_generated_task_and_pipeline_pass_bundled_schemas(self):
        with patch.object(
            socket, "create_connection", side_effect=AssertionError("no network")
        ):
            schema_validator("interface_import.schema.json").validate(self.task)
            schema_validator("pipeline.schema.json").validate(self.nodes)


class GiftTranslationTests(unittest.TestCase):
    def setUp(self):
        self.characters, self.gifts = catalogs()
        self.locales = config.render_interface_locales(self.characters, self.gifts)

    def test_five_languages_cover_every_interface_reference(self):
        self.assertEqual(set(self.locales), set(config.LANGUAGES))
        required = set(references(config.render_task(self.characters, self.gifts)))
        global_keys = {"option_switch_case_yes", "option_switch_case_no"}
        keys = set(self.locales["zh_cn"])
        for language, messages in self.locales.items():
            with self.subTest(language=language):
                self.assertEqual(set(messages), keys)
                self.assertFalse(required - (set(messages) | global_keys))
                self.assertTrue(
                    all(isinstance(value, str) and value for value in messages.values())
                )

    def test_verified_names_are_used_and_missing_names_stay_chinese(self):
        self.assertEqual(
            self.locales["en_us"]["task_gift_recipient_char_1951"],
            "Verified test alpha",
        )
        for language in config.LANGUAGES:
            self.assertEqual(
                self.locales[language]["task_gift_recipient_char_1952"], "测试乙"
            )
        for language in ("zh_cn", "zh_tw", "ja_jp", "ko_kr"):
            self.assertEqual(
                self.locales[language]["task_gift_recipient_char_1951"], "测试甲"
            )
        self.assertIn(
            "测试礼物甲 / Verified test gift",
            self.locales["en_us"]["task_gift_priority_desc"],
        )

    def test_ui_help_contains_every_gift_id_original_name_source_and_example(self):
        for language, messages in self.locales.items():
            description = messages["task_gift_priority_desc"]
            with self.subTest(language=language):
                self.assertIn("溶月茶具 > 时令寿司船", description)
                self.assertIn("resource/base/data/gifts.json", description)
                self.assertIn("`source`", description)
                self.assertEqual(
                    len(
                        [
                            line
                            for line in description.splitlines()
                            if line.startswith("- `")
                        ]
                    ),
                    2,
                )
                for gift in self.gifts["gifts"]:
                    self.assertIn("`%s`" % gift["id"], description)
                    self.assertIn(gift["names"]["zh_cn"], description)
                self.assertIn("10", messages["task_gift_desc"])
        for phrase in ("累计", "不是", "不购买", "未知喜好", "不代表不喜欢"):
            self.assertIn(phrase, self.locales["zh_cn"]["task_gift_desc"])

    def test_wiki_names_cannot_inject_markdown_or_html_into_help(self):
        self.gifts["gifts"][0]["names"][
            "zh_cn"
        ] = "[名称](https://example.invalid)<img>"
        description = config.render_interface_locales(self.characters, self.gifts)[
            "zh_cn"
        ]["task_gift_priority_desc"]
        self.assertIn(r"\[名称\]\(https://example.invalid\)&lt;img&gt;", description)
        self.assertNotIn("<img>", description)

    def test_agent_keys_and_percent_placeholders_match_in_all_languages(self):
        translations = config.render_agent_locales()
        self.assertEqual(set(translations), set(config.LANGUAGES))
        for language, messages in translations.items():
            self.assertEqual(set(messages), set(MESSAGE_FORMATS), language)
            for key, expected in MESSAGE_FORMATS.items():
                with self.subTest(language=language, key=key):
                    self.assertEqual(re.findall(r"%[sd]", messages[key]), expected)
                    args = tuple(1 if token == "%d" else "sample" for token in expected)
                    self.assertTrue(messages[key] % args)

    def test_rendered_translation_maps_do_not_share_mutable_state(self):
        first = config.render_agent_locales()
        first["zh_cn"]["gift.started"] = "changed"
        self.assertNotEqual(
            config.render_agent_locales()["zh_cn"]["gift.started"], "changed"
        )


class GiftUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maante-gift-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.characters, self.gifts = catalogs()
        for relative, data in (
            (config.CHARACTERS_PATH, self.characters),
            (config.GIFTS_PATH, self.gifts),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
            )
        self.locale_paths = []
        for family in ("interface", "agent"):
            for language in config.LANGUAGES:
                path = (
                    self.root
                    / "assets/resource/locales"
                    / family
                    / (language + ".json")
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(
                    (
                        '{\r\n    "cloud_game.keep" : "保留用户暂存改动",\r\n'
                        '    "unrelated.url": "https://example.invalid/a//b"\r\n}\r\n'
                    ).encode("utf-8")
                )
                self.locale_paths.append(path)
        self.interface = self.root / "assets/interface.json"
        self.interface.write_bytes(
            (
                '{\r\n    "controller": [{"name": "CloudGame-Front"}],\r\n'
                '    "import": [\r\n'
                '        // 未注册示例："resource/tasks/Gift.json"\r\n'
                '        "resource/tasks/CloudGame.json"\r\n    ],\r\n'
                '    "task": []\r\n}\r\n'
            ).encode("utf-8")
        )

    def snapshot(self):
        return {
            path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def test_check_is_read_only_and_offline_even_when_outputs_are_missing(self):
        before = self.snapshot()
        with patch.object(
            Path, "mkdir", side_effect=AssertionError("must not mkdir")
        ), patch.object(
            socket, "create_connection", side_effect=AssertionError("must not connect")
        ):
            diagnostics = config.check(self.root)
        self.assertEqual(len(diagnostics), 13)
        self.assertEqual(self.snapshot(), before)

    def test_update_is_idempotent_and_only_changes_authorized_paths(self):
        original = self.snapshot()
        changed = config.update(self.root)
        allowed = {
            self.root / config.TASK_PATH,
            self.root / config.CONFIG_PATH,
            self.interface,
            *self.locale_paths,
        }
        self.assertEqual(set(changed), allowed)
        self.assertEqual(config.check(self.root), [])
        generated = self.snapshot()
        self.assertEqual(
            set(generated) - set(original), {config.TASK_PATH, config.CONFIG_PATH}
        )
        self.assertEqual(config.update(self.root), [])
        self.assertEqual(self.snapshot(), generated)
        for path in (config.CHARACTERS_PATH, config.GIFTS_PATH):
            self.assertEqual(generated[path], original[path])

    def test_shared_cloud_fields_comments_urls_and_line_endings_are_preserved(self):
        previous_interface = config._read_text(self.interface)
        config.update(self.root)
        expected_interface = previous_interface.replace(
            '"resource/tasks/CloudGame.json"',
            '"resource/tasks/CloudGame.json",\r\n        "resource/tasks/Gift.json"',
        )
        self.assertEqual(config._read_text(self.interface), expected_interface)
        for path in self.locale_paths:
            text = config._read_text(path)
            self.assertIn('    "cloud_game.keep" : "保留用户暂存改动",\r\n', text)
            self.assertIn(
                '    "unrelated.url": "https://example.invalid/a//b",\r\n', text
            )
            self.assertNotIn("\n", text.replace("\r\n", ""))
            self.assertEqual(json.loads(text)["cloud_game.keep"], "保留用户暂存改动")

    def test_feature_drift_is_detected_and_repaired_without_touching_other_keys(self):
        config.update(self.root)
        path = self.root / config.TASK_PATH
        path.write_text("{}\n", encoding="utf-8")
        self.assertEqual(len(config.check(self.root)), 1)
        self.assertEqual(config.update(self.root), [path])
        locale = self.root / "assets/resource/locales/agent/en_us.json"
        text = config._read_text(locale)
        updated = config._merge_locale(
            text, {"gift.warning": "stale", "custom.user.note": "keep me"}
        )
        locale.write_bytes(updated.encode("utf-8"))
        self.assertEqual(len(config.check(self.root)), 1)
        self.assertEqual(config.update(self.root), [locale])
        self.assertEqual(
            json.loads(locale.read_text(encoding="utf-8"))["custom.user.note"],
            "keep me",
        )
        self.assertEqual(config.check(self.root), [])

    def test_invalid_input_is_rejected_before_any_generated_write(self):
        before = self.snapshot()
        self.characters["characters"][1]["giftable"] = "true"
        with self.assertRaises(ValueError):
            config.update(self.root, self.characters, self.gifts)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(config.check(self.root, self.characters, self.gifts))

    def test_missing_shared_locale_does_not_leave_partially_generated_files(self):
        self.locale_paths[-1].unlink()
        before = self.snapshot()
        with self.assertRaises(FileNotFoundError):
            config.update(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_keys_and_duplicate_task_import_are_rejected(self):
        for text in (
            '{"gift.warning": "one", "gift.warning": "two"}',
            '{"x": {"a": 1, "a": 2}}',
        ):
            with self.assertRaises(ValueError):
                config._merge_locale(text, {"gift.warning": "value"})
        with self.assertRaises(ValueError):
            config._ensure_import(
                json.dumps({"import": [config.TASK_IMPORT, config.TASK_IMPORT]})
            )

    def test_empty_containers_can_be_updated_without_invalid_json(self):
        self.assertEqual(
            json.loads(config._merge_locale("{}\n", {"gift.warning": "%s: %s"})),
            {"gift.warning": "%s: %s"},
        )
        self.assertEqual(
            config._loads(config._ensure_import('{"import": []}')),
            {"import": [config.TASK_IMPORT]},
        )

    def test_cli_check_write_and_check_again_remain_offline(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ), patch.object(
            socket, "create_connection", side_effect=AssertionError("no network")
        ):
            self.assertEqual(config.main(["--check", "--root", str(self.root)]), 1)
            self.assertEqual(config.main(["--write", "--root", str(self.root)]), 0)
            before = self.snapshot()
            self.assertEqual(config.main(["--check", "--root", str(self.root)]), 0)
            self.assertEqual(self.snapshot(), before)


class GiftRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.characters = json.loads(
            (ROOT / config.CHARACTERS_PATH).read_text(encoding="utf-8")
        )
        cls.gifts = json.loads((ROOT / config.GIFTS_PATH).read_text(encoding="utf-8"))
        cls.task = json.loads((ROOT / config.TASK_PATH).read_text(encoding="utf-8"))
        cls.nodes = json.loads((ROOT / config.CONFIG_PATH).read_text(encoding="utf-8"))

    def test_committed_resources_are_exactly_regenerable(self):
        self.assertEqual(config.check(ROOT), [])
        self.assertEqual(self.task, config.render_task(self.characters, self.gifts))
        self.assertEqual(self.nodes, config.render_config(self.characters))

    def test_all_eligible_characters_have_independent_options_and_existing_icons(self):
        self.assertEqual(len(self.characters["characters"]), 24)
        self.assertEqual(len(self.gifts["gifts"]), 214)
        eligible = {
            item["id"] for item in self.characters["characters"] if item["giftable"]
        }
        cases = self.task["option"]["GiftRecipients"]["cases"]
        self.assertEqual(len(cases), 23)
        self.assertEqual({case["name"] for case in cases}, eligible)
        for case in cases:
            self.assertTrue((ROOT / "assets" / case["icon"]).is_file(), case["name"])
            self.assertIn("GiftConfig_%s" % case["name"], self.nodes)
            for child in case["option"]:
                self.assertIn(child, self.task["option"])

    def test_real_interface_locales_cover_every_reference_and_all_214_gifts(self):
        required = set(references(self.task))
        for language in config.LANGUAGES:
            messages = json.loads(
                (
                    ROOT / "assets/resource/locales/interface" / (language + ".json")
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(required - set(messages), language)
            description = messages["task_gift_priority_desc"]
            self.assertEqual(
                len(
                    [
                        line
                        for line in description.splitlines()
                        if line.startswith("- `")
                    ]
                ),
                214,
            )
            for gift in self.gifts["gifts"]:
                self.assertIn("`%s`" % gift["id"], description)
                self.assertIn(
                    config._markdown_name(gift["names"]["zh_cn"]), description
                )

    def test_real_agent_locales_have_identical_gift_keys_and_formatting(self):
        for language in config.LANGUAGES:
            data = json.loads(
                (
                    ROOT / "assets/resource/locales/agent" / (language + ".json")
                ).read_text(encoding="utf-8")
            )
            messages = {
                key: value for key, value in data.items() if key.startswith("gift.")
            }
            self.assertEqual(set(messages), set(MESSAGE_FORMATS), language)
            for key, expected in MESSAGE_FORMATS.items():
                self.assertEqual(
                    re.findall(r"%[sd]", messages[key]), expected, (language, key)
                )

    @unittest.skipUnless(
        validator_for is not None, "需要已有 jsonschema；生成器本身仅用标准库"
    )
    def test_real_outputs_pass_actual_bundled_schemas(self):
        with patch.object(
            socket, "create_connection", side_effect=AssertionError("no network")
        ):
            schema_validator("interface_import.schema.json").validate(self.task)
            schema_validator("pipeline.schema.json").validate(self.nodes)


if __name__ == "__main__":
    unittest.main()
