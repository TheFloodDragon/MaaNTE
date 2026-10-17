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
    """Model sequential MaaFramework overrides: attach fields merge only shallowly."""
    for name, change in override.items():
        node = base.setdefault(name, {})
        for field, value in change.items():
            if field == "attach":
                node.setdefault("attach", {}).update(copy.deepcopy(value))
            else:
                node[field] = copy.deepcopy(value)


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


def mxu_render(task, nodes, selections=None, input_values=None):
    """Apply only visible controls; MXU expands child options for select/switch."""
    selections = selections or {}
    input_values = input_values or {}
    visible = []
    merged = copy.deepcopy(nodes)

    def visit(name, ancestors):
        if name in ancestors:
            raise AssertionError("option cycle: %s" % name)
        option = task["option"][name]
        visible.append(name)
        if option["type"] in ("select", "switch"):
            selected = selections.get(name, option["default_case"])
            case = next(case for case in option["cases"] if case["name"] == selected)
            merge(merged, case.get("pipeline_override", {}))
            for child in case.get("option", []):
                visit(child, ancestors | {name})
        elif option["type"] == "input":
            override = copy.deepcopy(option["pipeline_override"])
            values = input_values.get(name, {})
            for field in option["inputs"]:
                value = values.get(field["name"], field["default"])
                for node in override.values():
                    for key, placeholder in node["attach"].items():
                        if placeholder == "{%s}" % field["name"]:
                            node["attach"][key] = value
            merge(merged, override)
        # The checkbox branch in the actual OptionEditor never renders case.option.

    for name in task["task"][0]["option"]:
        visit(name, set())
    return visible, merged


class GiftRenderingTests(unittest.TestCase):
    def setUp(self):
        self.characters, self.gifts = catalogs()
        self.task = config.render_task(self.characters, self.gifts)
        self.nodes = config.render_config(self.characters)

    def test_daily_task_starts_with_one_empty_plan_and_no_checkbox(self):
        task = self.task["task"][0]
        self.assertEqual(
            (task["name"], task["entry"], task["group"]),
            ("Gift", "GiftEntry", ["Daily"]),
        )
        self.assertEqual(task["option"], ["GiftPlanCount", "GiftDryRun"])
        self.assertNotIn("default_check", task)
        self.assertNotIn("default_selected", task)
        self.assertNotIn("GiftRecipients", self.task["option"])
        self.assertTrue(
            all(option["type"] != "checkbox" for option in self.task["option"].values())
        )
        visible, merged = mxu_render(self.task, self.nodes)
        self.assertEqual(visible, ["GiftPlanCount", "GiftRecipient_1", "GiftDryRun"])
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_mode"], "rows")
        self.assertEqual(merged["GiftSettings"]["attach"]["plan_count"], 1)
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_1"], "")
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_2"], "")
        self.assertNotIn("plans", merged["GiftSettings"]["attach"])
        self.assertFalse(merged["GiftConfig_char_1951"]["attach"]["selected"])
        self.assertNotIn("GiftConfig_char_14", self.nodes)

    def test_mxu_checkbox_branch_does_not_expand_child_options(self):
        legacy = copy.deepcopy(self.task)
        legacy["option"]["GiftPlanCount"]["type"] = "checkbox"
        visible, merged = mxu_render(legacy, self.nodes)
        self.assertEqual(visible, ["GiftPlanCount", "GiftDryRun"])
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_mode"], "legacy")

    def test_plan_count_cases_expand_exactly_the_requested_rows(self):
        count = self.task["option"]["GiftPlanCount"]
        self.assertEqual((count["type"], count["default_case"]), ("select", "1"))
        self.assertEqual([case["name"] for case in count["cases"]], ["1", "2"])
        for number, case in enumerate(count["cases"], 1):
            self.assertEqual(
                case["option"],
                ["GiftRecipient_%d" % row for row in range(1, number + 1)],
            )
            self.assertEqual(
                case["pipeline_override"],
                {
                    "GiftSettings": {
                        "attach": {"recipient_mode": "rows", "plan_count": number}
                    }
                },
            )
            visible, merged = mxu_render(
                self.task, self.nodes, {"GiftPlanCount": str(number)}
            )
            self.assertEqual(visible, ["GiftPlanCount", *case["option"], "GiftDryRun"])
            self.assertEqual(merged["GiftSettings"]["attach"]["plan_count"], number)

    def test_recipient_rows_override_only_the_row_and_own_character(self):
        for row in (1, 2):
            picker = self.task["option"]["GiftRecipient_%d" % row]
            self.assertEqual(
                (picker["type"], picker["default_case"]), ("select", "None")
            )
            self.assertEqual(
                [case["name"] for case in picker["cases"]],
                ["None", "char_1951", "char_1952"],
            )
            self.assertEqual(
                picker["cases"][0]["pipeline_override"],
                {"GiftSettings": {"attach": {"recipient_%d" % row: ""}}},
            )
            self.assertNotIn("option", picker["cases"][0])
            for case in picker["cases"][1:]:
                identifier = case["name"]
                self.assertEqual(
                    case["pipeline_override"],
                    {
                        "GiftConfig_%s" % identifier: {"attach": {"selected": True}},
                        "GiftSettings": {"attach": {"recipient_%d" % row: identifier}},
                    },
                )
                self.assertEqual(
                    case["option"],
                    ["GiftPriorityMode_%s" % identifier, "GiftLimit_%s" % identifier],
                )
                self.assertEqual(
                    case["icon"],
                    "resource/base/image/Gift/Characters/%s.png" % identifier,
                )

    def test_selecting_character_reaches_list_fields_and_limit(self):
        visible, merged = mxu_render(
            self.task, self.nodes, {"GiftRecipient_1": "char_1951"}
        )
        self.assertEqual(
            visible,
            [
                "GiftPlanCount",
                "GiftRecipient_1",
                "GiftPriorityMode_char_1951",
                "GiftPrimary_char_1951",
                "GiftBackup_char_1951",
                "GiftExtra_char_1951",
                "GiftLimit_char_1951",
                "GiftDryRun",
            ],
        )
        self.assertTrue(merged["GiftConfig_char_1951"]["attach"]["selected"])
        self.assertEqual(
            merged["GiftConfig_char_1951"]["attach"]["gift_mode"], "choices"
        )
        self.assertEqual(merged["GiftConfig_char_1951"]["attach"]["primary_gift"], "")
        self.assertEqual(merged["GiftConfig_char_1951"]["attach"]["backup_gift"], "")

    def test_attach_merge_replaces_nested_maps_but_preserves_flat_recipient_rows(self):
        nested = {"GiftSettings": {"attach": {"plans": {"1": "char_1952"}}}}
        merge(nested, {"GiftSettings": {"attach": {"plans": {"2": "char_1951"}}}})
        self.assertEqual(nested["GiftSettings"]["attach"]["plans"], {"2": "char_1951"})
        merged = copy.deepcopy(self.nodes)
        for row, character in ((1, "char_1952"), (2, "char_1951")):
            case = next(
                case
                for case in self.task["option"]["GiftRecipient_%d" % row]["cases"]
                if case["name"] == character
            )
            attach = case["pipeline_override"]["GiftSettings"]["attach"]
            self.assertEqual(attach, {"recipient_%d" % row: character})
            self.assertTrue(
                all(not isinstance(value, dict) for value in attach.values())
            )
            merge(merged, case["pipeline_override"])
        settings = merged["GiftSettings"]["attach"]
        self.assertEqual(
            (settings["recipient_1"], settings["recipient_2"]),
            ("char_1952", "char_1951"),
        )
        self.assertNotIn("plans", settings)

    def test_two_rows_keep_character_gifts_and_limits_independent(self):
        visible, merged = mxu_render(
            self.task,
            self.nodes,
            {
                "GiftPlanCount": "2",
                "GiftRecipient_1": "char_1952",
                "GiftRecipient_2": "char_1951",
                "GiftPrimary_char_1952": "Furniture_draw_004",
                "GiftBackup_char_1952": "Food_003",
                "GiftPrimary_char_1951": "Food_003",
                "GiftLimit_char_1952": "1",
                "GiftLimit_char_1951": "2",
            },
            {"GiftExtra_char_1952": {"extra_gifts": "Food_003 > Furniture_draw_004"}},
        )
        self.assertIn("GiftRecipient_2", visible)
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_1"], "char_1952")
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_2"], "char_1951")
        self.assertNotIn("plans", merged["GiftSettings"]["attach"])
        first = merged["GiftConfig_char_1952"]["attach"]
        second = merged["GiftConfig_char_1951"]["attach"]
        self.assertEqual(
            (
                first["primary_gift"],
                first["backup_gift"],
                first["extra_gifts"],
                first["limit"],
            ),
            ("Furniture_draw_004", "Food_003", "Food_003 > Furniture_draw_004", 1),
        )
        self.assertEqual(
            (
                second["primary_gift"],
                second["backup_gift"],
                second["extra_gifts"],
                second["limit"],
            ),
            ("Food_003", "", "", 2),
        )

    def test_none_rows_select_no_characters_and_clear_previous_row_assignment(self):
        _, merged = mxu_render(self.task, self.nodes, {"GiftPlanCount": "2"})
        self.assertTrue(
            all(
                not node["attach"]["selected"]
                for name, node in merged.items()
                if name.startswith("GiftConfig_")
            )
        )
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_1"], "")
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_2"], "")
        self.assertNotIn("plans", merged["GiftSettings"]["attach"])
        picker = self.task["option"]["GiftRecipient_1"]
        merge(merged, picker["cases"][1]["pipeline_override"])
        merge(merged, picker["cases"][0]["pipeline_override"])
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_1"], "")

    def test_duplicate_rows_reuse_one_character_configuration(self):
        visible, merged = mxu_render(
            self.task,
            self.nodes,
            {
                "GiftPlanCount": "2",
                "GiftRecipient_1": "char_1951",
                "GiftRecipient_2": "char_1951",
                "GiftPrimary_char_1951": "Food_003",
                "GiftLimit_char_1951": "2",
            },
        )
        self.assertEqual(visible.count("GiftPriorityMode_char_1951"), 2)
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_1"], "char_1951")
        self.assertEqual(merged["GiftSettings"]["attach"]["recipient_2"], "char_1951")
        self.assertNotIn("plans", merged["GiftSettings"]["attach"])
        self.assertEqual(merged["GiftConfig_char_1951"]["attach"]["limit"], 2)
        self.assertEqual(
            merged["GiftConfig_char_1951"]["attach"]["primary_gift"], "Food_003"
        )
        self.assertFalse(merged["GiftConfig_char_1952"]["attach"]["selected"])

    def test_list_and_custom_modes_apply_only_active_fields_and_keep_saved_text(self):
        selections = {
            "GiftRecipient_1": "char_1951",
            "GiftPrimary_char_1951": "Food_003",
            "GiftBackup_char_1951": "Furniture_draw_004",
        }
        text = "Furniture_draw_004 > Food_003"
        saved = {
            "GiftPriority_char_1951": {"gifts": text},
            "GiftExtra_char_1951": {"extra_gifts": "Food_003 > Furniture_draw_004"},
        }
        original = copy.deepcopy(saved)
        visible, merged = mxu_render(self.task, self.nodes, selections, saved)
        self.assertNotIn("GiftPriority_char_1951", visible)
        attachment = merged["GiftConfig_char_1951"]["attach"]
        self.assertEqual(
            (attachment["gift_mode"], attachment["gifts"]), ("choices", "")
        )
        self.assertEqual(attachment["extra_gifts"], "Food_003 > Furniture_draw_004")
        selections["GiftPriorityMode_char_1951"] = "Custom"
        visible, merged = mxu_render(self.task, self.nodes, selections, saved)
        self.assertIn("GiftPriority_char_1951", visible)
        for field in ("GiftPrimary", "GiftBackup", "GiftExtra"):
            self.assertNotIn(field + "_char_1951", visible)
        attachment = merged["GiftConfig_char_1951"]["attach"]
        self.assertEqual(
            (attachment["gift_mode"], attachment["gifts"]), ("manual", text)
        )
        for field in ("primary_gift", "backup_gift", "extra_gifts"):
            self.assertEqual(attachment[field], "")
        self.assertEqual(saved, original)

    def test_extra_and_legacy_priority_inputs_are_unbounded_and_keep_original_keys(
        self,
    ):
        for identifier in ("char_1951", "char_1952"):
            for option, field in (
                ("GiftPriority", "gifts"),
                ("GiftExtra", "extra_gifts"),
            ):
                control = self.task["option"]["%s_%s" % (option, identifier)]
                self.assertEqual(control["type"], "input")
                self.assertEqual(len(control["inputs"]), 1)
                input_field = control["inputs"][0]
                self.assertEqual(
                    (
                        input_field["name"],
                        input_field["default"],
                        input_field["pipeline_type"],
                    ),
                    (field, "", "string"),
                )
                self.assertNotIn("verify", input_field)
                self.assertNotIn("max_length", input_field)
                self.assertEqual(
                    control["pipeline_override"],
                    {"GiftConfig_%s" % identifier: {"attach": {field: "{%s}" % field}}},
                )
        long_priority = " > ".join(["Food_003", "Furniture_draw_004"] * 160)
        _, merged = mxu_render(
            self.task,
            self.nodes,
            {"GiftRecipient_1": "char_1951"},
            {"GiftExtra_char_1951": {"extra_gifts": long_priority}},
        )
        self.assertEqual(
            merged["GiftConfig_char_1951"]["attach"]["extra_gifts"], long_priority
        )

    def test_limits_are_visible_integer_selects_with_only_one_two_three(self):
        for identifier in ("char_1951", "char_1952"):
            limit = self.task["option"]["GiftLimit_%s" % identifier]
            self.assertEqual((limit["type"], limit["default_case"]), ("select", "3"))
            self.assertEqual([case["name"] for case in limit["cases"]], ["1", "2", "3"])
            for count, case in enumerate(limit["cases"], 1):
                override = case["pipeline_override"]
                self.assertEqual(
                    override,
                    {"GiftConfig_%s" % identifier: {"attach": {"limit": count}}},
                )
                self.assertIs(
                    type(override["GiftConfig_%s" % identifier]["attach"]["limit"]), int
                )

    def test_plan_count_adapts_to_actual_giftable_characters(self):
        third = copy.deepcopy(self.characters["characters"][2])
        third["id"] = "char_1953"
        self.characters["characters"].append(third)
        for count in (1, 2, 3):
            characters = copy.deepcopy(self.characters)
            for index, character in enumerate(characters["characters"]):
                character["giftable"] = 1 <= index <= count
            task = config.render_task(characters, self.gifts)
            cases = task["option"]["GiftPlanCount"]["cases"]
            self.assertEqual(
                [case["name"] for case in cases], [str(i) for i in range(1, count + 1)]
            )
            self.assertEqual(
                len(
                    [
                        name
                        for name in task["option"]
                        if name.startswith("GiftRecipient_")
                    ]
                ),
                count,
            )
            settings = config.render_config(characters)["GiftSettings"]["attach"]
            self.assertEqual(
                {
                    key: value
                    for key, value in settings.items()
                    if re.fullmatch(r"recipient_[0-9]+", key)
                },
                {"recipient_%d" % i: "" for i in range(1, count + 1)},
            )
            self.assertNotIn("plans", settings)

    def test_data_nodes_are_inert_and_start_with_safe_defaults(self):
        self.assertEqual(
            self.nodes["GiftSettings"]["attach"],
            {
                "dry_run": False,
                "recipient_mode": "legacy",
                "plan_count": 1,
                "recipient_1": "",
                "recipient_2": "",
            },
        )
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
                        "gift_mode": "manual",
                        "primary_gift": "",
                        "backup_gift": "",
                        "extra_gifts": "",
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

    def test_compact_task_serialization_preserves_all_ui_and_override_values(self):
        original = copy.deepcopy(self.task)
        encoded = config._task_json(self.task)
        self.assertEqual(json.loads(encoded), self.task)
        self.assertEqual(self.task, original)
        self.assertEqual(encoded, config._task_json(json.loads(encoded)))
        self.assertTrue(encoded.endswith("\n"))
        pretty = json.dumps(self.task, ensure_ascii=False, indent=4) + "\n"
        self.assertLess(
            len(encoded.encode("utf-8")), len(pretty.encode("utf-8")) * 0.55
        )

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
        self.assertEqual(
            self.locales["en_us"]["task_gift_item_Food_003"], "Verified test gift"
        )
        for language in config.LANGUAGES:
            self.assertEqual(
                self.locales[language]["task_gift_item_Furniture_draw_004"],
                "测试礼物乙",
            )
        for language in ("zh_cn", "zh_tw", "ja_jp", "ko_kr"):
            self.assertEqual(
                self.locales[language]["task_gift_item_Food_003"], "测试礼物甲"
            )

    def test_new_help_is_short_local_and_full_catalog_only_in_manual_priority(self):
        task = config.render_task(self.characters, self.gifts)
        for language, messages in self.locales.items():
            with self.subTest(language=language):
                self.assertLess(len(messages["task_gift_desc"]), 320)
                self.assertNotIn("resource/base/data", messages["task_gift_desc"])
                self.assertIn("1", messages["task_gift_plan_recipient_1"])
                self.assertIn("2", messages["task_gift_plan_recipient_2"])
                for option in task["option"].values():
                    if option["type"] != "select":
                        continue
                    description = messages[option["description"][1:]]
                    self.assertLess(len(description), 300)
                    self.assertNotIn("- `", description)
                    self.assertNotIn("task_gift_priority_desc", option["description"])
        chinese = self.locales["zh_cn"]
        self.assertEqual(chinese["task_gift_plan_count"], "赠礼方案数量")
        self.assertEqual(chinese["task_gift_plan_recipient_1"], "方案 1 · 角色")
        self.assertEqual(chinese["task_gift_recipient_none"], "请选择角色")
        self.assertEqual(chinese["task_gift_primary_none"], "请选择首选礼物")
        self.assertEqual(chinese["task_gift_backup_none"], "不使用备用礼物")
        for phrase in ("不同角色", "共用", "只执行一次"):
            self.assertIn(phrase, chinese["task_gift_recipient_row_desc"])
        for phrase in ("当日累计", "不是本次追加", "重复运行", "已有次数"):
            self.assertIn(phrase, chinese["task_gift_limit_desc"])

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
                self.assertIn("10", messages["task_gift_limit_desc"])
        for phrase in ("不购买", "未知喜好", "不代表不喜欢"):
            self.assertIn(phrase, self.locales["zh_cn"]["task_gift_priority_desc"])

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

    def test_legacy_locale_keys_survive_catalog_reduction(self):
        config.update(self.root)
        reduced = copy.deepcopy(self.characters)
        reduced["characters"][2]["giftable"] = False
        fewer_gifts = copy.deepcopy(self.gifts)
        fewer_gifts["gifts"].pop()
        config.update(self.root, reduced, fewer_gifts)
        for language in config.LANGUAGES:
            path = (
                self.root / "assets/resource/locales/interface" / (language + ".json")
            )
            messages = json.loads(path.read_text(encoding="utf-8"))
            for key in (
                "task_gift_recipients",
                "task_gift_recipients_desc",
                "task_gift_limit_input",
                "task_gift_limit_invalid",
                "task_gift_plan_recipient_2",
                "task_gift_recipient_char_1952",
                "task_gift_item_Furniture_draw_004",
            ):
                self.assertIn(key, messages)
            self.assertEqual(messages["cloud_game.keep"], "保留用户暂存改动")

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

    def test_generated_task_remains_compact_without_collapsing_cases_into_one_line(
        self,
    ):
        text = (ROOT / config.TASK_PATH).read_text(encoding="utf-8")
        pretty = json.dumps(self.task, ensure_ascii=False, indent=4) + "\n"
        self.assertLess(len(text.encode("utf-8")), len(pretty.encode("utf-8")) * 0.5)
        self.assertLess(max(map(len, text.splitlines())), 4096)
        self.assertEqual(json.loads(text), self.task)
        ignored = (ROOT / ".prettierignore").read_text(encoding="utf-8").splitlines()
        for path in (
            "assets/resource/tasks/Gift.json",
            "assets/resource/base/data/gifts.json",
            "assets/resource/base/data/characters.json",
        ):
            self.assertIn(path, ignored)

    def test_all_eligible_characters_have_independent_options_and_existing_icons(self):
        self.assertEqual(len(self.characters["characters"]), 24)
        self.assertEqual(len(self.gifts["gifts"]), 214)
        eligible = {
            item["id"] for item in self.characters["characters"] if item["giftable"]
        }
        self.assertEqual(len(eligible), 23)
        counts = self.task["option"]["GiftPlanCount"]["cases"]
        self.assertEqual(
            [case["name"] for case in counts],
            [str(i) for i in range(1, len(eligible) + 1)],
        )
        for row in range(1, len(eligible) + 1):
            picker = self.task["option"]["GiftRecipient_%d" % row]
            self.assertEqual(picker["type"], "select")
            self.assertEqual(picker["cases"][0]["name"], "None")
            self.assertEqual({case["name"] for case in picker["cases"][1:]}, eligible)
            for case in picker["cases"][1:]:
                self.assertTrue(
                    (ROOT / "assets" / case["icon"]).is_file(), case["name"]
                )
                self.assertIn("GiftConfig_%s" % case["name"], self.nodes)
                for child in case["option"]:
                    self.assertIn(child, self.task["option"])
        for name, option in self.task["option"].items():
            if option["type"] in ("select", "switch"):
                case_names = [case["name"] for case in option["cases"]]
                self.assertEqual(len(case_names), len(set(case_names)), name)
                self.assertIn(option["default_case"], case_names, name)
                for case in option["cases"]:
                    self.assertFalse(
                        set(case.get("option", [])) - set(self.task["option"])
                    )
                    self.assertFalse(
                        set(case.get("pipeline_override", {})) - set(self.nodes)
                    )

    def test_every_real_gift_is_selectable_in_both_dropdowns_and_has_existing_icon(
        self,
    ):
        eligible = [
            item["id"] for item in self.characters["characters"] if item["giftable"]
        ]
        gift_ids = [gift["id"] for gift in self.gifts["gifts"]]
        icons = {
            gift["id"]: "resource/base/image/" + gift["image"]["path"]
            for gift in self.gifts["gifts"]
        }
        for gift_id, icon in icons.items():
            self.assertTrue((ROOT / "assets" / icon).is_file(), gift_id)
            self.assertIn("Gift/Gifts/", icon)
        for character in eligible:
            for option, field in (
                ("GiftPrimary", "primary_gift"),
                ("GiftBackup", "backup_gift"),
            ):
                picker = self.task["option"]["%s_%s" % (option, character)]
                self.assertEqual(
                    (picker["type"], picker["default_case"]), ("select", "None")
                )
                self.assertEqual(
                    [case["name"] for case in picker["cases"]], ["None", *gift_ids]
                )
                self.assertEqual(
                    picker["cases"][0]["pipeline_override"],
                    {"GiftConfig_%s" % character: {"attach": {field: ""}}},
                )
                for case in picker["cases"][1:]:
                    self.assertEqual(case["label"], "$task_gift_item_%s" % case["name"])
                    self.assertEqual(case["icon"], icons[case["name"]])
                    self.assertEqual(
                        case["pipeline_override"],
                        {
                            "GiftConfig_%s"
                            % character: {"attach": {field: case["name"]}}
                        },
                    )
        character = eligible[0]
        for gift_id in gift_ids:
            _, merged = mxu_render(
                self.task,
                self.nodes,
                {
                    "GiftRecipient_1": character,
                    "GiftPrimary_%s" % character: gift_id,
                    "GiftBackup_%s" % character: gift_id,
                },
            )
            attachment = merged["GiftConfig_%s" % character]["attach"]
            self.assertEqual(
                (attachment["primary_gift"], attachment["backup_gift"]),
                (gift_id, gift_id),
            )

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
            item_keys = {key for key in messages if key.startswith("task_gift_item_")}
            self.assertEqual(len(item_keys), 214)
            for gift in self.gifts["gifts"]:
                self.assertIn("`%s`" % gift["id"], description)
                self.assertIn(
                    config._markdown_name(gift["names"]["zh_cn"]), description
                )
                self.assertEqual(
                    messages["task_gift_item_%s" % gift["id"]],
                    gift["names"].get(language, gift["names"]["zh_cn"]),
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
