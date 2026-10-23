#!/usr/bin/env python3
"""Offline runtime preparation from an explicitly operator-reviewed manifest.

``candidate`` produces an UNAPPROVED review document, not an installable lock.
The operator must independently review expected hashes, every wheel's source,
exact target context and all declarations, then set approval.decision=approved
and approval.reference to the retained review record. ``prepare`` additionally
requires that chosen document's digest via --review-sha256. This digest selects
reviewed bytes; it is not a signature or proof of reviewer authority.

Output is a new atomic bundle containing RUNTIME_WHEELS.lock, wheelhouse/,
REVIEWED_RUNTIME.json, CHUDCODE_ARTIFACT.lock and declarations/. No environment
is created or modified and no network, package resolver or build backend runs.
Use the separately authenticated target base Python with -I -S for this tool.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.installer.wheelhouse import (
    MAX_WHEEL_BYTES, WheelhouseError, _bytes, _copy_wheel, _directory, _json, _keys,
    _packaging, _regular_file, _sha, _source_identity, local_path, target_key,
    verified_wheelhouse, verify_installed_runtime, verify_venv_interpreter,
)

REVIEW_SCHEMA = "chudware-runtime-review-v1"
WHEEL_FIELDS = {"filename", "name", "version", "sha256"}


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with _regular_file(path, MAX_WHEEL_BYTES) as stream:
        remaining = MAX_WHEEL_BYTES
        while block := stream.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(block)
            if remaining < 0:
                raise WheelhouseError(f"runtime input exceeds hash size bound: {path}")
            digest.update(block)
    return digest.hexdigest()


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _document(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise WheelhouseError(f"duplicate review key: {key}")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise WheelhouseError("review manifest must be an object")
    return value


def target_context(path: Path | None = None) -> dict:
    markers, _, _, versions, _, tags = _packaging()
    context = (_json(path) if path is not None else
               {"environment": markers.default_environment(), "tags": sorted(str(tag) for tag in tags.sys_tags())})
    _keys(context, {"environment", "tags"}, "target context")
    environment = context["environment"]
    _keys(environment, set(markers.default_environment()), "PEP 508 environment")
    if (any(not isinstance(value, str) or not value or len(value) > 512 for value in environment.values())
            or environment["platform_python_implementation"] != "CPython"
            or environment["implementation_name"] != "cpython"
            or not isinstance(context["tags"], list) or not context["tags"]
            or len(context["tags"]) > 10000
            or any(not isinstance(tag, str) or len(tag) > 256 for tag in context["tags"])
            or len(set(context["tags"])) != len(context["tags"])):
        raise WheelhouseError("invalid exact target PEP 508 environment/tags")
    full_version = versions.Version(environment["python_full_version"])
    if environment["python_version"] != ".".join(map(str, full_version.release[:2])):
        raise WheelhouseError("target Python versions are inconsistent")
    for value in context["tags"]:
        if len(tags.parse_tag(value)) != 1:
            raise WheelhouseError("target tags must be individual canonical tags")
    return {"environment": environment, "tags": sorted(context["tags"])}


def _review(path: Path, digest: str, context: dict) -> tuple[dict, bytes]:
    if not _sha(digest):
        raise WheelhouseError("--review-sha256 requires the externally selected reviewed manifest digest")
    raw = _bytes(path)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise WheelhouseError("reviewed manifest differs from the externally supplied digest")
    manifest = _document(raw)
    _keys(manifest, {"schema", "approval", "requirements_sha256", "additional_requirements_sha256",
                     "native_lock_sha256", "chudcode_source", "target"}, "review manifest")
    if manifest["schema"] != REVIEW_SCHEMA:
        raise WheelhouseError("input is not an operator review manifest")
    approval = manifest["approval"]
    _keys(approval, {"decision", "reference"}, "operator approval")
    reference = approval["reference"]
    if (approval["decision"] != "approved" or not isinstance(reference, str) or not reference.strip()
            or reference != reference.strip() or len(reference) > 512 or any(ord(char) < 32 for char in reference)):
        raise WheelhouseError("explicit operator approval and retained review reference are required; candidate is not approved")
    if not _sha(manifest["requirements_sha256"]) or not _sha(manifest["native_lock_sha256"]):
        raise WheelhouseError("invalid reviewed declaration/native lock digest")
    additional = manifest["additional_requirements_sha256"]
    if (not isinstance(additional, list) or any(not _sha(value) for value in additional)
            or len(set(additional)) != len(additional)):
        raise WheelhouseError("invalid/duplicate reviewed additional declaration digests")
    source = manifest["chudcode_source"]
    _keys(source, {"repository", "repository_commit"}, "reviewed Chudcode source")
    _source_identity({"kind": "vcs", **source}, True, source)
    target = manifest["target"]
    _keys(target, {"environment", "tags", "wheels"}, "reviewed target")
    if {key: target[key] for key in ("environment", "tags")} != context:
        raise WheelhouseError("selected exact PEP 508 environment/tags differ from operator review")
    if not isinstance(target["wheels"], list) or not target["wheels"] or len(target["wheels"]) > 4096:
        raise WheelhouseError("review requires a nonempty bounded complete wheel set")
    _, _, utils, versions, _, _ = _packaging()
    names, filenames = set(), set()
    for entry in target["wheels"]:
        _keys(entry, WHEEL_FIELDS | {"source"}, "reviewed wheel")
        filename = entry["filename"]
        if (not isinstance(filename, str) or len(filename) > 255 or Path(filename).name != filename
                or any(char in filename for char in ("/", "\\", ":"))):
            raise WheelhouseError("reviewed wheel filename must be local, not a traversal")
        name, version, _, _ = utils.parse_wheel_filename(filename)
        if (not isinstance(entry["name"], str) or not isinstance(entry["version"], str)
                or utils.canonicalize_name(entry["name"]) != name or versions.Version(entry["version"]) != version
                or not _sha(entry["sha256"])):
            raise WheelhouseError("mismatched reviewed wheel identity")
        if name in names or filename.casefold() in filenames:
            raise WheelhouseError("duplicate reviewed wheel identity")
        names.add(name)
        filenames.add(filename.casefold())
        _source_identity(entry["source"], name == "chudcode-rpc", source)
    return manifest, raw


def runtime_lock(manifest: dict) -> dict:
    wheels = [{key: entry[key] for key in WHEEL_FIELDS} for entry in manifest["target"]["wheels"]]
    wheels.sort(key=lambda entry: entry["filename"])
    return {"schema": "chudware-runtime-wheels-v1", "requirements_sha256": manifest["requirements_sha256"],
            "chudcode_source": manifest["chudcode_source"],
            "targets": {target_key(manifest["target"]["environment"]): {"wheels": wheels}}}


def _declaration_inputs(manifest: dict, requirements: Path, additional: list[Path], native: Path) -> tuple[bytes, list[bytes], bytes]:
    root = _bytes(requirements)
    extras = [_bytes(path) for path in additional]
    native_bytes = _bytes(native)
    if hashlib.sha256(root).hexdigest() != manifest["requirements_sha256"]:
        raise WheelhouseError("runtime declarations changed since operator review")
    if [hashlib.sha256(value).hexdigest() for value in extras] != manifest["additional_requirements_sha256"]:
        raise WheelhouseError("complete ordered release/gate declarations changed or omitted since operator review")
    if hashlib.sha256(native_bytes).hexdigest() != manifest["native_lock_sha256"]:
        raise WheelhouseError("native Chudcode lock changed since operator review")
    native_document = _document(native_bytes)
    if manifest["chudcode_source"] != {key: native_document.get(key) for key in manifest["chudcode_source"]}:
        raise WheelhouseError("reviewed chudcode-rpc source differs from native Chudcode commit")
    return root, extras, native_bytes


def _output(path: Path) -> Path:
    path = local_path(path)
    if path.exists():
        raise WheelhouseError("output already exists; preparation never replaces environments or retained evidence")
    if not path.parent.is_dir():
        raise WheelhouseError("output parent must already exist")
    parent = path.parent.stat()
    if os.name == "posix" and (parent.st_mode & 0o022) and not (parent.st_mode & stat.S_ISVTX):
        raise WheelhouseError("output parent must not be group/world writable unless protected by sticky-bit semantics")
    return path


def _publish(staged: Path, destination: Path) -> None:
    """Atomic no-replace publication; never silently overwrite a racing output."""
    local_path(destination)
    with _directory(destination.parent) as parent:
        if sys.platform.startswith("linux"):
            library = ctypes.CDLL(None, use_errno=True)
            rename = library.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(parent, os.fsencode(staged.name), parent, os.fsencode(destination.name), 1) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(destination))
        elif sys.platform == "darwin":
            library = ctypes.CDLL(None, use_errno=True)
            rename = library.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(os.fsencode(staged), os.fsencode(destination), 4) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(destination))
        elif os.name == "nt":
            os.rename(staged, destination)  # Windows refuses existing destinations.
        else:
            raise WheelhouseError("atomic no-replace publication is unavailable on this operator platform")


def _write(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(args) -> dict:
    context = target_context(args.target_context)
    manifest, raw = _review(args.manifest, args.review_sha256, context)
    root, extras, native = _declaration_inputs(manifest, args.requirements, args.additional_requirements, args.chudcode_lock)
    wheel_directory = local_path(args.wheel_directory)
    expected = {entry["filename"] for entry in manifest["target"]["wheels"]}
    supplied = {path.name for path in wheel_directory.iterdir() if path.name.lower().endswith(".whl")}
    if supplied != expected:
        missing, unexpected = sorted(expected - supplied), sorted(supplied - expected)
        raise WheelhouseError(f"wheel directory differs from reviewed complete set; missing={missing}, unexpected={unexpected}")
    output = _output(args.output)
    # All approval/source/declaration checks above precede any wheel copying.
    temporary = Path(tempfile.mkdtemp(prefix=".runtime-preparation-", dir=output.parent))
    try:
        _write(temporary / "REVIEWED_RUNTIME.json", raw)
        _write(temporary / "RUNTIME_WHEELS.lock", canonical(runtime_lock(manifest)))
        _write(temporary / "CHUDCODE_ARTIFACT.lock", native)
        declarations = temporary / "declarations"
        declarations.mkdir()
        _write(declarations / "requirements.txt", root)
        additional = []
        for index, contents in enumerate(extras):
            path = declarations / f"additional-{index:03d}.txt"
            _write(path, contents)
            additional.append(path)
        wheels = temporary / "wheelhouse"
        wheels.mkdir()
        with verified_wheelhouse(wheel_directory, declarations / "requirements.txt", temporary / "RUNTIME_WHEELS.lock",
                                 temporary / "CHUDCODE_ARTIFACT.lock", context["environment"], set(context["tags"]),
                                 additional_requirements=additional) as identity:
            # Publish only authenticated snapshot bytes, never reread candidates.
            snapshot = identity["requirements"].parent
            for entry in manifest["target"]["wheels"]:
                _copy_wheel(snapshot / entry["filename"], wheels / entry["filename"])
                if _digest(wheels / entry["filename"]) != entry["sha256"]:
                    raise WheelhouseError("authenticated snapshot changed during preparation")
            packages = identity["packages"]
        _publish(temporary, output)
        return {"status": "prepared", "output": str(output), "review_sha256": args.review_sha256,
                "target": target_key(context["environment"]), "packages": packages,
                "approval": "explicit operator review assertion; no signature/owner-authority attestation"}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def candidate(args) -> dict:
    context = target_context(args.target_context)
    directory = local_path(args.wheel_directory)
    if not directory.is_dir():
        raise WheelhouseError("candidate wheel directory is absent")
    _, _, utils, _, _, _ = _packaging()
    wheels = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".whl":
            continue
        name, version, _, _ = utils.parse_wheel_filename(path.name)
        wheels.append({"filename": path.name, "name": str(name), "version": str(version),
                       "sha256": _digest(path), "source": None})
        if len(wheels) > 4096:
            raise WheelhouseError("too many candidate wheels")
    native = _json(args.chudcode_lock)
    manifest = {"schema": REVIEW_SCHEMA, "approval": {"decision": "pending", "reference": None},
                "requirements_sha256": _digest(args.requirements),
                "additional_requirements_sha256": [_digest(path) for path in args.additional_requirements],
                "native_lock_sha256": _digest(args.chudcode_lock),
                "chudcode_source": {key: native.get(key) for key in ("repository", "repository_commit")},
                "target": {**context, "wheels": wheels}}
    output = _output(args.output)
    descriptor, name = tempfile.mkstemp(prefix=".runtime-review-", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        _publish(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"status": "unapproved-candidate", "output": str(output),
            "warning": "candidate hashes are observations, not approval; independently review every source/hash and declaration"}


def validate_installed(args) -> dict:
    bundle = local_path(args.bundle)
    context = target_context()
    manifest, _ = _review(bundle / "REVIEWED_RUNTIME.json", args.review_sha256, context)
    additional = [bundle / "declarations" / f"additional-{index:03d}.txt"
                  for index in range(len(manifest["additional_requirements_sha256"]))]
    requirements = bundle / "declarations/requirements.txt"
    native = bundle / "CHUDCODE_ARTIFACT.lock"
    _declaration_inputs(manifest, requirements, additional, native)
    lock = bundle / "RUNTIME_WHEELS.lock"
    if _bytes(lock) != canonical(runtime_lock(manifest)):
        raise WheelhouseError("runtime lock differs from explicitly reviewed preparation manifest")
    with verified_wheelhouse(bundle / "wheelhouse", requirements, lock, native,
                             context["environment"], set(context["tags"]), additional_requirements=additional) as identity:
        verify_venv_interpreter(args.venv)
        verify_installed_runtime(args.venv, identity)
    return {"status": "installed-runtime-validated", "target": target_key(context["environment"]),
            "review_sha256": args.review_sha256, "venv": str(args.venv)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("context", help="print this separately trusted base's exact target context")
    for command in ("candidate", "prepare"):
        entry = sub.add_parser(command)
        entry.add_argument("--wheel-directory", type=Path, required=True)
        entry.add_argument("--output", type=Path, required=True)
        entry.add_argument("--requirements", type=Path, default=REPO / "requirements.txt")
        entry.add_argument("--additional-requirements", type=Path, action="append", default=[],
                           help="repeat for every explicitly reviewed release/gate declarations file, in review order")
        entry.add_argument("--chudcode-lock", type=Path, default=REPO / "vendor/chudcode/CHUDCODE_ARTIFACT.lock")
        entry.add_argument("--target-context", type=Path, help="explicit exact cross-target context; defaults to this trusted base")
        if command == "prepare":
            entry.add_argument("--manifest", type=Path, required=True)
            entry.add_argument("--review-sha256", required=True)
    installed = sub.add_parser("validate-installed", help="read-only base-authenticated validation of an existing disposable/runtime venv")
    installed.add_argument("--bundle", type=Path, required=True)
    installed.add_argument("--venv", type=Path, required=True)
    installed.add_argument("--review-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = (target_context() if args.command == "context" else
                  prepare(args) if args.command == "prepare" else
                  candidate(args) if args.command == "candidate" else validate_installed(args))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (WheelhouseError, OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
