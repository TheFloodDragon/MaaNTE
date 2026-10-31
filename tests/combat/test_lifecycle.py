"""战斗生命周期验收：多证据判定、终局信号、撤离暂停、失明期间不输入、所有退出路径松键。"""

import unittest

from ._fakes import make_engine
from ._loader import submodule

runtime = submodule("runtime")
model = submodule("model")

STAY = {
    "version": 1,
    "combos": {"default": "l_click(0.5)"},
    "rotation": {"mode": "stay"},
}
VK_W = 0x57


def clicks_between(world, start, end):
    return [t for t in world.times("click", 0) if start <= t < end]


class EvidenceTests(unittest.TestCase):
    def test_level_or_boss_keeps_fight_without_health_bar(self):
        for field in ("enemy_level", "boss", "active"):
            engine, _, _, _ = make_engine(
                STAY,
                {"max_duration": 15.0},
                enemy=lambda t: t < 1.0,
                extra=lambda t, f=field: {f: True} if t >= 1.0 else {},
            )
            result = engine.run()
            self.assertEqual(result.reason, "timeout", field)  # 从不 target_lost
            self.assertFalse(result.success)

    def test_enemy_reappearing_resets_confirmation(self):
        engine, _, _, _ = make_engine(
            STAY, {"max_duration": 60.0}, enemy=lambda t: t < 2.0 or 4.0 <= t < 6.0
        )
        result = engine.run()
        self.assertEqual(result.reason, "target_lost")
        self.assertGreater(result.duration, 8.5)  # 从 6 秒重新计时，而不是从 2 秒

    def test_unknown_and_black_frames_do_not_count_toward_target_lost(self):
        for extra in (
            lambda t: {"enemy": None} if 2.0 <= t < 7.0 else {},
            lambda t: {"black": True} if 2.0 <= t < 7.0 else {},
            lambda t: {"valid": False} if 2.0 <= t < 7.0 else {},
        ):
            engine, world, _, _ = make_engine(
                STAY, {"max_duration": 60.0}, enemy=lambda t: t < 1.0, extra=extra
            )
            result = engine.run()
            self.assertEqual(result.reason, "target_lost")
            self.assertGreater(result.duration, 9.0)  # 5 秒未知期不计入
            self.assertEqual(clicks_between(world, 2.3, 6.9), [])  # 期间不输入

    def test_repeated_cached_frame_is_counted_once(self):
        monitor = runtime.Monitor(0.0, 0.0, 3.0, 10.0)
        negative = model.Snapshot(
            at=1.0, valid=True, in_hud=True, black=False, enemy=False
        )
        monitor.update(negative)
        monitor.update(model.Snapshot(**{**negative.__dict__, "at": 2.0}))
        before = monitor.clear_accum
        for _ in range(5):
            monitor.update(model.Snapshot(**{**negative.__dict__, "at": 2.0}))
        self.assertEqual(monitor.clear_accum, before)

    def test_long_observation_failure_ends_without_inputs(self):
        engine, world, _, _ = make_engine(
            STAY,
            {"max_duration": 60.0},
            extra=lambda t: {"valid": False} if t >= 2.0 else {},
        )
        result = engine.run()
        self.assertEqual(result.reason, "observation_lost")
        self.assertFalse(result.success)
        self.assertEqual(clicks_between(world, 2.3, 99.0), [])


class TerminalSignalTests(unittest.TestCase):
    def run_with(self, extra, **kwargs):
        engine, world, _, _ = make_engine(
            STAY, {"max_duration": 60.0}, extra=extra, **kwargs
        )
        return engine.run(), world

    def test_positive_terminal_signals_map_to_reasons(self):
        for signal, success in (
            ("cleared", True),
            ("disengaged", False),
            ("defeated", False),
            ("retreated", False),
        ):
            result, _ = self.run_with(
                lambda t, s=signal: {s: True} if t >= 3.0 else {},
                enemy=lambda t: t < 3.0,
            )
            self.assertEqual(result.reason, signal)
            self.assertEqual(result.success, success, signal)
            self.assertTrue(result.confirmed_end, signal)
            self.assertLess(
                result.duration, 4.5, signal
            )  # 正向证据立即结束，不等目标丢失

    def test_cleared_with_enemy_still_visible_is_not_victory(self):
        result, world = self.run_with(lambda t: {"cleared": True} if t >= 2.0 else {})
        # 敌人仍在却报清场：矛盾证据进入不确定状态，不输入，也不宣布胜利
        self.assertEqual(result.reason, "observation_lost")
        self.assertFalse(result.success)
        self.assertEqual(clicks_between(world, 2.3, 99.0), [])

    def test_single_frame_terminal_signal_is_ignored(self):
        result, _ = self.run_with(
            lambda t: {"cleared": True} if 3.0 <= t < 3.05 else {},
            enemy=lambda t: t < 3.0,
        )
        self.assertEqual(result.reason, "target_lost")

    def test_conflicting_terminal_signals_are_uncertain(self):
        result, _ = self.run_with(
            lambda t: {"cleared": True, "defeated": True} if t >= 3.0 else {},
            enemy=lambda t: t < 3.0,
        )
        self.assertNotIn(result.reason, ("cleared", "defeated"))
        self.assertFalse(result.success)


class RetreatTests(unittest.TestCase):
    def test_prompt_pauses_input_and_cancel_resumes(self):
        engine, world, _, _ = make_engine(
            STAY,
            {"max_duration": 14.0},
            extra=lambda t: {"retreat_prompt": True} if 3.0 <= t < 6.0 else {},
        )
        result = engine.run()
        self.assertEqual(result.reason, "timeout")
        self.assertEqual(clicks_between(world, 3.3, 5.95), [])  # 提示期间不输入
        self.assertTrue(clicks_between(world, 6.5, 14.0))  # 取消后见到敌人才恢复

    def test_confirmed_retreat_ends_without_restart(self):
        engine, world, _, _ = make_engine(
            STAY,
            {"max_duration": 60.0},
            enemy=lambda t: t < 3.0,
            extra=lambda t: (
                {"retreat_prompt": True}
                if 3.0 <= t < 4.0
                else (
                    {"black": True}
                    if 4.0 <= t < 5.0
                    else {"retreated": True} if t >= 5.0 else {}
                )
            ),
        )
        result = engine.run()
        self.assertEqual(result.reason, "retreated")
        self.assertEqual(clicks_between(world, 3.3, 99.0), [])

    def test_black_screen_after_prompt_is_not_retreat_evidence(self):
        engine, world, _, _ = make_engine(
            STAY,
            {"max_duration": 60.0, "hud_lost_timeout": 4.0},
            enemy=lambda t: t < 3.0,
            extra=lambda t: (
                {"retreat_prompt": True}
                if 3.0 <= t < 4.0
                else {"black": True} if t >= 4.0 else {}
            ),
        )
        result = engine.run()
        self.assertEqual(result.reason, "hud_lost")  # 无法确认离场：不冒充撤退成功
        self.assertNotEqual(result.reason, "retreated")

    def test_prompt_releases_held_keys(self):
        data = {
            "version": 1,
            "combos": {"default": "keydown(w), wait(5)"},
            "rotation": {"mode": "stay"},
        }
        engine, world, _, _ = make_engine(
            data,
            {"max_duration": 8.0},
            extra=lambda t: {"retreat_prompt": True} if 2.0 <= t < 3.0 else {},
        )
        engine.run()
        held = [
            (t, kind)
            for t, kind, vk in world.events
            if vk == VK_W and kind in ("down", "up")
        ]
        ups = [t for t, kind in held if kind == "up"]
        self.assertTrue(any(2.0 <= t < 2.5 for t in ups), held)  # 提示出现后立即松开 W
        self.assertEqual(held[-1][1], "up")


if __name__ == "__main__":
    unittest.main()
