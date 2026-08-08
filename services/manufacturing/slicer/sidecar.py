"""Arms-length CLI seam for additive slicing.

Only argv, ordinary files, and a subprocess cross this boundary.  There is no
in-process slicer API and this module has no printer transport or execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
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


@dataclass(frozen=True)
class ResolvedBinary:
    path: Path
    sha256: str


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_binary(
    configured_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedBinary | SliceRefusal:
    """Resolve settings first, then CHUDWARE_SLICER_BIN, without guessing a fallback."""

    environment = os.environ if environ is None else environ
    selected = str(configured_path) if configured_path is not None else environment.get(SLICER_ENV_VAR)
    if not selected:
        return SliceRefusal(
            code="missing_slicer_binary",
            message=f"Slicing requires {SLICER_ENV_VAR} or an explicit slicer settings path.",
            missing_prerequisite=SLICER_ENV_VAR,
        )
    path = Path(selected).expanduser().resolve()
    if not path.is_file():
        return SliceRefusal(
            code="slicer_binary_unavailable",
            message=f"The slicer configured by {SLICER_ENV_VAR} is not a file.",
            missing_prerequisite=SLICER_ENV_VAR,
            detail=str(path),
        )
    if path.suffix != ".py" and not os.access(path, os.X_OK):
        return SliceRefusal(
            code="slicer_binary_not_executable",
            message=f"The slicer configured by {SLICER_ENV_VAR} is not executable.",
            missing_prerequisite=SLICER_ENV_VAR,
            detail=str(path),
        )
    try:
        return ResolvedBinary(path=path, sha256=_hash_file(path))
    except OSError as exc:
        return SliceRefusal(
            code="slicer_binary_unreadable",
            message=f"The slicer configured by {SLICER_ENV_VAR} cannot be hashed.",
            missing_prerequisite=SLICER_ENV_VAR,
            detail=str(exc),
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
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


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
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1.0)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _decode_tail(payload: bytes, limit: int = 8192) -> str:
    return payload[-limit:].decode("utf-8", errors="replace")


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
    if not isinstance(reported_path, str) or Path(reported_path).resolve() != output_part.resolve():
        raise ContractValidationError("sidecar artifact path does not match the requested output")
    if not output_part.is_file():
        raise ContractValidationError("sidecar reported success without writing the artifact")

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

    output_hash = _hash_file(output_part)
    output_size = output_part.stat().st_size
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
) -> SliceResult | SliceRefusal:
    """Run one deterministic generate-class slice and return data, never a traceback."""

    resolved = resolve_binary(binary, environ=environ)
    if isinstance(resolved, SliceRefusal):
        return resolved
    if timeout_s <= 0:
        return SliceRefusal(code="invalid_timeout", message="Slicer timeout must be positive.")

    work_dir = Path(work_root).resolve() / request.request_id
    request_path = work_dir / "request.json"
    output_dir = work_dir / "out"
    extension = request.output_format
    output_part = output_dir / f"{request.request_id}.{extension}.part"
    output_final = output_dir / f"{request.request_id}.{extension}"
    sidecar_result_path = work_dir / "sidecar_result.json"

    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        input_hashes: dict[str, str] = {}
        for model in request.models:
            model_path = Path(model.path).expanduser().resolve()
            if not model_path.is_file():
                return _refusal(
                    "missing_model_input",
                    "A model input referenced by the slice request is missing.",
                    detail=f"{model.id}: {model_path}",
                    work_dir=work_dir,
                )
            actual_hash = _hash_file(model_path)
            if actual_hash != model.mesh_sha256:
                return _refusal(
                    "model_hash_mismatch",
                    "A model input no longer matches its content hash.",
                    detail=model.id,
                    work_dir=work_dir,
                )
            input_hashes[model.id] = actual_hash

        _atomic_write(request_path, canonical_json(request.to_dict()))
        for stale_path in (output_part, sidecar_result_path):
            try:
                stale_path.unlink()
            except FileNotFoundError:
                pass
        argv = build_argv(resolved, request_path, output_part, sidecar_result_path)
        _atomic_write(work_dir / "cmd.txt", (shlex.join(argv) + "\n").encode("utf-8"))

        child_env = dict(os.environ)
        if environ is not None:
            child_env.update(environ)
        process = subprocess.Popen(
            argv,
            cwd=work_dir,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _stop_process_group(process)
            stdout, stderr = process.communicate()
            _atomic_write(work_dir / "stdout.log", stdout)
            _atomic_write(work_dir / "stderr.log", stderr)
            return _refusal(
                "slicer_timeout",
                "The slicer exceeded its time limit and its process group was stopped.",
                detail=_decode_tail(stderr),
                work_dir=work_dir,
            )
        _atomic_write(work_dir / "stdout.log", stdout)
        _atomic_write(work_dir / "stderr.log", stderr)
        if process.returncode != 0:
            return _refusal(
                "slicer_failed",
                "The slicer exited without producing an accepted artifact.",
                detail=f"exit={process.returncode}; stderr={_decode_tail(stderr)}",
                work_dir=work_dir,
            )
        if not sidecar_result_path.is_file():
            return _refusal(
                "malformed_sidecar_output",
                "The slicer did not emit its required result JSON.",
                work_dir=work_dir,
            )
        try:
            raw = json.loads(sidecar_result_path.read_text(encoding="utf-8"))
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
        except (OSError, UnicodeError, json.JSONDecodeError, ContractValidationError, KeyError, TypeError, ValueError) as exc:
            return _refusal(
                "malformed_sidecar_output",
                "The slicer result did not satisfy the slice contract.",
                detail=str(exc),
                work_dir=work_dir,
            )
        _atomic_write(work_dir / "result.json", canonical_json(result.to_dict()))
        return result
    except OSError as exc:
        return _refusal(
            "sidecar_io_failure",
            "The slicer working directory or process could not be used.",
            detail=str(exc),
            work_dir=work_dir,
        )
