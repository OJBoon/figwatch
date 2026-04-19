"""Tests for figwatch.skills — prompt building and skill discovery."""

import json
import os
import pytest

from figwatch.domain import Audit, Comment, Trigger, TriggerMatch
from figwatch.services import AuditConfig
from figwatch.skills import _build_prompt, find_skills, _resolve_builtin_skill


# ── Helpers ───────────────────────────────────────────────────────────

def _make_audit(**kwargs):
    defaults = dict(
        trigger_keyword="@ux", skill_ref="builtin:ux",
        extra="", reply_lang="en",
    )
    defaults.update(kwargs)
    return Audit(
        audit_id="test-1",
        comment=Comment(
            comment_id="1", message="@ux check", parent_id="1",
            node_id="2:3", user_handle="alice", file_key="abc",
        ),
        trigger_match=TriggerMatch(
            trigger=Trigger(
                keyword=defaults["trigger_keyword"],
                skill_ref=defaults["skill_ref"],
            ),
            extra=defaults["extra"],
        ),
    )


def _make_config(**kwargs):
    defaults = dict(model="gemini-flash", claude_path="api",
                    reply_lang="en", locale="uk")
    defaults.update(kwargs)
    return AuditConfig(**defaults)


SKILL_CONTENT = "# UX Skill\nEvaluate usability."
REFS = ""
TREE_DATA = {"name": "Home Screen", "type": "FRAME", "children": []}
FRAME_NAME = "Home Screen"


# ── _build_prompt ─────────────────────────────────────────────────────

def test_build_prompt_contains_skill_content():
    audit = _make_audit()
    config = _make_config()
    data = {"screenshot": "/tmp/shot.png", "node_tree": "/tmp/tree.json"}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, TREE_DATA, FRAME_NAME,
                           inline_files=True, config=config)
    assert SKILL_CONTENT in prompt


def test_build_prompt_inline_screenshot():
    audit = _make_audit()
    config = _make_config()
    data = {"screenshot": "/tmp/shot.png"}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, TREE_DATA, FRAME_NAME,
                           inline_files=True, config=config)
    assert "attached as image" in prompt
    assert "/tmp/shot.png" not in prompt


def test_build_prompt_file_path_screenshot():
    audit = _make_audit()
    config = _make_config()
    data = {"screenshot": "/tmp/shot.png"}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, TREE_DATA, FRAME_NAME,
                           inline_files=False, config=config)
    assert "/tmp/shot.png" in prompt
    assert "attached as image" not in prompt


def test_build_prompt_inline_tree_data_embedded():
    audit = _make_audit()
    config = _make_config()
    data = {"node_tree": "/tmp/tree.json"}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, TREE_DATA, FRAME_NAME,
                           inline_files=True, config=config)
    assert "Home Screen" in prompt


def test_build_prompt_file_path_tree_passes_path():
    audit = _make_audit()
    config = _make_config()
    data = {"node_tree": "/tmp/tree.json"}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, TREE_DATA, FRAME_NAME,
                           inline_files=False, config=config)
    assert "/tmp/tree.json" in prompt


def test_build_prompt_contains_frame_name():
    audit = _make_audit()
    config = _make_config()
    data = {}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, None, "Checkout Flow",
                           inline_files=True, config=config)
    assert "Checkout Flow" in prompt


def test_build_prompt_contains_trigger():
    audit = _make_audit(trigger_keyword="@tone")
    config = _make_config()
    data = {}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, None, FRAME_NAME,
                           inline_files=True, config=config)
    assert "@tone" in prompt


def test_build_prompt_includes_extra_context():
    audit = _make_audit(extra="focus on the button colour")
    config = _make_config()
    data = {}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, None, FRAME_NAME,
                           inline_files=True, config=config)
    assert "focus on the button colour" in prompt


def test_build_prompt_no_extra_context_when_empty():
    audit = _make_audit(extra="")
    config = _make_config()
    data = {}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, None, FRAME_NAME,
                           inline_files=True, config=config)
    assert "Additional context" not in prompt


def test_build_prompt_chinese_instruction():
    audit = _make_audit()
    config = _make_config(reply_lang="cn")
    data = {}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, None, FRAME_NAME,
                           inline_files=True, config=config)
    assert "Simplified Chinese" in prompt


def test_build_prompt_no_chinese_instruction_by_default():
    audit = _make_audit()
    config = _make_config(reply_lang="en")
    data = {}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, None, FRAME_NAME,
                           inline_files=True, config=config)
    assert "Simplified Chinese" not in prompt


def test_build_prompt_tree_truncated_when_large(monkeypatch):
    import figwatch.skills as skills_mod
    monkeypatch.setattr(skills_mod, "_NODE_TREE_CHAR_LIMIT", 10)
    audit = _make_audit()
    config = _make_config()
    big_tree = {"name": "Frame", "data": "x" * 100}
    data = {"node_tree": "/tmp/tree.json"}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, big_tree, FRAME_NAME,
                           inline_files=True, config=config)
    assert "truncated" in prompt


def test_build_prompt_text_nodes_listed():
    audit = _make_audit()
    config = _make_config()
    data = {
        "text_nodes": [
            {"name": "Heading", "text": "Welcome back"},
            {"name": "Body", "text": "Here is your dashboard"},
        ]
    }
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, data, None, FRAME_NAME,
                           inline_files=True, config=config)
    assert "Welcome back" in prompt
    assert "Here is your dashboard" in prompt


# ── find_skills ───────────────────────────────────────────────────────

def test_find_skills_returns_list():
    skills = find_skills()
    assert isinstance(skills, list)


def test_find_skills_bundled_included():
    skills = find_skills()
    names = {s["name"] for s in skills}
    assert "ux" in names or "tone" in names


def test_find_skills_builtin_flag():
    skills = find_skills()
    builtins = [s for s in skills if s["builtin"]]
    assert len(builtins) > 0


# ── _resolve_builtin_skill ────────────────────────────────────────────

def test_resolve_builtin_skill_ux():
    path = _resolve_builtin_skill("builtin:ux")
    assert path is not None
    assert os.path.exists(path)
    assert path.endswith(".md")


def test_resolve_builtin_skill_tone():
    path = _resolve_builtin_skill("builtin:tone")
    assert path is not None
    assert os.path.exists(path)


def test_resolve_builtin_skill_missing():
    path = _resolve_builtin_skill("builtin:nonexistent")
    assert path is None
