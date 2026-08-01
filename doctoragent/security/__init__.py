"""Security service layer."""

from doctoragent.security.analytics import AnomalyType, SecurityAnalyticsEngine
from doctoragent.security.audit_log import ALLOWED_EVENT_TYPES, AuditLogger
from doctoragent.security.backup import (
    BackupResult,
    backup_vault,
    key_fingerprint,
    recombine_key,
    split_key,
    write_key_shares,
)
from doctoragent.security.compliance import ComplianceManager, RetentionPolicy
from doctoragent.security.crypto import decrypt_file_stream, encrypt_file_stream
from doctoragent.security.dlp import DataLossPrevention, DLPResult, SensitiveDataType
from doctoragent.security.field_crypto import FieldEncryptor
from doctoragent.security.keytree import derive_file_key, derive_vault_key, generate_salt
from doctoragent.security.master_key import (
    DpapiMasterKeyProvider,
    FilePasswordProvider,
    MasterKeyProvider,
    TpmMasterKeyProvider,
    create_master_key_provider,
    emergency_rotate,
    rotate_master_key,
    should_rotate_key,
)
from doctoragent.security.policy import (
    SecurityPolicyError,
    require_trusted_local_connection,
)
from doctoragent.security.resources import (
    BackpressureGuard,
    DiskWatermarkChecker,
    disk_usage_percent,
)
from doctoragent.security.sandbox import SandboxManager
from doctoragent.security.shamir import ShamirSecretSharing, Share
from doctoragent.security.tenant import TenantManager
from doctoragent.security.windows_hello import (
    WindowsHelloError,
    get_key_derivation_salt,
    verify_user_identity,
)
from doctoragent.security.zero_trust import TrustLevel, ZeroTrustEngine

__all__ = [
    "ALLOWED_EVENT_TYPES",
    "AnomalyType",
    "AuditLogger",
    "BackpressureGuard",
    "BackupResult",
    "ComplianceManager",
    "DLPResult",
    "DataLossPrevention",
    "DiskWatermarkChecker",
    "DpapiMasterKeyProvider",
    "FieldEncryptor",
    "FilePasswordProvider",
    "MasterKeyProvider",
    "RetentionPolicy",
    "SandboxManager",
    "SecurityPolicyError",
    "SensitiveDataType",
    "ShamirSecretSharing",
    "Share",
    "SecurityAnalyticsEngine",
    "TenantManager",
    "TpmMasterKeyProvider",
    "TrustLevel",
    "WindowsHelloError",
    "ZeroTrustEngine",
    "backup_vault",
    "create_master_key_provider",
    "decrypt_file_stream",
    "derive_file_key",
    "derive_vault_key",
    "disk_usage_percent",
    "emergency_rotate",
    "encrypt_file_stream",
    "generate_salt",
    "get_key_derivation_salt",
    "key_fingerprint",
    "recombine_key",
    "require_trusted_local_connection",
    "rotate_master_key",
    "should_rotate_key",
    "split_key",
    "verify_user_identity",
    "write_key_shares",
]
