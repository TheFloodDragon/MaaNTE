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


def default_layers(checked=None, cases=None, inputs=None):
    """按 MXU 的合并方式模拟全局选项，返回 collect_layers 的结果。

    checked：{checkbox 选项: [勾选项]}；cases：{select/switch 选项: case 名}；
    inputs：{input 字段名: 用户输入}。未给出的一律使用默认值。
    与 MXU 一致：只递归选中 case 的子选项；字符串输入经 JSON 转义后代入。
    """
    checked, cases, inputs = checked or {}, cases or {}, inputs or {}
    nodes = {name: dict(PIPELINE[name]) for name in settings.GLOBAL_SETTING_NODES}

    def apply(override):
        for node, body in override.items():
            merged = dict(nodes[node].get("attach", {}))
            merged.update(body.get("attach", {}))  # attach 与已有值字典合并
            nodes[node] = {**nodes[node], "attach": merged}

    def handle(key):
        option = OPTIONS[key]
        if option["type"] == "checkbox":
            chosen = set(checked.get(key, option.get("default_case", [])))
            for case in option["cases"]:
                if case["name"] in chosen:
                    apply(case["pipeline_override"])
        elif option["type"] == "input":
            text = json.dumps(option["pipeline_override"])
            for item in option["inputs"]:
                value = inputs.get(item["name"], item["default"])
                if item.get("pipeline_type") in ("int", "bool"):
                    text = text.replace('"{%s}"' % item["name"], value or "0")
                else:
                    escaped = json.dumps(value)[1:-1]
                    text = text.replace("{%s}" % item["name"], escaped)
            apply(json.loads(text))
        else:  # select / switch
            name = (
                cases.get(key)
                or option.get("default_case")
                or option["cases"][0]["name"]
            )
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
        combat_options = set(TASKS["CombatSetting.json"]["option"])
        sections = [
            s for s in INTERFACE["setting"] if set(s["option"]) & combat_options
        ]
        self.assertEqual(
            [s["name"] for s in sections],
            ["Combat", "CombatTeam", "CombatInput", "CombatStrategy"],
        )
        shown = [key for s in sections for key in s["option"]]
        # MXU 只在运行时合并 global_option 里的选项：设置页显示的每个顶层战斗选项都必须登记
        self.assertEqual(
            sorted(shown),
            sorted(k for k in INTERFACE["global_option"] if k in combat_options),
        )
        nested, frontier = set(), list(shown)
        while frontier:  # 子选项可以逐层嵌套，随父选项选中的 case 合并
            key = frontier.pop()
            for case in OPTIONS[key].get("cases", []):
                for child in case.get("option", []):
                    if child not in nested:
                        nested.add(child)
                        frontier.append(child)
        self.assertEqual(set(shown) | nested, combat_options)
        self.assertFalse(set(shown) & nested)  # 子选项不能重复登记为顶层

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
            default_layers(checked={"CombatDisabledActions": ["ultimate", "switch"]})
        )
        self.assertEqual(warnings, [])
        self.assertEqual(options.disabled_actions, ("ultimate", "switch"))
        self.assertEqual((options.team_size, options.lock_target), (4, True))

    def test_advanced_off_ignores_hidden_slot_inputs(self):
        """高级调整关闭时，即使之前填过槽位内容也不生效（MXU 只合并选中 case 的子选项）。"""
        layers = default_layers(
            checked={"CombatDisabledSlots": ["2"]},
            inputs={"slot1_combo": "jump", "rotation_order": "3,1"},
        )
        options, warnings, errors = settings.merge_options_strict(layers)
        self.assertEqual((warnings, errors), ([], []))
        self.assertEqual(options, settings.CombatOptions())

    def test_advanced_slot_overrides_merge_per_field(self):
        layers = default_layers(
            cases={"CombatAdvanced": "Yes", "CombatRotationMode": "cycle"},
            checked={"CombatDisabledSlots": ["3"]},
            inputs={
                "rotation_order": "2，1, 4",
                "min_field_time": "1.5",
                "slot1_character": '主C "零"',
                "slot1_combo": "skill, ultimate, l_click(1.5)",
                "slot2_field_time": "0",
            },
        )
        options, warnings, errors = settings.merge_options_strict(layers)
        self.assertEqual((warnings, errors), ([], []))
        self.assertEqual(options.rotation_mode, "cycle")
        self.assertEqual(options.rotation_order, (2, 1, 4))
        self.assertEqual(options.min_field_time, 1.5)
        self.assertEqual(options.disabled_slots, (3,))
        self.assertEqual(
            options.slot(1).character, '主C "零"'
        )  # 引号经 JSON 转义后原样保留
        self.assertEqual(options.slot(1).combo, "skill, ultimate, l_click(1.5)")
        self.assertEqual(options.slot(2).min_field_time, 0.0)  # 明确的 0 不是“未设置”
        self.assertIsNone(options.slot(3).min_field_time)
        self.assertTrue(options.slot(4).empty)

    def test_verify_patterns_match_parsers(self):
        patterns = {
            item["name"]: re.compile(item["verify"])
            for option in OPTIONS.values()
            for item in option.get("inputs", [])
            if "verify" in item
        }
        ok = {
            "combat_speed": ["0.5", "0.75", "1", "1.0", "1.25", "2", "2.0", "2.00"],
            "rotation_order": [
                "",
                "1",
                "1,2,1,3",
                "2，1、4",
                " 1, 2 ",
                ",".join(["1"] * 16),
            ],
            "min_field_time": ["", "0", "1.5", "9.99", "10", "10.0"],
        }
        bad = {
            "combat_speed": ["", "0", "0.4", "2.1", "3", "1.255", "abc", "-1"],
            "rotation_order": ["0", "5", "1,", ",1", "1,,2", "a", ",".join(["1"] * 17)],
            "min_field_time": ["-1", "10.5", "11", "1.234", "x"],
        }
        for name, values in ok.items():
            for value in values:
                self.assertTrue(patterns[name].search(value), (name, value))
                if name == "rotation_order" and value:
                    settings.parse_order(value)  # UI 放行的值，代码也必须接受
        for name, values in bad.items():
            for value in values:
                self.assertFalse(patterns[name].search(value), (name, value))

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
            if isinstance(reco, dict) and reco.get("type") in ("And", "Or"):
                key = "all_of" if reco["type"] == "And" else "any_of"
                for child in reco["param"][key]:
                    self.assertIn(child, PIPELINE, (name, child))
        registry = (ACTION_DIR / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("from .Combat.actions import *", registry)
        for cls in (
            "CombatRunAction",
            "CombatInHudRecognition",
            "CombatEnemyBarRecognition",
        ):
            self.assertIn('"%s"' % cls, registry)

    def test_enemy_nodes_and_signal_wiring(self):
        """精确颜色血条保留粉爪已实机校准的基准参数；其余证据各自独立、均已启用。"""
        api = submodule("api")
        pinkpaw = load_json(
            ASSETS / "resource" / "base" / "pipeline" / "PinkPawHeist" / "tools.json"
        )
        self.assertEqual(
            PIPELINE[api.ENEMY_NODE]["recognition"]["param"],
            pinkpaw["PinkPawHeist_CheckMonsterOnce"]["recognition"]["param"],
        )
        evidence = [api.ENEMY_NODE, api.BAR_SHAPE_NODE, api.LEVEL_NODE, api.BOSS_NODE]
        # Pipeline 门控与 Python 内核使用同一组敌人证据
        self.assertEqual(
            PIPELINE["CombatEnemyVisible"]["recognition"]["param"]["any_of"], evidence
        )
        for name in evidence + [api.RETREAT_PROMPT_NODE]:
            node = PIPELINE[name]
            self.assertNotEqual(node.get("enabled", True), False, name)
            self.assertNotIn("action", node, name)  # 只识别，不动作
            self.assertEqual(
                (node["rate_limit"], node["pre_delay"], node["post_delay"]), (0, 0, 0)
            )
        retreat = PIPELINE[api.RETREAT_PROMPT_NODE]["recognition"]["param"]
        self.assertEqual(
            retreat["roi"],
            pinkpaw["PinkPawHeist_CheckEvacuateOnce"]["recognition"]["param"]["roi"],
        )
        self.assertEqual(
            submodule("perception").SignalConfig().enemy, (api.ENEMY_NODE,)
        )

    def test_pipeline_accepts_lifecycle_reasons_explicitly(self):
        """改了 success 语义后，连续运行的入口必须显式列出可交还的结束原因。"""
        expected = ["cleared", "disengaged", "target_lost"]
        for name in ("AutoCombatMain", "CombatFightWhenEnemy"):
            param = PIPELINE[name]["action"]["param"]["custom_action_param"]
            self.assertEqual(param["accepted_reasons"], expected, name)
            reasons, warnings = settings.parse_accepted_reasons(
                param["accepted_reasons"]
            )
            self.assertEqual(warnings, [])
            self.assertNotIn("retreated", reasons)
            self.assertNotIn("timeout", reasons)
        run = PIPELINE["CombatRun"]["action"]["param"]["custom_action_param"]
        self.assertNotIn("accepted_reasons", run)  # 公开接口默认只接受 cleared


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

    @staticmethod
    def _runtime_errors():
        """从真实代码路径取得新增的错误码与参数，避免文案占位符和代码不一致。"""
        plan = submodule("plan")
        api = submodule("api")
        base = scheme.parse_scheme({"version": 1, "combos": {"default": "skill"}})
        found = []

        def record(exc):
            params = exc.params + (("detail",) if exc.combo_error else ())
            found.append(("combat.scheme.%s" % exc.code, params))

        for layers in (
            [{"slots": {"1": {"combo": "x = 1"}}}],
            [{"slots": {str(s): {"enabled": False} for s in (1, 2, 3, 4)}}],
        ):
            options, _, _ = settings.merge_options_strict(layers)
            try:
                plan.apply_overrides(base, options)
            except scheme.SchemeError as exc:
                record(exc)
        _, _, bad = settings.merge_options_strict([{"combat_speed": 9}])
        found.append(("combat.scheme.bad_option", bad[0]))
        try:
            api.validate_signal_nodes(lambda name: None, {"cleared": ["Missing"]})
        except scheme.SchemeError as exc:
            record(exc)
        try:
            dsl.compile_combo("speed(3)")
        except dsl.ComboError as exc:
            found.append(("combat.dsl.%s" % exc.code, exc.params))
        codes = [code for code, _ in found]
        assert codes == [
            "combat.scheme.slot_combo",
            "combat.scheme.no_enabled_slot",
            "combat.scheme.bad_option",
            "combat.scheme.bad_signal",
            "combat.dsl.speed_out_of_range",
        ], codes
        return found

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
        errors += self._runtime_errors()
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
            ("combat.strategy.assist_off", ()),
            ("combat.plan.header", ("name", "source", "1", "rotation")),
            ("combat.plan.cycle", ("1→2", 1)),
            ("combat.plan.stay", ()),
            ("combat.plan.slot", (1, "label", "source", "skill", "1")),
            ("combat.plan.slot_off", (4,)),
            ("combat.plan.from_builtin", ()),
            ("combat.plan.from_file", ("a.json",)),
            ("combat.plan.from_script", ()),
            ("combat.state.verifying", ()),
            ("combat.state.resumed", ()),
            ("combat.state.retreat_prompt", ()),
        ]
        model = submodule("model")
        reasons = sorted(model.END_REASONS)
        self.assertEqual(len(reasons), 13)
        errors += [("combat.reason.%s" % r, ()) for r in reasons]
        errors += [("combat.action.%s" % a, ()) for a in model.TOGGLE_ACTIONS]
        errors += [
            ("combat.plan.source.%s" % s, ()) for s in ("scheme", "default", "override")
        ]
        codes = {code for code, _ in errors}
        self.assertTrue(
            {
                "combat.dsl.not_text",
                "combat.dsl.too_many_commands",
                "combat.dsl.degrees_out_of_range",
                "combat.dsl.speed_out_of_range",
                "combat.scheme.missing_combo",
                "combat.scheme.slot_combo",
                "combat.scheme.no_enabled_slot",
                "combat.scheme.bad_option",
                "combat.scheme.bad_signal",
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
