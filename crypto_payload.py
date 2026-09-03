"""Encryption helpers shared by the cloud builder and the browser PWA.

The JSON envelope is intentionally compatible with the original FG bot:
PBKDF2-HMAC-SHA256 derives an AES-256-GCM key from the user's passcode.
Only ciphertext is published.  The passcode is supplied as a GitHub Actions
secret and never written to the site directory.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


DEFAULT_ITERATIONS = 310_000


def _derive_key(passcode: str, salt: bytes, iterations: int) -> bytes:
    if not passcode:
        raise ValueError("The bot passcode is empty.")
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    ).derive(passcode.encode("utf-8"))


def encrypt_payload(
    payload: dict[str, Any],
    passcode: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> dict[str, Any]:
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive_key(passcode, salt, iterations)
    plaintext = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    compressed = gzip.compress(plaintext, compresslevel=9, mtime=0)
    ciphertext = AESGCM(key).encrypt(iv, compressed, None)
    return {
        "v": 3,
        "zip": "gzip",
        "iter": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_payload(envelope: dict[str, Any], passcode: str) -> dict[str, Any]:
    iterations = int(envelope["iter"])
    salt = base64.b64decode(envelope["salt"])
    iv = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["ct"])
    key = _derive_key(passcode, salt, iterations)
    plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    if envelope.get("zip") == "gzip":
        plaintext = gzip.decompress(plaintext)
    value = json.loads(plaintext.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Decrypted inventory payload is not a JSON object.")
    return value
