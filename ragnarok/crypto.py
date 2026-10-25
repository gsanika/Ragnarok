"""Encryption at rest (NFR1.2): user-held key, field-level authenticated encryption.

Key sources, in order: RAGNAROK_PASSPHRASE (scrypt-derived, salt stored beside the DB),
else a random key file in the data dir (chmod 600) that the user can move/back up.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


class Vault:
    def __init__(self, data_dir: Path, passphrase: str | None = None):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        passphrase = passphrase if passphrase is not None else os.environ.get("RAGNAROK_PASSPHRASE")
        if passphrase:
            salt_file = data_dir / "salt.bin"
            if not salt_file.exists():
                salt_file.write_bytes(os.urandom(16))
            kdf = Scrypt(salt=salt_file.read_bytes(), length=32, n=2**14, r=8, p=1)
            key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))
        else:
            key_file = data_dir / "key.bin"
            if not key_file.exists():
                key_file.write_bytes(Fernet.generate_key())
                os.chmod(key_file, 0o600)
            key = key_file.read_bytes()
        self._f = Fernet(key)

    def encrypt(self, text: str) -> bytes:
        return self._f.encrypt(text.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str:
        try:
            return self._f.decrypt(bytes(blob)).decode("utf-8")
        except InvalidToken as e:
            raise ValueError("wrong key or corrupted memory") from e
