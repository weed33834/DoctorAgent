"""Ecosystem integrations: outbound webhooks and remote storage backends.

This package houses the Phase 7 integration surfaces that *push* data out of
DoctorAgent (as opposed to the plugin system under :mod:`doctoragent.extensions`,
which *pulls* code in).

* :mod:`doctoragent.integrations.webhooks` — HMAC-signed outbound event
  delivery with retry/backoff, wired into the audit-log alert channel and
  the classification/sync event sources.
* :mod:`doctoragent.integrations.storage` — pluggable remote storage
  backends (S3/MinIO, WebDAV) for encrypted offsite backup.
"""

from doctoragent.integrations.storage import (
    BackupTransferResult,
    LocalBackend,
    S3Backend,
    StorageBackend,
    StorageBackendError,
    StorageObject,
    WebDAVBackend,
    backup_vault_to_backend,
    create_storage_backend,
)
from doctoragent.integrations.webhooks import (
    WEBHOOK_EVENT_WILDCARD,
    WebhookDeliveryRecord,
    WebhookDispatcher,
    WebhookEndpoint,
    WebhookError,
    attach_security_alert_webhook,
)

__all__ = [
    "BackupTransferResult",
    "LocalBackend",
    "S3Backend",
    "StorageBackend",
    "StorageBackendError",
    "StorageObject",
    "WEBHOOK_EVENT_WILDCARD",
    "WebDAVBackend",
    "WebhookDeliveryRecord",
    "WebhookDispatcher",
    "WebhookEndpoint",
    "WebhookError",
    "attach_security_alert_webhook",
    "backup_vault_to_backend",
    "create_storage_backend",
]
