# mypy: ignore-errors
"""Tests for the Connection model and related enums."""

from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from doctoragent.connections.models import AuthMethod, Connection, PlatformType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(**overrides) -> Connection:
    """Return a Connection with sensible defaults, overridden by *overrides*."""
    defaults = {
        "name": "test-conn",
        "platform_type": PlatformType.OLLAMA,
        "base_url": "http://127.0.0.1:11434",
    }
    defaults.update(overrides)
    return Connection(**defaults)


# ---------------------------------------------------------------------------
# 1. Default values
# ---------------------------------------------------------------------------


class TestConnectionDefaults:
    """Verify that a minimal Connection initialises with correct defaults."""

    def test_id_is_uuid(self) -> None:
        conn = _make_conn()
        assert isinstance(conn.id, UUID)

    def test_api_key_is_empty_secret(self) -> None:
        conn = _make_conn()
        assert isinstance(conn.api_key, SecretStr)
        assert conn.api_key.get_secret_value() == ""

    def test_auth_method_defaults_to_none(self) -> None:
        conn = _make_conn()
        assert conn.auth_method == AuthMethod.NONE

    def test_is_local_and_enabled_by_default(self) -> None:
        conn = _make_conn()
        assert conn.is_local is True
        assert conn.is_enabled is True
        assert conn.is_cloud_authorized is False

    def test_capabilities_default(self) -> None:
        conn = _make_conn()
        assert conn.capabilities == ["chat"]

    def test_timeout_default(self) -> None:
        conn = _make_conn()
        assert conn.timeout == 120.0

    def test_priority_default(self) -> None:
        conn = _make_conn()
        assert conn.priority == 0

    def test_custom_headers_and_payload_empty_by_default(self) -> None:
        conn = _make_conn()
        assert conn.custom_headers == {}
        assert conn.custom_payload == {}


# ---------------------------------------------------------------------------
# 2 & 3. Enum values
# ---------------------------------------------------------------------------


class TestEnums:
    """PlatformType and AuthMethod enum members."""

    @pytest.mark.parametrize(
        "member,value",
        [
            (PlatformType.OLLAMA, "ollama"),
            (PlatformType.LM_STUDIO, "lm_studio"),
            (PlatformType.LLAMACPP_SERVER, "llamacpp_server"),
            (PlatformType.VLLM, "vllm"),
            (PlatformType.LOCALAI, "localai"),
            (PlatformType.OPENAI_COMPATIBLE, "openai_compatible"),
            (PlatformType.OPENAI, "openai"),
            (PlatformType.ANTHROPIC, "anthropic"),
            (PlatformType.CUSTOM, "custom"),
        ],
    )
    def test_platform_type_values(self, member: PlatformType, value: str) -> None:
        assert member.value == value

    def test_platform_type_has_exactly_nine_members(self) -> None:
        assert len(PlatformType) == 9

    @pytest.mark.parametrize(
        "member,value",
        [
            (AuthMethod.NONE, "none"),
            (AuthMethod.BEARER, "bearer"),
            (AuthMethod.API_KEY, "api_key"),
            (AuthMethod.BASIC, "basic"),
        ],
    )
    def test_auth_method_values(self, member: AuthMethod, value: str) -> None:
        assert member.value == value

    def test_auth_method_has_exactly_four_members(self) -> None:
        assert len(AuthMethod) == 4


# ---------------------------------------------------------------------------
# 4. base_url normalisation
# ---------------------------------------------------------------------------


class TestBaseUrlNormalization:
    """Trailing slashes are stripped from base_url."""

    def test_single_trailing_slash_stripped(self) -> None:
        conn = _make_conn(base_url="http://localhost:11434/")
        assert conn.base_url == "http://localhost:11434"

    def test_multiple_trailing_slashes_stripped(self) -> None:
        conn = _make_conn(base_url="http://localhost:11434///")
        assert conn.base_url == "http://localhost:11434"

    def test_no_trailing_slash_unchanged(self) -> None:
        conn = _make_conn(base_url="http://localhost:11434")
        assert conn.base_url == "http://localhost:11434"

    def test_path_preserved_except_trailing_slash(self) -> None:
        conn = _make_conn(base_url="https://api.example.com/v1/")
        assert conn.base_url == "https://api.example.com/v1"


# ---------------------------------------------------------------------------
# 5. is_trusted_local()
# ---------------------------------------------------------------------------


class TestIsTrustedLocal:
    """Verify is_trusted_local() accepts only safe loopback endpoints."""

    def test_ipv4_loopback_with_port(self) -> None:
        conn = _make_conn(base_url="http://127.0.0.1:11434")
        assert conn.is_trusted_local() is True

    def test_localhost_hostname(self) -> None:
        conn = _make_conn(base_url="http://localhost:8080")
        assert conn.is_trusted_local() is True

    def test_ipv6_loopback(self) -> None:
        conn = _make_conn(base_url="http://[::1]:8080")
        assert conn.is_trusted_local() is True

    def test_https_loopback(self) -> None:
        conn = _make_conn(base_url="https://127.0.0.1/v1")
        assert conn.is_trusted_local() is True

    def test_zero_zero_zero_zero_is_not_loopback(self) -> None:
        conn = _make_conn(base_url="http://0.0.0.0:8080")
        assert conn.is_trusted_local() is False

    def test_remote_host_rejected(self) -> None:
        conn = _make_conn(base_url="http://example.com")
        assert conn.is_trusted_local() is False

    def test_ftp_scheme_rejected(self) -> None:
        # Non-http(s) schemes are rejected at construction time to prevent
        # dangerous protocol smuggling (e.g. file://, ftp://).
        with pytest.raises(ValidationError):
            _make_conn(base_url="ftp://127.0.0.1")

    def test_embedded_credentials_rejected(self) -> None:
        conn = _make_conn(base_url="http://user:pass@127.0.0.1")
        assert conn.is_trusted_local() is False

    def test_is_local_false_overrides_everything(self) -> None:
        conn = _make_conn(base_url="http://127.0.0.1:11434", is_local=False)
        assert conn.is_trusted_local() is False


# ---------------------------------------------------------------------------
# 6. Serialisation (to / from dict)
# ---------------------------------------------------------------------------


class TestSerialization:
    """Round-trip Connection through dict serialisation."""

    def test_roundtrip_via_model_dump(self) -> None:
        original = _make_conn(
            model_name="llama3",
            auth_method=AuthMethod.BEARER,
            api_key="secret-token",
            timeout=60.0,
            priority=5,
        )
        data = original.model_dump()
        restored = Connection(**data)
        assert restored.id == original.id
        assert restored.name == original.name
        assert restored.platform_type == original.platform_type
        assert restored.base_url == original.base_url
        assert restored.model_name == original.model_name
        assert restored.auth_method == original.auth_method
        assert restored.api_key.get_secret_value() == original.api_key.get_secret_value()
        assert restored.timeout == original.timeout
        assert restored.priority == original.priority

    def test_api_key_not_leaked_in_dump_mode_dict(self) -> None:
        """model_dump() stores SecretStr objects; they must not be plain text."""
        conn = _make_conn(api_key="super-secret")
        data = conn.model_dump()
        # By default Pydantic serialises SecretStr instances, not their values.
        # Ensure the raw secret string does NOT appear as a plain string.
        assert not isinstance(data["api_key"], str) or data["api_key"] != "super-secret"

    def test_model_dump_json_masks_secrets(self) -> None:
        conn = _make_conn(api_key="super-secret")
        json_str = conn.model_dump_json()
        assert "super-secret" not in json_str


# ---------------------------------------------------------------------------
# 7. Custom headers and payload
# ---------------------------------------------------------------------------


class TestCustomFields:
    """Ensure custom_headers and custom_payload are persisted correctly."""

    def test_custom_headers_stored(self) -> None:
        headers = {"X-Custom-Auth": "abc123", "Accept-Language": "en"}
        conn = _make_conn(custom_headers=headers)
        assert conn.custom_headers == headers

    def test_custom_payload_stored(self) -> None:
        payload = {"temperature": 0.7, "max_tokens": 256, "stop": ["\n\n"]}
        conn = _make_conn(custom_payload=payload)
        assert conn.custom_payload == payload

    def test_custom_fields_survive_serialisation(self) -> None:
        conn = _make_conn(
            custom_headers={"X-Key": "val"},
            custom_payload={"n": 4},
        )
        restored = Connection(**conn.model_dump())
        assert restored.custom_headers == {"X-Key": "val"}
        assert restored.custom_payload == {"n": 4}


# ---------------------------------------------------------------------------
# 8. Timeout and priority
# ---------------------------------------------------------------------------


class TestTimeoutAndPriority:
    """Non-default timeout and priority values."""

    def test_custom_timeout(self) -> None:
        conn = _make_conn(timeout=30.0)
        assert conn.timeout == 30.0

    def test_custom_priority(self) -> None:
        conn = _make_conn(priority=10)
        assert conn.priority == 10

    def test_negative_priority_allowed(self) -> None:
        conn = _make_conn(priority=-1)
        assert conn.priority == -1
