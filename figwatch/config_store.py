"""~/.figwatch paths and the JSON read/write helpers every entry point shares.

Paths are resolved per call (not at import) so they follow $HOME.
"""

import json
import os


def config_dir():
    return os.path.join(os.path.expanduser('~'), '.figwatch')


def config_path():
    return os.path.join(config_dir(), 'config.json')


def load_json(path, default=None):
    """Parsed JSON from ``path``, or ``default`` if missing / unreadable / malformed."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, *, indent=2):
    """Write ``obj`` as JSON, creating the parent directory if needed."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=indent)


def load_config():
    """The first config dict holding a Figma token: ~/.figwatch, then the older
    ~/.figma-ds-cli location. {} if neither has one."""
    legacy = os.path.join(os.path.expanduser('~'), '.figma-ds-cli', 'config.json')
    for path in (config_path(), legacy):
        config = load_json(path)
        if isinstance(config, dict) and config.get('figmaPat'):
            return config
    return {}


def save_config(config):
    save_json(config_path(), config)
