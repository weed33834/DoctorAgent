"""Tests for the application settings dialog."""

# mypy: ignore-errors

# ruff: noqa: N802

import sys
from pathlib import Path

import pytest

from doctoragent.config import AegisConfig

from .presentation_stubs import (
    FakeApplication,
    FakeFileDialog,
    FakeMessageBox,
    install_presentation_stubs,
    restore_modules,
)


@pytest.fixture
def dialog_qt_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace PyQt6 widgets with stubs for dialog tests."""
    saved = install_presentation_stubs()
    FakeApplication._instance = None
    FakeMessageBox._last_warning = None
    FakeMessageBox._last_information = None
    FakeMessageBox._last_question = None
    FakeFileDialog.next_directory = ""
    yield
    FakeApplication._instance = None
    FakeFileDialog.next_directory = ""
    restore_modules(saved)


@pytest.fixture
def config(tmp_path: Path) -> AegisConfig:
    """Test configuration with isolated paths."""
    cfg = AegisConfig()
    cfg.paths.inbox = tmp_path / "Inbox"
    cfg.paths.vault = tmp_path / "Vault"
    cfg.paths.index = tmp_path / "Index"
    cfg.paths.settings = tmp_path / "settings.json"
    cfg.security.master_key_provider = "FilePassword"
    cfg.security.enable_semantic_search = False
    cfg.security.windows_hello_enabled = False
    cfg.model.base_url = "http://127.0.0.1:11434/v1"
    cfg.model.model_name = "qwen2.5:7b"
    cfg.model.ctx_size = 32768
    cfg.model.temperature = 0.3
    cfg.model.timeout = 120.0
    return cfg


def test_settings_dialog_loads_config_values(dialog_qt_stubs: None, config: AegisConfig) -> None:
    """SettingsDialog pre-populates fields from the provided config."""
    from doctoragent.presentation.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config)

    assert dialog.master_key_combo.currentText() == "FilePassword"
    assert dialog.semantic_search_check.isChecked() is False
    assert dialog.inbox_label.text == str(config.paths.inbox)
    assert dialog.model_url.text() == "http://127.0.0.1:11434/v1"
    assert dialog.model_ctx.text() == "32768"


def test_settings_dialog_saves_changes(dialog_qt_stubs: None, config: AegisConfig) -> None:
    """Accepting the dialog with changes persists the config and prompts restart."""
    from doctoragent.presentation.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config)
    dialog.master_key_combo.setCurrentText("TPM")
    dialog.semantic_search_check.setChecked(True)
    dialog.model_name.setText("llama3")
    dialog.model_ctx.setText("8192")
    dialog.model_temp.setText("0.7")
    dialog.model_timeout.setText("60")

    dialog.accept()

    assert config.security.master_key_provider == "TPM"
    assert config.security.enable_semantic_search is True
    assert config.model.model_name == "llama3"
    assert config.model.ctx_size == 8192
    assert config.paths.settings.exists()
    saved = config.paths.settings.read_text(encoding="utf-8")
    assert "TPM" in saved
    assert "llama3" in saved
    assert FakeMessageBox._last_information is not None
    assert FakeMessageBox._last_information[1] == "Restart Required"


def test_settings_dialog_no_changes_no_restart_prompt(
    dialog_qt_stubs: None, config: AegisConfig
) -> None:
    """Accepting without modifying fields does not show a restart prompt."""
    from doctoragent.presentation.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config)
    dialog.accept()

    assert FakeMessageBox._last_information is None


def test_settings_dialog_invalid_numeric_shows_warning(
    dialog_qt_stubs: None, config: AegisConfig
) -> None:
    """Non-numeric model settings show a validation warning and do not save."""
    from doctoragent.presentation.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config)
    dialog.model_ctx.setText("not-a-number")
    dialog.accept()

    assert FakeMessageBox._last_warning is not None
    assert FakeMessageBox._last_warning[1] == "Validation"
    assert not config.paths.settings.exists()


def test_settings_dialog_browse_updates_path(dialog_qt_stubs: None, config: AegisConfig) -> None:
    """Clicking Browse updates the corresponding path label."""
    from doctoragent.presentation.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config)
    FakeFileDialog.next_directory = "/tmp/new_inbox"
    dialog._browse_inbox()

    assert dialog.inbox_label.text == "/tmp/new_inbox"


def test_settings_dialog_windows_hello_visibility(
    dialog_qt_stubs: None, config: AegisConfig
) -> None:
    """Windows Hello checkbox is only visible on Windows."""
    from doctoragent.presentation.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config)
    assert dialog.windows_hello_check.isChecked() is False
    assert dialog.windows_hello_check.isVisible() == (sys.platform == "win32")
