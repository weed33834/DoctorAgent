"""Tests for sensitive operation security policy."""

from pathlib import Path

import pytest

from doctoragent.config import AegisConfig
from doctoragent.connections.models import Connection, PlatformType
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.policy import (
    SecurityPolicyError,
    require_trusted_local_connection,
)


def test_local_connection_passes() -> None:
    """Trusted local connection passes policy check."""
    conn = Connection(
        name="Local Ollama",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
    )
    require_trusted_local_connection(conn)  # should not raise


def test_cloud_connection_fails() -> None:
    """Cloud connection is rejected for sensitive tasks."""
    conn = Connection(
        name="Cloud OpenAI",
        platform_type=PlatformType.OPENAI,
        base_url="https://api.openai.com/v1",
        is_local=False,
    )
    with pytest.raises(SecurityPolicyError):
        require_trusted_local_connection(conn)


def test_policy_violation_is_audited(tmp_path: Path) -> None:
    """require_trusted_local_connection logs a policy_violation event."""
    config = AegisConfig()
    config.paths.logs = tmp_path / "logs"
    audit = AuditLogger(config, hmac_key=b"k" * 32)

    cloud = Connection(
        name="Cloud",
        platform_type=PlatformType.OPENAI,
        base_url="https://api.openai.com/v1",
        is_local=False,
    )
    with pytest.raises(SecurityPolicyError):
        require_trusted_local_connection(cloud, audit_logger=audit, operation="test_op")

    records = audit.query(event_type="policy_violation")
    assert len(records) == 1
    assert records[0]["details"]["operation"] == "test_op"
    assert records[0]["details"]["connection_name"] == "Cloud"


def test_local_connection_with_loopback_ipv6() -> None:
    """IPv6 loopback (``[::1]``) is treated as trusted local."""
    conn = Connection(
        name="Local v6",
        platform_type=PlatformType.OLLAMA,
        base_url="http://[::1]:11434/v1",
    )
    require_trusted_local_connection(conn)  # should not raise


def test_localhost_hostname_is_trusted() -> None:
    """The literal ``localhost`` hostname is trusted local."""
    conn = Connection(
        name="Localhost",
        platform_type=PlatformType.OLLAMA,
        base_url="http://localhost:11434/v1",
    )
    require_trusted_local_connection(conn)  # should not raise
