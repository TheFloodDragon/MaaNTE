"""云异环启动（网页）预任务：在 MXU 连接“云游戏 - 网页”控制器之前跑完整个网页端启动流程。

MXU v2.3.1 点“开始任务”的顺序固定为：预任务 → 搜索并连接窗口 → 普通任务。浏览器没开时，
只有预任务能赶在搜索窗口之前把它打开，所以“打开网页”与“云异环启动（网页）”合并在这里：
打开/复用专用浏览器 → 登录 → 时长/节点 → 排队 → 串流 → 进入大世界。本进程用进程内
MaaFramework Tasker 加载 resource/base 并运行 CloudWebStartEntrance，复用 cloud_web_flow
的动作与 Pipeline 识别节点；网页与游戏画面的截图、输入都走 CDP，控制器只提供空白帧。
流程结束后 MXU 连接“异环云游戏”窗口，继续执行列表里的其他任务。

MXU 预任务的限制（不改 MXU 无法绕开）：
- 标准输出被丢弃：用户提示与诊断日志写入 debug/cloud_web_pretask/。
- 点“停止”只结束 MXU 的等待，不会结束本进程；关闭浏览器窗口即可中止流程。再次开始时，
  新进程通知旧进程停止并接管，避免两个流程同时操作同一页面。
- 退出码非 0 时 MXU 只记录警告，仍会继续连接窗口。

退出码：0 成功（含关闭自动排队、登录后交由用户手动进入）；1 流程失败、被接管或中止；
2 参数或选项无效；3 缺少依赖；4 等待上一次流程退出超时；5 资源或框架初始化失败。
"""

import copy
import ctypes
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

AGENT_DIR = Path(__file__).resolve().parent
ROOT = AGENT_DIR.parent
CONTROLLER = "CloudGame-Web"
ENTRY = "CloudWebStartEntrance"
OPTION_KEYS = ("CloudWebAutoQueue", "CloudWebLoginTimeout", "CloudWebQueueTimeout", "CloudWebFullscreen")
RESOURCE_BUNDLE = Path("resource") / "base"
TASKS_FILE = Path("resource") / "tasks" / "CloudGame.json"
LOG_DIR_NAME = "cloud_web_pretask"
REQUIRED_MODULES = ("maa", "numpy", "cv2", "websockets")
TAKEOVER_TIMEOUT = 30.0
_RELAUNCH_ENV = "MAANTE_CLOUD_PRETASK_RELAUNCHED"
_STARTING = 1  # maa.event_sink.NotificationType.Starting

EXIT_OK, EXIT_FAILED, EXIT_BAD_OPTIONS, EXIT_MISSING_DEPS, EXIT_BUSY, EXIT_INIT_FAILED = range(6)


class OptionError(ValueError):
    """选项 JSON 或取值无效；只携带选项键名，不回显用户输入。"""


def interface_dir(candidates=None):
    """返回含 resource/base 与 CloudGame.json 的目录：发行版为安装根，开发版为 assets。"""
    if candidates is None:
        cwd = Path.cwd()
        candidates = (cwd, cwd / "assets", ROOT / "assets", ROOT)
    for base in candidates:
        if (base / RESOURCE_BUNDLE / "pipeline").is_dir() and (base / TASKS_FILE).is_file():
            return Path(base)
    return None


def parse_options(raw):
    """MXU 把 pretask.option 序列化为单行 JSON 追加为最后一个参数；缺省视为全部取默认值。"""
    if raw is None or not str(raw).strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise OptionError("options_json") from None
    if not isinstance(value, dict):
        raise OptionError("options_json")
    return value


def load_option_defs(path):
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    options = data.get("option") if isinstance(data, dict) else None
    if not isinstance(options, dict):
        raise OptionError("option_defs")
    return options


def _merge(target, override):
    for node, fields in (override or {}).items():
        if isinstance(fields, dict) and isinstance(target.get(node), dict):
            for key, value in fields.items():
                if isinstance(value, dict) and isinstance(target[node].get(key), dict):
                    target[node][key].update(copy.deepcopy(value))
                else:
                    target[node][key] = copy.deepcopy(value)
        else:
            target[node] = copy.deepcopy(fields)
    return target


def _substitute(value, inputs):
    """把 "{name}" 替换为输入值；整串恰为占位符时保留输入的类型（如 int）。"""
    if isinstance(value, dict):
        return {key: _substitute(item, inputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, inputs) for item in value]
    if isinstance(value, str):
        whole = re.fullmatch(r"\{(\w+)\}", value)
        if whole and whole.group(1) in inputs:
            return inputs[whole.group(1)]
        return re.sub(r"\{(\w+)\}", lambda m: str(inputs.get(m.group(1), m.group(0))), value)
    return value


def _convert(key, text, pipeline_type):
    if pipeline_type == "int":
        try:
            return int(text)
        except (TypeError, ValueError):
            raise OptionError(key) from None
    return text


def build_override(option_defs, values, keys=OPTION_KEYS):
    """按 CloudGame.json 的选项定义生成 pipeline_override，规则同 MXU 生成任务覆盖：
    未传的选项取默认值；传了但无效（未知 case、未通过 verify、类型不符）直接报错，不猜测。
    """
    override = {}
    for key in keys:
        definition = option_defs.get(key)
        if not isinstance(definition, dict):
            raise OptionError(key)
        value = values.get(key)
        kind = definition.get("type", "select")
        if kind == "input":
            if value is not None and not isinstance(value, dict):
                raise OptionError(key)
            inputs = {}
            for item in definition.get("inputs") or []:
                name = item.get("name")
                text = (value or {}).get(name, item.get("default", ""))
                if not isinstance(text, str):
                    text = str(text)
                verify = item.get("verify")
                if verify and not re.search(verify, text):
                    raise OptionError(key)
                inputs[name] = _convert(key, text, item.get("pipeline_type", "string"))
            _merge(override, _substitute(definition.get("pipeline_override") or {}, inputs))
            continue
        name = definition.get("default_case") if value is None else value
        case = next((c for c in definition.get("cases") or [] if c.get("name") == name), None)
        if not isinstance(name, str) or case is None:
            raise OptionError(key)
        _merge(override, case.get("pipeline_override") or {})
    return override


def focus_text(focus):
    """从 Context 事件的 focus 中取出 PrintT 发出的提示文本；Pipeline 自带的 $ 键留给界面翻译。"""
    if not isinstance(focus, dict):
        return None
    text = focus.get("Node.Action.Starting")
    if not isinstance(text, str) or not text.strip() or text.startswith("$"):
        return None
    return text


class InstanceGuard:
    """同一专用 profile 只允许一个启动流程：新流程先通知旧流程停止，等它退出后再接管。

    命名互斥体在进程崩溃时由系统释放（WAIT_ABANDONED 视为获得）；只在 Windows 上使用。
    """

    _WAIT_OBJECT_0, _WAIT_ABANDONED, _WAIT_TIMEOUT = 0, 0x80, 0x102

    def __init__(self, key):
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateEventW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        for name in ("SetEvent", "ResetEvent", "ReleaseMutex", "CloseHandle"):
            getattr(kernel, name).argtypes = [ctypes.c_void_p]
            getattr(kernel, name).restype = ctypes.c_int
        self._kernel, self.owned = kernel, False
        self._mutex = kernel.CreateMutexW(None, False, "Local\\MaaNTE.CloudWebPretask.%s.lock" % digest)
        self._event = kernel.CreateEventW(None, True, False, "Local\\MaaNTE.CloudWebPretask.%s.stop" % digest)
        if not self._mutex or not self._event:
            self.close()
            raise OSError("instance_guard")

    def _wait(self, handle, seconds):
        return self._kernel.WaitForSingleObject(handle, max(0, int(seconds * 1000)))

    def acquire(self, timeout, log=None):
        """返回 True 表示已获得；旧流程在 timeout 秒内未退出返回 False。"""
        result = self._wait(self._mutex, 0)
        if result == self._WAIT_TIMEOUT:
            if log:
                log("上一次云异环网页启动流程仍在运行，通知其停止并接管")
            deadline = time.monotonic() + timeout
            while result == self._WAIT_TIMEOUT and time.monotonic() < deadline:
                # 多个请求同时等待时，新持有者可能刚重置了事件；持续通知，直到真正接管。
                self._kernel.SetEvent(self._event)
                result = self._wait(self._mutex, min(0.2, max(0, deadline - time.monotonic())))
        if result not in (self._WAIT_OBJECT_0, self._WAIT_ABANDONED):
            return False
        self.owned = True
        self._kernel.ResetEvent(self._event)
        return True

    def stop_requested(self):
        return self._wait(self._event, 0) == self._WAIT_OBJECT_0

    def close(self):
        if self.owned and self._mutex:
            self._kernel.ReleaseMutex(self._mutex)
            self.owned = False
        for attr in ("_event", "_mutex"):
            handle = getattr(self, attr, None)
            if handle:
                self._kernel.CloseHandle(handle)
                setattr(self, attr, None)


def _watch(guard, state, log):
    """新流程请求接管时停止本进程的 Tasker；提交任务与停止共享锁，避免漏掉停止信号。"""
    while not state.done:
        if guard.stop_requested():
            with state.lock:
                state.stop_requested = True
                log("收到新的云异环网页启动请求，停止当前流程")
                if state.tasker is not None:
                    state.tasker.post_stop()
            return
        time.sleep(0.1)


def _blank_controller():
    """Tasker 每轮都会截图；网页与游戏画面改由 CDP 截取，这里只给空白帧并拒绝一切输入。"""
    import numpy as np
    from maa.controller import CustomController

    class BlankController(CustomController):
        def connect(self):
            return True

        def request_uuid(self):
            return "maante-cloud-web-pretask"

        def start_app(self, intent):
            return False

        def stop_app(self, intent):
            return False

        def screencap(self):
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        def click(self, x, y):
            return False

        def swipe(self, x1, y1, x2, y2, duration):
            return False

        def touch_down(self, contact, x, y, pressure):
            return False

        def touch_move(self, contact, x, y, pressure):
            return False

        def touch_up(self, contact):
            return False

        def click_key(self, keycode):
            return False

        def input_text(self, text):
            return False

        def key_down(self, keycode):
            return False

        def key_up(self, keycode):
            return False

    return BlankController()


def _focus_sink(log):
    """把 PrintT 发出的提示写进日志：MXU 不显示预任务输出，这是用户唯一能回看的记录。"""
    from maa.context import ContextEventSink

    class FocusSink(ContextEventSink):
        def on_node_action(self, context, noti_type, detail):
            try:
                text = focus_text(detail.focus) if int(noti_type) == _STARTING else None
                if text:
                    log("提示 | %s", text)
            except Exception:
                pass

    return FocusSink()


def load_flow():
    """按模块名导入流程逻辑；不经过 custom 包，避免连带导入全部动作与 AgentServer。"""
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))
    return importlib.import_module("cloud_web_flow")


def configure_framework(log_dir):
    """MaaFramework 原生日志写到本预任务目录，避免与 MXU 进程的 debug/maafw.log 互相覆盖。"""
    from maa.define import LoggingLevelEnum
    from maa.tasker import Tasker

    Tasker.set_log_dir(str(log_dir))
    Tasker.set_stdout_level(LoggingLevelEnum.Off)
    Tasker.set_save_draw(False)
    Tasker.set_save_on_error(False)  # 控制器只有空白帧，出错截图没有意义
    Tasker.set_debug_mode(False)


def run_flow(bundle, override, actions, state, log, entry=ENTRY, controller=None):
    """进程内运行 entry；返回 (退出码, 原因)。Tasker 存在 state.tasker 上，由调用方释放。"""
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    if not resource.post_bundle(str(bundle)).wait().succeeded:
        return EXIT_INIT_FAILED, "resource_load_failed"
    for name, action_type in actions.items():
        if not resource.register_custom_action(name, action_type()):
            return EXIT_INIT_FAILED, "action_register_failed"
    if controller is None:
        controller = _blank_controller()
    if not controller.post_connection().wait().succeeded:
        return EXIT_INIT_FAILED, "controller_connect_failed"
    tasker = Tasker()
    tasker.add_context_sink(_focus_sink(log))
    if not tasker.bind(resource, controller) or not tasker.inited:
        return EXIT_INIT_FAILED, "tasker_bind_failed"
    with state.lock:
        state.tasker = tasker
        if state.stop_requested:
            return EXIT_FAILED, "taken_over"
        job = tasker.post_task(entry, override)
    job.wait()
    if state.stop_requested:
        return EXIT_FAILED, "taken_over"
    return (EXIT_OK, "completed") if job.succeeded else (EXIT_FAILED, "flow_failed")


def missing_modules(names=REQUIRED_MODULES):
    missing = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def _dev_python():
    """开发环境下 MXU 用系统 python 启动预任务；依赖装在仓库 .venv 里（与 main.py 一致）。"""
    candidate = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not candidate.is_file():
        return None
    try:
        if Path(sys.executable).resolve() == candidate.resolve():
            return None
    except OSError:
        return None
    return candidate


def _null_streams():
    """pythonw 下 sys.stdout/stderr 为 None，日志模块导入时会因此报错。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _bootstrap_log(log_dir, message):
    """依赖缺失时 utils.logger 不可用，只用标准库追加一行。"""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "bootstrap.log", "a", encoding="utf-8") as file:
            file.write("%s | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def _install_local_dependencies(log_dir):
    """本地组装包首次运行还没有 Agent 安装依赖，只从随包 wheels 安装网页流程所需部分。"""
    requirements = ROOT / "requirements.txt"
    wheels = ROOT / "deps"
    config = ROOT / "config" / "pip_config.json"
    try:
        if config.is_file() and json.loads(config.read_text(encoding="utf-8")).get("enable_pip_install") is False:
            return False
        if not requirements.is_file() or not any(wheels.glob("*.whl")):
            return False
        packages = {"maafw", "numpy", "opencv-python", "websockets", "loguru"}
        specs = [
            line.strip()
            for line in requirements.read_text(encoding="utf-8").splitlines()
            if re.split(r"[<>=!~;\[\s]", line.strip(), maxsplit=1)[0].lower() in packages
        ]
        if not specs:
            return False
        log_dir.mkdir(parents=True, exist_ok=True)
        _bootstrap_log(log_dir, "从随包 wheels 安装网页启动依赖 | stage=dependencies.install")
        with open(log_dir / "pip.log", "a", encoding="utf-8") as output:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheels),
                    "--no-warn-script-location",
                    *specs,
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        importlib.invalidate_caches()
        return result.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _bootstrap_log(log_dir, "安装依赖失败 | error=%s" % type(exc).__name__)
        return False


def _ensure_dependencies(log_dir):
    """检查和安装也需串行：另一个预任务的 pip 可能尚在替换同一 Python 的包文件。"""
    guard = None
    try:
        if sys.platform == "win32":
            guard = InstanceGuard("dependencies:" + os.path.normcase(str(Path(sys.prefix).resolve())))
            if not guard.acquire(600):
                _bootstrap_log(log_dir, "等待依赖安装超时 | reason=dependencies_busy")
                return False
        importlib.invalidate_caches()
        if missing_modules():
            _install_local_dependencies(log_dir)
        missing = missing_modules()
        if missing:
            _bootstrap_log(
                log_dir,
                "缺少依赖 %s，请检查安装包或安装 requirements.txt | reason=missing_dependencies" % ",".join(missing),
            )
        return not missing
    except OSError as exc:
        _bootstrap_log(log_dir, "依赖初始化失败 | error=%s" % type(exc).__name__)
        return False
    finally:
        if guard is not None:
            guard.close()


def _setup_logging(log_dir):
    """日志单独成目录：被接管前的旧进程可能与 Agent 同时运行，不共用同一个滚动日志文件。"""
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))
    app_logging = importlib.import_module("utils.logger")
    app_logging.setup_logger(log_dir=str(log_dir), console_level="INFO")
    return app_logging.logger


def _profile_key():
    from cloud_web_start import default_profile

    return os.path.normcase(str(default_profile().resolve()))


def _run(base, args, log_dir, log):
    if base is None:
        log.error("未找到 resource/base 或 CloudGame.json，请检查安装目录 | reason=resource_missing")
        return EXIT_INIT_FAILED, "resource_missing"
    if len(args) > 1:
        log.error("预任务参数无效 | reason=invalid_arguments")
        return EXIT_BAD_OPTIONS, "invalid_arguments"
    try:
        override = build_override(load_option_defs(base / TASKS_FILE), parse_options(args[0] if args else None))
    except (OptionError, OSError, ValueError) as exc:
        log.error("云异环启动选项无效 | option=%s", exc.args[0] if isinstance(exc, OptionError) else type(exc).__name__)
        return EXIT_BAD_OPTIONS, "invalid_options"
    if sys.platform != "win32":
        log.error("云异环网页启动仅支持 Windows | reason=windows_required")
        return EXIT_INIT_FAILED, "windows_required"
    try:
        guard = InstanceGuard(_profile_key())
    except OSError:
        log.error("无法创建单实例保护 | reason=instance_guard_failed")
        return EXIT_INIT_FAILED, "instance_guard_failed"
    state = SimpleNamespace(tasker=None, stop_requested=False, done=False, lock=threading.Lock())
    flow, watcher = None, None
    try:
        if not guard.acquire(TAKEOVER_TIMEOUT, log.warning):
            log.error("上一次云异环网页启动流程未在 %d 秒内退出 | reason=previous_flow_busy", TAKEOVER_TIMEOUT)
            return EXIT_BUSY, "previous_flow_busy"
        watcher = threading.Thread(target=_watch, args=(guard, state, log.warning), daemon=True)
        watcher.start()
        log.debug("云异环启动选项 | override=%s", json.dumps(override, ensure_ascii=False, sort_keys=True))
        configure_framework(log_dir)
        importlib.import_module("utils.i18n").init()
        flow = load_flow()
        return run_flow(base / RESOURCE_BUNDLE, override, flow.ACTIONS, state, log.info)
    except Exception as exc:  # 只记录类型，不输出异常正文
        log.error("云异环网页预任务异常 | error=%s", type(exc).__name__)
        return EXIT_FAILED, "unexpected_" + type(exc).__name__
    finally:
        state.done = True
        if watcher is not None:
            watcher.join(timeout=1.0)
        # wait(task) 结束时仍可能有停止请求留在原生调度线程中；先排空再销毁，避免访问已释放句柄。
        if state.tasker is not None:
            state.tasker.post_stop().wait()
        if flow is not None:
            try:
                flow.cleanup_cloud_web_session()
            except Exception:
                pass
        state.tasker = None
        guard.close()


def main(argv=None):
    started = time.monotonic()
    args = list(sys.argv[1:] if argv is None else argv)
    _null_streams()
    base = interface_dir()
    log_dir = (base if base is not None else ROOT) / "debug" / LOG_DIR_NAME / str(os.getpid())
    missing = missing_modules()
    if missing:
        dev = None if os.environ.get(_RELAUNCH_ENV) else _dev_python()
        if dev is not None:
            env = dict(os.environ, **{_RELAUNCH_ENV: "1"})
            return subprocess.run([str(dev), str(Path(__file__).resolve()), *args], env=env, check=False).returncode
    if not _ensure_dependencies(log_dir):
        return EXIT_MISSING_DEPS
    os.environ.setdefault("PI_CONTROLLER", json.dumps({"name": CONTROLLER, "type": "Win32"}))
    try:
        log = _setup_logging(log_dir)
    except Exception as exc:
        _bootstrap_log(log_dir, "日志初始化失败 | error=%s" % type(exc).__name__)
        return EXIT_INIT_FAILED
    log.info("云异环网页预任务开始 | pid=%d", os.getpid())
    code, reason = _run(base, args, log_dir, log)
    (log.info if code == EXIT_OK else log.error)(
        "云异环网页预任务结束 | result=%s | reason=%s | exit_code=%d | elapsed=%.1fs",
        "succeeded" if code == EXIT_OK else "failed",
        reason,
        code,
        time.monotonic() - started,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
