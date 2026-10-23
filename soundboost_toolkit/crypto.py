"""Cryptographic protocol engine for SoundBoost.ai (Diktatorial Suite).

Implements the exact AES-CBC ciphers used across studio.soundboost.ai,
mastering.soundboost.ai, converta.soundboost.ai, and shoots.soundboost.ai:
1. Static URL Parameter Cipher (Studio Module 616): AES-128-CBC + PKCS7 + Base64URL
   with Known-Answer Self-Test (KAT: ID 1 -> 'Hck7LVPBFsyNC95Qzf6-0Q').
2. Time-Windowed UI Token Cipher (Studio Module 446): AES-128-CBC + PKCS7 with
   prepended 16-byte CSPRNG IV for `ui-token` request signing.
3. OpenSSL EVP_BytesToKey Cookie Cipher (Studio Module 459): Compatible with
   CryptoJS passphrase encryption used for `_d_a`, `_d_e`, `_d_i`, and `_d_u` cookies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from soundboost_toolkit.constants import (
    DEFAULT_COOKIE_CIPHER_VAL,
    DEFAULT_PARAM_CIPHER_VAL,
    DEFAULT_PARAM_IV_HEX,
    DEFAULT_TOKEN_CIPHER_VAL,
    DEFAULT_UI_HEADER_VAL,
    PRODUCTION_KAT_HASH,
    PRODUCTION_KAT_ID,
)


def _derive_clamped_16byte_key(raw_key: str) -> bytes:
    """Replicate CryptoJS Utf8.parse(key) + clamp() + 4-word (16-byte) slice/zero-pad."""
    if not raw_key:
        raise ValueError("Cipher key cannot be empty")
    utf8_bytes = raw_key.encode("utf-8")
    if len(utf8_bytes) >= 16:
        return utf8_bytes[:16]
    return utf8_bytes.ljust(16, b"\x00")


def _to_base64url_unpadded(b64_str: str) -> str:
    """Convert standard Base64 string to unpadded URL-safe Base64 (Module 616 `f`)."""
    return b64_str.replace("+", "-").replace("/", "_").rstrip("=")


def _from_base64url_to_bytes(b64url_str: str) -> bytes:
    """Convert URL-safe Base64 string back to raw bytes (Module 616 `s`)."""
    normalized = b64url_str.replace("-", "+").replace("_", "/")
    pad_len = (4 - (len(normalized) % 4)) % 4
    normalized += "=" * pad_len
    return base64.b64decode(normalized)


def _pkcs7_pad(data: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    return padder.update(data) + padder.finalize()


def _pkcs7_unpad(padded_data: bytes) -> bytes:
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded_data) + unpadder.finalize()


def encrypt_param_id(
    value: int | str,
    cipher_key: str | None = None,
    iv_hex: str | None = None,
) -> str:
    """Encrypt a track/original ID into a URL-safe SoundBoost `?track=` hash (Module 616)."""
    key_str = cipher_key or os.environ.get("SOUNDBOOST_PARAM_CIPHER", DEFAULT_PARAM_CIPHER_VAL)
    iv_str = (iv_hex or os.environ.get("SOUNDBOOST_PARAM_IV", DEFAULT_PARAM_IV_HEX)).strip()
    if iv_str.lower().startswith("0x"):
        iv_str = iv_str[2:]

    key_bytes = _derive_clamped_16byte_key(key_str)
    iv_bytes = bytes.fromhex(iv_str)
    if len(iv_bytes) != 16:
        raise ValueError(f"Expected 16-byte IV (32 hex chars), got {len(iv_bytes)} bytes")

    serialized = json.dumps(value, separators=(",", ":")).encode("utf-8")
    padded = _pkcs7_pad(serialized)

    encryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    b64 = base64.b64encode(ciphertext).decode("ascii")
    return _to_base64url_unpadded(b64)


def decrypt_param_id(
    token: str,
    cipher_key: str | None = None,
    iv_hex: str | None = None,
) -> int | str:
    """Decrypt a SoundBoost `?track=` URL hash back into its original track ID (Module 616)."""
    stripped = token.strip()
    if stripped.isdigit() and not stripped.startswith("0"):
        return int(stripped)

    key_str = cipher_key or os.environ.get("SOUNDBOOST_PARAM_CIPHER", DEFAULT_PARAM_CIPHER_VAL)
    iv_str = (iv_hex or os.environ.get("SOUNDBOOST_PARAM_IV", DEFAULT_PARAM_IV_HEX)).strip()
    if iv_str.lower().startswith("0x"):
        iv_str = iv_str[2:]

    key_bytes = _derive_clamped_16byte_key(key_str)
    iv_bytes = bytes.fromhex(iv_str)
    ciphertext = _from_base64url_to_bytes(stripped)

    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadded = _pkcs7_unpad(padded)
    result: int | str = json.loads(unpadded.decode("utf-8"))
    return result


def verify_param_cipher_kat() -> bool:
    """Run Studio Module 616's Known-Answer Self-Test (`1` -> `'Hck7LVPBFsyNC95Qzf6-0Q'`)."""
    encrypted = encrypt_param_id(
        PRODUCTION_KAT_ID,
        cipher_key=DEFAULT_PARAM_CIPHER_VAL,
        iv_hex=DEFAULT_PARAM_IV_HEX,
    )
    decrypted = decrypt_param_id(
        PRODUCTION_KAT_HASH,
        cipher_key=DEFAULT_PARAM_CIPHER_VAL,
        iv_hex=DEFAULT_PARAM_IV_HEX,
    )
    return encrypted == PRODUCTION_KAT_HASH and decrypted == PRODUCTION_KAT_ID


def generate_ui_token(
    ui_api_key: str | None = None,
    token_cipher_key: str | None = None,
    timestamp_iso: str | None = None,
) -> str:
    """Generate a time-windowed `ui-token` header value (Studio Module 446 `encryptToken`)."""
    api_key = ui_api_key or os.environ.get("SOUNDBOOST_UI_API_KEY", DEFAULT_UI_HEADER_VAL)
    secret_str = token_cipher_key or os.environ.get(
        "SOUNDBOOST_TOKEN_CIPHER", DEFAULT_TOKEN_CIPHER_VAL
    )
    key_bytes = secret_str.encode("utf-8")
    if len(key_bytes) not in (16, 24, 32):
        key_bytes = _derive_clamped_16byte_key(secret_str)

    if timestamp_iso is None:
        now = datetime.now(UTC)
        timestamp_iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    payload_json = json.dumps(f"{api_key}_{timestamp_iso}").encode("utf-8")
    iv_bytes = secrets.token_bytes(16)
    padded = _pkcs7_pad(payload_json)

    encryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    combined = iv_bytes + ciphertext
    return base64.b64encode(combined).decode("ascii")


def decrypt_ui_token(
    token_b64: str,
    token_cipher_key: str | None = None,
) -> str:
    """Decrypt a `ui-token` header value back into `<ui_api_key>_<iso_timestamp>` (Module 446)."""
    secret_str = token_cipher_key or os.environ.get(
        "SOUNDBOOST_TOKEN_CIPHER", DEFAULT_TOKEN_CIPHER_VAL
    )
    key_bytes = secret_str.encode("utf-8")
    if len(key_bytes) not in (16, 24, 32):
        key_bytes = _derive_clamped_16byte_key(secret_str)

    raw = base64.b64decode(token_b64)
    if len(raw) < 32:
        raise ValueError("Invalid ui-token payload length")
    iv_bytes = raw[:16]
    ciphertext = raw[16:]

    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadded = _pkcs7_unpad(padded)
    decoded: str = json.loads(unpadded.decode("utf-8"))
    return decoded


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey MD5 key/IV derivation used by CryptoJS passphrase mode."""
    derived = b""
    block = b""
    while len(derived) < (key_len + iv_len):
        block = hashlib.md5(block + password + salt, usedforsecurity=False).digest()
        derived += block
    return derived[:key_len], derived[key_len : key_len + iv_len]


def encrypt_cookie_value(
    value: Any,
    cookie_cipher_key: str | None = None,
) -> str:
    """Encrypt a cookie value (`_d_a`, `_d_e`, `_d_i`, `_d_u`) using CryptoJS OpenSSL format (Module 459)."""
    passphrase = (
        cookie_cipher_key
        or os.environ.get("SOUNDBOOST_COOKIE_CIPHER", DEFAULT_COOKIE_CIPHER_VAL)
    ).encode("utf-8")
    salt = secrets.token_bytes(8)
    key_bytes, iv_bytes = _evp_bytes_to_key(passphrase, salt, key_len=32, iv_len=16)

    plaintext = json.dumps(value).encode("utf-8")
    padded = _pkcs7_pad(plaintext)
    encryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    openssl_blob = b"Salted__" + salt + ciphertext
    return base64.b64encode(openssl_blob).decode("ascii")


def decrypt_cookie_value(
    encrypted_cookie: str,
    cookie_cipher_key: str | None = None,
) -> Any:
    """Decrypt a SoundBoost `_d_a` / `_d_u` / `_d_e` / `_d_i` cookie (Module 459)."""
    passphrase = (
        cookie_cipher_key
        or os.environ.get("SOUNDBOOST_COOKIE_CIPHER", DEFAULT_COOKIE_CIPHER_VAL)
    ).encode("utf-8")
    cleaned = unquote(encrypted_cookie.strip())
    raw = base64.b64decode(cleaned)
    if not raw.startswith(b"Salted__") or len(raw) < 16:
        raise ValueError("Cookie payload does not have OpenSSL 'Salted__' header")
    salt = raw[8:16]
    ciphertext = raw[16:]
    key_bytes, iv_bytes = _evp_bytes_to_key(passphrase, salt, key_len=32, iv_len=16)

    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadded = _pkcs7_unpad(padded)
    return json.loads(unpadded.decode("utf-8"))


def build_ui_security_headers(
    ui_api_key: str | None = None,
    token_cipher_key: str | None = None,
) -> dict[str, str]:
    """Build the `ui-api-key` and `ui-token` headers required by SoundBoost endpoints (Module 464)."""
    api_key = ui_api_key or os.environ.get("SOUNDBOOST_UI_API_KEY", DEFAULT_UI_HEADER_VAL)
    return {
        "ui-api-key": api_key,
        "ui-token": generate_ui_token(ui_api_key=api_key, token_cipher_key=token_cipher_key),
    }


def compute_mv_spending_limit(quoted_credits: int) -> int:
    """Calculate the AI Music Video (`h3-max`) spending cap (`ec(quote)` in Chunk 175).

    Formula: credits + 3 * floor(credits / 20) + ceil(((credits % 20) * 3) / 20)
    """
    if quoted_credits <= 0:
        return 0
    return (
        quoted_credits
        + 3 * (quoted_credits // 20)
        + math.ceil(((quoted_credits % 20) * 3) / 20)
    )
