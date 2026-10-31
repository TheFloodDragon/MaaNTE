"""有效方案覆盖、输入节奏与脚本会话 API 验收。"""

import unittest

from ._fakes import SLOT_VK, VK_E, VK_Q, make_engine
from ._loader import load, submodule

combat = load()
plan_mod = submodule("plan")
settings = submodule("settings")
executor = submodule("executor")
scheme_mod = submodule("scheme")

STAY = {"version": 1, "combos": {"default": "l_click(1)"}, "rotation": {"mode": "stay"}}
ROTATION = {
    "version": 1,
    "combos": {"default": "skill"},
    "rotation": {"order": [1, 2, 3, 4]},
}


def plan_for(data, *layers):
    options, warnings, errors = settings.merge_options_strict(list(layers))
    assert not errors, errors
    return plan_mod.apply_overrides(scheme_mod.parse_scheme(data), options)


def engine_for(plan, max_duration=10.0, **world):
    """按有效方案构造引擎，参数与 api.build_engine 传给 EngineConfig 的一致。"""
    cfg = {
        "max_duration": max_duration,
        "team_size": plan.options.team_size,
        "disabled": plan.options.disabled_actions,
        "disabled_slots": plan.disabled_slots,
        "speed": plan.options.combat_speed,
    }
    return make_engine(plan.scheme, cfg, **world)


class PlanTests(unittest.TestCase):
    def test_slot_overrides_merge_per_slot_and_field(self):
        plan = plan_for(
            ROTATION,
            {"slots": {"2": {"combo": "ultimate", "character": "零"}}},
            {"slots": {"2": {"note": "辅助", "combo": ""}, "3": {"enabled": False}}},
        )
        by_slot = {s.slot: s for s in plan.slots}
        self.assertEqual(
            (by_slot[2].combo, by_slot[2].combo_source), ("ultimate", "override")
        )
        self.assertEqual((by_slot[2].character, by_slot[2].note), ("零", "辅助"))
        self.assertEqual(
            (by_slot[1].combo, by_slot[1].combo_source), ("skill", "default")
        )
        self.assertFalse(by_slot[3].enabled)
        self.assertEqual(plan.order, (1, 2, 4))
        self.assertEqual(plan.disabled_slots, frozenset({3}))

    def test_overrides_do_not_mutate_cached_scheme(self):
        base = combat.load_scheme("team_rotation")
        options, _, _ = settings.merge_options_strict(
            [{"slots": {"1": {"combo": "jump"}}, "rotation_order": "2,1"}]
        )
        plan_mod.apply_overrides(base, options)
        again = combat.load_scheme("team_rotation")
        self.assertEqual(again.order, [1, 2, 3, 4])
        self.assertNotIn("1", again.combos)

    def test_rotation_order_sets_opener_and_team_size_filters(self):
        plan = plan_for(ROTATION, {"rotation_order": "3,1,4,2", "team_size": 3})
        self.assertEqual((plan.order, plan.opener), ((3, 1, 2), 3))
        stay = plan_for(STAY, {"rotation_order": "2,1"})
        self.assertEqual(stay.opener, 0)  # 站场模式不强制首发

    def test_invalid_configuration_fails_before_any_input(self):
        base = scheme_mod.parse_scheme(ROTATION)
        for layer, code in (
            ({"slots": {"1": {"combo": "perform"}}}, "slot_combo"),
            ({"slots": {s: {"enabled": False} for s in "1234"}}, "no_enabled_slot"),
            ({"rotation_order": "1,2", "rotation_mode": "cycle"}, None),
        ):
            options, _, errors = settings.merge_options_strict([layer])
            self.assertEqual(errors, [])
            if code is None:
                plan_mod.apply_overrides(base, options)
                continue
            with self.assertRaises(scheme_mod.SchemeError) as ctx:
                plan_mod.apply_overrides(base, options)
            self.assertEqual(ctx.exception.code, code)
        for bad in (
            {"combat_speed": 0},
            {"combat_speed": -1},
            {"combat_speed": 2.5},
            {"combat_speed": float("nan")},
            {"combat_speed": float("inf")},
            {"combat_speed": True},
            {"rotation_order": "1,5"},
            {"rotation_mode": "x"},
            {"slots": {"5": {"combo": "skill"}}},
            {"slots": {"1": {"bogus": 1}}},
            {"min_field_time": 11},
        ):
            _, _, errors = settings.merge_options_strict([bad])
            self.assertEqual(len(errors), 1, bad)

    def test_every_builtin_scheme_still_prepares(self):
        for name in scheme_mod.BUILTIN_SCHEMES:
            for size in (1, 2, 3, 4):
                plan = plan_for(
                    {"version": 1, "combos": {"default": "skill"}}, {"team_size": size}
                )
                plan = plan_mod.apply_overrides(combat.load_scheme(name), plan.options)
                self.assertTrue(all(s <= size for s in plan.order), (name, size))

    def test_fingerprint_tracks_effective_content(self):
        a = plan_for(ROTATION)
        b = plan_for(ROTATION, {"slots": {"1": {"combo": "jump"}}})
        c = plan_for(ROTATION, {"combat_speed": 1.5})
        self.assertEqual(len({a.fingerprint, b.fingerprint, c.fingerprint}), 3)
        self.assertEqual(a.fingerprint, plan_for(ROTATION).fingerprint)


class EngineOverrideTests(unittest.TestCase):
    def test_disabled_slot_never_switched_to(self):
        plan = plan_for(ROTATION, {"slots": {"3": {"enabled": False}}})
        engine, world, _, rep = engine_for(plan, max_duration=12.0)
        engine.run()
        presses = world.slot_presses()
        self.assertNotIn(3, presses)
        self.assertEqual(presses[:3], [2, 4, 1])
        self.assertNotIn(("unavailable", 3), rep.calls)

    def test_opener_from_rotation_order_is_switched_first(self):
        plan = plan_for(ROTATION, {"rotation_order": "3,1"})
        engine, world, _, _ = engine_for(plan, max_duration=8.0)  # 当前在 1 号位
        engine.run()
        self.assertEqual(world.slot_presses()[:3], [3, 1, 3])

    def test_each_slot_runs_its_own_combo(self):
        plan = plan_for(
            ROTATION,
            {"rotation_order": "1,2", "min_field_time": 0},
            {"slots": {"1": {"combo": "ultimate"}, "2": {"combo": "skill"}}},
        )
        engine, world, _, _ = engine_for(plan, max_duration=8.0)
        engine.run()
        downs = world.downs()
        switches = [(t, SLOT_VK[v]) for t, v in downs if v in SLOT_VK]
        on_two = [t for t, s in switches if s == 2]
        q = [t for t, v in downs if v == VK_Q]
        e = [t for t, v in downs if v == VK_E]
        self.assertTrue(q and e)
        self.assertLess(q[0], on_two[0])  # 1 号位出 Q
        self.assertGreater(e[0], on_two[0])  # 2 号位出 E


class SpeedTests(unittest.TestCase):
    def clicks_in_one_second(self, speed):
        data = {
            "version": 1,
            "combos": {"default": "l_click(1), wait(5)"},
            "rotation": {"mode": "stay"},
        }
        engine, world, _, _ = make_engine(data, {"max_duration": 3.0, "speed": speed})
        engine.run()
        return [t for t in world.times("click", 0) if t < 1.05]

    def test_speed_scales_click_density_only(self):
        normal, fast, slow = (
            len(self.clicks_in_one_second(s)) for s in (1.0, 2.0, 0.5)
        )
        self.assertGreater(fast, normal * 1.6)
        self.assertLess(slow, normal * 0.7)

    def test_explicit_durations_are_real_seconds(self):
        data = {
            "version": 1,
            "combos": {"default": "l_hold(0.6), walk(w, 0.5), wait(1)"},
            "rotation": {"mode": "stay", "min_field_time": 0},
        }
        for speed in (0.5, 2.0):
            engine, world, _, _ = make_engine(
                data, {"max_duration": 2.9, "speed": speed}
            )
            engine.run()
            down, up = world.times("tdown", 0)[0], world.times("tup", 0)[0]
            self.assertAlmostEqual(up - down, 0.6, delta=0.06)
            w = [t for t, kind, vk in world.events if vk == 0x57]
            self.assertAlmostEqual(w[1] - w[0], 0.5, delta=0.06)

    def test_invalid_speeds_rejected_everywhere(self):
        for value in (0, -1, 0.49, 2.01, float("nan"), float("inf"), True, "1"):
            with self.assertRaises(ValueError):
                executor.validate_speed(value)
        for text in ("speed(0)", "speed(3)", "speed(nan)", "speed()"):
            with self.assertRaises(combat.ComboError):
                combat.compile_combo(text)

    def test_dsl_speed_is_a_control_command_for_this_fight_only(self):
        data = {
            "version": 1,
            "combos": {"default": "speed(2), l_click(1)"},
            "rotation": {"mode": "stay"},
        }
        engine, _, _, _ = make_engine(data, {"max_duration": 2.0})
        engine.run()
        self.assertEqual(engine.executor.speed, 2.0)
        engine.run()  # 下一场从配置值开始
        self.assertEqual(engine.executor.base_speed, 1.0)


def scripted(script, scheme=ROTATION, config=None, **world):
    engine, w, clock, rep = make_engine(
        scheme, {"max_duration": 30.0, **(config or {})}, **world
    )
    return engine.run(script), w, clock, rep


class SessionTests(unittest.TestCase):
    def test_script_controls_switch_skill_and_combo(self):
        log = []

        def script(s):
            log.append(s.switch(3))
            log.append(s.skill())
            log.append(s.perform("ultimate, l_click(0.5)", slot=2))
            log.append(s.switch(2))  # 已是 2 号位：不发送按键
            log.append(s.set_speed(1.5))
            self.assertEqual((s.current_slot, s.speed, s.state), (2, 1.5, "active"))

        result, world, _, _ = scripted(script)
        self.assertEqual(result.reason, "script_finished")
        self.assertFalse(result.success)
        self.assertTrue(result.entered)
        statuses = [r.status if hasattr(r, "status") else r for r in log]
        self.assertEqual(statuses, ["sent", "sent", "sent", "unchanged", 1.5])
        self.assertEqual(world.slot_presses(), [3, 2])

    def test_disabled_actions_and_slots_are_not_bypassed(self):
        def script(s):
            self.assertEqual(s.ultimate().status, "skipped")
            self.assertEqual(s.perform("keydown(q), keypress(4)").status, "skipped")
            self.assertEqual(s.switch(4).status, "skipped")
            self.assertEqual(s.perform("skill", slot=4).status, "skipped")
            self.assertEqual(s.skill().status, "sent")

        result, world, _, _ = scripted(
            script, config={"disabled": {"ultimate"}, "disabled_slots": {4}}
        )
        self.assertEqual(result.reason, "script_finished")
        self.assertEqual(world.count("down", VK_Q), 0)
        self.assertNotIn(4, world.slot_presses())

    def test_failed_switch_is_reported(self):
        def script(s):
            self.assertEqual(s.switch(3).status, "switch_failed")
            self.assertIn(3, s.unavailable_slots)
            self.assertEqual(s.switch(3).status, "switch_failed")

        result, world, _, _ = scripted(script, alive=(1, 2, 4))
        self.assertEqual(
            world.slot_presses().count(3), 2
        )  # 首按 + 一次补按，之后不再尝试

    def test_stop_and_terminal_evidence(self):
        result, _, _, _ = scripted(lambda s: s.stop())
        self.assertEqual(result.reason, "script_finished")

        def until_end(s):
            while True:
                s.attack(0.5)

        result, _, _, _ = scripted(until_end, enemy=lambda t: t < 2.0)
        self.assertEqual(result.reason, "target_lost")
        result, _, _, _ = scripted(
            until_end,
            extra=lambda t: {"cleared": True, "enemy": False} if t >= 2.0 else {},
        )
        self.assertEqual((result.reason, result.success), ("cleared", True))

    def test_swallowed_end_is_kept_and_session_rejects_late_calls(self):
        holder = {}

        def script(s):
            holder["s"] = s
            try:
                while True:
                    s.attack(0.5)
            except Exception:
                pass  # 吞掉结束信号也不能继续输入
            with self.assertRaises(Exception):
                s.skill()

        result, world, _, _ = scripted(script, enemy=lambda t: t < 2.0)
        self.assertEqual(result.reason, "target_lost")
        with self.assertRaises(RuntimeError):
            holder["s"].skill()  # 会话结束后拒绝发送

    def test_script_exception_is_error_and_releases_keys(self):
        def script(s):
            s.perform("keydown(w)")
            raise RuntimeError("boom")

        result, world, _, _ = scripted(script)
        self.assertEqual(result.reason, "error")
        held = [
            kind
            for _, kind, vk in world.events
            if vk == 0x57 and kind in ("down", "up")
        ]
        self.assertEqual(held[-1], "up")

    def test_run_default_uses_the_same_rotation(self):
        result, world, _, _ = scripted(
            lambda s: s.run_default(turns=3), scheme=ROTATION
        )
        self.assertEqual(result.reason, "script_finished")
        self.assertEqual(world.slot_presses()[:3], [2, 3, 4])

    def test_bad_arguments_raise(self):
        def script(s):
            for call in (
                lambda: s.switch(5),
                lambda: s.switch(True),
                lambda: s.set_speed(3),
                lambda: s.run_default(0),
                lambda: s.perform(""),
            ):
                with self.assertRaises((ValueError, combat.ComboError)):
                    call()

        self.assertEqual(scripted(script)[0].reason, "script_finished")


if __name__ == "__main__":
    unittest.main()
