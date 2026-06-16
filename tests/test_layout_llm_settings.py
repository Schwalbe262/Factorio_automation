from pathlib import Path
import tempfile

from factorio_ai.layout_llm_settings import load_layout_llm_settings, save_layout_llm_settings


def test_layout_llm_settings_default_to_two_jobs(monkeypatch):
    monkeypatch.delenv("FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS", raising=False)
    monkeypatch.delenv("FACTORIO_AI_LAYOUT_LLM_MAX_ACTIVE_TASKS", raising=False)
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = load_layout_llm_settings(Path(temp_dir))

    assert settings["max_active_layout_tasks"] == 2
    assert settings["source"] == "env-default"


def test_layout_llm_settings_save_clamps_and_persists_value():
    with tempfile.TemporaryDirectory() as temp_dir:
        saved = save_layout_llm_settings(Path(temp_dir), "99")
        loaded = load_layout_llm_settings(Path(temp_dir))

    assert saved["max_active_layout_tasks"] == 8
    assert loaded["max_active_layout_tasks"] == 8
    assert loaded["source"] == "runtime"
