"""cpp_agent_parity.py — Python-provider vs C++-core (libaicad.dll) parity gate.

P51 ports the LLM provider layer (agent/providers/*.py) to C++ (core/agent/). This
offline gate proves the C++ request BUILDERS + response PARSERS match the REAL
Python provider code, byte for byte, by bypassing the heavy SDK construction:

  - Python ORACLE   : agent.providers.{anthropic_provider,openai_provider} —
                      we construct the provider objects with object.__new__ to
                      skip __init__ (no SDK client, no API key needed) and call
                      their pure methods (_block / _content / _message) + the
                      module-level _normalize_base_url.
  - C++  CANDIDATE  : libaicad.dll diagnostic RPCs agent_build_request /
                      agent_parse_response / agent_b64_file / agent_normalize_base_url.

Compares parsed JSON for structural equality (dict ==), NEVER strings. Exit code
is the number of mismatches (0 = full parity). Run under the project venv:

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_agent_parity.py \
        --dll core\build-occt\bin\Release\libaicad.dll

The LIVE http_post itself cannot be gated offline; an OPTIONAL live smoke runs
only when ANTHROPIC_API_KEY is set (otherwise SKIP) and just asserts a non-empty
reply — the gate never depends on the network.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- C++ DLL
class CppCore:
    def __init__(self, dll_path: Path):
        dll_path = dll_path.resolve()
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(dll_path.parent))
        self.lib = ctypes.CDLL(str(dll_path))
        self.lib.aicad_call.restype = ctypes.c_int
        self.lib.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                        ctypes.POINTER(ctypes.c_char_p)]
        self.lib.aicad_free.restype = None
        self.lib.aicad_free.argtypes = [ctypes.c_char_p]
        self.lib.aicad_abi_version.restype = ctypes.c_char_p

    def abi(self) -> str:
        return self.lib.aicad_abi_version().decode()

    def call(self, method: str, params: dict) -> tuple[int, dict]:
        out = ctypes.c_char_p()
        rc = self.lib.aicad_call(method.encode(), json.dumps(params).encode(),
                                 ctypes.byref(out))
        data = {}
        if out.value:
            try:
                data = json.loads(out.value.decode())
            finally:
                self.lib.aicad_free(out)
        return rc, data


# --------------------------------------------------------------------------- oracle
def _load_oracles():
    """Import the REAL provider modules and build SDK-free oracle instances."""
    sys.path.insert(0, str(REPO))
    import importlib
    base = importlib.import_module("agent.providers.base")
    ap_mod = importlib.import_module("agent.providers.anthropic_provider")
    op_mod = importlib.import_module("agent.providers.openai_provider")

    # Bypass __init__ so no anthropic/openai SDK client (or key) is constructed.
    ap = object.__new__(ap_mod.AnthropicProvider)
    ap.model = ap_mod.DEFAULT_MODEL  # "claude-opus-4-8"
    op = object.__new__(op_mod.OpenAIProvider)
    op.model = op_mod.DEFAULT_MODEL  # "gpt-4o"
    op.base_url = None
    op.supports_vision = True
    return base, ap_mod, op_mod, ap, op


def _make_png(dirpath: Path) -> Path:
    """Write a tiny but valid 1x1 PNG so the image-part path is a real file."""
    png = (b"\x89PNG\r\n\x1a\n"
           + struct.pack(">I", 13) + b"IHDR"
           + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    # CRC of IHDR
    import zlib
    crc = zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    png += struct.pack(">I", crc & 0xFFFFFFFF)
    # one IDAT with a single black pixel scanline (filter 0 + RGB)
    raw = b"\x00\x00\x00\x00"
    comp = zlib.compress(raw)
    png += struct.pack(">I", len(comp)) + b"IDAT" + comp
    png += struct.pack(">I", zlib.crc32(b"IDAT" + comp) & 0xFFFFFFFF)
    png += struct.pack(">I", 0) + b"IEND"
    png += struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    p = dirpath / "agent_parity_1x1.png"
    p.write_bytes(png)
    return p


# --------------------------------------------------------------------------- main
def main() -> int:
    ap_cli = argparse.ArgumentParser(description=__doc__)
    ap_cli.add_argument("--dll",
                        default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap_cli.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[agent-parity] libaicad.dll not found at {dll}\n"
              f"               build: cmake --build core/build-occt --config Release --target aicad")
        return 2
    core = CppCore(dll)
    print(f"[agent-parity] libaicad ABI={core.abi()}  dll={dll}")

    base, ap_mod, op_mod, ap, op = _load_oracles()

    mismatches = 0

    def check(label: str, ok: bool, detail: str = ""):
        nonlocal mismatches
        verdict = "MATCH" if ok else "DIFF"
        if not ok:
            mismatches += 1
        print(f"  [{verdict}] {label}" + (f"  -- {detail}" if (detail and not ok) else ""))

    tmp = Path(tempfile.mkdtemp(prefix="agent_parity_"))
    png = _make_png(tmp)
    png_str = str(png)

    # parts under test: a text part + an image part pointing at a real PNG.
    text_p = base.text_part("hello world")
    image_p = base.image_part(png_str)
    parts = [text_p, image_p]

    # ============================ (a) block / content conversion ===============
    print("\n(a) message-block / content conversion")
    # Anthropic per-message content blocks (built via agent_build_request, then we
    # pull message[0].content and compare to [ap._block(p) for p in parts]).
    msgs = [base.user(parts)]
    rc, cpp = core.call("agent_build_request", {
        "provider": "anthropic", "model": ap.model, "system": "sys",
        "messages": msgs, "max_tokens": 8000})
    cpp_blocks = cpp.get("body", {}).get("messages", [{}])[0].get("content")
    oracle_blocks = [ap._block(p) for p in parts]
    check("anthropic _block(parts)", cpp_blocks == oracle_blocks,
          f"cpp={cpp_blocks} oracle={oracle_blocks}")

    # OpenAI: _content(p) per part AND the _message branch (image present -> array).
    rc, cpp = core.call("agent_build_request", {
        "provider": "openai", "model": op.model, "system": "sys",
        "messages": msgs, "max_tokens": 8000})
    cpp_msg = cpp.get("body", {}).get("messages", [None, None])[1]  # [0]=system,[1]=user
    oracle_msg = op._message("user", parts)
    check("openai _message(role, parts) [image -> array]", cpp_msg == oracle_msg,
          f"cpp={cpp_msg} oracle={oracle_msg}")
    # also exercise _content directly on the image part (it is the array element).
    check("openai _content(image_part)",
          cpp_msg.get("content", [None])[1] == op._content(image_p) if isinstance(cpp_msg.get("content"), list) else False)

    # ============================ (b) base64 ===================================
    print("\n(b) base64 of the PNG bytes")
    oracle_b64 = base64.standard_b64encode(png.read_bytes()).decode("utf-8")
    rc, cpp = core.call("agent_b64_file", {"path": png_str})
    check("agent_b64_file == standard_b64encode", cpp.get("b64") == oracle_b64,
          f"cpp_len={len(cpp.get('b64',''))} oracle_len={len(oracle_b64)}")
    # the image block's data field must also equal it (anthropic source.data).
    check("anthropic image block data == b64",
          oracle_blocks[1]["source"]["data"] == oracle_b64 == cpp.get("b64"))

    # ============================ (c) full request body ========================
    print("\n(c) full request body + url + headers")
    # Anthropic body. Build the oracle body the way complete_ex does.
    api_messages = [
        {"role": mm["role"], "content": [ap._block(p) for p in mm["content"]]}
        for mm in msgs
    ]
    oracle_body = {
        "model": ap.model, "max_tokens": 8000, "system": "sys",
        "messages": api_messages,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    rc, cpp = core.call("agent_build_request", {
        "provider": "anthropic", "model": ap.model, "system": "sys",
        "messages": msgs, "max_tokens": 8000})
    check("anthropic body == oracle", cpp.get("body") == oracle_body,
          f"cpp={cpp.get('body')}\n        oracle={oracle_body}")
    check("anthropic url", cpp.get("url") == "https://api.anthropic.com/v1/messages",
          cpp.get("url"))
    hdr = cpp.get("headers", {})
    check("anthropic headers (x-api-key + anthropic-version)",
          "x-api-key" in hdr and hdr.get("anthropic-version") == "2023-06-01"
          and hdr.get("content-type") == "application/json", str(hdr))

    # OpenAI body (text+image message -> array branch).
    oracle_msgs = [{"role": "system", "content": "sys"}]
    for mm in msgs:
        oracle_msgs.append(op._message(mm["role"], mm["content"]))
    oracle_obody = {"model": op.model, "messages": oracle_msgs, "max_tokens": 8000}
    rc, cpp = core.call("agent_build_request", {
        "provider": "openai", "model": op.model, "system": "sys",
        "messages": msgs, "max_tokens": 8000})
    check("openai body == oracle", cpp.get("body") == oracle_obody,
          f"cpp={cpp.get('body')}\n        oracle={oracle_obody}")
    check("openai url (default root)",
          cpp.get("url") == "https://api.openai.com/v1/chat/completions", cpp.get("url"))
    hdr = cpp.get("headers", {})
    check("openai headers (Authorization Bearer)",
          hdr.get("Authorization", "").startswith("Bearer")
          and hdr.get("content-type") == "application/json", str(hdr))

    # OpenAI with a local base_url -> normalized to .../v1/chat/completions.
    rc, cpp = core.call("agent_build_request", {
        "provider": "openai", "model": op.model, "system": "sys",
        "messages": [base.user([base.text_part("hi")])], "max_tokens": 16,
        "base_url": "http://localhost:1234"})
    check("openai base_url normalization in url",
          cpp.get("url") == "http://localhost:1234/v1/chat/completions", cpp.get("url"))
    # text-only message -> string-join branch.
    rc, cpp = core.call("agent_build_request", {
        "provider": "openai", "model": op.model, "system": "sys",
        "messages": [base.user([base.text_part("a"), base.text_part("b")])],
        "max_tokens": 16})
    cpp_user = cpp.get("body", {}).get("messages", [None, None])[1]
    oracle_user = op._message("user", [base.text_part("a"), base.text_part("b")])
    check("openai text-only message join", cpp_user == oracle_user,
          f"cpp={cpp_user} oracle={oracle_user}")

    # ============================ (d) response parse ===========================
    print("\n(d) response parse")
    anth_resp = {"content": [
        {"type": "thinking", "thinking": "reasoning"},
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"},
    ]}
    rc, cpp = core.call("agent_parse_response", {"provider": "anthropic", "response": anth_resp})
    check("anthropic parse text", cpp.get("text") == "hello world", cpp.get("text"))
    check("anthropic parse thinking", cpp.get("thinking") == "reasoning", cpp.get("thinking"))

    rc, cpp = core.call("agent_parse_response",
                        {"provider": "openai", "response": {"choices": [{"message": {"content": "hi"}}]}})
    check("openai parse content", cpp.get("text") == "hi", cpp.get("text"))
    rc, cpp = core.call("agent_parse_response",
                        {"provider": "openai", "response": {"choices": []}})
    check("openai parse no-choices -> ''", cpp.get("text") == "", repr(cpp.get("text")))

    # ============================ (e) _normalize_base_url ======================
    print("\n(e) _normalize_base_url (module-level oracle)")
    cases = ["", "http://localhost:1234", "http://localhost:1234/",
             "http://localhost:1234/v1", "https://api.openai.com/v1",
             "http://127.0.0.1:8080//"]
    for c in cases:
        oracle = op_mod._normalize_base_url(c) if c else op_mod._normalize_base_url(c)
        rc, cpp = core.call("agent_normalize_base_url", {"base_url": c})
        check(f"normalize {c!r}", cpp.get("base_url") == (oracle or ""),
              f"cpp={cpp.get('base_url')!r} oracle={oracle!r}")

    # ============================ mock provider ================================
    print("\n(f) mock provider (offline scripted replies)")
    rc, cpp = core.call("agent_complete",
                        {"provider": "mock", "cfg": {"replies": ["one", "two"]}})
    check("mock first reply", cpp.get("text") == "one", cpp.get("text"))

    # ============================ live smoke (optional) ========================
    print("\n(g) live smoke")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        rc, cpp = core.call("agent_complete", {
            "provider": "anthropic", "cfg": {"api_key": key},
            "system": "Reply with the single word ok.",
            "messages": [base.user([base.text_part("ok")])], "max_tokens": 16})
        ok = bool(cpp.get("text")) and not cpp.get("error")
        check("live anthropic non-empty reply", ok, cpp.get("error") or cpp.get("text"))
    else:
        print("  [SKIP] live anthropic POST (set ANTHROPIC_API_KEY to enable)")

    print(f"\n[agent-parity] {mismatches} mismatch(es)" +
          ("  -> FULL PARITY" if mismatches == 0 else ""))
    return mismatches


if __name__ == "__main__":
    raise SystemExit(main())
