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
            "GiftCharacterSelectedOCR": text("已选择", [1158, 145, 44, 16]),
            "GiftQuotaOCR": text("3/3", [824, 557, 46, 23]),
            "GiftDetailTitleOCR": text("礼物详情", [580, 180, 75, 24]),
            "GiftDetailNameOCR": text("溶月茶具", [700, 202, 85, 25]),
            "GiftSelectedOCR": text("已选择", [696, 323, 48, 18]),
            "GiftInventoryOCR": text("库存:3", [760, 446, 74, 24]),
            "GiftQuantityOCR": text("赠送数量:1", [760, 504, 104, 23]),
            "GiftSendOCR": text("赠送", [815, 584, 55, 23]),
            "GiftListButton": template([1200, 640, 35, 35]),
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
}


def make_catalog():
    return SimpleNamespace(
        base=BASE, characters=copy.deepcopy(CHARACTERS), gifts=copy.deepcopy(GIFTS)
    )


class StrictOcrTests(unittest.TestCase):
    def test_quota_is_full_strict_and_never_clamped(self):
        for remaining in range(4):
            self.assertEqual(vision.parse_quota("%d/3" % remaining), (remaining, 3))
        self.assertEqual(vision.parse_quota(" ２ ／ ３ "), (2, 3))
        for invalid in (
            "13/3",
            "4/3",
            "-1/3",
            "03/3",
            "3/4",
            "3/30",
            "3/3次",
            "x3/3",
            "3.0/3",
            "∞",
            "",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(vision.parse_quota(invalid))

    def test_inventory_and_quantity_semantics(self):
        for raw, expected in (("0", 0), ("库存：3", 3), ("×2", 2), ("持有数量 12", 12)):
            self.assertEqual(vision.parse_inventory(raw), expected)
        for raw in ("", "∞", "-1", "1/3", "1.0", "1个", "000", "库存:"):
            self.assertIsNone(vision.parse_inventory(raw))
        self.assertEqual(vision.parse_quantity("赠送数量：１"), 1)
        self.assertEqual(vision.parse_quantity("数量:2"), 2)
        for raw in ("1", "库存:1", "数量:0", "数量:01", "数量:-1", "数量:1/3"):
            self.assertIsNone(vision.parse_quantity(raw))

    def test_full_name_only_nfkc_and_whitespace_normalization(self):
        hit = vision.TextHit(" 残 虹 ", 0.95, [680, 110, 60, 25])
        self.assertEqual(vision.unique_name([hit], CHARACTERS)[0], "char_a")
        for raw in ("残", "残虹·", "殘虹", "sakiri", "残虹xx"):
            self.assertIsNone(
                vision.unique_name([vision.TextHit(raw, 0.99, hit.box)], CHARACTERS)
            )

    def test_multiple_low_score_nonfinite_or_shared_names_are_rejected(self):
        box = [680, 110, 60, 25]
        for score in (0.899, float("nan"), float("inf")):
            self.assertIsNone(
                vision.unique_name([vision.TextHit("残虹", score, box)], CHARACTERS)
            )
        hits = [
            vision.TextHit("残虹", 0.99, box),
            vision.TextHit("薄荷", 0.1, [760, 110, 55, 25]),
        ]
        self.assertIsNone(vision.unique_name(hits, CHARACTERS))
        records = copy.deepcopy(CHARACTERS)
        records["char_b"]["names"]["zh_cn"] = "残虹"
        self.assertIsNone(vision.unique_name(hits[:1], records))

    def test_all_results_keep_conflicts_hidden_by_expected_or_threshold(self):
        result = text_detail(
            ("残虹", 0.99, [680, 110, 60, 25]), ("薄荷", 0.2, [760, 110, 60, 25])
        )
        self.assertEqual(len(result.filtered_results), 1)
        self.assertIsNone(vision.unique_name(vision.text_hits(result), CHARACTERS))
        raw = {
            "raw_detail": {
                "detail": {
                    "all": [{"text": "残虹", "score": 0.99, "box": [680, 110, 60, 25]}]
                }
            }
        }
        self.assertEqual(
            vision.unique_name(vision.text_hits(raw), CHARACTERS)[0], "char_a"
        )


class FrameEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.context = FakeContext()
        self.catalog = make_catalog()
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.reader = vision.GiftVision(self.context, self.catalog)
        self.image_boxes = {
            "char_a": [[1150, 102, 66, 66]],
            "char_b": [],
            "A": [[686, 270, 66, 66]],
            "B": [],
        }
        self.matcher = patch.object(
            self.reader,
            "_candidates",
            side_effect=lambda frame, identifier, kind, roi: self.image_boxes[
                identifier
            ],
        )
        self.matcher.start()
        self.addCleanup(self.matcher.stop)

    def test_independent_positive_evidence_can_form_complete_view(self):
        view = self.reader.inspect(self.frame)
        self.assertEqual(view.page, "gifts")
        self.assertEqual((view.character_id, view.gift_id), ("char_a", "A"))
        self.assertTrue(view.identity_verified)
        self.assertTrue(view.gift_verified)
        self.assertTrue(view.selected)
        self.assertEqual(
            (view.remaining, view.total, view.inventory, view.quantity), (3, 3, 3, 1)
        )
        self.assertEqual(view.send_box, [815, 584, 55, 23])
        self.assertEqual(view.errors, ())

    def test_portrait_candidate_without_selected_marker_never_verifies_identity(self):
        self.context.responses.pop("GiftCharacterSelectedOCR")
        view = self.reader.inspect(self.frame)
        self.assertEqual(view.character_id, "char_a")
        self.assertFalse(view.identity_verified)

    def test_wrong_selected_row_and_shared_portraits_never_verify_identity(self):
        self.context.responses["GiftCharacterSelectedOCR"] = text(
            "已选择", [1158, 245, 44, 16]
        )
        self.assertFalse(self.reader.inspect(self.frame).identity_verified)
        self.context.responses["GiftCharacterSelectedOCR"] = text(
            "已选择", [1158, 145, 44, 16]
        )
        self.image_boxes["char_b"] = self.image_boxes["char_a"]
        self.assertFalse(self.reader.inspect(self.frame).identity_verified)

    def test_multiple_or_low_confidence_names_never_verify_identity(self):
        for result in (
            text("残虹", [690, 117, 60, 28], 0.5),
            text_detail(
                ("残虹", 0.99, [690, 117, 60, 28]), ("薄荷", 0.2, [765, 117, 60, 28])
            ),
        ):
            self.context.responses["GiftCharacterNameOCR"] = result
            self.assertFalse(self.reader.inspect(self.frame).identity_verified)

    def test_shared_gift_image_alone_cannot_authorize_selection(self):
        self.image_boxes["B"] = self.image_boxes["A"]
        self.context.responses.pop("GiftDetailNameOCR")
        view = self.reader.inspect(self.frame)
        self.assertFalse(view.gift_verified)
        self.assertFalse(view.selected)
        self.assertIsNone(view.inventory)

    def test_wrong_image_or_wrong_selected_cell_does_not_verify_gift(self):
        self.image_boxes["A"] = []
        self.assertFalse(self.reader.inspect(self.frame).gift_verified)
        self.image_boxes["A"] = [[686, 270, 66, 66]]
        self.context.responses["GiftSelectedOCR"] = text("已选择", [850, 323, 48, 18])
        self.assertFalse(self.reader.inspect(self.frame).gift_verified)

    def test_two_physical_gift_candidates_remain_ambiguous(self):
        self.image_boxes["A"].append([800, 270, 66, 66])
        self.assertFalse(self.reader.inspect(self.frame).gift_verified)

    def test_missing_quantity_is_unknown_not_implicit_one(self):
        self.context.responses.pop("GiftQuantityOCR")
        view = self.reader.inspect(self.frame)
        self.assertIsNone(view.quantity)
        self.assertIn("quantity_unverified_no_single_item_layout_evidence", view.errors)
        self.context.responses["GiftQuantityOCR"] = text("数量:2", [760, 504, 104, 23])
        self.assertEqual(self.reader.inspect(self.frame).quantity, 2)

    def test_missing_inventory_exception_and_bad_counter_do_not_become_zero(self):
        self.context.responses.pop("GiftInventoryOCR")
        self.assertIsNone(self.reader.inspect(self.frame).inventory)
        self.context.responses["GiftQuotaOCR"] = text("13/3", [824, 557, 46, 23])
        self.assertIsNone(self.reader.inspect(self.frame).remaining)
        self.context.responses["GiftInventoryOCR"] = RuntimeError("OCR failed")
        view = self.reader.inspect(self.frame)
        self.assertEqual(view.page, "unknown")
        self.assertIsNone(view.inventory)
        self.assertTrue(view.errors)

    def test_infinite_template_and_exact_ocr_are_independent_blocking_channels(self):
        self.context.responses["GiftInfiniteOCR"] = text("∞", [720, 340, 18, 15])
        self.assertTrue(self.reader.inspect(self.frame).infinite)
        self.context.responses["GiftInfiniteOCR"] = text("oo", [720, 340, 18, 15])
        self.assertFalse(self.reader.inspect(self.frame).infinite)
        self.context.responses["GiftInfiniteMarker"] = template(
            [720, 340, 18, 15], [810, 340, 18, 15]
        )
        self.assertTrue(self.reader.inspect(self.frame).infinite)

    def test_unrelated_page_and_missing_bond_tab_expose_no_click_controls(self):
        self.context.responses.pop("GiftBondMarker")
        view = self.reader.inspect(self.frame)
        self.assertEqual(view.page, "unknown")
        self.assertEqual(view.controls, {})
        self.assertEqual(self.reader.gift_candidates(self.frame, "A"), [])
        self.context.responses["GiftBondMarker"] = template([24, 23, 31, 35])
        self.context.responses.pop("GiftTabOCR")
        self.assertEqual(self.reader.inspect(self.frame).controls, {})

    def test_unknown_confirmation_purchase_unlock_or_submit_blocks_underlying_bond(
        self,
    ):
        for word in ("确定", "确认", "购买", "解锁", "提交", "确认赠送"):
            with self.subTest(word=word):
                self.context.responses["GiftUnexpectedDialogOCR"] = text(
                    word, [590, 390, 70, 25], score=0.55
                )
                view = self.reader.inspect(self.frame)
                self.assertEqual(view.page, "unknown")
                self.assertIn("unexpected_dialog", view.errors)
                self.assertFalse(view.identity_verified)
                self.assertFalse(view.gift_verified)
                self.assertIsNone(view.send_box)
                self.assertEqual(view.controls, {})
        self.context.responses["GiftUnexpectedDialogOCR"] = text(
            "取消", [590, 390, 70, 25]
        )
        self.assertEqual(self.reader.inspect(self.frame).page, "gifts")

    def test_global_limit_is_explicit_and_invalid_frame_has_no_evidence(self):
        self.context.responses["GiftGlobalLimitOCR"] = text(
            "今日赠礼总次数已达上限", [400, 250, 400, 25]
        )
        view = self.reader.inspect(self.frame)
        self.assertTrue(view.global_limit)
        self.assertEqual(view.page, "global_limit")
        self.assertFalse(view.identity_verified)
        self.assertIsNone(view.send_box)
        view = self.reader.inspect(np.zeros((1080, 1920, 3), dtype=np.uint8))
        self.assertEqual(view.page, "unknown")
        self.assertTrue(view.errors)


class ScanEvidenceTests(FrameEvidenceTests):
    def setUp(self):
        super().setUp()
        self.context.responses["GiftCategoryOCR"] = text(
            "当前分类:全部礼物", [685, 246, 180, 25]
        )
        self.cells = [[680, 280, 76, 78], [770, 280, 76, 78]]
        self.track = [864, 275, 10, 240]
        self.thumb = [866, 277, 6, 90]
        self.rectangles = patch.object(
            vision, "_rectangles", return_value=self.cells + [self.track, self.thumb]
        )
        self.rectangles.start()
        self.addCleanup(self.rectangles.stop)

        def visible(override):
            roi = override["GiftVisibleNameOCR"]["recognition"]["param"]["roi"]
            return text("时令寿司船", [roi[0] + 5, roi[1] + 48, roi[2] - 10, 20])

        self.context.responses["GiftVisibleNameOCR"] = visible
        self.context.responses["GiftVisibleCountOCR"] = text(
            "当前显示2件礼物", [740, 518, 160, 24]
        )
        self.image_boxes["B"] = [[685, 285, 50, 40]]
        self.reader._candidates.side_effect = lambda frame, identifier, kind, roi: (
            [[roi[0] + 5, roi[1] + 5, 50, 40]]
            if identifier == "B" and roi in self.cells
            else self.image_boxes[identifier]
        )

    def test_explicit_scroll_track_thumb_and_all_names_prove_page_absence(self):
        info = self.reader.scan_info(self.frame, target_id="A")
        self.assertTrue(info.category_verified)
        self.assertTrue(info.start_verified)
        self.assertFalse(info.end_verified)
        self.assertTrue(info.absence_verified)
        self.assertEqual(info.scroll_box, [680, 280, 166, 78])
        self.assertEqual(info.visible_ids, ("B", "B"))
        self.assertTrue(info.progress_evidence)
        self.assertEqual(info.errors, ())

    def test_unchanged_frame_or_missing_thumb_never_implies_end(self):
        first = self.reader.scan_info(self.frame, target_id="A")
        second = self.reader.scan_info(self.frame, target_id="A")
        self.assertEqual(first.page_key, second.page_key)
        self.assertFalse(second.end_verified)
        self.rectangles.stop()
        with patch.object(
            vision, "_rectangles", return_value=self.cells + [self.track]
        ):
            info = self.reader.scan_info(self.frame, target_id="A")
        self.assertFalse(info.end_verified)
        self.assertFalse(info.single_page_verified)
        self.assertIsNone(info.scroll_box)
        self.assertIn("scrollbar_boundaries_unverified", info.errors)

    def test_partial_ocr_never_proves_target_absence(self):
        self.context.responses.pop("GiftVisibleNameOCR")
        info = self.reader.scan_info(self.frame, target_id="A")
        self.assertFalse(info.absence_verified)
        self.assertIn("not_all_visible_cells_positively_identified", info.errors)

    def test_unselected_all_tab_is_not_verified_category(self):
        self.context.responses["GiftCategoryOCR"] = text(
            "全部礼物", [685, 246, 180, 25]
        )
        info = self.reader.scan_info(self.frame, target_id="A")
        self.assertFalse(info.category_verified)
        self.assertFalse(info.absence_verified)
        self.assertIsNone(info.scroll_box)

    def test_target_on_page_disables_absence_and_unknown_target_never_gets_absence(
        self,
    ):
        self.assertFalse(
            self.reader.scan_info(self.frame, target_id="B").absence_verified
        )
        self.assertFalse(self.reader.scan_info(self.frame).absence_verified)

    def test_verified_empty_category_is_explicit_single_page(self):
        self.context.responses["GiftEmptyListOCR"] = text(
            "暂无礼物", [730, 350, 120, 30]
        )
        self.context.responses["GiftVisibleCountOCR"] = text(
            "当前显示0件礼物", [740, 518, 160, 24]
        )
        with patch.object(vision, "_rectangles", return_value=[]):
            info = self.reader.scan_info(self.frame, target_id="A")
        self.assertTrue(info.single_page_verified)
        self.assertTrue(info.start_verified)
        self.assertTrue(info.end_verified)
        self.assertTrue(info.absence_verified)
        self.assertIsNone(info.scroll_box)

    def test_explicit_complete_page_number_not_missing_scrollbar_proves_one_page(self):
        self.context.responses["GiftPaginationOCR"] = text(
            "第1页/共1页", [740, 550, 140, 25]
        )
        with patch.object(vision, "_rectangles", return_value=self.cells):
            info = self.reader.scan_info(self.frame, target_id="A")
        self.assertTrue(info.single_page_verified)
        self.assertTrue(info.start_verified)
        self.assertTrue(info.end_verified)
        self.assertTrue(info.absence_verified)

    def test_missing_or_mismatched_visible_count_does_not_certify_detected_subset(self):
        for count in (None, "当前显示3件礼物", "2"):
            with self.subTest(count=count):
                self.context.responses["GiftVisibleCountOCR"] = (
                    text(count, [740, 518, 160, 24]) if count is not None else None
                )
                info = self.reader.scan_info(self.frame, target_id="A")
                self.assertFalse(info.absence_verified)
                self.assertIn("visible_count_or_complete_grid_unverified", info.errors)

    def test_empty_text_conflicting_with_visible_cells_is_not_absent(self):
        self.context.responses["GiftEmptyListOCR"] = text(
            "暂无礼物", [730, 350, 120, 30]
        )
        info = self.reader.scan_info(self.frame, target_id="A")
        self.assertFalse(info.absence_verified)
        self.assertFalse(info.single_page_verified)
        self.assertIn("empty_list_conflicts_with_visible_cells", info.errors)

    def test_pagination_conflicting_with_scroll_thumb_rejects_boundary(self):
        self.context.responses["GiftPaginationOCR"] = text(
            "第1页/共1页", [740, 550, 140, 25]
        )
        info = self.reader.scan_info(self.frame, target_id="A")
        self.assertFalse(info.absence_verified)
        self.assertFalse(info.end_verified)
        self.assertFalse(info.single_page_verified)
        self.assertIsNone(info.scroll_box)
        self.assertIn("pagination_scrollbar_conflict", info.errors)

    def test_unknown_target_and_image_outside_card_cannot_prove_absence(self):
        self.assertFalse(
            self.reader.scan_info(self.frame, target_id="missing").absence_verified
        )
        self.reader._candidates.side_effect = lambda frame, identifier, kind, roi: [
            [680, 280, 50, 40]
        ]
        self.assertFalse(
            self.reader.scan_info(self.frame, target_id="A").absence_verified
        )


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

    def test_lossless_webp_keeps_candidate_pixels_alpha_crop_and_matching(self):
        from PIL import Image

        pixels = np.random.default_rng(23).integers(0, 256, (64, 64, 4), dtype=np.uint8)
        pixels[:, :, 3] = 0
        pixels[12:49, 7:47, 3] = 255
        with tempfile.TemporaryDirectory(
            prefix="gift-lossless-candidates-"
        ) as temporary:
            base = Path(temporary)
            folder = base / "image/Gift/Gifts"
            folder.mkdir(parents=True)
            picture = Image.fromarray(pixels)
            picture.save(folder / "same.png", format="PNG")
            picture.save(
                folder / "same.webp",
                format="WEBP",
                lossless=True,
                quality=100,
                method=4,
                exact=True,
            )
            records = {
                kind: {"image": {"path": "Gift/Gifts/same." + kind}}
                for kind in ("png", "webp")
            }
            catalog = SimpleNamespace(base=base, gifts=records, characters={})
            reader = vision.GiftVision(FakeContext(), catalog)
            png = reader._image(records["png"])
            webp = reader._image(records["webp"])
            self.assertEqual(png.shape, (37, 40, 4))
            np.testing.assert_array_equal(png, webp)
            frame = np.full((720, 1280, 3), 96, dtype=np.uint8)
            frame[302:339, 713:753] = png[:, :, :3]
            self.assertEqual(reader.gift_candidates(frame, "png"), [[713, 302, 40, 37]])
            self.assertEqual(
                reader.gift_candidates(frame, "webp"), [[713, 302, 40, 37]]
            )

    def test_real_rectangle_detector_requires_track_and_thumb(self):
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        cells = [[680, 280, 76, 78], [770, 280, 76, 78]]
        for x, y, w, h in cells + [[864, 270, 12, 240], [867, 273, 6, 90]]:
            cv2.rectangle(frame, (x, y), (x + w - 1, y + h - 1), (220, 220, 220), 1)
        rectangles = vision._rectangles(frame, vision.GIFT_ROI)
        self.assertTrue(any(box[3] >= 235 and box[2] < 20 for box in rectangles))
        self.assertIsNotNone(vision._scrollbar(rectangles, cells))
        self.assertIsNone(vision._scrollbar([], cells))

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

    def test_native_bundle_accepts_webp_candidates_with_unchanged_png_templates(self):
        from maa.resource import Resource

        candidates = [
            json.loads((BASE / "data" / (kind + ".json")).read_text(encoding="utf-8"))[
                kind
            ][0]["image"]["path"]
            for kind in ("gifts", "characters")
        ]
        self.assertTrue(all(Path(path).suffix == ".webp" for path in candidates))
        with tempfile.TemporaryDirectory(prefix="gift-webp-resource-") as temporary:
            bundle = Path(temporary)
            for relative in candidates + ["Gift/UI/BondPanel.png"]:
                target = bundle / "image" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((BASE / "image" / relative).read_bytes())
            pipeline = bundle / "pipeline"
            pipeline.mkdir()
            (pipeline / "probe.json").write_text(
                json.dumps(
                    {
                        "GiftFormatProbe": {
                            "recognition": {
                                "type": "TemplateMatch",
                                "param": {"template": "Gift/UI/BondPanel.png"},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            resource = Resource()
            try:
                self.assertTrue(resource.post_bundle(bundle).wait().succeeded)
            finally:
                del resource

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
