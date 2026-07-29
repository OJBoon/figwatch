"""Anthropic Messages API provider."""

import base64

from figwatch.providers.ai import as_image_list, media_type_for, with_retry


def _image_block(path):
    with open(path, 'rb') as f:
        img_b64 = base64.standard_b64encode(f.read()).decode()
    return {
        'type': 'image',
        'source': {'type': 'base64', 'media_type': media_type_for(path), 'data': img_b64},
    }


class AnthropicProvider:
    inline_files = True

    def __init__(self, model_name: str, api_key: str, rate_limiter=None,
                 *, base_url: 'str | None' = None, auth_token: 'str | None' = None,
                 max_tokens: int = 4096):
        self.model_id = model_name
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._auth_token = auth_token
        self._max_tokens = max_tokens
        self._rate_limiter = rate_limiter

    def call(self, prompt: str, image_path: 'str | list[str] | None') -> str:
        try:
            import anthropic
        except ImportError as err:
            raise RuntimeError(
                'anthropic package not installed — run: pip install anthropic',
            ) from err

        if self._rate_limiter:
            self._rate_limiter.acquire()

        # A custom base_url + bearer token targets an Anthropic-compatible
        # gateway (e.g. a cc-switch company profile); otherwise use the API key.
        client_kwargs: dict = {}
        if self._base_url:
            client_kwargs['base_url'] = self._base_url
        if self._auth_token:
            client_kwargs['auth_token'] = self._auth_token
        else:
            client_kwargs['api_key'] = self._api_key
        client = anthropic.Anthropic(**client_kwargs)

        # One path, a list of paths (journey review), or None. A single str is a
        # 1-element list, so the single-image request is byte-identical.
        content = [_image_block(p) for p in as_image_list(image_path)]
        content.append({'type': 'text', 'text': prompt})

        def _call():
            response = client.messages.create(
                model=self._model_name,
                max_tokens=self._max_tokens,
                messages=[{'role': 'user', 'content': content}],
            )
            return response.content[0].text.strip()

        def _is_rate_limit(e):
            return 'RateLimitError' in type(e).__name__ or '429' in str(e)

        return with_retry(_call, _is_rate_limit, 'anthropic')
