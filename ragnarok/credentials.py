"""Encrypted storage for logins (SMTP etc.), using the same user-held key as memory.
Environment variables still win, so existing setups keep working."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .crypto import Vault


class CredentialStore:
    def __init__(self, data_dir: Path, vault: Vault | None = None):
        self.dir = Path(data_dir)
        self.path = self.dir / "credentials.enc"
        self._vault = vault

    @property
    def vault(self) -> Vault:
        if self._vault is None:
            self._vault = Vault(self.dir)
        return self._vault

    def _load(self) -> dict:
        return json.loads(self.vault.decrypt(self.path.read_bytes())) if self.path.exists() else {}

    def get(self, name: str) -> dict | None:
        return self._load().get(name)

    def put(self, name: str, value: dict) -> None:
        d = self._load(); d[name] = value
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self.vault.encrypt(json.dumps(d)))
        try: os.chmod(self.path, 0o600)
        except OSError: pass

    def remove(self, name: str) -> bool:
        d = self._load(); gone = d.pop(name, None) is not None
        self.path.write_bytes(self.vault.encrypt(json.dumps(d)))
        return gone
