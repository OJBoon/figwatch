"""Anthropic Messages API provider."""

import base64

from figwatch.providers.ai import with_retry


class AnthropicProvider:
    inline_files = True

    def __init__(self, model_name: str, api_key: str, rate_limiter=None):
        self.model_id = model_name
        self._model_name = model_name
        self._api_key = api_key
        self._rate_limiter = rate_limiter

    def call(self, prompt: str, image_path: 'str | None') -> str:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError('anthropic package not installed — run: pip install anthropic')

        if self._rate_limiter:
            self._rate_limiter.acquire()

        client = anthropic.Anthropic(api_key=self._api_key)
        content = []

        if image_path:
            media_type = 'image/jpeg' if image_path.endswith('.jpg') else 'image/png'
            with open(image_path, 'rb') as f:
                img_b64 = base64.standard_b64encode(f.read()).decode()
            content.append({
                'type': 'image',
                'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64},
            })
        content.append({'type': 'text', 'text': prompt})

        def _call():
            response = client.messages.create(
                model=self._model_name,
                max_tokens=4096,
                messages=[{'role': 'user', 'content': content}],
            )
            return response.content[0].text.strip()

        def _is_rate_limit(e):
            return 'RateLimitError' in type(e).__name__ or '429' in str(e)

        return with_retry(_call, _is_rate_limit, 'anthropic')
