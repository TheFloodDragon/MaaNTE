"""赠礼安全状态机离线测试：不导入动作注册、Maa、视觉库或网络。"""

import ast
from dataclasses import FrozenInstanceError, replace
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "agent/custom/action/Gift/state.py"
SPEC = importlib.util.spec_from_file_location("_gift_pure_state_tests", STATE_PATH)
state = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = state
SPEC.loader.exec_module(state)

Recipient = state.Recipient
Evidence = state.Evidence
GiftSession = state.GiftSession
GiftSafetyError = state.GiftSafetyError
ScanEvidence = state.ScanEvidence
ScanProof = state.ScanProof
ScanTracker = state.ScanTracker


def observation(frame=1, **changes):
    return replace(
        Evidence(
            frame_id=frame,
            character_id="alice",
            gift_id="A",
            remaining=3,
            total=3,
            inventory=3,
            quantity=1,
            page_verified=True,
            identity_verified=True,
            gift_verified=True,
            selected=True,
        ),
        **changes,
    )


def quota_observation(frame=1, **changes):
    return replace(
        Evidence(
            frame_id=frame,
            character_id="alice",
            remaining=3,
            total=3,
            page_verified=True,
            identity_verified=True,
        ),
        **changes,
    )


def scan_page(frame=1, **changes):
    return replace(
        ScanEvidence(
            frame_id=frame,
            character_id="alice",
            gift_id="A",
            page_key="page-" + str(frame),
            page_verified=True,
            identity_verified=True,
            category_verified=True,
            no_errors=True,
            absence_verified=True,
        ),
        **changes,
    )


def session(limit=3, **options):
    return GiftSession([Recipient("alice", ("A", "B"), limit)], **options)


class GiftTestCase(unittest.TestCase):
    def denied(self, code, operation, *args, **kwargs):
        with self.assertRaises(GiftSafetyError) as raised:
            operation(*args, **kwargs)
        if code is not None:
            self.assertEqual(raised.exception.code, code)
        return raised.exception

    def assert_no_intent(self, run):
        self.assertIsNone(run.pending)
        self.assertEqual(run.attempts, 0)
        self.assertEqual(run.summary()["confirmed"], 0)

    def deliver(self, run, frame, remaining, inventory, **identity):
        pending = run.authorize(
            observation(frame, remaining=remaining, inventory=inventory, **identity)
        )
        self.assertIs(run.pending, pending)
        self.assertTrue(run.is_pending(pending))
        self.assertEqual(
            run.confirm(
                observation(
                    frame + 1,
                    remaining=remaining - 1,
                    inventory=inventory - 1,
                    **identity,
                )
            ),
            "confirmed",
        )
        self.assertFalse(run.is_pending(pending))
        return frame + 2

    def absent_proof(self, first=1, last=2):
        tracker = ScanTracker("alice", "A")
        self.assertFalse(tracker.observe(scan_page(first, start_verified=True)))
        self.assertTrue(
            tracker.observe(scan_page(last, progress_verified=True, end_verified=True))
        )
        return tracker.proof()


class ConfigurationAndQuotaTests(GiftTestCase):
    def test_default_limit_is_three(self):
        recipient = Recipient("alice", ("A",))
        self.assertEqual(recipient.limit, 3)
        self.assertEqual(GiftSession([recipient]).remaining_target(3, 3), 3)

    def test_target_subtracts_already_sent_for_every_valid_limit_and_counter(self):
        for limit in (1, 2, 3):
            for remaining in (0, 1, 2, 3):
                with self.subTest(limit=limit, remaining=remaining):
                    self.assertEqual(
                        session(limit).remaining_target(remaining, 3),
                        max(0, limit - (3 - remaining)),
                    )

    def test_invalid_limits_never_coerce_booleans_or_strings(self):
        for limit in (None, True, False, 0, -1, 4, 1.0, "1", "3", 999):
            with self.subTest(limit=limit):
                self.denied("invalid_limit", Recipient, "alice", ("A",), limit)

    def test_invalid_identifiers_and_gift_lists_rejected(self):
        for identifier in (None, "", " ", " alice", "alice ", True, 3):
            with self.subTest(identifier=identifier):
                self.denied("invalid_character_id", Recipient, identifier, ("A",))
        for gifts in (None, [], ["A"], (), "A"):
            with self.subTest(gifts=gifts):
                self.denied("invalid_gifts", Recipient, "alice", gifts)
        for gifts in (("",), (None,), (" A",)):
            with self.subTest(gifts=gifts):
                self.denied("invalid_gift_id", Recipient, "alice", gifts)
        self.denied("duplicate_gift", Recipient, "alice", ("A", "A"))

    def test_duplicate_characters_do_not_increase_limit(self):
        for second_limit in (1, 3):
            self.denied(
                "duplicate_recipient",
                GiftSession,
                [
                    Recipient("alice", ("A",), 1),
                    Recipient("alice", ("B",), second_limit),
                ],
            )

    def test_session_options_are_strict_and_budget_is_at_most_ten(self):
        for value in (-1, 11, True, False, "10", 10.0, None):
            with self.subTest(budget=value):
                self.denied("invalid_send_budget", session, send_budget=value)
        for value in (0, 1, "false", None):
            with self.subTest(dry_run=value):
                self.denied("invalid_dry_run", session, dry_run=value)
        self.denied("invalid_recipient", GiftSession, ["alice"])

    def test_empty_ui_selection_can_summarize_but_never_authorize(self):
        run = GiftSession(())
        self.assertIsNone(run.current)
        self.assertIsNone(run.next_recipient())
        self.denied("no_recipient", run.authorize, observation())
        self.assertEqual(run.summary()["recipients"], [])
        self.assert_no_intent(run)

    def test_quota_without_selected_gift_finishes_an_already_met_target(self):
        run = session(limit=2)
        evidence = observation(
            remaining=1,
            gift_id=None,
            gift_verified=False,
            selected=False,
            inventory=None,
            quantity=None,
        )
        self.assertEqual(run.check_quota(evidence), 0)
        self.assertEqual(run.current.status, "completed")
        self.assertEqual(run.current.reason, "target_reached")
        self.assert_no_intent(run)

    def test_quota_requires_fresh_page_and_identity_evidence(self):
        for changes in (
            {"page_verified": False},
            {"identity_verified": False},
            {"character_id": "bob"},
            {"remaining": None},
            {"total": 4},
        ):
            with self.subTest(changes=changes):
                run = session()
                self.denied(None, run.check_quota, observation(**changes))
                self.assert_no_intent(run)
        run = session()
        self.assertEqual(run.check_quota(observation()), 3)
        self.denied("stale_frame", run.authorize, observation())

    def test_quota_change_between_selection_and_submit_is_not_silently_accepted(self):
        run = session()
        self.assertEqual(run.check_quota(observation()), 3)
        self.denied("counter_changed", run.authorize, observation(2, remaining=2))
        self.assertEqual(run.current.status, "uncertain")
        self.assert_no_intent(run)

    def test_invalid_remaining_and_total_cannot_authorize(self):
        for remaining in (None, True, False, -1, 4, "3", 2.0):
            with self.subTest(remaining=remaining):
                run = session()
                self.denied(
                    "invalid_remaining", run.authorize, observation(remaining=remaining)
                )
                self.assert_no_intent(run)
        for total in (None, True, False, 0, 2, 4, "3", 3.0):
            with self.subTest(total=total):
                run = session()
                self.denied("invalid_total", run.authorize, observation(total=total))
                self.assert_no_intent(run)

    def test_already_at_target_never_creates_intent(self):
        for limit, remaining in ((3, 0), (2, 1), (1, 2), (1, 0)):
            run = session(limit)
            self.denied(
                "target_reached", run.authorize, observation(remaining=remaining)
            )
            self.assert_no_intent(run)
            self.assertEqual(run.current.status, "completed")

    def test_already_sent_one_only_sends_two_more(self):
        run = session()
        self.deliver(run, 1, 2, 5)
        self.deliver(run, 3, 1, 4)
        self.assertEqual(run.current.confirmed, 2)
        self.assertEqual(run.current.status, "completed")
        self.denied("recipient_closed", run.authorize, observation(5, remaining=0))

    def test_configuration_and_exposed_state_are_immutable(self):
        run = session()
        with self.assertRaises(FrozenInstanceError):
            run.current.recipient.limit = 9
        with self.assertRaises(FrozenInstanceError):
            run.current.gift_index = 1
        self.assertIsInstance(run.states, tuple)


class QuotaPrecheckTests(GiftTestCase):
    def test_quota_needs_no_gift_inventory_quantity_or_selection(self):
        for limit in (1, 2, 3):
            for remaining in (0, 1, 2, 3):
                with self.subTest(limit=limit, remaining=remaining):
                    run = session(limit)
                    target = max(0, limit - (3 - remaining))
                    self.assertEqual(
                        run.check_quota(quota_observation(remaining=remaining)), target
                    )
                    self.assertEqual(run.current.last_remaining, remaining)
                    self.assertEqual(
                        run.current.status, "ready" if target else "completed"
                    )
                    if target == 0:
                        self.assertEqual(run.current.reason, "target_reached")
                    self.assertEqual(run.current.gift_id, "A")
                    self.assert_no_intent(run)

    def test_page_and_identity_require_literal_true(self):
        for field in ("page_verified", "identity_verified"):
            for value in (False, None, 0, 1, "true"):
                with self.subTest(field=field, value=value):
                    run = session()
                    self.denied(
                        "unverified_" + field,
                        run.check_quota,
                        quota_observation(**{field: value}),
                    )
                    self.assertIsNone(run.current.last_remaining)
                    self.assert_no_intent(run)

    def test_unknown_or_wrong_character_cannot_supply_quota(self):
        for character in (None, "", "bob", True):
            with self.subTest(character=character):
                run = session()
                self.denied(
                    "wrong_character",
                    run.check_quota,
                    quota_observation(character_id=character),
                )
                self.assertIsNone(run.current.last_remaining)
                self.assert_no_intent(run)

    def test_counter_values_are_strict_and_invalid_values_do_not_close_target(self):
        for field, values, code in (
            (
                "remaining",
                (None, True, False, -1, 4, "0", "3", 0.0),
                "invalid_remaining",
            ),
            ("total", (None, True, False, 0, 2, 4, "3", 3.0), "invalid_total"),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    run = session()
                    self.denied(
                        code, run.check_quota, quota_observation(**{field: value})
                    )
                    self.assertEqual(run.current.status, "ready")
                    self.assertIsNone(run.current.last_remaining)
                    self.assert_no_intent(run)

    def test_quota_observations_require_new_strict_integer_frames(self):
        for frame in (None, -1, True, False, 1.0, "1"):
            run = session()
            self.denied("invalid_frame", run.check_quota, quota_observation(frame))
            self.assert_no_intent(run)
        run = session()
        self.denied("invalid_evidence", run.check_quota, None)
        self.denied(
            "unverified_page_verified",
            run.check_quota,
            quota_observation(2, page_verified=False),
        )
        self.denied("stale_frame", run.check_quota, quota_observation(2))
        self.assertEqual(run.check_quota(quota_observation(3)), 3)
        self.denied("stale_frame", run.check_quota, quota_observation(1))
        self.assert_no_intent(run)

    def test_quota_never_substitutes_for_full_click_evidence(self):
        run = session()
        self.assertEqual(run.check_quota(quota_observation()), 3)
        self.denied("stale_frame", run.authorize, observation())
        self.denied("wrong_gift", run.authorize, quota_observation(2))
        self.assert_no_intent(run)
        self.deliver(run, 3, 3, 3)
        self.assertEqual(run.check_quota(quota_observation(5, remaining=2)), 2)
        self.assertEqual(run.current.last_remaining, 2)
        self.assertEqual(run.current.confirmed, 1)
        self.assertEqual(run.attempts, 1)

    def test_pending_forbids_quota_precheck_and_keeps_original_intent(self):
        run = session()
        intent = run.authorize(observation())
        self.denied(
            "pending_exists", run.check_quota, quota_observation(2, remaining=0)
        )
        self.assertIs(run.pending, intent)
        self.assertTrue(run.is_pending(intent))
        self.assertEqual(run.current.status, "pending")
        self.assertEqual(run.current.last_remaining, 3)
        self.assertEqual(
            run.confirm(observation(2, remaining=2, inventory=2)), "confirmed"
        )
        self.assertEqual(run.current.confirmed, 1)

    def test_counter_change_locks_only_current_and_next_character_is_independent(self):
        for remaining in (1, 3):
            run = GiftSession([Recipient("alice", ("A",)), Recipient("bob", ("B",), 1)])
            self.assertEqual(run.check_quota(quota_observation(remaining=2)), 2)
            self.denied(
                "counter_changed",
                run.check_quota,
                quota_observation(2, remaining=remaining),
            )
            self.assertEqual(run.current.status, "uncertain")
            self.assertEqual(run.current.last_remaining, 2)
            self.assert_no_intent(run)
            self.assertEqual(run.next_recipient().character_id, "bob")
            self.assertEqual(
                run.check_quota(quota_observation(3, character_id="bob")), 1
            )
            self.deliver(run, 4, 3, 1, character_id="bob", gift_id="B")
            self.assertEqual([item.confirmed for item in run.states], [0, 1])

    def test_completed_quota_allows_next_character_to_read_its_own_counter(self):
        run = GiftSession([Recipient("alice", ("A",), 2), Recipient("bob", ("B",), 3)])
        self.assertEqual(run.check_quota(quota_observation(remaining=1)), 0)
        self.denied("recipient_closed", run.check_quota, quota_observation(2))
        self.assertEqual(run.next_recipient().character_id, "bob")
        self.denied(
            "stale_frame", run.check_quota, quota_observation(character_id="bob")
        )
        self.assertEqual(run.check_quota(quota_observation(2, character_id="bob")), 3)
        self.assertEqual([item.last_remaining for item in run.states], [1, 3])
        self.assert_no_intent(run)

    def test_infinite_and_global_flags_reject_non_boolean_values(self):
        for field, code in (
            ("infinite", "invalid_infinite"),
            ("global_limit", "invalid_global_limit"),
        ):
            for value in (None, 0, 1, "true", "∞"):
                run = session()
                self.denied(code, run.check_quota, quota_observation(**{field: value}))
                self.assertIsNone(run.current.last_remaining)
                self.assert_no_intent(run)
        run = session()
        self.denied(
            "infinite_quantity", run.check_quota, quota_observation(infinite=True)
        )
        self.assert_no_intent(run)

    def test_global_limit_blocks_remaining_without_erasing_completed_recipient(self):
        run = GiftSession(
            [
                Recipient("alice", ("A",), 1),
                Recipient("bob", ("B",)),
                Recipient("carol", ("C",)),
            ]
        )
        self.assertEqual(run.check_quota(quota_observation(remaining=2)), 0)
        run.next_recipient()
        self.denied(
            "global_limit",
            run.check_quota,
            quota_observation(2, character_id="bob", global_limit=True),
        )
        self.assertEqual(
            [item.status for item in run.states], ["completed", "skipped", "skipped"]
        )
        self.assertEqual(
            [item.reason for item in run.states],
            ["target_reached", "global_limit", "global_limit"],
        )
        self.assertIsNone(run.next_recipient())
        self.assert_no_intent(run)

    def test_dry_run_supports_quota_precheck_then_explicit_summary_reason(self):
        run = session(dry_run=True)
        self.assertEqual(run.check_quota(quota_observation()), 3)
        run.skip_current("dry_run")
        result = run.summary()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["recipients"][0]["status"], "skipped")
        self.assertEqual(result["recipients"][0]["reason"], "dry_run")
        self.assert_no_intent(run)
        complete = session(dry_run=True)
        self.assertEqual(complete.check_quota(quota_observation(remaining=0)), 0)
        self.assertEqual(complete.current.reason, "target_reached")
        self.assert_no_intent(complete)

    def test_quota_precheck_obeys_cancel_block_and_empty_session(self):
        stopped = session()
        stopped.cancel()
        self.denied("stopped", stopped.check_quota, quota_observation())
        self.assert_no_intent(stopped)
        blocked = session()
        blocked.block_remaining("send_budget")
        self.denied("send_budget", blocked.check_quota, quota_observation())
        self.assert_no_intent(blocked)
        empty = GiftSession(())
        self.denied("no_recipient", empty.check_quota, quota_observation())
        self.assert_no_intent(empty)

    def test_quota_cannot_reuse_an_observation_before_page_invalidation(self):
        run = session()
        run.invalidate("page_changed", frame_id=3)
        self.denied("stale_frame", run.check_quota, quota_observation(2, remaining=0))
        self.assertEqual(run.current.status, "ready")
        self.assertEqual(run.check_quota(quota_observation(4, remaining=0)), 0)
        self.assertEqual(run.current.status, "completed")
        self.assert_no_intent(run)


class AuthorizationTests(GiftTestCase):
    def test_default_evidence_is_unsafe(self):
        run = session()
        self.denied("invalid_frame", run.authorize, Evidence())
        self.assert_no_intent(run)

    def test_every_positive_gate_must_be_literal_true(self):
        for field in (
            "page_verified",
            "identity_verified",
            "gift_verified",
            "selected",
        ):
            for value in (False, None, 0, 1, "true"):
                with self.subTest(field=field, value=value):
                    run = session()
                    self.denied(
                        "unverified_" + field,
                        run.authorize,
                        observation(**{field: value}),
                    )
                    self.assert_no_intent(run)

    def test_unknown_or_wrong_identity_and_gift_rejected(self):
        for field, code in (
            ("character_id", "wrong_character"),
            ("gift_id", "wrong_gift"),
        ):
            for value in (None, "", "other", True):
                with self.subTest(field=field, value=value):
                    run = session()
                    self.denied(code, run.authorize, observation(**{field: value}))
                    self.assert_no_intent(run)

    def test_inventory_must_be_known_positive_integer(self):
        for value in (None, 0, -1, True, False, 1.0, "1", "∞"):
            with self.subTest(value=value):
                run = session()
                self.denied(
                    "invalid_inventory", run.authorize, observation(inventory=value)
                )
                self.assert_no_intent(run)

    def test_quantity_must_be_exactly_integer_one(self):
        for value in (None, 0, -1, 2, 3, True, False, 1.0, "1", "∞"):
            with self.subTest(value=value):
                run = session()
                self.denied(
                    "invalid_quantity", run.authorize, observation(quantity=value)
                )
                self.assert_no_intent(run)

    def test_infinity_marker_is_always_forbidden(self):
        run = session()
        self.denied("infinite_quantity", run.authorize, observation(infinite=True))
        self.assert_no_intent(run)
        for value in (0, 1, None, "∞"):
            run = session()
            self.denied("invalid_infinite", run.authorize, observation(infinite=value))
            self.assert_no_intent(run)

    def test_global_limit_flag_must_be_boolean(self):
        for value in (None, 0, 1, "true"):
            run = session()
            self.denied(
                "invalid_global_limit", run.authorize, observation(global_limit=value)
            )
            self.assert_no_intent(run)

    def test_authorization_is_recorded_before_input_and_consumes_attempt(self):
        run = session()
        pending = run.authorize(observation())
        self.assertIs(pending, run.pending)
        self.assertEqual(run.current.status, "pending")
        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.current.attempts, 1)
        self.assertEqual(pending.quantity, 1)
        self.assertEqual(pending.remaining, 3)
        self.assertEqual(pending.inventory, 3)
        self.assertEqual(run.summary()["confirmed"], 0)
        self.assertTrue(run.is_pending(pending))
        self.assertFalse(run.is_pending(replace(pending)))

    def test_frames_are_strict_nonnegative_integers(self):
        for value in (None, -1, True, False, 1.0, "1"):
            run = session()
            self.denied("invalid_frame", run.authorize, observation(frame_id=value))
            self.assert_no_intent(run)
        run = session()
        self.assertEqual(run.authorize(observation(0)).frame_id, 0)

    def test_rejected_frame_cannot_be_reused(self):
        run = session()
        self.denied("unverified_selected", run.authorize, observation(selected=False))
        self.denied("stale_frame", run.authorize, observation())
        self.assert_no_intent(run)
        run.authorize(observation(2))
        self.assertEqual(run.attempts, 1)

    def test_new_authorization_requires_fresh_frame_after_confirmation(self):
        run = session()
        self.deliver(run, 10, 3, 3)
        self.denied(
            "stale_frame", run.authorize, observation(11, remaining=2, inventory=2)
        )
        self.assertEqual(run.attempts, 1)
        self.assertIsNone(run.pending)

    def test_remaining_change_between_sends_locks_recipient(self):
        for remaining in (1, 3):
            run = session()
            self.deliver(run, 1, 3, 4)
            self.denied(
                "counter_changed",
                run.authorize,
                observation(3, remaining=remaining, inventory=3),
            )
            self.assertEqual(run.current.status, "uncertain")
            self.assertEqual(run.current.confirmed, 1)
            self.assertEqual(run.attempts, 1)


class ReceiptAndIsolationTests(GiftTestCase):
    def test_delayed_receipt_only_observes_and_never_replays_send(self):
        run = session()
        send = Mock()
        send(run.authorize(observation()))
        self.assertEqual(run.confirm(observation(2)), "waiting")
        self.denied("pending_exists", run.authorize, observation(3))
        self.assertEqual(run.confirm(observation(3)), "waiting")
        self.assertEqual(
            run.confirm(observation(4, remaining=2, inventory=2)), "confirmed"
        )
        send.assert_called_once()
        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.current.confirmed, 1)

    def test_missing_receipt_times_out_uncertain_not_success(self):
        run = session()
        pending = run.authorize(observation())
        self.assertEqual(run.confirm(Evidence(frame_id=2)), "waiting")
        self.assertEqual(run.timeout_pending(), "uncertain")
        self.assertEqual(run.current.status, "uncertain")
        self.assertEqual(run.current.reason, "confirmation_timeout")
        self.assertFalse(run.is_pending(pending))
        self.denied("recipient_closed", run.authorize, observation(3))
        self.denied("no_pending", run.confirm, observation(4, remaining=2, inventory=2))
        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.current.confirmed, 0)

    def test_partial_recognition_is_waiting_not_success(self):
        for field in ("page_verified", "identity_verified", "gift_verified"):
            run = session()
            run.authorize(observation())
            self.assertEqual(
                run.confirm(observation(2, remaining=2, inventory=2, **{field: False})),
                "waiting",
            )
            self.assertEqual(run.current.confirmed, 0)
            self.assertIsNotNone(run.pending)
        for field in ("character_id", "gift_id", "remaining", "total"):
            run = session()
            run.authorize(observation())
            self.assertEqual(run.confirm(observation(2, **{field: None})), "waiting")
            self.assertEqual(run.current.confirmed, 0)

    def test_exact_decrement_can_confirm_when_inventory_unreadable(self):
        run = session()
        run.authorize(observation())
        self.assertEqual(
            run.confirm(observation(2, remaining=2, inventory=None)), "confirmed"
        )
        self.assertEqual(run.current.confirmed, 1)

    def test_depleted_gift_still_confirms_from_recognizable_details(self):
        run = session()
        run.authorize(observation(inventory=1))
        self.assertEqual(
            run.confirm(
                observation(2, remaining=2, inventory=0, quantity=None, selected=False)
            ),
            "confirmed",
        )
        self.assertEqual(run.current.confirmed, 1)
        self.assertEqual(run.current.gift_id, "A")

    def test_counter_jumps_and_increases_lock_without_replay(self):
        for before, after in ((3, 1), (3, 0), (2, 3)):
            run = session()
            run.authorize(observation(remaining=before))
            self.assertEqual(
                run.confirm(observation(2, remaining=after, inventory=None)),
                "uncertain",
            )
            self.assertEqual(run.current.reason, "counter_jump")
            self.assertEqual(run.current.confirmed, 0)
            self.denied("recipient_closed", run.authorize, observation(3))

    def test_inventory_disagreement_is_uncertain_even_if_counter_looks_good(self):
        for remaining, inventory in ((2, 3), (2, 1), (2, 4), (3, 2), (3, 4)):
            run = session()
            run.authorize(observation())
            self.assertEqual(
                run.confirm(observation(2, remaining=remaining, inventory=inventory)),
                "uncertain",
            )
            self.assertEqual(run.current.reason, "inventory_conflict")
            self.assertEqual(run.current.confirmed, 0)

    def test_identity_or_gift_change_locks_current(self):
        for field, value in (("character_id", "bob"), ("gift_id", "B")):
            run = session()
            run.authorize(observation())
            self.assertEqual(
                run.confirm(observation(2, remaining=2, inventory=2, **{field: value})),
                "uncertain",
            )
            self.assertEqual(run.current.confirmed, 0)
            self.assertIsNone(run.pending)

    def test_invalid_receipt_values_never_count_as_confirmation(self):
        mutations = (
            {"total": True},
            {"total": 4},
            {"total": 3.0},
            {"remaining": True},
            {"remaining": -1},
            {"remaining": "2"},
            {"inventory": True},
            {"inventory": -1},
            {"inventory": "2"},
            {"quantity": True},
            {"quantity": 2},
            {"quantity": "1"},
            {"infinite": True},
            {"global_limit": "true"},
            {"page_verified": 1},
            {"identity_verified": 1},
            {"gift_verified": 1},
            {"selected": 1},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                run = session()
                run.authorize(observation())
                receipt = replace(observation(2, remaining=2, inventory=2), **mutation)
                self.assertEqual(run.confirm(receipt), "uncertain")
                self.assertEqual(run.current.confirmed, 0)
                self.assertEqual(run.attempts, 1)

    def test_stale_receipt_does_not_resolve_pending(self):
        run = session()
        intent = run.authorize(observation(5))
        self.denied(
            "stale_frame", run.confirm, observation(5, remaining=2, inventory=2)
        )
        self.assertIs(run.pending, intent)
        self.assertEqual(
            run.confirm(observation(6, remaining=2, inventory=2)), "confirmed"
        )
        self.denied("no_pending", run.confirm, observation(7, remaining=2, inventory=2))
        self.assertEqual(run.current.confirmed, 1)

    def test_pending_forbids_authorize_skip_exhaust_and_character_switch(self):
        run = GiftSession([Recipient("alice", ("A", "B")), Recipient("bob", ("A",))])
        intent = run.authorize(observation())
        operations = (
            lambda: run.authorize(observation(2)),
            lambda: run.exhaust_current_gift(observation(2, inventory=0)),
            lambda: run.skip_current("unsafe"),
            run.next_recipient,
        )
        for operation in operations:
            self.denied("pending_exists", operation)
            self.assertIs(run.pending, intent)
            self.assertEqual(run.current.character_id, "alice")
        run.timeout_pending()
        self.assertEqual(run.next_recipient().character_id, "bob")
        self.deliver(run, 2, 3, 3, character_id="bob")
        self.assertEqual(run.states[0].confirmed, 0)
        self.assertEqual(run.states[1].confirmed, 1)

    def test_page_change_revokes_intent_even_for_same_boundary_frame(self):
        for boundary in (1, 2):
            run = session()
            intent = run.authorize(observation())
            run.invalidate("page_changed", frame_id=boundary)
            self.assertFalse(run.is_pending(intent))
            self.assertEqual(run.current.status, "uncertain")
            self.denied("recipient_closed", run.authorize, observation(3))
            self.assertEqual(run.attempts, 1)

    def test_invalidating_old_frame_still_revokes_intent(self):
        run = session()
        intent = run.authorize(observation(5))
        self.denied("stale_frame", run.invalidate, "page_changed", frame_id=4)
        self.assertFalse(run.is_pending(intent))
        self.assertEqual(run.current.status, "uncertain")

    def test_page_boundary_rejects_unconsumed_old_screenshot(self):
        run = session()
        old = observation(3)
        run.invalidate("navigation", frame_id=4)
        self.denied("stale_frame", run.authorize, old)
        self.assert_no_intent(run)
        run.authorize(observation(5))

    def test_characters_have_independent_quota_and_candidate_order(self):
        run = GiftSession(
            [Recipient("alice", ("A", "B"), 1), Recipient("bob", ("B", "A"), 3)]
        )
        self.deliver(run, 1, 3, 3)
        self.assertEqual(run.current.status, "completed")
        current = run.next_recipient()
        self.assertEqual(current.character_id, "bob")
        self.assertEqual(current.gift_id, "B")
        self.assertEqual(run.remaining_target(3, 3), 3)
        for frame, remaining, inventory in ((3, 3, 3), (5, 2, 2), (7, 1, 1)):
            self.deliver(
                run, frame, remaining, inventory, character_id="bob", gift_id="B"
            )
        self.assertEqual([item.confirmed for item in run.states], [1, 3])
        self.assertIsNone(run.next_recipient())

    def test_new_run_reads_actual_remaining_instead_of_reusing_local_count(self):
        first = session()
        first.authorize(observation(50))
        first.timeout_pending()
        second = session()
        self.assertEqual(second.remaining_target(2, 3), 2)
        self.deliver(second, 1, 2, 2)
        self.deliver(second, 3, 1, 1)
        self.assertEqual(second.current.confirmed, 2)
        self.assertEqual(second.current.status, "completed")
        self.assertEqual(first.current.confirmed, 0)


class CandidateAndScanTests(GiftTestCase):
    def test_a_three_never_touches_b(self):
        run = session()
        gifts = []
        for frame, remaining, inventory in ((1, 3, 3), (3, 2, 2), (5, 1, 1)):
            gifts.append(run.current.gift_id)
            self.deliver(run, frame, remaining, inventory)
        self.assertEqual(gifts, ["A", "A", "A"])
        self.assertEqual(run.current.gift_id, "A")
        self.denied(
            "recipient_closed",
            run.exhaust_current_gift,
            observation(7, remaining=0, inventory=0),
        )

    def test_a_one_then_b_two_only_after_verified_zero(self):
        run = session()
        self.deliver(run, 1, 3, 1)
        self.assertEqual(run.current.gift_id, "A")
        self.denied(
            "wrong_gift", run.authorize, observation(3, gift_id="B", remaining=2)
        )
        run.exhaust_current_gift(
            observation(4, remaining=2, inventory=0, selected=False)
        )
        self.assertEqual(run.current.gift_id, "B")
        self.deliver(run, 5, 2, 2, gift_id="B")
        self.deliver(run, 7, 1, 1, gift_id="B")
        self.assertEqual(run.current.confirmed, 3)
        self.assertEqual(run.attempts, 3)

    def test_missing_ambiguous_or_positive_stock_cannot_skip_a(self):
        for inventory in (None, 1, 3, -1, False, "0", "∞"):
            run = session()
            self.denied(
                "stock_not_zero",
                run.exhaust_current_gift,
                observation(inventory=inventory),
            )
            self.assertEqual(run.current.gift_id, "A")
            self.assert_no_intent(run)
        for field in ("page_verified", "identity_verified", "gift_verified"):
            run = session()
            self.denied(
                "unverified_" + field,
                run.exhaust_current_gift,
                observation(inventory=0, **{field: False}),
            )
            self.assertEqual(run.current.gift_id, "A")

    def test_zero_stock_from_wrong_identity_or_infinite_badge_not_accepted(self):
        for changes in ({"character_id": "bob"}, {"gift_id": "B"}, {"infinite": True}):
            run = session()
            self.denied(
                None, run.exhaust_current_gift, observation(inventory=0, **changes)
            )
            self.assertEqual(run.current.gift_id, "A")

    def test_all_candidates_exhausted_marks_skip_not_success(self):
        run = session()
        run.exhaust_current_gift(observation(inventory=0))
        run.exhaust_current_gift(observation(2, gift_id="B", inventory=0))
        self.assertEqual(run.current.status, "skipped")
        self.assertEqual(run.current.reason, "gifts_exhausted")
        self.assertIsNone(run.current.gift_id)
        self.assert_no_intent(run)

    def test_target_reached_with_zero_stock_does_not_advance_candidate(self):
        run = session(1)
        run.exhaust_current_gift(observation(remaining=2, inventory=0))
        self.assertEqual(run.current.status, "completed")
        self.assertEqual(run.current.gift_id, "A")

    def test_a_found_on_later_page_prevents_fallback_to_b(self):
        run = session()
        tracker = ScanTracker("alice", "A")
        self.assertFalse(tracker.observe(scan_page(1, start_verified=True)))
        self.denied("scan_incomplete", tracker.proof)
        self.assertEqual(run.current.gift_id, "A")
        self.assertFalse(
            tracker.observe(
                scan_page(2, found=True, absence_verified=False, progress_verified=True)
            )
        )
        self.assertEqual(tracker.status, "found")
        self.denied("scan_incomplete", tracker.proof)
        intent = run.authorize(observation(3))
        self.assertEqual(intent.gift_id, "A")

    def test_only_complete_absence_can_advance_candidate(self):
        run = session()
        tracker = ScanTracker("alice", "A")
        tracker.observe(scan_page(1, start_verified=True))
        self.denied("scan_incomplete", tracker.proof)
        self.denied("invalid_exhaustion_proof", run.exhaust_current_gift, scan_page(1))
        self.assertEqual(run.current.gift_id, "A")
        tracker.observe(scan_page(2, progress_verified=True, end_verified=True))
        self.assertEqual(tracker.status, "absent")
        run.exhaust_current_gift(tracker.proof())
        self.assertEqual(run.current.gift_id, "B")
        self.denied("stale_frame", run.authorize, observation(2, gift_id="B"))
        run.authorize(observation(3, gift_id="B"))

    def test_scan_requires_correct_page_identity_category_and_no_errors(self):
        for field in (
            "page_verified",
            "identity_verified",
            "category_verified",
            "no_errors",
        ):
            for value in (False, None, 0, 1):
                with self.subTest(field=field, value=value):
                    tracker = ScanTracker("alice", "A")
                    self.denied(
                        "scan_" + field,
                        tracker.observe,
                        scan_page(start_verified=True, **{field: value}),
                    )
                    self.assertEqual(tracker.status, "uncertain")
                    self.denied("scan_incomplete", tracker.proof)

    def test_scan_requires_verified_start_not_merely_end(self):
        tracker = ScanTracker("alice", "A")
        self.denied(
            "scan_start_unverified",
            tracker.observe,
            scan_page(end_verified=True, single_page_verified=True),
        )
        self.denied("scan_incomplete", tracker.proof)

    def test_stationary_frame_is_not_list_end_even_when_requested(self):
        tracker = ScanTracker("alice", "A")
        tracker.observe(scan_page(start_verified=True, page_key="same"))
        self.denied(
            "scan_no_progress",
            tracker.observe,
            scan_page(2, page_key="same", progress_verified=True, end_verified=True),
        )
        self.assertEqual(tracker.status, "uncertain")
        self.denied("scan_incomplete", tracker.proof)

    def test_new_page_key_without_progress_proof_is_insufficient(self):
        tracker = ScanTracker("alice", "A")
        tracker.observe(scan_page(start_verified=True))
        self.denied(
            "scan_progress_unverified", tracker.observe, scan_page(2, end_verified=True)
        )
        self.denied("scan_incomplete", tracker.proof)

    def test_no_explicit_end_never_produces_absence(self):
        tracker = ScanTracker("alice", "A")
        tracker.observe(scan_page(start_verified=True))
        tracker.observe(scan_page(2, progress_verified=True))
        self.assertEqual(tracker.status, "scanning")
        self.denied("scan_incomplete", tracker.proof)

    def test_single_page_requires_explicit_all_visible_proof(self):
        tracker = ScanTracker("alice", "A")
        self.denied(
            "scan_single_page_unverified",
            tracker.observe,
            scan_page(start_verified=True, end_verified=True),
        )
        complete = ScanTracker("alice", "A")
        self.assertTrue(
            complete.observe(
                scan_page(
                    start_verified=True, end_verified=True, single_page_verified=True
                )
            )
        )
        run = session()
        run.exhaust_current_gift(complete.proof())
        self.assertEqual(run.current.gift_id, "B")

    def test_low_confidence_and_contradictory_matches_are_not_absence(self):
        for found, absent in ((False, False), (True, True)):
            tracker = ScanTracker("alice", "A")
            self.denied(
                "scan_ambiguous",
                tracker.observe,
                scan_page(start_verified=True, found=found, absence_verified=absent),
            )
            self.assertEqual(tracker.status, "uncertain")
            self.denied("scan_incomplete", tracker.proof)
            self.denied(
                "scan_closed", tracker.observe, scan_page(2, start_verified=True)
            )

    def test_scan_has_finite_budget_and_never_converts_timeout_to_absence(self):
        tracker = ScanTracker("alice", "A", max_pages=2)
        tracker.observe(scan_page(start_verified=True))
        self.denied(
            "scan_budget", tracker.observe, scan_page(2, progress_verified=True)
        )
        self.assertEqual(tracker.pages_seen, 2)
        self.denied("scan_incomplete", tracker.proof)
        self.denied(
            "scan_closed",
            tracker.observe,
            scan_page(3, progress_verified=True, end_verified=True),
        )
        for budget in (0, -1, 101, True, "20"):
            self.denied("invalid_scan_budget", ScanTracker, "alice", "A", budget)

    def test_scan_rejects_stale_frames_and_wrong_context(self):
        tracker = ScanTracker("alice", "A")
        tracker.observe(scan_page(3, start_verified=True))
        self.denied(
            "stale_frame",
            tracker.observe,
            scan_page(3, progress_verified=True, end_verified=True),
        )
        for field, code in (
            ("character_id", "wrong_character"),
            ("gift_id", "wrong_gift"),
        ):
            tracker = ScanTracker("alice", "A")
            self.denied(
                code,
                tracker.observe,
                scan_page(start_verified=True, **{field: "wrong"}),
            )

    def test_scan_proof_requires_every_independent_positive_flag(self):
        proof = self.absent_proof()
        for field in (
            "page_verified",
            "identity_verified",
            "category_verified",
            "start_verified",
            "end_verified",
            "progress_verified",
            "no_errors",
            "absence_verified",
        ):
            for value in (False, 1):
                with self.subTest(field=field, value=value):
                    run = session()
                    self.denied(
                        None, run.exhaust_current_gift, replace(proof, **{field: value})
                    )
                    self.assertEqual(run.current.gift_id, "A")
                    self.assert_no_intent(run)
        self.denied(None, session().exhaust_current_gift, ScanProof())

    def test_scan_cannot_cross_context_invalidation_boundary(self):
        run = session()
        run.invalidate("changed_category", frame_id=3)
        self.denied("stale_scan", run.exhaust_current_gift, self.absent_proof(1, 4))
        self.assertEqual(run.current.gift_id, "A")
        run.exhaust_current_gift(self.absent_proof(4, 5))
        self.assertEqual(run.current.gift_id, "B")


class BudgetDryRunAndStopTests(GiftTestCase):
    def test_ten_attempt_guard_is_per_run_not_account_daily_quota(self):
        run = GiftSession([Recipient(str(index), ("A",)) for index in range(5)])
        frame = 1
        for index in range(4):
            for count in range(3 if index < 3 else 1):
                frame = self.deliver(
                    run, frame, 3 - count, 3 - count, character_id=str(index)
                )
            if index < 3:
                run.next_recipient()
        summary = run.summary()
        self.assertEqual(summary["attempts"], 10)
        self.assertEqual(summary["confirmed"], 10)
        self.assertEqual(run.states[3].status, "skipped")
        self.assertEqual(run.states[4].reason, "send_budget")
        self.assertIsNone(run.next_recipient())
        self.denied("send_budget", run.authorize, observation(frame))
        self.assertEqual(session().attempts, 0)

    def test_uncertain_intent_consumes_budget_without_counting_success(self):
        run = GiftSession(
            [Recipient("alice", ("A",)), Recipient("bob", ("A",))], send_budget=1
        )
        run.authorize(observation())
        run.timeout_pending()
        self.assertEqual(run.summary()["attempts"], 1)
        self.assertEqual(run.summary()["confirmed"], 0)
        self.assertEqual(run.states[0].status, "uncertain")
        self.assertEqual(run.states[1].status, "skipped")
        self.assertEqual(run.states[1].reason, "send_budget")

    def test_zero_budget_allows_no_intent(self):
        run = session(send_budget=0)
        self.denied("send_budget", run.authorize, observation())
        self.assert_no_intent(run)
        self.assertEqual(run.current.status, "skipped")

    def test_global_limit_before_send_skips_every_remaining_character(self):
        run = GiftSession([Recipient("alice", ("A",)), Recipient("bob", ("B",))])
        self.denied("global_limit", run.authorize, observation(global_limit=True))
        self.assert_no_intent(run)
        self.assertEqual([item.status for item in run.states], ["skipped", "skipped"])
        self.assertTrue(all(item.reason == "global_limit" for item in run.states))
        self.assertIsNone(run.next_recipient())

    def test_global_limit_during_pending_never_invents_confirmation(self):
        run = GiftSession([Recipient("alice", ("A",)), Recipient("bob", ("B",))])
        run.authorize(observation())
        self.assertEqual(
            run.confirm(Evidence(frame_id=2, global_limit=True)), "global_limit"
        )
        self.assertEqual(run.states[0].status, "uncertain")
        self.assertEqual(run.states[1].status, "skipped")
        self.assertEqual(run.summary()["confirmed"], 0)
        self.assertEqual(run.attempts, 1)
        self.assertIsNone(run.pending)

    def test_dry_run_checks_real_evidence_but_never_creates_intent(self):
        run = session(dry_run=True)
        self.denied("dry_run", run.authorize, observation())
        self.denied("dry_run", run.authorize, observation(2))
        self.assert_no_intent(run)
        self.assertTrue(run.summary()["dry_run"])
        self.denied("invalid_quantity", run.authorize, observation(3, quantity=2))
        self.assert_no_intent(run)

    def test_dry_run_can_scan_exhaust_and_identify_next_candidate(self):
        run = session(dry_run=True)
        run.exhaust_current_gift(self.absent_proof())
        self.assertEqual(run.current.gift_id, "B")
        self.denied("dry_run", run.authorize, observation(3, gift_id="B"))
        self.assert_no_intent(run)

    def test_stop_before_authorization_is_latched(self):
        run = session()
        self.denied("stopped", run.authorize, observation(), stopping=True)
        self.assert_no_intent(run)
        self.denied("stopped", run.authorize, observation(2))
        self.assertTrue(run.summary()["stopped"])
        self.assertEqual(run.current.status, "skipped")

    def test_stop_after_intent_revokes_it_without_success(self):
        run = session()
        intent = run.authorize(observation())
        self.assertEqual(
            run.confirm(observation(2, remaining=2, inventory=2), stopping=True),
            "uncertain",
        )
        self.assertFalse(run.is_pending(intent))
        self.assertEqual(run.current.confirmed, 0)
        self.assertEqual(run.attempts, 1)
        self.denied("stopped", run.authorize, observation(3))

    def test_cancel_between_authorize_and_click_revokes_original_token(self):
        run = session()
        intent = run.authorize(observation())
        run.cancel()
        self.assertFalse(run.is_pending(intent))
        self.assertEqual(run.current.status, "uncertain")
        self.assertEqual(run.summary()["confirmed"], 0)
        self.assertIsNone(run.pending)

    def test_stopping_parameter_must_be_literal_bool(self):
        run = session()
        self.denied("invalid_stopping", run.authorize, observation(), stopping=1)
        self.assert_no_intent(run)

    def test_skip_is_explicit_and_does_not_change_confirmed(self):
        run = GiftSession([Recipient("alice", ("A",)), Recipient("bob", ("A",))])
        self.denied("recipient_unfinished", run.next_recipient)
        run.skip_current("unverified_identity")
        self.assertEqual(run.current.reason, "unverified_identity")
        self.assertEqual(run.next_recipient().character_id, "bob")
        self.deliver(run, 1, 3, 3, character_id="bob")
        self.assertEqual([item.confirmed for item in run.states], [0, 1])

    def test_summary_is_a_copy_and_distinguishes_attempts_pending_and_success(self):
        run = session()
        run.authorize(observation())
        summary = run.summary()
        self.assertEqual(
            (summary["attempts"], summary["confirmed"], summary["pending"]),
            (1, 0, True),
        )
        summary["recipients"][0]["confirmed"] = 999
        self.assertEqual(run.current.confirmed, 0)
        run.timeout_pending()
        self.assertEqual(run.summary()["confirmed"], 0)
        self.assertFalse(run.summary()["pending"])

    def test_pure_module_has_only_standard_library_data_dependencies(self):
        tree = ast.parse(STATE_PATH.read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertEqual(imports, {"dataclasses", "typing"})
        forbidden = {
            "post_click",
            "post_swipe",
            "post_screencap",
            "sleep",
            "run_task",
            "urlopen",
        }
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(called_attributes & forbidden)


if __name__ == "__main__":
    unittest.main()
