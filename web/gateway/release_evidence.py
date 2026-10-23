"""Authenticate retained release bytes before publication or promotion."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parents[2]


def _load_contract(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / relative)
    if spec is None or spec.loader is None:
        raise ImportError(f"release contract unavailable: {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Same explicit source-contract loading convention as admin_billing. Do not import
# the unrelated installed PyPI `packaging` package as the release implementation.
_json = _load_contract("_chudware_release_json", "packaging/release_json.py")
_payload = _load_contract("_chudware_payload_manifest", "services/installer/payload_manifest.py")
_contract_path = str(_REPO / "packaging")
sys.path.insert(0, _contract_path)
try:
    _publishers = _load_contract("_chudware_platform_publishers", "packaging/platform_publishers.py")
finally:
    sys.path.remove(_contract_path)
_SHA = re.compile(r"[0-9a-f]{64}")
_SIDECARS = {
    "payload": (".payload-manifest.v1.json", ".payload-manifest.v1.sig"),
    "sbom": (".sbom.cdx.json", ".sbom.cdx.json.sig"),
    "provenance": (".provenance.slsa.json", ".provenance.slsa.json.sig"),
    "platform_test": (".platform-test.v1.json", ".platform-test.v1.json.sig"),
}
_DOMAINS = {
    "sbom": b"chudware-sbom-v1\0",
    "provenance": b"chudware-provenance-v1\0",
    "platform_test": b"chudware-platform-test-v1\0",
}
_ARTIFACT_LIMIT = 2 * 1024**3
_SIDECAR_LIMIT = 16 * 1024**2


def _reject(detail: str) -> None:
    raise HTTPException(422, detail)


def _signature(raw: bytes) -> bytes:
    # Producers append one LF. Alternate whitespace/encodings are not signatures.
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError:
        _reject("release sidecar signature is not canonical base64")
    if len(decoded) != 64 or base64.b64encode(decoded) != encoded:
        _reject("release sidecar signature must be canonical Ed25519")
    return decoded


def _verify(public: bytes, signature: bytes, preimage: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, preimage)
    except (ValueError, InvalidSignature):
        _reject("release sidecar signature does not authenticate its bytes")


def _copy_input(source: Path, destination: Path, limit: int) -> None:
    try:
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as input_file:
            info = os.fstat(input_file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                _reject("release input is unsafe or exceeds its byte limit")
            total = 0
            with destination.open("xb") as output:
                while block := input_file.read(1024 * 1024):
                    total += len(block)
                    if total > limit:
                        _reject("release input exceeds its byte limit")
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
    except OSError as exc:
        raise HTTPException(422, "release input is missing or unsafe") from exc


@contextmanager
def capture_bundle(source: Path, *, evidence: bool = True) -> Iterator[Path]:
    """Validate/publish the same owner-private bytes, never a mutable input path."""
    with tempfile.TemporaryDirectory(prefix="chudware-publication-") as directory:
        captured = Path(directory) / source.name
        _copy_input(source, captured, _ARTIFACT_LIMIT)
        kinds = _SIDECARS if evidence else {"payload": _SIDECARS["payload"]}
        for pair in kinds.values():
            for suffix in pair:
                _copy_input(source.with_name(source.name + suffix),
                            captured.with_name(source.name + suffix), _SIDECAR_LIMIT)
        yield captured


def verify_bundle(artifact: Path, identity: dict[str, Any], public_keys: dict[str, bytes],
                  *, evidence: bool = True) -> dict[str, dict[str, Any]]:
    key_id = identity["key_id"]
    public = public_keys.get(key_id)
    if public is None:
        _reject("unknown release evidence key_id")
    with artifact.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != identity["sha256"] or artifact.stat().st_size != identity["size"]:
        _reject("release artifact bytes do not match the authenticated identity")
    parsed: dict[str, dict[str, Any]] = {}
    kinds = _SIDECARS if evidence else {"payload": _SIDECARS["payload"]}
    for kind, (document_suffix, signature_suffix) in kinds.items():
        raw = artifact.with_name(artifact.name + document_suffix).read_bytes()
        signature_raw = artifact.with_name(artifact.name + signature_suffix).read_bytes()
        prefix = "payload_manifest" if kind == "payload" else kind
        signature_prefix = "payload_signature" if kind == "payload" else kind + "_signature"
        for field, body in ((prefix + "_sha256", raw),
                            (signature_prefix + "_sha256", signature_raw)):
            expected = identity.get(field)
            if expected is not None and hashlib.sha256(body).hexdigest() != expected:
                _reject(f"{field} does not match retained bytes")
        signature = _signature(signature_raw)
        try:
            if kind == "payload":
                _verify(public, signature, _payload.payload_signature_preimage(key_id, raw))
                document = json.loads(raw.decode("utf-8"))
                if not isinstance(document, dict) or _payload.canonical_manifest_bytes(document) != raw:
                    _reject("release payload manifest is not canonical")
            else:
                _verify(public, signature, _DOMAINS[kind] + raw)
                document = _json.parse_canonical_json_bytes(raw, kind)
        except (ValueError, RecursionError, _payload.PayloadManifestError) as exc:
            raise HTTPException(422, f"invalid canonical {kind} release sidecar") from exc
        parsed[kind] = document
    payload = parsed["payload"]
    if any(payload.get(field) != identity[field] for field in ("key_id", "version", "platform", "arch")):
        _reject("payload manifest release identity does not match the artifact")
    if not payload["files"]:
        _reject("payload manifest must describe the shipped closure")
    if not evidence:
        return parsed
    test = parsed["platform_test"]
    if (set(test) != {"schema", "version", "platform", "arch", "commit", "artifact_sha256", "commands", "passed"}
            or test.get("schema") != "platform-test.v1" or test.get("passed") is not True
            or test.get("commands") != ["ctest core", "ctest host"]
            or test.get("artifact_sha256") != digest
            or any(test.get(field) != identity[field] for field in ("version", "platform", "arch"))
            or test.get("commit") != identity["commit_sha"]):
        _reject("platform tests do not authenticate this successful release build")
    sbom = parsed["sbom"]
    metadata = sbom.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if (sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.5"
            or not isinstance(component, dict)
            or component.get("type") != "application" or component.get("name") != "Chudware"
            or component.get("version") != identity["version"]):
        _reject("SBOM does not describe this Chudware release")
    components = sbom.get("components")
    if not isinstance(components, list):
        _reject("SBOM locked inputs are missing")
    locked = {}
    for component in components:
        if not isinstance(component, dict) or component.get("type") != "file":
            _reject("SBOM locked input has an invalid schema")
        name, hashes = component.get("name"), component.get("hashes")
        if (not isinstance(name, str) or not name or name in locked
                or not isinstance(hashes, list) or len(hashes) != 1
                or not isinstance(hashes[0], dict) or hashes[0].get("alg") != "SHA-256"
                or not isinstance(hashes[0].get("content"), str)
                or _SHA.fullmatch(hashes[0]["content"]) is None):
            _reject("SBOM locked input identity is invalid or ambiguous")
        locked[name] = hashes[0]["content"]
    for name in ("test-results.json", "core-junit.xml", "host-junit.xml", "core-ctest.log", "host-ctest.log"):
        if "retained-test-results/" + name not in locked:
            _reject("SBOM omits retained executed native test inputs")
    provenance = parsed["provenance"]
    predicate = provenance.get("predicate")
    definition = predicate.get("buildDefinition") if isinstance(predicate, dict) else None
    if not isinstance(definition, dict):
        _reject("provenance build definition is missing")
    expected_parameters = {"version": identity["version"], "platform": identity["platform"],
                           "arch": identity["arch"], "commit": identity["commit_sha"]}
    expected_subject = [{"name": artifact.name, "digest": {"sha256": digest}}]
    if (provenance.get("_type") != "https://in-toto.io/Statement/v1"
            or provenance.get("predicateType") != "https://slsa.dev/provenance/v1"
            or provenance.get("subject") != expected_subject
            or definition.get("buildType") != "https://chudware.com/build/platform/v1"
            or definition.get("externalParameters") != expected_parameters):
        _reject("provenance does not authenticate this artifact and source identity")
    dependencies = definition.get("resolvedDependencies")
    expected_dependencies = [{"uri": name, "digest": {"sha256": value}} for name, value in locked.items()]
    if dependencies != expected_dependencies:
        _reject("provenance and SBOM locked inputs disagree")
    return parsed


def native_publisher_verified(parsed: dict[str, dict[str, Any]], identity: dict[str, Any],
                              public_keys: dict[str, bytes], *, required: bool = False) -> bool:
    platform = identity["platform"]
    if platform not in {"windows", "macos"}:
        return False
    provenance = parsed.get("provenance", {})
    predicate = provenance.get("predicate")
    run = predicate.get("runDetails") if isinstance(predicate, dict) else None
    byproducts = run.get("byproducts", []) if isinstance(run, dict) else []
    descriptors = ([item for item in byproducts if isinstance(item, dict)
                    and item.get("name") == "platform-signing.v1.json"]
                   if isinstance(byproducts, list) else [])
    if not descriptors:
        if required:
            _reject("stable native release requires owner-authorized publisher verification")
        return False
    if len(descriptors) != 1:
        _reject("native publisher attestation is ambiguous")
    descriptor = descriptors[0]
    annotations = descriptor.get("annotations")
    report = annotations.get("report") if isinstance(annotations, dict) else None
    if not isinstance(report, dict):
        _reject("native publisher attestation report is missing")
    raw_report = _json.canonical_json_bytes(report)
    if (set(descriptor) != {"name", "digest", "annotations"}
            or descriptor["digest"] != {"sha256": hashlib.sha256(raw_report).hexdigest()}
            or report.get("schema") != "platform-signing.v1"
            or report.get("platform") != platform
            or report.get("kind") != ("authenticode" if platform == "windows" else "notarization")
            or report.get("artifact_sha256") != identity["sha256"]):
        _reject("native publisher attestation does not authenticate this artifact")
    common = {"schema", "platform", "kind", "artifact_sha256", "verified"}
    if report.get("verified") is False:
        if (set(report) != common | {"reason"} or set(annotations) != {"report"}
                or report.get("reason") != "owner_authorized_native_verification_not_supplied"):
            _reject("unverified native publisher attestation is malformed")
        if required:
            _reject("unsigned native beta cannot be promoted to stable")
        return False
    if (report.get("verified") is not True
            or set(report) != common | {"publisher_policy_sha256", "publisher"}
            or set(annotations) != {"report", "publisher_policy", "publisher_policy_signature"}):
        _reject("native publisher attestation is malformed")
    policy = annotations["publisher_policy"]
    if not isinstance(policy, dict):
        _reject("native publisher authorization policy is missing")
    raw_policy = _json.canonical_json_bytes(policy)
    try:
        _publishers.validate_policy_document(raw_policy)
    except _publishers.PublisherPolicyError as exc:
        raise HTTPException(422, "native publisher authorization policy is invalid") from exc
    signature = annotations["publisher_policy_signature"]
    if not isinstance(signature, str):
        _reject("native publisher authorization signature is missing")
    _verify(public_keys["release-v1"], _signature(signature.encode("ascii")),
            _publishers.POLICY_DOMAIN + raw_policy)
    if report["publisher_policy_sha256"] != hashlib.sha256(raw_policy).hexdigest():
        _reject("native publisher report and owner authorization policy disagree")
    publisher = report["publisher"]
    if not isinstance(publisher, dict):
        _reject("native artifact publisher is missing")
    if platform == "windows":
        permitted = (set(publisher) == {"certificate_sha256"}
                     and publisher["certificate_sha256"] in policy["windows"]["certificate_sha256"])
    else:
        permitted = (set(publisher) == {"team_id", "bundle_id"}
                     and publisher["team_id"] in policy["macos"]["team_ids"]
                     and publisher["bundle_id"] == _publishers.BUNDLE_ID)
    if not permitted:
        _reject("native artifact publisher is not authorized by the offline owner")
    return True
