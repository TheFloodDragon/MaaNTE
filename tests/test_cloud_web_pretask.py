"""云异环启动（网页）预任务离线回归：选项映射、配置契约、单实例接管与进程内 Tasker 冒烟。

进程内 Tasker 用例在独立子进程中运行：同一进程一旦导入 maa.agent（其他测试文件会），
MaaFramework 就切到 AgentServer 模式，无法再创建 Tasker。
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
ASSETS = ROOT / "assets"
sys.path.insert(0, str(AGENT))
import cloud_web_pretask as pretask  # noqa: E402

LANGS = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")


def _jsonc(path):
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE))


def _option_defs():
    return pretask.load_option_defs(ASSETS / pretask.TASKS_FILE)


class OptionTests(unittest.TestCase):
    def test_defaults_match_task_defaults(self):
        override = pretask.build_override(_option_defs(), {})
        self.assertEqual(
            override,
            {
                "CloudWebAutoQueueConfig": {"attach": {"auto_queue": True}},
                "CloudWebLoginTimeoutConfig": {"attach": {"minutes": 10}},
                "CloudWebQueueTimeoutConfig": {"attach": {"minutes": 60}},
                "CloudWebFullscreenConfig": {"attach": {"fullscreen": True}},
            },
        )

    def test_mxu_serialized_values(self):
        # MXU：switch 传 case.name，input 传 {输入名: 字符串}。
        raw = json.dumps(
            {
                "CloudWebAutoQueue": "No",
                "CloudWebLoginTimeout": {"minutes": "5"},
                "CloudWebQueueTimeout": {"minutes": "1440"},
                "CloudWebFullscreen": "No",
            }
        )
        override = pretask.build_override(_option_defs(), pretask.parse_options(raw))
        self.assertIs(override["CloudWebAutoQueueConfig"]["attach"]["auto_queue"], False)
        self.assertEqual(override["CloudWebLoginTimeoutConfig"]["attach"]["minutes"], 5)
        self.assertIsInstance(override["CloudWebLoginTimeoutConfig"]["attach"]["minutes"], int)
        self.assertEqual(override["CloudWebQueueTimeoutConfig"]["attach"]["minutes"], 1440)
        self.assertIs(override["CloudWebFullscreenConfig"]["attach"]["fullscreen"], False)

    def test_rejects_invalid_values_instead_of_guessing(self):
        defs = _option_defs()
        for values in (
            {"CloudWebAutoQueue": "Maybe"},
            {"CloudWebAutoQueue": True},
            {"CloudWebLoginTimeout": {"minutes": "0"}},
            {"CloudWebLoginTimeout": {"minutes": "121"}},
            {"CloudWebQueueTimeout": {"minutes": "abc"}},
            {"CloudWebQueueTimeout": "60"},
        ):
            with self.subTest(values=values), self.assertRaises(pretask.OptionError):
                pretask.build_override(defs, values)

    def test_parse_options(self):
        self.assertEqual(pretask.parse_options(None), {})
        self.assertEqual(pretask.parse_options("  "), {})
        for raw in ("{", "[]", "3", '"x"'):
            with self.subTest(raw=raw), self.assertRaises(pretask.OptionError):
                pretask.parse_options(raw)

    def test_substitute_keeps_type_only_for_whole_placeholder(self):
        self.assertEqual(pretask._substitute({"a": "{n}", "b": "x{n}"}, {"n": 3}), {"a": 3, "b": "x3"})

    def test_focus_text_only_takes_printt_messages(self):
        self.assertEqual(pretask.focus_text({"Node.Action.Starting": "开始排队"}), "开始排队")
        for focus in (
            None,
            {},
            {"Node.Action.Starting": "$cloud_web_focus_start"},
            {"Node.Action.Starting": "  "},
            {"Node.Action.Succeeded": "x"},
        ):
            with self.subTest(focus=focus):
                self.assertIsNone(pretask.focus_text(focus))


class ContractTests(unittest.TestCase):
    """合并后只剩一个预任务：interface、任务文件、Pipeline、动作注册与文案保持一致。"""

    @classmethod
    def setUpClass(cls):
        cls.interface = _jsonc(ASSETS / "interface.json")
        cls.tasks = json.loads((ASSETS / pretask.TASKS_FILE).read_text(encoding="utf-8"))
        cls.pipeline = json.loads(
            (ASSETS / "resource/base/pipeline/CloudGame/CloudGameWeb.json").read_text(encoding="utf-8")
        )

    def test_single_pretask_carries_all_web_options(self):
        pretasks = self.interface["pretask"]
        self.assertEqual([p["name"] for p in pretasks], ["CloudGameWebLaunch"])
        item = pretasks[0]
        self.assertEqual(item["controller"], [pretask.CONTROLLER])
        self.assertEqual(item["args"], ["../agent/cloud_web_pretask.py"])
        self.assertEqual(tuple(item["option"]), pretask.OPTION_KEYS)
        for key in item["option"]:
            self.assertIn(key, self.tasks["option"])

    def test_web_start_is_no_longer_a_normal_task(self):
        names = [task["name"] for task in self.tasks["task"]]
        self.assertNotIn("CloudGameWebStart", names)
        self.assertNotIn(pretask.ENTRY, [task["entry"] for task in self.tasks["task"]])
        self.assertEqual(names, ["CloudGameStart"])  # 客户端版云异环任务保持不变

    def test_other_controllers_unchanged(self):
        controllers = {c["name"]: c for c in self.interface["controller"]}
        self.assertEqual(
            list(controllers), ["Win32", "Win32-Front", "Win32-Background", "CloudGame-Front", "CloudGame-Web"]
        )
        for name in ("Win32", "Win32-Front", "Win32-Background"):
            self.assertEqual(controllers[name]["win32"]["class_regex"], "UnrealWindow")
        for item in self.interface["pretask"]:
            self.assertEqual(item.get("controller"), ["CloudGame-Web"])  # 不对其他控制器生效

    def test_entry_and_action_names_match_pipeline(self):
        self.assertIn(pretask.ENTRY, self.pipeline)
        used = set()
        for node in self.pipeline.values():
            action = node.get("action")
            if isinstance(action, dict) and action.get("type") == "Custom":
                used.add(action["param"]["custom_action"])
        source = (AGENT / "cloud_web_flow.py").read_text(encoding="utf-8")
        block = source[source.index("ACTIONS = {") :]
        declared = set(re.findall(r'"(cloud_web_\w+)":', block[: block.index("}")]))
        self.assertEqual(used, declared)

    def test_flow_module_never_imports_agent_server(self):
        source = (AGENT / "cloud_web_flow.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"^\s*(from|import)\s+maa\.agent", "导入 AgentServer 会让预任务无法创建 Tasker")
        self.assertNotRegex(
            (AGENT / "cloud_web_pretask.py").read_text(encoding="utf-8"), r"^\s*(from|import)\s+maa\.agent"
        )

    def test_locales_renamed_and_old_task_keys_removed(self):
        for lang in LANGS:
            data = json.loads((ASSETS / f"resource/locales/interface/{lang}.json").read_text(encoding="utf-8"))
            with self.subTest(lang=lang):
                self.assertNotIn("task_cloud_web_label", data)
                self.assertNotIn("task_cloud_web_desc", data)
                self.assertIn("cloud_web_pretask", data["pretask_cloud_web_desc"])
                for option in self.tasks["option"].values():
                    for text in (option["label"], option.get("description", "")):
                        if text.startswith("$"):
                            self.assertIn(text[1:], data)

    def test_install_mxu_points_pretask_to_bundled_python(self):
        source = (ROOT / "tools/ci/install_mxu.py").read_text(encoding="utf-8")
        self.assertIn('[r"./agent/cloud_web_pretask.py"]', source)
        self.assertNotIn("cloud_web_start.py", source)

    def test_interface_dir_detection(self):
        self.assertEqual(pretask.interface_dir([ROOT / "nope", ASSETS]), ASSETS)
        self.assertIsNone(pretask.interface_dir([ROOT / "nope"]))


@unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
class GuardTests(unittest.TestCase):
    def test_takeover_waits_for_old_owner_to_exit(self):
        key = "test-" + str(time.monotonic_ns())
        ready, saw_stop = threading.Event(), threading.Event()
        errors = []

        def old_owner():
            guard = pretask.InstanceGuard(key)
            try:
                if not guard.acquire(0):
                    errors.append("acquire")
                ready.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if guard.stop_requested():
                        saw_stop.set()
                        return
                    time.sleep(0.01)
                errors.append("stop_timeout")
            finally:
                guard.close()

        worker = threading.Thread(target=old_owner, daemon=True)
        worker.start()
        self.assertTrue(ready.wait(5))
        guard = pretask.InstanceGuard(key)
        try:
            self.assertTrue(guard.acquire(5))
            self.assertTrue(saw_stop.is_set())
            self.assertFalse(guard.stop_requested())
        finally:
            guard.close()
            worker.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(worker.is_alive())

    def test_timeout_does_not_release_another_owners_mutex(self):
        key = "test-" + str(time.monotonic_ns())
        ready, release = threading.Event(), threading.Event()

        def old_owner():
            guard = pretask.InstanceGuard(key)
            try:
                guard.acquire(0)
                ready.set()
                release.wait(5)
            finally:
                guard.close()

        worker = threading.Thread(target=old_owner, daemon=True)
        worker.start()
        self.assertTrue(ready.wait(5))
        guard = pretask.InstanceGuard(key)
        try:
            self.assertFalse(guard.acquire(0.05))
            self.assertFalse(guard.owned)
        finally:
            guard.close()
            release.set()
            worker.join(5)
        retry = pretask.InstanceGuard(key)
        try:
            self.assertTrue(retry.acquire(0))
        finally:
            retry.close()


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_rechecks_modules_after_acquiring_lock(self):
        events = []
        guard = SimpleNamespace(
            acquire=lambda timeout: events.append("acquire") or True, close=lambda: events.append("close")
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(pretask.sys, "platform", "win32"), patch.object(
            pretask, "InstanceGuard", return_value=guard
        ), patch.object(pretask, "missing_modules", side_effect=lambda: events.append("check") or []), patch.object(
            pretask, "_install_local_dependencies"
        ) as install:
            self.assertTrue(pretask._ensure_dependencies(Path(directory)))
            install.assert_not_called()
        self.assertEqual(events, ["acquire", "check", "check", "close"])

    def test_offline_installer_respects_disabled_pip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/pip_config.json").write_text('{"enable_pip_install": false}', encoding="utf-8")
            with patch.object(pretask, "ROOT", root), patch.object(pretask.subprocess, "run") as run:
                self.assertFalse(pretask._install_local_dependencies(root / "logs"))
                run.assert_not_called()

    def test_offline_install_uses_pinned_local_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deps").mkdir()
            (root / "deps/dummy.whl").touch()
            (root / "requirements.txt").write_text(
                "maafw==v5.10.4\nwebsockets>=14.0,<17.0\nnumpy\nopencv-python\nlibrosa\n", encoding="utf-8"
            )
            with patch.object(pretask, "ROOT", root), patch.object(pretask.subprocess, "run") as run:
                run.return_value = SimpleNamespace(returncode=0)
                self.assertTrue(pretask._install_local_dependencies(root / "logs"))
                command = run.call_args.args[0]
                self.assertIn("--no-index", command)
                self.assertIn(str(root / "deps"), command)
                self.assertIn("maafw==v5.10.4", command)
                self.assertIn("websockets>=14.0,<17.0", command)
                self.assertNotIn("librosa", command)


@unittest.skipIf(pretask.missing_modules(), "MaaFramework runtime unavailable")
class RuntimeTests(unittest.TestCase):
    def test_inprocess_pipeline_success_failure_and_takeover(self):
        script = textwrap.dedent(r"""
            import json, sys, threading, time
            from pathlib import Path
            from types import SimpleNamespace
            sys.path.insert(0, sys.argv[1])
            import cloud_web_pretask as host
            from maa.custom_action import CustomAction
            from maa.library import Library
            root = Path.cwd()
            (root / 'logs').mkdir()
            host.configure_framework(root / 'logs')
            flow = host.load_flow()
            assert not Library.is_agent_server()
            assert 'maa.agent' not in sys.modules
            (root / 'bundle/pipeline').mkdir(parents=True)
            (root / 'bundle/pipeline/main.json').write_text(json.dumps({
                'Entry': {'action': {'type': 'Custom', 'param': {'custom_action': 'probe'}},
                          'rate_limit': 0, 'pre_delay': 0, 'post_delay': 0},
                'Config': {'enabled': False, 'attach': {'minutes': 1}}
            }), encoding='utf-8')
            results = []
            for mode in ('ok', 'fail', 'stop'):
                messages, seen = [], []
                ready = threading.Event()
                state = SimpleNamespace(tasker=None, stop_requested=False, done=False, lock=threading.Lock())
                class Probe(CustomAction):
                    def run(self, context, argv):
                        seen.append(context.get_node_data('Config')['attach']['minutes'])
                        context.run_action('_Focus', pipeline_override={'_Focus': {
                            'action': 'DoNothing', 'focus': {'Node.Action.Starting': 'probe-message'},
                            'pre_delay': 0, 'post_delay': 0}})
                        ready.set()
                        if mode == 'stop':
                            deadline = time.monotonic() + 5
                            while not context.tasker.stopping and time.monotonic() < deadline:
                                time.sleep(0.01)
                        return CustomAction.RunResult(success=mode == 'ok')
                def stop():
                    if ready.wait(5):
                        with state.lock:
                            state.stop_requested = True
                            state.tasker.post_stop()
                worker = threading.Thread(target=stop) if mode == 'stop' else None
                if worker:
                    worker.start()
                code, reason = host.run_flow(root / 'bundle', {'Config': {'attach': {'minutes': 7}}},
                    {'probe': Probe}, state, lambda fmt, *args: messages.append(fmt % args), entry='Entry')
                if worker:
                    worker.join(5)
                results.append({'code': code, 'reason': reason, 'seen': seen, 'messages': messages})
                state.tasker.post_stop().wait()
                state.tasker.clear_context_sinks()
                state.tasker = None
            # 复用真实入口及全部七个动作，只替换浏览器/CDP 和大世界识别帧。
            import numpy as np
            pipeline = json.loads((Path(sys.argv[1]).parent / 'assets/resource/base/pipeline/CloudGame/CloudGameWeb.json').read_text(encoding='utf-8'))
            pipeline = {name: node for name, node in pipeline.items() if 'action' in node or name.endswith('Config')}
            pipeline['CloudWebWorldReady'] = {'recognition': 'DirectHit', 'pre_delay': 0, 'post_delay': 0}
            (root / 'web/pipeline').mkdir(parents=True)
            (root / 'web/pipeline/main.json').write_text(json.dumps(pipeline), encoding='utf-8')
            class Session:
                game = False
                closed = False
                fullscreen = False
                def install_hooks(self): pass
                def set_stream_override(self): pass
                def pointer_lock(self, block): pass
                def state(self):
                    return {'href_ok': True, 'ready': 'complete', 'logged_in': True, 'logged_out': False,
                            'game': self.game, 'home': not self.game, 'loading': False, 'pops': [], 'gift': None,
                            'start': [0, 0, 10, 10], 'free_min': 60, 'paid_min': 0, 'events': [],
                            'video': {'content': [0, 0, 1280, 720], 'rs': 2, 'w': 1280, 'h': 720}}
                def click_rect(self, rect): self.game = True
                def window_state(self, state): self.fullscreen = state == 'fullscreen'
                def capture(self, content): return np.zeros((720, 1280, 3), dtype=np.uint8)
                def close(self): self.closed = True
            session = Session()
            flow.controller_name = lambda: flow.CONTROLLER
            flow.start_browser = lambda **kwargs: SimpleNamespace(launched=True)
            flow.CloudSession.connect = staticmethod(lambda: session)
            state = SimpleNamespace(tasker=None, stop_requested=False, done=False, lock=threading.Lock())
            code, reason = host.run_flow(root / 'web', {}, flow.ACTIONS, state, lambda *args: None)
            assert code == 0, reason
            assert session.game and session.fullscreen and session.closed and flow._web is None
            state.tasker.post_stop().wait()
            state.tasker.clear_context_sinks()
            state.tasker = None
            print('RESULT:' + json.dumps(results))
        """)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-X", "faulthandler", "-u", "-c", script, str(AGENT)],
                cwd=directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(next(line[7:] for line in result.stdout.splitlines() if line.startswith("RESULT:")))
            self.assertEqual(
                [item["code"] for item in data], [pretask.EXIT_OK, pretask.EXIT_FAILED, pretask.EXIT_FAILED]
            )
            self.assertEqual(data[-1]["reason"], "taken_over")
            for item in data:
                self.assertEqual(item["seen"], [7])
                self.assertIn("提示 | probe-message", item["messages"])
            self.assertTrue((Path(directory) / "logs/maafw.log").is_file())

    def test_entrypoint_rejects_bad_options_before_launching_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "resource/base/pipeline").mkdir(parents=True)
            (base / "resource/tasks").mkdir()
            (base / pretask.TASKS_FILE).write_text(
                (ASSETS / pretask.TASKS_FILE).read_text(encoding="utf-8"), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(AGENT / "cloud_web_pretask.py"), '{"CloudWebAutoQueue": "UNEXPECTED_VALUE"}'],
                cwd=base,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, pretask.EXIT_BAD_OPTIONS, result.stderr)
            logs = list((base / "debug" / pretask.LOG_DIR_NAME).rglob("*.log"))
            text = "\n".join(path.read_text(encoding="utf-8") for path in logs)
            self.assertIn("invalid_options", text)
            self.assertNotIn("UNEXPECTED_VALUE", text)


if __name__ == "__main__":
    unittest.main()
