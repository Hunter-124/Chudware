"""Shared pytest fixtures: an in-process engine and the real CAD tools, for building
genuine deliverables offline (no model, no daemon)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import pytest

os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"


class _CaptureMCP:
    """FastMCP stand-in collecting ``@mcp.tool()`` registrations by function name."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture(scope="session")
def cad_tools() -> dict[str, Callable]:
    """``register_cad_tools`` on a stub MCP: new_part / add_features / save_part / …"""
    from services.mcp.cad_tools import register_cad_tools

    m = _CaptureMCP()
    register_cad_tools(m)
    return m.tools


@pytest.fixture
def build_part(cad_tools, tmp_path, monkeypatch) -> Callable[..., Path]:
    """``build_part(ops, name=..., path=...) -> Path``: build a real part and save .step+."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))

    def _build(ops: list[dict], *, name: str = "bench_part",
               path: Path | None = None) -> Path:
        doc = json.loads(cad_tools["new_part"](name=name, fresh=True))["doc_id"]
        built = json.loads(cad_tools["add_features"](doc_id=doc, ops=ops))
        assert built.get("ok"), built
        out = Path(path) if path else tmp_path / "deliverable.step+"
        saved = json.loads(cad_tools["save_part"](path=str(out), doc_id=doc))
        assert saved.get("ok"), saved
        return out

    return _build
