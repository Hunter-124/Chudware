"""Managed llama.cpp runtime support.

AI-CAD talks to local models through OpenAI-compatible chat completions. llama.cpp's
``llama-server`` exposes that API, so the app can either connect to a user-managed server or launch
a bundled headless server from ``%APPDATA%\\AI-CAD\\llamacpp``.

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

from engine import paths

REPO = Path(__file__).resolve().parent.parent
APP_LLAMA = paths.LLAMACPP
SERVER_NAMES = ("llama-server.exe", "server.exe", "llama-server", "server")


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


def find_server(root: Path | None = None) -> Path | None:
    base = root or APP_LLAMA
    for name in SERVER_NAMES:
        p = base / name
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

    cfg keys: base_url, server_path, model_path, auto_start, port, host, ctx_size, gpu_layers.
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

    args = [str(server), "--host", host, "--port", str(port), "--model", str(model)]
    if cfg.get("ctx_size"):
        args += ["--ctx-size", str(int(cfg["ctx_size"]))]
    if cfg.get("gpu_layers") is not None:
        args += ["--n-gpu-layers", str(int(cfg["gpu_layers"]))]

    log_dir = paths.ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "llamacpp-server.log", "ab")
    subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=str(server.parent),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    deadline = time.time() + float(cfg.get("startup_timeout_sec", 20))
    while time.time() < deadline:
        if _http_models_ready(base_url):
            return {"status": "ready", "base_url": base_url, "launched": True,
                    "server": str(server), "model": str(model)}
        time.sleep(0.25)
    raise RuntimeError(
        f"llama.cpp server was launched but did not become ready at {base_url}. "
        f"See {log_dir / 'llamacpp-server.log'}."
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Install/check/launch AI-CAD's managed llama.cpp server")
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
