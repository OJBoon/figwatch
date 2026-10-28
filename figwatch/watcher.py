"""FigWatch comment watcher — polls Figma for trigger comments and dispatches audits."""

import json
import logging
import os
import threading
from collections import OrderedDict

from figwatch.domain import Audit, Comment, ThreadMessage, TriggerMatch, match_trigger
from figwatch.log_context import new_audit_id
from figwatch.providers.figma import figma_get
from figwatch.trigger_config import load_trigger_config

logger = logging.getLogger(__name__)

_EM_DASH = '\u2014'
_OWN_REPLY_MARKERS = (_EM_DASH + ' figwatch',)

_PROCESSED_MAXLEN = 500

# Cap the refinement transcript so a long thread can't blow the prompt budget
# (the node tree already has its own 40k cap). Oldest turns are dropped first.
_THREAD_TRANSCRIPT_CHAR_LIMIT = 6000


# ── Bounded set ───────────────────────────────────────────────────────

class BoundedSet:
    """Set with a maximum size. Oldest entries evicted on overflow."""

    def __init__(self, maxlen=_PROCESSED_MAXLEN):
        self._maxlen = maxlen
        self._data = OrderedDict()

    def add(self, item):
        if item in self._data:
            self._data.move_to_end(item)
            return
        if len(self._data) >= self._maxlen:
            self._data.popitem(last=False)
        self._data[item] = None

    def __contains__(self, item):
        return item in self._data

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def update(self, items):
        for item in items:
            self.add(item)

    def clear(self):
        self._data.clear()


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
            ids = json.load(f)
            bounded = BoundedSet()
            bounded.update(ids)
            return bounded
    except Exception:
        return BoundedSet()


def save_processed(ids):
    with open(_processed_path(), 'w') as f:
        json.dump(list(ids), f)


# ── Trigger detection (fast path — 1 API call) ────────────────────────

def _is_bot_message(message):
    low = (message or '').lower()
    return any(m in low for m in _OWN_REPLY_MARKERS)


def _strip_mention(message, mention):
    """Drop the @figwatch mention token from a follow-up so the model sees the ask."""
    import re
    out = re.sub(re.escape(mention), '', message, flags=re.IGNORECASE)
    return ' '.join(out.split()).strip()


def _strip_bot_chrome(message):
    """Reduce a prior FigWatch reply to its body — drop the 🗣️ header line and
    everything from the '— FigWatch' sign-off onward."""
    body = []
    for ln in (message or '').splitlines():
        if _is_bot_message(ln):   # sign-off line reached
            break
        if ln.strip().startswith('\U0001f5e3'):  # 🗣️ header line
            continue
        body.append(ln)
    return '\n'.join(body).strip()


def _build_thread_history(thread, follow_up, created_key):
    """Ordered ThreadMessage transcript of everything in the thread before the
    follow-up, capped oldest-first to the char budget."""
    fu_id = follow_up['id']
    fu_created = created_key(follow_up)
    msgs = []
    for x in sorted(thread, key=created_key):
        if x['id'] == fu_id:
            continue
        xc = created_key(x)
        if xc and fu_created and xc > fu_created:
            continue  # created after the follow-up — not part of its context
        raw = x.get('message') or ''
        is_bot = _is_bot_message(raw)
        text = _strip_bot_chrome(raw) if is_bot else raw.strip()
        if not text:
            continue
        author = 'FigWatch' if is_bot else x.get('user', {}).get('handle', 'reviewer')
        msgs.append(ThreadMessage(is_bot=is_bot, author=author, text=text))
    # Drop oldest turns until under the budget.
    total = sum(len(m.text) for m in msgs)
    while len(msgs) > 1 and total > _THREAD_TRANSCRIPT_CHAR_LIMIT:
        total -= len(msgs.pop(0).text)
    return tuple(msgs)


def _detect_follow_ups(comments, comment_map, processed_ids, trigger_config,
                       file_key, mention, *, log):
    """Build refinement Audits for unprocessed @mention replies in audited threads."""
    def _created(c):
        return c.get('created_at') or ''

    mention_lower = mention.lower()
    out = []
    for c in comments:
        root_id = c.get('parent_id')
        if not root_id or c['id'] in processed_ids or c.get('resolved_at'):
            continue
        msg = c.get('message') or ''
        if mention_lower not in msg.lower() or _is_bot_message(msg):
            continue

        # Thread = root comment + all its replies (comments are flat with parent_id).
        thread = [x for x in comments if x['id'] == root_id or x.get('parent_id') == root_id]
        # Only engage if FigWatch has already audited this thread.
        if not any(_is_bot_message(x.get('message')) for x in thread):
            continue
        # Recover the original skill from the human trigger still in the thread.
        orig = None
        for x in sorted(thread, key=_created):
            if _is_bot_message(x.get('message')):
                continue
            orig = match_trigger(x.get('message') or '', trigger_config)
            if orig:
                break
        if not orig:
            continue  # can't route to a skill

        root = comment_map.get(root_id, {})
        node_id = (root.get('client_meta') or {}).get('node_id')
        if not node_id:
            processed_ids.add(c['id'])
            continue

        history = _build_thread_history(thread, c, _created)
        extra = _strip_mention(msg, mention)
        processed_ids.add(c['id'])
        user_handle = c.get('user', {}).get('handle', 'unknown')
        out.append(Audit(
            audit_id=new_audit_id(),
            comment=Comment(
                comment_id=c['id'],
                message=msg,
                parent_id=root_id,
                node_id=node_id,
                user_handle=user_handle,
                file_key=file_key,
            ),
            trigger_match=TriggerMatch(trigger=orig.trigger, extra=extra),
            thread_history=history,
        ))
        log(f'\U0001f4ac {mention} follow-up by {user_handle} on node {node_id}')
    return out


def detect_triggers(file_key, pat, processed_ids, trigger_config, *, log,
                    follow_up_mention=None):
    """Fetch comments, find trigger matches, return list[Audit].

    Fast path: single API call, <1s. Does NOT call any AI provider.

    follow_up_mention (e.g. "@figwatch"): when set, a human reply containing it in
    a thread already audited by FigWatch becomes a *refinement* Audit carrying the
    thread transcept and routed to the original skill. None (CLI/server) → the
    original one-shot behaviour, byte-identical.
    """
    data = figma_get(f'/files/{file_key}/comments', pat)
    comments = data.get('comments', []) if data else []

    comment_map = {c['id']: c for c in comments}

    replied_to = set()
    for c in comments:
        msg_lower = (c.get('message') or '').lower()
        if c.get('parent_id') and any(m in msg_lower for m in _OWN_REPLY_MARKERS):
            replied_to.add(c['parent_id'])
            processed_ids.add(c['parent_id'])

    initial_count = len(processed_ids)
    audits = []

    # ── Conversational follow-ups (@figwatch replies) ──────────────────
    # A separate admittance channel from the keyword-trigger loop below, so the
    # thread-poisoning that protects the original trigger from re-audit stays
    # intact. Only human replies (no sign-off marker) that mention the bot in a
    # thread that already has a FigWatch audit qualify.
    if follow_up_mention:
        audits.extend(_detect_follow_ups(
            comments, comment_map, processed_ids, trigger_config,
            file_key, follow_up_mention, log=log,
        ))

    candidates = []
    for c in comments:
        if c['id'] in processed_ids or c.get('resolved_at'):
            continue
        if not c.get('parent_id'):
            if (c.get('client_meta') or {}).get('node_id'):
                candidates.append(c)
        else:
            msg_lower = (c.get('message') or '').lower()
            if not any(m in msg_lower for m in _OWN_REPLY_MARKERS):
                candidates.append(c)

    for comment in candidates:
        if comment['id'] in processed_ids or comment['id'] in replied_to:
            continue
        if comment.get('parent_id') and comment['parent_id'] in replied_to:
            continue

        trigger_match = match_trigger(comment.get('message', ''), trigger_config)
        if not trigger_match:
            continue

        node_id = (comment.get('client_meta') or {}).get('node_id')
        comment['id']
        parent_id = None
        if comment.get('parent_id'):
            parent = comment_map.get(comment['parent_id'])
            if parent:
                node_id = node_id or (parent.get('client_meta') or {}).get('node_id')
            parent_id = comment['parent_id']

        if not node_id:
            processed_ids.add(comment['id'])
            continue

        processed_ids.add(comment['id'])
        user_handle = comment.get('user', {}).get('handle', 'unknown')

        audit = Audit(
            audit_id=new_audit_id(),
            comment=Comment(
                comment_id=comment['id'],
                message=comment.get('message', ''),
                parent_id=parent_id,
                node_id=node_id,
                user_handle=user_handle,
                file_key=file_key,
            ),
            trigger_match=trigger_match,
        )
        audits.append(audit)
        log(
            f'\U0001f4ac {trigger_match.trigger.keyword} comment'
            f' by {user_handle} on node {node_id}',
        )

    if len(processed_ids) > initial_count:
        save_processed(processed_ids)
    return audits


# ── Watcher class ─────────────────────────────────────────────────────

class FigmaWatcher:
    def __init__(self, file_key, pat, *, audit_service,
                 interval=30, log=print,
                 trigger_config=None, dispatch=None, on_poll=None,
                 initial_delay=0, event_listener=None, follow_up_mention=None):
        self.file_key = file_key
        self.pat = pat
        self.audit_service = audit_service
        self.interval = interval
        self.log = log
        self.trigger_config = trigger_config or load_trigger_config()
        self.dispatch = dispatch
        self.on_poll = on_poll
        self.initial_delay = initial_delay
        self._event_listener = event_listener
        self.follow_up_mention = follow_up_mention
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
                audits = detect_triggers(
                    self.file_key, self.pat, self._processed,
                    self.trigger_config, log=self.log,
                    follow_up_mention=self.follow_up_mention,
                )
                for audit in audits:
                    if self.dispatch:
                        self.dispatch(audit)
                    else:
                        self._execute_audit(audit)

                if self.on_poll:
                    self.on_poll()
            except Exception as err:
                self.log(f'\u26a0\ufe0f Poll error: {err}')
            self._stop_event.wait(timeout=self.interval)

    def _execute_audit(self, audit):
        """Execute audit via AuditService and notify event listener."""
        trigger_name = audit.trigger_match.trigger.keyword.lstrip('@')
        ack_id = self.audit_service.post_ack(
            audit,
            f'\u23f3 {trigger_name} audit received \u2014 working on it\u2026',
        )

        try:
            response = self.audit_service.execute(audit)
            self.audit_service.delete_ack(audit, ack_id)
            self.audit_service.post_reply(audit, response)
        except Exception as err:
            self.audit_service.delete_ack(audit, ack_id)
            try:
                self.audit_service.post_reply(
                    audit,
                    f'Audit failed: {err}\n\n{_EM_DASH} FigWatch',
                )
            except Exception:
                logger.exception('error reply post also failed')

        if self._event_listener:
            for event in audit.collect_events():
                self._event_listener(event, audit)


# ── CLI entry point ───────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    from figwatch.providers.figma import (
        FigmaCommentRepository,
        FigmaDesignDataRepository,
    )
    from figwatch.services import AuditConfig, AuditService

    args = sys.argv[1:]
    if not args or args[0] != 'watch' or len(args) < 2:
        print('Usage: python -m figwatch.watcher watch <file-key> [-l locale] [-i interval]')
        sys.exit(1)

    file_key = args[1]
    locale_idx = args.index('-l') if '-l' in args else -1
    locale = args[locale_idx + 1] if locale_idx >= 0 and locale_idx + 1 < len(args) else 'uk'
    interval_idx = args.index('-i') if '-i' in args else -1
    has_interval = interval_idx >= 0 and interval_idx + 1 < len(args)
    interval = int(args[interval_idx + 1]) if has_interval else 30

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

    svc = AuditService(
        comment_repo=FigmaCommentRepository(pat),
        design_repo=FigmaDesignDataRepository(pat),
        config=AuditConfig(model='sonnet', claude_path='api', reply_lang='en', locale=locale),
        trigger_config=load_trigger_config(),
    )
    w = FigmaWatcher(file_key, pat, audit_service=svc, interval=interval)
    w.start()

    try:
        w._thread.join()
    except KeyboardInterrupt:
        w.stop()
