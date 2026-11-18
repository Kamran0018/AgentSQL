"""
Data-Agent AI — Encryption Utilities
====================================
Uses cryptography.fernet to encrypt and decrypt sensitive database connection strings.
"""

import base64
import hashlib
import os
from cryptography.fernet import Fernet
from django.conf import settings


def _get_fernet_key() -> bytes:
    """
    Retrieve encryption key from DATASOURCE_ENCRYPTION_KEY env var,
    or derive a deterministic 32-byte urlsafe base64 key from settings.SECRET_KEY.
    """
    env_key = os.getenv("DATASOURCE_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    
    # Derive from SECRET_KEY using SHA256
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt(plain_text: str) -> str:
    """Encrypt plain text string and return ascii ciphertext."""
    if not plain_text:
        return ""
    f = Fernet(_get_fernet_key())
    return f.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt(cipher_text: str) -> str:
    """Decrypt ciphertext string and return plain text."""
    if not cipher_text:
        return ""
    f = Fernet(_get_fernet_key())
    return f.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
