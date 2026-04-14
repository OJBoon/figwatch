"""Work item processing: ack → run skill → post reply.

Public helpers (post_ack, update_ack, delete_ack, clean_reply, post_reply) are
used by both the macOS polling path (via process_work_item) and the server
worker loop, which owns its own ack lifecycle for queue position + retry updates.
"""

import re
from typing import Optional

from figwatch.domain import STATUS_PROCESSING, STATUS_REPLIED, STATUS_ERROR, load_trigger_config
from figwatch.providers.figma import figma_post, figma_delete

_EM_DASH = '\u2014'
FIGMA_COMMENT_LIMIT = 4900


# ── Ack lifecycle helpers ─────────────────────────────────────────────

def post_ack(item, message: str, *, log=print) -> Optional[str]:
    """Post an ack reply to the trigger comment. Returns ack_id or None on failure."""
    try:
        ack = figma_post(f'/files/{item.file_key}/comments', {
            'message': message,
            'comment_id': item.reply_to_id,
        }, item.pat)
        ack_id = ack.get('id')
        log(f'ack posted (comment {ack_id})')
        return ack_id
    except Exception as e:
        log(f'ack post failed (non-fatal): {e}')
        return None


def delete_ack(item, ack_id: Optional[str], *, log=print) -> None:
    """Delete an ack comment. Silent if ack_id is None or delete fails."""
    if not ack_id:
        return
    try:
        figma_delete(f'/files/{item.file_key}/comments/{ack_id}', item.pat)
        log('ack deleted')
    except Exception as e:
        log(f'ack delete failed (non-fatal): {e}')


def update_ack(item, old_ack_id: Optional[str], message: str, *, log=print) -> Optional[str]:
    """Replace an ack comment with a new one (Figma has no PUT). Returns the new ack_id."""
    delete_ack(item, old_ack_id, log=log)
    return post_ack(item, message, log=log)


# ── Reply formatting + posting ────────────────────────────────────────

def clean_reply(response: str, trigger_config=None) -> str:
    """Strip trigger words from reply to prevent feedback loops, then truncate."""
    for entry in (trigger_config or load_trigger_config()):
        trigger_word = entry.get('trigger', '')
        if trigger_word:
            response = re.sub(
                r'(?<!\w)' + re.escape(trigger_word) + r'(?!\w)',
                trigger_word.lstrip('@'),
                response,
                flags=re.IGNORECASE,
            )

    if len(response) > FIGMA_COMMENT_LIMIT:
        total = len(response)
        truncated = response[:FIGMA_COMMENT_LIMIT - 60]
        last_nl = truncated.rfind('\n')
        if last_nl > FIGMA_COMMENT_LIMIT // 2:
            truncated = truncated[:last_nl]
        response = truncated + f'\n\n(truncated \u2014 full audit was {total} chars)'
    return response


def post_reply(item, message: str) -> None:
    """Post a reply in the thread of the trigger comment."""
    figma_post(f'/files/{item.file_key}/comments', {
        'message': message,
        'comment_id': item.reply_to_id,
    }, item.pat)


# ── macOS polling path (creates ack itself) ───────────────────────────

def process_work_item(item, *, log=print, trigger_config=None):
    """Post ack, execute skill, post reply. Used by the macOS polling path.

    The server path uses the helpers above directly so it can manage ack lifecycle
    across retries and queue position updates.

    Returns True on success, False on failure (error reply posted to Figma).
    """
    from figwatch.skills import execute_skill

    if item.on_status:
        item.on_status(STATUS_PROCESSING, item)

    ack_id = post_ack(
        item,
        f'\u23f3 {item.trigger.lstrip("@")} audit received \u2014 working on it\u2026',
        log=log,
    )

    try:
        log(f'running skill {item.skill_path}\u2026')
        response = execute_skill(item)
        log(f'skill returned {len(response)} chars')

        delete_ack(item, ack_id, log=log)
        response = clean_reply(response, trigger_config)
        post_reply(item, response)
        log(f'reply posted to comment {item.reply_to_id}')

        if item.on_status:
            item.on_status(STATUS_REPLIED, item)
        return True

    except Exception as err:
        log(f'failed: {err}')
        delete_ack(item, ack_id, log=log)
        try:
            post_reply(item, f'Audit failed: {err}\n\n{_EM_DASH} FigWatch')
            log('error reply posted')
        except Exception:
            pass
        if item.on_status:
            item.on_status(STATUS_ERROR, item, error=str(err))
        return False
