"""感知函数测试：合成帧验证 HUD、黑屏、当前槽位判定与分辨率缩放。"""

import unittest

import numpy as np

from ._loader import submodule

p = submodule("perception")


def frame(value=60, width=1280, height=720):
    return np.full((height, width, 3), value, dtype=np.uint8)


def paint(img, roi, color, scale=1.0):
    x, y, w, h = (int(round(v * scale)) for v in roi)
    img[y : y + h, x : x + w] = color
    return img


def slot_roi(index):
    x, y, w, h = p.SLOT_ROI
    return (x, y + p.SLOT_SPACING * index, w, h)


class PerceptionTests(unittest.TestCase):
    def test_black_and_hud(self):
        self.assertTrue(p.is_black(frame(0)))
        self.assertFalse(p.is_black(frame(60)))
        self.assertFalse(p.in_hud(frame(60)))
        hud = paint(frame(60), (640, 670, 6, 6), (230, 230, 230))
        self.assertTrue(p.in_hud(hud))
        red = paint(frame(60), (640, 670, 6, 6), (0, 0, 255))  # 高饱和像素不算 HUD
        self.assertFalse(p.in_hud(red))

    def test_current_slot_each_index(self):
        for index in range(4):
            img = paint(frame(60), slot_roi(index), (255, 255, 255))
            self.assertEqual(p.current_slot(img), index + 1, index)

    def test_ambiguous_or_empty_returns_zero(self):
        self.assertEqual(p.current_slot(frame(60)), 0)
        img = paint(paint(frame(60), slot_roi(0), (255, 255, 255)), slot_roi(2), (255, 255, 255))
        self.assertEqual(p.current_slot(img), 0)

    def test_scaled_frame(self):
        img = paint(frame(60, 1920, 1080), slot_roi(2), (255, 255, 255), scale=1.5)
        img = paint(img, (640, 670, 6, 6), (230, 230, 230), scale=1.5)
        self.assertEqual(p.current_slot(img), 3)
        self.assertTrue(p.in_hud(img))

    def test_invalid_images(self):
        self.assertIsNone(p.as_bgr(None))
        self.assertIsNone(p.as_bgr(np.zeros((10, 10), dtype=np.uint8)))
        self.assertFalse(p.observe_frame(None, lambda image: True).valid)

    def test_observe_frame_skips_enemy_when_hud_hidden(self):
        calls = []

        def detect(image):
            calls.append(1)
            return True

        hidden = p.observe_frame(frame(60), detect)
        self.assertTrue(hidden.valid)
        self.assertFalse(hidden.in_hud)
        self.assertIsNone(hidden.enemy)
        self.assertEqual(calls, [])
        img = paint(paint(frame(60), (640, 670, 6, 6), (230, 230, 230)), slot_roi(1), (255, 255, 255))
        snap = p.observe_frame(img, detect)
        self.assertTrue(snap.ready)
        self.assertEqual((snap.slot, snap.enemy), (2, True))
        self.assertTrue(p.observe_frame(frame(0), detect).black)


if __name__ == "__main__":
    unittest.main()
