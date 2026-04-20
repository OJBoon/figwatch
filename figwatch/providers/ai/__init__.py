"""AI provider protocol and factory.

All providers expose:
  .model_id     — resolved model identifier (e.g. 'claude-sonnet-4-6')
  .inline_files — True if prompt embeds data inline; False if it uses file paths
  .call(prompt, image_path) -> str
"""

import logging
import os
import re
import threading
import time
from typing import Optional, Protocol, runtime_checkable

from figwatch.providers.ai.rate_limit import TokenBucket

logger = logging.getLogger(__name__)

# Friendly aliases → full Anthropic API model IDs
CLAUDE_API_MODELS = {
    'sonnet': 'claude-sonnet-4-6',
    'opus':   'claude-opus-4-6',
    'haiku':  'claude-haiku-4-5-20251001',
}

# Friendly aliases → full Google AI model IDs
# Values not listed here are passed through as-is.
GEMINI_MODELS = {
    'gemini':            'gemini-3.1-flash-lite-preview',
    'gemini-flash':      'gemini-3.1-flash-lite-preview',
    'gemini-flash-lite': 'gemini-3.1-flash-lite-preview',
}

# Shared rate limiters, lazy-initialized from env vars.
# Set FIGWATCH_{PROVIDER}_RPM=0 to disable rate limiting for that provider.
_UNSET = object()
_gemini_limiter: Optional[TokenBucket] = _UNSET
_anthropic_limiter: Optional[TokenBucket] = _UNSET
_limiter_lock = threading.Lock()


def _build_limiter(env_var: str, default_rpm: int) -> Optional[TokenBucket]:
    rpm = int(os.environ.get(env_var, str(default_rpm)))
    if rpm <= 0:
        return None
    # Capacity equals rpm so the bucket can absorb a one-minute burst.
    return TokenBucket(capacity=rpm, refill_per_second=rpm / 60)


def get_gemini_limiter() -> Optional[TokenBucket]:
    global _gemini_limiter
    if _gemini_limiter is not _UNSET:
        return _gemini_limiter
    with _limiter_lock:
        if _gemini_limiter is _UNSET:
            _gemini_limiter = _build_limiter('FIGWATCH_GEMINI_RPM', 15)
    return _gemini_limiter


def get_anthropic_limiter() -> Optional[TokenBucket]:
    global _anthropic_limiter
    if _anthropic_limiter is not _UNSET:
        return _anthropic_limiter
    with _limiter_lock:
        if _anthropic_limiter is _UNSET:
            _anthropic_limiter = _build_limiter('FIGWATCH_ANTHROPIC_RPM', 5)
    return _anthropic_limiter


def reset_limiters() -> None:
    """Reset module-level limiters. Used by tests."""
    global _gemini_limiter, _anthropic_limiter
    with _limiter_lock:
        _gemini_limiter = _UNSET
        _anthropic_limiter = _UNSET


@runtime_checkable
class AIProvider(Protocol):
    model_id: str
    inline_files: bool

    def call(self, prompt: str, image_path: 'str | None') -> str:
        ...


def parse_retry_seconds(err, default=5):
    """Extract suggested retry delay in seconds from a 429 error message."""
    m = re.search(r'retry[_\s]delay\D*?(\d+)|retry after (\d+)', str(err), re.IGNORECASE)
    if m:
        return int(m.group(1) or m.group(2))
    return default


def with_retry(call_fn, is_rate_limit_fn, label):
    """Call call_fn(), retrying once on a rate-limit error after the suggested delay."""
    for attempt in range(2):
        try:
            return call_fn()
        except Exception as e:
            if is_rate_limit_fn(e) and attempt == 0:
                wait = parse_retry_seconds(e)
                logger.warning(
                    'rate limited — retrying',
                    extra={'provider': label, 'retry_in_seconds': wait},
                )
                time.sleep(wait)
            else:
                raise


def make_provider(model: str, claude_path: str, *, skill_dir: str = '') -> AIProvider:
    """Return the appropriate AI provider for the given model and claude_path."""
    from figwatch.providers.ai.gemini import GeminiProvider
    from figwatch.providers.ai.anthropic import AnthropicProvider
    from figwatch.providers.ai.claude_cli import ClaudeCLIProvider

    if (model or '').startswith('gemini'):
        model_name = GEMINI_MODELS.get(model, model)
        return GeminiProvider(
            model_name,
            os.environ.get('GOOGLE_API_KEY', ''),
            rate_limiter=get_gemini_limiter(),
        )
    if claude_path == 'api':
        model_name = CLAUDE_API_MODELS.get(model, model)
        return AnthropicProvider(
            model_name,
            os.environ.get('ANTHROPIC_API_KEY', ''),
            rate_limiter=get_anthropic_limiter(),
        )
    return ClaudeCLIProvider(model, claude_path, skill_dir)
