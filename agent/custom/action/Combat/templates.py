"""Combat template system: validation, loading, and interpretation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Any

from .resources import find_template, resolve_combat_templates_dir


class TemplateError(Exception):
    """Template validation or loading error."""

    pass


class CustomTemplate:
    """Validated and parsed custom combat template."""

    VERSION = 1
    MAX_STEPS = 50
    MAX_BRANCH_DEPTH = 3
    MAX_FILE_SIZE = 102400  # 100KB

    ALLOWED_ACTIONS = {
        "switch",
        "normal_attack",
        "charged_attack",
        "skill",
        "ultimate",
        "dodge",
        "wait",
    }

    ALLOWED_CONDITIONS = {
        "always",
        "skill_ready",
        "ultimate_ready",
        "in_combat",
        "slot_alive",
    }

    ALLOWED_ON_FINISH = {"finish", "reobserve"}

    def __init__(self, data: dict):
        """Create template from validated data."""
        self.id = data["id"]
        self.type = data["type"]
        self.roles = data.get("roles", {})
        self.steps = data["steps"]
        self.on_finish = data.get("on_finish", "finish")
        self.max_cycles = data.get("max_cycles")

    @classmethod
    def load(cls, path: Path, skip_traversal_check: bool = False) -> CustomTemplate:
        """Load and validate custom template from file.

        Args:
            path: Path to template file
            skip_traversal_check: Skip path traversal check (for tests/builtin)
        """
        if not path.exists():
            raise TemplateError(f"Template file not found: {path}")

        if path.stat().st_size > cls.MAX_FILE_SIZE:
            raise TemplateError(f"Template file too large: {path.stat().st_size} bytes")

        # Path traversal check (skipped for tests and builtin templates)
        if not skip_traversal_check:
            try:
                path.resolve().relative_to(Path.cwd())
            except ValueError:
                raise TemplateError(f"Path traversal not allowed: {path}")

        # Load JSON
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise TemplateError(f"Invalid JSON: {e}")

        # Validate
        cls._validate(data, path)

        return cls(data)

    @classmethod
    def _validate(cls, data: dict, path: Path) -> None:
        """Validate template structure and constraints."""
        # Required fields
        if "version" not in data:
            raise TemplateError("Missing required field: version")
        if data["version"] != cls.VERSION:
            raise TemplateError(f"Unsupported version: {data['version']}")

        if "id" not in data or not isinstance(data["id"], str):
            raise TemplateError("Missing or invalid field: id")
        if "type" not in data or data["type"] != "custom":
            raise TemplateError("Missing or invalid field: type")
        if "steps" not in data or not isinstance(data["steps"], list):
            raise TemplateError("Missing or invalid field: steps")

        # Steps limit
        if len(data["steps"]) > cls.MAX_STEPS:
            raise TemplateError(f"Too many steps: {len(data['steps'])} > {cls.MAX_STEPS}")

        # Validate each step
        for i, step in enumerate(data["steps"]):
            cls._validate_step(step, i, depth=0)

        # Validate roles if present
        if "roles" in data:
            cls._validate_roles(data["roles"])

        # Validate on_finish
        on_finish = data.get("on_finish", "finish")
        if on_finish not in cls.ALLOWED_ON_FINISH:
            raise TemplateError(f"Invalid on_finish: {on_finish}")

        # If reobserve, require max_cycles
        if on_finish == "reobserve":
            max_cycles = data.get("max_cycles")
            if not isinstance(max_cycles, int) or max_cycles < 1:
                raise TemplateError("on_finish=reobserve requires positive integer max_cycles")
            if max_cycles > 1000:
                raise TemplateError(f"max_cycles too large: {max_cycles}")

    @classmethod
    def _validate_step(cls, step: dict, index: int, depth: int) -> None:
        """Validate a single step."""
        if not isinstance(step, dict):
            raise TemplateError(f"Step {index}: must be an object")

        if "action" not in step:
            raise TemplateError(f"Step {index}: missing action")

        action = step["action"]
        if action not in cls.ALLOWED_ACTIONS:
            raise TemplateError(f"Step {index}: unknown action '{action}'")

        # Validate action-specific fields
        if action == "switch":
            role = step.get("role")
            if not role or not isinstance(role, str):
                raise TemplateError(f"Step {index}: switch requires 'role' field")

        if action == "charged_attack":
            duration = step.get("duration_ms")
            if duration is not None:
                if not isinstance(duration, int) or duration < 0 or duration > 5000:
                    raise TemplateError(
                        f"Step {index}: duration_ms must be 0-5000"
                    )

        if action == "wait":
            timeout = step.get("timeout_ms")
            if not isinstance(timeout, int) or timeout < 0 or timeout > 30000:
                raise TemplateError(f"Step {index}: wait timeout must be 0-30000ms")

        # Validate condition
        when = step.get("when")
        if when is not None:
            if when not in cls.ALLOWED_CONDITIONS:
                raise TemplateError(f"Step {index}: unknown condition '{when}'")

        # Validate required flag
        required = step.get("required", False)
        if not isinstance(required, bool):
            raise TemplateError(f"Step {index}: required must be boolean")

        # Validate max_ms
        max_ms = step.get("max_ms")
        if max_ms is not None:
            if not isinstance(max_ms, int) or max_ms < 0 or max_ms > 30000:
                raise TemplateError(f"Step {index}: max_ms must be 0-30000")

    @classmethod
    def _validate_roles(cls, roles: dict) -> None:
        """Validate role assignments."""
        if not isinstance(roles, dict):
            raise TemplateError("roles must be an object")

        for role_name, slot in roles.items():
            if not isinstance(role_name, str):
                raise TemplateError(f"Role name must be string: {role_name}")
            if not isinstance(slot, int) or slot not in [1, 2, 3, 4]:
                raise TemplateError(
                    f"Role '{role_name}' slot must be 1-4, got: {slot}"
                )


def load_template(template_name: str, config_dir: Optional[Path] = None) -> CustomTemplate:
    """Load a template by name from built-in or user directory.

    Args:
        template_name: Template file name (e.g., "custom_example.json")
        config_dir: Optional user config directory (deprecated, uses find_template)

    Returns:
        Validated CustomTemplate

    Raises:
        TemplateError: If template not found or validation fails
    """
    # Use resource resolver to find template
    template_path = find_template(template_name)

    if template_path is None:
        raise TemplateError(f"Template not found: {template_name}")

    # Load with path traversal check disabled for builtin templates
    builtin_dir = resolve_combat_templates_dir()
    is_builtin = False
    try:
        template_path.resolve().relative_to(builtin_dir)
        is_builtin = True
    except ValueError:
        pass

    return CustomTemplate.load(template_path, skip_traversal_check=is_builtin)
