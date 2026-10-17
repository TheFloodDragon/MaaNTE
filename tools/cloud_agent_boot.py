"""云诊断 Agent 引导；由 cloud_live_run.py 启动，不属于发行内容。

用法：python tools/cloud_agent_boot.py <identifier>
保留 PI_CONTROLLER 接口，但不输出连接标识/原始 PI。应用日志只留脱敏元数据；
原生框架日志仍可能包含 OCR/路径，保存在本次受控诊断目录，分享前人工检查。
"""

import contextlib
import importlib
import logging
import os
import sys
from types import SimpleNamespace

if __package__:
    from .cloud_live_run import AGENT, Diagnostics, SafeParser, bounded_call, load_framework, require, safe_reason
else:
    from cloud_live_run import AGENT, Diagnostics, SafeParser, bounded_call, load_framework, require, safe_reason


class _MetadataHandler(logging.Handler):
    """不信任注册动作的自由文本/异常；每个来源级别最多一条摘要。"""

    def __init__(self, diagnostics):
        super().__init__()
        self.diagnostics = diagnostics
        self.seen = set()

    def emit(self, record):
        source = record.module if record.module in {"cloud_game", "cloud_start", "cloud_window", "i18n"} else "other"
        level = record.levelname if record.levelname in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "OTHER"
        key = (source, level)
        if key not in self.seen:
            self.seen.add(key)
            self.diagnostics.event("runtime", "event", source=source, level=level)


def load_agent(diagnostics):
    # 仅 CLI 子进程内改变 cwd：即便旧 logger 导入时自动建目录，也不会碰原 debug。
    os.chdir(diagnostics.path)
    if str(AGENT) not in sys.path:
        sys.path.insert(0, str(AGENT))
    from maa.agent.agent_server import AgentServer

    api = load_framework()
    diagnostics.configure_framework(api)
    app_logging = importlib.import_module("utils.logger")
    if getattr(app_logging, "_HAS_LOGURU", False):
        app_logging._loguru_logger.remove()
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.addHandler(_MetadataHandler(diagnostics))
    root_logger.setLevel(logging.DEBUG)
    from utils.i18n import init as i18n_init

    importlib.import_module("custom")  # 触发所有 AgentServer 注册，维持原工具用途。
    from custom.action.cloud_game import cleanup_cloud_session

    return SimpleNamespace(server=AgentServer, i18n_init=i18n_init, cleanup=cleanup_cloud_session)


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("identifier")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    diag = runtime = None
    code = 1
    previous_cwd = os.getcwd()
    try:
        diag = Diagnostics("AgentServer", run_id=os.environ.get("CLOUD_TOOLS_RUN_ID"), agent=True, fingerprints=False)
        # 第三方 Python 输出不通过应用级摘要白名单，直接丢弃，不转存原文。
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            runtime = load_agent(diag)
            runtime.i18n_init()
            diag.event("i18n", "ready")
            # 在可能需要对端参与的原生握手前发布标记，避免双方相互等待。
            diag.mark_agent_bootstrap_ready()
            require(runtime.server.start_up(args.identifier), "agent_startup")
            diag.event("agent", "started")
            runtime.server.join()
            code = 0
    except KeyboardInterrupt:
        code = 130
        if diag:
            diag.event("agent", "interrupted")
    except Exception as exc:
        if diag:
            diag.event("agent", "failed", reason=safe_reason(exc))
        else:
            sys.stderr.write("cloud-tools: agent_initialization_failed\n")
    finally:
        if runtime is not None:
            for phase, callback in (("session_cleanup", runtime.cleanup), ("agent_shutdown", runtime.server.shut_down)):
                try:
                    bounded_call(callback, 2.0)
                    diag.event(phase, "done")
                except BaseException as exc:
                    diag.event(phase, "failed", reason="cleanup_incomplete")
                    if isinstance(exc, KeyboardInterrupt):
                        code = 130
                    elif code == 0:
                        code = 1
        os.chdir(previous_cwd)
        if diag:
            diag.event("summary", "succeeded" if code == 0 else "interrupted" if code == 130 else "failed", exit_code=code)
            diag.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
