"""Trigger configuration loading — application-layer I/O."""

import json
import os
from pathlib import Path


DEFAULT_TRIGGERS = [
    {"trigger": "@tone", "skill": "builtin:tone"},
    {"trigger": "@ux", "skill": "builtin:ux"},
]


def _discover_custom_triggers(skills_dir=None):
    """Scan custom-skills directory for .md files and return trigger entries.

    Supports flat files (a11y.md → @a11y) and subdirectories (a11y/skill.md → @a11y).

    Args:
        skills_dir: Path to the custom-skills directory. Defaults to
                    ``os.getcwd() / 'custom-skills'`` when *None*.
    """
    custom_dir = Path(skills_dir) if skills_dir else Path(os.getcwd()) / 'custom-skills'
    if not custom_dir.is_dir():
        return []

    triggers = []
    seen = set()

    for path in sorted(custom_dir.glob('*.md')):
        name = path.stem
        if name not in seen:
            seen.add(name)
            triggers.append({'trigger': f'@{name}', 'skill': str(path.resolve())})

    for skill_dir in sorted(p for p in custom_dir.iterdir() if p.is_dir()):
        name = skill_dir.name
        if name in seen:
            continue
        for fname in ['skill.md', 'SKILL.md']:
            skill_path = skill_dir / fname
            if skill_path.exists():
                seen.add(name)
                triggers.append({'trigger': f'@{name}', 'skill': str(skill_path.resolve())})
                break

    return triggers


def load_trigger_config(skills_dir=None):
    """Load trigger config from config file or built-in defaults, plus any custom skills.

    Priority:
      1. ~/.figwatch/config.json  (written by the macOS app)
      2. Built-in defaults        (@tone, @ux)

    In both cases, any .md files found in the custom-skills directory are appended
    automatically.

    Args:
        skills_dir: Path to the custom-skills directory. Passed through to
                    :func:`_discover_custom_triggers`.
    """
    custom = _discover_custom_triggers(skills_dir)

    try:
        config_path = os.path.join(os.path.expanduser('~'), '.figwatch', 'config.json')
        with open(config_path) as f:
            config = json.load(f)
        triggers = config.get('triggers')
        if triggers and isinstance(triggers, list):
            existing = {t.get('trigger') for t in triggers}
            for t in custom:
                if t['trigger'] not in existing:
                    triggers.append(t)
            return triggers
    except Exception:
        pass

    return list(DEFAULT_TRIGGERS) + custom
