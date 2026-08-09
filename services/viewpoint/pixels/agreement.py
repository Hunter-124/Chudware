"""One-shot proof that the in-process pixels agree with the exact engine renderer."""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import json
from pathlib import Path
from typing import Iterable

import numpy as np

from services.viewpoint.dataset import geometry_hash
from services.viewpoint.geometry import Mesh
from services.viewpoint.parts import generate_part
from services.viewpoint.render import RenderRequest
from services.viewpoint.sampling import generate_benchmark_candidate_set

from .raster import RASTERIZER_VERSION, rasterize


@dataclass(frozen=True, slots=True)
class RendererAgreement:
    part_id: str
    candidate_index: int
    resolution: tuple[int, int]
    foreground_iou: float
    rgb_mae_union: float
    matching_foreground_pixels: int
    union_foreground_pixels: int
    engine_path: Path
    rasterizer_version: str = RASTERIZER_VERSION


def _components(mesh: Mesh) -> list[np.ndarray]:
    """Connected triangle groups; the engine imports each disconnected shell as one body."""
    triangles = mesh.triangles
    by_vertex: dict[int, list[int]] = {}
    for triangle_index, triangle in enumerate(triangles):
        for vertex in triangle:
            by_vertex.setdefault(int(vertex), []).append(triangle_index)
    unseen = set(range(len(triangles)))
    groups: list[np.ndarray] = []
    while unseen:
        start = unseen.pop()
        stack = [start]
        group = [start]
        while stack:
            current = stack.pop()
            for vertex in triangles[current]:
                for neighbor in by_vertex[int(vertex)]:
                    if neighbor in unseen:
                        unseen.remove(neighbor)
                        stack.append(neighbor)
                        group.append(neighbor)
        groups.append(np.asarray(group, np.int64))
    return groups


def _write_obj(path: Path, mesh: Mesh, triangle_indices: Iterable[int]) -> None:
    selected = mesh.triangles[np.asarray(tuple(triangle_indices), np.int64)]
    used = np.unique(selected)
    remap = np.full(len(mesh.vertices), -1, np.int64)
    remap[used] = np.arange(len(used))
    rows = [*(f"v {v[0]:.17g} {v[1]:.17g} {v[2]:.17g}" for v in mesh.vertices[used])]
    rows.extend(f"f {a + 1} {b + 1} {c + 1}" for a, b, c in remap[selected])
    path.write_text("\n".join(rows) + "\n", encoding="ascii")


class _Engine:
    def __init__(self, library: Path):
        self.library = ctypes.CDLL(str(library))
        self.library.chudware_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
        self.library.chudware_call.restype = ctypes.c_int
        self.library.chudware_free.argtypes = [ctypes.c_char_p]

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        output = ctypes.c_char_p()
        code = self.library.chudware_call(method.encode("utf-8"), json.dumps(params).encode("utf-8"), ctypes.byref(output))
        text = ctypes.string_at(output).decode("utf-8") if output.value else ""
        if output.value:
            self.library.chudware_free(output)
        if code != 0:
            raise RuntimeError(f"engine {method} failed ({code}): {text}")
        value = json.loads(text)
        if value.get("ok") is False:
            raise RuntimeError(f"engine {method} refused: {value}")
        return value


def compare_with_engine(
    *,
    part_index: int = 0,
    seed: int = 1729,
    resolution: tuple[int, int] = (160, 120),
    library: Path = Path("core/build-linux/bin/libchudware.so"),
    output_dir: Path = Path("scratch/viewpoint-pixels/agreement"),
) -> RendererAgreement:
    """Import one benchmark mesh into the real engine and compare exact-camera pixels.

    Foreground IoU is the primary pixel-level geometry metric.  RGB MAE over the union is
    reported separately because OCCT re-tessellates imported shells and can assign face normals
    differently even when projected coverage agrees.
    """
    library = library.resolve()
    if not library.exists():
        raise FileNotFoundError(f"engine library not found: {library}")
    output_dir.mkdir(parents=True, exist_ok=True)
    part = generate_part(part_index, seed=seed)
    candidates = generate_benchmark_candidate_set(
        part.scene,
        part.target_feature_ids[0],
        8,
        seed=seed ^ 0xA661,
        image_size=resolution,
    )
    local_frames = [rasterize(part.scene.mesh, camera, resolution=resolution) for camera in candidates]
    candidate_index = int(np.argmax([np.count_nonzero(frame.depth_mask) for frame in local_frames]))
    camera = candidates[candidate_index]
    engine = _Engine(library)
    document_id = ""
    for component_index, triangle_indices in enumerate(_components(part.scene.mesh)):
        obj_path = output_dir / f"{part.part_id}-component-{component_index:03d}.obj"
        _write_obj(obj_path, part.scene.mesh, triangle_indices)
        response = engine.call(
            "import_file",
            {"path": str(obj_path.resolve()), "into": document_id, "format": "obj", "as_solid": False},
        )
        document_id = str(response["doc_id"])
    engine_path = (output_dir / f"{part.part_id}-engine.png").resolve()
    request = RenderRequest(
        request_id="pixel-raster-agreement",
        candidate_id=f"candidate-{candidate_index}",
        doc_id=document_id,
        geometry_hash=geometry_hash(part.scene),
        feature="agreement-only; not encoder input",
        pose=camera,
        output_path=str(engine_path),
        annotate=False,
    )
    call = request.to_worker_rpc()
    engine.call(call.method, dict(call.params))
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required only for decoding the engine agreement PNG") from exc
    engine_rgb = np.asarray(Image.open(engine_path).convert("RGB"), dtype=np.uint8)
    local_rgb = local_frames[candidate_index].rgb
    if engine_rgb.shape != local_rgb.shape:
        raise RuntimeError(f"engine returned {engine_rgb.shape}, expected {local_rgb.shape}")
    engine_mask = np.any(engine_rgb != 245, axis=2)
    local_mask = local_frames[candidate_index].depth_mask
    intersection = int(np.count_nonzero(engine_mask & local_mask))
    union_mask = engine_mask | local_mask
    union = int(np.count_nonzero(union_mask))
    foreground_iou = float(intersection / union) if union else 1.0
    rgb_mae = float(np.abs(engine_rgb.astype(np.int16) - local_rgb.astype(np.int16))[union_mask].mean()) if union else 0.0
    return RendererAgreement(
        part.part_id,
        candidate_index,
        resolution,
        foreground_iou,
        rgb_mae,
        intersection,
        union,
        engine_path,
    )


__all__ = ["RendererAgreement", "compare_with_engine"]
