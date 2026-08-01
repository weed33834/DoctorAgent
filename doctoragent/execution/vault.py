"""Vault file operations.

The execution layer is responsible purely for encryption and storage.
Security policy enforcement (e.g. trusted-local validation) lives in the
orchestration layer that calls these primitives.
"""

import unicodedata
from pathlib import Path
from uuid import UUID

from doctoragent.api.schemas import ClassificationResult, EncryptResult
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.crypto import decrypt_file_stream, encrypt_file_stream
from doctoragent.security.keytree import derive_file_key, generate_salt

# Maximum length for a sanitised path component (typical filesystem limit).
_MAX_PATH_COMPONENT_LEN = 255


class VaultManager:
    """Manage encrypted Vault storage."""

    def __init__(
        self,
        vault_path: Path,
        vault_key: bytes,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self.vault_path = vault_path
        self.vault_key = vault_key
        self.audit_logger = audit_logger

    @staticmethod
    def _sanitize_path_component(value: str, field_name: str) -> str:
        """Sanitize a path component to prevent directory traversal."""
        # Normalize Unicode to NFC form before validation to defeat
        # look-alike attacks that exploit decomposition differences.
        value = unicodedata.normalize("NFC", value)
        # Only keep the final component (basename), stripping any path separators
        safe = Path(value).name
        if not safe or safe == "." or safe == "..":
            raise ValueError(f"Invalid {field_name}: {value!r} contains path traversal characters")
        # Reject any remaining backslashes or null bytes
        if "\\" in safe or "\x00" in safe:
            raise ValueError(f"Invalid {field_name}: {value!r} contains forbidden characters")
        if len(safe) > _MAX_PATH_COMPONENT_LEN:
            raise ValueError(
                f"Invalid {field_name}: {value!r} exceeds maximum length of "
                f"{_MAX_PATH_COMPONENT_LEN}"
            )
        return safe

    def encrypt(
        self,
        source: Path,
        classification: ClassificationResult,
        task_id: UUID,
    ) -> EncryptResult:
        """Encrypt a file into the Vault."""
        # Validate all inputs before performing the (expensive) KDF so we
        # don't waste a salt derivation on a request that will be rejected.
        if not source.exists() or not source.is_file():
            raise ValueError(f"Source file does not exist or is not readable: {source}")

        safe_category = self._sanitize_path_component(classification.category, "category")
        safe_disguise_name = self._sanitize_path_component(
            classification.disguise_name, "disguise_name"
        )
        safe_extension = self._sanitize_path_component(
            classification.disguise_extension, "disguise_extension"
        )

        salt = generate_salt()
        file_key = derive_file_key(self.vault_key, salt)

        disguise_filename = f"{safe_disguise_name}.{safe_extension}"
        category_dir = self.vault_path / safe_category
        category_dir.mkdir(parents=True, exist_ok=True)
        vault_path = category_dir / disguise_filename

        if vault_path.exists():
            # Use the task_id (guaranteed unique) as the suffix to avoid
            # race conditions between concurrent encrypts of the same
            # disguise name.
            suffix = task_id.hex
            disguise_filename = f"{safe_disguise_name}_{suffix}.{safe_extension}"
            vault_path = category_dir / disguise_filename

        nonce = encrypt_file_stream(source, vault_path, file_key, salt)

        return EncryptResult(
            task_id=task_id,
            vault_path=vault_path,
            salt=salt,
            nonce=nonce,
        )

    def decrypt(
        self,
        vault_path: Path,
        salt: bytes,
        destination: Path,
    ) -> None:
        """Decrypt a Vault file to destination."""
        file_key = derive_file_key(self.vault_key, salt)
        decrypt_file_stream(vault_path, destination, file_key)
        if self.audit_logger is not None:
            self.audit_logger.log(
                "decrypted",
                {
                    "vault_path": str(vault_path),
                    "destination": str(destination),
                },
            )
