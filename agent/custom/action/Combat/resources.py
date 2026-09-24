"""Resource path resolution for combat module."""

from pathlib import Path
from typing import Optional


def resolve_resource_base() -> Path:
    """Resolve base resource directory (dev or installed).

    Returns:
        Path to assets/resource or resource directory
    """
    # Try development structure first
    candidates = [
        Path.cwd() / "assets" / "resource",
        Path.cwd() / "resource",
    ]

    # Also try relative to this file (for installed structure)
    try:
        file_parent = Path(__file__).resolve().parent
        for _ in range(6):  # Search up to 6 levels
            candidates.append(file_parent / "assets" / "resource")
            candidates.append(file_parent / "resource")
            if file_parent.parent == file_parent:
                break
            file_parent = file_parent.parent
    except Exception:
        pass

    for candidate in candidates:
        if candidate.exists() and (candidate / "base").exists():
            return candidate

    # Fallback to cwd-based path
    return Path.cwd() / "assets" / "resource"


def resolve_combat_templates_dir() -> Path:
    """Resolve combat templates directory.

    Returns:
        Path to base/combat/templates directory
    """
    base = resolve_resource_base()
    return base / "base" / "combat" / "templates"


def resolve_config_templates_dir() -> Path:
    """Resolve user config templates directory.

    Returns:
        Path to config/combat/templates directory
    """
    return Path.cwd() / "config" / "combat" / "templates"


def resolve_sound_samples_dir() -> Path:
    """Resolve sound samples directory.

    Returns:
        Path to base/sounds directory
    """
    base = resolve_resource_base()
    return base / "base" / "sounds"


def find_template(name: str) -> Optional[Path]:
    """Find template by name in builtin or config directory.

    Args:
        name: Template filename (e.g. "basic.json" or "my_template.json")

    Returns:
        Path to template file if found, None otherwise
    """
    # Check user config first
    config_path = resolve_config_templates_dir() / name
    if config_path.exists():
        return config_path

    # Check builtin templates
    builtin_path = resolve_combat_templates_dir() / name
    if builtin_path.exists():
        return builtin_path

    return None


def find_sound_sample(name: str) -> Optional[Path]:
    """Find sound sample by name.

    Args:
        name: Sample filename (e.g. "dodge.wav")

    Returns:
        Path to sound file if found, None otherwise
    """
    samples_dir = resolve_sound_samples_dir()
    sample_path = samples_dir / name

    if sample_path.exists():
        return sample_path

    return None
