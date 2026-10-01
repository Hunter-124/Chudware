#!/usr/bin/env python3
"""Exercise compiled assets and RPC v2 without a provider turn or user profile."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


def smoke(binary: Path, version: str) -> dict:
    binary = binary.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="chudcode-runtime-smoke-") as temporary:
        root = Path(temporary)
        environment = {
            key: value for key, value in os.environ.items()
            if not (key.endswith(("_API_KEY", "_TOKEN")) or key.startswith(("PI_", "OMP_", "CHUDCODE_")))
        }
        environment.update({
            "HOME": str(root), "USERPROFILE": str(root),
            "APPDATA": str(root / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(root / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"), "PI_NOTIFICATIONS": "off",
        })
        for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            Path(environment[name]).mkdir(parents=True, exist_ok=True)
        actual = subprocess.run(
            [str(binary), "--version"], cwd=root, env=environment,
            text=True, capture_output=True, check=True, timeout=15,
        ).stdout.strip()
        if actual != f"Chudcode v{version}":
            raise RuntimeError(f"Unexpected product version: {actual!r}")
        bundle = subprocess.run(
            [str(binary), "--smoke-test"], cwd=root, env=environment,
            text=True, capture_output=True, timeout=180,
        )
        if bundle.returncode or "smoke-test: ok" not in bundle.stdout.splitlines():
            raise RuntimeError(f"Bundled worker/assets smoke failed: {bundle.stdout}\n{bundle.stderr}")
        frames = queue.Queue()
        stderr_path = root / "rpc-stderr.txt"
        with stderr_path.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(
                [str(binary), "--mode", "rpc", "--no-session", "--no-tools", "--no-lsp",
                 "--no-mcp", "--no-skills", "--no-rules", "--no-extensions", "--no-title"],
                cwd=root, env=environment, text=True, encoding="utf-8",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            )
            def read_frames():
                for line in process.stdout:
                    try:
                        frames.put(json.loads(line))
                    except json.JSONDecodeError:
                        frames.put(RuntimeError(f"Non-JSON RPC stdout: {line!r}"))
                frames.put(RuntimeError("RPC stdout closed before expected response"))

            reader = threading.Thread(target=read_frames, daemon=True)
            reader.start()

            def receive(predicate):
                deadline = time.monotonic() + 60
                while True:
                    try:
                        frame = frames.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty as error:
                        raise RuntimeError("Timed out waiting for RPC response") from error
                    if isinstance(frame, Exception):
                        raise frame
                    if predicate(frame):
                        return frame

            def request(command, **fields):
                process.stdin.write(json.dumps({"id": command, "type": command, **fields}) + "\n")
                process.stdin.flush()
                response = receive(lambda frame: frame.get("type") == "response" and frame.get("id") == command)
                if not response.get("success") or response.get("command") != command:
                    raise RuntimeError(f"RPC {command} failed: {response}")
                return response.get("data")

            try:
                ready = receive(lambda frame: frame.get("type") == "ready")
                if 2 not in ready.get("supportedProtocolVersions", []):
                    raise RuntimeError(f"RPC v2 not supported: {ready}")
                if request("negotiate_protocol", protocolVersion=2).get("protocolVersion") != 2:
                    raise RuntimeError("RPC protocol v2 negotiation returned wrong version")
                state = request("get_state")
                if state.get("isStreaming") is not False or state.get("messageCount") != 0:
                    raise RuntimeError(f"Fresh RPC session was not idle and empty: {state}")
                commands = request("get_available_commands")
                if not isinstance(commands, dict) or not isinstance(commands.get("commands"), list):
                    raise RuntimeError(f"RPC command discovery returned invalid data: {commands}")
                request("set_host_tools", tools=[])
                request("set_subagent_subscription", level="events")
                request("get_messages")
            except Exception as error:
                raise RuntimeError(f"{error}; runtime stderr: {stderr_path.read_text(encoding='utf-8')}") from error
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                reader.join(timeout=5)
                process.stdin.close()
                process.stdout.close()
        with binary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {
            "version_output": actual, "sha256": digest, "bytes": binary.stat().st_size,
            "bundle_smoke": "smoke-test: ok", "rpc_protocol": 2,
            "rpc_commands": ["negotiate_protocol", "get_state", "get_available_commands",
                             "set_host_tools", "set_subagent_subscription", "get_messages"],
            "provider_turns": 0,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.binary, args.version), sort_keys=True))


if __name__ == "__main__":
    main()
