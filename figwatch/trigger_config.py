"""Trigger configuration loading — application-layer I/O."""

import os
from pathlib import Path

from figwatch.config_store import config_path, load_json

DEFAULT_TRIGGERS = [
    {"trigger": "@tone", "skill": "builtin:tone"},
    {"trigger": "@ux", "skill": "builtin:ux"},
]

_SKILL_FILENAMES = ('skill.md', 'SKILL.md')


def skill_file_in(skill_dir):
    """Path to a skill directory's skill.md / SKILL.md, or None."""
    for fname in _SKILL_FILENAMES:
        path = Path(skill_dir) / fname
        if path.exists():
            return path
    return None


def skill_label(skill_ref):
    """Human name for a skill reference.

    builtin:ux → "ux". A custom skill at <dir>/<name>/skill.md is named after its
    folder (the file name alone would just say "skill"); a loose a11y.md is "a11y".
    """
    if skill_ref.startswith('builtin:'):
        return skill_ref.split('builtin:', 1)[1]
    fname = os.path.basename(skill_ref)
    if fname.lower() in ('skill.md', 'skill.markdown'):
        parent = os.path.basename(os.path.dirname(skill_ref))
        if parent:
            return parent
    return os.path.splitext(fname)[0]


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
        skill_path = skill_file_in(skill_dir)
        if name not in seen and skill_path:
            seen.add(name)
            triggers.append({'trigger': f'@{name}', 'skill': str(skill_path.resolve())})

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

    config = load_json(config_path())
    triggers = config.get('triggers') if isinstance(config, dict) else None
    if triggers and isinstance(triggers, list):
        existing = {t.get('trigger') for t in triggers}
        triggers.extend(t for t in custom if t['trigger'] not in existing)
        return triggers

    return list(DEFAULT_TRIGGERS) + custom
