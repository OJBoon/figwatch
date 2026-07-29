"""Google Gen AI (Gemini) provider."""

from figwatch.providers.ai import as_image_list, media_type_for, with_retry


class GeminiProvider:
    inline_files = True

    def __init__(self, model_name: str, api_key: str, rate_limiter=None):
        self.model_id = model_name
        self._model_name = model_name
        self._api_key = api_key
        self._rate_limiter = rate_limiter

    def call(self, prompt: str, image_path: 'str | list[str] | None') -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError as err:
            raise RuntimeError(
                'google-genai not installed — run: pip install google-genai',
            ) from err

        if self._rate_limiter:
            self._rate_limiter.acquire()

        client = genai.Client(api_key=self._api_key)

        # One path, a list (journey review), or None; a single str == 1-element list.
        contents = [prompt]
        for p in as_image_list(image_path):
            with open(p, 'rb') as f:
                contents.append(types.Part.from_bytes(data=f.read(), mime_type=media_type_for(p)))

        return with_retry(
            lambda: client.models.generate_content(
                model=self._model_name,
                contents=contents,
            ).text.strip(),
            lambda e: '429' in str(e) or 'quota' in str(e).lower(),
            'gemini',
        )
