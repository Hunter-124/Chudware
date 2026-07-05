"""Model manager for bundled llama.cpp GGUF integration — T-W26.

This module handles model registry, download (with resume), and path resolution
for the two default GGUF models bundled with the AI-CAD agent service.

Model picks rationale
---------------------
qwen3-4b
    Qwen3-4B-Q4_K_M.gguf from the official Qwen/Qwen3-4B-GGUF repository.
    Licensed Apache-2.0 — no use restrictions, safe to redistribute.
    The Qwen3 series is explicitly recommended by llama.cpp maintainers for
    tool-calling workloads, and 4B fits comfortably in 4–6 GB VRAM/RAM at Q4.

gemma3-4b-qat
    gemma-3-4b-it-qat-Q4_K_M.gguf from bartowski's Google Gemma QAT GGUF repo.
    QAT (Quantization-Aware Training) means the model was fine-tuned to absorb
    quantization noise; at Q4 it retains quality that a post-hoc Q4 quant would
    lose.  Google publishes their own GGUF weights via bartowski's mirror.
    Licensed under the Google Gemma Terms of Use (use-restricted, NOT Apache) —
    commercial deployment requires reviewing those terms.

Storage
-------
All GGUFs land in ``engine.paths.MODELS`` (project-level ``models/`` directory).

Usage
-----
    from services.agent.model_manager import provision, list_models, get_model_path

    result = provision("qwen3-4b")
    path   = get_model_path("qwen3-4b")
    models = list_models()
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from engine import paths

__all__ = [
    "provision",
    "list_models",
    "get_model_path",
    "ModelProvisionError",
    "_REGISTRY",
]

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, dict[str, Any]] = {
    "qwen3-4b": {
        "id": "qwen3-4b",
        "repo": "Qwen/Qwen3-4B-GGUF",
        "file": "Qwen3-4B-Q4_K_M.gguf",
        "quant": "Q4_K_M",
        "fallback_file": "Qwen3-4B-Q3_K_M.gguf",
        "fallback_quant": "Q3_K_M",
        "ctx": 32768,
        "license": "Apache-2.0",
        "sha256": None,
    },
    "gemma3-4b-qat": {
        "id": "gemma3-4b-qat",
        "repo": "bartowski/google_gemma-3-4b-it-qat-GGUF",
        "file": "gemma-3-4b-it-qat-Q4_K_M.gguf",
        "quant": "Q4_K_M",
        "fallback_file": "gemma-3-4b-it-qat-Q3_K_XL.gguf",
        "fallback_quant": "Q3_K_XL",
        "ctx": 131072,
        "license": "Gemma-ToU",
        "sha256": None,
    },
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ModelProvisionError(RuntimeError):
    """Raised when a model cannot be downloaded or verified."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_HF_BASE = "https://huggingface.co/{repo}/resolve/main/{filename}"

_USER_AGENT = (
    "Mozilla/5.0 (compatible; ai-cad-model-manager/1.0; "
    "+https://github.com/your-org/CAD)"
)


def _hf_url(repo: str, filename: str) -> str:
    return _HF_BASE.format(repo=repo, filename=filename)


def _model_dir() -> Path:
    """Return the models directory, creating it if necessary."""
    d = paths.MODELS
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dest_path(entry: dict[str, Any]) -> Path:
    return _model_dir() / entry["file"]


def _resolve_existing(entry: dict[str, Any]) -> Path | None:
    """Return the on-disk GGUF for *entry*, accepting either the flat layout
    (``MODELS/<file>``) or a per-model subdir (``MODELS/<id>/<file>``).

    The bundled provisioner and some external tools drop the GGUF under a
    per-model subdirectory; this makes ``get_model_path`` find it either way
    (T-W30 — the C1 proof run's qwen3-4b lives under ``models/qwen3-4b/``)."""
    flat = _model_dir() / entry["file"]
    if flat.exists():
        return flat
    nested = _model_dir() / entry["id"] / entry["file"]
    if nested.exists():
        return nested
    # Fallback: any GGUF under a subdir named after the model id.
    subdir = _model_dir() / entry["id"]
    if subdir.is_dir():
        ggufs = sorted(subdir.glob("*.gguf"))
        if ggufs:
            return ggufs[0]
    return None


def _part_path(entry: dict[str, Any]) -> Path:
    return _model_dir() / (entry["file"] + ".part")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    """Download *url* to *dest*, resuming via HTTP Range if a .part file exists.

    The download is staged through ``dest.with_suffix(dest.suffix + '.part')``
    and atomically renamed on completion.  The caller is responsible for passing
    the true destination (not the .part path).
    """
    part = Path(str(dest) + ".part")

    existing_bytes = part.stat().st_size if part.exists() else 0

    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    if existing_bytes:
        headers["Range"] = f"bytes={existing_bytes}-"

    try:
        req = urllib.request.Request(url, headers=headers)
        try:
            resp_ctx = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                # Server says range not satisfiable — .part already complete.
                os.replace(part, dest)
                return
            raise

        with resp_ctx as resp:
            status = resp.status
            if status == 416:
                # Some servers return 416 without raising (rare).
                os.replace(part, dest)
                return
            elif status == 206:
                # Partial content — append to the existing .part file.
                mode = "ab"
            else:
                # 200 OK: server returned the whole file regardless of Range.
                # Must overwrite (NOT append) to avoid corruption.
                mode = "wb"
                existing_bytes = 0  # reset so the progress is coherent

            with part.open(mode) as fh:
                while True:
                    chunk = resp.read(1 << 20)  # 1 MiB chunks
                    if not chunk:
                        break
                    fh.write(chunk)

    except urllib.error.HTTPError as exc:
        raise ModelProvisionError(
            f"HTTP {exc.code} fetching {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ModelProvisionError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc

    # Atomic rename of completed download.
    os.replace(part, dest)


def _verify_checksum(path: Path, expected: str | None) -> None:
    """Verify sha256 checksum if *expected* is not None.

    Raises ``ModelProvisionError`` on mismatch; is a no-op when *expected* is
    ``None`` (checksum unknown for this registry entry).
    """
    if expected is None:
        return
    actual = _sha256_file(path)
    if actual != expected.lower():
        raise ModelProvisionError(
            f"sha256 mismatch for {path.name}: "
            f"expected {expected}, got {actual}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def provision(model_id: str, *, force: bool = False) -> dict[str, Any]:
    """Download a registered GGUF model if not already present.

    Parameters
    ----------
    model_id:
        Key into ``_REGISTRY`` (e.g. ``"qwen3-4b"``).
    force:
        If *True*, re-download even if the file already exists.

    Returns
    -------
    dict
        ``{"status": "ok"|"already_present", "path": str, "model_id": str}``

    Raises
    ------
    KeyError
        If *model_id* is not in the registry.
    ModelProvisionError
        On network errors, HTTP errors, or checksum mismatch.
    """
    if model_id not in _REGISTRY:
        raise KeyError(f"Unknown model id {model_id!r}. Available: {list(_REGISTRY)}")

    entry = _REGISTRY[model_id]
    dest = _dest_path(entry)

    existing = _resolve_existing(entry)
    if existing is not None and not force:
        return {
            "status": "already_present",
            "path": str(existing),
            "model_id": model_id,
        }

    url = _hf_url(entry["repo"], entry["file"])

    try:
        _download(url, dest)
    except ModelProvisionError:
        raise
    except Exception as exc:
        raise ModelProvisionError(
            f"Unexpected error provisioning {model_id}: {exc}"
        ) from exc

    try:
        _verify_checksum(dest, entry.get("sha256"))
    except ModelProvisionError:
        # Remove the bad file so a retry starts clean.
        dest.unlink(missing_ok=True)
        raise

    return {
        "status": "ok",
        "path": str(dest),
        "model_id": model_id,
    }


def list_models() -> list[dict[str, Any]]:
    """Return a list of all registered models with their on-disk presence.

    Each dict contains: ``id``, ``repo``, ``file``, ``quant``, ``ctx``,
    ``license``, ``present`` (bool).
    """
    result: list[dict[str, Any]] = []
    for entry in _REGISTRY.values():
        result.append(
            {
                "id": entry["id"],
                "repo": entry["repo"],
                "file": entry["file"],
                "quant": entry["quant"],
                "ctx": entry["ctx"],
                "license": entry["license"],
                "present": _resolve_existing(entry) is not None,
            }
        )
    return result


def get_model_path(model_id: str) -> Path | None:
    """Return the local ``Path`` to the GGUF file if it exists, else ``None``.

    Parameters
    ----------
    model_id:
        Key into ``_REGISTRY``.

    Returns
    -------
    Path or None
    """
    if model_id not in _REGISTRY:
        return None
    return _resolve_existing(_REGISTRY[model_id])
