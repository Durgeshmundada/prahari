"""Provision an offline browser account without putting passwords in argv or logs."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import tempfile
from pathlib import Path

from prahari.identity import BrowserUser, hash_password


def write_user(path: Path, username: str, user: BrowserUser, *, replace: bool = False) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@-]{0,99}", username):
        raise ValueError("use a lowercase username of 1-100 identifier characters")
    if path.is_symlink():
        raise ValueError("users file must not be a symbolic link")
    # O_EXCL also makes a concurrent provisioner fail rather than overwrite changes.
    lock_path = path.with_name(path.name + ".lock")
    lock = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temporary: str | None = None
    try:
        users = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(users, dict):
            raise ValueError("users file must contain an object")
        for value in users.values():
            BrowserUser.model_validate(value)
        if username in users and not replace:
            raise ValueError(
                "user exists; pass --replace-user to rotate its credentials explicitly"
            )
        if any(
            name != username and value["actor_id"] == user.actor_id for name, value in users.items()
        ):
            raise ValueError("each account must have its own actor_id")
        users[username] = {
            "actor_id": user.actor_id,
            "role": user.role,
            "password_hash": user.password_hash.get_secret_value(),
            "disabled": user.disabled,
        }
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(users, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if path.exists() and hasattr(os, "chown"):
            owner = path.stat()
            os.chown(temporary, owner.st_uid, owner.st_gid)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        os.close(lock)
        lock_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument(
        "--role", choices=["viewer", "operator", "reviewer", "admin"], required=True
    )
    parser.add_argument("--replace-user", action="store_true")
    args = parser.parse_args()
    password = getpass.getpass("New passphrase (15-256 characters): ")
    if password != getpass.getpass("Repeat passphrase: "):
        raise SystemExit("Passphrases do not match; no file was changed.")
    user = BrowserUser(
        actor_id=args.actor_id, role=args.role, password_hash=hash_password(password)
    )
    write_user(args.file, args.username, user, replace=args.replace_user)
    print(f"Account saved to {args.file}. Protect this file as a credential secret.")


if __name__ == "__main__":
    main()
