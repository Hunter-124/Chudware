"""Persistent, non-solid mesh references in part documents."""
from __future__ import annotations

from typing import Optional

from .cad_common import _doc, _dumps, _err, _autosave, persist_result, cb
from .fs_tools import _canonical_target


def _mutate(method: str, doc_id: str, params: dict) -> dict:
    doc = _doc(doc_id)
    result = cb.call(method, {"doc_id": doc, **params})
    if result.get("ok"):
        _autosave(doc)
        persist_result(doc, result)
    return result


def add_reference(doc_id: str, path: str, *, name: Optional[str] = None,
                  transform: Optional[dict] = None, opacity: float = 0.4,
                  approximate: bool = False, provenance: Optional[dict] = None,
                  authorize: bool = True) -> dict:
    if authorize:
        target, blocked = _canonical_target(path)
        if blocked:
            raise cb.CoreError(blocked["hint"], code=blocked["reason"], detail={"field": "path"})
        path = str(target)
    params = {"path": path, "opacity": opacity, "approximate": approximate}
    if name is not None:
        params["name"] = name
    if transform is not None:
        params["transform"] = transform
    if provenance is not None:
        params["provenance"] = provenance
    return _mutate("doc_mesh_ref_add", doc_id, params)


def register_cad_reference_tools(mcp) -> None:
    @mcp.tool()
    def add_mesh_reference(doc_id: str, path: str, transform: Optional[dict] = None,
                           name: Optional[str] = None, opacity: float = 0.4,
                           approximate: bool = False) -> str:
        """Embed a PLY, STL or OBJ as a non-solid reference; transform maps source units to mm."""
        try:
            return _dumps(add_reference(doc_id, path, name=name, transform=transform,
                                        opacity=opacity, approximate=approximate))
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def list_mesh_references(doc_id: str) -> str:
        """List embedded mesh references, their placement and bounds in millimetres."""
        try:
            return _dumps(cb.call("doc_mesh_ref_list", {"doc_id": _doc(doc_id)}))
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def update_mesh_reference(doc_id: str, id: str, transform: Optional[dict] = None,
                              visible: Optional[bool] = None, opacity: Optional[float] = None,
                              color: Optional[list[float]] = None, name: Optional[str] = None) -> str:
        """Change a reference's placement, visibility, opacity, color or name."""
        try:
            values = {"transform": transform, "visible": visible, "opacity": opacity,
                      "color": color, "name": name}
            return _dumps(_mutate("doc_mesh_ref_update", doc_id,
                                  {"id": id, **{k: v for k, v in values.items() if v is not None}}))
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def remove_mesh_reference(doc_id: str, id: str) -> str:
        """Remove an embedded reference without changing solid features."""
        try:
            return _dumps(_mutate("doc_mesh_ref_remove", doc_id, {"id": id}))
        except Exception as exc:
            return _err(exc)
