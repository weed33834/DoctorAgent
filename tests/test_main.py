"""Tests for the CLI entry point."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from doctoragent.__main__ import (
    _build_config,
    _create_tray_app,
    _master_key_storage_path,
    main,
    run_headless,
    run_with_tray,
)


@pytest.fixture(autouse=True)
def _patch_first_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Suppress the first-run wizard in all CLI tests.

    The daemon command has an inline first-run check that imports PyQt6.
    We patch ``PathConfig`` so that ``settings`` always points to an
    existing file, preventing the wizard from launching.
    """
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    mock_instance = MagicMock()
    mock_instance.settings = settings
    monkeypatch.setattr("doctoragent.config.PathConfig", lambda: mock_instance)


def test_build_config_applies_overrides(tmp_path: Path) -> None:
    """_build_config applies CLI path overrides to AegisConfig."""
    config = _build_config(
        inbox=tmp_path / "Inbox",
        vault=tmp_path / "Vault",
        index=tmp_path / "Index",
        debug=False,
    )
    assert config.paths.inbox == tmp_path / "Inbox"
    assert config.paths.vault == tmp_path / "Vault"
    assert config.paths.index == tmp_path / "Index"


def test_main_daemon_no_tray_returns_zero() -> None:
    """main(['daemon', '--no-tray']) starts headless mode and returns 0."""
    with (
        patch("doctoragent.__main__._run_headless") as mock_run,
        patch("doctoragent.__main__.AegisAgent") as mock_agent_cls,
        patch("doctoragent.__main__.AuditLogger"),
    ):
        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent
        result = main(["daemon", "--no-tray"])

    assert result == 0
    mock_run.assert_called_once_with(mock_agent)


def test_main_daemon_falls_back_to_headless_without_qt() -> None:
    """main(['daemon']) falls back to headless mode when Qt is unavailable."""
    with patch("doctoragent.__main__._create_tray_app", side_effect=ImportError("no Qt")):
        with (
            patch("doctoragent.__main__._run_headless") as mock_run,
            patch("doctoragent.__main__.AegisAgent") as mock_agent_cls,
            patch("doctoragent.__main__.AuditLogger"),
        ):
            mock_agent = MagicMock()
            mock_agent_cls.return_value = mock_agent
            result = main(["daemon"])

    assert result == 0
    mock_run.assert_called_once_with(mock_agent)


def test_run_headless_starts_monitoring() -> None:
    """run_headless starts the Inbox watcher on an asyncio loop."""
    agent = MagicMock()
    with patch("doctoragent.__main__.asyncio") as mock_asyncio:
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        mock_asyncio.new_event_loop.return_value = mock_loop
        mock_asyncio.sleep = MagicMock(return_value=None)

        def stop_loop(*_args: object, **_kwargs: object) -> None:
            agent.stop_monitoring.side_effect = None
            # Simulate one iteration then KeyboardInterrupt on second call.
            if mock_loop.run_until_complete.call_count >= 2:
                raise KeyboardInterrupt

        mock_loop.run_until_complete.side_effect = stop_loop
        run_headless(agent)

    agent.start_monitoring.assert_called_once()
    agent.stop_monitoring.assert_called_once()
    mock_loop.call_soon.assert_called_once_with(mock_loop.stop)


def test_main_daemon_no_tray_does_not_import_qt() -> None:
    """main with daemon --no-tray should not require Qt."""
    # Simulate Qt being unavailable for the headless path.
    module_path = "doctoragent.presentation.tray"
    real_module = sys.modules.get(module_path)
    sys.modules[module_path] = None  # type: ignore[assignment]
    try:
        with (
            patch("doctoragent.__main__._run_headless") as mock_run,
            patch("doctoragent.__main__.AegisAgent") as mock_agent_cls,
            patch("doctoragent.__main__.AuditLogger"),
        ):
            mock_agent = MagicMock()
            mock_agent_cls.return_value = mock_agent
            result = main(["daemon", "--no-tray"])
        assert result == 0
        mock_run.assert_called_once_with(mock_agent)
    finally:
        if real_module is not None:
            sys.modules[module_path] = real_module
        else:
            sys.modules.pop(module_path, None)


def test_master_key_storage_path_derived_from_connections() -> None:
    """The master key storage path lives next to the connections file."""
    from doctoragent.config import AegisConfig

    config = AegisConfig()
    config.paths.connections = Path("/tmp/DoctorAgent/Config/connections.json")
    assert _master_key_storage_path(config) == Path("/tmp/DoctorAgent/Config/master_key.bin")


def test_main_daemon_with_windows_hello_passes_salt_to_provider() -> None:
    """When Windows Hello is enabled, the daemon obtains a salt and builds the provider."""
    from doctoragent.config import AegisConfig

    config = AegisConfig()
    config.security.windows_hello_enabled = True
    config.security.master_key_provider = "TPM"

    mock_provider = MagicMock()
    mock_provider.hello_salt = b"hello-salt"

    with (
        patch("doctoragent.__main__._build_config", return_value=config),
        patch("doctoragent.__main__.AuditLogger") as mock_audit,
        patch(
            "doctoragent.__main__.windows_hello.get_key_derivation_salt",
            return_value=b"hello-salt",
        ) as mock_get_salt,
        patch(
            "doctoragent.__main__.create_master_key_provider",
            return_value=mock_provider,
        ) as mock_create_provider,
        patch("doctoragent.__main__.AegisAgent") as mock_agent_cls,
        patch("doctoragent.__main__._run_headless") as mock_run,
    ):
        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent
        result = main(["daemon", "--no-tray"])

    assert result == 0
    mock_get_salt.assert_called_once_with(_master_key_storage_path(config))
    mock_create_provider.assert_called_once_with(
        "TPM",
        _master_key_storage_path(config),
        password=None,
        hello_salt=b"hello-salt",
    )
    mock_agent_cls.assert_called_once_with(
        config,
        audit_logger=mock_audit.return_value,
        master_key_provider=mock_provider,
    )
    mock_run.assert_called_once_with(mock_agent)


def test_main_daemon_with_windows_hello_propagates_verification_failure() -> None:
    """If Windows Hello verification fails, the daemon does not start the agent."""
    from doctoragent.config import AegisConfig
    from doctoragent.security.windows_hello import WindowsHelloError

    config = AegisConfig()
    config.security.windows_hello_enabled = True

    with (
        patch("doctoragent.__main__._build_config", return_value=config),
        patch("doctoragent.__main__.AuditLogger"),
        patch(
            "doctoragent.__main__.windows_hello.get_key_derivation_salt",
            side_effect=WindowsHelloError("cancelled"),
        ),
        patch("doctoragent.__main__.AegisAgent") as mock_agent_cls,
    ):
        with pytest.raises(WindowsHelloError, match="cancelled"):
            main(["daemon", "--no-tray"])

    mock_agent_cls.assert_not_called()
