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
from functools import lru_cache
import glob
import re
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
    resolve_agent_path,
)
# The one steer-pose driver. A steer film and a turning-radius study must pose the same
# detached clone the same way, so the animation drives the motion tools' own helper
# instead of re-deriving a rotation about a kingpin axis.
from .cad_motion_tools import steer_pose_sweep
# The save-path contract every render deliverable already obeys (a relative path is
# project-root relative; the file stays inside a configured project/workspace root; '..'
# is collapsed first). Imported, never re-implemented: an animation is a figure like any
# other, and a second interpretation of the same rule is how a film ends up somewhere
# nobody can point a report at.
from .cad_render_tools import _render_save_target
from .cad_render_tools import _render_stem

_KINDS = ("turntable", "explode", "motion", "fea")
_GIF_MAX_BYTES = 25 * 1024 * 1024
# The size ladder a too-large GIF walks: output fps first (the film keeps its full duration
# and both end points), then width. Never content.
_FPS_LADDER = (30, 24, 20, 15, 12, 10)
_WIDTH_LADDER = (1280, 1024, 854, 720, 640, 512)
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

    Daemon mode's negotiated ``hello`` contains its own ``engine_build_id``; in-process
    mode the library exports ``chudware_build_id``. Either way the value comes from the running
    binary, not from a file this module guessed at."""
    if cb.active_daemon_mode():
        try:
            from . import daemon_bridge  # noqa: PLC0415
            connection = daemon_bridge.client()
            info = connection.server_info
            source = "hostd hello.engine_build_id"
            if not info.get("engine_build_id"):
                if daemon_bridge.in_cell():
                    return None, "hostd hello.engine_build_id unavailable during admission"
                # An admission accepted while the engine loads has no build id yet.
                info = connection.call("daemon_status", {})
                source = "hostd daemon_status.engine_build_id"
            # Repeating hello changes the shared connection's writer identity. Provenance
            # must observe its negotiated metadata, not re-register the client.
            bid = str(info.get("engine_build_id") or "").strip()
            if bid:
                return bid, source
            return None, source + " unavailable"
        except Exception:  # noqa: BLE001
            return None, "hostd engine_build_id unavailable"
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
@lru_cache(maxsize=64)
def _font(bold: bool, px: int):
    """Resolve a scalable face as the showcase cards do, including Fedora's Noto fonts."""
    from PIL import ImageFont
    candidates = (("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if bold else
                  ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSansMono.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"))
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, px)
    pattern = "DejaVu Sans:bold,sans-serif:bold" if bold else "DejaVu Sans Mono,monospace"
    try:
        found = subprocess.run(["fc-match", "-f", "%{file}", pattern],
                               capture_output=True, text=True, check=True).stdout.strip()
        if found and os.path.isfile(found):
            return ImageFont.truetype(found, px)
    except (OSError, subprocess.CalledProcessError):
        pass
    raise ValueError("Animation typography needs a scalable font; install "
                     "dejavu-sans-fonts or google-noto-sans-mono-fonts and fontconfig.")


def _wrap_to_width(text: str, font, width: int) -> list:
    """Wrap measured glyphs, splitting long paths/tokens rather than clipping evidence."""
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if font.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            for char in word:
                if font.getlength(line + char) > width:
                    if not line:
                        raise ValueError("Animation text region is narrower than one glyph.")
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return lines or [""]


def _text_rows(text: str, font, width: int) -> tuple:
    rows = _wrap_to_width(text, font, width)
    ascent, descent = font.getmetrics()
    return rows, ascent + descent + max(2, font.size // 5)


def _footer_layout(width: int, left: str, right: str) -> tuple:
    pad = max(8, round(width * 0.012))
    font = _font(False, max(10, round(width / 72)))
    left_rows, pitch = _text_rows(left, font, width - 2 * pad)
    right_rows = _wrap_to_width(right, font, width - 2 * pad) if right else []
    needed = 2 * pad + pitch * (len(left_rows) + len(right_rows))
    return font, pad, pitch, left_rows, right_rows, needed


def _stamp_footer(png: str, left: str, right: str = "", *, height: Optional[int] = None) -> str:
    """Append two vertically separated, fully wrapped provenance fields; never overprint."""
    from PIL import Image as PILImage, ImageDraw
    with PILImage.open(png) as source:
        im = source.convert("RGB")
    w, h = im.size
    font, pad, pitch, left_rows, right_rows, needed = _footer_layout(w, left, right)
    if height is not None and needed > height:
        raise ValueError(f"Animation footer needs {needed}px, has {height}px; "
                         "shorten footer_note/document name or increase output size.")
    out = PILImage.new("RGB", (w, h + (height or needed)), _BG)
    out.paste(im, (0, 0))
    dr = ImageDraw.Draw(out)
    dr.line([(0, h), (w, h)], fill=(60, 62, 70))
    y = h + pad
    for rows, colour in ((left_rows, (196, 200, 210)), (right_rows, (150, 154, 164))):
        for row in rows:
            dr.text((pad, y), row, fill=colour, font=font, anchor="lt")
            y += pitch
    out.save(png)
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


def _mark_collision(png: str, text: str, *, current: bool = True) -> str:
    """A red current-hit banner or amber historical warning, never a body recolouring."""
    from PIL import Image as PILImage, ImageDraw
    with PILImage.open(png) as source:
        im = source.convert("RGB")
    dr = ImageDraw.Draw(im)
    w, h = im.size
    pad = max(8, round(w * 0.018))
    font = _font(True, max(12, round(w / 48)))
    rows, pitch = _text_rows(text, font, w - 2 * pad)
    band = 2 * pad + len(rows) * pitch
    if band > h // 2:
        raise ValueError("Collision legend exceeds half the frame; increase output size.")
    colour = (240, 120, 112) if current else (238, 188, 94)
    dr.rectangle((0, 0, w - 1, band), fill=_BG)
    dr.rectangle((0, 0, w - 1, h - 1), outline=colour, width=max(2, w // 320))
    for i, row in enumerate(rows):
        dr.text((pad, pad + i * pitch), row, fill=colour, font=font, anchor="lt")
    im.save(png)
    return png


def _caption_card(w: int, h: int, lines: list, out_png: str) -> str:
    """Center fully wrapped title/body blocks, refusing text that cannot remain readable."""
    from PIL import Image as PILImage, ImageDraw
    im = PILImage.new("RGB", (w, h), _BG)
    dr = ImageDraw.Draw(im)
    pad = max(12, round(w * 0.06))
    blocks = []
    total = 0
    for i, line in enumerate(lines):
        font = _font(i == 0, max(16 if i == 0 else 12,
                                round(h / (22 if i == 0 else 34))))
        rows, pitch = _text_rows(str(line), font, w - 2 * pad)
        gap = pitch // 2
        blocks.append((rows, pitch, gap, font, i == 0))
        total += len(rows) * pitch + gap
    if total > h - 2 * pad:
        raise ValueError("Title card text does not fit at readable size; split the card "
                         "or shorten the subtitle.")
    y = (h - total) // 2
    for rows, pitch, gap, font, bold in blocks:
        for row in rows:
            dr.text(((w - font.getlength(row)) / 2, y), row, font=font, anchor="lt",
                    fill=(238, 240, 245) if bold else (176, 182, 195))
            y += pitch
        y += gap
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


def _encode_gif(pattern: str, out: str, source_fps: int, width: Optional[int] = None,
                *, output_fps: Optional[int] = None) -> None:
    """Resample the source timeline, preserving endpoints and duration (GIF: 10ms ticks)."""
    exe = _ffmpeg()
    count = len(glob.glob(re.sub(r"%0?\d*d", "*", pattern)))
    if not count:
        raise ValueError(f"No animation frames match {pattern}")
    output_fps = output_fps or source_fps
    samples = min(count, max(2, round(count * output_fps / source_fps)))
    # Select evenly spaced SOURCE indices including 0 and count-1, not interpolated poses.
    # The output rate is adjusted by <1 frame to span the original duration exactly.
    vf = ""
    if samples < count:
        vf = (f"select='eq(n,round(round(n*{samples - 1}/{count - 1})"
              f"*{count - 1}/{samples - 1}))',")
    vf += f"settb=1/100,setpts=round(N*100*{count}/({samples}*{source_fps}))"
    if width:
        vf += f",scale={int(width)}:-2:flags=lanczos"
    duration_cs = math.floor(100 * count / source_fps + 0.5)
    last_pts_cs = math.floor(100 * (samples - 1) * count / (samples * source_fps) + 0.5)
    final_delay = max(1, duration_cs - last_pts_cs)
    pal = os.path.join(os.path.dirname(out) or ".", f".palette_{os.getpid()}.png")
    p = _run([exe, "-y", "-v", "error", "-f", "image2", "-framerate", str(source_fps),
              "-i", pattern, "-vf", vf + ",palettegen=stats_mode=diff", pal])
    if p.returncode != 0 or not os.path.exists(pal):
        raise ValueError(f"ffmpeg palettegen failed: {(p.stderr or '').strip()[:400]}")
    p = _run([exe, "-y", "-v", "error", "-f", "image2", "-framerate", str(source_fps),
              "-i", pattern, "-i", pal, "-lavfi",
              vf + "[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
              "-fps_mode", "vfr", "-enc_time_base", "1:100", "-final_delay", str(final_delay),
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
                _encode_gif(pattern, out, fps, w, output_fps=f)
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


def _freeze_clone_poses(doc_id: str) -> None:
    """Prevent mate solving from changing playback poses on the render copy."""
    tree = cb.call("doc_tree", {"doc_id": doc_id})["tree"]
    ops = [{"op": "asm_edit_mate", "id": mate["id"], "suppressed": True}
           for mate in tree.get("mates", []) if not mate.get("suppressed")]
    ops.extend({"op": "asm_edit_component", "id": component["id"], "fixed": True}
               for component in tree.get("components", []) if not component.get("fixed"))
    if ops:
        frozen = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops, "with_tess": False})
        if frozen.get("ok") is False:
            raise ValueError(f"Cannot freeze animation poses: {frozen}")


def _revision(doc_id: str) -> int:
    return int((cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("revision", 0) or 0)


def _apply_poses(doc_id: str, poses: dict) -> None:
    """Set component transforms on the CLONE: one doc_apply_ops with an asm_edit_component
    per component. Same op asm_add_component's re-pose path uses."""
    ops = [{"op": "asm_edit_component", "id": cid, "transform": [float(v) for v in t]}
           for cid, t in poses.items()]
    if not ops:
        return
    result = cb.call("doc_apply_ops", {"doc_id": doc_id, "base_revision": _revision(doc_id),
                                      "with_tess": False, "ops": ops})
    if result.get("ok") is False:
        raise ValueError(f"Animation component pose failed: {result.get('error') or result}")


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


def _pose_bounds(bodies: list, base: dict, poses: list) -> dict:
    """Conservative union of source WORLD boxes transported by relative rigid placements.

    Partial pose rows have the same carry-forward semantics as _apply_poses. No geometry
    is re-queried, and unposed bodies stay in the frame. Synthetic pattern instances need
    native preflight instead: their WORLD delta is offset * seed_delta * offset^-1, and
    asm_world_bodies does not expose those exact compiled offsets.
    """
    if poses and _has_pattern_instances(bodies, base):
        raise ValueError("Animation pattern framing needs measured posed world bounds.")
    points = {}
    for body in bodies:
        bb = body.get("bbox") or {}
        if (not all(len(bb.get(k, [])) == 3 for k in ("min", "max"))
                or not all(isinstance(v, (int, float)) and math.isfinite(v)
                           for k in ("min", "max") for v in bb[k])
                or any(bb["min"][a] > bb["max"][a] for a in range(3))):
            raise ValueError("Animation framing needs measured bounds for every body.")
        cid = str(body.get("component") or "")
        points.setdefault(cid, []).extend(
            tuple(float(bb["max" if mask & (1 << axis) else "min"][axis])
                  for axis in range(3)) for mask in range(8))
    if not points:
        raise ValueError("Animation framing has no measured scene bounds.")
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    inverse = {cid: _rigid_inverse(m) for cid, m in base.items()}
    current = dict(base)
    for row in [{}] + poses:
        current.update(row)
        for cid, corners in points.items():
            delta = _mat_mul(current[cid], inverse[cid]) if cid in inverse else None
            for point in corners:
                p = ([sum(delta[a * 4 + k] * point[k] for k in range(3))
                      + delta[a * 4 + 3] for a in range(3)] if delta else point)
                for a in range(3):
                    lo[a], hi[a] = min(lo[a], p[a]), max(hi[a], p[a])
    return {"min": lo, "max": hi}


def _has_pattern_instances(bodies: list, base: dict) -> bool:
    return any("#" in str(b.get("component") or "") and b.get("component") not in base
               for b in bodies)


def _pattern_pose_bounds(clone: str, bodies: list, base: dict, poses: list) -> dict:
    """Measure each sampled placement on the detached render clone, then restore its base.

    Compiled pattern offsets are not in world-body metadata; reconstructing them from
    feature parameters would duplicate the engine and misframe solved/nested placements.
    Keep full instance identities and refuse missing bodies/bounds instead of guessing.
    """
    bounds = _pose_bounds(bodies, {}, [])
    identities = sorted(str(b.get("component") or "") for b in bodies)
    try:
        _apply_poses(clone, base)
        for index, row in enumerate([{}] + poses):
            _apply_poses(clone, row)
            posed = _world_bodies(clone)
            if sorted(str(b.get("component") or "") for b in posed) != identities:
                raise ValueError(
                    f"Animation pattern framing sample {index}: world bodies changed "
                    "or could not be measured.")
            measured = _pose_bounds(posed, {}, [])
            for a in range(3):
                bounds["min"][a] = min(bounds["min"][a], measured["min"][a])
                bounds["max"][a] = max(bounds["max"][a], measured["max"][a])
    except Exception as exc:
        raise ValueError(f"Animation pattern framing preflight failed: {exc}") from exc
    finally:
        _apply_poses(clone, base)
    return bounds


def _orbit_framing(bounds: dict, direction, width: int, height: int) -> tuple:
    """Fit the full orbit at constant scale, including end-on slender parts.

    A small centered camera-fit cube avoids the native focus-margin cap. It is
    not geometry or a clipping volume: the actual bounds determine the scale.
    """
    d = _vnorm(direction)
    polar = abs(d[2]) > 0.985
    ref = (0, 1, 0) if polar else (0, 0, 1)
    right = _vnorm((ref[1] * d[2] - ref[2] * d[1],
                    ref[2] * d[0] - ref[0] * d[2],
                    ref[0] * d[1] - ref[1] * d[0]))
    up = (d[1] * right[2] - d[2] * right[1],
          d[2] * right[0] - d[0] * right[2],
          d[0] * right[1] - d[1] * right[0])
    span = [bounds["max"][a] - bounds["min"][a] for a in range(3)]
    if polar:
        max_u = max_v = math.sqrt(sum(s * s for s in span))
    else:
        max_u = math.hypot(span[0], span[1])
        max_v = abs(d[2]) * max_u + math.hypot(d[0], d[1]) * span[2]
    # Match the native focus fitter's two-percent minimum projected extent.
    target = 1.16 * max(max(max_u, 0.02 * max_v, 2e-5) / width,
                       max(max_v, 0.02 * max_u, 2e-5) / height)
    side = target * min(width, height) / math.sqrt(3)
    u = side * sum(abs(v) for v in right)
    v = side * sum(abs(value) for value in up)
    projected = max(max(u, 0.02 * v, 2e-5) / width,
                    max(v, 0.02 * u, 2e-5) / height)
    center = _bbox_centre(bounds)
    focus = {"min": [c - side / 2 for c in center],
             "max": [c + side / 2 for c in center]}
    return focus, max(0.0, (target / projected - 1) / 2)


def _animation_raster_size(scene_size: tuple) -> tuple:
    supersample = min(2.0, 4096 / max(scene_size))
    if supersample < 1:
        raise ValueError("Animation output dimensions exceed the renderer's "
                         "4096px limit; request a supported size.")
    return tuple(max(64, min(4096, round(x * supersample))) for x in scene_size)


def _downsample(png: str, size: tuple) -> tuple:
    """Downsample geometry before any film typography; never recolour CAD pixels."""
    from PIL import Image as PILImage
    with PILImage.open(png) as im:
        rendered = im.size
        if rendered != size:
            im.resize(size, PILImage.Resampling.LANCZOS).save(png)
    return rendered


def _collision_legend(measured: dict, index: int) -> tuple:
    """Return (text, current-hit) only from evidence for this sample."""
    samples = measured.get("sample_collisions") or []
    if index < len(samples) and samples[index] is not None:
        if not samples[index]:
            return "", False
        record = {"interference": {"detail": samples[index]}}
        return _collision_text(record), True
    first = (measured.get("interference") or {}).get("first_collision_step")
    if isinstance(first, int) and index == first:
        return _collision_text(measured), True
    if isinstance(first, int) and index > first:
        return "Prior collision detected; later state not checked", False
    return "", False


# ------------------------------------------------------------------------- FEA frames
_GENERIC_VON_MISES = ("von_mises", "vonmises", "von_mises_mpa")
_CELL_AVERAGED_NOTE = ("The file has this field only as cell data. The frames show, at each "
                       "node, the volume-weighted average of the retained incident cells.")


def _cell_to_point_average(grid, values):
    """Volume-weighted nodal average of a cell scalar, the rule the topology engine uses
    for reanalysis.vtu point von Mises: only cells with retained == 1 count (all cells when
    the file has no `retained` array); a node with no counted cell gets 0."""
    import numpy as np  # noqa: PLC0415
    vol = np.abs(np.asarray(grid.compute_cell_sizes(length=False, area=False, volume=True)
                            .cell_data["Volume"], dtype=float))
    if "retained" in grid.cell_data:
        vol = vol * (np.asarray(grid.cell_data["retained"], dtype=float) > 0)
    offsets = np.asarray(grid.offset, dtype=np.int64)
    conn = np.asarray(grid.cell_connectivity, dtype=np.int64)
    owner = np.repeat(np.arange(grid.n_cells), np.diff(offsets))
    weight = np.zeros(grid.n_points)
    total = np.zeros(grid.n_points)
    np.add.at(weight, conn, vol[owner])
    np.add.at(total, conn, vol[owner] * np.asarray(values, dtype=float)[owner])
    out = np.zeros(grid.n_points)
    np.divide(total, weight, out=out, where=weight > 0)
    return out


def _fea_field(grid, field: str) -> tuple:
    """Resolve (point field, displacement field or None, source) for an FEA film.

    source is "point" for a solved point field. If the requested von Mises exists only as
    cell data (a topology reanalysis.vtu written before point stress), it becomes a point
    field of the same name in `grid` and source is "cell_averaged". A per-case stress
    (von_mises@<case>) warps only by displacement@<case>; without it there is no warp."""
    names = list(grid.point_data.keys())
    cells = list(grid.cell_data.keys())
    generic = field.lower() in _GENERIC_VON_MISES
    fld = field if field in names else next(
        (n for n in names if generic and n.lower() in _GENERIC_VON_MISES), None)
    source = "point"
    if fld is None:
        cell = field if field in cells else next(
            (n for n in cells if generic and n.lower() in _GENERIC_VON_MISES), None)
        if cell is not None:
            grid.point_data[cell] = _cell_to_point_average(grid, grid.cell_data[cell])
            fld, source = cell, "cell_averaged"
    if fld is None:
        per_case = sorted({n for n in names + cells if n.startswith("von_mises@")})
        hint = (f"Set field to one of {per_case}." if per_case else
                "Solve with run_fea_static(vtu_path=...) to get one.")
        raise ValueError(f"no von Mises field '{field}' (point fields: {names or 'none'}; "
                         f"cell fields: {cells or 'none'}). {hint}")
    case = fld.partition("@")[2]
    if case:
        disp = f"displacement@{case}" if f"displacement@{case}" in names else None
    else:
        disp = next((n for n in names if n.lower().startswith("disp")), None)
    return fld, disp, source


def _fea_frames(vtu_path: str, out_dir: str, frames: int, size: tuple, view,
                warp_mm: Optional[float], field: str) -> dict:
    """Frames of a SOLVED field: von Mises on the warped mesh over the undeformed outline.

    The warp ramps 0 -> `warp_mm` of peak displacement so the deformation reads at all (a
    real 0.44 mm deflection on a 230 mm post is invisible at true scale); the grey wireframe
    of the UNDEFORMED grid stays behind it, which is what makes the exaggeration honest —
    the viewer sees both states. Returns the measured record (peak field value, peak
    displacement, node/cell counts, warp factor, field source)."""
    import numpy as np  # noqa: PLC0415
    import pyvista as pv  # noqa: PLC0415
    pv.OFF_SCREEN = True
    grid = pv.read(vtu_path)
    try:
        fld, disp, source = _fea_field(grid, field)
    except ValueError as exc:
        raise ValueError(f"{vtu_path}: {exc}") from None
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
        t = 0.5 - 0.5 * math.cos(math.pi * i / max(1, frames - 1))
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
    record = {"frames": written, "field": fld, "field_source": source,
              "displacement_field": disp, "peak_field": peak, "peak_disp_mm": peak_disp,
              "warp_factor": factor, "warp_target_mm": target,
              "n_points": int(grid.n_points), "n_cells": int(grid.n_cells),
              "model_diag_mm": span,
              "cell_order": ("tet10" if grid.get_cell(0).n_points == 10 else "tet4")
              if grid.n_cells else "unknown"}
    if source == "cell_averaged":
        record["field_note"] = _CELL_AVERAGED_NOTE
    return record


def _keyframe_indices(frame_count: int, count: int,
                      collision_index: Optional[int] = None) -> list[int]:
    """Select rendered frames, preserving endpoints and a recorded collision."""
    if count <= 0 or frame_count <= 0:
        return []
    n = min(frame_count, max(2, min(12, count)))
    if n == 1:
        return [0]
    indices = [round(i * (frame_count - 1) / (n - 1)) for i in range(n)]
    if (isinstance(collision_index, int) and not isinstance(collision_index, bool)
            and 0 <= collision_index < frame_count and collision_index not in indices):
        # The endpoint tiles are never displaced. With only two requested tiles,
        # add a third rather than losing either the start or the finish.
        if n == 2:
            indices.append(collision_index)
        else:
            replacement = min(indices[1:-1], key=lambda i: abs(i - collision_index))
            indices[indices.index(replacement)] = collision_index
    return sorted(indices)


def _keyframe_sheet(paths: list[str], destination: str, fps: int, count: int,
                    collision_index: Optional[int] = None,
                    times_s: Optional[list[float]] = None) -> list[int]:
    """Compose labeled rendered PNGs directly, without decoding the encoded film."""
    from PIL import Image as PILImage, ImageDraw

    indices = _keyframe_indices(len(paths), count, collision_index)
    if not indices:
        raise ValueError("no keyframes requested or rendered")
    with PILImage.open(paths[indices[0]]) as image:
        source_w, source_h = image.size
    columns = min(3, len(indices))
    gap = 8
    tile_w = min(source_w, (1600 - gap * (columns - 1)) // columns)
    tile_h = max(1, round(source_h * tile_w / source_w))
    rows = (len(indices) + columns - 1) // columns
    sheet = PILImage.new("RGB", (columns * tile_w + gap * (columns - 1),
                                 rows * tile_h + gap * (rows - 1)), _BG)
    draw = ImageDraw.Draw(sheet)
    font = _font(True, min(16, max(11, tile_w // 28)))
    for tile, index in enumerate(indices):
        x = (tile % columns) * (tile_w + gap)
        y = (tile // columns) * (tile_h + gap)
        with PILImage.open(paths[index]) as source:
            image = source.convert("RGB")
        if image.size != (tile_w, tile_h):
            image = image.resize((tile_w, tile_h), PILImage.Resampling.LANCZOS)
        sheet.paste(image, (x, y))
        seconds = times_s[index] if times_s is not None else index / fps
        labels = [f"#{index} t={seconds:.2f}s"]
        if index == collision_index:
            labels.append("COLLISION")
        label_h = draw.textbbox((0, 0), labels[0], font=font)[3] + 4
        for line, label in enumerate(labels):
            ly = y + 4 + line * label_h
            bounds = draw.textbbox((0, 0), label, font=font)
            label_w = min(tile_w - 4, bounds[2] + 12)
            draw.rectangle((x + 4, ly, x + label_w, ly + label_h),
                           fill=(36, 14, 14) if line else _BG)
            draw.text((x + 8, ly + 2), label, font=font,
                      fill=(255, 135, 105) if line else (240, 240, 240))
    sheet.save(destination, format="PNG")
    return indices


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
                         annotate: bool = False,
                         keyframes: int = 6) -> list:
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
                        frames. A measured collision sample gets a red border; a first-hit
                        record alone gives later samples an amber "prior collision detected;
                        later state not checked" warning. Per-sample results, when supplied
                        by the sweep, take precedence. THREE drives, exactly one per call:
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
                        run_fea_static(vtu_path=...)); no document is needed. A topology
                        reanalysis.vtu also works. For several load cases, set
                        field='von_mises@<case>'. If the file has the field only as
                        cell data, the film shows its volume-weighted nodal average and
                        the result reports field_source='cell_averaged'.

        save_path : REQUIRED path ending .gif or .mp4, inside the project/workspace root
                    (same containment rule as every render deliverable). A relative path is
                    project-root relative. A poster PNG is written beside it as
                    poster_<stem>.png and returned inline.
        frames/fps/size : frame COUNT, playback rate, [width, height] px. Duration is
                    frames/fps; a GIF over `max_bytes` (25 MB default) walks fps then width
                    and the caption reports the leg it landed on.
        keyframes : number of evenly spaced RENDERED frames (default 6, clamped 0..12)
                    to tile beside the poster as keyframes_<stem>.png. 0 disables it;
                    positive counts preserve both endpoints (at least two tiles) and
                    include the recorded first collision (an extra tile when needed).
        view      : starting camera — "iso"/"front"/... or {"azimuth","elevation"}/{"dir"}.
        highlight : render_part's highlight vocabulary (sid / feature id / query / list),
                    drawn on every frame.
        annotate  : overlay B-rep edges + scale bar on every frame (slower per frame).
        display_quality : "draft"|"standard"|"fine" display tessellation for the frames.
        title/subtitle/hold_s : a dark title card held for `hold_s` seconds at the head of
                    the film (2-3 s is a readable hold).
        frames_dir : optional directory the ordered PNG sequence (title card frames
                    first, then the film) is also written into as f00000.png... A relative
                    path is project-root relative. This is
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

        Returns [poster Image, keyframe-sheet Image, caption] by default, or
        [poster Image, caption] when keyframes=0 or sheet construction fails. The caption
        states the written path, bytes, sha256, frame count, fps, measured duration from
        ffprobe, the engine build id and polymesh commit burned into every frame's footer,
        and each kind's own measured numbers (component offsets, sweep range /
        `steer_range_deg` and first collision, peak MPa and warp factor). Numbers the
        engine did not report read "unknown" — never a guess.
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
                    "render_animation needs save_path — a .gif or .mp4 path under your "
                    "project folder (a relative path is project-root relative). A film "
                    "cannot be returned inline; the poster frame is what comes back in the "
                    "reply.")
            ext = os.path.splitext(str(save_path).strip())[1].lower()
            if ext not in (".gif", ".mp4"):
                raise ValueError("save_path must end in .gif or .mp4; got "
                                 f"{ext or 'no extension'!r}.")
            target = _render_save_target(save_path, film=True)
            poster = _render_save_target(os.path.join(
                os.path.dirname(target),
                f"poster_{os.path.splitext(os.path.basename(target))[0]}.png"))
            frame_root = (_render_save_target(frames_dir, directory=True)
                          if frames_dir else None)
            frames = max(2, int(frames))
            fps = max(1, min(60, int(fps)))
            keyframes = max(0, min(12, int(keyframes)))
            W, H = (int(size[0]), int(size[1])) if (isinstance(size, (list, tuple))
                                                    and len(size) == 2) else (1280, 720)
            W, H = max(160, W - W % 2), max(120, H - H % 2)
            footer_h = min(H - 32, max(88, round(H * 0.22)))
            scene_size = (W, H - footer_h)
            raster_size = _animation_raster_size(scene_size)
            raster_sizes = set()
            prov = _provenance()
            left_prefix = (f"chudware {_short(prov['engine_build_id'], 40)} | polymesh "
                           f"{_short(prov['polymesh_commit'])}")
            word = "sample" if kind == "motion" else "frame"
            work = tempfile.mkdtemp(prefix="chudware_anim_")
            t_start = time.time()
            measured: dict = {}
            doc_name = ""

            if kind == "fea":
                vtu_file = resolve_agent_path(vtu_path, "vtu_path") if vtu_path else ""
                if not vtu_file or not os.path.exists(vtu_file):
                    raise ValueError(
                        "render_animation(kind='fea') needs vtu_path — the .vtu a solve "
                        "wrote (run_fea_static(vtu_path='results/part.vtu')). A relative "
                        f"path is project-root relative. {vtu_path!r} does not exist.")
                rec = _fea_frames(vtu_file, work, frames, raster_size, view,
                                  warp_mm, field)
                measured = {k: v for k, v in rec.items() if k != "frames"}
                doc_name = os.path.basename(str(vtu_path))
                left = f"{left_prefix} | {doc_name}"
                paths = rec["frames"]
                for png in paths:
                    raster_sizes.add(_downsample(png, scene_size))
                averaged = (" (cell avg)" if measured.get("field_source") == "cell_averaged"
                            else "")
                foot_right = (f"{measured['field']}{averaged} peak "
                              f"{measured['peak_field']:.4g} MPa | "
                              f"warp x{measured['warp_factor']:.0f}")
            else:
                d = _doc(doc_id)
                doc_name = _handoff_doc_name(d) or d
                left = f"{left_prefix} | {doc_name}"
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
                        reference = sweep["reference_transforms"] if steer is None else None
                        for fr in traj:
                            frame_poses = {}
                            for b in fr.get("bodies") or []:
                                cid, tb = b.get("component"), b.get("transform")
                                if (cid not in base or not isinstance(tb, list)
                                        or len(tb) != 16):
                                    continue
                                if steer is not None:
                                    # The steering adapter reports component poses directly.
                                    frame_poses[cid] = list(tb)
                                    continue
                                t0 = first.get(cid)
                                if not t0 or len(t0) != 16:
                                    continue
                                # Native body motion may include a source-local placement.
                                # Transport the SOLVED first-sample component pose, not the
                                # authored document pose (which can differ from start).
                                frame_poses[cid] = _mat_mul(
                                    _mat_mul(tb, _rigid_inverse(t0)), reference[cid])
                            poses_per_frame.append(frame_poses)
                        interf = sweep.get("interference") or {}
                        driver = sweep.get("driver") or {}
                        measured = {"mate_id": sweep.get("mate_id"),
                                    "driver": driver,
                                    "samples": len(traj),
                                    "sample_collisions": [fr.get("collisions") for fr in traj],
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
                            if sweep.get("motion_study"):
                                # The film's sweep is also the doc's live Motion study.
                                measured["motion_study"] = sweep["motion_study"]
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
                    bodies = _world_bodies(clone)

                if poses_per_frame:
                    # Read initial placements and fixed/free ownership before grounding.
                    _freeze_clone_poses(render_doc)

                # One conservative envelope for all sampled rigid poses; never per-frame fit.
                patterned = bool(poses_per_frame) and _has_pattern_instances(bodies, base)
                focus = (_pattern_pose_bounds(render_doc, bodies, base, poses_per_frame)
                         if patterned else
                         _pose_bounds(bodies, base if poses_per_frame else {}, poses_per_frame))
                measured["framing"] = {
                    "bounds_mm": focus, "center_mm": list(_bbox_centre(focus)),
                    "method": "fixed full-orbit bounds projection" if kind == "turntable" else
                              "fixed measured world bounds across sampled pattern poses"
                              if patterned else
                              "fixed source-body bounds transported across sampled poses",
                    "clip_near": False}
                scales = []
                paths = []
                render_count = len(poses_per_frame) if poses_per_frame else frames
                footer_right = str(footer_note) if footer_note else foot_right
                _, _, _, _, _, footer_h = _footer_layout(
                    W, f"{left} | {word} {render_count}/{render_count}", footer_right)
                footer_h = max(88, footer_h)
                if footer_h > H - 32:
                    raise ValueError(f"Animation footer needs {footer_h}px; increase "
                                     "output height or shorten footer_note/document name.")
                scene_size = (W, H - footer_h)
                raster_size = _animation_raster_size(scene_size)
                bg = list(_BG) if dark else None
                for i in range(render_count):
                    if poses_per_frame:
                        _apply_poses(render_doc, poses_per_frame[i])
                    png = os.path.join(work, f"f{i:05d}.png")
                    camera_focus, margin = (_orbit_framing(focus, dirs[i], *raster_size)
                                            if kind == "turntable" else (focus, 0.08))
                    png, frame, _r = _render_view_png(render_doc, dirs[i], png, name="",
                                                      W=raster_size[0], H=raster_size[1],
                                                      focus=camera_focus, margin=margin,
                                                      clip_near=False,
                                                      annotate=bool(annotate),
                                                      background=bg)
                    raster_sizes.add(_downsample(png, scene_size))
                    if frame:
                        sx, sy = W / frame["W"], scene_size[1] / frame["H"]
                        frame = dict(frame, W=W, H=scene_size[1],
                                     scale=frame["scale"] * min(sx, sy))
                        scales.append(frame["scale"])
                    if annotate or highlight is not None:
                        png, _info = _annotate_render(
                            png, render_doc, dirs[i], highlight=highlight,
                            edges=bool(annotate), callouts=False, scale=bool(annotate),
                            frame=frame, out_png=png)
                    collision_text, current_hit = _collision_legend(measured, i)
                    if collision_text:
                        _mark_collision(png, collision_text, current=current_hit)
                    paths.append(png)
                measured["framing"]["output_pixels_per_mm_range"] = (
                    [min(scales), max(scales)] if scales else None)

            # ---- provenance footer on every RENDERED frame ----
            stem = os.path.splitext(os.path.basename(target))[0]
            if footer_note:
                foot_right = str(footer_note)
            for i, png in enumerate(paths):
                _stamp_footer(png, f"{left} | {word} {i + 1}/{len(paths)}", foot_right,
                              height=footer_h)
            measured["output_quality"] = {
                "rendered_geometry_px": [list(s) for s in sorted(raster_sizes)],
                "delivered_geometry_px": list(scene_size), "footer_px": footer_h,
                "downsample": "Lanczos before film typography",
                "material_appearance": "stored CAD appearance; no film recolouring"
                if kind != "fea" else "solved scalar field"}
            rendered_paths = list(paths) if keyframes else []

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
                ordered = [card] * head + list(paths)
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
                os.link(src, os.path.join(seq, f"f{k:05d}.png"))
            pattern = os.path.join(seq, "f%05d.png")
            total = len(ordered)
            exported = None
            if frame_root:
                os.makedirs(frame_root, exist_ok=True)
                for k in range(total):
                    destination = _render_save_target(
                        os.path.join(frame_root, f"f{k:05d}.png"))
                    shutil.copyfile(os.path.join(work, "seq", f"f{k:05d}.png"),
                                    destination)
                exported = frame_root

            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            enc = _encode(pattern, target, fps, int(max_bytes), W)
            probe = _ffprobe_film(target)
            with open(target, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            poster_src = paths[min(len(paths) - 1, int(len(paths) * 0.62))]
            shutil.copyfile(poster_src, poster)
            inline = render_output_path(_render_stem(doc_name or stem, "poster"), ".png")
            shutil.copyfile(poster_src, inline)

            dur = probe.get("duration_s")
            cap = [
                f"{kind} animation -> {target}",
                f"{enc['bytes']} B ({enc['bytes'] / 1048576.0:.2f} MiB), sha256 {sha[:12]}",
                f"{probe.get('frames') or 'unknown'} frames encoded ({total} source frames; "
                f"{len(set(paths))} rendered"
                + (f" + {head} title-card" if head else "")
                + f") at target {enc['fps']} fps (source {fps} fps), "
                f"{probe.get('width') or enc['width']}x{probe.get('height') or H} px, "
                f"duration {dur if dur is not None else 'unknown'} s measured by ffprobe "
                f"(source timeline {total / fps:.3f} s; codec {probe.get('codec') or 'n/a'}, "
                f"measured average rate {probe.get('avg_frame_rate') or 'unknown'})",
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
                cap.append("size ladder (fps, then width; temporal resampling preserves "
                           f"duration and both endpoints): {json.dumps(enc['ladder'])}")
            if enc["bytes"] > int(max_bytes):
                cap.append(f"WARNING: {enc['bytes']} B is still over the {int(max_bytes)} B "
                           + ("budget after the GIF size ladder." if ext == ".gif" else
                              "budget; MP4 quality is not reduced by the GIF size ladder."))
            result = [Image(path=inline)]
            if keyframes:
                try:
                    sheet_path = _render_save_target(os.path.join(
                        os.path.dirname(poster), f"keyframes_{stem}.png"))
                    first_collision = ((measured.get("interference") or {})
                                       .get("first_collision_step") if kind == "motion" else None)
                    first_time: dict[str, float] = {}
                    for index, path in enumerate(ordered):
                        first_time.setdefault(path, index / fps)
                    selected = _keyframe_sheet(
                        rendered_paths, sheet_path, fps, keyframes, first_collision,
                        [first_time[path] for path in rendered_paths])
                    result.append(Image(path=sheet_path))
                    cap.append(f"keyframes {sheet_path} (frames: {selected})")
                except Exception as exc:  # noqa: BLE001
                    cap.append(f"[keyframes unavailable: {exc}]")
            result.append("  ".join(cap))
            return result
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
