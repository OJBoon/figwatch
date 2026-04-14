"""FigWatch comment watcher — polls Figma for trigger comments and dispatches work items."""

import json
import logging
import os
import threading

from figwatch.domain import WorkItem, load_trigger_config, match_trigger
from figwatch.processor import process_work_item
from figwatch.providers.figma import figma_get

logger = logging.getLogger(__name__)

_EM_DASH = '\u2014'
_OWN_REPLY_MARKERS = (_EM_DASH + ' Claude', _EM_DASH + ' Gemini')


# ── Processed comment tracking ────────────────────────────────────────

_processed_path_cache = None


def _processed_path():
    global _processed_path_cache
    if _processed_path_cache:
        return _processed_path_cache
    config_dir = os.path.join(os.path.expanduser('~'), '.figwatch')
    os.makedirs(config_dir, exist_ok=True)
    _processed_path_cache = os.path.join(config_dir, '.processed-comments.json')
    return _processed_path_cache


def load_processed():
    try:
        with open(_processed_path()) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_processed(ids):
    id_list = list(ids)
    if len(id_list) > 500:
        id_list = id_list[-500:]
        ids.clear()
        ids.update(id_list)
    with open(_processed_path(), 'w') as f:
        json.dump(id_list, f)


# ── Trigger detection (fast path — 1 API call) ────────────────────────

def detect_triggers(file_key, pat, processed_ids, trigger_config, *, log, on_status=None):
    """Fetch comments, find trigger matches, return list[WorkItem].

    Fast path: single API call, <1s. Does NOT call any AI provider.
    """
    data = figma_get(f'/files/{file_key}/comments', pat)
    comments = data.get('comments', []) if data else []

    comment_map = {c['id']: c for c in comments}

    replied_to = set()
    for c in comments:
        if c.get('parent_id') and any(m in (c.get('message') or '') for m in _OWN_REPLY_MARKERS):
            replied_to.add(c['parent_id'])
            processed_ids.add(c['parent_id'])

    candidates = []
    for c in comments:
        if c['id'] in processed_ids or c.get('resolved_at'):
            continue
        if not c.get('parent_id'):
            if (c.get('client_meta') or {}).get('node_id'):
                candidates.append(c)
        else:
            if not any(m in (c.get('message') or '') for m in _OWN_REPLY_MARKERS):
                candidates.append(c)

    initial_count = len(processed_ids)
    items = []
    for comment in candidates:
        if comment['id'] in processed_ids or comment['id'] in replied_to:
            continue
        if comment.get('parent_id') and comment['parent_id'] in replied_to:
            continue

        match = match_trigger(comment.get('message', ''), trigger_config)
        if not match:
            continue

        node_id = (comment.get('client_meta') or {}).get('node_id')
        reply_to_id = comment['id']
        if comment.get('parent_id'):
            parent = comment_map.get(comment['parent_id'])
            node_id = node_id or ((parent.get('client_meta') or {}).get('node_id') if parent else None)
            reply_to_id = comment['parent_id']

        if not node_id:
            processed_ids.add(comment['id'])
            continue

        processed_ids.add(comment['id'])
        user_handle = comment.get('user', {}).get('handle', 'unknown')

        item = WorkItem(
            file_key=file_key,
            comment_id=comment['id'],
            reply_to_id=reply_to_id,
            node_id=node_id,
            trigger=match['trigger'],
            skill_path=match['skill'],
            user_handle=user_handle,
            extra=match['extra'],
            locale=None,
            model=None,
            reply_lang=None,
            pat=pat,
            claude_path=None,
            on_status=on_status,
        )
        items.append(item)
        log(f'\U0001f4ac {match["trigger"]} comment by {user_handle} on node {node_id}')

    if len(processed_ids) > initial_count:
        save_processed(processed_ids)
    return items


# ── Watcher class ─────────────────────────────────────────────────────

class FigmaWatcher:
    def __init__(self, file_key, pat, *, locale='uk', model='sonnet', reply_lang='en',
                 interval=30, claude_path='claude', log=print,
                 trigger_config=None, dispatch=None, on_poll=None, on_status=None,
                 initial_delay=0,
                 on_reply=None):  # on_reply deprecated — use on_status
        self.file_key = file_key
        self.pat = pat
        self.locale = locale
        self.model = model
        self.reply_lang = reply_lang
        self.interval = interval
        self.claude_path = claude_path
        self.log = log
        self.trigger_config = trigger_config or load_trigger_config()
        self.dispatch = dispatch
        self.on_poll = on_poll
        self.on_status = on_status
        self.initial_delay = initial_delay
        self._on_reply = on_reply
        self._stop_event = threading.Event()
        self._thread = None
        self._processed = load_processed()

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def reload_trigger_config(self, trigger_config):
        """Hot-reload triggers without restart."""
        self.trigger_config = trigger_config

    def _run(self):
        triggers_str = ', '.join(t.get('trigger', '') for t in self.trigger_config)
        self.log(f'\U0001f50d Watching {self.file_key} ({triggers_str})')

        if self.initial_delay > 0:
            self._stop_event.wait(timeout=self.initial_delay)

        while not self._stop_event.is_set():
            try:
                items = detect_triggers(
                    self.file_key, self.pat, self._processed,
                    self.trigger_config, log=self.log, on_status=self.on_status,
                )
                for item in items:
                    item = item._replace(
                        locale=self.locale,
                        model=self.model,
                        reply_lang=self.reply_lang,
                        claude_path=self.claude_path,
                        on_status=self.on_status,
                    )
                    if self.dispatch:
                        self.dispatch(item)
                    else:
                        process_work_item(item, trigger_config=self.trigger_config)
                        if self._on_reply:
                            self._on_reply(item.trigger, item.user_handle, item.node_id)

                if self.on_poll:
                    self.on_poll()
            except Exception as err:
                self.log(f'\u26a0\ufe0f Poll error: {err}')
            self._stop_event.wait(timeout=self.interval)


# ── CLI entry point ───────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    args = sys.argv[1:]
    if not args or args[0] != 'watch' or len(args) < 2:
        print('Usage: python -m figwatch.watcher watch <file-key> [-l locale] [-i interval]')
        sys.exit(1)

    file_key = args[1]
    locale_idx = args.index('-l') if '-l' in args else -1
    locale = args[locale_idx + 1] if locale_idx >= 0 and locale_idx + 1 < len(args) else 'uk'
    interval_idx = args.index('-i') if '-i' in args else -1
    interval = int(args[interval_idx + 1]) if interval_idx >= 0 and interval_idx + 1 < len(args) else 30

    home = os.path.expanduser('~')
    pat = None
    for config_path in [
        os.path.join(home, '.figwatch', 'config.json'),
        os.path.join(home, '.figma-ds-cli', 'config.json'),
    ]:
        try:
            with open(config_path) as f:
                config = json.load(f)
                if config.get('figmaPat'):
                    pat = config['figmaPat']
                    break
        except Exception:
            pass

    if not pat:
        print('\u274c No Figma PAT found in ~/.figwatch/config.json')
        sys.exit(1)

    w = FigmaWatcher(file_key, pat, locale=locale, interval=interval)
    w.start()

    try:
        w._thread.join()
    except KeyboardInterrupt:
        w.stop()
