"""flow 桥接逻辑：配置注入、入场/领奖/再次挑战闸门、40/80 领取事务、移动与战斗、结束语义。

闭环测试直接使用 DungeonFarm.json 中各节点的 custom_action_param，保证测试与 Pipeline 契约一致。
"""

import unittest

from ._fakes import FakeClock, FakeContext, combat_result, ocr, options, pipeline
from ._loader import PRINTED, submodule

flow = submodule("flow")
D = submodule("budget").Decision

BTN40 = [420, 520, 120, 40]
BTN80 = [680, 520, 120, 40]
ENTER_BOX = [1000, 650, 120, 40]


def param(node):
    action = pipeline()[node]["action"]
    return dict(action["param"].get("custom_action_param") or {})


def center(rect):
    return rect[0] + rect[2] // 2, rect[1] + rect[3] // 2


class Game:
    """模拟游戏画面：余额、领奖弹窗的按钮与成本文字。"""

    def __init__(self, balance, offered=(40, 80), charge=True):
        self.balance = balance
        self.offered = set(offered)
        self.charge = charge
        self.cost_text = {40: "40", 80: "80"}

    def recognize(self):
        def balance():
            return None if self.balance is None else "%d/240" % self.balance

        def options_text():
            items = [([500, 300, 80, 20], balance() or "")]
            if 40 in self.offered:
                items.append(([450, 450, 40, 20], self.cost_text[40]))
            if 80 in self.offered:
                items.append(([710, 450, 40, 20], self.cost_text[80]))
            return ocr(*items)

        return {
            "DungeonRewardPopup": True,
            "DungeonBalance": balance,
            "DungeonEntryBalance": balance,
            "DungeonRepeatBalance": balance,
            "DungeonRewardOptions": options_text,
            "DungeonClaimButton": lambda: ocr((BTN40, "领取")) if 40 in self.offered else None,
            "DungeonClaim80Button": lambda: ocr((BTN80, "双倍领取")) if 80 in self.offered else None,
            "DungeonEnterButton": lambda: ocr((ENTER_BOX, "进入")),
            "DungeonChallengeAgain": lambda: ocr((ENTER_BOX, "再次挑战")),
        }

    def charge_click(self, click):
        if not self.charge:
            return
        if click == center(BTN40):
            self.balance -= 40
        elif click == center(BTN80):
            self.balance -= 80


def new_ctx(game, **opts):
    ctx = FakeContext(options(**opts), game.recognize())
    assert flow.reset(ctx), PRINTED[-3:]
    return ctx


def last_route(ctx):
    return ctx.overrides[-1][1]


def farm(balance, max_rounds=20, offered=(40, 80), **opts):
    """模拟 Pipeline 主线：入场闸门 → 进入 → 战斗 → 领奖闸门 → 领取 → 结算 → 再次挑战闸门 …

    返回 (ctx, game, routes)。routes 记录每轮去向：again / exit / exit_at_entry / close / fail。
    """
    game = Game(balance, offered)
    ctx = new_ctx(game, **opts)
    clock = FakeClock()
    routes = []
    if not flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate")):
        return ctx, game, ["fail"]
    if last_route(ctx) != param("DungeonFarmEntryGate")["continue"]:
        return ctx, game, ["exit_at_entry"]
    assert flow.enter(ctx, param("DungeonFarmEnter"))
    for _ in range(max_rounds):
        assert flow.begin_round(ctx, param("DungeonFarmInDungeon"), clock=clock)
        assert flow.fight(ctx, param("DungeonFarmFight"), clock=clock, combat=lambda c, overrides: combat_result("cleared"))
        if not flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate")):
            routes.append("fail")
            break
        if last_route(ctx) != param("DungeonFarmClaimGate")["continue"]:
            routes.append("close")
            break
        clicks = len(ctx.controller.clicks())
        if not flow.claim(ctx, param("DungeonFarmClaim")):
            routes.append("fail")
            break
        assert len(ctx.controller.clicks()) == clicks + 1
        game.charge_click(ctx.controller.clicks()[-1])
        if not flow.confirm(ctx, "DungeonFarmSettlement", param("DungeonFarmSettlement")):
            routes.append("fail")
            break
        if last_route(ctx) == param("DungeonFarmSettlement")["finish"]:
            routes.append("exit")
            break
        if not flow.gate(ctx, "DungeonFarmRepeatGate", param("DungeonFarmRepeatGate")):
            routes.append("fail")
            break
        if last_route(ctx) != param("DungeonFarmRepeatGate")["continue"]:
            routes.append("exit")
            break
        routes.append("again")
        assert flow.enter(ctx, param("DungeonFarmAgain"))
    return ctx, game, routes


def claim_clicks(ctx):
    return [c for c in ctx.controller.clicks() if c in (center(BTN40), center(BTN80))]


class ResetTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()

    def test_valid_config_injects_profile(self):
        ctx = FakeContext(options(runs="3"))
        self.assertTrue(flow.reset(ctx))
        state = flow.session().state
        self.assertEqual((state.target, state.reserve, state.reward_mode, state.claimed), (3, 0, "40", 0))
        self.assertEqual(flow.session().stage, "clock_trick")
        self.assertEqual(len(ctx.pipeline_overrides), 1)
        injected = ctx.pipeline_overrides[0]
        self.assertIn("DungeonStageEntry", injected)
        self.assertIn("DungeonFarmTeleport", injected)
        self.assertEqual(injected["DungeonStageEntry"]["recognition"]["param"]["expected"], ["钟表把戏"])
        self.assertIn("dungeon.start|钟表把戏|dungeon.target_runs|3|dungeon.reward_mode.40", PRINTED)
        self.assertIn("dungeon.uncalibrated", PRINTED)  # 未实机校准必须提示
        self.assertIn("dungeon.team_current", PRINTED)

    def test_reserve_and_reward_options(self):
        self.assertTrue(flow.reset(FakeContext(options(reserve=60, reward="auto"))))
        state = flow.session().state
        self.assertEqual((state.reserve, state.reward_mode), (60, "auto"))

    def test_exhaust_plan_ignores_reserve_value(self):
        self.assertTrue(flow.reset(FakeContext(options(reserve=60, plan="exhaust"))))
        self.assertEqual(flow.session().state.reserve, 0)

    def test_reset_clears_previous_run(self):
        farm(200, runs="1")
        self.assertEqual(flow.session().state.claimed, 1)
        flow.reset(FakeContext(options()))
        self.assertEqual(flow.session().state.claimed, 0)
        self.assertEqual(flow.session().round_id, 0)
        self.assertIsNone(flow.session().finish)

    def test_invalid_config_fails_before_any_input(self):
        bad = (
            options(runs="abc"),
            options(runs="-1"),
            options(stage=""),
            options(stage="sculpture_gallery"),  # 目录中存在但未适配
            options(stage="no_such_stage"),
            options(reward="120"),
            options(reserve="x"),
        )
        for opts in bad:
            ctx = FakeContext(opts)
            self.assertFalse(flow.reset(ctx), opts)
            self.assertEqual(flow.session().finish, D.CONFIG_ERROR)
            self.assertEqual(ctx.pipeline_overrides, [])
            self.assertEqual(ctx.controller.events, [])

    def test_override_failure_is_config_error(self):
        ctx = FakeContext(options())
        ctx.override_ok = False
        self.assertFalse(flow.reset(ctx))
        self.assertEqual(flow.session().finish, D.CONFIG_ERROR)

    def test_gate_refuses_after_config_error(self):
        ctx = FakeContext(options(runs="abc"), Game(200).recognize())
        flow.reset(ctx)
        self.assertFalse(flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate")))
        self.assertEqual(ctx.overrides, [])


class ClosedLoopTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()

    def test_single_run_exits_without_again(self):
        ctx, game, routes = farm(200, runs="1")
        self.assertEqual((claim_clicks(ctx), game.balance, routes), ([center(BTN40)], 160, ["exit"]))
        self.assertEqual(flow.session().finish, D.DONE)
        self.assertTrue(flow.finish(ctx))
        self.assertEqual(PRINTED[-1], "dungeon.finished.done|1")
        self.assertIn("dungeon.summary|1|0|40", PRINTED)

    def test_n_runs_last_round_does_not_click_again(self):
        ctx, game, routes = farm(400, runs="3")
        self.assertEqual((len(claim_clicks(ctx)), game.balance), (3, 280))
        self.assertEqual(routes, ["again", "again", "exit"])
        self.assertEqual(ctx.controller.clicks().count(center(ENTER_BOX)), 3)  # 1 次进入 + 2 次再次挑战

    def test_exhaust_mode_spends_until_short(self):
        ctx, game, routes = farm(130)
        self.assertEqual((len(claim_clicks(ctx)), game.balance), (3, 10))
        self.assertEqual(routes, ["again", "again", "exit"])
        self.assertEqual(flow.session().finish, D.EXHAUSTED)
        self.assertTrue(flow.finish(ctx))

    def test_too_little_balance_never_enters(self):
        ctx, _, routes = farm(39)
        self.assertEqual(routes, ["exit_at_entry"])
        self.assertEqual(ctx.controller.clicks(), [])  # 不点进入，不战斗
        self.assertEqual(flow.session().finish, D.EXHAUSTED)
        self.assertIn("dungeon.plan|39|0|0", PRINTED)
        self.assertTrue(flow.finish(ctx))

    def test_reserve_auto_80_then_40(self):
        ctx, game, routes = farm(130, reserve=10, reward="auto")
        self.assertEqual(claim_clicks(ctx), [center(BTN80), center(BTN40)])
        self.assertEqual((game.balance, routes), (10, ["again", "exit"]))
        state = flow.session().state
        self.assertEqual((state.claimed, state.claims_80, state.claims_40, state.spent), (2, 1, 1, 120))
        self.assertIn("dungeon.plan|130|10|2", PRINTED)

    def test_reserve_only_40(self):
        ctx, game, _ = farm(130, reserve=10, reward="40")
        self.assertEqual((len(claim_clicks(ctx)), game.balance), (3, 10))

    def test_reserve_only_80(self):
        ctx, game, routes = farm(130, reserve=10, reward="80")
        self.assertEqual((claim_clicks(ctx), game.balance, routes), ([center(BTN80)], 50, ["exit"]))

    def test_reserve_above_balance_never_enters(self):
        ctx, _, routes = farm(100, reserve=120)
        self.assertEqual((routes, ctx.controller.clicks()), (["exit_at_entry"], []))

    def test_runs_limit_counts_80_as_one_round(self):
        ctx, game, routes = farm(240, runs="1", reward="auto")
        self.assertEqual((claim_clicks(ctx), game.balance, routes), ([center(BTN80)], 160, ["exit"]))
        self.assertEqual(flow.session().state.claimed, 1)

    def test_target_larger_than_budget_ends_exhausted(self):
        ctx, _, routes = farm(90, runs="5")
        self.assertEqual((len(claim_clicks(ctx)), routes), (2, ["again", "exit"]))
        self.assertEqual(flow.session().finish, D.EXHAUSTED)

    def test_auto_uses_40_when_only_40_offered(self):
        ctx, _, _ = farm(200, runs="1", reward="auto", offered=(40,))
        self.assertEqual(claim_clicks(ctx), [center(BTN40)])

    def test_recovery_does_not_extend_frozen_budget(self):
        game = Game(120)
        ctx = new_ctx(game)
        clock = FakeClock()
        flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate"))
        flow.enter(ctx, param("DungeonFarmEnter"))
        claims = 0
        for _ in range(5):
            flow.begin_round(ctx, {}, clock=clock)
            if not flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate")):
                break
            if last_route(ctx) != param("DungeonFarmClaimGate")["continue"]:
                break
            flow.claim(ctx, param("DungeonFarmClaim"))
            game.charge_click(ctx.controller.clicks()[-1])
            claims += 1
            game.balance += 2  # 领奖到结算之间自然恢复
            flow.confirm(ctx, "DungeonFarmSettlement", param("DungeonFarmSettlement"))
            if not flow.gate(ctx, "DungeonFarmRepeatGate", param("DungeonFarmRepeatGate")):
                self.fail(flow.session().finish)
            if last_route(ctx) != param("DungeonFarmRepeatGate")["continue"]:
                break
            flow.enter(ctx, param("DungeonFarmAgain"))
        self.assertEqual(claims, 3)  # 冻结预算 120 = 3 × 40；恢复的 6 点不追加轮次
        self.assertEqual(flow.session().finish, D.EXHAUSTED)

    def test_unknown_entry_balance_fails_without_input(self):
        game = Game(None)
        ctx = new_ctx(game)
        self.assertFalse(flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate")))
        self.assertEqual((ctx.overrides, ctx.controller.clicks()), ([], []))
        self.assertEqual(flow.session().finish, D.UNKNOWN_BALANCE)
        self.assertFalse(flow.finish(ctx))
        self.assertEqual(PRINTED[-1], "dungeon.failed|0")

    def test_garbled_balance_is_unknown(self):
        game = Game(200)
        recognize = game.recognize()
        recognize["DungeonEntryBalance"] = "4O/240"
        ctx = FakeContext(options(), recognize)
        flow.reset(ctx)
        self.assertFalse(flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate")))
        self.assertEqual(flow.session().finish, D.UNKNOWN_BALANCE)

    def test_conflicting_balance_readings_are_unknown(self):
        game = Game(200)
        recognize = game.recognize()
        recognize["DungeonEntryBalance"] = lambda: ocr(([0, 0, 10, 10], "200/240"), ([0, 30, 10, 10], "1/3"))
        ctx = FakeContext(options(), recognize)
        flow.reset(ctx)
        self.assertFalse(flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate")))
        self.assertEqual(flow.session().finish, D.UNKNOWN_BALANCE)

    def test_balance_not_charged_is_mismatch(self):
        game = Game(200)
        game.charge = False  # 点击后余额没变：不能当作扣费成功继续刷
        ctx = new_ctx(game)
        clock = FakeClock()
        flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate"))
        flow.enter(ctx, param("DungeonFarmEnter"))
        flow.begin_round(ctx, {}, clock=clock)
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))
        flow.claim(ctx, param("DungeonFarmClaim"))
        game.charge_click(ctx.controller.clicks()[-1])
        # 结算画面没有余额显示：先按推算值记一轮；再次挑战闸门读到余额未减少必须停止。
        self.assertTrue(flow.confirm(ctx, "DungeonFarmSettlement", param("DungeonFarmSettlement")))
        clicks = len(ctx.controller.clicks())
        self.assertFalse(flow.gate(ctx, "DungeonFarmRepeatGate", param("DungeonFarmRepeatGate")))
        self.assertEqual(flow.session().finish, "balance_mismatch")
        self.assertFalse(flow.enter(ctx, param("DungeonFarmAgain")))
        self.assertEqual(len(ctx.controller.clicks()), clicks)


def _in_round(game, **opts):
    """进入到第 1 轮关卡内（已批准入场、已开始本轮）。"""
    ctx = new_ctx(game, **opts)
    assert flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate"))
    assert flow.enter(ctx, param("DungeonFarmEnter"))
    assert flow.begin_round(ctx, {}, clock=FakeClock())
    return ctx


class ClaimTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()

    def test_claim_requires_gate_approval(self):
        ctx = _in_round(Game(200))
        self.assertFalse(flow.claim(ctx, param("DungeonFarmClaim")))
        self.assertEqual(claim_clicks(ctx), [])
        self.assertEqual(flow.session().finish, "claim_unapproved")

    def test_approval_is_single_use(self):
        ctx = _in_round(Game(200))
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))
        self.assertTrue(flow.claim(ctx, param("DungeonFarmClaim")))
        self.assertFalse(flow.claim(ctx, param("DungeonFarmClaim")))  # 未确认前拒绝再点
        self.assertEqual(len(claim_clicks(ctx)), 1)
        self.assertEqual(PRINTED[-1], "dungeon.claim_repeat")

    def test_balance_change_between_gate_and_claim_refuses(self):
        game = Game(130)
        ctx = _in_round(game, reward="auto")
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))  # 批准 80
        game.balance = 60  # 领取前余额变化：只够 40
        self.assertFalse(flow.claim(ctx, param("DungeonFarmClaim")))
        self.assertEqual(claim_clicks(ctx), [])

    def test_click_failure_locks_without_second_click(self):
        ctx = _in_round(Game(200))
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))
        ctx.controller.click_status = False
        self.assertFalse(flow.claim(ctx, param("DungeonFarmClaim")))
        self.assertEqual(flow.session().finish, "input_failed")
        self.assertEqual(flow.session().state.claimed, 0)
        ctx.controller.click_status = True
        self.assertFalse(flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate")))  # 扣费未知：锁定
        self.assertFalse(flow.claim(ctx, param("DungeonFarmClaim")))
        self.assertEqual(len(ctx.controller.clicks()), 2)  # 进入 + 第一次领取，没有第二次领取

    def test_click_exception_counts_as_failure(self):
        ctx = _in_round(Game(200))
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))
        ctx.controller.fail_click = True
        self.assertFalse(flow.claim(ctx, param("DungeonFarmClaim")))
        self.assertTrue(flow.session().state.uncertain)

    def test_ambiguous_button_never_clicks(self):
        game = Game(200)
        ctx = _in_round(game)
        ctx.recognize["DungeonClaimButton"] = lambda: ocr((BTN40, "领取"), ([420, 600, 120, 40], "领取"))
        self.assertFalse(flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate")))
        self.assertEqual(flow.session().finish, "unknown_cost")
        self.assertEqual(claim_clicks(ctx), [])

    def test_cost_text_must_match_button(self):
        game = Game(200)
        game.cost_text = {40: "80", 80: "40"}  # 成本数字与按钮对不上：不能按位置猜
        ctx = _in_round(game, reward="auto")
        self.assertFalse(flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate")))
        self.assertEqual(flow.session().finish, "unknown_cost")

    def test_no_option_with_enough_budget_is_failure(self):
        ctx = _in_round(Game(200, offered=()))
        self.assertFalse(flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate")))
        self.assertEqual(flow.session().finish, D.NO_REWARD)

    def test_only_80_mode_with_only_40_on_screen_fails(self):
        ctx = _in_round(Game(200, offered=(40,)), reward="80")
        self.assertFalse(flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate")))
        self.assertEqual(flow.session().finish, D.NO_REWARD)
        self.assertEqual(claim_clicks(ctx), [])

    def test_settlement_without_claim_is_not_counted(self):
        ctx = _in_round(Game(200))
        self.assertFalse(flow.confirm(ctx, "DungeonFarmSettlement", param("DungeonFarmSettlement")))
        self.assertEqual(flow.session().state.claimed, 0)
        self.assertEqual(flow.session().finish, "unexpected_settlement")
        self.assertFalse(flow.finish(ctx))

    def test_same_settlement_twice_counts_once_and_keeps_route(self):
        game = Game(200)
        ctx = _in_round(game)
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))
        flow.claim(ctx, param("DungeonFarmClaim"))
        game.charge_click(ctx.controller.clicks()[-1])
        self.assertTrue(flow.confirm(ctx, "DungeonFarmSettlement", param("DungeonFarmSettlement")))
        first = last_route(ctx)
        self.assertTrue(flow.confirm(ctx, "DungeonFarmSettlement", param("DungeonFarmSettlement")))
        self.assertEqual(last_route(ctx), first)
        self.assertEqual(flow.session().state.claimed, 1)

    def test_fail_reports_claimed_and_keeps_failure(self):
        game = Game(200)
        ctx = _in_round(game)
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))
        flow.claim(ctx, param("DungeonFarmClaim"))
        game.charge_click(ctx.controller.clicks()[-1])
        flow.confirm(ctx, "DungeonFarmSettlement", param("DungeonFarmSettlement"))
        self.assertFalse(flow.fail(ctx))  # 已成功 1 次也不能整体报成功
        self.assertEqual(PRINTED[-1], "dungeon.failed|1")

    def test_stop_blocks_claim(self):
        ctx = _in_round(Game(200))
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))
        ctx.tasker.stopping = True
        self.assertFalse(flow.claim(ctx, param("DungeonFarmClaim")))
        self.assertEqual(claim_clicks(ctx), [])


class EntryTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()

    def test_enter_requires_approval(self):
        ctx = new_ctx(Game(200))
        self.assertFalse(flow.enter(ctx, param("DungeonFarmEnter")))
        self.assertEqual(ctx.controller.clicks(), [])
        self.assertEqual(flow.session().finish, "entry_unapproved")

    def test_entry_approval_is_single_use(self):
        ctx = new_ctx(Game(200))
        flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate"))
        self.assertTrue(flow.enter(ctx, param("DungeonFarmEnter")))
        self.assertFalse(flow.enter(ctx, param("DungeonFarmEnter")))
        self.assertEqual(ctx.controller.clicks(), [center(ENTER_BOX)])

    def test_repeat_approval_does_not_open_entry(self):
        ctx = new_ctx(Game(200))
        flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate"))
        self.assertFalse(flow.enter(ctx, param("DungeonFarmAgain")))  # 阶段不符

    def test_begin_round_requires_enter_click(self):
        ctx = new_ctx(Game(200))
        self.assertFalse(flow.begin_round(ctx, {}))
        self.assertEqual(flow.session().finish, "round_state")

    def test_begin_round_is_idempotent_within_round(self):
        ctx = _in_round(Game(200))
        deadline = flow.session().round_deadline
        self.assertTrue(flow.begin_round(ctx, {}, clock=FakeClock(step=100)))
        self.assertEqual((flow.session().round_id, flow.session().round_deadline), (1, deadline))

    def test_repeat_gate_requires_committed_round(self):
        ctx = _in_round(Game(200))
        self.assertFalse(flow.gate(ctx, "DungeonFarmRepeatGate", param("DungeonFarmRepeatGate")))
        self.assertEqual(flow.session().finish, "round_state")

    def test_stop_blocks_entry(self):
        ctx = new_ctx(Game(200))
        ctx.tasker.stopping = True
        self.assertFalse(flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate")))
        self.assertEqual(flow.session().finish, "stopped")

    def test_unsafe_start_reason(self):
        ctx = new_ctx(Game(200))
        self.assertFalse(flow.fail(ctx, {"reason": "unsafe_start"}))
        self.assertEqual(flow.session().finish, "unsafe_start")
        self.assertIn("dungeon.unsafe_start", PRINTED)


class WalkTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()
        flow.reset(FakeContext(options()))

    def _ctx(self, hits, guard=True):
        frames = iter(hits)
        recognize = {"Target": lambda: next(frames, False)}
        if guard is not None:
            recognize["Guard"] = guard
        return FakeContext(recognize=recognize, use_pipeline=False)

    def _keys(self, ctx):
        return [e for e in ctx.controller.events if e[0] in ("down", "up")]

    def test_already_there_presses_nothing(self):
        ctx = self._ctx([True])
        self.assertTrue(flow.walk_until(ctx, {"key": "w", "until": "Target", "guard": "Guard"}))
        self.assertEqual(self._keys(ctx), [])

    def test_walks_until_target_and_releases(self):
        ctx = self._ctx([False, False, False, True])
        self.assertTrue(flow.walk_until(ctx, {"key": "W", "until": "Target", "guard": "Guard", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual(self._keys(ctx), [("down", 0x57), ("up", 0x57)])

    def test_abort_node_releases_and_hands_back(self):
        ctx = self._ctx([False, False])
        hits = iter([False, True])
        ctx.recognize["Loading"] = lambda: next(hits, True)
        self.assertTrue(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "abort_nodes": ["Loading"], "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual(self._keys(ctx), [("down", 0x57), ("up", 0x57)])

    def test_timeout_releases_and_reports(self):
        ctx = self._ctx([])
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 2}, clock=FakeClock(0.5)))
        self.assertEqual(ctx.controller.count("down"), ctx.controller.count("up"))
        self.assertEqual(PRINTED[-1], "dungeon.walk_timeout|Target")
        self.assertEqual(flow.session().finish, "walk_timeout")

    def test_guard_lost_releases(self):
        frames = iter([True, True, False, False, False])
        ctx = self._ctx([], guard=lambda: next(frames, False))
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 30}, clock=FakeClock(0.1)))
        self.assertEqual((ctx.controller.count("down"), ctx.controller.count("up")), (1, 1))
        self.assertEqual(flow.session().finish, "walk_lost")

    def test_not_in_guard_scene_never_presses(self):
        ctx = self._ctx([], guard=False)
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard"}))
        self.assertEqual(self._keys(ctx), [])

    def test_screencap_failures_stop_walking(self):
        ctx = self._ctx([])
        calls = {"n": 0}
        original = ctx.controller.post_screencap

        def flaky():
            calls["n"] += 1
            ctx.controller.screencap_ok = calls["n"] == 1
            return original()

        ctx.controller.post_screencap = flaky
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 30}, clock=FakeClock(0.1)))
        self.assertEqual((ctx.controller.count("down"), ctx.controller.count("up")), (1, 1))
        self.assertEqual(flow.session().finish, "walk_lost")

    def test_stop_before_walking_sends_no_input(self):
        ctx = self._ctx([])
        ctx.tasker.stopping = True
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual(self._keys(ctx), [])

    def test_stop_while_walking_releases(self):
        ctx = self._ctx([])

        def guard():
            if ctx.controller.count("down"):
                ctx.tasker.stopping = True  # 按下后用户点了停止
            return True

        ctx.recognize["Guard"] = guard
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual((ctx.controller.count("down"), ctx.controller.count("up")), (1, 1))

    def test_key_down_exception_still_releases(self):
        ctx = self._ctx([])
        ctx.controller.key_down_raises = RuntimeError("down failed")
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual(ctx.controller.count("up"), 1)

    def test_key_down_failure_status_releases(self):
        ctx = self._ctx([])
        ctx.controller.key_down_status = False
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual(ctx.controller.count("up"), 1)
        self.assertEqual(flow.session().finish, "input_failed")

    def test_key_up_failure_is_failure_even_when_arrived(self):
        ctx = self._ctx([False, True])
        ctx.controller.key_up_status = False
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual(flow.session().finish, "input_failed")

    def test_recognition_error_releases(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("recognition crashed")
            return False

        ctx = FakeContext(recognize={"Target": boom, "Guard": True}, use_pipeline=False)
        self.assertFalse(flow.walk_until(ctx, {"until": "Target", "guard": "Guard", "timeout": 5}, clock=FakeClock(0.1)))
        self.assertEqual((ctx.controller.count("down"), ctx.controller.count("up")), (1, 1))

    def test_invalid_params_do_nothing(self):
        for params in ({"key": "Q", "until": "Target"}, {"key": "W"}, {"until": "Target", "timeout": "x"}):
            ctx = self._ctx([])
            self.assertFalse(flow.walk_until(ctx, params))
            self.assertEqual(ctx.controller.events, [])


class FightTests(unittest.TestCase):
    def setUp(self):
        PRINTED.clear()
        self.clock = FakeClock()
        self.ctx = new_ctx(Game(200))
        flow.gate(self.ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate"))
        flow.enter(self.ctx, param("DungeonFarmEnter"))
        flow.begin_round(self.ctx, {"round_timeout": 300}, clock=self.clock)
        self.calls = []

    def _combat(self, *reasons):
        results = iter(reasons)

        def run(context, overrides=None):
            self.calls.append(overrides)
            return combat_result(next(results))

        return run

    def test_requires_active_round(self):
        flow.reset(FakeContext(options()))
        self.assertFalse(flow.fight(self.ctx, {}, clock=self.clock, combat=self._combat("cleared")))
        self.assertEqual(self.calls, [])

    def test_waves_share_round_deadline_including_wait(self):
        combat = self._combat("cleared", "cleared")
        self.assertTrue(flow.fight(self.ctx, {"wait_timeout": 5}, clock=self.clock, combat=combat))
        self.clock.t += 120  # 第一波打了 120 秒
        self.assertTrue(flow.fight(self.ctx, {"wait_timeout": 5}, clock=self.clock, combat=combat))
        self.assertEqual([(c["wait_timeout"], c["max_duration"]) for c in self.calls], [(5, 295), (5, 175)])

    def test_wait_timeout_never_zero(self):
        flow.fight(self.ctx, {"wait_timeout": 0}, clock=self.clock, combat=self._combat("cleared"))
        self.assertGreater(self.calls[0]["wait_timeout"], 0)  # 0 在战斗系统里是无限等待

    def test_deadline_passed_skips_combat(self):
        self.clock.t += 301
        self.assertFalse(flow.fight(self.ctx, {}, clock=self.clock, combat=self._combat()))
        self.assertEqual(self.calls, [])
        self.assertEqual(flow.session().finish, "round_timeout")

    def test_abort_reasons_fail(self):
        for reason in ("stopped", "error", "scheme_error"):
            self.assertFalse(flow.fight(self.ctx, {}, clock=self.clock, combat=self._combat(reason)), reason)
            self.assertEqual(flow.session().finish, "combat_%s" % reason)

    def test_combat_timeout_is_round_timeout_not_clear(self):
        self.assertFalse(flow.fight(self.ctx, {}, clock=self.clock, combat=self._combat("timeout")))
        self.assertEqual(flow.session().finish, "round_timeout")

    def test_hand_back_reasons_do_not_count(self):
        for reason in ("hud_lost", "no_combat", "cleared"):
            self.assertTrue(flow.fight(self.ctx, {}, clock=self.clock, combat=self._combat(reason)), reason)
            self.assertIsNone(flow.session().finish)  # 交还画面，不记结束
            self.assertEqual(flow.session().state.claimed, 0)

    def test_new_round_gets_new_deadline(self):
        game = Game(200)
        ctx = new_ctx(game)
        clock = FakeClock()
        flow.gate(ctx, "DungeonFarmEntryGate", param("DungeonFarmEntryGate"))
        flow.enter(ctx, param("DungeonFarmEnter"))
        flow.begin_round(ctx, {"round_timeout": 60}, clock=clock)
        flow.gate(ctx, "DungeonFarmClaimGate", param("DungeonFarmClaimGate"))
        flow.claim(ctx, param("DungeonFarmClaim"))
        game.charge_click(ctx.controller.clicks()[-1])
        flow.confirm(ctx, "DungeonFarmSettlement", param("DungeonFarmSettlement"))
        clock.t += 100
        flow.gate(ctx, "DungeonFarmRepeatGate", param("DungeonFarmRepeatGate"))
        flow.enter(ctx, param("DungeonFarmAgain"))
        flow.begin_round(ctx, {"round_timeout": 60}, clock=clock)  # 下一轮从头计时
        self.assertTrue(flow.fight(ctx, {}, clock=clock, combat=self._combat("cleared")))


if __name__ == "__main__":
    unittest.main()
