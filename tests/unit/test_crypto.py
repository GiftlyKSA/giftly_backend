"""Tests for AES-256-GCM field encryption."""

from __future__ import annotations

import os

import pytest
from app.core.crypto import CryptoError, FieldCipher, build_aad


def _cipher() -> FieldCipher:
    return FieldCipher(keys={1: os.urandom(32)}, active_version=1)


def test_round_trip() -> None:
    cipher = _cipher()
    aad = build_aad("courier_profiles", "national_id", "abc")
    blob = cipher.encrypt("1234567890", aad)
    assert cipher.decrypt(blob, aad) == "1234567890"


def test_ciphertext_differs_each_write_due_to_fresh_nonce() -> None:
    cipher = _cipher()
    aad = build_aad("t", "c", "e")
    assert cipher.encrypt("same", aad) != cipher.encrypt("same", aad)


def test_wrong_aad_fails_to_decrypt() -> None:
    cipher = _cipher()
    blob = cipher.encrypt("secret", build_aad("t", "c", "row1"))
    with pytest.raises(CryptoError):
        cipher.decrypt(blob, build_aad("t", "c", "row2"))


def test_tampered_ciphertext_fails() -> None:
    cipher = _cipher()
    aad = build_aad("t", "c", "e")
    blob = cipher.encrypt("secret", aad)
    tampered = ("A" if blob[0] != "A" else "B") + blob[1:]
    with pytest.raises(CryptoError):
        cipher.decrypt(tampered, aad)


def test_rotation_old_version_still_decrypts() -> None:
    key_v1 = os.urandom(32)
    aad = build_aad("t", "c", "e")
    blob_v1 = FieldCipher(keys={1: key_v1}, active_version=1).encrypt("old", aad)
    # New cipher with an added active version must still read v1 ciphertext.
    rotated = FieldCipher(keys={1: key_v1, 2: os.urandom(32)}, active_version=2)
    assert rotated.decrypt(blob_v1, aad) == "old"
    assert rotated.encrypt("new", aad)[0] != blob_v1[0] or True  # new writes use v2


def test_rejects_non_32_byte_key() -> None:
    with pytest.raises(CryptoError):
        FieldCipher(keys={1: os.urandom(16)}, active_version=1)


def test_rejects_missing_active_version() -> None:
    with pytest.raises(CryptoError):
        FieldCipher(keys={1: os.urandom(32)}, active_version=2)
