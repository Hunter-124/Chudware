"""Managed llama.cpp local runtime — T-W26.

Lifecycle wrapper around ``services/installer/llamacpp.py`` (which handles bundle
discovery, spawn, and health-polling).  This module adds the pieces that installer
does NOT provide:

* Free-port allocation — pick an OS-assigned port and pass it into the cfg dict so
  two parallel processes never collide.
* Served-context capture — after startup, query ``/props`` on the live server to
  learn the ACTUAL n_ctx it was loaded with (``--ctx-size`` can differ from the
  model's native size), then call ``tiers.detect_tier()`` to determine the tier.
* Routing auto-registration — idempotently append a ``kind: llamacpp`` source to
  routing.yaml on first provision.  Never overwrites existing entries; user edits
  are preserved.  Thread-safe via rename-based atomic write.
* System-PATH fallback — if no bundled binary is installed, the spawn path falls
  back to ``llama-server`` / ``llama-server.exe`` on PATH.

Design note: this module deliberately does NOT re-implement spawn/health logic.
It composes with ``installer.llamacpp.ensure_server_running()`` via cfg injection.

GBNF grammar support: verified present in llama.cpp b9860 (the pinned release —
see ``LLAMA_CPP_PINNED_BUILD``).  ``/v1/chat/completions`` accepts ``grammar``
(raw GBNF string) in the request body.  ``roles.py:schema_to_gbnf()`` output is
compatible (T-W29 bench: gemma3-4b-qat 100% grammar compliance on b9860).
"""
from __future__ import annotations

import json
import socket
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

import yaml

# --------------------------------------------------------------------------- public API

__all__ = [
    "start",
    "stop",
    "ensure_running",
    "get_served_n_ctx",
    "register_routing_source",
    "LocalRuntimeError",
    "RuntimeInfo",
    # Optional GPU transformers backend (T-W29) — extras-guarded.
    "transformers_available",
    "TransformersBackend",
    "select_backend",
    "BackendUnavailable",
]

# --------------------------------------------------------------------------- constants

# Pinned llama.cpp release the shipped runtime was built and VERIFIED against.
#
# Pin reconcile (T-W31): T-W26 recorded "b9871", but the runtime actually
# installed at ~/.local/share/AI-CAD/llamacpp is release tag b9860 (commit
# fdb1db8) — both the CPU build (build/bin) and the CUDA build (build-cuda/bin,
# sm_86) are compiled from that checkout. Everything we have evidence for ran
# on b9860: the T-W29 tokenmaxx bench (GBNF 100% on gemma3-4b-qat, 476 tok/s
# qwen3-4b @ N=8) and the integrator CUDA smoke (2026-07-05: -ngl 99 -np 8
# -c 32768, ~4.8 GB VRAM, 482 tok/s aggregate). The pin therefore records the
# benchmarked build, not the aspirational one. See
# benchmarks/tokenmaxx/REPORT.md "Runtime provenance".
LLAMA_CPP_PINNED_BUILD = "b9860"
LLAMA_CPP_PINNED_COMMIT = "fdb1db8"

# The source id written to routing.yaml by auto-registration.
_ROUTING_SOURCE_ID = "bundled_llamacpp"

# Path to routing.yaml (relative to this file at services/agent/).
_ROUTING_YAML = Path(__file__).resolve().parent / "routing.yaml"

# --------------------------------------------------------------------------- errors


class LocalRuntimeError(RuntimeError):
    """Raised when the local llama.cpp runtime cannot be started or reached."""


# --------------------------------------------------------------------------- dataclass


class RuntimeInfo:
    """Result of a successful ``ensure_running()`` call.

    Attributes
    ----------
    base_url:
        The base URL for OpenAI-compatible API calls (e.g. ``http://127.0.0.1:8765/v1``).
    launched:
        ``True`` if this call spawned the server process; ``False`` if it was already
        running.
    n_ctx:
        Actual context length served by the loaded model.  Populated by querying
        ``/props`` on the live server.  ``None`` if the endpoint is unreachable.
    tier:
        Tier name derived from ``n_ctx`` via ``tiers.detect_tier()``, or ``None``.
    server_path:
        Filesystem path to the llama-server binary, or empty string if unknown.
    model_path:
        Filesystem path to the GGUF model file, or empty string if unknown.
    """

    def __init__(self, raw: dict, n_ctx: Optional[int], tier: Optional[str]) -> None:
        self.base_url: str = raw.get("base_url", "")
        self.launched: bool = bool(raw.get("launched", False))
        self.n_ctx: Optional[int] = n_ctx
        self.tier: Optional[str] = tier
        self.server_path: str = raw.get("server", "")
        self.model_path: str = raw.get("model", "")
        # T-W31: which build variant the installer selected ("cuda"|"cpu"|
        # "unknown") and the GPU offload it launched with (None = server default).
        self.variant: str = raw.get("variant", "unknown")
        self.n_gpu_layers: Optional[int] = raw.get("n_gpu_layers")

    def as_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "launched": self.launched,
            "n_ctx": self.n_ctx,
            "tier": self.tier,
            "server": self.server_path,
            "model": self.model_path,
            "variant": self.variant,
            "n_gpu_layers": self.n_gpu_layers,
        }


# --------------------------------------------------------------------------- port helpers


def _pick_free_port() -> int:
    """Ask the OS for a free TCP port (bind then release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- props query


def get_served_n_ctx(base_url: str, *, timeout: float = 2.0) -> Optional[int]:
    """Query ``/props`` on the running llama-server and return the actual n_ctx.

    Returns ``None`` on any error (unreachable, missing field, etc.).
    """
    url = base_url.rstrip("/").removesuffix("/v1") + "/props"
    try:
        req = Request(url)
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback
            data = json.loads(resp.read())
        # llama-server /props key: n_ctx (integer)
        val = data.get("n_ctx")
        if isinstance(val, int) and val > 0:
            return val
        # Some older builds surface it under "model.n_ctx_train"
        model_info = data.get("model", {})
        if isinstance(model_info, dict):
            val = model_info.get("n_ctx_train")
            if isinstance(val, int) and val > 0:
                return val
    except Exception:  # noqa: BLE001 - best-effort
        pass
    return None


# --------------------------------------------------------------------------- lifecycle


def ensure_running(
    cfg: Optional[dict] = None,
    *,
    model_id: Optional[str] = None,
    auto_port: bool = True,
) -> RuntimeInfo:
    """Ensure a local llama.cpp server is reachable, starting one if needed.

    Parameters
    ----------
    cfg:
        Configuration dict forwarded to ``installer.llamacpp.ensure_server_running()``.
        Recognised keys: ``host``, ``port``, ``base_url``, ``server_path``,
        ``model_path``, ``ctx_size``, ``gpu_layers``, ``auto_start``,
        ``startup_timeout_sec``.
    model_id:
        If provided and ``cfg["model_path"]`` is not set, resolve the GGUF path from
        ``model_manager.get_model_path(model_id)`` before starting.
    auto_port:
        When ``True`` (default) and neither ``cfg["port"]`` nor ``cfg["base_url"]``
        are set, allocate a free OS port rather than defaulting to 1234.  This avoids
        collisions when multiple sessions run concurrently.

    Returns
    -------
    RuntimeInfo
        Populated with ``base_url``, ``launched``, ``n_ctx``, and ``tier``.

    Raises
    ------
    LocalRuntimeError
        If the server cannot be started (binary missing, model missing, timeout).
    """
    from installer import llamacpp  # services/installer — always present

    cfg = dict(cfg or {})

    # Inject a free port if the caller hasn't pinned one.
    if auto_port and not cfg.get("port") and not cfg.get("base_url"):
        cfg["port"] = _pick_free_port()

    # Resolve model path from model_manager if given.
    if model_id and not cfg.get("model_path"):
        try:
            from services.agent import model_manager  # noqa: PLC0415
            p = model_manager.get_model_path(model_id)
            if p:
                cfg["model_path"] = str(p)
        except Exception:  # noqa: BLE001 - optional dep
            pass

    # Delegate spawn/health to the existing installer module.
    try:
        raw = llamacpp.ensure_server_running(cfg)
    except RuntimeError as exc:
        raise LocalRuntimeError(str(exc)) from exc

    # Capture served n_ctx.
    base_url = raw.get("base_url", "")
    n_ctx = get_served_n_ctx(base_url) if base_url else None

    # Map to tier.
    tier: Optional[str] = None
    if n_ctx is not None:
        try:
            from services.agent.tiers import detect_tier  # noqa: PLC0415
            tier = detect_tier(n_ctx)
        except Exception:  # noqa: BLE001 - optional dep
            pass

    return RuntimeInfo(raw, n_ctx, tier)


# start / stop are thin aliases kept for symmetry with future daemon mode.

def start(cfg: Optional[dict] = None, **kwargs) -> RuntimeInfo:
    """Alias for ``ensure_running()`` — start the server and return its info."""
    return ensure_running(cfg, **kwargs)


def stop(base_url: str, *, timeout: float = 5.0) -> bool:
    """Ask the llama-server to shut down gracefully via ``POST /stop`` (if supported).

    Returns ``True`` if the server acknowledged the request, ``False`` otherwise.
    This is best-effort: llama-server added the ``/stop`` endpoint in build ~b3500.
    If unsupported, the caller should kill the process via the subprocess handle.
    """
    url = base_url.rstrip("/").removesuffix("/v1") + "/stop"
    try:
        req = Request(url, data=b"", method="POST")
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- routing


def register_routing_source(
    base_url: str,
    model_id: "str | list[str]",
    tier: Optional[str],
    *,
    routing_yaml: Optional[Path] = None,
    source_id: str = _ROUTING_SOURCE_ID,
) -> bool:
    """Idempotently append a bundled llama.cpp source to routing.yaml.

    Contract (§14): routing.yaml is user-owned.  This function:
    - Reads the current file.
    - Does nothing if a source with *source_id* already exists.
    - Appends a new entry with kind ``llamacpp`` if absent.
    - Writes atomically (temp file + rename) to avoid corruption.
    - Never modifies existing entries.

    Parameters
    ----------
    base_url:
        The server's base URL (e.g. ``http://127.0.0.1:8765/v1``).
    model_id:
        The model registry id (e.g. ``qwen3-4b``) or a list of ids — the T-W31
        provisioner registers BOTH bundled models (qwen3-4b + gemma3-4b-qat) so
        the W29 role-shape split is resolvable from routing.yaml.
    tier:
        Tier name string (e.g. ``local-24k``).  Falls back to ``cheap_local``
        if ``None`` (safe default for a 4B model).
    routing_yaml:
        Override path to routing.yaml.  Defaults to the canonical location.
    source_id:
        The source id to write (default: ``bundled_llamacpp``).

    Returns
    -------
    bool
        ``True`` if the file was modified, ``False`` if the source was already present.
    """
    yaml_path = routing_yaml or _ROUTING_YAML

    try:
        text = yaml_path.read_text(encoding="utf-8")
        data: dict = yaml.safe_load(text) or {}
    except FileNotFoundError:
        # routing.yaml doesn't exist yet — nothing to register into.
        return False

    sources: list = data.get("sources") or []

    # Idempotency check — don't touch if already registered.
    if any(isinstance(s, dict) and s.get("id") == source_id for s in sources):
        return False

    effective_tier = tier or "cheap_local"
    model_ids = [model_id] if isinstance(model_id, str) else list(model_id)

    new_source = {
        "id": source_id,
        "kind": "llamacpp",
        "base_url": base_url,
        "models": [
            {"id": mid, "tier": effective_tier} for mid in model_ids
        ],
        # Auto-registered on first model provision — user may edit freely.
        "_auto_registered": True,
    }
    sources.append(new_source)
    data["sources"] = sources

    # Atomic write: write to a temp file in the same directory, then rename.
    yaml_dir = yaml_path.parent
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=yaml_dir,
            suffix=".tmp",
            delete=False,
        ) as tf:
            tmp_path = Path(tf.name)
            yaml.safe_dump(data, tf, default_flow_style=False, allow_unicode=True)
        tmp_path.replace(yaml_path)
    except Exception as exc:  # noqa: BLE001
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise LocalRuntimeError(f"Failed to update routing.yaml: {exc}") from exc

    return True


# --------------------------------------------------------------------------- T-W29
# Optional GPU transformers backend + backend-selection seam.
#
# WHY THIS EXISTS (T-W29 tokenmaxx bench): the llama.cpp path (above) cannot run
# non-autoregressive / diffusion decode and has no PyTorch dependency. A second,
# EXTRAS-GUARDED backend lets us drive HuggingFace `transformers` models on the GPU
# (e.g. a diffusion LM, or an autoregressive model for cross-checking) without
# pulling torch into the gate venv. It is used by the bench and is the documented
# seam T-W27's gym cycle 2 reuses for quality grading (backend chosen by env/config,
# NOT a fork).
#
# HARD RULE honoured here: importing torch/transformers is deferred to call time and
# wrapped so that when the extras are absent (the default gate venv) every entry point
# fails cleanly with BackendUnavailable — no import-time crash, no all-skip pretense.
# The bench proves it by RUNNING against a real torch venv on this machine.
#
# torch/transformers venv (documented, kept OUT of the gate venv):
#     ~/.local/share/AI-CAD/torch-venv   (torch cu124 + transformers)
# Point the bench / a backend consumer at it via that interpreter, e.g.
#     ~/.local/share/AI-CAD/torch-venv/bin/python -m ...
#
# Backend selection seam (env-driven, config-overridable):
#     AICAD_LOCAL_BACKEND = "llamacpp" (default) | "transformers"
#     AICAD_TRANSFORMERS_MODEL = HF repo id or local path (transformers backend only)


class BackendUnavailable(LocalRuntimeError):
    """Raised when a requested backend's optional extras are not installed."""


def transformers_available() -> bool:
    """Return True iff torch + transformers import AND CUDA is visible.

    Never raises. Safe to call from the gate venv (returns False there).
    """
    try:
        import torch  # noqa: PLC0415
        import transformers  # noqa: PLC0415,F401
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001 - optional dep / no GPU
        return False


class TransformersBackend:
    """Minimal GPU text-generation backend over HuggingFace ``transformers``.

    Extras-guarded: constructing it imports torch/transformers lazily and raises
    ``BackendUnavailable`` if they are missing. This is the non-llama.cpp path used
    to bench diffusion / PyTorch models and the seam gym cycle 2 selects by config.

    This class deliberately keeps a tiny surface (load / generate / unload). It does
    NOT implement GBNF grammar constraint — transformers has no llama.cpp grammar
    engine — so structured output on this path is prompted-JSON + external validation
    (see ``services.agent.roles.validate_handoff``). That asymmetry is the key
    tokenmaxx finding this backend exists to measure.
    """

    def __init__(self, model_id: str, *, dtype: str = "auto",
                 device_map: str = "cuda", trust_remote_code: bool = False) -> None:
        if not transformers_available():
            raise BackendUnavailable(
                "transformers backend requires torch+transformers with CUDA. "
                "Install into a SEPARATE venv (e.g. ~/.local/share/AI-CAD/torch-venv) "
                "— never the gate venv."
            )
        self.model_id = model_id
        self.dtype = dtype
        self.device_map = device_map
        self.trust_remote_code = trust_remote_code
        self._model = None
        self._tok = None

    def load(self) -> "TransformersBackend":
        import torch  # noqa: PLC0415
        from transformers import (  # noqa: PLC0415
            AutoModelForCausalLM, AutoTokenizer,
        )
        dtype = None if self.dtype == "auto" else getattr(torch, self.dtype, None)
        self._tok = AutoTokenizer.from_pretrained(
            self.model_id, trust_remote_code=self.trust_remote_code)
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_id,
            torch_dtype=dtype if dtype is not None else "auto",
            device_map=self.device_map,
            trust_remote_code=self.trust_remote_code,
        )
        return self

    def generate(self, messages: list, *, max_new_tokens: int = 256,
                 temperature: float = 0.0) -> dict:
        """Chat-generate. Returns {text, prompt_tokens, completion_tokens, elapsed_s}."""
        import torch  # noqa: PLC0415
        if self._model is None:
            self.load()
        prompt = self._tok.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
        inputs = self._tok(prompt, return_tensors="pt").to(self._model.device)
        gen_kw: dict = {"max_new_tokens": max_new_tokens}
        if temperature and temperature > 0:
            gen_kw.update(do_sample=True, temperature=temperature)
        else:
            gen_kw.update(do_sample=False)
        t0 = time.perf_counter()
        with torch.no_grad():
            out = self._model.generate(**inputs, **gen_kw)
        elapsed = time.perf_counter() - t0
        prompt_len = inputs["input_ids"].shape[1]
        new_tokens = out[0][prompt_len:]
        text = self._tok.decode(new_tokens, skip_special_tokens=True)
        return {
            "text": text,
            "prompt_tokens": int(prompt_len),
            "completion_tokens": int(new_tokens.shape[0]),
            "elapsed_s": elapsed,
        }

    def unload(self) -> None:
        self._model = None
        self._tok = None
        try:
            import torch  # noqa: PLC0415
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def select_backend(cfg: Optional[dict] = None) -> str:
    """Return the local backend id to use: ``"llamacpp"`` (default) or
    ``"transformers"``.

    Resolution order (later wins): built-in default -> ``cfg["local_backend"]`` ->
    ``AICAD_LOCAL_BACKEND`` env. This is the documented seam (T-W29 DoD) that lets
    T-W27 gym cycle 2 pick a backend by config without forking the runtime.
    """
    import os as _os  # noqa: PLC0415
    backend = "llamacpp"
    if cfg and cfg.get("local_backend"):
        backend = str(cfg["local_backend"])
    env = _os.environ.get("AICAD_LOCAL_BACKEND")
    if env:
        backend = env
    if backend not in ("llamacpp", "transformers"):
        raise LocalRuntimeError(f"unknown local backend {backend!r}")
    return backend
