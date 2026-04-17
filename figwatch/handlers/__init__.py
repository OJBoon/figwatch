"""Shared handler utilities."""

import os
import re
import subprocess


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
        'PATH': f"/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '/usr/bin:/bin')}",
    }


def parse_claude_output(result, fallback_msg='Unable to generate evaluation.'):
    """Parse a Claude subprocess result into a reply string."""
    stdout = result.stdout.decode('utf-8', errors='replace').strip()
    if stdout:
        return strip_markdown(stdout)
    err = result.stderr.decode('utf-8', errors='replace').strip()
    if len(err) > 400:
        err = err[:400] + '\u2026'
    return fallback_msg + '\n\n' + (f'Error: {err}' if err else f'claude exited with code {result.returncode}')
