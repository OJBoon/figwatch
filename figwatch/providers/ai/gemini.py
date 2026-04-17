"""Google Gen AI (Gemini) provider."""

from figwatch.providers.ai import with_retry


class GeminiProvider:
    name = 'Gemini'
    inline_files = True

    def __init__(self, model_name: str, api_key: str, rate_limiter=None):
        self._model_name = model_name
        self._api_key = api_key
        self._rate_limiter = rate_limiter

    def call(self, prompt: str, image_path: 'str | None') -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise RuntimeError('google-genai not installed — run: pip install google-genai')

        if self._rate_limiter:
            self._rate_limiter.acquire()

        client = genai.Client(api_key=self._api_key)

        contents = [prompt]
        if image_path:
            mime_type = 'image/jpeg' if image_path.endswith('.jpg') else 'image/png'
            with open(image_path, 'rb') as f:
                contents.append(types.Part.from_bytes(data=f.read(), mime_type=mime_type))

        return with_retry(
            lambda: client.models.generate_content(
                model=self._model_name,
                contents=contents,
            ).text.strip(),
            lambda e: '429' in str(e) or 'quota' in str(e).lower(),
            'gemini',
        )
