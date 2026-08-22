#!/usr/bin/env python3
"""Prove a built web bundle carries the CURRENT repo assets — desktop/web parity gate.

Why this exists
---------------
The desktop app reads ``assets/`` off disk at startup, so regenerating an asset
ships to desktop the instant you rebuild. The web app does not: Emscripten bakes
every asset into ``Chudware.data`` at LINK time (see the ``--preload-file`` flags
in ``host/CMakeLists.txt``). So a web bundle is a *derived artifact* that goes
stale silently the moment any preloaded asset changes.

That is not hypothetical. On 2026-08-17 commit 69690e57 replaced every ribbon
tool icon with an engine-rendered 3D tile. The desktop picked them up
immediately. Production served a bundle linked on 2026-08-07 until 2026-08-22 —
ten days of users looking at the old icons — and nothing anywhere noticed,
because no CI job builds WASM and no check compares a bundle to its sources. It
was found by hand-scanning ``Chudware.data`` for PNG bytes.

What it checks
--------------
Emscripten emits an exact file table in ``Chudware.js``
(``loadPackage({files:[{filename,start,end},...]})``) giving the byte range of
every asset inside ``Chudware.data``. So this is not a heuristic: we slice the
pack at the recorded offsets and compare a SHA-256 against the file on disk.

The expected asset list is derived from the ``--preload-file`` flags in
``host/CMakeLists.txt`` rather than duplicated here, so adding a preloaded asset
automatically joins the gate instead of quietly opting out of it.

Three distinct failures are reported separately, because they need different
fixes:

  STALE    embedded bytes differ from the source     -> relink (./build.sh wasm)
  MISSING  source is preloaded but absent from pack  -> relink
  ORPHAN   pack carries an asset no longer preloaded -> relink (stale pack)

It also verifies the precompressed ``.br``/``.gz`` siblings are not older than
the files they encode. ``Webservice/gateway/appserve.py::_fresh_variant`` refuses
to serve a stale variant, so a half-finished restage silently drops every client
to identity encoding.

Usage
-----
  check_web_parity.py                      # default staged dir
  check_web_parity.py --bundle DIR         # a specific bundle dir
  check_web_parity.py --url https://app.chudware.com   # what PROD actually serves
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CMAKELISTS = REPO / "host" / "CMakeLists.txt"

# Emscripten's file table. Keys are UNQUOTED in the emitted JS (`filename:"/x"`),
# so this is a targeted regex rather than a JSON parse.
_ENTRY_RE = re.compile(r'filename:"([^"]+)",start:(\d+),end:(\d+)')
_PRELOAD_RE = re.compile(r"--preload-file=([^@\s\"]+)@([^@\s\"]+)")


class ParityError(RuntimeError):
    """The bundle could not be evaluated at all (as opposed to being stale)."""


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def preloaded_assets() -> dict[str, Path]:
    """Map every preloaded virtual path -> its source file, per host/CMakeLists.txt.

    Directory preloads (``.../fonts@/assets/fonts``) are expanded to their files so
    the manifest can be compared entry by entry.
    """
    if not CMAKELISTS.is_file():
        raise ParityError(f"cannot read the preload contract: {CMAKELISTS} is missing")
    text = CMAKELISTS.read_text(encoding="utf-8")

    # Resolve the one variable the preload flags use.
    m = re.search(r"set\(_WASM_ASSETS\s+\"([^\"]+)\"\s*\)", text)
    if not m:
        raise ParityError(
            "host/CMakeLists.txt no longer defines _WASM_ASSETS — the preload contract "
            "moved and this gate must be updated to follow it"
        )
    wasm_assets = m.group(1).replace("${CMAKE_CURRENT_SOURCE_DIR}", str(REPO / "host"))
    root = Path(os.path.normpath(wasm_assets))

    pairs = _PRELOAD_RE.findall(text)
    if not pairs:
        raise ParityError("host/CMakeLists.txt lists no --preload-file flags")

    out: dict[str, Path] = {}
    for raw_src, dest in pairs:
        src = Path(os.path.normpath(raw_src.replace("${_WASM_ASSETS}", str(root))))
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    out[f"{dest.rstrip('/')}/{f.relative_to(src).as_posix()}"] = f
        else:
            out[dest] = src
    return out


def _read_manifest(js_text: str) -> list[tuple[str, int, int]]:
    entries = [(n, int(a), int(b)) for n, a, b in _ENTRY_RE.findall(js_text)]
    if not entries:
        raise ParityError(
            "no Emscripten file table in Chudware.js — this is not a preloaded bundle, "
            "or emscripten changed its loader format"
        )
    return entries


class _LocalPack:
    def __init__(self, bundle: Path):
        js = bundle / "Chudware.js"
        data = bundle / "Chudware.data"
        for p in (js, data):
            if not p.is_file():
                raise ParityError(f"{p} is missing — nothing to verify in {bundle}")
        self.entries = _read_manifest(js.read_text(encoding="utf-8", errors="replace"))
        self._data = data.read_bytes()
        self.size = len(self._data)
        self.label = str(bundle)

    def slice(self, start: int, end: int) -> bytes:
        return self._data[start:end]


class _RemotePack:
    """Reads a served bundle, using HTTP Range so a watch job pulls kilobytes."""

    def __init__(self, base: str, cookie: str | None):
        base = base.rstrip("/")
        self.app = base if base.endswith("/app") else f"{base}/app"
        self._hdr = {"Accept-Encoding": "identity", "User-Agent": "chudware-web-parity"}
        if cookie:
            self._hdr["Cookie"] = cookie
        self.entries = _read_manifest(
            _get_bytes(f"{self.app}/Chudware.js", cookie).decode("utf-8", "replace")
        )
        self.size = -1
        self.label = self.app

    def slice(self, start: int, end: int) -> bytes:
        hdr = dict(self._hdr)
        hdr["Range"] = f"bytes={start}-{end - 1}"
        req = urllib.request.Request(f"{self.app}/Chudware.data", headers=hdr)
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            # A server that ignores Range hands back the whole pack; slice locally
            # rather than silently comparing the wrong bytes.
            return body if r.status == 206 else body[start:end]


def _get_bytes(url: str, cookie: str | None) -> bytes:
    hdr = {"Accept-Encoding": "identity", "User-Agent": "chudware-web-parity"}
    if cookie:
        hdr["Cookie"] = cookie
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60) as r:
        return r.read()


def check_compression_freshness(bundle: Path) -> list[str]:
    """Flag precompressed siblings the gateway will refuse to serve.

    Mirrors appserve.py::_fresh_variant — a .br/.gz older than its source is
    treated as stale and skipped, so clients silently fall back to identity.
    """
    problems: list[str] = []
    for src in sorted(bundle.iterdir()):
        if not src.is_file() or src.suffix in (".br", ".gz"):
            continue
        for ext in (".br", ".gz"):
            var = src.with_name(src.name + ext)
            if var.is_file() and var.stat().st_mtime < src.stat().st_mtime:
                problems.append(
                    f"{var.name} is older than {src.name} — appserve treats it as stale and "
                    f"will serve uncompressed"
                )
    return problems


def verify(pack, expected: dict[str, Path]) -> dict[str, list[str]]:
    found = {name: (a, b) for name, a, b in pack.entries}
    res: dict[str, list[str]] = {"ok": [], "stale": [], "missing": [], "orphan": []}

    for vpath, src in sorted(expected.items()):
        if vpath not in found:
            res["missing"].append(f"{vpath} (source {src.relative_to(REPO)}) is not in the pack")
            continue
        start, end = found[vpath]
        embedded = pack.slice(start, end)
        want = src.read_bytes()
        if _sha(embedded) == _sha(want):
            res["ok"].append(vpath)
        else:
            res["stale"].append(
                f"{vpath}: pack has {len(embedded)}B sha {_sha(embedded)[:12]}, "
                f"repo has {len(want)}B sha {_sha(want)[:12]}  <- {src.relative_to(REPO)}"
            )

    for vpath in sorted(found):
        if vpath not in expected:
            res["orphan"].append(f"{vpath} is embedded but no longer preloaded by CMakeLists")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--bundle", type=Path, help="staged bundle directory to verify")
    g.add_argument("--url", help="base URL of a SERVED bundle (verifies what users actually get)")
    ap.add_argument("--cookie", help="Cookie header for --url when /app is auth-gated")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    args = ap.parse_args()

    expected = preloaded_assets()

    comp_problems: list[str] = []
    if args.url:
        pack = _RemotePack(args.url, args.cookie or os.environ.get("CHUDWARE_PARITY_COOKIE"))
    else:
        bundle = args.bundle or Path(
            os.environ.get("CHUDWARE_WEB_APP", str(Path.home() / "chudware-web" / "app"))
        )
        pack = _LocalPack(bundle)
        comp_problems = check_compression_freshness(bundle)

    res = verify(pack, expected)
    bad = res["stale"] + res["missing"] + res["orphan"]

    if args.json:
        print(json.dumps({**res, "compression": comp_problems, "target": pack.label}, indent=2))
    else:
        print(f"[web-parity] target: {pack.label}")
        print(f"[web-parity] {len(res['ok'])}/{len(expected)} preloaded assets match the repo")
        for kind, label in (
            ("stale", "STALE   "),
            ("missing", "MISSING "),
            ("orphan", "ORPHAN  "),
        ):
            for line in res[kind]:
                print(f"[web-parity] {label} {line}")
        for line in comp_problems:
            print(f"[web-parity] COMPRESS {line}")

    if bad:
        print(
            f"\n[web-parity] FAIL: {len(bad)} asset(s) in this bundle do not match the repo.\n"
            f"[web-parity] The web app bakes assets in at LINK time, so desktop and web have\n"
            f"[web-parity] diverged. Relink and restage:  ./build.sh wasm",
            file=sys.stderr,
        )
        return 1
    if comp_problems:
        print(
            f"\n[web-parity] FAIL: {len(comp_problems)} precompressed variant(s) are stale; the\n"
            f"[web-parity] gateway will silently serve these uncompressed. Re-run ./build.sh wasm",
            file=sys.stderr,
        )
        return 1
    print("[web-parity] PASS: web bundle is at parity with the repo")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ParityError as e:
        print(f"[web-parity] ERROR: {e}", file=sys.stderr)
        sys.exit(2)
