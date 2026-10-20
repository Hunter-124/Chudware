"""Isolated tests for browser profile lifecycle and expiry checks."""
from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture()
def profile_store(tmp_path, monkeypatch):
    from services import paths
    from services.browser import profiles

    monkeypatch.setattr(paths, "BROWSER_PROFILES", tmp_path / "browser_profiles")
    return profiles


def _verified(profiles, site: str = "vendor") -> None:
    profiles.save_meta(site, created_ts=1.0, login_verified_ts=2.0)


def test_status_probe_reports_active_and_preserves_redirect_url(profile_store):
    profiles = profile_store
    _verified(profiles)
    calls: list[tuple[Any, ...]] = []

    def fetch(url: str, site: str, *, no_cache: bool) -> dict[str, Any]:
        calls.append((url, site, no_cache))
        return {
            "ok": True,
            "content": "<html>account</html>",
            "url": url,
            "final_url": "https://vendor.example/account",
        }

    def classify(site: str, content: str, url: str) -> str:
        calls.append((site, content, url))
        return "logged_in"

    status = profiles.check_profile_status(
        "Vendor",
        _fetch=fetch,
        _probe_url="https://vendor.example/login",
        _classify=classify,
    )

    assert status == "active"
    assert calls == [
        ("https://vendor.example/login", "vendor", True),
        ("vendor", "<html>account</html>", "https://vendor.example/account"),
    ]
    meta = profiles.load_meta("vendor")
    assert meta["status"] == "active"
    assert meta["login_verified_ts"] == 2.0


def test_status_probe_marks_login_page_expired_and_clears_verification(profile_store):
    profiles = profile_store
    _verified(profiles)

    status = profiles.check_profile_status(
        "vendor",
        _fetch=lambda *_args, **_kwargs: {
            "ok": True,
            "content": "<form>sign in</form>",
            "final_url": "https://vendor.example/login",
        },
        _probe_url="https://vendor.example/login",
        _classify=lambda *_args: "login_page",
    )

    assert status == "expired"
    meta = profiles.load_meta("vendor")
    assert meta["status"] == "expired"
    assert "login_verified_ts" not in meta
    assert profiles.has_login("vendor") is False
    # An expired profile has no verified stamp to probe again, but retains its
    # definitive persisted state for profile listings.
    assert profiles.check_profile_status(
        "vendor",
        _fetch=lambda *_args, **_kwargs: pytest.fail("expired profile was re-probed"),
        _probe_url="https://vendor.example/login",
        _classify=lambda *_args: "logged_in",
    ) == "expired"


@pytest.mark.parametrize(
    ("fetch_result", "classification"),
    [
        ({"ok": False, "reason": "browser_fetch_failed"}, "logged_in"),
        ({"ok": True, "content": "challenge", "final_url": "https://vendor.example/"},
         "challenge"),
        ({"ok": True, "content": "ambiguous", "final_url": "https://vendor.example/"},
         "unknown"),
    ],
)
def test_status_probe_failures_are_unknown_without_expiring(
    profile_store, fetch_result, classification
):
    profiles = profile_store
    _verified(profiles)

    status = profiles.check_profile_status(
        "vendor",
        _fetch=lambda *_args, **_kwargs: fetch_result,
        _probe_url="https://vendor.example/login",
        _classify=lambda *_args: classification,
    )

    assert status == "unknown"
    meta = profiles.load_meta("vendor")
    assert meta["status"] == "unknown"
    assert meta["login_verified_ts"] == 2.0


def test_status_probe_holds_lock_through_metadata_commit(
    profile_store, monkeypatch
):
    profiles = profile_store
    _verified(profiles)
    real_write = profiles._write_meta
    writes = []

    def guarded_write(site, meta):
        peer = profiles.profile_lock(site)
        assert peer.acquire(blocking=False) is False
        writes.append(meta["status"])
        return real_write(site, meta)

    monkeypatch.setattr(profiles, "_write_meta", guarded_write)
    status = profiles.check_profile_status(
        "vendor",
        _fetch=lambda *_args, **_kwargs: {
            "ok": True,
            "content": "<form>sign in</form>",
            "final_url": "https://vendor.example/login",
        },
        _probe_url="https://vendor.example/login",
        _classify=lambda *_args: "login_page",
    )

    assert status == "expired"
    assert writes == ["expired"]

    lock = profiles.profile_lock("vendor")
    assert lock.acquire(blocking=False) is True
    try:
        assert profiles.delete_profile("vendor") is True
    finally:
        lock.release()
    assert profiles.load_meta("vendor") == {}


def test_dot_slugs_are_rejected_before_profile_storage_is_touched(profile_store):
    profiles = profile_store
    root = profiles.paths.BROWSER_PROFILES
    operations = (
        lambda site: profiles.profile_dir(site),
        lambda site: profiles.load_meta(site),
        lambda site: profiles.save_meta(site, created_ts=1.0),
        lambda site: profiles.save_session_cookies(site, []),
        lambda site: profiles.delete_profile(site),
    )

    for site in (".", "..", " ./ ", " ../ "):
        for operation in operations:
            with pytest.raises(ValueError, match="dot path"):
                operation(site)
    assert not root.exists()


def test_delete_profile_removes_directory_metadata_and_cookie_sidecar(profile_store):
    profiles = profile_store
    profile_dir = profiles.profile_dir("Vendor")
    profile_dir.mkdir()
    (profile_dir / "Default").mkdir()
    (profile_dir / "Default" / "Cookies").write_text("browser data", encoding="utf-8")
    profiles.save_meta("Vendor", created_ts=1.0, login_verified_ts=2.0)
    profiles.save_session_cookies("Vendor", [{"name": "sid", "value": "secret"}])

    root = profiles.paths.BROWSER_PROFILES
    metadata = root / "vendor.json"
    cookie_sidecar = root / "vendor.cookies.json"
    assert profile_dir.exists() and metadata.exists() and cookie_sidecar.exists()

    assert profiles.delete_profile("Vendor") is True
    assert not profile_dir.exists()
    assert not metadata.exists()
    assert not cookie_sidecar.exists()
    assert profiles.delete_profile("Vendor") is False


def test_cell_profiles_metadata_cookies_and_locks_are_actor_private(profile_store, tmp_path, monkeypatch):
    import hashlib
    from services.agent import request_context as ctx
    profiles = profile_store
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))

    def principal(actor):
        return {"cell": True, "workspace_key": "team:shared", "actor_key": actor,
                "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
                "right": "editor", "epoch": 1}

    with ctx.bind_context(principal("alice")):
        a = profiles.ensure_profile("same.site")
        profiles.save_meta("same.site", login_verified_ts=42, label="alice")
        profiles.save_session_cookies("same.site", [{"name": "sid", "value": "alice-secret"}])
        alice_lock = profiles.profile_lock("same.site")
        assert a.name != "same.site"
        assert alice_lock.acquire(blocking=False)

    with ctx.bind_context(principal("bob")):
        assert profiles.list_profiles() == []
        assert not profiles.has_profile("same.site")
        for operation in (
            lambda site: profiles.load_meta(site),
            lambda site: profiles.consume_session_cookies(site),
            lambda site: profiles.delete_profile(site),
        ):
            for site in ("same.site", "absent"):
                with pytest.raises(KeyError, match="not_found"):
                    operation(site)
        b = profiles.ensure_profile("same.site")
        assert a != b
        profiles.save_meta("same.site", label="bob")
        assert profiles.profile_lock("same.site") is not alice_lock
        bob_lock = profiles.profile_lock("same.site")
        assert bob_lock.acquire(blocking=False)
        bob_lock.release()
        assert [p["label"] for p in profiles.list_profiles()] == ["bob"]
        assert profiles.delete_profile("same.site")

    with ctx.bind_context(principal("alice")):
        assert profiles.load_meta("same.site")["label"] == "alice"
        assert profiles.has_profile("same.site")
        assert profiles.consume_session_cookies("same.site")[0]["value"] == "alice-secret"
        alice_lock.release()
        assert profiles.delete_profile("same.site")
