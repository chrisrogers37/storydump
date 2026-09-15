"""Where the token lives (decision F2, the ``gh`` pattern).

The keychain backend fails closed: ``keyring`` will happily fall back to a
plaintext store (``keyrings.alt``) or a ``null``/``fail`` stub on a headless
host, and a token written there is worse off than in the 0600 file the
person could have chosen on purpose. The file backend refuses to read a
file anyone else can read. The variable wins over both, for agents and CI.
"""

from __future__ import annotations

import os
import stat
import types

import pytest

from storydump_cli.config import Config, read_config, write_config
from storydump_cli.storage import (
    EnvBackend,
    FileBackend,
    KeychainBackend,
    MemoryBackend,
    StorageUnavailable,
    resolve_token,
    token_path,
)

SECRET = "sdt_" + "a" * 43
HOST = "api.storydump.test"


class _Store:
    """What a ``keyring`` backend instance looks like to the CLI."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str):
        return self.rows.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.rows[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.rows[(service, username)]


def _fake_keyring(module_name: str, **attrs):
    """A fake ``keyring`` module whose active backend's class lives in
    *module_name* — the only thing the CLI is allowed to judge it by."""
    klass = type("Keyring", (_Store,), {"__module__": module_name})
    store = klass()
    for key, value in attrs.items():
        setattr(store, key, value)
    return types.SimpleNamespace(get_keyring=lambda: store), store


@pytest.mark.parametrize(
    "module_name",
    ["keyrings.alt.file", "keyring.backends.fail", "keyring.backends.null"],
)
def test_keychain_refuses_anything_that_is_not_an_os_store(module_name):
    module, store = _fake_keyring(module_name)
    backend = KeychainBackend(HOST, keyring_module=module)
    with pytest.raises(StorageUnavailable) as caught:
        backend.get()
    assert "--insecure-storage" in str(caught.value)
    assert "STORYDUMP_TOKEN" in str(caught.value)
    with pytest.raises(StorageUnavailable):
        backend.set(SECRET)
    assert store.rows == {}, "nothing may be written to a store that was refused"


def test_keychain_accepts_the_macos_store_and_keys_by_host():
    module, store = _fake_keyring("keyring.backends.macOS")
    backend = KeychainBackend(HOST, keyring_module=module)
    assert backend.get() is None
    backend.set(SECRET)
    assert store.rows == {("storydump", HOST): SECRET}
    assert backend.get() == SECRET
    backend.delete()
    assert backend.get() is None
    backend.delete()  # nothing stored is not an error: logout is idempotent


def test_keychain_refuses_a_chain_with_a_plaintext_member():
    _, macos = _fake_keyring("keyring.backends.macOS")
    _, plaintext = _fake_keyring("keyrings.alt.file")
    module, _ = _fake_keyring("keyring.backends.chainer", backends=[macos, plaintext])
    with pytest.raises(StorageUnavailable):
        KeychainBackend(HOST, keyring_module=module).get()


def test_keychain_accepts_a_chain_of_os_stores_only():
    _, macos = _fake_keyring("keyring.backends.macOS")
    _, secret_service = _fake_keyring("keyring.backends.SecretService")
    module, chain = _fake_keyring(
        "keyring.backends.chainer", backends=[macos, secret_service]
    )
    backend = KeychainBackend(HOST, keyring_module=module)
    backend.set(SECRET)
    assert chain.rows == {("storydump", HOST): SECRET}


def test_file_backend_writes_0600_inside_a_0700_directory(tmp_path):
    path = token_path(tmp_path / "storydump")
    FileBackend(path).set(SECRET)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert FileBackend(path).get() == SECRET


def test_file_backend_refuses_to_read_a_file_others_can_read(tmp_path):
    path = token_path(tmp_path)
    FileBackend(path).set(SECRET)
    os.chmod(path, 0o644)
    with pytest.raises(StorageUnavailable) as caught:
        FileBackend(path).get()
    assert "0600" in str(caught.value)


def test_file_backend_tightens_a_loose_file_on_write(tmp_path):
    path = token_path(tmp_path)
    path.write_text("stale\n")
    os.chmod(path, 0o644)
    FileBackend(path).set(SECRET)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert FileBackend(path).get() == SECRET


def test_file_backend_absent_is_none_and_delete_is_idempotent(tmp_path):
    backend = FileBackend(token_path(tmp_path))
    assert backend.get() is None
    backend.delete()
    backend.set(SECRET)
    backend.delete()
    assert backend.get() is None
    assert not token_path(tmp_path).exists()


def test_env_backend_reads_the_variable_and_never_stores():
    assert EnvBackend({}).get() is None
    assert EnvBackend({"STORYDUMP_TOKEN": f" {SECRET}\n"}).get() == SECRET
    with pytest.raises(StorageUnavailable):
        EnvBackend({"STORYDUMP_TOKEN": SECRET}).set(SECRET)
    with pytest.raises(StorageUnavailable):
        EnvBackend({"STORYDUMP_TOKEN": SECRET}).delete()


def test_the_variable_wins_over_both_stores(tmp_path):
    in_keychain = "sdt_" + "k" * 43
    in_file = "sdt_" + "f" * 43
    in_env = "sdt_" + "e" * 43
    keychain = MemoryBackend()
    keychain.set(in_keychain)
    FileBackend(token_path(tmp_path)).set(in_file)

    def resolve(env, storage):
        return resolve_token(
            env=env, config_dir=tmp_path, token_storage=storage, keychain=keychain
        )

    assert resolve({"STORYDUMP_TOKEN": in_env}, "keychain") == in_env
    assert resolve({"STORYDUMP_TOKEN": in_env}, "file") == in_env
    assert resolve({}, "file") == in_file
    assert resolve({}, "keychain") == in_keychain


def test_config_records_the_token_storage(tmp_path):
    directory = tmp_path / "storydump"
    assert read_config(directory) == Config(api_url=None, token_storage="keychain")
    path = write_config(
        directory, Config(api_url="https://api.test", token_storage="file")
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert read_config(directory) == Config(
        api_url="https://api.test", token_storage="file"
    )


def test_a_store_that_refuses_to_delete_is_the_same_refusal_as_get_and_set():
    """`logout` must not traceback on a locked keychain: the store's own
    error becomes the CLI's StorageUnavailable, like `get` and `set`."""

    class KeyringError(Exception):
        pass

    def refuse(*args):
        raise KeyringError("locked")

    module, store = _fake_keyring("keyring.backends.macOS", delete_password=refuse)
    module.errors = types.SimpleNamespace(KeyringError=KeyringError)
    backend = KeychainBackend(HOST, keyring_module=module)
    backend.set(SECRET)
    with pytest.raises(StorageUnavailable) as caught:
        backend.delete()
    assert "keychain" in str(caught.value)
    assert store.rows, "a refused delete leaves the row where it was"
