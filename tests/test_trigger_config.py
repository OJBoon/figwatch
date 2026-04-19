"""Tests for figwatch.trigger_config — config loading and custom skill discovery."""

import json

from figwatch.trigger_config import (
    DEFAULT_TRIGGERS,
    _discover_custom_triggers,
    load_trigger_config,
)


# ── load_trigger_config ───────────────────────────────────────────────

def test_load_trigger_config_defaults_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    config = load_trigger_config()
    assert config == DEFAULT_TRIGGERS


def test_load_trigger_config_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    figwatch_dir = tmp_path / ".figwatch"
    figwatch_dir.mkdir()
    config_data = {"triggers": [{"trigger": "@a11y", "skill": "/skills/a11y.md"}]}
    (figwatch_dir / "config.json").write_text(json.dumps(config_data))

    config = load_trigger_config()
    assert any(t["trigger"] == "@a11y" for t in config)


def test_load_trigger_config_discovers_custom_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    custom_dir = tmp_path / "custom-skills"
    custom_dir.mkdir()
    (custom_dir / "brand.md").write_text("# Brand skill")

    config = load_trigger_config()
    assert any(t["trigger"] == "@brand" for t in config)


def test_load_trigger_config_custom_skill_subdir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / "custom-skills" / "motion"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text("# Motion skill")

    config = load_trigger_config()
    assert any(t["trigger"] == "@motion" for t in config)


# ── FIGWATCH_SKILLS_DIR ─────────────────────────────────────────────

def test_discover_custom_triggers_explicit_dir(tmp_path):
    skills = tmp_path / "my-skills"
    skills.mkdir()
    (skills / "perf.md").write_text("# Perf skill")

    triggers = _discover_custom_triggers(str(skills))
    assert any(t["trigger"] == "@perf" for t in triggers)


def test_load_trigger_config_with_skills_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    skills = tmp_path / "my-skills"
    skills.mkdir()
    (skills / "perf.md").write_text("# Perf skill")

    config = load_trigger_config(skills_dir=str(skills))
    assert any(t["trigger"] == "@perf" for t in config)


def test_discover_custom_triggers_explicit_dir_missing(tmp_path):
    """Non-existent explicit dir returns empty (caller validates at startup)."""
    triggers = _discover_custom_triggers(str(tmp_path / "nope"))
    assert triggers == []
