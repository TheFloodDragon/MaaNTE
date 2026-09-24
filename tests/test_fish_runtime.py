from dataclasses import dataclass, replace
import sys
import types
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from fish_test_support import load_fish_module
from test_fish_vision import frame

params = load_fish_module("fish_params")
control = load_fish_module("fish_control")


@dataclass
class Profile:
    name: str = "test"
    type: str = "Win32"


class FakeAction:
    RunArg = object

    @dataclass
    class RunResult:
        success: bool


class FakeRecognition:
    AnalyzeArg = object

    @dataclass
    class AnalyzeResult:
        box: list
        detail: dict


def load_runtime():
    stubs = {name: types.ModuleType(name) for name in (
        "maa", "maa.agent", "maa.agent.agent_server", "maa.context",
        "maa.custom_action", "maa.custom_recognition", "utils", "utils.pienv",
        "utils.logger", "utils.maafocus",
    )}
    registrations = {}

    def register(name):
        def decorate(cls):
            registrations[name] = cls
            return cls
        return decorate

    stubs["maa.agent.agent_server"].AgentServer = types.SimpleNamespace(custom_action=register, custom_recognition=register)
    stubs["maa.context"].Context = object
    stubs["maa.custom_action"].CustomAction = FakeAction
    stubs["maa.custom_recognition"].CustomRecognition = FakeRecognition
    stubs["utils.logger"].logger = Mock()
    stubs["utils.maafocus"].PrintT = Mock()
    stubs["utils.pienv"].controller = Mock(return_value=Profile())
    with patch.dict(sys.modules, stubs):
        runtime = load_fish_module("auto_fish_withoutCV")
    return runtime, registrations


runtime, registrations = load_runtime()


class Clock:
    def __init__(self):
        self.now = 1.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class Job:
    def __init__(self, callback=None, succeeded=True):
        self.callback = callback
        self.succeeded = succeeded
        self.waited = False

    def wait(self):
        if not self.waited and self.callback:
            self.callback()
        self.waited = True
        return self


class Controller:
    def __init__(self, clock, frames):
        self.clock = clock
        self.frames = frames
        self.frame_index = 0
        self.keys = set()
        self.events = []
        self.cache_reads = 0
        self.image = None
        self.capture_ms = 30
        self.fail_capture = False
        self.fail_down = False
        self.fail_up = False
        self.raise_down = False
        self.stop_on_down = False
        self.stop_on_capture = False
        self.stopped = False
        self.down_count = 0

    def post_screencap(self):
        if self.keys:
            raise AssertionError("captured while a key was held")
        if self.frame_index > 200:
            raise AssertionError("unbounded runtime in test")
        self.events.append(("capture", self.clock()))

        def capture():
            self.clock.now += self.capture_ms / 1000
            self.image = self.frames[min(self.frame_index, len(self.frames) - 1)]
            self.frame_index += 1
            if self.stop_on_capture:
                self.stopped = True

        return Job(capture, succeeded=not self.fail_capture)

    @property
    def cached_image(self):
        self.cache_reads += 1
        if self.fail_capture:
            raise AssertionError("read stale screenshot after failed job")
        return self.image

    def post_key_down(self, key):
        self.events.append(("down", key, self.clock()))
        if self.raise_down:
            self.keys.add(key)
            raise RuntimeError("simulated partial input")

        def press():
            if self.keys:
                raise AssertionError("simultaneous control keys")
            self.keys.add(key)
            self.down_count += 1
            if self.stop_on_down:
                self.stopped = True

        return Job(press, succeeded=not self.fail_down)

    def post_key_up(self, key):
        self.events.append(("up", key, self.clock()))
        succeeds = not (self.fail_up and self.down_count)
        return Job(lambda: self.keys.discard(key) if succeeds else None, succeeded=succeeds)


class Tasker:
    def __init__(self, controller):
        self.controller = controller

    @property
    def stopping(self):
        return self.controller.stopped


class Learner:
    def __init__(self, controller):
        self.controller = controller
        self.issue = None
        self.records = []
        self.invalidations = []
        self.finished = False
        self.raise_finish = False

    def observe(self, observation):
        self.controller.events.append(("observe", observation.frame_id))

    def predict(self, observation):
        return 0.0

    def record_action(self, observation, decision, **execution):
        if self.controller.keys:
            raise AssertionError("learned before release")
        self.records.append((observation, decision, execution))

    def invalidate(self, reason):
        self.invalidations.append(reason)

    def finish(self):
        if self.controller.keys:
            raise AssertionError("saved while key held")
        self.controller.events.append(("finish",))
        self.finished = True
        if self.raise_finish:
            raise OSError("disk error")
        return True


class FishRuntimeTests(unittest.TestCase):
    def make_runtime(self, frames=None, **settings):
        clock = Clock()
        controller = Controller(clock, frames or [frame(cursor=650), frame(cursor=650), frame(cursor=630), np.zeros((720, 1280, 3), dtype=np.uint8)])
        for name, value in settings.items():
            setattr(controller, name, value)
        context = types.SimpleNamespace(tasker=Tasker(controller))
        run = runtime._FishingRuntime(context, params.FishControlConfig(), clock=clock, sleep=clock.sleep)
        learner = Learner(controller)
        return run, controller, clock, learner

    def run_with_learner(self, run, learner):
        with patch.object(runtime, "FishLearningSession", return_value=learner):
            return run.run()

    def assert_released(self, controller):
        self.assertEqual(controller.keys, set())
        final_ups = [event[1] for event in controller.events[-3:] if event[0] == "up"]
        self.assertEqual(final_ups, [65, 68])

    def test_normal_end_returns_to_pipeline_without_claiming_catch(self):
        run, controller, clock, learner = self.make_runtime()
        self.assertTrue(self.run_with_learner(run, learner))
        self.assertGreater(controller.down_count, 0)
        self.assertTrue(learner.finished)
        self.assert_released(controller)
        for _, decision, execution in learner.records:
            self.assertLessEqual(execution["held_ms"], 70.001)
            self.assertGreaterEqual(execution["finished_at"], execution["started_at"])
        self.assertTrue(all(value <= 0.005 for value in clock.sleeps))
        first_down = next(index for index, event in enumerate(controller.events) if event[0] == "down")
        self.assertGreaterEqual(sum(event[0] == "capture" for event in controller.events[:first_down]), 2)

    def test_stopping_during_pulse_releases_keys_before_exit(self):
        run, controller, _, learner = self.make_runtime(stop_on_down=True)
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertTrue(learner.finished)
        self.assert_released(controller)
        self.assertTrue(all(record[1].key is None for record in learner.records))

    def test_stop_after_capture_does_not_use_frame_or_press(self):
        run, controller, _, learner = self.make_runtime(stop_on_capture=True)
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertEqual(controller.cache_reads, 0)
        self.assertEqual(controller.down_count, 0)
        self.assert_released(controller)

    def test_stop_before_start_has_no_capture(self):
        run, controller, _, learner = self.make_runtime(stopped=True)
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertEqual(controller.frame_index, 0)
        self.assertEqual(controller.keys, set())

    def test_failed_capture_never_uses_stale_cache(self):
        run, controller, _, learner = self.make_runtime(fail_capture=True)
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertEqual(controller.cache_reads, 0)
        self.assertEqual(controller.down_count, 0)
        self.assert_released(controller)

    def test_partial_key_down_failure_has_emergency_release(self):
        for settings in ({"fail_down": True}, {"raise_down": True}):
            with self.subTest(settings=settings):
                run, controller, _, learner = self.make_runtime(**settings)
                self.assertFalse(self.run_with_learner(run, learner))
                self.assert_released(controller)
                self.assertTrue(all(record[1].key is None for record in learner.records))

    def test_key_up_failure_attempts_both_keys_and_skips_save(self):
        run, controller, _, learner = self.make_runtime(fail_up=True)
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertEqual(controller.down_count, 1)
        self.assertFalse(learner.finished)
        self.assertEqual([event[1] for event in controller.events[-2:]], [65, 68])

    def test_persistent_missing_target_cannot_use_old_green_box(self):
        missing = frame(cursor=660)
        missing[45:55, 560:640] = 0
        run, controller, _, learner = self.make_runtime([frame(cursor=650), frame(cursor=650), missing])
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertLessEqual(controller.down_count, 1)
        self.assertIn("missing_detection", learner.invalidations)
        self.assert_released(controller)

    def test_never_seen_control_does_not_report_normal_completion(self):
        run, controller, _, learner = self.make_runtime([np.zeros((720, 1280, 3), dtype=np.uint8)])
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertEqual(controller.down_count, 0)
        self.assert_released(controller)

    def test_duplicate_frame_after_input_is_not_blindly_repeated(self):
        run, controller, _, learner = self.make_runtime([frame(cursor=650)])
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertEqual(controller.down_count, 1)
        self.assertIn("duplicate_frame", learner.invalidations)
        self.assert_released(controller)

    def test_storage_exception_does_not_change_normal_handoff(self):
        run, controller, _, learner = self.make_runtime()
        learner.raise_finish = True
        self.assertTrue(self.run_with_learner(run, learner))
        self.assert_released(controller)

    def test_invalid_aspect_ratio_releases_and_fails(self):
        run, controller, _, learner = self.make_runtime([np.zeros((768, 1024, 3), dtype=np.uint8)])
        self.assertFalse(self.run_with_learner(run, learner))
        self.assertEqual(controller.down_count, 0)
        self.assert_released(controller)

    def test_input_hard_cap_is_independent_of_configuration(self):
        run, controller, clock, _ = self.make_runtime()
        started, finished, held = run.input.execute(control.FishDecision(key=65, duration_ms=10000, action=-1))
        self.assertLessEqual(held, 70.001)
        self.assertAlmostEqual(finished - started, 0.07)
        self.assertEqual(controller.keys, set())

    def test_recognition_shares_detection_and_maps_to_input_size(self):
        recognizer = runtime.FishControlVisible()
        _, controller, _, _ = self.make_runtime()
        context = types.SimpleNamespace(tasker=Tasker(controller))
        image = cv2.resize(frame(), (1920, 1080), interpolation=cv2.INTER_NEAREST)
        result = recognizer.analyze(context, types.SimpleNamespace(image=image))
        self.assertIsNotNone(result)
        self.assertLessEqual(abs(result.box[0] - 900), 2)
        self.assertEqual(result.detail["reference_size"], [1280, 720])
        self.assertIsNone(recognizer.analyze(context, types.SimpleNamespace(image=np.zeros_like(image))))
        self.assertIn("fish_control_visible", registrations)
        self.assertIn("auto_fish_without_cv", registrations)


if __name__ == "__main__":
    unittest.main()
