"""Reply formatting utilities."""

import re

from figwatch.trigger_config import load_trigger_config

FIGMA_COMMENT_LIMIT = 4900


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
