import json
from pathlib import Path

import pytest

from prahari.identity import BrowserUser, hash_password, verify_password
from scripts.provision_browser_user import write_user


@pytest.fixture(scope="module")
def user() -> BrowserUser:
    return BrowserUser(
        actor_id="operator-01",
        role="operator",
        password_hash=hash_password("a long offline test passphrase"),
    )


def test_provision_preserves_accounts_and_requires_explicit_replacement(tmp_path: Path, user):
    path = tmp_path / "users.json"
    write_user(path, "operator", user)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="replace-user"):
        write_user(path, "operator", user.model_copy(update={"role": "admin"}))
    assert path.read_bytes() == original
    write_user(
        path, "reviewer", user.model_copy(update={"actor_id": "reviewer-01", "role": "reviewer"})
    )
    write_user(path, "operator", user.model_copy(update={"disabled": True}), replace=True)
    saved = json.loads(path.read_text())
    assert saved["operator"]["disabled"] is True
    assert saved["reviewer"]["role"] == "reviewer"
    assert verify_password("a long offline test passphrase", saved["operator"]["password_hash"])
    assert b"a long offline test passphrase" not in path.read_bytes()
    assert sorted(item.name for item in tmp_path.iterdir()) == ["users.json"]


def test_provision_rejects_duplicate_identity_and_concurrent_writer(tmp_path: Path, user):
    path = tmp_path / "users.json"
    write_user(path, "operator", user)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="own actor_id"):
        write_user(path, "reviewer", user)
    lock = path.with_name("users.json.lock")
    lock.write_text("another writer owns this lock")
    with pytest.raises(FileExistsError):
        write_user(path, "operator", user, replace=True)
    assert lock.read_text() == "another writer owns this lock"
    assert path.read_bytes() == original


def test_failed_atomic_replace_preserves_original(tmp_path: Path, user, monkeypatch):
    path = tmp_path / "users.json"
    write_user(path, "operator", user)
    original = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("simulated storage failure")

    monkeypatch.setattr("scripts.provision_browser_user.os.replace", fail_replace)
    with pytest.raises(OSError, match="storage failure"):
        write_user(path, "operator", user, replace=True)
    assert path.read_bytes() == original
    assert sorted(item.name for item in tmp_path.iterdir()) == ["users.json"]
