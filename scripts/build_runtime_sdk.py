#!/usr/bin/env python3
"""Explicit offline SDK wheel production; never an installer/source fallback.

An operator-approved chudware-sdk-build-review-v1 manifest selects the exact Git
export archive, native source lock, separately trusted base executable/version,
setuptools==83.0.0 wheel, target context and SOURCE_DATE_EPOCH. The supplied review
digest selects operator-chosen bytes, not a signature or owner authorization.
Output remains an UNAPPROVED runtime wheel candidate requiring separate review.
"""
from __future__ import annotations

from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
import zipfile

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import prepare_runtime_wheels as preparation
from services.installer.wheelhouse import (
    WheelhouseError, _archive_files, _archive_members, _bytes, _copy_wheel,
    _keys, _packaging, _sha, _source_identity, install_command, local_path,
    verify_installed_runtime, verify_venv_interpreter,
)

BUILD_SCHEMA = "chudware-sdk-build-review-v1"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_PROCESS_OUTPUT = 2 * 1024 * 1024
SDK_FILENAME = "chudcode_rpc-0.3.1-py3-none-any.whl"
BACKEND_FILENAME = "setuptools-83.0.0-py3-none-any.whl"


def _manifest(args, context):
    raw = _bytes(args.manifest)
    if not _sha(args.review_sha256) or hashlib.sha256(raw).hexdigest() != args.review_sha256:
        raise WheelhouseError("SDK build review differs from externally supplied reviewed digest")
    value = preparation._document(raw)
    _keys(value, {"schema", "approval", "source_archive_sha256", "source_subdirectory", "chudcode_source",
                  "native_lock_sha256", "base_python", "backend_wheel", "target", "source_date_epoch"}, "SDK build review")
    if value["schema"] != BUILD_SCHEMA:
        raise WheelhouseError("invalid SDK build review schema")
    approval = value["approval"]
    _keys(approval, {"decision", "reference"}, "SDK build approval")
    reference = approval["reference"]
    if (approval["decision"] != "approved" or not isinstance(reference, str) or not reference.strip()
            or reference != reference.strip() or len(reference) > 512 or any(ord(char) < 32 for char in reference)):
        raise WheelhouseError("SDK source/toolchain require explicit operator review before any execution")
    for field in ("source_archive_sha256", "native_lock_sha256"):
        if not _sha(value[field]):
            raise WheelhouseError(f"invalid reviewed SDK {field}")
    if value["source_subdirectory"] != "python/chudcode-rpc":
        raise WheelhouseError("SDK source must be the exact exported python/chudcode-rpc subtree")
    source = value["chudcode_source"]
    _keys(source, {"repository", "repository_commit"}, "SDK immutable source")
    _source_identity({"kind": "vcs", **source}, True, source)
    if source["repository"] != "https://github.com/Hunter-124/chudcode.git":
        raise WheelhouseError("SDK build source must be the reviewed private Chudcode repository")
    base = value["base_python"]
    _keys(base, {"executable_sha256", "version", "identity_reference"}, "SDK build base")
    reference = base["identity_reference"]
    if (not _sha(base["executable_sha256"]) or not isinstance(reference, str) or not reference.strip()
            or reference != reference.strip() or len(reference) > 512 or any(ord(char) < 32 for char in reference)):
        raise WheelhouseError("SDK build requires operator-authenticated base/stdlib/loader identity reference")
    if sys.prefix != sys.base_prefix or (Path(sys.executable).absolute().parent.parent / "pyvenv.cfg").exists():
        raise WheelhouseError("SDK builder must run from the separately reviewed non-venv base Python")
    if (base["executable_sha256"] != preparation._digest(Path(sys._base_executable).resolve(strict=True))
            or base["version"] != ".".join(map(str, sys.version_info[:3]))):
        raise WheelhouseError("SDK build executable/version differs from operator-reviewed base")
    _keys(value["target"], {"environment", "tags"}, "SDK build target")
    if value["target"] != context:
        raise WheelhouseError("SDK build target differs from the exact operator-reviewed base context")
    epoch = value["source_date_epoch"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or not 315532800 <= epoch <= 4354819198:
        raise WheelhouseError("SDK build requires a fixed ZIP-representable SOURCE_DATE_EPOCH")
    backend = value["backend_wheel"]
    _keys(backend, preparation.WHEEL_FIELDS | {"source"}, "SDK backend wheel")
    if (backend["filename"] != BACKEND_FILENAME or backend["name"] != "setuptools"
            or backend["version"] != "83.0.0" or not _sha(backend["sha256"])):
        raise WheelhouseError("SDK build backend must be reviewed setuptools==83.0.0, matching the runtime pin")
    _source_identity(backend["source"], False, source)
    return value, raw


def _source_archive(archive_bytes: bytes, destination: Path, epoch: int) -> Path:
    """Extract bounded regular source files only, with normalized build timestamps."""
    import io
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        seen = set()
        expanded = 0
        for member in archive:
            expanded += member.size
            if len(seen) >= 4096 or expanded > MAX_SOURCE_BYTES:
                raise WheelhouseError("SDK source export exceeds file/expanded byte bounds")
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (not name or name in seen or "\\" in name or ":" in name
                    or any(part in ("", ".", "..") for part in parts)
                    or not (name == "python" or name == "python/chudcode-rpc" or name.startswith("python/chudcode-rpc/"))
                    or not (member.isdir() or member.isreg()) or member.size < 0 or member.size > 4 * 1024 * 1024):
                raise WheelhouseError("SDK source export contains duplicate/unsafe/nonregular members")
            seen.add(name)
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                stream = archive.extractfile(member)
                if stream is None:
                    raise WheelhouseError("SDK source member is unreadable")
                with stream:
                    content = stream.read(member.size + 1)
                if len(content) != member.size:
                    raise WheelhouseError("SDK source export member size differs")
                preparation._write(target, content)
                target.chmod(0o644)
                os.utime(target, (epoch, epoch))
    source = destination / "python/chudcode-rpc"
    project = tomllib.loads(_bytes(source / "pyproject.toml").decode("utf-8"))
    build = project.get("build-system", {})
    if (build != {"requires": ["setuptools>=77.0.3"], "build-backend": "setuptools.build_meta"}
            or project.get("project", {}).get("name") != "chudcode-rpc"
            or project.get("project", {}).get("version") != "0.3.1"
            or project.get("project", {}).get("dynamic")):
        raise WheelhouseError("exported SDK declarations differ from the reviewed static 0.3.1 setuptools project")
    if (source / "setup.py").exists() or (source / "setup.cfg").exists():
        raise WheelhouseError("SDK build refuses undeclared executable/alternate setup configuration")
    return source


def _backend_identity(path: Path, entry: dict, context: dict) -> dict:
    if preparation._digest(path) != entry["sha256"]:
        raise WheelhouseError("SDK backend wheel hash differs from explicit operator review")
    _, requirements, utils, _, specifiers, tags = _packaging()
    name, version, _, supported = utils.parse_wheel_filename(path.name)
    if not {str(tag) for tag in supported}.intersection(context["tags"]):
        raise WheelhouseError("SDK backend wheel does not support reviewed base target")
    with zipfile.ZipFile(path) as archive:
        members = _archive_members(archive, path.name)
        metadata_members = [member for member in members if member.endswith(".dist-info/METADATA") and member.count("/") == 1]
        if len(metadata_members) != 1:
            raise WheelhouseError("SDK backend wheel has ambiguous distribution metadata")
        metadata_path = metadata_members[0]
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        wheel = BytesParser().parsebytes(archive.read(metadata_path.rsplit("/", 1)[0] + "/WHEEL"))
        actual_tags = set()
        for tag in wheel.get_all("Tag", []):
            actual_tags.update(tags.parse_tag(tag))
        if (metadata.get_all("Name", []) != [entry["name"]] or metadata.get_all("Version", []) != [entry["version"]]
                or wheel.get_all("Root-Is-Purelib", []) != ["true"] or wheel.get_all("Wheel-Version", []) != ["1.0"]
                or actual_tags != supported):
            raise WheelhouseError("SDK backend wheel metadata/tags differ from reviewed identity")
        requires_python = metadata.get_all("Requires-Python", [])
        if len(requires_python) != 1 or not specifiers.SpecifierSet(requires_python[0]).contains(context["environment"]["python_full_version"]):
            raise WheelhouseError("SDK backend wheel Python compatibility differs from reviewed base")
        for raw in metadata.get_all("Requires-Dist", []):
            dependency = requirements.Requirement(raw)
            if not dependency.marker or dependency.marker.evaluate({**context["environment"], "extra": ""}):
                raise WheelhouseError("SDK backend has unreviewed mandatory dependency closure")
        files = _archive_files(archive, "true", path.name)
    return {"packages": {str(name): str(version)}, "hashes": {str(name): entry["sha256"]}, "files": {str(name): files}}


def _run(command, cwd: Path, environment: dict, timeout: int):
    """Bound subprocess output while leaving the caller's ambient environment out."""
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
    output = bytearray()
    overflow = threading.Event()
    def kill():
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    def collect():
        with process.stdout:
            while block := process.stdout.read(4096):
                if len(output) + len(block) > MAX_PROCESS_OUTPUT:
                    overflow.set()
                    kill()
                    break
                output.extend(block)
    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        result = process.wait(timeout=timeout)
        reader.join(timeout=5)
        if reader.is_alive():
            kill()
            raise WheelhouseError("SDK build subprocess retained an output pipe after exit")
        if overflow.is_set():
            raise WheelhouseError("SDK build subprocess exceeded bounded output")
        if result:
            raise WheelhouseError("SDK build subprocess failed: " + output.decode("utf-8", "replace"))
    except BaseException:
        kill()
        process.wait()
        raise
    finally:
        reader.join(timeout=5)


def build_sdk(args) -> dict:
    context = preparation.target_context()
    manifest, review_bytes = _manifest(args, context)
    archive_bytes = _bytes(args.source_archive, MAX_SOURCE_BYTES)
    if hashlib.sha256(archive_bytes).hexdigest() != manifest["source_archive_sha256"]:
        raise WheelhouseError("SDK source export hash differs from explicit operator review")
    native_bytes = _bytes(args.chudcode_lock)
    native = preparation._document(native_bytes)
    if (hashlib.sha256(native_bytes).hexdigest() != manifest["native_lock_sha256"]
            or manifest["chudcode_source"] != {key: native.get(key) for key in manifest["chudcode_source"]}):
        raise WheelhouseError("SDK reviewed source/native lock identity mismatch")
    output = preparation._output(args.output)
    backend_input = local_path(args.wheel_directory) / BACKEND_FILENAME
    # Everything above is reviewed before source/backend copying or process execution.
    with tempfile.TemporaryDirectory(prefix="chudware-reviewed-sdk-build-") as work:
        private = Path(work)
        backend = private / BACKEND_FILENAME
        _copy_wheel(backend_input, backend)
        identity = _backend_identity(backend, manifest["backend_wheel"], context)
        source = _source_archive(archive_bytes, private / "source", manifest["source_date_epoch"])
        home = private / "home"
        home.mkdir()
        environment = {"PATH": os.defpath, "HOME": str(home), "TMPDIR": str(private), "TEMP": str(private), "TMP": str(private),
                       "SOURCE_DATE_EPOCH": str(manifest["source_date_epoch"]), "PYTHONHASHSEED": "0",
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PIP_CONFIG_FILE": os.devnull}
        if os.name == "nt":
            environment["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
        venv = private / "backend-venv"
        _run([sys._base_executable, "-I", "-S", "-m", "venv", str(venv)], private, environment, args.timeout_seconds)
        python = verify_venv_interpreter(venv)
        requirements = private / "backend-requirements.lock"
        preparation._write(requirements, f"{backend.as_uri()} --hash=sha256:{manifest['backend_wheel']['sha256']}\n".encode("ascii"))
        _run(install_command(python, requirements), private, environment, args.timeout_seconds)
        verify_installed_runtime(venv, identity)
        wheels = private / "built-wheels"
        wheels.mkdir()
        bootstrap = (
            "import sys,sysconfig;sys.prefix=sys.exec_prefix=sys.argv[1];"
            "p=sysconfig.get_paths(scheme='venv',vars={'base':sys.argv[1],'platbase':sys.argv[1]});"
            "sys.path[:0]=list(dict.fromkeys([p['purelib'],p['platlib']]));"
            "from setuptools.build_meta import build_wheel;build_wheel(sys.argv[2])"
        )
        _run([str(python), "-I", "-S", "-c", bootstrap, str(venv), str(wheels)], source, environment, args.timeout_seconds)
        if {path.name for path in wheels.iterdir()} != {SDK_FILENAME}:
            raise WheelhouseError("SDK backend did not produce the exact expected 0.3.1 pure wheel")
        wheel = wheels / SDK_FILENAME
        with zipfile.ZipFile(wheel) as archive:
            _archive_members(archive, SDK_FILENAME)
            _archive_files(archive, "true", SDK_FILENAME)
            metadata = BytesParser().parsebytes(archive.read("chudcode_rpc-0.3.1.dist-info/METADATA"))
            if metadata.get_all("Name", []) != ["chudcode-rpc"] or metadata.get_all("Version", []) != ["0.3.1"]:
                raise WheelhouseError("built SDK wheel metadata differs from the pinned project")
        result = {"schema": "chudware-sdk-build-result-v1", "status": "unapproved-built-candidate",
                  "build_review_sha256": args.review_sha256, "source_archive_sha256": manifest["source_archive_sha256"],
                  "chudcode_source": manifest["chudcode_source"], "base_python": manifest["base_python"],
                  "backend_wheel": manifest["backend_wheel"], "source_date_epoch": manifest["source_date_epoch"],
                  "wheel": {"filename": SDK_FILENAME, "name": "chudcode-rpc", "version": "0.3.1", "sha256": preparation._digest(wheel)},
                  "approval": "build input operator assertion only; produced wheel is not approved for runtime installation"}
        staged = Path(tempfile.mkdtemp(prefix=".sdk-build-publication-", dir=output.parent))
        try:
            _copy_wheel(wheel, staged / SDK_FILENAME)
            if preparation._digest(staged / SDK_FILENAME) != result["wheel"]["sha256"]:
                raise WheelhouseError("SDK wheel changed during publication")
            preparation._write(staged / "SDK_BUILD_RESULT.json", preparation.canonical(result))
            preparation._write(staged / "REVIEWED_SDK_BUILD.json", review_bytes)
            preparation._publish(staged, output)
        finally:
            if staged.exists():
                import shutil
                shutil.rmtree(staged)
    return {**result, "output": str(output)}


def add_parser(subparsers):
    parser = subparsers.add_parser("build-sdk", help="explicit approved-source/backend offline SDK production; never runtime approval")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--review-sha256", required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--wheel-directory", type=Path, required=True, help="offline reviewed setuptools==83.0.0 wheel directory")
    parser.add_argument("--chudcode-lock", type=Path, default=REPO / "vendor/chudcode/CHUDCODE_ARTIFACT.lock")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300, help="explicit per-subprocess operator build timeout")


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.timeout_seconds <= 3600:
            raise WheelhouseError("operator build timeout must be between 1 and 3600 seconds")
        print(json.dumps(build_sdk(args), sort_keys=True))
        return 0
    except (WheelhouseError, OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError, tarfile.TarError,
            zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
