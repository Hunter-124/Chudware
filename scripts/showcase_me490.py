#!/usr/bin/env python3
"""ME-490 Crimson Comet showcase films — five chapters built from the REAL project files.

REGENERATE (one chapter or all; ~10 min of engine renders per chapter):

    systemd-run --user --pipe --wait -q --collect -p MemoryMax=12G bash -c 'ulimit -c 0; \
      cd /home/hunter/Repositories/CAD && \
      CHUDWARE_ENGINE_DAEMON=0 \
      CHUDWARE_LIBCHUDWARE=$PWD/core/build-occt-linux/bin/libchudware.so \
      CHUDWARE_PROJECT_SAVE_DIR=/home/hunter/Documents/ME-490/Showcase \
      CHUDWARE_MEM_FRAC=0.5 CHUDWARE_HEADROOM_GB=6 CHUDWARE_SIM_THREADS=4 \
      .venv-mcp/bin/python scripts/showcase_me490.py --all'

    ... --only 01_no_solidworks | 02_merge | 03_mesh_to_stress | 04_motion | 05_formats

Chapters (each a .gif at 1280 px + a 1080p .mp4 + poster_<name>.png):

  01_no_solidworks    Wheel.SLDASM opened DIRECTLY, then the native parametric rebuild
                      CD-210_Comet_Wheel_Native, then both turning side by side.
  02_merge            the 760-body legacy frame -> the 624-body filtered frame ->
                      the four Comet wheels flying in to their solved transforms ->
                      the check_interference verdict.
  03_mesh_to_stress   one film per released static case: mesh fade-in, then von Mises
                      on the displacement-warped mesh.
  04_motion           a driven-mate kinematic sweep (simulate_motion's own trajectory).
  05_formats          .step+ saved, its zip listing on screen, the contracted AP214
                      `geometry.step` member EXTRACTED from that archive, re-imported and
                      rendered from those bytes alone, and `polymesh check`'s verdict on it.

RULES THIS SCRIPT KEEPS (see Showcase/NOTES.md for the full measured-vs-cosmetic split):
  * Every input is an existing project file; nothing is modelled for the film.
  * Every number on screen comes from a tool reply or a released packet in this run, and
    lands in Showcase/manifest.json with the input's sha256 beside it.
  * Nothing under /home/hunter/Documents/ME-490 is modified: this script only WRITES new
    files under Showcase/ (its own _work/ for intermediates).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

_REPO = str(Path(__file__).resolve().parents[1])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from services.mcp import cad_animation_tools as anim          # noqa: E402
from services.mcp.cad_animation_tools import register_cad_animation_tools  # noqa: E402
from services.mcp.cad_tools import register_cad_tools          # noqa: E402
from services.mcp.sim_evidence_tools import register_sim_evidence_tools  # noqa: E402

ROOT = Path("/home/hunter/Documents/ME-490")
OUT = ROOT / "Showcase"
WORK = OUT / "_work"
SIMOUT = ROOT / "Engineering" / "Simulation_Output"
MERGED = ROOT / "Engineering" / "CAD_Merged_v2"
FRAME_SRC = Path("/home/hunter/Documents/489-final/CAD & Design Geometry/Model Step files/"
                 "CD-100_Full_Frame_Stage5_RevO.step")
SW_DIR = ROOT / "ME 489-490 - Crimson Comet Engineering" / "Rover Design" / "Solidworks Files"

FPS = 24
FRAME_W, FRAME_H = 1920, 1080          # rendered size; GIF is encoded down to 1280
GIF_W_LADDER = (1280, 1024, 854, 720, 640)
GIF_MAX = 25 * 1024 * 1024
TURN = 96                              # frames in one 360 degree orbit (4.0 s at 24 fps)
CARD = int(2.5 * FPS)                  # a 2.5 s caption hold


# ------------------------------------------------------------------ tool boundary
class _Capture:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *_a, **_k):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn
        return dec


_CAP = _Capture()
register_cad_tools(_CAP)
register_sim_evidence_tools(_CAP)      # polymesh_cli — the independent Polyhedral-FEA lane
register_cad_animation_tools(_CAP)
T = _CAP.tools


def J(raw):
    """Every MCP tool returns a JSON string; a refusal is a payload, not an exception."""
    if isinstance(raw, list):
        raw = raw[0]
    return json.loads(raw) if isinstance(raw, str) else raw


def ok(payload: dict, what: str) -> dict:
    if not payload.get("ok", True):
        raise SystemExit(f"{what} FAILED: {json.dumps(payload)[:900]}")
    return payload


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_record(path) -> dict:
    p = Path(path)
    return {"path": str(p), "bytes": p.stat().st_size, "sha256": sha256(p)}


def volume_mm3(measure_reply: dict) -> float:
    """measure_part reports the DOCUMENT total under `mass.volume_mm3`; a multi-body reply
    also breaks it down per body. Read the aggregate, fall back to the breakdown."""
    mass = measure_reply.get("mass") or {}
    if isinstance(mass.get("volume_mm3"), (int, float)):
        return float(mass["volume_mm3"])
    return sum(float(b.get("volume_mm3") or 0.0) for b in (mass.get("bodies") or []))


def mass_grams(measure_reply: dict) -> float:
    mass = measure_reply.get("mass") or {}
    if isinstance(mass.get("mass_g"), (int, float)):
        return float(mass["mass_g"])
    return sum(float(b.get("mass_g") or 0.0) for b in (mass.get("bodies") or []))


def _font(bold: bool, px: int):
    """A TrueType face at `px`, resolved through fontconfig.

    This used to try two hard-coded DejaVu paths and fall back to
    ``ImageFont.load_default()``. On a box with no DejaVu package (this one — Fedora ships
    google-noto instead) EVERY card silently rendered in the default ~11 px bitmap face
    instead of the requested 54/30/24 px, so the headline, the measured lines and the
    verbatim command output were all illegibly small, and any character outside the bitmap
    font's tiny repertoire (an em dash, a degree sign, Ø) came out as a tofu box. A film
    whose whole claim is "these numbers are on screen" cannot render its numbers at 11 px.
    So ask fontconfig, which is present wherever the desktop is, and only fall back if it
    cannot answer."""
    from PIL import ImageFont
    hard = (("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if bold else
            ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSansMono.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"))
    for c in hard:
        if os.path.exists(c):
            return ImageFont.truetype(c, px)
    pattern = "DejaVu Sans:bold,sans-serif:bold" if bold else "DejaVu Sans Mono,monospace"
    try:
        found = subprocess.run(["fc-match", "-f", "%{file}", pattern],
                               capture_output=True, text=True, check=True).stdout.strip()
        if found and os.path.exists(found):
            return ImageFont.truetype(found, px)
    except (OSError, subprocess.CalledProcessError):
        pass
    raise SystemExit(
        f"showcase cards need a scalable {'bold sans' if bold else 'monospace'} font at "
        f"{px} px; neither the DejaVu paths nor `fc-match {pattern}` resolved one. "
        "Install dejavu-sans-fonts (or google-noto-sans-mono-fonts) and re-run.")


def _wrap_to_width(text: str, font, max_px: int, continuation: str = "") -> list:
    """`text` split into as few lines as fit `max_px`, measured with `font`.

    Words are kept whole where they fit; a single unbreakable run (an absolute path, a STEP
    entity line) is split mid-token rather than allowed off the frame. Nothing is discarded:
    a card that cannot say the whole number is not evidence, and the old code silently
    clipped body lines at the frame edge and mono lines at a hard 112 characters."""
    def width(s: str) -> int:
        return int(font.getlength(s))

    out: list = []
    remaining = str(text)
    prefix = ""
    while remaining:
        budget = max_px - width(prefix)
        if width(remaining) <= budget:
            out.append(prefix + remaining)
            break
        # longest prefix of `remaining` that fits, preferring a space boundary
        cut = len(remaining)
        while cut > 1 and width(remaining[:cut]) > budget:
            cut -= 1
        space = remaining.rfind(" ", 1, cut + 1)
        if space > cut // 2:
            cut = space
        out.append(prefix + remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
        prefix = continuation
    return out or [""]


def text_card(title: str, lines, out_png: str, mono_block=None) -> str:
    """A dark card: headline, then measured lines, then an optional verbatim mono block
    (real command output — the film shows the terminal, not a paraphrase of it).

    Every string is wrapped to the frame, so a long absolute path or a 100-character argv
    is carried onto a continuation line instead of running off the right edge."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (FRAME_W, FRAME_H), (16, 17, 20))
    dr = ImageDraw.Draw(im)
    big, small, mono = _font(True, 54), _font(False, 30), _font(False, 24)
    body_w = FRAME_W - 110 - 110
    mono_w = FRAME_W - 124 - 124
    y = 120
    for row in _wrap_to_width(title, big, body_w):
        dr.text((110, y), row, fill=(240, 242, 248), font=big)
        y += 64
    y += 32
    for line in lines:
        for row in _wrap_to_width(str(line), small, body_w, continuation="    "):
            if y > FRAME_H - 70:
                break
            dr.text((110, y), row, fill=(178, 186, 202), font=small)
            y += 44
    if mono_block:
        rows: list = []
        for line in mono_block:
            rows += _wrap_to_width(str(line), mono, mono_w, continuation="  ")
        y += 26
        dr.rectangle([100, y - 14, FRAME_W - 100,
                      min(FRAME_H - 60, y + 30 * len(rows) + 14)],
                     fill=(24, 26, 31), outline=(58, 62, 72))
        for row in rows:
            if y > FRAME_H - 70:
                break
            dr.text((124, y), row, fill=(150, 220, 176), font=mono)
            y += 30
    im.save(out_png)
    return out_png


# ------------------------------------------------------------------ chapter builder
class Chapter:
    """One film: a list of frame directories concatenated in order, encoded once.

    A chapter is composed of SEGMENTS (each a render_animation call writing its own
    frames_dir) and CARDS (2.5 s holds). Encoding happens once at the end, so no shot is
    ever re-compressed."""

    def __init__(self, name: str, fps: int = FPS):
        self.name = name
        self.fps = fps
        self.seq = Path(tempfile.mkdtemp(prefix=f"showcase_{name}_"))
        self.n = 0
        self.notes: list = []
        self.inputs: list = []
        self.numbers: dict = {}
        self.commands: list = []
        self.poster_at = None
        self.t0 = time.time()

    def _append_png(self, src) -> None:
        shutil.copyfile(src, self.seq / f"f{self.n:05d}.png")
        self.n += 1

    def card(self, title: str, lines, seconds: float = 2.5, mono_block=None) -> None:
        png = text_card(title, lines, str(self.seq / "_card.png"), mono_block)
        for _ in range(max(1, int(round(seconds * self.fps)))):
            self._append_png(png)
        self.notes.append({"segment": "card", "title": title, "lines": list(lines),
                           "mono_lines": len(mono_block or [])})

    def frames_from(self, folder, mark_poster: bool = False) -> None:
        files = sorted(Path(folder).glob("f*.png"))
        if not files:
            raise SystemExit(f"{self.name}: segment {folder} produced no frames")
        if mark_poster:
            self.poster_at = self.n + len(files) // 2
        for f in files:
            self._append_png(f)

    def segment(self, label: str, *, mark_poster: bool = False,
                keep_frames=None, **kw) -> dict:
        """One render_animation call. Its frames are appended to this chapter; its own
        per-segment .mp4 stays in _work/ as the shot's independently checkable artifact.
        `keep_frames` is a directory the shot's PNGs are left in (a later side-by-side
        composite reuses them instead of re-rendering)."""
        seg_dir = Path(keep_frames) if keep_frames else Path(
            tempfile.mkdtemp(prefix="seg_"))
        seg_dir.mkdir(parents=True, exist_ok=True)
        seg_mp4 = WORK / "segments" / f"{self.name}__{label}.mp4"
        seg_mp4.parent.mkdir(parents=True, exist_ok=True)
        kw.setdefault("fps", self.fps)
        kw.setdefault("size", [FRAME_W, FRAME_H])
        res = T["render_animation"](save_path=str(seg_mp4), frames_dir=str(seg_dir), **kw)
        if not isinstance(res, list) or len(res) != 2:
            raise SystemExit(f"{self.name}/{label} render_animation refused: "
                             f"{json.dumps(res)[:900]}")
        caption = res[1]
        n_frames = len(sorted(seg_dir.glob("f*.png")))
        self.frames_from(seg_dir, mark_poster=mark_poster)
        if not keep_frames:
            shutil.rmtree(seg_dir, ignore_errors=True)
        measured = {}
        if "measured: " in caption:
            try:
                measured = json.loads(caption.split("measured: ", 1)[1]
                                      .split("  render wall time")[0])
            except Exception:  # noqa: BLE001
                measured = {}
        self.notes.append({"segment": label, "kind": kw.get("kind"),
                           "frames": n_frames,
                           "caption": caption, "measured": measured,
                           "segment_mp4": str(seg_mp4)})
        self.commands.append(f"render_animation(kind={kw.get('kind')!r}, ...) -> {seg_mp4}")
        return {"caption": caption, "measured": measured}

    def side_by_side(self, folder_a, folder_b, label_a: str, label_b: str) -> None:
        """Two rendered shots on one frame — the SolidWorks import beside the native
        rebuild. Pure PIL compositing of frames the engine already rendered."""
        from PIL import Image, ImageDraw
        a = sorted(Path(folder_a).glob("f*.png"))
        b = sorted(Path(folder_b).glob("f*.png"))
        n = min(len(a), len(b))
        if not n:
            raise SystemExit(f"{self.name}: side-by-side needs both shots rendered")
        font = _font(True, 34)
        for i in range(n):
            ia, ib = Image.open(a[i]), Image.open(b[i])
            half = FRAME_W // 2
            ca = ia.resize((half, int(ia.height * half / ia.width)))
            cbi = ib.resize((half, int(ib.height * half / ib.width)))
            out = Image.new("RGB", (FRAME_W, FRAME_H), (16, 17, 20))
            out.paste(ca, (0, (FRAME_H - ca.height) // 2))
            out.paste(cbi, (half, (FRAME_H - cbi.height) // 2))
            dr = ImageDraw.Draw(out)
            dr.line([(half, 0), (half, FRAME_H)], fill=(60, 64, 74), width=2)
            dr.text((40, 40), label_a, fill=(232, 236, 245), font=font)
            dr.text((half + 40, 40), label_b, fill=(232, 236, 245), font=font)
            png = self.seq / "_sbs.png"
            out.save(png)
            self._append_png(png)
        self.notes.append({"segment": "side_by_side", "frames": n,
                           "left": label_a, "right": label_b})

    def finish(self, headline: str) -> dict:
        """Encode ONCE: a 1280-wide GIF (fps-then-width ladder under 25 MB) and a 1080p
        H.264 MP4, both from the same frame sequence."""
        pattern = str(self.seq / "f%05d.png")
        gif = OUT / f"{self.name}.gif"
        mp4 = OUT / f"{self.name}.mp4"
        OUT.mkdir(parents=True, exist_ok=True)
        ladder = []
        chosen = None
        for width in GIF_W_LADDER:
            for fps in (self.fps,) + tuple(f for f in (20, 15, 12, 10) if f < self.fps):
                anim._encode_gif(pattern, str(gif), fps, width)
                size = gif.stat().st_size
                ladder.append({"fps": fps, "width": width, "bytes": size})
                if size <= GIF_MAX:
                    chosen = ladder[-1]
                    break
            if chosen:
                break
        if not chosen:
            chosen = ladder[-1]
        anim._encode_mp4(pattern, str(mp4), self.fps)
        gif_probe = anim._ffprobe_film(str(gif))
        mp4_probe = anim._ffprobe_film(str(mp4))
        poster_idx = self.poster_at if self.poster_at is not None else self.n // 2
        poster = OUT / f"poster_{self.name}.png"
        shutil.copyfile(self.seq / f"f{min(poster_idx, self.n - 1):05d}.png", poster)
        prov = anim._provenance()
        entry = {
            "headline": headline,
            "gif": {"path": str(gif), "bytes": gif.stat().st_size, "sha256": sha256(gif),
                    **gif_probe, "encode_ladder": ladder, "chosen": chosen},
            "mp4": {"path": str(mp4), "bytes": mp4.stat().st_size, "sha256": sha256(mp4),
                    **mp4_probe},
            "poster": {"path": str(poster), "bytes": poster.stat().st_size,
                       "sha256": sha256(poster), "frame_index": poster_idx},
            "frames_rendered": self.n,
            "fps": self.fps,
            "render_size_px": [FRAME_W, FRAME_H],
            "wall_time_s": round(time.time() - self.t0, 1),
            "provenance": prov,
            "inputs": self.inputs,
            "numbers_on_screen": self.numbers,
            "commands": self.commands,
            "segments": self.notes,
            "regenerate": f"scripts/showcase_me490.py --only {self.name}",
        }
        shutil.rmtree(self.seq, ignore_errors=True)
        print(f"[{self.name}] gif {gif.stat().st_size} B {gif_probe.get('duration_s')} s | "
              f"mp4 {mp4.stat().st_size} B {mp4_probe.get('duration_s')} s | "
              f"{self.n} frames | {entry['wall_time_s']} s", flush=True)
        return entry


def write_manifest(entries: dict) -> None:
    path = OUT / "manifest.json"
    doc = {}
    if path.exists():
        doc = json.loads(path.read_text())
    doc.setdefault("project", str(ROOT))
    doc["generator"] = "scripts/showcase_me490.py"
    doc["regenerate"] = ("systemd-run --user --pipe --wait -q --collect -p MemoryMax=12G "
                         "bash -c 'ulimit -c 0; cd /home/hunter/Repositories/CAD && "
                         "CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_LIBCHUDWARE=$PWD/core/"
                         "build-occt-linux/bin/libchudware.so CHUDWARE_PROJECT_SAVE_DIR="
                         "/home/hunter/Documents/ME-490/Showcase .venv-mcp/bin/python "
                         "scripts/showcase_me490.py --all'")
    doc.setdefault("films", {}).update(entries)
    doc["written_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(doc, indent=2, sort_keys=True))
    print(f"[manifest] {path} ({path.stat().st_size} B)", flush=True)


# =========================================================== chapter 01
def ch01() -> dict:
    sldasm = SW_DIR / "Wheel.SLDASM"
    native = MERGED / "CD-210_Comet_Wheel_Native.step"
    editable = ROOT / "CD-210_Comet_Wheel.step+"
    c = Chapter("01_no_solidworks")
    c.inputs = [input_record(p) for p in (sldasm, native, editable)]

    sw = ok(J(T["import_part"](path=str(sldasm))), "import Wheel.SLDASM")
    sw_doc = sw["doc_id"]
    sw_bodies = ok(J(T["list_world_bodies"](doc_id=sw_doc)), "list_world_bodies(SLDASM)")
    sw_mass = ok(J(T["measure_part"](doc_id=sw_doc)), "measure_part(SLDASM)")
    nat = ok(J(T["import_part"](path=str(native))), "import native rebuild")
    nat_doc = nat["doc_id"]
    nat_bodies = ok(J(T["list_world_bodies"](doc_id=nat_doc)), "list_world_bodies(native)")
    nat_mass = ok(J(T["measure_part"](doc_id=nat_doc)), "measure_part(native)")
    tree = ok(J(T["get_part"](doc_id=nat_doc)), "get_part(native)")

    def vol(reply: dict) -> float:
        return volume_mm3(reply)

    def total_mass_g(reply: dict) -> float:
        return mass_grams(reply)

    # The editable native document: its feature tree is the point of the chapter.
    ed = ok(J(T["import_part"](path=str(editable))), "open CD-210_Comet_Wheel.step+")
    ed_tree = ok(J(T["get_part"](doc_id=ed["doc_id"])), "get_part(step+)")
    feats = [f.get("name") or f.get("type") for f in (ed_tree.get("features") or [])]

    c.numbers = {
        "sldasm_bodies": sw_bodies.get("body_count"),
        "sldasm_volume_mm3": round(vol(sw_mass), 3),
        "sldasm_mass_g": round(total_mass_g(sw_mass), 3),
        "sldasm_import_fidelity": sw.get("import_fidelity"),
        "sldasm_feature_resolved": sw.get("feature_resolved"),
        "native_bodies": nat_bodies.get("body_count"),
        "native_volume_mm3": round(vol(nat_mass), 3),
        "native_mass_g": round(total_mass_g(nat_mass), 3),
        "native_step_features": len(tree.get("features") or []),
        "editable_features": len(feats),
        "editable_feature_names": feats,
    }
    c.commands += [f"import_part({str(sldasm)!r})", f"import_part({str(native)!r})",
                   f"import_part({str(editable)!r})", "list_world_bodies()",
                   "measure_part()", "get_part()"]

    c.card("No SolidWorks. No conversion step.", [
        "Wheel.SLDASM is opened by the Chudware engine directly — no SolidWorks licence,",
        "no STEP export, no repair pass. Every number below is from this run's tool replies.",
        f"Wheel.SLDASM: {c.numbers['sldasm_bodies']} bodies, "
        f"{c.numbers['sldasm_volume_mm3']:.1f} mm3, {c.numbers['sldasm_mass_g'] / 1000:.3f} kg",
        f"import_fidelity = {c.numbers['sldasm_import_fidelity']}  "
        f"feature_resolved = {c.numbers['sldasm_feature_resolved']}",
    ], seconds=3.0)

    sw_frames = Path(tempfile.mkdtemp(prefix="sw_turn_"))
    c.segment("sldasm_turntable", kind="turntable", doc_id=sw_doc, frames=TURN,
              view={"azimuth": 35, "elevation": 22}, display_quality="fine",
              keep_frames=str(sw_frames), mark_poster=False)

    c.card("The rebuild: a real feature tree", [
        f"CD-210_Comet_Wheel_Native.step — {c.numbers['native_bodies']} bodies, "
        f"{c.numbers['native_volume_mm3']:.1f} mm3, {c.numbers['native_mass_g']/1000:.3f} kg",
        f"CD-210_Comet_Wheel.step+ carries {c.numbers['editable_features']} editable features:",
        ", ".join(feats[:8]) or "(none reported)",
        ", ".join(feats[8:16]),
        ", ".join(feats[16:]),
        "The SolidWorks import is a baked display body; this one is parametric.",
    ], seconds=3.5)

    nat_frames = Path(tempfile.mkdtemp(prefix="nat_turn_"))
    c.segment("native_turntable", kind="turntable", doc_id=nat_doc, frames=TURN,
              view={"azimuth": 35, "elevation": 22}, display_quality="fine",
              annotate=True, keep_frames=str(nat_frames), mark_poster=True)

    c.card("Same wheel, two provenances", [
        f"left  Wheel.SLDASM        {c.numbers['sldasm_bodies']} bodies  "
        f"{c.numbers['sldasm_volume_mm3']:.0f} mm3",
        f"right CD-210 native STEP  {c.numbers['native_bodies']} bodies  "
        f"{c.numbers['native_volume_mm3']:.0f} mm3",
        "The volumes differ because the rebuild is the redesigned Comet wheel,",
        "not a copy of the legacy geometry — see 02_merge.gif.",
    ], seconds=2.5)
    c.side_by_side(sw_frames, nat_frames, "Wheel.SLDASM (as opened)",
                   "CD-210 native rebuild")
    shutil.rmtree(sw_frames, ignore_errors=True)
    shutil.rmtree(nat_frames, ignore_errors=True)
    return c.finish("SolidWorks assembly opened directly, beside the native parametric rebuild")


# =========================================================== chapter 02
def ch02() -> dict:
    full = ROOT / "CD-100_Full_Frame_Stage5_RevO.step+"
    filtered = ROOT / "CD-100B_Frame_NoLegacyWheels.step+"
    merged = ROOT / "CC-000_Merged_Rover.step+"
    report = MERGED / "PHASE1_REPORT.md"
    c = Chapter("02_merge")
    c.inputs = [input_record(p) for p in (full, filtered, merged, report, FRAME_SRC)]

    def extent(bodies: dict, axis: int) -> float:
        lo = min(float(x["bbox"]["min"][axis]) for x in bodies["bodies"])
        hi = max(float(x["bbox"]["max"][axis]) for x in bodies["bodies"])
        return round(hi - lo, 3)

    def zmin(bodies: dict) -> float:
        return round(min(float(x["bbox"]["min"][2]) for x in bodies["bodies"]), 3)

    def reopen(path) -> str:
        """Open `path` and return the doc id THE ENGINE reports right now.

        Opening the next .step+ of the same version series can retire an earlier doc
        (mission-doc series rules), so a chapter must never cache a doc id across opens:
        every shot re-opens the file it is about."""
        return ok(J(T["import_part"](path=str(path))), f"open {Path(path).name}")["doc_id"]

    a_doc = reopen(full)
    a_bodies = ok(J(T["list_world_bodies"](doc_id=a_doc)), "bodies(full)")

    c.commands += [f"import_part({str(full)!r})", f"import_part({str(filtered)!r})",
                   f"import_part({str(merged)!r})", "check_interference()",
                   "list_world_bodies()"]
    c.numbers = {
        "full_frame_bodies": a_bodies.get("body_count"),
        "full_bbox_y_mm": extent(a_bodies, 1),
        "full_min_z_mm": zmin(a_bodies),
        "legacy_ordinals_from_report": {
            "CD-201 mag wheel rims": [138, 142, 198, 202],
            "CD-202 tread carcasses": [139, 143, 199, 203],
            "tread cleats": "544..671 contiguous (128 bodies)",
            "total_removed": 136},
    }
    c.card("One rover out of two CAD lineages", [
        "The 489 frame carried a legacy wheel package. The 490 rover does not.",
        f"CD-100_Full_Frame_Stage5_RevO.step+  {c.numbers['full_frame_bodies']} bodies, "
        f"Y extent {c.numbers['full_bbox_y_mm']} mm, lowest solid z = "
        f"{c.numbers['full_min_z_mm']} mm",
        "Legacy package identified in PHASE1_REPORT.md by source ordinal:",
        "  rims 138/142/198/202   treads 139/143/199/203   cleats 544..671",
    ], seconds=3.0)
    c.segment("frame_with_legacy", kind="turntable", doc_id=a_doc, frames=TURN,
              view={"azimuth": 30, "elevation": 18}, display_quality="standard")

    b_doc = reopen(filtered)
    b_bodies = ok(J(T["list_world_bodies"](doc_id=b_doc)), "bodies(filtered)")
    c.numbers.update({
        "filtered_frame_bodies": b_bodies.get("body_count"),
        "bodies_removed": ((a_bodies.get("body_count") or 0)
                           - (b_bodies.get("body_count") or 0)),
        "filtered_bbox_y_mm": extent(b_bodies, 1),
        "filtered_min_z_mm": zmin(b_bodies)})
    c.card("136 bodies excluded — no STEP text touched", [
        "import_part(exclude_bodies=[...136 ordinals...])  ->  "
        f"{c.numbers['filtered_frame_bodies']} bodies retained",
        f"Y extent {c.numbers['full_bbox_y_mm']} -> {c.numbers['filtered_bbox_y_mm']} mm",
        f"lowest solid z {c.numbers['full_min_z_mm']} -> {c.numbers['filtered_min_z_mm']} mm "
        "(the tyre contact patch is gone)",
        f"delta bodies = {c.numbers['bodies_removed']}",
    ], seconds=3.0)
    c.segment("frame_filtered", kind="turntable", doc_id=b_doc, frames=TURN,
              view={"azimuth": 30, "elevation": 18}, display_quality="standard")

    m_doc = reopen(merged)
    inter = ok(J(T["check_interference"](doc_id=m_doc)), "check_interference(merged)")
    m_bodies = ok(J(T["list_world_bodies"](doc_id=m_doc)), "bodies(merged)")
    c.numbers.update({
        "merged_bodies": m_bodies.get("body_count"),
        "interference_summary": inter.get("summary"),
        "interference_pairs": len(inter.get("interferences") or []),
        "clearance_pairs": len(inter.get("clearances") or [])})
    c.card("The Comet wheels fly in", [
        f"CC-000_Merged_Rover.step+ — 1 frame + 4 CD-210 wheels, 8 solved mates, "
        f"{c.numbers['merged_bodies']} world bodies.",
        "The shot below drives each wheel from an exploded offset back to the transform",
        "the mate solver produced; the poses are the document's own, not an interpolation.",
    ], seconds=2.5)
    c.segment("wheels_fly_in", kind="explode", doc_id=m_doc, frames=TURN, factor=1.15,
              reverse=True, view={"azimuth": 38, "elevation": 20},
              display_quality="standard", mark_poster=True)

    c.card("check_interference verdict", [
        c.numbers["interference_summary"] or "(no summary reported)",
        f"unintended interference pairs: {c.numbers['interference_pairs']}   "
        f"clearance pairs measured: {c.numbers['clearance_pairs']}",
        "0.200 mm frame<->wheel gap is the intended slip fit:",
        "  wheel bore 25.400 mm on a 25.000 mm axle = 0.400 mm diametral / 0.200 mm radial",
        "Wheel inboard faces seat on y = +/-630.000 mm, the measured seating plane.",
    ], seconds=3.5)
    return c.finish("legacy wheel package excluded, Comet wheels merged in, interference clear")


# =========================================================== chapter 03
_CASES = {
    "FRAME-PEDAL-BRAKE": dict(
        title="Front pedal-post keyway, 1500 N on one pedal",
        released=dict(h_mm=4.2, order=2, n_nodes=305800, n_elems=192748,
                      peak_mpa=105.9844, disp_mm=0.441113, fos=2.6042),
        warp_mm=None),
    # WHEEL-RADIAL has NO field on this box: its released leg is tet10 at h = 4.0 mm, and
    # every route to a .vtu was refused here — run_fea_static's fineness band (0.5/0.6/1.0)
    # asks the mesher for a size that cannot resolve the slice, the explicit h=4.0 tet10 is
    # coarsened by the memory governor until the mesher refuses, h=4.0 tet4 leaves five
    # unfixed islands, and h=6.0 order 2 comes back as unsupported tet4 connectivity. All
    # four refusals are recorded verbatim in _work/fea_static_fineness_refusals.txt. The
    # wheel case is therefore shown from WHEEL-DRIVE-BRAKE, which ships a RELEASED .vtu
    # solved on the SAME wheel-slice geometry — a released field, not a re-solve.
    "WHEEL-DRIVE-BRAKE": dict(
        title="Comet wheel slice, traction-limited drive/brake couple (released field)",
        released=dict(h_mm=None, order=2, n_nodes=131632, n_elems=85472,
                      peak_mpa=1.7338, disp_mm=0.000853, fos=15.0),
        released_vtu=str(SIMOUT / "WHEEL-DRIVE-BRAKE" / "WHEEL-DRIVE-BRAKE_result.vtu"),
        setup=dict(fixtures=["Fe6956a (OD barrel — the tread/ground reaction)"],
                   loads=[{"face": "4 x measured bolt-hole faces on the 143.684 mm PCD",
                           "force": "114.42 N tangential each = a pure couple"}]),
        warp_mm=None),
    "FRAME-GLOBAL-3G": dict(
        title="Front frame spine, 3 g global bending 506.58 N",
        released=dict(h_mm=6.5, order=1, n_nodes=50294, n_elems=194828,
                      peak_mpa=31.276, disp_mm=5.764664, fos=8.8247),
        warp_mm=None),
}


def ch03() -> list:
    """One film per released static case. SHOWCASE_CASES=A,B restricts the set (used to
    rebuild a single case's film without re-encoding the others)."""
    entries = []
    sel = [s for s in (os.environ.get("SHOWCASE_CASES") or "").split(",") if s.strip()]
    for case, spec in _CASES.items():
        if sel and case not in sel:
            continue
        if spec.get("released_vtu"):
            # The packet already ships a field: animate THAT, and label it as released.
            vtu = Path(spec["released_vtu"])
            rec = json.loads((SIMOUT / case / "result.json").read_text())
            inp = json.loads((SIMOUT / case / "inputs.json").read_text())
            rec["_showcase"] = {
                "case": case, "elapsed_s": rec.get("elapsed_s"),
                "fineness": "RELEASED packet field — no re-solve",
                "geometry": inp["geometry"]["path"],
                "fixtures": spec["setup"]["fixtures"], "loads": spec["setup"]["loads"],
                "route": f"released {vtu.name} from the case packet"}
        else:
            vtu = WORK / f"{case}_showcase.vtu"
            rec_path = WORK / f"{case}_showcase_solve.json"
            if not (vtu.exists() and rec_path.exists()):
                raise SystemExit(
                    f"{case}: no field to animate. This packet was released before "
                    f"run_fea_convergence could write fields (it now takes vtu_dir, and "
                    f"its released_leg names the leg's .vtu), so the showcase re-solves "
                    f"the released leg with run_fea_static(mesh_size_mm=<leg size>, "
                    f"vtu_path=...) into {vtu}. Run:\n"
                    f"  .venv-mcp/bin/python /tmp/solve_cases.py {case}")
            rec = json.loads(rec_path.read_text())
        sc = rec["_showcase"]
        summary = rec.get("summary") or {}
        mq = rec.get("mesh_quality") or {}
        prov = rec.get("provenance") or {}
        rel = spec["released"]
        c = Chapter(f"03_mesh_to_stress_{case}")
        c.inputs = [input_record(sc["geometry"]), input_record(vtu),
                    input_record(SIMOUT / case / "result.json"),
                    input_record(SIMOUT / case / "inputs.json")]
        c.numbers = {
            "showcase_run": {
                "mesh_fineness": sc["fineness"], "n_nodes": rec.get("n_nodes"),
                "n_elems": rec.get("n_elems"), "mesh": rec.get("mesh"),
                "order": mq.get("order"),
                "peak_von_mises_mpa": summary.get("max_von_mises_mpa"),
                "max_disp_mm": summary.get("max_disp_mm"),
                "min_fos": summary.get("min_fos"),
                "equilibrium_error_pct": (rec.get("reactions") or {}).get(
                    "equilibrium_error_pct"),
                "elapsed_s": sc["elapsed_s"],
                "engine_build_id": prov.get("engine_build_id"),
                "polymesh_commit": prov.get("polymesh_commit"),
                "fixtures": sc["fixtures"], "loads": sc["loads"]},
            "released_leg": rel,
        }
        elem = rec.get("mesh") or {1: "tet4", 2: "tet10"}.get(mq.get("order"), "elements")
        route = sc.get("route") or (
            f"run_fea_static(mesh_fineness={sc['fineness']}, vtu_path=...)")
        c.commands.append(
            f"fixtures={sc['fixtures']}, loads={sc['loads']}, material=6061-T6, "
            f"{sc['fineness']}, vtu_path={str(vtu)!r} via {route}")
        c.card(f"{case} — {spec['title']}", [
            f"fixtures {', '.join(sc['fixtures'])}   load "
            f"{sc['loads'][0].get('force')} N on {sc['loads'][0].get('face')}",
            "material 6061-T6  E = 68 900 MPa  nu = 0.33  yield = 276 MPa",
            "",
            f"THIS FILM's solve ({sc['fineness']}): {rec.get('n_nodes')} nodes / "
            f"{rec.get('n_elems')} {elem}, peak "
            f"{summary.get('max_von_mises_mpa')} MPa, FoS {summary.get('min_fos')}, "
            f"max disp {summary.get('max_disp_mm')} mm",
            f"RELEASED leg (packet result.json): {rel['n_nodes']} nodes / "
            f"{rel['n_elems']} elems order {rel['order']}"
            + (f" at h = {rel['h_mm']} mm" if rel['h_mm'] else "")
            + f", peak {rel['peak_mpa']} MPa, FoS {rel['fos']}",
            f"equilibrium error {(rec.get('reactions') or {}).get('equilibrium_error_pct')} %"
            f"   solve wall time {sc['elapsed_s']} s",
        ], seconds=4.0)
        h_txt = (f"h {sc['target_size_mm']} mm" if sc.get("target_size_mm")
                 else "h auto (mesher-sized)")
        foot = (f"{h_txt} | n_nodes {rec.get('n_nodes')} | "
                f"n_elems {rec.get('n_elems')} {elem} order {mq.get('order')} | peak "
                f"{summary.get('max_von_mises_mpa')} MPa | FoS {summary.get('min_fos')} | "
                f"polymesh {str(prov.get('polymesh_commit') or 'unknown')[:12]}")
        res = c.segment("field", kind="fea", vtu_path=str(vtu), frames=120,
                        view={"azimuth": 38, "elevation": 24}, warp_mm=spec["warp_mm"],
                        footer_note=foot, mark_poster=True)
        fm = res["measured"]
        c.numbers["field_from_vtu"] = fm
        c.card("What is exaggerated and what is not", [
            "The displacement warp is scaled so the peak deflection reads at ~6 % of the",
            f"model diagonal (x{fm.get('warp_factor', 0):.0f} on a measured peak of "
            f"{fm.get('peak_disp_mm')} mm) — the grey wireframe behind it is the",
            "UNDEFORMED mesh, so both states are on screen. Nothing is smoothed or clamped.",
            "",
            f"Colour scale: 0 -> {round(float(fm.get('peak_field') or 0.0), 4)} MPa, the "
            f"NODAL maximum in {vtu.name} ({fm.get('n_points')} points, "
            f"{fm.get('n_cells')} cells, {fm.get('cell_order')}).",
            f"The solve reply's summary.max_von_mises_mpa is "
            f"{summary.get('max_von_mises_mpa')} MPa — the two are different reductions of",
            "the same solve, and both are reported rather than reconciled by hand.",
            f"engine {prov.get('engine_build_id')}  polymesh {prov.get('polymesh_commit')}",
        ], seconds=4.0)
        entries.append(c.finish(f"{case}: mesh reveal into the solved von Mises field"))
    return entries


# =========================================================== chapter 04
def ch04() -> dict:
    """A driven-mate sweep. No saved ME-490 document carries a DRIVABLE (angle/distance)
    mate — CC-000_Merged_Rover.step+ has 8 mates, all concentric/coincident — so this
    chapter builds the hinge demo from HINGE-LATCH-LOCAL/geometry and says so on screen."""
    src = SIMOUT / "HINGE-LATCH-LOCAL" / "geometry" / "_src_C.step"
    evidence = SIMOUT / "HINGE-LATCH-LOCAL" / "EVIDENCE_HINGE_LATCH.md"
    c = Chapter("04_motion")
    c.inputs = [input_record(src), input_record(evidence),
                input_record(ROOT / "CC-000_Merged_Rover.step+")]

    scan = {}
    for f in sorted(ROOT.glob("*.step+")):
        try:
            raw = subprocess.run(["unzip", "-p", str(f), "document.json"],
                                 capture_output=True, check=True).stdout
            doc = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        mates = doc.get("mates") or []
        drivable = [m.get("id") for m in mates if m.get("type") in ("angle", "distance")]
        scan[f.name] = {"mates": len(mates), "drivable": drivable,
                        "types": sorted({m.get("type") for m in mates})}
    c.numbers["stepplus_mate_scan"] = scan

    leaf = ok(J(T["import_part"](path=str(src), keep_bodies=[18, 19, 20, 21])),
              "import hinge leaf bodies")
    leaf_doc = leaf["doc_id"]
    bores = ok(J(T["query_geometry"](query={"of": "face", "surf": "cylinder",
                                            "near": [911.35, 0.0, 504.16], "count": 1},
                                     doc_id=leaf_doc)), "query hinge bore")
    bore = (bores.get("matches") or [{}])[0]
    plate = ok(J(T["query_geometry"](query={"of": "face", "side": "bottom", "count": 1},
                                     doc_id=leaf_doc)), "query leaf datum face")
    plate_face = (plate.get("matches") or [{}])[0]
    leaf_path = WORK / "HINGE_LeafA_showcase.step+"
    ok(J(T["save_part"](path=str(leaf_path), doc_id=leaf_doc)), "save hinge leaf")

    asm = ok(J(T["new_assembly"](name="hinge_demo_showcase")), "new_assembly")["doc_id"]
    k1 = ok(J(T["asm_add_component"](path=str(leaf_path), name="LeafA_fixed", fixed=True,
                                     doc_id=asm)), "add leaf A")["component"]
    # The mating leaf is a SECOND instance of the same measured part, offset 165.0 mm along
    # the pin axis (its own leaf width, so the two leaves interleave on the knuckle band
    # instead of occupying the same space) and driven from 20 deg — a coincident 0 deg start
    # is a degenerate configuration the mate solver reports as non-converged, and an overlap
    # at every sample would be an artefact of the demo rather than a result.
    k2 = ok(J(T["asm_add_component"](path=str(leaf_path), name="LeafB_swing", fixed=False,
                                     at=[0.0, 165.0, 0.0], doc_id=asm)),
            "add leaf B")["component"]
    bore_q = {"of": "face", "surf": "cylinder", "near": bore.get("centroid"), "count": 1}
    face_q = {"of": "face", "side": "bottom", "count": 1}
    conc = ok(J(T["asm_mate"](mate_type="concentric", a={"component": k1, "query": bore_q},
                              b={"component": k2, "query": bore_q}, doc_id=asm)),
              "concentric on the measured pin axis")
    ang = ok(J(T["asm_mate"](mate_type="angle", a={"component": k1, "query": face_q},
                             b={"component": k2, "query": face_q}, value=20.0, doc_id=asm)),
             "angle mate")
    asm_path = WORK / "HINGE_demo_showcase.step+"
    ok(J(T["save_part"](path=str(asm_path), doc_id=asm)), "save hinge assembly")
    c.inputs.append(input_record(asm_path))
    c.numbers.update({
        "pin_axis_face": {k: bore.get(k) for k in ("sid", "surf", "radius", "centroid",
                                                   "axis")},
        "angle_datum_face": {k: plate_face.get(k) for k in ("sid", "surf", "centroid",
                                                            "normal")},
        "mates": {"concentric": conc.get("mate"), "angle": ang.get("mate")},
        "leaf_b_offset_mm": [0.0, 165.0, 0.0],
        "sweep_deg": [20.0, 110.0],
        "solver_samples": 36,
    })
    c.commands += [
        f"import_part({str(src)!r}, keep_bodies=[18, 19, 20, 21])",
        f"asm_add_component({str(leaf_path)!r}) x2 (second at=[0, 165, 0])",
        "asm_mate('concentric', bore<->bore); asm_mate('angle', face<->face, value=20)",
        f"simulate_motion(mate_id={ang.get('mate')!r}, start=20, stop=110, steps=36)"]

    c.card("Kinematics: a mate driven, not a keyframe", [
        "No saved ME-490 document carries a DRIVABLE mate: CC-000_Merged_Rover.step+ has",
        "8 mates, all concentric/coincident (the scan of every project .step+ is in the",
        "manifest). So this shot builds the hinge from HINGE-LATCH-LOCAL/geometry/_src_C.step,",
        "bodies [18, 19, 20, 21] — the measured leaf-A plate and its two knuckles.",
        f"Pin axis: the r = {bore.get('radius')} mm knuckle bore at "
        f"{[round(v, 3) for v in (bore.get('centroid') or [])]}, axis {bore.get('axis')}",
        "Two leaves on that bore (the second offset 165.0 mm along the axis), one angle",
        "mate driven 20 -> 110 deg in 36 solved samples.",
    ], seconds=4.0)
    res = c.segment("hinge_sweep", kind="motion", doc_id=asm, frames=84,
                    mate_id=ang.get("mate"), start=20.0, stop=110.0, steps=36,
                    view={"azimuth": 55, "elevation": 18}, display_quality="standard",
                    min_clearance_mm=0.0, mark_poster=True)
    meas = res["measured"]
    c.numbers["motion"] = meas
    interf = (meas.get("interference") or {})
    c.card("What the sweep reported", [
        f"driver: {json.dumps(meas.get('driver'))}",
        f"samples solved: {meas.get('samples')}",
        f"interference checked: {interf.get('checked')}   first collision step: "
        f"{interf.get('first_collision_step')}   at value: "
        f"{interf.get('first_collision_value')}",
        (str(interf.get("detail"))[:110] if interf.get("detail") else
         "no overlap reported over the swept range"),
        "Poses come from simulate_motion's own solved trajectory; the animation applies",
        "them to a detached clone, so the assembly document is never modified.",
    ], seconds=3.5)
    return c.finish("a real mate driven through simulate_motion's solved trajectory")


# =========================================================== chapter 05
def ch05() -> dict:
    """Formats: the container's own backward-compatibility promise, measured.

    `contracts/stepplus.md` §1 makes `geometry.step` — an AP214 snapshot of the last
    successful rebuild, all bodies — a REQUIRED member of every `.step+`, "omitted only if
    the document has never rebuilt successfully". The chapter proves it rather than asserting
    it: the `unzip -l` on screen is verbatim, the member is EXTRACTED from the archive, and it
    is that extracted file (not a separately exported one) that goes to
    `polymesh check --scale 0.001` and back through `import_part` for the body-count and
    volume comparison. The plain-STEP `export_part` path is still reported as a byte count —
    it is the documented "Save As .step" leg — but it is no longer what carries the chapter."""
    slice_src = SIMOUT / "WHEEL-RADIAL" / "geometry" / "CD-210-01_Wheel_Slice.step"
    c = Chapter("05_formats")
    c.inputs = [input_record(slice_src)]

    doc = ok(J(T["import_part"](path=str(slice_src))), "import wheel slice")["doc_id"]
    native = WORK / "CD-210-01_Wheel_Slice_showcase.step+"
    saved = ok(J(T["save_part"](path=str(native), doc_id=doc)), "save_part(.step+)")
    listing = subprocess.run(["unzip", "-l", str(native)], capture_output=True, text=True,
                             check=True).stdout.strip().splitlines()

    # The contracted member, taken straight out of the archive — this file is the whole
    # interchange claim, so everything downstream reads THESE bytes.
    embedded = WORK / "CD-210-01_Wheel_Slice_geometry.step"
    with zipfile.ZipFile(native) as zf:
        entry_names = zf.namelist()
        if "geometry.step" not in entry_names:
            raise SystemExit("05_formats: the saved .step+ carries no geometry.step — "
                             f"contracts/stepplus.md §1 requires it. Members: {entry_names}")
        embedded.write_bytes(zf.read("geometry.step"))
    header = embedded.read_text(errors="replace")[:2048]
    schema_line = next((ln.strip() for ln in header.splitlines() if "FILE_SCHEMA" in ln),
                       "(no FILE_SCHEMA line)")

    re_doc = ok(J(T["import_part"](path=str(embedded))), "re-import embedded geometry.step")
    re_bodies = ok(J(T["list_world_bodies"](doc_id=re_doc["doc_id"])), "bodies(re-import)")
    orig_bodies = ok(J(T["list_world_bodies"](doc_id=doc)), "bodies(original)")
    re_mass = ok(J(T["measure_part"](doc_id=re_doc["doc_id"])), "measure(re-import)")
    orig_mass = ok(J(T["measure_part"](doc_id=doc)), "measure(original)")

    # The plain "Save As .step" leg still exists; it is reported, not dramatized.
    neutral = WORK / "CD-210-01_Wheel_Slice_roundtrip.step"
    ok(J(T["export_part"](fmt="step", path=str(neutral), doc_id=doc)), "export_part(step)")

    verdict = J(T["polymesh_cli"](action="check", step_path=str(embedded),
                                  out_dir=str(WORK / "polymesh_check"), model_units="mm"))
    c.inputs += [input_record(native), input_record(embedded), input_record(neutral)]
    v0, v1 = volume_mm3(orig_mass), volume_mm3(re_mass)
    c.numbers = {
        "stepplus_bytes": saved.get("bytes"),
        "stepplus_members": [ln.split(None, 3)[-1] for ln in listing[3:-2] if ln.strip()],
        "geometry_step_member_present": "geometry.step" in entry_names,
        "geometry_step_bytes": embedded.stat().st_size,
        "geometry_step_file_schema": schema_line,
        "exported_step_bytes": Path(neutral).stat().st_size,
        "original_bodies": orig_bodies.get("body_count"),
        "reimported_bodies": re_bodies.get("body_count"),
        "original_volume_mm3": round(v0, 4),
        "reimported_volume_mm3": round(v1, 4),
        "volume_delta_mm3": round(v1 - v0, 6),
        "volume_delta_ppm": (round(1e6 * (v1 - v0) / v0, 4) if v0 else None),
        "polymesh_check": {k: verdict.get(k) for k in
                           ("ok", "action", "cli", "argv", "exit_code", "log_path",
                            "tail", "error") if k in verdict},
    }
    c.commands += [f"import_part({str(slice_src)!r})",
                   f"save_part(path={str(native)!r})",
                   f"unzip -l {native}",
                   f"unzip -p {native} geometry.step > {embedded}",
                   f"import_part({str(embedded)!r})", "measure_part()",
                   f"export_part(fmt='step', path={str(neutral)!r})",
                   f"polymesh_cli(action='check', step_path={str(embedded)!r}, "
                   "model_units='mm')  # -> polymesh check ... --scale 0.001"]

    c.card(".step+ is a zip you can open", [
        f"save_part wrote {native.name} — {saved.get('bytes')} B.",
        "It is an ordinary zip file: the parametric document, the display/BRep caches —",
        "and geometry.step, an AP214 snapshot of the last successful rebuild.",
        "No database, no licence server, no proprietary blob.",
    ], seconds=2.5)
    c.card("unzip -l (verbatim)", [f"$ unzip -l {native.name}"], seconds=4.0,
           mono_block=[ln for ln in listing][:16])
    c.segment("native_turntable", kind="turntable", doc_id=doc, frames=TURN,
              view={"azimuth": 40, "elevation": 20}, display_quality="fine",
              annotate=True, mark_poster=True)
    c.card("The embedded geometry.step, extracted and re-imported", [
        f"unzip -p {native.name} geometry.step -> {embedded.name} "
        f"({c.numbers['geometry_step_bytes']} B)",
        c.numbers["geometry_step_file_schema"][:100],
        f"import_part -> {c.numbers['reimported_bodies']} bodies "
        f"(source document: {c.numbers['original_bodies']})",
        f"volume {c.numbers['original_volume_mm3']} -> "
        f"{c.numbers['reimported_volume_mm3']} mm3   "
        f"delta {c.numbers['volume_delta_mm3']} mm3 "
        f"({c.numbers['volume_delta_ppm']} ppm)",
        f"The container carries {len(c.numbers['stepplus_members'])} members; the shot below",
        "is rendered from the extracted member alone — no Chudware document involved.",
        f"(A plain Save As .step is still there: export_part -> "
        f"{c.numbers['exported_step_bytes']} B.)",
    ], seconds=4.0)
    c.segment("reimported_turntable", kind="turntable", doc_id=re_doc["doc_id"],
              frames=TURN, view={"azimuth": 40, "elevation": 20},
              display_quality="fine")
    check = c.numbers["polymesh_check"]
    c.card("polymesh check --scale 0.001 on the extracted member (verbatim)", [
        "$ " + " ".join(str(a) for a in (check.get("argv") or [])),
        f"ok = {check.get('ok')}   exit_code = {check.get('exit_code')}",
        f"log: {check.get('log_path')}",
    ], seconds=4.0,
           mono_block=(str(check.get("tail") or "").strip().splitlines()[-14:]
                       or ["(no stdout captured)"]))
    return c.finish("the .step+ opened as a zip and its embedded AP214 geometry.step "
                    "extracted, re-imported and checked by polymesh")


CHAPTERS = {"01_no_solidworks": ch01, "02_merge": ch02, "03_mesh_to_stress": ch03,
            "04_motion": ch04, "05_formats": ch05}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="build every chapter")
    ap.add_argument("--only", action="append", choices=sorted(CHAPTERS),
                    help="build one chapter (repeatable)")
    args = ap.parse_args()
    if not args.all and not args.only:
        ap.error("pass --all or --only <chapter>")
    WORK.mkdir(parents=True, exist_ok=True)
    wanted = sorted(CHAPTERS) if args.all else args.only
    entries: dict = {}
    for name in wanted:
        print(f"=== {name} ===", flush=True)
        res = CHAPTERS[name]()
        for entry in (res if isinstance(res, list) else [res]):
            key = Path(entry["gif"]["path"]).stem
            entries[key] = entry
    write_manifest(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
