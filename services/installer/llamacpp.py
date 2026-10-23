"""Managed llama.cpp runtime support.

Chudware talks to local models through OpenAI-compatible chat completions. llama.cpp's
``llama-server`` exposes that API, so the app can either connect to a user-managed server or launch
a bundled headless server from ``%APPDATA%\\Chudware\\llamacpp``.

This module deliberately does not download binaries from the internet. Packaging is clean and
auditable: if a release bundle includes a ``llamacpp`` folder containing ``llama-server.exe`` (or
``server.exe``), provisioning copies it into AppData. If it is absent, status reports "missing" with
instructions instead of hiding the dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

from services import paths

REPO = Path(__file__).resolve().parent.parent
APP_LLAMA = paths.LLAMACPP
SERVER_NAMES = ("llama-server.exe", "server.exe", "llama-server", "server")

# ---------------------------------------------------------------------------
# GPU / build-variant selection
#
# The installed runtime at APP_LLAMA may carry per-variant build trees produced
# from the pinned source checkout (tag b9860, commit fdb1db8):
#
#   APP_LLAMA/build-cuda/bin/llama-server   GGML_CUDA=ON  (sm_86; measured with
#                                           gemma3-4b-qat -ngl 99 -np 8 -c 32768
#                                           -> ~4.8 GB VRAM, 482 tok/s aggregate
#                                           vs 332 CPU)
#   APP_LLAMA/build/bin/llama-server        GGML_CUDA=OFF (CPU-only)
#   APP_LLAMA/llama-server[.exe]            flat legacy layout (variant unknown)
#
# When an NVIDIA GPU is present the CUDA variant is preferred; otherwise the
# CPU build is used (graceful fallback — a CUDA binary without a driver would
# fail to load libggml-cuda). Detection is env-overridable for CI and users:
#
#   CHUDWARE_LLAMA_VARIANT=cuda|cpu   force a variant (wins over detection)
# ---------------------------------------------------------------------------

_VARIANT_ENV = "CHUDWARE_LLAMA_VARIANT"


def cuda_available() -> bool:
    """Best-effort NVIDIA GPU probe: ``nvidia-smi -L`` exits 0 and lists a GPU.

    Env override: ``CHUDWARE_LLAMA_VARIANT=cuda`` forces True, ``=cpu`` forces
    False. Never raises; absence of the tool means no usable GPU.
    """
    forced = os.environ.get(_VARIANT_ENV, "").strip().lower()
    if forced == "cuda":
        return True
    if forced == "cpu":
        return False
    try:
        out = subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5,
        )
        return out.returncode == 0 and "GPU" in (out.stdout or "")
    except Exception:  # noqa: BLE001 - no nvidia-smi / sandbox / timeout
        return False


def _variant_dirs(base: Path, prefer_cuda: bool) -> list[Path]:
    """Ordered server search dirs for *base* given the GPU preference."""
    cuda_bin = base / "build-cuda" / "bin"
    cpu_bin = base / "build" / "bin"
    if prefer_cuda:
        return [cuda_bin, cpu_bin, base]
    return [cpu_bin, base]


def server_variant(server: Path | None) -> str:
    """Classify a discovered server binary path: ``cuda`` | ``cpu`` | ``unknown``."""
    if server is None:
        return "unknown"
    parts = {p.name for p in server.parents}
    if "build-cuda" in parts:
        return "cuda"
    if "build" in parts:
        return "cpu"
    return "unknown"


def _expand_path(value: object) -> Path:
    """Expand user/env vars before turning config text into a filesystem path."""
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def _candidate_bundle_dirs() -> list[Path]:
    return [
        REPO / "llamacpp",
        REPO.parent / "llamacpp",
        REPO / "third_party" / "llamacpp",
        REPO / "vendor" / "llamacpp",
        Path(sys.executable).resolve().parent / "llamacpp",
        Path(sys.executable).resolve().parent.parent / "llamacpp",
        Path.cwd() / "llamacpp",
        Path.cwd().parent / "llamacpp",
    ]


def _candidate_model_dirs(bundle_dir: Path | None = None) -> list[Path]:
    bases = [REPO / "models", REPO.parent / "models", Path.cwd() / "models", Path.cwd().parent / "models"]
    if bundle_dir:
        bases.insert(0, bundle_dir.parent / "models")
        bases.insert(0, bundle_dir / "models")
    return bases


def find_server(root: Path | None = None, *, prefer_cuda: bool | None = None) -> Path | None:
    """Locate a llama-server binary under *root* (default: the installed runtime).

    Searches the per-variant build trees first (``build-cuda/bin`` when an
    NVIDIA GPU is present, else ``build/bin``), then the flat legacy layout.
    *prefer_cuda* overrides GPU auto-detection (used by tests / callers that
    already probed).
    """
    base = root or APP_LLAMA
    if prefer_cuda is None:
        prefer_cuda = cuda_available()
    for d in _variant_dirs(base, prefer_cuda):
        for name in SERVER_NAMES:
            p = d / name
            if p.exists():
                return p
    return None


def find_default_model() -> Path | None:
    if not paths.MODELS.exists():
        return None
    models = sorted(paths.MODELS.glob("*.gguf"))
    return models[0] if models else None


def install_from_bundle(bundle_dir: Path | None = None, force: bool = False) -> dict:
    """Copy a packaged llama.cpp folder into AppData if present."""
    srcs = [bundle_dir] if bundle_dir else _candidate_bundle_dirs()
    src = next((p for p in srcs if p and find_server(p)), None)
    if src is None:
        return {"installed": False, "reason": "no bundled llama.cpp server found"}
    if force and APP_LLAMA.exists():
        shutil.rmtree(APP_LLAMA, ignore_errors=True)
    APP_LLAMA.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, APP_LLAMA, dirs_exist_ok=True)
    model_src = next((p for p in _candidate_model_dirs(src) if p.exists()), None)
    if model_src:
        paths.MODELS.mkdir(parents=True, exist_ok=True)
        shutil.copytree(model_src, paths.MODELS, dirs_exist_ok=True)
    server = find_server(APP_LLAMA)
    return {"installed": bool(server), "path": str(APP_LLAMA), "server": str(server) if server else "",
            "models_dir": str(paths.MODELS), "models_copied": bool(model_src)}


def status() -> dict:
    server = find_server(APP_LLAMA)
    model = find_default_model()
    return {
        "root": str(APP_LLAMA),
        "installed": server is not None,
        "server": str(server) if server else "",
        "variant": server_variant(server),
        "cuda_available": cuda_available(),
        "models_dir": str(paths.MODELS),
        "default_model": str(model) if model else "",
    }


def _http_models_ready(base_url: str) -> bool:
    try:
        req = Request(base_url.rstrip("/") + "/models")
        with urlopen(req, timeout=0.5) as r:  # noqa: S310 - loopback managed by caller
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def ensure_server_running(cfg: dict | None = None) -> dict:
    """Ensure the configured llama.cpp server is reachable, launching it if configured.

    cfg keys: base_url, server_path, model_path, auto_start, port, host, ctx_size,
    gpu_layers, parallel, startup_timeout_sec.

    Variant-aware defaults: when the discovered binary is the CUDA build and the
    caller did not set them, ``gpu_layers`` defaults to 99 (full offload) and
    ``parallel``/``ctx_size`` default to 8/32768 — the configuration verified on an
    RTX 3080 Ti (health ok, ~4.8 GB VRAM, GBNF valid, 482 tok/s aggregate).
    Explicit cfg values always win.
    """
    cfg = cfg or {}
    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 1234)
    base_url = str(cfg.get("base_url") or f"http://{host}:{port}/v1").rstrip("/")
    auto_start = bool(cfg.get("auto_start", True))

    if _http_models_ready(base_url):
        return {"status": "ready", "base_url": base_url, "launched": False}
    if not auto_start:
        return {"status": "not_running", "base_url": base_url, "launched": False}

    server = _expand_path(cfg.get("server_path")) if cfg.get("server_path") else find_server(APP_LLAMA)
    if not server or not server.exists():
        installed = install_from_bundle()
        server_s = installed.get("server")
        server = Path(server_s) if server_s else None
    if not server or not server.exists():
        raise RuntimeError(
            "llama.cpp server is not installed. Put a llama.cpp release folder containing "
            "llama-server.exe under the app bundle as 'llamacpp', or install it to "
            f"{APP_LLAMA}, then try again."
        )

    model = _expand_path(cfg.get("model_path")) if cfg.get("model_path") else find_default_model()
    if model and not model.exists() and model.name.lower() == "model.gguf":
        model = find_default_model()
    if not model or not model.exists():
        raise RuntimeError(
            "No GGUF model configured for llama.cpp. Set config['llamacpp']['model_path'] or place "
            f"a .gguf file in {paths.MODELS}."
        )

    variant = server_variant(server)
    ctx_size = cfg.get("ctx_size")
    gpu_layers = cfg.get("gpu_layers")
    parallel = cfg.get("parallel")
    if variant == "cuda":
        # Shipped CUDA defaults = the integrator-verified smoke configuration
        # (b9860 build-cuda, -ngl 99 -np 8 -c 32768). User cfg wins when set.
        if gpu_layers is None:
            gpu_layers = 99
        if parallel is None and ctx_size is None:
            parallel, ctx_size = 8, 32768

    args = [str(server), "--host", host, "--port", str(port), "--model", str(model)]
    if ctx_size:
        args += ["--ctx-size", str(int(ctx_size))]
    if gpu_layers is not None:
        args += ["--n-gpu-layers", str(int(gpu_layers))]
    if parallel:
        args += ["--parallel", str(int(parallel))]

    log_dir = paths.ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "llamacpp-server.log", "ab")
    # Provenance line so the log always records which variant/offload was chosen.
    log.write(
        (f"[chudware] launching llama-server variant={variant} "
         f"n_gpu_layers={gpu_layers} parallel={parallel} ctx_size={ctx_size} "
         f"server={server}\n").encode()
    )
    log.flush()
    subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=str(server.parent),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    deadline = time.time() + float(cfg.get("startup_timeout_sec", 20))
    while time.time() < deadline:
        if _http_models_ready(base_url):
            return {"status": "ready", "base_url": base_url, "launched": True,
                    "server": str(server), "model": str(model),
                    "variant": variant,
                    "n_gpu_layers": gpu_layers, "parallel": parallel,
                    "ctx_size": ctx_size}
        time.sleep(0.25)
    raise RuntimeError(
        f"llama.cpp server was launched but did not become ready at {base_url}. "
        f"See {log_dir / 'llamacpp-server.log'}."
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Install/check/launch Chudware's managed llama.cpp server")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--bundle-dir", type=Path)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    if args.install:
        print(json.dumps(install_from_bundle(args.bundle_dir, args.force), indent=2))
        return 0
    if args.launch:
        print(json.dumps(ensure_server_running(), indent=2))
        return 0
    print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    os.chdir(REPO)
    raise SystemExit(main(sys.argv[1:]))
