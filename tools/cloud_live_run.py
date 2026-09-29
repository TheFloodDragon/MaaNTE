"""云异环实机诊断（不属于发行内容）。

保留 --calibrate / --recog-only / --entry。只读模式不调整窗口，输入方法为 Null。
默认不保存截图；--save-frame 才保存一帧，可能含账号/聊天等隐私，不要直接上传。
诊断位于 .narrafork/cloud-tools/<run_id>；框架原生日志仍可能包含 OCR/路径，
分享前必须人工脱敏。应用层摘要不会回显 identifier、PI、OCR 或异常正文。
内容指纹明确限定为 cloud_startup：仅云启动代码、模板、流程及相关配置/文案，
不是完整发行资源哈希，不扫描模型或其他任务的代码/模板。
本模块只在显式调用时初始化框架，不应在生产进程中反复运行 main()。
"""

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
RESOURCE = ROOT / "assets" / "resource" / "base"
DIAGNOSTIC_ROOT = ROOT / ".narrafork" / "cloud-tools"
FINGERPRINT_SCOPE = "cloud_startup"
FINGERPRINT_CODE_FILES = (
    "agent/custom/__init__.py",
    "agent/custom/action/__init__.py",
    "agent/custom/action/cloud_game.py",
    "agent/cloud_start.py",
    "agent/utils/__init__.py",
    "agent/utils/cloud_window.py",
    "agent/utils/logger.py",
    "agent/utils/i18n.py",
    "agent/utils/maafocus.py",
    "agent/utils/pienv.py",
    "agent/utils/screen.py",
    "agent/utils/win32_process.py",
    "tools/cloud_live_run.py",
    "tools/cloud_agent_boot.py",
    "tools/cloud_recog_image.py",
    "requirements.txt",
)
FINGERPRINT_RESOURCE_FILES = (
    "assets/interface.json",
    "assets/resource/tasks/CloudGame.json",
) + tuple(
    "assets/resource/locales/%s/%s.json" % (group, language)
    for group in ("agent", "interface")
    for language in ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")
)
FINGERPRINT_RESOURCE_TREES = (
    ("assets/resource/base/pipeline/CloudGame", "*.json"),
    ("assets/resource/base/image/CloudGame", "*.png"),
    ("assets/resource/base/pipeline/SceneManager", "*.json"),
    ("assets/resource/base/pipeline/Interface/Scene", "*.json"),
)
CONTROLLER = {
    "name": "CloudGame-Front",
    "label": "CloudGame-Front",
    "display_short_side": 720,
    "type": "Win32",
    "permission_required": True,
    "win32": {
        "class_regex": r"^Qt\d+QWindow(?:Icon|OwnDC)?$",
        "window_regex": r"^\s*(云.*?异环)\s*$",
        "screencap": "PrintWindow",
        "mouse": "Seize",
        "keyboard": "Seize",
    },
}
RECOG_NODES = [
    "CloudGameLoginScreen", "CloudGameHomeScreen", "CloudGameHome",
    "CloudGameEnterText", "CloudGameGameLogin", "CloudGameInWorld",
    "CloudGameQueueScreen", "CloudGameLoading", "CloudGameErrorState",
    "CloudGameDailyLoginTitle", "CloudGameLoginOtherMethods", "CloudGameLoginSubmit",
]
NODE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_framework_log_path = None
_default_diagnostics = None
ERROR_CODES = frozenset({
    "run_id_invalid", "framework_already_initialized", "log_directory", "log_stdout",
    "log_draw", "log_on_error", "log_debug", "operation_timeout", "screencap",
    "empty_frame", "recognition_detail", "tasker_bind", "tasker_init", "window_not_unique",
    "window_prepare", "controller_connect", "resource_load", "probe_register",
    "agent_create", "agent_timeout", "agent_identifier", "agent_bind", "agent_connect",
    "agent_unavailable", "probe_override", "calibration_override", "agent_startup", "png_required", "job_invalid",
})


class ToolError(RuntimeError):
    """仅由本工具固定阶段码创建；不包装外部异常正文。"""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 的默认错误会回显用户参数（可能是路径或连接标识）。
        self.exit(2, "cloud-tools: invalid_arguments; use --help\n")


def node_name(value):
    if not NODE_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid_node")
    return value


def require(ok, stage):
    if not ok:
        raise ToolError(stage)


def safe_reason(exc):
    if isinstance(exc, ToolError) and len(exc.args) == 1 and type(exc.args[0]) is str:
        if exc.args[0] in ERROR_CODES:
            return exc.args[0]
    return "unexpected_error"


def wait_job(job):
    """避免在不可中断的原生 wait 中长期阻塞主线程；只轮询作业状态，不重试任务。"""
    while True:
        status = job.status
        if status.done:
            return job.wait()
        require(status.pending or status.running, "job_invalid")
        time.sleep(0.05)


def task_exit_code(task_job):
    """Maa 5.10 的 wait() 返回 TaskJob 自身，非 TaskDetail。"""
    return 0 if task_job is not None and task_job.succeeded else 1


def fingerprint(paths, root):
    """按相对路径排序并哈希路径+内容；包含未跟踪修改，不依赖 Git 状态。"""
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda p: p.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        with path.open("rb") as source:
            digest.update(hashlib.file_digest(source, "sha256").digest())
    return digest.hexdigest()


def cloud_startup_files(root):
    """只枚举指定云启动子树；包括未跟踪文件，不触碰模型和无关任务目录。"""
    code = [root / relative for relative in FINGERPRINT_CODE_FILES]
    resources = [root / relative for relative in FINGERPRINT_RESOURCE_FILES]
    for relative, pattern in FINGERPRINT_RESOURCE_TREES:
        resources.extend(path for path in (root / relative).rglob(pattern) if path.is_file())
    return code, resources


class Diagnostics:
    """每次 CLI 一份摘要，每个进程至多初始化一次原生日志选项。"""

    def __init__(self, entry, *, root=DIAGNOSTIC_ROOT, fingerprints=True, run_id=None, agent=False):
        self.run_id = run_id or uuid.uuid4().hex
        self.agent = agent
        require(bool(re.fullmatch(r"[0-9a-f]{32}", self.run_id)), "run_id_invalid")
        self.path = Path(root) / self.run_id
        if agent:
            self.path /= "agent"
        self.path.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.started = time.monotonic()
        self.interrupted = False
        self.logger = logging.getLogger("cloud_tools.%s.%s" % (self.run_id, "agent" if agent else "main"))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(logging.StreamHandler(sys.stderr))
        self.logger.addHandler(logging.FileHandler(self.path / "summary.jsonl", encoding="utf-8"))
        self.event("initialize", "started", entry=entry)
        self.event("privacy", "notice", reason="native_logs_may_contain_ocr_and_paths")
        try:
            if fingerprints:
                code, resources = cloud_startup_files(ROOT)
                self.event("fingerprint", "ready", fingerprint_scope=FINGERPRINT_SCOPE,
                           code=fingerprint(code, ROOT), resource=fingerprint(resources, ROOT),
                           code_files=len(code), resource_files=len(resources),
                           python=sys.version.split()[0], maafw=importlib.metadata.version("MaaFw"))
        except BaseException:
            self.close()
            raise

    def event(self, phase, state, **fields):
        # 调用方仅传固定阶段码、经过校验的节点名、哈希或数字，绝不传异常对象。
        data = {"run_id": self.run_id, "phase": phase, "state": state,
                "elapsed": round(time.monotonic() - self.started, 3), **fields}
        self.logger.info("%s", json.dumps(data, ensure_ascii=True, sort_keys=True))

    def configure_framework(self, api):
        global _framework_log_path
        if _framework_log_path is not None:
            require(_framework_log_path == (self.path, True), "framework_already_initialized")
            return
        # 即使中途配置失败，也不再次切换原生异步日志目录。
        _framework_log_path = (self.path, False)
        require(api.Tasker.set_log_dir(self.path), "log_directory")
        require(api.Tasker.set_stdout_level(api.LoggingLevel.Off), "log_stdout")
        if not self.agent:
            # MaaAgentServer 5.10.4 仅支持日志选项；图像由框架进程生成并在其侧禁用。
            require(api.Tasker.set_save_draw(False), "log_draw")
            require(api.Tasker.set_save_on_error(False), "log_on_error")
            require(api.Tasker.set_debug_mode(False), "log_debug")
        _framework_log_path = (self.path, True)

    def close(self):
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()


def diagnostics_default():
    global _default_diagnostics
    if _default_diagnostics is None:
        _default_diagnostics = Diagnostics("CloudGameStartEntrance")
    return _default_diagnostics


def load_framework():
    from maa.agent_client import AgentClient
    from maa.controller import CustomController, Win32Controller
    from maa.custom_action import CustomAction
    from maa.define import LoggingLevelEnum, MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
    from maa.resource import Resource
    from maa.tasker import Tasker

    return SimpleNamespace(
        AgentClient=AgentClient, CustomController=CustomController, Win32Controller=Win32Controller,
        CustomAction=CustomAction, Resource=Resource, Tasker=Tasker,
        Input=MaaWin32InputMethodEnum, Screencap=MaaWin32ScreencapMethodEnum,
        LoggingLevel=LoggingLevelEnum,
    )


def load_cloud_start():
    if str(AGENT) not in sys.path:
        sys.path.insert(0, str(AGENT))
    return importlib.import_module("cloud_start")


def bounded_call(callback, timeout):
    """只用于收尾或 IPC；不在此线程发窗口输入。异常回到调用者且无 traceback 输出。"""
    result = []

    def invoke():
        try:
            result.append((True, callback()))
        except BaseException as exc:
            result.append((False, exc))

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    thread.join(timeout)
    require(not thread.is_alive(), "operation_timeout")
    ok, value = result[0]
    if not ok:
        raise value
    return value


def close_agent(client, proc, diagnostics=None):
    """只回收本次 Popen 对象：disconnect -> wait -> terminate -> wait -> kill -> wait。"""
    diag = diagnostics
    if client is not None:
        try:
            bounded_call(client.disconnect, 2.0)
        except BaseException as exc:
            if diag:
                if isinstance(exc, KeyboardInterrupt):
                    diag.interrupted = True
                diag.event("disconnect", "failed", reason="disconnect_incomplete")
    if proc is None:
        return True
    for action in (None, proc.terminate, proc.kill):
        try:
            if action is not None:
                action()
            proc.wait(timeout=2.0)
            if diag:
                diag.event("agent_cleanup", "reaped")
            return True
        except BaseException as exc:
            # 包括二次 Ctrl+C；不能让它打断最后的 kill/wait。
            if diag and isinstance(exc, KeyboardInterrupt):
                diag.interrupted = True
            continue
    if diag:
        diag.event("agent_cleanup", "failed", reason="process_not_reaped")
    return False


def stop_tasker(tasker, diagnostics):
    if tasker is not None:
        try:
            bounded_call(lambda: tasker.post_stop().wait(), 3.0)
        except BaseException:
            diagnostics.event("task_stop", "failed", reason="stop_incomplete")


def save_frame(image, diagnostics):
    from PIL import Image

    Image.fromarray(image[:, :, :3][:, :, ::-1]).save(diagnostics.path / "frame.png")
    diagnostics.event("frame", "saved", reason="private_image_do_not_share_unredacted")


def make_probe(api, diagnostics, save=False):
    class Probe(api.CustomAction):
        def run(self, context, argv):
            try:
                controller = context.tasker.controller
                require(wait_job(controller.post_screencap()).succeeded, "screencap")
                image = controller.cached_image
                require(image is not None and image.size > 0, "empty_frame")
                if save:
                    save_frame(image, diagnostics)
                for node in RECOG_NODES:
                    detail = context.run_recognition(node, image)
                    require(detail is not None, "recognition_detail")
                    diagnostics.event("recognition", "done", entry=node, hit=bool(detail.hit))
                return api.CustomAction.RunResult(success=True)
            except BaseException:
                diagnostics.event("probe", "failed", reason="probe_failed")
                return api.CustomAction.RunResult(success=False)

    return Probe()


def bind_tasker(api, resource, controller):
    tasker = api.Tasker()
    require(tasker.bind(resource, controller), "tasker_bind")
    require(tasker.inited, "tasker_init")
    return tasker


def build_framework(*, recog_only=False, diagnostics=None, save=False):
    """保持原返回三元组；只读模式跳过窗口准备，并在控制器层禁用输入。"""
    diag = diagnostics or diagnostics_default()
    api = load_framework()
    diag.configure_framework(api)
    cloud_start = load_cloud_start()
    windows = cloud_start.client_windows()
    require(len(windows) == 1, "window_not_unique")
    hwnd = windows[0].hwnd
    if not recog_only:
        require(cloud_start.prepare_client_window(hwnd), "window_prepare")
    method = api.Input.Null if recog_only else api.Input.Seize
    controller = api.Win32Controller(hwnd, screencap_method=api.Screencap.PrintWindow,
                                     mouse_method=method, keyboard_method=method)
    require(wait_job(controller.post_connection()).succeeded, "controller_connect")
    resource = api.Resource()
    require(wait_job(resource.post_bundle(RESOURCE)).succeeded, "resource_load")
    require(resource.register_custom_action("live_probe", make_probe(api, diag, save)), "probe_register")
    tasker = bind_tasker(api, resource, controller)
    diag.event("framework", "ready")
    return tasker, resource, controller


def start_agent(resource, *, diagnostics=None):
    """返回 client/proc，失败（含中断）时不把回收义务留给尚未拿到它们的调用者。"""
    diag = diagnostics or diagnostics_default()
    api = load_framework()
    client, proc = None, None
    try:
        client = api.AgentClient.create_tcp(0)
        require(client is not None, "agent_create")
        require(client.set_timeout(5000), "agent_timeout")
        require(client.identifier, "agent_identifier")
        require(client.bind(resource), "agent_bind")
        env = dict(os.environ)
        env["PI_CONTROLLER"] = json.dumps(CONTROLLER, ensure_ascii=False)
        env.setdefault("PI_RESOURCE", json.dumps({"name": "官服", "path": [str(RESOURCE)]}))
        env["CLOUD_TOOLS_RUN_ID"] = diag.run_id
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-B", "-u", str(ROOT / "tools" / "cloud_agent_boot.py"), str(client.identifier)],
            cwd=str(diag.path), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        diag.event("agent", "spawned")
        require(bounded_call(client.connect, 10.0), "agent_connect")
        require(client.connected and client.alive, "agent_unavailable")
        diag.event("agent", "connected")
        return client, proc
    except BaseException:
        close_agent(client, proc, diag)
        raise


def recog_only(tasker, resource):
    require(resource.override_pipeline({"LiveProbe": {
        "action": {"type": "Custom", "param": {"custom_action": "live_probe"}},
        "rate_limit": 0, "pre_delay": 0, "post_delay": 0,
    }}), "probe_override")
    return wait_job(tasker.post_task("LiveProbe"))


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--recog-only", action="store_true")
    parser.add_argument("--entry", default="CloudGameStartEntrance", type=node_name)
    parser.add_argument("--save-frame", action="store_true", help="显式保存一帧；可能含账号/聊天隐私")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    diag = tasker = client = proc = None
    code = 1
    try:
        entry = "LiveProbe" if args.recog_only else args.entry
        diag = Diagnostics(entry)
        diag.event("options", "ready", calibrated=args.calibrate, readonly=args.recog_only, save_frame=args.save_frame)
        tasker, resource, controller = build_framework(recog_only=args.recog_only, diagnostics=diag, save=args.save_frame)
        if args.calibrate:
            require(resource.override_pipeline({"CloudGameProfile": {"attach": {"calibrated": True}}}), "calibration_override")
        if args.recog_only:
            task = recog_only(tasker, resource)
        else:
            client, proc = start_agent(resource, diagnostics=diag)
            task = wait_job(tasker.post_task(args.entry))
        code = task_exit_code(task)
        diag.event("task", "succeeded" if code == 0 else "failed", task_id=task.job_id)
        if args.save_frame and not args.recog_only:
            require(wait_job(controller.post_screencap()).succeeded, "screencap")
            require(controller.cached_image is not None, "empty_frame")
            save_frame(controller.cached_image, diag)
    except KeyboardInterrupt:
        code = 130
        if diag:
            diag.event("run", "interrupted")
            stop_tasker(tasker, diag)
    except Exception as exc:
        code = 1
        if diag:
            diag.event("run", "failed", reason=safe_reason(exc))
            stop_tasker(tasker, diag)
        else:
            sys.stderr.write("cloud-tools: initialization_failed\n")
    finally:
        if not close_agent(client, proc, diag) and code == 0:
            code = 1
        if diag:
            if getattr(diag, "interrupted", False) is True:
                code = 130
            diag.event("summary", "succeeded" if code == 0 else "interrupted" if code == 130 else "failed", exit_code=code)
            diag.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
