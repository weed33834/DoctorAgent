"""Tests for the three-tier key hierarchy."""

import os

from doctoragent.security.keytree import (
    FILE_KEY_LEN,
    VAULT_KEY_LEN,
    derive_file_key,
    derive_vault_key,
    generate_salt,
)


class TestDeriveVaultKey:
    def test_produces_correct_length(self):
        master_key = os.urandom(32)
        vault_key = derive_vault_key(master_key)
        assert len(vault_key) == VAULT_KEY_LEN

    def test_deterministic_for_same_input(self):
        master_key = os.urandom(32)
        k1 = derive_vault_key(master_key)
        k2 = derive_vault_key(master_key)
        assert k1 == k2

    def test_different_master_keys_produce_different_vault_keys(self):
        k1 = derive_vault_key(os.urandom(32))
        k2 = derive_vault_key(os.urandom(32))
        assert k1 != k2

    def test_different_info_produces_different_keys(self):
        master_key = os.urandom(32)
        k1 = derive_vault_key(master_key, info=b"vault-key-v1")
        k2 = derive_vault_key(master_key, info=b"vault-key-v2")
        assert k1 != k2


class TestDeriveFileKey:
    def test_produces_correct_length(self):
        vault_key = os.urandom(32)
        salt = generate_salt()
        file_key = derive_file_key(vault_key, salt)
        assert len(file_key) == FILE_KEY_LEN

    def test_deterministic_for_same_input(self):
        vault_key = os.urandom(32)
        salt = generate_salt()
        k1 = derive_file_key(vault_key, salt)
        k2 = derive_file_key(vault_key, salt)
        assert k1 == k2

    def test_different_salts_produce_different_keys(self):
        vault_key = os.urandom(32)
        salt1 = generate_salt()
        salt2 = generate_salt()
        assert derive_file_key(vault_key, salt1) != derive_file_key(vault_key, salt2)

    def test_different_vault_keys_produce_different_file_keys(self):
        salt = generate_salt()
        k1 = derive_file_key(os.urandom(32), salt)
        k2 = derive_file_key(os.urandom(32), salt)
        assert k1 != k2


class TestGenerateSalt:
    def test_produces_32_bytes(self):
        salt = generate_salt()
        assert len(salt) == 32

    def test_produces_random_salts(self):
        salt1 = generate_salt()
        salt2 = generate_salt()
        assert salt1 != salt2
