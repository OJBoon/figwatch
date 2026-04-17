"""FigWatch domain types and trigger configuration."""

import json
import os
from collections import namedtuple
from pathlib import Path

# ── Status constants ──────────────────────────────────────────────────

STATUS_LIVE = 'live'
STATUS_DETECTED = 'detected'
STATUS_PROCESSING = 'processing'
STATUS_REPLIED = 'replied'
STATUS_ERROR = 'error'

# ── WorkItem ──────────────────────────────────────────────────────────

WorkItem = namedtuple('WorkItem', [
    'file_key', 'comment_id', 'reply_to_id', 'node_id',
    'trigger', 'skill_path', 'user_handle', 'extra',
    'locale', 'model', 'reply_lang', 'pat', 'claude_path', 'on_status',
])

# ── Trigger config ────────────────────────────────────────────────────

DEFAULT_TRIGGERS = [
    {"trigger": "@tone", "skill": "builtin:tone"},
    {"trigger": "@ux", "skill": "builtin:ux"},
]


def _discover_custom_triggers():
    """Scan ./custom-skills/ for .md files and return trigger entries.

    Supports flat files (a11y.md → @a11y) and subdirectories (a11y/skill.md → @a11y).
    """
    custom_dir = Path(os.getcwd()) / 'custom-skills'
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


def load_trigger_config():
    """Load trigger config from config file or built-in defaults, plus any custom skills.

    Priority:
      1. ~/.figwatch/config.json  (written by the macOS app)
      2. Built-in defaults        (@tone, @ux)

    In both cases, any .md files found in ./custom-skills/ are appended automatically.
    """
    custom = _discover_custom_triggers()

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


def match_trigger(message, trigger_config):
    """Match a comment message against configured triggers.

    Returns {"trigger": str, "skill": str, "extra": str} or None.
    """
    lower = message.lower().strip()
    for entry in trigger_config:
        trigger = entry.get('trigger', '')
        if trigger and trigger.lower() in lower:
            idx = lower.index(trigger.lower())
            extra = message[idx + len(trigger):].strip()
            return {'trigger': trigger, 'skill': entry.get('skill', ''), 'extra': extra}
    return None
