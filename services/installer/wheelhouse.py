"""Validate reviewed runtime wheels before pip sees any installable input.

The lock is part of the trusted source/payload, never read from the supplied
wheelhouse. Offline preparation requires a separately approved review manifest.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from email.parser import BytesParser
import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import sys
import zipfile


class WheelhouseError(ValueError):
    """Missing, incomplete or unauthenticated runtime input."""


_BOOTSTRAP_RESOURCES = ExitStack()
atexit.register(_BOOTSTRAP_RESOURCES.close)

MAX_WHEEL_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * MAX_WHEEL_BYTES
MAX_METADATA_BYTES = 1024 * 1024
MAX_IDENTITY_BYTES = 4 * 1024 * 1024
# The public repository the chudcode-rpc SDK wheel is built from (python/chudcode-rpc),
# at the commit vendor/chudcode/CHUDCODE_ARTIFACT.lock pins for the native runtime.
CHUDCODE_REPOSITORY = "https://github.com/Hunter-124/chudcode.git"


def local_path(path: Path) -> Path:
    """Reject lexical traversal and symlinks, including existing ancestors."""
    path = Path(path)
    if ".." in path.parts:
        raise WheelhouseError(f"parent traversal is not permitted: {path}")
    absolute = path.absolute()
    for parent in reversed((absolute, *absolute.parents)):
        if parent.is_symlink():
            raise WheelhouseError(f"symlink in runtime input/output path: {path}")
    return absolute


@contextmanager
def _directory(path: Path):
    path = local_path(path)
    if os.name != "posix":
        yield None
        return
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _regular_file(path: Path, maximum: int):
    path = local_path(path)
    with _directory(path.parent) as parent:
        descriptor = os.open(path.name if parent is not None else path,
                             os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                             dir_fd=parent)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise WheelhouseError(f"runtime input is not a bounded regular file: {path}")
        yield stream
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise WheelhouseError(f"runtime input changed while reading: {path}")


def _bytes(path: Path, maximum: int = MAX_IDENTITY_BYTES) -> bytes:
    with _regular_file(path, maximum) as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise WheelhouseError(f"runtime input exceeds size bound: {path}")
    return value


def _copy_wheel(original: Path, copied: Path) -> None:
    with _regular_file(original, MAX_WHEEL_BYTES) as stream, copied.open("xb") as destination:
        remaining = MAX_WHEEL_BYTES
        while block := stream.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(block)
            if remaining < 0:
                raise WheelhouseError(f"wheel exceeds size bound: {original.name}")
            destination.write(block)


def _archive_members(archive, filename: str) -> list[str]:
    entries = archive.infolist()
    members = [entry.filename for entry in entries]
    if len(entries) > 100000 or len(set(members)) != len(members):
        raise WheelhouseError(f"duplicate/oversized wheel archive members: {filename}")
    if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
        raise WheelhouseError(f"wheel archive exceeds expanded size bound: {filename}")
    for entry in entries:
        mode = entry.external_attr >> 16
        if entry.flag_bits & 1 or stat.S_ISLNK(mode):
            raise WheelhouseError(f"encrypted/symlink wheel member: {filename}")
        if entry.filename.endswith((".dist-info/METADATA", ".dist-info/WHEEL")) and entry.file_size > MAX_METADATA_BYTES:
            raise WheelhouseError(f"wheel metadata exceeds size bound: {filename}")
    return members


def _source_identity(value: dict, sdk: bool, native_source: dict) -> None:
    """Source identities are reviewer assertions, never inferred from wheel bytes."""
    if not isinstance(value, dict):
        raise WheelhouseError("wheel source identity requires explicit review")
    if value.get("kind") == "vcs":
        _keys(value, {"kind", "repository", "repository_commit"}, "wheel source")
        if not re.fullmatch(r"[0-9a-f]{40}", str(value["repository_commit"])):
            raise WheelhouseError("wheel source requires an immutable repository commit")
        url = value["repository"]
        if sdk and {key: value[key] for key in native_source} != native_source:
            raise WheelhouseError("chudcode-rpc wheel source differs from the reviewed Chudcode commit")
    elif not sdk and value.get("kind") == "archive":
        _keys(value, {"kind", "url"}, "wheel source")
        url = value["url"]
    else:
        raise WheelhouseError("chudcode-rpc requires reviewed immutable VCS provenance; "
                              "other wheels need a reviewed VCS or archive source")
    from urllib.parse import urlsplit
    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 33 for char in url):
        raise WheelhouseError("invalid reviewed source URL")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise WheelhouseError("reviewed source URL must be credential-free HTTPS")
    except ValueError as exc:
        raise WheelhouseError("invalid reviewed source URL") from exc

@contextmanager
def _bootstrap_wheels():
    """The selected base interpreter's bundled/distro ensurepip is trusted input."""
    import ensurepip
    from importlib import resources
    with ExitStack() as stack:
        if hasattr(ensurepip, "_get_packages"):  # CPython 3.11/3.12 bootstrap API
            wheels = []
            for package in ensurepip._get_packages().values():
                if package.wheel_path:
                    wheels.append(Path(package.wheel_path))
                else:
                    wheels.append(stack.enter_context(resources.as_file(
                        resources.files("ensurepip") / "_bundled" / package.wheel_name)))
        else:  # CPython 3.13+ bootstrap API
            wheels = [stack.enter_context(ensurepip._get_pip_whl_path_ctx())]
        yield wheels


def _archive_files(archive, pure: str, filename: str) -> list[dict]:
    files = []
    for member in _archive_members(archive, filename):
        parts = member.rstrip("/").split("/")
        if ("\\" in member or ":" in member or member.startswith("/")
                or any(part in ("", ".", "..") for part in parts)):
            raise WheelhouseError(f"unsafe wheel member: {filename}")
        if member.endswith("/"):
            continue
        if member.endswith(".dist-info/direct_url.json"):
            raise WheelhouseError(f"wheel supplies spoofable installed provenance: {filename}")
        if (len(parts) == 2 and parts[0].endswith(".dist-info")
                and parts[1] in ("RECORD", "INSTALLER", "REQUESTED")):
            continue  # Only this wheel's top-level metadata is generated by pip.
        scheme = "purelib" if pure == "true" else "platlib"
        relative = member
        if parts[0].endswith(".data"):
            if len(parts) < 3 or parts[1] not in ("purelib", "platlib", "data", "scripts", "headers"):
                raise WheelhouseError(f"invalid wheel data scheme: {filename}")
            if parts[1] in ("scripts", "headers"):
                continue  # relocated/rewritten by pip, not Python importable package bytes
            scheme, relative = parts[1], "/".join(parts[2:])
        with archive.open(member) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"scheme": scheme, "path": relative, "sha256": digest})
    return files


def _bootstrap_files() -> dict:
    files = {}
    with _bootstrap_wheels() as wheels:
        for path in wheels:
            with zipfile.ZipFile(path) as archive:
                metadata_path = next(member for member in archive.namelist() if member.endswith(".dist-info/METADATA"))
                metadata = BytesParser().parsebytes(archive.read(metadata_path))
                wheel = BytesParser().parsebytes(archive.read(metadata_path.rsplit("/", 1)[0] + "/WHEEL"))
                name = re.sub(r"[-_.]+", "-", metadata["Name"]).lower()
                files[name] = _archive_files(archive, wheel["Root-Is-Purelib"].lower(), path.name)
    return files


def _packaging():
    # Do not import an arbitrary installed parser before authenticating site
    # packages. Reuse pip's parser from the trusted base Python bootstrap wheel.
    try:
        if not hasattr(_packaging, "wheel"):
            wheels = _BOOTSTRAP_RESOURCES.enter_context(_bootstrap_wheels())
            pip_wheel = next(path for path in wheels if path.name.startswith("pip-"))
            sys.path.insert(0, str(pip_wheel))
            _packaging.wheel = pip_wheel
        from pip._vendor.packaging import markers, requirements, utils, version, specifiers, tags
    except (ImportError, OSError, AttributeError, StopIteration) as exc:
        raise WheelhouseError("trusted base Python requires its original complete ensurepip wheel payload") from exc
    return markers, requirements, utils, version, specifiers, tags


def _json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise WheelhouseError(f"duplicate lock key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(_bytes(path), object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise WheelhouseError(f"reviewed runtime identity unavailable at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WheelhouseError(f"expected a lock object at {path}")
    return value


def _keys(value: dict, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise WheelhouseError(f"invalid {label} fields")


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


def _sha(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def target_key(environment: dict) -> str:
    machine = environment["platform_machine"].lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"{environment['sys_platform']}-{machine}-cp{environment['python_version'].replace('.', '')}"


def _declarations(path: Path, environment: dict, chudcode_source: dict) -> list:
    _, requirements, utils, _, _, _ = _packaging()
    pinned_sdk = f"git+{chudcode_source['repository']}@{chudcode_source['repository_commit']}"
    result = []
    for raw in _bytes(path).decode("utf-8").splitlines():
        # pip's comment rule: '#' starts a comment only at line start or after
        # whitespace, so a URL fragment such as #subdirectory= survives.
        line = re.sub(r"(^|\s+)#.*$", "", raw).strip()
        if not line or line in ("--no-index", "--only-binary=:all:"):
            continue
        try:
            req = requirements.Requirement(line)
        except ValueError as exc:
            raise WheelhouseError(f"invalid runtime requirement: {line}") from exc
        if req.url:
            # Developers build the SDK from this direct reference; the reviewed
            # chudcode-rpc wheel from the same commit satisfies it here.
            if (utils.canonicalize_name(req.name) != "chudcode-rpc"
                    or req.url.split("#", 1)[0] != pinned_sdk):
                raise WheelhouseError("runtime declarations cannot supply unreviewed direct URLs")
            req = requirements.Requirement("chudcode-rpc" + (f"; {req.marker}" if req.marker else ""))
        if not req.marker or req.marker.evaluate(environment):
            result.append(req)
    return result


@contextmanager
def verified_wheelhouse(wheelhouse: Path, requirements: Path, lock_path: Path,
                        chudcode_lock: Path, environment: dict | None = None,
                        supported_tags: set[str] | None = None,
                        additional_requirements: Path | list[Path] | None = None):
    """Yield a hash-locked requirements snapshot after checking the full closure.

    Callers may select a target interpreter's PEP 508 environment, but cannot
    override expected hashes through wheelhouse content or environment variables.
    No wheel modules or build backends are imported/executed during inspection.
    """
    lock = _json(lock_path)
    markers, req_parser, utils, versions, specifiers, tags = _packaging()
    environment = environment or markers.default_environment()
    supported_tags = supported_tags if supported_tags is not None else {str(tag) for tag in tags.sys_tags()}
    if environment["platform_python_implementation"] != "CPython":
        raise WheelhouseError("runtime wheel locks require CPython")
    _keys(lock, {"schema", "requirements_sha256", "chudcode_source", "targets"}, "runtime lock")
    if lock["schema"] != "chudware-runtime-wheels-v1" or not _sha(lock["requirements_sha256"]):
        raise WheelhouseError("invalid runtime lock schema/requirements digest")
    if _digest(requirements) != lock["requirements_sha256"]:
        raise WheelhouseError("runtime declarations changed since wheel lock review")
    additional = ([] if additional_requirements is None else
                  [additional_requirements] if isinstance(additional_requirements, (str, Path)) else
                  list(additional_requirements))
    source = lock["chudcode_source"]
    _keys(source, {"repository", "repository_commit"}, "Chudcode source")
    native = _json(chudcode_lock)
    if source != {key: native.get(key) for key in source}:
        raise WheelhouseError("chudcode-rpc wheel source differs from the reviewed Chudcode commit")
    if (source["repository"] != CHUDCODE_REPOSITORY
            or not re.fullmatch(r"[0-9a-f]{40}", str(source["repository_commit"]))):
        raise WheelhouseError("chudcode-rpc wheel lacks immutable source provenance")
    key = target_key(environment)
    if not isinstance(lock["targets"], dict) or key not in lock["targets"]:
        raise WheelhouseError(f"reviewed complete runtime wheel set unavailable for {key}")
    target = lock["targets"][key]
    _keys(target, {"wheels"}, "target")
    if not isinstance(target["wheels"], list) or not target["wheels"]:
        raise WheelhouseError("empty runtime wheel lock")
    root = local_path(wheelhouse).resolve(strict=True)
    if not root.is_dir():
        raise WheelhouseError("runtime wheelhouse is not a directory")
    with tempfile.TemporaryDirectory(prefix="chudware-reviewed-wheels-") as temporary:
        snapshot = Path(temporary)
        installed = {}
        installed_files = {}
        lines = []
        filenames = set()
        payload_paths = set()
        for entry in target["wheels"]:
            _keys(entry, {"filename", "name", "version", "sha256"}, "wheel")
            filename, name, version, digest = (entry[field] for field in ("filename", "name", "version", "sha256"))
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or "/" in filename or "\\" in filename or ":" in filename or not filename.endswith(".whl")):
                raise WheelhouseError("wheel lock contains a non-local wheel filename")
            if not _sha(digest) or not isinstance(name, str) or not isinstance(version, str):
                raise WheelhouseError("invalid wheel identity")
            if filename.casefold() in filenames:
                raise WheelhouseError(f"duplicate wheel filename: {filename}")
            filenames.add(filename.casefold())
            try:
                wheel_name, wheel_version, _, wheel_tags = utils.parse_wheel_filename(filename)
                expected_version = versions.Version(version)
            except ValueError as exc:
                raise WheelhouseError(f"invalid wheel identity: {filename}") from exc
            canonical = utils.canonicalize_name(name)
            if canonical in installed or canonical != wheel_name or expected_version != wheel_version:
                raise WheelhouseError(f"ambiguous/mismatched wheel identity: {filename}")
            if not {str(tag) for tag in wheel_tags}.intersection(supported_tags):
                raise WheelhouseError(f"wheel does not support selected target: {filename}")
            original = root / filename
            if original.is_symlink() or not original.is_file():
                raise WheelhouseError(f"approved wheel absent or symlinked: {filename}")
            copied = snapshot / filename
            _copy_wheel(original, copied)
            if _digest(copied) != digest:
                raise WheelhouseError(f"approved wheel hash mismatch: {filename}")
            try:
                with zipfile.ZipFile(copied) as archive:
                    members = _archive_members(archive, filename)
                    metadata_files = [member for member in members
                                      if member.endswith(".dist-info/METADATA") and member.count("/") == 1]
                    if len(metadata_files) != 1:
                        raise WheelhouseError(f"ambiguous wheel metadata: {filename}")
                    metadata_path = metadata_files[0]
                    metadata = BytesParser().parsebytes(archive.read(metadata_path))
                    wheel = BytesParser().parsebytes(archive.read(metadata_path.rsplit("/", 1)[0] + "/WHEEL"))
                    declared_tags = set()
                    for raw_tag in wheel.get_all("Tag", []):
                        declared_tags.update(tags.parse_tag(raw_tag))
                    if (declared_tags != wheel_tags or len(wheel.get_all("Root-Is-Purelib", [])) != 1
                            or wheel.get_all("Wheel-Version", []) != ["1.0"]):
                        raise WheelhouseError(f"wheel header/tags differ from reviewed filename: {filename}")
                    pure = wheel.get("Root-Is-Purelib", "").lower()
                    if pure not in ("true", "false"):
                        raise WheelhouseError(f"invalid wheel install scheme: {filename}")
                    installed_files[canonical] = _archive_files(archive, pure, filename)
                    for member in installed_files[canonical]:
                        location = (("site" if member["scheme"] in ("purelib", "platlib") else member["scheme"]),
                                    member["path"].casefold())
                        if location in payload_paths:
                            raise WheelhouseError(f"overlapping wheel install payload: {filename}")
                        payload_paths.add(location)
            except (OSError, zipfile.BadZipFile, KeyError) as exc:
                raise WheelhouseError(f"invalid wheel archive: {filename}") from exc
            if (len(metadata.get_all("Name", [])) != 1 or len(metadata.get_all("Version", [])) != 1
                    or utils.canonicalize_name(metadata["Name"]) != canonical
                    or metadata["Version"] != version):
                raise WheelhouseError(f"wheel metadata does not match reviewed identity: {filename}")
            requires_python = metadata.get("Requires-Python")
            if requires_python:
                if not specifiers.SpecifierSet(requires_python).contains(environment["python_full_version"]):
                    raise WheelhouseError(f"wheel does not support selected Python: {filename}")
            dependencies = []
            for raw in metadata.get_all("Requires-Dist", []):
                try:
                    dependency = req_parser.Requirement(raw)
                except ValueError as exc:
                    raise WheelhouseError(f"invalid dependency metadata: {filename}") from exc
                if dependency.url:
                    raise WheelhouseError(f"unreviewed direct dependency URL: {filename}")
                dependencies.append(dependency)
            installed[canonical] = (version, dependencies)
            lines.append(f"{copied.as_uri()} --hash=sha256:{digest}\n")
        if installed.get("chudcode-rpc", (None,))[0] != "0.3.1":
            raise WheelhouseError("reviewed chudcode-rpc==0.3.1 wheel is required")
        pending = _declarations(requirements, environment, source)
        for path in additional:
            pending.extend(_declarations(Path(path), environment, source))
        # Even additional reviewed wheels must have a complete dependency closure.
        pending.extend(req_parser.Requirement(f"{name}=={value[0]}") for name, value in installed.items())
        visited = set()
        while pending:
            requirement = pending.pop()
            name = utils.canonicalize_name(requirement.name)
            actual = installed.get(name)
            if actual is None or not requirement.specifier.contains(actual[0]):
                raise WheelhouseError(f"runtime wheel lock is incomplete/incompatible: {requirement}")
            for extra in {"", *requirement.extras}:
                node = (name, extra)
                if node in visited:
                    continue
                visited.add(node)
                for dependency in actual[1]:
                    if not dependency.marker or dependency.marker.evaluate({**environment, "extra": extra}):
                        pending.append(dependency)
        locked_requirements = snapshot / "requirements.lock"
        locked_requirements.write_text("".join(lines), encoding="utf-8")
        yield {"requirements": locked_requirements, "lock_sha256": _digest(lock_path), "target": key,
               "packages": {name: value[0] for name, value in installed.items()},
               "hashes": {utils.canonicalize_name(entry["name"]): entry["sha256"] for entry in target["wheels"]},
               "files": installed_files}


def pip_environment() -> dict:
    # Interpreter/ELF/Mach-O overrides act before pip's own --isolated flag.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("PIP_", "PYTHON", "LD_", "DYLD_"))
                   and key.upper() not in ("__PYVENV_LAUNCHER__", "VIRTUAL_ENV")}
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


def install_command(python: Path, requirements: Path) -> list[str]:
    # Site-disabled startup: even an authenticated .pth is unnecessary for pip.
    bootstrap = (
        "import sys,runpy;sys.prefix=sys.exec_prefix=sys.argv[1];import sysconfig; "
        "p=sysconfig.get_paths(scheme='venv',vars={'base':sys.argv[1],'platbase':sys.argv[1]}); "
        "sys.path[:0]=list(dict.fromkeys([p['purelib'],p['platlib']])); "
        "sys.argv=['pip',*sys.argv[2:]];runpy.run_module('pip',run_name='__main__')"
    )
    return [str(python), "-I", "-S", "-c", bootstrap, str(python.absolute().parent.parent),
            "--isolated", "install", "--no-index", "--no-deps", "--require-hashes",
            "--only-binary=:all:", "--no-cache-dir", "--force-reinstall",
            "--disable-pip-version-check", "-r", str(requirements)]


def verify_venv_interpreter(venv: Path) -> Path:
    """Authenticate a candidate offline from a separately trusted base process.

    The selected base executable, stdlib, venv launchers and platform loader
    libraries are operator-authenticated prerequisites. Candidate metadata is
    never allowed to establish those identities, and the candidate never runs.
    """
    import sysconfig
    import venv as base_venv

    original = Path(sys.executable).absolute()
    if ((original.parent.parent / "pyvenv.cfg").exists()
            or sys.prefix != sys.base_prefix):
        raise WheelhouseError("interpreter inspection requires a separately trusted non-venv base Python")
    base = Path(sys._base_executable).absolute()
    base_target = base.resolve(strict=True)
    directory = venv.absolute()
    configuration = directory / "pyvenv.cfg"
    if configuration.is_symlink():
        raise WheelhouseError("candidate pyvenv.cfg must not be a symlink")
    fields = {}
    for line in configuration.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        key = key.strip().lower()
        if not separator or key in fields or key not in (
                "home", "include-system-site-packages", "version", "executable", "command", "prompt"):
            raise WheelhouseError("candidate pyvenv.cfg has unapproved/duplicate fields")
        fields[key] = value.strip()
    if (fields.get("include-system-site-packages") != "false"
            or not Path(fields.get("home", "")).is_absolute()
            or not Path(fields.get("executable", "")).is_absolute()
            or fields.get("version") != ".".join(map(str, sys.version_info[:3]))
            or Path(fields.get("home", "")).resolve() != base.parent.resolve()
            or Path(fields.get("executable", "")).resolve() != original.resolve()):
        raise WheelhouseError("candidate pyvenv.cfg does not identify the selected trusted base")
    binary_directory = directory / ("Scripts" if sys.platform == "win32" else "bin")
    candidate = binary_directory / ("python.exe" if sys.platform == "win32" else "python")
    if not candidate.is_file():
        raise WheelhouseError("candidate Python executable is absent")
    references = [base_target]
    if sys.platform == "win32":
        debug = "_d" if base.stem.lower().endswith("_d") else ""
        free_threaded = "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""
        scripts = Path(base_venv.__file__).parent / "scripts" / "nt"
        references.extend(path for path in (
            scripts / ("python" + debug + ".exe"),
            scripts / ("venvlauncher" + free_threaded + debug + ".exe"),
            base.parent / ("venvlauncher" + free_threaded + debug + ".exe"),
        ) if path.is_file())
    if candidate.is_symlink():
        if candidate.resolve(strict=True) != base_target:
            raise WheelhouseError("candidate Python symlink does not resolve to the trusted base")
    elif _digest(candidate) not in {_digest(path) for path in references}:
        raise WheelhouseError("candidate Python executable differs from authenticated base/launcher bytes")
    # Startup path files and local loader libraries can redirect authenticated
    # executable bytes before Python's isolation flags take effect.
    for root in (directory, binary_directory):
        for path in root.iterdir():
            if path.name.endswith("._pth") or (path.name == "pyvenv.cfg" and root == binary_directory):
                raise WheelhouseError(f"unapproved candidate startup path configuration: {path}")
            if sys.platform == "win32" and path.suffix.lower() == ".dll":
                reference = base.parent / path.name
                if not reference.is_file() or _digest(path) != _digest(reference):
                    raise WheelhouseError(f"candidate loader library differs from trusted base: {path}")
    if sys.platform != "win32":
        reference_dirs = {base.parent, base.parent.parent / "lib", base.parent.parent / "lib64"}
        library_dir = sysconfig.get_config_var("LIBDIR")
        if library_dir:
            reference_dirs.add(Path(library_dir))
        for root in (directory, binary_directory, directory / "lib", directory / "lib64"):
            if not root.exists():
                continue
            for path in root.iterdir():
                if path.is_file() and (".so" in path.name or path.suffix == ".dylib"):
                    originals = [parent / path.name for parent in reference_dirs if (parent / path.name).is_file()]
                    if not originals or _digest(path) not in {_digest(reference) for reference in originals}:
                        raise WheelhouseError(f"candidate loader library differs from trusted base: {path}")
        if (directory / "Frameworks").exists() or (directory / ".Python").exists():
            raise WheelhouseError("candidate contains an unapproved framework loader path")
    library = directory / ("Lib" if sys.platform == "win32" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}")
    if library.exists():
        for path in library.iterdir():
            if path.name != "site-packages":
                raise WheelhouseError(f"candidate contains unapproved stdlib shadow: {path}")
    for root in (directory, binary_directory, directory / "lib"):
        if root.exists() and any(root.glob("python*.zip")):
            raise WheelhouseError("candidate contains an unapproved stdlib archive")
    return candidate


def verify_installed_runtime(venv: Path, identity: dict) -> None:
    """Inspect candidate installs under -I -S; no site/package code is imported."""
    import importlib.metadata as metadata
    from importlib.util import MAGIC_NUMBER, source_from_cache
    import marshal
    import sysconfig
    from types import CodeType

    scheme = sysconfig.get_paths(scheme="venv", vars={"base": str(venv), "platbase": str(venv)})
    for name in ("purelib", "platlib"):
        directory = Path(scheme[name]).resolve()
        if not directory.is_relative_to(venv.resolve()):
            raise WheelhouseError("installed site directory escapes the selected runtime")
        scheme[name] = str(directory)
    all_files = dict(identity["files"])
    all_files.update({name: files for name, files in _bootstrap_files().items() if name not in all_files})
    approved_paths = set()
    package_roots = set()
    module_names = set()
    for files in all_files.values():
        for member in files:
            path = Path(scheme[member["scheme"]]) / member["path"]
            if path.is_symlink() or not path.is_file() or _digest(path) != member["sha256"]:
                raise WheelhouseError(f"installed payload differs from authenticated wheel: {path}")
            approved_paths.add(path)
            parts = member["path"].split("/")
            if member["scheme"] in ("purelib", "platlib") and not parts[0].endswith(".dist-info"):
                if len(parts) > 1:
                    package_roots.add(Path(scheme[member["scheme"]]) / parts[0])
                elif path.suffix in (".py", ".so", ".pyd"):
                    module_names.add(parts[0].split(".", 1)[0])

    def normalized_code(code):
        return code.replace(co_filename="", co_consts=tuple(
            normalized_code(value) if isinstance(value, CodeType) else value for value in code.co_consts))

    def verify_cache(path):
        try:
            source = Path(source_from_cache(str(path))) if path.parent.name == "__pycache__" else path.with_suffix(".py")
            if source not in approved_paths:
                raise WheelhouseError(f"unapproved installed bytecode: {path}")
            data = path.read_bytes()
            if data[:4] != MAGIC_NUMBER or len(data) < 16 or int.from_bytes(data[4:8], "little") not in (0, 1, 3):
                raise WheelhouseError(f"invalid installed bytecode: {path}")
            optimization = re.search(r"\.opt-([12])\.", path.name)
            level = int(optimization.group(1)) if optimization else 0
            actual = marshal.loads(data[16:])
            # Builtin compilation does not execute source or any build backend.
            expected = compile(source.read_bytes(), str(source), "exec", dont_inherit=True, optimize=level)
            if not isinstance(actual, CodeType) or normalized_code(actual) != normalized_code(expected):
                raise WheelhouseError(f"installed bytecode differs from authenticated source: {path}")
        except (OSError, ValueError, TypeError, EOFError) as exc:
            raise WheelhouseError(f"invalid installed bytecode: {path}") from exc

    def inspect_extra(path):
        if path.is_symlink():
            raise WheelhouseError(f"symlink in authenticated package tree: {path}")
        if path.is_file() and path not in approved_paths:
            if path.suffix == ".pyc":
                verify_cache(path)
            else:
                raise WheelhouseError(f"unapproved file in authenticated package tree: {path}")

    for root in package_roots:
        if root.is_symlink():
            raise WheelhouseError(f"symlinked authenticated package root: {root}")
        for path in root.rglob("*"):
            inspect_extra(path)
    for directory in {Path(scheme["purelib"]), Path(scheme["platlib"])}:
        for path in directory.iterdir():
            if path.name.endswith(".pth") or path.name.startswith(("sitecustomize", "usercustomize")):
                if path not in approved_paths and path not in package_roots:
                    raise WheelhouseError(f"unapproved Python startup hook: {path}")
            if path.name.split(".", 1)[0] in module_names:
                inspect_extra(path)
        for path in (directory / "__pycache__").glob("*.pyc"):
            try:
                source = Path(source_from_cache(str(path)))
            except ValueError as exc:
                raise WheelhouseError(f"invalid installed bytecode: {path}") from exc
            if source in approved_paths:
                inspect_extra(path)

    found = {}
    for distribution in metadata.distributions(path=list({scheme["purelib"], scheme["platlib"]})):
        name = re.sub(r"[-_.]+", "-", distribution.metadata.get("Name", "")).lower()
        if name in identity["packages"]:
            if name in found:
                raise WheelhouseError(f"duplicate installed distribution: {name}")
            found[name] = distribution
    for name, version in identity["packages"].items():
        distribution = found.get(name)
        if distribution is None or distribution.version != version:
            raise WheelhouseError(f"installed version differs from reviewed wheel: {name}")
        origin = json.loads(distribution.read_text("direct_url.json") or "{}")
        if origin.get("dir_info") or not origin.get("url", "").startswith("file:"):
            raise WheelhouseError(f"installed wheel provenance is missing/editable: {name}")
        archive = origin.get("archive_info", {})
        digest = archive.get("hashes", {}).get("sha256") or archive.get("hash", "").removeprefix("sha256=")
        if digest != identity["hashes"][name]:
            raise WheelhouseError(f"installed wheel provenance differs from reviewed digest: {name}")
