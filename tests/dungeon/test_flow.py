"""flow 桥接逻辑：闸门路由、领取计次闭环、有反馈的移动、战斗调度、结束语义。"""

import unittest

from ._fakes import FakeClock, FakeContext, combat_result, options
from ._loader import PRINTED, submodule

flow = submodule("flow")
D = submodule("budget").Decision

GATE = {"balance_node": "DungeonBalance", "continue": ["DungeonClaim"], "finish": ["DungeonExit"]}
AGAIN = {"continue": ["DungeonAgain"], "finish": ["DungeonExit"]}


def farm(runs, balance, cost=40, max_rounds=50):
    """模拟 Pipeline：领奖弹窗闸门 → 点普通领取 → 结算确认 → 再次挑战 / 退出。

    返回 (context, 领取次数, 最终余额, 每轮结算后的去向)。
    """
    wallet = {"value": balance}
    ctx = FakeContext(options(runs=str(runs), cost=cost), {"DungeonBalance": lambda: str(wallet["value"])})
    assert flow.reset(ctx)
    routes = []
    for _ in range(max_rounds):
        ctx.overrides.clear()
        if not flow.gate(ctx, "DungeonClaimGate", GATE):
            break
        if ctx.overrides[-1][1] != ["DungeonClaim"]:
            routes.append("exit_at_gate")
            break
        assert flow.claim(ctx, [400, 520, 200, 60])
        wallet["value"] -= cost  # 游戏扣费
        assert flow.confirm(ctx, "DungeonSettlement", AGAIN)
        target = ctx.overrides[-1][1]
        routes.append("again" if target == ["DungeonAgain"] else "exit")
        if target != ["DungeonAgain"]:
            break
    return ctx, ctx.controller.count("click"), wallet["value"], routes


class ResetTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()

    def test_valid_config(self):
        ctx = FakeContext(options(runs="3"))
        self.assertTrue(flow.reset(ctx))
        state = flow.session().state
        self.assertEqual((state.target, state.cost, state.claimed), (3, 40, 0))
        self.assertIsNone(flow.session().finish)
        self.assertEqual(PRINTED[-1], "dungeon.start|dungeon.stage.clock_trick|dungeon.target_runs|3|40")

    def test_reset_clears_previous_run(self):
        farm(runs=1, balance=200)
        self.assertEqual(flow.session().state.claimed, 1)
        flow.reset(FakeContext(options()))
        self.assertEqual(flow.session().state.claimed, 0)
        self.assertIsNone(flow.session().finish)

    def test_invalid_config(self):
        for nodes in (options(runs="abc"), options(runs="-1"), options(cost=0), options(stage=""), {}):
            self.assertFalse(flow.reset(FakeContext(nodes)), nodes)
            self.assertEqual(flow.session().finish, D.CONFIG_ERROR)


class ClosedLoopTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()

    def test_single_run_exits_without_again(self):
        ctx, clicks, left, routes = farm(runs=1, balance=200)
        self.assertEqual((clicks, left, routes), (1, 160, ["exit"]))
        self.assertEqual(flow.session().finish, D.DONE)
        self.assertTrue(flow.finish(ctx))
        self.assertEqual(PRINTED[-1], "dungeon.finished.done|1")

    def test_n_runs_last_round_does_not_click_again(self):
        _, clicks, left, routes = farm(runs=3, balance=400)
        self.assertEqual((clicks, left), (3, 280))
        self.assertEqual(routes, ["again", "again", "exit"])

    def test_exhaust_mode_spends_until_short(self):
        ctx, clicks, left, routes = farm(runs=0, balance=130)
        self.assertEqual((clicks, left), (3, 10))
        self.assertEqual(routes, ["again", "again", "exit"])
        self.assertEqual(flow.session().finish, D.EXHAUSTED)
        self.assertTrue(flow.finish(ctx))

    def test_exhaust_mode_with_too_little_balance_never_claims(self):
        ctx, clicks, _, routes = farm(runs=0, balance=39)
        self.assertEqual((clicks, routes), (0, ["exit_at_gate"]))
        self.assertEqual(flow.session().finish, D.EXHAUSTED)
        self.assertTrue(flow.finish(ctx))

    def test_target_larger_than_budget_ends_exhausted(self):
        _, clicks, _, routes = farm(runs=5, balance=90)
        self.assertEqual((clicks, routes), (2, ["again", "exit"]))
        self.assertEqual(flow.session().finish, D.EXHAUSTED)

    def test_unknown_balance_fails_without_claim(self):
        ctx = FakeContext(options(), {"DungeonBalance": "4O"})  # OCR 把 0 认成 O
        flow.reset(ctx)
        self.assertFalse(flow.gate(ctx, "DungeonClaimGate", GATE))
        self.assertEqual(ctx.overrides, [])
        self.assertEqual(ctx.controller.count("click"), 0)
        self.assertEqual(flow.session().finish, D.UNKNOWN_BALANCE)
        self.assertFalse(flow.finish(ctx))
        self.assertEqual(PRINTED[-1], "dungeon.failed|0")

    def test_balance_not_visible_is_unknown(self):
        ctx = FakeContext(options(), {"DungeonBalance": None})
        flow.reset(ctx)
        self.assertFalse(flow.gate(ctx, "DungeonClaimGate", GATE))
        self.assertEqual(flow.session().finish, D.UNKNOWN_BALANCE)

    def test_settlement_balance_node_overrides_estimate(self):
        ctx = FakeContext(options(), {"DungeonBalance": "200", "DungeonSettleBalance": "20"})
        flow.reset(ctx)
        flow.gate(ctx, "DungeonClaimGate", GATE)
        flow.claim(ctx, [0, 0, 10, 10])
        # 领取前 200，估算剩 160；结算画面实际读到 20，应走最终退出
        self.assertTrue(flow.confirm(ctx, "DungeonSettlement", dict(AGAIN, balance_node="DungeonSettleBalance")))
        self.assertEqual(ctx.overrides[-1], ("DungeonSettlement", ["DungeonExit"]))
        self.assertEqual(flow.session().finish, D.EXHAUSTED)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()
        self.ctx = FakeContext(options(), {"DungeonBalance": "200"})
        flow.reset(self.ctx)

    def test_claim_clicks_box_center_once(self):
        self.assertTrue(flow.claim(self.ctx, [400, 520, 200, 60]))
        self.assertFalse(flow.claim(self.ctx, [400, 520, 200, 60]))  # 未确认前拒绝再点
        self.assertEqual(self.ctx.controller.events, [("click", 500, 550)])
        self.assertEqual(PRINTED[-1], "dungeon.claim_repeat")

    def test_click_failure_does_not_count(self):
        self.ctx.controller.fail_click = True
        self.assertFalse(flow.claim(self.ctx, [0, 0, 10, 10]))
        self.assertFalse(flow.session().state.pending)
        self.assertEqual(flow.session().state.claimed, 0)

    def test_missing_box(self):
        self.assertFalse(flow.claim(self.ctx, None))
        self.assertFalse(flow.session().state.pending)

    def test_settlement_without_claim_is_not_counted(self):
        self.assertFalse(flow.confirm(self.ctx, "DungeonSettlement", AGAIN))
        self.assertEqual(flow.session().state.claimed, 0)
        self.assertEqual(flow.session().finish, "unexpected_settlement")
        self.assertFalse(flow.finish(self.ctx))

    def test_same_settlement_twice_counts_once(self):
        flow.gate(self.ctx, "DungeonClaimGate", GATE)
        flow.claim(self.ctx, [0, 0, 10, 10])
        self.assertTrue(flow.confirm(self.ctx, "DungeonSettlement", AGAIN))
        self.assertFalse(flow.confirm(self.ctx, "DungeonSettlement", AGAIN))
        self.assertEqual(flow.session().state.claimed, 1)

    def test_fail_reports_claimed_and_keeps_failure(self):
        flow.gate(self.ctx, "DungeonClaimGate", GATE)
        flow.claim(self.ctx, [0, 0, 10, 10])
        flow.confirm(self.ctx, "DungeonSettlement", AGAIN)
        self.assertFalse(flow.fail(self.ctx))  # 已成功 1 次也不能整体报成功
        self.assertEqual(PRINTED[-1], "dungeon.failed|1")


class WalkTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()

    def _ctx(self, hits):
        frames = iter(hits)
        return FakeContext(recognize={"Target": lambda: next(frames, False)})

    def test_already_there_presses_nothing(self):
        ctx = self._ctx([True])
        self.assertTrue(flow.walk_until(ctx, {"key": "w", "until": "Target"}))
        self.assertEqual(ctx.controller.count("down"), 0)

    def test_walks_until_target_and_releases(self):
        ctx = self._ctx([False, False, False, True])
        self.assertTrue(flow.walk_until(ctx, {"key": "W", "until": "Target", "timeout": 5}, clock=FakeClock(0.1)))
        keys = [e for e in ctx.controller.events if e[0] in ("down", "up")]
        self.assertEqual(keys, [("down", 0x57), ("up", 0x57)])

    def test_timeout_releases_and_reports(self):
        ctx = self._ctx([])
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "timeout": 2}, clock=FakeClock(0.5)))
        self.assertEqual(ctx.controller.count("down"), ctx.controller.count("up"))
        self.assertEqual(PRINTED[-1], "dungeon.walk_timeout|Target")

    def test_stop_releases(self):
        ctx = self._ctx([])
        ctx.tasker.stopping = True
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual(ctx.controller.count("up"), 1)

    def test_recognition_error_releases(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("recognition crashed")
            return False

        ctx = FakeContext(recognize={"Target": boom})
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual((ctx.controller.count("down"), ctx.controller.count("up")), (1, 1))

    def test_invalid_params_do_nothing(self):
        for params in ({"key": "Q", "until": "Target"}, {"key": "W"}, {"until": "Target", "timeout": "x"}):
            ctx = self._ctx([])
            self.assertFalse(flow.walk_until(ctx, params))
            self.assertEqual(ctx.controller.events, [])


class FightTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()
        flow.reset(FakeContext(options()))
        self.calls = []

    def _combat(self, *reasons):
        results = iter(reasons)

        def run(context, overrides=None):
            self.calls.append(overrides)
            return combat_result(next(results))

        return run

    def test_waves_share_round_deadline(self):
        clock = FakeClock()
        combat = self._combat("cleared", "cleared")
        self.assertTrue(flow.fight(None, {"round_timeout": 300}, clock=clock, combat=combat))
        clock.t += 120  # 第一波打了 120 秒
        self.assertTrue(flow.fight(None, {"round_timeout": 300}, clock=clock, combat=combat))
        self.assertEqual([c["max_duration"] for c in self.calls], [300, 180])

    def test_deadline_passed_skips_combat(self):
        clock = FakeClock()
        flow.fight(None, {"round_timeout": 60}, clock=clock, combat=self._combat("cleared"))
        clock.t += 61
        self.assertFalse(flow.fight(None, {"round_timeout": 60}, clock=clock, combat=self._combat()))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(flow.session().finish, "round_timeout")

    def test_confirm_resets_round_deadline(self):
        ctx = FakeContext(options(), {"DungeonBalance": "200"})
        flow.reset(ctx)
        clock = FakeClock()
        flow.fight(ctx, {"round_timeout": 60}, clock=clock, combat=self._combat("cleared", "cleared"))
        flow.gate(ctx, "DungeonClaimGate", GATE)
        flow.claim(ctx, [0, 0, 10, 10])
        flow.confirm(ctx, "DungeonSettlement", AGAIN)
        clock.t += 100  # 下一轮从头计时
        self.assertTrue(flow.fight(ctx, {"round_timeout": 60}, clock=clock, combat=self._combat("cleared")))

    def test_abort_reasons_fail(self):
        for reason in ("stopped", "error", "scheme_error"):
            flow.reset(FakeContext(options()))
            self.assertFalse(flow.fight(None, {}, clock=FakeClock(), combat=self._combat(reason)), reason)
            self.assertEqual(flow.session().finish, "combat_%s" % reason)

    def test_combat_timeout_is_round_timeout_not_clear(self):
        self.assertFalse(flow.fight(None, {}, clock=FakeClock(), combat=self._combat("timeout")))
        self.assertEqual(flow.session().finish, "round_timeout")

    def test_hud_lost_and_no_combat_hand_back_without_success_claim(self):
        for reason in ("hud_lost", "no_combat", "cleared"):
            flow.reset(FakeContext(options()))
            self.assertTrue(flow.fight(None, {}, clock=FakeClock(), combat=self._combat(reason)), reason)
            self.assertIsNone(flow.session().finish)  # 交还画面，不记结束
            self.assertEqual(flow.session().state.claimed, 0)

    def test_wait_timeout_passthrough(self):
        flow.fight(None, {"wait_timeout": 5}, clock=FakeClock(), combat=self._combat("cleared"))
        self.assertEqual(self.calls[0]["wait_timeout"], 5)


if __name__ == "__main__":
    unittest.main()
