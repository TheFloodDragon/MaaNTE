"""Optional integration checks in a separate native-framework process."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def native_suite():
    import numpy as np
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker

    class NativeCases(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            class VirtualController(CustomController):
                def connect(self):
                    return True

                def request_uuid(self):
                    return "task-control-integration"

                def screencap(self):
                    return np.zeros((720, 1280, 3), dtype=np.uint8)

            cls.controller = VirtualController()
            if not cls.controller.post_connection().wait().succeeded:
                raise RuntimeError("Virtual controller connection failed")
            cls.resource = Resource()
            spec = importlib.util.spec_from_file_location(
                "_pipeline_fail_action_test",
                ROOT / "agent/custom/action/Common/task_result.py",
            )
            module = importlib.util.module_from_spec(spec)
            # Importing maa.agent switches the library into server mode.
            # Register the production action with the local Resource instead.
            server = ModuleType("maa.agent.agent_server")
            server.AgentServer = SimpleNamespace(
                custom_action=lambda _: lambda action: action
            )
            with patch.dict(
                sys.modules,
                {
                    "maa.agent": ModuleType("maa.agent"),
                    "maa.agent.agent_server": server,
                },
            ):
                spec.loader.exec_module(module)
            cls.fail_action = module.FailTask()
            if not cls.resource.register_custom_action("fail_task", cls.fail_action):
                raise RuntimeError("Could not register the production failure action")
            cls.temp = tempfile.TemporaryDirectory()
            pipeline = {
                "Parent": {"next": ["[JumpBack]Child"]},
                "Child": {"next": ["Finish"]},
                "Finish": {"action": "StopTask"},
                "Tail": {},
                "Failure": {"action": "Custom", "custom_action": "fail_task"},
                "RecoveredFailure": {
                    "on_error": ["Failure"],
                    "action": "Custom",
                    "custom_action": "fail_task",
                },
            }
            pipeline = {
                name: {"rate_limit": 0, "pre_delay": 0, "post_delay": 0, **node}
                for name, node in pipeline.items()
            }
            path = Path(cls.temp.name) / "pipeline.json"
            path.write_text(json.dumps(pipeline), encoding="utf-8")
            if not cls.resource.post_pipeline(path).wait().succeeded:
                raise RuntimeError("Could not load integration pipeline")
            cls.tasker = Tasker()
            if not cls.tasker.bind(cls.resource, cls.controller):
                raise RuntimeError("Could not bind virtual tasker")

        @classmethod
        def tearDownClass(cls):
            cls.temp.cleanup()

        def test_stop_task_clears_jump_back_and_preserves_queue(self):
            first = self.tasker.post_task("Parent")
            queued = self.tasker.post_task("Tail")
            self.assertTrue(first.wait().succeeded)
            self.assertTrue(queued.wait().succeeded)
            self.assertFalse(self.tasker.stopping)

        def test_explicit_failure_stays_failed_after_error_branch(self):
            first = self.tasker.post_task("RecoveredFailure")
            queued = self.tasker.post_task("Tail")
            self.assertTrue(first.wait().failed)
            self.assertTrue(queued.wait().succeeded)
            self.assertFalse(self.tasker.stopping)

        def test_repository_pipeline_and_failure_exits(self):
            resource = Resource()
            self.assertTrue(
                resource.post_pipeline(ROOT / "assets/resource/base/pipeline")
                .wait()
                .succeeded
            )
            self.assertIsNotNone(resource.get_node_data("__RealTimeTaskAction_Holder"))
            self.assertIsNotNone(resource.get_node_data("RealTimeSleep"))
            self.assertTrue(
                resource.register_custom_action("fail_task", self.fail_action)
            )
            tasker = Tasker()
            self.assertTrue(tasker.bind(resource, self.controller))
            for entry in ("FountainCheckinNotFound", "TetrisExitWithError"):
                with self.subTest(entry=entry):
                    self.assertTrue(tasker.post_task(entry).wait().failed)
                    self.assertFalse(tasker.stopping)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeCases)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


class PipelineControlIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("maa"), "MaaFramework runtime is not installed"
    )
    def test_native_pipeline_controls(self):
        # Agent unit tests import MaaAgentServer. A separate process avoids
        # mixing its native library handles with local Tasker/Resource handles.
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--native"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PipelineConfigurationTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("jsonschema"), "jsonschema is not installed"
    )
    def test_changed_pipeline_and_task_files_match_local_schemas(self):
        import re
        from jsonschema.validators import validator_for
        from referencing import Registry, Resource

        schema_dir = ROOT / "deps/tools"
        registry = Registry().with_resources(
            (
                path.as_uri(),
                Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))),
            )
            for path in schema_dir.glob("*.schema.json")
        )
        files = [
            "assets/resource/base/pipeline/FountainCheckin.json",
            "assets/resource/base/pipeline/Tetris/Tetris.json",
            "assets/resource/base/pipeline/PukaLand/PukaLandLuckyStar.json",
            "assets/resource/base/pipeline/realtime_assist.json",
            "assets/resource/base/pipeline/realtime_assist/RealTimeSkipStory.json",
            "assets/resource/tasks/Tetris.json",
        ]
        comment_pattern = r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\r\n]*'
        for relative in files:
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                text = re.sub(
                    comment_pattern,
                    lambda match: match[0] if match[0].startswith('"') else "",
                    text,
                )
                data = json.loads(text)
                schema_name = (
                    "interface_import.schema.json"
                    if "/tasks/" in relative
                    else "pipeline.schema.json"
                )
                schema_path = schema_dir / schema_name
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                schema["$id"] = schema_path.as_uri()
                validator_for(schema)(schema, registry=registry).validate(data)

    def test_task_options_preserve_failure_routing(self):
        data = json.loads(
            (ROOT / "assets/resource/tasks/Tetris.json").read_text(encoding="utf-8")
        )
        overrides = []

        def collect(value):
            if isinstance(value, dict):
                override = value.get("pipeline_override", {}).get(
                    "TetrisHandleGameActive", {}
                )
                if "on_error" in override:
                    overrides.append(override["on_error"])
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(data)
        self.assertEqual(overrides, [["TetrisExitWithError"], ["TetrisExitWithError"]])


if __name__ == "__main__":
    if "--native" in sys.argv:
        sys.exit(0 if native_suite() else 1)
    unittest.main()
