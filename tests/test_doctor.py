import pytest

from ragnarok import __version__
from ragnarok.cli import main
from ragnarok.credentials import CredentialStore


def run_doctor(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("RAGNAROK_HOME", str(tmp_path))
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")          # unreachable on purpose
    with pytest.raises(SystemExit):
        main(["doctor"])
    return capsys.readouterr().out


def test_doctor_shows_version_data_folder_and_email_state(capsys, tmp_path, monkeypatch):
    out = run_doctor(capsys, tmp_path, monkeypatch)
    assert f"v{__version__}" in out and str(tmp_path.resolve()) in out and "email not set up in this data folder" in out
    CredentialStore(tmp_path).put("smtp", {"host": "h", "user": "me@x.com", "password": "p"})
    assert "email sending is set up (me@x.com)" in run_doctor(capsys, tmp_path, monkeypatch)


def test_doctor_flags_unreadable_saved_login(capsys, tmp_path, monkeypatch):
    (tmp_path / "credentials.enc").write_bytes(b"garbage")
    assert "cannot be read" in run_doctor(capsys, tmp_path, monkeypatch)
