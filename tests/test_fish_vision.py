import unittest

import cv2
import numpy as np

from fish_test_support import load_fish_module

vision = load_fish_module("fish_vision")


def color(hsv):
    return cv2.cvtColor(np.uint8([[hsv]]), cv2.COLOR_HSV2BGR)[0, 0]


def frame(cursor=601, width=80, dusk=False):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    image[45:55, 600 - width // 2 : 600 + width // 2] = color((82, 180, 210))
    image[43:57, cursor - 1 : cursor + 1] = color((28, 180, 180) if dusk else (27, 110, 255))
    return image


class FishVisionTests(unittest.TestCase):
    def test_primary_and_cursor_occlusion(self):
        green, cursor = vision.detect_control_boxes(frame())
        self.assertEqual(green, (560, 45, 80, 10))
        self.assertEqual(cursor, (600, 43, 2, 14))

    def test_primary_low_saturation_is_compatible(self):
        image = frame()
        image[43:57, 600:602] = color((27, 70, 255))
        self.assertEqual(vision.detect_control_boxes(image)[1], (600, 43, 2, 14))

    def test_dusk_cursor_is_shape_filtered(self):
        green, cursor = vision.detect_control_boxes(frame(dusk=True))
        self.assertEqual(green, (560, 45, 80, 10))
        self.assertEqual(cursor, (600, 43, 2, 14))
        image = frame(dusk=True)
        image[43:57, 600:602] = 0
        image[45:52, 700:730] = color((28, 180, 180))
        self.assertIsNone(vision.detect_control_boxes(image)[1])

    def test_scaled_and_bgra_output_stays_720p(self):
        for size in ((1280, 720), (1920, 1080), (2560, 1440)):
            for channels in (3, 4):
                with self.subTest(size=size, channels=channels):
                    image = cv2.resize(frame(), size, interpolation=cv2.INTER_NEAREST)
                    if channels == 4:
                        image = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
                    green, cursor = vision.detect_control_boxes(image)
                    self.assertIsNotNone(green)
                    self.assertIsNotNone(cursor)
                    self.assertLessEqual(abs(cursor[0] - 600), 1)
                    self.assertLessEqual(abs(green[0] - 560), 1)
                    self.assertLessEqual(abs(green[2] - 80), 2)

    def test_noise_does_not_merge_into_target(self):
        image = frame()
        image[44:48, 405:409] = color((82, 180, 210))
        image[44:48, 810:814] = color((82, 180, 210))
        image[52:55, 450:470] = color((27, 110, 255))
        green, cursor = vision.detect_control_boxes(image)
        self.assertEqual(green, (560, 45, 80, 10))
        self.assertEqual(cursor, (600, 43, 2, 14))

    def test_non_cursor_gap_is_not_bridged(self):
        image = frame(cursor=700)
        image[45:55, 600:604] = 0
        green, _ = vision.detect_control_boxes(image)
        self.assertLess(green[2], 80)

    def test_large_background_and_short_cursor_are_rejected(self):
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        image[43:57, 399:885] = color((82, 180, 210))
        image[43:47, 620:622] = color((27, 110, 255))
        self.assertEqual(vision.detect_control_boxes(image), (None, None))

    def test_last_position_breaks_candidate_tie(self):
        image = frame(cursor=700)
        image[43:57, 499:501] = color((27, 110, 255))
        self.assertEqual(vision.detect_control_boxes(image, 500)[1][0], 499)
        self.assertEqual(vision.detect_control_boxes(image, 700)[1][0], 699)

    def test_missing_target_does_not_hide_cursor(self):
        image = frame()
        image[45:55, 560:600] = 0
        image[45:55, 602:640] = 0
        green, cursor = vision.detect_control_boxes(image)
        self.assertIsNone(green)
        self.assertIsNotNone(cursor)

    def test_unsupported_frames_are_not_stretched(self):
        invalid = (
            None, [], np.zeros((0, 0, 3), dtype=np.uint8),
            np.zeros((720, 1280), dtype=np.uint8),
            np.zeros((720, 1280, 2), dtype=np.uint8),
            np.zeros((720, 1280, 3), dtype=np.float32),
            np.zeros((768, 1024, 3), dtype=np.uint8),
        )
        for image in invalid:
            self.assertIsNone(vision.normalize_control_image(image))
            self.assertEqual(vision.detect_control_boxes(image), (None, None))


if __name__ == "__main__":
    unittest.main()
