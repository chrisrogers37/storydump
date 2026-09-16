"""The CLI's configuration: where the API is, and where the token lives.

One directory — ``$STORYDUMP_CONFIG_DIR`` or ``~/.config/storydump`` — holding
``config.json`` with two keys, ``api_url`` and ``token_storage`` (``"keychain"``
or ``"file"``). JSON rather than TOML because CI runs Python 3.10 and
``tomllib`` arrived in 3.11; a third-party parser for two keys is not worth
a dependency. The directory is 0700 and the file 0600: the config names no
secret, but the token file that may sit beside it does, and one rule for
the whole directory is simpler than two.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from src.services.target.vocabulary import API_URL
from pathlib import Path
from typing import Any, Mapping, Optional

DEFAULT_API_URL = API_URL
CONFIG_DIR_ENV = "STORYDUMP_CONFIG_DIR"
API_URL_ENV = "STORYDUMP_API"
CONFIG_FILE = "config.json"

#: The closed set ``token_storage`` may name; the keychain is the default
#: because it is the safe one (decision F2).
TOKEN_STORAGES = ("keychain", "file")


class ConfigError(ValueError):
    """The config file exists but cannot be used: not JSON, or a value
    outside its closed set. Loud on purpose — a hand-edited file that
    silently fell back to defaults would send a token to the wrong place."""


@dataclass
class Config:
    api_url: Optional[str] = None
    token_storage: str = "keychain"


def config_dir(env: Mapping[str, str]) -> Path:
    """``$STORYDUMP_CONFIG_DIR`` when set, else ``~/.config/storydump``."""
    override = env.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "storydump"


def ensure_dir(directory: Path) -> Path:
    """Create *directory* as 0700 — asserted after creation too, because
    ``mkdir``'s mode is subject to the umask and an existing directory
    keeps whatever mode it had."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def write_private(path: Path, text: str) -> None:
    """Create or overwrite *path* as 0600. The mode is passed to ``open`` so
    a new file never exists with a wider one, and asserted before the write
    because an existing file keeps its mode across ``O_TRUNC``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(path, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def read_config(directory: Path) -> Config:
    """The config on disk, or the defaults when there is none yet."""
    path = directory / CONFIG_FILE
    if not path.exists():
        return Config()
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    api_url = raw.get("api_url")
    if api_url is not None and not isinstance(api_url, str):
        raise ConfigError(f"{path}: api_url must be a string")
    token_storage = raw.get("token_storage", "keychain")
    if token_storage not in TOKEN_STORAGES:
        raise ConfigError(
            f"{path}: token_storage must be one of {', '.join(TOKEN_STORAGES)},"
            f" not {token_storage!r}"
        )
    return Config(api_url=api_url or None, token_storage=token_storage)


def write_config(directory: Path, config: Config) -> Path:
    """Write *config* under *directory* (created 0700), the file 0600."""
    ensure_dir(directory)
    path = directory / CONFIG_FILE
    payload = {"api_url": config.api_url, "token_storage": config.token_storage}
    write_private(path, json.dumps(payload, indent=2) + "\n")
    return path
