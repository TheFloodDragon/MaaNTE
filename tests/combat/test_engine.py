"""战斗引擎场景测试（假时钟 + 假游戏世界，不依赖 maa 与游戏）。"""

import unittest

from ._fakes import VK_E, VK_Q, make_engine
from ._loader import submodule

engine_mod = submodule("engine")
targeting = submodule("targeting")
keys = submodule("keys")

ROTATION = {
    "version": 1,
    "combos": {"default": "ultimate, skill, l_click(1.5)"},
    "rotation": {"mode": "cycle", "order": [1, 2, 3, 4], "min_field_time": 1.0},
}
STAY = {
    "version": 1,
    "combos": {"default": "skill, l_click(1)"},
    "rotation": {"mode": "stay"},
}
VK_W = 0x57
VK_SPACE = 0x20


def scheme(combos, mode="cycle", order=(1, 2, 3, 4), min_field_time=1.0):
    return {
        "version": 1,
        "combos": combos,
        "rotation": {
            "mode": mode,
            "order": list(order),
            "min_field_time": min_field_time,
        },
    }


class RotationTests(unittest.TestCase):
    def test_cycles_slots_in_order_with_q_e_each_turn(self):
        engine, world, _, rep = make_engine(ROTATION, {"max_duration": 12.0})
        result = engine.run()
        self.assertEqual(result.reason, "timeout")
        self.assertTrue(result.success)
        self.assertEqual(world.slot_presses()[:4], [2, 3, 4, 1])
        downs = [v for _, v in world.downs()]
        self.assertEqual(downs[:2], [VK_Q, VK_E])  # 首个角色先 Q 再 E
        self.assertEqual(rep.calls[0], ("waiting",))
        self.assertEqual(rep.calls[1], ("started",))
        self.assertEqual(rep.calls[-1], ("ended", "timeout"))

    def test_dead_slot_removed_from_rotation_after_one_resend(self):
        engine, world, _, rep = make_engine(
            ROTATION, {"max_duration": 20.0}, alive=(1, 2, 4)
        )
        engine.run()
        presses = world.slot_presses()
        self.assertEqual(presses.count(3), 2)  # 首次按键 + 窗口内补按一次，之后不再尝试
        self.assertIn(("unavailable", 3), rep.calls)
        self.assertEqual(rep.calls.count(("unavailable", 3)), 1)
        self.assertEqual(presses[:4], [2, 3, 3, 4])
        self.assertIn(1, presses[4:])

    def test_unknown_start_switches_to_first_slot(self):
        engine, world, _, _ = make_engine(ROTATION, {"max_duration": 4.0}, slot=0)
        engine.run()
        self.assertEqual(world.slot_presses()[0], 1)

    def test_degrades_to_stay_when_slots_never_detected(self):
        engine, world, _, rep = make_engine(
            ROTATION, {"max_duration": 10.0}, detect_slots=False
        )
        result = engine.run()
        self.assertEqual(result.reason, "timeout")
        self.assertIn(("degraded",), rep.calls)
        self.assertLessEqual(
            len(world.slot_presses()), 2
        )  # 只在首次确认时按过，之后站场
        self.assertNotIn(("unavailable", 2), rep.calls)

    def test_main_dps_returns_to_slot_one(self):
        data = scheme({"default": "skill"}, order=(1, 2, 1, 3, 1, 4))
        engine, world, _, _ = make_engine(data, {"max_duration": 12.0})
        engine.run()
        self.assertEqual(world.slot_presses()[:6], [2, 1, 3, 1, 4, 1])

    def test_skipping_dead_slot_does_not_give_current_slot_two_turns(self):
        data = scheme({"default": "skill"}, order=(1, 2, 1, 3, 1, 4))
        engine, world, _, _ = make_engine(data, {"max_duration": 12.0}, alive=(1, 3, 4))
        engine.run()
        downs = world.downs()
        last_two = max(t for t, v in downs if v == 0x32)
        first_three = min(t for t, v in downs if v == 0x33)
        self.assertLess(last_two, first_three)
        # 2 号位切人失败后直接切 3 号位，1 号位不会在中间再多打一轮
        self.assertEqual(
            [t for t, v in downs if v == VK_E and last_two < t < first_three], []
        )

    def test_min_field_time_filled_with_attacks(self):
        data = scheme({"default": "skill"}, order=(1, 2), min_field_time=2.0)
        engine, world, _, _ = make_engine(data, {"max_duration": 9.0})
        engine.run()
        times = [t for t, v in world.downs() if v in (0x31, 0x32)]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(gaps)
        self.assertTrue(all(gap >= 2.0 for gap in gaps), gaps)
        self.assertGreater(world.count("click"), 10)


class StayTests(unittest.TestCase):
    def test_stay_never_presses_slot_keys(self):
        engine, world, _, _ = make_engine(STAY, {"max_duration": 6.0})
        engine.run()
        self.assertEqual(world.slot_presses(), [])
        self.assertGreater(world.count("down", VK_E), 2)

    def test_stay_follows_manual_switch(self):
        data = scheme(
            {
                "1": "skill, l_click(0.5)",
                "2": "jump, l_click(0.5)",
                "3": "skill",
                "4": "skill",
            },
            mode="stay",
        )
        engine, world, _, _ = make_engine(
            data, {"max_duration": 6.0}, manual=[(3.0, 2)]
        )
        engine.run()
        jumps = [t for t, v in world.downs() if v == VK_SPACE]
        self.assertTrue(jumps)
        self.assertGreaterEqual(min(jumps), 3.0)


class EndConditionTests(unittest.TestCase):
    def test_cleared_after_confirm_window(self):
        engine, _, _, _ = make_engine(
            STAY, {"max_duration": 60.0}, enemy=lambda t: t < 5.0
        )
        result = engine.run()
        self.assertEqual(result.reason, "cleared")
        self.assertTrue(result.success)
        self.assertGreater(result.duration, 7.0)
        self.assertLess(result.duration, 9.5)

    def test_clear_timer_pauses_while_hud_hidden(self):
        engine, _, _, _ = make_engine(
            STAY, {"max_duration": 60.0}, enemy=lambda t: t < 4.0, hidden=[(4.5, 9.5)]
        )
        result = engine.run()
        self.assertEqual(result.reason, "cleared")
        self.assertGreater(result.duration, 11.0)

    def test_hud_lost_ends_fight(self):
        engine, _, _, _ = make_engine(STAY, {"max_duration": 60.0}, hud_lost_at=3.0)
        result = engine.run()
        self.assertEqual(result.reason, "hud_lost")
        self.assertTrue(result.success)
        self.assertGreater(result.duration, 12.0)
        self.assertLess(result.duration, 14.5)

    def test_no_combat_when_enemy_never_appears(self):
        engine, world, _, rep = make_engine(STAY, {"wait_timeout": 2.0}, enemy=False)
        result = engine.run()
        self.assertEqual(result.reason, "no_combat")
        self.assertFalse(result.success)
        self.assertEqual(result.duration, 0.0)
        self.assertEqual(world.downs(), [])
        self.assertNotIn(("started",), rep.calls)

    def test_wait_forever_until_enemy(self):
        engine, world, _, _ = make_engine(
            STAY, {"wait_timeout": 0, "max_duration": 3.0}, enemy=lambda t: t >= 30.0
        )
        result = engine.run()
        self.assertEqual(result.reason, "timeout")
        self.assertGreaterEqual(world.downs()[0][0], 30.0)

    def test_single_frame_enemy_flicker_is_ignored(self):
        engine, _, _, _ = make_engine(
            STAY, {"wait_timeout": 3.0}, enemy=lambda t: 1.0 <= t < 1.1
        )
        self.assertEqual(engine.run().reason, "no_combat")

    def test_stop_releases_held_keys(self):
        data = scheme({"default": "keydown(w), mousedown(right), wait(5)"}, mode="stay")
        engine, world, _, _ = make_engine(data, {"max_duration": 60.0}, stop_at=1.0)
        result = engine.run()
        self.assertEqual(result.reason, "stopped")
        self.assertFalse(result.success)
        w_events = [
            kind for _, kind, v in world.events if v == VK_W and kind in ("down", "up")
        ]
        self.assertEqual(w_events[-1], "up")
        self.assertEqual(world.count("tdown", 1), world.count("tup", 1))

    def test_controller_crash_reports_error_and_releases(self):
        data = scheme({"default": "keydown(w), l_click(1)"}, mode="stay")
        engine, world, _, rep = make_engine(data, {"max_duration": 60.0}, crash_at=0.5)
        result = engine.run()
        self.assertEqual(result.reason, "error")
        self.assertFalse(result.success)
        self.assertEqual(world.events[-1][1:], ("up", VK_W))
        self.assertEqual(rep.calls[-1], ("ended", "error"))


class ComboTests(unittest.TestCase):
    def test_ultimate_animation_pauses_inputs(self):
        engine, world, _, _ = make_engine(
            STAY | {"combos": {"default": "ultimate, skill, l_click(0.5)"}},
            {"max_duration": 8.0},
            ult_hide=2.0,
        )
        engine.run()
        downs = world.downs()
        for i, (t, vk) in enumerate(downs):
            if vk == VK_Q:
                nxt = next((t2 for t2, v2 in downs[i + 1 :] if v2 == VK_E), None)
                if nxt is not None:  # HUD 在 Q 后 0.1~2.1s 消失，E 必须等到 HUD 恢复
                    self.assertGreaterEqual(nxt - t, 2.1 - 1e-6)
        hidden_clicks = [
            t
            for t, kind, _ in world.events
            if kind == "click" and not world.hud_visible(t)
        ]
        self.assertEqual(hidden_clicks, [])

    def test_failed_switch_aborts_rest_of_combo_and_is_not_retried(self):
        data = scheme(
            {"1": "skill, switch(3), jump", "2": "skill", "3": "skill", "4": "skill"},
            mode="stay",
        )
        engine, world, _, rep = make_engine(
            data, {"max_duration": 8.0}, alive=(1, 2, 4)
        )
        engine.run()
        self.assertEqual(world.slot_presses().count(3), 2)
        self.assertEqual([v for _, v in world.downs() if v == VK_SPACE], [])
        self.assertIn(("unavailable", 3), rep.calls)

    def test_combo_switch_degrades_when_slots_never_detected(self):
        data = scheme({"default": "skill, switch(2), jump"}, mode="stay")
        engine, world, _, rep = make_engine(
            data, {"max_duration": 6.0}, detect_slots=False
        )
        engine.run()
        self.assertIn(("degraded",), rep.calls)
        self.assertNotIn(("unavailable", 2), rep.calls)
        self.assertTrue(
            [v for _, v in world.downs() if v == VK_SPACE]
        )  # 降级后继续本段出招
        self.assertGreaterEqual(
            world.slot_presses().count(2), 2
        )  # 降级后仍按键（不确认）

    def test_unreleased_keydown_released_at_segment_end(self):
        data = scheme({"default": "keydown(w), skill"}, order=(1, 2))
        engine, world, _, _ = make_engine(data, {"max_duration": 6.0})
        engine.run()
        state = None
        for _, kind, value in world.events:
            if value == VK_W and kind in ("down", "up"):
                state = kind
            if kind == "down" and value in (0x31, 0x32):
                self.assertEqual(state, "up")


class EffectiveOrderTests(unittest.TestCase):
    def test_excluded_slots_removed_and_neighbours_merged(self):
        eo = engine_mod.effective_order
        self.assertEqual(eo([1, 2, 3, 4], {3, 4}), [1, 2])
        self.assertEqual(
            eo([1, 2, 1, 3, 1, 4], {3, 4}), [1, 2]
        )  # 主C站场 2 人队：1 不会连续两轮
        self.assertEqual(eo([1, 2, 1, 3, 1, 4], {4}), [1, 2, 1, 3])
        self.assertEqual(eo([1, 2, 1, 3, 1, 4], {2, 3, 4}), [1])
        self.assertEqual(eo([3, 1, 4, 2], {3, 4}), [1, 2])
        self.assertEqual(eo([2, 3, 4, 1], {2, 3, 4}), [1])

    def test_repeats_written_by_scheme_are_kept(self):
        eo = engine_mod.effective_order
        self.assertEqual(eo([1, 1, 2], set()), [1, 1, 2])
        self.assertEqual(eo([1, 2, 1], set()), [1, 2, 1])
        self.assertEqual(eo([3, 4], {3, 4}), [])


class StrategyTests(unittest.TestCase):
    def test_team_size_skips_slots_beyond_team(self):
        engine, world, _, rep = make_engine(
            ROTATION, {"max_duration": 12.0, "team_size": 2}
        )
        engine.run()
        presses = world.slot_presses()
        self.assertEqual(presses[:3], [2, 1, 2])
        self.assertTrue(set(presses) <= {1, 2}, presses)  # 空位不再花 1.2 秒确认
        self.assertNotIn(("unavailable", 3), rep.calls)

    def test_team_size_one_stays_and_skips_combo_switch(self):
        data = scheme({"default": "skill, switch(3), jump"})
        engine, world, _, rep = make_engine(data, {"max_duration": 6.0, "team_size": 1})
        engine.run()
        self.assertEqual(world.slot_presses(), [])
        self.assertTrue(
            [v for _, v in world.downs() if v == VK_SPACE]
        )  # switch(3) 跳过，本段继续
        self.assertNotIn(("unavailable", 3), rep.calls)

    def test_disabled_actions_are_skipped(self):
        engine, world, _, _ = make_engine(
            ROTATION, {"max_duration": 8.0, "disabled": {"ultimate", "switch"}}
        )
        engine.run()
        self.assertEqual(world.count("down", VK_Q), 0)
        self.assertGreater(world.count("down", VK_E), 1)
        self.assertEqual(world.slot_presses(), [])  # 禁用切人：轮转方案改为站场

    def test_fully_skipped_combo_falls_back_to_attacks(self):
        data = scheme({"default": "ultimate, skill"}, mode="stay", min_field_time=0)
        engine, world, _, _ = make_engine(
            data, {"max_duration": 4.0, "disabled": {"ultimate", "skill"}}
        )
        engine.run()
        self.assertEqual(world.count("down", VK_Q) + world.count("down", VK_E), 0)
        self.assertGreater(world.count("click", 0), 10)


class InputGatingTests(unittest.TestCase):
    def test_attacks_pause_while_hud_hidden_and_duration_extends(self):
        data = scheme({"default": "l_click(2), jump"}, mode="stay", min_field_time=0)
        engine, world, _, _ = make_engine(
            data, {"max_duration": 6.0}, hidden=[(1.0, 2.0)]
        )
        engine.run()
        late = [t for t in world.times("click", 0) if 1.25 <= t < 2.0]
        self.assertEqual(late, [])  # 最多只差一次节流快照（0.2 秒）
        first_jump = min(t for t, v in world.downs() if v == VK_SPACE)
        self.assertGreater(first_jump, 2.8)  # 被遮挡的时间不计入 2 秒普攻

    def test_field_time_filler_waits_for_hud(self):
        data = scheme({"default": "skill"}, order=(1, 2), min_field_time=3.0)
        engine, world, _, _ = make_engine(
            data, {"max_duration": 6.0}, hidden=[(1.0, 2.0)]
        )
        engine.run()
        self.assertEqual([t for t in world.times("click", 0) if 1.25 <= t < 2.0], [])


class TargetingIntegrationTests(unittest.TestCase):
    def test_locks_at_start_and_relocks_after_enemy_lost(self):
        engine, world, _, _ = make_engine(
            STAY, {"max_duration": 60.0, "lock_target": True}, enemy=lambda t: t < 3.0
        )
        result = engine.run()
        self.assertEqual(result.reason, "cleared")
        locks = world.times("click", 2)
        self.assertTrue(locks and locks[0] < 0.5, locks)  # 开战立即锁定
        self.assertEqual(
            [t for t in locks if 0.5 <= t < 3.0], []
        )  # 看得到敌人时不重复按中键
        self.assertGreaterEqual(len([t for t in locks if t >= 3.0]), 2)

    def test_ultimate_held_while_enemy_lost(self):
        data = scheme(
            {"default": "ultimate, l_click(0.5)"}, mode="stay", min_field_time=0
        )
        engine, world, _, _ = make_engine(
            data, {"max_duration": 60.0}, enemy=lambda t: t < 3.0
        )
        self.assertEqual(engine.run().reason, "cleared")
        q_times = world.times("down", VK_Q)
        self.assertTrue(q_times and q_times[0] < 1.0)
        self.assertEqual([t for t in q_times if t >= 4.5], [])

    def test_search_turn_rotates_and_extends_clear_confirm(self):
        engine, world, _, _ = make_engine(
            STAY, {"max_duration": 60.0, "search_turn": True}, enemy=lambda t: t < 3.0
        )
        result = engine.run()
        self.assertEqual(result.reason, "cleared")
        moved = sum(dx for _, kind, dx in world.events if kind == "move")
        expected = (
            targeting.SEARCH_MAX_TURNS
            * targeting.SEARCH_DEGREES
            * keys.TURN_PIXELS_PER_DEGREE
        )
        self.assertEqual(moved, int(expected))
        self.assertGreater(
            result.duration, 3.0 + 3.0 + targeting.SEARCH_EXTRA_CONFIRM - 1.0
        )


if __name__ == "__main__":
    unittest.main()
