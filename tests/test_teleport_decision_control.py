import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TeleportDecisionControlTests(unittest.TestCase):
    def setUp(self):
        logger = ModuleType("utils.logger")
        logger.logger = Mock()
        server = ModuleType("maa.agent.agent_server")
        server.AgentServer = SimpleNamespace(custom_action=lambda _: lambda cls: cls)
        self.modules = patch.dict(
            sys.modules,
            {
                "utils.logger": logger,
                "maa.agent": ModuleType("maa.agent"),
                "maa.agent.agent_server": server,
            },
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)
        load_module("utils.task_control", "agent/utils/task_control.py")
        self.module = load_module(
            "_teleport_decision_control_test",
            "agent/custom/action/MapTeleport/check_teleport_required.py",
        )
        self.provider = Mock()
        self.provider.uses_visual_positioning.return_value = False
        self.provider.locate.return_value = SimpleNamespace(found=False, point=None)
        self.factory = patch.object(
            self.module,
            "load_coordinate_position_provider_class",
            return_value=Mock(return_value=self.provider),
        ).start()
        self.addCleanup(patch.stopall)
        self.visual = patch.object(self.module, "visual_locate").start()

    def test_cancel_before_lookup_does_not_initialize_provider(self):
        self.assertIsNone(
            self.module.locate_current_position(should_cancel=lambda: True)
        )
        self.factory.assert_not_called()

    def test_cancel_during_lookup_rejects_valid_pose_and_closes_provider(self):
        stopped = [False]

        def locate(*_):
            stopped[0] = True
            return SimpleNamespace(found=True, point=(1, 2))

        self.provider.locate.side_effect = locate
        self.assertIsNone(
            self.module.locate_current_position(should_cancel=lambda: stopped[0])
        )
        self.provider.close.assert_called_once()
        self.visual.assert_not_called()

    def test_cancel_during_wait_does_not_fallback_to_visual_positioning(self):
        stopped = [False]

        def sleep(_):
            stopped[0] = True

        with patch.object(self.module.time, "sleep", side_effect=sleep):
            self.assertIsNone(
                self.module.locate_current_position(
                    coordinate_timeout=60, should_cancel=lambda: stopped[0]
                )
            )
        self.assertEqual(self.provider.locate.call_count, 1)
        self.provider.close.assert_called_once()
        self.visual.assert_not_called()

    def test_missing_pose_retries_only_sampling_within_total_budget(self):
        elapsed = [0.0]
        with patch.object(
            self.module.time, "monotonic", side_effect=lambda: elapsed[0]
        ):
            with patch.object(
                self.module.time,
                "sleep",
                side_effect=lambda seconds: elapsed.__setitem__(
                    0, elapsed[0] + seconds
                ),
            ):
                self.assertIsNone(
                    self.module.locate_current_position(
                        position_backend="coordinate",
                        coordinate_timeout=0.12,
                        coordinate_interval=0.05,
                    )
                )
        self.assertAlmostEqual(elapsed[0], 0.12)
        self.assertEqual(self.provider.locate.call_count, 4)
        self.provider.close.assert_called_once()

    def test_invalid_wait_configuration_is_rejected_before_provider(self):
        for params in (
            {"coordinate_timeout": -1},
            {"coordinate_timeout": float("nan")},
            {"coordinate_timeout": float("inf")},
            {"coordinate_interval": 0},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.module.locate_current_position(**params)
        self.factory.assert_not_called()

    def test_successful_pose_returns_without_wait_or_fallback(self):
        pose = SimpleNamespace(found=True, point=(1, 2))
        self.provider.locate.return_value = pose
        self.assertIs(self.module.locate_current_position(), pose)
        self.provider.close.assert_called_once()
        self.visual.assert_not_called()


if __name__ == "__main__":
    unittest.main()
