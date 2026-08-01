"""Tests for DoctorAgent configuration."""

import json

from doctoragent.config import AegisConfig


class TestAegisConfig:
    def test_default_config(self):
        config = AegisConfig()
        assert config.security is not None
        assert config.paths is not None
        assert config.model is not None

    def test_save_to_file_excludes_sensitive_fields(self, tmp_path):
        config = AegisConfig()
        config.security.master_key_password = "super_secret_password"
        settings_path = tmp_path / "settings.json"
        config.save_to_file(settings_path)
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        security = data.get("security", {})
        assert "master_key_password" not in security

    def test_save_to_file_is_atomic(self, tmp_path):
        config = AegisConfig()
        settings_path = tmp_path / "settings.json"
        config.save_to_file(settings_path)
        # The tmp file should not remain after save
        assert not (tmp_path / "settings.tmp").exists()
        assert settings_path.exists()

    def test_load_from_file(self, tmp_path):
        config = AegisConfig()
        config.security.master_key_password = "secret"
        settings_path = tmp_path / "settings.json"
        config.save_to_file(settings_path)
        loaded = AegisConfig.load_from_file(settings_path)
        assert loaded.security.master_key_password is None  # sensitive field excluded

    def test_load_from_file_corrupt_json(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("not valid json {{{", encoding="utf-8")
        # Corrupt JSON falls back to defaults rather than crashing.
        config = AegisConfig.load_from_file(settings_path)
        assert isinstance(config, AegisConfig)

    def test_load_from_file_missing(self, tmp_path):
        settings_path = tmp_path / "nonexistent.json"
        config = AegisConfig.load_from_file(settings_path)
        assert config is not None
