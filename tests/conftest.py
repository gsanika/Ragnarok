import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Tests must never use your real mail/cloud credentials (e.g. SMTP_* set in your shell)."""
    for k in list(os.environ):
        if k.startswith(("SMTP_", "IMAP_", "GOOGLE_", "MS_")) or k in ("RAGNAROK_PASSPHRASE", "RAGNAROK_PROJECTS"):
            monkeypatch.delenv(k, raising=False)
