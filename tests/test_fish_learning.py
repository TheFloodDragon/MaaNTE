from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from fish_test_support import load_fish_module

control = load_fish_module("fish_control")
params = load_fish_module("fish_params")
learning = load_fish_module("fish_learning")


def observation(timestamp=1.0, frame_id=1, error=50.0, reliable=True):
    edge = 40.0 - abs(error)
    return control.FishObservation(
        timestamp=timestamp, dt=0.05, frame_id=frame_id, capture_ms=10,
        cursor_x=600 + error, cursor_vx=0, target_center_x=600, target_vx=0,
        target_left=560, target_right=640, target_width=80,
        predicted_cursor_x=600 + error, predicted_target_center_x=600,
        error_px=error, predicted_error_px=error,
        edge_margin_px=edge, predicted_edge_margin_px=edge, safe_margin_px=10,
        inside_target=edge >= 0, in_safe_zone=edge >= 10, reliable=reliable,
    )


def decision(residual=0.1):
    return control.FishDecision(
        key=control.KEY_A, duration_ms=10, action=-0.5, rule_action=-0.5 - residual,
        residual=residual, requested_residual=residual, mode="recover",
    )


def transition(session, index=0, residual=0.1):
    start = 1 + index * 0.2
    first = observation(start, index * 2 + 1)
    session.record_action(first, decision(residual), started_at=start + 0.002, finished_at=start + 0.012, held_ms=10)
    session.observe(observation(start + 0.05, index * 2 + 2, error=44))


class FishLearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "runtime"
        self.config = replace(params.FishControlConfig(), learning_mode="learn", learning_seed=7)

    def session(self, **changes):
        return learning.FishLearningSession(replace(self.config, **changes), "controller-test", self.root)

    def test_off_has_no_artifact_or_policy_side_effects(self):
        with patch.object(learning.np, "load", side_effect=AssertionError("must not load")):
            session = self.session(learning_mode="off")
            self.assertEqual(session.predict(observation()), 0)
            transition(session)
            self.assertEqual(session.round_samples, 0)
            self.assertTrue(session.finish())
        self.assertFalse(self.root.exists())

    def test_infer_missing_model_falls_back_without_writing(self):
        session = self.session(learning_mode="infer")
        self.assertFalse(session.active)
        self.assertIsNotNone(session.issue)
        self.assertEqual(session.predict(observation()), 0)
        self.assertTrue(session.finish())
        self.assertFalse(self.root.exists())

    def test_learn_updates_and_only_saves_at_finish(self):
        session = self.session()
        self.assertTrue(session.active)
        transition(session)
        self.assertEqual(session.round_samples, 1)
        self.assertEqual(session.update_count, 1)
        self.assertTrue(np.any(session.weights != 0))
        self.assertFalse(self.root.exists())
        self.assertTrue(session.finish())
        self.assertTrue(session.model_path.exists())
        rows = [json.loads(line) for line in session.history_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["type"] for row in rows], ["transition", "summary"])
        self.assertEqual(rows[0]["held_ms"], 10)
        self.assertEqual(rows[0]["schema"], learning.SCHEMA_VERSION)
        self.assertTrue(rows[0]["updated"])
        self.assertTrue(session.finish())
        self.assertEqual(len(session.history_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_inference_does_not_update_or_write(self):
        trained = self.session()
        transition(trained)
        self.assertTrue(trained.finish())
        before_model = trained.model_path.read_bytes()
        before_history = trained.history_path.read_bytes()
        session = self.session(learning_mode="infer")
        self.assertTrue(session.active)
        old = session.weights.copy()
        self.assertNotEqual(session.predict(observation()), 0)
        transition(session)
        self.assertEqual(session.round_samples, 0)
        self.assertTrue(session.finish())
        np.testing.assert_array_equal(old, session.weights)
        self.assertEqual(before_model, trained.model_path.read_bytes())
        self.assertEqual(before_history, trained.history_path.read_bytes())

    def test_seed_and_residual_bounds(self):
        one, two = self.session(), self.session()
        first = [one.predict(observation()) for _ in range(8)]
        second = [two.predict(observation()) for _ in range(8)]
        self.assertEqual(first, second)
        self.assertTrue(any(value != 0 for value in first))
        self.assertTrue(all(abs(value) <= self.config.learning_residual_limit for value in first))
        self.assertEqual(one.predict(observation(error=0)), 0)
        self.assertEqual(one.predict(observation(reliable=False)), 0)

    def test_clipped_or_silent_actions_do_not_get_learning_credit(self):
        session = self.session()
        transition(session, residual=0)
        self.assertEqual(session.round_samples, 1)
        self.assertEqual(session.update_count, 0)
        np.testing.assert_array_equal(session.weights, np.zeros(13))
        idle = control.FishDecision()
        first = observation(2, 3, error=0)
        session.record_action(first, idle, started_at=2, finished_at=2, held_ms=0)
        session.observe(observation(2.05, 4, error=0))
        self.assertEqual(session.update_count, 0)

    def test_drop_invalid_feedback_and_cross_round_pending(self):
        for next_observation in (
            observation(0.9, 2), observation(1.001, 2), observation(1.05, 1),
            observation(2.0, 2), observation(1.05, 2, reliable=False),
            replace(observation(1.05, 2), target_width=120),
            replace(observation(1.05, 2), error_px=float("nan")),
        ):
            with self.subTest(next_observation=next_observation):
                session = self.session()
                session.record_action(observation(), decision(), started_at=1, finished_at=1.01, held_ms=10)
                session.observe(next_observation)
                self.assertEqual(session.round_samples, 0)
                self.assertEqual(session.dropped_samples, 1)
        session = self.session()
        session.record_action(observation(), decision(), started_at=1, finished_at=1.01, held_ms=10)
        session.invalidate("missing")
        session.observe(observation(1.05, 2))
        self.assertEqual(session.update_count, 0)
        session.record_action(observation(2, 3), decision(), started_at=2, finished_at=2.01, held_ms=10)
        session.finish()
        self.assertIsNone(session._pending)

    def test_short_feedback_accumulates_without_attributing_another_action(self):
        session = self.session(learning_min_dt_ms=50)
        session.record_action(observation(), decision(), started_at=1, finished_at=1.01, held_ms=10)
        early = observation(1.02, 2)
        session.observe(early)
        session.record_action(early, control.FishDecision(), started_at=1.02, finished_at=1.02, held_ms=0)
        self.assertEqual(session.round_samples, 0)
        session.observe(observation(1.06, 3, error=40))
        self.assertEqual(session.round_samples, 1)
        session.record_action(observation(2, 4), decision(), started_at=2, finished_at=2.01, held_ms=10)
        session.observe(observation(2.02, 5))
        session.record_action(observation(2.02, 5), decision(), started_at=2.02, finished_at=2.03, held_ms=10)
        self.assertEqual(session._drops["overlapping_action"], 1)

    def test_buffer_is_bounded_but_weights_keep_updating(self):
        session = self.session(learning_buffer_limit=2)
        for index in range(10):
            transition(session, index)
        self.assertEqual(len(session._records), 2)
        self.assertEqual(session.round_samples, 10)
        self.assertEqual(session.dropped_samples, 8)
        self.assertTrue(np.all(np.isfinite(session.weights)))
        self.assertTrue(np.all(np.abs(session.weights) <= 5))

    def test_model_rejects_wrong_shape_nonfinite_versions_and_object_arrays(self):
        template = self.session()
        metadata = json.dumps(template.metadata, sort_keys=True)
        template.model_path.parent.mkdir(parents=True)
        good = dict(metadata=np.array(metadata), features=np.asarray(learning.FEATURE_NAMES),
                    weights=np.zeros(13), sample_count=np.int64(0), update_count=np.int64(0), reward_ema=np.float64(0))
        variants = (
            {"weights": np.zeros((13, 1))}, {"weights": np.full(13, np.nan)},
            {"weights": np.full(13, 10)}, {"weights": np.full(13, object(), dtype=object)},
            {"metadata": np.array("{}")}, {"sample_count": np.int64(-1)},
            {"reward_ema": np.float64(np.inf)}, {"features": np.asarray(list(reversed(learning.FEATURE_NAMES)))},
        )
        for invalid in variants:
            with self.subTest(keys=list(invalid)):
                np.savez(template.model_path, **(good | invalid))
                before = template.model_path.read_bytes()
                session = self.session()
                self.assertFalse(session.active)
                self.assertIsNotNone(session.issue)
                self.assertEqual(session.predict(observation()), 0)
                self.assertTrue(session.finish())
                self.assertEqual(before, template.model_path.read_bytes())

    def test_corrupt_archive_and_profile_are_isolated(self):
        template = self.session()
        template.model_path.parent.mkdir(parents=True)
        template.model_path.write_bytes(b"not npz")
        self.assertFalse(self.session().active)
        other = learning.FishLearningSession(self.config, "another controller", self.root)
        self.assertNotEqual(template.model_path, other.model_path)
        self.assertTrue(other.active)

    def test_save_failure_preserves_last_valid_model(self):
        template = self.session()
        transition(template)
        self.assertTrue(template.finish())
        before = template.model_path.read_bytes()
        session = self.session()
        transition(session)
        with patch.object(learning.os, "replace", side_effect=OSError("write failure")):
            self.assertFalse(session.finish())
        self.assertEqual(before, template.model_path.read_bytes())
        self.assertEqual(list(template.model_path.parent.glob("*.tmp")), [])

    def test_read_only_directory_is_nonfatal(self):
        session = self.session()
        transition(session)
        with patch.object(Path, "mkdir", side_effect=PermissionError()):
            self.assertFalse(session.finish())
        self.assertIsNotNone(session.issue)

    def test_runtime_nan_policy_falls_back(self):
        session = self.session()
        session.weights[0] = float("nan")
        self.assertEqual(session.predict(observation()), 0)
        self.assertFalse(session.active)


if __name__ == "__main__":
    unittest.main()
