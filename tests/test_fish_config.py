"""FishNew 配置契约检查，不导入 Maa 或钓鱼控制核心。"""

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / "assets" / "resource"
LANGUAGES = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")
MODE_OPTION = "FishNewControlMode"
INTERFACE_PREFIX = "task_fish_new_option_control_mode"
INTERFACE_KEYS = (
    INTERFACE_PREFIX,
    INTERFACE_PREFIX + "_desc",
    INTERFACE_PREFIX + "_case_off",
    INTERFACE_PREFIX + "_case_infer",
    INTERFACE_PREFIX + "_case_learn",
)
AGENT_FORMATS = {
    "fish.learning_unavailable": (),
    "fish.learning_save_failed": (),
    "fish.unsupported_frame": (),
    "fish.engine_summary": ("%d", "%d"),
}

# 先匹配完整的 JSON 字符串，避免把 URL、转义引号后的 // 等误当注释。
_JSONC_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*.*?\*/', re.DOTALL)
_FORMAT_TOKENS = re.compile(
    r"%(?:\([^)]+\))?[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[a-zA-Z%]"
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _loads_jsonc(text):
    """测试专用：支持行/块注释，保留字符串及注释中的换行位置。"""

    def replace_token(match):
        token = match.group()
        if token.startswith('"'):
            return token
        return "".join(char if char in "\r\n" else " " for char in token)

    return json.loads(
        _JSONC_TOKENS.sub(replace_token, text), object_pairs_hook=_unique_object
    )


def _load_jsonc(path):
    return _loads_jsonc(path.read_text(encoding="utf-8-sig"))


def _format_tokens(text):
    return tuple(token for token in _FORMAT_TOKENS.findall(text) if token != "%%")


class JsoncLoaderTests(unittest.TestCase):
    def test_comment_markers_inside_strings_are_preserved(self):
        expected = {
            "url": "https://example.test/a//b",
            "quoted": 'an escaped quote " followed by // text',
            "block": "/* not a comment */",
            "backslashes": r"C:\folder\\//keep",
        }
        body = json.dumps(expected, ensure_ascii=False)
        source = "/* banner */\n{// property comment\n" + body[1:] + "// end"
        self.assertEqual(_loads_jsonc(source), expected)

    def test_comments_between_tokens_are_supported(self):
        self.assertEqual(
            _loads_jsonc('{"left"/* one */: 1, /* two\n lines */"right": 2}'),
            {"left": 1, "right": 2},
        )

    def test_comments_cannot_join_invalid_number_tokens(self):
        with self.assertRaises(json.JSONDecodeError):
            _loads_jsonc('{"number": 1/* gap */2}')

    def test_unterminated_comment_is_rejected(self):
        with self.assertRaises(json.JSONDecodeError):
            _loads_jsonc('{"number": 1} /* unfinished')

    def test_duplicate_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            _loads_jsonc('{"key": 1, "key": 2}')


class FishConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = _load_jsonc(RESOURCE / "tasks" / "Fish.json")
        cls.tasks = {task["name"]: task for task in cls.config["task"]}
        cls.pipeline = _load_jsonc(RESOURCE / "base" / "pipeline" / "Fish" / "FishNew.json")
        cls.gaming = cls.pipeline["FishNewGaming"]
        cls.mode = cls.config["option"][MODE_OPTION]

    def test_control_mode_is_only_available_to_fish_new(self):
        self.assertEqual(self.tasks["FishNew"]["option"].count(MODE_OPTION), 1)
        pending = list(self.tasks["Fish"]["option"])
        visited = set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            self.assertNotEqual(name, MODE_OPTION)
            option = self.config["option"][name]
            for definition in (option, *option.get("cases", [])):
                self.assertNotIn("FishNewGaming", definition.get("pipeline_override", {}))
                pending.extend(definition.get("option", []))

    def test_all_three_cases_use_only_the_v2_learning_mode_override(self):
        self.assertEqual(self.mode["type"], "select")
        self.assertEqual(self.mode["default_case"], "off")
        self.assertEqual(self.mode["label"], "$" + INTERFACE_PREFIX)
        self.assertEqual(self.mode["description"], "$" + INTERFACE_PREFIX + "_desc")
        self.assertEqual(
            [case["name"] for case in self.mode["cases"]], ["off", "infer", "learn"]
        )
        for case in self.mode["cases"]:
            with self.subTest(mode=case["name"]):
                self.assertEqual(
                    case["label"], "$" + INTERFACE_PREFIX + "_case_" + case["name"]
                )
                self.assertEqual(
                    case["pipeline_override"],
                    {
                        "FishNewGaming": {
                            "action": {
                                "param": {
                                    "custom_action_param": {
                                        "learning_mode": case["name"]
                                    }
                                }
                            }
                        }
                    },
                )

    def test_gaming_uses_shared_recognition_and_existing_action_names(self):
        self.assertEqual(
            self.gaming["recognition"],
            {"type": "Custom", "param": {"custom_recognition": "fish_control_visible"}},
        )
        self.assertEqual(
            self.gaming["action"],
            {
                "type": "Custom",
                "param": {
                    "custom_action": "auto_fish_without_cv",
                    "custom_action_param": {"learning_mode": "off"},
                },
            },
        )
        for field in ("all_of", "custom_action", "custom_action_param"):
            self.assertNotIn(field, self.gaming)
        for field in ("rate_limit", "pre_delay", "post_delay"):
            self.assertEqual(self.gaming[field], 0)

    def test_gaming_handoffs_are_unchanged(self):
        self.assertEqual(
            self.gaming["next"],
            [
                "FishNewGameResult",
                "FishNewEscapeResult",
                "FishNewStart",
                "FishNewGaming",
            ],
        )
        self.assertEqual(self.gaming["on_error"], ["[Anchor]FishNewErrorRestart"])

    def test_interface_still_registers_fish_once_and_preserves_entries(self):
        interface = _load_jsonc(ROOT / "assets" / "interface.json")
        self.assertEqual(interface["import"].count("resource/tasks/Fish.json"), 1)
        self.assertEqual(self.tasks["Fish"]["entry"], "FishEntrance")
        self.assertEqual(self.tasks["FishNew"]["entry"], "FishNewEntrance")
        self.assertIn(self.tasks["FishNew"]["entry"], self.pipeline)

    def test_all_five_interface_locales_have_mode_labels(self):
        for language in LANGUAGES:
            translations = _load_jsonc(
                RESOURCE / "locales" / "interface" / (language + ".json")
            )
            for key in INTERFACE_KEYS:
                with self.subTest(language=language, key=key):
                    self.assertIn(key, translations)
                    text = translations[key]
                    self.assertIsInstance(text, str)
                    self.assertTrue(text.strip())
                    self.assertEqual(_format_tokens(text), ())
                    self.assertEqual(text % (), text)

    def test_all_five_agent_locales_have_matching_format_parameters(self):
        for language in LANGUAGES:
            translations = _load_jsonc(RESOURCE / "locales" / "agent" / (language + ".json"))
            for key, expected in AGENT_FORMATS.items():
                with self.subTest(language=language, key=key):
                    self.assertIn(key, translations)
                    text = translations[key]
                    self.assertIsInstance(text, str)
                    self.assertTrue(text.strip())
                    self.assertEqual(_format_tokens(text), expected)
                    self.assertIsInstance(text % ((7, 3) if expected else ()), str)
            self.assertIn("16:9", translations["fish.unsupported_frame"])


if __name__ == "__main__":
    unittest.main()
