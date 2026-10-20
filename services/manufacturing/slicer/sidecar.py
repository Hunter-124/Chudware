"""Arms-length CLI seam for additive slicing.

Only argv, ordinary files, and a subprocess cross this boundary.  There is no
in-process slicer API and this module has no printer transport or execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping

from .contract import (
    Artifact,
    ContractValidationError,
    PerObjectStats,
    SliceProvenance,
    SliceRefusal,
    SliceRequest,
    SliceResult,
    canonical_json,
)

SLICER_ENV_VAR = "CHUDWARE_SLICER_BIN"
_SIDECAR_RESULT_SCHEMA = "chudware.slicer_sidecar_result/1"
MAX_MODEL_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_SIDECAR_RESULT_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_BYTES = 1024 * 1024
MAX_BINARY_BYTES = 512 * 1024 * 1024
_SAFE_ENV_KEYS = frozenset({"LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ"})


@dataclass(frozen=True)
class ResolvedBinary:
    path: Path
    sha256: str


class _SecurePathError(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _ProcessLimitError(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _absolute_without_symlinks(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.abspath(candidate))
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise _SecurePathError(f"{label}_missing", f"{label} is missing") from None
        if stat.S_ISLNK(mode):
            raise _SecurePathError(f"{label}_symlink", f"{label} may not use symlinks")
    return candidate


def _hash_file(path: Path, *, limit: int, label: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _SecurePathError(f"{label}_symlink", f"{label} may not use symlinks") from None
        raise
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise _SecurePathError(f"{label}_not_regular", f"{label} must be a regular file")
        if metadata.st_size > limit:
            raise _SecurePathError(
                f"{label}_too_large",
                f"{label} exceeds the {limit} byte limit",
            )
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def resolve_binary(
    configured_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedBinary | SliceRefusal:
    """Resolve an explicit binary without PATH lookup or symlink traversal."""

    environment = os.environ if environ is None else environ
    selected = str(configured_path) if configured_path is not None else environment.get(SLICER_ENV_VAR)
    if not selected:
        return SliceRefusal(
            code="missing_slicer_binary",
            message=f"Slicing requires {SLICER_ENV_VAR} or an explicit slicer settings path.",
            missing_prerequisite=SLICER_ENV_VAR,
        )
    try:
        path = _absolute_without_symlinks(selected, label="slicer_binary")
        if path.suffix != ".py" and not os.access(path, os.X_OK):
            return SliceRefusal(
                code="slicer_binary_not_executable",
                message=f"The slicer configured by {SLICER_ENV_VAR} is not executable.",
                missing_prerequisite=SLICER_ENV_VAR,
            )
        digest = _hash_file(path, limit=MAX_BINARY_BYTES, label="slicer_binary")
        return ResolvedBinary(path=path, sha256=digest)
    except _SecurePathError as exc:
        return SliceRefusal(
            code=exc.code,
            message=exc.reason,
            missing_prerequisite=SLICER_ENV_VAR,
        )
    except OSError:
        return SliceRefusal(
            code="slicer_binary_unreadable",
            message=f"The slicer configured by {SLICER_ENV_VAR} cannot be read.",
            missing_prerequisite=SLICER_ENV_VAR,
        )


def build_argv(
    binary: ResolvedBinary | str | Path,
    request_path: str | Path,
    output_path: str | Path,
    sidecar_result_path: str | Path,
) -> list[str]:
    """Build the complete coarse-grained files-and-argv protocol."""

    path = binary.path if isinstance(binary, ResolvedBinary) else Path(binary)
    prefix = [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]
    return [
        *prefix,
        "--request",
        str(Path(request_path)),
        "--output",
        str(Path(output_path)),
        "--result",
        str(Path(sidecar_result_path)),
    ]


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

def _read_limited(path: Path, *, limit: int, label: str) -> bytes:
    try:
        _absolute_without_symlinks(path, label=label)
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _SecurePathError(f"{label}_symlink", f"{label} may not use symlinks") from None
        raise
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise _SecurePathError(f"{label}_not_regular", f"{label} must be a regular file")
        if metadata.st_size > limit:
            raise _SecurePathError(f"{label}_too_large", f"{label} exceeds the {limit} byte limit")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, min(1024 * 1024, limit + 1 - total)):
            total += len(chunk)
            if total > limit:
                raise _SecurePathError(f"{label}_too_large", f"{label} exceeds the {limit} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _stage_binary(resolved: ResolvedBinary, work_dir: Path) -> ResolvedBinary:
    suffix = ".py" if resolved.path.suffix == ".py" else ""
    staged = work_dir / f"slicer-bin{suffix}"
    source_fd = os.open(resolved.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".slicer-bin.", dir=work_dir)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as target:
            while chunk := os.read(source_fd, 1024 * 1024):
                total += len(chunk)
                if total > MAX_BINARY_BYTES:
                    raise _SecurePathError(
                        "slicer_binary_too_large",
                        f"slicer_binary exceeds the {MAX_BINARY_BYTES} byte limit",
                    )
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest() != resolved.sha256:
            raise _SecurePathError(
                "slicer_binary_changed",
                "The slicer binary changed while it was being prepared.",
            )
        temporary.chmod(0o700)
        os.replace(temporary, staged)
        return ResolvedBinary(staged, resolved.sha256)
    finally:
        os.close(source_fd)
        temporary.unlink(missing_ok=True)


def _scrubbed_environment(source: Mapping[str, str] | None) -> dict[str, str]:
    provided = os.environ if source is None else source
    environment = {key: provided[key] for key in _SAFE_ENV_KEYS if key in provided}
    environment["PATH"] = ""
    environment["PYTHONHASHSEED"] = "0"
    return environment


def _refusal(
    code: str,
    message: str,
    *,
    detail: str | None = None,
    work_dir: Path | None = None,
) -> SliceRefusal:
    refusal = SliceRefusal(code=code, message=message, detail=detail)
    if work_dir is not None:
        try:
            _atomic_write(work_dir / "result.json", canonical_json(refusal.to_dict()))
        except OSError:
            pass
    return refusal


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 1.0
    group_alive = True
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            group_alive = False
            break
        time.sleep(0.01)
    if group_alive:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout_s: float | None,
    limit: int,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("slicer pipes were not created")
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    try:
        while selector.get_map():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise _ProcessLimitError(
                    "slicer_timeout",
                    "The slicer exceeded its wall-clock time limit.",
                )
            events = selector.select(remaining)
            if not events:
                continue
            for key, _ in events:
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(stream)
                    continue
                target = streams[stream]
                if len(target) + len(chunk) > limit:
                    name = "stdout" if stream is process.stdout else "stderr"
                    raise _ProcessLimitError(
                        "slicer_output_limit",
                        f"The slicer exceeded the {limit} byte {name} limit.",
                    )
                target.extend(chunk)
        # EOF is not process exit. Keep the original operation deadline while
        # waiting for a child that has closed both pipes but is still running.
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise _ProcessLimitError(
                "slicer_timeout",
                "The slicer exceeded its wall-clock time limit.",
            ) from exc
        return bytes(streams[process.stdout]), bytes(streams[process.stderr])
    except _ProcessLimitError:
        _stop_process_group(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _number(payload: Mapping[str, Any], name: str) -> int | float:
    value = payload[name]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractValidationError(f"sidecar field {name} must be numeric")
    return value

def _integer(payload: Mapping[str, Any], name: str) -> int:
    value = payload[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractValidationError(f"sidecar field {name} must be an integer")
    return value


def _numeric_list(values: list[Any], name: str) -> tuple[float, ...]:
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool)
        for value in values
    ):
        raise ContractValidationError(f"sidecar field {name} must contain only numbers")
    return tuple(float(value) for value in values)


def _parse_success(
    payload: Mapping[str, Any],
    *,
    request: SliceRequest,
    resolved: ResolvedBinary,
    output_part: Path,
    output_final: Path,
    input_hashes: Mapping[str, str],
) -> SliceResult:
    if payload.get("schema") != _SIDECAR_RESULT_SCHEMA:
        raise ContractValidationError("sidecar result schema is missing or unsupported")
    if payload.get("status") != "ok":
        raise ContractValidationError("sidecar returned a non-ok result after a zero exit")
    artifact_payload = payload.get("artifact")
    if not isinstance(artifact_payload, Mapping):
        raise ContractValidationError("sidecar result is missing artifact metadata")
    reported_path = artifact_payload.get("path")
    if (
        not isinstance(reported_path, str)
        or Path(os.path.abspath(reported_path)) != output_part
    ):
        raise ContractValidationError("sidecar artifact path does not match the requested output")

    identity = payload.get("sidecar")
    if not isinstance(identity, Mapping):
        raise ContractValidationError("sidecar identity is missing")
    name = identity.get("name")
    version = identity.get("version")
    commit = identity.get("commit")
    if not all(isinstance(value, str) and value.strip() for value in (name, version, commit)):
        raise ContractValidationError("sidecar name, version, and commit must be non-empty")

    raw_stats = payload.get("per_object_stats")
    if not isinstance(raw_stats, list):
        raise ContractValidationError("sidecar per_object_stats must be a list")
    stats: list[PerObjectStats] = []
    for raw in raw_stats:
        if not isinstance(raw, Mapping):
            raise ContractValidationError("sidecar per-object statistic must be an object")
        filament_mm = raw.get("filament_mm")
        filament_g = raw.get("filament_g")
        if not isinstance(filament_mm, list) or not isinstance(filament_g, list):
            raise ContractValidationError("sidecar filament statistics must be lists")
        stats.append(
            PerObjectStats(
                object_id=raw.get("object_id", ""),
                estimated_time_s=float(_number(raw, "estimated_time_s")),
                filament_mm=_numeric_list(filament_mm, "filament_mm"),
                filament_g=_numeric_list(filament_g, "filament_g"),
                layer_count=_integer(raw, "layer_count"),
            )
        )
    expected_ids = sorted(model.id for model in request.models)
    if sorted(item.object_id for item in stats) != expected_ids:
        raise ContractValidationError("sidecar per-object statistics do not match request models")

    raw_warnings = payload.get("warnings", [])
    if not isinstance(raw_warnings, list) or any(not isinstance(item, str) for item in raw_warnings):
        raise ContractValidationError("sidecar warnings must be a list of strings")

    output_hash = _hash_file(output_part, limit=MAX_ARTIFACT_BYTES, label="slicer_artifact")
    output_size = output_part.lstat().st_size
    os.replace(output_part, output_final)
    profile_hashes = {
        f"{profile.kind}:{profile.id}": profile.bundle_sha256 for profile in request.profiles
    }
    provenance = SliceProvenance(
        request_id=request.request_id,
        sidecar_name=name,
        sidecar_version=version,
        sidecar_commit=commit,
        sidecar_build_id=request.sidecar_build_id,
        binary_sha256=resolved.sha256,
        input_sha256=dict(input_hashes),
        output_sha256=output_hash,
        profile_hashes=profile_hashes,
    )
    return SliceResult(
        request_id=request.request_id,
        artifact=Artifact(
            path=str(output_final),
            sha256=output_hash,
            bytes=output_size,
            format=request.output_format,
        ),
        per_object_stats=tuple(stats),
        warnings=tuple(raw_warnings),
        provenance=provenance,
    )


def invoke_sidecar(
    request: SliceRequest,
    work_root: str | Path,
    *,
    binary: str | Path | None = None,
    timeout_s: float = 60.0,
    environ: Mapping[str, str] | None = None,
    input_root: str | Path | None = None,
    max_capture_bytes: int = MAX_CAPTURE_BYTES,
) -> SliceResult | SliceRefusal:
    """Run one deterministic generate-class slice and return data, never a traceback."""

    resolved = resolve_binary(binary, environ=environ)
    if isinstance(resolved, SliceRefusal):
        return resolved
    if timeout_s <= 0:
        return SliceRefusal(code="invalid_timeout", message="Slicer timeout must be positive.")
    if max_capture_bytes <= 0:
        return SliceRefusal(
            code="invalid_output_limit",
            message="Slicer stdout and stderr limits must be positive.",
        )

    raw_work_root = Path(work_root).expanduser()
    if ".." in raw_work_root.parts:
        return SliceRefusal(
            code="work_root_traversal",
            message="The slicer working root may not contain parent traversal.",
        )
    work_dir: Path | None = None
    try:
        if not raw_work_root.is_absolute():
            raw_work_root = Path.cwd() / raw_work_root
        raw_work_root.mkdir(parents=True, exist_ok=True)
        secure_work_root = _absolute_without_symlinks(raw_work_root, label="work_root")
        allowed_root = _absolute_without_symlinks(
            input_root if input_root is not None else secure_work_root.parent,
            label="model_root",
        )
        work_dir = secure_work_root / request.request_id
        request_path = work_dir / "request.json"
        output_dir = work_dir / "out"
        extension = request.output_format
        output_part = output_dir / f"{request.request_id}.{extension}.part"
        output_final = output_dir / f"{request.request_id}.{extension}"
        sidecar_result_path = work_dir / "sidecar_result.json"

        work_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        _absolute_without_symlinks(work_dir, label="work_directory")
        _absolute_without_symlinks(output_dir, label="output_directory")
        staged_binary = _stage_binary(resolved, work_dir)

        input_hashes: dict[str, str] = {}
        for model in request.models:
            raw_model_path = Path(model.path).expanduser()
            if not raw_model_path.is_absolute():
                return _refusal(
                    "model_path_not_absolute",
                    "Model input paths must be absolute.",
                    work_dir=work_dir,
                )
            if ".." in raw_model_path.parts:
                return _refusal(
                    "model_path_traversal",
                    "Model input paths may not contain parent traversal.",
                    work_dir=work_dir,
                )
            try:
                model_path = _absolute_without_symlinks(raw_model_path, label="model_input")
            except _SecurePathError as exc:
                return _refusal(exc.code, exc.reason, work_dir=work_dir)
            if not model_path.is_relative_to(allowed_root):
                return _refusal(
                    "model_path_escape",
                    "A model input is outside the allowed model root.",
                    work_dir=work_dir,
                )
            try:
                actual_hash = _hash_file(
                    model_path,
                    limit=MAX_MODEL_BYTES,
                    label="model_input",
                )
            except _SecurePathError as exc:
                return _refusal(exc.code, exc.reason, work_dir=work_dir)
            if actual_hash != model.mesh_sha256:
                return _refusal(
                    "model_hash_mismatch",
                    "A model input no longer matches its content hash.",
                    work_dir=work_dir,
                )
            input_hashes[model.id] = actual_hash

        request_payload = canonical_json(request.to_dict())
        if len(request_payload) > MAX_SIDECAR_RESULT_BYTES:
            return _refusal(
                "slice_request_too_large",
                f"The slice request exceeds the {MAX_SIDECAR_RESULT_BYTES} byte limit.",
                work_dir=work_dir,
            )
        _atomic_write(request_path, request_payload)
        for stale_path in (output_part, sidecar_result_path):
            stale_path.unlink(missing_ok=True)
        argv = build_argv(staged_binary, request_path, output_part, sidecar_result_path)
        _atomic_write(work_dir / "cmd.txt", (shlex.join(argv) + "\n").encode("utf-8"))

        process = subprocess.Popen(
            argv,
            cwd=work_dir,
            env=_scrubbed_environment(environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = _communicate_bounded(
                process,
                timeout_s=timeout_s,
                limit=max_capture_bytes,
            )
        except _ProcessLimitError as exc:
            _atomic_write(work_dir / "stdout.log", b"")
            _atomic_write(work_dir / "stderr.log", b"")
            return _refusal(
                exc.code,
                exc.reason + " Its process group was stopped.",
                work_dir=work_dir,
            )
        _atomic_write(work_dir / "stdout.log", stdout)
        _atomic_write(work_dir / "stderr.log", stderr)
        if process.returncode != 0:
            return _refusal(
                "slicer_failed",
                "The slicer exited without producing an accepted artifact.",
                detail=f"exit={process.returncode}",
                work_dir=work_dir,
            )
        if not sidecar_result_path.exists():
            return _refusal(
                "malformed_sidecar_output",
                "The slicer did not emit its required result JSON.",
                work_dir=work_dir,
            )
        try:
            result_bytes = _read_limited(
                sidecar_result_path,
                limit=MAX_SIDECAR_RESULT_BYTES,
                label="sidecar_result",
            )
            raw = json.loads(result_bytes)
            if not isinstance(raw, Mapping):
                raise ContractValidationError("sidecar result must be a JSON object")
            result = _parse_success(
                raw,
                request=request,
                resolved=resolved,
                output_part=output_part,
                output_final=output_final,
                input_hashes=input_hashes,
            )
        except _SecurePathError as exc:
            return _refusal(exc.code, exc.reason, work_dir=work_dir)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ContractValidationError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return _refusal(
                "malformed_sidecar_output",
                "The slicer result did not satisfy the slice contract.",
                work_dir=work_dir,
            )
        _atomic_write(work_dir / "result.json", canonical_json(result.to_dict()))
        return result
    except _SecurePathError as exc:
        return _refusal(exc.code, exc.reason, work_dir=work_dir)
    except OSError:
        return _refusal(
            "sidecar_io_failure",
            "The slicer working directory or process could not be used.",
            work_dir=work_dir,
        )
