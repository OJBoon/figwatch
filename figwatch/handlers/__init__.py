"""Shared handler utilities."""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# ── Status constants (shared by watcher.py and the macOS app) ───────

STATUS_LIVE = 'live'
STATUS_DETECTED = 'detected'
STATUS_PROCESSING = 'processing'
STATUS_REPLIED = 'replied'
STATUS_ERROR = 'error'


def strip_markdown(text):
    """Remove markdown formatting for Figma plain-text comments."""
    text = re.sub(r'\*\*', '', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*\u2022]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def subprocess_env():
    """Augmented PATH for subprocess calls (covers Homebrew and common install locations)."""
    return {
        **os.environ,
        "PATH": f"/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '/usr/bin:/bin')}",
    }


def urllib_quote(s):
    """URL-encode a string for Figma API paths."""
    return urllib.parse.quote(s, safe='')


def figma_get_retry(path, pat, retries=1):
    """GET a Figma API endpoint with retry on 429. Returns parsed JSON or None."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                f'https://api.figma.com/v1{path}',
                headers={'X-Figma-Token': pat}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                wait = 0
                try:
                    wait = int(e.headers.get('Retry-After', '0') or 0)
                except Exception:
                    wait = 0
                time.sleep(max(wait, 2))
                continue
            return None
        except Exception:
            return None
    return None


def parse_claude_output(result, fallback_msg='Unable to generate evaluation.'):
    """Parse a Claude subprocess result into a reply string."""
    stdout = result.stdout.decode('utf-8', errors='replace').strip()
    if stdout:
        return strip_markdown(stdout)
    err = result.stderr.decode('utf-8', errors='replace').strip()
    if len(err) > 400:
        err = err[:400] + '\u2026'
    return fallback_msg + '\n\n' + (f'Error: {err}' if err else f'claude exited with code {result.returncode}')
