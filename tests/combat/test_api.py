"""run_combat 入口集成测试：假 context（节点数据、识别、控制器），真实时钟的短时场景。"""

import unittest

import numpy as np

from ._loader import PRINTED, load, submodule

combat = load()
api = submodule("api")


class _Job:
    succeeded = True

    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class FakeController:
    def __init__(self, image):
        self.image = image
        self.calls = []
        self.args = []

    def post_screencap(self):
        self.calls.append("screencap")
        return _Job(self.image)

    def __getattr__(self, name):  # post_click / post_key_down / ...
        def record(*args):
            self.calls.append(name)
            self.args.append((name, args))
            return _Job()

        return record


class FakeContext:
    def __init__(self, image, nodes=None, enemy=True, stop_after=None):
        controller = FakeController(image)
        self.tasker = type("Tasker", (), {})()
        self.tasker.controller = controller
        self.nodes = nodes or {}
        self.enemy = enemy
        self.recognitions = 0
        self.stop_after = stop_after
        type(self.tasker).stopping = property(lambda _: self._stopping())

    def _stopping(self):
        return self.stop_after is not None and self.recognitions >= self.stop_after

    def get_node_data(self, name):
        return self.nodes.get(name)

    def run_recognition(self, entry, image, pipeline_override=None):
        assert entry == api.ENEMY_NODE
        self.recognitions += 1
        return type("Detail", (), {"hit": self.enemy})()


def hud_frame():
    img = np.full((720, 1280, 3), 60, dtype=np.uint8)
    img[668:676, 636:646] = 230  # 底部血条斜杠区域
    img[164:200, 1168:1236] = 255  # 1 号位高亮
    return img


class RunCombatTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()
        api._announced.clear()

    def test_scheme_error_reported_without_input(self):
        nodes = {"CombatSetting_Scheme": {"attach": {"scheme": "custom"}}}
        ctx = FakeContext(hud_frame(), nodes)
        result = combat.run_combat(ctx, overrides={"custom_scheme": "definitely_missing_scheme"})
        self.assertEqual(result.reason, "scheme_error")
        self.assertFalse(result.success)
        self.assertEqual(ctx.tasker.controller.calls, [])
        self.assertTrue(PRINTED[0].startswith("combat.scheme_error|combat.scheme.custom_not_found|"))

    def test_no_combat_on_black_screen(self):
        ctx = FakeContext(np.zeros((720, 1280, 3), dtype=np.uint8))
        result = combat.run_combat(ctx, overrides={"wait_timeout": 0.5, "scheme": "basic_attack"})
        self.assertEqual(result.reason, "no_combat")
        self.assertEqual(ctx.recognitions, 0)  # 黑屏不做敌人识别
        self.assertIn("combat.ended_no_fight", PRINTED)

    def test_fight_until_stopped_sends_attacks_at_frame_center(self):
        ctx = FakeContext(hud_frame(), stop_after=12)
        result = combat.run_combat(ctx, overrides={"scheme": "basic_attack", "max_duration": 30})
        self.assertEqual(result.reason, "stopped")
        clicks = [args for name, args in ctx.tasker.controller.args if name == "post_click"]
        self.assertTrue(clicks)
        self.assertTrue(all(args == (640, 360, 0) for args in clicks), clicks[:3])  # 截图坐标系中心、左键
        self.assertEqual(PRINTED[0], "combat.waiting|combat.scheme_name.basic_attack")
        self.assertEqual(PRINTED[1], "combat.started|combat.scheme_name.basic_attack")
        self.assertTrue(PRINTED[-1].startswith("combat.ended|combat.reason.stopped|"))

    def test_team_hint_printed_once_per_scheme(self):
        nodes = {"CombatSetting_Scheme": {"attach": {"scheme": "main_dps"}}}
        for _ in range(2):
            combat.run_combat(FakeContext(np.zeros((720, 1280, 3), dtype=np.uint8), nodes), overrides={"wait_timeout": 0.3})
        hints = [line for line in PRINTED if line.startswith("combat.team_hint|")]
        self.assertEqual(len(hints), 1)
        self.assertIn("combat.team.slot|1|combat.team.any (combat.team.main_dps)", hints[0])

    def test_describe_combo_error_and_display(self):
        err = combat.SchemeError("combo", "2", combo_error=combat.ComboError("unknown_command", 3, 5, "perform"))
        text = api.describe_scheme_error(err)
        self.assertEqual(text, "combat.scheme.combo|2|combat.dsl.at|3|5combat.dsl.unknown_command|perform")
        self.assertEqual(api.display("$combat.team.any"), "combat.team.any")
        self.assertEqual(api.display("零"), "零")


if __name__ == "__main__":
    unittest.main()
