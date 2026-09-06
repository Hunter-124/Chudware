"""Cinematic CAD animation MCP tool: render_animation (turntable | explode | motion | fea).

A still render answers "did the feature land?". A FILM answers "is this a machine?" — it is
the artifact a design review, a poster session or a launch page actually plays. This module
is the one animation implementation:

  turntable  360 degree orbit of a document               (read-only)
  explode    components translate outward from the assembly centroid, ramp 0 -> factor
  motion     one mate driven through simulate_motion's own solved trajectory
  fea        a solved .vtu: von Mises + displacement warp ramp over the undeformed outline

Three rules bind every kind:

1. **The frames are the engine's own renders.** Every CAD frame goes through
   ``cad_common._render_view_png`` / ``_annotate_render`` — the same two calls render_part
   and contact_sheet use — so a film and a still of the same document cannot disagree about
   what the geometry looks like. Nothing here reimplements a rasterizer.
2. **The caller's document is never mutated.** ``turntable`` only renders. ``explode`` and
   ``motion`` need per-frame component poses, so they open a DETACHED CLONE of the
   document's own file (``doc_open(path, detach_path=True)``) and move components on the
   clone; the caller's doc keeps its geometry, revision, undo history and dirty flag. A doc
   with no file is refused rather than quietly edited.
3. **Every number on screen is measured.** The per-frame footer carries the engine build id,
   the polymesh commit, the document and the frame index; the caption carries bytes, frame
   count, fps, duration, sha256 and each kind's own measured quantities. A value the engine
   did not report is printed as "unknown", never guessed.

Encoding is ffmpeg: GIF through palettegen/paletteuse (a 256-colour global palette — the
default dithering turns a grey CAD gradient into mud), MP4 through libx264 + yuv420p (the
only combination every browser and PowerPoint plays). A GIF over ``max_bytes`` walks a
ladder — output fps first, then width — and reports the leg it landed on; it never truncates
the film, because a cut film is a wrong film.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from typing import Optional
from typing import Union
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time

from .cad_common import (
    _annotate_render,
    _doc,
    _err,
    _handoff_doc_name,
    _normalize_optional_slot,
    _render_view_png,
    _vnorm,
    cb,
    coerce_json_or_repr,
    render_output_path,
    require_list,
)
# The one steer-pose driver. A steer film and a turning-radius study must pose the same
# detached clone the same way, so the animation drives the motion tools' own helper
# instead of re-deriving a rotation about a kingpin axis.
from .cad_motion_tools import steer_pose_sweep
# The save-path contract every render deliverable already obeys (absolute, inside a
# configured project/workspace root, '..' collapsed first). Imported, never re-implemented:
# an animation is a figure like any other, and a second interpretation of the same rule is
# how a film ends up somewhere nobody can point a report at.
from .cad_render_tools import _render_save_target
from .cad_render_tools import _render_stem

_KINDS = ("turntable", "explode", "motion", "fea")
_GIF_MAX_BYTES = 25 * 1024 * 1024
# The size ladder a too-large GIF walks: output fps first (the film keeps its full duration
# and both end points), then width. Never content.
_FPS_LADDER = (30, 24, 20, 15, 12, 10)
_WIDTH_LADDER = (1280, 1024, 854, 720, 640, 512)
_FOOTER_H = 34
_BG = (16, 17, 20)


# --------------------------------------------------------------------------- provenance
def _polymesh_commit_from_build() -> tuple:
    """(commit, source) for the polymesh revision COMPILED INTO the loaded engine.

    ``CHUDWARE_POLYMESH_COMMIT`` is a compile definition (core/CMakeLists.txt), so the only
    places it can be read without running a solve are the build directory that produced the
    library and the solve provenance itself. Returns (None, reason) when neither is
    readable — a missing commit stays missing."""
    try:
        from . import core_bridge as _cbmod  # noqa: PLC0415
        cands = [p for p in _cbmod._lib_candidates() if os.path.exists(str(p))]
    except Exception:  # noqa: BLE001
        cands = []
    for lib in cands:
        # <build>/bin/libchudware.so -> walk up to the build root's CMakeCache.txt.
        here = os.path.dirname(os.path.abspath(str(lib)))
        for _ in range(4):
            cache = os.path.join(here, "CMakeCache.txt")
            try:
                with open(cache, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("POLYMESH_COMMIT:"):
                            val = line.split("=", 1)[1].strip()
                            if val:
                                return val, cache
            except OSError:
                pass
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
    return None, "no CMakeCache.txt beside the loaded libchudware"


def _engine_build_id() -> tuple:
    """(build id, source) for the engine actually serving this process.

    Daemon mode answers ``hello`` with its own ``engine_build_id``; in-process mode the
    library exports ``chudware_build_id``. Either way the value comes from the running
    binary, not from a file this module guessed at."""
    try:
        r = cb.call("hello", {"client": "mcp-animation"})
        bid = str(r.get("engine_build_id") or "").strip()
        if bid:
            return bid, "hostd hello.engine_build_id"
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes  # noqa: PLC0415
        from . import core_bridge as _cbmod  # noqa: PLC0415
        dll = _cbmod._load()
        fn = dll.chudware_build_id
        fn.restype = ctypes.c_char_p
        val = (fn() or b"").decode("utf-8", "replace").strip()
        if val:
            return val, "libchudware chudware_build_id()"
    except Exception:  # noqa: BLE001
        pass
    return None, "engine exported no build id"


_PROVENANCE_CACHE: dict = {}


def _provenance() -> dict:
    """{engine_build_id, engine_build_id_source, polymesh_commit, polymesh_commit_source}.

    Measured once per process (neither value can change under a running engine)."""
    if not _PROVENANCE_CACHE:
        bid, bsrc = _engine_build_id()
        pmc, psrc = _polymesh_commit_from_build()
        _PROVENANCE_CACHE.update({"engine_build_id": bid, "engine_build_id_source": bsrc,
                                  "polymesh_commit": pmc, "polymesh_commit_source": psrc})
    return dict(_PROVENANCE_CACHE)


def _short(commit: Optional[str], n: int = 12) -> str:
    return (commit or "unknown")[:n] if commit else "unknown"


# The film's dark field is rendered, not keyed. doc_render_view takes a `background` [r,g,b]
# (core/doc/session.cpp: the frame is cleared to it instead of the constant 245 light grey),
# so a shot for a dark room or a dark page is produced by the engine at full fidelity — the
# antialiased silhouette blends against the real background instead of against a light grey
# that a post-hoc chroma key then has to unpick, and a genuinely light-coloured part is no
# longer at risk of being keyed away with it. Shading, tinting and the overlay are unchanged.


# ------------------------------------------------------------------------------ footer
def _stamp_footer(png: str, left: str, right: str = "") -> str:
    """Burn a provenance footer band into `png` IN PLACE and return it.

    The footer is not decoration: a frame lifted out of a film into a slide deck has to
    carry which engine built it and which document it is of, or it is an unattributable
    picture. Never raises — a missing font must not be able to lose a frame."""
    try:
        from PIL import Image as PILImage, ImageDraw, ImageFont  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return png
    try:
        im = PILImage.open(png).convert("RGB")
        w, h = im.size
        out = PILImage.new("RGB", (w, h + _FOOTER_H), _BG)
        out.paste(im, (0, 0))
        dr = ImageDraw.Draw(out)
        font = None
        for cand in ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSansMono.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                     "/usr/share/fonts/dejavu/DejaVuSansMono.ttf"):
            if os.path.exists(cand):
                font = ImageFont.truetype(cand, 13)
                break
        if font is None:
            font = ImageFont.load_default()
        dr.line([(0, h), (w, h)], fill=(60, 62, 70), width=1)
        dr.text((8, h + 9), left, fill=(196, 200, 210), font=font)
        if right:
            try:
                tw = dr.textlength(right, font=font)
            except Exception:  # noqa: BLE001
                tw = 7 * len(right)
            dr.text((max(8.0, w - 8 - tw), h + 9), right, fill=(150, 154, 164), font=font)
        out.save(png)
        return png
    except Exception:  # noqa: BLE001
        return png


def _collision_text(measured: dict) -> str:
    """"INTERFERENCE A<->B at 42.9 deg" from the sweep's OWN interference record."""
    interf = measured.get("interference") or {}
    detail = interf.get("detail")
    pair = ""
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        pair = f" {detail[0].get('a')}<->{detail[0].get('b')}"
    val = interf.get("first_collision_value")
    unit = ((measured.get("driver") or {}).get("unit") or "")
    at = f" at {val} {unit}".rstrip() if val is not None else ""
    return f"INTERFERENCE{pair}{at}"


def _mark_collision(png: str, text: str) -> str:
    """Mark a COLLIDING sample: a red border plus the colliding pair, in place.

    The engine's renderer has no per-body colour override (doc_render_view tints by face
    direction), so a film cannot paint the two offending bodies red without reimplementing
    the shading. Marking the SAMPLE and naming the pair states exactly what
    simulate_motion reported instead of implying a body-level result it did not give."""
    try:
        from PIL import Image as PILImage, ImageDraw, ImageFont  # noqa: PLC0415
        im = PILImage.open(png).convert("RGB")
        dr = ImageDraw.Draw(im)
        w, h = im.size
        for k in range(6):
            dr.rectangle([k, k, w - 1 - k, h - 1 - k], outline=(226, 66, 58))
        font = None
        for cand in ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            if os.path.exists(cand):
                font = ImageFont.truetype(cand, max(18, h // 34))
                break
        font = font or ImageFont.load_default()
        dr.text((24, 20), text, fill=(240, 120, 112), font=font)
        im.save(png)
    except Exception:  # noqa: BLE001
        pass
    return png


def _caption_card(w: int, h: int, lines: list, out_png: str) -> str:
    """A dark title card holding `lines` — the 2-3 second hold that tells a viewer what the
    next shot IS. Rendered at the film's own size so no scaling is needed at encode time."""
    from PIL import Image as PILImage, ImageDraw, ImageFont  # noqa: PLC0415
    im = PILImage.new("RGB", (w, h), _BG)
    dr = ImageDraw.Draw(im)
    big = small = None
    for cand in ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(cand):
            big = ImageFont.truetype(cand, max(18, h // 22))
            break
    for cand in ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSansMono.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        if os.path.exists(cand):
            small = ImageFont.truetype(cand, max(13, h // 34))
            break
    big = big or ImageFont.load_default()
    small = small or ImageFont.load_default()
    y = int(h * 0.30)
    for i, line in enumerate(lines):
        font = big if i == 0 else small
        colour = (238, 240, 245) if i == 0 else (176, 182, 195)
        try:
            tw = dr.textlength(str(line), font=font)
        except Exception:  # noqa: BLE001
            tw = 8 * len(str(line))
        dr.text(((w - tw) / 2.0, y), str(line), fill=colour, font=font)
        y += int((h // 18) if i == 0 else (h // 26))
    im.save(out_png)
    return out_png


# ------------------------------------------------------------------------------ encode
def _run(cmd: list, timeout: float = 900.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ValueError("render_animation needs ffmpeg on PATH to encode the film "
                         "(the frames are rendered by the engine; only the container needs "
                         "ffmpeg). Install ffmpeg and retry.")
    return exe


def _ffprobe_film(path: str) -> dict:
    """MEASURED container facts: {duration_s, width, height, frames, codec}."""
    exe = shutil.which("ffprobe")
    if not exe:
        return {}
    p = _run([exe, "-v", "error", "-select_streams", "v:0", "-count_frames",
              "-show_entries", "stream=width,height,nb_read_frames,codec_name,avg_frame_rate",
              "-show_entries", "format=duration", "-of", "json", path], timeout=300.0)
    try:
        js = json.loads(p.stdout or "{}")
        st = (js.get("streams") or [{}])[0]
        dur = float((js.get("format") or {}).get("duration") or 0.0)
        return {"duration_s": round(dur, 3),
                "width": int(st.get("width") or 0), "height": int(st.get("height") or 0),
                "frames": int(st.get("nb_read_frames") or 0),
                "codec": st.get("codec_name"), "avg_frame_rate": st.get("avg_frame_rate")}
    except Exception:  # noqa: BLE001
        return {}


def _encode_gif(pattern: str, out: str, fps: int, width: Optional[int] = None) -> None:
    """Two-pass palettegen/paletteuse GIF. One global 256-colour palette; Bayer dithering
    keeps a shaded CAD surface smooth instead of speckled."""
    exe = _ffmpeg()
    vf = f"fps={int(fps)}"
    if width:
        vf += f",scale={int(width)}:-2:flags=lanczos"
    pal = os.path.join(os.path.dirname(out) or ".", f".palette_{os.getpid()}.png")
    p = _run([exe, "-y", "-v", "error", "-f", "image2", "-framerate", str(int(fps)),
              "-i", pattern, "-vf", vf + ",palettegen=stats_mode=diff", pal])
    if p.returncode != 0 or not os.path.exists(pal):
        raise ValueError(f"ffmpeg palettegen failed: {(p.stderr or '').strip()[:400]}")
    p = _run([exe, "-y", "-v", "error", "-f", "image2", "-framerate", str(int(fps)),
              "-i", pattern, "-i", pal, "-lavfi",
              vf + "[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
              "-loop", "0", out])
    try:
        os.unlink(pal)
    except OSError:
        pass
    if p.returncode != 0 or not os.path.exists(out):
        raise ValueError(f"ffmpeg gif encode failed: {(p.stderr or '').strip()[:400]}")


def _h264_encoder() -> str:
    """The H.264 encoder this ffmpeg actually HAS.

    A Fedora ffmpeg ships without libx264 (patent-clean build) — hard-coding it fails the
    whole film after every frame is already rendered. libopenh264 is the CPU fallback and
    h264_nvenc the GPU one; all three write yuv420p H.264, which is the only combination a
    browser, Keynote and PowerPoint all play."""
    have = _run([_ffmpeg(), "-hide_banner", "-encoders"], timeout=60.0).stdout or ""
    for enc in ("libx264", "libopenh264", "h264_nvenc"):
        if enc in have:
            return enc
    raise ValueError("this ffmpeg has no H.264 encoder (looked for libx264, libopenh264, "
                     "h264_nvenc) — save the animation as .gif instead.")


def _encode_mp4(pattern: str, out: str, fps: int, width: Optional[int] = None) -> None:
    exe = _ffmpeg()
    enc = _h264_encoder()
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if width:
        vf = f"scale={int(width)}:-2:flags=lanczos," + vf
    quality = (["-preset", "slow", "-crf", "20"] if enc == "libx264" else
               ["-preset", "p6", "-cq", "22", "-b:v", "0"] if enc == "h264_nvenc" else
               ["-b:v", "10M", "-maxrate", "12M", "-bufsize", "20M",
                "-allow_skip_frames", "0"])
    p = _run([exe, "-y", "-v", "error", "-f", "image2", "-framerate", str(int(fps)),
              "-i", pattern, "-vf", vf, "-c:v", enc, *quality,
              "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-r", str(int(fps)), out])
    if p.returncode != 0 or not os.path.exists(out):
        raise ValueError(f"ffmpeg {enc} encode failed: {(p.stderr or '').strip()[:400]}")


def _encode(pattern: str, out: str, fps: int, max_bytes: int, source_w: int) -> dict:
    """Encode, then walk the size ladder while the file is over `max_bytes`.

    Returns the MEASURED encode record: {fps, width, bytes, ladder:[...]}. `ladder` names
    every leg tried and its size, so a caption can state why the film is 20 fps."""
    is_gif = out.lower().endswith(".gif")
    ladder: list = []
    for w in (None,) + tuple(x for x in _WIDTH_LADDER if x < source_w):
        for f in (fps,) + tuple(x for x in _FPS_LADDER if x < fps):
            if is_gif:
                _encode_gif(pattern, out, f, w)
            else:
                _encode_mp4(pattern, out, f, w)
            size = os.path.getsize(out)
            ladder.append({"fps": f, "width": w or source_w, "bytes": size})
            if size <= max_bytes or not is_gif:
                return {"fps": f, "width": w or source_w, "bytes": size, "ladder": ladder}
    return {"fps": ladder[-1]["fps"], "width": ladder[-1]["width"],
            "bytes": ladder[-1]["bytes"], "ladder": ladder}


# ------------------------------------------------------------------------------ camera
def _view_angles(view) -> tuple:
    """(azimuth deg, elevation deg) for the orbit's starting camera.

    Accepts the render_part view vocabulary: a named view, {"azimuth","elevation"} or
    {"dir":[x,y,z]}. The default is the same 35.26 degree iso elevation render_part's
    (1,1,0.8) direction sits at, so a turntable opens on the familiar shot."""
    named = {"front": (0.0, 0.0), "back": (180.0, 0.0), "left": (-90.0, 0.0),
             "right": (90.0, 0.0), "top": (0.0, 89.0), "bottom": (0.0, -89.0),
             "iso": (45.0, 35.264)}
    if view is None:
        return 45.0, 35.264
    if isinstance(view, str):
        return named.get(view.strip().lower(), (45.0, 35.264))
    if isinstance(view, dict):
        if isinstance(view.get("dir"), (list, tuple)) and len(view["dir"]) == 3:
            x, y, z = (float(v) for v in view["dir"])
            r = math.hypot(x, y)
            return (math.degrees(math.atan2(x, -y)) if r > 1e-9 else 0.0,
                    math.degrees(math.atan2(z, r)) if (r > 1e-9 or abs(z) > 1e-9) else 0.0)
        return (float(view.get("azimuth", 45.0)), float(view.get("elevation", 35.264)))
    return 45.0, 35.264


def _dir_from_angles(az_deg: float, el_deg: float) -> tuple:
    """Scene->camera direction for (azimuth, elevation): azimuth 0 looks from -Y (FRONT),
    90 from +X (RIGHT), elevation 90 from +Z (TOP) — render_part's own convention."""
    az, el = math.radians(az_deg), math.radians(el_deg)
    ce = math.cos(el)
    return _vnorm((math.sin(az) * ce, -math.cos(az) * ce, math.sin(el)))


# ------------------------------------------------------------------------------ clones
def _doc_row(doc_id: str) -> dict:
    for row in (cb.call("doc_list", {}) or {}).get("docs", []):
        if row.get("doc_id") == doc_id:
            return row
    return {}


def _clone_detached(doc_id: str, kind: str) -> str:
    """A DETACHED copy of `doc_id`, opened from the document's own file.

    This is what keeps `explode`/`motion` honest: the poses are applied to the clone, so
    the caller's document is untouched — no ops, no revision bump, no dirty flag, no undo
    entry. `detach_path=True` also leaves the clone unbound to the file, so nothing this
    tool does can ever overwrite the source."""
    row = _doc_row(doc_id)
    path = row.get("path")
    if not path or not os.path.exists(str(path)):
        raise ValueError(
            f"render_animation(kind={kind!r}) renders a DETACHED CLONE of the document so "
            "your live doc is never modified, and a clone can only be opened from a file. "
            f"Document {doc_id} has no saved file — call save_part(path=...) first, then "
            "animate it.")
    r = cb.call("doc_open", {"path": str(path), "detach_path": True})
    clone = r.get("doc_id")
    if not clone:
        raise ValueError(f"doc_open({path}) returned no doc_id; cannot animate a clone.")
    return clone


def _components(doc_id: str) -> list:
    tr = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("tree") or {}
    comps = tr.get("components")
    return comps if isinstance(comps, list) else []


def _revision(doc_id: str) -> int:
    return int((cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("revision", 0) or 0)


def _apply_poses(doc_id: str, poses: dict) -> None:
    """Set component transforms on the CLONE: one doc_apply_ops with an asm_edit_component
    per component. Same op asm_add_component's re-pose path uses."""
    ops = [{"op": "asm_edit_component", "id": cid, "transform": [float(v) for v in t]}
           for cid, t in poses.items()]
    if not ops:
        return
    cb.call("doc_apply_ops", {"doc_id": doc_id, "base_revision": _revision(doc_id),
                              "with_tess": False, "ops": ops})


def _mat_mul(a: list, b: list) -> list:
    """Row-major 4x4 product a@b."""
    out = [0.0] * 16
    for i in range(4):
        for j in range(4):
            out[i * 4 + j] = sum(a[i * 4 + k] * b[k * 4 + j] for k in range(4))
    return out


def _rigid_inverse(m: list) -> list:
    """Inverse of a rigid row-major 4x4 (R^T, -R^T t) — exact, no general solve."""
    r = [[m[0], m[1], m[2]], [m[4], m[5], m[6]], [m[8], m[9], m[10]]]
    t = [m[3], m[7], m[11]]
    ti = [-sum(r[k][i] * t[k] for k in range(3)) for i in range(3)]
    return [r[0][0], r[1][0], r[2][0], ti[0],
            r[0][1], r[1][1], r[2][1], ti[1],
            r[0][2], r[1][2], r[2][2], ti[2],
            0.0, 0.0, 0.0, 1.0]


def _translation(v) -> list:
    return [1.0, 0.0, 0.0, float(v[0]),
            0.0, 1.0, 0.0, float(v[1]),
            0.0, 0.0, 1.0, float(v[2]),
            0.0, 0.0, 0.0, 1.0]


def _world_bodies(doc_id: str) -> list:
    return (cb.call("asm_world_bodies", {"doc_id": doc_id, "explode": False})
            or {}).get("bodies", []) or []


def _bbox_centre(bb: dict) -> tuple:
    lo, hi = bb.get("min") or [0, 0, 0], bb.get("max") or [0, 0, 0]
    return tuple((float(lo[i]) + float(hi[i])) / 2.0 for i in range(3))


def _scene_bbox(bodies: list) -> tuple:
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for b in bodies:
        bb = b.get("bbox") or {}
        for i in range(3):
            lo[i] = min(lo[i], float((bb.get("min") or [0, 0, 0])[i]))
            hi[i] = max(hi[i], float((bb.get("max") or [0, 0, 0])[i]))
    if lo[0] == float("inf"):
        return (0.0, 0.0, 0.0), 0.0
    centre = tuple((lo[i] + hi[i]) / 2.0 for i in range(3))
    diag = math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3)))
    return centre, diag


# ------------------------------------------------------------------------- FEA frames
def _fea_frames(vtu_path: str, out_dir: str, frames: int, size: tuple, view,
                warp_mm: Optional[float], field: str) -> dict:
    """Frames of a SOLVED field: von Mises on the warped mesh over the undeformed outline.

    The warp ramps 0 -> `warp_mm` of peak displacement so the deformation reads at all (a
    real 0.44 mm deflection on a 230 mm post is invisible at true scale); the grey wireframe
    of the UNDEFORMED grid stays behind it, which is what makes the exaggeration honest —
    the viewer sees both states. Returns the measured record (peak field value, peak
    displacement, node/cell counts, warp factor)."""
    import numpy as np  # noqa: PLC0415
    import pyvista as pv  # noqa: PLC0415
    pv.OFF_SCREEN = True
    grid = pv.read(vtu_path)
    names = list(grid.point_data.keys())
    fld = field if field in names else next(
        (n for n in names if n.lower() in ("von_mises", "vonmises", "von_mises_mpa")), None)
    if fld is None:
        raise ValueError(f"{vtu_path}: no von Mises point field (have {names or 'none'}). "
                         "Solve with run_fea_static(vtu_path=...) to get one.")
    disp = next((n for n in names if n.lower().startswith("disp")), None)
    vals = np.asarray(grid.point_data[fld], dtype=float)
    peak = float(np.nanmax(vals))
    peak_disp = 0.0
    if disp is not None:
        d = np.asarray(grid.point_data[disp], dtype=float)
        peak_disp = float(np.nanmax(np.linalg.norm(d, axis=1))) if d.ndim == 2 else \
            float(np.nanmax(np.abs(d)))
    span = float(np.linalg.norm(np.asarray(grid.bounds[1::2]) - np.asarray(grid.bounds[::2])))
    # Default exaggeration: peak deflection drawn at 6 % of the model diagonal — the same
    # rule ParaView's "auto" warp uses, stated in the caption as a factor so it is never
    # mistaken for true scale.
    target = float(warp_mm) if warp_mm else 0.06 * span
    factor = (target / peak_disp) if (peak_disp > 1e-12) else 0.0
    az, el = _view_angles(view)
    cam_dir = _dir_from_angles(az, el)
    surf = grid.extract_surface(algorithm="dataset_surface")
    written = []
    for i in range(frames):
        t = 0.5 - 0.5 * math.cos(math.pi * min(1.0, i / max(1, frames * 0.62)))  # ease-in hold
        pl = pv.Plotter(off_screen=True, window_size=[int(size[0]), int(size[1])])
        pl.set_background("#101114")
        pl.add_mesh(surf, style="wireframe", color="#4a4d55", line_width=1, opacity=0.55)
        warped = (grid.warp_by_vector(disp, factor=factor * t) if disp is not None else grid)
        pl.add_mesh(warped, scalars=fld, cmap="turbo", clim=[0.0, peak],
                    scalar_bar_args={"title": f"{fld} (MPa)", "color": "#e0e2e8",
                                     "n_labels": 5, "vertical": True, "position_x": 0.88,
                                     "position_y": 0.12, "height": 0.72, "width": 0.06},
                    smooth_shading=True)
        pl.camera_position = [
            tuple(c + d * span * 2.4 for c, d in zip(grid.center, cam_dir)),
            tuple(grid.center), (0.0, 0.0, 1.0)]
        pl.camera.zoom(1.25)
        png = os.path.join(out_dir, f"f{i:05d}.png")
        pl.screenshot(png)
        pl.close()
        written.append(png)
    return {"frames": written, "field": fld, "peak_field": peak, "peak_disp_mm": peak_disp,
            "warp_factor": factor, "warp_target_mm": target,
            "n_points": int(grid.n_points), "n_cells": int(grid.n_cells),
            "model_diag_mm": span,
            "cell_order": ("tet10" if grid.get_cell(0).n_points == 10 else "tet4")
            if grid.n_cells else "unknown"}


def register_cad_animation_tools(mcp: FastMCP) -> None:
    """The CAD animation MCP tool: render_animation (turntable | explode | motion | fea)."""

    @mcp.tool()
    def render_animation(kind: str = "turntable",
                         doc_id: Optional[str] = None,
                         save_path: Optional[str] = None,
                         frames: int = 90,
                         fps: int = 30,
                         size: Optional[list] = None,
                         view: Optional[Union[str, dict]] = None,
                         highlight: Optional[Union[str, dict, list]] = None,
                         display_quality: Optional[str] = None,
                         factor: float = 1.0,
                         components: Optional[list] = None,
                         reverse: bool = False,
                         mate_id: Optional[str] = None,
                         start: Optional[float] = None,
                         stop: Optional[float] = None,
                         steps: Optional[int] = None,
                         steer: Optional[dict] = None,
                         rear_steer: Optional[dict] = None,
                         poses: Optional[list] = None,
                         min_clearance_mm: float = 0.0,
                         vtu_path: Optional[str] = None,
                         field: str = "von_mises",
                         warp_mm: Optional[float] = None,
                         title: Optional[str] = None,
                         subtitle: Optional[list] = None,
                         hold_s: float = 0.0,
                         max_bytes: int = _GIF_MAX_BYTES,
                         frames_dir: Optional[str] = None,
                         footer_note: Optional[str] = None,
                         dark: bool = True,
                         annotate: bool = False) -> list:
        """Render a document as an ANIMATION (.gif or .mp4) — the artifact a review PLAYS.

        kind:
          "turntable" — a full 360 degree orbit of the document at the requested elevation.
                        Read-only: nothing but doc_render_view is called.
          "explode"   — every non-grounded component translates radially outward from the
                        assembly centroid, ramping 0 -> `factor` x the component's own
                        distance from that centroid. Needs an ASSEMBLY (components); the
                        poses are applied to a detached clone opened from the document's
                        file, so your document is never touched. `reverse=True` plays the
                        same move as an ASSEMBLE (parts fly in to their final transforms).
          "motion"    — a SOLVED trajectory, filmed one frame per sample: the engine's own
                        poses, never a second interpolation. Each sample is held on screen
                        until the film is `frames` long, because a solve can refuse a very
                        fine increment while a watchable shot still needs seconds of
                        frames. Every sample from the sweep's `first_collision_step` onward
                        is marked with a red border naming the colliding pair
                        (doc_render_view tints by face direction and has no per-body colour
                        override, so the film marks the SAMPLE and names the pair rather
                        than implying a body-level result the engine did not report); the
                        caption carries the step and driver value. THREE drives, exactly
                        one per call:
                          `mate_id` + `start`/`stop`/`steps` — simulate_motion sweeps a
                            dimensional (angle/distance) mate.
                          `steer` (+ optional `rear_steer`) — a STEER SWEEP for an assembly
                            that owns no drivable mate: named components are rigidly
                            rotated about a kingpin axis on a detached clone and the mate
                            network re-solves around them, ramping from 0 (straight ahead)
                            to the requested angle over `steps` samples. Same spec as
                            `steering_turning_radius` / `sweep_envelope`, driven by the
                            same helper:
                              steer={"components":["upright_FL","upright_FR"],
                                     "axis":{"point":[x,y,z],"dir":[0,0,1]},  # or mate_id
                                     "angle_deg":30}     # or "angles_deg":[30,26.4]
                            The caption reports `steer_range_deg` and each component's
                            requested angle; solid overlap is checked at ZERO clearance at
                            every sample (`min_clearance_mm` applies to the mate drive).
                          `poses` — a fully explicit frame list,
                            [{component: [16 numbers]}, ...], one object per frame, each
                            an ABSOLUTE row-major 4x4 for that component. The escape hatch
                            for a pose sequence you computed yourself; no solve is run.
          "fea"       — a solved .vtu: von Mises on the displacement-warped mesh over the
                        grey undeformed outline, warp ramping in. Pass `vtu_path` (from
                        run_fea_static(vtu_path=...)); no document is needed.

        save_path : REQUIRED absolute path ending .gif or .mp4, inside the project/workspace
                    root (same containment rule as every render deliverable). A poster PNG
                    is written beside it as poster_<stem>.png and returned inline.
        frames/fps/size : frame COUNT, playback rate, [width, height] px. Duration is
                    frames/fps; a GIF over `max_bytes` (25 MB default) walks fps then width
                    and the caption reports the leg it landed on.
        view      : starting camera — "iso"/"front"/... or {"azimuth","elevation"}/{"dir"}.
        highlight : render_part's highlight vocabulary (sid / feature id / query / list),
                    drawn on every frame.
        annotate  : overlay B-rep edges + scale bar on every frame (slower per frame).
        display_quality : "draft"|"standard"|"fine" display tessellation for the frames.
        title/subtitle/hold_s : a dark title card held for `hold_s` seconds at the head of
                    the film (2-3 s is a readable hold).
        frames_dir : optional ABSOLUTE directory the ordered PNG sequence (title card
                    frames first, then the film) is also written into as f00000.png... —
                    how a MULTI-SEGMENT chapter is composed: render each shot with its own
                    frames_dir, concatenate the directories in order, encode once. Every
                    frame already carries its provenance footer.
        footer_note : replaces the right-hand footer text on every frame — for numbers the
                    CALLER measured (a released packet's h / node count / peak stress);
                    the left half (engine build, polymesh commit, doc, frame) is not
                    overridable.
        dark      : default True — render the frames onto the film's dark field by passing
                    it to the engine as doc_render_view's `background`, instead of the
                    engine's flat 245 light grey. Shading, tinting and overlays are
                    untouched; dark=False keeps the engine's own light background exactly
                    as render_part shows it.

        Returns [poster Image, caption]. The caption states the written path, bytes,
        sha256, frame count, fps, measured duration from ffprobe, the engine build id and
        polymesh commit burned into every frame's footer, and each kind's own measured
        numbers (component offsets, sweep range / `steer_range_deg` and first collision,
        peak MPa and warp factor). Numbers the engine did not report read "unknown" —
        never a guess.
        """
        work: Optional[str] = None
        clone: Optional[str] = None
        try:
            kind = str(kind or "").strip().lower()
            if kind not in _KINDS:
                raise ValueError(f"render_animation: kind must be one of {', '.join(_KINDS)}; "
                                 f"got {kind!r}.")
            steer = _normalize_optional_slot(steer)
            rear_steer = _normalize_optional_slot(rear_steer)
            poses = _normalize_optional_slot(poses)
            off_kind = [k for k, v in (("mate_id", mate_id), ("steer", steer),
                                       ("rear_steer", rear_steer), ("poses", poses))
                        if v is not None]
            if kind != "motion" and off_kind:
                raise ValueError(
                    f"render_animation(kind={kind!r}): {', '.join(off_kind)} drive(s) "
                    "belong to kind='motion' — they would be silently ignored here.")
            if not save_path:
                raise ValueError(
                    "render_animation needs save_path — an ABSOLUTE .gif or .mp4 path under "
                    "your project folder. A film cannot be returned inline; the poster frame "
                    "is what comes back in the reply.")
            ext = os.path.splitext(str(save_path).strip())[1].lower()
            if ext not in (".gif", ".mp4"):
                raise ValueError("save_path must end in .gif or .mp4; got "
                                 f"{ext or 'no extension'!r}.")
            target = _render_save_target(save_path)          # absolute + containment + '..'
            if target.lower().endswith(ext + ".png"):
                target = target[: -len(".png")]
            frames = max(2, int(frames))
            fps = max(1, min(60, int(fps)))
            W, H = (int(size[0]), int(size[1])) if (isinstance(size, (list, tuple))
                                                    and len(size) == 2) else (1280, 720)
            W, H = max(160, W - W % 2), max(120, H - H % 2)
            prov = _provenance()
            work = tempfile.mkdtemp(prefix="chudware_anim_")
            t_start = time.time()
            measured: dict = {}
            doc_name = ""

            if kind == "fea":
                if not vtu_path or not os.path.exists(str(vtu_path)):
                    raise ValueError(
                        "render_animation(kind='fea') needs vtu_path — the .vtu a solve "
                        "wrote (run_fea_static(vtu_path='/abs/path.vtu')). "
                        f"{vtu_path!r} does not exist.")
                rec = _fea_frames(str(vtu_path), work, frames, (W, H - _FOOTER_H), view,
                                  warp_mm, field)
                measured = {k: v for k, v in rec.items() if k != "frames"}
                doc_name = os.path.basename(str(vtu_path))
                paths = rec["frames"]
                foot_right = (f"{measured['field']} peak {measured['peak_field']:.4g} MPa | "
                              f"warp x{measured['warp_factor']:.0f}")
            else:
                d = _doc(doc_id)
                doc_name = _handoff_doc_name(d) or d
                if display_quality:
                    cb.call("doc_set_display_quality",
                            {"doc_id": d, "quality": str(display_quality)})
                render_doc = d
                poses_per_frame: list = []
                if kind == "turntable":
                    az0, el = _view_angles(view)
                    dirs = [_dir_from_angles(az0 + 360.0 * i / frames, el)
                            for i in range(frames)]
                    bodies = _world_bodies(d)
                    _c, diag = _scene_bbox(bodies)
                    measured = {"bodies": len(bodies), "bbox_diag_mm": round(diag, 3),
                                "elevation_deg": round(el, 3),
                                "azimuth_start_deg": round(az0, 3), "azimuth_span_deg": 360.0}
                    foot_right = f"{len(bodies)} bodies | orbit 360 deg"
                elif kind == "explode":
                    clone = _clone_detached(d, kind)
                    render_doc = clone
                    if display_quality:
                        cb.call("doc_set_display_quality",
                                {"doc_id": clone, "quality": str(display_quality)})
                    comps = _components(clone)
                    if len(comps) < 2:
                        raise ValueError(
                            "render_animation(kind='explode') needs an ASSEMBLY with 2+ "
                            f"components; {doc_name} has {len(comps)}. A multi-body PART has "
                            "no components to move — build it with new_assembly + "
                            "asm_add_component (or animate kind='turntable').")
                    bodies = _world_bodies(clone)
                    centre, diag = _scene_bbox(bodies)
                    base = {c["id"]: list(c.get("transform")
                                          or _translation((0, 0, 0))) for c in comps}
                    body_centre: dict = {}
                    for b in bodies:
                        cid = b.get("component")
                        if cid in base and cid not in body_centre:
                            body_centre[cid] = _bbox_centre(b.get("bbox") or {})
                    want = set(components or []) or {c["id"] for c in comps
                                                     if not c.get("fixed")}
                    if not want:
                        want = {c["id"] for c in comps[1:]}
                    offsets: dict = {}
                    for cid in sorted(want):
                        bc = body_centre.get(cid)
                        if bc is None:
                            continue
                        vec = [bc[i] - centre[i] for i in range(3)]
                        n = math.sqrt(sum(v * v for v in vec))
                        if n < 1e-9:
                            continue
                        offsets[cid] = [v * float(factor) for v in vec]
                    if not offsets:
                        raise ValueError(
                            "render_animation(kind='explode'): every movable component sits "
                            "on the assembly centroid, so there is no outward direction to "
                            "explode along. Pass `components` explicitly.")
                    for i in range(frames):
                        t = i / (frames - 1)
                        t = 0.5 - 0.5 * math.cos(math.pi * t)          # ease in/out
                        if reverse:
                            t = 1.0 - t
                        poses_per_frame.append(
                            {cid: _mat_mul(_translation([o * t for o in off]), base[cid])
                             for cid, off in offsets.items()})
                    measured = {"components": len(comps), "moved": len(offsets),
                                "factor": float(factor),
                                "assembly_centroid_mm": [round(c, 3) for c in centre],
                                "bbox_diag_mm": round(diag, 3),
                                "max_offset_mm": round(max(
                                    math.sqrt(sum(v * v for v in off))
                                    for off in offsets.values()), 3),
                                "offsets_mm": {cid: [round(v, 3) for v in off]
                                               for cid, off in offsets.items()},
                                "direction": "assemble (reverse)" if reverse else "explode"}
                    foot_right = (("assemble" if reverse else "explode")
                                  + f" x{float(factor):g} | {len(offsets)} components")
                    az, el = _view_angles(view)
                    dirs = [_dir_from_angles(az, el)] * frames
                else:  # motion
                    # The SOLVER's sample count and the FILM's frame count are different
                    # numbers: a solve can fail to converge at a very fine increment (and
                    # each sample costs a solve), while a watchable shot needs 2-4 s of
                    # frames. Render one frame per solved sample and hold each on screen
                    # for as long as `frames` asks.
                    samples = max(2, int(steps)) if steps else frames
                    named = [k for k, v in (("mate_id", mate_id), ("steer", steer),
                                            ("poses", poses)) if v is not None]
                    if len(named) > 1:
                        raise ValueError(
                            "render_animation(kind='motion'): " + ", ".join(named) +
                            " are DIFFERENT drives — pass exactly one. mate_id sweeps a "
                            "dimensional mate, steer rotates named components about a "
                            "kingpin axis, poses is an explicit per-frame transform list.")
                    if steer is None and rear_steer is not None:
                        raise ValueError(
                            "render_animation: `rear_steer` is the 4WS companion to "
                            "`steer` — pass `steer` too (or drive the rear axle with "
                            "`steer` alone).")
                    if not named:
                        raise ValueError(
                            "render_animation(kind='motion') needs a drive: mate_id (+ "
                            "start/stop) for a dimensional mate, steer={'components':"
                            "[...],'axis':{'point':[...],'dir':[...]},'angle_deg':...} for "
                            "a steer sweep, or poses=[{component: [16 numbers]}, ...] for "
                            "an explicit frame list.")

                    # The component set and their poses are the same on the document and
                    # on its clone, so the drive is resolved (and refused) against the
                    # LIVE document first: a refusal must be the drive's own teaching
                    # text, not "this document has no file to clone from".
                    comps = _components(d)
                    base = {c["id"]: list(c.get("transform")
                                          or _translation((0, 0, 0))) for c in comps}

                    if poses is not None:
                        # The caller's own frame list: ABSOLUTE component transforms, taken
                        # verbatim. No solve, no interpolation — the only thing checked is
                        # that every component exists here and every matrix is a finite 4x4.
                        rows = require_list(
                            poses, "poses",
                            "one {component: [16 numbers]} object per frame")
                        by_key: dict = {}
                        for c in comps:
                            by_key[str(c["id"])] = str(c["id"])
                            if c.get("name"):
                                by_key[str(c["name"])] = str(c["id"])
                        for k, row in enumerate(rows):
                            row = coerce_json_or_repr(row)
                            if not isinstance(row, dict) or not row:
                                raise ValueError(
                                    f"poses[{k}] must be a non-empty object "
                                    "{component: [16 numbers]}; got "
                                    f"{type(row).__name__}.")
                            frame_poses: dict = {}
                            for token, m in row.items():
                                cid = by_key.get(str(token))
                                if cid is None:
                                    raise ValueError(
                                        f"poses[{k}]: component '{token}' is not in "
                                        f"{doc_name}. Components: "
                                        + ", ".join(f"{c['id']}({c.get('name')})"
                                                    for c in comps) + ".")
                                mm = coerce_json_or_repr(m)
                                if (not isinstance(mm, list) or len(mm) != 16
                                        or not all(isinstance(v, (int, float))
                                                   and math.isfinite(float(v))
                                                   for v in mm)):
                                    raise ValueError(
                                        f"poses[{k}]['{token}'] is not a row-major 4x4: "
                                        "give 16 finite numbers (the shape doc_tree and "
                                        "simulate_motion already report).")
                                frame_poses[cid] = [float(v) for v in mm]
                            poses_per_frame.append(frame_poses)
                        if len(poses_per_frame) < 2:
                            raise ValueError(f"poses has {len(poses_per_frame)} frame(s); "
                                             "an animation needs at least 2.")
                        moved = sorted({cid for row in poses_per_frame for cid in row})
                        measured = {"mate_id": None,
                                    "driver": {"type": "poses", "unit": "transform",
                                               "frames": len(poses_per_frame),
                                               "components": moved},
                                    "samples": len(poses_per_frame),
                                    "posed_components": len(moved)}
                        foot_right = (f"explicit poses | {len(poses_per_frame)} frames | "
                                      f"{len(moved)} components")
                    else:
                        if steer is not None:
                            # The motion tools' own steer driver, on its own detached
                            # clone: the film and a turning-radius study cannot disagree.
                            sweep = steer_pose_sweep(d, steer, rear_steer, samples,
                                                     True, "render_animation")
                            if not sweep.get("ok"):
                                raise ValueError(
                                    "render_animation(kind='motion', steer=...): the mate "
                                    "network did not solve around the steered pose — "
                                    f"{sweep.get('error')}")
                        else:
                            sweep = json.loads(_motion_tool(mate_id, start, stop, samples,
                                                            min_clearance_mm, d))
                            if not sweep.get("ok"):
                                raise ValueError(
                                    "render_animation(kind='motion'): simulate_motion "
                                    f"refused this sweep — {sweep.get('error')}")
                        traj = sweep.get("trajectory") or []
                        if len(traj) < 2:
                            raise ValueError("the sweep returned fewer than 2 samples; "
                                             "nothing to animate.")
                        first = {b.get("component"): list(b.get("transform") or [])
                                 for b in (traj[0].get("bodies") or [])
                                 if isinstance(b.get("transform"), list)}
                        for fr in traj:
                            frame_poses = {}
                            for b in fr.get("bodies") or []:
                                cid, tb = b.get("component"), b.get("transform")
                                if (cid not in base or not isinstance(tb, list)
                                        or len(tb) != 16):
                                    continue
                                t0 = first.get(cid)
                                if not t0 or len(t0) != 16:
                                    continue
                                # delta = T_i * inv(T_0) is the rigid motion the solver
                                # applied to this body between the first sample and this
                                # one; composing it with the component's own pose is exact
                                # even when a component carries several bodies.
                                frame_poses[cid] = _mat_mul(
                                    _mat_mul(tb, _rigid_inverse(t0)), base[cid])
                            poses_per_frame.append(frame_poses)
                        interf = sweep.get("interference") or {}
                        driver = sweep.get("driver") or {}
                        measured = {"mate_id": sweep.get("mate_id"),
                                    "driver": driver,
                                    "samples": len(traj),
                                    "interference": {
                                        "checked": interf.get("checked"),
                                        "first_collision_step": interf.get(
                                            "first_collision_step"),
                                        "first_collision_value": interf.get(
                                            "first_collision_value"),
                                        "detail": interf.get("detail")},
                                    "values": [fr.get("value") for fr in traj]}
                        unit = driver.get("unit") or ""
                        if steer is not None:
                            lo = float(driver.get("start") or 0.0)
                            hi = float(driver.get("stop") or 0.0)
                            # The steer drive checks solid overlap at zero clearance (it is
                            # the same eval_interference call steering_turning_radius
                            # makes); min_clearance_mm belongs to the mate sweep.
                            measured["min_clearance_mm"] = 0.0
                            measured["steer_range_deg"] = [round(lo, 4), round(hi, 4)]
                            measured["steer_components"] = driver.get("components")
                            measured["steer_angles_deg"] = driver.get("angles_deg")
                            measured["steer_axes"] = driver.get("axes")
                            foot_right = ("steer "
                                          + ", ".join(driver.get("components") or [])
                                          + f" {lo:g}->{hi:g} {unit or 'deg'}")
                        else:
                            measured["min_clearance_mm"] = float(min_clearance_mm)
                            foot_right = (f"mate {sweep.get('mate_id')} "
                                          f"{traj[0].get('value')}->"
                                          f"{traj[-1].get('value')} {unit}")

                    clone = _clone_detached(d, kind)
                    render_doc = clone
                    if display_quality:
                        cb.call("doc_set_display_quality",
                                {"doc_id": clone, "quality": str(display_quality)})
                    az, el = _view_angles(view)
                    dirs = [_dir_from_angles(az, el)] * len(poses_per_frame)

                # ---- the frames themselves: the engine's own renders, one per pose ----
                paths = []
                render_count = len(poses_per_frame) if poses_per_frame else frames
                collide_from = None
                if kind == "motion":
                    cs = (measured.get("interference") or {}).get("first_collision_step")
                    collide_from = int(cs) if isinstance(cs, int) else None
                bg = list(_BG) if dark else None
                for i in range(render_count):
                    if poses_per_frame:
                        _apply_poses(render_doc, poses_per_frame[i])
                    png = os.path.join(work, f"f{i:05d}.png")
                    png, frame, _r = _render_view_png(render_doc, dirs[i], png, name="",
                                                      W=W, H=H - _FOOTER_H,
                                                      annotate=bool(annotate),
                                                      background=bg)
                    if annotate or highlight is not None:
                        png, _info = _annotate_render(
                            png, render_doc, dirs[i], highlight=highlight,
                            edges=bool(annotate), callouts=False, scale=bool(annotate),
                            frame=frame, out_png=png)
                    if collide_from is not None and i >= collide_from:
                        _mark_collision(png, _collision_text(measured))
                    paths.append(png)

            # ---- provenance footer on every RENDERED frame ----
            stem = os.path.splitext(os.path.basename(target))[0]
            left = (f"chudware {_short(prov['engine_build_id'], 40)} | polymesh "
                    f"{_short(prov['polymesh_commit'])} | {doc_name}")
            if footer_note:
                foot_right = str(footer_note)
            word = "sample" if kind == "motion" else "frame"
            for i, png in enumerate(paths):
                _stamp_footer(png, f"{left} | {word} {i + 1}/{len(paths)}", foot_right)

            # ---- playback: hold each solved sample until the film is `frames` long ----
            if len(paths) < frames:
                paths = [paths[min(len(paths) - 1, (i * len(paths)) // frames)]
                         for i in range(frames)]
                measured["playback"] = (
                    f"{len(set(paths))} rendered poses held across {frames} frames at "
                    f"{fps} fps (the solver's sample count, not the film's frame rate, "
                    "sets how many poses exist)")

            # ---- optional title card hold, prepended as real frames ----
            head = 0
            ordered = list(paths)
            if title and hold_s > 0:
                head = max(1, int(round(float(hold_s) * fps)))
                card = _caption_card(W, H, [title] + list(subtitle or []),
                                     os.path.join(work, "card.png"))
                for k in range(head):
                    dst = os.path.join(work, f"c{k:05d}.png")
                    shutil.copyfile(card, dst)
                ordered = [os.path.join(work, f"c{k:05d}.png")
                           for k in range(head)] + list(paths)
            # One contiguously numbered sequence, ALWAYS: what ffmpeg's image2 demuxer
            # needs, and what a caller composing a multi-segment chapter out of several
            # render_animation calls concatenates. It cannot be skipped when the film
            # happens to have no title card — `ordered` also carries the HELD repeats of
            # each solved sample, and pointing the demuxer at the rendered f%05d.png
            # files instead silently encoded one frame per sample (a 3-sample steer sweep
            # asked for 8 frames and played 3).
            seq = os.path.join(work, "seq")
            os.makedirs(seq, exist_ok=True)
            for k, src in enumerate(ordered):
                shutil.copyfile(src, os.path.join(seq, f"f{k:05d}.png"))
            pattern = os.path.join(seq, "f%05d.png")
            total = len(ordered)
            exported = None
            if frames_dir:
                if not os.path.isabs(str(frames_dir)):
                    raise ValueError("frames_dir must be an ABSOLUTE directory path.")
                os.makedirs(str(frames_dir), exist_ok=True)
                for k in range(total):
                    shutil.copyfile(os.path.join(work, "seq", f"f{k:05d}.png"),
                                    os.path.join(str(frames_dir), f"f{k:05d}.png"))
                exported = str(frames_dir)

            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            enc = _encode(pattern, target, fps, int(max_bytes), W)
            probe = _ffprobe_film(target)
            with open(target, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            poster_src = paths[min(len(paths) - 1, int(len(paths) * 0.62))]
            poster = os.path.join(os.path.dirname(target), f"poster_{stem}.png")
            shutil.copyfile(poster_src, poster)
            inline = render_output_path(_render_stem(doc_name or stem, "poster"), ".png")
            shutil.copyfile(poster_src, inline)

            dur = probe.get("duration_s") or round(total / float(enc["fps"]), 3)
            cap = [
                f"{kind} animation -> {target}",
                f"{enc['bytes']} B ({enc['bytes'] / 1048576.0:.2f} MiB), sha256 {sha[:12]}",
                f"{total} frames ({len(set(paths))} rendered"
                + (f" + {head} title-card" if head else "") + f") at {enc['fps']} fps, "
                f"{enc['width']}x{probe.get('height') or H} px, duration {dur} s measured by "
                f"ffprobe (codec {probe.get('codec') or 'n/a'}, "
                f"{probe.get('frames') or '?'} frames read back)",
                f"engine build {prov['engine_build_id'] or 'unknown'} "
                f"({prov['engine_build_id_source']}); polymesh commit "
                f"{prov['polymesh_commit'] or 'unknown'} ({prov['polymesh_commit_source']}) "
                "- both burned into every frame footer",
                f"poster {poster}",
                f"measured: {json.dumps(measured, sort_keys=True, default=str)}",
                f"render wall time {round(time.time() - t_start, 2)} s",
            ]
            if exported:
                cap.append(f"frames exported: {total} PNG in {exported}")
            if len(enc["ladder"]) > 1:
                cap.append("size ladder (fps, then width; the film keeps every frame's "
                           f"content and both end points): {json.dumps(enc['ladder'])}")
            if enc["bytes"] > int(max_bytes):
                cap.append(f"WARNING: {enc['bytes']} B is still over the {int(max_bytes)} B "
                           "budget after the whole ladder — lower `frames` or `size`.")
            return [Image(path=inline), "  ".join(cap)]
        except Exception as e:  # noqa: BLE001
            return [_err(e)]
        finally:
            if clone:
                try:
                    cb.call("doc_close", {"doc_id": clone})
                except Exception:  # noqa: BLE001
                    pass
            if work and os.path.isdir(work):
                shutil.rmtree(work, ignore_errors=True)

    # simulate_motion lives in the "sim"-tagged assembly module; the animation calls it
    # rather than re-deriving a trajectory, so a film and a motion study can never disagree.
    def _motion_tool(mate_id, start, stop, steps, min_clearance_mm, doc):
        from .cad_asm_tools import register_cad_asm_tools  # noqa: PLC0415

        class _Capture:
            def __init__(self):
                self.tools: dict = {}

            def tool(self, *_a, **_k):
                def dec(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return dec

        cap = _Capture()
        register_cad_asm_tools(cap)
        return cap.tools["simulate_motion"](
            mate_id=mate_id, start=start, stop=stop, steps=max(2, int(steps)),
            check_interference=True, min_clearance_mm=float(min_clearance_mm), doc_id=doc)

    return None
