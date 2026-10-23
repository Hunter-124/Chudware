#!/usr/bin/env python3
"""Live hostd bridge smoke — two AF_UNIX clients against chudware-webd.

Direct socket only (no gateway). Client 1 creates a doc and applies a box
feature; client 2 (subscribed to *) must receive doc.opened + doc.changed for
that doc.

Standalone:
  CHUDWARE_LIVE=1 .venv/bin/python tests/live_smoke.py

Pytest (skipped unless CHUDWARE_LIVE=1):
  .venv/bin/python -m pytest tests/live_smoke.py -q
  CHUDWARE_LIVE=1 .venv/bin/python -m pytest tests/live_smoke.py -q
"""
from __future__ import annotations

import json
import os
import select
import socket
import sys
import time
from pathlib import Path

import pytest

SOCK = os.environ.get(
    "CHUDWARE_HOSTD_SOCKET",
    "/run/user/1000/chudware/hostd-chudwareweb.sock",
)

# Exact add_feature box shape from services/mcp/cad_tools.py (add_features docstring)
# and contracts/doc_ops.md (op=add_feature, feature envelope).
BOX_OP = {
    "op": "add_feature",
    "feature": {
        "type": "box",
        "name": "Block",
        "params": {
            "length": {"value": 40, "expr": None},
            "width": {"value": 30, "expr": None},
            "height": {"value": 20, "expr": None},
        },
    },
}

_LIVE = os.environ.get("CHUDWARE_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set CHUDWARE_LIVE=1 to run against live hostd socket",
)


def _event_doc_id(msg: dict) -> str | None:
    data = msg.get("data")
    if isinstance(data, dict) and data.get("doc_id"):
        return data["doc_id"]
    return msg.get("doc_id")


class HostdClient:
    def __init__(self, sock_path: str, label: str = "c") -> None:
        self.label = label
        self.sock_path = sock_path
        self._n = 0
        s = socket.socket(socket.AF_UNIX)
        s.connect(sock_path)
        s.settimeout(30.0)
        self.s = s
        self.f = s.makefile("rwb", buffering=0)
        self.pending_events: list[dict] = []

    def _send(self, method: str, params: dict | None = None) -> int:
        self._n += 1
        rid = self._n
        line = json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n"
        self.f.write(line.encode())
        return rid

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        rid = self._send(method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            r, _, _ = select.select([self.s], [], [], remaining)
            if not r:
                continue
            raw = self.f.readline()
            if not raw:
                raise ConnectionError(f"{self.label}: daemon closed")
            msg = json.loads(raw)
            if msg.get("id") == rid:
                return msg
            if "event" in msg or msg.get("id") is None:
                self.pending_events.append(msg)
        raise TimeoutError(f"{self.label}: timeout waiting for {method} id={rid}")

    def drain_events(
        self,
        want: set[str],
        *,
        doc_id: str | None = None,
        timeout: float = 10.0,
    ) -> list[dict]:
        """Collect push events until all names in *want* have been seen (or timeout).

        When *doc_id* is set, only events for that document count.
        """
        found: list[dict] = []
        seen: set[str] = set()

        def consider(m: dict) -> bool:
            name = m.get("event")
            if name not in want or name in seen:
                return False
            if doc_id is not None and _event_doc_id(m) != doc_id:
                return False
            found.append(m)
            seen.add(name)
            return True

        leftover = []
        for m in self.pending_events:
            if not consider(m):
                leftover.append(m)
        self.pending_events = leftover
        if seen >= want:
            return found

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and seen < want:
            remaining = max(0.01, deadline - time.monotonic())
            r, _, _ = select.select([self.s], [], [], remaining)
            if not r:
                continue
            raw = self.f.readline()
            if not raw:
                break
            msg = json.loads(raw)
            if "event" in msg:
                if not consider(msg):
                    self.pending_events.append(msg)
            else:
                self.pending_events.append(msg)
        return found

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass


def run_smoke(sock: str = SOCK) -> int:
    path = Path(sock)
    print(f"socket: {path} exists: {path.exists()}")
    if not path.exists():
        print("FAIL: hostd socket missing", file=sys.stderr)
        return 1

    c1 = HostdClient(sock, "ws1")
    c2 = HostdClient(sock, "ws2")
    doc_id = None
    try:
        h1 = c1.call("hello", {"client": "test", "pid": 1, "proto": 1})
        assert h1.get("result", {}).get("state") == "ready", h1
        print("hello1:", {k: h1["result"].get(k) for k in ("proto", "state", "git_rev")})

        h2 = c2.call("hello", {"client": "test", "pid": 2, "proto": 1})
        assert h2.get("result", {}).get("state") == "ready", h2
        print("hello2: ready")

        # Bug fix (1): ws2 MUST subscribe right after hello→ready
        sub = c2.call("subscribe", {"doc_ids": "*"})
        print("subscribe2:", sub.get("result", sub))
        assert (sub.get("result") or {}).get("ok") is True, sub

        newdoc = c1.call("doc_new", {"name": "P0Smoke"})
        result = newdoc.get("result") or {}
        doc_id = result.get("doc_id") or result.get("id")
        print("doc_new:", json.dumps({"doc_id": doc_id, "name": result.get("name")}))
        assert doc_id, newdoc

        # Bug fix (2): real valid add_feature box op
        ops = c1.call(
            "doc_apply_ops",
            {"doc_id": doc_id, "ops": [BOX_OP]},
            timeout=60.0,
        )
        if "error" in ops:
            print("apply_ops ERROR:", ops, file=sys.stderr)
            return 1
        op_res = ops.get("result") or {}
        print("apply_ops id_map:", op_res.get("id_map"))

        events = c2.drain_events(
            {"doc.opened", "doc.changed"},
            doc_id=doc_id,
            timeout=10.0,
        )
        names = [e.get("event") for e in events]
        print("ws2 events for", doc_id, ":", names)
        for e in events:
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            print(
                "  ",
                json.dumps(
                    {
                        "event": e.get("event"),
                        "doc_id": _event_doc_id(e),
                        "revision": data.get("revision") if isinstance(data, dict) else None,
                        "method": data.get("method") if isinstance(data, dict) else None,
                    }
                ),
            )

        if "doc.opened" not in names or "doc.changed" not in names:
            print(
                f"FAIL: ws2 expected doc.opened+doc.changed for {doc_id}, got {names}",
                file=sys.stderr,
            )
            return 1

        print("SMOKE OK — ws2 received doc.opened + doc.changed")
        return 0
    finally:
        if doc_id:
            try:
                c1.call("doc_close", {"doc_id": doc_id}, timeout=10.0)
                print("doc_close:", doc_id)
            except Exception as exc:
                print("doc_close warning:", exc, file=sys.stderr)
        c1.close()
        c2.close()


def test_live_hostd_event_fanout() -> None:
    """Two clients: subscribe + real box op → doc.opened + doc.changed."""
    rc = run_smoke()
    assert rc == 0, "live hostd smoke failed (see stdout for evidence)"


def main() -> int:
    if not _LIVE:
        print(
            "CHUDWARE_LIVE is not set — refusing to hit the live daemon.\n"
            "Re-run with: CHUDWARE_LIVE=1 python tests/live_smoke.py",
            file=sys.stderr,
        )
        return 2
    return run_smoke()


if __name__ == "__main__":
    sys.exit(main())
