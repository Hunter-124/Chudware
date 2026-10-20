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
import json
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
from gateway import release_evidence

PUBLISH_TOKEN = "ci-publish-secret"
BETA = "0.1.0-beta.42"
STABLE = "0.1.0"


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
    monkeypatch.setitem(globals(), "_STAGE_SIGNER", private)
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


def signed_sidecars(private, name, artifact, version, platform, arch, key_id, *,
                    evidence=True, owner=None, publisher=None):
    """Inert bytes with genuine signatures from disposable test keys only."""
    digest = hashlib.sha256(artifact).hexdigest()
    payload = {"schema": "chudware-payload-v1", "product": "Chudware",
               "version": version, "platform": platform, "arch": arch,
               "created_at": "2026-10-12T00:00:00Z", "key_id": key_id,
               "files": [{"path": "Chudware", "size": len(artifact), "sha256": digest,
                          "mode": 0o755, "kind": "executable"}]}
    raw = release_evidence._payload.canonical_manifest_bytes(payload)
    files = {name + ".payload-manifest.v1.json": raw,
             name + ".payload-manifest.v1.sig": base64.b64encode(private.sign(
                 release_evidence._payload.payload_signature_preimage(key_id, raw))) + b"\n"}
    if not evidence:
        return files
    components = [{"type": "file", "name": "reviewed-input.lock",
                   "hashes": [{"alg": "SHA-256", "content": "a" * 64}]}]
    for filename in ("core-ctest.log", "core-junit.xml", "host-ctest.log",
                     "host-junit.xml", "test-results.json"):
        components.append({"type": "file", "name": "retained-test-results/" + filename,
                           "hashes": [{"alg": "SHA-256",
                                       "content": hashlib.sha256(filename.encode()).hexdigest()}]})
    run_details: dict[str, Any] = {"builder": {"id": "https://chudware.com/builders/platform-v1"}}
    if platform in {"windows", "macos"}:
        kind = "authenticode" if platform == "windows" else "notarization"
        report: dict[str, Any] = {"schema": "platform-signing.v1", "platform": platform,
                                  "kind": kind, "artifact_sha256": digest}
        annotations: dict[str, Any] = {"report": report}
        if owner is None:
            report.update({"verified": False,
                           "reason": "owner_authorized_native_verification_not_supplied"})
        else:
            policy = {"schema": "chudware-platform-publishers-v1", "key_id": "release-v1",
                      "windows": {"certificate_sha256": ["c" * 64]},
                      "macos": {"team_ids": ["ABCDE12345"], "bundle_id": "com.chudware.Chudware"}}
            raw_policy = release_evidence._json.canonical_json_bytes(policy)
            report.update({
                "verified": True,
                "publisher_policy_sha256": hashlib.sha256(raw_policy).hexdigest(),
                "publisher": publisher or ({"certificate_sha256": "c" * 64} if platform == "windows"
                                           else {"team_id": "ABCDE12345",
                                                 "bundle_id": "com.chudware.Chudware"}),
            })
            annotations.update({
                "publisher_policy": policy,
                "publisher_policy_signature": base64.b64encode(owner.sign(
                    release_evidence._publishers.POLICY_DOMAIN + raw_policy)).decode("ascii"),
            })
        run_details["byproducts"] = [{
            "name": "platform-signing.v1.json",
            "digest": {"sha256": hashlib.sha256(
                release_evidence._json.canonical_json_bytes(report)).hexdigest()},
            "annotations": annotations,
        }]
    documents = {
        "sbom": {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
                 "metadata": {"component": {"type": "application", "name": "Chudware",
                                            "version": version}},
                 "components": components},
        "provenance": {"_type": "https://in-toto.io/Statement/v1",
                       "subject": [{"name": name, "digest": {"sha256": digest}}],
                       "predicateType": "https://slsa.dev/provenance/v1",
                       "predicate": {"buildDefinition": {
                           "buildType": "https://chudware.com/build/platform/v1",
                           "externalParameters": {"version": version, "platform": platform,
                                                  "arch": arch, "commit": "d" * 40},
                           "resolvedDependencies": [
                               {"uri": row["name"], "digest": {"sha256": row["hashes"][0]["content"]}}
                               for row in components]},
                           "runDetails": run_details}},
        "platform_test": {"schema": "platform-test.v1", "version": version, "platform": platform,
                          "arch": arch, "commit": "d" * 40, "artifact_sha256": digest,
                          "commands": ["ctest core", "ctest host"], "passed": True},
    }
    for kind, document in documents.items():
        suffix, signature_suffix = release_evidence._SIDECARS[kind]
        raw = release_evidence._json.canonical_json_bytes(document)
        files[name + suffix] = raw
        files[name + signature_suffix] = base64.b64encode(private.sign(
            release_evidence._DOMAINS[kind] + raw)) + b"\n"
    return files



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
    fmt: str = "default",
    owner: Ed25519PrivateKey | None = None,
    publisher: dict[str, str] | None = None,
) -> dict[str, bytes]:
    """Write an artifact plus its sidecars into incoming/, exactly as CI does."""
    name = releases._canonical_artifact(version, platform, arch, fmt)
    files = {name: f"artifact bytes for {name}".encode()}
    files.update(signed_sidecars(
        _STAGE_SIGNER, name, files[name], version, platform, arch,
        releases._CI_KEY_ID, evidence=evidence, owner=owner, publisher=publisher,
    ))
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
    fmt: str = "default",
    **overrides: Any,
) -> dict[str, Any]:
    name = releases._canonical_artifact(version, platform, arch, fmt)
    artifact = files[name]
    digest = hashlib.sha256(artifact).hexdigest()
    body: dict[str, Any] = {
        "version": version,
        "stable_version": stable_version,
        "platform": platform,
        "arch": arch,
        "format": fmt,
        "channel": "beta",
        "key_id": releases._CI_KEY_ID,
        "min_supported": "0.1.0",
        "sha256": digest,
        "size": len(artifact),
        # The preimage is (key_id, version, platform, arch, sha256) with NO
        # format, so the same signature is valid for every format of a version
        # that happens to share a digest — the digest is what binds the bytes.
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
    owner = kwargs.pop("owner", None)
    publisher = kwargs.pop("publisher", None)
    files = _stage(
        kwargs.get("version", BETA),
        kwargs.get("platform", "linux"),
        kwargs.get("arch", "x86_64"),
        evidence=kwargs.get("evidence", True),
        fmt=kwargs.get("fmt", "default"),
        owner=owner,
        publisher=publisher,
    )
    return ci.post("/api/releases/ci/publish", json=_body(private, files, **kwargs))


def _insert_stable(
    version: str, platform: str = "linux", arch: str = "x86_64", fmt: str = "default"
) -> None:
    """A published stable row, inserted directly: base-version math must hold for
    rows this module did not mint, including ones older than the CI key."""
    name = releases._canonical_artifact(version, platform, arch, fmt)
    key = f"stable/{version}/{name}"
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO releases
                   (channel, version, min_supported, platform, arch, format, key_id,
                    sha256, size, signature, notes_url, artifact_key,
                    payload_manifest_key, payload_manifest_sha256,
                    payload_signature_key, payload_signature_sha256)
            VALUES ('stable', %s, '0.1.0', %s, %s, %s, 'release-v1', %s, 4096, 'sig',
                    'https://chudware.com/releases', %s, %s, %s, %s, %s)
            """,
            (
                version, platform, arch, fmt,
                hashlib.sha256(key.encode()).hexdigest(),
                key, key + ".payload-manifest.v1.json", "a" * 64,
                key + ".payload-manifest.v1.sig", "b" * 64,
            ),
        )
    # Bytes as well as a row: a published row with nothing behind it is not a
    # state the gateway can be in, and retention has to be able to observe that
    # a stable artifact it must never delete is actually on disk.
    store = releases._store_path(key)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(f"stable artifact bytes for {name}".encode())
    for suffix in releases._PAYLOAD_SIDECAR_SUFFIXES:
        store.with_name(store.name + suffix).write_bytes(b"sidecar")




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


def test_ci_publish_rejects_substituted_staged_artifact_before_registration(
    pg, ci: TestClient, ci_key
) -> None:
    files = _stage()
    body = _body(ci_key, files)
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    (releases._incoming_dir() / name).write_bytes(b"substituted bytes")
    response = ci.post("/api/releases/ci/publish", json=body)
    assert response.status_code == 422
    with db.tx() as cur:
        cur.execute("SELECT artifact_key FROM releases")
        assert cur.fetchone() is None
    assert not releases._store_path(f"beta/{BETA}/{name}").exists()


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
    assert "release input is missing or unsafe" in response.json()["detail"]


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
            "commit_sha": "d" * 40,
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
                "run_id": f"1748392{run_number}", "commit_sha": "d" * 40,
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


def test_ci_upload_assembles_chunks_and_reports_one_digest(ci: TestClient, ci_env) -> None:
    """The public origin answers 413 over 100 MB, so big artifacts arrive in pieces."""
    name = "Chudware-Setup-0.1.0-beta.1-x86_64.exe"
    payload = bytes(range(256)) * 4096
    chunk = len(payload) // 4
    offset = 0
    body: dict[str, object] = {}
    while offset < len(payload):
        piece = payload[offset:offset + chunk]
        last = offset + len(piece) >= len(payload)
        response = ci.put(
            f"/api/releases/ci/upload/{name}?offset={offset}"
            + ("&complete=1" if last else ""),
            content=piece,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["complete"] is last
        # Only the finalizing call may claim a digest: a per-chunk digest is a
        # digest of a fragment, which nothing should mistake for the artifact's.
        assert ("sha256" in body) is last
        offset += len(piece)
    staged = releases._incoming_dir() / name
    assert staged.read_bytes() == payload
    assert not (releases._incoming_dir() / (name + ".part")).exists()
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()


def test_ci_upload_rejects_a_chunk_that_does_not_continue_the_part(
    ci: TestClient, ci_env
) -> None:
    name = "Chudware-Setup-0.1.0-beta.2-x86_64.exe"
    assert ci.put(f"/api/releases/ci/upload/{name}?offset=0", content=b"abc").status_code == 200
    # A repeated first chunk, or a gap, would corrupt the artifact silently.
    assert ci.put(f"/api/releases/ci/upload/{name}?offset=0", content=b"abc").status_code == 409
    assert ci.put(f"/api/releases/ci/upload/{name}?offset=9", content=b"abc").status_code == 409
    assert ci.put(f"/api/releases/ci/upload/{name}?offset=-1", content=b"abc").status_code == 422
    finalized = ci.put(f"/api/releases/ci/upload/{name}?offset=3&complete=1", content=b"def")
    assert finalized.status_code == 200, finalized.text
    assert (releases._incoming_dir() / name).read_bytes() == b"abcdef"


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
    files = {name: artifact}
    files.update(signed_sidecars(ci_key, name, artifact, version, platform, arch,
                                 releases._CI_KEY_ID, evidence=False))
    for filename, blob in files.items():
        if filename != name:
            assert ci.put(f"/api/releases/ci/upload/{filename}", content=blob).status_code == 200
    response = ci.post(
        "/api/releases/ci/publish", json=_body(ci_key, files, evidence=False)
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Formats: publish -> latest -> promote, against a real database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "expected_beta", "expected_stable"),
    [
        (
            "default",
            f"Chudware-{BETA}-linux-x86_64.AppImage",
            f"Chudware-{STABLE}-linux-x86_64.AppImage",
        ),
        # 0.1.0-beta.42 -> 0.1.0~beta.42; the release drops the suffix entirely.
        ("deb", "chudware_0.1.0~beta.42_amd64.deb", "chudware_0.1.0_amd64.deb"),
        ("rpm", "chudware-0.1.0-0.beta.42.x86_64.rpm", "chudware-0.1.0-1.x86_64.rpm"),
        (
            "tar",
            f"Chudware-{BETA}-linux-x86_64.tar.zst",
            f"Chudware-{STABLE}-linux-x86_64.tar.zst",
        ),
    ],
)
def test_a_format_round_trips_through_publish_latest_and_promote(
    pg, ci: TestClient, admin: TestClient, ci_key, fmt, expected_beta, expected_stable
) -> None:
    published = _publish(ci, ci_key, fmt=fmt)
    assert published.status_code == 200, published.text
    assert published.json()["format"] == fmt
    assert published.json()["artifact_key"] == f"beta/{BETA}/{expected_beta}"

    manifest = ci.get(
        "/api/releases/latest",
        params={"channel": "beta", "platform": "linux", "arch": "x86_64", "format": fmt},
    )
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["format"] == fmt
    assert manifest.json()["version"] == BETA

    promoted = admin.post(
        "/api/admin/releases/promote",
        json={
            "channel": "beta", "version": BETA, "platform": "linux",
            "arch": "x86_64", "format": fmt,
        },
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["format"] == fmt
    assert promoted.json()["artifact_key"] == f"stable/{STABLE}/{expected_stable}"
    # The bytes were copied, not re-signed, so the digest is unchanged.
    assert promoted.json()["sha256"] == manifest.json()["sha256"]
    assert (releases._store_dir() / "stable" / STABLE / expected_stable).is_file()

    stable_manifest = ci.get(
        "/api/releases/latest",
        params={
            "channel": "stable", "platform": "linux", "arch": "x86_64", "format": fmt,
        },
    )
    assert stable_manifest.status_code == 200
    assert stable_manifest.json()["version"] == STABLE
    assert stable_manifest.json()["format"] == fmt


@pytest.fixture
def owner_key(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    private = Ed25519PrivateKey.generate()
    monkeypatch.setitem(releases._RELEASE_PUBLIC_KEYS, "release-v1", private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return private


def test_windows_msi_round_trips_publish_latest_and_promote(
    pg, ci: TestClient, admin: TestClient, ci_key, owner_key
) -> None:
    published = _publish(
        ci, ci_key, platform="windows", arch="x86_64", fmt="msi", owner=owner_key
    )
    assert published.status_code == 200, published.text
    assert published.json()["format"] == "msi"
    assert published.json()["artifact_key"] == (
        f"beta/{BETA}/Chudware-{BETA}-x86_64.msi"
    )

    manifest = ci.get(
        "/api/releases/latest",
        params={
            "channel": "beta", "platform": "windows", "arch": "x86_64",
            "format": "msi",
        },
    )
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["format"] == "msi"

    promoted = admin.post(
        "/api/admin/releases/promote",
        json={
            "channel": "beta", "version": BETA, "platform": "windows",
            "arch": "x86_64", "format": "msi",
        },
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["format"] == "msi"
    assert promoted.json()["artifact_key"] == (
        f"stable/{STABLE}/Chudware-{STABLE}-x86_64.msi"
    )
    assert promoted.json()["sha256"] == manifest.json()["sha256"]

    stable = ci.get(
        "/api/releases/latest",
        params={
            "channel": "stable", "platform": "windows", "arch": "x86_64",
            "format": "msi",
        },
    )
    assert stable.status_code == 200, stable.text
    assert stable.json()["version"] == STABLE
    assert stable.json()["format"] == "msi"


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_unsigned_native_beta_is_retained_but_never_promoted(pg, ci, admin, ci_key, platform):
    assert _publish(ci, ci_key, platform=platform).status_code == 200
    rows = admin.get("/api/admin/release-manifests").json()
    assert rows[0]["promotable"] is False
    response = admin.post("/api/admin/releases/promote",
                          json={"version": BETA, "platform": platform, "arch": "x86_64"})
    assert response.status_code == 422
    assert "unsigned native beta" in response.json()["detail"]
    with db.tx() as cur:
        cur.execute("SELECT artifact_key FROM releases WHERE channel='stable'")
        assert cur.fetchone() is None


@pytest.mark.parametrize("case", ["publisher", "non_owner_policy"])
def test_native_attestation_requires_owner_authorized_actual_publisher(pg, ci, ci_key, owner_key, case):
    if case == "publisher":
        response = _publish(ci, ci_key, platform="windows", owner=owner_key,
                            publisher={"certificate_sha256": "e" * 64})
    else:
        response = _publish(ci, ci_key, platform="windows", owner=Ed25519PrivateKey.generate())
    assert response.status_code == 422
    with db.tx() as cur:
        cur.execute("SELECT artifact_key FROM releases")
        assert cur.fetchone() is None


def test_four_formats_of_one_version_coexist_and_latest_picks_the_right_one(
    pg, ci: TestClient, ci_key
) -> None:
    """The whole point of moving the primary key. Before 0018 the second format
    of a version was a 409 that read like a duplicate publish."""
    for fmt in ("default", "deb", "rpm", "tar"):
        response = _publish(ci, ci_key, fmt=fmt)
        assert response.status_code == 200, f"{fmt}: {response.text}"

    with db.tx() as cur:
        cur.execute("SELECT format FROM releases WHERE channel='beta' ORDER BY format")
        assert [row["format"] for row in cur.fetchall()] == [
            "deb", "default", "rpm", "tar",
        ]

    digests = {}
    for fmt in ("default", "deb", "rpm", "tar"):
        body = ci.get(
            "/api/releases/latest",
            params={
                "channel": "beta", "platform": "linux", "arch": "x86_64", "format": fmt,
            },
        ).json()
        assert body["format"] == fmt
        digests[fmt] = body["sha256"]
    # Four different files, so four different digests: a manifest that handed
    # back the AppImage's digest for the .deb would break every verification.
    assert len(set(digests.values())) == 4


def test_0019_widens_populated_format_check_and_is_retry_safe(pg) -> None:
    migration = (
        Path(releases.__file__).parent
        / "migrations"
        / "0019_release_format_msi.sql"
    ).read_text(encoding="utf-8")
    with db.tx() as cur:
        cur.execute(
            "ALTER TABLE releases DROP CONSTRAINT IF EXISTS releases_format_check"
        )
        cur.execute(
            "ALTER TABLE releases ADD CONSTRAINT releases_format_check "
            "CHECK (format IN ('default', 'deb', 'rpm', 'tar'))"
        )
    _insert_stable("9.9.0", platform="windows")

    # Operator retries against a populated pre-0019 schema are harmless.
    with db.tx() as cur:
        cur.execute(migration)
        cur.execute(migration)
    with db.tx() as cur:
        cur.execute(
            "UPDATE releases SET format='msi' "
            "WHERE channel='stable' AND version='9.9.0' AND platform='windows'"
        )
        cur.execute(
            "SELECT format FROM releases "
            "WHERE channel='stable' AND version='9.9.0' AND platform='windows'"
        )
        assert cur.fetchone()["format"] == "msi"

    with pytest.raises(Exception) as error:
        with db.tx() as cur:
            cur.execute(
                "UPDATE releases SET format='junk' "
                "WHERE channel='stable' AND version='9.9.0' AND platform='windows'"
            )
    assert "releases_format_check" in str(error.value)


def test_latest_without_a_format_serves_the_appimage_beside_three_packages(
    pg, ci: TestClient, ci_key
) -> None:
    """The shipped 0.1.1 updater's exact request, with all four formats present."""
    for fmt in ("deb", "rpm", "tar", "default"):
        assert _publish(ci, ci_key, fmt=fmt).status_code == 200
    body = ci.get(
        "/api/releases/latest",
        params={"channel": "beta", "platform": "linux", "arch": "x86_64"},
    ).json()
    assert body["format"] == "default"
    appimage = releases._canonical_artifact(BETA, "linux", "x86_64", "default")
    assert body["sha256"] == hashlib.sha256(
        f"artifact bytes for {appimage}".encode()
    ).hexdigest()


def test_ci_publish_refuses_a_package_format_for_windows(
    pg, ci: TestClient, ci_key
) -> None:
    _stage(BETA, "linux", "x86_64")
    response = ci.post(
        "/api/releases/ci/publish",
        json=_body(ci_key, _stage(BETA, "linux", "x86_64"), platform="linux")
        | {"platform": "windows", "format": "deb"},
    )
    assert response.status_code == 422
    assert "only default and msi formats" in response.json()["detail"]


# ---------------------------------------------------------------------------
# /api/releases/available against a real database
# ---------------------------------------------------------------------------


def test_available_lists_the_newest_build_per_tuple_anonymously(
    pg, ci: TestClient, ci_key
) -> None:
    for fmt in ("default", "deb"):
        assert _publish(ci, ci_key, fmt=fmt).status_code == 200
    # A second, newer beta of the AppImage only: `available` must report the
    # newer one for that tuple and leave the .deb's older version alone.
    newer = "0.1.0-beta.43"
    assert _publish(ci, ci_key, version=newer, fmt="default").status_code == 200
    _insert_stable("0.1.1", "windows", "x86_64")

    anonymous = TestClient(app_mod.app)
    beta = anonymous.get("/api/releases/available", params={"channel": "beta"})
    assert beta.status_code == 200, beta.text
    assert beta.headers["cache-control"] == "public, max-age=300"
    builds = beta.json()["builds"]
    assert [(b["platform"], b["arch"], b["format"], b["version"]) for b in builds] == [
        ("linux", "x86_64", "default", newer),
        ("linux", "x86_64", "deb", BETA),
    ]
    for build in builds:
        assert set(build) == {
            "platform", "arch", "format", "version", "size", "sha256", "notes_url",
        }
    assert "signature" not in beta.text and "artifact_key" not in beta.text

    stable = anonymous.get("/api/releases/available").json()
    assert stable["channel"] == "stable"
    assert [(b["platform"], b["format"]) for b in stable["builds"]] == [
        ("windows", "default")
    ]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _store_files(channel: str) -> set[str]:
    root = releases._store_dir() / channel
    if not root.is_dir():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def test_retention_prunes_old_betas_per_format_and_never_touches_stable(
    pg, ci: TestClient, ci_key
) -> None:
    versions = [f"0.1.0-beta.{run}" for run in (40, 41, 42, 43, 44)]
    for version in versions:
        for fmt in ("default", "deb"):
            assert _publish(
                ci, ci_key, version=version, fmt=fmt
            ).status_code == 200, f"{version} {fmt}"
    _insert_stable("0.1.0")
    stable_before = _store_files("stable")
    assert stable_before

    report = releases.prune_release_store()
    assert report["ok"] is True and report["dry_run"] is False
    assert report["keep_beta_versions"] == 3
    # Two tuples x (5 published - 3 kept) = 4 rows, each with 9 files.
    assert len(report["pruned"]) == 4
    assert {(entry["version"], entry["format"]) for entry in report["pruned"]} == {
        ("0.1.0-beta.40", "default"), ("0.1.0-beta.40", "deb"),
        ("0.1.0-beta.41", "default"), ("0.1.0-beta.41", "deb"),
    }
    assert report["deleted_files"] == 4 * 9
    assert report["freed_bytes"] > 0
    assert report["errors"] == []

    with db.tx() as cur:
        cur.execute(
            "SELECT version, format FROM releases WHERE channel='beta'"
            " ORDER BY version, format"
        )
        remaining = [(row["version"], row["format"]) for row in cur.fetchall()]
    assert remaining == [
        ("0.1.0-beta.42", "deb"), ("0.1.0-beta.42", "default"),
        ("0.1.0-beta.43", "deb"), ("0.1.0-beta.43", "default"),
        ("0.1.0-beta.44", "deb"), ("0.1.0-beta.44", "default"),
    ]

    # The bytes went with the rows, and the emptied version directories with
    # them; nothing a surviving row points at was touched.
    beta_files = _store_files("beta")
    assert not any(path.startswith(("0.1.0-beta.40/", "0.1.0-beta.41/")) for path in beta_files)
    assert not (releases._store_dir() / "beta" / "0.1.0-beta.40").exists()
    for version in ("0.1.0-beta.42", "0.1.0-beta.43", "0.1.0-beta.44"):
        assert any(path.startswith(version + "/") for path in beta_files)
    assert _store_files("stable") == stable_before

    # Idempotent: nothing is superseded any more.
    again = releases.prune_release_store()
    assert again["pruned"] == [] and again["deleted_files"] == 0


def test_retention_dry_run_reports_without_deleting(pg, ci: TestClient, ci_key) -> None:
    for run in (40, 41, 42, 43):
        assert _publish(ci, ci_key, version=f"0.1.0-beta.{run}").status_code == 200
    before_files = _store_files("beta")

    report = releases.prune_release_store(dry_run=True)
    assert report["dry_run"] is True
    assert [entry["version"] for entry in report["pruned"]] == ["0.1.0-beta.40"]
    assert report["deleted_files"] == 9
    assert report["freed_bytes"] > 0
    assert _store_files("beta") == before_files
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM releases WHERE channel='beta'")
        assert cur.fetchone()["n"] == 4

    # And the numbers a dry run reported are the numbers a real run frees.
    real = releases.prune_release_store()
    assert real["deleted_files"] == report["deleted_files"]
    assert real["freed_bytes"] == report["freed_bytes"]


def test_retention_keeps_a_promoted_beta_downloadable_as_stable(
    pg, ci: TestClient, admin: TestClient, ci_key
) -> None:
    """Promotion copies bytes rather than linking them, so pruning the beta it
    came from must leave the stable artifact intact and downloadable."""
    assert _publish(ci, ci_key, version="0.1.0-beta.40").status_code == 200
    assert admin.post(
        "/api/admin/releases/promote",
        json={
            "channel": "beta", "version": "0.1.0-beta.40", "platform": "linux",
            "arch": "x86_64",
        },
    ).status_code == 200
    for run in (41, 42, 43):
        assert _publish(ci, ci_key, version=f"0.1.0-beta.{run}").status_code == 200

    assert [e["version"] for e in releases.prune_release_store()["pruned"]] == [
        "0.1.0-beta.40"
    ]
    stable_name = releases._canonical_artifact(STABLE, "linux", "x86_64")
    assert (releases._store_dir() / "stable" / STABLE / stable_name).is_file()
    downloaded = admin.get(f"/api/releases/artifact/stable/{STABLE}/{stable_name}")
    assert downloaded.status_code == 200
    assert downloaded.content


def test_prune_route_is_admin_only(pg, ci: TestClient, admin: TestClient) -> None:
    assert TestClient(app_mod.app).post("/api/admin/releases/prune").status_code in (
        401, 403,
    )
    response = admin.post("/api/admin/releases/prune?dry_run=true")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "dry_run": True,
        "keep_beta_versions": 3,
        "pruned": [],
        "deleted_files": 0,
        "freed_bytes": 0,
        "incoming_orphans": [],
        "incoming_freed_bytes": 0,
        "errors": [],
    }


def test_retention_sweeps_stale_incoming_orphans_only(
    pg, ci: TestClient, ci_key
) -> None:
    """A 422 or a cancelled run leaves bytes in incoming/ that nothing ever
    comes back for. That was a second unbounded pile with nothing watching it."""
    import os
    import time as _time

    incoming = releases._incoming_dir()
    incoming.mkdir(parents=True, exist_ok=True)
    stale = incoming / "Chudware-0.1.0-beta.9-linux-x86_64.AppImage"
    stale.write_bytes(b"abandoned artifact" * 512)
    part = incoming / "Chudware-Setup-0.1.0-beta.9-x86_64.exe.part"
    part.write_bytes(b"truncated chunk upload" * 256)
    old = _time.time() - releases.INCOMING_ORPHAN_AGE_SECONDS - 60
    for path in (stale, part):
        os.utime(path, (old, old))
    # An upload in flight: same shape, minutes old rather than a day.
    fresh = incoming / "Chudware-0.1.0-beta.10-linux-x86_64.AppImage"
    fresh.write_bytes(b"upload in flight" * 64)

    report = releases.prune_release_store(dry_run=True)
    assert sorted(e["name"] for e in report["incoming_orphans"]) == sorted(
        [stale.name, part.name]
    )
    assert report["incoming_freed_bytes"] == stale.stat().st_size + part.stat().st_size
    assert stale.is_file() and part.is_file(), "a dry run must delete nothing"

    real = releases.prune_release_store()
    assert real["incoming_freed_bytes"] == report["incoming_freed_bytes"]
    assert real["errors"] == []
    assert not stale.exists() and not part.exists()
    assert fresh.is_file(), "an upload in flight must survive"

    # Idempotent.
    assert releases.prune_release_store()["incoming_orphans"] == []


def test_retention_keeps_a_stale_incoming_file_that_a_release_row_accounts_for(
    pg, ci: TestClient, ci_key
) -> None:
    """A published artifact's name is not an orphan even when a copy lingers:
    the sweep is for bytes NOTHING accounts for, and deleting a duplicate of a
    live release on a name match is a rule with no upside and a real downside."""
    assert _publish(ci, ci_key).status_code == 200
    import os
    import time as _time

    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    incoming = releases._incoming_dir()
    lingering = incoming / name
    lingering.write_bytes(b"a copy of a published artifact")
    sidecar = incoming / (name + ".payload-manifest.v1.json")
    sidecar.write_bytes(b"a copy of a published sidecar")
    old = _time.time() - releases.INCOMING_ORPHAN_AGE_SECONDS - 60
    for path in (lingering, sidecar):
        os.utime(path, (old, old))

    report = releases.prune_release_store()
    assert report["incoming_orphans"] == []
    assert lingering.is_file() and sidecar.is_file()

@pytest.mark.parametrize("kind", ["payload", "sbom", "provenance", "platform_test"])
def test_sidecar_hashes_do_not_substitute_for_authenticity(pg, ci, ci_key, kind):
    files = _stage()
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    suffix = release_evidence._SIDECARS[kind][1]
    files[name + suffix] = base64.b64encode(b"untrusted-signature".ljust(64, b"!")) + b"\n"
    (releases._incoming_dir() / (name + suffix)).write_bytes(files[name + suffix])
    response = ci.post("/api/releases/ci/publish", json=_body(ci_key, files))
    assert response.status_code == 422
    with db.tx() as cur:
        cur.execute("SELECT artifact_key FROM releases")
        assert cur.fetchone() is None
    assert not releases._store_path(f"beta/{BETA}/{name}").exists()


@pytest.mark.parametrize("kind,mutation", [
    ("payload", "version"), ("provenance", "subject"), ("provenance", "commit"),
    ("platform_test", "failed"), ("platform_test", "commands"),
    ("platform_test", "digest"), ("sbom", "component_version"),
    ("sbom", "input_digest"), ("sbom", "malformed_metadata"),
])
def test_signed_evidence_must_describe_this_successful_build(pg, ci, ci_key, kind, mutation):
    files = _stage()
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    document_suffix, signature_suffix = release_evidence._SIDECARS[kind]
    document = json.loads(files[name + document_suffix])
    if mutation == "version":
        document["version"] = "9.9.9"
    elif mutation == "subject":
        document["subject"][0]["digest"]["sha256"] = "e" * 64
    elif mutation == "commit":
        document["predicate"]["buildDefinition"]["externalParameters"]["commit"] = "e" * 40
    elif mutation == "failed":
        document["passed"] = False
    elif mutation == "commands":
        document["commands"] = ["ctest core"]
    elif mutation == "digest":
        document["artifact_sha256"] = "e" * 64
    elif mutation == "component_version":
        document["metadata"]["component"]["version"] = "9.9.9"
    elif mutation == "malformed_metadata":
        document["metadata"] = []
    else:
        document["components"][0]["hashes"][0]["content"] = "e" * 64
    if kind == "payload":
        raw = release_evidence._payload.canonical_manifest_bytes(document)
        preimage = release_evidence._payload.payload_signature_preimage(releases._CI_KEY_ID, raw)
    else:
        raw = release_evidence._json.canonical_json_bytes(document)
        preimage = release_evidence._DOMAINS[kind] + raw
    files[name + document_suffix] = raw
    files[name + signature_suffix] = base64.b64encode(ci_key.sign(preimage)) + b"\n"
    for suffix in (document_suffix, signature_suffix):
        (releases._incoming_dir() / (name + suffix)).write_bytes(files[name + suffix])
    response = ci.post("/api/releases/ci/publish", json=_body(ci_key, files))
    assert response.status_code == 422
    with db.tx() as cur:
        cur.execute("SELECT artifact_key FROM releases")
        assert cur.fetchone() is None


def test_publication_uses_checked_snapshot_when_incoming_files_change(pg, ci, ci_key, monkeypatch):
    files = _stage()
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    verify = releases.verify_bundle
    def replace_incoming_after_verification(artifact, *args, **kwargs):
        result = verify(artifact, *args, **kwargs)
        for filename in files:
            (releases._incoming_dir() / filename).write_bytes(b"concurrent substituted bytes")
        return result
    monkeypatch.setattr(releases, "verify_bundle", replace_incoming_after_verification)
    response = ci.post("/api/releases/ci/publish", json=_body(ci_key, files))
    assert response.status_code == 200
    stored = releases._store_path(response.json()["artifact_key"])
    for filename, expected in files.items():
        assert stored.with_name(filename).read_bytes() == expected


def test_promotion_rechecks_stored_sidecar_signature_even_with_new_database_digest(pg, ci, ci_key, admin):
    assert _publish(ci, ci_key).status_code == 200
    name = releases._canonical_artifact(BETA, "linux", "x86_64")
    key = f"beta/{BETA}/{name}"
    signature = releases._store_path(key + ".provenance.slsa.json.sig")
    signature.write_bytes(base64.b64encode(b"forged".ljust(64, b"!")) + b"\n")
    with db.tx() as cur:
        cur.execute("UPDATE releases SET provenance_signature_sha256=%s WHERE artifact_key=%s",
                    (hashlib.sha256(signature.read_bytes()).hexdigest(), key))
    response = admin.post("/api/admin/releases/promote",
                          json={"version": BETA, "platform": "linux", "arch": "x86_64"})
    assert response.status_code == 422
    with db.tx() as cur:
        cur.execute("SELECT artifact_key FROM releases WHERE channel='stable'")
        assert cur.fetchone() is None
    assert not releases._store_path(f"stable/{STABLE}/" + releases._canonical_artifact(STABLE, "linux", "x86_64")).exists()


def _manual_publication(private, version, floor):
    files = _stage(version)
    name = releases._canonical_artifact(version, "linux", "x86_64")
    body = _body(private, files, version=version, stable_version=version)
    evidence = body.pop("evidence")
    body.pop("stable_version")
    body.pop("stable_signature")
    body.update({"channel": "stable", "min_supported": floor,
                 "artifact_key": f"stable/{version}/{name}", **evidence})
    for prefix, filename in releases._EVIDENCE_SIDECARS:
        body[prefix + "_key"] = body["artifact_key"] + "." + filename
        body[prefix + "_signature_key"] = body[prefix + "_key"] + ".sig"
    body.update({
        "payload_manifest_key": body["artifact_key"] + releases._PAYLOAD_SIDECAR_SUFFIXES[0],
        "payload_signature_key": body["artifact_key"] + releases._PAYLOAD_SIDECAR_SUFFIXES[1],
        "payload_manifest_sha256": hashlib.sha256(files[name + releases._PAYLOAD_SIDECAR_SUFFIXES[0]]).hexdigest(),
        "payload_signature_sha256": hashlib.sha256(files[name + releases._PAYLOAD_SIDECAR_SUFFIXES[1]]).hexdigest(),
    })
    destination = releases._store_path(body["artifact_key"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    for filename, raw in files.items():
        destination.with_name(filename).write_bytes(raw)
    return body, files


def test_manual_publication_authenticates_bytes_and_numeric_support_floor(pg, ci, ci_key):
    first, _ = _manual_publication(ci_key, "2.20.0", "2.9.0")
    anonymous = TestClient(app_mod.app)
    assert anonymous.post("/api/releases/latest/publish", json=first).status_code == 401
    assert ci.post("/api/releases/latest/publish", json=first).status_code == 200
    second, _ = _manual_publication(ci_key, "2.20.1", "2.10.0")
    assert ci.post("/api/releases/latest/publish", json=second).status_code == 200
    older_floor, _ = _manual_publication(ci_key, "2.20.2", "2.9.99")
    assert ci.post("/api/releases/latest/publish", json=older_floor).status_code == 409
    with db.tx() as cur:
        cur.execute("SELECT min_supported FROM release_channels WHERE channel='stable'")
        assert cur.fetchone()["min_supported"] == "2.10.0"
        cur.execute("SELECT version FROM releases WHERE channel='stable' ORDER BY version")
        assert [row["version"] for row in cur.fetchall()] == ["2.20.0", "2.20.1"]


def test_manual_publication_rejects_hash_matching_unsigned_sidecars(pg, ci, ci_key):
    body, _ = _manual_publication(ci_key, "2.20.0", "1.0.0")
    signature = releases._store_path(body["payload_signature_key"])
    signature.write_bytes(base64.b64encode(b"untrusted".ljust(64, b"!")) + b"\n")
    body["payload_signature_sha256"] = hashlib.sha256(signature.read_bytes()).hexdigest()
    assert ci.post("/api/releases/latest/publish", json=body).status_code == 422
    with db.tx() as cur:
        cur.execute("SELECT artifact_key FROM releases")
        assert cur.fetchone() is None
