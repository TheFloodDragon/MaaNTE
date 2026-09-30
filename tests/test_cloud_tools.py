"""云诊断工具回归；窗口、输入和子进程全部使用替身。"""

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def job(success=True):
    result = Mock(succeeded=success, failed=not success, job_id=17)
    result.wait.return_value = result
    return result


class CloudToolsTests(unittest.TestCase):
    def setUp(self):
        self.live = importlib.import_module("tools.cloud_live_run")
        self.assertTrue(hasattr(self.live, "ToolError"), "先补工具失败契约")
        self.diag = Mock(path=ROOT / ".narrafork" / "mock-run", run_id="a" * 32)
        self.api = SimpleNamespace(
            Resource=Mock(), Tasker=Mock(), Win32Controller=Mock(),
            AgentClient=Mock(), CustomAction=type("Action", (), {"RunResult": staticmethod(lambda success: SimpleNamespace(success=success))}),
            Input=SimpleNamespace(Null=0, Seize=1), Screencap=SimpleNamespace(PrintWindow=1),
            LoggingLevel=SimpleNamespace(Off=0),
        )
        self.controller = self.api.Win32Controller.return_value
        self.controller.post_connection.return_value = job()
        self.resource = self.api.Resource.return_value
        self.resource.post_bundle.return_value = job()
        self.tasker = self.api.Tasker.return_value
        self.tasker.post_task.return_value = job()
        self.cloud = Mock()
        self.cloud.client_windows.return_value = [SimpleNamespace(hwnd=42)]
        self.cloud.prepare_client_window.return_value = True
        self.client = self.api.AgentClient.create_tcp.return_value
        self.client.identifier = "PRIVATE_IDENTIFIER"
        self.proc = Mock()
        self.proc.wait.return_value = 0
        self.proc.poll.return_value = None

    @contextlib.contextmanager
    def framework(self):
        with patch.object(self.live, "load_framework", return_value=self.api), patch.object(
            self.live, "load_cloud_start", return_value=self.cloud
        ):
            yield

    def build(self, readonly=False):
        with self.framework():
            return self.live.build_framework(recog_only=readonly, diagnostics=self.diag)

    def test_real_task_job_status_to_exit_code(self):
        from maa.job import TaskJob
        from maa.define import MaaStatusEnum
        for status, expected in [(MaaStatusEnum.succeeded, 0), (MaaStatusEnum.failed, 1), (MaaStatusEnum.invalid, 1), (MaaStatusEnum.running, 1)]:
            with self.subTest(status=status):
                task = TaskJob(17, lambda _: status, lambda _: status, lambda _: None, lambda *a: True)
                self.assertEqual(self.live.task_exit_code(task.wait()), expected)

    def test_readonly_does_not_prepare_or_enable_input(self):
        self.build(readonly=True)
        self.cloud.prepare_client_window.assert_not_called()
        self.assertEqual(self.api.Win32Controller.call_args.kwargs["mouse_method"], 0)
        self.assertEqual(self.api.Win32Controller.call_args.kwargs["keyboard_method"], 0)

    def test_normal_build_keeps_window_preparation(self):
        self.build()
        self.cloud.prepare_client_window.assert_called_once_with(42)

    def test_build_checks_every_prerequisite(self):
        failures = [
            (self.cloud.client_windows, []),
            (self.cloud.prepare_client_window, False),
            (self.controller.post_connection, job(False)),
            (self.resource.post_bundle, job(False)),
            (self.resource.register_custom_action, False),
            (self.tasker.bind, False),
        ]
        for method, value in failures:
            with self.subTest(method=method):
                old = method.return_value
                method.return_value = value
                try:
                    with self.assertRaises(self.live.ToolError):
                        self.build()
                finally:
                    method.return_value = old
        self.tasker.inited = False
        with self.assertRaises(self.live.ToolError):
            self.build()

    def test_probe_override_failure_never_posts_task(self):
        self.resource.override_pipeline.return_value = False
        with self.assertRaises(self.live.ToolError):
            self.live.recog_only(self.tasker, self.resource)
        self.tasker.post_task.assert_not_called()

    def test_probe_returns_actual_job(self):
        result = self.live.recog_only(self.tasker, self.resource)
        self.assertIs(result, self.tasker.post_task.return_value)

    def test_probe_capture_failure_is_not_success(self):
        probe = self.live.make_probe(self.api, self.diag)
        self.controller.post_screencap.return_value = job(False)
        context = Mock(tasker=SimpleNamespace(controller=self.controller))
        self.assertFalse(probe.run(context, None).success)
        context.run_recognition.assert_not_called()

    def test_probe_does_not_save_by_default(self):
        import numpy as np
        self.controller.post_screencap.return_value = job()
        self.controller.cached_image = np.zeros((720, 1280, 3), dtype=np.uint8)
        probe = self.live.make_probe(self.api, self.diag)
        with patch.object(self.live, "save_frame") as save:
            self.assertTrue(probe.run(Mock(tasker=SimpleNamespace(controller=self.controller)), None).success)
        save.assert_not_called()

    def test_probe_save_requires_opt_in(self):
        import numpy as np
        self.controller.post_screencap.return_value = job()
        self.controller.cached_image = np.zeros((720, 1280, 3), dtype=np.uint8)
        probe = self.live.make_probe(self.api, self.diag, save=True)
        with patch.object(self.live, "save_frame") as save:
            self.assertTrue(probe.run(Mock(tasker=SimpleNamespace(controller=self.controller)), None).success)
        save.assert_called_once()

    def test_graceful_cleanup_avoids_termination(self):
        self.assertTrue(self.live.close_agent(self.client, self.proc, self.diag))
        self.client.disconnect.assert_called_once()
        self.proc.wait.assert_called_once()
        self.proc.terminate.assert_not_called()
        self.proc.kill.assert_not_called()

    def test_cleanup_escalates_and_reaps(self):
        self.proc.wait.side_effect = [subprocess.TimeoutExpired("PRIVATE_PATH", 1), subprocess.TimeoutExpired("PRIVATE_PATH", 1), 0]
        self.assertTrue(self.live.close_agent(self.client, self.proc, self.diag))
        self.proc.terminate.assert_called_once()
        self.proc.kill.assert_called_once()
        self.assertEqual(self.proc.wait.call_count, 3)
        for call in self.proc.wait.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)

    def test_cleanup_survives_interrupt_and_terminate_error(self):
        self.client.disconnect.side_effect = KeyboardInterrupt
        self.proc.wait.side_effect = [subprocess.TimeoutExpired("x", 1), 0]
        self.proc.terminate.side_effect = OSError("PRIVATE_PATH")
        self.assertTrue(self.live.close_agent(self.client, self.proc, self.diag))
        self.proc.kill.assert_called_once()

    def test_cleanup_is_bounded_when_process_cannot_be_reaped(self):
        self.proc.wait.side_effect = subprocess.TimeoutExpired("PRIVATE_PATH", 1)
        self.assertFalse(self.live.close_agent(self.client, self.proc, self.diag))
        self.assertEqual(self.proc.wait.call_count, 3)

    def test_connect_failure_and_interrupt_reap_created_child(self):
        for failure in [False, RuntimeError("PRIVATE_PATH"), KeyboardInterrupt()]:
            with self.subTest(failure=type(failure).__name__), self.framework(), patch.object(
                self.live.subprocess, "Popen", return_value=self.proc
            ), patch.object(self.live, "close_agent", return_value=True) as close:
                self.client.connect.side_effect = failure if isinstance(failure, BaseException) else None
                self.client.connect.return_value = failure
                with self.assertRaises((self.live.ToolError, RuntimeError, KeyboardInterrupt)):
                    self.live.start_agent(self.resource, diagnostics=self.diag)
                close.assert_called_once_with(self.client, self.proc, self.diag)

    def test_agent_waits_for_imports_before_connecting_once(self):
        order = []
        self.diag.wait_agent_bootstrap.side_effect = lambda proc: order.append("bootstrap")
        self.client.connect.side_effect = lambda: order.append("connect") or True
        with self.framework(), patch.object(self.live.subprocess, "Popen", return_value=self.proc):
            self.live.start_agent(self.resource, diagnostics=self.diag)
        self.assertEqual(order, ["bootstrap", "connect"])
        self.client.connect.assert_called_once()


    def test_bootstrap_wait_allows_imports_longer_than_transport_timeout(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            diag = self.live.Diagnostics("BootstrapTest", root=directory, fingerprints=False)
            stack.callback(diag.close)
            clock = [0.0]
            with patch.object(self.live.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                self.live.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)
            ), patch.object(Path, "is_file", side_effect=lambda: clock[0] >= 6.0):
                diag.wait_agent_bootstrap(self.proc)
            self.assertGreaterEqual(clock[0], 6)
            self.assertLess(clock[0], 7)


    def test_bootstrap_wait_fails_on_exit_or_deadline(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            diag = self.live.Diagnostics("BootstrapTest", root=directory, fingerprints=False)
            stack.callback(diag.close)
            self.proc.poll.return_value = 1
            with self.assertRaisesRegex(self.live.ToolError, "agent_bootstrap_exit"):
                diag.wait_agent_bootstrap(self.proc)
            self.proc.poll.return_value = None
            clock = [0.0]
            with patch.object(self.live.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                self.live.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)
            ), self.assertRaisesRegex(self.live.ToolError, "agent_bootstrap_timeout"):
                diag.wait_agent_bootstrap(self.proc, timeout=0.2)


    def test_bootstrap_marker_has_no_connection_payload(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            diag = self.live.Diagnostics("AgentServer", root=directory, agent=True, fingerprints=False)
            stack.callback(diag.close)
            diag.mark_agent_bootstrap_ready()
            self.assertEqual((diag.path / "bootstrap.ready").read_bytes(), b"")


    def test_agent_bind_failure_never_spawns(self):
        self.client.bind.return_value = False
        with self.framework(), patch.object(self.live.subprocess, "Popen") as spawn:
            with self.assertRaises(self.live.ToolError):
                self.live.start_agent(self.resource, diagnostics=self.diag)
            spawn.assert_not_called()

    def test_agent_transport_checks_connected_and_alive(self):
        for field in ["connected", "alive"]:
            with self.subTest(field=field), self.framework(), patch.object(self.live.subprocess, "Popen", return_value=self.proc):
                setattr(self.client, field, False)
                with self.assertRaises(self.live.ToolError):
                    self.live.start_agent(self.resource, diagnostics=self.diag)
                setattr(self.client, field, True)

    def test_spawn_captures_no_raw_agent_output(self):
        with self.framework(), patch.object(self.live.subprocess, "Popen", return_value=self.proc) as spawn:
            self.live.start_agent(self.resource, diagnostics=self.diag)
        self.assertEqual(spawn.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["env"]["CLOUD_TOOLS_RUN_ID"], "a" * 32)

    @contextlib.contextmanager
    def main_mocks(self):
        with patch.object(self.live, "Diagnostics", return_value=self.diag), patch.object(
            self.live, "build_framework", return_value=(self.tasker, self.resource, self.controller)
        ), patch.object(self.live, "start_agent", return_value=(self.client, self.proc)), patch.object(
            self.live, "close_agent", return_value=True
        ) as close:
            yield close

    def test_main_propagates_failure_and_success(self):
        for success, code in [(False, 1), (True, 0)]:
            self.tasker.post_task.return_value = job(success)
            with self.main_mocks() as close:
                self.assertEqual(self.live.main([]), code)
                close.assert_called_once()

    def test_main_interrupted_returns_130_and_cleans_up(self):
        self.tasker.post_task.return_value.wait.side_effect = KeyboardInterrupt
        with self.main_mocks() as close:
            self.assertEqual(self.live.main([]), 130)
            close.assert_called_once()
            self.tasker.post_stop.assert_called_once()

    def test_main_calibration_failure_stops_before_agent(self):
        self.resource.override_pipeline.return_value = False
        with self.main_mocks(), patch.object(self.live, "start_agent") as start:
            self.assertEqual(self.live.main(["--calibrate"]), 1)
            start.assert_not_called()

    def test_main_recog_only_does_not_start_agent(self):
        with self.main_mocks(), patch.object(self.live, "start_agent") as start:
            self.assertEqual(self.live.main(["--recog-only"]), 0)
            start.assert_not_called()

    def test_main_error_does_not_echo_exception_text(self):
        with self.main_mocks(), patch.object(self.live, "build_framework", side_effect=RuntimeError("PRIVATE_PATH PRIVATE_IDENTIFIER")):
            self.assertEqual(self.live.main([]), 1)
        self.assertNotIn("PRIVATE", str(self.diag.event.call_args_list))

    def test_bad_cli_value_is_not_echoed(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(self.live.main(["--entry", "PRIVATE/PATH"]), 2)
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_diagnostic_options_are_set_once_and_images_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            diag = self.live.Diagnostics("LiveProbe", root=Path(directory), fingerprints=False)
            try:
                with patch.object(self.live, "_framework_log_path", None):
                    diag.configure_framework(self.api)
                    diag.configure_framework(self.api)
                self.api.Tasker.set_log_dir.assert_called_once()
                self.api.Tasker.set_save_draw.assert_called_once_with(False)
                self.api.Tasker.set_save_on_error.assert_called_once_with(False)
                self.api.Tasker.set_debug_mode.assert_called_once_with(False)
            finally:
                diag.close()

    def test_fingerprint_is_order_independent_and_content_sensitive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "one", root / "two"
            first.write_text("one")
            second.write_text("two")
            digest = self.live.fingerprint([first, second], root)
            self.assertEqual(digest, self.live.fingerprint([second, first], root))
            second.write_text("changed")
            self.assertNotEqual(digest, self.live.fingerprint([first, second], root))

    def _fingerprint_fixture(self, root):
        paths = list(self.live.FINGERPRINT_CODE_FILES) + list(self.live.FINGERPRINT_RESOURCE_FILES)
        paths += [
            "assets/resource/base/pipeline/CloudGame/CloudGame.json",
            "assets/resource/base/image/CloudGame/Confirm.png",
            "assets/resource/base/pipeline/SceneManager/SceneWorld.json",
            "assets/resource/base/pipeline/Interface/Scene/Status.json",
        ]
        for relative in paths:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"scoped-content")
        return paths

    def test_cloud_startup_fingerprint_never_scans_or_opens_unrelated_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fingerprint_fixture(root)
            unrelated = [
                root / "assets/resource/base/model/ocr/model.onnx",
                root / "assets/resource/base/image/Fish/template.png",
                root / "agent/custom/action/unrelated.py",
            ]
            for path in unrelated:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"not-cloud-startup")
            allowed_roots = {
                root / "assets/resource/base/pipeline/CloudGame",
                root / "assets/resource/base/image/CloudGame",
                root / "assets/resource/base/pipeline/SceneManager",
                root / "assets/resource/base/pipeline/Interface/Scene",
            }
            original_glob, original_open = Path.rglob, Path.open
            scanned, opened = [], []

            def scoped_glob(path, pattern, *args, **kwargs):
                self.assertIn(path, allowed_roots)
                scanned.append(path)
                return original_glob(path, pattern, *args, **kwargs)

            def scoped_open(path, *args, **kwargs):
                self.assertNotIn(path, unrelated)
                opened.append(path)
                return original_open(path, *args, **kwargs)

            with patch.object(self.live, "ROOT", root), patch.object(Path, "rglob", scoped_glob), patch.object(
                Path, "open", scoped_open
            ), patch.object(self.live.Diagnostics, "event") as event:
                diag = self.live.Diagnostics("CloudGameStartEntrance", root=root / "diagnostics")
                diag.close()
            self.assertEqual(set(scanned), allowed_roots)
            self.assertIn(root / "assets/resource/base/image/CloudGame/Confirm.png", opened)
            fingerprint_event = next(call for call in event.call_args_list if call.args[0] == "fingerprint")
            self.assertEqual(fingerprint_event.kwargs["fingerprint_scope"], "cloud_startup")

    def test_cloud_startup_fingerprint_includes_untracked_cloud_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fingerprint_fixture(root)
            self.assertFalse((root / ".git").exists())

            def resource_digest():
                _, resources = self.live.cloud_startup_files(root)
                return self.live.fingerprint(resources, root)

            before = resource_digest()
            fresh = root / "assets/resource/base/image/CloudGame/Untracked.png"
            fresh.write_bytes(b"new-cloud-template")
            added = resource_digest()
            self.assertNotEqual(before, added)
            fresh.write_bytes(b"modified-cloud-template")
            self.assertNotEqual(added, resource_digest())
            unrelated = root / "assets/resource/base/model/unrelated.onnx"
            unrelated.parent.mkdir(parents=True, exist_ok=True)
            stable = resource_digest()
            unrelated.write_bytes(b"unrelated-model")
            self.assertEqual(stable, resource_digest())

    def test_cloud_startup_fingerprint_covers_code_and_locales(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fingerprint_fixture(root)
            code, resources = self.live.cloud_startup_files(root)
            for relative in ("agent/custom/action/cloud_game.py", "agent/cloud_start.py", "agent/utils/cloud_window.py",
                             "agent/utils/logger.py", "agent/utils/i18n.py", "requirements.txt", "tools/cloud_live_run.py"):
                self.assertIn(root / relative, code)
            original = self.live.fingerprint(resources, root)
            locale = root / "assets/resource/locales/agent/zh_cn.json"
            self.assertIn(locale, resources)
            locale.write_bytes(b"updated-cloud-copy")
            self.assertNotEqual(original, self.live.fingerprint(resources, root))

    def test_import_image_has_no_argv_env_or_window_side_effect(self):
        old_env, old_path = dict(os.environ), list(sys.path)
        with patch.object(sys, "argv", ["tool"]), patch.object(self.live.subprocess, "Popen") as spawn:
            image = importlib.import_module("tools.cloud_recog_image")
            importlib.reload(image)
            spawn.assert_not_called()
        self.assertEqual(old_env, dict(os.environ))
        self.assertEqual(old_path, sys.path)

    def test_image_expectations_match_and_mismatch(self):
        image = importlib.import_module("tools.cloud_recog_image")
        for hit, expected, success in [(True, True, True), (False, False, True), (False, True, False), (True, False, False)]:
            with self.subTest(hit=hit, expected=expected):
                context = Mock()
                context.run_recognition.return_value = SimpleNamespace(hit=hit, filtered_results=[])
                probe = image.make_probe(self.api, self.diag, object(), ["CloudGameHome"], {"CloudGameHome": expected})
                self.assertEqual(probe.run(context, None).success, success)

    def test_image_missing_recognition_detail_is_failure(self):
        image = importlib.import_module("tools.cloud_recog_image")
        context = Mock()
        context.run_recognition.return_value = None
        probe = image.make_probe(self.api, self.diag, object(), ["CloudGameHome"], {"CloudGameHome": False})
        self.assertFalse(probe.run(context, None).success)

    def test_image_parser_keeps_default_nodes_and_validates_assertions(self):
        image = importlib.import_module("tools.cloud_recog_image")
        args = image.parse_args(["private.png"])
        self.assertEqual(args.nodes, image.DEFAULT_NODES)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(image.main(["private.png", "Node", "--expect-hit", "Other"]), 2)
            self.assertEqual(image.main(["private.png", "Node", "--expect-hit", "Node", "--expect-miss", "Node"]), 2)

    def test_boot_initializes_i18n_before_startup(self):
        boot = importlib.import_module("tools.cloud_agent_boot")
        order = []
        server = Mock()
        server.start_up.side_effect = lambda _: order.append("start") or True
        runtime = SimpleNamespace(server=server, i18n_init=lambda: order.append("i18n"), cleanup=Mock())
        self.diag.mark_agent_bootstrap_ready.side_effect = lambda: order.append("bootstrap")
        with patch.object(boot, "Diagnostics", return_value=self.diag), patch.object(boot, "load_agent", return_value=runtime):
            self.assertEqual(boot.main(["PRIVATE_IDENTIFIER"]), 0)
        self.assertEqual(order, ["i18n", "bootstrap", "start"])
        runtime.cleanup.assert_called_once()
        server.shut_down.assert_called_once()
        self.assertNotIn("PRIVATE", str(self.diag.event.call_args_list))

    def test_boot_start_failure_still_shuts_down(self):
        boot = importlib.import_module("tools.cloud_agent_boot")
        runtime = SimpleNamespace(server=Mock(), i18n_init=Mock(), cleanup=Mock())
        runtime.server.start_up.return_value = False
        with patch.object(boot, "Diagnostics", return_value=self.diag), patch.object(boot, "load_agent", return_value=runtime):
            self.assertEqual(boot.main(["PRIVATE_IDENTIFIER"]), 1)
        runtime.cleanup.assert_called_once()
        runtime.server.shut_down.assert_called_once()

    def test_boot_interrupt_survives_cleanup_error(self):
        boot = importlib.import_module("tools.cloud_agent_boot")
        runtime = SimpleNamespace(server=Mock(), i18n_init=Mock(), cleanup=Mock(side_effect=OSError("PRIVATE_PATH")))
        runtime.server.join.side_effect = KeyboardInterrupt
        with patch.object(boot, "Diagnostics", return_value=self.diag), patch.object(boot, "load_agent", return_value=runtime):
            self.assertEqual(boot.main(["PRIVATE_IDENTIFIER"]), 130)
        runtime.server.shut_down.assert_called_once()
        self.assertNotIn("PRIVATE", str(self.diag.event.call_args_list))

    def test_wait_job_polls_status_without_reposting(self):
        from maa.job import TaskJob
        from maa.define import MaaStatusEnum
        statuses = iter([MaaStatusEnum.running, MaaStatusEnum.succeeded])
        wait = Mock()
        task = TaskJob(17, lambda _: next(statuses), wait, lambda _: None, lambda *a: True)
        with patch.object(self.live.time, "sleep") as sleep:
            self.assertIs(self.live.wait_job(task), task)
        sleep.assert_called_once_with(0.05)
        wait.assert_called_once_with(17)

    def test_wait_job_rejects_invalid_without_waiting_forever(self):
        from maa.job import TaskJob
        from maa.define import MaaStatusEnum
        wait = Mock()
        task = TaskJob(17, lambda _: MaaStatusEnum.invalid, wait, lambda _: None, lambda *a: True)
        with patch.object(self.live.time, "sleep", side_effect=AssertionError("invalid job polled")):
            with self.assertRaises(self.live.ToolError):
                self.live.wait_job(task)
        wait.assert_not_called()

    def test_wait_job_accepts_interrupt_before_native_wait(self):
        task = Mock(status=SimpleNamespace(done=False, pending=False, running=True), done=False)
        with patch.object(self.live.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.live.wait_job(task)
        task.wait.assert_not_called()

    def test_cleanup_interrupt_changes_success_to_130(self):
        with self.main_mocks(), patch.object(self.live, "close_agent") as close:
            def cleanup(*args):
                self.diag.interrupted = True
                return True
            close.side_effect = cleanup
            self.assertEqual(self.live.main([]), 130)

    def test_safe_reason_rejects_external_tool_error_payload(self):
        self.assertEqual(self.live.safe_reason(self.live.ToolError("PRIVATE_PATH")), "unexpected_error")
        self.assertEqual(self.live.safe_reason(self.live.ToolError("resource_load")), "resource_load")
        self.assertEqual(self.live.safe_reason(RuntimeError("PRIVATE_IDENTIFIER")), "unexpected_error")

    def test_bounded_call_cannot_block_forever(self):
        import threading
        release = threading.Event()
        try:
            with self.assertRaises(self.live.ToolError):
                self.live.bounded_call(release.wait, 0.01)
        finally:
            release.set()

    def test_failed_log_initialization_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            diag = self.live.Diagnostics("ProbeImg", root=Path(directory), fingerprints=False)
            try:
                self.api.Tasker.set_save_draw.return_value = False
                with patch.object(self.live, "_framework_log_path", None):
                    with self.assertRaises(self.live.ToolError):
                        diag.configure_framework(self.api)
                    with self.assertRaises(self.live.ToolError):
                        diag.configure_framework(self.api)
                self.api.Tasker.set_log_dir.assert_called_once()
            finally:
                diag.close()

    def test_image_controller_rejects_every_input(self):
        image = importlib.import_module("tools.cloud_recog_image")
        self.api.CustomController = type("Controller", (), {})
        controller = image.make_controller(self.api, Mock())
        for name in ("start_app", "stop_app", "click", "swipe", "touch_down", "touch_move", "touch_up", "click_key", "input_text", "key_down", "key_up", "scroll", "relative_move"):
            with self.subTest(name=name):
                self.assertFalse(getattr(controller, name)(0, 0))
        self.assertIsNone(controller.shell("ignored"))
        self.assertTrue(controller.connect())
        self.assertEqual(controller.get_features(), 0)

    @contextlib.contextmanager
    def image_main_mocks(self):
        image = importlib.import_module("tools.cloud_recog_image")
        with patch.object(image, "Diagnostics", return_value=self.diag), patch.object(
            image, "load_framework", return_value=self.api
        ), patch.object(image, "make_controller", return_value=self.controller):
            yield image

    def test_image_main_returns_task_failure(self):
        self.tasker.post_task.return_value = job(False)
        with self.image_main_mocks() as image:
            self.assertEqual(image.main([str(ROOT / "tests/fixtures/cloud_game/start_confirm_v2_720p.png")]), 1)
        self.api.Win32Controller.assert_not_called()

    def test_image_main_checks_all_prerequisites(self):
        failures = [(self.controller.post_connection, job(False)), (self.resource.post_bundle, job(False)),
                    (self.resource.register_custom_action, False), (self.resource.override_pipeline, False), (self.tasker.bind, False)]
        with self.image_main_mocks() as image:
            for method, value in failures:
                with self.subTest(method=method):
                    old = method.return_value
                    method.return_value = value
                    try:
                        self.assertEqual(image.main([str(ROOT / "tests/fixtures/cloud_game/start_confirm_v2_720p.png")]), 1)
                    finally:
                        method.return_value = old
            self.tasker.inited = False
            self.assertEqual(image.main([str(ROOT / "tests/fixtures/cloud_game/start_confirm_v2_720p.png")]), 1)
        self.tasker.post_task.assert_not_called()

    def test_image_main_missing_png_does_not_echo_path(self):
        with self.image_main_mocks() as image:
            self.assertEqual(image.main(["PRIVATE_PATH.png"]), 1)
        self.assertNotIn("PRIVATE", str(self.diag.event.call_args_list))

    def test_image_main_interrupt_returns_130(self):
        self.tasker.post_task.return_value.wait.side_effect = KeyboardInterrupt
        with self.image_main_mocks() as image:
            self.assertEqual(image.main([str(ROOT / "tests/fixtures/cloud_game/start_confirm_v2_720p.png")]), 130)
        self.tasker.post_stop.assert_called_once()

    def test_agent_application_logs_drop_raw_text_and_bound_duplicates(self):
        import logging
        boot = importlib.import_module("tools.cloud_agent_boot")
        handler = boot._MetadataHandler(self.diag)
        record = logging.LogRecord("maante", logging.ERROR, "PRIVATE_PATH", 1, "PRIVATE_IDENTIFIER PI_CONTROLLER=PRIVATE", (), None)
        for _ in range(50):
            handler.emit(record)
        self.diag.event.assert_called_once()
        self.assertNotIn("PRIVATE", str(self.diag.event.call_args_list))

    def test_boot_cleanup_interrupt_returns_130(self):
        boot = importlib.import_module("tools.cloud_agent_boot")
        runtime = SimpleNamespace(server=Mock(), i18n_init=Mock(), cleanup=Mock(side_effect=KeyboardInterrupt))
        with patch.object(boot, "Diagnostics", return_value=self.diag), patch.object(boot, "load_agent", return_value=runtime):
            self.assertEqual(boot.main(["PRIVATE_IDENTIFIER"]), 130)
        runtime.server.shut_down.assert_called_once()

    def test_agent_log_options_do_not_use_framework_only_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            diag = self.live.Diagnostics("AgentServer", root=Path(directory), agent=True, fingerprints=False)
            try:
                self.api.Tasker.set_save_draw.return_value = False
                self.api.Tasker.set_save_on_error.return_value = False
                self.api.Tasker.set_debug_mode.return_value = False
                with patch.object(self.live, "_framework_log_path", None):
                    diag.configure_framework(self.api)
                self.api.Tasker.set_log_dir.assert_called_once()
                self.api.Tasker.set_stdout_level.assert_called_once()
                self.api.Tasker.set_save_draw.assert_not_called()
                self.api.Tasker.set_save_on_error.assert_not_called()
                self.api.Tasker.set_debug_mode.assert_not_called()
            finally:
                diag.close()

    def test_live_and_boot_imports_preserve_environment(self):
        old_env, old_path = dict(os.environ), list(sys.path)
        with patch.object(sys, "argv", ["tool"]), patch.object(self.live.subprocess, "Popen") as spawn:
            importlib.reload(self.live)
            importlib.reload(importlib.import_module("tools.cloud_agent_boot"))
            spawn.assert_not_called()
        self.assertEqual(old_env, dict(os.environ))
        self.assertEqual(old_path, sys.path)


if __name__ == "__main__":
    unittest.main()
