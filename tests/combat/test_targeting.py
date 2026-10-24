"""锁敌与索敌逻辑测试（假时钟 + 记录型驱动）。"""

import unittest

from ._loader import submodule

targeting = submodule("targeting")
model = submodule("model")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class RecordingDriver:
    def __init__(self):
        self.calls = []

    def click(self, button):
        self.calls.append(("click", button))
        return True

    def turn(self, degrees):
        self.calls.append(("turn", degrees))
        return True


def snap(at, enemy, ready=True):
    return model.Snapshot(
        at=at,
        valid=True,
        in_hud=ready,
        black=False,
        slot=1,
        enemy=enemy if ready else None,
    )


def make(lock=True, search=False):
    clock, driver = Clock(), RecordingDriver()
    return targeting.Targeting(driver, clock, lock=lock, search=search), driver, clock


class TargetingTests(unittest.TestCase):
    def test_engage_locks_only_when_enabled(self):
        t, driver, _ = make(lock=True)
        t.engage()
        self.assertEqual(driver.calls, [("click", "middle")])
        t, driver, _ = make(lock=False)
        t.engage()
        self.assertEqual(driver.calls, [])

    def test_lost_timer_ignores_hidden_and_unknown_frames(self):
        t, _, clock = make()
        t.observe(snap(1.0, False))
        clock.t = 1.5
        self.assertAlmostEqual(t.lost_for(), 0.5)
        t.observe(snap(1.6, None))  # 识别失败：不改变状态
        t.observe(snap(1.7, True, ready=False))  # HUD 不可见：不改变状态
        self.assertAlmostEqual(t.lost_since, 1.0)
        t.observe(snap(1.8, True))
        self.assertIsNone(t.lost_since)
        self.assertEqual(t.lost_for(), 0.0)

    def test_relock_waits_then_repeats_at_interval(self):
        t, driver, clock = make()
        t.engage()  # t=0 时锁定一次
        driver.calls.clear()
        t.observe(snap(0.0, False))
        presses = []
        for now in (0.3, 0.61, 0.9, 1.22, 1.5, 1.83):
            clock.t = now
            before = len(driver.calls)
            t.maintain()
            if len(driver.calls) > before:
                presses.append(now)
        self.assertEqual(presses, [0.61, 1.22, 1.83])
        self.assertTrue(all(call == ("click", "middle") for call in driver.calls))

    def test_no_relock_when_lock_disabled(self):
        t, driver, clock = make(lock=False)
        t.engage()
        t.observe(snap(0.0, False))
        for i in range(1, 30):
            clock.t = i * 0.2
            t.maintain()
        self.assertEqual(driver.calls, [])

    def test_search_turns_are_limited_and_reset_when_enemy_returns(self):
        t, driver, clock = make(lock=False, search=True)
        t.engage()
        t.observe(snap(0.0, False))
        for i in range(40):
            clock.t = 1.0 + i * 0.7
            t.maintain()
        self.assertEqual(
            driver.calls,
            [("turn", targeting.SEARCH_DEGREES)] * targeting.SEARCH_MAX_TURNS,
        )
        t.observe(snap(clock.t, True))
        t.observe(snap(clock.t + 0.1, False))
        clock.t += 1.2
        t.maintain()
        self.assertEqual(len(driver.calls), targeting.SEARCH_MAX_TURNS + 1)

    def test_turn_happens_before_relock(self):
        t, driver, clock = make(lock=True, search=True)
        t.engage()
        driver.calls.clear()
        t.observe(snap(0.0, False))
        clock.t = 1.0
        t.maintain()
        self.assertEqual(
            driver.calls, [("turn", targeting.SEARCH_DEGREES), ("click", "middle")]
        )

    def test_hold_ultimate_after_enemy_lost(self):
        t, _, clock = make(lock=False)
        t.observe(snap(0.0, False))
        clock.t = targeting.HOLD_ULTIMATE_AFTER - 0.01
        self.assertFalse(t.hold_ultimate())
        clock.t = targeting.HOLD_ULTIMATE_AFTER + 0.01
        self.assertTrue(t.hold_ultimate())
        t.observe(snap(clock.t, True))
        self.assertFalse(t.hold_ultimate())


if __name__ == "__main__":
    unittest.main()
