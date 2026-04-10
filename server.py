#!/usr/bin/env python3
"""FigWatch server — headless watcher for Docker / server deployment.

Configuration via environment variables:

  FIGMA_PAT          Figma Personal Access Token (required)
  FIGWATCH_FILES     Comma-separated Figma file URLs or file keys (required)
  FIGWATCH_LOCALE    Locale for tone audits: uk, de, fr, nl, benelux (default: uk)
  FIGWATCH_MODEL     Claude model: sonnet, opus, haiku (default: sonnet)
  FIGWATCH_INTERVAL  Poll interval in seconds (default: 30)
  CLAUDE_PATH        Path to the claude CLI binary (default: claude)
  ANTHROPIC_API_KEY  Passed through to the claude CLI for authentication
"""

import os
import re
import signal
import sys
import threading

# Allow running from the repo root without installing the package
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from figwatch.watcher import FigmaWatcher, load_trigger_config, process_work_item


def _parse_file_keys(files_str):
    """Parse FIGWATCH_FILES — comma-separated Figma URLs or bare file keys."""
    keys = []
    for item in files_str.split(','):
        item = item.strip()
        if not item:
            continue
        m = re.search(r'figma\.com/(?:design|file|board)/([a-zA-Z0-9]+)', item)
        if m:
            keys.append(m.group(1))
        elif re.match(r'^[a-zA-Z0-9]{10,}$', item):
            keys.append(item)
        else:
            print(f'⚠️  Skipping unrecognised entry: {item!r}', flush=True)
    return keys


def main():
    pat = os.environ.get('FIGMA_PAT', '').strip()
    files_str = os.environ.get('FIGWATCH_FILES', '').strip()

    if not pat:
        print('❌ FIGMA_PAT is required.', file=sys.stderr)
        sys.exit(1)
    if not files_str:
        print('❌ FIGWATCH_FILES is required (comma-separated Figma URLs or file keys).', file=sys.stderr)
        sys.exit(1)

    file_keys = _parse_file_keys(files_str)
    if not file_keys:
        print('❌ No valid Figma file keys found in FIGWATCH_FILES.', file=sys.stderr)
        sys.exit(1)

    locale = os.environ.get('FIGWATCH_LOCALE', 'uk')
    model = os.environ.get('FIGWATCH_MODEL', 'sonnet')
    interval = int(os.environ.get('FIGWATCH_INTERVAL', '30'))
    claude_path = os.environ.get('CLAUDE_PATH', 'claude')

    trigger_config = load_trigger_config()
    triggers_str = ', '.join(t.get('trigger', '') for t in trigger_config)

    print(f'🔍 FigWatch server starting', flush=True)
    print(f'   Files:    {len(file_keys)} ({", ".join(file_keys)})', flush=True)
    print(f'   Triggers: {triggers_str}', flush=True)
    print(f'   Locale:   {locale}  Model: {model}  Interval: {interval}s', flush=True)

    watchers = []
    n = len(file_keys)

    for i, key in enumerate(file_keys):
        # Stagger poll starts so multiple files don't all hit the API at once
        delay = (interval / n) * i if n > 1 else 0

        def make_dispatch():
            def dispatch(item):
                threading.Thread(target=process_work_item, args=(item,), daemon=True).start()
            return dispatch

        w = FigmaWatcher(
            key, pat,
            locale=locale,
            model=model,
            interval=interval,
            claude_path=claude_path,
            log=lambda msg: print(msg, flush=True),
            trigger_config=trigger_config,
            dispatch=make_dispatch(),
            initial_delay=int(delay),
        )
        w.start()
        watchers.append(w)

    def _shutdown(sig, frame):
        print('\n⏹  Shutting down…', flush=True)
        for w in watchers:
            w.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Block main thread until all watcher threads exit
    for w in watchers:
        if w._thread:
            w._thread.join()


if __name__ == '__main__':
    main()
