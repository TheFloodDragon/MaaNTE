"""冻结预算、保留体力、40/80 策略与一次性领奖事务，不依赖 maa。"""

import unittest

from ._loader import submodule

budget = submodule("budget")
D = budget.Decision
BOUNDARIES = (0, 39, 40, 79, 80, 119, 120, 130)


class ParseTests(unittest.TestCase):
    def test_parse_runs(self):
        for value, expected in (("0", 0), (" 3 ", 3), (7, 7), (999, 999), ("0007", 7)):
            with self.subTest(value=value):
                self.assertEqual(budget.parse_runs(value), expected)

    def test_parse_runs_rejects_invalid_values_and_overflow(self):
        for bad in (
            -1,
            1000,
            "",
            "abc",
            "1.5",
            "-2",
            None,
            True,
            False,
            2.0,
            float("inf"),
            float("nan"),
            "²",
            "１２",
            "1 2",
            "9" * 5000,
            10**5000,
        ):
            with self.subTest(kind=type(bad).__name__):
                self.assertIsNone(budget.parse_runs(bad))

    def test_parse_reserve(self):
        for value, expected in (
            (0, 0),
            (10, 10),
            (999999, 999999),
            (" 130 ", 130),
            ("00010", 10),
        ):
            with self.subTest(value=value):
                self.assertEqual(budget.parse_reserve(value), expected)

    def test_parse_reserve_rejects_invalid_values_and_overflow(self):
        for bad in (
            None,
            True,
            False,
            -1,
            1.0,
            float("nan"),
            1000000,
            "1000000",
            "",
            "-1",
            "+1",
            "1.5",
            "1,000",
            "1 000",
            "²",
            "１２",
            "9" * 5000,
            10**5000,
        ):
            with self.subTest(kind=type(bad).__name__):
                self.assertIsNone(budget.parse_reserve(bad))

    def test_parse_balance(self):
        for text, expected in (
            ("120", 120),
            ("120/240", 120),
            (" 120 / 240 ", 120),
            ("0/240", 0),
            ("35／240", 35),
            ("999999", 999999),
            ("999999/999999", 999999),
            ("000040", 40),
        ):
            with self.subTest(text=text):
                self.assertEqual(budget.parse_balance(text), expected)

    def test_balance_may_exceed_displayed_capacity(self):
        self.assertEqual(budget.parse_balance("400/240"), 400)
        self.assertEqual(budget.parse_balance("999999/1"), 999999)

    def test_parse_balance_requires_whole_input(self):
        for bad in (
            "",
            "  ",
            "abc",
            "12a",
            "体力120/240",
            "120/240剩余",
            "1/2/3",
            "/240",
            "120/",
            "12 34",
            "12\n34",
            "1,200",
            "1，200",
            " 1,200 / 2,400 ",
            "120/240 80/240",
            "12.0",
            "-40",
            "+40",
            "１２",
            "²",
            None,
            120,
            True,
            120.0,
        ):
            with self.subTest(value=bad):
                self.assertIsNone(budget.parse_balance(bad))

    def test_parse_balance_rejects_zero_denominator_and_overflow(self):
        for bad in ("120/0", "0/0", "120/000000", "1000000", "120/1000000", "9" * 5000):
            with self.subTest(length=len(bad)):
                self.assertIsNone(budget.parse_balance(bad))


class BudgetTests(unittest.TestCase):
    def test_default_is_only_40(self):
        state = budget.FarmState()
        self.assertEqual(state.cost, 40)
        self.assertEqual(state.reserve, 0)
        self.assertEqual(state.reward_mode, "40")
        self.assertEqual(state.supported_costs, (40, 80))
        self.assertEqual(state.decide(130), D.CONTINUE)
        self.assertEqual(state.selected_cost, 40)
        self.assertEqual(state.planned_runs, 3)
        self.assertIsNone(state.remaining)

    def test_boundaries_for_all_reward_modes(self):
        for mode in ("40", "80", "auto"):
            for balance in BOUNDARIES:
                with self.subTest(mode=mode, balance=balance):
                    state = budget.FarmState(reward_mode=mode)
                    expected_cost = (
                        80 if mode == "80" or (mode == "auto" and balance >= 80) else 40
                    )
                    affordable = balance >= expected_cost
                    planned = (
                        balance // 80 + balance % 80 // 40
                        if mode == "auto"
                        else balance // int(mode)
                    )
                    self.assertEqual(
                        state.decide(balance), D.CONTINUE if affordable else D.EXHAUSTED
                    )
                    self.assertEqual(
                        state.selected_cost, expected_cost if affordable else None
                    )
                    self.assertEqual(state.planned_runs, planned)
                    self.assertEqual(state.initial_balance, balance)
                    self.assertEqual(state.budget_limit, balance)

    def test_arbitrary_reserve_is_subtracted_before_strategy(self):
        for reserve in (0, 1, 10, 39, 40, 41, 79, 80, 119, 120, 130, 999999):
            for balance in (*BOUNDARIES, 999999):
                for mode in ("40", "80", "auto"):
                    with self.subTest(reserve=reserve, balance=balance, mode=mode):
                        state = budget.FarmState(reserve=reserve, reward_mode=mode)
                        available = max(balance - reserve, 0)
                        expected = (
                            available // 80 + available % 80 // 40
                            if mode == "auto"
                            else available // int(mode)
                        )
                        self.assertEqual(
                            state.decide(balance),
                            D.CONTINUE if expected else D.EXHAUSTED,
                        )
                        self.assertEqual(state.budget_limit, available)
                        self.assertEqual(state.planned_runs, expected)

    def test_130_reserve_10_auto_is_80_plus_40(self):
        state = budget.FarmState(reserve=10, reward_mode="auto")
        self.assertEqual(state.decide(130), D.CONTINUE)
        self.assertEqual(
            (state.selected_cost, state.planned_runs, state.budget_limit), (80, 2, 120)
        )
        self.assertTrue(state.begin_claim())
        self.assertTrue(state.confirm_claim())
        self.assertEqual((state.claimed, state.spent, state.last_balance), (1, 80, 50))
        self.assertEqual(state.decide(50), D.CONTINUE)
        self.assertEqual((state.selected_cost, state.planned_runs), (40, 1))
        self.assertTrue(state.begin_claim(40))
        self.assertTrue(state.confirm_claim())
        self.assertEqual(state.decide(10), D.EXHAUSTED)
        self.assertEqual(
            (state.claimed, state.spent, state.claims_40, state.claims_80),
            (2, 120, 1, 1),
        )

    def test_130_reserve_10_fixed_modes(self):
        for mode, runs, spent, remaining in (("40", 3, 120, 10), ("80", 1, 80, 50)):
            with self.subTest(mode=mode):
                state = budget.FarmState(reserve=10, reward_mode=mode)
                balance = 130
                for index in range(runs):
                    self.assertEqual(state.decide(balance), D.CONTINUE)
                    self.assertEqual(state.planned_runs, runs - index)
                    self.assertTrue(state.begin_claim())
                    self.assertTrue(state.confirm_claim())
                    balance = state.last_balance
                self.assertEqual(
                    (state.claimed, state.spent, balance), (runs, spent, remaining)
                )
                self.assertEqual(state.decide(balance), D.EXHAUSTED)

    def test_target_one_means_one_claim_even_at_80(self):
        for mode, cost in (("40", 40), ("80", 80), ("auto", 80)):
            with self.subTest(mode=mode):
                state = budget.FarmState(target=1, reward_mode=mode)
                self.assertEqual(state.decide(130), D.CONTINUE)
                self.assertEqual(
                    (state.selected_cost, state.planned_runs, state.remaining),
                    (cost, 1, 1),
                )
                self.assertTrue(state.begin_claim())
                self.assertTrue(state.confirm_claim())
                self.assertEqual(
                    (state.claimed, state.spent, state.remaining), (1, cost, 0)
                )
                self.assertEqual(state.decide(None), D.DONE)
                self.assertEqual(state.planned_runs, 0)
                self.assertFalse(state.begin_claim())

    def test_target_less_than_budget_stops_at_target(self):
        state = budget.FarmState(target=2)
        for balance in (200, 160):
            self.assertEqual(state.decide(balance), D.CONTINUE)
            self.assertTrue(state.begin_claim())
            self.assertTrue(state.confirm_claim())
        self.assertEqual(state.decide(120), D.DONE)
        self.assertEqual(state.decide(None), D.DONE)
        self.assertEqual(state.remaining, 0)

    def test_target_more_than_budget_ends_exhausted(self):
        state = budget.FarmState(target=5)
        for balance in (90, 50):
            self.assertEqual(state.decide(balance), D.CONTINUE)
            self.assertTrue(state.begin_claim())
            self.assertTrue(state.confirm_claim())
        self.assertEqual(state.decide(10), D.EXHAUSTED)
        self.assertEqual((state.claimed, state.remaining), (2, 3))

    def test_unknown_balance_does_not_freeze_or_spend_budget(self):
        state = budget.FarmState()
        self.assertEqual(state.decide(None), D.UNKNOWN_BALANCE)
        self.assertIsNone(state.initial_balance)
        self.assertIsNone(state.budget_limit)
        self.assertFalse(state.begin_claim())
        self.assertEqual(state.decide(130), D.CONTINUE)
        self.assertEqual(state.budget_limit, 130)

    def test_invalid_balances_are_unknown_and_do_not_freeze(self):
        for bad in (
            None,
            True,
            False,
            -1,
            40.0,
            float("inf"),
            float("nan"),
            "40",
            1000000,
            10**5000,
        ):
            with self.subTest(kind=type(bad).__name__):
                state = budget.FarmState()
                self.assertEqual(state.decide(bad), D.UNKNOWN_BALANCE)
                self.assertIsNone(state.initial_balance)
                self.assertIsNone(state.last_balance)
                self.assertFalse(state.begin_claim())

    def test_external_consumption_reduces_current_plan(self):
        state = budget.FarmState(reserve=10, reward_mode="auto")
        self.assertEqual(state.decide(130), D.CONTINUE)
        self.assertEqual(state.planned_runs, 2)
        self.assertEqual(state.decide(89), D.CONTINUE)
        self.assertEqual((state.selected_cost, state.planned_runs), (40, 1))
        self.assertTrue(state.begin_claim())
        self.assertTrue(state.confirm_claim())
        self.assertEqual(
            (state.spent, state.last_balance, state.budget_limit), (40, 49, 120)
        )
        self.assertEqual(state.decide(49), D.EXHAUSTED)

    def test_recovery_does_not_expand_initial_budget(self):
        state = budget.FarmState(reserve=10, reward_mode="auto")
        self.assertEqual(state.decide(130), D.CONTINUE)
        self.assertTrue(state.begin_claim())
        self.assertTrue(state.confirm_claim())
        self.assertEqual(state.decide(999999), D.CONTINUE)
        self.assertEqual((state.selected_cost, state.planned_runs), (40, 1))
        self.assertTrue(state.begin_claim())
        self.assertTrue(state.confirm_claim())
        self.assertEqual(state.decide(999999), D.EXHAUSTED)
        self.assertEqual(
            (state.initial_balance, state.budget_limit, state.spent), (130, 120, 120)
        )

    def test_first_zero_balance_freezes_zero_even_after_recovery(self):
        state = budget.FarmState()
        self.assertEqual(state.decide(0), D.EXHAUSTED)
        self.assertEqual(state.decide(130), D.EXHAUSTED)
        self.assertEqual(
            (state.initial_balance, state.budget_limit, state.planned_runs), (0, 0, 0)
        )

    def test_recovery_may_restore_only_unspent_initial_budget(self):
        state = budget.FarmState(reward_mode="auto")
        state.decide(120)
        self.assertEqual(state.decide(40), D.CONTINUE)
        self.assertEqual((state.selected_cost, state.planned_runs), (40, 1))
        self.assertEqual(state.decide(400), D.CONTINUE)
        self.assertEqual(
            (state.selected_cost, state.planned_runs, state.budget_limit), (80, 2, 120)
        )

    def test_large_budget_is_a_numeric_plan_not_a_runs_list(self):
        state = budget.FarmState(target=999)
        self.assertEqual(state.decide(999999), D.CONTINUE)
        self.assertEqual(state.planned_runs, 999)
        self.assertIsInstance(state.planned_runs, int)
        unlimited = budget.FarmState()
        self.assertEqual(unlimited.decide(999999), D.CONTINUE)
        self.assertEqual(unlimited.planned_runs, 24999)


class RewardTests(unittest.TestCase):
    def test_auto_uses_profile_supported_costs(self):
        for supported, selected, planned in (
            ((40,), 40, 3),
            ((80,), 80, 1),
            ((40, 80), 80, 2),
        ):
            with self.subTest(supported=supported):
                state = budget.FarmState(
                    reserve=10, reward_mode="auto", supported_costs=supported
                )
                self.assertEqual(state.decide(130), D.CONTINUE)
                self.assertEqual(
                    (state.selected_cost, state.planned_runs), (selected, planned)
                )

    def test_offered_costs_reduce_auto_candidates(self):
        for offered, selected, planned in (
            ((40,), 40, 3),
            ([80], 80, 1),
            ({40, 80}, 80, 2),
        ):
            with self.subTest(offered=offered):
                state = budget.FarmState(reserve=10, reward_mode="auto")
                self.assertEqual(state.decide(130, offered), D.CONTINUE)
                self.assertEqual(
                    (state.selected_cost, state.planned_runs), (selected, planned)
                )

    def test_offered_costs_cannot_enable_unsupported_profile_cost(self):
        state = budget.FarmState(reward_mode="auto", supported_costs=(40,))
        self.assertEqual(state.decide(130, (40, 80)), D.CONTINUE)
        self.assertEqual(state.selected_cost, 40)
        self.assertEqual(state.decide(130, (80,)), D.NO_REWARD)
        self.assertFalse(state.begin_claim(80))

    def test_fixed_mode_never_silently_changes_cost(self):
        for mode, unsupported in (("40", 80), ("80", 40)):
            with self.subTest(mode=mode):
                state = budget.FarmState(reward_mode=mode)
                self.assertEqual(state.decide(130, (unsupported,)), D.NO_REWARD)
                self.assertIsNone(state.selected_cost)
                self.assertEqual(state.planned_runs, 0)
                self.assertFalse(state.begin_claim())

    def test_profile_without_selected_mode_reports_no_reward(self):
        state = budget.FarmState(reward_mode="80", supported_costs=(40,))
        self.assertEqual(state.decide(130), D.NO_REWARD)

    def test_missing_rewards_are_not_exhaustion(self):
        for balance in (0, 130):
            with self.subTest(balance=balance):
                state = budget.FarmState(reward_mode="auto")
                self.assertEqual(state.decide(balance, []), D.NO_REWARD)
                self.assertNotIn(D.NO_REWARD, budget.FINISHED)

    def test_present_but_unaffordable_reward_is_exhaustion(self):
        state = budget.FarmState(reward_mode="auto")
        self.assertEqual(state.decide(79, (80,)), D.EXHAUSTED)
        self.assertEqual(state.decide(79, (40,)), D.CONTINUE)
        self.assertEqual(state.selected_cost, 40)

    def test_only_done_and_exhausted_are_success(self):
        self.assertEqual(budget.FINISHED, {D.DONE, D.EXHAUSTED})


class ConfigTests(unittest.TestCase):
    def test_invalid_target(self):
        for value in (None, True, False, -1, 1.0, "1", 1000, 10**5000):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(
                    budget.FarmState(target=value).decide(130), D.CONFIG_ERROR
                )

    def test_invalid_reserve(self):
        for value in (None, True, False, -1, 10.0, "10", 1000000, 10**5000):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(
                    budget.FarmState(reserve=value).decide(130), D.CONFIG_ERROR
                )

    def test_legacy_cost_is_strictly_40(self):
        for value in (None, True, False, -1, 0, 39, 80, 40.0, "40", 10**5000):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(
                    budget.FarmState(cost=value).decide(130), D.CONFIG_ERROR
                )

    def test_invalid_reward_mode(self):
        for value in (None, True, 40, 80, "", "AUTO", "automatic", " 40 ", []):
            with self.subTest(value=value):
                self.assertEqual(
                    budget.FarmState(reward_mode=value).decide(130), D.CONFIG_ERROR
                )

    def test_invalid_supported_costs(self):
        for value in (
            None,
            True,
            40,
            "40",
            (),
            [40, 40],
            [40, 80, 120],
            [None],
            [True],
            [-40],
            [40.0],
            [120],
            [10**5000],
            [[40]],
            {"40": 40},
        ):
            with self.subTest(kind=type(value).__name__):
                state = budget.FarmState(supported_costs=value)
                self.assertEqual(state.decide(130), D.CONFIG_ERROR)
                self.assertFalse(state.begin_claim())

    def test_invalid_offered_costs(self):
        for value in (
            True,
            40,
            "40",
            [40, 40],
            [40, 80, 120],
            [None],
            [True],
            [-40],
            [40.0],
            [120],
            [10**5000],
            [[40]],
            {"40": 40},
        ):
            with self.subTest(kind=type(value).__name__):
                state = budget.FarmState()
                self.assertEqual(state.decide(130, value), D.CONFIG_ERROR)
                self.assertFalse(state.begin_claim())

    def test_invalid_existing_counters_fail_closed(self):
        for field in ("claimed", "spent", "claims_40", "claims_80"):
            for value in (None, True, -1, 1.0, 10**5000):
                with self.subTest(field=field, kind=type(value).__name__):
                    self.assertEqual(
                        budget.FarmState(**{field: value}).decide(130), D.CONFIG_ERROR
                    )


class ClaimTests(unittest.TestCase):
    def test_begin_requires_prior_continue(self):
        state = budget.FarmState()
        self.assertFalse(state.begin_claim())
        self.assertFalse(state.confirm_claim())
        self.assertFalse(state.pending)
        self.assertIsNone(state.pending_cost)
        self.assertIsNone(state.pending_balance)

    def test_pending_locks_approved_cost_and_balance(self):
        state = budget.FarmState(reward_mode="auto", reserve=10)
        self.assertEqual(state.decide(130), D.CONTINUE)
        self.assertTrue(state.begin_claim(80))
        self.assertEqual((state.pending_cost, state.pending_balance), (80, 130))
        self.assertEqual(state.decide(40), D.PENDING)
        self.assertEqual(
            (state.last_balance, state.pending_cost, state.pending_balance),
            (130, 80, 130),
        )
        self.assertFalse(state.begin_claim())
        self.assertFalse(state.begin_claim(40))
        self.assertTrue(state.confirm_claim())
        self.assertEqual((state.spent, state.claimed, state.last_balance), (80, 1, 50))

    def test_confirm_counts_once_per_claim(self):
        for mode, cost in (("40", 40), ("80", 80)):
            with self.subTest(mode=mode):
                state = budget.FarmState(reward_mode=mode)
                self.assertFalse(state.confirm_claim())
                state.decide(130)
                self.assertTrue(state.begin_claim())
                self.assertFalse(state.begin_claim())
                self.assertTrue(state.confirm_claim())
                self.assertFalse(state.confirm_claim())
                self.assertEqual(
                    (state.claimed, state.spent, state.last_balance),
                    (1, cost, 130 - cost),
                )
                self.assertEqual(
                    (state.claims_40, state.claims_80), (1, 0) if cost == 40 else (0, 1)
                )
                self.assertIsNone(state.pending_cost)
                self.assertIsNone(state.pending_balance)

    def test_after_confirm_requires_a_new_decision(self):
        state = budget.FarmState()
        state.decide(130)
        state.begin_claim()
        state.confirm_claim()
        self.assertFalse(state.begin_claim())
        self.assertEqual(state.decide(90), D.CONTINUE)
        self.assertTrue(state.begin_claim())

    def test_wrong_explicit_cost_does_not_change_selected_reward(self):
        state = budget.FarmState(reward_mode="auto")
        state.decide(130)
        for value in (40, 0, -80, 80.0, True, "80", 10**5000):
            with self.subTest(kind=type(value).__name__):
                self.assertFalse(state.begin_claim(value))
                self.assertFalse(state.pending)
        self.assertEqual(state.selected_cost, 80)
        self.assertTrue(state.begin_claim(80))

    def test_non_continue_decision_revokes_old_approval(self):
        for balance, offered, expected in (
            (10, None, D.EXHAUSTED),
            (None, None, D.UNKNOWN_BALANCE),
            (130, [], D.NO_REWARD),
            (130, [120], D.CONFIG_ERROR),
        ):
            with self.subTest(expected=expected):
                state = budget.FarmState()
                self.assertEqual(state.decide(130), D.CONTINUE)
                self.assertEqual(state.decide(balance, offered), expected)
                self.assertIsNone(state.selected_cost)
                self.assertFalse(state.begin_claim())

    def test_public_snapshot_changes_cannot_bypass_approval(self):
        for field, value in (
            ("last_balance", 999999),
            ("last_balance", 0),
            ("selected_cost", 40),
            ("selected_cost", 80.0),
            ("spent", 100),
        ):
            with self.subTest(field=field, value=value):
                state = budget.FarmState(reward_mode="auto")
                state.decide(130, (80,))
                setattr(state, field, value)
                self.assertFalse(state.begin_claim())

    def test_cancel_keeps_unknown_charge_locked_without_counting(self):
        state = budget.FarmState(reward_mode="auto")
        state.decide(130)
        self.assertTrue(state.begin_claim())
        state.cancel_claim()
        state.cancel_claim()
        self.assertTrue(state.pending)
        self.assertTrue(state.uncertain)
        self.assertEqual(
            (state.pending_cost, state.pending_balance, state.spent, state.claimed),
            (80, 130, 0, 0),
        )
        self.assertEqual(state.decide(130), D.PENDING)
        self.assertFalse(state.begin_claim())
        self.assertFalse(state.begin_claim(40))

    def test_late_success_evidence_can_settle_uncertain_claim_once(self):
        state = budget.FarmState(reward_mode="auto")
        state.decide(130)
        state.begin_claim()
        state.cancel_claim()
        self.assertTrue(state.confirm_claim())
        self.assertFalse(state.confirm_claim())
        self.assertFalse(state.uncertain)
        self.assertEqual((state.claimed, state.spent, state.last_balance), (1, 80, 50))
        self.assertEqual(state.decide(50), D.CONTINUE)
        self.assertEqual(state.selected_cost, 40)

    def test_cancel_without_pending_does_not_fabricate_a_charge(self):
        state = budget.FarmState()
        state.decide(130)
        state.cancel_claim()
        self.assertFalse(state.pending)
        self.assertFalse(state.uncertain)
        self.assertFalse(state.confirm_claim())
        self.assertFalse(state.begin_claim())
        self.assertEqual(state.decide(130), D.CONTINUE)
        self.assertTrue(state.begin_claim())

    def test_pending_without_a_valid_locked_transaction_cannot_confirm(self):
        state = budget.FarmState(pending=True)
        self.assertFalse(state.confirm_claim())
        self.assertEqual(state.claimed, 0)
        self.assertTrue(state.pending)

    def test_invalid_locked_values_cannot_confirm(self):
        for field, value in (
            ("pending_cost", None),
            ("pending_cost", True),
            ("pending_cost", -40),
            ("pending_cost", 40.0),
            ("pending_cost", 120),
            ("pending_balance", None),
            ("pending_balance", True),
            ("pending_balance", -1),
            ("pending_balance", 130.0),
            ("pending_balance", 1000000),
            ("pending_balance", 39),
        ):
            with self.subTest(field=field, value=value):
                state = budget.FarmState()
                state.decide(130)
                state.begin_claim()
                setattr(state, field, value)
                self.assertFalse(state.confirm_claim())
                self.assertEqual((state.claimed, state.spent), (0, 0))
                self.assertTrue(state.pending)


if __name__ == "__main__":
    unittest.main()
