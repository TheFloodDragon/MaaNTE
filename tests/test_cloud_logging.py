"""Log backends run in disposable processes/cwds, never in repository debug/."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


LOGGER_PATH = Path(__file__).resolve().parents[1] / "agent/utils/logger.py"
WORKER = r'''
import contextlib
import importlib.util
import io
import json
import logging
from pathlib import Path
import sys
import types

path, backend, client = sys.argv[1:]
package = types.ModuleType("isolated_logging")
package.__path__ = []
env = types.ModuleType("isolated_logging.pienv")
env.client_name = lambda: client
package.pienv = env
sys.modules[package.__name__] = package
sys.modules[env.__name__] = env
if backend == "std":
    sys.modules["loguru"] = None
root = logging.getLogger()
old_handlers, old_level = root.handlers[:], root.level
out, err = io.StringIO(), io.StringIO()
module = None
try:
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        spec = importlib.util.spec_from_file_location("isolated_logging.logger", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        assert module._HAS_LOGURU == (backend == "loguru")
        module.logger.debug("file-only %d", 7)
        def caller():
            line = sys._getframe().f_lineno + 1
            module.logger.info("formatted %s %d %.2f <tag>&", "value", 3, 1.25)
            return line
        line = caller()
        module.get_logger("third.party").warning("third-party %s", "warning")
        secret = "PRIVATE_" + "LOCAL_" + str(918273645)
        def fail_safely():
            try:
                assert secret == "expected"
            except AssertionError:
                module.logger.exception("safe failure")
                if backend == "loguru":
                    module._loguru_logger.exception("direct safe failure")
        fail_safely()
        # Exercise console threshold independently of the default client level.
        if backend == "loguru":
            module._loguru_logger.complete()
            module._loguru_logger.remove()
        else:
            for handler in root.handlers:
                handler.flush()
                handler.close()
        module.setup_logger(log_dir="threshold", console_level="WARNING")
        module.logger.info("threshold-hidden")
        module.logger.warning("threshold-visible")
        if backend == "loguru":
            module._loguru_logger.complete()
        else:
            handler = root.handlers[-1]
            assert handler.when == "MIDNIGHT" and handler.backupCount == 14
            for handler in root.handlers:
                handler.flush()
    files = "\n".join(p.read_text(encoding="utf-8") for p in Path("debug/custom").glob("*.log"))
    Path("result.json").write_text(json.dumps({
        "stdout": out.getvalue(), "stderr": err.getvalue(), "files": files,
        "line": line, "secret": secret,
    }), encoding="utf-8")
finally:
    if module is not None and module._HAS_LOGURU:
        module._loguru_logger.complete()
        module._loguru_logger.remove()
    for handler in root.handlers:
        if handler not in old_handlers:
            handler.close()
    root.handlers = old_handlers
    root.setLevel(old_level)
'''


class CloudLoggingTests(unittest.TestCase):
    def check_backend(self, backend, client):
        with tempfile.TemporaryDirectory(prefix="maante-log-test-") as directory:
            # A real source file lets loguru inspect the failing expression;
            # using -c would hide locals even with diagnose=True.
            worker_path = Path(directory) / "worker.py"
            worker_path.write_text(WORKER, encoding="utf-8")
            process = subprocess.run(
                [sys.executable, "-B", str(worker_path), str(LOGGER_PATH), backend, client],
                cwd=directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(
                (Path(directory) / "result.json").read_text(encoding="utf-8")
            )
        console = result["stdout"] if client == "MXU" else result["stderr"]
        unused = result["stderr"] if client == "MXU" else result["stdout"]
        self.assertEqual(unused, "")
        self.assertIn("formatted value 3 1.25", console)
        self.assertNotIn("file-only", console)
        self.assertNotIn("threshold-hidden", console)
        self.assertIn("threshold-visible", console)
        if client == "MXU":
            self.assertIn('<span style="color:forestgreen;">', console)
            self.assertIn("&lt;tag&gt;&amp;", console)
        elif client == "MFAAvalonia":
            self.assertIn("info:formatted", console)
            self.assertIn("warn:third-party", console)
            self.assertNotIn("\033[", console)
        else:
            self.assertIn("\033[32mformatted", console)
        files = result["files"]
        self.assertIn("file-only 7", files)
        self.assertIn("formatted value 3 1.25 <tag>&", files)
        self.assertIn(":caller:%d |" % result["line"], files)
        self.assertNotIn("logging:callHandlers", files)
        self.assertIn("third-party warning", files)
        self.assertIn("AssertionError", files)
        self.assertIn("safe failure", files)
        self.assertNotIn(result["secret"], files + console)
        self.assertRegex(files, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        if backend == "loguru":
            self.assertIn("direct safe failure", files)

    def test_loguru_mxu(self):
        self.check_backend("loguru", "MXU")

    def test_loguru_mfaa(self):
        self.check_backend("loguru", "MFAAvalonia")

    def test_loguru_terminal(self):
        self.check_backend("loguru", "terminal")

    def test_std_mxu(self):
        self.check_backend("std", "MXU")

    def test_std_mfaa(self):
        self.check_backend("std", "MFAAvalonia")

    def test_std_terminal(self):
        self.check_backend("std", "terminal")


if __name__ == "__main__":
    unittest.main()
