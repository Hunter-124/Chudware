"""export_part explains a failed read-back instead of implying the live part is broken."""
from __future__ import annotations

import json

from services.mcp import cad_exchange_tools as M
from services.mcp.core_bridge import CoreError


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def test_readback_failure_names_the_checked_copy(monkeypatch, tmp_path):
    message = ("export_file: wrote x.step but re-reading it recovered 1/1 solids (0 valid) "
               "— the file is incomplete or malformed and would not open elsewhere")

    def fake_call(method, params):
        if method == "doc_list":
            return {"docs": [{"doc_id": "doc-live", "revision": 1}]}
        if method == "asm_world_bodies":
            return {"bodies": [{"name": "Block"}]}
        if method == "export_file":
            raise CoreError(message, code="export_failed",
                            detail={"path": params["path"], "format": "step"})
        return {}

    monkeypatch.setattr(M.cb, "call", fake_call)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    mcp = _StubMCP()
    M.register_cad_exchange_tools(mcp)

    out = json.loads(mcp.tools["export_part"](fmt="step", path="part.step", doc_id="doc-live"))

    assert out["ok"] is False and out["code"] == "export_failed"
    assert out["error"] == message
    assert out["detail"]["format"] == "step" and "re-read" in out["detail"]["check"]
    assert "heal_body" in out["hint"]
