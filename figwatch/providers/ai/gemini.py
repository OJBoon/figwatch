"""Google Generative AI (Gemini) provider."""

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
            import google.generativeai as genai
        except ImportError:
            raise RuntimeError('google-generativeai not installed — run: pip install google-generativeai')

        if self._rate_limiter:
            self._rate_limiter.acquire()

        genai.configure(api_key=self._api_key)
        model = genai.GenerativeModel(self._model_name)

        parts = []
        if image_path:
            mime_type = 'image/jpeg' if image_path.endswith('.jpg') else 'image/png'
            with open(image_path, 'rb') as f:
                parts.append({'mime_type': mime_type, 'data': f.read()})
        parts.append(prompt)

        return with_retry(
            lambda: model.generate_content(parts).text.strip(),
            lambda e: '429' in str(e) or 'quota' in str(e).lower(),
            'gemini',
        )
