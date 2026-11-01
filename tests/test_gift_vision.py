"""赠礼视觉契约及原生小模板测试；合成画面不代表实机 OCR/消耗验收。"""

import copy
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "assets/resource/base"


def load_vision():
    modules = {name: ModuleType(name) for name in ("utils", "utils.logger")}
    modules["utils.logger"].logger = logging.getLogger("gift-vision-test")
    spec = importlib.util.spec_from_file_location(
        "_gift_vision_test_module", ROOT / "agent/custom/action/Gift/vision.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


vision = load_vision()


def jsonc(path):
    return json.loads(
        re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.M)
    )


def text_detail(*rows):
    all_results = [
        SimpleNamespace(text=text, score=score, box=box) for text, score, box in rows
    ]
    return SimpleNamespace(
        hit=bool(all_results),
        all_results=all_results,
        filtered_results=[row for row in all_results if row.score >= 0.9],
    )


def text(text, box, score=0.99):
    return text_detail((text, score, box))


def template(*boxes):
    return SimpleNamespace(
        hit=bool(boxes),
        all_results=[],
        filtered_results=[SimpleNamespace(score=0.99, box=box) for box in boxes],
    )


class FakeContext:
    def __init__(self):
        self.tasker = SimpleNamespace(stopping=False)
        self.calls = []
        self.responses = {
            "GiftBondMarker": template([24, 23, 31, 35]),
            "GiftTabOCR": text("赠礼", [994, 78, 64, 24]),
            "GiftCharacterNameOCR": text("残虹", [690, 117, 60, 28]),
            "GiftQuotaOCR": text("赠送3/3", [790, 556, 90, 24]),
            "GiftCurrentSelectionOCR": text("溶月茶具", [700, 202, 85, 25]),
        }

    def run_recognition(self, name, frame, pipeline_override=None):
        self.calls.append((name, pipeline_override))
        result = self.responses.get(name)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(pipeline_override)
        return result


CHARACTERS = {
    "char_a": {
        "names": {"zh_cn": "残虹", "en_us": "Sakiri"},
        "image": {"path": "Gift/UI/BondPanel.png"},
    },
    "char_b": {
        "names": {"zh_cn": "薄荷"},
        "image": {"path": "Gift/UI/InfiniteGift.png"},
    },
}
GIFTS = {
    "A": {"names": {"zh_cn": "溶月茶具"}, "image": {"path": "Gift/UI/BondPanel.png"}},
    "B": {
        "names": {"zh_cn": "时令寿司船"},
        "image": {"path": "Gift/UI/InfiniteGift.png"},
    },
    "N": {"names": {"zh_cn": "一封手写信"}, "image": None},
}


def make_catalog():
    return SimpleNamespace(
        base=BASE, characters=copy.deepcopy(CHARACTERS), gifts=copy.deepcopy(GIFTS)
    )


class StrictOcrTests(unittest.TestCase):
    def test_quota_accepts_merged_or_bare_counter_and_never_clamps(self):
        for remaining in range(4):
            self.assertEqual(vision.parse_quota("赠送%d/3" % remaining), (remaining, 3))
            self.assertEqual(vision.parse_quota("%d/3" % remaining), (remaining, 3))
        self.assertEqual(vision.parse_quota(" 赠送 ２ ／ ３ "), (2, 3))
        self.assertEqual(vision.parse_quota("贈送1/3"), (1, 3))
        for invalid in (
            "13/3",
            "赠送13/3",
            "4/3",
            "-1/3",
            "03/3",
            "3/4",
            "3/30",
            "3/3次",
            "赠礼3/3",
            "x3/3",
            "3.0/3",
            "赠送",
            "∞",
            "",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(vision.parse_quota(invalid))

    def test_counter_box_is_the_merged_or_combined_send_button(self):
        merged = [vision.TextHit("赠送2/3", 0.95, [790, 556, 90, 24])]
        self.assertEqual(vision.quota_hit(merged), ((2, 3), [790, 556, 90, 24]))
        split = [
            vision.TextHit("赠送", 0.95, [780, 556, 44, 24]),
            vision.TextHit("1/3", 0.97, [830, 556, 40, 24]),
        ]
        self.assertEqual(vision.quota_hit(split), ((1, 3), [780, 556, 90, 24]))
        for hits in (
            [vision.TextHit("赠送", 0.95, [780, 556, 44, 24])],
            [vision.TextHit("2/3", 0.5, [830, 556, 40, 24])],
            [
                vision.TextHit("2/3", 0.95, [830, 556, 40, 24]),
                vision.TextHit("1/3", 0.95, [870, 556, 40, 24]),
            ],
        ):
            with self.subTest(hits=hits):
                self.assertIsNone(vision.quota_hit(hits))


class NameTests(unittest.TestCase):
    def test_full_name_only_nfkc_and_whitespace_normalization(self):
        hit = vision.TextHit(" 残 虹 ", 0.95, [680, 110, 60, 25])
        self.assertEqual(vision.unique_name([hit], CHARACTERS)[0], "char_a")
        for raw in ("残", "残虹·", "殘虹", "sakiri", "残虹xx"):
            self.assertIsNone(
                vision.unique_name([vision.TextHit(raw, 0.99, hit.box)], CHARACTERS)
            )

    def test_name_box_with_level_or_title_text_still_identifies_character(self):
        hits = [
            vision.TextHit("Lv.5", 0.99, [671, 110, 40, 22]),
            vision.TextHit("残虹", 0.97, [720, 110, 60, 28]),
            vision.TextHit("羁遇等级", 0.95, [800, 110, 80, 22]),
        ]
        self.assertEqual(vision.name_in_box(hits, CHARACTERS)[0], "char_a")

    def test_two_names_low_scores_or_shared_names_never_identify(self):
        box = [680, 110, 60, 25]
        both = [
            vision.TextHit("残虹", 0.99, box),
            vision.TextHit("薄荷", 0.99, [760, 110, 55, 25]),
        ]
        self.assertIsNone(vision.name_in_box(both, CHARACTERS))
        for score in (0.899, float("nan"), float("inf")):
            self.assertIsNone(
                vision.name_in_box([vision.TextHit("残虹", score, box)], CHARACTERS)
            )
        records = copy.deepcopy(CHARACTERS)
        records["char_b"]["names"]["zh_cn"] = "残虹"
        self.assertIsNone(
            vision.name_in_box([vision.TextHit("残虹", 0.99, box)], records)
        )
        # 同一名字被识别两次仍是同一个角色。
        twice = [vision.TextHit("残虹", 0.99, box), vision.TextHit("残虹", 0.95, box)]
        self.assertEqual(vision.name_in_box(twice, CHARACTERS)[0], "char_a")

    def test_all_results_keep_conflicts_hidden_by_expected_or_threshold(self):
        result = text_detail(
            ("残虹", 0.99, [680, 110, 60, 25]), ("薄荷", 0.95, [760, 110, 60, 25])
        )
        self.assertIsNone(vision.name_in_box(vision.text_hits(result), CHARACTERS))
        raw = {
            "raw_detail": {
                "detail": {
                    "all": [{"text": "残虹", "score": 0.99, "box": [680, 110, 60, 25]}]
                }
            }
        }
        self.assertEqual(
            vision.name_in_box(vision.text_hits(raw), CHARACTERS)[0], "char_a"
        )


class _FrameCase(unittest.TestCase):
    def setUp(self):
        self.context = FakeContext()
        self.catalog = make_catalog()
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.reader = vision.GiftVision(self.context, self.catalog)

    def view(self):
        return self.reader.inspect(self.frame)


class FrameEvidenceTests(_FrameCase):
    def test_merged_counter_name_box_and_current_selection_form_gift_view(self):
        view = self.view()
        self.assertEqual(view.page, "gifts")
        self.assertEqual((view.character_id, view.identity_verified), ("char_a", True))
        self.assertEqual(
            (view.gift_id, view.gift_verified, view.selected), ("A", True, True)
        )
        self.assertEqual((view.remaining, view.total), (3, 3))
        self.assertEqual(view.send_box, [790, 556, 90, 24])
        self.assertEqual(view.controls["gift_tab"], [994, 78, 64, 24])
        self.assertIsNone(view.affection)
        self.assertIsNone(view.limit_prompt)
        self.assertEqual(view.errors, ())

    def test_split_counter_text_is_still_the_gift_page(self):
        self.context.responses["GiftQuotaOCR"] = text_detail(
            ("赠送", 0.96, [780, 556, 44, 24]), ("2/3", 0.97, [830, 556, 40, 24])
        )
        view = self.view()
        self.assertEqual((view.page, view.remaining), ("gifts", 2))
        self.assertEqual(view.send_box, [780, 556, 90, 24])

    def test_bond_tab_without_counter_is_bond_page_without_send_button(self):
        self.context.responses["GiftQuotaOCR"] = None
        view = self.view()
        self.assertEqual(view.page, "bond")
        self.assertIsNone(view.send_box)
        self.assertEqual(view.controls, {"gift_tab": [994, 78, 64, 24]})
        self.assertEqual(view.character_id, "char_a")

    def test_missing_or_ambiguous_selection_never_verifies_a_gift(self):
        for detail in (
            None,
            text("溶月", [700, 202, 50, 25]),
            text("溶月茶具", [700, 202, 85, 25], score=0.5),
            text_detail(
                ("溶月茶具", 0.99, [700, 202, 85, 25]),
                ("时令寿司船", 0.99, [700, 240, 95, 25]),
            ),
        ):
            with self.subTest(detail=detail):
                self.context.responses["GiftCurrentSelectionOCR"] = detail
                view = self.view()
                self.assertIsNone(view.gift_id)
                self.assertFalse(view.gift_verified)
                self.assertIn("gift_selection_unverified", view.errors)

    def test_unreadable_name_box_never_verifies_identity(self):
        self.context.responses["GiftCharacterNameOCR"] = text_detail(
            ("残虹", 0.99, [690, 117, 60, 28]), ("薄荷", 0.99, [760, 117, 50, 28])
        )
        view = self.view()
        self.assertIsNone(view.character_id)
        self.assertFalse(view.identity_verified)
        self.assertIn("character_identity_unverified", view.errors)


class PromptEvidenceTests(_FrameCase):
    def test_affection_prompt_names_exactly_one_known_character(self):
        cases = (
            (text("【残虹】的好感度提升了50点。", [400, 90, 300, 30]), "char_a"),
            (text("【残虹】的好感度提升了120点", [400, 90, 300, 30]), "char_a"),
            (text("【陌生人】的好感度提升了50点。", [400, 90, 300, 30]), None),
            (text("【残虹】的好感度提升了0点。", [400, 90, 300, 30]), None),
            (text("【残虹】的好感度提升了50点。", [400, 90, 300, 30], 0.5), None),
            (
                text_detail(
                    ("【残虹】的好感度提升了50点。", 0.99, [400, 90, 300, 30]),
                    ("【薄荷】的好感度提升了50点。", 0.99, [400, 130, 300, 30]),
                ),
                None,
            ),
        )
        for detail, expected in cases:
            with self.subTest(detail=detail):
                self.context.responses["GiftAffectionOCR"] = detail
                self.assertEqual(self.view().affection, expected)

    def test_only_exact_refusal_texts_become_character_limit_prompt(self):
        for raw, expected in (
            ("对方今日无法接受更多的礼物", "character"),
            ("提升羁遇等级后才能继续赠送礼物", "character"),
            ("對方今日無法接受更多的禮物", "character"),
            ("无法接受", None),
            ("今日还能赠送3次礼物", None),
        ):
            with self.subTest(raw=raw):
                self.context.responses["GiftLimitPromptOCR"] = text(
                    raw, [420, 300, 360, 30]
                )
                self.assertEqual(self.view().limit_prompt, expected)

    def test_daily_total_limit_text_is_global_limit_without_controls(self):
        self.context.responses["GiftGlobalLimitOCR"] = text(
            "今日赠礼次数已用完", [500, 300, 260, 30]
        )
        view = self.view()
        self.assertEqual((view.page, view.global_limit), ("global_limit", True))
        self.assertIsNone(view.send_box)
        self.assertEqual(view.controls, {})

    def test_unknown_confirmation_or_purchase_blocks_even_at_low_score(self):
        for word in ("确定", "购买", "解锁", "确认赠送"):
            with self.subTest(word=word):
                self.context.responses["GiftUnexpectedDialogOCR"] = text(
                    word, [600, 450, 60, 30], score=0.3
                )
                view = self.view()
                self.assertEqual(view.page, "unknown")
                self.assertIsNone(view.send_box)
                self.assertIn("unexpected_dialog", view.errors)

    def test_unrelated_page_missing_tab_and_bad_frame_expose_no_controls(self):
        for node in ("GiftBondMarker", "GiftTabOCR"):
            with self.subTest(node=node):
                context = FakeContext()
                context.responses[node] = None
                view = vision.GiftVision(context, self.catalog).inspect(self.frame)
                self.assertEqual(view.page, "unknown")
                self.assertEqual(view.controls, {})
        bad = np.zeros((360, 640, 3), dtype=np.uint8)
        view = self.reader.inspect(bad)
        self.assertEqual(view.errors, ("unsupported_frame_size_or_format",))

    def test_infinite_badge_is_checked_only_inside_the_selected_cell_badge(self):
        cells = self.reader.grid_cells()
        expected = vision.badge_box(cells[0])

        def badge(override):
            roi = override["GiftInfiniteMarker"]["recognition"]["param"]["roi"]
            return template([680, 330, 20, 20]) if roi == expected else template()

        self.context.responses["GiftInfiniteMarker"] = badge
        self.assertTrue(self.reader.infinite_at(self.frame, cells[0]))
        self.assertFalse(self.reader.infinite_at(self.frame, cells[1]))
        self.assertEqual(self.reader.gift_candidates(self.frame, "N"), [])


class GeometryTests(unittest.TestCase):
    def test_grid_cells_badges_and_sidebar_slots_stay_inside_their_regions(self):
        x, y, w, h = vision.GRID_ROI
        cells = vision.grid_cells()
        self.assertEqual(len(cells), vision.GRID_COLUMNS * vision.GRID_ROWS)
        for cell in cells:
            self.assertTrue(x <= cell[0] and cell[0] + cell[2] <= x + w, cell)
            self.assertTrue(y <= cell[1] and cell[1] + cell[3] <= y + h, cell)
            badge = vision.badge_box(cell)
            self.assertTrue(0 <= badge[0] <= cell[0] and 0 <= badge[1] <= cell[1])
        self.assertEqual(
            vision.slot_box(vision.SIDEBAR_SLOTS_Y[0]), vision.SIDEBAR_COMPARE
        )
        for slot in vision.GiftVision.sidebar_slots():
            self.assertTrue(0 <= slot[1] and slot[1] + slot[3] <= 720, slot)

    def test_icon_match_maps_to_nearest_cell_or_none(self):
        cells = vision.grid_cells()
        for index in (0, 4, 7):
            x, y, w, h = cells[index]
            icon = [x + w // 2 - 20, y - 30, 40, 40]
            self.assertEqual(vision.cell_at(icon), index)
        self.assertIsNone(vision.cell_at([100, 100, 40, 40]))

    def test_region_signature_detects_change_and_blank_slots(self):
        rng = np.random.default_rng(7)
        frame = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        same = frame.copy()
        reader = vision.GiftVision
        self.assertTrue(
            reader.same(reader.grid_signature(frame), reader.grid_signature(same))
        )
        moved = np.roll(frame, 40, axis=0)
        self.assertFalse(
            reader.same(reader.grid_signature(frame), reader.grid_signature(moved))
        )
        self.assertFalse(reader.same(None, reader.grid_signature(frame)))
        blank = np.full((720, 1280, 3), 40, dtype=np.uint8)
        self.assertIsNone(reader.slot_signature(blank, 0))
        self.assertIsNone(reader.cell_signature(blank, 0))
        self.assertIsNotNone(reader.slot_signature(frame, 0))
        self.assertIsNotNone(reader.cell_signature(frame, 3))

    def test_every_recognition_node_used_by_vision_exists(self):
        source = (ROOT / "agent/custom/action/Gift/vision.py").read_text(
            encoding="utf-8"
        )
        used = set(re.findall(r'"(Gift[A-Za-z]+(?:OCR|Marker))"', source))
        nodes = jsonc(BASE / "pipeline/Gift/GiftRecognition.json")
        self.assertTrue(used)
        self.assertFalse(used - set(nodes), used - set(nodes))


class ImageAndResourceContractTests(unittest.TestCase):
    def test_runtime_candidate_matching_returns_actual_boxes_not_grid_constants(self):
        context = FakeContext()
        reader = vision.GiftVision(context, make_catalog())
        image = cv2.imread(str(BASE / "image/Gift/UI/BondPanel.png"))
        frame = np.full((720, 1280, 3), 96, dtype=np.uint8)
        h, w = image.shape[:2]
        frame[302 : 302 + h, 713 : 713 + w] = image
        candidates = reader.gift_candidates(frame, "A")
        self.assertEqual(candidates, [[713, 302, w, h]])
        frame[405 : 405 + h, 905 : 905 + w] = image
        self.assertEqual(len(reader.gift_candidates(frame, "A")), 2)
        blank = np.zeros_like(frame)
        self.assertEqual(reader.gift_candidates(blank, "A"), [])

    def test_ui_sources_are_small_exact_crops_and_do_not_claim_game_art_license(self):
        source = json.loads(
            (BASE / "image/Gift/UI/source.json").read_text(encoding="utf-8")
        )
        self.assertFalse(source["extraction"]["source_is_full_screenshot"])
        self.assertEqual(
            source["upstream"]["commit"], "3c478c233c35e102889e36aa89a0e46e2f8d2f80"
        )
        self.assertEqual(source["upstream"]["license"], "GPL-3.0")
        self.assertFalse(source["license_notice"]["permission_confirmed"])
        self.assertFalse(source["calibration"]["real_game_validation"])
        for record in source["templates"]:
            path = BASE / "image" / record["path"]
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), record["sha256"]
            )
            self.assertLess(path.stat().st_size, 4096)
            image = cv2.imread(str(path))
            self.assertEqual([image.shape[1], image.shape[0]], record["size"])

    def test_new_navigation_and_recognition_nodes_are_readable_and_zero_delay(self):
        nodes = jsonc(BASE / "pipeline/Gift/GiftRecognition.json")
        nodes.update(jsonc(BASE / "pipeline/SceneManager/SceneBond.json"))
        menu = jsonc(BASE / "pipeline/Interface/Scene/SceneMenu.json")
        status = jsonc(BASE / "pipeline/Interface/Scene/Status.json")
        nodes["SceneAnyEnterBondMenu"] = menu["SceneAnyEnterBondMenu"]
        nodes["InBondMenu"] = status["InBondMenu"]
        for name, node in nodes.items():
            with self.subTest(node=name):
                for delay in ("rate_limit", "pre_delay", "post_delay"):
                    self.assertEqual(node[delay], 0)
                self.assertNotIn("max_hit", node)
                self.assertNotIn("on_error", node)
                for template_path in (
                    [node.get("recognition", {}).get("param", {}).get("template")]
                    if node.get("recognition", {}).get("type") == "TemplateMatch"
                    else []
                ):
                    self.assertTrue((BASE / "image" / template_path).is_file())
        self.assertEqual(
            menu["SceneAnyEnterBondMenu"]["next"],
            [
                "__ScenePrivateAnyEnterBondMenuSuccess",
                "__ScenePrivateEscEnterBondMenu",
                "__ScenePrivateWorldEnterBondMenu",
            ],
        )
        self.assertEqual(
            nodes["__ScenePrivateEscEnterBondMenu"]["recognition"]["param"][
                "box_index"
            ],
            1,
        )
        self.assertEqual(
            nodes["__ScenePrivateEscEnterBondMenu"]["next"],
            ["__ScenePrivateAnyEnterBondMenuSuccess"],
        )


class NativeSmallTemplateTests(unittest.TestCase):
    """真实 Maa TemplateMatch 解析实际小图；从未运行真实游戏控制器。"""

    @classmethod
    def setUpClass(cls):
        import maa
        from maa.library import Library
        from maa.define import LoggingLevelEnum
        from maa.tasker import Tasker

        Library.open(
            Path(
                os.environ.get("MAAFW_BINARY_PATH", Path(maa.__file__).parent / "bin")
            ),
            agent_server=False,
        )
        # 同一类只配置一次，避免重复切换原生日志目录与异步日志线程竞争。
        Tasker.set_log_dir(ROOT / ".narrafork")
        Tasker.set_stdout_level(LoggingLevelEnum.Off)
        Tasker.set_save_draw(False)
        Tasker.set_save_on_error(False)
        Tasker.set_debug_mode(False)

    def test_native_template_positive_negative_and_roi_isolation(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker

        class Controller(CustomController):
            def __init__(self, frame):
                self.image = frame
                self.clicks = []
                super().__init__()

            def connect(self):
                return True

            def request_uuid(self):
                return "gift-vision-offline"

            def get_features(self):
                return 0

            def screencap(self):
                return self.image.copy()

            def click(self, x, y):
                self.clicks.append((x, y))
                return True

        class Probe(CustomAction):
            def __init__(self, frames):
                self.frames = frames
                self.results = []
                super().__init__()

            def run(self, context, argv):
                for node, frame in self.frames:
                    self.results.append(context.run_recognition(node, frame))
                return True

        templates = {
            "GiftBondMarker": ("BondPanel.png", [24, 23]),
            "GiftInfiniteMarker": ("InfiniteGift.png", [680, 340]),
        }
        frames = []
        expected = []
        for node, (filename, position) in templates.items():
            image = cv2.imread(str(BASE / "image/Gift/UI" / filename))
            h, w = image.shape[:2]
            frame = np.full((720, 1280, 3), 110, dtype=np.uint8)
            x, y = position
            frame[y : y + h, x : x + w] = image
            frames.append((node, frame))
            expected.append((True, [x, y, w, h]))
            frames.append((node, np.zeros_like(frame)))
            expected.append((False, None))
            outside = np.zeros_like(frame)
            outside[680 : 680 + h, 100 : 100 + w] = image
            frames.append((node, outside))
            expected.append((False, None))
        data = jsonc(BASE / "pipeline/Gift/GiftRecognition.json")
        data = {name: data[name] for name in templates}
        data["GiftVisionProbe"] = {
            "recognition": {"type": "DirectHit"},
            "action": {
                "type": "Custom",
                "param": {"custom_action": "gift_vision_probe"},
            },
            "rate_limit": 0,
            "pre_delay": 0,
            "post_delay": 0,
        }
        with tempfile.TemporaryDirectory(
            prefix="gift-vision-", dir=ROOT / ".narrafork"
        ) as directory:
            bundle = Path(directory)
            (bundle / "pipeline").mkdir()
            (bundle / "pipeline/gift.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            image_dir = bundle / "image/Gift/UI"
            image_dir.mkdir(parents=True)
            for filename, _ in templates.values():
                (image_dir / filename).write_bytes(
                    (BASE / "image/Gift/UI" / filename).read_bytes()
                )
            resource = Resource()
            probe = Probe(frames)
            self.assertTrue(resource.register_custom_action("gift_vision_probe", probe))
            self.assertTrue(resource.post_bundle(bundle).wait().succeeded)
            controller = Controller(frames[0][1])
            self.assertTrue(controller.post_connection().wait().succeeded)
            tasker = Tasker()
            self.assertTrue(tasker.bind(resource, controller))
            try:
                self.assertTrue(tasker.post_task("GiftVisionProbe").wait().succeeded)
                self.assertEqual(len(probe.results), len(expected))
                for result, (hit, box) in zip(probe.results, expected):
                    self.assertIsNotNone(result)
                    self.assertEqual(result.algorithm, "TemplateMatch")
                    self.assertEqual(result.hit, hit)
                    self.assertEqual(vision._box(result.box), box)
                self.assertEqual(controller.clicks, [])
            finally:
                tasker.post_stop().wait()

    def test_native_public_navigation_uses_confirmed_states_and_verifies_arrival(self):
        from maa.controller import CustomController
        from maa.custom_recognition import CustomRecognition
        from maa.resource import Resource
        from maa.tasker import Tasker

        class Controller(CustomController):
            def __init__(self, stage, arrive):
                self.stage = stage
                self.arrive = arrive
                self.inputs = []
                self.bond = cv2.imread(str(BASE / "image/Gift/UI/BondPanel.png"))
                super().__init__()

            def connect(self):
                return True

            def request_uuid(self):
                return "gift-bond-navigation-offline"

            def get_features(self):
                return 0

            def screencap(self):
                frame = np.zeros((720, 1280, 3), dtype=np.uint8)
                frame[0, 0, 0] = self.stage
                if self.stage == 3:
                    frame[23:58, 24:55] = self.bond
                return frame

            def click(self, x, y):
                self.inputs.append(("click", self.stage, x, y))
                self.stage = 3 if self.arrive else 0
                return True

            def click_key(self, key):
                self.inputs.append(("key", self.stage, key))
                self.stage = 1 if self.stage == 2 and key == 27 else 0
                return True

        class StateRecognition(CustomRecognition):
            def analyze(self, context, argv):
                stage = int(argv.image[0, 0, 0])
                expected = json.loads(argv.custom_recognition_param)["stage"]
                if stage == expected:
                    return self.AnalyzeResult(box=[700, 300, 80, 30], detail={})
                return None

        data = jsonc(BASE / "pipeline/SceneManager/SceneBond.json")
        data["SceneAnyEnterBondMenu"] = jsonc(
            BASE / "pipeline/Interface/Scene/SceneMenu.json"
        )["SceneAnyEnterBondMenu"]
        data["InBondMenu"] = jsonc(BASE / "pipeline/Interface/Scene/Status.json")[
            "InBondMenu"
        ]
        data["GiftBondMarker"] = jsonc(BASE / "pipeline/Gift/GiftRecognition.json")[
            "GiftBondMarker"
        ]
        # 状态文字用合成识别器隔离OCR，导航组合/按键/点击和到达核验由真实调度器运行。
        for name, stage in (
            ("InEscMenu", 1),
            ("InWorld", 2),
            ("GiftTabOCR", 3),
            ("__ScenePrivateBondEntryText", 1),
        ):
            data[name] = {
                "recognition": {
                    "type": "Custom",
                    "param": {
                        "custom_recognition": "gift_scene_test",
                        "custom_recognition_param": {"stage": stage},
                    },
                },
                "rate_limit": 0,
                "pre_delay": 0,
                "post_delay": 0,
            }
        for node in data.values():
            node["timeout"] = 200
        with tempfile.TemporaryDirectory(
            prefix="gift-bond-", dir=ROOT / ".narrafork"
        ) as directory:
            bundle = Path(directory)
            (bundle / "pipeline").mkdir()
            (bundle / "pipeline/navigation.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            image_dir = bundle / "image/Gift/UI"
            image_dir.mkdir(parents=True)
            (image_dir / "BondPanel.png").write_bytes(
                (BASE / "image/Gift/UI/BondPanel.png").read_bytes()
            )
            for stage, arrive, succeeds, input_count in (
                (1, True, True, 1),
                (2, True, True, 2),
                (3, True, True, 0),
                (0, True, False, 0),
                (1, False, False, 1),
            ):
                with self.subTest(stage=stage, arrive=arrive):
                    resource = Resource()
                    recognition = StateRecognition()
                    self.assertTrue(
                        resource.register_custom_recognition(
                            "gift_scene_test", recognition
                        )
                    )
                    self.assertTrue(resource.post_bundle(bundle).wait().succeeded)
                    controller = Controller(stage, arrive)
                    self.assertTrue(controller.post_connection().wait().succeeded)
                    tasker = Tasker()
                    self.assertTrue(tasker.bind(resource, controller))
                    try:
                        result = tasker.post_task("SceneAnyEnterBondMenu").wait()
                        self.assertEqual(result.succeeded, succeeds)
                        self.assertEqual(len(controller.inputs), input_count)
                        for operation in controller.inputs:
                            if operation[0] == "click":
                                self.assertEqual(operation[1], 1)
                                self.assertTrue(700 <= operation[2] < 780)
                                self.assertTrue(300 <= operation[3] < 330)
                            else:
                                self.assertEqual(operation, ("key", 2, 27))
                    finally:
                        tasker.post_stop().wait()


if __name__ == "__main__":
    unittest.main()
