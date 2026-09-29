"""对给定 PNG 回放指定识别节点，不枚举窗口、不连接云客户端、不发送输入。

兼容：python tools/cloud_recog_image.py <png> <Node1> [Node2 ...]
可追加 --expect-hit Node / --expect-miss Node（可重复）进行断言；不符返回 1。
不指定节点时保留原来的两项确认弹窗节点。默认仅输出命中、数量、耗时；
诊断目录中的原生 Maa 日志仍可能含 OCR/路径，不能直接对外分享。
"""

import sys

if __package__:
    from .cloud_live_run import (
        RESOURCE, Diagnostics, SafeParser, bind_tasker, load_framework, node_name,
        require, safe_reason, stop_tasker, task_exit_code, wait_job,
    )
else:
    from cloud_live_run import (
        RESOURCE, Diagnostics, SafeParser, bind_tasker, load_framework, node_name,
        require, safe_reason, stop_tasker, task_exit_code, wait_job,
    )

DEFAULT_NODES = ["CloudGameStartConfirmNotice", "CloudGameStartConfirmEnter"]


def make_controller(api, image):
    class ImageController(api.CustomController):
        """只具有连接与取帧能力，所有输入回调显式拒绝。"""

        def connect(self):
            return True

        def request_uuid(self):
            return "cloud-image-replay"

        def get_features(self):
            return 0

        def screencap(self):
            return image.copy()

        def deny_input(self, *args):
            return False

        start_app = stop_app = click = swipe = touch_down = deny_input
        touch_move = touch_up = click_key = input_text = deny_input
        key_down = key_up = scroll = relative_move = deny_input

        def shell(self, *args):
            return None

    return ImageController()


def make_probe(api, diagnostics, image, nodes, expectations):
    class Probe(api.CustomAction):
        def run(self, context, argv):
            success = True
            try:
                for node in nodes:
                    detail = context.run_recognition(node, image)
                    require(detail is not None, "recognition_detail")
                    hit = bool(detail.hit)
                    diagnostics.event("recognition", "done", entry=node, hit=hit, count=len(detail.filtered_results))
                    if node in expectations and hit != expectations[node]:
                        diagnostics.event("assertion", "failed", entry=node)
                        success = False
            except BaseException:
                diagnostics.event("recognition", "failed", reason="recognition_failed")
                success = False
            return api.CustomAction.RunResult(success=success)

    return Probe()


def parse_args(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("png")
    parser.add_argument("nodes", nargs="*", type=node_name)
    parser.add_argument("--expect-hit", action="append", default=[], type=node_name, metavar="NODE")
    parser.add_argument("--expect-miss", action="append", default=[], type=node_name, metavar="NODE")
    args = parser.parse_args(argv)
    args.nodes = args.nodes or list(DEFAULT_NODES)
    hit, miss = set(args.expect_hit), set(args.expect_miss)
    if hit & miss or not (hit | miss).issubset(args.nodes):
        parser.error("invalid_expectations")
    return args


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    diag = tasker = None
    code = 1
    try:
        diag = Diagnostics("ProbeImg")
        import numpy as np
        from PIL import Image

        # 参数只用于读取；路径和图片原文绝不进入应用摘要。
        with Image.open(args.png) as source:
            require(source.format == "PNG", "png_required")
            image = np.asarray(source.convert("RGB"))[:, :, ::-1].copy()
        api = load_framework()
        diag.configure_framework(api)
        controller = make_controller(api, image)
        require(wait_job(controller.post_connection()).succeeded, "controller_connect")
        resource = api.Resource()
        require(wait_job(resource.post_bundle(RESOURCE)).succeeded, "resource_load")
        expectations = {node: True for node in args.expect_hit}
        expectations.update({node: False for node in args.expect_miss})
        probe = make_probe(api, diag, image, args.nodes, expectations)
        require(resource.register_custom_action("probe_img", probe), "probe_register")
        require(resource.override_pipeline({"ProbeImg": {
            "action": {"type": "Custom", "param": {"custom_action": "probe_img"}},
            "rate_limit": 0, "pre_delay": 0, "post_delay": 0,
        }}), "probe_override")
        tasker = bind_tasker(api, resource, controller)
        task = wait_job(tasker.post_task("ProbeImg"))
        code = task_exit_code(task)
        diag.event("task", "succeeded" if code == 0 else "failed", task_id=task.job_id)
    except KeyboardInterrupt:
        code = 130
        if diag:
            stop_tasker(tasker, diag)
    except Exception as exc:
        code = 1
        if diag:
            diag.event("replay", "failed", reason=safe_reason(exc))
            stop_tasker(tasker, diag)
        else:
            sys.stderr.write("cloud-tools: replay_initialization_failed\n")
    finally:
        if diag:
            diag.event("summary", "succeeded" if code == 0 else "interrupted" if code == 130 else "failed", exit_code=code)
            diag.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
