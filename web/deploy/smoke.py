#!/usr/bin/env python3
"""Exercise a running isolated browser deployment, not a mocked/native-only path.

Requires httpx==0.28.1 and websockets==16.1. No daemon or container is started by
this command. Use --keep --receipt FILE, recreate the deployment without deleting
its volumes, then use --resume FILE to prove saved-file persistence and clean up.
Only this run's UUID-named document/file is touched; recents/chat are not changed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import time
from urllib.parse import urlsplit
import uuid

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


class SmokeFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


class Engine:
    def __init__(self, socket, timeout):
        self.socket = socket
        self.timeout = timeout
        self.next_id = 0

    async def call(self, method, params=None):
        self.next_id += 1
        ident = self.next_id
        await self.socket.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        async with asyncio.timeout(self.timeout):
            while True:
                frame = json.loads(await self.socket.recv())
                require(isinstance(frame, dict), "engine sent a non-object frame")
                if "event" in frame:
                    continue  # Events can share the request id; they are not replies.
                require(frame.get("id") == ident, f"unexpected engine reply: {frame}")
                require("error" not in frame and frame.get("ok", True) is not False,
                        f"{method} failed: {frame}")
                result = frame.get("result")
                require(isinstance(result, dict) and result.get("ok", True) is not False,
                        f"{method} returned a failed/non-object result: {result}")
                return result


async def open_engine(url, timeout):
    deadline = time.monotonic() + timeout
    ws_url = ("wss" if url.startswith("https:") else "ws") + url[url.index(":"):] + "/engine"
    last_error = "engine never became ready"
    while time.monotonic() < deadline:
        socket = None
        try:
            socket = await connect(ws_url, origin=url, proxy=None, open_timeout=10,
                                   close_timeout=5, max_size=16 * 1024 * 1024)
            engine = Engine(socket, timeout)
            while time.monotonic() < deadline:
                hello = await engine.call("hello", {"client": "gui", "proto": 1})
                require(hello.get("state") in {"loading", "ready"}, f"engine load failed: {hello}")
                if hello["state"] == "ready":
                    require(isinstance(hello.get("workspace_key"), str), "hello omitted workspace identity")
                    return engine, hello["workspace_key"]
                await asyncio.sleep(0.5)
        except (OSError, ConnectionClosed, TimeoutError) as exc:
            last_error = str(exc)
        except BaseException:
            if socket is not None:
                await socket.close()
            raise
        if socket is not None:
            await socket.close()
        await asyncio.sleep(0.5)
    raise SmokeFailure(f"engine not ready after {timeout}s: {last_error}")


def check_headers(response, asset=False):
    require(response.headers.get("x-content-type-options") == "nosniff", "missing nosniff header")
    require(response.headers.get("cross-origin-opener-policy") == "same-origin", "missing COOP header")
    csp = response.headers.get("content-security-policy", "")
    require("object-src 'none'" in csp, "missing restrictive CSP")
    if asset:
        require(response.headers.get("cross-origin-embedder-policy") == "require-corp", "missing COEP header")
        require("'wasm-unsafe-eval'" in csp and "worker-src 'self' blob:" in csp,
                "CSP cannot load the WASM/worker client")
        require("no-cache" in response.headers.get("cache-control", ""), "bundle must revalidate together")


async def check_http(client, url, filename, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            response = await client.get("/healthz")
            if response.status_code == 200:
                break
        except httpx.TransportError:
            pass
        require(time.monotonic() < deadline, "gateway health endpoint did not become available")
        await asyncio.sleep(0.5)
    require(response.json() == {"ok": True}, "unexpected health response")
    check_headers(response)
    for path, mime, magic in (
        ("/app/", "text/html", None),
        ("/app/Chudware.js", "application/javascript", None),
        ("/app/Chudware.wasm", "application/wasm", b"\x00asm"),
        ("/app/Chudware.data", "application/octet-stream", None),
    ):
        async with client.stream("GET", path, headers={"Accept-Encoding": "identity"}) as asset:
            require(asset.status_code == 200, f"{path}: HTTP {asset.status_code}")
            check_headers(asset, asset=True)
            require(asset.headers.get("content-type", "").split(";")[0] == mime, f"{path}: wrong MIME type")
            total, prefix = 0, b""
            async for chunk in asset.aiter_bytes():
                total += len(chunk)
                if len(prefix) < 4:
                    prefix += chunk[:4 - len(prefix)]
            require(total > 0 and (magic is None or prefix == magic), f"{path}: empty/invalid asset")
    hostile = await client.get("/healthz", headers={"Host": "chudware-smoke.invalid"})
    require(hostile.status_code == 400, f"hostile Host was not refused: HTTP {hostile.status_code}")
    hostile = await client.post("/workspace/delete", json={"path": filename},
                                headers={"Origin": "https://chudware-smoke.invalid"})
    require(hostile.status_code == 403, f"cross-origin write was not refused: HTTP {hostile.status_code}")
    ws_url = ("wss" if url.startswith("https:") else "ws") + url[url.index(":"):] + "/engine"
    try:
        async with connect(ws_url, origin="https://chudware-smoke.invalid", proxy=None, open_timeout=10):
            raise SmokeFailure("cross-origin WebSocket handshake was accepted")
    except InvalidStatus as exc:
        require(exc.response.status_code == 403,
                f"cross-origin WebSocket refusal was not HTTP 403: {exc.response.status_code}")
    print("PASS HTTP health, complete assets/headers, Host and Origin refusal", flush=True)


def check_box(result):
    tree = result.get("tree", {})
    boxes = [item for item in tree.get("features", []) if item.get("type") == "box"]
    require(len(boxes) == 1, "saved document does not contain exactly one box feature")
    for dimension in ("length", "width", "height"):
        value = boxes[0].get("params", {}).get(dimension)
        if isinstance(value, dict):
            value = value.get("value")
        require(value == 10, f"box {dimension} changed: {value}")
    statuses = result.get("rebuild", {}).get("feature_status", [])
    require(statuses and all(item.get("ok") is True for item in statuses),
            f"native rebuild did not succeed: {statuses}")
    require(result.get("tess", {}).get("bodies"), "native rebuild produced no bodies")


async def download(client, filename):
    response = await client.get("/workspace/download", params={"path": filename})
    response.raise_for_status()
    require(response.content, "saved document download is empty")
    return hashlib.sha256(response.content).hexdigest()


async def mesh(client, workspace, doc_id):
    response = await client.get("/files/aicm", params={"workspace_key": workspace, "doc_id": doc_id})
    response.raise_for_status()
    data = response.content
    require(len(data) > 12 and data[:4] == b"AICM", "authorized native mesh download is not AICM")
    version, chunks = struct.unpack_from("<II", data, 4)
    require(version == 1 and chunks > 0, "native mesh has no supported geometry chunks")


async def run(args):
    parsed = urlsplit(args.url)
    require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
            and not parsed.password and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
            "URL must be the deployment's HTTP(S) origin, without credentials or a path")
    url = args.url.rstrip("/")
    receipt = None
    if args.resume:
        receipt = json.loads(args.resume.read_text(encoding="utf-8"))
        require(receipt.get("version") == 1 and receipt.get("url") == url, "receipt belongs to another deployment")
        filename = receipt.get("path", "")
        require(isinstance(filename, str) and re.fullmatch(r"chudware-smoke-[0-9a-f]{32}\.step\+", filename),
                "receipt does not name a smoke-owned file")
        require(re.fullmatch(r"[0-9a-f]{64}", receipt.get("sha256", "")), "receipt hash is invalid")
    else:
        filename = f"chudware-smoke-{uuid.uuid4().hex}.step+"
    require(not args.receipt or not args.receipt.exists(), "receipt output already exists; choose a new path")
    engine = None
    documents = set()
    owned = False
    saved_hash = receipt["sha256"] if receipt else None
    async with httpx.AsyncClient(base_url=url, headers={"Origin": url}, timeout=args.timeout,
                                 follow_redirects=False, trust_env=False) as client:
        try:
            await check_http(client, url, filename, args.timeout)
            existing = await client.get("/workspace/download", params={"path": filename})
            if receipt:
                existing.raise_for_status()
                require(hashlib.sha256(existing.content).hexdigest() == saved_hash,
                        "persisted file differs from pre-restart receipt; leaving it untouched")
            else:
                require(existing.status_code == 404, "unique smoke filename already exists; refusing to overwrite")
            owned = True
            engine, workspace = await open_engine(url, args.timeout)
            if not receipt:
                created = await engine.call("doc_new", {"name": filename[:-6], "kind": "part"})
                doc_id = created["doc_id"]
                documents.add(doc_id)
                result = await engine.call("doc_apply_ops", {"doc_id": doc_id, "ops": [{
                    "op": "add_feature", "feature": {"type": "box", "name": "smoke-box",
                    "params": {"length": 10, "width": 10, "height": 10}, "refs": {}}}]})
                check_box(result)
                await mesh(client, workspace, doc_id)
                await engine.call("doc_save", {"doc_id": doc_id, "path": filename})
                saved_hash = await download(client, filename)
                await engine.call("doc_close", {"doc_id": doc_id})
                documents.remove(doc_id)
                print("PASS real native box build, authorized mesh, save/download", flush=True)
            reopened = await engine.call("doc_open", {"path": filename})
            doc_id = reopened["doc_id"]
            documents.add(doc_id)
            check_box(reopened)
            await mesh(client, workspace, doc_id)
            require(await download(client, filename) == saved_hash, "reopening changed the saved file")
            # Registered in services/parts/gui_rpc.py and services/worker_main.py:
            # this is a real general Python worker request, not native ping or AI.
            categories = await engine.call("parts_list_categories")
            require(any(item.get("id") == "catalog" for item in categories.get("categories", [])),
                    "non-AI Python parts worker did not return its real category catalog")
            print("PASS persisted file reopen/mesh and non-AI Python worker", flush=True)
            if args.keep:
                if args.receipt:
                    with args.receipt.open("x", encoding="utf-8") as output:
                        json.dump({"version": 1, "url": url, "path": filename, "sha256": saved_hash}, output)
                        output.write("\n")
                print(f"KEEP {filename}; restart/recreate without deleting volumes, then run --resume {args.receipt or args.resume}",
                      flush=True)
        finally:
            if engine is not None:
                try:
                    for doc_id in documents:
                        await engine.call("doc_close", {"doc_id": doc_id})
                finally:
                    await engine.socket.close()
            if owned and not args.keep:
                current = await client.get("/workspace/download", params={"path": filename})
                if current.status_code != 404:
                    current.raise_for_status()
                    require(saved_hash is not None and hashlib.sha256(current.content).hexdigest() == saved_hash,
                            f"cleanup refused a changed/unverified file: {filename}")
                    deleted = await client.post("/workspace/delete", json={"path": filename})
                    deleted.raise_for_status()
                    print(f"CLEANED {filename}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="isolated deployment origin, e.g. http://127.0.0.1:8300")
    parser.add_argument("--timeout", type=float, default=180, help="startup and per-RPC timeout in seconds")
    parser.add_argument("--keep", action="store_true", help="retain only the saved smoke file for restart acceptance")
    parser.add_argument("--receipt", type=Path, help="new local receipt file (required with --keep unless resuming)")
    parser.add_argument("--resume", type=Path, help="reopen/hash-check a saved receipt instead of creating a new file")
    args = parser.parse_args()
    if args.timeout <= 0 or (args.keep and not (args.receipt or args.resume)) or (args.receipt and not args.keep):
        parser.error("timeout must be positive; --receipt requires --keep; --keep requires --receipt or --resume")
    try:
        asyncio.run(run(args))
    except (SmokeFailure, httpx.HTTPError, OSError, ValueError, KeyError, TimeoutError,
            ConnectionClosed, InvalidStatus) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("PASS browser deployment smoke", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
