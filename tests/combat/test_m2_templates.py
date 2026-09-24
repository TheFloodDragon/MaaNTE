"""M2 tests for template validation and custom policy execution."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from _loader import load_combat_module


class TestTemplateValidation(unittest.TestCase):
    """Test custom template validation."""

    def test_rejects_missing_version(self):
        """Template without version is rejected."""
        templates_mod = load_combat_module("templates")

        data = {"id": "test", "type": "custom", "steps": []}
        with self.assertRaises(templates_mod.TemplateError) as ctx:
            templates_mod.CustomTemplate._validate(data, Path("test.json"))
        self.assertIn("version", str(ctx.exception))

    def test_rejects_wrong_version(self):
        """Template with unsupported version is rejected."""
        templates_mod = load_combat_module("templates")

        data = {"version": 999, "id": "test", "type": "custom", "steps": []}
        with self.assertRaises(templates_mod.TemplateError) as ctx:
            templates_mod.CustomTemplate._validate(data, Path("test.json"))
        self.assertIn("version", str(ctx.exception))

    def test_rejects_too_many_steps(self):
        """Template with excessive steps is rejected."""
        templates_mod = load_combat_module("templates")

        data = {
            "version": 1,
            "id": "test",
            "type": "custom",
            "steps": [{"action": "normal_attack"}] * 100,
        }
        with self.assertRaises(templates_mod.TemplateError) as ctx:
            templates_mod.CustomTemplate._validate(data, Path("test.json"))
        self.assertIn("Too many steps", str(ctx.exception))

    def test_rejects_unknown_action(self):
        """Template with unknown action is rejected."""
        templates_mod = load_combat_module("templates")

        data = {
            "version": 1,
            "id": "test",
            "type": "custom",
            "steps": [{"action": "unknown_action"}],
        }
        with self.assertRaises(templates_mod.TemplateError) as ctx:
            templates_mod.CustomTemplate._validate(data, Path("test.json"))
        self.assertIn("unknown action", str(ctx.exception))

    def test_rejects_reobserve_without_max_cycles(self):
        """Template with on_finish=reobserve requires max_cycles."""
        templates_mod = load_combat_module("templates")

        data = {
            "version": 1,
            "id": "test",
            "type": "custom",
            "steps": [{"action": "normal_attack"}],
            "on_finish": "reobserve",
        }
        with self.assertRaises(templates_mod.TemplateError) as ctx:
            templates_mod.CustomTemplate._validate(data, Path("test.json"))
        self.assertIn("max_cycles", str(ctx.exception))

    def test_accepts_valid_template(self):
        """Valid template passes validation."""
        templates_mod = load_combat_module("templates")

        data = {
            "version": 1,
            "id": "test",
            "type": "custom",
            "roles": {"main": 1, "support": 2},
            "steps": [
                {"action": "switch", "role": "main", "required": True},
                {"action": "skill", "when": "skill_ready"},
                {"action": "normal_attack", "max_ms": 1000},
            ],
            "on_finish": "reobserve",
            "max_cycles": 50,
        }

        # Should not raise
        templates_mod.CustomTemplate._validate(data, Path("test.json"))

    def test_loads_from_file(self):
        """Template can be loaded from valid JSON file."""
        templates_mod = load_combat_module("templates")

        data = {
            "version": 1,
            "id": "file_test",
            "type": "custom",
            "steps": [{"action": "normal_attack"}],
            "on_finish": "finish",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            temp_path = Path(f.name)

        try:
            template = templates_mod.CustomTemplate.load(temp_path, skip_traversal_check=True)
            self.assertEqual(template.id, "file_test")
            self.assertEqual(len(template.steps), 1)
        finally:
            temp_path.unlink()


class TestCustomPolicyExecution(unittest.TestCase):
    """Test custom template policy decision making."""

    def setUp(self):
        """Load required modules."""
        self.models = load_combat_module("models")
        self.policies = load_combat_module("policies")
        self.templates = load_combat_module("templates")

    def test_custom_policy_follows_step_order(self):
        """Custom policy executes steps in template order."""
        # Create simple template
        template_data = {
            "version": 1,
            "id": "test_order",
            "type": "custom",
            "roles": {"main": 1},
            "steps": [
                {"action": "switch", "role": "main", "required": True},
                {"action": "skill", "when": "skill_ready"},
                {"action": "normal_attack"},
            ],
            "on_finish": "finish",
        }
        self.templates.CustomTemplate._validate(template_data, Path("test.json"))
        template = self.templates.CustomTemplate(template_data)

        policy = self.policies.CustomCombatPolicy(template, {})
        session = self.models.CombatSession(
            session_id="test",
            settings={},
            started_at=time.monotonic(),
            phase=self.models.Phase.ACTIVE,
        )

        # First decision should be switch
        snapshot = self.models.CombatSnapshot(
            frame_id=1,
            captured_at=time.monotonic(),
            valid=True,
            combat=True,
        )
        intent = policy.decide(session, snapshot)
        self.assertEqual(intent.action, "switch")
        self.assertEqual(intent.slot, 1)

    def test_custom_policy_skips_unavailable_skills(self):
        """Custom policy skips skill when skill_ready=False."""
        template_data = {
            "version": 1,
            "id": "test_skip",
            "type": "custom",
            "steps": [
                {"action": "skill", "when": "skill_ready"},
                {"action": "normal_attack"},
            ],
            "on_finish": "finish",
        }
        template = self.templates.CustomTemplate(template_data)
        policy = self.policies.CustomCombatPolicy(template, {})

        session = self.models.CombatSession(
            session_id="test",
            settings={},
            started_at=time.monotonic(),
            phase=self.models.Phase.ACTIVE,
        )

        # Skill not ready, should skip to attack
        snapshot = self.models.CombatSnapshot(
            frame_id=1,
            captured_at=time.monotonic(),
            valid=True,
            combat=True,
            skill_ready=False,
        )
        intent = policy.decide(session, snapshot)
        self.assertEqual(intent.action, "normal_attack")

    def test_custom_policy_waits_for_pending_confirmation(self):
        """Custom policy returns None when action is pending."""
        template_data = {
            "version": 1,
            "id": "test_pending",
            "type": "custom",
            "steps": [{"action": "skill"}],
            "on_finish": "finish",
        }
        template = self.templates.CustomTemplate(template_data)
        policy = self.policies.CustomCombatPolicy(template, {})

        session = self.models.CombatSession(
            session_id="test",
            settings={},
            started_at=time.monotonic(),
            phase=self.models.Phase.ACTIVE,
        )

        # Set pending action
        now = time.monotonic()
        session.pending = self.models.PendingAction(
            intent=self.models.ActionIntent(action="skill"),
            sent_at=now,
            deadline=now + 2.0,
        )

        snapshot = self.models.CombatSnapshot(
            frame_id=1,
            captured_at=time.monotonic(),
            valid=True,
            combat=True,
        )

        intent = policy.decide(session, snapshot)
        self.assertIsNone(intent)

    def test_custom_policy_respects_max_cycles(self):
        """Custom policy stops after max_cycles reached."""
        template_data = {
            "version": 1,
            "id": "test_cycles",
            "type": "custom",
            "steps": [{"action": "normal_attack"}],
            "on_finish": "reobserve",
            "max_cycles": 3,
        }
        template = self.templates.CustomTemplate(template_data)
        policy = self.policies.CustomCombatPolicy(template, {})

        session = self.models.CombatSession(
            session_id="test",
            settings={},
            started_at=time.monotonic(),
            phase=self.models.Phase.ACTIVE,
            cycles_completed=3,  # Already at limit
        )

        snapshot = self.models.CombatSnapshot(
            frame_id=1,
            captured_at=time.monotonic(),
            valid=True,
            combat=True,
        )

        # Force step index past end to trigger completion handler
        policy.current_step = len(template.steps)

        intent = policy.decide(session, snapshot)
        self.assertIsNone(intent)
        self.assertTrue(session.terminal)
        self.assertEqual(session.exit_reason, "cycle_limit_reached")

    def test_custom_policy_finishes_after_single_cycle(self):
        """Custom policy with on_finish=finish ends after one cycle."""
        template_data = {
            "version": 1,
            "id": "test_finish",
            "type": "custom",
            "steps": [{"action": "normal_attack"}],
            "on_finish": "finish",
        }
        template = self.templates.CustomTemplate(template_data)
        policy = self.policies.CustomCombatPolicy(template, {})

        session = self.models.CombatSession(
            session_id="test",
            settings={},
            started_at=time.monotonic(),
            phase=self.models.Phase.ACTIVE,
        )

        snapshot = self.models.CombatSnapshot(
            frame_id=1,
            captured_at=time.monotonic(),
            valid=True,
            combat=True,
        )

        # Complete first step
        policy.on_action_result(
            self.models.ActionIntent(action="normal_attack"), succeeded=True
        )

        # Now at end of template
        intent = policy.decide(session, snapshot)
        self.assertIsNone(intent)
        self.assertTrue(session.terminal)
        self.assertEqual(session.exit_reason, "template_completed")


if __name__ == "__main__":
    unittest.main()
