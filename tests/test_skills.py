"""Tests for figwatch.skills — prompt building and skill discovery."""

import os

from figwatch.domain import Audit, Comment, ThreadMessage, Trigger, TriggerMatch
from figwatch.services import AuditConfig
from figwatch.skills import (
    _build_prompt,
    _build_refine_prompt,
    _resolve_builtin_skill,
    find_skills,
)

# ── Helpers ───────────────────────────────────────────────────────────

def _make_audit(**kwargs):
    defaults = dict(
        trigger_keyword="@ux", skill_ref="builtin:ux",
        extra="", reply_lang="en", thread_history=(),
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
        thread_history=defaults["thread_history"],
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


def test_build_prompt_shows_text_node_beyond_first_50():
    # Regression: a placeholder deep in a content-heavy page (node #90 of 158)
    # was dropped by the old first-50 cap, so audits missed it. It must now appear.
    audit = _make_audit()
    nodes = [{"name": f"n{i}", "text": f"label {i}"} for i in range(158)]
    nodes[90] = {"name": "Subtitle", "text": "Subtitle subtitle subtitle subtitle"}
    prompt = _build_prompt(audit, SKILL_CONTENT, REFS, {"text_nodes": nodes}, None,
                           FRAME_NAME, inline_files=True, config=_make_config())
    assert "Subtitle subtitle subtitle subtitle" in prompt


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


# ── execute_skill journey vs single-screen ────────────────────────────

import figwatch.skills as _skills  # noqa: E402


def _screen_child(cid, name, x, w=400, h=800):
    return {"id": cid, "name": name, "type": "FRAME",
            "absoluteBoundingBox": {"x": x, "y": 0, "width": w, "height": h}}


class _FakeRepo:
    def __init__(self, tree, journey_screens=None, screenshot=None, node_name=None):
        self._tree = tree
        self._journey = journey_screens or []
        self._shot = screenshot
        self._node_name = node_name
        self.calls = []
        self.name_calls = []

    def fetch(self, required_data, file_key, node_id):
        req = list(required_data)
        self.calls.append(req)
        data = {}
        if "node_tree" in req:
            data["node_tree"] = "/tmp/figwatch-test-tree.json"
        if "screenshot" in req:
            data["screenshot"] = self._shot
        return data, (self._tree if "node_tree" in req else None)

    def fetch_journey_screenshots(self, file_key, node_id, tree_data):
        return list(self._journey)

    def fetch_node_name(self, file_key, node_id):
        self.name_calls.append((file_key, node_id))
        return self._node_name


class _FakeProvider:
    inline_files = True
    model_id = "test-model"

    def __init__(self):
        self.calls = []

    def call(self, prompt, image_arg):
        self.calls.append((prompt, image_arg))
        return "REVIEW-OK"


def test_execute_skill_journey_multi_screen(monkeypatch):
    prov = _FakeProvider()
    monkeypatch.setattr(_skills, "make_provider", lambda *a, **k: prov)
    tree = {"name": "SONY Journey", "type": "FRAME", "children": [
        _screen_child("1:1", "Home", 0), _screen_child("1:2", "PDP", 500),
        _screen_child("1:3", "Cart", 1000)]}
    screens = [{"id": "1:1", "name": "Home", "path": "/tmp/s1.png"},
               {"id": "1:2", "name": "PDP", "path": "/tmp/s2.png"},
               {"id": "1:3", "name": "Cart", "path": "/tmp/s3.png"}]
    repo = _FakeRepo(tree, journey_screens=screens)
    result = _skills.execute_skill(_make_audit(), config=_make_config(), design_repo=repo)
    assert "Journey Audit" in result and "(3 screens)" in result
    assert "REVIEW-OK" in result
    _, image_arg = prov.calls[0]
    assert image_arg == ["/tmp/s1.png", "/tmp/s2.png", "/tmp/s3.png"]
    # journey skips the doomed whole-board render — never fetched 'screenshot'
    assert all("screenshot" not in c for c in repo.calls)


def test_execute_skill_single_screen_unchanged(monkeypatch):
    prov = _FakeProvider()
    monkeypatch.setattr(_skills, "make_provider", lambda *a, **k: prov)
    tree = {"name": "Home", "type": "FRAME", "children": []}  # not a journey
    repo = _FakeRepo(tree, screenshot="/tmp/shot.png")
    result = _skills.execute_skill(_make_audit(), config=_make_config(), design_repo=repo)
    assert "Journey Audit" not in result
    assert "Audit" in result
    _, image_arg = prov.calls[0]
    assert image_arg == "/tmp/shot.png"  # a single str, not a list
    assert any("screenshot" in c for c in repo.calls)


def test_execute_skill_explicit_journey_keyword(monkeypatch):
    prov = _FakeProvider()
    monkeypatch.setattr(_skills, "make_provider", lambda *a, **k: prov)
    # Only one screen → not auto-detected, but the keyword forces journey mode.
    tree = {"name": "Board", "type": "FRAME", "children": [_screen_child("1:1", "A", 0)]}
    repo = _FakeRepo(tree, journey_screens=[{"id": "1:1", "name": "A", "path": "/tmp/a.png"}])
    audit = _make_audit(extra="journey please")
    result = _skills.execute_skill(audit, config=_make_config(), design_repo=repo)
    assert "Journey Audit" in result


def test_execute_skill_resolves_name_without_tree(monkeypatch):
    # A skill that doesn't request node_tree (e.g. a report generator) leaves
    # tree_data None; the header must still name the node (SECTION included) via
    # the fetch_node_name fallback rather than reading "Unknown frame".
    prov = _FakeProvider()
    monkeypatch.setattr(_skills, "make_provider", lambda *a, **k: prov)
    monkeypatch.setattr(_skills, "_get_introspection",
                        lambda *a, **k: {"required_data": ["screenshot"]})
    repo = _FakeRepo(tree=None, screenshot="/tmp/shot.png", node_name="Sony SiS demo")
    result = _skills.execute_skill(_make_audit(), config=_make_config(), design_repo=repo)
    assert "Sony SiS demo" in result
    assert "Unknown frame" not in result
    assert repo.name_calls == [("abc", "2:3")]


# ── Refinement / follow-up ────────────────────────────────────────────

_HISTORY = (
    ThreadMessage(is_bot=False, author="alice", text="@ux please review"),
    ThreadMessage(is_bot=True, author="FigWatch", text="Contrast is low on the CTA."),
)


def test_build_refine_prompt_includes_transcript_and_latest():
    audit = _make_audit(extra="focus on the CTA copy", thread_history=_HISTORY)
    data = {"screenshot": "/tmp/s.png", "node_tree": "/tmp/t.json"}
    prompt = _build_refine_prompt(audit, SKILL_CONTENT, REFS, data, TREE_DATA, FRAME_NAME,
                                  inline_files=True, config=_make_config())
    assert "FOLLOW-UP" in prompt
    assert "focus on the CTA copy" in prompt          # latest message
    assert "Contrast is low on the CTA." in prompt     # prior bot turn
    assert "FigWatch:" in prompt and "alice:" in prompt
    assert "Do NOT repeat the whole audit" in prompt


def test_execute_skill_refinement_header_and_prompt(monkeypatch):
    prov = _FakeProvider()
    monkeypatch.setattr(_skills, "make_provider", lambda *a, **k: prov)
    tree = {"name": "Home", "type": "FRAME", "children": []}
    repo = _FakeRepo(tree, screenshot="/tmp/shot.png")
    audit = _make_audit(extra="also check error states", thread_history=_HISTORY)
    result = _skills.execute_skill(audit, config=_make_config(), design_repo=repo)
    assert "Follow-up" in result and "Audit —" not in result
    prompt, image_arg = prov.calls[0]
    assert "also check error states" in prompt
    assert "Contrast is low on the CTA." in prompt   # thread transcript threaded in
    assert image_arg == "/tmp/shot.png"              # single-screen context, re-fetched


