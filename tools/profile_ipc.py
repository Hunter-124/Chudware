"""Profile daemon_bridge framing/JSON decoding over a private socketpair, without hostd.

Examples (sizes are exact bytes per newline-terminated JSON reply):
  python3 tools/profile_ipc.py --sizes 524288 4194304 16777216 --repeats 7
  python3 tools/profile_ipc.py --module-file scratch/performance-2026-10-15/daemon_bridge_before.py
  python3 -m cProfile -o scratch/ipc.prof tools/profile_ipc.py --output scratch/ipc.json

Only stdlib modules are required. No discovery, daemon connection, subprocess,
external network, credentials or document operations are used. Encoded reply
construction and full content/event-order validation are outside timed regions.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import platform
import queue
import socket
import statistics
import sys
import threading
import time
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _load_bridge(module_file):
    sys.path.insert(0, str(_ROOT))
    if module_file is None:
        return importlib.import_module("services.mcp.daemon_bridge")
    # The original snapshot keeps its package context (including relative imports),
    # without replacing the checkout module or changing any on-disk source files.
    importlib.import_module("services.mcp")
    name = "services.mcp._profile_ipc_bridge"
    spec = importlib.util.spec_from_file_location(name, module_file)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load bridge module: {module_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _encode(message):
    return json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _reply(size, sequence, values):
    result = {"sequence": sequence, "values": values, "padding": ""}
    reply = {"id": sequence + 1, "result": result}
    padding = size - len(_encode(reply))
    if padding < 0:
        raise ValueError(f"reply size {size} is too small for its content")
    result["padding"] = "x" * padding
    return result, _encode(reply)


def _serve(peer, jobs, failures):
    try:
        with peer, peer.makefile("rb") as requests:
            while True:
                job = jobs.get()
                if job is None:
                    return
                sequence, event, reply = job
                request = requests.readline()
                if not request:
                    raise RuntimeError("client closed before sending its request")
                expected = {
                    "id": sequence + 1,
                    "method": "ipc_profile",
                    "params": {"sequence": sequence},
                }
                if json.loads(request) != expected:
                    raise RuntimeError("request contents/order did not match the workload")
                peer.sendall(event)
                peer.sendall(reply)
    except BaseException as error:
        failures.put(error)


def _p95(samples):
    return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]


def _measure_size(bridge, size, repeats):
    # Dense deterministic numeric arrays exercise JSON parsing as well as byte
    # framing; final string padding makes each reply exactly the requested size.
    block = list(range(256))
    base_bytes = len(_encode({"id": repeats, "result": {
        "sequence": repeats - 1, "values": [], "padding": "",
    }}))
    if size < base_bytes:
        raise ValueError(f"--sizes entries must be at least {base_bytes} bytes for these repeats")
    block_bytes = len(json.dumps(block, separators=(",", ":"))) - 1
    values = block * ((size - base_bytes) // block_bytes)
    client = bridge.DaemonClient()
    client_socket, peer = socket.socketpair()
    # Bounds a broken profiling peer, not production RPC behavior.
    client_socket.settimeout(60)
    peer.settimeout(60)
    client._sock = client_socket
    jobs, failures = queue.Queue(), queue.Queue()
    sender = threading.Thread(target=_serve, args=(peer, jobs, failures), daemon=True)
    sender.start()
    raw = []
    complete = False
    try:
        for sequence in range(repeats):
            expected, reply = _reply(size, sequence, values)
            event = {"event": "profile.sequence", "data": {"sequence": sequence}}
            event_wire = _encode(event)
            jobs.put((sequence, event_wire, reply))
            wall_start = time.perf_counter_ns()
            cpu_start = time.process_time_ns()
            client_cpu_start = time.thread_time_ns()
            result = client._call_raw("ipc_profile", {"sequence": sequence})
            client_cpu_ns = time.thread_time_ns() - client_cpu_start
            cpu_ns = time.process_time_ns() - cpu_start
            wall_ns = time.perf_counter_ns() - wall_start
            if result != expected:
                raise RuntimeError(f"reply contents/order mismatch at repeat {sequence + 1}")
            if client.poll_events() != [event]:
                raise RuntimeError(f"event contents/order mismatch at repeat {sequence + 1}")
            if not failures.empty():
                raise failures.get_nowait()
            raw.append({
                "repeat": sequence + 1,
                "reply_bytes": len(reply),
                "event_bytes": len(event_wire),
                "received_bytes": len(reply) + len(event_wire),
                "wall_seconds": wall_ns / 1e9,
                "cpu_seconds": cpu_ns / 1e9,
                "client_cpu_seconds": client_cpu_ns / 1e9,
            })
        complete = True
    finally:
        jobs.put(None)
        if not complete:
            client.close()
            # Wake a failed peer blocked in read/send. These sockets belong only
            # to this process, never to a running user session.
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        sender.join(5)
        client.close()
        peer.close()
        if sender.is_alive():
            raise RuntimeError("profiling peer failed to stop")
    if not failures.empty():
        raise failures.get_nowait()
    report = {"reply_bytes": size, "repeats": repeats, "values_per_reply": len(values), "raw": raw}
    for metric in ("wall_seconds", "cpu_seconds", "client_cpu_seconds"):
        samples = [row[metric] for row in raw]
        report[f"median_{metric}"] = statistics.median(samples)
        report[f"p95_{metric}"] = _p95(samples)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=_positive_int, nargs="+", default=[524288, 4194304, 16777216],
                        metavar="BYTES", help="exact JSON reply sizes in bytes")
    parser.add_argument("--repeats", type=_positive_int, default=7)
    parser.add_argument("--module-file", type=Path, help="load an original daemon_bridge.py snapshot")
    parser.add_argument("--output", type=Path, help="also write the JSON report to this file")
    args = parser.parse_args(argv)
    bridge = _load_bridge(args.module_file)
    module_path = Path(bridge.__file__).resolve()
    report = {
        "schema": "chudware.ipc_profile.v1",
        "transport": "socket.socketpair",
        "entrypoint": "DaemonClient._call_raw",
        "module_file": str(module_path),
        "module_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_scope": "process CPU includes client and peer; client_cpu is calling-thread CPU",
        "timed_scope": "request serialization, socketpair transport, event/reply framing and JSON decoding",
        "excluded_scope": "module import, reply construction, full content/order validation",
        "p95_method": "nearest rank",
        "sizes": [_measure_size(bridge, size, args.repeats) for size in args.sizes],
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
