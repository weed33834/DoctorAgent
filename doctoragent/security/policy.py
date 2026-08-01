"""Security policy for sensitive operations.

Sensitive tasks (Vault encryption/decryption, password fill, key derivation)
must use a connection that is local and bound to 127.0.0.1/localhost unless the
operator has explicitly enabled cloud fallback and marked the connection as
authorised.
"""

from doctoragent.connections.models import Connection
from doctoragent.security.audit_log import AuditLogger


class SecurityPolicyError(Exception):
    """Raised when a sensitive operation violates the security policy."""


def require_trusted_local_connection(
    connection: Connection,
    audit_logger: AuditLogger | None = None,
    operation: str = "sensitive_operation",
) -> None:
    """Validate that a connection is trusted local for sensitive tasks."""
    if not connection.is_trusted_local():
        if audit_logger is not None:
            audit_logger.log(
                "policy_violation",
                {
                    "connection_id": str(connection.id),
                    "connection_name": connection.name,
                    "base_url": connection.base_url,
                    "operation": operation,
                },
            )
        raise SecurityPolicyError(
            f"Connection '{connection.name}' ({connection.base_url}) is not a "
            "trusted local connection. Sensitive tasks require 127.0.0.1 or localhost."
        )
