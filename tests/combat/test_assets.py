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
    PIPELINE.update(load_json(ASSETS / "resource" / "base" / "pipeline" / "Combat" / _name))
TASKS = {name: load_json(ASSETS / "resource" / "tasks" / name) for name in ("AutoCombat.json", "CombatSetting.json")}
INTERFACE = load_json(ASSETS / "interface.json")
OPTIONS = {k: v for t in TASKS.values() for k, v in t.get("option", {}).items()}


def iter_labels(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("label", "description") and isinstance(value, str) and value.startswith("$"):
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


class WiringTests(unittest.TestCase):
    def test_interface_registration(self):
        imports = INTERFACE["import"]
        self.assertIn("resource/tasks/AutoCombat.json", imports)
        self.assertIn("resource/tasks/CombatSetting.json", imports)
        self.assertEqual(INTERFACE["global_option"], ["CombatScheme"])
        section = next(s for s in INTERFACE["setting"] if s["name"] == "Combat")
        for key in section["option"] + INTERFACE["global_option"]:
            self.assertIn(key, OPTIONS)

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
        cases = {c["name"] for c in OPTIONS["CombatScheme"]["cases"]}
        self.assertEqual(cases, set(settings.SCHEME_CHOICES))
        self.assertEqual(OPTIONS["CombatScheme"]["default_case"], scheme.DEFAULT_SCHEME)
        self.assertEqual(PIPELINE["CombatSetting_Scheme"]["attach"]["scheme"], scheme.DEFAULT_SCHEME)
        for name in settings.GLOBAL_SETTING_NODES:
            self.assertFalse(PIPELINE[name]["enabled"], name)

    def test_custom_names_registered_and_option_nodes_exist(self):
        source = (ACTION_DIR / "Combat" / "actions.py").read_text(encoding="utf-8")
        actions = set(re.findall(r'custom_action\("([^"]+)"\)', source))
        recognitions = set(re.findall(r'custom_recognition\("([^"]+)"\)', source))
        for name, node in PIPELINE.items():
            action = node.get("action", {})
            if isinstance(action, dict) and action.get("type") == "Custom":
                self.assertIn(action["param"]["custom_action"], actions, name)
                for carrier in action["param"]["custom_action_param"].get("option_nodes", []):
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
        pinkpaw = load_json(ASSETS / "resource" / "base" / "pipeline" / "PinkPawHeist" / "tools.json")
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

    def test_agent_combat_keys_identical_across_languages(self):
        reference = {k for k in locale("agent", "zh_cn") if k.startswith("combat.")}
        self.assertGreater(len(reference), 50)
        for lang in LANGS:
            data = locale("agent", lang)
            self.assertEqual({k for k in data if k.startswith("combat.")}, reference, lang)
            for key in reference:
                self.assertEqual(
                    len(PLACEHOLDER.findall(data[key])), len(PLACEHOLDER.findall(locale("agent", "zh_cn")[key])), (lang, key)
                )

    def test_builtin_scheme_names_translated(self):
        for name in scheme.BUILTIN_SCHEMES:
            s = locate.load_builtin(name)
            for text in [s.name] + [t.note for t in s.team] + [t.character for t in s.team]:
                if text.startswith("$"):
                    for lang in LANGS:
                        self.assertIn(text[1:], locale("agent", lang), (lang, text))

    def test_every_error_code_formats_in_every_language(self):
        errors = []
        for source in [
            "skill,\nl_click(", "x = 1", "self.click()", "perform", "jump(1)", "wait", "skill(speed=1)",
            "skill(0.1, seconds=0.2)", "wait(0.5*2)", "skill(fast)", "wait(11)", ", ".join(["jump"] * 201), "", 1,
        ]:
            try:
                dsl.compile_combo(source)
            except dsl.ComboError as exc:
                errors.append(("combat.dsl.%s" % exc.code, exc.params))
        scheme_cases = [
            [], {"combos": {}}, {"version": 1}, {"version": 1, "combos": {"9": "skill"}},
            {"version": 1, "combos": {"1": "x = 1"}}, {"version": 1, "combos": {"1": "skill"}, "rotation": {"order": [1, 2]}},
            {"version": 1, "combos": {"default": "skill"}, "rotation": []},
            {"version": 1, "combos": {"default": "skill"}, "rotation": {"mode": "x"}},
            {"version": 1, "combos": {"default": "skill"}, "rotation": {"order": [9]}},
            {"version": 1, "combos": {"default": "skill"}, "rotation": {"min_field_time": 99}},
            {"version": 1, "combos": {"default": "skill"}, "team": 1},
            {"version": 1, "combos": {"default": "skill"}, "team": [{"slot": 1}, {"slot": 1}]},
        ]
        for data in scheme_cases:
            with self.assertRaises(scheme.SchemeError) as ctx:
                scheme.parse_scheme(data)
            exc = ctx.exception  # combo 错误的模板末尾追加 DSL 详情（见 api.describe_scheme_error）
            errors.append(("combat.scheme.%s" % exc.code, exc.params + (("detail",) if exc.combo_error else ())))
        for code, params in [
            ("bad_json", (3, 7)), ("too_large", (64,)), ("read_failed", ("a.json", "denied")), ("not_utf8", ("a.json",)),
            ("unknown_builtin", ("x",)), ("builtin_missing", ("x",)), ("custom_empty", ()),
            ("custom_not_found", ("x", "dirs")), ("custom_not_json", ("x.txt",)),
        ]:
            errors.append(("combat.scheme.%s" % code, params))
        errors += [
            ("combat.dsl.at", (1, 2)), ("combat.waiting", ("name",)), ("combat.started", ("name",)),
            ("combat.ended", ("reason", 12.3)), ("combat.ended_no_fight", ()), ("combat.scheme_error", ("detail",)),
            ("combat.slot_unavailable", (3,)), ("combat.degraded", ()), ("combat.team_hint", ("name", "slots")),
            ("combat.team.slot", (1, "label")),
        ]
        reasons = [v for k, v in vars(submodule("model").EndReason).items() if not k.startswith("_")]
        self.assertEqual(len(reasons), 7)
        errors += [("combat.reason.%s" % r, ()) for r in reasons]
        codes = {code for code, _ in errors}
        self.assertTrue({"combat.dsl.not_text", "combat.dsl.too_many_commands", "combat.scheme.missing_combo"} <= codes)
        for lang in LANGS:
            data = locale("agent", lang)
            for code, params in errors:
                self.assertIn(code, data, (lang, code))
                try:
                    data[code] % params if params or PLACEHOLDER.search(data[code]) else data[code]
                except (TypeError, ValueError) as exc:
                    self.fail("%s %s %r: %s" % (lang, code, params, exc))


if __name__ == "__main__":
    unittest.main()
