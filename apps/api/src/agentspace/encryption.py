"""Encrypts users' secrets (MCP `env` values) before they reach the database (ADR-025).

AES-256-GCM, after CompanyOS's `secrets/account-resource.ts`: each user gets
their own key, derived from one server secret with HKDF, so a value copied into
another user's row can't be read there.
"""

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

VERSION = "v1"
"""Starts every stored value, so a later scheme can tell its own values from these."""
NONCE_BYTES = 12


class SecretError(Exception):
    """A stored value can't be decrypted: the server secret changed, or the value isn't ours."""


class SecretBox:
    """Encrypts and decrypts one user's values with a key derived from the server secret."""

    def __init__(self, server_secret: str) -> None:
        self._secret = server_secret.encode()

    def _key(self, user_id: str) -> AESGCM:
        """The user's own AES key; the same secret and user id always give the same key."""
        hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"agentspace-user-secret:{user_id}".encode())
        return AESGCM(hkdf.derive(self._secret))

    def encrypt(self, user_id: str, value: str) -> str:
        """`v1:<nonce + ciphertext, base64url>`; a fresh random nonce each time, so equal values look different."""
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._key(user_id).encrypt(nonce, value.encode(), None)
        return f"{VERSION}:{base64.urlsafe_b64encode(nonce + sealed).decode()}"

    def decrypt(self, user_id: str, stored: str) -> str:
        """The plain value; raises `SecretError` if it was encrypted with another secret or for another user."""
        # TS: str.partition is like split(":", 2) that always returns three parts.
        version, _, data = stored.partition(":")
        if version != VERSION:
            raise SecretError("This value isn't in a format the API can read.")
        try:
            raw = base64.urlsafe_b64decode(data)
            return self._key(user_id).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], None).decode()
        except (InvalidTag, ValueError) as exc:
            raise SecretError("Can't decrypt a saved secret: was AGENTSPACE_SECRET_KEY changed?") from exc

    def encrypt_dict(self, user_id: str, values: dict[str, str]) -> dict[str, str]:
        """Every value encrypted; the keys (variable names) stay readable."""
        # TS: Object.fromEntries(Object.entries(values).map(([k, v]) => [k, encrypt(v)]))
        return {name: self.encrypt(user_id, value) for name, value in values.items()}

    def decrypt_dict(self, user_id: str, values: dict[str, str]) -> dict[str, str]:
        """Every value decrypted."""
        return {name: self.decrypt(user_id, value) for name, value in values.items()}


def load_server_secret(secret_key: str | None, data_dir: Path) -> str:
    """`AGENTSPACE_SECRET_KEY` if set, else a key generated once into `<data_dir>/secret.key`."""
    if secret_key:
        return secret_key
    path = data_dir / "secret.key"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Created readable by this user only in one step (no moment with wider permissions), and only if missing:
        # O_EXCL fails if another process made it first, so two starting together never write different keys.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    # TS: `with` closes the file when the block ends, like `await using` or try/finally.
    with os.fdopen(fd, "w") as file:
        file.write(secrets.token_urlsafe(32))
    log.warning("AGENTSPACE_SECRET_KEY is not set: generated %s. Back it up: without it, MCP env is lost.", path)
    return path.read_text().strip()
