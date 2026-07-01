#!/usr/bin/env python3
"""H5 port helper: split host sketch_mode.cpp into ui/imgui/src/ui/sketch/* with seam transforms."""
from __future__ import annotations
import re
from pathlib import Path

ROOT = Path("/home/hunter/Repositories/CAD")
SRC = ROOT / "host/src/ui/sketch_mode.cpp"
OUT = ROOT / "ui/imgui/src/ui/sketch"

# Line ranges (1-based, inclusive) per output TU — body only, preamble injected separately.
SPLITS = {
    # lifecycle + worker + enter/exit + ribbon (no copy/paste, no quad/slot — those are edit/tools)
    "sketch_mode.cpp": [(218, 474), (610, 731), (964, 1422)],
    "sketch_tools.cpp": [(790, 963), (1433, 1729), (1940, 2097)],
    "sketch_edit.cpp": [(475, 604), (1730, 1939), (2216, 2711)],
    "sketch_constraints.cpp": [(2712, 3559)],
    "sketch_overlay.cpp": [(3560, 8214)],
}

PREAMBLE = '''// Sketch mode (H5 port from host/src/ui/sketch_mode.cpp). See sketch_mode.h.
#include "sketch_mode.h"
#include "sketch_internal.h"
#include "sketch_seam.h"
#include "sketch_palette.h"
#include "airfoil.h"
#include "platform/file_dialog.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <set>

using ui::sketch::SketchHost;
using ui::sketch::worldToScreen;
using ui::sketch::screenRay;
using ui::sketch::arcSweepContains;
#define skPal ui::sketch::sketchPalette()

namespace ui {
'''

TRANSFORMS = [
    # Remove duplicate camera math (F1 owns these).
    (r"// Project a world point.*?^}\n\n// Inverse of a column-major.*?^}\n\n// Unproject the cursor.*?^}\n\n", "", re.DOTALL | re.MULTILINE),
    (r"^bool worldToScreen\(.*?\n}\n", "", re.MULTILINE | re.DOTALL),
    (r"^bool invert4x4\(.*?\n}\n", "", re.MULTILINE | re.DOTALL),
    (r"^bool screenRay\(.*?\n}\n", "", re.MULTILINE | re.DOTALL),
    # AppState -> SketchHost
    (r"AppState&", "SketchHost&"),
    (r"\bAppState::Clipboard\b", "ui::sketch::SketchClipboard"),
    (r"st\.docMirror\b", "st.docMirror()"),
    (r"st\.worker\b", "st.worker()"),
    (r"st\.workerStarted\b", "st.workerStarted()"),
    (r"st\.renderer\b", "st.renderer"),
    (r"st\.selection\.items\(\)", "st.selectionItems()"),
    (r"ui::ToastLevel::", "ui::sketch::ToastLevel::"),
    (r"ui::CursorKind::", "ui::sketch::CursorKind::"),
    (r"ui::Palette::toU32", "skPal.toU32"),
    (r"ui::palette\b", "skPal"),
    (r"ui::uiScale\b", "ui::sketch::uiScale()"),
    (r"pmParseDim\b", "ui::sketch::parseSketchDim"),
    (r"pmFormatDim\b", "ui::sketch::formatSketchDim"),
    (r"render::PickKind::", "worker::PickKind::"),
    (r"render::PickHit", "worker::PickHit"),
    (r"openFileDialog\b", "platform::openCadFile"),
    (r"ui::platform::openCadFile\(\s*\"[^\"]*\"[^)]*\)", "platform::openCadFile()"),
    (r"st\.selection\.clear\(\)", "st.selectionClear()"),
    # arcSweepContains lives in sketch_internal.cpp
    (r"^// Is angle \(deg\) within the arc's drawn sweep\?.*?\nbool arcSweepContains\(const SketchEntity& arc, float ang\) \{.*?\n\}\n\n", "", re.DOTALL | re.MULTILINE),
    # catmullRomUV + helpers now in sketch_internal
    (r"inline std::vector<ImVec2> catmullRomUV.*?^}\n\n", "", re.DOTALL | re.MULTILINE),
    (r"^constexpr float kSnapPx.*?^bool arcSweepContains.*?\n\n", "", re.DOTALL | re.MULTILINE),
    (r"^ImU32 col\(.*?\n}\n\n", "", re.DOTALL | re.MULTILINE),
    (r"^bool circumcircle\(.*?\n}\n\n", "", re.DOTALL | re.MULTILINE),
    (r"^void drawDashedLine\(.*?\n}\n\n", "", re.DOTALL | re.MULTILINE),
    (r"^void drawDashedPolyline\(.*?\n}\n\n", "", re.DOTALL | re.MULTILINE),
    # camera_anim on exit — direct restore (anim lands G2)
    (r"ui::cameraAnimTo\(st, prevYaw_, prevPitch_, prevZoom_, prevPan_\);",
     "st.yaw = prevYaw_; st.pitch = prevPitch_; st.zoom = prevZoom_;\n"
     "    st.pan[0]=prevPan_[0]; st.pan[1]=prevPan_[1]; st.pan[2]=prevPan_[2];"),
    (r"#include \"camera_anim\.h\".*\n", ""),
    (r"#include \"property_manager\.h\".*\n", ""),
    (r"#include \"file_dialog\.h\".*\n", ""),
    (r"#include \"airfoil\.h\".*\n", ""),
    (r"#include \"\.\./core/app_state\.h\".*\n", ""),
    (r"#include \"\.\./renderer\.h\".*\n", ""),
    (r"#include \"\.\./worker_client\.h\".*\n", ""),
    (r"#include \"\.\./ui_theme\.h\".*\n", ""),
    (r"^#include \"sketch_mode\.h\".*\n", ""),
    (r"^// Sketch mode UI \(C18\).*?\n", ""),
]


def read_lines() -> list[str]:
    return SRC.read_text(encoding="utf-8").splitlines(keepends=True)


def extract_ranges(lines: list[str], ranges: list[tuple[int, int]]) -> str:
    chunks = []
    for a, b in ranges:
        chunks.append("".join(lines[a - 1 : b]))
    return "".join(chunks)


def transform(body: str) -> str:
    for item in TRANSFORMS:
        if len(item) == 2:
            pat, repl = item
            flags = 0
        else:
            pat, repl, flags = item
        body = re.sub(pat, repl, body, flags=flags)
    return body


def main() -> None:
    lines = read_lines()
    OUT.mkdir(parents=True, exist_ok=True)
    total_out = 0
    for fname, ranges in SPLITS.items():
        body = extract_ranges(lines, ranges)
        body = transform(body)
        content = PREAMBLE + body + "\n}  // namespace ui\n"
        path = OUT / fname
        path.write_text(content, encoding="utf-8")
        n = content.count("\n")
        total_out += n
        print(f"Wrote {path.name}: {n} lines")
    host_n = len(lines)
    pct = min(100, round(100 * total_out / host_n))
    print(f"Host source: {host_n} lines; ported output: {total_out} lines (~{pct}%)")


if __name__ == "__main__":
    main()
