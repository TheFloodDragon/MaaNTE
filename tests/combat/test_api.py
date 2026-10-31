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
    def __init__(self, image, nodes=None, enemy=True, stop_after=None, signals=None):
        controller = FakeController(image)
        self.tasker = type("Tasker", (), {})()
        self.tasker.controller = controller
        self.nodes = nodes or {}
        self.enemy = enemy
        self.signals = signals or {}  # 调用方信号节点名 -> 是否命中
        self.recognitions = 0
        self.seen: set[str] = set()
        self.stop_after = stop_after
        type(self.tasker).stopping = property(lambda _: self._stopping())

    def _stopping(self):
        return self.stop_after is not None and self.recognitions >= self.stop_after

    def get_node_data(self, name):
        return self.nodes.get(name)

    def run_recognition(self, entry, image, pipeline_override=None):
        self.recognitions += 1
        self.seen.add(entry)
        if entry == api.ENEMY_NODE:
            hit = self.enemy
        elif entry in self.signals:
            hit = self.signals[entry]
        else:
            hit = False  # 等级/Boss/撤离提示等：明确未命中
        return type("Detail", (), {"hit": hit})()


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
        result = combat.run_combat(
            ctx, overrides={"custom_scheme": "definitely_missing_scheme"}
        )
        self.assertEqual(result.reason, "scheme_error")
        self.assertFalse(result.success)
        self.assertEqual(ctx.tasker.controller.calls, [])
        self.assertTrue(
            PRINTED[0].startswith("combat.scheme_error|combat.scheme.custom_not_found|")
        )

    def test_no_combat_on_black_screen(self):
        ctx = FakeContext(np.zeros((720, 1280, 3), dtype=np.uint8))
        result = combat.run_combat(
            ctx, overrides={"wait_timeout": 0.5, "scheme": "basic_attack"}
        )
        self.assertEqual(result.reason, "no_combat")
        self.assertEqual(ctx.recognitions, 0)  # 黑屏不做敌人识别
        self.assertIn("combat.ended_no_fight", PRINTED)

    def test_fight_until_stopped_sends_attacks_at_frame_center(self):
        ctx = FakeContext(hud_frame(), stop_after=12)
        result = combat.run_combat(
            ctx, overrides={"scheme": "basic_attack", "max_duration": 30}
        )
        self.assertEqual(result.reason, "stopped")
        clicks = [
            args for name, args in ctx.tasker.controller.args if name == "post_click"
        ]
        self.assertTrue(
            all(args[:2] == (640, 360) for args in clicks), clicks[:3]
        )  # 截图坐标系中心
        self.assertEqual(clicks[0][2], 2)  # 默认开启锁敌：开战先按中键
        self.assertTrue(
            len(clicks) > 1 and all(args[2] == 0 for args in clicks[1:]), clicks[:3]
        )  # 之后都是左键普攻
        self.assertEqual(
            PRINTED[0],
            "combat.plan.header|combat.scheme_name.basic_attack|combat.plan.from_builtin"
            "|1|combat.plan.stay",
        )  # 摘要只写来源类别，不暴露安装路径
        self.assertEqual(
            PRINTED[1:5],
            [
                "combat.plan.slot|%d|combat.team.any|combat.plan.source.default|l_click(1)|0"
                % slot
                for slot in (1, 2, 3, 4)
            ],
        )
        self.assertEqual(PRINTED[5], "combat.waiting|combat.scheme_name.basic_attack")
        self.assertEqual(PRINTED[6], "combat.started|combat.scheme_name.basic_attack")
        self.assertTrue(PRINTED[-1].startswith("combat.ended|combat.reason.stopped|"))

    def test_summary_repeats_only_when_effective_plan_changes(self):
        def run(overrides):
            combat.run_combat(
                FakeContext(np.zeros((720, 1280, 3), dtype=np.uint8)),
                overrides={"wait_timeout": 0.3, **overrides},
            )

        run({})
        run({})
        run({"slots": {"2": {"combo": "jump", "character": "零"}}})
        headers = [p for p in PRINTED if p.startswith("combat.plan.header|")]
        self.assertEqual(len(headers), 2)
        self.assertIn(
            "combat.plan.slot|2|零|combat.plan.source.override|jump|1", PRINTED
        )

    def test_disabled_slot_shown_and_cycle_summary(self):
        combat.run_combat(
            FakeContext(np.zeros((720, 1280, 3), dtype=np.uint8)),
            overrides={
                "wait_timeout": 0.3,
                "rotation_order": "3,1",
                "slots": {"2": {"enabled": False}},
                "combat_speed": 1.25,
            },
        )
        self.assertTrue(
            PRINTED[0].endswith("|1.25|combat.plan.cycle|3→1|3"), PRINTED[0]
        )
        self.assertIn("combat.plan.slot_off|2", PRINTED)

    def test_invalid_new_option_fails_before_input(self):
        for overrides, code in (
            ({"combat_speed": 5}, "bad_option"),
            ({"slots": {"1": {"combo": "perform"}}}, "slot_combo"),
            ({"slots": {s: {"enabled": False} for s in "1234"}}, "no_enabled_slot"),
        ):
            PRINTED.clear()
            ctx = FakeContext(hud_frame())
            result = combat.run_combat(ctx, overrides=overrides)
            self.assertEqual(result.reason, "scheme_error", overrides)
            self.assertEqual(ctx.tasker.controller.calls, [], overrides)
            self.assertTrue(
                PRINTED[0].startswith("combat.scheme_error|combat.scheme.%s|" % code)
                or PRINTED[0] == "combat.scheme_error|combat.scheme.%s" % code,
                PRINTED[0],
            )

    def test_signal_nodes_validated_and_used(self):
        nodes = {
            "Mine_Victory": {"recognition": {"type": "OCR", "param": {}}},
            "Mine_Direct": {"recognition": {"type": "DirectHit"}},
            "Mine_Off": {"recognition": {"type": "OCR"}, "enabled": False},
        }
        for signal in (
            {"cleared": ["Mine_Direct"]},
            {"cleared": ["Mine_Off"]},
            {"cleared": ["Missing"]},
            {"victory": ["Mine_Victory"]},
            {"cleared": [""]},
            ["Mine_Victory"],
        ):
            ctx = FakeContext(hud_frame(), nodes)
            result = combat.run_combat(ctx, signal_nodes=signal)
            self.assertEqual(result.reason, "scheme_error", signal)
            self.assertEqual(ctx.tasker.controller.calls, [], signal)
        # 进战后敌人消失、调用方的胜利节点命中：两张新图确认后以 cleared 结束
        ctx = FakeContext(hud_frame(), nodes)
        ctx.enemy_until = 4  # 前几次敌人识别命中，之后消失
        calls = {"n": 0}
        original = ctx.run_recognition

        def scripted_recognition(entry, image, pipeline_override=None):
            if entry == api.ENEMY_NODE:
                calls["n"] += 1
                ctx.enemy = calls["n"] <= ctx.enemy_until
            if entry == "Mine_Victory":
                ctx.signals["Mine_Victory"] = not ctx.enemy
            return original(entry, image, pipeline_override)

        ctx.run_recognition = scripted_recognition
        result = combat.run_combat(
            ctx,
            overrides={"scheme": "basic_attack", "max_duration": 30},
            signal_nodes={"cleared": "Mine_Victory"},
        )
        self.assertIn("Mine_Victory", ctx.seen)
        self.assertEqual((result.reason, result.success), ("cleared", True))
        self.assertTrue(result.confirmed_end)
        self.assertIn(("cleared", True), result.evidence)

    def test_prepare_combat_has_no_side_effects(self):
        ctx = FakeContext(hud_frame())
        plan = combat.prepare_combat(
            ctx,
            overrides={"rotation_order": "2,1", "slots": {"1": {"combo": "skill"}}},
            scheme_data={"version": 1, "combos": {"default": "jump"}},
        )
        self.assertEqual((plan.order, plan.opener), ((2, 1), 2))
        self.assertEqual(plan.slots[0].combo, "skill")
        self.assertEqual(plan.slots[1].combo, "jump")
        self.assertEqual(ctx.tasker.controller.calls, [])
        self.assertEqual(ctx.recognitions, 0)
        self.assertEqual(PRINTED, [])

    def test_team_hint_printed_once_per_scheme(self):
        nodes = {"CombatSetting_Scheme": {"attach": {"scheme": "main_dps"}}}
        for _ in range(2):
            combat.run_combat(
                FakeContext(np.zeros((720, 1280, 3), dtype=np.uint8), nodes),
                overrides={"wait_timeout": 0.3},
            )
        hints = [line for line in PRINTED if line.startswith("combat.team_hint|")]
        self.assertEqual(len(hints), 1)
        self.assertIn(
            "combat.team.slot|1|combat.team.any (combat.team.main_dps)", hints[0]
        )

    def test_strategy_hint_printed_once_for_non_default_strategy(self):
        overrides = {"wait_timeout": 0.3, "team_size": 3, "use_ultimate": False}
        for _ in range(2):
            combat.run_combat(
                FakeContext(np.zeros((720, 1280, 3), dtype=np.uint8)),
                overrides=overrides,
            )
        hints = [line for line in PRINTED if line.startswith("combat.strategy_hint|")]
        self.assertEqual(
            hints,
            [
                "combat.strategy_hint|combat.strategy.team_size|3 | combat.strategy.disabled|combat.action.ultimate"
            ],
        )

    def test_default_strategy_prints_no_hint(self):
        combat.run_combat(
            FakeContext(np.zeros((720, 1280, 3), dtype=np.uint8)),
            overrides={"wait_timeout": 0.3},
        )
        self.assertFalse(
            [line for line in PRINTED if line.startswith("combat.strategy_hint|")]
        )

    def test_strategy_options_reach_engine(self):
        options, _, _ = combat.merge_options_strict(
            [
                {
                    "team_size": 2,
                    "lock_target": False,
                    "search_turn": True,
                    "use_switch": False,
                }
            ]
        )
        scheme = combat.load_scheme("main_dps")
        plan = combat.apply_overrides(scheme, options)
        engine = api.build_engine(
            FakeContext(hud_frame()), plan, options, combat.Reporter()
        )
        self.assertEqual(engine.order, [1, 2])
        self.assertFalse(engine.switch_allowed)
        self.assertEqual(
            (engine.targeting.lock, engine.targeting.search), (False, True)
        )
        self.assertEqual(engine.config.disabled, frozenset({"switch"}))

    def test_describe_combo_error_and_display(self):
        err = combat.SchemeError(
            "combo",
            "2",
            combo_error=combat.ComboError("unknown_command", 3, 5, "perform"),
        )
        text = api.describe_scheme_error(err)
        self.assertEqual(
            text,
            "combat.scheme.combo|2|combat.dsl.at|3|5combat.dsl.unknown_command|perform",
        )
        self.assertEqual(api.display("$combat.team.any"), "combat.team.any")
        self.assertEqual(api.display("零"), "零")


if __name__ == "__main__":
    unittest.main()
