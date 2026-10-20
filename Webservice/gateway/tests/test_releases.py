from __future__ import annotations

import base64
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, get_args

# Before ANY gateway import: config.Settings is built once, at import time, from
# the environment as it stands then. Whichever gateway test module pytest
# imports first therefore decides dev_mode for the whole session, and without
# this preamble a combined run in which this file is collected first leaves
# dev_mode False — which makes AuthMiddleware refuse to construct and every
# real-app test in a sibling module fail with "no strong auth configured".
# Same preamble as test_portal.py and test_releases_ci.py, deliberately.
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault(
    "CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test")
)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# `gateway.*`, not `Webservice.gateway.*`: conftest puts both the gateway dir and
# its parent on sys.path, so only this spelling works when the suite is run from
# Webservice/gateway (the documented invocation), and it is the same module
# object test_releases_ci.py monkeypatches.
from gateway import artifact_names, auth, releases  # noqa: E402


@pytest.fixture
def manifest_row() -> dict[str, Any]:
    return {
        "version": "1.2.0",
        "min_supported": "1.0.0",
        "key_id": "release-v1",
        "sha256": "a" * 64,
        "size": 123456789,
        "signature": base64.b64encode(b"s" * 64).decode("ascii"),
        "notes_url": "https://chudware.com/releases/1.2.0",
        "artifact_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage",
        "payload_manifest_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.payload-manifest.v1.json",
        "payload_manifest_sha256": "b" * 64,
        "payload_signature_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.payload-manifest.v1.sig",
        "payload_signature_sha256": "c" * 64,
        "commit_sha": "d" * 40,
        "sbom_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.sbom.cdx.json",
        "sbom_sha256": "d" * 64,
        "sbom_signature_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.sbom.cdx.json.sig",
        "sbom_signature_sha256": "e" * 64,
        "provenance_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.provenance.slsa.json",
        "provenance_sha256": "f" * 64,
        "provenance_signature_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.provenance.slsa.json.sig",
        "provenance_signature_sha256": "1" * 64,
        "platform_test_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.platform-test.v1.json",
        "platform_test_sha256": "2" * 64,
        "platform_test_signature_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.platform-test.v1.json.sig",
        "platform_test_signature_sha256": "3" * 64,
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]) -> TestClient:
    monkeypatch.setattr(releases, "_manifest_by_ip", releases.RateLimiter(1000, 60.0))
    monkeypatch.setattr(releases, "_fetch_latest", lambda *_: dict(manifest_row))
    app = FastAPI()
    app.include_router(releases.router)
    return TestClient(app)


def test_anonymous_and_bad_entitlement_receive_byte_identical_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: False)
    monkeypatch.setattr(
        releases,
        "_presigned_url",
        lambda _key: pytest.fail("anonymous or invalid callers must not mint a URL"),
    )
    query = "?channel=stable&platform=linux&arch=x86_64"
    anonymous = client.get("/api/releases/latest" + query)
    invalid = client.get(
        "/api/releases/latest" + query,
        headers={"X-Chudware-Entitlement": "forged.email@example.com.token"},
    )
    assert anonymous.status_code == invalid.status_code == 200
    assert anonymous.content == invalid.content
    assert "url" not in anonymous.json()
    assert set(anonymous.json()) == {
        "version", "min_supported", "platform", "arch", "format", "key_id", "sha256",
        "size", "signature", "notes_url", "payload_manifest_key",
        "payload_manifest_sha256", "payload_signature_key", "payload_signature_sha256",
    }


def test_manifest_names_the_target_identity_the_updater_requires(
    client: TestClient,
) -> None:
    """The desktop updater refuses a manifest that is not for its own target.

    manifestFromJson() reads platform and arch and compares them with the
    running build's, so a manifest without them is unparseable and the app can
    never update.
    """
    body = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64"
    ).json()
    assert (body["platform"], body["arch"]) == ("linux", "x86_64")




def test_authenticated_download_falls_back_to_this_host_when_r2_is_absent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No CDN credential is a supported deployment, not a broken one.

    Answering 503 here left a complete, correctly signed release undownloadable
    and every updater permanently stuck, so the gateway now points at its own
    artifact route instead.
    """
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: True)
    for name in releases._R2_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CHUDWARE_RELEASES_PUBLIC_BASE_URL", raising=False)
    response = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"X-Chudware-Entitlement": "valid"},
    )
    assert response.status_code == 200
    assert response.json()["url"] == (
        "https://app.chudware.com/api/releases/artifact/"
        "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage"
    )
    assert response.headers["cache-control"] == "private, no-store"


def test_presigned_url_still_names_every_missing_r2_variable(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in releases._R2_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(Exception) as error:
        releases._presigned_url("stable/1.2.0/artifact")
    assert getattr(error.value, "status_code", None) == 503
    for name in releases._R2_ENV:
        assert name in error.value.detail




@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("2.0.0", "1.99.99", True),
        ("2.10.0", "2.9.99", True),
        ("2.10.4", "2.10.3", True),
        ("2.9.0", "2.10.0", False),
    ],
)
def test_release_version_order_is_numeric(left: str, right: str, expected: bool) -> None:
    assert (releases._parse_release_version(left, "version") > releases._parse_release_version(right, "version")) is expected


@pytest.mark.parametrize("version", ["01.2.3", "1.02.3", "1.2.03", "1.2", "1.2.3-rc.1", "1.2.3+build", "v1.2.3"])
def test_release_version_rejects_noncanonical_and_prerelease(version: str) -> None:
    with pytest.raises(Exception) as error:
        releases._parse_release_version(version, "version")
    assert getattr(error.value, "status_code", None) == 422





def test_publish_rejects_noncanonical_artifact_key(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    app = FastAPI()
    app.include_router(releases.router)
    payload = {
        "channel": "stable",
        "platform": "linux",
        "arch": "x86_64",
        **manifest_row,
        "artifact_key": "stable/1.2.0/../../hostile.AppImage",
    }
    response = TestClient(app).post(
        "/api/releases/latest/publish",
        json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert "artifact_key" in response.json()["detail"]


def test_publish_rejects_signature_outside_artifact_domain(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {
        "channel": "stable",
        "platform": "linux",
        "arch": "x86_64",
        **manifest_row,
    }
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish",
        json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "signature does not match release identity and sha256"


def test_publish_rejects_unknown_release_key_id(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {
        "channel": "stable",
        "platform": "linux",
        "arch": "x86_64",
        **manifest_row,
        "key_id": "attacker-key",
    }
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish",
        json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "unknown release key_id"



@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_publish_rejects_missing_payload_metadata(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any], channel: str
) -> None:
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {"channel": channel, "platform": "linux", "arch": "x86_64", **manifest_row}
    payload["artifact_key"] = payload["artifact_key"].replace("stable/", channel + "/", 1)
    for field in (
        "payload_manifest_key", "payload_manifest_sha256",
        "payload_signature_key", "payload_signature_sha256",
    ):
        payload.pop(field)
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish", json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422


def test_publish_rejects_identity_mismatched_payload_key(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {"channel": "stable", "platform": "linux", "arch": "x86_64", **manifest_row}
    payload["payload_manifest_key"] = "stable/9.9.9/other.payload-manifest.v1.json"
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish", json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert "payload_manifest_key" in response.json()["detail"]


def test_publish_rejects_noncanonical_payload_digest(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {"channel": "stable", "platform": "linux", "arch": "x86_64", **manifest_row}
    payload["payload_signature_sha256"] = "A" * 64
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish", json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert "payload_signature_sha256" in response.json()["detail"]

@pytest.mark.parametrize("field", [
    "sbom_key", "sbom_sha256", "sbom_signature_key", "sbom_signature_sha256",
    "provenance_key", "provenance_sha256", "provenance_signature_key", "provenance_signature_sha256",
    "platform_test_key", "platform_test_sha256", "platform_test_signature_key", "platform_test_signature_sha256",
])
def test_publish_rejects_missing_or_mismatched_evidence(monkeypatch, manifest_row, field):
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {"channel": "stable", "platform": "linux", "arch": "x86_64", **manifest_row}
    if field.endswith("_key"):
        payload[field] = "stable/stale/hostile"
    else:
        payload.pop(field)
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post("/api/releases/latest/publish", json=payload, headers={"Authorization": "Bearer publish-secret"})
    assert response.status_code == 422



def test_artifact_signature_preimage_is_domain_separated() -> None:
    digest = "a" * 64
    assert releases._artifact_signature_preimage(
        "release-v1", "1.2.3", "linux", "x86_64", digest
    ) == b"\0".join((
        b"chudware-artifact-v1", b"release-v1", b"1.2.3", b"linux",
        b"x86_64", digest.encode("ascii"),
    ))


def test_release_manifest_is_exactly_soft_auth_public() -> None:
    assert auth._is_public_path("/api/releases/latest")
    assert auth._is_public_path("/api/releases/latest/publish")
    assert not auth._is_public_path("/api/releases")
    assert not auth._is_public_path("/api/releases/latestly")


def test_anonymous_manifest_is_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases, "_manifest_by_ip", releases.RateLimiter(1, 60.0))
    query = "?channel=stable&platform=linux&arch=x86_64"
    assert client.get("/api/releases/latest" + query).status_code == 200
    response = client.get("/api/releases/latest" + query)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------


def test_format_literal_and_artifact_names_cannot_drift() -> None:
    """``Format`` is hand-written because FastAPI needs a literal; this is the
    only thing keeping it equal to the runtime tuple everything else uses."""
    assert get_args(releases.Format) == artifact_names.FORMATS
    assert releases.DEFAULT_FORMAT == artifact_names.FORMATS[0] == "default"


@pytest.mark.parametrize(
    ("version", "fmt", "expected"),
    [
        ("0.1.1", "default", "Chudware-0.1.1-linux-x86_64.AppImage"),
        ("0.1.1", "deb", "chudware_0.1.1_amd64.deb"),
        ("0.1.1", "rpm", "chudware-0.1.1-1.x86_64.rpm"),
        ("0.1.1", "tar", "Chudware-0.1.1-linux-x86_64.tar.zst"),
        (
            "0.1.0-beta.21", "default",
            "Chudware-0.1.0-beta.21-linux-x86_64.AppImage",
        ),
        # Debian and RPM both refuse '-' in a version, and they sanitise it
        # differently. Getting either wrong makes the beta sort NEWER than the
        # release it precedes, so the upgrade is never offered.
        ("0.1.0-beta.21", "deb", "chudware_0.1.0~beta.21_amd64.deb"),
        ("0.1.0-beta.21", "rpm", "chudware-0.1.0-0.beta.21.x86_64.rpm"),
        ("0.1.0-beta.21", "tar", "Chudware-0.1.0-beta.21-linux-x86_64.tar.zst"),
    ],
)
def test_canonical_artifact_names_every_linux_format(
    version: str, fmt: str, expected: str
) -> None:
    assert releases._canonical_artifact(version, "linux", "x86_64", fmt) == expected
    # The CI pipeline reaches the same rule through the dependency-free module.
    assert artifact_names.canonical_artifact(version, "linux", "x86_64", fmt) == expected


def test_deb_and_rpm_version_fields_sort_the_prerelease_first() -> None:
    """The filenames are only half of it: the package METADATA has to order the
    beta below the final release or `apt upgrade` becomes a downgrade."""
    assert artifact_names.deb_version("0.1.0-beta.21") == "0.1.0~beta.21"
    assert artifact_names.deb_version("0.1.1") == "0.1.1"
    assert artifact_names.rpm_version_release("0.1.0-beta.21") == ("0.1.0", "0.beta.21")
    assert artifact_names.rpm_version_release("0.1.1") == ("0.1.1", "1")


@pytest.mark.parametrize("fmt", ["deb", "rpm", "tar"])
def test_windows_rejects_linux_package_formats(fmt: str) -> None:
    with pytest.raises(Exception) as error:
        releases._canonical_artifact("0.1.1", "windows", "x86_64", fmt)
    assert getattr(error.value, "status_code", None) == 422
    assert "only default and msi formats" in error.value.detail


@pytest.mark.parametrize("fmt", ["deb", "rpm", "tar", "msi"])
def test_macos_rejects_every_nondefault_format(fmt: str) -> None:
    with pytest.raises(Exception) as error:
        releases._canonical_artifact("0.1.1", "macos", "x86_64", fmt)
    assert getattr(error.value, "status_code", None) == 422
    assert "only the default format" in error.value.detail


def test_canonical_windows_msi_and_unsupported_architecture() -> None:
    assert releases._canonical_artifact(
        "0.1.0-beta.21", "windows", "x86_64", "msi"
    ) == "Chudware-0.1.0-beta.21-x86_64.msi"
    assert artifact_names.canonical_artifact(
        "1.2.0", "windows", "x86_64", "msi"
    ) == "Chudware-1.2.0-x86_64.msi"
    with pytest.raises(Exception) as error:
        releases._canonical_artifact("1.2.0", "windows", "arm64", "msi")
    assert getattr(error.value, "status_code", None) == 422
    assert "unsupported platform and architecture" in error.value.detail


def test_canonical_artifact_keeps_its_pre_format_names_untouched() -> None:
    """Existing published rows were validated against these exact strings; a
    change here invalidates every artifact_key already in the table."""
    assert releases._canonical_artifact("1.2.0", "linux", "x86_64") == (
        "Chudware-1.2.0-linux-x86_64.AppImage"
    )
    assert releases._canonical_artifact("1.2.0", "windows", "x86_64") == (
        "Chudware-Setup-1.2.0-x86_64.exe"
    )
    assert releases._canonical_artifact("1.2.0", "macos", "arm64") == (
        "Chudware-1.2.0-macos-arm64.zip"
    )


def test_format_is_absent_from_the_frozen_signature_preimage() -> None:
    """Every shipped client verifies against this tuple. Adding format would
    make each new release fail verification on every installed 0.1.1."""
    digest = "a" * 64
    preimage = releases._artifact_signature_preimage(
        "release-v1", "0.1.1", "linux", "x86_64", digest
    )
    assert preimage.split(b"\0") == [
        b"chudware-artifact-v1", b"release-v1", b"0.1.1", b"linux", b"x86_64",
        digest.encode("ascii"),
    ]
    for fmt in artifact_names.FORMATS:
        assert fmt.encode("ascii") not in preimage.split(b"\0")


def test_latest_without_a_format_still_asks_for_the_appimage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    """The shipped desktop updater sends channel+platform+arch and nothing else.
    It must keep receiving the row it received before this column existed."""
    seen: list[tuple[Any, ...]] = []

    def fake_fetch(*args: Any) -> dict[str, Any]:
        seen.append(args)
        return dict(manifest_row)

    monkeypatch.setattr(releases, "_fetch_latest", fake_fetch)
    body = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64"
    ).json()
    assert seen == [("stable", "linux", "x86_64", "default")]
    assert body["format"] == "default"
    assert body["version"] == manifest_row["version"]


def test_latest_selects_and_echoes_the_requested_format(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    seen: list[tuple[Any, ...]] = []

    def fake_fetch(*args: Any) -> dict[str, Any]:
        seen.append(args)
        return dict(manifest_row)

    monkeypatch.setattr(releases, "_fetch_latest", fake_fetch)
    body = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64&format=deb"
    ).json()
    assert seen == [("stable", "linux", "x86_64", "deb")]
    assert body["format"] == "deb"


def test_latest_rejects_an_unknown_format(client: TestClient) -> None:
    response = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64&format=snap"
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /api/releases/available
# ---------------------------------------------------------------------------


_AVAILABLE_ROWS = [
    {
        "platform": "linux", "arch": "x86_64", "format": "rpm", "version": "0.1.1",
        "size": 318767104, "sha256": "4" * 64,
        "notes_url": "https://chudware.com/releases/0.1.1",
        # Never returned. Present here because _fetch_available selects from the
        # same table, and a careless route would forward whatever it was handed.
        "artifact_key": "stable/0.1.1/chudware-0.1.1-1.x86_64.rpm",
        "signature": "should-never-be-published",
    },
    {
        "platform": "linux", "arch": "x86_64", "format": "default",
        "version": "0.1.1", "size": 340724216, "sha256": "1" * 64,
        "notes_url": "https://chudware.com/releases/0.1.1",
    },
    {
        "platform": "windows", "arch": "x86_64", "format": "default",
        "version": "0.1.1", "size": 239491858, "sha256": "2" * 64,
        "notes_url": "https://chudware.com/releases/0.1.1",
    },
    {
        "platform": "windows", "arch": "x86_64", "format": "msi",
        "version": "0.1.1", "size": 251658240, "sha256": "5" * 64,
        "notes_url": "https://chudware.com/releases/0.1.1",
    },
    {
        "platform": "linux", "arch": "x86_64", "format": "deb", "version": "0.1.1",
        "size": 318767105, "sha256": "3" * 64,
        "notes_url": "https://chudware.com/releases/0.1.1",
    },
]


@pytest.fixture
def available_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Fakes the CURSOR, not ``_fetch_available``.

    The declared-order re-key lives in ``_fetch_available``, so stubbing that
    function would leave the ordering contract every consumer depends on
    untested. Rows come back in the order Postgres would give them — format
    sorted as text — so the re-key has something to actually fix.
    """
    monkeypatch.setattr(releases, "_available_by_ip", releases.RateLimiter(1000, 60.0))

    class Cursor:
        def execute(self, _statement: str, _values: tuple[Any, ...]) -> None:
            pass

        def fetchall(self) -> list[dict[str, Any]]:
            return [
                dict(row)
                for row in sorted(
                    _AVAILABLE_ROWS,
                    key=lambda row: (row["platform"], row["arch"], row["format"]),
                )
            ]

    @contextmanager
    def fake_tx():
        yield Cursor()

    monkeypatch.setattr(releases, "tx", fake_tx)
    app = FastAPI()
    app.include_router(releases.router)
    return TestClient(app)


def test_available_is_anonymous_and_publishes_no_url_or_signature(
    available_client: TestClient,
) -> None:
    response = available_client.get("/api/releases/available?channel=stable")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"channel", "builds"}
    assert body["channel"] == "stable"
    for build in body["builds"]:
        assert set(build) == {
            "platform", "arch", "format", "version", "size", "sha256", "notes_url",
        }
    assert "signature" not in response.text
    assert "artifact_key" not in response.text
    assert response.headers["cache-control"] == "public, max-age=300"


def test_available_echoes_only_an_allowlisted_origin(
    available_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chudware.com renders live versions from this route and is a different
    origin. An allowlist, never a wildcard and never a reflector."""
    monkeypatch.setattr(releases.settings, "dev_mode", False)
    for origin in ("https://chudware.com", "https://www.chudware.com"):
        response = available_client.get(
            "/api/releases/available", headers={"Origin": origin}
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        # Without this, the five-minute edge cache would serve one origin's
        # response — header and all — to every other caller.
        assert response.headers["vary"] == "Origin"


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://chudware.com.evil.example",
        "https://chudware.com/",
        "http://chudware.com",
        "null",
        "*",
    ],
)
def test_available_refuses_every_other_origin(
    available_client: TestClient, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setattr(releases.settings, "dev_mode", False)
    response = available_client.get(
        "/api/releases/available", headers={"Origin": origin}
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    # Still varies, so a refusal is not cached as an approval for someone else.
    assert response.headers["vary"] == "Origin"


def test_available_allows_the_astro_dev_origin_only_in_dev_mode(
    available_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases.settings, "dev_mode", False)
    assert releases._catalogue_cors_origins() == (
        "https://chudware.com", "https://www.chudware.com",
    )
    for origin in ("http://localhost:4321", "http://127.0.0.1:4321"):
        production = available_client.get(
            "/api/releases/available", headers={"Origin": origin}
        )
        assert "access-control-allow-origin" not in production.headers, origin

    monkeypatch.setattr(releases.settings, "dev_mode", True)
    for origin in ("http://localhost:4321", "http://127.0.0.1:4321"):
        development = available_client.get(
            "/api/releases/available", headers={"Origin": origin}
        )
        assert development.headers["access-control-allow-origin"] == origin


def test_no_other_release_or_portal_route_is_opened_cross_origin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`available` is safe to open BECAUSE it carries no URL, signature or
    identity. Anything that does must stay same-origin."""
    monkeypatch.setattr(releases.settings, "dev_mode", False)
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: False)
    manifest = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"Origin": "https://chudware.com"},
    )
    assert manifest.status_code == 200
    assert "access-control-allow-origin" not in manifest.headers
    # No preflight handler was added anywhere: a bare GET needs none, and one
    # would be a door nothing is knocking on.
    assert not any(
        "OPTIONS" in (getattr(route, "methods", None) or set())
        for route in releases.router.routes
    )


def test_available_orders_windows_then_linux_then_macos_and_msi_first(
    available_client: TestClient,
) -> None:
    """Presentation order is Windows, Linux, macOS; MSI leads Windows."""
    builds = available_client.get("/api/releases/available?channel=stable").json()[
        "builds"
    ]
    assert [(b["platform"], b["arch"], b["format"]) for b in builds] == [
        ("windows", "x86_64", "msi"),
        ("windows", "x86_64", "default"),
        ("linux", "x86_64", "default"),
        ("linux", "x86_64", "deb"),
        ("linux", "x86_64", "rpm"),
    ]


def test_available_defaults_to_stable_and_refuses_an_unknown_channel(
    available_client: TestClient,
) -> None:
    assert available_client.get("/api/releases/available").json()["channel"] == "stable"
    assert (
        available_client.get("/api/releases/available?channel=nightly").status_code
        == 422
    )


def test_available_is_rate_limited(
    available_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases, "_available_by_ip", releases.RateLimiter(1, 60.0))
    assert available_client.get("/api/releases/available").status_code == 200
    assert available_client.get("/api/releases/available").status_code == 429


# ---------------------------------------------------------------------------
# Download tokens
# ---------------------------------------------------------------------------


class _Account:
    def __init__(self, status: str = "active") -> None:
        self.status = status


@pytest.fixture
def token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(releases.settings, "session_secret", "download-token-secret")
    monkeypatch.setattr(releases.accounts, "get_account", lambda _email: _Account())


def test_download_token_round_trips_and_carries_its_account(token_env: None) -> None:
    token, ttl = releases.mint_download_token("buyer@example.com")
    assert ttl == releases.DOWNLOAD_TOKEN_TTL == 900
    assert releases._redeem_download_token(token) == "buyer@example.com"


def test_download_token_expires(token_env: None) -> None:
    token, ttl = releases.mint_download_token("buyer@example.com")
    minted_at = int(time.time())
    assert releases._redeem_download_token(token, now=minted_at + ttl - 1) is not None
    assert releases._redeem_download_token(token, now=minted_at + ttl) is None
    assert releases._redeem_download_token(token, now=minted_at + ttl + 3600) is None


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"), id="signature"),
        pytest.param(
            lambda t: t.split(".")[0] + "." + str(int(t.split(".")[1]) + 86400)
            + "." + t.split(".")[2],
            id="extended_expiry",
        ),
        pytest.param(
            lambda t: base64.urlsafe_b64encode(b"attacker@example.com")
            .decode("ascii").rstrip("=") + "." + ".".join(t.split(".")[1:]),
            id="swapped_account",
        ),
        pytest.param(lambda t: t.replace(".", "", 1), id="shape"),
        pytest.param(lambda _t: "", id="empty"),
    ],
)
def test_download_token_rejects_every_tampering(token_env: None, mangle) -> None:
    token, _ = releases.mint_download_token("buyer@example.com")
    assert releases._redeem_download_token(mangle(token)) is None


def test_download_token_is_refused_when_the_account_stops_being_active(
    monkeypatch: pytest.MonkeyPatch, token_env: None
) -> None:
    """A token minted while active must stop working the moment the account is
    suspended — which is why status is re-read at redeem time rather than baked
    into the token."""
    token, _ = releases.mint_download_token("buyer@example.com")
    assert releases._redeem_download_token(token) == "buyer@example.com"
    monkeypatch.setattr(
        releases.accounts, "get_account", lambda _email: _Account("suspended")
    )
    assert releases._redeem_download_token(token) is None
    monkeypatch.setattr(releases.accounts, "get_account", lambda _email: None)
    assert releases._redeem_download_token(token) is None




def test_download_token_needs_a_session_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no secret every token would share one forgeable MAC, so minting
    refuses rather than issuing something unverifiable."""
    monkeypatch.setattr(releases.settings, "session_secret", "")
    with pytest.raises(Exception) as error:
        releases.mint_download_token("buyer@example.com")
    assert getattr(error.value, "status_code", None) == 503


@pytest.mark.parametrize("carrier", ["header", "query"])
def test_a_download_token_earns_a_download_url_by_header_or_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, token_env: None, carrier: str
) -> None:
    """Both carriers are required: a browser can set the header, but curl
    following the manifest's URL cannot, so the script appends the query."""
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: False)
    for name in releases._R2_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CHUDWARE_RELEASES_PUBLIC_BASE_URL", raising=False)
    token, _ = releases.mint_download_token("buyer@example.com")
    query = "?channel=stable&platform=linux&arch=x86_64"
    if carrier == "header":
        response = client.get(
            "/api/releases/latest" + query,
            headers={"X-Chudware-Download-Token": token},
        )
    else:
        response = client.get(
            "/api/releases/latest" + query + "&download_token=" + token
        )
    assert response.status_code == 200
    assert response.json()["url"].startswith("https://app.chudware.com/api/releases/")
    assert response.headers["cache-control"] == "private, no-store"


def test_a_forged_download_token_gets_the_anonymous_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, token_env: None
) -> None:
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: False)
    monkeypatch.setattr(
        releases, "_presigned_url",
        lambda _key: pytest.fail("a forged token must not mint a URL"),
    )
    response = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64"
        "&download_token=not-a-real-token"
    )
    assert response.status_code == 200
    assert "url" not in response.json()


def test_the_new_public_paths_are_exactly_scoped() -> None:
    assert auth._is_public_path("/api/releases/available")
    assert auth._is_public_path("/install.sh")
    assert auth._is_public_path("/brand-assets/Rubik-VF.247dbaa0aa4a.woff2")
    # The authenticated minting route must NOT have been opened along the way.
    assert not auth._is_public_path("/api/portal/download-token")
    assert not auth._is_public_path("/api/releases/availablely")
    assert not auth._is_public_path("/install.shx")
    assert not auth._is_public_path("/brand-assetsx")


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_keeps_three_beta_versions_per_tuple_and_every_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row selection only — the filesystem half is exercised against a real
    database and real bytes in test_releases_ci.py."""
    rows = []
    for platform, fmt in (("linux", "default"), ("linux", "deb"), ("windows", "default")):
        for index in range(5):
            rows.append(
                {
                    "channel": "beta",
                    "version": f"0.1.0-beta.{20 - index}",
                    "platform": platform,
                    "arch": "x86_64",
                    "format": fmt,
                    "artifact_key": f"beta/0.1.0-beta.{20 - index}/{platform}-{fmt}",
                    "size": 1024,
                }
            )

    class Cursor:
        def execute(self, _statement: str, _values: tuple[Any, ...]) -> None:
            pass

        def fetchall(self) -> list[dict[str, Any]]:
            return rows

    @contextmanager
    def fake_tx():
        yield Cursor()

    monkeypatch.setattr(releases, "tx", fake_tx)
    prunable = releases._prunable_beta_rows(releases.BETA_RETENTION_VERSIONS)
    assert releases.BETA_RETENTION_VERSIONS == 3
    # Three tuples x (5 published - 3 kept).
    assert len(prunable) == 6
    kept = {(r["platform"], r["format"], r["version"]) for r in rows} - {
        (r["platform"], r["format"], r["version"]) for r in prunable
    }
    assert {version for _, _, version in kept} == {
        "0.1.0-beta.20", "0.1.0-beta.19", "0.1.0-beta.18",
    }
    # Stable is never a candidate: the query itself filters on the beta channel.
    assert all(row["channel"] == "beta" for row in prunable)


def test_retention_is_a_noop_when_nothing_is_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        def execute(self, _statement: str, _values: tuple[Any, ...] = ()) -> None:
            pytest.fail("an idle prune must not issue a DELETE")

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    @contextmanager
    def fake_tx():
        yield Cursor()

    monkeypatch.setattr(releases, "_prunable_beta_rows", lambda _keep: [])
    monkeypatch.setattr(releases, "_incoming_orphans", lambda: [])
    monkeypatch.setattr(releases, "tx", fake_tx)
    report = releases.prune_release_store()
    assert report == {
        "ok": True,
        "dry_run": False,
        "keep_beta_versions": 3,
        "pruned": [],
        "deleted_files": 0,
        "freed_bytes": 0,
        "incoming_orphans": [],
        "incoming_freed_bytes": 0,
        "errors": [],
    }

@pytest.mark.parametrize("cdn", [False, True])
def test_staging_download_metadata_cannot_select_production_origin_or_bucket(client, monkeypatch, cdn):
    from urllib.parse import urlparse
    monkeypatch.setattr(releases.settings, "deploy_environment", "staging")
    monkeypatch.setattr(releases.settings, "public_base_url", "https://stage-fixture.example.invalid")
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: True)
    monkeypatch.delenv("CHUDWARE_RELEASES_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("CHUDWARE_RELEASES_BUCKET", raising=False)
    if cdn:
        monkeypatch.setenv("R2_ENDPOINT", "https://objects.example.invalid")
        monkeypatch.setenv("R2_ACCESS_KEY_ID", "fixture-access-id")
        monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "fixture-secret")
    else:
        for name in releases._R2_ENV:
            monkeypatch.delenv(name, raising=False)
    response = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"X-Chudware-Entitlement": "valid"},
    )
    assert response.status_code == 200
    url = urlparse(response.json()["url"])
    assert url.hostname == ("objects.example.invalid" if cdn else "stage-fixture.example.invalid")
    if cdn:
        assert url.path.startswith("/chudware-releases-staging/")
        monkeypatch.setenv("CHUDWARE_RELEASES_BUCKET", "chudware-releases")
    else:
        monkeypatch.setenv("CHUDWARE_RELEASES_PUBLIC_BASE_URL", "https://app.chudware.com")
    rejected = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"X-Chudware-Entitlement": "valid"},
    )
    assert rejected.status_code == 503


def test_staging_release_storage_refuses_a_foreign_root(tmp_path, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(releases.settings, "deploy_environment", "staging")
    monkeypatch.setattr(releases.settings, "data_root", tmp_path / "stage")
    monkeypatch.setenv("CHUDWARE_RELEASES_DIR", str(tmp_path / "production-releases"))
    with pytest.raises(HTTPException) as refused:
        releases._releases_dir()
    assert refused.value.status_code == 503
