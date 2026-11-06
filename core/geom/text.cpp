#include "geom/text.h"

#include "geom/booleans.h"  // geom::fuse / geom::cut (IsDone-checked, empty on fail)
#include "util/platform_paths.h"  // developerSourceRoot: the checkout's shared assets/fonts

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <vector>

#ifndef _WIN32
#include <dlfcn.h>  // dladdr: locate the loaded libchudware module
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <string>
#endif

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX  // keep std::min/std::max usable in this TU
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

#include <BRepBuilderAPI_Transform.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <GProp_GProps.hxx>
#include <Graphic3d_HorizontalTextAlignment.hxx>
#include <Graphic3d_VerticalTextAlignment.hxx>
#include <NCollection_String.hxx>
#include <Standard_Failure.hxx>
#include <StdPrs_BRepFont.hxx>
#include <StdPrs_BRepTextBuilder.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Ax3.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace geom {

namespace {
double solid_volume(const TopoDS_Shape& s) {
    if (s.IsNull()) return 0.0;
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}

// Count the TopAbs_SOLIDs in a shape. An emboss that overhangs the target face fuses its
// off-face glyph prisms onto nothing, so they survive as SEPARATE floating solids — the
// solid count goes UP. Comparing before/after catches that (the per-body BRepCheck +
// volume-increase gate alone would pass a compound of disjoint valid solids).
int count_solids(const TopoDS_Shape& s) {
    int n = 0;
    for (TopExp_Explorer ex(s, TopAbs_SOLID); ex.More(); ex.Next()) ++n;
    return n;
}

// Does the shape contain at least one FACE? StdPrs_BRepTextBuilder hands back a compound
// of glyph faces; an all-space / no-glyph string yields an empty compound.
bool has_face(const TopoDS_Shape& s) {
    if (s.IsNull()) return false;
    TopExp_Explorer ex(s, TopAbs_FACE);
    return ex.More();
}
}  // namespace

TextResult emboss_text(const TopoDS_Shape& body, const gp_Pnt& face_origin,
                       const gp_Dir& normal, const gp_Dir& xdir, const std::string& text,
                       const std::string& font_path, double height, double depth,
                       bool engrave, int halign, int valign) {
    TextResult res;
    res.shape = body;  // default: hand back the body UNCHANGED (the fall-back)
    if (body.IsNull()) {
        res.warning = "text: empty body";
        return res;  // ok stays false — a truly unusable input
    }
    res.ok = true;  // we always at least return the body; ok=true means "feature ran"

    try {
        if (text.empty()) {
            res.fell_back = true;
            res.warning = "text: empty string — nothing to mark";
            return res;
        }
        if (!(height > 1e-4) || !(depth > 1e-4)) {
            res.fell_back = true;
            res.warning = "text: height and depth must be > 0";
            return res;
        }
        if (font_path.empty()) {
            res.fell_back = true;
            res.warning = "text: no font available — left the part unmarked";
            return res;
        }

        // -- load the font ----------------------------------------------------------
        // StdPrs_BRepFont sizes glyphs in MODEL units: a face size S gives an ascender
        // ~0.7-0.9*S. Init once at S=height, read the actual ascender, then re-init so
        // the ascender (the tall-letter height) equals `height` — a predictable
        // "the text is `height` mm tall" contract. Re-init is cheap (clears the cache).
        StdPrs_BRepFont font;
        if (!font.Init(NCollection_String(font_path.c_str()), height, 0)) {
            res.fell_back = true;
            res.warning = "text: could not load font '" + font_path +
                          "' — left the part unmarked";
            return res;
        }
        const double asc0 = font.Ascender();
        if (asc0 > 1e-6 && std::abs(asc0 - height) > 1e-3) {
            // ascender scales linearly with size: asc(S)=k*S, k=asc0/height. Want
            // asc(S')=height -> S' = height/k = height*height/asc0.
            const double better = height * height / asc0;
            if (better > 1e-4)
                font.Init(NCollection_String(font_path.c_str()), better, 0);
        }

        // -- render the glyph outlines to flat B-rep faces (in the XOY plane) --------
        Graphic3d_HorizontalTextAlignment ha =
            (halign == 1) ? Graphic3d_HTA_CENTER
                          : (halign == 2 ? Graphic3d_HTA_RIGHT : Graphic3d_HTA_LEFT);
        Graphic3d_VerticalTextAlignment va =
            (valign == 1) ? Graphic3d_VTA_CENTER
                          : (valign == 2 ? Graphic3d_VTA_TOP : Graphic3d_VTA_BOTTOM);
        StdPrs_BRepTextBuilder builder;
        TopoDS_Shape flat =
            builder.Perform(font, NCollection_String(text.c_str()), gp_Ax3(), ha, va);
        if (!has_face(flat)) {
            res.fell_back = true;
            res.warning = "text: the string rendered no glyphs (font lacks these "
                          "characters, or the string is all spaces) — left the part unmarked";
            return res;
        }

        // -- place the flat text on the target face plane ---------------------------
        // The text lives in XOY (x = reading direction, y = up, z = 0). Displace XOY so it
        // coincides with the face frame (face_origin, normal, xdir) -> the text lies ON the
        // face, reading along xdir, raised toward `normal`.
        gp_Ax3 target(face_origin, normal, xdir);
        gp_Trsf place;
        place.SetDisplacement(gp_Ax3(), target);
        // A tiny normal nudge so the prism overlaps the solid for a robust boolean: for an
        // ENGRAVE the tool must poke OUT of the face (start just proud, cut inward); for an
        // EMBOSS the tool base must sink slightly INTO the solid (start just below, raise
        // outward). eps is small vs depth so it does not visibly change the mark.
        const double eps = std::min(depth * 0.05, 0.02);
        const gp_Vec vnorm(normal);
        gp_Trsf nudge;
        nudge.SetTranslation(engrave ? vnorm.Multiplied(eps) : vnorm.Multiplied(-eps));
        TopoDS_Shape placed =
            BRepBuilderAPI_Transform(flat, nudge.Multiplied(place), true).Shape();
        if (!has_face(placed)) {
            res.fell_back = true;
            res.warning = "text: failed to position the text on the face — left it unmarked";
            return res;
        }

        // -- prism the glyph faces along the normal into a solid tool ---------------
        // ENGRAVE: extrude INWARD (-normal) by depth+eps; CUT it out (recessed mark).
        // EMBOSS:  extrude OUTWARD (+normal) by depth+eps; FUSE it on (proud mark).
        const double prism_len = depth + eps;
        const gp_Vec extrude =
            engrave ? vnorm.Multiplied(-prism_len) : vnorm.Multiplied(prism_len);
        BRepPrimAPI_MakePrism mk(placed, extrude);
        if (!mk.IsDone()) {
            res.fell_back = true;
            res.warning = "text: could not extrude the glyphs — left the part unmarked";
            return res;
        }
        TopoDS_Shape tool = mk.Shape();
        if (tool.IsNull() || solid_volume(tool) <= 1e-9) {
            res.fell_back = true;
            res.warning = "text: the extruded glyphs had no volume — left the part unmarked";
            return res;
        }

        // -- boolean the tool onto the body -----------------------------------------
        const double vol_before = solid_volume(body);
        const int solids_before = count_solids(body);
        TopoDS_Shape out = engrave ? cut(body, tool) : fuse(body, tool);
        const double vol_after = solid_volume(out);
        // Validity gate (mirror thread.cpp): a null / invalid / volume-unchanged result
        // means the mark did not take — fall back to the clean part rather than ship a
        // corrupt or no-op body. ENGRAVE must REMOVE material (vol_after < vol_before);
        // EMBOSS must ADD it (vol_after > vol_before).
        const bool changed = engrave ? (vol_after < vol_before - 1e-6)
                                      : (vol_after > vol_before + 1e-6);
        // The mark must not DISCONNECT the body: an emboss whose glyphs overhang the target
        // face fuses the off-face prisms onto nothing, leaving them as SEPARATE floating
        // solids (solid count rises); a too-deep engrave could likewise split the body.
        // Either is silent-wrong geometry the per-body validity check would miss — reject.
        const bool disconnected = count_solids(out) > solids_before;
        if (out.IsNull() || vol_after <= 1e-9 || !changed || disconnected ||
            !BRepCheck_Analyzer(out).IsValid()) {
            res.fell_back = true;
            res.warning =
                disconnected
                    ? std::string("text: the mark left detached bodies — the text overhangs "
                      "the target face. Fit it within the face (reduce height, shift offset, or "
                      "pick a bigger face); left the part unmarked")
                : engrave
                    ? std::string("text: the engrave produced no valid recess (the text may not "
                      "reach the surface, or the depth is too small) — left the part unmarked")
                    : std::string("text: the emboss produced no valid raised mark — left the "
                      "part unmarked");
            res.shape = body;
            return res;
        }
        res.shape = out;
        res.volume_delta = vol_after - vol_before;
        res.fell_back = false;
        return res;
    } catch (const Standard_Failure&) {
        res.fell_back = true;
        res.warning = "text: marking failed (OCCT exception) — left the part unmarked";
        res.shape = body;
        return res;
    } catch (...) {
        res.fell_back = true;
        res.warning = "text: marking failed — left the part unmarked";
        res.shape = body;
        return res;
    }
}

namespace {

// Absolute directory of the module this code is linked into (libchudware.{dll,so,dylib}, or
// the executable when the engine is linked statically, e.g. unit tests). Empty when unknown.
std::filesystem::path loaded_module_dir() {
    namespace fs = std::filesystem;
#ifdef _WIN32
    HMODULE self = nullptr;
    if (::GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                 GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                             reinterpret_cast<LPCSTR>(&loaded_module_dir), &self) &&
        self) {
        char buf[MAX_PATH] = {0};
        DWORD n = ::GetModuleFileNameA(self, buf, MAX_PATH);
        if (n > 0 && n < MAX_PATH) return fs::path(buf).parent_path();
    }
    return {};
#else
    Dl_info info;
    if (!::dladdr(reinterpret_cast<void*>(&loaded_module_dir), &info) || !info.dli_fname)
        return {};
    fs::path module(info.dli_fname);
#  if defined(__linux__)
    // The packaged engine is dlopen'd through /proc/self/fd/N and N is closed afterwards, so
    // dladdr reports that stale spelling; the memory map still names the real library.
    if (module.string().rfind("/proc/self/fd/", 0) == 0) {
        module.clear();
        const auto address = reinterpret_cast<std::uintptr_t>(&loaded_module_dir);
        std::ifstream maps("/proc/self/maps");
        std::string line;
        while (std::getline(maps, line)) {
            char* end = nullptr;
            const std::uintptr_t first = std::strtoull(line.c_str(), &end, 16);
            if (!end || *end != '-') continue;
            const std::uintptr_t last = std::strtoull(end + 1, &end, 16);
            if (address < first || address >= last) continue;
            const std::size_t slash = line.find('/');
            if (slash != std::string::npos) module = line.substr(slash);
            break;
        }
    }
#  endif
    // dladdr names the main executable by its exec string, which may be relative.
    if (module.empty() || !module.is_absolute()) return {};
    return module.parent_path();
#endif
}

}  // namespace

std::string resolve_font_path(const std::string& req) {
    namespace fs = std::filesystem;
    std::error_code ec;

    // 1. An explicit, existing font file path wins outright.
    if (!req.empty()) {
        fs::path p(req);
        if ((p.has_extension()) && fs::exists(p, ec)) return p.string();
    }

    // 2. Map a friendly name -> a bundled file (default: Rubik-Regular).
    std::string low;
    low.reserve(req.size());
    for (char c : req) low += static_cast<char>(std::tolower((unsigned char)c));
    std::string file = "Rubik-Regular.ttf";
    if (low.find("mono") != std::string::npos || low.find("jetbrains") != std::string::npos)
        file = "JetBrainsMono-Regular.ttf";
    else if (low.find("bold") != std::string::npos)
        file = "Rubik-Bold.ttf";
    else if (low.find("medium") != std::string::npos)
        file = "Rubik-Medium.ttf";

    // 3. Candidate assets/fonts directories, most-specific first:
    //    explicit dist dir -> install-relative -> source checkout -> (below) system fonts.
    std::vector<fs::path> dirs;
    if (const char* d = std::getenv("CHUDWARE_DIST"))
        dirs.push_back(fs::path(d) / "assets" / "fonts");

    // Install-relative. Each shipped layout is reached from the engine's directory and from the
    // executable's (a worker/daemon/test binary may host the engine):
    //   Windows          <dir>/assets/fonts                       (engine beside the exe)
    //   AppImage, .deb,  <root>/usr/lib/libchudware.so -> <root>/usr/bin/assets/fonts
    //   .rpm (/opt/chudware)   (packaging/linux/build_appimage.sh:412,465)
    //   macOS bundle     Contents/Frameworks/libchudware.dylib -> Contents/MacOS/assets/fonts
    //                    (packaging/macos/build_app.sh:393,412)
    // and <dir>/assets/fonts when the engine sits beside the assets (usr/bin, Contents/MacOS).
    std::vector<fs::path> bases;
    bases.push_back(loaded_module_dir());
    {
        const std::string exe = ::chudware::platform::executablePath();
        if (!exe.empty()) bases.push_back(fs::u8path(exe).parent_path());
    }
    std::vector<fs::path> seen;
    for (const fs::path& base : bases) {
        if (base.empty() || std::find(seen.begin(), seen.end(), base) != seen.end()) continue;
        seen.push_back(base);
        dirs.push_back(base / "assets" / "fonts");
        dirs.push_back(base.parent_path() / "bin" / "assets" / "fonts");
        dirs.push_back(base.parent_path() / "MacOS" / "assets" / "fonts");
    }

    dirs.push_back(fs::path("dist") / "Chudware" / "assets" / "fonts");  // repo-root run
    // Developer checkout (engine run from a build tree, e.g. ctest with cwd = the build dir):
    // the shared repo-root assets/ (moved out of host/ by the T-G11 de-dup). Anchored on the
    // executable / working directory's checkout, so it resolves wherever the build tree lives.
    // Without it a tree run uses whatever system font the scan hits first; on an image with
    // only variable fonts (Cantarell/Noto VF) every mark came out as "detached bodies".
    const fs::path source_root = ::chudware::platform::developerSourceRoot();
    if (!source_root.empty()) dirs.push_back(source_root / "assets" / "fonts");

    for (const fs::path& d : dirs) {
        fs::path c = d / file;
        if (fs::exists(c, ec)) return c.string();
        fs::path r = d / "Rubik-Regular.ttf";  // named file missing? fall to the default
        if (fs::exists(r, ec)) return r.string();
    }

    // 4. Last resort: a system font so a mark is still possible off-tree.
#ifdef _WIN32
    if (const char* w = std::getenv("WINDIR")) {
        for (const char* f : {"arial.ttf", "segoeui.ttf", "tahoma.ttf"}) {
            fs::path c = fs::path(w) / "Fonts" / f;
            if (fs::exists(c, ec)) return c.string();
        }
    }
#else
    // Unix (Linux/macOS): probe well-known font files first, then fall back to the
    // first .ttf/.otf found under the standard font roots — distros place fonts at
    // different paths (DejaVu / Liberation / Noto / Adwaita layouts all differ), so a
    // recursive scan is the only reliable catch-all.
    {
        const char* known[] = {
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
            "/usr/share/fonts/liberation-fonts/LiberationSans-Regular.ttf",
            "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",                 // Debian/Ubuntu
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/Library/Fonts/Arial.ttf",                                        // macOS
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        };
        for (const char* f : known) {
            if (fs::exists(f, ec)) return std::string(f);
        }
        std::vector<fs::path> roots = {"/usr/share/fonts", "/usr/local/share/fonts",
                                       "/Library/Fonts", "/System/Library/Fonts"};
        if (const char* home = std::getenv("HOME")) {
            roots.push_back(fs::path(home) / ".fonts");
            roots.push_back(fs::path(home) / ".local" / "share" / "fonts");
        }
        for (const fs::path& root : roots) {
            if (!fs::exists(root, ec)) continue;
            for (fs::recursive_directory_iterator it(root, ec), end; it != end; it.increment(ec)) {
                if (ec) break;
                if (!it->is_regular_file(ec)) continue;
                std::string ext = it->path().extension().string();
                for (char& c : ext) c = static_cast<char>(std::tolower((unsigned char)c));
                if (ext == ".ttf" || ext == ".otf") return it->path().string();
            }
        }
    }
#endif
    return "";  // nothing found -> emboss_text falls back gracefully
}

}  // namespace geom
