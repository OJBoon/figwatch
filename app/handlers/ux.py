"""UX heuristic evaluation handler — responds to @ux comments."""

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_PATH = os.path.join(_THIS_DIR, '..', 'skills', 'ux', 'skill.md')
_HEURISTICS_PATH = os.path.join(_THIS_DIR, '..', 'skills', 'ux', 'references', 'nielsen-heuristics.md')

_skill_cache = None
_heuristics_cache = None


def _load(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def _get_skill():
    global _skill_cache
    if _skill_cache is None:
        _skill_cache = _load(_SKILL_PATH) or ''
    return _skill_cache


def _get_heuristics():
    global _heuristics_cache
    if _heuristics_cache is None:
        _heuristics_cache = _load(_HEURISTICS_PATH) or ''
    return _heuristics_cache


def _strip_markdown(text):
    import re
    text = re.sub(r'\*\*', '', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*•]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _figma_get(path, pat):
    import urllib.request
    try:
        req = urllib.request.Request(
            f'https://api.figma.com/v1{path}',
            headers={'X-Figma-Token': pat}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _resolve_parent_frame(file_key, node_id, pat):
    node_data = _figma_get(f'/files/{file_key}/nodes?ids={urllib_quote(node_id)}', pat)
    node = node_data.get('nodes', {}).get(node_id, {}).get('document') if node_data else None
    if not node:
        return None

    if node.get('type') in ('FRAME', 'COMPONENT', 'COMPONENT_SET'):
        return {'id': node['id'], 'name': node.get('name', ''), 'type': node['type']}

    file_data = _figma_get(f'/files/{file_key}?depth=2', pat)
    if file_data and file_data.get('document', {}).get('children'):
        for page in file_data['document']['children']:
            for frame in page.get('children', []):
                if frame.get('id') == node_id:
                    return {'id': frame['id'], 'name': frame.get('name', ''), 'type': frame.get('type', '')}

    return {'id': node['id'], 'name': node.get('name', ''), 'type': node.get('type', '')}


def urllib_quote(s):
    import urllib.parse
    return urllib.parse.quote(s, safe='')


def _get_screenshot(file_key, frame_id, pat):
    out_path = os.path.join(tempfile.gettempdir(), f'figwatch-screenshot-{frame_id.replace(":", "-")}.png')

    for scale in [2, 1]:
        try:
            data = _figma_get(
                f'/images/{file_key}?ids={urllib_quote(frame_id)}&scale={scale}&format=png', pat
            )
            if not data or data.get('err') or data.get('status') == 400:
                continue
            url = (data.get('images') or {}).get(frame_id)
            if not url:
                continue

            import urllib.request
            with urllib.request.urlopen(url, timeout=30) as r:
                with open(out_path, 'wb') as f:
                    f.write(r.read())
            return out_path
        except Exception:
            continue

    # Fallback: figma-ds-cli (optional, handles large frames)
    try:
        subprocess.run(
            ['figma-ds-cli', 'export', 'node', frame_id, '-s', '2', '-f', 'png', '-o', out_path],
            capture_output=True, timeout=30
        )
        if os.path.exists(out_path):
            return out_path
    except Exception:
        pass

    return None


def _get_node_tree(file_key, frame_id, pat):
    try:
        data = _figma_get(
            f'/files/{file_key}/nodes?ids={urllib_quote(frame_id)}&depth=100', pat
        )
        node = data.get('nodes', {}).get(frame_id, {}).get('document') if data else None
        if not node:
            return None
        out_path = os.path.join(tempfile.gettempdir(), f'figwatch-tree-{frame_id.replace(":", "-")}.json')
        with open(out_path, 'w') as f:
            json.dump(node, f, indent=2)
        return out_path
    except Exception:
        return None


def ux_handler(*, node_id, file_key, pat, extra, claude_path, **_):
    # Phase 1: Resolve parent frame
    frame = _resolve_parent_frame(file_key, node_id, pat)
    if not frame:
        return '\U0001f5e3\ufe0f Claude UX Audit\n\n\u26a0\ufe0f Could not locate the commented frame.\n\n\u2014 Claude'

    frame_id = frame['id']
    screen_name = frame.get('name') or 'Unnamed screen'

    # Phase 2+3: Screenshot and node tree in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        ss_future = pool.submit(_get_screenshot, file_key, frame_id, pat)
        tree_future = pool.submit(_get_node_tree, file_key, frame_id, pat)
        screenshot_path = ss_future.result()
        tree_path = tree_future.result()

    if not screenshot_path and not tree_path:
        return '\U0001f5e3\ufe0f Claude UX Audit\n\n\u26a0\ufe0f Could not retrieve design data from Figma API.\n\n\u2014 Claude'

    # Phase 4: Build prompt
    skill = _get_skill()
    heuristics = _get_heuristics()

    data_instructions = ''
    if screenshot_path:
        data_instructions += f'\nRead the screenshot image at: {screenshot_path}'
    if tree_path:
        data_instructions += f'\nRead the node tree JSON at: {tree_path}'
    if not screenshot_path:
        data_instructions += '\n\nNote: Screenshot unavailable. Evaluate using node tree only.'
    if not tree_path:
        data_instructions += '\n\nNote: Node tree unavailable. Evaluate using screenshot only.'

    prompt = f'''You have a skill for heuristic evaluation. Follow the skill instructions exactly.

{skill}

Here are the detailed heuristic evaluation criteria:
{heuristics}

Now evaluate this screen:
- screenName: {screen_name}
- screenshotPath: {screenshot_path or "N/A"}
- treePath: {tree_path or "N/A"}
{f'- Additional context from reviewer: "{extra}"' if extra else ""}

{data_instructions}

Read the data sources, evaluate all 10 heuristics, then respond with ONLY the comment reply as specified by the output format. No preamble, no markdown, no explanation — just the formatted reply.'''

    try:
        result = subprocess.run(
            [claude_path, '-p', prompt, '--print', '--allowedTools', 'Read,Bash'],
            capture_output=True, timeout=120
        )
        reply = _strip_markdown(result.stdout.decode('utf-8', errors='replace').strip() or 'Unable to generate evaluation.')
        return f'\U0001f5e3\ufe0f Claude UX Audit \u2014 {screen_name}\n\n{reply}\n\n\u2014 Claude'
    except Exception:
        # Fallback: inline tree data (no image)
        try:
            tree_data = _load(tree_path) if tree_path else None
            fallback_prompt = prompt
            if tree_data:
                fallback_prompt += f'\n\nNODE TREE JSON:\n{tree_data[:50000]}'

            result = subprocess.run(
                [claude_path, '--print', '-p', fallback_prompt],
                capture_output=True, timeout=120
            )
            reply = _strip_markdown(result.stdout.decode('utf-8', errors='replace').strip() or 'Unable to generate evaluation.')
            return f'\U0001f5e3\ufe0f Claude UX Audit \u2014 {screen_name}\n\n{reply}\n\nNote: Visual analysis limited\n\n\u2014 Claude'
        except Exception as e:
            return f'\U0001f5e3\ufe0f Claude UX Audit\n\n\u26a0\ufe0f Evaluation failed: {e}\n\n\u2014 Claude'
    finally:
        for p in [screenshot_path, tree_path]:
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass
