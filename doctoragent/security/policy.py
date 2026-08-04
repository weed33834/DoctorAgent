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
    allow_cloud_fallback: bool = False,
) -> None:
    """Validate that a connection is trusted local for sensitive tasks.

    默认 fail-closed：敏感任务（含 PHI 的分类/加密）必须使用本地
    loopback 连接。仅当 *allow_cloud_fallback* 为 True 且连接已被操作员
    显式授权（``is_cloud_authorized=True``）时才放行云端连接——
    对应操作员主动把云端网关纳入可信处理通道的意图（见模块 docstring）。
    """
    if connection.is_trusted_local():
        return
    # 云端回退：操作员显式授权该云端连接作为敏感任务通道时放行。
    if allow_cloud_fallback and connection.is_cloud_authorized:
        if audit_logger is not None:
            audit_logger.log(
                "cloud_fallback_used",
                {
                    "connection_id": str(connection.id),
                    "connection_name": connection.name,
                    "base_url": connection.base_url,
                    "operation": operation,
                },
            )
        return
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
