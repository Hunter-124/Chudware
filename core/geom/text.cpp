#include "geom/text.h"

#include "geom/booleans.h"  // geom::fuse / geom::cut (IsDone-checked, empty on fail)

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <vector>

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

    // 3. Candidate assets/fonts directories, most-specific first.
    std::vector<fs::path> dirs;
#ifdef _WIN32
    {
        HMODULE self = nullptr;
        if (::GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                     GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                 reinterpret_cast<LPCSTR>(&resolve_font_path), &self) &&
            self) {
            char buf[MAX_PATH] = {0};
            DWORD n = ::GetModuleFileNameA(self, buf, MAX_PATH);
            if (n > 0 && n < MAX_PATH)
                dirs.push_back(fs::path(buf).parent_path() / "assets" / "fonts");
        }
    }
#endif
    if (const char* d = std::getenv("AI_CAD_DIST"))
        dirs.push_back(fs::path(d) / "assets" / "fonts");
    dirs.push_back(fs::path("dist") / "AI-CAD" / "assets" / "fonts");  // repo-root run
    dirs.push_back(fs::path("host") / "assets" / "fonts");             // dev tree

    for (const fs::path& d : dirs) {
        fs::path c = d / file;
        if (fs::exists(c, ec)) return c.string();
        fs::path r = d / "Rubik-Regular.ttf";  // named file missing? fall to the default
        if (fs::exists(r, ec)) return r.string();
    }

    // 4. Last resort: a Windows system font so a mark is still possible off-tree.
#ifdef _WIN32
    if (const char* w = std::getenv("WINDIR")) {
        for (const char* f : {"arial.ttf", "segoeui.ttf", "tahoma.ttf"}) {
            fs::path c = fs::path(w) / "Fonts" / f;
            if (fs::exists(c, ec)) return c.string();
        }
    }
#endif
    return "";  // nothing found -> emboss_text falls back gracefully
}

}  // namespace geom
