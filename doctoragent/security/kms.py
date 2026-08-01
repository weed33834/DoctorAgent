"""Cloud Key Management Service (KMS) abstraction for DoctorAgent.

This module wraps external KMS providers (AWS KMS, Azure Key Vault, Google
Cloud KMS) behind a uniform :class:`KMSProvider` interface so that the master
key — and any tenant/file-scoped data — can be protected by an external HSM
instead of a local file/DPAPI/TPM/Keychain store.

Design goals
------------

* **Graceful degradation.** Only the ``local`` provider (backed by the
  already-required ``cryptography`` library) is always available. The cloud
  providers import their SDKs lazily and raise :class:`ImportError` with a
  clear install hint when the SDK is missing — the server still starts.
* **Encryption context.** Every ``encrypt``/``decrypt`` call takes a
  ``context: dict``. For AWS KMS this maps directly to the *EncryptionContext*
  used to associate ciphertext with a tenant/file; for the local provider it
  is mixed into the AES-GCM AAD so a ciphertext cannot be replayed against a
  different context.
* **Optional layer.** The KMS provider is a *wrapper* over the existing
  master-key providers, not a replacement. When
  ``DOCTORAGENT_KMS_PROVIDER`` is set the master key material can be envelope-
  encrypted via :class:`KMSProvider`; otherwise the existing
  :mod:`doctoragent.security.master_key` path is unchanged.
"""

from __future__ import annotations

import logging
import os
import secrets
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


# ── Abstract interface ───────────────────────────────────────────────────────


class KMSProvider(ABC):
    """Abstract base class for KMS providers.

    All methods are synchronous: cloud KMS calls are blocking I/O and the
    master-key path is not on the hot request loop.
    """

    @abstractmethod
    def encrypt(self, plaintext: bytes, context: dict[str, str]) -> bytes:
        """Encrypt *plaintext*, binding the ciphertext to *context*.

        *context* is the KMS encryption context (AWS) / AAD (local). It must
        be reproducible verbatim for :meth:`decrypt`.
        """

    @abstractmethod
    def decrypt(self, ciphertext: bytes, context: dict[str, str]) -> bytes:
        """Decrypt *ciphertext* previously produced by :meth:`encrypt`.

        The same *context* used at encryption time must be supplied. A
        mismatch (or tampered ciphertext) raises an error.
        """

    @abstractmethod
    def info(self) -> dict[str, Any]:
        """Return a JSON-serialisable description of this provider."""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _normalize_context(context: dict[str, str] | None) -> dict[str, str]:
    """Coerce *context* to a ``dict[str, str]`` (KMS providers require strings)."""
    if not context:
        return {}
    return {str(k): str(v) for k, v in context.items()}


def _context_to_aad(context: dict[str, str]) -> bytes:
    """Deterministically serialise *context* into AAD bytes.

    Sorted by key so the same logical context always yields the same AAD,
    regardless of insertion order.
    """
    if not context:
        return b"doctoragent-local-kms-v1"
    parts = [f"{k}={v}".encode() for k, v in sorted(context.items())]
    return b"doctoragent-local-kms-v1\x00" + b"\x00".join(parts)


# ── Local provider (always available; no cloud dependency) ───────────────────


class LocalKMSProvider(KMSProvider):
    """AES-256-GCM KMS provider backed by a locally-held key.

    The 32-byte key is derived (or loaded) from the ``DOCTORAGENT_KMS_LOCAL_KEY``
    environment variable (hex). When unset, an ephemeral random key is
    generated for the process — suitable for tests but *not* for production
    (a warning is logged). A persistent key can also be supplied directly via
    the *master_key* constructor argument.

    Ciphertext layout: ``version(1) || nonce(12) || ciphertext+tag``.
    """

    _VERSION = b"\x01"
    _NONCE_LEN = 12

    def __init__(self, master_key: bytes | None = None) -> None:
        if master_key is not None:
            if len(master_key) != 32:
                raise ValueError("master_key must be exactly 32 bytes for AES-256")
            self._key = bytes(master_key)
        else:
            env_key = os.environ.get("DOCTORAGENT_KMS_LOCAL_KEY")
            if env_key:
                try:
                    self._key = bytes.fromhex(env_key)
                except ValueError as exc:
                    raise ValueError(
                        "DOCTORAGENT_KMS_LOCAL_KEY must be 64 hex chars (32 bytes)"
                    ) from exc
                if len(self._key) != 32:
                    raise ValueError("DOCTORAGENT_KMS_LOCAL_KEY must decode to exactly 32 bytes")
            else:
                logger.warning(
                    "LocalKMSProvider: DOCTORAGENT_KMS_LOCAL_KEY not set; using an "
                    "EPHEMERAL random key — encrypted data will NOT be "
                    "decryptable after this process exits."
                )
                self._key = secrets.token_bytes(32)

    def encrypt(self, plaintext: bytes, context: dict[str, str] | None = None) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        ctx = _normalize_context(context)
        aad = _context_to_aad(ctx)
        nonce = os.urandom(self._NONCE_LEN)
        aesgcm = AESGCM(self._key)
        ciphertext = aesgcm.encrypt(nonce, bytes(plaintext), aad)
        return self._VERSION + nonce + ciphertext

    def decrypt(self, ciphertext: bytes, context: dict[str, str] | None = None) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(ciphertext) < 1 + self._NONCE_LEN + 16:
            raise ValueError("ciphertext too short to be a LocalKMSProvider blob")
        version = ciphertext[:1]
        if version != self._VERSION:
            raise ValueError(f"unsupported LocalKMSProvider version: {version!r}")
        nonce = ciphertext[1 : 1 + self._NONCE_LEN]
        body = ciphertext[1 + self._NONCE_LEN :]
        ctx = _normalize_context(context)
        aad = _context_to_aad(ctx)
        aesgcm = AESGCM(self._key)
        return aesgcm.decrypt(nonce, body, aad)

    def info(self) -> dict[str, Any]:
        return {
            "provider": "local",
            "algorithm": "AES-256-GCM",
            "version": int.from_bytes(self._VERSION, "big"),
            "ephemeral_key": "DOCTORAGENT_KMS_LOCAL_KEY" not in os.environ,
        }


# ── AWS KMS ──────────────────────────────────────────────────────────────────


class AWSKMSProvider(KMSProvider):
    """AWS Key Management Service provider (uses ``boto3``).

    The KMS key id and region are read from the ``DOCTORAGENT_KMS_AWS_KEY_ID`` and
    ``DOCTORAGENT_KMS_AWS_REGION`` environment variables (or supplied explicitly).
    boto3 is part of the ``s3`` extra; when it is not installed, constructing
    this provider raises :class:`ImportError` with the install command.
    """

    def __init__(
        self,
        key_id: str | None = None,
        region: str | None = None,
        client: Any = None,
    ) -> None:
        try:
            import boto3  # type: ignore[import-untyped]  # noqa: F401
        except ImportError as exc:  # pragma: no cover — depends on env
            raise ImportError(
                "boto3 is required for the AWS KMS provider. "
                "Install it with: pip install 'doctoragent[s3]'"
            ) from exc

        self._key_id = key_id or os.environ.get("DOCTORAGENT_KMS_AWS_KEY_ID")
        if not self._key_id:
            raise ValueError(
                "AWS KMS key id is required: set DOCTORAGENT_KMS_AWS_KEY_ID or pass key_id"
            )
        self._region = region or os.environ.get("DOCTORAGENT_KMS_AWS_REGION", "us-east-1")

        if client is not None:
            self._client = client
        else:  # pragma: no cover — real AWS client creation not exercised in CI
            self._client = boto3.client("kms", region_name=self._region)

    def encrypt(self, plaintext: bytes, context: dict[str, str] | None = None) -> bytes:
        ctx = _normalize_context(context)
        resp = self._client.encrypt(
            KeyId=self._key_id,
            Plaintext=bytes(plaintext),
            EncryptionContext=ctx,
        )
        # ``CiphertextBlob`` is raw bytes; return as-is so decrypt can pass it back.
        return resp["CiphertextBlob"]

    def decrypt(self, ciphertext: bytes, context: dict[str, str] | None = None) -> bytes:
        ctx = _normalize_context(context)
        resp = self._client.decrypt(
            CiphertextBlob=bytes(ciphertext),
            EncryptionContext=ctx,
        )
        return resp["Plaintext"]

    def info(self) -> dict[str, Any]:
        return {
            "provider": "aws",
            "key_id": self._key_id,
            "region": self._region,
        }


# ── Azure Key Vault ──────────────────────────────────────────────────────────


class AzureKeyVaultProvider(KMSProvider):
    """Azure Key Vault KMS provider.

    Uses ``azure-identity`` and ``azure-keyvault-secrets`` (technically the
    ``azure-keyvault-keys`` API for crypto operations, surfaced via the same
    package family). Both libraries are part of the ``kms`` extra and are
    *not* installed by default; constructing this provider without them raises
    :class:`ImportError` with the install command.

    The vault URL and key name are read from ``DOCTORAGENT_KMS_AZURE_VAULT_URL``
    and ``DOCTORAGENT_KMS_AZURE_KEY_NAME``.
    """

    _INSTALL_HINT = (
        "azure-identity and azure-keyvault libraries are required for the "
        "Azure Key Vault KMS provider. "
        "Install them with: pip install 'doctoragent[kms]'"
    )

    def __init__(
        self,
        vault_url: str | None = None,
        key_name: str | None = None,
        credential: Any = None,
    ) -> None:
        try:
            from azure.identity import DefaultAzureCredential  # type: ignore[import-not-found]
            from azure.keyvault.keys.crypto import (
                CryptographyClient,  # type: ignore[import-not-found]
            )
        except ImportError as exc:  # pragma: no cover — azure not in CI
            raise ImportError(self._INSTALL_HINT) from exc

        self._vault_url = vault_url or os.environ.get("DOCTORAGENT_KMS_AZURE_VAULT_URL")
        self._key_name = key_name or os.environ.get("DOCTORAGENT_KMS_AZURE_KEY_NAME")
        if not self._vault_url or not self._key_name:
            raise ValueError(
                "Azure Key Vault requires DOCTORAGENT_KMS_AZURE_VAULT_URL and "
                "DOCTORAGENT_KMS_AZURE_KEY_NAME (or constructor args)"
            )
        self._credential = credential or DefaultAzureCredential()
        self._crypto_client = CryptographyClient(
            key=f"https://{self._vault_url}/keys/{self._key_name}",
            credential=self._credential,
        )

    def encrypt(self, plaintext: bytes, context: dict[str, str] | None = None) -> bytes:
        # Azure Key Vault does not natively support an encryption context like
        # AWS KMS; we fold the context into the AAD of the RSA-OAEP envelope.
        from azure.keyvault.keys.crypto import EncryptionAlgorithm  # type: ignore[import-not-found]

        aad = _context_to_aad(_normalize_context(context))
        result = self._crypto_client.encrypt(
            algorithm=EncryptionAlgorithm.rsa_oaep_256,
            value=bytes(plaintext),
            additional_authenticated_data=aad if context else None,
        )
        return result.ciphertext

    def decrypt(self, ciphertext: bytes, context: dict[str, str] | None = None) -> bytes:
        from azure.keyvault.keys.crypto import EncryptionAlgorithm  # type: ignore[import-not-found]

        aad = _context_to_aad(_normalize_context(context))
        result = self._crypto_client.decrypt(
            algorithm=EncryptionAlgorithm.rsa_oaep_256,
            value=bytes(ciphertext),
            additional_authenticated_data=aad if context else None,
        )
        return result.plaintext

    def info(self) -> dict[str, Any]:
        return {
            "provider": "azure",
            "vault_url": self._vault_url,
            "key_name": self._key_name,
        }


# ── Google Cloud KMS ─────────────────────────────────────────────────────────


class GCPKMSProvider(KMSProvider):
    """Google Cloud Key Management Service provider.

    Uses ``google-cloud-kms`` (the ``kms`` extra). When the library is not
    installed, constructing this provider raises :class:`ImportError` with the
    install command.

    The key resource path is read from ``DOCTORAGENT_KMS_GCP_KEY_PATH`` in the
    form ``projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>``.
    """

    _INSTALL_HINT = (
        "google-cloud-kms is required for the GCP KMS provider. "
        "Install it with: pip install 'doctoragent[kms]'"
    )

    def __init__(self, key_path: str | None = None, client: Any = None) -> None:
        try:
            from google.cloud import kms  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — gcp not in CI
            raise ImportError(self._INSTALL_HINT) from exc

        self._key_path = key_path or os.environ.get("DOCTORAGENT_KMS_GCP_KEY_PATH")
        if not self._key_path:
            raise ValueError(
                "GCP KMS requires DOCTORAGENT_KMS_GCP_KEY_PATH "
                "(projects/.../cryptoKeys/<name>) or a key_path argument"
            )
        self._kms_module = kms
        self._client = client or kms.KeyManagementServiceClient()

    def encrypt(self, plaintext: bytes, context: dict[str, str] | None = None) -> bytes:
        ctx = _normalize_context(context)
        # GCP KMS supports up to 8 context key/value pairs (both <= 63 bytes).
        request = {
            "name": self._key_path,
            "plaintext": bytes(plaintext),
            "additional_authenticated_data": _context_to_aad(ctx),
        }
        response = self._client.encrypt(request=request)
        # ``ciphertext`` is bytes; prepend nothing — GCP handles its own framing.
        return response.ciphertext

    def decrypt(self, ciphertext: bytes, context: dict[str, str] | None = None) -> bytes:
        ctx = _normalize_context(context)
        request = {
            "name": self._key_path,
            "ciphertext": bytes(ciphertext),
            "additional_authenticated_data": _context_to_aad(ctx),
        }
        response = self._client.decrypt(request=request)
        return response.plaintext

    def info(self) -> dict[str, Any]:
        return {
            "provider": "gcp",
            "key_path": self._key_path,
        }


# ── Factory ──────────────────────────────────────────────────────────────────


def create_kms_provider(provider: str) -> KMSProvider:
    """Construct a :class:`KMSProvider` by name.

    *provider* is case-insensitive and one of:

    * ``aws``   → :class:`AWSKMSProvider` (requires boto3 / ``s3`` extra)
    * ``azure`` → :class:`AzureKeyVaultProvider` (requires ``kms`` extra)
    * ``gcp``   → :class:`GCPKMSProvider` (requires ``kms`` extra)
    * ``local`` → :class:`LocalKMSProvider` (always available)
    * ``none``  → :class:`LocalKMSProvider` (explicit "no cloud" fallback)

    Any other value raises :class:`ValueError`.
    """
    name = (provider or "").strip().lower()
    if name in ("local", "none"):
        return LocalKMSProvider()
    if name == "aws":
        return AWSKMSProvider()
    if name == "azure":
        return AzureKeyVaultProvider()
    if name == "gcp":
        return GCPKMSProvider()
    raise ValueError(
        f"Unknown KMS provider: {provider!r}. Expected one of: aws, azure, gcp, local, none"
    )


__all__ = [
    "AWSKMSProvider",
    "AzureKeyVaultProvider",
    "GCPKMSProvider",
    "KMSProvider",
    "LocalKMSProvider",
    "create_kms_provider",
]
