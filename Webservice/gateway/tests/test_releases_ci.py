"""Nightly CI publication, beta -> stable promotion, and self-hosted downloads.

Driven through the real ASGI app rather than a bare router: the new routes are
useless if AuthMiddleware answers 401 before the handler's own Bearer/entitlement
check runs, and that is exactly the failure a router-only test cannot see.

The CI public key is REPLACED with a per-test ephemeral one. Nothing here can
mint a signature under the real release-ci-v1 key — that seed is a CI secret and
must never exist in the tree — so the tests prove the verification path with a key
they own, and one separate assertion pins the real key material.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from gateway import accounts, app as app_mod, db, releases
from gateway.config import settings

PUBLISH_TOKEN = "ci-publish-secret"
BETA = "0.1.0-beta.42"
STABLE = "0.1.0"
_REAL_CI_PUBLIC_KEY = releases._RELEASE_PUBLIC_KEYS[releases._CI_KEY_ID]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def ci_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private releases directory, a known publish token, and no R2."""
    root = tmp_path / "chudware-releases"
    (root / "incoming").mkdir(parents=True)
    monkeypatch.setenv("CHUDWARE_RELEASES_DIR", str(root))
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", PUBLISH_TOKEN)
    for name in releases._R2_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CHUDWARE_RELEASES_PUBLIC_BASE_URL", raising=False)
    for limiter in ("_manifest_by_ip", "_publish_by_ip", "_base_version_by_ip", "_artifact_by_ip", "_ci_by_ip"):
        monkeypatch.setattr(releases, limiter, releases.RateLimiter(10_000, 60.0))
    return root


@pytest.fixture(autouse=True)
def ci_key(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    private = Ed25519PrivateKey.generate()
    monkeypatch.setitem(
        releases._RELEASE_PUBLIC_KEYS,
        releases._CI_KEY_ID,
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ),
    )
    return private


@pytest.fixture
def ci() -> TestClient:
    return TestClient(app_mod.app, headers={"Authorization": f"Bearer {PUBLISH_TOKEN}"})


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    email = "release-admin@example.com"
    accounts.ensure_account(email, display=email)
    accounts.set_status(email, "active", decided_by="test")
    accounts.set_plan(email, "desktop")
    with db.tx() as cur:
        cur.execute("UPDATE accounts SET role='admin' WHERE email=%s", (email,))
    monkeypatch.setattr(settings, "dev_mode", True)
    return TestClient(app_mod.app, headers={"X-Dev-User": email})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(private: Ed25519PrivateKey, version: str, platform: str, arch: str, digest: str) -> str:
    return base64.b64encode(
        private.sign(
            releases._artifact_signature_preimage(
                releases._CI_KEY_ID, version, platform, arch, digest
            )
        )
    ).decode("ascii")


def _stage(
    version: str = BETA,
    platform: str = "linux",
    arch: str = "x86_64",
    *,
    evidence: bool = True,
) -> dict[str, bytes]:
    """Write an artifact plus its sidecars into incoming/, exactly as CI does."""
    name = releases._canonical_artifact(version, platform, arch)
    files = {name: f"artifact bytes for {name}".encode()}
    for suffix in releases._PAYLOAD_SIDECAR_SUFFIXES:
        files[name + suffix] = f"{name}{suffix} contents".encode()
    if evidence:
        for _, filename in releases._EVIDENCE_SIDECARS:
            files[f"{name}.{filename}"] = f"{name}.{filename} contents".encode()
            files[f"{name}.{filename}.sig"] = f"{name}.{filename}.sig contents".encode()
    incoming = releases._incoming_dir()
    incoming.mkdir(parents=True, exist_ok=True)
    for filename, blob in files.items():
        (incoming / filename).write_bytes(blob)
    return files


def _body(
    private: Ed25519PrivateKey,
    files: dict[str, bytes],
    version: str = BETA,
    stable_version: str = STABLE,
    platform: str = "linux",
    arch: str = "x86_64",
    *,
    evidence: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    name = releases._canonical_artifact(version, platform, arch)
    artifact = files[name]
    digest = hashlib.sha256(artifact).hexdigest()
    body: dict[str, Any] = {
        "version": version,
        "stable_version": stable_version,
        "platform": platform,
        "arch": arch,
        "channel": "beta",
        "key_id": releases._CI_KEY_ID,
        "min_supported": "0.1.0",
        "sha256": digest,
        "size": len(artifact),
        "signature": _sign(private, version, platform, arch, digest),
        "stable_signature": _sign(private, stable_version, platform, arch, digest),
        "notes_url": f"https://chudware.com/releases/{version}",
        "commit_sha": "d" * 40,
    }
    if evidence:
        block: dict[str, str] = {}
        for prefix, filename in releases._EVIDENCE_SIDECARS:
            block[f"{prefix}_sha256"] = hashlib.sha256(files[f"{name}.{filename}"]).hexdigest()
            block[f"{prefix}_signature_sha256"] = hashlib.sha256(
                files[f"{name}.{filename}.sig"]
            ).hexdigest()
        body["evidence"] = block
    body.update(overrides)
    return body


def _publish(ci: TestClient, private: Ed25519PrivateKey, **kwargs: Any):
    files = _stage(
        kwargs.get("version", BETA),
        kwargs.get("platform", "linux"),
        kwargs.get("arch", "x86_64"),
        evidence=kwargs.get("evidence", True),
    )
    return ci.post("/api/releases/ci/publish", json=_body(private, files, **kwargs))


def _insert_stable(version: str, platform: str = "linux", arch: str = "x86_64") -> None:
    """A published stable row, inserted directly: base-version math must hold for
    rows this module did not mint, including ones older than the CI key."""
    name = releases._canonical_artifact(version, platform, arch)
    key = f"stable/{version}/{name}"
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO releases
                   (channel, version, min_supported, platform, arch, key_id, sha256,
                    size, signature, notes_url, artifact_key, payload_manifest_key,
                    payload_manifest_sha256, payload_signature_key, payload_signature_sha256)
            VALUES ('stable', %s, '0.1.0', %s, %s, 'release-v1', %s, 4096, 'sig',
                    'https://chudware.com/releases', %s, %s, %s, %s, %s)
            """,
            (
                version, platform, arch, hashlib.sha256(key.encode()).hexdigest(),
                key, key + ".payload-manifest.v1.json", "a" * 64,
                key + ".payload-manifest.v1.sig", "b" * 64,
            ),
        )


# ---------------------------------------------------------------------------
# Key material
# ---------------------------------------------------------------------------


def test_release_ci_v1_public_key_is_registered_unconditionally() -> None:
    # Captured at import, BEFORE the ephemeral-key fixture swaps it out. Asserted
    # against the literal from the pipeline contract: a typo here makes every
    # nightly a 422 that nobody is watching for.
    assert _REAL_CI_PUBLIC_KEY == bytes.fromhex(
        "d7434dc67f4d6e11cd4c8ced9b5ebbccf2e1477b5949d52d7685b6ca43a010f6"
    )
    assert releases._CI_KEY_ID == "release-ci-v1"


# ---------------------------------------------------------------------------
# ci/publish rejections
# ---------------------------------------------------------------------------


def test_ci_publish_requires_the_bearer_token(pg, ci_key: Ed25519PrivateKey) -> None:
    files = _stage()
    anonymous = TestClient(app_mod.app)
    response = anonymous.post("/api/releases/ci/publish", json=_body(ci_key, files))
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid release publishing token"


def test_ci_publish_rejects_a_non_beta_channel(pg, ci: TestClient, ci_key) -> None:
    response = _publish(ci, ci_key, channel="stable")
    assert response.status_code == 422
    assert response.json()["detail"] == "ci publications must target the beta channel"
    # The staged bytes are untouched: a rejected publication must not consume the
    # artifact a corrected retry would reuse.
    assert (releases._incoming_dir() / releases._canonical_artifact(BETA, "linux", "x86_64")).is_file()


def test_ci_publish_rejects_a_foreign_key_id(pg, ci: TestClient, ci_key) -> None:
    response = _publish(ci, ci_key, key_id="release-v1")
    assert response.status_code == 422
    assert response.json()["detail"] == "ci publications must be signed by release-ci-v1"


def test_ci_publish_rejects_a_digest_mismatch_and_destroys_the_staged_artifact(
    pg, ci: TestClient, ci_key
) -> None:
    files = _stage()
    body = _body(ci_key, files)
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    (releases._incoming_dir() / name).write_bytes(b"substituted bytes")
    response = ci.post("/api/releases/ci/publish", json=body)
    assert response.status_code == 422
    assert "does not match the publication" in response.json()["detail"]
    # Left in place it would sit under a canonical name waiting to be trusted by
    # the next run's publish.
    assert not (releases._incoming_dir() / name).exists()


def test_ci_publish_rejects_a_missing_staged_artifact(pg, ci: TestClient, ci_key) -> None:
    files = _stage()
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    (releases._incoming_dir() / name).unlink()
    response = ci.post("/api/releases/ci/publish", json=_body(ci_key, files))
    assert response.status_code == 404
    assert name in response.json()["detail"]


def test_ci_publish_rejects_a_stable_signature_over_the_wrong_version(
    pg, ci: TestClient, ci_key
) -> None:
    files = _stage()
    body = _body(ci_key, files)
    digest = body["sha256"]
    body["stable_signature"] = _sign(ci_key, "9.9.9", "linux", "x86_64", digest)
    response = ci.post("/api/releases/ci/publish", json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "stable_signature does not match the stable release identity"
    )


def test_ci_publish_rejects_evidence_digests_that_disagree_with_the_staged_bytes(
    pg, ci: TestClient, ci_key
) -> None:
    files = _stage()
    body = _body(ci_key, files)
    body["evidence"]["provenance_sha256"] = "0" * 64
    response = ci.post("/api/releases/ci/publish", json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == "provenance_sha256 does not match the staged sidecar"


def test_ci_publish_rejects_a_missing_payload_manifest_sidecar(
    pg, ci: TestClient, ci_key
) -> None:
    files = _stage()
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    (releases._incoming_dir() / (name + ".payload-manifest.v1.sig")).unlink()
    response = ci.post("/api/releases/ci/publish", json=_body(ci_key, files))
    assert response.status_code == 422
    assert "payload manifest sidecar" in response.json()["detail"]


def test_ci_publish_rejects_a_prerelease_stable_version(pg, ci: TestClient, ci_key) -> None:
    response = _publish(ci, ci_key, stable_version="0.1.0-beta.42")
    assert response.status_code == 422
    assert "stable_version" in response.json()["detail"]


# ---------------------------------------------------------------------------
# ci/publish happy path
# ---------------------------------------------------------------------------


def test_ci_publish_moves_every_file_into_the_store_and_records_the_stable_pair(
    pg, ci: TestClient, ci_key
) -> None:
    files = _stage()
    response = ci.post("/api/releases/ci/publish", json=_body(ci_key, files))
    assert response.status_code == 200, response.text
    payload = response.json()
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    assert payload["artifact_key"] == f"beta/{BETA}/{name}"
    assert payload["stable_version"] == STABLE
    assert payload["evidence_complete"] is True
    assert len(payload["stored"]) == len(files) == 9

    # incoming/ is drained; store/ holds every byte under the canonical layout.
    assert sorted(p.name for p in releases._incoming_dir().iterdir()) == []
    store = releases._store_dir() / "beta" / BETA
    assert sorted(p.name for p in store.iterdir()) == sorted(files)
    for filename, blob in files.items():
        assert (store / filename).read_bytes() == blob

    with db.tx() as cur:
        cur.execute("SELECT * FROM releases WHERE channel='beta'")
        rows = cur.fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["version"] == BETA
    assert row["stable_version"] == STABLE
    assert row["stable_signature"] and row["key_id"] == releases._CI_KEY_ID
    assert row["sha256"] == hashlib.sha256(files[name]).hexdigest()
    assert row["commit_sha"] == "d" * 40
    assert row["sbom_key"] == f"beta/{BETA}/{name}.sbom.cdx.json"
    assert row["platform_test_signature_key"] == f"beta/{BETA}/{name}.platform-test.v1.json.sig"


def test_ci_publish_is_not_repeatable_for_the_same_identity(pg, ci: TestClient, ci_key) -> None:
    assert _publish(ci, ci_key).status_code == 200
    second = _publish(ci, ci_key)
    assert second.status_code == 409
    assert "already registered" in second.json()["detail"]


def test_ci_publish_without_evidence_stores_no_partial_bundle(
    pg, ci: TestClient, ci_key
) -> None:
    response = _publish(ci, ci_key, evidence=False)
    assert response.status_code == 200, response.text
    assert response.json()["evidence_complete"] is False
    with db.tx() as cur:
        cur.execute("SELECT * FROM releases WHERE channel='beta'")
        row = dict(cur.fetchone())
    # 0012 makes the bundle all-or-none WITH commit_sha; a commit id with no
    # provenance would be a claim the gateway cannot support.
    assert all(row[column] is None for column in releases._EVIDENCE_COLUMNS)
    assert row["payload_manifest_sha256"] is not None


def test_successive_nightlies_share_one_constant_min_supported(
    pg, ci: TestClient, ci_key
) -> None:
    """The exact body nightly-release.yml sends, twice, verbatim in shape.

    The workflow holds min_supported at a constant on purpose so a nightly never
    advances the channel floor — and the floor guard rejects only a STRICTLY
    backward move, so the second run must be accepted. A guard that rejected an
    equal floor would have broken every build after the first, silently, on the
    second night rather than in review.
    """
    for run_number in (42, 43):
        version = f"0.1.0-beta.{run_number}"
        files = _stage(version)
        name = releases._canonical_artifact(version, "linux", "x86_64")
        digest = hashlib.sha256(files[name]).hexdigest()
        body = {
            "version": version,
            "stable_version": "0.1.0",
            "platform": "linux",
            "arch": "x86_64",
            "channel": "beta",
            "key_id": "release-ci-v1",
            "min_supported": "0.1.0",
            "sha256": digest,
            "size": len(files[name]),
            "signature": _sign(ci_key, version, "linux", "x86_64", digest),
            "stable_signature": _sign(ci_key, "0.1.0", "linux", "x86_64", digest),
            "notes_url": "https://app.chudware.com/",
            "commit_sha": "9f" * 20,
            "evidence": {
                key: value
                for prefix, filename in releases._EVIDENCE_SIDECARS
                for key, value in (
                    (f"{prefix}_sha256", hashlib.sha256(files[f"{name}.{filename}"]).hexdigest()),
                    (
                        f"{prefix}_signature_sha256",
                        hashlib.sha256(files[f"{name}.{filename}.sig"]).hexdigest(),
                    ),
                )
            },
        }
        published = ci.post("/api/releases/ci/publish", json=body)
        assert published.status_code == 200, published.text

        # GITHUB_RUN_ID is a long digit string; one ledger row per run.
        reported = ci.post(
            "/api/releases/ci/status",
            json={
                "run_id": f"1748392{run_number}", "commit_sha": "9f" * 20,
                "channel": "beta", "platform": "linux", "arch": "x86_64",
                "version": version, "status": "published", "detail": "",
            },
        )
        assert reported.status_code == 200, reported.text

    with db.tx() as cur:
        cur.execute("SELECT version FROM releases WHERE channel='beta' ORDER BY version")
        assert [row["version"] for row in cur.fetchall()] == [
            "0.1.0-beta.42", "0.1.0-beta.43",
        ]
        cur.execute("SELECT min_supported FROM release_channels WHERE channel='beta'")
        assert dict(cur.fetchone())["min_supported"] == "0.1.0"


# ---------------------------------------------------------------------------
# ci/status
# ---------------------------------------------------------------------------


def test_ci_status_upserts_one_row_per_run_platform_arch(pg, ci: TestClient) -> None:
    def report(status: str, detail: str = "") -> Any:
        return ci.post(
            "/api/releases/ci/status",
            json={
                "run_id": "4711", "commit_sha": "e" * 40, "channel": "beta",
                "platform": "linux", "arch": "x86_64", "version": BETA,
                "status": status, "detail": detail,
            },
        )

    first = report("queued")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "queued"
    for status in ("building", "testing", "packaging", "publishing", "published"):
        assert report(status).status_code == 200
    failed_other_platform = ci.post(
        "/api/releases/ci/status",
        json={
            "run_id": "4711", "commit_sha": "e" * 40, "channel": "beta",
            "platform": "windows", "arch": "x86_64", "version": BETA,
            "status": "failed", "detail": "iscc missing",
        },
    )
    assert failed_other_platform.status_code == 200

    with db.tx() as cur:
        cur.execute("SELECT platform, status, detail FROM ci_builds ORDER BY platform")
        rows = [dict(row) for row in cur.fetchall()]
    assert rows == [
        {"platform": "linux", "status": "published", "detail": ""},
        {"platform": "windows", "status": "failed", "detail": "iscc missing"},
    ]


def test_ci_status_rejects_an_unknown_status(pg, ci: TestClient) -> None:
    response = ci.post(
        "/api/releases/ci/status",
        json={
            "run_id": "4711", "commit_sha": "e" * 40, "channel": "beta",
            "platform": "linux", "arch": "x86_64", "version": BETA,
            "status": "exploded", "detail": "",
        },
    )
    assert response.status_code == 422
    assert "status must be one of" in response.json()["detail"]


def test_ci_builds_is_token_gated_and_filters_the_planner_query(
    pg, ci: TestClient
) -> None:
    for run_id, commit, status in (("1", "a" * 40, "published"), ("2", "b" * 40, "failed")):
        assert ci.post(
            "/api/releases/ci/status",
            json={
                "run_id": run_id, "commit_sha": commit, "channel": "beta",
                "platform": "linux", "arch": "x86_64", "version": BETA,
                "status": status, "detail": "",
            },
        ).status_code == 200
    assert TestClient(app_mod.app).get("/api/releases/ci/builds").status_code == 401
    published = ci.get("/api/releases/ci/builds", params={"commit_sha": "a" * 40, "status": "published"})
    assert published.status_code == 200
    assert [b["run_id"] for b in published.json()["builds"]] == ["1"]
    assert ci.get(
        "/api/releases/ci/builds", params={"commit_sha": "b" * 40, "status": "published"}
    ).json()["builds"] == []


# ---------------------------------------------------------------------------
# base-version
# ---------------------------------------------------------------------------


def test_base_version_floors_at_the_first_release(pg) -> None:
    response = TestClient(app_mod.app).get("/api/releases/base-version")
    assert response.status_code == 200
    assert response.json() == {"base": "0.1.0"}


def test_base_version_is_the_numeric_maximum_patch_plus_one(pg) -> None:
    for version in ("0.9.9", "2.9.99", "2.10.4", "1.0.0"):
        _insert_stable(version)
    # Betas never advance the base: 2.11.0-beta.7 must not become 2.11.1.
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO releases
                   (channel, version, min_supported, platform, arch, key_id, sha256,
                    size, signature, notes_url, artifact_key, payload_manifest_key,
                    payload_manifest_sha256, payload_signature_key, payload_signature_sha256)
            VALUES ('beta', '9.9.9-beta.1', '0.1.0', 'linux', 'x86_64', 'release-ci-v1',
                    %s, 1, 'sig', 'https://chudware.com/r', 'beta/9.9.9-beta.1/a',
                    'beta/9.9.9-beta.1/a.json', %s, 'beta/9.9.9-beta.1/a.sig', %s)
            """,
            ("c" * 64, "d" * 64, "e" * 64),
        )
    response = TestClient(app_mod.app).get("/api/releases/base-version")
    assert response.json() == {"base": "2.10.5"}


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


def test_promote_requires_an_admin_session(pg, ci: TestClient, ci_key) -> None:
    assert _publish(ci, ci_key).status_code == 200
    response = ci.post(
        "/api/admin/releases/promote",
        json={"channel": "beta", "version": BETA, "platform": "linux", "arch": "x86_64"},
    )
    assert response.status_code in (401, 403)


def test_promote_refuses_a_release_without_a_complete_evidence_bundle(
    pg, ci: TestClient, ci_key, admin: TestClient
) -> None:
    assert _publish(ci, ci_key, evidence=False).status_code == 200
    response = admin.post(
        "/api/admin/releases/promote",
        json={"channel": "beta", "version": BETA, "platform": "linux", "arch": "x86_64"},
    )
    assert response.status_code == 422
    assert "evidence bundle is incomplete" in response.json()["detail"]
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM releases WHERE channel='stable'")
        assert dict(cur.fetchone())["n"] == 0


def test_promote_refuses_a_manual_release_with_no_signed_stable_pair(
    pg, admin: TestClient
) -> None:
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO releases
                   (channel, version, min_supported, platform, arch, key_id, sha256,
                    size, signature, notes_url, artifact_key, payload_manifest_key,
                    payload_manifest_sha256, payload_signature_key, payload_signature_sha256)
            VALUES ('beta', '0.2.0', '0.1.0', 'linux', 'x86_64', 'release-v1', %s, 1,
                    'sig', 'https://chudware.com/r', 'beta/0.2.0/a', 'beta/0.2.0/a.json',
                    %s, 'beta/0.2.0/a.sig', %s)
            """,
            ("c" * 64, "d" * 64, "e" * 64),
        )
    response = admin.post(
        "/api/admin/releases/promote",
        json={"channel": "beta", "version": "0.2.0", "platform": "linux", "arch": "x86_64"},
    )
    assert response.status_code == 422
    assert "no signed stable pair" in response.json()["detail"]


def test_promote_refuses_an_unknown_beta(pg, admin: TestClient) -> None:
    response = admin.post(
        "/api/admin/releases/promote",
        json={"channel": "beta", "version": "7.7.7-beta.1", "platform": "linux", "arch": "x86_64"},
    )
    assert response.status_code == 404


def test_promote_copies_the_bytes_and_serves_the_stable_row_from_this_host(
    pg, ci: TestClient, ci_key, admin: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _stage()
    assert ci.post("/api/releases/ci/publish", json=_body(ci_key, files)).status_code == 200
    beta_name = releases._canonical_artifact(BETA, "linux", "x86_64")
    stable_name = releases._canonical_artifact(STABLE, "linux", "x86_64")

    promoted = admin.post(
        "/api/admin/releases/promote",
        json={"channel": "beta", "version": BETA, "platform": "linux", "arch": "x86_64"},
    )
    assert promoted.status_code == 200, promoted.text
    body = promoted.json()
    assert body["channel"] == "stable"
    assert body["version"] == STABLE
    assert body["artifact_key"] == f"stable/{STABLE}/{stable_name}"
    assert body["promoted_from"] == f"beta/{BETA}/{beta_name}"
    assert body["sha256"] == hashlib.sha256(files[beta_name]).hexdigest()

    # Bytes are copied under the stable name; the beta row keeps its own copy.
    stable_dir = releases._store_dir() / "stable" / STABLE
    assert (stable_dir / stable_name).read_bytes() == files[beta_name]
    assert len(list(stable_dir.iterdir())) == 9
    assert (releases._store_dir() / "beta" / BETA / beta_name).is_file()

    # Second promotion is refused rather than re-announcing a shipped identity.
    again = admin.post(
        "/api/admin/releases/promote",
        json={"channel": "beta", "version": BETA, "platform": "linux", "arch": "x86_64"},
    )
    assert again.status_code == 409

    # The stable row is now what /api/releases/latest hands the updater, with a
    # self-hosted URL because no R2 credential exists on this host.
    monkeypatch.setattr(releases, "_active_entitlement", lambda token: token == "valid")
    manifest = TestClient(app_mod.app).get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"X-Chudware-Entitlement": "valid"},
    )
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["version"] == STABLE
    assert manifest.json()["url"] == (
        f"https://app.chudware.com/api/releases/artifact/stable/{STABLE}/{stable_name}"
    )
    # Ed25519 is deterministic, so re-signing the same preimage reproduces the
    # exact signature CI minted at build time: the stable row carries THAT one,
    # not something re-signed on this host.
    assert manifest.json()["signature"] == _sign(
        ci_key, STABLE, "linux", "x86_64", hashlib.sha256(files[beta_name]).hexdigest()
    )


def test_latest_honours_a_configured_public_base_url(
    pg, ci: TestClient, ci_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _publish(ci, ci_key).status_code == 200
    monkeypatch.setenv("CHUDWARE_RELEASES_PUBLIC_BASE_URL", "https://releases.example.test/")
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: True)
    manifest = TestClient(app_mod.app).get(
        "/api/releases/latest?channel=beta&platform=linux&arch=x86_64",
        headers={"X-Chudware-Entitlement": "valid"},
    )
    assert manifest.status_code == 200
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    assert manifest.json()["url"] == (
        f"https://releases.example.test/api/releases/artifact/beta/{BETA}/{name}"
    )


# ---------------------------------------------------------------------------
# artifact download
# ---------------------------------------------------------------------------


def test_artifact_download_is_entitlement_gated(
    pg, ci: TestClient, ci_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _stage()
    assert ci.post("/api/releases/ci/publish", json=_body(ci_key, files)).status_code == 200
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    url = f"/api/releases/artifact/beta/{BETA}/{name}"

    monkeypatch.setattr(releases, "_active_entitlement", lambda token: token == "valid")
    anonymous = TestClient(app_mod.app).get(url)
    assert anonymous.status_code in (401, 403)
    forged = TestClient(app_mod.app).get(url, headers={"X-Chudware-Entitlement": "forged"})
    assert forged.status_code in (401, 403)

    entitled = TestClient(app_mod.app).get(url, headers={"X-Chudware-Entitlement": "valid"})
    assert entitled.status_code == 200, entitled.text
    assert entitled.content == files[name]
    assert entitled.headers["cache-control"] == "private, no-store"


def test_artifact_download_refuses_a_key_with_no_published_row(
    pg, ci: TestClient, ci_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _stage()
    assert ci.post("/api/releases/ci/publish", json=_body(ci_key, files)).status_code == 200
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: True)
    client = TestClient(app_mod.app)
    # A sidecar sitting right next to a published artifact is still not published.
    sidecar = client.get(
        f"/api/releases/artifact/beta/{BETA}/{name}.sbom.cdx.json",
        headers={"X-Chudware-Entitlement": "valid"},
    )
    assert sidecar.status_code == 404
    assert (releases._store_dir() / "beta" / BETA / f"{name}.sbom.cdx.json").is_file()


def test_artifact_download_never_serves_from_incoming(
    pg, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    key = f"beta/{BETA}/{name}"
    # A row exists but the verified bytes do not; an unverified staged file under
    # the same name must not be substituted for them.
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO releases
                   (channel, version, min_supported, platform, arch, key_id, sha256,
                    size, signature, notes_url, artifact_key, payload_manifest_key,
                    payload_manifest_sha256, payload_signature_key, payload_signature_sha256)
            VALUES ('beta', %s, '0.1.0', 'linux', 'x86_64', 'release-ci-v1', %s, 1,
                    'sig', 'https://chudware.com/r', %s, %s, %s, %s, %s)
            """,
            (BETA, "c" * 64, key, key + ".json", "d" * 64, key + ".sig", "e" * 64),
        )
    (releases._incoming_dir() / name).write_bytes(b"unverified")
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: True)
    response = TestClient(app_mod.app).get(
        f"/api/releases/artifact/{key}", headers={"X-Chudware-Entitlement": "valid"}
    )
    assert response.status_code == 404
    assert "not present" in response.json()["detail"]


@pytest.mark.parametrize(
    "key",
    [
        "beta/../etc/passwd",
        "beta/0.1.0/../../../etc/passwd",
        "/etc/passwd",
        "beta/0.1.0/sub/dir/name",
    ],
)
def test_store_path_refuses_anything_but_three_safe_components(key: str) -> None:
    with pytest.raises(Exception) as error:
        releases._store_path(key)
    assert getattr(error.value, "status_code", None) in (400, 422)


# ---------------------------------------------------------------------------
# admin surfaces
# ---------------------------------------------------------------------------


def test_release_manifests_badge_promotability_and_evidence(
    pg, ci: TestClient, ci_key, admin: TestClient
) -> None:
    assert _publish(ci, ci_key).status_code == 200
    assert _publish(ci, ci_key, version="0.1.0-beta.43", evidence=False).status_code == 200
    rows = admin.get("/api/admin/release-manifests")
    assert rows.status_code == 200, rows.text
    by_version = {row["version"]: row for row in rows.json()}
    assert by_version[BETA]["has_stable_pair"] is True
    assert by_version[BETA]["evidence_complete"] is True
    assert by_version["0.1.0-beta.43"]["has_stable_pair"] is True
    assert by_version["0.1.0-beta.43"]["evidence_complete"] is False


def test_admin_ci_builds_lists_newest_first_and_requires_admin(
    pg, ci: TestClient, admin: TestClient
) -> None:
    for run_id in ("1", "2"):
        assert ci.post(
            "/api/releases/ci/status",
            json={
                "run_id": run_id, "commit_sha": "f" * 40, "channel": "beta",
                "platform": "linux", "arch": "x86_64", "version": BETA,
                "status": "published", "detail": "",
            },
        ).status_code == 200
    assert ci.get("/api/admin/ci/builds").status_code in (401, 403)
    builds = admin.get("/api/admin/ci/builds", params={"limit": 1})
    assert builds.status_code == 200, builds.text
    assert [b["run_id"] for b in builds.json()] == ["2"]


def test_admin_can_delete_a_prerelease_manifest(pg, ci: TestClient, ci_key, admin: TestClient) -> None:
    assert _publish(ci, ci_key).status_code == 200
    deleted = admin.delete(
        "/api/admin/release-manifests",
        params={"channel": "beta", "version": BETA, "platform": "linux", "arch": "x86_64"},
    )
    assert deleted.status_code == 200, deleted.text


def test_new_release_routes_are_reachable_through_the_auth_middleware() -> None:
    from gateway import auth

    for path in (
        "/api/releases/base-version",
        "/api/releases/ci/publish",
        "/api/releases/ci/status",
        "/api/releases/ci/builds",
        "/api/releases/artifact/beta/0.1.0-beta.1/Chudware-0.1.0-beta.1-linux-x86_64.AppImage",
    ):
        assert auth._is_public_path(path), path
    # Prefix, not substring: the admin surfaces stay behind the session gate.
    assert not auth._is_public_path("/api/admin/releases/promote")
    assert not auth._is_public_path("/api/admin/ci/builds")
    assert not auth._is_public_path("/api/releases/artifactory")


def test_ci_upload_streams_into_incoming_and_digests(ci: TestClient, ci_env) -> None:
    payload = b"fake-installer-bytes" * 4096
    expected = hashlib.sha256(payload).hexdigest()
    response = ci.put(
        "/api/releases/ci/upload/Chudware-0.1.0-beta.1-linux-x86_64.AppImage",
        content=payload,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["size"] == len(payload)
    assert body["sha256"] == expected
    staged = releases._incoming_dir() / "Chudware-0.1.0-beta.1-linux-x86_64.AppImage"
    assert staged.read_bytes() == payload
    assert not (releases._incoming_dir() / (staged.name + ".part")).exists()


def test_ci_upload_requires_the_publish_token(ci_env) -> None:
    anonymous = TestClient(app_mod.app)
    response = anonymous.put("/api/releases/ci/upload/payload.bin", content=b"x")
    assert response.status_code == 401
    wrong = TestClient(app_mod.app, headers={"Authorization": "Bearer wrong"})
    assert wrong.put("/api/releases/ci/upload/payload.bin", content=b"x").status_code == 401


def test_ci_upload_sanitizes_the_name(ci: TestClient, ci_env) -> None:
    response = ci.put("/api/releases/ci/upload/..%2f..%2fevil", content=b"x")
    assert response.status_code in (400, 404, 422)


def test_ci_upload_then_publish_round_trip(ci: TestClient, ci_key, ci_env) -> None:
    version, platform, arch = BETA, "linux", "x86_64"
    name = releases._canonical_artifact(version, platform, arch)
    # Stage via the upload endpoint instead of _stage()'s direct file writes: the
    # upload path is the one the Windows and macOS runners actually use.
    artifact = b"round-trip-artifact" * 1024
    uploaded = ci.put(f"/api/releases/ci/upload/{name}", content=artifact)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["sha256"] == hashlib.sha256(artifact).hexdigest()
    for suffix in (".payload-manifest.v1.json", ".payload-manifest.v1.sig"):
        small = b"sidecar" + suffix.encode()
        assert ci.put(
            f"/api/releases/ci/upload/{name}{suffix}", content=small
        ).status_code == 200
    files = {name: artifact}
    for suffix in (".payload-manifest.v1.json", ".payload-manifest.v1.sig"):
        files[f"{name}{suffix}"] = b"sidecar" + suffix.encode()
    response = ci.post(
        "/api/releases/ci/publish", json=_body(ci_key, files, evidence=False)
    )
    assert response.status_code == 200, response.text
