"""资源一致性测试：Pipeline / 任务配置 / interface 注册 / 5 语言文案 与代码互相对得上。"""

import json
import re
import unittest

from ._loader import ACTION_DIR, ROOT, submodule

scheme = submodule("scheme")
locate = submodule("locate")
dsl = submodule("dsl")
settings = submodule("settings")

ASSETS = ROOT / "assets"
LANGS = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")
PLACEHOLDER = re.compile(r"%(?!%)[-#0 +]*\d*(?:\.\d+)?[sdif]")


def load_json(path):
    return json.loads(scheme.strip_json_comments(path.read_text(encoding="utf-8-sig")))


def locale(kind, lang):
    return load_json(ASSETS / "resource" / "locales" / kind / ("%s.json" % lang))


PIPELINE = {}
for _name in ("Combat.json", "AutoCombat.json"):
    PIPELINE.update(
        load_json(ASSETS / "resource" / "base" / "pipeline" / "Combat" / _name)
    )
TASKS = {
    name: load_json(ASSETS / "resource" / "tasks" / name)
    for name in ("AutoCombat.json", "CombatSetting.json")
}
INTERFACE = load_json(ASSETS / "interface.json")
OPTIONS = {k: v for t in TASKS.values() for k, v in t.get("option", {}).items()}


def iter_labels(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if (
                key in ("label", "description", "pattern_msg")
                and isinstance(value, str)
                and value.startswith("$")
            ):
                yield value[1:]
            else:
                yield from iter_labels(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_labels(item)


def override_nodes(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "pipeline_override" and isinstance(value, dict):
                yield from value
            yield from override_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from override_nodes(item)


def override_attaches(obj):
    """obj 中所有 pipeline_override 的 (节点, attach)。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "pipeline_override" and isinstance(value, dict):
                for node, body in value.items():
                    yield node, body.get("attach", {})
            else:
                yield from override_attaches(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from override_attaches(item)


def default_layers(checked=()):
    """模拟 MXU：未改动的全局选项按默认值覆盖载体节点；checked 为「不使用的动作」里勾选的项。"""
    nodes = {name: dict(PIPELINE[name]) for name in settings.GLOBAL_SETTING_NODES}

    def apply(override):
        for node, body in override.items():
            merged = dict(nodes[node].get("attach", {}))
            merged.update(body.get("attach", {}))  # attach 与已有值字典合并
            nodes[node] = {**nodes[node], "attach": merged}

    def handle(key):
        option = OPTIONS[key]
        if option["type"] == "checkbox":
            chosen = set(checked) if checked else set(option.get("default_case", []))
            for case in option["cases"]:
                if case["name"] in chosen:
                    apply(case["pipeline_override"])
        elif option["type"] == "input":
            text = json.dumps(option["pipeline_override"])
            for item in option["inputs"]:
                if item.get("pipeline_type") in ("int", "bool"):
                    text = text.replace('"{%s}"' % item["name"], item["default"])
                else:
                    text = text.replace("{%s}" % item["name"], item["default"])
            apply(json.loads(text))
        else:  # select / switch
            name = option.get("default_case") or option["cases"][0]["name"]
            case = next(c for c in option["cases"] if c["name"] == name)
            apply(case.get("pipeline_override", {}))
            for nested in case.get("option", []):
                handle(nested)

    for key in INTERFACE["global_option"]:
        handle(key)
    return settings.collect_layers(nodes.get)


class WiringTests(unittest.TestCase):
    def test_interface_registration(self):
        imports = INTERFACE["import"]
        self.assertIn("resource/tasks/AutoCombat.json", imports)
        self.assertIn("resource/tasks/CombatSetting.json", imports)
        sections = {s["name"]: s for s in INTERFACE["setting"]}
        self.assertTrue({"Combat", "CombatStrategy"} <= set(sections))
        shown = [
            key
            for name in ("Combat", "CombatStrategy")
            for key in sections[name]["option"]
        ]
        combat_options = set(TASKS["CombatSetting.json"]["option"])
        # MXU 只在运行时合并 global_option 里的选项：设置页显示的每个顶层战斗选项都必须登记
        self.assertEqual(
            sorted(shown),
            sorted(k for k in INTERFACE["global_option"] if k in combat_options),
        )
        nested = {
            n
            for key in shown
            for c in OPTIONS[key].get("cases", [])
            for n in c.get("option", [])
        }
        self.assertEqual(set(shown) | nested, combat_options)
        self.assertFalse(
            set(shown) & nested
        )  # 子选项随父选项的 case 合并，不能重复登记

    def test_task_entry_options_and_override_targets_exist(self):
        task = TASKS["AutoCombat.json"]["task"][0]
        self.assertIn(task["entry"], PIPELINE)
        for key in task["option"]:
            self.assertIn(key, OPTIONS)
        for option in OPTIONS.values():
            for case in option.get("cases", []):
                for nested in case.get("option", []):
                    self.assertIn(nested, OPTIONS)
        for name in override_nodes(TASKS):
            self.assertIn(name, PIPELINE, name)

    def test_scheme_cases_match_code(self):
        option = OPTIONS["CombatScheme"]
        names = [c["name"] for c in option["cases"]]
        self.assertEqual(
            names, list(scheme.BUILTIN_SCHEMES) + ["custom"]
        )  # 下拉顺序与代码一致
        self.assertEqual(set(names), set(settings.SCHEME_CHOICES))
        self.assertEqual(option["default_case"], scheme.DEFAULT_SCHEME)
        self.assertEqual(
            PIPELINE["CombatSetting_Scheme"]["attach"]["scheme"], scheme.DEFAULT_SCHEME
        )
        for case in option["cases"]:
            self.assertEqual(
                case["pipeline_override"]["CombatSetting_Scheme"]["attach"],
                {"scheme": case["name"]},
            )
        for name in settings.GLOBAL_SETTING_NODES:
            self.assertIn(name, PIPELINE, name)
            self.assertFalse(PIPELINE[name]["enabled"], name)

    def test_strategy_options_match_code(self):
        actions = OPTIONS["CombatDisabledActions"]
        self.assertEqual(actions["type"], "checkbox")
        self.assertEqual(actions["default_case"], [])
        self.assertEqual(
            [c["name"] for c in actions["cases"]],
            list(submodule("model").TOGGLE_ACTIONS),
        )
        for case in actions["cases"]:
            node = "CombatSetting_Disable%s" % case["name"].capitalize()
            self.assertEqual(
                case["pipeline_override"],
                {node: {"attach": {"use_" + case["name"]: False}}},
            )
            self.assertEqual(PIPELINE[node]["attach"], {})  # 未勾选 = 启用
        sizes = {
            c["name"]: c["pipeline_override"]["CombatSetting_TeamSize"]["attach"][
                "team_size"
            ]
            for c in OPTIONS["CombatTeamSize"]["cases"]
        }
        self.assertEqual(sizes, {"1": 1, "2": 2, "3": 3, "4": 4})
        for key in ("CombatLockTarget", "CombatSearchTurn"):
            self.assertEqual(OPTIONS[key]["type"], "switch")
            self.assertEqual({c["name"] for c in OPTIONS[key]["cases"]}, {"Yes", "No"})

    def test_attach_keys_are_valid_options(self):
        pairs = list(override_attaches(TASKS["CombatSetting.json"]))
        self.assertTrue(pairs)
        for node, attach in pairs:
            self.assertIn(node, settings.GLOBAL_SETTING_NODES, node)
            for key in attach:
                self.assertIn(key, settings.OPTION_KEYS, (node, key))

    def test_untouched_settings_reproduce_code_defaults(self):
        options, warnings = settings.merge_options(default_layers())
        self.assertEqual(warnings, [])
        self.assertEqual(options, settings.CombatOptions())

    def test_checked_actions_disable_only_those(self):
        options, warnings = settings.merge_options(
            default_layers(checked=("ultimate", "switch"))
        )
        self.assertEqual(warnings, [])
        self.assertEqual(options.disabled_actions, ("ultimate", "switch"))
        self.assertEqual((options.team_size, options.lock_target), (4, True))

    def test_custom_names_registered_and_option_nodes_exist(self):
        source = (ACTION_DIR / "Combat" / "actions.py").read_text(encoding="utf-8")
        actions = set(re.findall(r'custom_action\("([^"]+)"\)', source))
        recognitions = set(re.findall(r'custom_recognition\("([^"]+)"\)', source))
        for name, node in PIPELINE.items():
            action = node.get("action", {})
            if isinstance(action, dict) and action.get("type") == "Custom":
                self.assertIn(action["param"]["custom_action"], actions, name)
                for carrier in action["param"]["custom_action_param"].get(
                    "option_nodes", []
                ):
                    self.assertIn(carrier, PIPELINE)
                    self.assertFalse(PIPELINE[carrier]["enabled"])
            reco = node.get("recognition", {})
            if isinstance(reco, dict) and reco.get("type") == "Custom":
                self.assertIn(reco["param"]["custom_recognition"], recognitions, name)
            if isinstance(reco, dict) and reco.get("type") == "And":
                for child in reco["param"]["all_of"]:
                    self.assertIn(child, PIPELINE)
        registry = (ACTION_DIR / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("from .Combat.actions import *", registry)
        for cls in ("CombatRunAction", "CombatInHudRecognition"):
            self.assertIn('"%s"' % cls, registry)

    def test_enemy_node_matches_pinkpaw_calibration(self):
        pinkpaw = load_json(
            ASSETS / "resource" / "base" / "pipeline" / "PinkPawHeist" / "tools.json"
        )
        self.assertEqual(
            PIPELINE["CombatEnemyVisible"]["recognition"]["param"],
            pinkpaw["PinkPawHeist_CheckMonsterOnce"]["recognition"]["param"],
        )
        self.assertIn(submodule("api").ENEMY_NODE, PIPELINE)


class LocaleTests(unittest.TestCase):
    def test_interface_labels_in_all_languages(self):
        keys = set(iter_labels(TASKS)) | set(iter_labels(INTERFACE["setting"]))
        self.assertTrue(keys)
        for lang in LANGS:
            missing = keys - set(locale("interface", lang))
            self.assertFalse(missing, "%s missing %s" % (lang, sorted(missing)))

    def test_no_stale_setting_keys(self):
        used = {
            k
            for k in set(iter_labels(TASKS)) | set(iter_labels(INTERFACE["setting"]))
            if k.startswith("setting_combat")
        }
        for lang in LANGS:
            present = {
                k for k in locale("interface", lang) if k.startswith("setting_combat")
            }
            self.assertEqual(present - used, set(), lang)

    def test_agent_combat_keys_identical_across_languages(self):
        reference = {k for k in locale("agent", "zh_cn") if k.startswith("combat.")}
        self.assertGreater(len(reference), 50)
        for lang in LANGS:
            data = locale("agent", lang)
            self.assertEqual(
                {k for k in data if k.startswith("combat.")}, reference, lang
            )
            for key in reference:
                self.assertEqual(
                    len(PLACEHOLDER.findall(data[key])),
                    len(PLACEHOLDER.findall(locale("agent", "zh_cn")[key])),
                    (lang, key),
                )

    def test_builtin_scheme_names_translated(self):
        for name in scheme.BUILTIN_SCHEMES:
            s = locate.load_builtin(name)
            for text in (
                [s.name] + [t.note for t in s.team] + [t.character for t in s.team]
            ):
                if text.startswith("$"):
                    for lang in LANGS:
                        self.assertIn(text[1:], locale("agent", lang), (lang, text))

    def test_every_error_code_formats_in_every_language(self):
        errors = []
        for source in [
            "skill,\nl_click(",
            "x = 1",
            "self.click()",
            "perform",
            "jump(1)",
            "wait",
            "skill(speed=1)",
            "skill(0.1, seconds=0.2)",
            "wait(0.5*2)",
            "skill(fast)",
            "wait(11)",
            "turn(400)",
            ", ".join(["jump"] * 201),
            "",
            1,
        ]:
            try:
                dsl.compile_combo(source)
            except dsl.ComboError as exc:
                errors.append(("combat.dsl.%s" % exc.code, exc.params))
        scheme_cases = [
            [],
            {"combos": {}},
            {"version": 1},
            {"version": 1, "combos": {"9": "skill"}},
            {"version": 1, "combos": {"1": "x = 1"}},
            {"version": 1, "combos": {"1": "skill"}, "rotation": {"order": [1, 2]}},
            {"version": 1, "combos": {"default": "skill"}, "rotation": []},
            {"version": 1, "combos": {"default": "skill"}, "rotation": {"mode": "x"}},
            {"version": 1, "combos": {"default": "skill"}, "rotation": {"order": [9]}},
            {
                "version": 1,
                "combos": {"default": "skill"},
                "rotation": {"min_field_time": 99},
            },
            {"version": 1, "combos": {"default": "skill"}, "team": 1},
            {
                "version": 1,
                "combos": {"default": "skill"},
                "team": [{"slot": 1}, {"slot": 1}],
            },
        ]
        for data in scheme_cases:
            with self.assertRaises(scheme.SchemeError) as ctx:
                scheme.parse_scheme(data)
            exc = (
                ctx.exception
            )  # combo 错误的模板末尾追加 DSL 详情（见 api.describe_scheme_error）
            errors.append(
                (
                    "combat.scheme.%s" % exc.code,
                    exc.params + (("detail",) if exc.combo_error else ()),
                )
            )
        for code, params in [
            ("bad_json", (3, 7)),
            ("too_large", (64,)),
            ("read_failed", ("a.json", "denied")),
            ("not_utf8", ("a.json",)),
            ("unknown_builtin", ("x",)),
            ("builtin_missing", ("x",)),
            ("custom_empty", ()),
            ("custom_not_found", ("x", "dirs")),
            ("custom_not_json", ("x.txt",)),
        ]:
            errors.append(("combat.scheme.%s" % code, params))
        errors += [
            ("combat.dsl.at", (1, 2)),
            ("combat.waiting", ("name",)),
            ("combat.started", ("name",)),
            ("combat.ended", ("reason", 12.3)),
            ("combat.ended_no_fight", ()),
            ("combat.scheme_error", ("detail",)),
            ("combat.slot_unavailable", (3,)),
            ("combat.degraded", ()),
            ("combat.team_hint", ("name", "slots")),
            ("combat.team.slot", (1, "label")),
            ("combat.strategy_hint", ("parts",)),
            ("combat.strategy.team_size", (3,)),
            ("combat.strategy.disabled", ("a/b",)),
            ("combat.strategy.lock_off", ()),
            ("combat.strategy.search_turn", ()),
        ]
        reasons = [
            v
            for k, v in vars(submodule("model").EndReason).items()
            if not k.startswith("_")
        ]
        self.assertEqual(len(reasons), 7)
        errors += [("combat.reason.%s" % r, ()) for r in reasons]
        errors += [
            ("combat.action.%s" % a, ()) for a in submodule("model").TOGGLE_ACTIONS
        ]
        codes = {code for code, _ in errors}
        self.assertTrue(
            {
                "combat.dsl.not_text",
                "combat.dsl.too_many_commands",
                "combat.dsl.degrees_out_of_range",
                "combat.scheme.missing_combo",
            }
            <= codes
        )
        for lang in LANGS:
            data = locale("agent", lang)
            for code, params in errors:
                self.assertIn(code, data, (lang, code))
                try:
                    (
                        data[code] % params
                        if params or PLACEHOLDER.search(data[code])
                        else data[code]
                    )
                except (TypeError, ValueError) as exc:
                    self.fail("%s %s %r: %s" % (lang, code, params, exc))


if __name__ == "__main__":
    unittest.main()
