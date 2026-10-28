"""Fixed text FigWatch posts to Figma: sign-off, acks, queue position, error replies.

Everything that recognises FigWatch's own comments goes through SIGN_OFF and
is_bot_message here, so a sign-off change can't silently break own-reply
detection (and re-trigger audits) in one entry point but not another.
"""

SIGN_OFF = '— FigWatch'
_SIGN_OFF_LOWER = SIGN_OFF.lower()


def is_bot_message(message):
    """True if a comment carries FigWatch's sign-off (case-insensitive)."""
    return _SIGN_OFF_LOWER in (message or '').lower()


def ack_message(trigger, lang='en'):
    name = trigger.lstrip('@')
    if lang == 'cn':
        return f'⏳ 已收到 {name} 审查请求，正在处理…'
    return f'⏳ {name} audit received — working on it…'


def queue_message(trigger, position, trace_line=''):
    """Ack body for a queued audit. position = audits ahead; 0 means 'starting shortly'."""
    name = trigger.lstrip('@')
    if position <= 0:
        return f'⏳ {name} audit queued — starting shortly…{trace_line}'
    return f'⏳ {name} audit queued ({position} ahead of you)…{trace_line}'


def error_kind(err):
    """'too_large' when the screenshot couldn't be captured (usually a frame too
    big to render as one image), else 'generic'."""
    from figwatch.skills import MissingDesignData
    if isinstance(err, MissingDesignData) and 'screenshot' in err.missing:
        return 'too_large'
    return 'generic'


def error_reply(kind, trigger, lang='en'):
    """Short reply posted instead of an audit when it fails, so the reviewer
    gets feedback and the trigger (now replied to) isn't re-audited."""
    name = trigger.lstrip('@')
    cn = lang == 'cn'
    if kind == 'too_large':
        if cn:
            body = (
                f'⚠️ 无法截取该画面的截图，它可能太大或太复杂，Figma 无法一次性渲染。'
                f'如果这是完整的用户旅程，请在其中某个具体页面上触发 @{name}，我会单独审查。'
            )
        else:
            body = (
                f"⚠️ I couldn't capture a screenshot of this frame — it may be too "
                f'large or complex for Figma to render as a single image. For a big user '
                f"journey, trigger @{name} on a specific screen inside it and I'll review that."
            )
    elif cn:
        body = (
            f'⚠️ 暂时无法完成 {name} 审查。'
            f'该画面的设计数据可能暂不可用，或服务繁忙。请重新触发以重试。'
        )
    else:
        body = (
            f"⚠️ Couldn't complete the {name} audit right now. This frame's design "
            f'data may be unavailable, or the service was busy. Re-trigger to retry.'
        )
    return f'{body}\n\n{SIGN_OFF}'
