#!/usr/bin/env python3
"""Minimal static file server with COOP/COEP headers for SharedArrayBuffer/pthreads.

Usage:
  python3 host/wasm/coop_server.py [port] [directory]

Defaults: port 8765, directory host/build-wasm/app (where `./build.sh wasm` stages)
"""
from __future__ import annotations

import http.server
import os
import sys


class CoopHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    default_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build-wasm", "app")
    directory = os.path.abspath(os.path.expanduser(sys.argv[2] if len(sys.argv) > 2 else default_dir))
    os.chdir(directory)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), CoopHandler)
    print(f"[coop_server] serving {directory} on http://127.0.0.1:{port}/  (COOP/COEP on)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[coop_server] stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
