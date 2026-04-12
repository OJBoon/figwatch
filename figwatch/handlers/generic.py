"""Generic skill execution handler."""

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from figwatch.handlers import (
    strip_markdown, subprocess_env, urllib_quote,
    figma_get_retry, parse_claude_output,
)

_HOME = Path.home()
_BUNDLED_SKILLS = Path(__file__).parent.parent / 'skills'

# ── Provider / model helpers ───────────────────────────────────────

# Friendly aliases → full Anthropic API model IDs
_CLAUDE_API_MODELS = {
    'sonnet': 'claude-sonnet-4-6',
    'opus':   'claude-opus-4-6',
    'haiku':  'claude-haiku-4-5-20251001',
}
# Friendly aliases → full Google AI model IDs
# For models not listed here, the value passed to FIGWATCH_MODEL is used as-is.
_GEMINI_MODELS = {
    'gemini':            'gemini-3.1-flash-lite-preview',
    'gemini-flash':      'gemini-3.1-flash-lite-preview',
    'gemini-flash-lite': 'gemini-3.1-flash-lite-preview',
}

# Node tree is embedded inline for API providers — cap to avoid token limit blowout
_NODE_TREE_CHAR_LIMIT = 40_000


def _detect_provider(model, claude_path):
    """Return 'gemini', 'anthropic-api', or 'claude-cli'."""
    if (model or '').startswith('gemini'):
        return 'gemini'
    if claude_path == 'api':
        return 'anthropic-api'
    return 'claude-cli'


def _parse_retry_seconds(err, default=60):
    """Extract suggested retry delay in seconds from a 429 error message."""
    m = re.search(r'retry[_\s]delay\D*?(\d+)|retry after (\d+)', str(err), re.IGNORECASE)
    if m:
        return int(m.group(1) or m.group(2))
    return default


def _with_retry(call_fn, is_rate_limit_fn, label):
    """Call call_fn(), retrying once on a rate-limit error after the suggested delay."""
    for attempt in range(2):
        try:
            return call_fn()
        except Exception as e:
            if is_rate_limit_fn(e) and attempt == 0:
                wait = _parse_retry_seconds(e)
                print(f'   {label} 429 — retrying in {wait}s…', flush=True)
                time.sleep(wait)
            else:
                raise


def _call_anthropic_api(prompt_text, image_path, model_name, api_key):
    """Call the Anthropic Messages API directly. Returns reply text."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError('anthropic package not installed — run: pip install anthropic')

    client = anthropic.Anthropic(api_key=api_key)
    content = []

    if image_path:
        media_type = 'image/jpeg' if image_path.endswith('.jpg') else 'image/png'
        with open(image_path, 'rb') as f:
            img_b64 = base64.standard_b64encode(f.read()).decode()
        content.append({
            'type': 'image',
            'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64},
        })
    content.append({'type': 'text', 'text': prompt_text})

    def _call():
        response = client.messages.create(
            model=model_name,
            max_tokens=4096,
            messages=[{'role': 'user', 'content': content}],
        )
        return response.content[0].text.strip()

    def _is_rate_limit(e):
        return '429' in str(e) or 'rate' in str(e).lower() or 'RateLimitError' in type(e).__name__

    return _with_retry(_call, _is_rate_limit, 'anthropic')


def _call_gemini(prompt_text, image_path, model_name, api_key):
    """Call the Google Generative AI API. Returns reply text."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError('google-generativeai not installed — run: pip install google-generativeai')

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    parts = []
    if image_path:
        mime_type = 'image/jpeg' if image_path.endswith('.jpg') else 'image/png'
        with open(image_path, 'rb') as f:
            parts.append({'mime_type': mime_type, 'data': f.read()})
    parts.append(prompt_text)

    return _with_retry(
        lambda: model.generate_content(parts).text.strip(),
        lambda e: '429' in str(e) or 'quota' in str(e).lower(),
        'gemini',
    )

# ── Skill cache ────────────────────────────────────────────────────

def _skill_cache_path():
    return _HOME / '.figwatch' / 'skill-cache.json'


def _load_skill_cache():
    try:
        with open(_skill_cache_path(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_skill_cache(cache):
    cache_path = _skill_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)


# ── Skill discovery ────────────────────────────────────────────────

def _resolve_builtin_skill(skill_ref):
    """Map 'builtin:tone' -> figwatch/skills/tone/skill.md etc."""
    name = skill_ref.replace('builtin:', '')
    for base in [_BUNDLED_SKILLS, _HOME / '.claude' / 'skills']:
        for fname in ['skill.md', 'SKILL.md']:
            path = base / name / fname
            if path.exists():
                return str(path)
    return None


def _find_skills():
    """Scan for .md skill files across known directories.

    Returns list of {"path": str, "name": str, "builtin": bool}.
    """
    skills = []
    seen = set()

    search_dirs = [
        (_HOME / '.claude' / 'skills', False),
        (Path(os.getcwd()) / '.claude' / 'skills', False),
        (_HOME / '.figwatch' / 'skills', False),
        (_BUNDLED_SKILLS, True),
    ]

    for base_dir, builtin in search_dirs:
        if not base_dir.is_dir():
            continue
        for entry in os.listdir(base_dir):
            skill_dir = base_dir / entry
            if not skill_dir.is_dir():
                continue
            for fname in ['skill.md', 'SKILL.md']:
                skill_path = skill_dir / fname
                if skill_path.exists() and str(skill_path) not in seen:
                    seen.add(str(skill_path))
                    skills.append({
                        'path': str(skill_path),
                        'name': entry,
                        'builtin': builtin,
                    })
                    break

    return skills


# ── Skill introspection ────────────────────────────────────────────

_INTROSPECTION_PROMPT = """You are analysing a skill definition file for compatibility with a Figma comment bot.
The bot can provide these data points to the skill:

Frame-scoped: screenshot, node_tree, text_nodes, prototype_flows, dev_resources, annotations
File-scoped: variables_local, variables_published, styles, components, file_structure

The skill will receive data as file paths or inline JSON, and must produce a plain-text reply
suitable for posting as a Figma comment (no markdown, no file creation).

Read the skill definition below and respond with ONLY a JSON object (no other text):
{
  "comment_compatible": true/false,
  "incompatible_reason": "reason" or null,
  "required_data": ["screenshot", "node_tree", ...]
}

Set comment_compatible=false if the skill requires interactive user input, file creation,
CLI tools, or anything that can't work in a non-interactive single-shot context.

Skill definition:
"""


def introspect_skill(skill_path, claude_path, model=None):
    """Analyse a skill file to determine compatibility and required data.

    Uses the cheapest available model (Haiku / Gemini Flash). Returns dict with
    comment_compatible, incompatible_reason, required_data.
    """
    cache = _load_skill_cache()
    try:
        mtime = os.path.getmtime(skill_path)
    except Exception:
        mtime = 0
    cache_key = f'{skill_path}:{mtime}'

    if cache_key in cache:
        return cache[cache_key]

    safe_default = {
        'comment_compatible': True,
        'incompatible_reason': None,
        'required_data': ['screenshot', 'node_tree'],
    }

    try:
        with open(skill_path, encoding='utf-8') as f:
            skill_content = f.read()
    except Exception:
        return safe_default

    prompt = _INTROSPECTION_PROMPT + skill_content
    provider = _detect_provider(model, claude_path)

    try:
        if provider == 'gemini':
            stdout = _call_gemini(
                prompt, None, _GEMINI_MODELS['gemini-flash'],
                os.environ.get('GOOGLE_API_KEY', ''),
            )
        elif provider == 'anthropic-api':
            stdout = _call_anthropic_api(
                prompt, None, _CLAUDE_API_MODELS['haiku'],
                os.environ.get('ANTHROPIC_API_KEY', ''),
            )
        else:
            result = subprocess.run(
                [claude_path, '--print', '-p', prompt, '--model', 'haiku'],
                capture_output=True, timeout=30, env=subprocess_env(),
                cwd=str(_HOME),
            )
            stdout = result.stdout.decode('utf-8', errors='replace').strip()

        if stdout:
            json_match = re.search(r'\{[^{}]*\}', stdout, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                result_data = {
                    'comment_compatible': bool(parsed.get('comment_compatible', True)),
                    'incompatible_reason': parsed.get('incompatible_reason'),
                    'required_data': parsed.get('required_data', ['screenshot', 'node_tree']),
                }
                cache[cache_key] = result_data
                _save_skill_cache(cache)
                return result_data
    except Exception:
        pass

    return safe_default


# ── Figma data fetching ────────────────────────────────────────────

# Base64 adds ~33% overhead, so cap raw bytes at 3.75 MB to stay under the 5 MB API limit.
_MAX_IMAGE_BYTES = int(3.75 * 1024 * 1024)


def fetch_screenshot(file_key, node_id, pat):
    """Download a screenshot of a Figma node. Returns file path or None.

    Tries progressively smaller PNG scales, then falls back to JPEG (much better
    compression for large frames). Returns None if nothing fits within 3.75 MB
    (the safe ceiling before base64 encoding hits the 5 MB API limit).
    """
    enc_id = urllib_quote(node_id)

    attempts = [
        ('png', 1),
        ('png', 0.5),
        ('jpg', 1),
        ('jpg', 0.5),
        ('jpg', 0.25),
    ]

    for fmt, scale in attempts:
        out_path = os.path.join(
            tempfile.gettempdir(),
            f'figwatch-screenshot-{node_id.replace(":", "-")}.{fmt}',
        )
        try:
            data = figma_get_retry(
                f'/images/{file_key}?ids={enc_id}&scale={scale}&format={fmt}', pat
            )
            if not data or data.get('err') or data.get('status') == 400:
                continue
            url = (data.get('images') or {}).get(node_id)
            if not url:
                continue
            with urllib.request.urlopen(url, timeout=30) as r:
                img_bytes = r.read()
            if len(img_bytes) > _MAX_IMAGE_BYTES:
                continue  # still too large — try next attempt
            with open(out_path, 'wb') as f:
                f.write(img_bytes)
            return out_path
        except Exception:
            continue
    return None


def fetch_node_tree(file_key, node_id, pat):
    """Fetch the full node tree for a Figma node. Returns (file_path, parsed_data) or (None, None)."""
    enc_id = urllib_quote(node_id)
    try:
        data = figma_get_retry(f'/files/{file_key}/nodes?ids={enc_id}&depth=100', pat)
        node = data.get('nodes', {}).get(node_id, {}).get('document') if data else None
        if not node:
            return None, None
        out_path = os.path.join(tempfile.gettempdir(), f'figwatch-tree-{node_id.replace(":", "-")}.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(node, f, indent=2)
        return out_path, node
    except Exception:
        return None, None


def fetch_figma_data(required_data, file_key, node_id, pat):
    """Fetch only the declared data points from Figma API in parallel.

    Returns (dict[data_type -> value], tree_data) where value is a file path or parsed data.
    """
    result = {}
    enc_id = urllib_quote(node_id)
    tree_data = None

    futures = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        if 'screenshot' in required_data:
            futures['screenshot'] = pool.submit(fetch_screenshot, file_key, node_id, pat)
        needs_tree = any(k in required_data for k in ('node_tree', 'text_nodes', 'annotations', 'prototype_flows'))
        if needs_tree:
            futures['_tree'] = pool.submit(fetch_node_tree, file_key, node_id, pat)
        if 'dev_resources' in required_data:
            futures['dev_resources'] = pool.submit(
                figma_get_retry, f'/files/{file_key}/dev_resources?node_ids={enc_id}', pat
            )
        if 'variables_local' in required_data:
            futures['variables_local'] = pool.submit(
                figma_get_retry, f'/files/{file_key}/variables/local', pat
            )
        if 'variables_published' in required_data:
            futures['variables_published'] = pool.submit(
                figma_get_retry, f'/files/{file_key}/variables/published', pat
            )
        if 'styles' in required_data:
            futures['styles'] = pool.submit(
                figma_get_retry, f'/files/{file_key}/styles', pat
            )
        if 'components' in required_data:
            futures['components'] = pool.submit(
                figma_get_retry, f'/files/{file_key}/components', pat
            )
        if 'file_structure' in required_data:
            futures['file_structure'] = pool.submit(
                figma_get_retry, f'/files/{file_key}?depth=2', pat
            )

        for key, future in futures.items():
            try:
                result[key] = future.result()
            except Exception:
                result[key] = None

    tree_result = result.pop('_tree', None)
    if tree_result:
        tree_path, tree_data = tree_result
        result['node_tree'] = tree_path

    if tree_data:
        if 'text_nodes' in required_data:
            from figwatch.watcher import extract_text_from_node
            result['text_nodes'] = extract_text_from_node(tree_data)
        if 'prototype_flows' in required_data:
            result['prototype_flows'] = _extract_prototype_flows(tree_data)
        if 'annotations' in required_data:
            result['annotations'] = _extract_annotations(tree_data)

    return result, tree_data


def _extract_prototype_flows(node):
    flows = []

    def walk(n):
        for reaction in n.get('reactions', []):
            flows.append({
                'node_id': n.get('id'),
                'node_name': n.get('name'),
                'trigger': reaction.get('trigger', {}).get('type'),
                'action': reaction.get('action', {}).get('type'),
                'destination': reaction.get('action', {}).get('destinationId'),
            })
        for child in n.get('children', []):
            walk(child)

    walk(node)
    return flows


def _extract_annotations(node):
    annotations = []

    def walk(n):
        name = (n.get('name') or '').lower()
        if 'annotation' in name or 'note' in name:
            annotations.append({
                'id': n.get('id'),
                'name': n.get('name'),
                'type': n.get('type'),
                'characters': n.get('characters', ''),
            })
        for child in n.get('children', []):
            walk(child)

    walk(node)
    return annotations


# ── Pre-seeded introspection cache for bundled skills ──────────────

_BUILTIN_INTROSPECTION = {
    'builtin:tone': {
        'comment_compatible': True,
        'incompatible_reason': None,
        'required_data': ['node_tree', 'text_nodes'],
    },
    'builtin:ux': {
        'comment_compatible': True,
        'incompatible_reason': None,
        'required_data': ['screenshot', 'node_tree'],
    },
}


def _get_introspection(skill_ref, skill_path, claude_path, model):
    if skill_ref in _BUILTIN_INTROSPECTION:
        return _BUILTIN_INTROSPECTION[skill_ref]
    return introspect_skill(skill_path, claude_path, model)


# ── Prompt builder ─────────────────────────────────────────────────

def _build_prompt(item, skill_content, refs_section, data, tree_data, frame_name, *, inline_files=False):
    """Build the skill execution prompt.

    inline_files=True: node tree JSON is embedded directly (for API providers).
    inline_files=False: file paths are passed and the model reads them (Claude CLI).
    """
    data_desc = []

    if data.get('screenshot'):
        if inline_files:
            data_desc.append('Screenshot: [attached as image]')
        else:
            data_desc.append(f'Screenshot image at: {data["screenshot"]}')

    if data.get('node_tree'):
        if inline_files:
            if tree_data:
                tree_json = json.dumps(tree_data, indent=2)
                if len(tree_json) > _NODE_TREE_CHAR_LIMIT:
                    tree_json = tree_json[:_NODE_TREE_CHAR_LIMIT] + '\n... (truncated)'
                data_desc.append(f'Node tree:\n{tree_json}')
        else:
            data_desc.append(f'Node tree JSON at: {data["node_tree"]}')

    if data.get('text_nodes'):
        text_list = '\n'.join(
            f'  {i+1}. [{t["name"]}]: "{t["text"]}"'
            for i, t in enumerate(data['text_nodes'][:50])
        )
        data_desc.append(f'Text nodes:\n{text_list}')

    for key in ['dev_resources', 'variables_local', 'variables_published',
                'styles', 'components', 'file_structure', 'prototype_flows', 'annotations']:
        if data.get(key):
            data_desc.append(f'{key}: {json.dumps(data[key], indent=2)[:5000]}')

    data_section = '\n\n'.join(data_desc) if data_desc else 'No data available.'
    extra_ctx = f'\nAdditional context from reviewer: "{item.extra}"' if item.extra else ''
    lang_instruction = (
        '\nIMPORTANT: Write your entire reply in Simplified Chinese.'
        if item.reply_lang == 'cn' else ''
    )
    eval_instruction = (
        'Evaluate according to the skill using the data provided, then respond with ONLY a'
        if inline_files else
        'Read any file paths provided, evaluate according to the skill, then respond with ONLY a'
    )

    return f"""You have a skill to evaluate a Figma design. Follow the skill instructions exactly.
Use Mode 3 (Comment Reply) if the skill defines it.

{skill_content}{refs_section}

Now evaluate this screen:
- Frame name: {frame_name}
- Trigger: {item.trigger}{extra_ctx}

Available data:
{data_section}

{eval_instruction}
plain-text comment reply suitable for posting as a Figma comment.

CRITICAL RULES:
- Do NOT create any files. Your entire output IS the comment reply.
- Figma comments are PLAIN TEXT ONLY: no markdown, no asterisks, no hashes, no backticks.
- Keep it CONCISE. The entire reply MUST be under 4000 characters.
- Do NOT add sign-offs — the sign-off is added automatically.
{lang_instruction}"""


# ── Provider-specific runners ──────────────────────────────────────

def _run_via_claude_cli(item, skill_content, refs_section, data, tree_data, frame_name, skill_dir):
    prompt = _build_prompt(item, skill_content, refs_section, data, tree_data, frame_name, inline_files=False)
    cmd = [item.claude_path, '-p', prompt, '--print', '--allowedTools', 'Read', '--model', item.model]
    cmd.extend(['--add-dir', tempfile.gettempdir()])
    cmd.extend(['--add-dir', skill_dir])
    result = subprocess.run(cmd, capture_output=True, timeout=120,
                            env=subprocess_env(), cwd=str(_HOME))
    return parse_claude_output(result)


def _run_via_anthropic_api(item, skill_content, refs_section, data, tree_data, frame_name):
    model_name = _CLAUDE_API_MODELS.get(item.model, item.model)
    prompt = _build_prompt(item, skill_content, refs_section, data, tree_data, frame_name, inline_files=True)
    return _call_anthropic_api(
        prompt, data.get('screenshot'), model_name,
        os.environ.get('ANTHROPIC_API_KEY', ''),
    )


def _run_via_gemini(item, skill_content, refs_section, data, tree_data, frame_name):
    model_name = _GEMINI_MODELS.get(item.model, item.model)
    prompt = _build_prompt(item, skill_content, refs_section, data, tree_data, frame_name, inline_files=True)
    return _call_gemini(
        prompt, data.get('screenshot'), model_name,
        os.environ.get('GOOGLE_API_KEY', ''),
    )


# ── Skill execution ────────────────────────────────────────────────

def execute_skill(item):
    """Execute any skill (builtin or custom) for a WorkItem. Returns the reply string."""
    skill_ref = item.skill_path
    skill_path = skill_ref
    sign_off = 'Gemini' if _detect_provider(item.model, item.claude_path) == 'gemini' else 'Claude'

    if skill_path.startswith('builtin:'):
        resolved = _resolve_builtin_skill(skill_path)
        if not resolved:
            return f'\u26a0\ufe0f Could not find skill: {skill_path}\n\n\u2014 {sign_off}'
        skill_path = resolved

    if not os.path.exists(skill_path):
        return f'\u26a0\ufe0f Skill file not found: {skill_path}\n\n\u2014 {sign_off}'

    intro = _get_introspection(skill_ref, skill_path, item.claude_path, item.model)
    required_data = intro.get('required_data', ['screenshot', 'node_tree'])

    data, tree_data = fetch_figma_data(required_data, item.file_key, item.node_id, item.pat)

    with open(skill_path, encoding='utf-8') as f:
        skill_content = f.read()

    skill_dir = os.path.dirname(skill_path)
    refs_dir = os.path.join(skill_dir, 'references')
    refs_section = ''
    if os.path.isdir(refs_dir):
        ref_parts = []
        for fname in sorted(os.listdir(refs_dir)):
            fpath = os.path.join(refs_dir, fname)
            if os.path.isfile(fpath) and fname.endswith('.md'):
                try:
                    with open(fpath, encoding='utf-8') as f:
                        ref_parts.append(f'--- {fname} ---\n{f.read()}')
                except Exception:
                    pass
        if ref_parts:
            refs_section = '\n\nReference files:\n' + '\n\n'.join(ref_parts)

    frame_name = tree_data.get('name', 'Unknown frame') if tree_data else 'Unknown frame'
    provider = _detect_provider(item.model, item.claude_path)

    try:
        if provider == 'gemini':
            reply = _run_via_gemini(item, skill_content, refs_section, data, tree_data, frame_name)
        elif provider == 'anthropic-api':
            reply = _run_via_anthropic_api(item, skill_content, refs_section, data, tree_data, frame_name)
        else:
            reply = _run_via_claude_cli(item, skill_content, refs_section, data, tree_data, frame_name, skill_dir)

        header = f'\U0001f5e3\ufe0f {item.trigger} Audit \u2014 {frame_name}'
        return f'{header}\n\n{reply}\n\n\u2014 {sign_off}'

    finally:
        for key in ['screenshot', 'node_tree']:
            p = data.get(key)
            if p and isinstance(p, str):
                try:
                    os.unlink(p)
                except Exception:
                    pass
