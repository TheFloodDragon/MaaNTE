"""预算/计次模型：次数与余额解析、停止判定、领取确认去重。"""

import unittest

from ._loader import submodule

budget = submodule("budget")
D = budget.Decision


class ParseTests(unittest.TestCase):
    def test_parse_runs(self):
        self.assertEqual(budget.parse_runs("0"), 0)
        self.assertEqual(budget.parse_runs(" 3 "), 3)
        self.assertEqual(budget.parse_runs(7), 7)
        self.assertEqual(budget.parse_runs(999), 999)
        for bad in (-1, 1000, "", "abc", "1.5", "-2", None, True, 2.0):
            self.assertIsNone(budget.parse_runs(bad), bad)

    def test_parse_balance(self):
        self.assertEqual(budget.parse_balance("120"), 120)
        self.assertEqual(budget.parse_balance("120/240"), 120)
        self.assertEqual(budget.parse_balance(" 1,200 / 2,400 "), 1200)
        self.assertEqual(budget.parse_balance("0/240"), 0)
        self.assertEqual(budget.parse_balance("35／240"), 35)
        for bad in ("", "  ", "abc", "12a", "1/2/3", "/240", "120/", None, 120):
            self.assertIsNone(budget.parse_balance(bad), bad)


class DecideTests(unittest.TestCase):
    def test_exhaust_mode_boundaries(self):
        state = budget.FarmState(target=0, cost=40)
        self.assertEqual(state.decide(0), D.EXHAUSTED)
        self.assertEqual(state.decide(39), D.EXHAUSTED)  # 少于一次
        self.assertEqual(state.decide(40), D.CONTINUE)  # 刚好一次
        self.assertEqual(state.decide(200), D.CONTINUE)  # 可刷多次
        self.assertIsNone(state.remaining)

    def test_unknown_balance_is_neither_zero_nor_infinite(self):
        state = budget.FarmState(target=0, cost=40)
        self.assertEqual(state.decide(None), D.UNKNOWN_BALANCE)
        self.assertNotIn(D.UNKNOWN_BALANCE, budget.FINISHED)

    def test_config_error(self):
        self.assertEqual(budget.FarmState(target=0, cost=0).decide(100), D.CONFIG_ERROR)
        self.assertEqual(budget.FarmState(target=-1, cost=40).decide(100), D.CONFIG_ERROR)

    def test_target_less_than_budget_stops_at_target(self):
        state = budget.FarmState(target=2, cost=40)
        balance = 200
        for _ in range(2):
            self.assertEqual(state.decide(balance), D.CONTINUE)
            self.assertTrue(state.begin_claim())
            self.assertTrue(state.confirm_claim())
            balance -= 40
        self.assertEqual(state.decide(balance), D.DONE)
        self.assertEqual(state.decide(None), D.DONE)  # 达到次数后不再需要余额
        self.assertEqual(state.remaining, 0)

    def test_target_more_than_budget_ends_exhausted(self):
        state = budget.FarmState(target=5, cost=40)
        balance = 90
        claims = 0
        while state.decide(balance) == D.CONTINUE:
            state.begin_claim()
            state.confirm_claim()
            balance -= 40
            claims += 1
        self.assertEqual(claims, 2)
        self.assertEqual(state.decide(balance), D.EXHAUSTED)
        self.assertEqual(state.remaining, 3)

    def test_decision_uses_latest_balance_not_start_estimate(self):
        state = budget.FarmState(target=0, cost=40)
        self.assertEqual(state.decide(400), D.CONTINUE)
        self.assertEqual(state.decide(10), D.EXHAUSTED)  # 余额被别处花掉
        self.assertEqual(state.last_balance, 10)


class ClaimTests(unittest.TestCase):
    def test_confirm_counts_once_per_claim(self):
        state = budget.FarmState(target=0, cost=40)
        self.assertFalse(state.confirm_claim())  # 未点击领取就看到结算：不计
        self.assertTrue(state.begin_claim())
        self.assertFalse(state.begin_claim())  # 待确认时拒绝再次点击
        self.assertTrue(state.confirm_claim())
        self.assertFalse(state.confirm_claim())  # 同一结算界面重复识别：不重计
        self.assertEqual(state.claimed, 1)

    def test_cancel_does_not_count(self):
        state = budget.FarmState(target=0, cost=40)
        state.begin_claim()
        state.cancel_claim()
        self.assertFalse(state.confirm_claim())
        self.assertEqual(state.claimed, 0)
        self.assertTrue(state.begin_claim())


if __name__ == "__main__":
    unittest.main()
