"""输入驱动测试：组合键的按下/松开顺序、冲刺与带方向闪避的时序、视角转动的分段与降级。"""

import unittest

from ._fakes import FakeClock, FakeWorld
from ._loader import submodule

inputs = submodule("inputs")
keys = submodule("keys")
model = submodule("model")

VK_W, VK_A, VK_S, VK_D, VK_SHIFT = 0x57, 0x41, 0x53, 0x44, 0xA0


def make_driver(**world_kwargs):
    clock = FakeClock()
    world = FakeWorld(clock, **world_kwargs)
    return inputs.InputDriver(world, clock.sleep), world, clock


def key_events(world):
    return [(round(t, 6), kind, vk) for t, kind, vk in world.events if kind in ("down", "up")]


class KeyComboTests(unittest.TestCase):
    def test_diagonal_walk_releases_in_reverse_order(self):
        driver, world, clock = make_driver()
        driver.hold_keys(("w", "a"), 0.3)
        self.assertEqual(key_events(world), [(0.0, "down", VK_W), (0.0, "down", VK_A), (0.3, "up", VK_A), (0.3, "up", VK_W)])
        self.assertFalse(driver.holding)

    def test_sprint_taps_shift_while_holding_forward(self):
        driver, world, clock = make_driver()
        driver.sprint(0.8)
        events = key_events(world)
        self.assertEqual([(kind, vk) for _, kind, vk in events], [("down", VK_W), ("down", VK_SHIFT), ("up", VK_SHIFT), ("up", VK_W)])
        self.assertAlmostEqual(events[1][0], keys.SPRINT_LEAD)
        self.assertAlmostEqual(events[-1][0], 0.8)

    def test_directional_dodge_holds_direction_around_shift(self):
        driver, world, _ = make_driver()
        driver.dodge("s")
        events = key_events(world)
        self.assertEqual([(kind, vk) for _, kind, vk in events], [("down", VK_S), ("down", VK_SHIFT), ("up", VK_SHIFT), ("up", VK_S)])
        self.assertAlmostEqual(events[1][0], keys.DODGE_LEAD)

    def test_plain_dodge_only_taps_shift(self):
        driver, world, _ = make_driver()
        driver.dodge("")
        self.assertEqual([(kind, vk) for _, kind, vk in key_events(world)], [("down", VK_SHIFT), ("up", VK_SHIFT)])

    def test_interrupted_hold_still_releases(self):
        clock = FakeClock()
        world = FakeWorld(clock)

        def stopping_sleep(seconds):
            clock.sleep(seconds)
            if clock.t >= 0.2:
                raise model.CombatStopped()

        driver = inputs.InputDriver(world, stopping_sleep)
        with self.assertRaises(model.CombatStopped):
            driver.hold_keys(("w", "d"), 5.0)
        self.assertEqual([kind for _, kind, _ in key_events(world)], ["down", "down", "up", "up"])
        self.assertFalse(driver.holding)


class TurnTests(unittest.TestCase):
    def test_turn_is_split_into_small_relative_moves(self):
        driver, world, _ = make_driver()
        self.assertTrue(driver.turn(90))
        moves = [dx for _, kind, dx in world.events if kind == "move"]
        self.assertEqual(sum(moves), int(90 * keys.TURN_PIXELS_PER_DEGREE))
        self.assertTrue(all(0 < dx <= keys.TURN_STEP_PIXELS for dx in moves), moves)
        world.events.clear()
        self.assertTrue(driver.turn(-45))
        self.assertEqual(sum(dx for _, kind, dx in world.events if kind == "move"), int(-45 * keys.TURN_PIXELS_PER_DEGREE))
        world.events.clear()
        self.assertTrue(driver.turn(0))
        self.assertEqual(world.events, [])

    def test_unsupported_controller_disables_turning_once(self):
        class NoRelativeMove(FakeWorld):
            calls = 0

            def post_relative_move(self, dx, dy):
                NoRelativeMove.calls += 1
                raise RuntimeError("relative move not supported")

        clock = FakeClock()
        driver = inputs.InputDriver(NoRelativeMove(clock), clock.sleep)
        self.assertFalse(driver.turn(90))
        self.assertFalse(driver.turn(90))
        self.assertEqual(NoRelativeMove.calls, 1)  # 第一次失败后不再尝试

    def test_controller_without_relative_move(self):
        driver = inputs.InputDriver(object(), lambda seconds: None)
        self.assertFalse(driver.turn(30))


if __name__ == "__main__":
    unittest.main()
