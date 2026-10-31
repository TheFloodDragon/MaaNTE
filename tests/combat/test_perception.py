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
        img = paint(
            paint(frame(60), slot_roi(0), (255, 255, 255)), slot_roi(2), (255, 255, 255)
        )
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

        def detect(name, image):
            calls.append(1)
            return True

        hidden = p.observe_frame(frame(60), detect)
        self.assertTrue(hidden.valid)
        self.assertFalse(hidden.in_hud)
        self.assertIsNone(hidden.enemy)
        self.assertEqual(calls, [])
        img = paint(
            paint(frame(60), (640, 670, 6, 6), (230, 230, 230)),
            slot_roi(1),
            (255, 255, 255),
        )
        snap = p.observe_frame(img, detect)
        self.assertTrue(snap.ready)
        self.assertEqual((snap.slot, snap.enemy), (2, True))
        self.assertTrue(p.observe_frame(frame(0), detect).black)


RED = (40, 40, 235)  # BGR，落在 ok-NTE 敌人血条颜色范围内


def hud(img):
    return paint(img, (640, 670, 6, 6), (230, 230, 230))


class HealthBarShapeTests(unittest.TestCase):
    def test_normal_and_low_health_bars(self):
        full = paint(frame(60), (500, 200, 80, 4), RED)
        self.assertEqual(len(p.find_health_bars(full)), 1)
        low = paint(
            frame(60), (500, 200, 12, 4), RED
        )  # 残血约 15%，上游 50px 下限会漏掉
        self.assertEqual(len(p.find_health_bars(low)), 1)
        # 更短的残血条不满足长宽比，交给精确颜色节点（count=3）与等级/Boss 证据
        stub = paint(frame(60), (500, 200, 7, 4), RED)
        self.assertEqual(p.find_health_bars(stub), [])

    def test_rejects_red_scenery_and_excluded_regions(self):
        blob = paint(frame(60), (400, 200, 200, 120), RED)  # 大片红色场景
        self.assertEqual(p.find_health_bars(blob), [])
        dot = paint(frame(60), (500, 200, 4, 4), RED)  # 点状噪点
        self.assertEqual(p.find_health_bars(dot), [])
        own = paint(frame(60), (500, 670, 80, 4), RED)  # 底部自身血条
        self.assertEqual(p.find_health_bars(own), [])
        team = paint(frame(60), (1180, 200, 60, 4), RED)  # 右侧队伍栏
        self.assertEqual(p.find_health_bars(team), [])
        pale = paint(frame(60), (500, 200, 80, 4), (120, 120, 200))  # 低饱和
        self.assertEqual(p.find_health_bars(pale), [])

    def test_scaled_frame_keeps_720p_thresholds(self):
        img = paint(frame(60, 1920, 1080), (500, 200, 80, 4), RED, scale=1.5)
        boxes = p.find_health_bars(img)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0][2], 120)  # 截图坐标


class MultiEvidenceTests(unittest.TestCase):
    def observe(self, img, hits, signals=None):
        calls = []

        def recognize(name, image):
            calls.append(name)
            value = hits.get(name, False)
            if isinstance(value, Exception):
                raise value
            return value

        snap = p.observe_frame(
            img,
            recognize,
            signals
            or p.SignalConfig(
                enemy=("Bar",),
                level=("Lv",),
                boss=("Boss",),
                retreat_prompt=("Retreat",),
            ),
        )
        return snap, calls

    def test_health_bar_hit_skips_text_recognition(self):
        snap, calls = self.observe(hud(frame(60)), {"Bar": True})
        self.assertIs(snap.enemy, True)
        self.assertNotIn("Lv", calls)
        self.assertNotIn("Boss", calls)

    def test_level_or_boss_alone_is_enemy(self):
        for name in ("Lv", "Boss"):
            snap, _ = self.observe(hud(frame(60)), {name: True})
            self.assertIs(snap.enemy, True, name)
            self.assertIs(snap.health_bar, False, name)

    def test_negative_requires_every_configured_check(self):
        snap, _ = self.observe(hud(frame(60)), {})
        self.assertIs(snap.enemy, False)
        snap, _ = self.observe(hud(frame(60)), {"Lv": None})  # 识别没能启动
        self.assertIsNone(snap.enemy)
        snap, _ = self.observe(hud(frame(60)), {"Boss": RuntimeError("ocr")})
        self.assertIsNone(snap.enemy)

    def test_hidden_hud_still_checks_retreat_but_black_checks_nothing(self):
        snap, calls = self.observe(frame(60), {"Retreat": True})
        self.assertIs(snap.retreat_prompt, True)
        self.assertEqual(calls, ["Retreat"])  # 界面隐藏：不判断敌人与槽位
        snap, calls = self.observe(frame(0), {"Retreat": True})
        self.assertTrue(snap.black)
        self.assertIsNone(snap.retreat_prompt)
        self.assertEqual(calls, [])

    def test_each_node_runs_once_per_frame(self):
        signals = p.SignalConfig(
            enemy=("Bar",),
            level=("Shared",),
            boss=("Shared",),
            caller=(("active", ("Shared",)),),
        )
        _, calls = self.observe(hud(frame(60)), {}, signals)
        self.assertEqual(calls.count("Shared"), 1)


if __name__ == "__main__":
    unittest.main()
