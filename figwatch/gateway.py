"""Detect an Anthropic-compatible gateway configured for the Claude Code CLI.

Tools such as cc-switch (https://github.com/farion1231/cc-switch) let a user
point the ``claude`` CLI at a company or self-hosted gateway by writing an
``env`` block into ``~/.claude/settings.json``::

    {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example.com",
             "ANTHROPIC_AUTH_TOKEN": "...",
             "ANTHROPIC_MODEL": "company-model-id"}}

The ``claude`` CLI applies that block to itself on every run, so FigWatch —
which shells out to ``claude`` — should defer to whatever is active there
instead of assuming a personal ``claude login``.

Two things follow when a gateway is active:

* Auth is satisfied by the gateway token, so the OAuth-only ``claude auth
  status`` check (which reports ``loggedIn: false`` under token auth) must not
  gate the app.
* The model is dictated by the gateway's own ``ANTHROPIC_MODEL``. Passing a
  public Anthropic model id / alias (``sonnet``/``opus``/``haiku`` →
  ``claude-sonnet-4-6`` …) makes the gateway reject the call with
  ``400 model not found``, so we must not override it.

This module reads only the settings file; it never contacts the gateway. The
``env`` argument on each function exists so tests can inject a block without
touching the real filesystem.
"""

import json
import os
from urllib.parse import urlparse

CLAUDE_SETTINGS_PATH = os.path.expanduser(os.path.join("~", ".claude", "settings.json"))

# Keys cc-switch (and manual setups) place in the settings `env` block.
GATEWAY_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
)


def read_claude_env(path=CLAUDE_SETTINGS_PATH):
    """Return the ``env`` block from the Claude Code settings file.

    Returns an empty dict if the file is missing, unreadable, or malformed.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    env = data.get("env")
    return env if isinstance(env, dict) else {}


def gateway_info(env=None):
    """Describe an active gateway, or return ``None`` for personal / OAuth mode.

    A gateway is considered active when the settings define a custom
    ``ANTHROPIC_BASE_URL`` together with a bearer credential
    (``ANTHROPIC_AUTH_TOKEN`` or ``ANTHROPIC_API_KEY``). ``env`` may be passed
    in for testing; otherwise it is read from the settings file.
    """
    if env is None:
        env = read_claude_env()
    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "").strip()
    if not base_url or not token:
        return None
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
    """
    if env is None:
        env = read_claude_env()
    if gateway_info(env) is None:
        return {}
    return {k: env[k] for k in GATEWAY_ENV_KEYS if env.get(k)}
