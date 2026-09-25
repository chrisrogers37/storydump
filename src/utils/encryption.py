"""Token encryption utility for secure database storage.

Supports key rotation via MultiFernet. Set ENCRYPTION_KEYS (comma-separated,
newest first) to enable rotation. Falls back to single ENCRYPTION_KEY for
backward compatibility.
"""

import binascii
from typing import Optional

from cryptography.fernet import Fernet, MultiFernet, InvalidToken

from src.config.settings import settings
from src.utils.logger import logger


class TokenEncryption:
    """
    Encrypt/decrypt sensitive tokens for database storage.

    Uses MultiFernet for key rotation support. Encrypts with the primary
    (first) key; decrypts by trying all keys in order.

    Key configuration (checked in order):
        1. ENCRYPTION_KEYS — comma-separated Fernet keys, newest first
        2. ENCRYPTION_KEY  — single key (backward compat, wrapped as MultiFernet)

    Rotation workflow:
        1. Generate new key: TokenEncryption.generate_key()
        2. Prepend to ENCRYPTION_KEYS: NEW_KEY,OLD_KEY
        3. Deploy — new tokens encrypt with NEW_KEY, old tokens still decrypt
        4. Enqueue the `reencrypt_credentials` system job (registered; its executor is
           not yet built) to re-encrypt every row with NEW_KEY
        5. Remove OLD_KEY from ENCRYPTION_KEYS

    Usage:
        encryption = TokenEncryption()
        encrypted = encryption.encrypt("my_secret_token")
        decrypted = encryption.decrypt(encrypted)
    """

    _instance: Optional["TokenEncryption"] = None
    _cipher: Optional[MultiFernet] = None

    def __new__(cls) -> "TokenEncryption":
        """Singleton pattern - reuse cipher instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    _fernets: Optional[list] = None

    def __init__(self):
        """Initialize encryption cipher with key(s) from settings."""
        if self._cipher is not None:
            return

        # A variable saved blank is absent, as every blank variable here is —
        # so a blank rotation list never shadows a working ENCRYPTION_KEY.
        keys_raw = (getattr(settings, "ENCRYPTION_KEYS", None) or "").strip()
        single_key = (settings.ENCRYPTION_KEY or "").strip()

        if keys_raw:
            source = "ENCRYPTION_KEYS"
            key_strings = [k.strip() for k in keys_raw.split(",") if k.strip()]
        elif single_key:
            source = "ENCRYPTION_KEY"
            key_strings = [single_key]
        else:
            # Both roots print this at a refused boot (#1401), so the remedy is
            # the production one first: a NEW key boots and then cannot read a
            # single stored credential.
            raise ValueError(
                "ENCRYPTION_KEY not configured (nor ENCRYPTION_KEYS). Where credentials"
                " are already stored, set the key they were encrypted with — a newly"
                " generated key cannot read them. Only on a first install, generate"
                ' one with: python -c "from src.utils.encryption import TokenEncryption;'
                ' print(TokenEncryption.generate_key())"'
            )

        # The message names the variable that was read and, for the rotation
        # list, the entry's position — never a value. A non-ASCII key's own
        # error would quote the offending character, so it is not repeated.
        fernets = []
        for position, key in enumerate(key_strings, 1):
            try:
                fernets.append(Fernet(key.encode()))
            except (ValueError, binascii.Error) as e:
                why = "it is not ASCII" if isinstance(e, UnicodeError) else str(e)
                where = (
                    f"entry {position} of {len(key_strings)} is not a Fernet key: {why}"
                    if source == "ENCRYPTION_KEYS"
                    else why
                )
                raise ValueError(f"Invalid {source} format: {where}") from None
        if not fernets:
            raise ValueError(f"Invalid {source} format: it names no key")
        self._fernets = fernets
        self._cipher = MultiFernet(fernets)

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a token with the primary (first) key.

        Args:
            plaintext: The sensitive token to encrypt

        Returns:
            Base64-encoded encrypted string (safe for database storage)
        """
        if not plaintext:
            raise ValueError("Cannot encrypt empty string")

        return self._cipher.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt a token, trying all configured keys in order.

        First attempts MultiFernet (fast path). On failure, falls back to
        trying each individual Fernet key so that a token encrypted with
        any key in ENCRYPTION_KEYS can be recovered.

        Args:
            ciphertext: The encrypted token from database

        Returns:
            Original plaintext token

        Raises:
            ValueError: If decryption fails with all keys or data is corrupted
        """
        if not ciphertext:
            raise ValueError("Cannot decrypt empty string")

        ciphertext_bytes = ciphertext.encode()

        # Fast path: MultiFernet tries all keys internally.
        try:
            return self._cipher.decrypt(ciphertext_bytes).decode()
        except InvalidToken:
            pass

        # Explicit per-key fallback — handles edge cases where MultiFernet
        # gives up early (e.g. version-byte mismatch before HMAC check).
        for i, fernet in enumerate(self._fernets):
            try:
                result = fernet.decrypt(ciphertext_bytes).decode()
                logger.info("Token decrypted by key index %s (per-key fallback)", i)
                return result
            except InvalidToken:
                continue

        key_count = len(self._fernets)
        logger.error("Token decryption failed — all %s keys exhausted", key_count)
        raise ValueError(
            f"Failed to decrypt token. "
            f"None of the {key_count} configured encryption keys can "
            f"decrypt this data, or the data is corrupted."
        )

    def rotate(self, ciphertext: str) -> str:
        """
        Re-encrypts with the primary key unconditionally.

        Output ciphertext will differ from input due to fresh IV and
        timestamp.

        Args:
            ciphertext: The encrypted token to re-encrypt

        Returns:
            Token re-encrypted with the primary key

        Raises:
            ValueError: If decryption fails (no matching key or corrupted data)

        Waits on the `reencrypt_credentials` executor
        (`work_loop.UNBUILT_KINDS`); driven today only by
        `tests/src/utils/test_encryption.py` (#1325 audit, TD-C16).
        """
        if not ciphertext:
            raise ValueError("Cannot rotate empty string")

        try:
            return self._cipher.rotate(ciphertext.encode()).decode()
        except InvalidToken:
            logger.error("Token rotation failed - no matching key or corrupted data")
            raise ValueError(
                "Failed to rotate token. "
                "This may indicate none of the configured keys can decrypt "
                "this data, or the data is corrupted."
            )

    @staticmethod
    def generate_key() -> str:
        """
        Generate a new encryption key.

        Run this once during initial setup and store the result in .env:
            ENCRYPTION_KEY=<generated_key>

        For rotation, prepend the new key to ENCRYPTION_KEYS:
            ENCRYPTION_KEYS=<new_key>,<old_key>

        Returns:
            A new Fernet key (base64-encoded, 44 characters)
        """
        return Fernet.generate_key().decode()

    @classmethod
    def reset(cls) -> None:
        """
        Reset the singleton instance.

        Useful for testing or when encryption key changes.
        """
        cls._instance = None
        cls._cipher = None
