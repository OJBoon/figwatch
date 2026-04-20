"""Skill discovery, introspection, prompt building, and execution."""

import json
import logging
import os
import re
from pathlib import Path

from figwatch.providers.ai import make_provider, GEMINI_MODELS, CLAUDE_API_MODELS
from figwatch.providers.ai.gemini import GeminiProvider
from figwatch.providers.ai.anthropic import AnthropicProvider
from figwatch.providers.ai.claude_cli import ClaudeCLIProvider

logger = logging.getLogger(__name__)

_HOME = Path.home()
_BUNDLED_SKILLS = Path(__file__).parent / 'skills'

# Node tree is embedded inline for API providers — cap to avoid token limit blowout.
_NODE_TREE_CHAR_LIMIT = 40_000


# ── Skill cache ──────────────────────────────────────────────────────

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


# ── Skill discovery ──────────────────────────────────────────────────

def find_skills():
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


def _resolve_builtin_skill(skill_ref):
    name = skill_ref.replace('builtin:', '')
    for base in [_BUNDLED_SKILLS, _HOME / '.claude' / 'skills']:
        for fname in ['skill.md', 'SKILL.md']:
            path = base / name / fname
            if path.exists():
                return str(path)
    return None


# ── Skill introspection ──────────────────────────────────────────────

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

    # Always use the cheapest model for introspection.
    if (model or '').startswith('gemini'):
        provider = GeminiProvider(GEMINI_MODELS['gemini-flash'], os.environ.get('GOOGLE_API_KEY', ''))
    elif claude_path == 'api':
        provider = AnthropicProvider(CLAUDE_API_MODELS['haiku'], os.environ.get('ANTHROPIC_API_KEY', ''))
    else:
        provider = ClaudeCLIProvider('haiku', claude_path)

    try:
        stdout = provider.call(prompt, None)
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


def _get_introspection(skill_ref, skill_path, claude_path, model):
    if skill_ref in _BUILTIN_INTROSPECTION:
        return _BUILTIN_INTROSPECTION[skill_ref]
    return introspect_skill(skill_path, claude_path, model)


# ── Prompt builder ────────────────────────────────────────────────────

def _build_prompt(audit, skill_content, refs_section, data, tree_data, frame_name, *, inline_files, config=None):
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
        if inline_files and tree_data:
            tree_json = json.dumps(tree_data, indent=2)
            if len(tree_json) > _NODE_TREE_CHAR_LIMIT:
                tree_json = tree_json[:_NODE_TREE_CHAR_LIMIT] + '\n... (truncated)'
            data_desc.append(f'Node tree:\n{tree_json}')
        elif not inline_files:
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

    extra = audit.trigger_match.extra
    trigger_kw = audit.trigger_match.trigger.keyword
    reply_lang = config.reply_lang if config else ''

    extra_ctx = f'\nAdditional context from reviewer: "{extra}"' if extra else ''
    lang_instruction = (
        '\nIMPORTANT: Write your entire reply in Simplified Chinese.'
        if reply_lang == 'cn' else ''
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
- Trigger: {trigger_kw}{extra_ctx}

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


# ── Skill execution ───────────────────────────────────────────────────

def execute_skill(audit, *, config, design_repo):
    """Execute any skill (builtin or custom) for an Audit. Returns the reply string."""
    skill_ref = audit.trigger_match.trigger.skill_ref
    file_key = audit.comment.file_key
    node_id = audit.comment.node_id
    trigger_kw = audit.trigger_match.trigger.keyword
    model = config.model
    claude_path = config.claude_path

    skill_path = skill_ref

    if skill_path.startswith('builtin:'):
        resolved = _resolve_builtin_skill(skill_path)
        if not resolved:
            raise FileNotFoundError(f'Could not find skill: {skill_path}')
        skill_path = resolved

    if not os.path.exists(skill_path):
        raise FileNotFoundError(f'Skill file not found: {skill_path}')

    intro = _get_introspection(skill_ref, skill_path, claude_path, model)
    required_data = intro.get('required_data', ['screenshot', 'node_tree'])
    logger.debug(
        'fetching figma data',
        extra={'required': ','.join(required_data)},
    )

    data, tree_data = design_repo.fetch(required_data, file_key, node_id)

    with open(skill_path, encoding='utf-8') as f:
        skill_content = f.read()

    skill_dir = os.path.dirname(skill_path)
    refs_section = ''
    refs_dir = os.path.join(skill_dir, 'references')
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
    provider = make_provider(model, claude_path, skill_dir=skill_dir)
    logger.debug(
        'calling ai provider',
        extra={'provider': provider.model_id, 'frame': frame_name},
    )
    prompt = _build_prompt(
        audit, skill_content, refs_section, data, tree_data, frame_name,
        inline_files=provider.inline_files, config=config,
    )

    try:
        reply = provider.call(prompt, data.get('screenshot'))
        header = f'\U0001f5e3\ufe0f {trigger_kw} Audit \u2014 {frame_name}'
        return f'{header}\n\n{reply}\n\n\u2014 {provider.model_id}'
    finally:
        for key in ['screenshot', 'node_tree']:
            p = data.get(key)
            if p and isinstance(p, str):
                try:
                    os.unlink(p)
                except Exception:
                    pass
