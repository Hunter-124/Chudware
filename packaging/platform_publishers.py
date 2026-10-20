"""Offline-owner-authorized native publisher policy and artifact verification."""
from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import zipfile

try:
    from packaging.release_json import parse_canonical_json_bytes
except ImportError:
    from release_json import parse_canonical_json_bytes

# The fixed offline OWNER root, identical to core/security/release_keys.h's
# release-v1. Neither the CI root nor a supplied payload key authorizes policy.
_OWNER_PUBLIC_KEY = bytes.fromhex(
    "29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7"
)
POLICY_DOMAIN = b"chudware-platform-publishers-v1\0"
BUNDLE_ID = "com.chudware.Chudware"
SIDECAR_LIMIT = 16 * 1024**2
ARTIFACT_LIMIT = 2 * 1024**3
NATIVE_OUTPUT_LIMIT = 1024**2
NATIVE_TIMEOUT = 120


class PublisherPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class PublisherPolicy:
    raw: bytes
    signature: bytes
    sha256: str
    certificate_sha256: tuple[str, ...]
    team_ids: tuple[str, ...]

def open_regular(path: Path):
    if path.is_symlink():
        raise PublisherPolicyError("verification input must not be a symlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise PublisherPolicyError("verification input must be a regular file")
    return os.fdopen(descriptor, "rb")


def read_bounded(path: Path, limit: int = SIDECAR_LIMIT) -> bytes:
    with open_regular(path) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise PublisherPolicyError("verification input exceeds the byte limit")
    return raw


def run_native(command: list[str], *, check: bool = True, stdout=None, stderr=None,
               text: bool = False) -> subprocess.CompletedProcess:
    """Run the existing native verifiers with bounded output, never truncating proof."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=os.name != "nt")
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    read_error = threading.Event()
    lock = threading.Lock()
    total = 0

    def stop() -> None:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass

    def consume(stream, index: int, retain: bool) -> None:
        nonlocal total
        try:
            while chunk := stream.read(65536):
                with lock:
                    total += len(chunk)
                    if total > NATIVE_OUTPUT_LIMIT:
                        overflow.set()
                        stop()
                        return
                    if retain:
                        buffers[index].extend(chunk)
        except OSError:
            read_error.set()
            stop()
        finally:
            stream.close()

    readers = [
        threading.Thread(target=consume, args=(process.stdout, 0, stdout == subprocess.PIPE), daemon=True),
        threading.Thread(target=consume, args=(process.stderr, 1, stderr == subprocess.PIPE), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=NATIVE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        stop()
        process.wait()
        raise PublisherPolicyError("native verifier timed out") from exc
    finally:
        for reader in readers:
            reader.join(timeout=1)
    if overflow.is_set():
        raise PublisherPolicyError("native verifier output exceeds the byte limit")
    if read_error.is_set():
        raise PublisherPolicyError("native verifier output could not be read")
    if any(reader.is_alive() for reader in readers):
        stop()
        raise PublisherPolicyError("native verifier left an open output stream")
    values = [bytes(value).decode("utf-8", "strict") if text else bytes(value) for value in buffers]
    result = subprocess.CompletedProcess(command, returncode,
        values[0] if stdout == subprocess.PIPE else None,
        values[1] if stderr == subprocess.PIPE else None)
    if check:
        result.check_returncode()
    return result


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with open_regular(path) as stream:
        total = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > ARTIFACT_LIMIT:
                raise PublisherPolicyError("artifact exceeds the release byte limit")
            value.update(chunk)
    return value.hexdigest()


def validate_policy_document(raw: bytes) -> dict:
    document = parse_canonical_json_bytes(raw, "platform publisher policy", PublisherPolicyError)
    if (set(document) != {"schema", "key_id", "windows", "macos"}
            or document.get("schema") != "chudware-platform-publishers-v1"
            or document.get("key_id") != "release-v1"):
        raise PublisherPolicyError("platform publisher policy requires the offline release-v1 owner")
    windows = document["windows"]
    macos = document["macos"]
    if (not isinstance(windows, dict) or set(windows) != {"certificate_sha256"}
            or not isinstance(macos, dict) or set(macos) != {"team_ids", "bundle_id"}
            or macos["bundle_id"] != BUNDLE_ID):
        raise PublisherPolicyError("platform publisher policy has invalid publisher fields")
    certificates = windows["certificate_sha256"]
    teams = macos["team_ids"]
    for values, pattern in ((certificates, r"[0-9a-f]{64}"), (teams, r"[A-Z0-9]{10}")):
        if (not isinstance(values, list)
                or not all(isinstance(value, str) and re.fullmatch(pattern, value) for value in values)
                or len(set(values)) != len(values)):
            raise PublisherPolicyError("platform publisher policy has invalid publisher allowlists")
    return document


def require_owner_signing_key(key) -> None:
    from cryptography.hazmat.primitives import serialization
    public_key = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if public_key != _OWNER_PUBLIC_KEY:
        raise PublisherPolicyError("only the offline release-v1 owner key may authorize publishers")


def _parse_policy(raw: bytes, signature_raw: bytes) -> PublisherPolicy:
    document = validate_policy_document(raw)
    encoded = signature_raw.strip()
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PublisherPolicyError("publisher policy signature is not canonical base64") from exc
    if len(signature) != 64 or encoded != base64.b64encode(signature):
        raise PublisherPolicyError("publisher policy signature is not a canonical Ed25519 signature")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise PublisherPolicyError("cryptography is required to authenticate publisher policy") from exc
    try:
        Ed25519PublicKey.from_public_bytes(_OWNER_PUBLIC_KEY).verify(signature, POLICY_DOMAIN + raw)
    except InvalidSignature as exc:
        raise PublisherPolicyError("publisher policy is not authorized by the offline owner") from exc
    return PublisherPolicy(raw, signature_raw, hashlib.sha256(raw).hexdigest(),
                           tuple(document["windows"]["certificate_sha256"]), tuple(document["macos"]["team_ids"]))


def load_policy(policy_path: Path, signature_path: Path) -> PublisherPolicy:
    """Authenticate exact policy bytes with the fixed owner root, without native tools."""
    return _parse_policy(read_bounded(policy_path), read_bounded(signature_path))


def _snapshot(source: Path, destination: Path) -> str:
    before = _digest(source)
    with open_regular(source) as reader, destination.open("xb") as writer:
        total = 0
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            total += len(chunk)
            if total > ARTIFACT_LIMIT:
                raise PublisherPolicyError("artifact exceeds the release byte limit")
            writer.write(chunk)
    if _digest(destination) != before or _digest(source) != before:
        raise PublisherPolicyError("artifact changed while creating the private verification snapshot")
    destination.chmod(0o400)
    return before


def _windows_powershell_executable() -> str:
    # Do not let a workspace/PATH executable impersonate the OS certificate
    # inspector. The trusted native provider lives in the actual system directory.
    import ctypes
    from ctypes import wintypes
    try:
        getattr(ctypes.WinDLL("ntdll.dll"), "wine_get_version")
    except AttributeError:
        pass
    else:
        raise PublisherPolicyError("Wine cannot authorize native Authenticode qualification")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    get_system_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_system_directory(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise PublisherPolicyError("cannot locate the Windows system certificate inspector")
    return str(Path(buffer.value) / "WindowsPowerShell/v1.0/powershell.exe")


def _mac_tool(name: str) -> str:
    return "/usr/sbin/spctl" if name == "spctl" else f"/usr/bin/{name}"


def _windows_publisher(artifact: Path, policy: PublisherPolicy) -> dict[str, str]:
    if sys.platform != "win32":
        raise PublisherPolicyError("Authenticode verification requires a Windows host")
    if not policy.certificate_sha256:
        raise PublisherPolicyError("publisher policy authorizes no Windows certificates")
    from services.installer.build_release import find_tool
    program_files = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    signtool = find_tool(
        "SIGNTOOL", "signtool.exe",
        tuple(program_files.glob("Windows Kits/10/bin/*/x64/signtool.exe")),
    )
    if not signtool:
        raise PublisherPolicyError("Authenticode verification requires signtool.exe or SIGNTOOL")
    run_native([signtool, "verify", "/pa", "/v", str(artifact)], check=True)
    # Obtain the actual embedded signer certificate from the OS, not its display
    # name, the signing selector, or text printed by SigTool.
    literal = "'" + str(artifact).replace("'", "''") + "'"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -lt 1) "
        "{ throw 'Windows PowerShell 5.1 is required' }; "
        "$signature = Get-AuthenticodeSignature -LiteralPath " + literal + "; "
        "if ($signature.Status -ne 'Valid' -or $signature.SignatureType -ne 'Authenticode' "
        "-or -not $signature.SignerCertificate) { throw 'invalid embedded Authenticode signer' }; "
        "[Convert]::ToBase64String($signature.SignerCertificate.RawData)"
    )
    result = run_native([_windows_powershell_executable(), "-NoProfile", "-NonInteractive", "-Command", script],
                        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        certificate = base64.b64decode(result.stdout.strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PublisherPolicyError("native Authenticode signer certificate is invalid") from exc
    if not certificate:
        raise PublisherPolicyError("native Authenticode signer certificate is missing")
    fingerprint = hashlib.sha256(certificate).hexdigest()
    if fingerprint not in policy.certificate_sha256:
        raise PublisherPolicyError("Authenticode signer is not an owner-authorized Chudware publisher")
    return {"certificate_sha256": fingerprint}


def _extract_mac_app(artifact: Path, destination: Path) -> Path:
    with zipfile.ZipFile(artifact) as archive:
        links = set()
        names = set()
        if len(archive.infolist()) > 100000 or sum(entry.file_size for entry in archive.infolist()) > 4 * 1024**3:
            raise PublisherPolicyError("platform archive exceeds the extraction limit")
        for entry in archive.infolist():
            # ZIP member names are POSIX paths even when inspection runs on Windows.
            path = PurePosixPath(entry.filename)
            if path in names:
                raise PublisherPolicyError("platform archive contains a duplicate path")
            names.add(path)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                raise PublisherPolicyError("platform archive contains an unsafe path")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                if entry.file_size > 4096:
                    raise PublisherPolicyError("platform archive link exceeds the byte limit")
                target = PurePosixPath(archive.read(entry).decode("utf-8"))
                if target.is_absolute():
                    raise PublisherPolicyError("platform archive contains an unsafe link")
                resolved = (destination / path.parent / target).resolve()
                if not resolved.is_relative_to(destination):
                    raise PublisherPolicyError("platform archive contains an unsafe link")
                links.add(path)
        for entry in archive.infolist():
            if any(parent in links for parent in PurePosixPath(entry.filename).parents):
                raise PublisherPolicyError("platform archive writes through a link")
    run_native([_mac_tool("ditto"), "-x", "-k", str(artifact), str(destination)], check=True)
    for path in destination.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(destination):
            raise PublisherPolicyError("platform archive contains an unsafe link chain")
    app = destination / "Chudware.app"
    if not app.is_dir() or set(destination.iterdir()) != {app}:
        raise PublisherPolicyError("platform archive must contain only Chudware.app")
    return app


def _macos_publisher(artifact: Path, policy: PublisherPolicy, destination: Path) -> dict[str, str]:
    if sys.platform != "darwin":
        raise PublisherPolicyError("notarization verification requires a macOS host")
    if not policy.team_ids:
        raise PublisherPolicyError("publisher policy authorizes no macOS teams")
    app = _extract_mac_app(artifact, destination)
    for command in (
        [_mac_tool("codesign"), "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        [_mac_tool("xcrun"), "stapler", "validate", str(app)],
        [_mac_tool("spctl"), "--assess", "--type", "execute", "--verbose=2", str(app)],
    ):
        run_native(command, check=True)
    display = run_native([_mac_tool("codesign"), "--display", "--verbose=4", str(app)], check=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    fields = {}
    for line in (display.stdout + "\n" + display.stderr).splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("TeamIdentifier", "Identifier"):
            if key in fields:
                raise PublisherPolicyError("native codesign publisher identity is ambiguous")
            fields[key] = value
    if fields.get("TeamIdentifier") not in policy.team_ids or fields.get("Identifier") != BUNDLE_ID:
        raise PublisherPolicyError("codesign signer is not an owner-authorized Chudware publisher")
    info = plistlib.loads(read_bounded(app / "Contents/Info.plist"))
    if not isinstance(info, dict) or info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise PublisherPolicyError("sealed app Info.plist has the wrong Chudware bundle identifier")
    return {"team_id": fields["TeamIdentifier"], "bundle_id": BUNDLE_ID}


def verify_artifact(artifact: Path, platform: str, policy_path: Path, signature_path: Path) -> dict:
    """Verify private immutable snapshots and bind the actual signer to owner policy."""
    with tempfile.TemporaryDirectory(prefix="chudware-publisher-verify-") as temporary:
        root = Path(temporary)
        policy_snapshot = root / "platform-publishers.v1.json"
        signature_snapshot = root / "platform-publishers.v1.sig"
        policy_raw = read_bounded(policy_path)
        signature_raw = read_bounded(signature_path)
        policy_snapshot.write_bytes(policy_raw)
        signature_snapshot.write_bytes(signature_raw)
        policy_snapshot.chmod(0o400)
        signature_snapshot.chmod(0o400)
        artifact_root = root / "artifact"
        artifact_root.mkdir()
        snapshot = artifact_root / artifact.name
        protected = [policy_snapshot, signature_snapshot, snapshot]
        try:
            # Only authenticated offline-owner policy reaches a native tool.
            policy = load_policy(policy_snapshot, signature_snapshot)
            artifact_sha256 = _snapshot(artifact, snapshot)
            if platform == "windows":
                publisher = _windows_publisher(snapshot, policy)
            elif platform == "macos":
                destination = root / "expanded"
                destination.mkdir()
                publisher = _macos_publisher(snapshot, policy, destination)
            else:
                raise PublisherPolicyError("unsupported native verification platform")
            if (_digest(snapshot) != artifact_sha256 or _digest(artifact) != artifact_sha256
                    or read_bounded(policy_snapshot) != policy_raw
                    or read_bounded(signature_snapshot) != signature_raw
                    or read_bounded(policy_path) != policy_raw
                    or read_bounded(signature_path) != signature_raw):
                raise PublisherPolicyError("artifact or publisher policy changed during native verification")
            return {"artifact_sha256": artifact_sha256, "publisher_policy_sha256": policy.sha256,
                    "publisher": publisher}
        except (zipfile.BadZipFile, UnicodeError, plistlib.InvalidFileException, RuntimeError) as exc:
            raise PublisherPolicyError(f"native publisher verification failed: {exc}") from exc
        finally:
            # Windows cannot remove a read-only temporary file. Restore write
            # permission only after every native command and integrity check ends.
            for path in protected:
                if path.exists():
                    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path, help="canonical owner-reviewed publisher allowlist")
    parser.add_argument("--out", type=Path, required=True, help="new owner signing request")
    args = parser.parse_args(argv)
    try:
        raw = read_bounded(args.policy)
        validate_policy_document(raw)
        with args.out.open("xb") as stream:
            stream.write(POLICY_DOMAIN + raw)
    except (OSError, PublisherPolicyError) as exc:
        print(f"platform_publishers: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
