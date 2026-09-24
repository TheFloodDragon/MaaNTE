from dataclasses import asdict, replace
import math
import unittest

from fish_test_support import load_fish_module

params = load_fish_module("fish_params")
control = load_fish_module("fish_control")


def boxes(cursor=600.0, center=600.0, width=80.0):
    return (center - width / 2, 43, width, 12), (cursor - 1, 43, 2, 14)


def stable(engine, cursor=600.0, center=600.0, width=80.0):
    green, yellow = boxes(cursor, center, width)
    engine.observe(green, yellow, 0.0, frame_id=1)
    return engine.observe(green, yellow, 1 / 30, frame_id=2)


class FishConfigTests(unittest.TestCase):
    def test_modes_and_fingerprint(self):
        off = params.load_fish_engine_config(None)
        infer = params.load_fish_engine_config('{"learning_mode": "infer"}')
        learn = params.load_fish_engine_config({"learning_enabled": True})
        self.assertEqual((off.learning_mode, infer.learning_mode, learn.learning_mode), ("off", "infer", "learn"))
        self.assertEqual(off.fingerprint(), infer.fingerprint())
        self.assertEqual(off.fingerprint(), learn.fingerprint())
        self.assertNotEqual(off.fingerprint(), replace(off, prediction_ms=100).fingerprint())

    def test_invalid_parameters_are_finite(self):
        defaults = params.FishControlConfig()
        invalid = {key: float("nan") for key, value in asdict(defaults).items() if isinstance(value, (int, float))}
        invalid.update(learning_mode=[], learning_seed=True, width_confirm_frames=1.2)
        config = params.load_fish_engine_config(invalid)
        self.assertEqual(config, defaults)
        for value in ([], "bad json", 2, '"value"'):
            self.assertEqual(params.load_fish_engine_config(value), defaults)

    def test_old_parameters_and_new_precedence(self):
        config = params.load_fish_engine_config({
            "center_band_ratio": 0.6,
            "pulse_min_ms": 18,
            "pulse_max_ms": 36,
            "pulse_ms_per_px": 0.45,
            "inside_pulse_max_ms": 22,
            "prediction_ms": 140,
        })
        self.assertEqual(config.center_width_ratio, 0.3)
        self.assertEqual(config.inside_pulse_min_ms, 18)
        self.assertEqual(config.inside_pulse_max_ms, 22)
        self.assertEqual(config.outside_pulse_max_ms, 36)
        self.assertEqual(config.pulse_error_gain, 0.45)
        self.assertEqual(config.prediction_ms, 140)

    def test_cross_parameter_safety(self):
        config = params.load_fish_engine_config({
            "action_deadzone": 1,
            "inside_action_cap": 0.02,
            "inside_edge_action_cap": 0.02,
            "outside_action_cap": 0.02,
            "approach_action_cap": 0.02,
            "inside_pulse_min_ms": 10000,
            "inside_pulse_max_ms": 1,
            "learning_min_dt_ms": 100,
            "observation_gap_ms": 20,
            "lost_abort_ms": 100,
            "control_end_grace_ms": 2000,
        })
        self.assertLess(config.action_deadzone, config.inside_action_cap)
        self.assertLessEqual(config.inside_pulse_min_ms, config.inside_pulse_max_ms)
        self.assertLessEqual(config.inside_pulse_max_ms, params.MAX_KEY_HOLD_MS)
        self.assertLessEqual(config.learning_min_dt_ms, config.learning_max_dt_ms)
        self.assertLessEqual(config.learning_max_dt_ms, config.observation_gap_ms)
        self.assertGreater(config.lost_abort_ms, config.control_end_grace_ms)


class FishEngineTests(unittest.TestCase):
    def test_center_is_silent_even_with_residual(self):
        engine = control.FishingEngine()
        obs = stable(engine)
        decision = engine.decide(obs, 1.0)
        self.assertTrue(obs.in_safe_zone)
        self.assertIsNone(decision.key)
        self.assertEqual(decision.action, 0.0)
        self.assertEqual(decision.residual, 0.0)

    def test_left_right_recovery_is_symmetric(self):
        left = control.FishingEngine()
        right = control.FishingEngine()
        a = left.decide(stable(left, cursor=540, width=60))
        b = right.decide(stable(right, cursor=660, width=60))
        self.assertEqual(a.key, control.KEY_D)
        self.assertEqual(b.key, control.KEY_A)
        self.assertAlmostEqual(a.action, -b.action)
        self.assertEqual(a.duration_ms, b.duration_ms)
        self.assertGreater(a.duration_ms, 0)
        self.assertLessEqual(a.duration_ms, 70)

    def test_same_velocity_has_no_false_relative_motion(self):
        engine = control.FishingEngine()
        stable(engine)
        for index in range(2, 12):
            position = 600 + (index - 1) * 5
            obs = engine.observe(*boxes(position, position), index / 30, frame_id=index + 1)
            self.assertAlmostEqual(obs.relative_vx, 0.0)
            self.assertAlmostEqual(obs.error_px, 0.0)
            self.assertAlmostEqual(obs.predicted_error_px, 0.0)
            self.assertIsNone(engine.decide(obs).key)

    def test_predictive_edge_action_reaches_output(self):
        engine = control.FishingEngine()
        stable(engine, width=40)
        obs = engine.observe(*boxes(610, width=40), 2 / 30, frame_id=3)
        self.assertTrue(obs.inside_target)
        self.assertFalse(obs.in_safe_zone)
        decision = engine.decide(obs)
        self.assertEqual(decision.key, control.KEY_A)
        self.assertGreater(abs(decision.action), engine.config.action_deadzone)
        self.assertLessEqual(decision.duration_ms, engine.config.inside_pulse_max_ms)

    def test_outside_breaks_center_silence(self):
        engine = control.FishingEngine()
        engine.decide(stable(engine, width=24))
        obs = engine.observe(*boxes(615, width=24), 2 / 30, frame_id=3)
        self.assertFalse(obs.inside_target)
        self.assertIsNotNone(engine.decide(obs).key)

    def test_residual_cannot_reverse_recovery(self):
        engine = control.FishingEngine()
        obs = stable(engine, cursor=638, width=80)
        decision = engine.decide(obs, 500.0)
        self.assertEqual(decision.key, control.KEY_A)
        self.assertLess(decision.action, 0)
        self.assertLessEqual(abs(decision.residual), engine.config.learning_residual_limit + 1e-9)
        self.assertLessEqual(abs(decision.requested_residual), engine.config.learning_residual_limit)
        invalid = engine.decide(obs, float("nan"))
        self.assertEqual(invalid.requested_residual, 0)

    def test_execution_memory_only_changes_on_confirmation(self):
        engine = control.FishingEngine()
        obs = stable(engine, cursor=540)
        decision = engine.decide(obs)
        self.assertEqual(engine.last_action, 0.0)
        engine.remember_execution(decision)
        self.assertEqual(engine.last_action, decision.action)
        engine.reset_tracking()
        self.assertEqual(engine.last_action, 0.0)
        self.assertIsNone(engine.last_cursor_center)

    def test_missing_and_stale_samples_do_not_advance_state(self):
        engine = control.FishingEngine()
        obs = stable(engine)
        self.assertIsNone(engine.observe(None, boxes()[1], 0.05, frame_id=3))
        self.assertIsNone(engine.observe(*boxes(), 0.02, frame_id=3))
        self.assertIsNone(engine.observe(*boxes(), 0.05, frame_id=2))
        self.assertIsNone(engine.observe(*boxes(), float("nan"), frame_id=3))
        self.assertIsNone(engine.observe(*boxes(), 0.05, capture_ms=1000, frame_id=3))
        self.assertEqual(engine.last_cursor_center, obs.cursor_x)
        current = engine.observe(*boxes(), 0.5, frame_id=3)
        self.assertFalse(current.reliable)
        self.assertEqual(current.cursor_vx, 0)
        self.assertIsNone(engine.decide(current).key)

    def test_width_shrink_requires_confirmation_and_bounds_margin(self):
        engine = control.FishingEngine()
        stable(engine, width=80)
        first = engine.observe(*boxes(width=12), 2 / 30, frame_id=3)
        self.assertFalse(first.reliable)
        self.assertLessEqual(first.target_width, 12)
        self.assertLess(first.safe_margin_px, first.target_width / 2)
        self.assertIsNone(engine.decide(first).key)
        engine.observe(*boxes(width=12), 3 / 30, frame_id=4)
        third = engine.observe(*boxes(width=12), 4 / 30, frame_id=5)
        self.assertTrue(third.reliable)
        self.assertEqual(third.target_width, 12)

    def test_outlier_and_confirmed_relocation(self):
        engine = control.FishingEngine(replace(params.FishControlConfig(), max_velocity=100))
        stable(engine)
        self.assertIsNone(engine.observe(*boxes(700), 2 / 30, frame_id=3))
        relocated = engine.observe(*boxes(700), 3 / 30, frame_id=4)
        self.assertFalse(relocated.reliable)
        self.assertEqual(relocated.cursor_vx, 0)
        self.assertEqual(relocated.cursor_x, 700)
        stable_obs = engine.observe(*boxes(700), 4 / 30, frame_id=5)
        self.assertTrue(stable_obs.reliable)

    def test_dt_aware_jump_threshold(self):
        engine = control.FishingEngine(replace(params.FishControlConfig(), max_velocity=300))
        stable(engine)
        obs = engine.observe(*boxes(650), 1 / 30 + 0.2, frame_id=3)
        self.assertIsNotNone(obs)
        self.assertTrue(obs.reliable)
        self.assertLessEqual(abs(obs.cursor_vx), 300)

    def test_static_target_converges(self):
        engine = control.FishingEngine()
        cursor, now = 470.0, 0.0
        for frame in range(1, 101):
            obs = engine.observe(*boxes(cursor), now, frame_id=frame)
            decision = engine.decide(obs)
            cursor += (1 if decision.key == control.KEY_D else -1 if decision.key == control.KEY_A else 0) * decision.duration_ms * 0.7
            now += 0.035 + decision.duration_ms / 1000
            engine.remember_execution(decision)
            self.assertTrue(math.isfinite(cursor))
            self.assertLessEqual(decision.duration_ms, 70)
        self.assertLess(abs(cursor - 600), 31)


if __name__ == "__main__":
    unittest.main()
