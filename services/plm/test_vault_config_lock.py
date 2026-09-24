"""Concurrent vault-local configuration updates must preserve every patch."""
from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor

from services.plm.vault import Vault


def _write_key(project: str, index: int) -> None:
    vault = Vault.open(project)
    try:
        vault.set_local_config({"link_checkouts": {str(index): f"checkout-{index}"}})
    finally:
        vault.close()


def test_concurrent_process_config_updates_preserve_distinct_keys(tmp_path):
    vault = Vault.open(tmp_path)
    vault.close()
    count = 16
    with multiprocessing.get_context("spawn").Pool(8) as pool:
        pool.starmap(_write_key, [(str(tmp_path), i) for i in range(count)])
    vault = Vault.open(tmp_path)
    try:
        assert vault.get_local_config()["link_checkouts"] == {
            str(i): f"checkout-{i}" for i in range(count)
        }
    finally:
        vault.close()


def test_concurrent_thread_config_updates_preserve_distinct_keys(tmp_path, monkeypatch):
    vault = Vault.open(tmp_path)
    vault.close()
    original = Vault.get_local_config

    def slow_read(self):
        result = original(self)
        time.sleep(0.005)
        return result

    monkeypatch.setattr(Vault, "get_local_config", slow_read)
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda i: _write_key(str(tmp_path), i), range(24)))
    vault = Vault.open(tmp_path)
    try:
        assert vault.get_local_config()["link_checkouts"] == {
            str(i): f"checkout-{i}" for i in range(24)
        }
    finally:
        vault.close()
