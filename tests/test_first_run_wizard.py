"""Tests for the first-run setup wizard."""

# mypy: ignore-errors

# ruff: noqa: N802

from pathlib import Path

import pytest

from doctoragent.config import AegisConfig

from .presentation_stubs import (
    FakeApplication,
    FakeFileDialog,
    FakeMessageBox,
    FakeWizard,
    install_presentation_stubs,
    restore_modules,
)

pytestmark = pytest.mark.gui


@pytest.fixture
def wizard_qt_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace PyQt6 widgets with stubs for wizard tests."""
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
    cfg.model.base_url = "http://127.0.0.1:11434/v1"
    cfg.model.model_name = "qwen2.5:7b"
    return cfg


# --- password_strength unit tests ---


def test_password_strength_empty(wizard_qt_stubs: None) -> None:
    """Empty password returns empty label."""
    from doctoragent.presentation.first_run_wizard import password_strength

    label, colour = password_strength("")
    assert label == ""
    assert colour == ""


def test_password_strength_weak(wizard_qt_stubs: None) -> None:
    """Short password is weak."""
    from doctoragent.presentation.first_run_wizard import password_strength

    label, colour = password_strength("abc123")
    assert label == "Weak"
    assert colour == "red"


def test_password_strength_medium(wizard_qt_stubs: None) -> None:
    """8+ chars with limited types is medium."""
    from doctoragent.presentation.first_run_wizard import password_strength

    label, colour = password_strength("abcdefgh")
    assert label == "Medium"
    assert colour == "orange"


def test_password_strength_strong(wizard_qt_stubs: None) -> None:
    """12+ chars with 3 types is strong."""
    from doctoragent.presentation.first_run_wizard import password_strength

    label, colour = password_strength("Password1234")
    assert label == "Strong"
    assert colour == "green"


def test_password_strength_very_strong(wizard_qt_stubs: None) -> None:
    """16+ chars with all 4 types is very strong."""
    from doctoragent.presentation.first_run_wizard import password_strength

    label, colour = password_strength("SuperSecret!2024Pass")
    assert label == "Very Strong"
    assert colour == "darkgreen"


# --- Page creation tests ---


def test_welcome_page_creation(wizard_qt_stubs: None) -> None:
    """WelcomePage creates with expected title."""
    from doctoragent.presentation.first_run_wizard import WelcomePage

    page = WelcomePage()
    assert page._title == "欢迎使用 DoctorAgent"


def test_paths_page_loads_defaults(wizard_qt_stubs: None, config: AegisConfig) -> None:
    """PathsPage pre-populates fields from config."""
    from doctoragent.presentation.first_run_wizard import PathsPage

    page = PathsPage(config)
    assert page._inbox_edit.text() == str(config.paths.inbox)
    assert page._vault_edit.text() == str(config.paths.vault)


def test_paths_page_browse_inbox(wizard_qt_stubs: None, config: AegisConfig) -> None:
    """Browse button updates the inbox path."""
    from doctoragent.presentation.first_run_wizard import PathsPage

    page = PathsPage(config)
    FakeFileDialog.next_directory = "/custom/inbox"
    page._browse_dir(page._inbox_edit, "Select")
    assert page._inbox_edit.text() == "/custom/inbox"


def test_paths_page_browse_no_selection(wizard_qt_stubs: None, config: AegisConfig) -> None:
    """If user cancels the file dialog, the path stays unchanged."""
    from doctoragent.presentation.first_run_wizard import PathsPage

    page = PathsPage(config)
    original = page._inbox_edit.text()
    FakeFileDialog.next_directory = ""
    page._browse_dir(page._inbox_edit, "Select")
    assert page._inbox_edit.text() == original


def test_model_page_loads_defaults(wizard_qt_stubs: None, config: AegisConfig) -> None:
    """ModelPage pre-populates URL and model name from config."""
    from doctoragent.presentation.first_run_wizard import ModelPage

    page = ModelPage(config)
    assert page._url_edit.text() == "http://127.0.0.1:11434/v1"
    assert page._name_edit.text() == "qwen2.5:7b"


def test_security_page_strength_update(wizard_qt_stubs: None) -> None:
    """Password strength label updates as text changes."""
    from doctoragent.presentation.first_run_wizard import SecurityPage

    page = SecurityPage()
    page._update_strength("abc123")
    assert "Weak" in page._strength_label._text
    page._update_strength("Password1234")
    assert "Strong" in page._strength_label._text


def test_security_page_validate_mismatch(wizard_qt_stubs: None) -> None:
    """validatePage returns False when passwords don't match."""
    from doctoragent.presentation.first_run_wizard import SecurityPage

    wiz = FakeWizard()
    page = SecurityPage()
    wiz.addPage(page)
    page._password_edit.setText("password123")
    page._confirm_edit.setText("different")
    assert page.validatePage() is False


def test_security_page_validate_short_password(wizard_qt_stubs: None) -> None:
    """validatePage returns False when password is too short."""
    from doctoragent.presentation.first_run_wizard import SecurityPage

    wiz = FakeWizard()
    page = SecurityPage()
    wiz.addPage(page)
    page._password_edit.setText("abc")
    page._confirm_edit.setText("abc")
    assert page.validatePage() is False


def test_security_page_validate_ok(wizard_qt_stubs: None) -> None:
    """validatePage returns True for matching, strong passwords."""
    from doctoragent.presentation.first_run_wizard import SecurityPage

    wiz = FakeWizard()
    page = SecurityPage()
    wiz.addPage(page)
    page._password_edit.setText("Password123")
    page._confirm_edit.setText("Password123")
    assert page.validatePage() is True


def test_finish_page_summary(wizard_qt_stubs: None, config: AegisConfig) -> None:
    """FinishPage shows collected fields in the summary."""
    from doctoragent.presentation.first_run_wizard import FinishPage

    wiz = FakeWizard()
    finish = FinishPage()
    wiz.addPage(finish)

    wiz.setField("inbox_path", "/test/inbox")
    wiz.setField("vault_path", "/test/vault")
    wiz.setField("model_url", "http://localhost:8080/v1")
    wiz.setField("model_name", "llama3")
    wiz.setField("master_password", "secret")

    finish.initializePage()
    text = finish._summary._text
    assert "/test/inbox" in text
    assert "/test/vault" in text
    assert "llama3" in text
    assert "已设置" in text


def test_finish_page_summary_no_password(wizard_qt_stubs: None) -> None:
    """FinishPage shows '未设置' when no password is configured."""
    from doctoragent.presentation.first_run_wizard import FinishPage

    wiz = FakeWizard()
    finish = FinishPage()
    wiz.addPage(finish)

    wiz.setField("inbox_path", "/inbox")
    wiz.setField("vault_path", "/vault")
    wiz.setField("model_url", "http://x")
    wiz.setField("model_name", "m")
    wiz.setField("master_password", "")

    finish.initializePage()
    assert "未设置" in finish._summary._text


# --- FirstRunWizard integration tests ---


def test_wizard_accept_saves_config(wizard_qt_stubs: None, config: AegisConfig) -> None:
    """Accepting the wizard persists configuration to disk."""
    from doctoragent.presentation.first_run_wizard import FirstRunWizard

    wizard = FirstRunWizard(config)

    # Use paths under tmp_path so the wizard's mkdir(inbox/vault) succeeds
    # on CI runners that cannot write to filesystem root (e.g. /test/inbox).
    inbox_path = str(config.paths.inbox)
    vault_path = str(config.paths.vault)
    wizard.setField("inbox_path", inbox_path)
    wizard.setField("vault_path", vault_path)
    wizard.setField("model_url", "http://localhost:8080/v1")
    wizard.setField("model_name", "llama3")
    wizard.setField("master_password", "secret123")

    wizard.accept()

    assert config.paths.inbox == Path(inbox_path)
    assert config.paths.vault == Path(vault_path)
    assert config.model.base_url == "http://localhost:8080/v1"
    assert config.model.model_name == "llama3"
    assert config.security.master_key_password == "secret123"
    assert config.security.master_key_provider == "FilePassword"
    assert config.paths.settings.exists()


def test_wizard_accept_no_password(wizard_qt_stubs: None, config: AegisConfig) -> None:
    """Accepting without a password keeps the default provider."""
    from doctoragent.presentation.first_run_wizard import FirstRunWizard

    wizard = FirstRunWizard(config)

    inbox_path = str(config.paths.inbox)
    vault_path = str(config.paths.vault)
    wizard.setField("inbox_path", inbox_path)
    wizard.setField("vault_path", vault_path)
    wizard.setField("model_url", "http://x")
    wizard.setField("model_name", "m")
    wizard.setField("master_password", "")

    wizard.accept()

    assert config.security.master_key_password is None
    assert config.security.master_key_provider == "FilePassword"
    assert config.paths.settings.exists()


def test_wizard_saves_to_default_path(wizard_qt_stubs: None, config: AegisConfig) -> None:
    """The saved file content contains the expected values."""
    from doctoragent.presentation.first_run_wizard import FirstRunWizard

    wizard = FirstRunWizard(config)
    inbox_path = str(config.paths.inbox)
    vault_path = str(config.paths.vault)
    wizard.setField("inbox_path", inbox_path)
    wizard.setField("vault_path", vault_path)
    wizard.setField("model_url", "http://u")
    wizard.setField("model_name", "mn")
    wizard.setField("master_password", "")

    wizard.accept()

    saved = config.paths.settings.read_text(encoding="utf-8")
    assert inbox_path in saved or inbox_path.replace("\\", "\\\\") in saved
    assert vault_path in saved or vault_path.replace("\\", "\\\\") in saved
    assert "http://u" in saved
    assert "mn" in saved
    assert "master_key_password" not in saved  # passwords are never persisted to disk
