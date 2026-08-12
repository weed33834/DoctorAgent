"""Tests for the cloud KMS abstraction (``doctoragent.security.kms``)."""

from __future__ import annotations

import pytest

from doctoragent.security.kms import (
    AWSKMSProvider,
    AzureKeyVaultProvider,
    GCPKMSProvider,
    KMSProvider,
    LocalKMSProvider,
    create_kms_provider,
)

# ── LocalKMSProvider ─────────────────────────────────────────────────────────


class TestLocalKMSProvider:
    """The always-available local provider must round-trip and bind context."""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        provider = LocalKMSProvider(master_key=b"\x00" * 32)
        ciphertext = provider.encrypt(b"hello world", {})
        assert ciphertext != b"hello world"
        assert provider.decrypt(ciphertext, {}) == b"hello world"

    def test_roundtrip_with_context(self) -> None:
        provider = LocalKMSProvider(master_key=b"\x01" * 32)
        ctx = {"tenant": "acme", "file_id": "123"}
        ciphertext = provider.encrypt(b"secret", ctx)
        assert provider.decrypt(ciphertext, ctx) == b"secret"

    def test_context_mismatch_rejected(self) -> None:
        from cryptography.exceptions import InvalidTag

        provider = LocalKMSProvider(master_key=b"\x02" * 32)
        ciphertext = provider.encrypt(b"secret", {"tenant": "acme"})
        with pytest.raises(InvalidTag):
            provider.decrypt(ciphertext, {"tenant": "other"})

    def test_missing_context_rejected(self) -> None:
        from cryptography.exceptions import InvalidTag

        provider = LocalKMSProvider(master_key=b"\x03" * 32)
        ciphertext = provider.encrypt(b"secret", {"tenant": "acme"})
        with pytest.raises(InvalidTag):
            provider.decrypt(ciphertext, {})

    def test_tampered_ciphertext_rejected(self) -> None:
        from cryptography.exceptions import InvalidTag

        provider = LocalKMSProvider(master_key=b"\x04" * 32)
        ciphertext = bytearray(provider.encrypt(b"secret", {}))
        # Flip a byte in the ciphertext body.
        ciphertext[-1] ^= 0x01
        with pytest.raises(InvalidTag):
            provider.decrypt(bytes(ciphertext), {})

    def test_context_order_independent(self) -> None:
        """Context serialisation is sorted, so insertion order is irrelevant."""
        provider = LocalKMSProvider(master_key=b"\x05" * 32)
        ciphertext = provider.encrypt(b"secret", {"a": "1", "b": "2"})
        assert provider.decrypt(ciphertext, {"b": "2", "a": "1"}) == b"secret"

    def test_invalid_master_key_length(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            LocalKMSProvider(master_key=b"short")

    def test_env_key_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key_hex = (b"\x06" * 32).hex()
        monkeypatch.setenv("DOCTORAGENT_KMS_LOCAL_KEY", key_hex)
        provider = LocalKMSProvider()
        ciphertext = provider.encrypt(b"x", {})
        # A fresh provider with the same env key must decrypt it.
        provider2 = LocalKMSProvider()
        assert provider2.decrypt(ciphertext, {}) == b"x"

    def test_ephemeral_key_flag_reflects_actual_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression for #18: info()['ephemeral_key'] must reflect the real
        # key persistence, considering BOTH the constructor arg and env var.
        monkeypatch.delenv("DOCTORAGENT_KMS_LOCAL_KEY", raising=False)
        # No key anywhere → ephemeral.
        assert LocalKMSProvider().info()["ephemeral_key"] is True
        # Env key set → persistent.
        monkeypatch.setenv("DOCTORAGENT_KMS_LOCAL_KEY", (b"\x09" * 32).hex())
        assert LocalKMSProvider().info()["ephemeral_key"] is False
        # master_key supplied (even without env) → persistent.
        monkeypatch.delenv("DOCTORAGENT_KMS_LOCAL_KEY", raising=False)
        assert LocalKMSProvider(master_key=b"\x0a" * 32).info()["ephemeral_key"] is False

    def test_env_key_invalid_hex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCTORAGENT_KMS_LOCAL_KEY", "not-hex")
        with pytest.raises(ValueError, match="64 hex chars"):
            LocalKMSProvider()

    def test_info(self) -> None:
        provider = LocalKMSProvider(master_key=b"\x07" * 32)
        info = provider.info()
        assert info["provider"] == "local"
        assert info["algorithm"] == "AES-256-GCM"
        assert info["version"] == 1

    def test_is_kms_provider(self) -> None:
        assert isinstance(LocalKMSProvider(master_key=b"\x08" * 32), KMSProvider)


# ── Factory ──────────────────────────────────────────────────────────────────


class TestCreateKMSProvider:
    """The factory dispatches by name and degrades gracefully."""

    def test_local_returns_local_provider(self) -> None:
        provider = create_kms_provider("local")
        assert isinstance(provider, LocalKMSProvider)

    def test_none_returns_local_provider(self) -> None:
        provider = create_kms_provider("none")
        assert isinstance(provider, LocalKMSProvider)

    def test_case_insensitive(self) -> None:
        assert isinstance(create_kms_provider("LOCAL"), LocalKMSProvider)
        assert isinstance(create_kms_provider(" None "), LocalKMSProvider)

    def test_azure_raises_importerror_with_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Azure libraries are not installed in CI; the constructor must raise
        # ImportError whose message contains the install command.
        with pytest.raises(ImportError) as exc:
            create_kms_provider("azure")
        assert "doctoragent[kms]" in str(exc.value)

    def test_gcp_raises_importerror_with_install_hint(self) -> None:
        with pytest.raises(ImportError) as exc:
            create_kms_provider("gcp")
        assert "doctoragent[kms]" in str(exc.value)

    def test_unknown_provider_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="Unknown KMS provider"):
            create_kms_provider("martian-moon-base")


# ── AWSKMSProvider (boto3 mocked) ────────────────────────────────────────────


class TestAWSKMSProvider:
    """AWSKMSProvider wiring is verified with a mocked boto3 KMS client."""

    def test_aws_encrypt_decrypt_via_mocked_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The mock client "encrypts" by tagging the plaintext, and "decrypts"
        # by stripping the tag — sufficient to exercise the provider wiring
        # without a real AWS round-trip.
        encrypt_calls: list[dict] = []
        decrypt_calls: list[dict] = []

        class FakeKMSClient:
            def encrypt(self, *, KeyId, Plaintext, EncryptionContext):  # noqa: N803
                encrypt_calls.append(
                    {"KeyId": KeyId, "EncryptionContext": EncryptionContext}
                )
                tag = b"AWS1"
                return {"CiphertextBlob": tag + Plaintext}

            def decrypt(self, *, CiphertextBlob, EncryptionContext):  # noqa: N803
                decrypt_calls.append(
                    {"EncryptionContext": EncryptionContext}
                )
                blob = CiphertextBlob
                assert blob.startswith(b"AWS1"), blob[:8]
                return {"Plaintext": blob[len(b"AWS1") :]}

        monkeypatch.setenv("DOCTORAGENT_KMS_AWS_KEY_ID", "alias/test-key")
        monkeypatch.setenv("DOCTORAGENT_KMS_AWS_REGION", "us-west-2")

        provider = AWSKMSProvider(client=FakeKMSClient())
        assert provider.info() == {
            "provider": "aws",
            "key_id": "alias/test-key",
            "region": "us-west-2",
        }

        ctx = {"tenant": "acme", "file": "report.pdf"}
        ciphertext = provider.encrypt(b"sensitive-data", ctx)
        assert ciphertext.startswith(b"AWS1")
        assert provider.decrypt(ciphertext, ctx) == b"sensitive-data"

        # The encryption context must have been forwarded to KMS verbatim.
        assert encrypt_calls[0]["EncryptionContext"] == ctx
        assert encrypt_calls[0]["KeyId"] == "alias/test-key"
        assert decrypt_calls[0]["EncryptionContext"] == ctx

    def test_aws_requires_key_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DOCTORAGENT_KMS_AWS_KEY_ID", raising=False)

        class FakeKMSClient:
            pass

        with pytest.raises(ValueError, match="DOCTORAGENT_KMS_AWS_KEY_ID"):
            AWSKMSProvider(client=FakeKMSClient())

    def test_aws_context_values_coerced_to_strings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        class FakeKMSClient:
            def encrypt(self, *, KeyId, Plaintext, EncryptionContext):  # noqa: N803
                captured["ctx"] = EncryptionContext
                return {"CiphertextBlob": b"X" + Plaintext}

            def decrypt(self, *, CiphertextBlob, EncryptionContext):  # noqa: N803
                return {"Plaintext": CiphertextBlob[1:]}

        monkeypatch.setenv("DOCTORAGENT_KMS_AWS_KEY_ID", "k")
        provider = AWSKMSProvider(client=FakeKMSClient())
        provider.encrypt(b"d", {"count": 42, "flag": True})
        # KMS encryption context values must be strings.
        assert captured["ctx"] == {"count": "42", "flag": "True"}


# ── Azure / GCP construction guards (no SDK installed) ───────────────────────


class TestCloudKMSMissingSDK:
    """Constructing azure/gcp providers without their SDK fails clearly."""

    def test_azure_importerror_message(self) -> None:
        with pytest.raises(ImportError) as exc:
            AzureKeyVaultProvider(
                vault_url="https://vault.example.net", key_name="k"
            )
        msg = str(exc.value)
        assert "azure" in msg.lower()
        assert "doctoragent[kms]" in msg

    def test_gcp_importerror_message(self) -> None:
        with pytest.raises(ImportError) as exc:
            GCPKMSProvider(key_path="projects/p/locations/l/keyRings/r/cryptoKeys/k")
        msg = str(exc.value)
        assert "google-cloud-kms" in msg.lower() or "google" in msg.lower()
        assert "doctoragent[kms]" in msg
