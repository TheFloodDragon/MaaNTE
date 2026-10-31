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

    def test_lost_timer_counts_only_consecutive_judgeable_frames(self):
        t, _, clock = make()
        t.observe(snap(1.0, False))
        t.observe(snap(1.5, False))
        self.assertAlmostEqual(t.lost_for(), 0.5)
        t.observe(snap(1.6, None))  # 识别失败：未知不等于丢失，也不允许补计
        self.assertEqual(t.lost_for(), 0.0)
        t.observe(snap(1.7, True, ready=False))  # HUD 不可见
        t.observe(snap(1.8, False))
        self.assertEqual(t.lost_for(), 0.0)  # 恢复后的首帧不追补缺失时长
        t.observe(snap(2.0, False))
        self.assertAlmostEqual(t.lost_for(), 0.2)
        t.observe(snap(2.1, True))
        self.assertIsNone(t.lost_since)
        self.assertEqual(t.lost_for(), 0.0)

    def test_relock_once_per_loss_and_one_action_per_frame(self):
        t, driver, clock = make()
        t.engage()
        driver.calls.clear()
        presses = []
        for now in (0.0, 0.3, 0.61, 0.9, 1.22, 1.5, 1.83):
            clock.t = now
            t.observe(snap(now, False))
            before = len(driver.calls)
            t.maintain()
            t.maintain()  # 同一张图不会做第二个动作
            if len(driver.calls) > before:
                presses.append(now)
        self.assertEqual(presses, [0.61])  # 不周期性盲按中键（中键可能是开关）
        t.observe(snap(2.0, True))  # 重新看到敌人后，下一次丢失可以再锁一次
        for now in (2.1, 2.4, 2.7, 3.3):
            clock.t = now
            t.observe(snap(now, False))
            t.maintain()
        self.assertEqual(driver.calls, [("click", "middle")] * 2)

    def test_no_relock_when_lock_disabled(self):
        t, driver, clock = make(lock=False)
        t.engage()
        for i in range(30):
            clock.t = i * 0.2
            t.observe(snap(clock.t, False))
            t.maintain()
        self.assertEqual(driver.calls, [])

    def test_search_turns_are_limited_and_reset_when_enemy_returns(self):
        t, driver, clock = make(lock=False, search=True)
        t.engage()
        for i in range(80):
            clock.t = i * 0.2
            t.observe(snap(clock.t, False))
            t.maintain()
        self.assertEqual(
            driver.calls,
            [("turn", targeting.SEARCH_DEGREES)] * targeting.SEARCH_MAX_TURNS,
        )
        clock.t += 0.2
        t.observe(snap(clock.t, True))
        for _ in range(7):
            clock.t += 0.2
            t.observe(snap(clock.t, False))
            t.maintain()
        self.assertEqual(len(driver.calls), targeting.SEARCH_MAX_TURNS + 1)

    def test_turn_happens_before_relock(self):
        t, driver, clock = make(lock=True, search=True)
        t.engage()
        driver.calls.clear()
        for i in range(9):
            clock.t = i * 0.2
            t.observe(snap(clock.t, False))
            t.maintain()
        self.assertEqual(
            driver.calls[:2], [("turn", targeting.SEARCH_DEGREES), ("click", "middle")]
        )
        self.assertEqual(driver.calls.count(("click", "middle")), 1)

    def test_hold_ultimate_after_enemy_lost_or_unknown(self):
        t, _, clock = make(lock=False)
        t.observe(snap(0.0, False))
        t.observe(snap(targeting.HOLD_ULTIMATE_AFTER - 0.01, False))
        self.assertFalse(t.hold_ultimate())
        t.observe(snap(targeting.HOLD_ULTIMATE_AFTER + 0.01, False))
        self.assertTrue(t.hold_ultimate())
        t.observe(snap(1.2, True))
        self.assertFalse(t.hold_ultimate())
        t.observe(snap(1.4, None))
        self.assertTrue(t.hold_ultimate())  # 敌人状态未知时不放 Q


if __name__ == "__main__":
    unittest.main()
