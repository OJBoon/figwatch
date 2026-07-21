"""Feedback collection — storage and HTML form serving."""

import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger(__name__)

_HOME = Path.home()
_FEEDBACK_FILE = _HOME / '.figwatch' / '.feedback.json'
_lock = threading.Lock()


def _ensure_dir():
    _FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)


def save_feedback(audit_id: str, skill: str, attempt: str,
                  trace_id: str, rating: int, comment: str) -> None:
    """Append a feedback entry to the JSON file (thread-safe)."""
    entry = {
        'audit_id': audit_id,
        'skill': skill,
        'attempt': attempt,
        'trace_id': trace_id,
        'rating': rating,
        'comment': comment,
        'submitted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    _ensure_dir()
    with _lock:
        existing = []
        if _FEEDBACK_FILE.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                existing = json.loads(_FEEDBACK_FILE.read_text())
        existing.append(entry)
        _FEEDBACK_FILE.write_text(json.dumps(existing, indent=2))
    logger.info('feedback saved', extra={'audit_id': audit_id, 'rating': rating})


def build_feedback_url(base_url: str, audit_id: str, skill: str,
                       attempt: int, trace_id: str) -> str:
    """Build feedback form URL with query params."""
    params = {
        'audit_id': audit_id,
        'skill': skill,
        'attempt': str(attempt),
    }
    if trace_id:
        params['trace_id'] = trace_id
    return f'{base_url.rstrip("/")}/feedback?{urlencode(params)}'


def parse_feedback_params(path: str) -> dict:
    """Extract query params from a request path."""
    parsed = urlparse(path)
    qs = parse_qs(parsed.query)
    return {k: v[0] for k, v in qs.items()}


def render_form(params: dict) -> str:
    """Render the feedback HTML form."""
    audit_id = _esc(params.get('audit_id', ''))
    skill = _esc(params.get('skill', ''))
    attempt = _esc(params.get('attempt', ''))
    trace_id = _esc(params.get('trace_id', ''))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FigWatch Feedback</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f5f5; color: #333; padding: 2rem; }}
  .container {{ max-width: 480px; margin: 0 auto; background: #fff;
                border-radius: 12px; padding: 2rem; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.5rem; }}
  .meta {{ font-size: 0.85rem; color: #666; margin-bottom: 1.5rem; }}
  .meta span {{ display: inline-block; background: #f0f0f0; padding: 2px 8px;
                border-radius: 4px; margin: 2px 4px 2px 0; }}
  .stars {{ display: flex; gap: 0.25rem; margin-bottom: 1.5rem; flex-direction: row-reverse;
            justify-content: flex-end; }}
  .stars input {{ display: none; }}
  .stars label {{ font-size: 2rem; color: #ddd; cursor: pointer; transition: color 0.15s; }}
  .stars input:checked ~ label,
  .stars label:hover,
  .stars label:hover ~ label {{ color: #f5a623; }}
  textarea {{ width: 100%; height: 120px; border: 1px solid #ddd; border-radius: 8px;
              padding: 0.75rem; font-size: 0.95rem; resize: vertical; margin-bottom: 1rem; }}
  textarea:focus {{ outline: none; border-color: #4a90d9; }}
  button {{ background: #333; color: #fff; border: none; border-radius: 8px;
            padding: 0.75rem 1.5rem; font-size: 1rem; cursor: pointer; }}
  button:hover {{ background: #555; }}
  .success {{ text-align: center; padding: 2rem 0; }}
  .success h2 {{ color: #2ecc71; margin-bottom: 0.5rem; }}
</style>
</head>
<body>
<div class="container">
  <h1>FigWatch Feedback</h1>
  <div class="meta">
    <span>Audit: {audit_id or 'N/A'}</span>
    <span>Skill: {skill or 'N/A'}</span>
    {f'<span>Attempt: {attempt}</span>' if attempt else ''}
    {_trace_span(trace_id)}
  </div>
  <form method="POST" action="/feedback">
    <input type="hidden" name="audit_id" value="{audit_id}">
    <input type="hidden" name="skill" value="{skill}">
    <input type="hidden" name="attempt" value="{attempt}">
    <input type="hidden" name="trace_id" value="{trace_id}">

    <p style="margin-bottom: 0.5rem; font-weight: 500;">How was the response?</p>
    <div class="stars">
      <input type="radio" id="s5" name="rating" value="5"><label for="s5">&#9733;</label>
      <input type="radio" id="s4" name="rating" value="4"><label for="s4">&#9733;</label>
      <input type="radio" id="s3" name="rating" value="3"><label for="s3">&#9733;</label>
      <input type="radio" id="s2" name="rating" value="2"><label for="s2">&#9733;</label>
      <input type="radio" id="s1" name="rating" value="1"><label for="s1">&#9733;</label>
    </div>

    <textarea name="comment" placeholder="Any additional feedback? (optional)"></textarea>

    <button type="submit">Submit Feedback</button>
  </form>
</div>
</body>
</html>"""


THANK_YOU_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FigWatch Feedback</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f5f5; color: #333; padding: 2rem; }
  .container { max-width: 480px; margin: 0 auto; background: #fff;
               border-radius: 12px; padding: 2rem; box-shadow: 0 2px 8px rgba(0,0,0,0.1);
               text-align: center; }
  h1 { font-size: 1.4rem; margin-bottom: 0.5rem; color: #2ecc71; }
  p { color: #666; }
</style>
</head>
<body>
<div class="container">
  <h1>Thank you!</h1>
  <p>Your feedback has been recorded.</p>
</div>
</body>
</html>"""


def _esc(s: str) -> str:
    """Escape HTML special characters."""
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


def _trace_span(trace_id: str) -> str:
    """Render a trace ID badge, truncating long IDs."""
    if not trace_id:
        return ''
    label = f'{trace_id[:12]}...' if len(trace_id) > 12 else trace_id
    return f'<span>Trace: {label}</span>'
