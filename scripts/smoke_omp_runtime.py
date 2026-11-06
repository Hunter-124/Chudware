#!/usr/bin/env python3
"""Smoke the pinned OMP CLI, SDK session and CAD extension without a provider turn.

Run in a separate process: the script replaces its environment with a private,
credential-free profile for the lifetime of the smoke.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile


def smoke(binary: Path, version: str, *, cad_schema: bool = False) -> dict:
    binary = binary.expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="omp-runtime-smoke-") as temporary:
        root = Path(temporary)
        # An allowlist rather than suffix filtering prevents inheriting ambient provider
        # credentials, user OMP settings, proxy credentials or application overrides.
        environment = {key: os.environ[key] for key in (
            "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
            "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
        ) if key in os.environ}
        environment.update({
            "HOME": str(root), "USERPROFILE": str(root),
            "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "local"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"), "CHUDWARE_OMP_BIN": str(binary),
            "CHUDWARE_PROJECT_DIR": str(root), "CHUDWARE_PROJECT_SAVE_DIR": str(root),
        })
        for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            Path(environment[name]).mkdir(parents=True, exist_ok=True)
        actual = subprocess.run([str(binary), "--version"], cwd=root, env=environment,
                                text=True, capture_output=True, check=True, timeout=15).stdout.strip()
        if actual != f"omp/{version}":
            raise RuntimeError(f"Unexpected OMP version: {actual!r}")
        os.environ.clear()
        os.environ.update(environment)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from services.agent.omp_host import OmpSessionHost
        from services.agent.omp_tools import build_host_tools
        from services.agent.tool_categories import MAIN_CHAT_CORE_TOOLS

        context = {"allowed_tools": list(MAIN_CHAT_CORE_TOOLS),
                   "categories": [], "project_dir": str(root)}
        bindings = build_host_tools(context) if cad_schema else []
        host = OmpSessionHost()
        session = None
        (root / "sessions").mkdir()
        try:
            session = host._get_or_create_session(
                "smoke", {"workspace_dir": str(root), "omp_session_dir": str(root / "sessions"),
                          "memory_backend": "off", "host_tools": bindings}, None)
            # Initial registration and this SDK command are handled by the real
            # extension's session_start handler, not by a fabricated RPC command.
            session.client._omp_bridge.wait_tools(timeout=60)
            policy = session.client._omp_bridge.call("settings_get", {}, timeout=60)
            if not isinstance(policy, dict) or not isinstance(policy.get("compaction"), dict):
                raise RuntimeError("Extension did not return its actual compaction settings")
            state = host.session_call("smoke", "get_state")
            if not state.get("ok") or state.get("isStreaming") is not False:
                raise RuntimeError(f"Fresh OMP session is not idle: {state!r}")
            if state.get("messageCount") != 0:
                raise RuntimeError(f"Fresh OMP session is not empty: {state!r}")
            dispatched = False
            if cad_schema:
                bridge = session.client._omp_bridge
                started = bridge._handle("tool_start", {
                    "name": "list_tool_categories", "arguments": {"query": "cad_guide"},
                    "context": {"agent_id": "smoke", "session_id": "smoke",
                                "tool_call_id": "smoke-discovery"},
                })
                if "id" not in started:
                    raise RuntimeError(f"CAD bridge rejected dispatch: {started!r}")
                deadline = time.monotonic() + 30
                while True:
                    response = bridge._handle("tool_poll", {"id": started["id"]})
                    if response.get("done"):
                        break
                    if response.get("error") or time.monotonic() >= deadline:
                        raise RuntimeError(f"CAD bridge dispatch did not complete: {response!r}")
                    time.sleep(0.05)
                envelope = response.get("result")
                result = envelope.get("details") if isinstance(envelope, dict) else None
                if response.get("error") or not isinstance(result, dict) or not any(
                    item.get("name") == "cad_guide" for item in result.get("tools", [])
                    if isinstance(item, dict)
                ):
                    raise RuntimeError(f"CAD bridge dispatch failed: {response!r}")
                dispatched = True
            with binary.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            return {"version": actual, "binary_sha256": digest, "session_idle": True,
                    "extension_settings": True, "cad_tool_count": len(bindings),
                    "cad_registry_dispatch": dispatched,
                    "provider_prompt_executed": False}
        finally:
            if session is not None:
                host._release_live_session(session)
            host.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omp-bin", type=Path, required=True, help="digest-verified official OMP executable")
    parser.add_argument("--version", default="18.13.3")
    parser.add_argument("--cad-schema", action="store_true", help="publish CAD tools and dispatch registry discovery")
    args = parser.parse_args()
    print(json.dumps(smoke(args.omp_bin, args.version, cad_schema=args.cad_schema), sort_keys=True))


if __name__ == "__main__":
    main()
