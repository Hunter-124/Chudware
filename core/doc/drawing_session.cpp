// Stateful 2D DrawingSession — see drawing_session.h. Port of the stateful half of
// engine/doc/drawing.py over the EXISTING HLR projection (doc/drawing.cpp).

#include "doc/drawing_session.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include "doc/annot_draw.h"  // compile_annotation / compile_hatch (shared primitives)
#include "doc/assembly.h"  // compile_assembly / placed_world_shape (multi-body / asm sourcing)
#include "doc/document.h"
#include "doc/drawing.h"
#include "doc/session.h"  // OpError

using nlohmann::json;

namespace aicad::doc {

namespace {

#ifndef AICAD_PI
#define AICAD_PI 3.14159265358979323846
#endif

constexpr const char* kSchemaDrawing = "aicad.drawing/1";

// ISO + ANSI sheet sizes in mm (drawing.py SHEET_DIMS_MM). Landscape (w >= h).
std::array<double, 2> sheet_dims(const std::string& size) {
    static const std::map<std::string, std::array<double, 2>> dims = {
        {"A4", {297.0, 210.0}},  {"A3", {420.0, 297.0}},  {"A2", {594.0, 420.0}},
        {"A1", {841.0, 594.0}},  {"A0", {1189.0, 841.0}}, {"A", {279.4, 215.9}},
        {"B", {431.8, 279.4}},   {"C", {558.8, 431.8}},   {"D", {863.6, 558.8}},
        {"E", {1117.6, 863.6}},
    };
    auto it = dims.find(size);
    return it == dims.end() ? std::array<double, 2>{0.0, 0.0} : it->second;
}

// ----------------------------------------------------------------- standard drawing scales
// The preferred drawing scales (ISO 5455 / ASME Y14.1), descending. A drawing is laid out at
// ONE of these so the title-block "SCALE" reads conventionally (e.g. 2:1, 1:1, 1:50) instead of
// an arbitrary fraction. Used by D5 auto-scale: pick the LARGEST standard scale whose scaled
// part footprint still fits the available view area (so the part fills the sheet without
// overflowing). Range spans 100:1 (tiny parts) down to 1:1000 (very large parts).
const std::vector<double>& standard_scales() {
    static const std::vector<double> k = {
        100.0, 50.0, 20.0, 10.0, 5.0, 2.0, 1.0,
        1.0 / 2, 1.0 / 5, 1.0 / 10, 1.0 / 20, 1.0 / 50,
        1.0 / 100, 1.0 / 200, 1.0 / 500, 1.0 / 1000,
    };
    return k;
}

// Snap an ideal (continuous) scale DOWN to the nearest standard scale that does not exceed it,
// so the scaled part is guaranteed to still fit the area `ideal` was sized for (we never round a
// scale UP past what fits). Clamped to the table's extremes.
double snap_scale_down(double ideal) {
    const auto& tbl = standard_scales();
    if (ideal >= tbl.front()) return tbl.front();
    for (double s : tbl)
        if (s <= ideal + 1e-12) return s;  // first (largest) standard scale <= ideal
    return tbl.back();
}

// Human "N:1" / "1:N" scale text for the title block (matches the SCALE field convention).
std::string scale_text(double scale) {
    char buf[48];
    if (scale >= 1.0) {
        double n = scale;
        if (std::fabs(n - std::round(n)) < 1e-6) std::snprintf(buf, sizeof(buf), "%d:1", (int)std::round(n));
        else std::snprintf(buf, sizeof(buf), "%.3g:1", n);
    } else {
        double inv = 1.0 / scale;
        if (std::fabs(inv - std::round(inv)) < 1e-6) std::snprintf(buf, sizeof(buf), "1:%d", (int)std::round(inv));
        else std::snprintf(buf, sizeof(buf), "1:%.3g", inv);
    }
    return std::string(buf);
}

// ----------------------------------------------------------------- session store
struct DrawingSession {
    std::string doc_id;
    std::string source_ref_id;  // LIVE source DocSession id ("doc-cpp...") — empty = inline
    json doc;             // the aicad.drawing/1 document
    json source_doc;      // the source part tree (initial snapshot / inline fallback)
    json source_env;      // equation env for the source compile
    long revision = 0;
    long source_revision = 0;
};

std::mutex g_mtx;
// Owner-tracking for crash recovery (see force_release_drawing_lock / drawing_session.h).
// Mirrors the identical pattern in core/doc/session.cpp (DocLock / force_release_doc_lock).
// g_mtx_held is true only while a DrawingLock is alive; g_mtx_owner is meaningful only
// when held. Together they let the SEH dispatch boundary detect and drop an orphaned lock
// after a Windows structured exception (access violation) without running C++ destructors.
std::atomic<bool> g_mtx_held{false};
std::thread::id g_mtx_owner;  // guarded by g_mtx (only read/written while held)

// RAII lock for the drawing-session store. Mirrors DocLock in session.cpp exactly.
class DrawingLock {
public:
    DrawingLock() {
        g_mtx.lock();
        g_mtx_owner = std::this_thread::get_id();
        g_mtx_held.store(true, std::memory_order_release);
    }
    ~DrawingLock() {
        g_mtx_held.store(false, std::memory_order_release);
        g_mtx.unlock();
    }
    DrawingLock(const DrawingLock&) = delete;
    DrawingLock& operator=(const DrawingLock&) = delete;
};

std::map<std::string, DrawingSession> g_drawings;
std::atomic<unsigned long long> g_seq{1};

std::string new_drawing_id() {
    unsigned long long n = g_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "dwg-cpp%08llx", n);
    return std::string(buf);
}

DrawingSession& require_session(const std::string& doc_id) {
    auto it = g_drawings.find(doc_id);
    if (it == g_drawings.end())
        throw OpError("unknown_doc", "no open drawing '" + doc_id + "'", {{"doc_id", doc_id}});
    return it->second;
}

json blank_drawing(const std::string& name, const std::string& source_doc_id) {
    return json{
        {"schema", kSchemaDrawing},
        {"id", std::string("doc-cppd") + std::to_string(g_seq.fetch_add(1))},
        {"name", name},
        {"units", {{"system", "MMGS"}}},
        {"source", source_doc_id},
        {"sheets", json::array({json{{"id", "s1"}, {"size", "A3"}, {"views", json::array()}}})},
    };
}

// ----------------------------------------------------------------- source geometry
// Compile the source tree into its surviving solid bodies (drawing.py _source_shapes).
// When the drawing references a LIVE source session (source_ref_id), re-pull that
// session's CURRENT tree on every fetch so the drawing stays associative to the part
// the user is editing — exactly like the Python oracle pulling the open source session.
// If that session is gone (closed), fall back to the last good inline snapshot. With no
// ref id, recompile the fixed inline tree (the stateless probe / parity path).
//
// EVERY body of the source goes into the SAME returned vector so each projected view
// receives the WHOLE part in the shared world frame — project_view HLR-adds all of them,
// so front/top/iso each show the complete model exactly like the 3D viewport (D10). A
// multi-body PART naturally yields all its surviving solids from compile_document; an
// ASSEMBLY source (schema aicad.asm/1) is NOT a feature tree — compile_document would
// ignore its components and project nothing — so it is resolved through compile_assembly
// and each component world-placed (placed_world_shape), the same geometry the viewport
// tessellates. Component-load failures degrade gracefully (fewer bodies), never throw.
std::vector<TopoDS_Shape> source_shapes(const DrawingSession& s) {
    json tree = s.source_doc;
    json env = s.source_env;
    if (!s.source_ref_id.empty()) {
        json live = doc_live_source(s.source_ref_id);
        if (live.is_object() && live.contains("tree")) {
            tree = live["tree"];
            env = live.value("env", json::object());
        }
    }
    // Assembly source: resolve + mate-solve its components and return every world-placed
    // body so all of them superimpose in each view (one combined projection per orientation).
    if (tree.is_object() && tree.value("schema", std::string()) == "aicad.asm/1") {
        std::vector<TopoDS_Shape> out;
        try {
            AssemblyResult ares = compile_assembly(tree);
            out.reserve(ares.bodies.size());
            for (const AsmBodyOut& b : ares.bodies) {
                TopoDS_Shape world = placed_world_shape(b);
                if (!world.IsNull()) out.push_back(world);
            }
        } catch (...) { /* unloadable assembly -> empty -> "no bodies to project" per view */ }
        return out;
    }
    CompileResult res = compile_document(tree, env.is_null() ? json::object() : env);
    return res.shapes;
}

// ----------------------------------------------------------------- view helpers
// All (sheet&, view&) pairs in document order. Returns pointers into doc.
std::vector<json*> all_views(json& doc) {
    std::vector<json*> out;
    if (doc.contains("sheets") && doc["sheets"].is_array())
        for (json& sh : doc["sheets"])
            if (sh.contains("views") && sh["views"].is_array())
                for (json& v : sh["views"]) out.push_back(&v);
    return out;
}

json* find_view(json& doc, const std::string& view_id) {
    for (json* v : all_views(doc))
        if (v->value("id", std::string()) == view_id) return v;
    return nullptr;
}

const json* find_view_c(const json& doc, const std::string& view_id) {
    if (doc.contains("sheets") && doc["sheets"].is_array())
        for (const json& sh : doc["sheets"])
            if (sh.contains("views") && sh["views"].is_array())
                for (const json& v : sh["views"])
                    if (v.value("id", std::string()) == view_id) return &v;
    return nullptr;
}

json& find_sheet(json& doc, const std::string& sheet_id) {
    if (!doc.contains("sheets") || !doc["sheets"].is_array() || doc["sheets"].empty())
        throw OpError("invalid_op", "drawing has no sheets", {{"reason", "no sheet"}});
    json& sheets = doc["sheets"];
    if (sheet_id.empty()) return sheets[0];
    for (json& sh : sheets)
        if (sh.value("id", std::string()) == sheet_id) return sh;
    throw OpError("invalid_op", "no sheet '" + sheet_id + "'", {{"reason", "unknown sheet"}});
}

std::string next_view_id(json& doc) {
    int hi = 0;
    for (json* v : all_views(doc)) {
        std::string vid = v->value("id", std::string());
        if (vid.size() >= 2 && vid[0] == 'v') {
            bool digits = true;
            for (std::size_t i = 1; i < vid.size(); ++i)
                if (!std::isdigit((unsigned char)vid[i])) { digits = false; break; }
            if (digits) hi = std::max(hi, std::atoi(vid.c_str() + 1));
        }
    }
    return "v" + std::to_string(hi + 1);
}

std::string next_label(json& doc) {
    std::set<char> used;
    for (json* v : all_views(doc)) {
        if (v->contains("label") && (*v)["label"].is_string()) {
            std::string lab = (*v)["label"].get<std::string>();
            if (lab.size() == 1 && std::isalpha((unsigned char)lab[0]))
                used.insert((char)std::toupper((unsigned char)lab[0]));
        }
    }
    for (char c = 'A'; c <= 'Z'; ++c)
        if (!used.count(c)) return std::string(1, c);
    return "A";
}

std::string next_ann_id(const json& view) {
    int hi = 0;
    if (view.contains("annotations") && view["annotations"].is_array())
        for (const json& a : view["annotations"]) {
            std::string aid = a.value("id", std::string());
            if (aid.size() >= 2 && aid[0] == 'a') {
                bool digits = true;
                for (std::size_t i = 1; i < aid.size(); ++i)
                    if (!std::isdigit((unsigned char)aid[i])) { digits = false; break; }
                if (digits) hi = std::max(hi, std::atoi(aid.c_str() + 1));
            }
        }
    return "a" + std::to_string(hi + 1);
}

std::array<double, 2> get_pos(const json& v, std::array<double, 2> dflt = {0.0, 0.0}) {
    if (v.is_array() && v.size() >= 2 && v[0].is_number() && v[1].is_number())
        return {v[0].get<double>(), v[1].get<double>()};
    return dflt;
}

bool is_xy(const json& p) {
    return p.is_array() && p.size() == 2 && p[0].is_number() && p[1].is_number() &&
           !p[0].is_boolean() && !p[1].is_boolean();
}

std::string doc_units(const json& doc) {
    if (doc.contains("units") && doc["units"].is_object()) {
        std::string u = doc["units"].value("system", std::string());
        if (u == "MMGS" || u == "IPS") return u;
    }
    return "MMGS";
}

// ----------------------------------------------------------------- _shift_to_position
// Translate a cached (origin-centered) projection to a sheet position. Mirrors
// drawing.py::_shift_to_position (cheap; no re-HLR). Operates on a copy.
json shift_to_position(const json& proj, std::array<double, 2> pos) {
    double dx = pos[0], dy = pos[1];
    json out = proj;
    if (dx == 0.0 && dy == 0.0) return out;
    auto shift_pls = [&](json& pls) {
        if (!pls.is_array()) return;
        for (json& pl : pls)
            for (json& p : pl) {
                p[0] = p[0].get<double>() + dx;
                p[1] = p[1].get<double>() + dy;
            }
    };
    if (out.contains("visible")) shift_pls(out["visible"]);
    if (out.contains("hidden")) shift_pls(out["hidden"]);
    if (out.contains("section_faces")) shift_pls(out["section_faces"]);
    if (out.contains("arcs") && out["arcs"].is_array())
        for (json& arc : out["arcs"]) {
            arc["center"][0] = arc["center"][0].get<double>() + dx;
            arc["center"][1] = arc["center"][1].get<double>() + dy;
        }
    if (out.contains("bbox") && out["bbox"].is_array() && out["bbox"].size() == 4) {
        out["bbox"][0] = out["bbox"][0].get<double>() + dx;
        out["bbox"][1] = out["bbox"][1].get<double>() + dy;
        out["bbox"][2] = out["bbox"][2].get<double>() + dx;
        out["bbox"][3] = out["bbox"][3].get<double>() + dy;
    }
    return out;
}

// ----------------------------------------------------------- section cut-plane resolve
// orientation -> (view_dir, screen_x, screen_y) unit vectors (drawing.py _orient_frame).
using Vec3 = std::array<double, 3>;
Vec3 v_norm(Vec3 v) {
    double n = std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
    if (n < 1e-30) n = 1.0;
    return {v[0] / n, v[1] / n, v[2] / n};
}
Vec3 v_cross(const Vec3& a, const Vec3& b) {
    return {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]};
}
double v_dot(const Vec3& a, const Vec3& b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

void orient_frame(const std::string& orientation, Vec3& d, Vec3& sx, Vec3& sy) {
    Vec3 view_dir, screen_x;
    if (orientation == "iso") {
        view_dir = v_norm({-1, -1, -1});
        screen_x = v_norm(v_cross({0, 0, 1}, view_dir));
    } else if (orientation == "front") { view_dir = {0, 1, 0}; screen_x = {1, 0, 0}; }
    else if (orientation == "back")   { view_dir = {0, -1, 0}; screen_x = {-1, 0, 0}; }
    else if (orientation == "top")    { view_dir = {0, 0, -1}; screen_x = {1, 0, 0}; }
    else if (orientation == "bottom") { view_dir = {0, 0, 1};  screen_x = {1, 0, 0}; }
    else if (orientation == "right")  { view_dir = {-1, 0, 0}; screen_x = {0, 1, 0}; }
    else if (orientation == "left")   { view_dir = {1, 0, 0};  screen_x = {0, -1, 0}; }
    else
        throw OpError("invalid_op", "unknown orientation '" + orientation + "'",
                      {{"reason", "bad orientation"}});
    d = v_norm(view_dir);
    sx = v_norm(screen_x);
    sy = v_norm(v_cross(d, sx));
}

// Resolve a section view's cut plane -> world {normal, point} json for project_section.
// Mirrors drawing.py::_resolve_section_plane (explicit-plane OR cut_line-in-parent form).
json resolve_section_plane(const json& view, const json& parent_view) {
    // Custom-section controls carried on the view -> the resolved world plane.
    auto attach_mode = [&](json plane) -> json {
        if (view.contains("mode") && view["mode"].is_string()) plane["mode"] = view["mode"];
        if (view.contains("depth") && view["depth"].is_number()) plane["depth"] = view["depth"];
        if (view.contains("reverse") && view["reverse"].is_boolean())
            plane["reverse"] = view["reverse"];
        return plane;
    };
    if (view.contains("plane") && view["plane"].is_object() &&
        view["plane"].contains("normal") && view["plane"].contains("point")) {
        // Explicit world plane — pass through (project_section validates further).
        return attach_mode(view["plane"]);
    }
    const json& cut_line = view.contains("cut_line") ? view["cut_line"] : json(nullptr);
    // Accept a 2-point straight cut OR a multi-point (offset/jogged) polyline cut line; the
    // section plane is taken through the first and last points (the dominant cut direction),
    // and the jog points still draw on the parent as a stepped cut-line marker.
    if (!(cut_line.is_array() && cut_line.size() >= 2 && is_xy(cut_line[0]) &&
          is_xy(cut_line[cut_line.size() - 1])))
        throw OpError("invalid_op",
                      "section needs a world 'plane' {normal, point} or a 2D 'cut_line' "
                      "[[x,y],[x,y],...]",
                      {{"reason", "no plane"}});
    std::string p_orient = parent_view.value("orientation", std::string("front"));
    double p_scale = parent_view.value("scale", 1.0);
    if (p_scale == 0.0) p_scale = 1.0;
    std::array<double, 2> p_pos = get_pos(parent_view.value("position", json::array({0.0, 0.0})));
    Vec3 d, sx_p, sy_p;
    orient_frame(p_orient, d, sx_p, sy_p);
    auto unproj = [&](const json& q) -> Vec3 {
        double u = (q[0].get<double>() - p_pos[0]) / p_scale;
        double v = (q[1].get<double>() - p_pos[1]) / p_scale;
        return {sx_p[0] * u + sy_p[0] * v, sx_p[1] * u + sy_p[1] * v, sx_p[2] * u + sy_p[2] * v};
    };
    Vec3 a = unproj(cut_line[0]);
    Vec3 b = unproj(cut_line[cut_line.size() - 1]);
    Vec3 line = {b[0] - a[0], b[1] - a[1], b[2] - a[2]};
    if (std::sqrt(v_dot(line, line)) < 1e-9)
        throw OpError("invalid_op", "section cut_line endpoints coincide", {{"reason", "degenerate"}});
    line = v_norm(line);
    Vec3 plane_normal = v_cross(line, d);
    if (std::sqrt(v_dot(plane_normal, plane_normal)) < 1e-9)
        throw OpError("invalid_op", "section cut line is parallel to the view direction",
                      {{"reason", "degenerate"}});
    plane_normal = v_norm(plane_normal);
    // Anchor the plane at the cut-line MIDPOINT, not endpoint `a`. Both endpoints lie on the
    // cut plane, so this leaves the section plane unchanged — but it matters for a HALF section
    // (D11): the half division half-space starts at `point`, so anchoring at an endpoint (which
    // can sit at or beyond the part's extent along the cut direction) made the division cover the
    // whole part and degenerate the half section into a full one. The midpoint bisects the part.
    Vec3 mid = {(a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5, (a[2] + b[2]) * 0.5};
    json plane = {{"normal", json::array({plane_normal[0], plane_normal[1], plane_normal[2]})},
                  {"point", json::array({mid[0], mid[1], mid[2]})}};
    // For a half section, keep the unsectioned half along the cut-line direction in world.
    plane["half_axis"] = json::array({line[0], line[1], line[2]});
    return attach_mode(plane);
}

// ----------------------------------------------------------------- _project_one
// Project a single view. Dispatches on the view "type": section cuts + projects;
// detail clips the parent's projected geometry; ortho/iso go through project_view.
// (drawing.py::_project_one. The per-(rev,orient,scale,hidden) cache is omitted —
// the C++ core recomputes; the OUTPUT is identical, only timing differs.)
json project_one(DrawingSession& s, const json& view,
                 const std::vector<TopoDS_Shape>& src_shapes) {
    std::string vtype = view.value("type", std::string());
    double scale = view.value("scale", 1.0);
    bool show_hidden = view.value("show_hidden", true);
    std::array<double, 2> pos = get_pos(view.value("position", json::array({0.0, 0.0})));
    std::string vid = view.value("id", std::string("v?"));

    // D3-followup: a DERIVED view (section/detail) re-projection that FAILS must NOT throw out
    // of drawing_get — drawing_get re-projects EVERY view on each fetch, so one bad derived view
    // (e.g. a section whose cut line went degenerate after the parent moved/rescaled, or a detail
    // whose parent now errors) would abort the whole sheet and make review_drawing report "not a
    // drawing document". Instead, contain the failure per view: record an `error` field + empty
    // geometry (the SAME shape project_view/project_detail already use for contained failures, and
    // the same shape the `!parent` detail fallback below uses), so the rest of the sheet renders.
    auto derived_error = [&](const std::string& orient, const std::string& msg) -> json {
        return json{{"view_id", vid}, {"scale", scale}, {"orientation", orient},
                    {"visible", json::array()}, {"hidden", json::array()},
                    {"section_faces", json::array()}, {"arcs", json::array()},
                    {"bbox", json(nullptr)}, {"ms", 0.0}, {"error", msg}};
    };
    json proj;
    if (vtype == "section") {
        const json* parent = view.contains("parent") && view["parent"].is_string()
                                 ? find_view_c(s.doc, view["parent"].get<std::string>())
                                 : nullptr;
        try {
            json plane = resolve_section_plane(view, parent ? *parent : json::object());
            proj = project_section(src_shapes, plane, scale, pos, show_hidden, vid);
        } catch (const OpError& e) {
            proj = derived_error("section", e.what());
        } catch (const std::exception& e) {
            proj = derived_error("section", std::string("section projection failed: ") + e.what());
        }
    } else if (vtype == "detail") {
        const json* parent = view.contains("parent") && view["parent"].is_string()
                                 ? find_view_c(s.doc, view["parent"].get<std::string>())
                                 : nullptr;
        std::array<double, 2> center = get_pos(view.value("center", json::array({0.0, 0.0})));
        double radius = view.value("radius", 0.0);
        if (!parent) {
            proj = {{"view_id", vid}, {"scale", scale}, {"orientation", "detail"},
                    {"visible", json::array()}, {"hidden", json::array()},
                    {"bbox", json(nullptr)}, {"ms", 0.0},
                    {"error", "detail view has no resolvable parent"}};
        } else {
            try {
                json parent_geom = project_one(s, *parent, src_shapes);
                proj = project_detail(parent_geom, center, radius, scale, pos, vid);
            } catch (const OpError& e) {
                proj = derived_error("detail", e.what());
            } catch (const std::exception& e) {
                proj = derived_error("detail", std::string("detail projection failed: ") + e.what());
            }
        }
    } else {
        std::string orient = view.value("orientation", std::string("front"));
        // Project at origin then translate (mirrors the cache+_shift_to_position path).
        proj = project_view(src_shapes, orient, scale, {0.0, 0.0}, show_hidden, vid);
        proj = shift_to_position(proj, pos);
    }

    json out = proj;
    out["view_id"] = view.value("id", json(nullptr));
    out["type"] = vtype.empty() ? json(nullptr) : json(vtype);
    out["position"] = json::array({pos[0], pos[1]});
    out["show_hidden"] = show_hidden;
    if (!out.contains("arcs")) out["arcs"] = json::array();
    if (vtype == "section") {
        out["parent"] = view.value("parent", json(nullptr));
        if (view.contains("cut_line")) out["cut_line"] = view["cut_line"];
        if (view.contains("plane")) out["plane"] = view["plane"];
    } else if (vtype == "detail") {
        out["parent"] = view.value("parent", json(nullptr));
        std::array<double, 2> center = get_pos(view.value("center", json::array({0.0, 0.0})));
        out["center"] = json::array({center[0], center[1]});
        out["radius"] = view.value("radius", 0.0);
    }
    out["annotations"] = view.contains("annotations") && view["annotations"].is_array()
                             ? view["annotations"]
                             : json::array();
    if (view.contains("label")) out["label"] = view["label"];
    return out;
}

// ----------------------------------------------------------------- parent markers
// Decorate parent views with section_lines / detail_circles (drawing.py
// _attach_parent_markers). Mutates views_out in place.
void attach_parent_markers(json& views_out) {
    std::map<std::string, json*> by_id;
    for (json& g : views_out) by_id[g.value("view_id", std::string())] = &g;
    for (json& g : views_out) {
        std::string t = g.value("type", std::string());
        if (t == "section" && g.contains("cut_line") && !g["cut_line"].is_null()) {
            auto it = by_id.find(g.value("parent", std::string()));
            if (it != by_id.end())
                (*it->second)["section_lines"].push_back(
                    json{{"label", g.value("label", json(nullptr))},
                         {"view_id", g.value("view_id", json(nullptr))},
                         {"line", g["cut_line"]}});
        } else if (t == "detail") {
            auto it = by_id.find(g.value("parent", std::string()));
            if (it != by_id.end() && g.contains("center"))
                (*it->second)["detail_circles"].push_back(
                    json{{"label", g.value("label", json(nullptr))},
                         {"view_id", g.value("view_id", json(nullptr))},
                         {"center", g["center"]}, {"radius", g.value("radius", 0.0)}});
        }
    }
}

// ----------------------------------------------------------------- annotation math
std::string format_dim_value(double value, const std::string& units, const std::string& kind) {
    char buf[64];
    if (kind == "angular_dim") {
        std::snprintf(buf, sizeof(buf), "%.4g\xC2\xB0", value);  // U+00B0 degree
        return std::string(buf);
    }
    std::string body;
    if (units == "IPS") {
        std::snprintf(buf, sizeof(buf), "%.4g in", value / 25.4);
        body = buf;
    } else {
        std::snprintf(buf, sizeof(buf), "%.4g", value);
        body = buf;
    }
    if (kind == "radial_dim") return "R" + body;
    if (kind == "diameter_dim") return "\xE2\x8C\x80" + body;  // U+2300 diameter sign
    return body;
}

// Format a length (model mm) in the drawing's display units, no unit word. `prec` < 0
// uses %.4g (significant-figures, the default look); >= 0 uses fixed decimals.
std::string fmt_len(double mm, bool ips, int prec) {
    double v = ips ? mm / 25.4 : mm;
    char buf[64];
    if (prec >= 0) std::snprintf(buf, sizeof(buf), "%.*f", prec, v);
    else std::snprintf(buf, sizeof(buf), "%.4g", v);
    return std::string(buf);
}

// Compose the on-sheet display string for a dimension annotation from its canonical
// model `value` + optional detailing fields (count / prefix / suffix / precision /
// tolerance). A drawing is a fixed-unit deliverable, so units follow the drawing's own
// units.system. Non-dimension kinds (notes / center marks / center lines) never reach
// here. Tolerance styles: symmetric "x ±t", deviation "x +tu / -tl", limit "hi / lo",
// basic "[x]".
std::string compose_disp(const json& ann, const std::string& units) {
    std::string kind = ann.value("kind", std::string());
    if (kind.empty()) kind = ann.value("type", std::string());
    bool ips = (units == "IPS");
    bool angular = (kind == "angular_dim");
    double value = ann.value("value", 0.0);
    int prec = -1;
    if (ann.contains("precision") && ann["precision"].is_number_integer())
        prec = ann["precision"].get<int>();
    int tprec = prec >= 0 ? prec : 2;
    std::string pre = ann.value("prefix", std::string());
    std::string suf = ann.value("suffix", std::string());
    int count = ann.value("count", 0);
    std::string tt = ann.value("tol_type", std::string("none"));
    double tu = ann.value("tol_upper", 0.0), tl = ann.value("tol_lower", 0.0);

    // Standard STACKED hole callout (ASME Y14.5): a hole_callout that carries a hole_type
    // (counterbore/countersink/tapped) renders the conventional one-line stacked form
    //   counterbore: "N× ⌀d ⌴ ⌀D ↧ h"   countersink: "N× ⌀d ⌵ ⌀D × A°"
    //   tapped:      "N× <thread> ↧ depth"   simple/blind: "N× ⌀d [↧ depth]"
    // (⌀ U+2300, ⌴ U+2334, ⌵ U+2335, ↧ U+21A7, × U+00D7, ° U+00B0). The `count` multiplier
    // collapses identical holes to a single leader; an explicit `text_override` still wins
    // upstream. This is the localization for D4's counterbore/countersink formatting.
    {
        std::string ht = ann.value("hole_type", std::string());
        if (kind == "hole_callout" && !ht.empty() && ht != "simple") {
            const std::string DIA = "\xE2\x8C\x80";    // U+2300
            const std::string CBORE = "\xE2\x8C\xB4";  // U+2334
            const std::string CSK = "\xE2\x8C\xB5";    // U+2335
            const std::string DEPTH = "\xE2\x86\xA7";  // U+21A7
            const std::string TIMES = "\xC3\x97";      // U+00D7
            const std::string DEG = "\xC2\xB0";        // U+00B0
            double d = value;  // pilot/minor diameter (model mm)
            double D = ann.value("cbore_dia", 0.0);
            double h = ann.value("cbore_depth", 0.0);
            double ang = ann.value("csk_angle", 0.0);
            double depth = ann.value("depth", 0.0);
            std::string thread = ann.value("thread", std::string());
            std::string out;
            if (ht == "tapped") {
                out = (!thread.empty() ? thread : DIA + fmt_len(d, ips, prec));
                if (depth > 1e-9) out += " " + DEPTH + " " + fmt_len(depth, ips, prec);
            } else if (ht == "counterbore" || ht == "cbore") {
                out = DIA + fmt_len(d, ips, prec) + " " + CBORE + " " + DIA + fmt_len(D, ips, prec);
                if (h > 1e-9) out += " " + DEPTH + " " + fmt_len(h, ips, prec);
            } else if (ht == "countersink" || ht == "csk") {
                out = DIA + fmt_len(d, ips, prec) + " " + CSK + " " + DIA + fmt_len(D, ips, prec);
                if (ang > 1e-9) {  // angle is degrees, never unit-converted
                    char ab[32];
                    if (prec >= 0) std::snprintf(ab, sizeof(ab), "%.*f", prec, ang);
                    else std::snprintf(ab, sizeof(ab), "%.4g", ang);
                    out += " " + TIMES + " " + std::string(ab) + DEG;
                }
            } else {
                out = DIA + fmt_len(d, ips, prec);
                if (depth > 1e-9) out += " " + DEPTH + " " + fmt_len(depth, ips, prec);
            }
            out = pre + out + suf;
            if (count > 1) out = std::to_string(count) + TIMES + " " + out;
            return out;
        }
    }

    std::string sym;  // leading symbol
    if (kind == "radial_dim") sym = "R";
    else if (kind == "diameter_dim" || kind == "hole_callout") sym = "\xE2\x8C\x80";  // U+2300

    std::string core;
    if (angular) {
        char buf[64];
        if (prec >= 0) std::snprintf(buf, sizeof(buf), "%.*f", prec, value);
        else std::snprintf(buf, sizeof(buf), "%.4g", value);
        core = std::string(buf) + "\xC2\xB0";  // U+00B0
    } else if (tt == "limit") {
        core = fmt_len(value + tu, ips, tprec) + " / " + fmt_len(value - tl, ips, tprec);
    } else {
        core = fmt_len(value, ips, prec);
        if (tt == "symmetric")
            core += " \xC2\xB1" + fmt_len(tu, ips, tprec);  // U+00B1 plus-minus
        else if (tt == "deviation")
            core += " +" + fmt_len(tu, ips, tprec) + " / -" + fmt_len(tl, ips, tprec);
    }

    std::string body = sym + core;
    if (ips && !angular) body += " in";
    body = pre + body + suf;
    if (count > 1) body = std::to_string(count) + "\xC3\x97 " + body;  // U+00D7 multiply
    if (tt == "basic" && !angular) body = "[" + body + "]";
    return body;
}

double round6(double v) { return std::round(v * 1e6) / 1e6; }

// Parse a metric standard size string (e.g. "M4", "M10", "M3.5") to its nominal diameter
// in model mm. Accepts "M<digits>" and "M<digits>.<digits>" (ISO 261 / DIN 13 nominal).
// Returns 0.0 for anything that does not parse as a valid metric size (unrecognised strings,
// imperial strings, or sizes below 1 mm that are likely noise). Used as a last-resort fallback
// in the hole callout path so an M-standard hole never prints a literal "DIA 0" / "⌀0".
double parse_metric_standard_diameter(const std::string& std_str) {
    if (std_str.empty()) return 0.0;
    const char* p = std_str.c_str();
    // Accept leading 'M' or 'm' only.
    if (*p != 'M' && *p != 'm') return 0.0;
    ++p;
    // Parse digits (and optional single decimal point) for the nominal size.
    char* end = nullptr;
    double d = std::strtod(p, &end);
    if (end == p || d < 1.0) return 0.0;  // must be at least 1 mm to be credible
    return d;
}

// The arc in geom["arcs"] whose ring lies nearest `point` (drawing.py::_nearest_arc).
// `min_r` skips degenerate near-zero-radius projected circles: a chamfered / counterbored /
// multi-step hole projects SEVERAL concentric circles, and the silhouette of a chamfer cone or a
// tiny relief can land an almost-zero-radius "circle" at the hole axis. When the pick sits at the
// hole centre, nearest_arc's metric |dist-r| reduces to r, so that spurious ~0 ring would WIN and
// the callout diameter would read 0 (D-B2). A small positive `min_r` (sheet mm) excludes those so
// the real bore is chosen; pass 0 to keep the historical any-radius behaviour.
const json* nearest_arc(const json& geom, const json& point, int want_closed /*-1 any,0,1*/,
                        double min_r = 0.0) {
    if (!geom.contains("arcs") || !geom["arcs"].is_array()) return nullptr;
    double px = point[0].get<double>(), py = point[1].get<double>();
    const json* best = nullptr;
    double best_d = 0.0;
    for (const json& arc : geom["arcs"]) {
        bool closed = arc.value("closed", false);
        if (want_closed == 1 && !closed) continue;
        if (want_closed == 0 && closed) continue;
        double cx = arc["center"][0].get<double>(), cy = arc["center"][1].get<double>();
        double r = arc.value("radius", 0.0);
        if (r < min_r) continue;  // skip degenerate / spurious near-zero-radius circles
        double d = std::fabs(std::hypot(px - cx, py - cy) - r);
        if (!best || d < best_d) { best_d = d; best = &arc; }
    }
    return best;
}

std::array<double, 2> line_dir(const json& a, const json& b) {
    double dx = b[0].get<double>() - a[0].get<double>();
    double dy = b[1].get<double>() - a[1].get<double>();
    double n = std::hypot(dx, dy);
    if (n < 1e-9)
        throw OpError("invalid_op", "angular dim line endpoints coincide",
                      {{"reason", "degenerate line"}});
    return {dx / n, dy / n};
}

double angle_between_lines(const json& a, const json& b, const json& c, const json& d) {
    auto u = line_dir(a, b);
    auto v = line_dir(c, d);
    double dot = std::max(-1.0, std::min(1.0, u[0] * v[0] + u[1] * v[1]));
    return std::acos(dot) * 180.0 / AICAD_PI;
}

json line_intersection(const json& a, const json& b, const json& c, const json& d) {
    double x1 = a[0].get<double>(), y1 = a[1].get<double>();
    double x2 = b[0].get<double>(), y2 = b[1].get<double>();
    double x3 = c[0].get<double>(), y3 = c[1].get<double>();
    double x4 = d[0].get<double>(), y4 = d[1].get<double>();
    double den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4);
    if (std::fabs(den) < 1e-12) return json(nullptr);
    double t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den;
    return json::array({x1 + t * (x2 - x1), y1 + t * (y2 - y1)});
}

// drawing_get without taking g_mtx (the caller already holds it). The full drawing
// doc + every view's re-projected geometry (drawing.py::get_drawing_full).
json drawing_get_locked_unsafe(const std::string& doc_id) {
    DrawingSession& s = require_session(doc_id);
    auto shapes = source_shapes(s);
    json sheets_out = json::array();
    double total_ms = 0.0;
    if (s.doc.contains("sheets") && s.doc["sheets"].is_array())
        for (const json& sh : s.doc["sheets"]) {
            json views_out = json::array();
            if (sh.contains("views") && sh["views"].is_array())
                for (const json& v : sh["views"]) {
                    json geom = project_one(s, v, shapes);
                    total_ms += geom.value("ms", 0.0);
                    views_out.push_back(geom);
                }
            attach_parent_markers(views_out);
            // Compile every annotation into shared draw primitives (one source of truth
            // for the host renderer + DXF/PDF export + PNG raster), and pre-compute the
            // section-face hatch engine-side so exports/PNG hatch too.
            std::string units = doc_units(s.doc);
            for (json& g : views_out) {
                if (g.contains("annotations") && g["annotations"].is_array())
                    for (json& an : g["annotations"])
                        if (an.is_object()) an["draw"] = compile_annotation(an, units);
                if (g.contains("section_faces") && g["section_faces"].is_array()) {
                    json hatch = json::array();
                    for (const json& poly : g["section_faces"]) {
                        json hr = compile_hatch(poly, 0.0, 45.0);
                        if (hr.contains("segments"))
                            for (const json& sgmt : hr["segments"]) hatch.push_back(sgmt);
                    }
                    g["hatch"] = hatch;
                }
            }
            std::array<double, 2> sz = sheet_dims(sh.value("size", std::string()));
            sheets_out.push_back(json{{"id", sh.value("id", json(nullptr))},
                                      {"size", sh.value("size", json(nullptr))},
                                      {"size_mm", json::array({sz[0], sz[1]})},
                                      {"title_block", sh.value("title_block", json::object())},
                                      {"views", views_out}});
        }
    return json{
        {"doc_id", doc_id}, {"revision", s.revision},
        {"source_doc_id", s.source_ref_id.empty() ? s.doc_id : s.source_ref_id},
        {"source_revision", s.source_revision}, {"tree", s.doc}, {"sheets", sheets_out},
        {"total_hlr_ms", round6(total_ms)},
    };
}

}  // namespace

// ----------------------------------------------------------------- public API

json drawing_new(const json& source_doc, const json& env, const std::string& name,
                 const std::string& source_doc_id) {
    DrawingLock lk;
    // LIVE source: snapshot the open session's current tree+env now (for the name + a
    // fallback if it later closes) and remember the id for the associative re-pull.
    json src = source_doc;
    json src_env = env.is_null() ? json::object() : env;
    long src_rev = 0;
    if (!source_doc_id.empty()) {
        json live = doc_live_source(source_doc_id);
        if (live.is_object() && live.contains("tree")) {
            src = live["tree"];
            src_env = live.value("env", json::object());
            src_rev = live.value("revision", 0L);
        }
    }
    std::string src_name;
    if (src.is_object() && src.contains("name") && src["name"].is_string())
        src_name = src["name"].get<std::string>();
    if (src_name.empty()) src_name = "Part";
    std::string nm = name.empty() ? (src_name + " - Drawing") : name;
    std::string doc_id = new_drawing_id();
    DrawingSession s;
    s.doc_id = doc_id;
    s.source_ref_id = source_doc_id;
    s.source_doc = src;
    s.source_env = src_env;
    // The drawing's "source" field records the part it projects (the live ref id when
    // present, else its own id for the inline probe — matches drawing.py's source ref).
    s.doc = blank_drawing(nm, source_doc_id.empty() ? doc_id : source_doc_id);
    s.revision = 0;
    s.source_revision = src_rev;
    g_drawings[doc_id] = std::move(s);
    DrawingSession& cur = g_drawings[doc_id];
    return json{{"doc_id", cur.doc_id}, {"drawing", cur.doc}};
}

json drawing_set_source(const std::string& doc_id, const json& source_doc, const json& env) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    // An explicit inline source replaces any live ref (the caller now owns the tree).
    s.source_ref_id.clear();
    s.source_doc = source_doc;
    s.source_env = env.is_null() ? json::object() : env;
    s.source_revision += 1;
    return json{{"doc_id", doc_id}, {"revision", s.revision}, {"source_revision", s.source_revision}};
}

json drawing_add_view(const std::string& doc_id, const std::string& type,
                      const std::string& orientation, const std::string& sheet_id,
                      const json& scale, const json& position, const json& show_hidden) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json& sheet = find_sheet(s.doc, sheet_id);
    if (type != "base" && type != "projected" && type != "iso")
        throw OpError("invalid_op", "view type '" + type + "' must be base|projected|iso",
                      {{"reason", "bad type"}});
    std::string orient = (type == "iso") ? "iso" : orientation;
    json view = {
        {"id", next_view_id(s.doc)},
        {"type", type},
        {"orientation", orient},
        {"scale", scale.is_number() ? scale.get<double>() : 1.0},
        {"position", is_xy(position) ? json::array({position[0].get<double>(), position[1].get<double>()})
                                     : json::array({0.0, 0.0})},
        {"show_hidden", show_hidden.is_boolean() ? show_hidden.get<bool>() : true},
    };
    sheet["views"].push_back(view);
    s.revision += 1;
    auto shapes = source_shapes(s);
    json geom = project_one(s, view, shapes);
    return json{{"view_id", view["id"]}, {"revision", s.revision}, {"view", geom}};
}

json drawing_add_section(const std::string& doc_id, const std::string& parent_view_id,
                         const json& plane, const json& cut_line, const json& label,
                         const json& scale, const json& position, const std::string& sheet_id,
                         const json& show_hidden, const json& opts) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json& sheet = find_sheet(s.doc, sheet_id);
    json* parent = find_view(s.doc, parent_view_id);
    if (!parent)
        throw OpError("invalid_op", "section parent view '" + parent_view_id + "' not found",
                      {{"reason", "unknown parent"}});
    if ((plane.is_null() || !plane.is_object()) && (cut_line.is_null() || !cut_line.is_array()))
        throw OpError("invalid_op",
                      "section needs a world 'plane' {normal, point} or a 2D 'cut_line'",
                      {{"reason", "no plane"}});
    std::string lab = (label.is_string() && !label.get<std::string>().empty())
                          ? label.get<std::string>()
                          : next_label(s.doc);
    double p_scale = parent->value("scale", 1.0);
    std::array<double, 2> p_pos = get_pos(parent->value("position", json::array({0.0, 0.0})));
    // Default placement: to the RIGHT of the parent — but never off the sheet. If +120 mm would
    // push the section past the right margin (e.g. the parent already sits in the right-most grid
    // column), drop it BELOW the parent instead, both clamped to the drawable area. Without this a
    // right-most auto-section landed at x>sheet_w and rendered as a clipped sliver off the edge.
    double def_x = p_pos[0] + 120.0, def_y = p_pos[1];
    {
        std::array<double, 2> ssz = sheet_dims("A3");
        const json& smm = sheet.value("size_mm", json(nullptr));
        if (smm.is_array() && smm.size() == 2 && smm[0].is_number() && smm[0].get<double>() > 0.0)
            ssz = {smm[0].get<double>(), smm[1].get<double>()};
        const double smarg = 20.0;
        if (def_x > ssz[0] - smarg) {  // overflows right edge -> place below the parent
            def_x = std::min(std::max(p_pos[0], smarg), ssz[0] - smarg);
            def_y = std::max(smarg, p_pos[1] - 110.0);
        }
    }
    json view = {
        {"id", next_view_id(s.doc)},
        {"type", "section"},
        {"orientation", "section"},
        {"parent", parent_view_id},
        {"label", lab},
        {"scale", scale.is_number() ? scale.get<double>() : (p_scale != 0.0 ? p_scale : 1.0)},
        {"position", is_xy(position)
                         ? json::array({position[0].get<double>(), position[1].get<double>()})
                         : json::array({def_x, def_y})},
        {"show_hidden", show_hidden.is_boolean() ? show_hidden.get<bool>() : false},
    };
    if (plane.is_object()) view["plane"] = plane;
    if (cut_line.is_array() && cut_line.size() >= 2) {
        // Store the full (possibly jogged) cut-line polyline; resolve uses first+last.
        json cl = json::array();
        for (const auto& q : cut_line)
            if (is_xy(q)) cl.push_back(json::array({q[0].get<double>(), q[1].get<double>()}));
        if (cl.size() >= 2) view["cut_line"] = cl;
    }
    // Custom-section controls (optional, default a full straight section).
    if (opts.is_object()) {
        std::string mode = opts.value("mode", std::string());
        if (mode == "full" || mode == "half") view["mode"] = mode;
        if (opts.contains("depth") && opts["depth"].is_number() && opts["depth"].get<double>() > 0.0)
            view["depth"] = opts["depth"].get<double>();
        if (opts.contains("reverse") && opts["reverse"].is_boolean())
            view["reverse"] = opts["reverse"].get<bool>();
    }
    sheet["views"].push_back(view);
    s.revision += 1;
    auto shapes = source_shapes(s);
    json geom = project_one(s, view, shapes);
    return json{{"view_id", view["id"]}, {"revision", s.revision}, {"view", geom}};
}

json drawing_add_detail(const std::string& doc_id, const std::string& parent_view_id,
                        const json& center, double radius, const json& scale, const json& label,
                        const json& position, const std::string& sheet_id, const json& show_hidden) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json& sheet = find_sheet(s.doc, sheet_id);
    json* parent = find_view(s.doc, parent_view_id);
    if (!parent)
        throw OpError("invalid_op", "detail parent view '" + parent_view_id + "' not found",
                      {{"reason", "unknown parent"}});
    if (!is_xy(center))
        throw OpError("invalid_op", "detail center must be [x,y] (parent sheet mm)",
                      {{"reason", "bad center"}});
    if (radius <= 0.0)
        throw OpError("invalid_op", "detail radius must be a positive number",
                      {{"reason", "bad radius"}});
    std::string lab = (label.is_string() && !label.get<std::string>().empty())
                          ? label.get<std::string>()
                          : next_label(s.doc);
    double p_scale = parent->value("scale", 1.0);
    if (p_scale == 0.0) p_scale = 1.0;
    std::array<double, 2> p_pos = get_pos(parent->value("position", json::array({0.0, 0.0})));
    json view = {
        {"id", next_view_id(s.doc)},
        {"type", "detail"},
        {"orientation", "detail"},
        {"parent", parent_view_id},
        {"label", lab},
        {"center", json::array({center[0].get<double>(), center[1].get<double>()})},
        {"radius", radius},
        {"scale", scale.is_number() ? scale.get<double>() : p_scale * 2.0},
        {"position", is_xy(position)
                         ? json::array({position[0].get<double>(), position[1].get<double>()})
                         : json::array({p_pos[0] + 150.0, p_pos[1]})},
        {"show_hidden", show_hidden.is_boolean() ? show_hidden.get<bool>() : false},
    };
    sheet["views"].push_back(view);
    s.revision += 1;
    auto shapes = source_shapes(s);
    json geom = project_one(s, view, shapes);
    return json{{"view_id", view["id"]}, {"revision", s.revision}, {"view", geom}};
}

json drawing_remove_view(const std::string& doc_id, const std::string& view_id) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    bool removed = false;
    if (s.doc.contains("sheets") && s.doc["sheets"].is_array())
        for (json& sh : s.doc["sheets"]) {
            if (!sh.contains("views") || !sh["views"].is_array()) continue;
            json kept = json::array();
            for (const json& v : sh["views"]) {
                if (v.value("id", std::string()) == view_id) removed = true;
                else kept.push_back(v);
            }
            sh["views"] = kept;
        }
    if (!removed)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    s.revision += 1;
    return json{{"removed", true}, {"revision", s.revision}};
}

json drawing_set_view(const std::string& doc_id, const std::string& view_id, const json& scale,
                      const json& position, const json& orientation, const json& show_hidden) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    if (scale.is_number()) {
        if (scale.get<double>() <= 0.0)
            throw OpError("invalid_op", "scale must be positive", {{"reason", "bad scale"}});
        (*view)["scale"] = scale.get<double>();
    }
    if (is_xy(position))
        (*view)["position"] = json::array({position[0].get<double>(), position[1].get<double>()});
    if (orientation.is_string() && view->value("type", std::string()) != "iso")
        (*view)["orientation"] = orientation.get<std::string>();
    if (show_hidden.is_boolean()) (*view)["show_hidden"] = show_hidden.get<bool>();
    s.revision += 1;
    auto shapes = source_shapes(s);
    json geom = project_one(s, *view, shapes);
    return json{{"view_id", view_id}, {"revision", s.revision}, {"view", geom}};
}

namespace {
// Build one extended (SW-parity) annotation (drawing.py::_build_ext_annotation).
json build_ext_annotation(DrawingSession& s, const json& view, const std::string& kind,
                          const json& a, const json& b, const json& c, const json& d,
                          const json& pos, const json& text, const std::string& units,
                          double scale) {
    double ax = a[0].get<double>(), ay = a[1].get<double>();
    std::string base_id = next_ann_id(view);

    if (kind == "radial_dim" || kind == "diameter_dim" || kind == "center_mark") {
        auto shapes = source_shapes(s);
        json geom = project_one(s, view, shapes);
        int want_closed = (kind == "diameter_dim") ? 1 : -1;
        const json* arc = nearest_arc(geom, a, want_closed);
        if (!arc) {
            std::string what = (kind == "diameter_dim") ? "full circle" : "circular arc";
            throw OpError("invalid_op", kind + ": no projected " + what + " near pick in this view",
                          {{"reason", "no arc"}});
        }
        double cx = (*arc)["center"][0].get<double>(), cy = (*arc)["center"][1].get<double>();
        double r_sheet = arc->value("radius", 0.0);
        double r_model = r_sheet / scale;
        double value = 0.0;
        if (kind == "radial_dim") value = r_model;
        else if (kind == "diameter_dim") value = 2.0 * r_model;
        json ppos = is_xy(pos) ? json::array({pos[0].get<double>(), pos[1].get<double>()})
                               : json::array({cx + r_sheet + 8.0, cy + r_sheet + 8.0});
        return json{
            {"id", base_id}, {"type", "note"}, {"kind", kind},
            {"a", json::array({ax, ay})}, {"b", json::array({cx, cy})},
            {"center", json::array({cx, cy})}, {"radius", round6(r_sheet)},
            {"model_radius", round6(r_model)}, {"closed", arc->value("closed", false)},
            {"pos", ppos}, {"value", round6(value)},
            {"text", (text.is_string() && !text.get<std::string>().empty())
                         ? text.get<std::string>()
                         : format_dim_value(value, units, kind)},
        };
    }

    if (kind == "angular_dim") {
        if (!is_xy(b) || !is_xy(c) || !is_xy(d))
            throw OpError("invalid_op", "angular_dim needs four sheet points a,b,c,d",
                          {{"reason", "bad points"}});
        double value = angle_between_lines(a, b, c, d);
        json vtx = line_intersection(a, b, c, d);
        if (vtx.is_null())
            vtx = json::array({(ax + c[0].get<double>()) / 2.0, (ay + c[1].get<double>()) / 2.0});
        json ppos = is_xy(pos) ? json::array({pos[0].get<double>(), pos[1].get<double>()})
                               : json::array({vtx[0].get<double>() + 12.0, vtx[1].get<double>() + 12.0});
        return json{
            {"id", base_id}, {"type", "note"}, {"kind", "angular_dim"},
            {"a", json::array({ax, ay})},
            {"b", json::array({b[0].get<double>(), b[1].get<double>()})},
            {"line2", json::array({json::array({c[0].get<double>(), c[1].get<double>()}),
                                   json::array({d[0].get<double>(), d[1].get<double>()})})},
            {"vertex", json::array({vtx[0].get<double>(), vtx[1].get<double>()})},
            {"pos", ppos}, {"value", round6(value)},
            {"text", (text.is_string() && !text.get<std::string>().empty())
                         ? text.get<std::string>()
                         : format_dim_value(value, units, "angular_dim")},
        };
    }

    // center_line — a construction line between two picked endpoints.
    if (!is_xy(b))
        throw OpError("invalid_op", "center_line needs two sheet points a,b",
                      {{"reason", "bad points"}});
    double bx = b[0].get<double>(), by = b[1].get<double>();
    json ppos = is_xy(pos) ? json::array({pos[0].get<double>(), pos[1].get<double>()})
                           : json::array({(ax + bx) / 2.0, (ay + by) / 2.0});
    return json{
        {"id", base_id}, {"type", "note"}, {"kind", "center_line"},
        {"a", json::array({ax, ay})}, {"b", json::array({bx, by})},
        {"pos", ppos}, {"value", 0.0}, {"text", text.is_string() ? text.get<std::string>() : ""},
    };
}
}  // namespace

json drawing_add_dim(const std::string& doc_id, const std::string& view_id, const std::string& type,
                     const json& a, const json& b_in, const json& c, const json& d, const json& pos,
                     const json& text) {
    static const std::set<std::string> kAnn = {"linear_dim", "aligned_dim", "note"};
    static const std::set<std::string> kExt = {"radial_dim", "diameter_dim", "angular_dim",
                                               "center_mark", "center_line"};
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    if (!kAnn.count(type) && !kExt.count(type))
        throw OpError("invalid_op", "annotation type '" + type + "' not recognized",
                      {{"reason", "bad type"}});
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    if (!is_xy(a))
        throw OpError("invalid_op", "dimension point a must be [x,y] sheet mm",
                      {{"reason", "bad points"}});

    std::string units = doc_units(s.doc);
    double scale = view->value("scale", 1.0);
    if (scale == 0.0) scale = 1.0;

    if (kExt.count(type)) {
        json ann = build_ext_annotation(s, *view, type, a, b_in, c, d, pos, text, units, scale);
        (*view)["annotations"].push_back(ann);
        s.revision += 1;
        return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
    }

    json b = b_in;
    if (type == "note" && !is_xy(b)) b = a;
    if (!is_xy(b))
        throw OpError("invalid_op", "dimension endpoints a,b must each be [x,y] sheet mm",
                      {{"reason", "bad points"}});

    double ax = a[0].get<double>(), ay = a[1].get<double>();
    double bx = b[0].get<double>(), by = b[1].get<double>();
    // A linear / aligned dimension between coincident endpoints would measure 0 — reject it
    // up front rather than emit a meaningless "0" dimension (D9). (A `note` legitimately uses
    // a == b as a single leader-anchor for free text, so it is exempt.)
    if (type != "note" && std::hypot(bx - ax, by - ay) < 1e-6)
        throw OpError("invalid_op",
                      "dimension: endpoints are coincident (zero-length dimension)",
                      {{"reason", "coincident endpoints"}});
    double sheet_dist;
    if (type == "linear_dim") sheet_dist = std::max(std::fabs(bx - ax), std::fabs(by - ay));
    else if (type == "aligned_dim") sheet_dist = std::hypot(bx - ax, by - ay);
    else sheet_dist = 0.0;
    double value = sheet_dist / scale;

    json ppos;
    if (!is_xy(pos)) {
        double mx = (ax + bx) / 2.0, my = (ay + by) / 2.0;
        double dx = bx - ax, dy = by - ay;
        double n = std::hypot(dx, dy);
        if (n == 0.0) n = 1.0;
        ppos = json::array({mx - dy / n * 10.0, my + dx / n * 10.0});
    } else {
        ppos = json::array({pos[0].get<double>(), pos[1].get<double>()});
    }

    json ann = {
        {"id", next_ann_id(*view)}, {"type", type},
        {"a", json::array({ax, ay})}, {"b", json::array({bx, by})}, {"pos", ppos},
        {"value", round6(value)},
        {"text", (text.is_string() && !text.get<std::string>().empty())
                     ? text.get<std::string>()
                     : format_dim_value(value, units, type)},
    };
    (*view)["annotations"].push_back(ann);
    s.revision += 1;
    return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_remove_annotation(const std::string& doc_id, const std::string& view_id,
                               const std::string& ann_id) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    json kept = json::array();
    bool found = false;
    if (view->contains("annotations") && (*view)["annotations"].is_array())
        for (const json& an : (*view)["annotations"]) {
            if (an.value("id", std::string()) == ann_id) found = true;
            else kept.push_back(an);
        }
    if (!found)
        throw OpError("invalid_op", "no annotation '" + ann_id + "' on view '" + view_id + "'",
                      {{"reason", "unknown annotation"}});
    (*view)["annotations"] = kept;
    s.revision += 1;
    return json{{"removed", true}, {"view_id", view_id}, {"ann_id", ann_id}, {"revision", s.revision}};
}

json drawing_get(const std::string& doc_id) {
    DrawingLock lk;
    return drawing_get_locked_unsafe(doc_id);
}

json drawing_bom(const std::string& doc_id) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    const std::string src_id = s.source_ref_id;
    // No live source (inline probe) -> no BOM. Else ask the source session for its real
    // components (empty array when the source is a part or has since closed).
    json comps = src_id.empty() ? json::array() : doc_asm_bom_components(src_id);
    if (!comps.is_array() || comps.empty())
        return json{{"rows", json::array()}, {"is_assembly", false},
                    {"source_doc_id", src_id.empty() ? doc_id : src_id},
                    {"note", src_id.empty() ? "inline-source drawing has no BOM"
                                            : "part drawings have no BOM"}};
    // Group identical parts: same source path (or name when there is no path). Each
    // component contributes its own quantity (pattern instances already rolled in).
    std::vector<std::string> order;
    std::map<std::string, json> groups;
    for (const json& c : comps) {
        std::string name = c.value("name", std::string());
        if (name.empty()) name = c.value("component", std::string("Part"));
        std::string source = c.value("source", std::string());
        std::string key = !source.empty() ? source : ("name:" + name);
        auto g = groups.find(key);
        if (g == groups.end()) {
            groups[key] = json{{"name", name}, {"source", source}, {"quantity", 0}};
            order.push_back(key);
        }
        groups[key]["quantity"] = groups[key]["quantity"].get<int>() + c.value("quantity", 1);
    }
    json rows = json::array();
    int item = 1;
    for (const std::string& key : order) {
        const json& g = groups[key];
        rows.push_back({{"item", item++}, {"name", g["name"]},
                        {"quantity", g["quantity"]}, {"source", g["source"]}});
    }
    return json{{"rows", rows}, {"is_assembly", true}, {"source_doc_id", src_id}};
}

std::map<std::string, std::string> drawings_for_source(const std::string& source_doc_id) {
    DrawingLock lk;
    std::map<std::string, std::string> out;
    for (const auto& [id, s] : g_drawings)
        if (s.source_ref_id == source_doc_id)
            out["drawings/" + id + ".json"] = s.doc.dump();
    return out;
}

json register_restored_drawing(const json& drawing_doc, const std::string& source_doc_id) {
    DrawingLock lk;
    if (!drawing_doc.is_object() ||
        drawing_doc.value("schema", std::string()) != kSchemaDrawing)
        throw OpError("invalid_op",
                      std::string("register_restored_drawing: not a ") + kSchemaDrawing +
                          " document",
                      {{"reason", "bad schema"}});
    const std::string doc_id = new_drawing_id();
    DrawingSession s;
    s.doc_id = doc_id;
    s.source_ref_id = source_doc_id;  // re-point the live source to the reopened part
    s.doc = drawing_doc;
    s.doc["source"] = source_doc_id;  // keep the doc's own source ref in sync
    s.source_doc = json::object();    // no inline snapshot — re-pull live from the source
    s.source_env = json::object();
    s.revision = 0;
    s.source_revision = 0;
    g_drawings[doc_id] = std::move(s);
    return json{{"doc_id", doc_id},
                {"name", g_drawings[doc_id].doc.value("name", std::string("Drawing"))},
                {"source_doc_id", source_doc_id}};
}

// ----------------------------------------------------------- detailing (W17 overhaul)
// The numeric dimension kinds whose on-sheet text is composed from value + detailing
// fields (vs. notes / center marks / center lines, whose text is literal).
static bool is_numeric_dim(const std::string& type, const std::string& kind) {
    const std::string& dk = kind.empty() ? type : kind;
    return dk == "linear_dim" || dk == "aligned_dim" || dk == "radial_dim" ||
           dk == "diameter_dim" || dk == "angular_dim" || dk == "hole_callout";
}

json drawing_set_annotation(const std::string& doc_id, const std::string& view_id,
                            const std::string& ann_id, const json& patch) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    json* ann = nullptr;
    if (view->contains("annotations") && (*view)["annotations"].is_array())
        for (json& a : (*view)["annotations"])
            if (a.value("id", std::string()) == ann_id) { ann = &a; break; }
    if (!ann)
        throw OpError("invalid_op", "no annotation '" + ann_id + "' on view '" + view_id + "'",
                      {{"reason", "unknown annotation"}});

    if (patch.is_object())
        for (auto it = patch.begin(); it != patch.end(); ++it) {
            const std::string& k = it.key();
            const json& v = it.value();
            if (k == "tol_type" && v.is_string()) (*ann)["tol_type"] = v;
            else if (k == "prefix" && v.is_string()) (*ann)["prefix"] = v;
            else if (k == "suffix" && v.is_string()) (*ann)["suffix"] = v;
            else if (k == "critical" && v.is_boolean()) (*ann)["critical"] = v;
            else if ((k == "tol_upper" || k == "tol_lower" || k == "value") && v.is_number())
                (*ann)[k] = v.get<double>();
            else if (k == "precision" && v.is_number_integer()) (*ann)["precision"] = v.get<int>();
            else if (k == "count" && v.is_number_integer()) (*ann)["count"] = v.get<int>();
            else if (k == "pos" && is_xy(v))
                (*ann)["pos"] = json::array({v[0].get<double>(), v[1].get<double>()});
            else if (k == "text") (*ann)["text_override"] = v.is_string() ? v : json("");
        }

    std::string units = doc_units(s.doc);
    std::string ov = ann->value("text_override", std::string());
    std::string type = ann->value("type", std::string());
    std::string kind = ann->value("kind", std::string());
    if (is_numeric_dim(type, kind))
        (*ann)["text"] = ov.empty() ? compose_disp(*ann, units) : ov;
    else if (!ov.empty())
        (*ann)["text"] = ov;
    s.revision += 1;
    return json{{"annotation", *ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_add_callout(const std::string& doc_id, const std::string& view_id,
                         const std::string& kind, const json& a, const json& opts) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    std::string units = doc_units(s.doc);
    double scale = view->value("scale", 1.0);
    if (scale == 0.0) scale = 1.0;
    const json& o = opts.is_object() ? opts : json::object();

    if (kind == "hole") {
        if (!is_xy(a))
            throw OpError("invalid_op", "hole callout needs a pick point a [x,y]",
                          {{"reason", "bad point"}});
        auto shapes = source_shapes(s);
        json geom = project_one(s, *view, shapes);
        // D-B2: a counterbore / chamfered / multi-step hole projects several concentric circles,
        // including a possible spurious near-zero-radius ring at the axis (chamfer-cone silhouette
        // / relief). Exclude rings under ~0.05 sheet mm so the real bore is picked, not the ~0 one
        // that made the callout read "⌀0". Fall back to the unfiltered pick only if that leaves
        // nothing (a genuinely tiny hole), so a legitimate small circle is never lost.
        const json* arc = nearest_arc(geom, a, 1, 0.05);  // closed circle, skip degenerate rings
        if (!arc) arc = nearest_arc(geom, a, 1);
        if (!arc)
            throw OpError("invalid_op", "hole callout: no projected circle near pick in this view",
                          {{"reason", "no circle"}});
        double cx = (*arc)["center"][0].get<double>(), cy = (*arc)["center"][1].get<double>();
        double r_sheet = arc->value("radius", 0.0);
        double r_model = r_sheet / scale;
        // D-B2 fallback chain: if the measured bore is still degenerate (no real circle resolved,
        // e.g. an odd projection of a complex hole), try these fallbacks in order rather than
        // emitting "⌀0":
        //   1. Explicit `diameter` opt (model mm) — works for holes where the caller passes it.
        //   2. `standard` opt (e.g. "M4", "M6") — parse the metric nominal diameter; handles M-
        //      standard mounting holes in thin ears where the bore rim is too thin to project.
        //   3. Suppress the callout entirely (return without adding an annotation) — "no callout"
        //      is ALWAYS better than a literal "⌀0" on a finished drawing.
        if (r_model <= 1e-6) {
            if (o.contains("diameter") && o["diameter"].is_number() &&
                o["diameter"].get<double>() > 0.0) {
                r_model = o["diameter"].get<double>() / 2.0;
                r_sheet = r_model * scale;
            } else if (o.contains("standard") && o["standard"].is_string()) {
                double std_dia = parse_metric_standard_diameter(o["standard"].get<std::string>());
                if (std_dia > 0.0) {
                    r_model = std_dia / 2.0;
                    r_sheet = r_model * scale;
                }
            }
            // If the diameter is STILL not positive after all fallbacks, suppress this callout —
            // never print "⌀0" on a drawing.
            if (r_model <= 1e-6)
                return json{{"suppressed", true},
                            {"reason", "could not resolve a positive hole diameter"}};
        }
        // Count same-diameter holes in this view -> "n× ⌀d" (SW hole-callout convention).
        // Dedupe by center so a through hole (whose top + bottom rims project to the SAME
        // circle) counts once, while a real pattern of N holes counts N.
        std::vector<std::array<double, 2>> hole_centers;
        if (geom.contains("arcs") && geom["arcs"].is_array())
            for (const json& ar : geom["arcs"]) {
                if (!ar.value("closed", false)) continue;
                if (std::fabs(ar.value("radius", 0.0) - r_sheet) >= 1e-4) continue;
                double hx = ar["center"][0].get<double>(), hy = ar["center"][1].get<double>();
                bool dup = false;
                for (auto& c : hole_centers)
                    if (std::hypot(c[0] - hx, c[1] - hy) < 1e-3) { dup = true; break; }
                if (!dup) hole_centers.push_back({hx, hy});
            }
        int count = (int)hole_centers.size();
        json pos = is_xy(o.value("pos", json(nullptr)))
                       ? json::array({o["pos"][0].get<double>(), o["pos"][1].get<double>()})
                       : json::array({cx + r_sheet + 14.0, cy + r_sheet + 14.0});
        json ann = {
            {"id", next_ann_id(*view)}, {"type", "note"}, {"kind", "hole_callout"},
            {"a", json::array({a[0].get<double>(), a[1].get<double>()})},
            {"b", json::array({cx, cy})}, {"center", json::array({cx, cy})},
            {"radius", round6(r_sheet)}, {"model_radius", round6(r_model)}, {"closed", true},
            {"value", round6(2.0 * r_model)}, {"count", count > 1 ? count : 0}, {"pos", pos},
        };
        if (o.contains("suffix") && o["suffix"].is_string()) ann["suffix"] = o["suffix"];
        if (o.contains("prefix") && o["prefix"].is_string()) ann["prefix"] = o["prefix"];
        // Optional hole-feature attributes (from the source model) drive the STANDARD stacked
        // callout (counterbore/countersink/tapped) in compose_disp — see its hole_type branch
        // (D4). Absent -> a plain "N× ⌀d" callout as before.
        if (o.contains("hole_type") && o["hole_type"].is_string())
            ann["hole_type"] = o["hole_type"];
        if (o.contains("thread") && o["thread"].is_string()) ann["thread"] = o["thread"];
        for (const char* k : {"cbore_dia", "cbore_depth", "csk_angle", "depth"})
            if (o.contains(k) && o[k].is_number()) ann[k] = o[k].get<double>();
        ann["text"] = compose_disp(ann, units);
        (*view)["annotations"].push_back(ann);
        s.revision += 1;
        return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
    }

    if (kind == "balloon") {
        int item = o.value("item", 0);
        json anchor = is_xy(a) ? json::array({a[0].get<double>(), a[1].get<double>()}) : json(nullptr);
        json pos = is_xy(o.value("pos", json(nullptr)))
                       ? json::array({o["pos"][0].get<double>(), o["pos"][1].get<double>()})
                       : (is_xy(a) ? json::array({a[0].get<double>() + 14.0, a[1].get<double>() + 14.0})
                                   : json::array({0.0, 0.0}));
        std::string txt = o.contains("text") && o["text"].is_string() && !o["text"].get<std::string>().empty()
                              ? o["text"].get<std::string>()
                              : (item > 0 ? std::to_string(item) : "");
        json ann = {
            {"id", next_ann_id(*view)}, {"type", "note"}, {"kind", "balloon"},
            {"a", anchor.is_null() ? pos : anchor}, {"pos", pos},
            {"value", (double)item}, {"item", item}, {"text", txt},
        };
        (*view)["annotations"].push_back(ann);
        s.revision += 1;
        return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
    }

    throw OpError("invalid_op", "callout kind must be hole|balloon", {{"reason", "bad kind"}});
}

// ----------------------------------------------------------- GD&T (ASME Y14.5)
namespace {
// The 14 geometric-characteristic symbols accepted in a feature control frame.
bool valid_gdt_symbol(const std::string& s) {
    static const std::set<std::string> k = {
        "straightness", "flatness",     "circularity",  "cylindricity",
        "profile_line", "profile_surface", "perpendicularity", "angularity",
        "parallelism",  "position",     "concentricity", "symmetry",
        "circular_runout", "total_runout"};
    return k.count(s) > 0;
}
// Short ASCII tag for a symbol — used for the human/AI/export fallback `text`.
std::string gdt_symbol_tag(const std::string& s) {
    static const std::map<std::string, std::string> t = {
        {"straightness", "STR"}, {"flatness", "FLAT"}, {"circularity", "CIRC"},
        {"cylindricity", "CYL"}, {"profile_line", "PROF-L"}, {"profile_surface", "PROF-S"},
        {"perpendicularity", "PERP"}, {"angularity", "ANG"}, {"parallelism", "PARA"},
        {"position", "POS"}, {"concentricity", "CONC"}, {"symmetry", "SYM"},
        {"circular_runout", "RUNOUT"}, {"total_runout", "T-RUNOUT"}};
    auto it = t.find(s);
    return it == t.end() ? s : it->second;
}
std::string mod_tag(const std::string& m) {
    if (m == "mmc") return "(M)";
    if (m == "lmc") return "(L)";
    if (m == "projected") return "(P)";
    if (m == "free") return "(F)";
    if (m == "tangent") return "(T)";
    return "";
}
// Build the readable fallback text for a feature control frame (the primitives carry the
// real symbology; this is for export ASCII / BOM / AI vision context).
std::string compose_gdt_text(const json& ann) {
    std::string out = gdt_symbol_tag(ann.value("symbol", std::string("position")));
    out += " ";
    if (ann.value("tol_diameter", false)) out += "DIA ";
    if (ann.contains("tol_value")) {
        if (ann["tol_value"].is_string()) out += ann["tol_value"].get<std::string>();
        else if (ann["tol_value"].is_number()) {
            char b[48];
            std::snprintf(b, sizeof(b), "%.4g", ann["tol_value"].get<double>());
            out += b;
        }
    }
    out += mod_tag(ann.value("tol_modifier", std::string("none")));
    if (ann.contains("datums") && ann["datums"].is_array())
        for (const auto& dd : ann["datums"])
            if (dd.is_object() && !dd.value("ref", std::string()).empty())
                out += " " + dd.value("ref", std::string()) + mod_tag(dd.value("modifier", std::string("none")));
    return out;
}
}  // namespace

json drawing_add_gdt(const std::string& doc_id, const std::string& view_id, const json& spec) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    const json& sp = spec.is_object() ? spec : json::object();
    std::string symbol = sp.value("symbol", std::string("position"));
    if (!valid_gdt_symbol(symbol))
        throw OpError("invalid_op",
                      "gdt symbol '" + symbol +
                          "' must be one of the 14 Y14.5 characteristics "
                          "(position, flatness, perpendicularity, profile_surface, ...)",
                      {{"reason", "bad symbol"}});
    json ann = {
        {"id", next_ann_id(*view)}, {"type", "gdt"}, {"kind", "gdt_frame"},
        {"symbol", symbol},
        {"tol_diameter", sp.value("tol_diameter", false)},
        {"tol_modifier", sp.value("tol_modifier", std::string("none"))},
        {"datums", sp.contains("datums") && sp["datums"].is_array() ? sp["datums"] : json::array()},
    };
    if (sp.contains("tol_value")) ann["tol_value"] = sp["tol_value"];
    if (sp.contains("row2") && sp["row2"].is_object()) ann["row2"] = sp["row2"];
    if (is_xy(sp.value("a", json(nullptr))))
        ann["a"] = json::array({sp["a"][0].get<double>(), sp["a"][1].get<double>()});
    if (is_xy(sp.value("pos", json(nullptr))))
        ann["pos"] = json::array({sp["pos"][0].get<double>(), sp["pos"][1].get<double>()});
    else if (ann.contains("a"))
        ann["pos"] = json::array({ann["a"][0].get<double>() + 16.0, ann["a"][1].get<double>() + 12.0});
    else
        ann["pos"] = json::array({20.0, 20.0});
    ann["text"] = compose_gdt_text(ann);
    (*view)["annotations"].push_back(ann);
    s.revision += 1;
    return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_add_datum(const std::string& doc_id, const std::string& view_id, const json& spec) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    const json& sp = spec.is_object() ? spec : json::object();
    std::string label = sp.value("label", std::string());
    if (label.empty()) label = "A";
    json ann = {{"id", next_ann_id(*view)}, {"type", "gdt"}, {"kind", "datum_feature"},
                {"label", label}, {"text", "DATUM " + label}};
    if (is_xy(sp.value("a", json(nullptr))))
        ann["a"] = json::array({sp["a"][0].get<double>(), sp["a"][1].get<double>()});
    if (is_xy(sp.value("pos", json(nullptr))))
        ann["pos"] = json::array({sp["pos"][0].get<double>(), sp["pos"][1].get<double>()});
    else if (ann.contains("a"))
        ann["pos"] = json::array({ann["a"][0].get<double>(), ann["a"][1].get<double>() - 12.0});
    else
        ann["pos"] = json::array({20.0, 20.0});
    (*view)["annotations"].push_back(ann);
    s.revision += 1;
    return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_add_datum_target(const std::string& doc_id, const std::string& view_id,
                              const json& spec) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    const json& sp = spec.is_object() ? spec : json::object();
    std::string label = sp.value("label", std::string("A1"));
    std::string ttype = sp.value("target_type", std::string("point"));
    json ann = {{"id", next_ann_id(*view)}, {"type", "gdt"}, {"kind", "datum_target"},
                {"label", label}, {"target_type", ttype}, {"text", "TGT " + label}};
    if (sp.contains("size") && sp["size"].is_string()) ann["size"] = sp["size"];
    if (is_xy(sp.value("a", json(nullptr))))
        ann["a"] = json::array({sp["a"][0].get<double>(), sp["a"][1].get<double>()});
    if (is_xy(sp.value("pos", json(nullptr))))
        ann["pos"] = json::array({sp["pos"][0].get<double>(), sp["pos"][1].get<double>()});
    else if (ann.contains("a"))
        ann["pos"] = json::array({ann["a"][0].get<double>() + 12.0, ann["a"][1].get<double>() + 12.0});
    else
        ann["pos"] = json::array({20.0, 20.0});
    (*view)["annotations"].push_back(ann);
    s.revision += 1;
    return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_add_surface_finish(const std::string& doc_id, const std::string& view_id,
                                const json& spec) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    const json& sp = spec.is_object() ? spec : json::object();
    std::string machining = sp.value("machining", std::string("any"));  // any|required|prohibited
    json ann = {{"id", next_ann_id(*view)}, {"type", "gdt"}, {"kind", "surface_finish"},
                {"machining", machining}, {"all_around", sp.value("all_around", false)}};
    std::string val = sp.value("value", std::string());
    if (val.empty() && sp.contains("ra") && sp["ra"].is_number()) {
        char b[32];
        std::snprintf(b, sizeof(b), "%.3g", sp["ra"].get<double>());
        val = b;
    }
    if (!val.empty()) ann["value"] = val;
    if (sp.contains("lay") && sp["lay"].is_string()) ann["lay"] = sp["lay"];
    ann["text"] = std::string("RA ") + (val.empty() ? "" : val);
    if (is_xy(sp.value("a", json(nullptr))))
        ann["a"] = json::array({sp["a"][0].get<double>(), sp["a"][1].get<double>()});
    if (is_xy(sp.value("pos", json(nullptr))))
        ann["pos"] = json::array({sp["pos"][0].get<double>(), sp["pos"][1].get<double>()});
    else if (ann.contains("a"))
        ann["pos"] = json::array({ann["a"][0].get<double>() + 8.0, ann["a"][1].get<double>() + 8.0});
    else
        ann["pos"] = json::array({20.0, 20.0});
    (*view)["annotations"].push_back(ann);
    s.revision += 1;
    return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_add_weld(const std::string& doc_id, const std::string& view_id, const json& spec) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    const json& sp = spec.is_object() ? spec : json::object();
    std::string wtype = sp.value("weld_type", std::string("fillet"));
    json ann = {{"id", next_ann_id(*view)}, {"type", "gdt"}, {"kind", "weld_symbol"},
                {"weld_type", wtype}, {"other_side", sp.value("other_side", false)},
                {"all_around", sp.value("all_around", false)}};
    if (sp.contains("size") && sp["size"].is_string()) ann["size"] = sp["size"];
    ann["text"] = std::string("WELD ") + wtype + " " + ann.value("size", std::string());
    if (is_xy(sp.value("a", json(nullptr))))
        ann["a"] = json::array({sp["a"][0].get<double>(), sp["a"][1].get<double>()});
    if (is_xy(sp.value("pos", json(nullptr))))
        ann["pos"] = json::array({sp["pos"][0].get<double>(), sp["pos"][1].get<double>()});
    else if (ann.contains("a"))
        ann["pos"] = json::array({ann["a"][0].get<double>() + 16.0, ann["a"][1].get<double>()});
    else
        ann["pos"] = json::array({20.0, 20.0});
    (*view)["annotations"].push_back(ann);
    s.revision += 1;
    return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_model_items(const std::string& doc_id, const std::string& view_id) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    std::string units = doc_units(s.doc);
    double scale = view->value("scale", 1.0);
    if (scale == 0.0) scale = 1.0;
    auto shapes = source_shapes(s);
    json geom = project_one(s, *view, shapes);
    if (!(*view).contains("annotations") || !(*view)["annotations"].is_array())
        (*view)["annotations"] = json::array();
    json& anns = (*view)["annotations"];
    int added = 0;

    // Overall envelope: width below + height to the left (SW "auto-dimension" baseline).
    if (geom.contains("bbox") && geom["bbox"].is_array() && geom["bbox"].size() == 4 &&
        geom["bbox"][0].is_number()) {
        double minx = geom["bbox"][0].get<double>(), miny = geom["bbox"][1].get<double>();
        double maxx = geom["bbox"][2].get<double>(), maxy = geom["bbox"][3].get<double>();
        double w = maxx - minx, h = maxy - miny;
        if (w > 1e-6) {
            json ann = {{"id", next_ann_id(*view)}, {"type", "linear_dim"}, {"auto", true},
                        {"a", json::array({minx, miny})}, {"b", json::array({maxx, miny})},
                        {"pos", json::array({(minx + maxx) / 2.0, miny - 12.0})},
                        {"value", round6(w / scale)}};
            ann["text"] = compose_disp(ann, units);
            anns.push_back(ann);
            ++added;
        }
        if (h > 1e-6) {
            json ann = {{"id", next_ann_id(*view)}, {"type", "linear_dim"}, {"auto", true},
                        {"a", json::array({minx, miny})}, {"b", json::array({minx, maxy})},
                        {"pos", json::array({minx - 12.0, (miny + maxy) / 2.0})},
                        {"value", round6(h / scale)}};
            ann["text"] = compose_disp(ann, units);
            anns.push_back(ann);
            ++added;
        }
    }

    // ONE diameter callout per DISTINCT diameter — identical holes collapse to a single
    // "N× ⌀d" callout instead of one "⌀d" per hole (D4). First dedupe coincident inner/outer
    // rims (a through hole projects its two rims to the same circle), then group the distinct
    // hole centers by radius: each radius-group yields one callout anchored at its first hole
    // with count = number of holes of that diameter (compose_disp prefixes "N× " when >1).
    std::vector<std::array<double, 3>> distinct;  // (cx,cy,r) of each distinct projected circle
    if (geom.contains("arcs") && geom["arcs"].is_array())
        for (const json& ar : geom["arcs"]) {
            if (!ar.value("closed", false)) continue;
            double cx = ar["center"][0].get<double>(), cy = ar["center"][1].get<double>();
            double r = ar.value("radius", 0.0);
            // D-B2: skip degenerate / spurious near-zero-radius circles (a chamfer-cone silhouette
            // or relief on a complex hole) so we don't emit a "⌀0" auto diameter callout for them.
            if (r < 0.05) continue;
            bool dup = false;
            for (auto& sd : distinct)
                if (std::hypot(sd[0] - cx, sd[1] - cy) < 1e-4 && std::fabs(sd[2] - r) < 1e-4) {
                    dup = true;
                    break;
                }
            if (!dup) distinct.push_back({cx, cy, r});
        }
    // Group by radius (preserve first-seen order for stable placement).
    struct DiaGroup { double cx, cy, r; int count; };
    std::vector<DiaGroup> groups;
    for (const auto& c : distinct) {
        bool merged = false;
        for (auto& g : groups)
            if (std::fabs(g.r - c[2]) < 1e-4) { ++g.count; merged = true; break; }
        if (!merged) groups.push_back({c[0], c[1], c[2], 1});
    }
    // D-B4: concentric bores (e.g. ⌀40/⌀30/⌀16 on an impeller hub) share a centre, so the default
    // leader pos (centre + r along the 45° diagonal) lands every callout on the same diagonal — they
    // pile up and become unreadable. Fan stacked callouts out: for each group, count how many EARLIER
    // groups share its centre and step that label down by a fixed pitch so the concentric set reads
    // as a vertical stack of distinct leaders instead of one overlapping blob.
    std::vector<std::array<double, 2>> placed_centers;  // centres already given a leader
    const double kStackPitch = 12.0;                    // sheet-mm vertical step between stacked labels
    for (const DiaGroup& g : groups) {
        int same_center = 0;
        for (const auto& pc : placed_centers)
            if (std::hypot(pc[0] - g.cx, pc[1] - g.cy) < 1e-3) ++same_center;
        placed_centers.push_back({g.cx, g.cy});
        double px = g.cx + g.r + 10.0;
        double py = g.cy + g.r + 10.0 - same_center * kStackPitch;  // step concentric labels down
        json ann = {{"id", next_ann_id(*view)}, {"type", "note"}, {"kind", "diameter_dim"},
                    {"auto", true}, {"a", json::array({g.cx, g.cy})},
                    {"b", json::array({g.cx, g.cy})}, {"center", json::array({g.cx, g.cy})},
                    {"radius", round6(g.r)}, {"model_radius", round6(g.r / scale)},
                    {"closed", true}, {"value", round6(2.0 * g.r / scale)},
                    {"count", g.count > 1 ? g.count : 0},
                    {"pos", json::array({px, py})}};
        ann["text"] = compose_disp(ann, units);
        anns.push_back(ann);
        ++added;
    }

    s.revision += 1;
    json out = drawing_get_locked_unsafe(doc_id);
    out["added"] = added;
    out["view_id"] = view_id;
    return out;
}

json drawing_set_titleblock(const std::string& doc_id, const std::string& sheet_id,
                            const json& fields) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json& sheet = find_sheet(s.doc, sheet_id);
    json tb = sheet.contains("title_block") && sheet["title_block"].is_object()
                  ? sheet["title_block"]
                  : json::object();
    if (fields.is_object())
        for (auto it = fields.begin(); it != fields.end(); ++it)
            if (it->is_string() || it->is_number() || it->is_boolean()) tb[it.key()] = it.value();
    sheet["title_block"] = tb;
    s.revision += 1;
    return json{{"doc_id", doc_id}, {"sheet_id", sheet.value("id", json(nullptr))},
                {"title_block", tb}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_set_sheet(const std::string& doc_id, const std::string& sheet_id,
                       const std::string& size) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json& sheet = find_sheet(s.doc, sheet_id);
    if (sheet_dims(size)[0] <= 0.0)
        throw OpError("invalid_op", "unknown sheet size '" + size + "'", {{"reason", "bad size"}});
    sheet["size"] = size;
    s.revision += 1;
    return json{{"doc_id", doc_id}, {"sheet_id", sheet.value("id", json(nullptr))},
                {"size", size}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_make(const std::string& source_doc_id, const json& spec) {
    // Orchestrates the public single-step RPCs (each takes g_mtx itself) — so we must NOT hold
    // the lock here. This is the copilot's high-level entry: one compact spec -> a full drawing.
    const json& s = spec.is_object() ? spec : json::object();
    std::string name = s.value("name", std::string());
    json created = drawing_new(json::object(), json::object(), name, source_doc_id);
    std::string doc_id = created.value("doc_id", std::string());
    if (doc_id.empty())
        throw OpError("invalid_op", "drawing_make: could not create drawing",
                      {{"reason", "no doc_id"}});

    if (s.contains("sheet") && s["sheet"].is_string())
        try { drawing_set_sheet(doc_id, "s1", s["sheet"].get<std::string>()); }
        catch (const OpError&) { /* bad size -> keep default A3 */ }

    // Requested views (default the SolidWorks standard set). Lay them out in a tidy grid so a
    // fresh drawing reads well before the user/agent nudges anything.
    std::vector<std::string> views;
    if (s.contains("views") && s["views"].is_array())
        for (const auto& v : s["views"])
            if (v.is_string()) views.push_back(v.get<std::string>());
    if (views.empty()) views = {"front", "top", "right", "iso"};

    // Relative grid cell (col,row) for each standard view — a SolidWorks 3rd-angle arrangement:
    // front centre, top above it, right beside it, iso upper-right. Cells are sized by the
    // (scaled) part footprint after auto-scale, so the absolute mm positions are computed below.
    struct Slot { const char* name; const char* type; int col, row; };
    static const Slot kSlots[] = {
        {"front", "base", 1, 1},      {"top", "projected", 1, 0},
        {"right", "projected", 2, 1}, {"bottom", "projected", 1, 2},
        {"left", "projected", 0, 1},  {"back", "projected", 2, 0},
        {"iso", "iso", 2, 0},
    };
    struct Placed { std::string vid; int col, row; };
    std::vector<Placed> placed;
    std::vector<std::string> ortho_view_ids;  // for model items / hole callouts (not the iso)
    bool used_base = false;
    for (const std::string& vn : views) {
        const Slot* slot = nullptr;
        for (const auto& k : kSlots)
            if (vn == k.name) { slot = &k; break; }
        if (!slot) continue;  // unknown view name -> skip (never throw)
        std::string type = slot->type;
        // The first orthographic view is the drawing's base; the rest project off it.
        if (type == "base") { if (used_base) type = "projected"; else used_base = true; }
        try {
            // Create at scale 1.0 first so we can read the part's true projected footprint, then
            // auto-scale + reposition all views below. Temporary position; overwritten by layout.
            json added = drawing_add_view(doc_id, type, vn, "s1", 1.0,
                                          json::array({0.0, 0.0}), json(nullptr));
            std::string vid = added.value("view_id", std::string());
            if (vid.empty()) continue;
            placed.push_back({vid, slot->col, slot->row});
            if (vn != "iso") ortho_view_ids.push_back(vid);
        } catch (const OpError&) { /* skip a view that fails to project */ }
    }

    // ---------------------------------------------------------------- D5: auto-scale + layout
    // Without this every view sat at a fixed 1:1, so a 1 mm part rendered as a few pixels and a
    // 5000 mm part overflowed the sheet. Now: measure the part's worst-case projected footprint
    // (from the scale-1.0 view bboxes we just produced), choose ONE standard drawing scale so the
    // scaled part fills ~65% of its grid cell, then place every view on a grid sized to the scaled
    // footprint (non-overlapping). The title-block SCALE text is set to match.
    double chosen_scale = 1.0;
    {
        json probe = drawing_get(doc_id);
        // Worst-case single-view model footprint (max width/height over the placed views). At
        // scale 1.0 the view bbox extent IS the part's projected model extent in mm.
        double fw = 0.0, fh = 0.0;
        // Per-view scale-1.0 projected bbox CENTRE (model mm). project_view lands geometry at
        // model_pt*scale + position, so a view's bbox centre on the sheet is
        // centre_model*chosen_scale + position. To put that centre at the cell centre (R1/R2)
        // we set position = cell_centre - centre_model*chosen_scale below. Without this a view
        // is offset by its own bbox centre: a short/asymmetric view (e.g. a 15 mm-tall plate
        // front view, or any body not modelled about the origin) sits at a cell EDGE instead of
        // the middle, reading as a thin strip crammed against one side with dead space opposite.
        std::map<std::string, std::array<double, 2>> view_center;  // vid -> (cx,cy) model mm
        std::map<std::string, std::array<double, 2>> view_half;    // vid -> (halfw,halfh) model mm
        if (probe.contains("sheets") && probe["sheets"].is_array())
            for (const auto& sh : probe["sheets"])
                for (const auto& v : sh.value("views", json::array())) {
                    const json& bb = v.value("bbox", json(nullptr));
                    if (bb.is_array() && bb.size() == 4 && bb[0].is_number()) {
                        fw = std::max(fw, bb[2].get<double>() - bb[0].get<double>());
                        fh = std::max(fh, bb[3].get<double>() - bb[1].get<double>());
                        std::string vid = v.value("view_id", v.value("id", std::string()));
                        if (!vid.empty()) {
                            view_center[vid] = {(bb[0].get<double>() + bb[2].get<double>()) / 2.0,
                                                (bb[1].get<double>() + bb[3].get<double>()) / 2.0};
                            view_half[vid] = {(bb[2].get<double>() - bb[0].get<double>()) / 2.0,
                                              (bb[3].get<double>() - bb[1].get<double>()) / 2.0};
                        }
                    }
                }
        // Sheet drawable area (the real sheet mm; default A3 if size_mm is missing).
        std::array<double, 2> sz = sheet_dims("A3");
        if (probe.contains("sheets") && probe["sheets"].is_array() && !probe["sheets"].empty()) {
            const json& mm = probe["sheets"][0].value("size_mm", json(nullptr));
            if (mm.is_array() && mm.size() == 2 && mm[0].is_number() && mm[0].get<double>() > 0.0)
                sz = {mm[0].get<double>(), mm[1].get<double>()};
        }
        const double margin = 20.0;          // paper border
        const double draw_w = std::max(40.0, sz[0] - 2 * margin);
        const double draw_h = std::max(40.0, sz[1] - 2 * margin);
        // Grid extent over the placed cells (so a single-view drawing uses the whole sheet, while
        // the 2x2 standard set splits it). +1 because cells are 0-based indices.
        int max_col = 0, max_row = 0;
        bool any_placed = !placed.empty();
        for (const auto& p : placed) { max_col = std::max(max_col, p.col); max_row = std::max(max_row, p.row); }
        const double cols = any_placed ? (double)(max_col + 1) : 1.0;
        const double rows = any_placed ? (double)(max_row + 1) : 1.0;
        const double gap = 12.0;             // gutter between view cells
        const double cell_w = std::max(20.0, (draw_w - (cols - 1) * gap) / cols);
        const double cell_h = std::max(20.0, (draw_h - (rows - 1) * gap) / rows);
        const double target_fill = 0.65;     // the part should fill ~65% of its cell
        if (fw > 1e-9 || fh > 1e-9) {
            double sx = (fw > 1e-9) ? target_fill * cell_w / fw : 1e30;
            double sy = (fh > 1e-9) ? target_fill * cell_h / fh : 1e30;
            double ideal = std::min(sx, sy);
            if (ideal > 1e-12 && std::isfinite(ideal)) chosen_scale = snap_scale_down(ideal);
        }
        // Re-place every view: assign its grid cell a mm centre and apply the chosen scale. The
        // cell pitch is (cell + gap); the grid is centred on the drawable area. Y grows UP on the
        // sheet, but rows count DOWN (row 0 = top), so flip the row when mapping to mm.
        const double pitch_x = cell_w + gap, pitch_y = cell_h + gap;
        const double grid_w = cols * cell_w + (cols - 1) * gap;
        const double grid_h = rows * cell_h + (rows - 1) * gap;
        const double x0 = margin + (draw_w - grid_w) / 2.0 + cell_w / 2.0;
        const double y_top = margin + (draw_h - grid_h) / 2.0 + grid_h - cell_h / 2.0;
        for (const auto& p : placed) {
            // Cell CENTRE on the sheet (mm).
            double cx_cell = x0 + p.col * pitch_x;
            double cy_cell = y_top - p.row * pitch_y;  // row 0 sits at the top
            // Centre the view's projected bbox in its cell: position offsets the geometry, and
            // geometry = model*chosen_scale + position, so to land the bbox centre on the cell
            // centre, position = cell_centre - centre_model*chosen_scale (R1/R2). Falls back to
            // the bare cell centre when we have no probe bbox for this view (degenerate/empty).
            double px = cx_cell, py = cy_cell;
            auto it = view_center.find(p.vid);
            if (it != view_center.end()) {
                px = cx_cell - it->second[0] * chosen_scale;
                py = cy_cell - it->second[1] * chosen_scale;
                // Keep the view's scaled projected bbox inside the sheet margins. Without this an
                // off-origin part (built away from the origin, e.g. a base + a tall wall whose Z
                // centre is well above 0) lands a view at negative / over-width sheet mm, drawn off
                // the paper edge. Leave ~26 mm extra on the LEFT for the model-items overall-height
                // dimension (placed at minx-12 plus its text/extension lines).
                auto hf = view_half.find(p.vid);
                if (hf != view_half.end()) {
                    const double hw = hf->second[0] * chosen_scale;
                    const double hh = hf->second[1] * chosen_scale;
                    const double lo_x = margin + hw + 26.0, hi_x = sz[0] - margin - hw;
                    const double lo_y = margin + hh,        hi_y = sz[1] - margin - hh;
                    if (hi_x > lo_x) px = std::min(std::max(px, lo_x), hi_x);
                    if (hi_y > lo_y) py = std::min(std::max(py, lo_y), hi_y);
                }
            }
            try {
                drawing_set_view(doc_id, p.vid, json(chosen_scale), json::array({px, py}),
                                 json(nullptr), json(nullptr));
            } catch (const OpError&) { /* keep the view at scale 1.0 if reposition fails */ }
        }
    }

    bool model_items = s.value("model_items", true);
    bool hole_callouts = s.value("hole_callouts", false);
    if (model_items)
        for (const std::string& vid : ortho_view_ids)
            try { drawing_model_items(doc_id, vid); } catch (const OpError&) {}

    if (hole_callouts) {
        // Re-fetch geometry, then drop ONE ⌀ callout per DISTINCT hole DIAMETER per view.
        // Identical holes (same diameter) must collapse to a single "N× ⌀d" callout with one
        // leader — NOT one callout each (D4). drawing_add_callout already counts every
        // same-diameter circle in the view and emits "N× ⌀d", so we anchor it on the FIRST
        // hole of each radius and dedupe by radius (not by center). Dedupe by center too so a
        // through hole (whose two rims project to coincident circles) seeds the radius once.
        //
        // D-B1 (no DOUBLE callout): when `model_items` ALSO ran above, it already emitted a
        // diameter_dim "N× ⌀d" per distinct hole diameter on every ortho view — anchored at the
        // SAME hole centre this pass would re-annotate. Adding a hole_callout for the same
        // diameter stacked a second identical "N× ⌀d" leader on top of the model-items one (the
        // duplicated pair). So when model_items owns the plain-hole diameter callouts, seed each
        // view's `radii_done` with the radii those auto diameter_dim annotations already cover,
        // making this pass a no-op for already-annotated diameters. With model_items OFF, the
        // seed is empty and this pass remains the sole hole-callout emitter (unchanged).
        json full = drawing_get(doc_id);
        if (full.contains("sheets") && full["sheets"].is_array())
            for (const auto& sh : full["sheets"])
                for (const auto& v : sh.value("views", json::array())) {
                    std::string vid = v.value("view_id", v.value("id", std::string()));
                    if (vid.empty() || v.value("type", std::string()) == "iso") continue;
                    std::vector<double> radii_done;  // one callout per distinct radius
                    std::vector<std::array<double, 2>> seen_centers;
                    // D-B1: pre-seed with radii model_items already called out on this view so we
                    // don't emit a second, identical hole_callout over the existing diameter_dim.
                    if (model_items)
                        for (const auto& an : v.value("annotations", json::array())) {
                            if (!an.value("auto", false)) continue;
                            std::string k = an.value("kind", an.value("type", std::string()));
                            if (k != "diameter_dim") continue;
                            double rr = an.value("radius", 0.0);
                            if (rr > 0.0) radii_done.push_back(rr);
                        }
                    for (const auto& ar : v.value("arcs", json::array())) {
                        if (!ar.value("closed", false)) continue;
                        double cx = ar["center"][0].get<double>(), cy = ar["center"][1].get<double>();
                        double r = ar.value("radius", 0.0);
                        bool dup_center = false;
                        for (auto& c : seen_centers)
                            if (std::hypot(c[0] - cx, c[1] - cy) < 1e-3) { dup_center = true; break; }
                        if (dup_center) continue;
                        seen_centers.push_back({cx, cy});
                        bool dup_radius = false;
                        for (double rd : radii_done)
                            if (std::fabs(rd - r) < 1e-3) { dup_radius = true; break; }
                        if (dup_radius) continue;  // a callout for this diameter already exists
                        radii_done.push_back(r);
                        try {
                            drawing_add_callout(doc_id, vid, "hole",
                                                json::array({cx, cy}), json::object());
                        } catch (const OpError&) {}
                    }
                }
    }

    // Auto-sections: each spec entry cuts a parent view through its center along an axis, so an
    // LLM can request a section WITHOUT knowing sheet-mm coordinates. Entry:
    //   {"parent":"front"|<view_id>, "axis":"vertical"|"horizontal", "mode":"full"|"half",
    //    "reverse":bool, "label":"A"}
    if (s.contains("sections") && s["sections"].is_array() && !s["sections"].empty()) {
        json full = drawing_get(doc_id);
        // Map orientation + view_id -> bbox for the current ortho views.
        auto find_parent = [&](const std::string& want, json& bbox, std::string& vid) -> bool {
            if (!full.contains("sheets") || !full["sheets"].is_array()) return false;
            for (const auto& sh : full["sheets"])
                for (const auto& v : sh.value("views", json::array())) {
                    if (v.value("type", std::string()) == "section" ||
                        v.value("type", std::string()) == "detail")
                        continue;
                    std::string id = v.value("view_id", v.value("id", std::string()));
                    std::string orient = v.value("orientation", std::string());
                    if (want == id || want == orient) {
                        if (v.contains("bbox") && v["bbox"].is_array() && v["bbox"].size() == 4 &&
                            v["bbox"][0].is_number()) {
                            bbox = v["bbox"];
                            vid = id;
                            return true;
                        }
                    }
                }
            return false;
        };
        for (const auto& sec : s["sections"]) {
            if (!sec.is_object()) continue;
            std::string parent = sec.value("parent", std::string("front"));
            std::string axis = sec.value("axis", std::string("vertical"));
            json bbox;
            std::string vid;
            if (!find_parent(parent, bbox, vid)) continue;
            double x0 = bbox[0].get<double>(), y0 = bbox[1].get<double>();
            double x1 = bbox[2].get<double>(), y1 = bbox[3].get<double>();
            double cx = (x0 + x1) / 2.0, cy = (y0 + y1) / 2.0;
            double pad = 0.1 * std::max(x1 - x0, y1 - y0) + 2.0;
            json cut = (axis == "horizontal")
                           ? json::array({json::array({x0 - pad, cy}), json::array({x1 + pad, cy})})
                           : json::array({json::array({cx, y0 - pad}), json::array({cx, y1 + pad})});
            json opts = {{"mode", sec.value("mode", std::string("full"))},
                         {"reverse", sec.value("reverse", false)}};
            try {
                drawing_add_section(doc_id, vid, json(nullptr), cut,
                                    sec.contains("label") ? sec["label"] : json(nullptr),
                                    json(nullptr), json(nullptr), "s1", json(false), opts);
            } catch (const OpError&) { /* skip a section that fails to cut */ }
        }
    }

    // Title block: merge the caller's fields with the auto-chosen drawing SCALE (D5). The scale
    // is no longer cosmetic — it reflects the actual view scale the auto-layout picked. An
    // explicit `scale` in the caller's title spec still wins (the user named the scale).
    json title = (s.contains("title") && s["title"].is_object()) ? s["title"] : json::object();
    if (!(title.contains("scale") && title["scale"].is_string() &&
          !title["scale"].get<std::string>().empty()))
        title["scale"] = scale_text(chosen_scale);
    try { drawing_set_titleblock(doc_id, "s1", title); } catch (const OpError&) {}

    return json{{"doc_id", doc_id}, {"drawing", drawing_get(doc_id)}};
}

bool drawing_close(const std::string& doc_id) {
    DrawingLock lk;
    return g_drawings.erase(doc_id) > 0;
}

// ----------------------------------------------------------------- crash recovery
// Drop an orphaned g_mtx lock left held by THIS thread after a Windows structured
// exception (access violation) unwound past a DrawingLock without running its C++
// destructor under /EHsc. Mirrors force_release_doc_lock in core/doc/session.cpp —
// same pattern, same safety contract, same idempotency guarantee.
// Called from the libaicad C dispatch boundary's __except filter alongside
// force_release_doc_lock so ANY hardware fault releases ALL orphaned registry locks.
bool force_release_drawing_lock() {
    if (!g_mtx_held.load(std::memory_order_acquire)) return false;
    if (g_mtx_owner != std::this_thread::get_id()) return false;
    g_mtx_held.store(false, std::memory_order_release);
    g_mtx.unlock();
    return true;
}

}  // namespace aicad::doc
