"""Generic skill execution handler — replaces hardcoded tone/ux logic."""

import json
import os
import re
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from handlers import (
    strip_markdown, subprocess_env, urllib_quote,
    figma_get_retry, parse_claude_output,
)

_HOME = os.path.expanduser('~')
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_RESOURCES = os.path.join(_THIS_DIR, '..')

# ── Skill cache ────────────────────────────────────────────────────

def _skill_cache_path():
    return os.path.join(_HOME, '.figwatch', 'skill-cache.json')


def _load_skill_cache():
    try:
        with open(_skill_cache_path(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


_figwatch_dir_created = False

def _save_skill_cache(cache):
    global _figwatch_dir_created
    if not _figwatch_dir_created:
        os.makedirs(os.path.join(_HOME, '.figwatch'), exist_ok=True)
        _figwatch_dir_created = True
    with open(_skill_cache_path(), 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)


# ── Skill discovery ────────────────────────────────────────────────

def _resolve_builtin_skill(skill_ref):
    """Map 'builtin:tone' -> bundle/skills/tone/skill.md etc."""
    name = skill_ref.replace('builtin:', '')
    for base in [_RESOURCES, os.path.join(_HOME, '.claude', 'skills')]:
        path = os.path.join(base, 'skills', name, 'skill.md')
        if os.path.exists(path):
            return path
        alt = os.path.join(base, name, 'skill.md')
        if os.path.exists(alt):
            return alt
        alt_upper = os.path.join(base, name, 'SKILL.md')
        if os.path.exists(alt_upper):
            return alt_upper
    return None


def _find_skills():
    """Scan for .md skill files across known directories.

    Returns list of {"path": str, "name": str, "builtin": bool}.
    """
    skills = []
    seen = set()

    search_dirs = [
        (os.path.join(_HOME, '.claude', 'skills'), False),
        (os.path.join(os.getcwd(), '.claude', 'skills'), False),
        (os.path.join(_HOME, '.figwatch', 'skills'), False),
        (os.path.join(_RESOURCES, 'skills'), True),
    ]

    for base_dir, builtin in search_dirs:
        if not os.path.isdir(base_dir):
            continue
        for entry in os.listdir(base_dir):
            skill_dir = os.path.join(base_dir, entry)
            if not os.path.isdir(skill_dir):
                continue
            for fname in ['skill.md', 'SKILL.md']:
                skill_path = os.path.join(skill_dir, fname)
                if os.path.exists(skill_path) and skill_path not in seen:
                    seen.add(skill_path)
                    skills.append({
                        'path': skill_path,
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


def introspect_skill(skill_path, claude_path):
    """Analyse a skill file to determine compatibility and required data.

    Uses a fast Haiku call. Returns dict with comment_compatible, incompatible_reason, required_data.
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

    try:
        result = subprocess.run(
            [claude_path, '--print', '-p', prompt, '--model', 'haiku'],
            capture_output=True, timeout=30, env=subprocess_env(),
            cwd=os.path.expanduser('~'),
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


# ── Figma data fetching (module-level for reuse by ux.py) ──────────

def fetch_screenshot(file_key, node_id, pat):
    """Download a screenshot of a Figma node. Returns file path or None."""
    enc_id = urllib_quote(node_id)
    out_path = os.path.join(tempfile.gettempdir(), f'figwatch-screenshot-{node_id.replace(":", "-")}.png')

    for scale in [2, 1]:
        try:
            data = figma_get_retry(
                f'/images/{file_key}?ids={enc_id}&scale={scale}&format=png', pat
            )
            if not data or data.get('err') or data.get('status') == 400:
                continue
            url = (data.get('images') or {}).get(node_id)
            if not url:
                continue
            with urllib.request.urlopen(url, timeout=30) as r:
                with open(out_path, 'wb') as f:
                    f.write(r.read())
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
    """Fetch only the declared data points from Figma API.

    Returns dict[data_type -> value] where value is a file path or parsed data.
    """
    result = {}
    enc_id = urllib_quote(node_id)
    tree_data = None

    # Parallel fetch for screenshot + node_tree + file-scoped data
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

    # Unpack tree result (path + parsed data to avoid double read)
    tree_result = result.pop('_tree', None)
    if tree_result:
        tree_path, tree_data = tree_result
        result['node_tree'] = tree_path
    else:
        tree_path = None

    # Derived data from node_tree (uses already-parsed data)
    if tree_data:
        if 'text_nodes' in required_data:
            from watcher import extract_text_from_node
            result['text_nodes'] = extract_text_from_node(tree_data)
        if 'prototype_flows' in required_data:
            result['prototype_flows'] = _extract_prototype_flows(tree_data)
        if 'annotations' in required_data:
            result['annotations'] = _extract_annotations(tree_data)

    return result, tree_data


def _extract_prototype_flows(node):
    """Extract prototype flow reactions from a node tree."""
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
    """Extract annotation-like nodes from the tree."""
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


def _get_introspection(skill_ref, skill_path, claude_path):
    """Get introspection for a skill, using pre-seeded data for builtins."""
    if skill_ref in _BUILTIN_INTROSPECTION:
        return _BUILTIN_INTROSPECTION[skill_ref]
    return introspect_skill(skill_path, claude_path)


# ── Skill execution ────────────────────────────────────────────────

def execute_skill(item):
    """Execute any skill (builtin or custom) for a WorkItem. Returns the reply string."""
    skill_ref = item.skill_path
    skill_path = skill_ref

    if skill_path.startswith('builtin:'):
        resolved = _resolve_builtin_skill(skill_path)
        if not resolved:
            return f'\u26a0\ufe0f Could not find skill: {skill_path}\n\n\u2014 Claude'
        skill_path = resolved

    if not os.path.exists(skill_path):
        return f'\u26a0\ufe0f Skill file not found: {skill_path}\n\n\u2014 Claude'

    intro = _get_introspection(skill_ref, skill_path, item.claude_path)
    required_data = intro.get('required_data', ['screenshot', 'node_tree'])

    data, tree_data = fetch_figma_data(required_data, item.file_key, item.node_id, item.pat)

    with open(skill_path, encoding='utf-8') as f:
        skill_content = f.read()

    # Load reference files from the skill's references/ directory
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

    # Build data description
    data_desc = []
    if data.get('screenshot'):
        data_desc.append(f'Screenshot image at: {data["screenshot"]}')
    if data.get('node_tree'):
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
    locale_ctx = f'\n- Locale: {item.locale}' if item.locale else ''

    lang_instruction = ''
    if item.reply_lang == 'cn':
        lang_instruction = '\nIMPORTANT: Write your entire reply in Simplified Chinese.'

    prompt = f"""You have a skill to evaluate a Figma design. Follow the skill instructions exactly.
Use Mode 3 (Comment Reply) if the skill defines it.

{skill_content}{refs_section}

Now evaluate this screen:
- Frame name: {frame_name}
- Trigger: {item.trigger}{locale_ctx}{extra_ctx}

Available data:
{data_section}

Read any file paths provided, evaluate according to the skill, then respond with ONLY a
plain-text comment reply suitable for posting as a Figma comment.

CRITICAL RULES:
- Do NOT create any files. Your entire output IS the comment reply.
- Figma comments are PLAIN TEXT ONLY: no markdown, no asterisks, no hashes, no backticks.
- Keep it CONCISE. The entire reply MUST be under 4000 characters.
- Do NOT add sign-offs — the sign-off is added automatically.
{lang_instruction}"""

    cmd = [item.claude_path, '-p', prompt, '--print', '--allowedTools', 'Read', '--model', item.model]
    tmp_dir = tempfile.gettempdir()
    cmd.extend(['--add-dir', tmp_dir])

    # Grant access to the skill's directory so Claude can read reference files directly
    cmd.extend(['--add-dir', skill_dir])

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120, env=subprocess_env(),
                                    cwd=os.path.expanduser('~'))
        reply = parse_claude_output(result)
        header = f'\U0001f5e3\ufe0f Claude {item.trigger} Audit \u2014 {frame_name}'
        return f'{header}\n\n{reply}\n\n\u2014 Claude'

    except Exception as e:
        return f'\U0001f5e3\ufe0f Claude {item.trigger} Audit\n\n\u26a0\ufe0f Evaluation failed: {e}\n\n\u2014 Claude'
    finally:
        for key in ['screenshot', 'node_tree']:
            p = data.get(key)
            if p and isinstance(p, str):
                try:
                    os.unlink(p)
                except Exception:
                    pass
