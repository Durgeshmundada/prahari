"""Offline human identities and versioned, memory-hard password hashes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

PASSWORD_PREFIX = "scrypt$16384$8$5"
DUMMY_PASSWORD_HASH = f"{PASSWORD_PREFIX}${'00' * 16}${'00' * 32}"


class BrowserUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,99}$")
    role: Literal["viewer", "operator", "reviewer", "admin"]
    password_hash: SecretStr
    disabled: bool = False

    @field_validator("password_hash")
    @classmethod
    def validate_password_hash(cls, value: SecretStr) -> SecretStr:
        _password_parts(value.get_secret_value())
        return value

    @property
    def credential_fingerprint(self) -> str:
        payload = f"{self.actor_id}\0{self.role}\0{self.password_hash.get_secret_value()}"
        return hashlib.sha256(payload.encode()).hexdigest()


def _password_parts(encoded: str) -> tuple[bytes, bytes]:
    parts = encoded.split("$")
    if len(parts) != 6 or "$".join(parts[:4]) != PASSWORD_PREFIX:
        raise ValueError("password hash must use the supported scrypt parameters")
    try:
        salt, digest = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError as error:
        raise ValueError("invalid password hash encoding") from error
    if len(salt) != 16 or len(digest) != 32:
        raise ValueError("invalid password hash lengths")
    return salt, digest


def hash_password(password: str) -> str:
    if not 15 <= len(password) <= 256:
        raise ValueError("use a password or passphrase containing 15-256 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=5, dklen=32)
    return f"{PASSWORD_PREFIX}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    salt, expected = _password_parts(encoded)
    actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=5, dklen=32)
    return hmac.compare_digest(actual, expected)
