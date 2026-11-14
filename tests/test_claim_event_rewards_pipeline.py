"""七格领奖配置与离线FSM契约；模拟识别命中，不替代MaaSDK回放或实机验收。"""

from itertools import product
import json
from pathlib import Path
import re
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = ROOT / "assets/resource/base/pipeline/ClaimEventRewards.json"
IMAGE_ROOT = ROOT / "assets/resource/base/image"
PREFIX = "ClaimEventRewards"
BUTTONS = (
    (709, 410, 116, 36),
    (827, 410, 116, 36),
    (945, 410, 116, 36),
    (709, 595, 116, 36),
    (827, 595, 116, 36),
    (945, 595, 116, 36),
    (1067, 574, 177, 57),
)
AVAILABLE = "可领取"
CLAIMED = "已领取"
LOCKED = "未解锁"
JSONC_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\r\n]*')
PUBLIC_REFERENCES = {
    "InEventsMenu",
    "InWorld",
    "SceneAnyEnterEventsMenu",
    "SceneAnyEnterWorld",
    "SceneClickBlankToExit",
}


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_pipeline():
    text = PIPELINE_PATH.read_text(encoding="utf-8")
    text = JSONC_TOKEN.sub(
        lambda match: match[0] if match[0].startswith('"') else "", text
    )
    return json.loads(text, object_pairs_hook=unique_mapping)


def unpack_reference(reference):
    attributes = set()
    while reference.startswith("["):
        attribute, reference = reference[1:].split("]", 1)
        attributes.add(attribute)
    return reference, attributes


def day_name(day, suffix):
    return "%sDay%d%s" % (PREFIX, day, suffix)


def next_day(day):
    if day == 7:
        return [PREFIX + "CheckedAllDays"]
    return [
        day_name(day + 1, "Available"),
        day_name(day + 1, "AlreadyClaimed"),
        day_name(day + 1, "Locked"),
    ]


class PipelineSimulation:
    """只模拟持久识别命中/超时、顺序next、JumpBack与运行期锚点，无图像或游戏操作。"""

    def __init__(self, pipeline, states, **options):
        self.pipeline = pipeline
        self.states = list(states)
        self.options = options
        self.stage = options.get("stage", "event_page")
        self.scores = [0.99] * 7
        self.anchors = {}
        self.awaiting_day = None
        self.trace = []
        self.clicks = []
        self.dismissals = []
        self.messages = []
        self.navigation_calls = 0
        self.world_navigation_calls = 0
        self.scan_calls = 0
        self.swipes = []
        self.overrides = {}
        self.failure_node = None
        self.success = False

    def resolve(self, reference):
        name, attributes = unpack_reference(reference)
        if "Anchor" in attributes:
            name = self.anchors.get(name)
        return name, attributes

    def ocr_text(self, name, param):
        roi = tuple(param["roi"])
        if roi in BUTTONS:
            index = BUTTONS.index(roi)
            if self.stage != "event_page":
                return None, 0.0
            return self.states[index], self.scores[index]
        if name == PREFIX + "EventTabText":
            if self.stage not in ("event_page", "event_menu"):
                return None, 0.0
            if not self.options.get("tab_visible", True):
                return None, 0.0
            return self.options.get("tab_text", "环期赠礼"), 0.99
        if name == PREFIX + "Title":
            valid = self.stage == "event_page" and self.options.get("title", True)
            return ("环期赠礼", 0.99) if valid else (None, 0.0)
        if name == PREFIX + "SignInHint":
            valid = self.stage == "event_page" and self.options.get("hint", True)
            return ("签到领取奖励", 0.99) if valid else (None, 0.0)
        if name == PREFIX + "ScanHeader":
            valid = self.stage in ("event_menu", "event_page") and self.options.get(
                "scan_header", True
            )
            return (
                ("限时", self.options.get("scan_header_score", 0.99))
                if valid
                else (None, 0.0)
            )
        if name == PREFIX + "ScanNames":
            valid = self.stage in ("event_menu", "event_page") and self.options.get(
                "scan_names", True
            )
            return ("其他活动", 0.99) if valid else (None, 0.0)
        if name in (PREFIX + "RewardPopupTitle", PREFIX + "RewardPopupHint"):
            valid = self.stage == "popup" and self.options.get(name, True)
            if not valid:
                return None, 0.0
            # 弹窗文案取配置的字面值；这里只驱动已验证配置的识别命中，不生成假截图。
            defaults = param["expected"][0].removeprefix("^").removesuffix("$")
            return self.options.get(name + "Text", defaults), 0.99
        raise AssertionError("Unmodelled OCR node: %s" % name)

    def hit(self, name):
        if name == "InEventsMenu":
            return self.stage in ("event_menu", "event_page")
        if name == "InWorld":
            return self.stage == "world" and self.options.get("world_identity", True)
        if name in ("SceneAnyEnterEventsMenu", "SceneAnyEnterWorld"):
            return True
        if name == "SceneClickBlankToExit":
            return self.stage == "popup"
        node = self.pipeline[name]
        if not node.get("enabled", True):
            return False
        recognition = node["recognition"]
        kind = recognition["type"]
        param = recognition.get("param", {})
        if kind == "DirectHit":
            return True
        if kind == "And":
            return all(self.hit(child) for child in param["all_of"])
        if kind == "Or":
            return any(self.hit(child) for child in param["any_of"])
        if kind == "OCR":
            text, score = self.ocr_text(name, param)
            if text is None or score < param["threshold"]:
                return False
            return not param["expected"] or any(
                re.search(pattern, text) for pattern in param["expected"]
            )
        if kind == "ColorMatch":
            roi = tuple(param["roi"])
            index = BUTTONS.index(roi)
            yellow = self.options.get("yellow", {}).get(
                index + 1, self.states[index] == AVAILABLE
            )
            return self.stage == "event_page" and yellow
        if kind == "TemplateMatch":
            if name.endswith("RewardSlot"):
                day = int(re.fullmatch(PREFIX + r"Day([1-7])RewardSlot", name)[1])
                return (
                    self.stage == "event_page"
                    and self.options.get("layout", True)
                    and day != self.options.get("missing_slot")
                )
            if name == PREFIX + "RewardPopupIcon":
                return self.stage == "popup" and self.options.get(name, True)
        raise AssertionError("Unmodelled recognition: %s" % name)

    def choose(self, references):
        for reference in references:
            name, attributes = self.resolve(reference)
            if name and self.hit(name):
                return name, attributes
        return None, set()

    def execute(self, name):
        self.trace.append(name)
        if name == "SceneAnyEnterEventsMenu":
            self.navigation_calls += 1
            self.stage = self.options.get("navigation_result", "event_menu")
            return not self.options.get("navigation_action_failed", False)
        if name == "SceneAnyEnterWorld":
            self.world_navigation_calls += 1
            self.stage = self.options.get("return_world_result", "world")
            return not self.options.get("world_action_failed", False)
        node = self.pipeline[name]
        action = node.get("action", {"type": "DoNothing"})
        if action["type"] == "Custom":
            custom_action = action["param"]["custom_action"]
            if custom_action == "claim_event_rewards_scan":
                self.scan_calls += 1
                result = self.options.get("scan_result", "failed")
                if result == "found":
                    self.options["tab_visible"] = True
                    return True
                if result == "absent_proved":
                    self.overrides[name] = [PREFIX + "NoEvent"]
                    return True
                return False
            if custom_action != "claim_event_rewards_fail":
                raise AssertionError("Unexpected custom action: %s" % name)
            self.failure_node = name
            message = node.get("focus", {}).get("Node.Action.Failed")
            if message:
                self.messages.append(message)
            return False
        if action["type"] == "Click":
            if name == PREFIX + "EventTab":
                self.stage = self.options.get("tab_result", "event_page")
            elif name == PREFIX + "RewardPopup":
                self.dismissals.append(self.awaiting_day)
                if self.options.get("dismiss_action_failed", False):
                    return False
                if self.options.get("popup_remains", False):
                    self.stage = "popup"
                else:
                    self.stage = "event_page"
                    day = self.awaiting_day
                    self.states[day - 1] = self.options.get("post_claim", {}).get(
                        day, CLAIMED
                    )
                    self.scores[day - 1] = self.options.get("post_claim_score", 0.99)
            else:
                match = re.fullmatch(PREFIX + r"Day([1-7])Available", name)
                if not match:
                    raise AssertionError("Unexpected click node: %s" % name)
                day = int(match[1])
                self.clicks.append(day)
                self.awaiting_day = day
                if self.options.get("input_action_failed", False):
                    return False
                self.stage = (
                    "popup" if self.options.get("show_popup", True) else "event_page"
                )
        elif action["type"] == "Swipe":
            self.swipes.append(name)
            if self.options.get("swipe_action_failed", False):
                return False
        elif action["type"] != "DoNothing":
            raise AssertionError("Unexpected action: %s" % action["type"])
        for anchor, target in node.get("anchor", {}).items():
            if target:
                self.anchors[anchor] = target
            else:
                self.anchors.pop(anchor, None)
        self.messages.extend(node.get("focus", {}).values())
        for day, state in self.options.get("after_node", {}).get(name, {}).items():
            self.states[day - 1] = state
        return True

    def run(self, entry=PREFIX + "Entrance"):
        self.trace = []
        self.clicks = []
        self.dismissals = []
        self.messages = []
        self.navigation_calls = 0
        self.world_navigation_calls = 0
        self.scan_calls = 0
        self.swipes = []
        self.overrides = {}
        self.failure_node = None
        self.success = False
        if not self.hit(entry):
            return False
        current = entry
        stack = []
        references = None
        for _ in range(200):
            if references is None:
                if not self.execute(current):
                    references = self.pipeline.get(current, {}).get("on_error", [])
                    stack.clear()
                    if not references:
                        return False
                else:
                    references = self.overrides.get(
                        current, self.pipeline.get(current, {}).get("next", [])
                    )
            if not references:
                if stack:
                    current, references = stack.pop()
                    continue
                self.success = True
                return True
            target, attributes = self.choose(references)
            if target is None:
                references = self.pipeline.get(current, {}).get("on_error", [])
                stack.clear()
                if not references:
                    return False
                target, attributes = self.choose(references)
                if target is None:
                    return False
            if "JumpBack" in attributes:
                stack.append((current, references))
            current = target
            references = None
        raise AssertionError("Pipeline did not terminate within the finite FSM budget")


class ClaimEventRewardsPipelineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = load_pipeline()

    def test_jsonc_loader_rejects_duplicate_keys(self):
        with self.assertRaises(ValueError):
            json.loads('{"a": 1, "a": 2}', object_pairs_hook=unique_mapping)

    def test_nodes_have_prefix_and_explicit_zero_delays(self):
        for name, node in self.pipeline.items():
            with self.subTest(node=name):
                self.assertTrue(name.startswith(PREFIX))
                self.assertIsInstance(node["recognition"], dict)
                for field in ("rate_limit", "pre_delay", "post_delay"):
                    self.assertEqual(node[field], 0)
                for field in (
                    "max_hit",
                    "repeat",
                    "repeat_delay",
                    "timeout",
                    "inverse",
                ):
                    self.assertNotIn(field, node)

    def test_all_references_exist_and_scene_references_are_public(self):
        for name, node in self.pipeline.items():
            references = list(node.get("next", [])) + list(node.get("on_error", []))
            param = node["recognition"].get("param", {})
            references += param.get("all_of", []) + param.get("any_of", [])
            references += [value for value in node.get("anchor", {}).values() if value]
            for reference in references:
                with self.subTest(node=name, reference=reference):
                    target, attributes = unpack_reference(reference)
                    self.assertTrue(attributes <= {"Anchor", "JumpBack"})
                    self.assertNotIn("__ScenePrivate", target)
                    if "Anchor" not in attributes:
                        self.assertIn(target, set(self.pipeline) | PUBLIC_REFERENCES)

    def test_all_rectangles_are_within_1280_by_720(self):
        def check(value, key=""):
            if isinstance(value, dict):
                for child_key, child in value.items():
                    check(child, child_key)
            elif isinstance(value, list):
                if key in ("roi", "target", "source_roi", "begin", "end"):
                    self.assertEqual(len(value), 4)
                    self.assertTrue(all(isinstance(number, int) for number in value))
                    x, y, width, height = value
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertGreater(width, 0)
                    self.assertGreater(height, 0)
                    self.assertLessEqual(x + width, 1280)
                    self.assertLessEqual(y + height, 720)
                else:
                    for child in value:
                        check(child)

        check(self.pipeline)

    def test_templates_exist_fit_roi_and_are_lossless_png(self):
        for name, node in self.pipeline.items():
            recognition = node["recognition"]
            if recognition["type"] != "TemplateMatch":
                continue
            param = recognition["param"]
            with self.subTest(node=name):
                self.assertTrue(param["template"].startswith(PREFIX + "/"))
                image = IMAGE_ROOT / param["template"]
                self.assertTrue(image.is_file())
                header = image.read_bytes()[:24]
                self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(header[12:16], b"IHDR")
                width, height = struct.unpack(">II", header[16:24])
                self.assertGreater(width, 0)
                self.assertGreater(height, 0)
                self.assertLessEqual(width, param["roi"][2])
                self.assertLessEqual(height, param["roi"][3])
        first = self.pipeline[day_name(1, "RewardSlot")]
        self.assertEqual(first["attach"]["source_roi"], [865, 328, 41, 42])

    def test_page_identity_uses_title_hint_and_seven_stable_slots(self):
        identity = self.pipeline[PREFIX + "PageIdentity"]["recognition"]["param"]
        self.assertEqual(
            identity["all_of"],
            [
                "InEventsMenu",
                PREFIX + "Title",
                PREFIX + "SignInHint",
                PREFIX + "CardLayout",
            ],
        )
        self.assertEqual(
            self.pipeline[PREFIX + "CardLayout"]["recognition"]["param"]["all_of"],
            [day_name(day, "RewardSlot") for day in range(1, 8)],
        )
        for name, text, roi in (
            ("Title", "环期赠礼", [1030, 90, 217, 78]),
            ("SignInHint", "签到领取奖励", [1110, 202, 135, 45]),
        ):
            param = self.pipeline[PREFIX + name]["recognition"]["param"]
            self.assertEqual(param["expected"], [text])
            self.assertEqual(param["threshold"], 0.8)
            self.assertEqual(param["roi"], roi)
        quantities = [
            self.pipeline[day_name(day, "RewardSlot")]["attach"]["reward_count"]
            for day in range(1, 8)
        ]
        self.assertEqual(quantities, [1, 1, 2, 1, 1, 1, 3])
        self.assertEqual(sum(quantities), 10)

    def test_each_day_uses_exact_words_confidence_and_same_button_roi(self):
        for day in range(1, 8):
            for suffix, text in (
                ("AvailableText", AVAILABLE),
                ("ClaimedText", CLAIMED),
                ("LockedText", LOCKED),
            ):
                param = self.pipeline[day_name(day, suffix)]["recognition"]["param"]
                with self.subTest(day=day, state=text):
                    self.assertEqual(tuple(param["roi"]), BUTTONS[day - 1])
                    self.assertEqual(param["threshold"], 0.8)
                    self.assertEqual(param["expected"], ["^" + text + "$"])
                    for other in ("不" + text, text + "中", text[:-1], "状态：" + text):
                        self.assertIsNone(re.search(param["expected"][0], other))
            yellow = self.pipeline[day_name(day, "YellowButton")]["recognition"][
                "param"
            ]
            self.assertEqual(tuple(yellow["roi"]), BUTTONS[day - 1])
            self.assertEqual(yellow["method"], 40)
            self.assertEqual(yellow["lower"], [18, 100, 120])
            self.assertEqual(yellow["upper"], [36, 255, 255])
            self.assertEqual(yellow["count"], 600)
            self.assertIs(yellow["connected"], True)
            available = self.pipeline[day_name(day, "Available")]
            self.assertEqual(
                tuple(available["action"]["param"]["target"]), BUTTONS[day - 1]
            )
            self.assertEqual(
                available["recognition"]["param"]["all_of"],
                [
                    PREFIX + "PageIdentity",
                    day_name(day, "AvailableText"),
                    day_name(day, "YellowButton"),
                ],
            )

    def test_every_day_advances_monotonically_after_positive_state(self):
        for day in range(1, 8):
            for suffix, text_suffix in (
                ("AlreadyClaimed", "ClaimedText"),
                ("Locked", "LockedText"),
                ("ClaimConfirmed", "ClaimedText"),
            ):
                node = self.pipeline[day_name(day, suffix)]
                with self.subTest(day=day, branch=suffix):
                    self.assertEqual(
                        node["recognition"]["param"]["all_of"],
                        [PREFIX + "PageIdentity", day_name(day, text_suffix)],
                    )
                    self.assertEqual(node["next"], next_day(day))
                    self.assertNotIn("action", node)
            available = self.pipeline[day_name(day, "Available")]
            self.assertEqual(available["next"], [PREFIX + "RewardPopup"])
            self.assertEqual(
                available["anchor"],
                {PREFIX + "ClaimConfirmation": day_name(day, "ClaimConfirmed")},
            )
            confirmed = self.pipeline[day_name(day, "ClaimConfirmed")]
            self.assertEqual(
                confirmed["anchor"][PREFIX + "Outcome"], PREFIX + "Complete"
            )
            self.assertEqual(confirmed["anchor"][PREFIX + "ClaimConfirmation"], "")

    def test_popup_requires_reward_identity_and_only_routes_to_confirmation(self):
        popup = self.pipeline[PREFIX + "RewardPopup"]
        self.assertTrue(popup.get("enabled", True))
        self.assertEqual(popup["recognition"]["type"], "And")
        required = popup["recognition"]["param"]["all_of"]
        self.assertIn(PREFIX + "RewardPopupTitle", required)
        self.assertIn(PREFIX + "RewardPopupHint", required)
        self.assertIn(PREFIX + "RewardPopupIcon", required)
        self.assertEqual(popup["action"]["type"], "Click")
        self.assertEqual(popup["next"], ["[Anchor]" + PREFIX + "ClaimConfirmation"])
        for suffix in ("RewardPopupTitle", "RewardPopupHint"):
            recognition = self.pipeline[PREFIX + suffix]["recognition"]
            self.assertEqual(recognition["type"], "OCR")
            self.assertEqual(recognition["param"]["threshold"], 0.8)
            self.assertNotEqual(recognition["param"]["expected"], ["关闭"])

    def test_scanner_default_routes_and_raw_ocr_contract(self):
        self.assertEqual(
            self.pipeline[PREFIX + "InMenu"]["next"],
            [PREFIX + "PageReady", PREFIX + "EventTab", PREFIX + "ScanSidebar"],
        )
        scanner = self.pipeline[PREFIX + "ScanSidebar"]
        self.assertEqual(
            scanner["recognition"]["param"]["all_of"],
            ["InEventsMenu", PREFIX + "ScanHeader"],
        )
        self.assertEqual(
            scanner["action"]["param"]["custom_action"], "claim_event_rewards_scan"
        )
        self.assertEqual(scanner["next"], [PREFIX + "EventTab"])
        self.assertEqual(scanner["on_error"], [PREFIX + "Failed"])
        self.assertEqual(
            self.pipeline[PREFIX + "EventTab"]["on_error"], [PREFIX + "Failed"]
        )
        header = self.pipeline[PREFIX + "ScanHeader"]["recognition"]["param"]
        self.assertEqual(
            header, {"roi": [28, 109, 153, 44], "expected": ["限时"], "threshold": 0.8}
        )
        names = self.pipeline[PREFIX + "ScanNames"]["recognition"]["param"]
        self.assertEqual(
            names, {"roi": [75, 157, 124, 460], "expected": [], "threshold": 0.8}
        )
        for name, node in self.pipeline.items():
            with self.subTest(node=name):
                for reference in node.get("next", []) + node.get("on_error", []):
                    self.assertNotEqual(
                        unpack_reference(reference)[0], PREFIX + "NoEvent"
                    )

    def test_scan_moves_are_single_input_with_overlap_and_no_recovery_chain(self):
        viewport = self.pipeline[PREFIX + "ScanNames"]["recognition"]["param"]["roi"]
        for suffix, direction in (("ScanUp", 1), ("ScanDown", -1)):
            node = self.pipeline[PREFIX + suffix]
            with self.subTest(node=suffix):
                self.assertEqual(
                    node["recognition"]["param"]["all_of"],
                    ["InEventsMenu", PREFIX + "ScanHeader"],
                )
                self.assertEqual(node["action"]["type"], "Swipe")
                param = node["action"]["param"]
                self.assertEqual(param["duration"], 300)
                self.assertEqual(param["end"][1] - param["begin"][1], direction * 300)
                for rect in (param["begin"], param["end"]):
                    self.assertGreaterEqual(rect[0], viewport[0])
                    self.assertGreaterEqual(rect[1], viewport[1])
                    self.assertLessEqual(rect[0] + rect[2], viewport[0] + viewport[2])
                    self.assertLessEqual(rect[1] + rect[3], viewport[1] + viewport[3])
                self.assertEqual(
                    viewport[3] - abs(param["end"][1] - param["begin"][1]), 160
                )
                self.assertEqual(
                    node["post_wait_freezes"],
                    {"time": 150, "target": viewport, "rate_limit": 0},
                )
                self.assertEqual(node["next"], [])
                self.assertEqual(node["on_error"], [])

    def test_only_world_confirmation_emits_completion_and_consumes_exit_navigation(
        self,
    ):
        emitters = [
            name
            for name, node in self.pipeline.items()
            if "$claim_event_rewards_complete" in node.get("focus", {}).values()
        ]
        self.assertEqual(emitters, [PREFIX + "WorldConfirmed"])
        self.assertEqual(
            self.pipeline[PREFIX + "NoReward"]["next"], [PREFIX + "Complete"]
        )
        self.assertEqual(
            self.pipeline[PREFIX + "Complete"]["next"], [PREFIX + "ReturnWorld"]
        )
        self.assertEqual(
            self.pipeline[PREFIX + "NoEvent"]["next"], [PREFIX + "ReturnWorld"]
        )
        self.assertEqual(
            self.pipeline[PREFIX + "NoEvent"]["recognition"]["param"]["all_of"],
            ["InEventsMenu", PREFIX + "ScanHeader"],
        )
        returning = self.pipeline[PREFIX + "ReturnWorld"]
        self.assertEqual(
            returning["next"],
            [
                PREFIX + "WorldConfirmed",
                "[JumpBack][Anchor]" + PREFIX + "WorldNavigation",
            ],
        )
        self.assertEqual(
            returning["anchor"],
            {PREFIX + "WorldNavigation": PREFIX + "NavigateToWorld"},
        )
        navigation = self.pipeline[PREFIX + "NavigateToWorld"]
        self.assertEqual(navigation["next"], ["SceneAnyEnterWorld"])
        self.assertEqual(navigation["anchor"], {PREFIX + "WorldNavigation": ""})
        confirmed = self.pipeline[PREFIX + "WorldConfirmed"]
        self.assertEqual(confirmed["recognition"]["param"]["all_of"], ["InWorld"])
        self.assertEqual(confirmed["anchor"], {PREFIX + "WorldNavigation": ""})
        self.assertEqual(confirmed["next"], [])

    def test_popup_texts_and_geometry_match_real_capture(self):
        for suffix, text, roi in (
            ("RewardPopupTitle", "获得物品", [585, 289, 111, 45]),
            ("RewardPopupHint", "点击空白区域关闭", [550, 616, 184, 42]),
        ):
            param = self.pipeline[PREFIX + suffix]["recognition"]["param"]
            self.assertEqual(param["expected"], [text])
            self.assertEqual(param["roi"], roi)
            self.assertEqual(param["threshold"], 0.8)
        icon = self.pipeline[PREFIX + "RewardPopupIcon"]
        self.assertEqual(icon["recognition"]["param"]["roi"], [603, 356, 75, 74])
        self.assertEqual(icon["attach"]["source_roi"], [617, 370, 46, 47])
        self.assertEqual(
            self.pipeline[PREFIX + "RewardPopup"]["action"]["param"]["target"],
            [550, 616, 184, 42],
        )

    def test_no_direct_hit_success_fallback_or_stop_task_failure(self):
        allowed = {
            PREFIX + suffix
            for suffix in (
                "Entrance",
                "NavigateToEvents",
                "NavigateToWorld",
                "UnverifiedList",
                "Failed",
            )
        }
        for name, node in self.pipeline.items():
            if node["recognition"]["type"] == "DirectHit":
                self.assertIn(name, allowed)
            action = node.get("action", {}).get("type", "DoNothing")
            self.assertNotEqual(action, "StopTask")
            if action == "Custom":
                custom_action = node["action"]["param"]["custom_action"]
                if name == PREFIX + "ScanSidebar":
                    self.assertEqual(custom_action, "claim_event_rewards_scan")
                    self.assertEqual(node["next"], [PREFIX + "EventTab"])
                    self.assertEqual(node["on_error"], [PREFIX + "Failed"])
                else:
                    self.assertIn(name, {PREFIX + "UnverifiedList", PREFIX + "Failed"})
                    self.assertEqual(custom_action, "claim_event_rewards_fail")
                    self.assertEqual(node["next"], [])
                    self.assertEqual(node["on_error"], [])
                    self.assertEqual(
                        node["focus"],
                        {"Node.Action.Failed": "$claim_event_rewards_failed"},
                    )
        init_text = (ROOT / "agent/custom/action/__init__.py").read_text(
            encoding="utf-8"
        )
        scan_text = (
            ROOT / "agent/custom/action/claim_event_rewards_scan.py"
        ).read_text(encoding="utf-8")
        for action_name, class_name in (
            ("claim_event_rewards_scan", "ClaimEventRewardsScan"),
            ("claim_event_rewards_fail", "ClaimEventRewardsFail"),
        ):
            self.assertIn(f'custom_action("{action_name}")', scan_text)
            self.assertIn(class_name, init_text)
            self.assertIn(f'"{class_name}"', init_text)
        for suffix in ("NoReward", "Complete"):
            self.assertEqual(
                self.pipeline[PREFIX + suffix]["recognition"]["param"]["all_of"],
                [PREFIX + "CheckedAllDays"],
            )
        self.assertEqual(
            self.pipeline[PREFIX + "CheckedAllDays"]["recognition"]["param"]["all_of"],
            [PREFIX + "PageIdentity"]
            + [day_name(day, "Resolved") for day in range(1, 8)],
        )


class ClaimEventRewardsPipelineFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = load_pipeline()

    def simulate(self, states, **options):
        model = PipelineSimulation(self.pipeline, states, **options)
        model.run()
        return model

    def assert_failed_without_completion(self, model):
        self.assertFalse(model.success, model.trace)
        self.assertNotIn(PREFIX + "WorldConfirmed", model.trace)
        self.assertNotIn("$claim_event_rewards_complete", model.messages)
        self.assertNotIn("$claim_event_rewards_no_reward", model.messages)

    def test_all_2187_known_state_combinations(self):
        for states in product((AVAILABLE, CLAIMED, LOCKED), repeat=7):
            with self.subTest(states=states):
                model = self.simulate(states)
                expected = [
                    day for day, state in enumerate(states, 1) if state == AVAILABLE
                ]
                self.assertTrue(model.success, model.trace)
                self.assertIn(PREFIX + "Complete", model.trace)
                self.assertEqual(model.trace[-1], PREFIX + "WorldConfirmed")
                self.assertEqual(model.world_navigation_calls, 1)
                self.assertEqual(model.stage, "world")
                self.assertEqual(model.clicks, expected)
                self.assertEqual(model.dismissals, expected)
                self.assertEqual(
                    model.messages.count("$claim_event_rewards_claimed"), len(expected)
                )
                self.assertEqual(
                    model.messages.count("$claim_event_rewards_no_reward"),
                    int(not expected),
                )
                for day, state in enumerate(states, 1):
                    suffix = (
                        "ClaimConfirmed"
                        if state == AVAILABLE
                        else ("AlreadyClaimed" if state == CLAIMED else "Locked")
                    )
                    self.assertEqual(model.trace.count(day_name(day, suffix)), 1)
                    self.assertEqual(
                        model.trace.count(day_name(day, "Available")),
                        int(state == AVAILABLE),
                    )

    def test_actual_before_claim_state_drives_only_day2_input(self):
        model = self.simulate(
            [CLAIMED, AVAILABLE, LOCKED, LOCKED, LOCKED, LOCKED, LOCKED]
        )
        self.assertTrue(model.success)
        self.assertEqual(model.clicks, [2])
        self.assertEqual(model.dismissals, [2])
        self.assertLess(
            model.trace.index(day_name(2, "Available")),
            model.trace.index(PREFIX + "RewardPopup"),
        )
        self.assertLess(
            model.trace.index(PREFIX + "RewardPopup"),
            model.trace.index(day_name(2, "ClaimConfirmed")),
        )

    def test_each_unknown_day_fails_without_input_or_fake_no_reward(self):
        for day in range(1, 8):
            states = [CLAIMED] * 7
            states[day - 1] = None
            model = self.simulate(states)
            with self.subTest(day=day):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.clicks, [])

    def test_each_state_below_ocr_threshold_fails(self):
        for day in range(1, 8):
            for state in (AVAILABLE, CLAIMED, LOCKED):
                states = [CLAIMED] * 7
                states[day - 1] = state
                model = PipelineSimulation(self.pipeline, states)
                model.scores[day - 1] = 0.799
                model.run()
                with self.subTest(day=day, state=state):
                    self.assert_failed_without_completion(model)
                    self.assertEqual(model.clicks, [])

    def test_available_text_without_same_button_yellow_is_not_clicked(self):
        for day in range(1, 8):
            states = [CLAIMED] * 7
            states[day - 1] = AVAILABLE
            model = self.simulate(states, yellow={day: False})
            with self.subTest(day=day):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.clicks, [])

    def test_substring_and_unknown_statuses_have_no_success_path(self):
        for text in (
            "不可领取",
            "领取中",
            "未解",
            "未领取",
            "已领取中",
            "状态：已领取",
        ):
            model = self.simulate(
                [CLAIMED, text, LOCKED, LOCKED, LOCKED, LOCKED, LOCKED]
            )
            with self.subTest(text=text):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.clicks, [])

    def test_title_hint_or_layout_miss_blocks_all_reward_inputs(self):
        for component in ("title", "hint", "layout"):
            model = self.simulate([AVAILABLE] * 7, **{component: False})
            with self.subTest(component=component):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.clicks, [])

    def test_each_missing_reward_slot_blocks_all_reward_inputs(self):
        for day in range(1, 8):
            model = self.simulate([AVAILABLE] * 7, missing_slot=day)
            with self.subTest(day=day):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.clicks, [])

    def test_missing_visible_tab_fails_without_claiming_event_absence(self):
        model = self.simulate([CLAIMED] * 7, stage="event_menu", tab_visible=False)
        self.assert_failed_without_completion(model)
        self.assertEqual(model.failure_node, PREFIX + "Failed")
        self.assertNotIn("$claim_event_rewards_no_event", model.messages)
        self.assertEqual(model.clicks, [])

    def test_sidebar_click_is_once_and_right_page_must_be_verified(self):
        model = self.simulate(
            [AVAILABLE] * 7, stage="event_menu", tab_result="event_menu"
        )
        self.assert_failed_without_completion(model)
        self.assertEqual(model.trace.count(PREFIX + "EventTab"), 1)
        self.assertEqual(model.clicks, [])

    def test_single_public_navigation_from_world_then_verified_events_page(self):
        model = self.simulate([CLAIMED] * 7, stage="world")
        self.assertTrue(model.success, model.trace)
        self.assertEqual(model.navigation_calls, 1)
        self.assertIn("SceneAnyEnterEventsMenu", model.trace)
        self.assertLess(
            model.trace.index("SceneAnyEnterEventsMenu"),
            model.trace.index(PREFIX + "InMenu"),
        )
        self.assertNotIn(PREFIX + "Navigation", model.anchors)

    def test_failed_navigation_is_not_repeated_or_treated_as_no_reward(self):
        model = self.simulate([CLAIMED] * 7, stage="world", navigation_result="world")
        self.assert_failed_without_completion(model)
        self.assertEqual(model.navigation_calls, 1)
        self.assertEqual(model.trace.count(PREFIX + "NavigateToEvents"), 1)

    def test_navigation_action_failure_does_not_complete(self):
        model = self.simulate(
            [CLAIMED] * 7, stage="world", navigation_action_failed=True
        )
        self.assert_failed_without_completion(model)
        self.assertEqual(model.navigation_calls, 1)

    def test_each_missing_popup_component_prevents_dismissal_and_reclick(self):
        for suffix in ("RewardPopupTitle", "RewardPopupHint", "RewardPopupIcon"):
            model = self.simulate(
                [AVAILABLE] + [LOCKED] * 6, **{PREFIX + suffix: False}
            )
            with self.subTest(component=suffix):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.clicks, [1])
                self.assertEqual(model.dismissals, [])
                self.assertEqual(model.trace.count(day_name(1, "Available")), 1)

    def test_ordinary_close_label_is_not_a_reward_popup(self):
        model = self.simulate(
            [AVAILABLE] + [LOCKED] * 6,
            **{PREFIX + "RewardPopupHintText": "关闭"},
        )
        self.assert_failed_without_completion(model)
        self.assertEqual(model.clicks, [1])
        self.assertEqual(model.dismissals, [])

    def test_no_reward_popup_after_click_is_failure_not_direct_confirmation(self):
        model = self.simulate([AVAILABLE] + [LOCKED] * 6, show_popup=False)
        self.assert_failed_without_completion(model)
        self.assertEqual(model.clicks, [1])
        self.assertEqual(model.dismissals, [])

    def test_each_unconfirmed_post_claim_state_fails_without_reclick(self):
        for day in range(1, 8):
            for state in (AVAILABLE, LOCKED, None, "领取中"):
                states = [CLAIMED] * 7
                states[day - 1] = AVAILABLE
                model = self.simulate(states, post_claim={day: state})
                with self.subTest(day=day, after=state):
                    self.assert_failed_without_completion(model)
                    self.assertEqual(model.clicks, [day])
                    self.assertEqual(model.dismissals, [day])
                    self.assertNotIn(day_name(day, "ClaimConfirmed"), model.trace)
                    self.assertNotIn("$claim_event_rewards_claimed", model.messages)

    def test_low_confidence_after_popup_does_not_confirm_claim(self):
        model = self.simulate([AVAILABLE] + [LOCKED] * 6, post_claim_score=0.799)
        self.assert_failed_without_completion(model)
        self.assertEqual(model.clicks, [1])
        self.assertEqual(model.dismissals, [1])
        self.assertNotIn("$claim_event_rewards_claimed", model.messages)

    def test_another_claimed_cell_does_not_confirm_clicked_cell(self):
        model = self.simulate(
            [CLAIMED, AVAILABLE, LOCKED, LOCKED, LOCKED, LOCKED, LOCKED],
            post_claim={2: AVAILABLE},
            after_node={PREFIX + "RewardPopup": {3: CLAIMED}},
        )
        self.assert_failed_without_completion(model)
        self.assertEqual(model.clicks, [2])
        self.assertNotIn(day_name(2, "ClaimConfirmed"), model.trace)

    def test_popup_that_remains_is_not_dismissed_or_claimed_again(self):
        model = self.simulate([AVAILABLE] + [LOCKED] * 6, popup_remains=True)
        self.assert_failed_without_completion(model)
        self.assertEqual(model.clicks, [1])
        self.assertEqual(model.dismissals, [1])

    def test_input_or_dismissal_action_failure_is_propagated(self):
        for option in ("input_action_failed", "dismiss_action_failed"):
            model = self.simulate([AVAILABLE] + [LOCKED] * 6, **{option: True})
            with self.subTest(action=option):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.clicks, [1])
                self.assertEqual(model.failure_node, PREFIX + "Failed")

    def test_final_verification_rejects_earlier_cell_changed_to_available_or_unknown(
        self,
    ):
        for state in (AVAILABLE, None):
            model = self.simulate(
                [LOCKED] * 7,
                after_node={day_name(7, "Locked"): {1: state}},
            )
            with self.subTest(state=state):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.clicks, [])

    def test_partial_claim_then_unknown_later_cell_still_fails(self):
        model = self.simulate([AVAILABLE, None, LOCKED, LOCKED, LOCKED, LOCKED, LOCKED])
        self.assert_failed_without_completion(model)
        self.assertEqual(model.clicks, [1])
        self.assertEqual(model.messages.count("$claim_event_rewards_claimed"), 1)

    def test_already_on_verified_target_skips_tab_and_scanner(self):
        model = self.simulate([CLAIMED] * 7)
        self.assertTrue(model.success, model.trace)
        self.assertNotIn(PREFIX + "EventTab", model.trace)
        self.assertEqual(model.scan_calls, 0)
        self.assertEqual(model.world_navigation_calls, 1)
        self.assertEqual(model.stage, "world")

    def test_visible_target_on_other_event_page_skips_scanning(self):
        model = self.simulate([CLAIMED] * 7, stage="event_menu")
        self.assertTrue(model.success, model.trace)
        self.assertEqual(model.trace.count(PREFIX + "EventTab"), 1)
        self.assertEqual(model.scan_calls, 0)
        self.assertEqual(model.world_navigation_calls, 1)

    def test_scanner_found_target_selects_once_and_checks_page(self):
        model = self.simulate(
            [CLAIMED] * 7, stage="event_menu", tab_visible=False, scan_result="found"
        )
        self.assertTrue(model.success, model.trace)
        self.assertEqual(model.scan_calls, 1)
        self.assertEqual(model.trace.count(PREFIX + "EventTab"), 1)
        self.assertEqual(model.world_navigation_calls, 1)
        self.assertNotIn("$claim_event_rewards_no_event", model.messages)

    def test_only_proved_absence_callback_emits_no_event_and_returns_world(self):
        model = self.simulate(
            [None] * 7,
            stage="event_menu",
            tab_visible=False,
            scan_result="absent_proved",
        )
        self.assertTrue(model.success, model.trace)
        self.assertEqual(model.scan_calls, 1)
        self.assertEqual(model.clicks, [])
        self.assertEqual(model.messages.count("$claim_event_rewards_no_event"), 1)
        self.assertNotIn("$claim_event_rewards_no_reward", model.messages)
        self.assertNotIn("$claim_event_rewards_claimed", model.messages)
        self.assertEqual(model.world_navigation_calls, 1)
        self.assertEqual(model.stage, "world")
        self.assertEqual(model.messages[-1], "$claim_event_rewards_complete")
        self.assertNotIn(PREFIX + "PageReady", model.trace)
        self.assertIn(PREFIX + "WorldConfirmed", model.trace)

    def test_unproved_empty_or_failed_scan_never_reports_no_event(self):
        for result in ("empty", "failed", "unknown"):
            model = self.simulate(
                [None] * 7, stage="event_menu", tab_visible=False, scan_result=result
            )
            with self.subTest(result=result):
                self.assert_failed_without_completion(model)
                self.assertNotIn("$claim_event_rewards_no_event", model.messages)
                self.assertEqual(model.scan_calls, 1)
                self.assertEqual(model.world_navigation_calls, 0)
                self.assertEqual(model.failure_node, PREFIX + "Failed")

    def test_missing_or_low_confidence_header_prevents_scan_input(self):
        for option in ({"scan_header": False}, {"scan_header_score": 0.799}):
            model = self.simulate(
                [None] * 7, stage="event_menu", tab_visible=False, **option
            )
            with self.subTest(option=option):
                self.assert_failed_without_completion(model)
                self.assertEqual(model.scan_calls, 0)
                self.assertEqual(model.world_navigation_calls, 0)
                self.assertEqual(model.failure_node, PREFIX + "UnverifiedList")
                self.assertNotIn("$claim_event_rewards_no_event", model.messages)

    def test_scanner_selection_that_does_not_open_page_is_not_retried(self):
        model = self.simulate(
            [CLAIMED] * 7,
            stage="event_menu",
            tab_visible=False,
            scan_result="found",
            tab_result="event_menu",
        )
        self.assert_failed_without_completion(model)
        self.assertEqual(model.scan_calls, 1)
        self.assertEqual(model.trace.count(PREFIX + "EventTab"), 1)
        self.assertEqual(model.world_navigation_calls, 0)

    def test_scan_move_subtasks_are_guarded_and_only_one_input(self):
        for suffix in ("ScanUp", "ScanDown"):
            model = PipelineSimulation(self.pipeline, [None] * 7, stage="event_menu")
            self.assertTrue(model.run(PREFIX + suffix))
            self.assertEqual(model.trace, [PREFIX + suffix])
            self.assertEqual(model.swipes, [PREFIX + suffix])
            for option in (
                {"stage": "world"},
                {"scan_header": False},
                {"swipe_action_failed": True},
            ):
                settings = {"stage": "event_menu", **option}
                failed = PipelineSimulation(self.pipeline, [None] * 7, **settings)
                with self.subTest(node=suffix, option=option):
                    self.assertFalse(failed.run(PREFIX + suffix))
                    self.assertNotIn(PREFIX + "NoEvent", failed.trace)
                    self.assertNotIn(PREFIX + "WorldConfirmed", failed.trace)
                    self.assertLessEqual(len(failed.swipes), 1)

    def test_all_nonclaim_results_finish_only_after_world_confirmation(self):
        for states in product((CLAIMED, LOCKED), repeat=7):
            model = self.simulate(states)
            with self.subTest(states=states):
                self.assertTrue(model.success, model.trace)
                self.assertEqual(model.clicks, [])
                self.assertEqual(model.world_navigation_calls, 1)
                self.assertEqual(model.stage, "world")
                self.assertEqual(model.trace[-1], PREFIX + "WorldConfirmed")
                self.assertEqual(model.messages[-1], "$claim_event_rewards_complete")
                self.assertNotIn(PREFIX + "WorldNavigation", model.anchors)

    def test_failed_world_identity_or_return_is_not_retried_or_completed(self):
        for options in (
            {"return_world_result": "event_menu"},
            {"return_world_result": "unknown"},
            {"world_identity": False},
        ):
            model = self.simulate([CLAIMED] * 7, **options)
            with self.subTest(options=options):
                self.assertFalse(model.success, model.trace)
                self.assertEqual(model.world_navigation_calls, 1)
                self.assertEqual(model.trace.count(PREFIX + "NavigateToWorld"), 1)
                self.assertNotIn(PREFIX + "WorldConfirmed", model.trace)
                self.assertNotIn("$claim_event_rewards_complete", model.messages)
                self.assertNotIn(PREFIX + "WorldNavigation", model.anchors)

    def test_world_navigation_action_failure_cannot_emit_completion(self):
        model = self.simulate([CLAIMED] * 7, world_action_failed=True)
        self.assertFalse(model.success, model.trace)
        self.assertEqual(model.world_navigation_calls, 1)
        self.assertNotIn(PREFIX + "WorldConfirmed", model.trace)
        self.assertNotIn("$claim_event_rewards_complete", model.messages)

    def test_exit_phase_already_in_world_does_not_navigate_again(self):
        model = PipelineSimulation(self.pipeline, [None] * 7, stage="world")
        self.assertTrue(model.run(PREFIX + "ReturnWorld"))
        self.assertEqual(model.world_navigation_calls, 0)
        self.assertEqual(model.trace[-1], PREFIX + "WorldConfirmed")
        self.assertNotIn(PREFIX + "WorldNavigation", model.anchors)

    def test_unknown_exit_scene_does_not_have_direct_hit_success(self):
        model = PipelineSimulation(self.pipeline, [None] * 7, stage="unknown")
        self.assertFalse(model.run(PREFIX + "ReturnWorld"))
        self.assertEqual(model.world_navigation_calls, 0)
        self.assertNotIn("$claim_event_rewards_complete", model.messages)

    def test_verified_page_subflow_resets_outcome_without_navigation(self):
        model = PipelineSimulation(self.pipeline, [CLAIMED] * 7)
        model.anchors[PREFIX + "Outcome"] = PREFIX + "Complete"
        model.anchors[PREFIX + "ClaimConfirmation"] = day_name(2, "ClaimConfirmed")
        self.assertTrue(model.run(PREFIX + "PageReady"))
        self.assertEqual(model.clicks, [])
        self.assertEqual(model.navigation_calls, 0)
        self.assertEqual(model.messages.count("$claim_event_rewards_no_reward"), 1)
        self.assertNotIn(PREFIX + "ClaimConfirmation", model.anchors)

    def test_successive_runs_reset_transient_outcome_and_never_reclaim(self):
        model = PipelineSimulation(self.pipeline, [CLAIMED, AVAILABLE] + [LOCKED] * 5)
        self.assertTrue(model.run())
        self.assertEqual(model.clicks, [2])
        self.assertEqual(model.anchors[PREFIX + "Outcome"], PREFIX + "Complete")
        self.assertTrue(model.run())
        self.assertEqual(model.clicks, [])
        self.assertEqual(model.dismissals, [])
        self.assertEqual(model.messages.count("$claim_event_rewards_no_reward"), 1)
        self.assertNotIn(PREFIX + "ClaimConfirmation", model.anchors)


if __name__ == "__main__":
    unittest.main()
