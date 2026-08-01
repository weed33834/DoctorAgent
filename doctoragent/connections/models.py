"""Connection schemas for platform management."""

import ipaddress
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from doctoragent.compat import StrEnum


class PlatformType(StrEnum):
    """Built-in platform types."""

    OLLAMA = "ollama"
    LM_STUDIO = "lm_studio"
    LLAMACPP_SERVER = "llamacpp_server"
    VLLM = "vllm"
    LOCALAI = "localai"
    OPENAI_COMPATIBLE = "openai_compatible"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    CUSTOM = "custom"


class AuthMethod(StrEnum):
    """Authentication methods."""

    NONE = "none"
    BEARER = "bearer"
    API_KEY = "api_key"
    BASIC = "basic"


class Connection(BaseModel):
    """A configurable platform connection."""

    id: UUID = Field(default_factory=uuid4)
    name: str
    platform_type: PlatformType
    base_url: str
    model_name: str = ""
    auth_method: AuthMethod = AuthMethod.NONE
    api_key: SecretStr = SecretStr("")
    username: str = ""
    password: SecretStr = SecretStr("")
    is_local: bool = True
    is_enabled: bool = True
    is_cloud_authorized: bool = False
    capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    custom_headers: dict[str, str] = Field(default_factory=dict)
    custom_payload: dict[str, Any] = Field(default_factory=dict)
    timeout: float = Field(default=120.0, gt=0)
    # Priority is a relative ranking; negative values legitimately express
    # "less preferred than default", so no lower bound is enforced.
    priority: int = Field(default=0)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        """Validate and normalise the base URL."""
        value = value.rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"base_url must use http or https scheme, got: {parsed.scheme!r}")
        if not parsed.hostname:
            raise ValueError("base_url must have a valid hostname")
        return value

    @model_validator(mode="after")
    def validate_auth_consistency(self) -> "Connection":
        """Ensure auth_method and credentials are consistent."""
        if self.auth_method in (AuthMethod.BEARER, AuthMethod.API_KEY):
            if not self.api_key.get_secret_value():
                raise ValueError(f"auth_method={self.auth_method!r} requires a non-empty api_key")
        elif self.auth_method == AuthMethod.BASIC:
            if not self.username or not self.password.get_secret_value():
                raise ValueError("auth_method='basic' requires non-empty username and password")
        return self

    def is_trusted_local(self) -> bool:
        """Return True if connection is considered safe for sensitive tasks.

        Only plain http/https loopback URLs without embedded credentials are
        accepted. IPv6 addresses and IPv4-mapped IPv6 are normalised before
        the loopback check.
        """
        if not self.is_local:
            return False
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        # Reject URLs with embedded credentials; they complicate auditing and
        # can be used to smuggle non-loopback hosts in the userinfo section.
        if parsed.username is not None or parsed.password is not None:
            return False
        host = parsed.hostname
        if host is None:
            return False
        host = host.lower().strip()
        if host == "localhost":
            return True
        # Strip IPv6 zone index (e.g. ::1%lo0) before parsing.
        host = host.split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return addr.is_loopback
