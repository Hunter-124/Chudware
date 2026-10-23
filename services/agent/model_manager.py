"""Model manager for bundled llama.cpp GGUF integration — T-W26.

This module handles model registry, download (with resume), and path resolution
for the two default GGUF models bundled with the Chudware agent service.

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
All GGUFs land in ``services.paths.MODELS`` (project-level ``models/`` directory).

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

from services import paths

__all__ = [
    "provision",
    "list_models",
    "get_model_path",
    "structured_output_for",
    "capability_profile_for",
    "model_for_role_shape",
    "ModelProvisionError",
    "_REGISTRY",
]

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Per-model capability fields, from the local-model throughput and grammar
# benchmark (numbers copied verbatim, never re-derived):
#
#   structured_output  "gbnf" | "prompted_json" — how this model must produce
#                      structured handoffs on the local llama.cpp runtime.
#                      qwen3-4b's GBNF path is BROKEN (thinking-mode vs grammar
#                      deadlock; bench GBNF success 0.0) so it uses prompted-JSON
#                      + roles.validate_handoff (bench JSON first-pass 1.00).
#                      gemma3-4b-qat follows schema_to_gbnf grammars at 100%.
#   capability_profile Optional "A"|"B"|"C" override (harness-tiers-spec §0.1).
#                      When set, wins over the tier's capability_profile for
#                      resolve_capability(tier, model_id). Default: unset →
#                      inherit from tier (same pattern as structured_output).
#   role_shape         "fanout"      — speed / parallel fan-out roles
#                      (qwen3-4b: 175 tok/s single-stream, 476 tok/s @ N=8);
#                      "gbnf_worker" — grammar-constrained worker roles
#                      (gemma3-4b-qat: 100% GBNF, smallest footprint 3086 MB,
#                      332 tok/s @ N=8).
#   parallel_slots     recommended llama-server --parallel for this model on the
#                      bundled CUDA runtime (12 GB has headroom past N=8).
#
# gemma4-e4b is NOT the shipped default for any role_shape (the benchmark measured it
# slower (115 tok/s) and larger (3464 MB) than gemma3-4b-qat
# with no measured upside for production role routing) — but it IS registered
# here (unlike diffusiongemma, which is a hard NO-GO on 12 GB: 26B-A4B MoE,
# smallest quant 16 GB, draft-CLI only, no server/grammar/parallel slots) so
# the benchmark gym tiers (tools/flywheel/bench/gym) can address it as a
# backup GBNF-worker comparison point. Its role_shape is deliberately left
# unset (not "gbnf_worker") so model_for_role_shape() never picks it as a
# shipped default over gemma3-4b-qat.
#
# Deliberately NOT in the registry:
#   diffusiongemma   — NO-GO on 12 GB: 26B-A4B MoE, smallest quant 16 GB,
#                      draft-CLI only (no server / grammar / parallel slots).
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
        "structured_output": "prompted_json",
        "role_shape": "fanout",
        "parallel_slots": 8,
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
        "structured_output": "gbnf",
        "role_shape": "gbnf_worker",
        "parallel_slots": 8,
    },
    "gemma4-e4b": {
        "id": "gemma4-e4b",
        "repo": "unsloth/gemma-4-E4B-it-GGUF",
        "file": "gemma-4-E4B-it-Q4_K_M.gguf",
        "quant": "Q4_K_M",
        "fallback_file": None,
        "fallback_quant": None,
        "ctx": 131072,
        "license": "Gemma-ToU",
        "sha256": None,
        "structured_output": "gbnf",
        "role_shape": "",  # not a shipped default (see comment above) — comparison-only
        "parallel_slots": 8,
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
    "Mozilla/5.0 (compatible; chudware-model-manager/1.0; "
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
                "structured_output": entry.get("structured_output", "gbnf"),
                "role_shape": entry.get("role_shape", ""),
                "parallel_slots": entry.get("parallel_slots"),
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


def structured_output_for(model_id: str, default: str = "gbnf") -> str:
    """Return how *model_id* must produce structured handoffs on the local runtime.

    ``"gbnf"`` — pass a ``roles.schema_to_gbnf`` grammar to llama-server;
    ``"prompted_json"`` — prompt for JSON and validate via
    ``roles.validate_handoff`` (retry on failure).

    This is the ONE lookup the turn path uses to honour the T-W29 finding that
    a tier's ``structured_output: gbnf`` setting must be downgraded per-model
    when the model's grammar path is broken (qwen3-4b). Unknown models fall back
    to *default* (the tier's own setting).
    """
    entry = _REGISTRY.get(model_id)
    if entry is None:
        return default
    return str(entry.get("structured_output", default))


def capability_profile_for(model_id: str) -> str | None:
    """Return the optional per-model capability letter override, or ``None``.

    When a registry entry sets ``capability_profile`` to ``"A"|"B"|"C"``, that
    letter wins over the tier default in ``capability.resolve_capability``.
    Unknown models and entries without the key return ``None`` (inherit tier).
    Mirrors :func:`structured_output_for` but returns ``None`` (not a default)
    so the resolver can distinguish "unset" from an explicit letter.
    """
    entry = _REGISTRY.get(model_id)
    if entry is None:
        return None
    val = entry.get("capability_profile")
    if val is None or val == "":
        return None
    return str(val)


def model_for_role_shape(shape: str) -> str | None:
    """Return the bundled model id shipped as the default for *role_shape*.

    Shapes (T-W29 role x stage mapping): ``"gbnf_worker"`` (grammar-constrained
    worker roles) -> gemma3-4b-qat; ``"fanout"`` (speed / parallel fan-out
    roles) -> qwen3-4b. Returns ``None`` for unknown shapes.

    NOTE: this is the SHIPPED capability default only. Actual role->model
    bindings live in routing.yaml (contract advisor.md section 14) and user
    edits there always win.
    """
    for entry in _REGISTRY.values():
        if entry.get("role_shape") == shape:
            return str(entry["id"])
    return None


def classify_bundled_task(prompt: str, *, cfg=None) -> str | None:
    """Use the running bundled fanout model, never a cloud or download fallback.

    An absent server/model is normal (including an offline install without GGUFs).
    The deterministic classifier then owns the decision. Do not spawn a competing
    model server merely to classify a turn.
    """
    import json
    from urllib.parse import urlparse

    local = (cfg or {}).get("llamacpp") or {}
    base = str(local.get("base_url") or "http://127.0.0.1:1234/v1").rstrip("/")
    if urlparse(base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None
    model = model_for_role_shape("fanout")
    if not model:
        return None
    # No proxy or redirect may move classification data off the computer.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(base + "/models", timeout=0.25) as response:
            models = json.loads(response.read(65536))
        ids = {str(row.get("id", "")) for row in models.get("data", [])}
        filename = _REGISTRY[model]["file"]
        served = next((mid for mid in sorted(ids) if mid == model or Path(mid).name == filename), None)
        if served is None:
            return None
        payload = {
            "model": served, "temperature": 0, "max_tokens": 12,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": "Classify the request. Reply with only chat, modelling, analysis, or mission. chat: question without mutation intent. modelling: features on one document. analysis: simulation or engineering analysis. mission: multiple deliverables or documents."},
                {"role": "user", "content": prompt[:12000]},
            ],
        }
        request = urllib.request.Request(base + "/chat/completions",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=2) as response:
            result = json.loads(response.read(65536))
        kind = result["choices"][0]["message"]["content"].strip().lower()
        return kind if kind in {"chat", "modelling", "analysis", "mission"} else None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
