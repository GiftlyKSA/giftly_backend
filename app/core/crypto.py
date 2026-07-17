"""Field-level encryption for Restricted data (SPEC SECTION 17.1).

AES-256-GCM (AEAD) with env-var key management and versioning. Implemented ONCE
here; nothing elsewhere in the codebase may inline crypto. The Restricted fields
this protects: passport/national ids, message content, the chat preview, IBANs.

On-disk format (base64-encoded):
    key_version_byte(1) || IV(12) || ciphertext || tag(16)

The AAD binds each ciphertext to ``"{table}:{column}:{owning_entity_id}"`` so a
ciphertext copied between rows fails to decrypt. The IV is 12 fresh CSPRNG bytes
per write — nonce reuse under one key catastrophically breaks GCM, so it is never
derived from a row id, counter, or timestamp.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_BYTES = 12
_KEY_BYTES = 32
_VERSION_BYTES = 1


class CryptoError(Exception):
    """Raised on any encryption/decryption failure, including auth-tag mismatch."""


class FieldCipher:
    """Versioned AES-256-GCM cipher over a map of key-version -> 32-byte key.

    Decryption accepts any version present in the map so old ciphertext still reads
    during a rotation; encryption always uses the configured active version.
    """

    def __init__(self, keys: dict[int, bytes], active_version: int) -> None:
        """Validate and hold the key map.

        Args:
            keys: Mapping of version int -> raw 32-byte key.
            active_version: The version new writes are encrypted under; must be
                present in ``keys``.

        Raises:
            CryptoError: A key is not 32 bytes, or the active version is missing.
        """
        for version, key in keys.items():
            if len(key) != _KEY_BYTES:
                raise CryptoError(f"Encryption key version {version} is not 32 bytes.")
            if not 0 <= version <= 255:
                raise CryptoError("Key version must fit in a single byte (0-255).")
        if active_version not in keys:
            raise CryptoError("Active key version is not present in the key map.")
        self._keys = keys
        self._active_version = active_version

    def encrypt(self, plaintext: str, aad: str) -> str:
        """Encrypt a plaintext string under the active key, bound to ``aad``.

        Returns:
            Base64 of ``version || iv || ciphertext || tag``.
        """
        key = self._keys[self._active_version]
        iv = os.urandom(_IV_BYTES)  # SECURITY: fresh CSPRNG nonce every write.
        ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), aad.encode("utf-8"))
        blob = bytes([self._active_version]) + iv + ct
        return base64.b64encode(blob).decode("ascii")

    def decrypt(self, blob_b64: str, aad: str) -> str:
        """Decrypt a stored blob, verifying the auth tag and the bound ``aad``.

        Raises:
            CryptoError: The blob is malformed, the version is unknown, or the tag
                (or AAD) does not verify — meaning tampering or a mismatched context.
        """
        try:
            blob = base64.b64decode(blob_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise CryptoError("Ciphertext is not valid base64.") from exc
        if len(blob) < _VERSION_BYTES + _IV_BYTES + 16:
            raise CryptoError("Ciphertext blob is too short.")
        version = blob[0]
        iv = blob[1 : 1 + _IV_BYTES]
        ct = blob[1 + _IV_BYTES :]
        key = self._keys.get(version)
        if key is None:
            raise CryptoError(f"No key for version {version}.")
        try:
            pt = AESGCM(key).decrypt(iv, ct, aad.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — cryptography raises InvalidTag et al.
            raise CryptoError("Decryption failed (bad tag, key, or AAD).") from exc
        return pt.decode("utf-8")


def build_aad(table: str, column: str, entity_id: str) -> str:
    """Build the AAD context string that binds a ciphertext to its row and column."""
    return f"{table}:{column}:{entity_id}"


def build_cipher(keys: dict[int, bytes], active_version: int) -> FieldCipher:
    """Construct a :class:`FieldCipher` from a decoded key map and active version."""
    return FieldCipher(keys=keys, active_version=active_version)
