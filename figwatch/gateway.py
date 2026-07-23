"""Detect an Anthropic-compatible gateway configured for the Claude Code CLI.

Tools such as cc-switch (https://github.com/farion1231/cc-switch) let a user
point the ``claude`` CLI at a company or self-hosted gateway by writing an
``env`` block into ``~/.claude/settings.json``::

    {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example.com",
             "ANTHROPIC_AUTH_TOKEN": "...",
             "ANTHROPIC_MODEL": "company-model-id"}}

FigWatch reuses that configuration in two ways:

* On macOS, when a gateway is active FigWatch talks to it through the Anthropic
  Messages API **directly** (no ``claude`` CLI required) — see
  :func:`gateway_api_config` and ``figwatch.providers.ai.make_provider``.
* The legacy CLI path (kept for personal ``claude login`` users) defers to the
  same gateway credentials via :func:`gateway_subprocess_env`.

Detection sources and precedence
---------------------------------
When no ``env`` is injected, the effective gateway env is resolved by walking
these sources in order and returning the **first** one that defines a complete
gateway (a base URL *and* a bearer credential):

1. ``~/.claude/settings.json``            (cc-switch's primary target; PRIMARY)
2. ``~/.claude/settings.local.json``      (per-machine Claude Code overrides)
3. ``~/.config/claude/settings.json``     (XDG-style Claude Code config)
4. ``~/.claude.json``                     (older single-file Claude Code config)
5. ``os.environ``                         (weak/bonus — a Finder-launched .app
                                           does not inherit shell env, and shell
                                           env can be stale)
6. ``~/.figwatch/gateway.json``           (Option-B manual fallback, pasted into
                                           FigWatch itself)

For the JSON settings files (1)-(4) both an ``env`` block **and** top-level
``ANTHROPIC_*`` keys are honoured, because ``~/.claude.json`` may store the keys
at the top level rather than nested under ``env``.

Every source is read **gracefully**: a missing / unreadable / malformed /
empty / non-object file, or an ``env`` block that is not a dict, is skipped and
the walk continues to the next source. No source can raise out of detection.

A gateway requires **both** a base URL and a token — a base URL without a token
(or vice-versa) is *not* a gateway and is skipped.

This module reads only local settings; it never contacts the gateway. The
``env`` argument on each public function exists so tests can inject a block
without touching the real filesystem; when ``env`` is passed it is used
verbatim and no source walk happens.
"""

import contextlib
import json
import os
from urllib.parse import urlparse

# ── Detection source paths (precedence order) ─────────────────────────────
CLAUDE_SETTINGS_PATH = os.path.expanduser(os.path.join("~", ".claude", "settings.json"))
CLAUDE_SETTINGS_LOCAL_PATH = os.path.expanduser(
    os.path.join("~", ".claude", "settings.local.json")
)
CLAUDE_CONFIG_XDG_PATH = os.path.expanduser(
    os.path.join("~", ".config", "claude", "settings.json")
)
CLAUDE_JSON_PATH = os.path.expanduser(os.path.join("~", ".claude.json"))

# Option B fallback: a gateway the user pastes into FigWatch itself, stored
# outside cc-switch. Read only when no higher-precedence source defines one.
# Shape: {"base_url": ..., "auth_token": ... (or "api_key"), "model": ...}
MANUAL_GATEWAY_PATH = os.path.expanduser(os.path.join("~", ".figwatch", "gateway.json"))

# cc-switch's own install locations. Their *presence* means "cc-switch is
# installed" (used only for macOS not-found messaging). We never parse
# cc-switch.db — its schema is undocumented and fragile.
CC_SWITCH_DIRS = (
    os.path.expanduser(os.path.join("~", ".cc-switch")),
    os.path.expanduser(
        os.path.join("~", "Library", "Application Support", "com.ccswitch.desktop")
    ),
)

# Keys cc-switch (and manual setups) place in the settings `env` block.
GATEWAY_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
)

# Maps ~/.figwatch/gateway.json keys → the ANTHROPIC_* names the settings
# sources yield, so every gateway source funnels through one detection path.
_MANUAL_KEY_MAP = {
    "base_url": "ANTHROPIC_BASE_URL",
    "auth_token": "ANTHROPIC_AUTH_TOKEN",
    "api_key": "ANTHROPIC_API_KEY",
    "model": "ANTHROPIC_MODEL",
    "small_fast_model": "ANTHROPIC_SMALL_FAST_MODEL",
}


def _load_json_dict(path):
    """Load a JSON file and return it if it is an object, else ``None``.

    Graceful: a missing / unreadable / malformed / non-object file yields
    ``None`` and never raises. This is the single place the module's "never
    raise, hand back a dict or nothing" file-reading contract lives; every
    JSON reader below builds on it.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def read_claude_env(path=CLAUDE_SETTINGS_PATH):
    """Return the ``env`` block from a Claude Code settings file.

    Returns an empty dict if the file is missing, unreadable, malformed, or has
    no dict ``env`` block. This reads *only* the nested ``env`` block (it does
    not consider top-level ``ANTHROPIC_*`` keys); it backs the CLI-subprocess
    injection path. The multi-source walk uses :func:`_json_file_env`, which is
    more permissive.
    """
    data = _load_json_dict(path)
    env = data.get("env") if data else None
    return env if isinstance(env, dict) else {}


def _json_file_env(path):
    """Read ``ANTHROPIC_*`` gateway keys from a Claude settings JSON file.

    Merges any top-level ``ANTHROPIC_*`` keys with the keys in a nested ``env``
    block (the ``env`` block wins on conflict, as that is cc-switch's canonical
    location). Supporting top-level keys lets ``~/.claude.json`` — which may not
    nest under ``env`` — be detected.

    Graceful: a missing / unreadable / malformed / non-object file yields an
    empty dict. Only non-blank string values are kept.
    """
    data = _load_json_dict(path)
    if data is None:
        return {}
    out = {}
    # Top-level ANTHROPIC_* keys first (e.g. ~/.claude.json layout)...
    for k in GATEWAY_ENV_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v
    # ...then the nested env block overrides them (cc-switch canonical layout).
    env = data.get("env")
    if isinstance(env, dict):
        for k in GATEWAY_ENV_KEYS:
            v = env.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v
    return out


def _os_environ_env():
    """Return the recognised ``ANTHROPIC_*`` gateway keys set in ``os.environ``.

    Weak/bonus source: a Finder-launched ``.app`` does not inherit the shell's
    environment, and any inherited values may be stale. Only non-blank values
    for keys in :data:`GATEWAY_ENV_KEYS` are returned.
    """
    return {k: os.environ[k] for k in GATEWAY_ENV_KEYS if (os.environ.get(k) or "").strip()}


def read_manual_gateway(path=MANUAL_GATEWAY_PATH):
    """Return the raw Option-B manual gateway dict, or ``None``.

    Reads ``~/.figwatch/gateway.json`` and returns its parsed contents using the
    file's own key names (``base_url`` / ``auth_token`` / ``api_key`` /
    ``model`` / ``small_fast_model``) so a UI can display or pre-fill the stored
    values. Returns ``None`` if the file is missing, unreadable, malformed, or
    not a JSON object. Graceful — never raises.

    (For detection, the manual source is consumed via :func:`_manual_gateway_env`
    which maps these keys onto ``ANTHROPIC_*`` names.)
    """
    return _load_json_dict(path)


def write_manual_gateway(base_url, *, auth_token=None, api_key=None, model=None,
                         small_fast_model=None, path=MANUAL_GATEWAY_PATH):
    """Write the Option-B manual gateway file and return its path.

    Persists ``~/.figwatch/gateway.json`` in the documented shape::

        {"base_url": ..., "auth_token": ... (or "api_key"), "model": ...}

    Exactly one credential must be supplied; ``auth_token`` is preferred over
    ``api_key`` if both are given. ``base_url`` and a credential are required
    (``ValueError`` otherwise). ``model`` / ``small_fast_model`` are optional.
    Only non-empty fields are written. The parent directory is created ``0700``
    and the file is written ``0600`` because it holds a bearer credential.
    """
    base_url = (base_url or "").strip()
    auth_token = (auth_token or "").strip()
    api_key = (api_key or "").strip()
    if not base_url:
        raise ValueError("write_manual_gateway: base_url is required")
    if not (auth_token or api_key):
        raise ValueError("write_manual_gateway: an auth_token or api_key is required")

    payload = {"base_url": base_url}
    if auth_token:
        payload["auth_token"] = auth_token
    else:
        payload["api_key"] = api_key
    if (model or "").strip():
        payload["model"] = model.strip()
    if (small_fast_model or "").strip():
        payload["small_fast_model"] = small_fast_model.strip()

    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    # Write then chmod so the credential is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    finally:
        # An existing file keeps its old mode under O_TRUNC, so re-assert 0600.
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return path


def _manual_gateway_env(path=MANUAL_GATEWAY_PATH):
    """Return the Option-B manual gateway as an ``ANTHROPIC_*`` env dict.

    Reads ``~/.figwatch/gateway.json`` (shape
    ``{"base_url": ..., "auth_token": ... (or "api_key"), "model": ...}``) and
    maps its keys onto the same ``ANTHROPIC_*`` names the settings sources
    yield, so downstream detection treats every source identically. Returns an
    empty dict if the file is missing, unreadable, malformed, or not an object.
    """
    data = read_manual_gateway(path)
    if not isinstance(data, dict):
        return {}
    out = {}
    for src, dst in _MANUAL_KEY_MAP.items():
        val = data.get(src)
        if isinstance(val, str) and val.strip():
            out[dst] = val
    return out


def _has_gateway(env):
    """True if ``env`` defines a gateway (custom base URL + a bearer credential)."""
    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "").strip()
    return bool(base_url and token)


# Detection sources in precedence order. Each entry is (name, loader); the
# first loader whose result satisfies _has_gateway() wins. The name labels each
# source for readability in this table — it is not surfaced anywhere at runtime.
_GATEWAY_SOURCES = (
    ("claude_settings", lambda: _json_file_env(CLAUDE_SETTINGS_PATH)),
    ("claude_settings_local", lambda: _json_file_env(CLAUDE_SETTINGS_LOCAL_PATH)),
    ("claude_config_xdg", lambda: _json_file_env(CLAUDE_CONFIG_XDG_PATH)),
    ("claude_json", lambda: _json_file_env(CLAUDE_JSON_PATH)),
    ("os_environ", _os_environ_env),
    ("manual", lambda: _manual_gateway_env(MANUAL_GATEWAY_PATH)),
)


def effective_gateway_env():
    """Resolve the effective gateway env by walking the detection sources.

    Returns the ``ANTHROPIC_*`` env dict from the first source (see module
    docstring for the ordered list) that defines a complete gateway, or an
    empty dict if none do. Every source is read gracefully — a failing source
    is skipped and the walk continues. Used by :func:`gateway_info` and
    :func:`gateway_api_config` when their ``env`` argument is ``None``.
    """
    for _name, loader in _GATEWAY_SOURCES:
        try:
            env = loader()
        except Exception:
            continue
        if isinstance(env, dict) and _has_gateway(env):
            return env
    return {}


def cc_switch_installed():
    """True if cc-switch appears installed on this machine.

    Detected purely by the *presence* of ``~/.cc-switch`` or the macOS app
    support directory ``~/Library/Application Support/com.ccswitch.desktop``.
    Intended for macOS not-found messaging ("cc-switch is installed but no
    gateway was detected"). Never reads cc-switch's SQLite store.
    """
    return any(os.path.isdir(d) for d in CC_SWITCH_DIRS)


def gateway_info(env=None):
    """Describe an active gateway, or return ``None`` for personal / OAuth mode.

    A gateway is active when the effective env defines a custom
    ``ANTHROPIC_BASE_URL`` together with a bearer credential
    (``ANTHROPIC_AUTH_TOKEN`` or ``ANTHROPIC_API_KEY``).

    ``env`` may be passed for testing and is used verbatim. When ``env`` is
    ``None`` the effective env is resolved via :func:`effective_gateway_env`
    (the source walk documented in the module docstring).

    Returns ``{"base_url", "host", "model", "small_fast_model"}`` or ``None``.
    """
    if env is None:
        env = effective_gateway_env()
    if not _has_gateway(env):
        return None
    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    try:
        host = urlparse(base_url).netloc or base_url
    except Exception:
        host = base_url
    return {
        "base_url": base_url,
        "host": host,
        "model": (env.get("ANTHROPIC_MODEL") or "").strip(),
        "small_fast_model": (env.get("ANTHROPIC_SMALL_FAST_MODEL") or "").strip(),
    }


def gateway_subprocess_env(env=None):
    """Return the gateway credentials to inject into a ``claude`` subprocess.

    Only the recognised ``ANTHROPIC_*`` keys that are actually set are returned,
    so the subprocess deterministically targets the gateway even if the
    launching process carries stale or partial ``ANTHROPIC_*`` variables.
    Returns an empty dict when no gateway is configured.

    When ``env`` is ``None`` this reads only ``~/.claude/settings.json`` (the
    canonical CLI source), since the ``claude`` CLI itself reads that file —
    this preserves the historical CLI-injection behaviour.
    """
    if env is None:
        env = read_claude_env()
    if gateway_info(env) is None:
        return {}
    return {k: env[k] for k in GATEWAY_ENV_KEYS if env.get(k)}


def gateway_api_config(env=None):
    """Return kwargs for an Anthropic SDK client against the active gateway.

    Returns ``None`` when no gateway is active. Otherwise a dict::

        {"base_url": <str>, "model": <str>, "auth_token": <str>}   # or
        {"base_url": <str>, "model": <str>, "api_key": <str>}

    with exactly one credential: ``ANTHROPIC_AUTH_TOKEN`` is preferred and maps
    to ``auth_token``; otherwise ``ANTHROPIC_API_KEY`` maps to ``api_key``.
    ``model`` is the gateway's ``ANTHROPIC_MODEL`` (empty string if unset — the
    caller decides the fallback model id).

    ``env`` may be passed for testing and is used verbatim. When ``env`` is
    ``None`` the effective env is resolved via :func:`effective_gateway_env`
    (the same precedence :func:`gateway_info` uses).
    """
    if env is None:
        env = effective_gateway_env()
    info = gateway_info(env)
    if info is None:
        return None
    cfg = {"base_url": info["base_url"], "model": info["model"]}
    auth_token = (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if auth_token:
        cfg["auth_token"] = auth_token
    else:
        cfg["api_key"] = (env.get("ANTHROPIC_API_KEY") or "").strip()
    return cfg
