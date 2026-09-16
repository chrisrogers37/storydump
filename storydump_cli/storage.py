"""Where the bearer token lives (decision F2, the ``gh`` pattern).

Three stores and one order. ``STORYDUMP_TOKEN`` wins whenever it is set —
that is the door for agents and CI. Otherwise the store the config names:
the OS keychain by default, or a 0600 file the person chose on purpose with
``login --insecure-storage``.

The keychain backend fails closed. ``keyring`` picks the highest-priority
backend it can find and, on a headless host, that is a plaintext file
(``keyrings.alt``) or a stub that stores nothing — worse than the file the
person could have chosen, and silent about it. So the backend judges the
active store by its class's module and accepts only the four real OS
stores (a chain of them counts; a chain with anything else in it does not).
Anything else is a ``StorageUnavailable`` that names the fix. ``keyring``
is imported lazily: it ships in the ``storydump[cli]`` extra, so importing
this package must never need it, and tests hand in a fake module instead.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from storydump_cli.config import ConfigDirectoryUnusable, ensure_dir, write_private

TOKEN_ENV = "STORYDUMP_TOKEN"
TOKEN_FILE = "token"
KEYCHAIN_SERVICE = "storydump"

#: The modules whose backends are real OS stores. A class from anywhere
#: else — ``keyring.backends.fail``, ``keyring.backends.null``,
#: ``keyrings.alt.*`` — is refused.
OS_STORE_MODULES = (
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.Windows",
    "keyring.backends.kwallet",
)
CHAINER_MODULE = "keyring.backends.chainer"

NO_KEYCHAIN_FIX = (
    "use `storydump login --insecure-storage` for a 0600 file, or set STORYDUMP_TOKEN"
)


class StorageUnavailable(Exception):
    """The store cannot be used as it is; ``fix`` says what would make it usable."""

    def __init__(self, detail: str, fix: str) -> None:
        super().__init__(f"{detail} — {fix}")
        self.detail = detail
        self.fix = fix


class Backend(Protocol):
    def get(self) -> Optional[str]: ...

    def set(self, secret: str) -> None: ...

    def delete(self) -> None: ...


def token_path(config_dir: Path) -> Path:
    """The file backend's path: ``<config dir>/token``."""
    return config_dir / TOKEN_FILE


class _NoError(Exception):
    """Raised by nothing: the exception type the keychain backend catches
    when the keyring module in hand (a test's fake) has no ``errors``."""


def _module_of(store: Any) -> str:
    return getattr(type(store), "__module__", "") or ""


def _is_os_store(store: Any) -> bool:
    module = _module_of(store)
    if module == CHAINER_MODULE or module.startswith(CHAINER_MODULE + "."):
        members = list(getattr(store, "backends", None) or [])
        return bool(members) and all(_is_os_store(member) for member in members)
    return any(module == ok or module.startswith(ok + ".") for ok in OS_STORE_MODULES)


class KeychainBackend:
    """The OS keychain through ``keyring``: service ``storydump``, username
    the API host, so a staging token and a production token never collide.
    The store is resolved at first use, never at construction, so building
    one costs nothing and a runtime that never needs it never imports
    ``keyring``."""

    def __init__(self, host: str, keyring_module: Any = None) -> None:
        self.host = host
        self._keyring = keyring_module
        self._store: Any = None
        self._error_type: type = _NoError

    def _resolve(self) -> Any:
        if self._store is not None:
            return self._store
        keyring = self._keyring
        if keyring is None:
            try:
                import keyring  # type: ignore[no-redef]
            except ImportError as exc:
                raise StorageUnavailable(
                    "the keyring package is not installed (pip install 'storydump[cli]')",
                    NO_KEYCHAIN_FIX,
                ) from exc
        store = keyring.get_keyring()
        if not _is_os_store(store):
            chosen = f"{_module_of(store)}.{type(store).__name__}"
            raise StorageUnavailable(
                f"no OS keychain here (keyring chose {chosen})", NO_KEYCHAIN_FIX
            )
        errors = getattr(keyring, "errors", None)
        self._error_type = getattr(errors, "KeyringError", None) or _NoError
        self._store = store
        return store

    def get(self) -> Optional[str]:
        store = self._resolve()
        try:
            value = store.get_password(KEYCHAIN_SERVICE, self.host)
        except self._error_type as exc:
            raise StorageUnavailable(
                f"the keychain refused to answer: {exc}",
                "unlock the keychain, or " + NO_KEYCHAIN_FIX,
            ) from exc
        return value or None

    def set(self, secret: str) -> None:
        store = self._resolve()
        try:
            store.set_password(KEYCHAIN_SERVICE, self.host, secret)
        except self._error_type as exc:
            raise StorageUnavailable(
                f"the keychain refused the write: {exc}",
                "unlock the keychain, or " + NO_KEYCHAIN_FIX,
            ) from exc

    def delete(self) -> None:
        """Idempotent: nothing stored is not an error (logout twice is fine).
        A locked or broken store is the same refusal `get` and `set` give."""
        if self.get() is not None:
            store = self._resolve()
            try:
                store.delete_password(KEYCHAIN_SERVICE, self.host)
            except self._error_type as exc:
                raise StorageUnavailable(
                    "the keychain refused to delete the token — unlock it",
                    NO_KEYCHAIN_FIX,
                ) from exc


class FileBackend:
    """A 0600 file under the config directory, only ever by explicit opt-in.
    Reading refuses a file whose mode is not exactly 0600: a token anyone
    else can read is already compromised, and reading it anyway would hide
    that."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> Optional[str]:
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            return None
        if mode != 0o600:
            raise StorageUnavailable(
                f"{self.path} is mode {mode:04o}, not 0600 — refusing to read a token"
                " others can read",
                f"chmod 600 {self.path}, or run `storydump login --insecure-storage` again",
            )
        return self.path.read_text(encoding="utf-8").strip() or None

    def set(self, secret: str) -> None:
        ensure_dir(self.path.parent)
        write_private(self.path, secret + "\n")

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # a file where the config directory should be, no permission: the
            # local configuration's fault, answered as such (never a traceback)
            raise ConfigDirectoryUnusable(self.path.parent, exc) from exc


class EnvBackend:
    """``STORYDUMP_TOKEN``: read-only by nature — a shell variable is not a
    place the CLI writes to, so ``set`` and ``delete`` say so."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env

    def get(self) -> Optional[str]:
        return (self.env.get(TOKEN_ENV) or "").strip() or None

    def set(self, secret: str) -> None:
        raise StorageUnavailable(
            f"{TOKEN_ENV} is set and wins over any stored token",
            f"unset {TOKEN_ENV} to sign in with a stored token",
        )

    def delete(self) -> None:
        raise StorageUnavailable(
            f"{TOKEN_ENV} is set in this shell, not stored by the CLI",
            f"unset {TOKEN_ENV}",
        )


class MemoryBackend:
    """For tests: a store that forgets when the process does."""

    def __init__(self) -> None:
        self.secret: Optional[str] = None

    def get(self) -> Optional[str]:
        return self.secret

    def set(self, secret: str) -> None:
        self.secret = secret

    def delete(self) -> None:
        self.secret = None


def resolve_token(
    *,
    env: Mapping[str, str],
    config_dir: Path,
    token_storage: str,
    keychain: Backend,
) -> Optional[str]:
    """The token to send: ``STORYDUMP_TOKEN`` first, then the store the
    config names — the file under *config_dir* or *keychain*."""
    from_env = EnvBackend(env).get()
    if from_env:
        return from_env
    if token_storage == "file":
        return FileBackend(token_path(config_dir)).get()
    return keychain.get()
