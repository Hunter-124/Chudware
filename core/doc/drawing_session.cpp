// Stateful 2D DrawingSession — see drawing_session.h. Port of the stateful half of
// engine/doc/drawing.py over the EXISTING HLR projection (doc/drawing.cpp).

#include "doc/drawing_session.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <cstdlib>
#include <ctime>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include <BRepAdaptor_Surface.hxx>
#include <BRepBndLib.hxx>
#include <Bnd_Box.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Standard_Failure.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <gp_Cylinder.hxx>

#include "doc/annot_draw.h"  // compile_annotation / compile_hatch (shared primitives)
#include "doc/assembly.h"  // compile_assembly / placed_world_shape (multi-body / asm sourcing)
#include "doc/document.h"
#include "doc/drawing.h"
#include "doc/session.h"  // OpError
#include "doc/stepplus.h"

using nlohmann::json;

namespace chudware::doc {

namespace {

#ifndef CHUDWARE_PI
#define CHUDWARE_PI 3.14159265358979323846
#endif

constexpr const char* kSchemaDrawing = "aicad.drawing/1";

// ISO 216 + ANSI Y14.1 sheet sizes in mm (drawing.py SHEET_DIMS_MM), landscape (w >= h).
// A trailing "P" selects the portrait orientation of the same paper ("A4P" = 210 x 297).
std::array<double, 2> sheet_dims(const std::string& size) {
    static const std::map<std::string, std::array<double, 2>> dims = {
        {"A4", {297.0, 210.0}},  {"A3", {420.0, 297.0}},  {"A2", {594.0, 420.0}},
        {"A1", {841.0, 594.0}},  {"A0", {1189.0, 841.0}}, {"A", {279.4, 215.9}},
        {"B", {431.8, 279.4}},   {"C", {558.8, 431.8}},   {"D", {863.6, 558.8}},
        {"E", {1117.6, 863.6}},
    };
    auto it = dims.find(size);
    if (it != dims.end()) return it->second;
    if (size.size() >= 2 && size.back() == 'P') {
        auto base = dims.find(size.substr(0, size.size() - 1));
        if (base != dims.end()) return {base->second[1], base->second[0]};
    }
    return {0.0, 0.0};
}

// Canonical sheet key: "a4 portrait" / "A4-Portrait" / "a4p" -> "A4P"; "A3 landscape" -> "A3".
std::string canon_sheet_size(const std::string& raw) {
    std::string s;
    for (char ch : raw)
        if (!std::isspace(static_cast<unsigned char>(ch)) && ch != '-' && ch != '_')
            s.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(ch))));
    auto strip = [&](const char* suffix, const char* repl) {
        const std::string suf = suffix;
        if (s.size() > suf.size() && s.compare(s.size() - suf.size(), suf.size(), suf) == 0)
            s = s.substr(0, s.size() - suf.size()) + repl;
    };
    strip("PORTRAIT", "P");
    strip("LANDSCAPE", "");
    return s;
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

struct ComponentShape {
    std::string id;
    std::string name;
    std::string source;
    TopoDS_Shape shape;
};
struct ComponentViewCache {
    long source_revision = -1;
    std::string orientation;
    double scale = 0.0;
    json local_components;
};


// ----------------------------------------------------------------- session store
struct DrawingSession {
    std::string doc_id;
    std::string source_ref_id;  // LIVE source DocSession id ("doc-cpp...") — empty = inline
    json doc;             // the aicad.drawing/1 document
    json source_doc;      // the source part tree (initial snapshot / inline fallback)
    json source_env;      // equation env for the source compile
    long revision = 0;
    long source_revision = 0;
    long saved_revision = -1, saved_source_revision = -1;
    // Per-view HLR is expensive: only Auto Balloon fills this cache. Geometry is
    // stored relative to the view origin so moving the view never invalidates it.
    std::map<std::string, ComponentViewCache> component_views;
    std::string standalone_path;
    std::map<std::string, std::string> standalone_extra;
    std::string source_state = "live";
    bool source_missing = false;
    bool owns_source = false;
    json cached_views;
    // Compiled source bodies keyed by the source revision, and orthographic/pictorial HLR
    // results (at the view origin) keyed by orientation/scale/hidden. A drawing_get, an
    // arrange or a title-block edit re-uses them, so only a source edit or a new scale pays
    // for the expensive hidden-line removal again.
    mutable std::string shapes_key;
    mutable std::vector<TopoDS_Shape> shapes_cache;
    mutable std::map<std::string, json> projection_cache;
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
        {"active_sheet", "s1"},
        {"sheets", json::array({json{{"id", "s1"}, {"name", "Sheet 1"}, {"size", "A3"},
                                      {"views", json::array()}, {"tables", json::array()}}})},
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
std::vector<TopoDS_Shape> source_shapes(const DrawingSession& s,
                                       std::vector<ComponentShape>* components = nullptr) {
    json tree = s.source_doc;
    json env = s.source_env;
    long rev = s.source_revision;
    std::string ref = "inline";
    std::string component_base;  // folder for relative component sources (live source only)
    if (!s.source_ref_id.empty()) {
        json live = doc_live_source(s.source_ref_id);
        if (live.is_object() && live.contains("tree")) {
            tree = live["tree"];
            env = live.value("env", json::object());
            rev = live.value("revision", 0L);
            ref = s.source_ref_id;
            if (live.contains("component_source_base") &&
                live["component_source_base"].is_string())
                component_base = live["component_source_base"].get<std::string>();
        }
    }
    const std::string key = ref + ":" + std::to_string(rev) + ":" +
                            std::to_string(std::hash<std::string>{}(tree.dump() + env.dump()));
    if (!components && key == s.shapes_key) return s.shapes_cache;
    std::vector<TopoDS_Shape> out;
    // Assembly source: resolve + mate-solve its components and return every world-placed
    // body so all of them superimpose in each view (one combined projection per orientation).
    if (tree.is_object() && tree.value("schema", std::string()) == "aicad.asm/1") {
        try {
            AssemblyResult ares = compile_assembly(tree, component_base);
            out.reserve(ares.bodies.size());
            std::map<std::string, std::string> sources;
            if (components) {
                components->clear();
                components->reserve(ares.bodies.size());
                for (const json& c : tree.value("components", json::array()))
                    sources[c.value("id", std::string())] =
                        c.value("source", json::object()).value("path", std::string());
            }
            for (const AsmBodyOut& b : ares.bodies) {
                TopoDS_Shape world = placed_world_shape(b);
                if (world.IsNull()) continue;
                out.push_back(world);
                if (components) {
                    const std::string seed = b.component_id.substr(0, b.component_id.find('#'));
                    components->push_back({b.component_id, b.name, sources[seed], world});
                }
            }
        } catch (...) { /* unresolved source projects as an empty view */ }
    } else {
        CompileResult res = compile_document(tree, env.is_null() ? json::object() : env);
        out = res.shapes;
    }
    if (key != s.shapes_key) {
        s.shapes_key = key;
        s.projection_cache.clear();
    }
    s.shapes_cache = out;
    return out;
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
    std::string wanted = sheet_id;
    if (wanted.empty()) wanted = doc.value("active_sheet", std::string());
    if (wanted.empty()) return sheets[0];
    for (json& sh : sheets)
        if (sh.value("id", std::string()) == wanted) return sh;
    throw OpError("invalid_op", "no sheet '" + wanted + "'", {{"reason", "unknown sheet"}});
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
std::string next_sheet_id(const json& doc) {
    int hi = 0;
    for (const json& sh : doc.value("sheets", json::array())) {
        const std::string id = sh.value("id", std::string());
        if (id.size() < 2 || id[0] != 's') continue;
        bool digits = true;
        for (std::size_t i = 1; i < id.size(); ++i)
            if (!std::isdigit(static_cast<unsigned char>(id[i]))) { digits = false; break; }
        if (digits) hi = std::max(hi, std::atoi(id.c_str() + 1));
    }
    return "s" + std::to_string(hi + 1);
}

std::string next_table_id(const json& doc) {
    int hi = 0;
    for (const json& sh : doc.value("sheets", json::array()))
        for (const json& table : sh.value("tables", json::array())) {
            const std::string id = table.value("id", std::string());
            if (id.size() < 2 || id[0] != 't') continue;
            bool digits = true;
            for (std::size_t i = 1; i < id.size(); ++i)
                if (!std::isdigit(static_cast<unsigned char>(id[i]))) { digits = false; break; }
            if (digits) hi = std::max(hi, std::atoi(id.c_str() + 1));
        }
    return "t" + std::to_string(hi + 1);
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
// orientation -> (view_dir, screen_x, screen_y) unit vectors: the line of sight (eye -> model)
// and the sheet +x/+y model axes of contracts/doc_schema.md §8 — exactly the frame
// drawing.cpp's projector draws in (sheet +y = screen_x x view_dir).
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
        screen_x = v_norm(v_cross(view_dir, {0, 0, 1}));
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
    sy = v_norm(v_cross(sx, d));
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
        if (view.contains("offset") && view["offset"].is_number() &&
            plane.contains("normal") && plane.contains("point") &&
            plane["normal"].is_array() && plane["normal"].size() == 3 &&
            plane["point"].is_array() && plane["point"].size() == 3) {
            Vec3 n = v_norm({plane["normal"][0].get<double>(),
                             plane["normal"][1].get<double>(),
                             plane["normal"][2].get<double>()});
            const double offset = view["offset"].get<double>();
            for (int i = 0; i < 3; ++i)
                plane["point"][i] = plane["point"][i].get<double>() + n[i] * offset;
            plane["offset"] = offset;
        }
        if (view.contains("hatch_style") && view["hatch_style"].is_object())
            plane["hatch_style"] = view["hatch_style"];
        if (view.contains("hatch_styles") && view["hatch_styles"].is_array())
            plane["hatch_styles"] = view["hatch_styles"];
        return plane;
    };
    if (view.contains("plane") && view["plane"].is_object() &&
        view["plane"].contains("normal") && view["plane"].contains("point")) {
        // Explicit world plane — pass through (project_section validates further).
        return attach_mode(view["plane"]);
    }
    const json& cut_line = view.contains("cut_line") ? view["cut_line"] : json(nullptr);
    // A straight cut may contain redundant collinear points. A genuinely jogged/offset
    // cut crosses multiple planes and is not representable by project_section's single
    // BRep cut plane, so reject it instead of silently projecting the dominant endpoint plane.
    if (!(cut_line.is_array() && cut_line.size() >= 2 && is_xy(cut_line[0]) &&
          is_xy(cut_line[cut_line.size() - 1])))
        throw OpError("invalid_op",
                      "section needs a world 'plane' {normal, point} or a 2D 'cut_line' "
                      "[[x,y],[x,y],...]",
                      {{"reason", "no plane"}});
    if (cut_line.size() > 2) {
        const double ax = cut_line[0][0].get<double>();
        const double ay = cut_line[0][1].get<double>();
        const double bx = cut_line[cut_line.size() - 1][0].get<double>();
        const double by = cut_line[cut_line.size() - 1][1].get<double>();
        const double dx = bx - ax, dy = by - ay;
        const double len = std::hypot(dx, dy);
        if (len < 1e-9)
            throw OpError("invalid_op", "section cut_line endpoints coincide",
                          {{"reason", "degenerate"}});
        for (std::size_t i = 1; i + 1 < cut_line.size(); ++i) {
            if (!is_xy(cut_line[i]))
                throw OpError("invalid_op", "section cut_line points must be [x,y]",
                              {{"reason", "bad cut_line"}});
            const double cross = std::fabs((cut_line[i][0].get<double>() - ax) * dy -
                                           (cut_line[i][1].get<double>() - ay) * dx);
            if (cross > 1e-6 * len)
                throw OpError(
                    "unsupported_offset_section",
                    "multi-plane offset section cut lines are not supported; supply one "
                    "straight cut line or an explicit world plane",
                    {{"reason", "multi_plane_cut_line"}, {"cut_line", cut_line}});
        }
    }
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
    // The section's eye looks along the cut-line arrows, which point to the sheet-LEFT of the
    // a->b direction (host drawing_panel endArrow). Sheet-left of `line` is line x d, and the
    // eye sits on the removed (+normal) side looking along -normal, so normal = d x line.
    Vec3 plane_normal = v_cross(d, line);
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
// the C++ core recomputes; the OUTPUT is identical, only timing differs.) `topology`
// (index_source_topology of src_shapes) adds model sids to every line's provenance; the
// internal pick resolvers that only read geometry pass none.
json project_one(DrawingSession& s, const json& view,
                 const std::vector<TopoDS_Shape>& src_shapes,
                 const std::vector<SourceTopology>* topology = nullptr) {
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
                    {"visible_src", json::array()}, {"hidden_src", json::array()},
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
            proj = project_section(src_shapes, plane, scale, pos, show_hidden, vid, topology);
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
                    {"visible_src", json::array()}, {"hidden_src", json::array()},
                    {"bbox", json(nullptr)}, {"ms", 0.0},
                    {"error", "detail view has no resolvable parent"}};
        } else {
            try {
                json parent_geom = project_one(s, *parent, src_shapes, topology);
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
        char key[160];
        std::snprintf(key, sizeof(key), "%s|%.12g|%d|%d", orient.c_str(), scale,
                      show_hidden ? 1 : 0, topology ? 1 : 0);
        auto hit = s.projection_cache.find(key);
        if (hit != s.projection_cache.end()) {
            proj = hit->second;
        } else {
            proj = project_view(src_shapes, orient, scale, {0.0, 0.0}, show_hidden, vid, topology);
            if (s.projection_cache.size() > 48) s.projection_cache.clear();
            const json err = proj.value("error", json());
            if (err.is_null() || (err.is_string() && err.get<std::string>().empty()))
                s.projection_cache[key] = proj;
        }
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
        for (const char* key : {"mode", "depth", "reverse", "offset",
                                "hatch_style", "hatch_styles"})
            if (view.contains(key)) out[key] = view[key];
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
    if (view.contains("caption")) out["caption"] = view["caption"];
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
                         {"reverse", g.value("reverse", false)},
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
    return std::acos(dot) * 180.0 / CHUDWARE_PI;
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

json live_source_tree(const DrawingSession& s) {
    if (!s.source_ref_id.empty()) {
        json live = doc_live_source(s.source_ref_id);
        if (live.is_object() && live.contains("tree")) return live["tree"];
    }
    return s.source_doc;
}

json bom_rows_locked(const DrawingSession& s) {
    const std::string src_id = s.source_ref_id;
    json comps = src_id.empty() ? json::array() : doc_asm_bom_components(src_id);
    if (src_id.empty() && live_source_tree(s).value("schema", std::string()) == "aicad.asm/1") {
        std::vector<ComponentShape> bodies;
        source_shapes(s, &bodies);
        std::map<std::string, int> counts;
        for (const auto& body : bodies)
            ++counts[body.id.substr(0, body.id.find('#'))];
        for (const auto& body : bodies) {
            if (body.id.find('#') != std::string::npos) continue;
            comps.push_back({{"component", body.id}, {"name", body.name},
                             {"source", body.source}, {"quantity", counts[body.id]}});
        }
    }
    if (!comps.is_array() || comps.empty()) return json::array();
    std::vector<std::string> order;
    std::map<std::string, json> groups;
    for (const json& c : comps) {
        std::string name = c.value("name", std::string());
        if (name.empty()) name = c.value("component", std::string("Part"));
        std::string source = c.value("source", std::string());
        std::string key = !source.empty() ? source : ("name:" + name);
        if (!groups.count(key)) {
            groups[key] = json{{"name", name}, {"source", source}, {"quantity", 0}};
            order.push_back(key);
        }
        groups[key]["quantity"] =
            groups[key]["quantity"].get<int>() + c.value("quantity", 1);
    }
    json rows = json::array();
    int item = 1;
    for (const std::string& key : order) {
        const json& g = groups[key];
        rows.push_back({{"item", item++}, {"name", g["name"]},
                        {"quantity", g["quantity"]}, {"source", g["source"]}});
    }
    return rows;
}

double scalar_number(const json& v, double fallback = 0.0) {
    if (v.is_number()) return v.get<double>();
    if (v.is_object() && v.contains("value") && v["value"].is_number())
        return v["value"].get<double>();
    return fallback;
}

std::string scalar_text(const json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_float()) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.6g", v.get<double>());
        return buf;
    }
    if (v.is_boolean()) return v.get<bool>() ? "true" : "false";
    if (v.is_object() && v.contains("value")) return scalar_text(v["value"]);
    return "";
}
std::string compose_chamfer(const json& ann, const std::string& units) {
    const json& length = ann.value("length", json(nullptr));
    const json& angle = ann.value("angle", json(nullptr));
    const int precision = ann.contains("precision") && ann["precision"].is_number_integer()
                              ? ann["precision"].get<int>() : -1;
    const std::string distance = length.is_number()
                                     ? fmt_len(length.get<double>(), units == "IPS", precision)
                                     : scalar_text(length);
    std::string text = distance + " \xC3\x97 " + scalar_text(angle) + "\xC2\xB0";
    if (ann.value("count", 0) > 1)
        text = std::to_string(ann.value("count", 0)) + "X " + text;
    return ann.value("prefix", std::string()) + text +
           ann.value("suffix", std::string());
}


void collect_hole_features(const json& node, json& rows) {
    if (node.is_array()) {
        for (const json& child : node) collect_hole_features(child, rows);
        return;
    }
    if (!node.is_object()) return;
    if (node.value("type", std::string()) == "hole") {
        const json& p = node.contains("params") && node["params"].is_object()
                            ? node["params"] : node;
        std::string style = p.value("style", std::string("simple"));
        std::string thread;
        for (const char* key : {"thread", "tap", "standard", "size"})
            if (p.contains(key) && p[key].is_string() && !p[key].get<std::string>().empty()) {
                thread = p[key].get<std::string>();
                break;
            }
        const bool tapped = style == "tap" || style == "tapped" ||
                            p.contains("tap") || p.value("tapped", false);
        int count = 1;
        for (const char* key : {"positions", "world_points"})
            if (p.contains(key) && p[key].is_array() && !p[key].empty()) {
                count = static_cast<int>(p[key].size());
                break;
            }
        double dia = 0.0;
        for (const char* key : {"diameter", "dia", "d", "hole_diameter"})
            if (p.contains(key)) {
                dia = scalar_number(p[key]);
                if (dia > 0.0) break;
            }
        double depth = p.contains("depth") ? scalar_number(p["depth"]) : 0.0;
        std::string description;
        if (tapped && !thread.empty()) description = thread + " TAPPED";
        else {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "DIA %.6g %s", dia,
                          style == "cbore" ? "CBORE" :
                          (style == "csink" || style == "countersink") ? "CSK" : "HOLE");
            description = buf;
        }
        if (p.value("end", std::string()) == "through_all") description += " THRU";
        rows.push_back({{"item", static_cast<int>(rows.size()) + 1},
                        {"feature", node.value("name", node.value("id", std::string("Hole")))},
                        {"quantity", count}, {"description", description},
                        {"diameter", dia}, {"depth", depth}, {"thread", thread},
                        {"hole_type", tapped ? "tapped" : style}});
    }
    for (auto it = node.begin(); it != node.end(); ++it)
        if (it.key() != "params") collect_hole_features(it.value(), rows);
}

json hole_rows_locked(const DrawingSession& s) {
    json rows = json::array();
    collect_hole_features(live_source_tree(s), rows);
    if (!rows.empty()) return rows;
    for (const json& sh : s.doc.value("sheets", json::array()))
        for (const json& view : sh.value("views", json::array()))
            for (const json& ann : view.value("annotations", json::array()))
                if (ann.value("kind", ann.value("type", std::string())) == "hole_callout")
                    rows.push_back({{"item", static_cast<int>(rows.size()) + 1},
                                    {"feature", ann.value("id", std::string("Hole"))},
                                    {"quantity", ann.value("count", 1)},
                                    {"description", ann.value("text", std::string())},
                                    {"diameter", ann.value("value", 0.0)},
                                    {"depth", ann.value("depth", 0.0)},
                                    {"thread", ann.value("thread", std::string())},
                                    {"hole_type", ann.value("hole_type", std::string("simple"))}});
    return rows;
}

json default_table_columns(const std::string& kind) {
    auto col = [](const char* key, const char* title, double width) {
        return json{{"key", key}, {"title", title}, {"width", width}};
    };
    if (kind == "bom")
        return json::array({col("item", "ITEM", 14), col("name", "PART", 70),
                            col("quantity", "QTY", 14), col("source", "SOURCE", 82)});
    if (kind == "holes")
        return json::array({col("item", "ID", 12), col("quantity", "QTY", 14),
                            col("description", "HOLE / THREAD", 64),
                            col("depth", "DEPTH", 22)});
    if (kind == "revision")
        return json::array({col("rev", "REV", 14), col("date", "DATE", 28),
                            col("description", "DESCRIPTION", 65),
                            col("approved_by", "APPROVED", 30)});
    if (kind == "cutlist")
        return json::array({col("item", "ITEM", 14), col("description", "MEMBER", 50),
                            col("quantity", "QTY", 14), col("length", "LENGTH", 24),
                            col("material", "MATERIAL", 36)});
    return json::array({col("note", "GENERAL NOTES", 125)});
}

json resolved_table(const DrawingSession& s, const json& stored) {
    json table = stored;
    const std::string kind = table.value("kind", std::string());
    if (!table.contains("columns") || !table["columns"].is_array() || table["columns"].empty())
        table["columns"] = default_table_columns(kind);
    if (kind == "bom") {
        if (!table.contains("rows") || !table["rows"].is_array() || table["rows"].empty())
            table["rows"] = bom_rows_locked(s);
    } else if (kind == "holes") {
        if (!table.contains("rows") || !table["rows"].is_array() || table["rows"].empty())
            table["rows"] = hole_rows_locked(s);
    } else if (!table.contains("rows") || !table["rows"].is_array()) {
        table["rows"] = json::array();
    }
    return table;
}

json compile_table_draw(const json& table) {
    const json columns = table.value("columns", json::array());
    const json rows = table.value("rows", json::array());
    std::array<double, 2> anchor = get_pos(table.value("anchor", json::array({25.0, 282.0})));
    const double row_h = table.value("row_height", 6.0);
    double total_w = 0.0;
    for (const json& c : columns) total_w += std::max(8.0, c.value("width", 30.0));
    const std::string title = table.value("title", std::string());
    const int title_rows = title.empty() ? 0 : 1;
    const int nrows = 1 + title_rows + static_cast<int>(rows.size());
    const double bottom = anchor[1] - nrows * row_h;
    json segs = json::array();
    json texts = json::array();
    auto line = [&](double x0, double y0, double x1, double y1) {
        segs.push_back(json::array({json::array({x0, y0}), json::array({x1, y1})}));
    };
    for (int r = 0; r <= nrows; ++r)
        line(anchor[0], anchor[1] - r * row_h, anchor[0] + total_w,
             anchor[1] - r * row_h);
    double x = anchor[0];
    line(x, bottom, x, anchor[1]);
    for (const json& c : columns) {
        x += std::max(8.0, c.value("width", 30.0));
        line(x, bottom, x, anchor[1] - title_rows * row_h);
    }
    if (!title.empty())
        texts.push_back({{"pos", json::array({anchor[0] + 2.0, anchor[1] - row_h + 1.5})},
                         {"text", title}, {"h", 3.0}});
    const double header_y = anchor[1] - (title_rows + 1) * row_h + 1.5;
    x = anchor[0];
    for (const json& c : columns) {
        texts.push_back({{"pos", json::array({x + 1.5, header_y})},
                         {"text", c.value("title", c.value("key", std::string()))}, {"h", 2.7}});
        x += std::max(8.0, c.value("width", 30.0));
    }
    for (std::size_t r = 0; r < rows.size(); ++r) {
        const json& row = rows[r];
        const double y = anchor[1] - (title_rows + 2 + static_cast<int>(r)) * row_h + 1.5;
        x = anchor[0];
        for (std::size_t ci = 0; ci < columns.size(); ++ci) {
            const json& c = columns[ci];
            std::string value;
            if (row.is_object()) {
                const std::string key = c.value("key", std::string());
                if (!key.empty() && row.contains(key)) value = scalar_text(row[key]);
            } else if (row.is_array() && ci < row.size()) value = scalar_text(row[ci]);
            else if (ci == 0) value = scalar_text(row);
            if (!value.empty())
                texts.push_back({{"pos", json::array({x + 1.5, y})},
                                 {"text", value}, {"h", 2.6}});
            x += std::max(8.0, c.value("width", 30.0));
        }
    }
    return json{{"segments", segs}, {"dashed", json::array()}, {"tris", json::array()},
                {"arcs", json::array()}, {"texts", texts}};
}

// Component-to-view mapping is computed against the same complete HLR projection.
// Associate each visible assembly component with its own projected outline. An
// individual HLR alone also exposes edges hidden by OTHER components: only keep an
// anchor when a point on that outline survives in the combined view's visible edges.
bool on_visible_edge(const json& visible, double x, double y) {
    for (const json& poly : visible) {
        if (!poly.is_array()) continue;
        for (std::size_t i = 1; i < poly.size(); ++i) {
            if (!is_xy(poly[i - 1]) || !is_xy(poly[i])) continue;
            const double ax = poly[i - 1][0].get<double>(), ay = poly[i - 1][1].get<double>();
            const double dx = poly[i][0].get<double>() - ax;
            const double dy = poly[i][1].get<double>() - ay;
            const double len2 = dx * dx + dy * dy;
            if (len2 < 1e-12) continue;
            const double t = std::clamp(((x - ax) * dx + (y - ay) * dy) / len2, 0.0, 1.0);
            if (std::hypot(x - ax - t * dx, y - ay - t * dy) < 0.05) return true;
        }
    }
    return false;
}

json projected_components(const json& view, const json& combined,
                          const std::vector<ComponentShape>& components) {
    json result = json::array();
    const std::string kind = view.value("type", std::string());
    if (kind == "section" || kind == "detail" || !combined.contains("visible")) return result;
    const auto pos = get_pos(view.value("position", json::array({0.0, 0.0})));
    const std::string orientation = view.value("orientation", std::string("front"));
    const double scale = view.value("scale", 1.0);
    for (const auto& component : components) {
        json projection = project_view({component.shape}, orientation, scale, pos, false,
                                       view.value("id", std::string()));
        const json& bbox = projection.value("bbox", json(nullptr));
        if (!bbox.is_array() || bbox.size() != 4) continue;
        json anchor;
        for (const json& poly : projection.value("visible", json::array())) {
            if (!poly.is_array()) continue;
            for (std::size_t i = 1; i < poly.size() && anchor.is_null(); ++i) {
                if (!is_xy(poly[i - 1]) || !is_xy(poly[i])) continue;
                const double ax = poly[i - 1][0].get<double>(), ay = poly[i - 1][1].get<double>();
                const double bx = poly[i][0].get<double>(), by = poly[i][1].get<double>();
                for (double t : {0.5, 0.25, 0.75}) {
                    const double x = ax + (bx - ax) * t, y = ay + (by - ay) * t;
                    if (on_visible_edge(combined["visible"], x, y)) {
                        anchor = json::array({x, y});
                        break;
                    }
                }
            }
            if (!anchor.is_null()) break;
        }
        if (!anchor.is_null())
            result.push_back({{"component", component.id}, {"name", component.name},
                              {"source", component.source}, {"bbox", bbox},
                              {"anchor", anchor}});
    }
    return result;
}

json drawing_get_locked_unsafe(const std::string& doc_id,
                               std::vector<ComponentShape>* components = nullptr) {
    DrawingSession& s = require_session(doc_id);
    if (s.source_missing) {
        json frozen = s.cached_views.is_object() ? s.cached_views : json::object();
        frozen["doc_id"] = doc_id;
        frozen["revision"] = s.revision;
        frozen["source_doc_id"] = json(nullptr);
        frozen["source_revision"] = s.source_revision;
        frozen["tree"] = s.doc;
        frozen["active_sheet_id"] =
            s.doc.value("active_sheet", std::string("s1"));
        frozen["source_state"] = s.source_state;
        frozen["source_missing"] = true;
        frozen["views_stale"] = true;
        frozen["warning"] = s.source_state == "closed"
                                ? "source closed — views frozen from last projection"
                                : "source missing — views frozen from last projection";
        if (!frozen.contains("sheets") || !frozen["sheets"].is_array())
            frozen["sheets"] = json::array();
        // Geometry stays frozen, but non-geometric drawing data remains editable:
        // overlay current title blocks and annotations onto the cached projection.
        const std::string units = doc_units(s.doc);
        const int sheet_count = static_cast<int>(frozen["sheets"].size());
        int sheet_index = 0;
        for (json& cached_sheet : frozen["sheets"]) {
            ++sheet_index;
            const std::string sheet_id =
                cached_sheet.value("id", std::string());
            const json* stored_sheet = nullptr;
            if (s.doc.contains("sheets") && s.doc["sheets"].is_array())
                for (const json& candidate : s.doc["sheets"])
                    if (candidate.value("id", std::string()) == sheet_id) {
                        stored_sheet = &candidate;
                        break;
                    }
            if (!stored_sheet) continue;
            cached_sheet["name"] = stored_sheet->value("name", json(nullptr));
            cached_sheet["size"] = stored_sheet->value("size", json(nullptr));
            const auto paper = sheet_dims(stored_sheet->value("size", std::string()));
            if (paper[0] > 0.0) cached_sheet["size_mm"] = json::array({paper[0], paper[1]});
            cached_sheet["title_block"] =
                stored_sheet->value("title_block", json::object());
            cached_sheet["frame"] =
                compile_sheet_frame(cached_sheet, units, sheet_index, sheet_count);
            if (!cached_sheet.contains("views") || !cached_sheet["views"].is_array())
                continue;
            for (json& cached_view : cached_sheet["views"]) {
                const std::string view_id =
                    cached_view.value("view_id", std::string());
                const json* stored_view = nullptr;
                if (stored_sheet->contains("views") &&
                    (*stored_sheet)["views"].is_array())
                    for (const json& candidate : (*stored_sheet)["views"])
                        if (candidate.value("id", std::string()) == view_id) {
                            stored_view = &candidate;
                            break;
                        }
                if (!stored_view) continue;
                cached_view["annotations"] =
                    stored_view->value("annotations", json::array());
                for (json& annotation : cached_view["annotations"])
                    if (annotation.is_object())
                        annotation["draw"] = compile_annotation(annotation, units);
            }
        }
        return frozen;
    }
    auto shapes = source_shapes(s, components);
    // Model identity for every projected line/arc, computed once for all views.
    const auto topology = index_source_topology(shapes);
    json sheets_out = json::array();
    double total_ms = 0.0;
    const int sheet_count =
        s.doc.contains("sheets") && s.doc["sheets"].is_array() ? (int)s.doc["sheets"].size() : 0;
    int sheet_index = 0;
    if (s.doc.contains("sheets") && s.doc["sheets"].is_array())
        for (const json& sh : s.doc["sheets"]) {
            ++sheet_index;
            json views_out = json::array();
            if (sh.contains("views") && sh["views"].is_array())
                for (const json& v : sh["views"]) {
                    json geom = project_one(s, v, shapes, &topology);
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
                g["draw"] = compile_view_decorations(g);
                if (g.contains("section_faces") && g["section_faces"].is_array()) {
                    json hatch = json::array();
                    if (g.contains("section_bodies") && g["section_bodies"].is_array()) {
                        for (const json& body : g["section_bodies"]) {
                            const json style = body.value("hatch_style", json::object());
                            for (const json& poly : body.value("faces", json::array())) {
                                json hr = compile_hatch(poly, style);
                                if (hr.contains("segments"))
                                    for (const json& segment : hr["segments"])
                                        hatch.push_back(segment);
                            }
                        }
                    } else {
                        for (const json& poly : g["section_faces"]) {
                            json hr = compile_hatch(poly, 3.0, 45.0);
                            if (hr.contains("segments"))
                                for (const json& segment : hr["segments"]) hatch.push_back(segment);
                        }
                    }
                    g["hatch"] = hatch;
                }
            }
            std::array<double, 2> sz = sheet_dims(sh.value("size", std::string()));
            json sh_out = {{"id", sh.value("id", json(nullptr))},
                           {"name", sh.value("name", json(nullptr))},
                           {"size", sh.value("size", json(nullptr))},
                           {"size_mm", json::array({sz[0], sz[1]})},
                           {"title_block", sh.value("title_block", json::object())}};
            sh_out["frame"] = compile_sheet_frame(sh_out, units, sheet_index, sheet_count);
            json tables_out = json::array();
            json overlay_annotations = json::array();
            for (const json& stored : sh.value("tables", json::array())) {
                json table = resolved_table(s, stored);
                table["draw"] = compile_table_draw(table);
                tables_out.push_back(table);
                overlay_annotations.push_back(
                    json{{"id", table.value("id", std::string())},
                         {"type", "table"}, {"kind", table.value("kind", std::string())},
                         {"draw", table["draw"]}});
            }
            if (!overlay_annotations.empty())
                views_out.push_back(
                    json{{"view_id", "__tables_" + sh.value("id", std::string())},
                         {"type", "table_overlay"}, {"orientation", "sheet"},
                         {"visible", json::array()}, {"hidden", json::array()},
                         {"annotations", overlay_annotations}, {"bbox", json(nullptr)}});
            sh_out["tables"] = tables_out;
            sh_out["views"] = views_out;
            sheets_out.push_back(std::move(sh_out));
        }
    return json{
        {"doc_id", doc_id}, {"revision", s.revision},
        {"source_doc_id", s.source_ref_id.empty() ? s.doc_id : s.source_ref_id},
        {"source_revision", s.source_revision}, {"tree", s.doc}, {"sheets", sheets_out},
        {"active_sheet_id", s.doc.value("active_sheet", std::string("s1"))},
        {"source_state", s.source_state},
        {"source_missing", false},
        {"views_stale", false},
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
    s.doc["units"]["system"] = doc_units(src);
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
    s.source_missing = false;
    s.source_state = "live";
    s.cached_views = json();
    s.component_views.clear();
    s.source_revision += 1;
    return json{{"doc_id", doc_id}, {"revision", s.revision}, {"source_revision", s.source_revision}};
}
json drawing_add_sheet(const std::string& doc_id, const std::string& size,
                       const std::string& name) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    const std::string paper = size.empty() ? "A3" : canon_sheet_size(size);
    if (sheet_dims(paper)[0] <= 0.0)
        throw OpError("invalid_op", "unknown sheet size '" + paper + "'",
                      {{"reason", "bad size"}});
    const std::string id = next_sheet_id(s.doc);
    const std::string sheet_name =
        name.empty() ? ("Sheet " + id.substr(1)) : name;
    json sheet = {{"id", id}, {"name", sheet_name}, {"size", paper},
                  {"views", json::array()}, {"tables", json::array()}};
    s.doc["sheets"].push_back(sheet);
    s.doc["active_sheet"] = id;
    ++s.revision;
    return json{{"doc_id", doc_id}, {"sheet_id", id}, {"sheet", sheet},
                {"active_sheet_id", id}, {"revision", s.revision}};
}

json drawing_list_sheets(const std::string& doc_id) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json sheets = json::array();
    for (const json& sh : s.doc.value("sheets", json::array()))
        sheets.push_back({{"id", sh.value("id", json(nullptr))},
                          {"name", sh.value("name", json(nullptr))},
                          {"size", sh.value("size", json(nullptr))},
                          {"view_count", sh.value("views", json::array()).size()},
                          {"table_count", sh.value("tables", json::array()).size()}});
    return json{{"doc_id", doc_id}, {"sheets", sheets},
                {"active_sheet_id", s.doc.value("active_sheet", std::string("s1"))},
                {"revision", s.revision}};
}

json drawing_set_active_sheet(const std::string& doc_id, const std::string& sheet_id) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json& sheet = find_sheet(s.doc, sheet_id);
    const std::string id = sheet.value("id", std::string());
    s.doc["active_sheet"] = id;
    ++s.revision;
    return json{{"doc_id", doc_id}, {"sheet_id", id}, {"sheet", sheet},
                {"active_sheet_id", id}, {"revision", s.revision}};
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
    const auto topology = index_source_topology(shapes);
    json geom = project_one(s, view, shapes, &topology);
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
        const double bottom_safe = std::min(ssz[1] - smarg, smarg + 55.0);
        if (def_x > ssz[0] - smarg) {  // overflows right edge -> place below the parent
            def_x = std::min(std::max(p_pos[0], smarg), ssz[0] - smarg);
            def_y = std::max(bottom_safe, p_pos[1] - 110.0);
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
        json cl = json::array();
        for (const auto& q : cut_line) {
            if (!is_xy(q))
                throw OpError("invalid_op", "section cut_line points must be [x,y]",
                              {{"reason", "bad cut_line"}});
            cl.push_back(json::array({q[0].get<double>(), q[1].get<double>()}));
        }
        // Validate now, before mutating the sheet. project_one will revalidate restored docs.
        if (cl.size() > 2) {
            const double ax = cl[0][0].get<double>(), ay = cl[0][1].get<double>();
            const double bx = cl.back()[0].get<double>(), by = cl.back()[1].get<double>();
            const double dx = bx - ax, dy = by - ay, len = std::hypot(dx, dy);
            if (len < 1e-9)
                throw OpError("invalid_op", "section cut_line endpoints coincide",
                              {{"reason", "degenerate"}});
            for (std::size_t i = 1; i + 1 < cl.size(); ++i)
                if (std::fabs((cl[i][0].get<double>() - ax) * dy -
                              (cl[i][1].get<double>() - ay) * dx) > 1e-6 * len)
                    throw OpError(
                        "unsupported_offset_section",
                        "multi-plane offset section cut lines are not supported; supply one "
                        "straight cut line or an explicit world plane",
                        {{"reason", "multi_plane_cut_line"}, {"cut_line", cl}});
        }
        view["cut_line"] = cl;
    }
    // Custom-section controls (optional, default a full straight section).
    if (opts.is_object()) {
        std::string mode = opts.value("mode", std::string());
        if (mode == "full" || mode == "half") view["mode"] = mode;
        if (opts.contains("depth") && opts["depth"].is_number() && opts["depth"].get<double>() > 0.0)
            view["depth"] = opts["depth"].get<double>();
        if (opts.contains("offset") && opts["offset"].is_number())
            view["offset"] = opts["offset"].get<double>();
        if (opts.contains("reverse") && opts["reverse"].is_boolean())
            view["reverse"] = opts["reverse"].get<bool>();
        if (opts.contains("hatch_style") && opts["hatch_style"].is_object())
            view["hatch_style"] = opts["hatch_style"];
        if (opts.contains("hatch_styles") && opts["hatch_styles"].is_array())
            view["hatch_styles"] = opts["hatch_styles"];
    }
    sheet["views"].push_back(view);
    s.revision += 1;
    auto shapes = source_shapes(s);
    const auto topology = index_source_topology(shapes);
    json geom = project_one(s, view, shapes, &topology);
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
    const auto topology = index_source_topology(shapes);
    json geom = project_one(s, view, shapes, &topology);
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
    s.component_views.erase(view_id);
    s.revision += 1;
    return json{{"removed", true}, {"revision", s.revision}};
}

// Annotation anchors and derived-view picks are stored in absolute sheet mm. Move them
// through the same model-space origin as the projected geometry, never through a
// translation alone (which leaves dimensions detached after a scale change).
void remap_sheet_point(json& point, const std::array<double, 2>& before,
                       const std::array<double, 2>& after, double ratio,
                       const std::array<double, 4>& axes) {
    if (!is_xy(point)) return;
    const double u = (point[0].get<double>() - before[0]) * ratio;
    const double v = (point[1].get<double>() - before[1]) * ratio;
    point[0] = after[0] + axes[0] * u + axes[1] * v;
    point[1] = after[1] + axes[2] * u + axes[3] * v;
}

void remap_annotation(json& ann, const std::array<double, 2>& before,
                      const std::array<double, 2>& after, double ratio,
                      const std::array<double, 4>& axes) {
    if (!ann.is_object()) return;
    for (const char* key : {"a", "b", "c", "d", "pos", "center", "vertex", "origin"})
        if (ann.contains(key)) remap_sheet_point(ann[key], before, after, ratio, axes);
    for (const char* key : {"line2", "points"})
        if (ann.contains(key) && ann[key].is_array())
            for (json& point : ann[key])
                remap_sheet_point(point, before, after, ratio, axes);
    if (ann.contains("radius") && ann["radius"].is_number())
        ann["radius"] = ann["radius"].get<double>() * ratio;
}

// Apply a scale / position / orientation / hidden-line change to one stored view and carry
// its annotations and its derived views' cut lines / detail circles along (no re-projection).
void apply_view_transform(DrawingSession& s, const std::string& view_id, json* view,
                          const json& scale, const json& position, const json& orientation,
                          const json& show_hidden) {
    const double old_scale = view->value("scale", 1.0);
    const std::array<double, 2> old_pos =
        get_pos(view->value("position", json::array({0.0, 0.0})));
    const std::string old_orient = view->value("orientation", std::string());
    if (scale.is_number()) {
        if (!std::isfinite(scale.get<double>()) || scale.get<double>() <= 0.0)
            throw OpError("invalid_op", "scale must be positive", {{"reason", "bad scale"}});
        (*view)["scale"] = scale.get<double>();
    }
    if (is_xy(position))
        (*view)["position"] = json::array({position[0].get<double>(), position[1].get<double>()});
    if (orientation.is_string() && view->value("type", std::string()) != "iso")
        (*view)["orientation"] = orientation.get<std::string>();
    if (show_hidden.is_boolean()) (*view)["show_hidden"] = show_hidden.get<bool>();
    const double new_scale = view->value("scale", 1.0);
    const std::array<double, 2> new_pos =
        get_pos(view->value("position", json::array({0.0, 0.0})));
    std::array<double, 4> axes{1.0, 0.0, 0.0, 1.0};
    const std::string new_orient = view->value("orientation", std::string());
    if (old_orient != new_orient &&
        (view->value("type", std::string()) == "base" ||
         view->value("type", std::string()) == "projected")) {
        Vec3 old_d, old_x, old_y, new_d, new_x, new_y;
        orient_frame(old_orient, old_d, old_x, old_y);
        orient_frame(new_orient, new_d, new_x, new_y);
        // Only opposite/same-plane views admit a lossless sheet-plane remap.
        // Orthogonal changes discard depth: sheet anchors cannot reconstruct 3D picks.
        if (std::fabs(v_dot(old_d, new_d)) > 1.0 - 1e-9)
            axes = {v_dot(new_x, old_x), v_dot(new_x, old_y),
                    v_dot(new_y, old_x), v_dot(new_y, old_y)};
    }
    if (old_scale > 0.0 &&
        (old_scale != new_scale || old_pos != new_pos ||
         axes != std::array<double, 4>{1.0, 0.0, 0.0, 1.0})) {
        const double ratio = new_scale / old_scale;
        if (view->contains("annotations") && (*view)["annotations"].is_array())
            for (json& ann : (*view)["annotations"])
                remap_annotation(ann, old_pos, new_pos, ratio, axes);
        for (json* child : all_views(s.doc)) {
            if (child->value("parent", std::string()) != view_id) continue;
            if (child->value("type", std::string()) == "section" &&
                child->contains("cut_line") && (*child)["cut_line"].is_array())
                for (json& point : (*child)["cut_line"])
                    remap_sheet_point(point, old_pos, new_pos, ratio, axes);
            if (child->value("type", std::string()) == "detail") {
                if (child->contains("center"))
                    remap_sheet_point((*child)["center"], old_pos, new_pos, ratio, axes);
                if (child->contains("radius") && (*child)["radius"].is_number())
                    (*child)["radius"] = (*child)["radius"].get<double>() * ratio;
            }
        }
    }
}

json drawing_set_view(const std::string& doc_id, const std::string& view_id, const json& scale,
                      const json& position, const json& orientation, const json& show_hidden) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    apply_view_transform(s, view_id, view, scale, position, orientation, show_hidden);
    s.revision += 1;
    auto shapes = source_shapes(s);
    const auto topology = index_source_topology(shapes);
    json geom = project_one(s, *view, shapes, &topology);
    if (geom.contains("annotations") && geom["annotations"].is_array())
        for (json& ann : geom["annotations"])
            if (ann.is_object()) ann["draw"] = compile_annotation(ann, doc_units(s.doc));
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
        if (text.is_string() && !text.get<std::string>().empty())
            ann["text_override"] = text;
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
    if (text.is_string() && !text.get<std::string>().empty())
        ann["text_override"] = text;
    (*view)["annotations"].push_back(ann);
    s.revision += 1;
    return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}
json drawing_add_chamfer_dimension(const std::string& doc_id,
                                   const std::string& view_id,
                                   const json& spec) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'",
                      {{"reason", "unknown view"}});
    const json a = spec.contains("a") ? spec["a"] : json();
    if (!is_xy(a))
        throw OpError("invalid_op", "chamfer dimension needs feature anchor a [x,y]",
                      {{"reason", "bad points"}});
    json pos = spec.contains("pos") ? spec["pos"] : json();
    if (!is_xy(pos))
        pos = json::array({a[0].get<double>() + 18.0, a[1].get<double>() + 12.0});
    json ann = {{"id", next_ann_id(*view)}, {"type", "note"},
                {"kind", "chamfer"},
                {"a", json::array({a[0].get<double>(), a[1].get<double>()})},
                {"pos", json::array({pos[0].get<double>(), pos[1].get<double>()})}};
    if (spec.contains("length")) ann["length"] = spec["length"];
    else if (spec.contains("distance")) ann["length"] = spec["distance"];
    else
        throw OpError("invalid_op", "chamfer dimension needs length",
                      {{"reason", "missing length"}});
    if (spec.contains("angle")) ann["angle"] = spec["angle"];
    else if (spec.contains("angle_deg")) ann["angle"] = spec["angle_deg"];
    else ann["angle"] = 45.0;
    for (const char* key : {"count", "precision", "prefix", "suffix"})
        if (spec.contains(key)) ann[key] = spec[key];
    if (spec.contains("text") && spec["text"].is_string() &&
        !spec["text"].get<std::string>().empty()) {
        ann["text"] = spec["text"];
        ann["text_override"] = spec["text"];
    } else {
        ann["text"] = compose_chamfer(ann, doc_units(s.doc));
    }
    (*view)["annotations"].push_back(ann);
    ++s.revision;
    return json{{"annotation", ann}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}

json drawing_add_ordinate_dimensions(const std::string& doc_id,
                                     const std::string& view_id,
                                     const json& spec) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'",
                      {{"reason", "unknown view"}});
    const json origin = spec.contains("origin") ? spec["origin"] : json();
    if (!is_xy(origin))
        throw OpError("invalid_op", "ordinate dimensions need origin [x,y]",
                      {{"reason", "bad origin"}});
    const std::string axis = spec.value("axis", std::string("x"));
    if (axis != "x" && axis != "y")
        throw OpError("invalid_op", "ordinate axis must be x|y",
                      {{"reason", "bad axis"}});
    json points = spec.contains("points") && spec["points"].is_array()
                      ? spec["points"] : json::array();
    if (points.empty() && spec.contains("a")) points.push_back(spec["a"]);
    if (points.empty())
        throw OpError("invalid_op", "ordinate dimensions need points[] or a",
                      {{"reason", "no points"}});
    double scale = view->value("scale", 1.0);
    if (scale == 0.0) scale = 1.0;
    const std::string units = doc_units(s.doc);
    json annotations = json::array();
    for (std::size_t i = 0; i < points.size(); ++i) {
        const json& entry = points[i];
        const json point = entry.is_object() && entry.contains("point")
                               ? entry["point"] : entry;
        if (!is_xy(point))
            throw OpError("invalid_op", "ordinate point must be [x,y]",
                          {{"reason", "bad points"}, {"index", i}});
        double value = (point[axis == "x" ? 0 : 1].get<double>() -
                        origin[axis == "x" ? 0 : 1].get<double>()) / scale;
        if (entry.is_object() && entry.contains("value") && entry["value"].is_number())
            value = entry["value"].get<double>();
        json pos = spec.contains("pos") ? spec["pos"] : json();
        if (entry.is_object() && entry.contains("pos")) pos = entry["pos"];
        if (!is_xy(pos))
            pos = axis == "x"
                      ? json::array({point[0].get<double>(),
                                     origin[1].get<double>() + 15.0})
                      : json::array({origin[0].get<double>() + 15.0,
                                     point[1].get<double>()});
        json ann = {{"id", next_ann_id(*view)}, {"type", "note"},
                    {"kind", "ordinate_dim"}, {"origin", origin},
                    {"a", json::array({point[0].get<double>(), point[1].get<double>()})},
                    {"axis", axis}, {"value", round6(value)}, {"pos", pos}};
        if (entry.is_object() && entry.contains("text")) ann["text"] = entry["text"];
        else if (spec.contains("text") && points.size() == 1) ann["text"] = spec["text"];
        else ann["text"] = format_dim_value(value, units, "linear_dim");
        if (ann.contains("text") &&
            ((entry.is_object() && entry.contains("text")) ||
             (spec.contains("text") && points.size() == 1)) &&
            ann["text"].is_string())
            ann["text_override"] = ann["text"];
        (*view)["annotations"].push_back(ann);
        annotations.push_back(ann);
    }
    ++s.revision;
    return json{{"annotations", annotations},
                {"drawing", drawing_get_locked_unsafe(doc_id)}};
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
    json rows = bom_rows_locked(s);
    const std::string src_id = s.source_ref_id.empty() ? doc_id : s.source_ref_id;
    json out = {{"rows", rows}, {"is_assembly", !rows.empty()},
                {"source_doc_id", src_id}};
    if (rows.empty())
        out["note"] = s.source_ref_id.empty() ? "inline-source drawing has no BOM"
                                               : "part drawings have no BOM";
    return out;
}

json drawing_auto_balloon(const std::string& doc_id, const std::string& view_id) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json live = s.source_ref_id.empty() ? json() : doc_live_source(s.source_ref_id);
    const json& tree = live.is_object() && live.contains("tree") ? live["tree"] : s.source_doc;
    if (!tree.is_object() || tree.value("schema", std::string()) != "aicad.asm/1")
        throw OpError("invalid_op", "auto balloon requires an assembly drawing",
                      {{"reason", "not_assembly"}});
    if (s.source_missing)
        throw OpError("invalid_op", "auto balloon requires an available assembly source",
                      {{"reason", "source_missing"}});
    const json rows = bom_rows_locked(s);
    if (rows.empty())
        throw OpError("invalid_op", "auto balloon: assembly has no BOM components",
                      {{"reason", "empty_bom"}});
    if (!view_id.empty() && !find_view(s.doc, view_id))
        throw OpError("invalid_op", "no view '" + view_id + "'",
                      {{"reason", "unknown view"}});

    const long source_revision = live.is_object()
                                     ? live.value("revision", s.source_revision)
                                     : s.source_revision;
    auto supported = [](const json& v) {
        const std::string type = v.value("type", std::string());
        return type == "base" || type == "projected" || type == "iso";
    };
    auto cache_matches = [&](const json& v) {
        const auto it = s.component_views.find(v.value("id", std::string()));
        return it != s.component_views.end() &&
               it->second.source_revision == source_revision &&
               it->second.orientation == v.value("orientation", std::string("front")) &&
               it->second.scale == v.value("scale", 1.0);
    };
    bool need_shapes = false;
    for (const json* v : all_views(s.doc))
        if (supported(*v) && (view_id.empty() || v->value("id", std::string()) == view_id) &&
            !cache_matches(*v)) {
            need_shapes = true;
            break;
        }

    // The ordinary drawing fetch projects only the combined assembly. Only a cache
    // miss on THIS command asks for the placed component bodies and their own HLR.
    std::vector<ComponentShape> shapes;
    json full = drawing_get_locked_unsafe(doc_id, need_shapes ? &shapes : nullptr);
    struct Candidate { const json* view; json components; };
    std::vector<Candidate> candidates;
    for (const json& sheet : full["sheets"])
        for (const json& projected : sheet["views"]) {
            const std::string id = projected.value("view_id", std::string());
            if (!view_id.empty() && id != view_id) continue;
            const json* stored = find_view_c(s.doc, id);
            if (!stored || !supported(*stored)) continue;
            const auto pos = get_pos(stored->value("position", json::array({0.0, 0.0})));
            if (!cache_matches(*stored)) {
                json local = projected_components(*stored, projected, shapes);
                for (json& c : local)
                    for (int axis = 0; axis < 2; ++axis) {
                        c["anchor"][axis] = c["anchor"][axis].get<double>() - pos[axis];
                        c["bbox"][axis] = c["bbox"][axis].get<double>() - pos[axis];
                        c["bbox"][axis + 2] = c["bbox"][axis + 2].get<double>() - pos[axis];
                    }
                s.component_views[id] = {source_revision,
                                         stored->value("orientation", std::string("front")),
                                         stored->value("scale", 1.0), std::move(local)};
            }
            json components = s.component_views.at(id).local_components;
            for (json& c : components)
                for (int axis = 0; axis < 2; ++axis) {
                    c["anchor"][axis] = c["anchor"][axis].get<double>() + pos[axis];
                    c["bbox"][axis] = c["bbox"][axis].get<double>() + pos[axis];
                    c["bbox"][axis + 2] = c["bbox"][axis + 2].get<double>() + pos[axis];
                }
            candidates.push_back({&projected, std::move(components)});
        }
    std::stable_sort(candidates.begin(), candidates.end(),
                     [&](const Candidate& lhs, const Candidate& rhs) {
        auto coverage = [&](const Candidate& candidate) {
            int count = 0;
            for (const json& row : rows) {
                const std::string source = row.value("source", std::string());
                const std::string name = row.value("name", std::string());
                for (const json& c : candidate.components)
                    if ((!source.empty() && c.value("source", std::string()) == source) ||
                        (source.empty() && c.value("source", std::string()).empty() &&
                         c.value("name", std::string()) == name)) {
                        ++count;
                        break;
                    }
            }
            return count;
        };
        return coverage(lhs) > coverage(rhs);
    });
    struct Choice {
        std::string view;
        std::string component;
        std::string source;
        json anchor;
        json component_bounds;
        json view_bounds;
        int item;
    };
    std::vector<Choice> choices;
    for (const json& row : rows) {
        const std::string source = row.value("source", std::string());
        const std::string name = row.value("name", std::string());
        bool found = false;
        for (const Candidate& candidate : candidates) {
            for (const json& c : candidate.components) {
                if ((!source.empty() && c.value("source", std::string()) != source) ||
                    (source.empty() && (c.value("source", std::string()) != "" ||
                                        c.value("name", std::string()) != name)))
                    continue;
                choices.push_back({candidate.view->at("view_id").get<std::string>(),
                                   c.value("component", std::string()), source, c["anchor"],
                                   c["bbox"], candidate.view->at("bbox"),
                                   row.at("item").get<int>()});
                found = true;
                break;
            }
            if (found) break;
        }
        if (!found)
            throw OpError("invalid_op",
                          "auto balloon: no visible component for BOM item " +
                              std::to_string(row.at("item").get<int>()),
                          {{"reason", "component_not_visible"}, {"item", row.at("item")}});
    }

    // Idempotent regeneration affects only our own annotations, never manually placed
    // balloons. All edits happen under one drawing lock and one revision increment.
    for (json* view : all_views(s.doc)) {
        json kept = json::array();
        for (const json& ann : view->value("annotations", json::array()))
            if (!(ann.value("kind", std::string()) == "balloon" &&
                  ann.value("auto", false)))
                kept.push_back(ann);
        (*view)["annotations"] = std::move(kept);
    }
    std::map<std::string, int> column;
    json used_views = json::array();
    json placements = json::array();
    for (const Choice& choice : choices) {
        json& view = *find_view(s.doc, choice.view);
        const int index = column[choice.view]++;
        if (index == 0) used_views.push_back(choice.view);
        const double x = choice.view_bounds[2].get<double>() + 18.0;
        const double y = choice.view_bounds[3].get<double>() - 6.0 - 12.0 * index;
        view["annotations"].push_back({
            {"id", next_ann_id(view)}, {"type", "note"}, {"kind", "balloon"},
            {"a", choice.anchor}, {"pos", json::array({x, y})},
            {"value", static_cast<double>(choice.item)}, {"item", choice.item},
            {"text", std::to_string(choice.item)}, {"auto", true},
        });
        placements.push_back({{"item", choice.item}, {"view_id", choice.view},
                              {"component", choice.component}, {"source", choice.source},
                              {"anchor", choice.anchor}, {"bbox", choice.component_bounds}});
    }
    ++s.revision;
    full["revision"] = s.revision;
    full["tree"] = s.doc;
    for (json& sheet : full["sheets"])
        for (json& projected : sheet["views"]) {
            const json* stored = find_view_c(s.doc, projected.value("view_id", std::string()));
            if (!stored) continue;
            projected["annotations"] = stored->value("annotations", json::array());
            for (json& ann : projected["annotations"])
                ann["draw"] = compile_annotation(ann, doc_units(s.doc));
        }
    return json{{"added", choices.size()}, {"view_ids", used_views},
                {"placements", std::move(placements)}, {"drawing", std::move(full)}};
}

bool dirty_drawing_count(std::uint64_t& count) {
    std::unique_lock<std::mutex> lock(g_mtx, std::try_to_lock);
    if (!lock.owns_lock()) return false;
    for (const auto& [id, session] : g_drawings)
        if (session.revision != session.saved_revision ||
            session.source_revision != session.saved_source_revision) ++count;
    return true;
}

void drawings_saved(const DrawingSaveRevisions& revisions) {
    DrawingLock lock;
    for (const auto& [id, revision] : revisions) {
        const auto found = g_drawings.find(id);
        if (found == g_drawings.end()) continue;
        auto& session = found->second;
        // An edit racing the source save remains dirty, not falsely certified.
        if (session.revision != revision.first || session.source_revision != revision.second) continue;
        session.saved_revision = revision.first;
        session.saved_source_revision = revision.second;
    }
}

std::map<std::string, std::string> drawings_for_source(
    const std::string& source_doc_id, DrawingSaveRevisions& revisions) {
    DrawingLock lk;
    std::map<std::string, std::string> out;
    for (const auto& [id, s] : g_drawings)
        if (s.source_ref_id == source_doc_id) {
            out["drawings/" + id + ".json"] = s.doc.dump();
            revisions[id] = {s.revision, s.source_revision};
        }
    return out;
}
// doc_close calls this only AFTER dropping both its registry and per-doc locks.
// The drawing lock must never be nested under either: drawing_get takes it before
// doc_live_source acquires those locks, and the opposite order would deadlock.
void drawings_source_closed(const std::string& source_doc_id, const json& source_doc,
                            const json& source_env, long source_revision) {
    DrawingLock lk;
    for (auto& [id, s] : g_drawings) {
        if (s.source_ref_id != source_doc_id || s.source_missing) continue;
        s.source_doc = source_doc;
        s.source_env = source_env;
        s.source_revision = source_revision;
        // The registry no longer exposes the source. Project its final captured tree
        // once, then freeze it as the existing stale-view path does for a missing file.
        try {
            s.cached_views = drawing_get_locked_unsafe(id);
        } catch (const std::exception&) {
            // Even an unprojectable final tree must be identified as stale, not silently
            // served from the drawing's creation-time snapshot.
            s.cached_views = json::object();
        }
        s.source_state = "closed";
        s.source_missing = true;
        ++s.revision;
    }
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
    if (!s.doc.contains("source") || !s.doc["source"].is_object())
        s.doc["source"] = source_doc_id;  // legacy embedded drawings store a string ref
    if (!s.doc.contains("active_sheet") || !s.doc["active_sheet"].is_string()) {
        const json& sheets = s.doc.value("sheets", json::array());
        s.doc["active_sheet"] = !sheets.empty() ? sheets[0].value("id", std::string("s1"))
                                                : "s1";
    }
    if (s.doc.contains("sheets") && s.doc["sheets"].is_array())
        for (json& sheet : s.doc["sheets"]) {
            if (!sheet.contains("tables") || !sheet["tables"].is_array())
                sheet["tables"] = json::array();
            if (!sheet.contains("name") || !sheet["name"].is_string())
                sheet["name"] = sheet.value("id", std::string("Sheet"));
        }
    s.source_doc = json::object();    // no inline snapshot — re-pull live from the source
    s.source_env = json::object();
    s.revision = 0;
    s.source_revision = 0;
    s.saved_revision = s.revision;
    s.saved_source_revision = s.source_revision;
    g_drawings[doc_id] = std::move(s);
    return json{{"doc_id", doc_id},
                {"name", g_drawings[doc_id].doc.value("name", std::string("Drawing"))},
                {"source_doc_id", source_doc_id}};
}

json drawing_save_standalone(const std::string& drawing_id, const std::string& path) {
    if (path.empty())
        throw OpError("io_error", "drawing_save_standalone: path is required",
                      {{"reason", "missing path"}});

    DrawingLock lk;
    DrawingSession& s = require_session(drawing_id);
    std::map<std::string, std::string> extra = s.standalone_extra;

    if (!s.source_missing) {
        json source = s.source_doc;
        json live;
        if (!s.source_ref_id.empty()) {
            live = doc_live_source(s.source_ref_id);
            if (live.is_object() && live.contains("tree")) source = live["tree"];
        }
        const std::string schema =
            source.is_object() ? source.value("schema", std::string()) : std::string();
        if (schema != "aicad.part/1" && schema != "aicad.asm/1")
            throw OpError("invalid_op",
                          "drawing_save_standalone: drawing has no serializable part/assembly source",
                          {{"drawing_id", drawing_id}, {"source_schema", schema}});

        json source_ref = {{"kind", "embedded"}, {"source_doc_schema", schema}};
        if (live.is_object() && live.contains("path") && live["path"].is_string()) {
            const std::string source_path = live["path"].get<std::string>();
            if (!source_path.empty()) {
                namespace fs = std::filesystem;
                std::error_code ec;
                fs::path rel = fs::relative(fs::path(source_path),
                                            fs::path(path).parent_path(), ec);
                source_ref["step_plus_rel_path"] =
                    (!ec && !rel.empty()) ? rel.generic_string() : source_path;
            }
        }
        s.doc["source"] = std::move(source_ref);
        s.source_doc = source;
        s.source_env = live.is_object() ? live.value("env", json::object()) : s.source_env;
        extra["source/document.json"] = editable_document_json(source);
    } else {
        extra.erase("source/document.json");
    }

    // Cache the complete assembled sheets, not just the stored view definitions. It is
    // display-only and explicitly marked stale if the source cannot be restored later.
    extra["cache/views.json"] = drawing_get_locked_unsafe(drawing_id).dump(2);
    const std::string written =
        editable_container_save(s.doc, path, extra, "aicad.draw+/1", "drawing", ".draw+");
    s.standalone_path = written;
    s.standalone_extra = std::move(extra);
    s.saved_revision = s.revision;
    s.saved_source_revision = s.source_revision;
    return json{{"drawing_id", drawing_id},
                {"doc_id", drawing_id},
                {"path", written},
                {"source_state", s.source_state}};
}

json drawing_open_standalone(const std::string& path) {
    StepPlusDoc loaded =
        editable_container_open(path, "aicad.draw+/", ".draw+");
    if (!loaded.doc.is_object() ||
        loaded.doc.value("schema", std::string()) != kSchemaDrawing)
        throw OpError("unsupported",
                      "drawing_open_standalone: expected an aicad.drawing/1 document",
                      {{"schema", loaded.doc.is_object()
                                      ? loaded.doc.value("schema", std::string())
                                      : std::string()},
                       {"reason", "unsupported document kind"}});

    const json source_ref =
        loaded.doc.contains("source") && loaded.doc["source"].is_object()
            ? loaded.doc["source"] : json::object();
    std::string source_path;
    if (source_ref.contains("step_plus_rel_path") &&
        source_ref["step_plus_rel_path"].is_string()) {
        namespace fs = std::filesystem;
        fs::path p(source_ref["step_plus_rel_path"].get<std::string>());
        if (p.is_relative()) p = fs::path(path).parent_path() / p;
        source_path = p.lexically_normal().string();
    }

    std::string source_doc_id;
    std::string source_state = "missing";
    if (!source_path.empty())
        source_doc_id = doc_find_open_by_path(source_path);
    if (!source_doc_id.empty()) {
        source_state = "live";
    } else {
        auto embedded = loaded.extra.find("source/document.json");
        if (embedded != loaded.extra.end()) {
            try {
                json source_doc = json::parse(embedded->second);
                json opened = doc_open_embedded(source_doc);
                source_doc_id = opened.value("doc_id", std::string());
                if (!source_doc_id.empty()) source_state = "embedded_opened";
            } catch (const std::exception&) {
                source_doc_id.clear();
            }
        }
        if (source_doc_id.empty() && !source_path.empty() &&
            std::filesystem::exists(source_path)) {
            try {
                json opened = doc_open(source_path, false);
                source_doc_id = opened.value("doc_id", std::string());
                if (!source_doc_id.empty()) source_state = "live";
            } catch (const std::exception&) {
                source_doc_id.clear();
            }
        }
    }

    json restored = register_restored_drawing(loaded.doc, source_doc_id);
    const std::string drawing_id = restored.value("doc_id", std::string());
    {
        DrawingLock lk;
        DrawingSession& s = require_session(drawing_id);
        s.standalone_path = path;
        s.standalone_extra = std::move(loaded.extra);
        s.source_state = source_state;
        s.source_missing = source_doc_id.empty();
        auto cached = s.standalone_extra.find("cache/views.json");
        if (cached != s.standalone_extra.end()) {
            try {
                s.cached_views = json::parse(cached->second);
            } catch (const std::exception&) {
                s.cached_views = json::object();
            }
        }
        s.owns_source = source_state == "embedded_opened";
    }
    return json{{"drawing_id", drawing_id},
                {"doc_id", drawing_id},
                {"name", restored.value("name", std::string("Drawing"))},
                {"source_doc_id", source_doc_id.empty() ? json(nullptr)
                                                         : json(source_doc_id)},
                {"source_state", source_state},
                {"path", path}};
}

// ----------------------------------------------------------- detailing (W17 overhaul)
// The numeric dimension kinds whose on-sheet text is composed from value + detailing
// fields (vs. notes / center marks / center lines, whose text is literal).
static bool is_numeric_dim(const std::string& type, const std::string& kind) {
    const std::string& dk = kind.empty() ? type : kind;
    return dk == "linear_dim" || dk == "aligned_dim" || dk == "radial_dim" ||
           dk == "diameter_dim" || dk == "angular_dim" || dk == "hole_callout" ||
           dk == "ordinate_dim";
}

json drawing_set_units(const std::string& doc_id, const std::string& system) {
    if (system != "MMGS" && system != "IPS")
        throw OpError("invalid_op", "drawing units must be MMGS|IPS",
                      {{"reason", "bad units"}, {"system", system}});
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    if (doc_units(s.doc) != system) {
        s.doc["units"]["system"] = system;
        for (json* view : all_views(s.doc)) {
            if (!view->contains("annotations") || !(*view)["annotations"].is_array())
                continue;
            for (json& ann : (*view)["annotations"]) {
                if (!ann.is_object()) continue;
                const std::string kind = ann.value("kind", std::string());
                const std::string override = ann.value("text_override", std::string());
                if (!override.empty()) continue;
                if (is_numeric_dim(ann.value("type", std::string()), kind))
                    ann["text"] = compose_disp(ann, system);
                else if (kind == "chamfer")
                    ann["text"] = compose_chamfer(ann, system);
            }
        }
        ++s.revision;
    }
    return json{{"doc_id", doc_id}, {"system", system},
                {"revision", s.revision}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
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
    if (sp.contains("size") &&
        (sp["size"].is_string() || sp["size"].is_number()))
        ann["size"] = scalar_text(sp["size"]);
    for (const char* key : {"pitch", "contour", "finish", "field_weld",
                            "tail_text", "full_penetration"})
        if (sp.contains(key) && (sp[key].is_string() || sp[key].is_number() ||
                                 sp[key].is_boolean()))
            ann[key] = sp[key];
    if (sp.contains("intermittent_length") &&
        (sp["intermittent_length"].is_string() ||
         sp["intermittent_length"].is_number())) {
        ann["intermittent_length"] = sp["intermittent_length"];
        ann["length"] = sp["intermittent_length"];
    } else if (sp.contains("length") &&
               (sp["length"].is_string() || sp["length"].is_number())) {
        ann["length"] = sp["length"];
        ann["intermittent_length"] = sp["length"];
    }
    ann["text"] = std::string("WELD ") + wtype + " " +
                  ann.value("size", std::string());
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

namespace {
// ----------------------------------------------------------- professional auto-dimensioning
// Shared state of one auto-dimension pass across the views of a sheet: which model axes
// already carry an overall dimension and which diameter groups are already called out, so
// a feature is dimensioned ONCE on the sheet (ISO 129-1: no redundant dimensions).
struct DimLedger {
    std::array<bool, 3> axes{false, false, false};
    std::set<std::string> diameters;  // "d|count" keys in model mm
};

int dominant_axis(const Vec3& v) {
    int best = 0;
    for (int i = 1; i < 3; ++i)
        if (std::fabs(v[i]) > std::fabs(v[best])) best = i;
    return best;
}

bool bbox4(const json& g, std::array<double, 4>& bb) {
    const json& b = g.value("bbox", json(nullptr));
    if (!(b.is_array() && b.size() == 4 && b[0].is_number())) return false;
    bb = {b[0].get<double>(), b[1].get<double>(), b[2].get<double>(), b[3].get<double>()};
    return bb[2] > bb[0] - 1e-9 && bb[3] > bb[1] - 1e-9;
}

struct Circ { double cx, cy, r; };

// Distinct closed circles of a projected view (coincident rims collapsed, slivers dropped).
std::vector<Circ> view_circles(const json& geom) {
    std::vector<Circ> out;
    for (const json& ar : geom.value("arcs", json::array())) {
        if (!ar.value("closed", false) || !ar.contains("center")) continue;
        const double cx = ar["center"][0].get<double>(), cy = ar["center"][1].get<double>();
        const double r = ar.value("radius", 0.0);
        // D-B2: skip degenerate near-zero rings (chamfer-cone silhouettes) — never "⌀0".
        if (r < 0.05) continue;
        bool dup = false;
        for (const Circ& c : out)
            if (std::hypot(c.cx - cx, c.cy - cy) < 1e-4 && std::fabs(c.r - r) < 1e-4) {
                dup = true;
                break;
            }
        if (!dup) out.push_back({cx, cy, r});
    }
    return out;
}

// {width, height} of a view spanned by a circle concentric with its outline (a round part
// seen along its axis): those extents are stated by the ⌀, not by linear dimensions.
std::array<bool, 2> round_outline(const json& geom) {
    std::array<bool, 2> out{false, false};
    std::array<double, 4> bb{};
    if (!bbox4(geom, bb)) return out;
    const double w = bb[2] - bb[0], h = bb[3] - bb[1];
    const double mx = (bb[0] + bb[2]) * 0.5, my = (bb[1] + bb[3]) * 0.5;
    const double tol = 0.004 * std::max(w, h) + 0.02;
    for (const Circ& c : view_circles(geom)) {
        if (std::fabs(2 * c.r - w) < tol && std::fabs(c.cx - mx) < tol) out[0] = true;
        if (std::fabs(2 * c.r - h) < tol && std::fabs(c.cy - my) < tol) out[1] = true;
    }
    return out;
}

// A world cylinder axis segment (holes, bosses, shafts) for centre lines in side views.
struct AxisSeg { Vec3 p0, p1; double r; };

std::vector<AxisSeg> cylinder_axes(const std::vector<TopoDS_Shape>& shapes) {
    std::vector<AxisSeg> out;
    for (const TopoDS_Shape& sh : shapes) {
        if (sh.IsNull()) continue;
        for (TopExp_Explorer ex(sh, TopAbs_FACE); ex.More(); ex.Next()) {
            try {
                BRepAdaptor_Surface surf(TopoDS::Face(ex.Current()));
                if (surf.GetType() != GeomAbs_Cylinder) continue;
                // Fillets / partial rounds (< 3/8 of a turn) carry no centre line.
                if (surf.LastUParameter() - surf.FirstUParameter() < CHUDWARE_PI * 0.75) continue;
                const gp_Cylinder cyl = surf.Cylinder();
                const gp_Pnt o = cyl.Axis().Location();
                const gp_Dir z = cyl.Axis().Direction();
                const double v0 = surf.FirstVParameter(), v1 = surf.LastVParameter();
                if (!std::isfinite(v0) || !std::isfinite(v1) || v1 - v0 < 1e-6) continue;
                out.push_back({{o.X() + z.X() * v0, o.Y() + z.Y() * v0, o.Z() + z.Z() * v0},
                               {o.X() + z.X() * v1, o.Y() + z.Y() * v1, o.Z() + z.Z() * v1},
                               cyl.Radius()});
            } catch (const Standard_Failure&) {
            }
        }
    }
    return out;
}

json new_ann(json& view, const char* type, const char* kind) {
    json ann = {{"id", next_ann_id(view)}, {"type", type}, {"auto", true}};
    if (kind) ann["kind"] = kind;
    return ann;
}

// Auto-dimension one stored view from its projected geometry. `ledger` (sheet-level pass)
// suppresses dimensions another view already carries, lets a round outline's diameter stand
// in for the overall width/height, and adds centre marks + bolt circles; nullptr = the
// single-view Model Items command (envelope + diameters only). `axes` (may be null) adds
// centre lines for cylinder axes lying in the view plane. `envelope_only` skips circles.
int add_model_items(json& view, const json& geom, const std::string& units, DimLedger* ledger,
                    const std::vector<AxisSeg>* axes, bool envelope_only = false) {
    double scale = view.value("scale", 1.0);
    if (scale == 0.0) scale = 1.0;
    if (!view.contains("annotations") || !view["annotations"].is_array())
        view["annotations"] = json::array();
    int added = 0;
    const std::string orient = view.value("orientation", std::string());
    Vec3 d{0, 1, 0}, sx{1, 0, 0}, sy{0, 0, 1};
    bool framed = false;
    try {
        orient_frame(orient, d, sx, sy);
        framed = true;
    } catch (const OpError&) {
    }
    const std::array<double, 2> pos = get_pos(view.value("position", json::array({0.0, 0.0})));
    const int ax_h = dominant_axis(sx), ax_v = dominant_axis(sy);

    std::array<double, 4> bb{};
    const bool has_bb = bbox4(geom, bb);
    const std::vector<Circ> circles = envelope_only ? std::vector<Circ>{} : view_circles(geom);

    // Round outline: a circle concentric with the view whose diameter spans the bbox stands in
    // for that overall dimension (the ⌀ callout says it once, a linear dim would repeat it).
    bool round_w = false, round_h = false;
    if (ledger && !envelope_only) {
        const auto r = round_outline(geom);
        round_w = r[0];
        round_h = r[1];
        if (round_w) ledger->axes[ax_h] = true;
        if (round_h) ledger->axes[ax_v] = true;
    }

    // Overall envelope: width below the view, height to its left (10 mm off the outline).
    if (has_bb) {
        const double minx = bb[0], miny = bb[1], maxx = bb[2], maxy = bb[3];
        const double w = maxx - minx, h = maxy - miny;
        const bool want_w = w > 1e-6 && !round_w && (!ledger || !framed || !ledger->axes[ax_h]);
        const bool want_h = h > 1e-6 && !round_h && (!ledger || !framed || !ledger->axes[ax_v]);
        if (want_w) {
            json ann = new_ann(view, "linear_dim", nullptr);
            ann["a"] = json::array({minx, miny});
            ann["b"] = json::array({maxx, miny});
            ann["pos"] = json::array({(minx + maxx) / 2.0, miny - 10.0});
            ann["value"] = round6(w / scale);
            ann["text"] = compose_disp(ann, units);
            view["annotations"].push_back(ann);
            ++added;
            if (ledger) ledger->axes[ax_h] = true;
        }
        if (want_h) {
            json ann = new_ann(view, "linear_dim", nullptr);
            ann["a"] = json::array({minx, miny});
            ann["b"] = json::array({minx, maxy});
            ann["pos"] = json::array({minx - 10.0, (miny + maxy) / 2.0});
            ann["value"] = round6(h / scale);
            ann["text"] = compose_disp(ann, units);
            view["annotations"].push_back(ann);
            ++added;
            if (ledger) ledger->axes[ax_v] = true;
        }
    }

    // Centres of the view's circles (largest radius per centre) — leaders fan out around them.
    struct Centre { double cx, cy, rmax; std::vector<double> used_angles; };
    std::vector<Centre> centres;
    auto centre_idx = [&](double cx, double cy) -> int {
        for (size_t i = 0; i < centres.size(); ++i)
            if (std::hypot(centres[i].cx - cx, centres[i].cy - cy) < 1e-3) return (int)i;
        return -1;
    };
    for (const Circ& c : circles) {
        const int hit = centre_idx(c.cx, c.cy);
        if (hit >= 0) centres[hit].rmax = std::max(centres[hit].rmax, c.r);
        else centres.push_back({c.cx, c.cy, c.r, {}});
    }
    if (ledger) {
        for (const Centre& c : centres) {
            json ann = new_ann(view, "note", "center_mark");
            ann["center"] = json::array({c.cx, c.cy});
            ann["a"] = json::array({c.cx, c.cy});
            ann["radius"] = round6(c.rmax);
            ann["value"] = 0.0;
            ann["text"] = "";
            view["annotations"].push_back(ann);
        }
    }

    // ONE diameter callout per DISTINCT diameter — identical holes collapse to a single
    // "N× ⌀d" callout (D4). Grouped by radius in first-seen order.
    struct DiaGroup { std::vector<Circ> members; double r; };
    std::vector<DiaGroup> groups;
    for (const Circ& c : circles) {
        bool merged = false;
        for (DiaGroup& g : groups)
            if (std::fabs(g.r - c.r) < 1e-4) {
                g.members.push_back(c);
                merged = true;
                break;
            }
        if (!merged) groups.push_back({{c}, c.r});
    }
    // D-B4: leader directions fan out around each centre so concentric callouts never stack.
    static const double kAngles[] = {45, 135, 25, 155, 65, 115, -35, -145, 5, 175};
    auto place_leader = [&](double cx, double cy, double reach) -> json {
        const int ci = centre_idx(cx, cy);
        double ang = 45.0;
        for (double cand : kAngles) {
            bool taken = false;
            if (ci >= 0)
                for (double u : centres[ci].used_angles)
                    if (std::fabs(u - cand) < 1.0) taken = true;
            if (!taken) { ang = cand; break; }
        }
        if (ci >= 0) centres[ci].used_angles.push_back(ang);
        const double rad = ang * CHUDWARE_PI / 180.0;
        const double dist = reach + 7.0;
        return json::array({cx + dist * std::cos(rad), cy + dist * std::sin(rad)});
    };
    for (const DiaGroup& g : groups) {
        const int count = (int)g.members.size();
        char key[64];
        std::snprintf(key, sizeof(key), "%.4f|%d", 2.0 * g.r / scale, count);
        if (ledger && !ledger->diameters.insert(key).second) continue;
        // Anchor a pattern's leader on its upper-right-most hole.
        Circ anchor = g.members.front();
        for (const Circ& m : g.members)
            if (m.cx + m.cy > anchor.cx + anchor.cy) anchor = m;
        const int ci = centre_idx(anchor.cx, anchor.cy);
        const double reach = ci >= 0 ? centres[ci].rmax : anchor.r;
        json ann = new_ann(view, "note", "diameter_dim");
        ann["a"] = json::array({anchor.cx, anchor.cy});
        ann["b"] = json::array({anchor.cx, anchor.cy});
        ann["center"] = json::array({anchor.cx, anchor.cy});
        ann["radius"] = round6(anchor.r);
        ann["model_radius"] = round6(anchor.r / scale);
        ann["closed"] = true;
        ann["value"] = round6(2.0 * anchor.r / scale);
        ann["count"] = count > 1 ? count : 0;
        ann["pos"] = place_leader(anchor.cx, anchor.cy, reach);
        ann["text"] = compose_disp(ann, units);
        view["annotations"].push_back(ann);
        ++added;

        // Bolt circle: >= 3 equal holes equidistant from a common centre -> a chain pitch
        // circle through the hole centres plus its diameter.
        if (ledger && count >= 3) {
            double px = 0, py = 0;
            for (const Circ& m : g.members) { px += m.cx; py += m.cy; }
            px /= count;
            py /= count;
            const double R = std::hypot(g.members[0].cx - px, g.members[0].cy - py);
            bool equal = R > 1e-3;
            for (const Circ& m : g.members)
                if (std::fabs(std::hypot(m.cx - px, m.cy - py) - R) > 0.002 * R + 1e-4)
                    equal = false;
            char pkey[64];
            std::snprintf(pkey, sizeof(pkey), "pcd|%.4f", 2.0 * R / scale);
            if (equal && ledger->diameters.insert(pkey).second) {
                json pc = new_ann(view, "note", "pitch_circle");
                pc["center"] = json::array({px, py});
                pc["a"] = json::array({px, py});
                pc["radius"] = round6(R);
                pc["value"] = 0.0;
                pc["text"] = "";
                view["annotations"].push_back(pc);
                if (centre_idx(px, py) < 0) centres.push_back({px, py, R, {}});
                const int pci = centre_idx(px, py);
                json pd = new_ann(view, "note", "diameter_dim");
                pd["a"] = json::array({px + R, py});
                pd["b"] = json::array({px, py});
                pd["center"] = json::array({px, py});
                pd["radius"] = round6(R);
                pd["model_radius"] = round6(R / scale);
                pd["closed"] = true;
                pd["value"] = round6(2.0 * R / scale);
                pd["count"] = 0;
                pd["pos"] = place_leader(px, py, std::max(R, centres[pci].rmax));
                pd["text"] = compose_disp(pd, units);
                view["annotations"].push_back(pd);
                ++added;
            }
        }
    }

    // Centre lines on cylinder axes lying in the view plane (holes and bosses seen side-on).
    if (axes && framed && orient != "iso") {
        struct Line { double nx, ny, off, t0, t1; };
        std::vector<Line> lines;
        auto same_line = [](const Line& A, const Line& B) {
            return std::fabs(A.nx - B.nx) < 1e-6 && std::fabs(A.ny - B.ny) < 1e-6 &&
                   std::fabs(A.off - B.off) < 0.05 && B.t0 <= A.t1 + 0.5 && B.t1 >= A.t0 - 0.5;
        };
        for (const AxisSeg& a : *axes) {
            const Vec3 dir = v_norm({a.p1[0] - a.p0[0], a.p1[1] - a.p0[1], a.p1[2] - a.p0[2]});
            if (std::fabs(v_dot(dir, d)) > 1e-6) continue;  // not side-on in this view
            if (a.r * scale < 0.4) continue;
            const double x0 = v_dot(a.p0, sx) * scale + pos[0], y0 = v_dot(a.p0, sy) * scale + pos[1];
            const double x1 = v_dot(a.p1, sx) * scale + pos[0], y1 = v_dot(a.p1, sy) * scale + pos[1];
            double ux = x1 - x0, uy = y1 - y0;
            const double l = std::hypot(ux, uy);
            if (l < 1.0) continue;
            ux /= l;
            uy /= l;
            if (ux < -1e-9 || (std::fabs(ux) <= 1e-9 && uy < 0)) { ux = -ux; uy = -uy; }
            const double nx = -uy, ny = ux;
            double t0 = x0 * ux + y0 * uy, t1 = x1 * ux + y1 * uy;
            if (t0 > t1) std::swap(t0, t1);
            Line cand{nx, ny, x0 * nx + y0 * ny, t0, t1};
            bool merged = false;
            for (Line& L : lines)
                if (same_line(L, cand)) {
                    L.t0 = std::min(L.t0, cand.t0);
                    L.t1 = std::max(L.t1, cand.t1);
                    merged = true;
                    break;
                }
            if (!merged) lines.push_back(cand);
        }
        // Intervals widened by later segments may now overlap — merge to a fixed point.
        for (size_t i = 0; i < lines.size(); ++i)
            for (size_t j = i + 1; j < lines.size();) {
                if (same_line(lines[i], lines[j])) {
                    lines[i].t0 = std::min(lines[i].t0, lines[j].t0);
                    lines[i].t1 = std::max(lines[i].t1, lines[j].t1);
                    lines.erase(lines.begin() + (long)j);
                    j = i + 1;
                } else {
                    ++j;
                }
            }
        std::sort(lines.begin(), lines.end(),
                  [](const Line& a, const Line& b) { return a.t1 - a.t0 > b.t1 - b.t0; });
        if (lines.size() > 40) lines.resize(40);
        for (const Line& L : lines) {
            const double ux = L.ny, uy = -L.nx;
            const double bx = L.nx * L.off, by = L.ny * L.off;
            json ann = new_ann(view, "note", "center_line");
            ann["a"] = json::array({bx + ux * L.t0, by + uy * L.t0});
            ann["b"] = json::array({bx + ux * L.t1, by + uy * L.t1});
            ann["pos"] = json::array({bx + ux * (L.t0 + L.t1) * 0.5, by + uy * (L.t0 + L.t1) * 0.5});
            ann["value"] = 0.0;
            ann["text"] = "";
            view["annotations"].push_back(ann);
        }
    }
    return added;
}
}  // namespace

json drawing_model_items(const std::string& doc_id, const std::string& view_id) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json* view = find_view(s.doc, view_id);
    if (!view)
        throw OpError("invalid_op", "no view '" + view_id + "'", {{"reason", "unknown view"}});
    std::string units = doc_units(s.doc);
    auto shapes = source_shapes(s);
    json geom = project_one(s, *view, shapes);
    const int added = add_model_items(*view, geom, units, nullptr, nullptr);
    s.revision += 1;
    json out = drawing_get_locked_unsafe(doc_id);
    out["added"] = added;
    out["view_id"] = view_id;
    return out;
}

namespace {
// ----------------------------------------------------------- professional auto layout
// Standard-view lattice (col, row; row 0 at the top). Third-angle (ASME): top above front,
// right to its right. First-angle (ISO E): top below front, right view to its left.
bool standard_slot(const std::string& orient, bool first_angle, int& col, int& row) {
    struct S { const char* name; int col, row; };
    static const S third[] = {{"front", 1, 1}, {"top", 1, 0}, {"bottom", 1, 2},
                              {"left", 0, 1},  {"right", 2, 1}, {"back", 3, 1}};
    static const S first[] = {{"front", 1, 1}, {"top", 1, 2}, {"bottom", 1, 0},
                              {"left", 2, 1},  {"right", 0, 1}, {"back", 3, 1}};
    for (const S& s : first_angle ? first : third)
        if (orient == s.name) { col = s.col; row = s.row; return true; }
    return false;
}

bool is_first_angle(const std::string& projection) {
    std::string p;
    for (char c : projection) p.push_back((char)std::tolower((unsigned char)c));
    return p.find("first") != std::string::npos || p.find("1st") != std::string::npos;
}

// Re-lay every view of one sheet: ONE standard scale for the whole sheet (largest that fits the
// drawing area left free by the title block and tables), views on the projection lattice with
// shared centre lines (top/front share x, front/right share y), derived views (sections,
// details) in a column of their own, everything centred in the free area with room for the
// dimensions between views. Annotations and derived cuts move with their views. Returns the
// chosen {scale, projection}. Caller holds the lock.
json arrange_sheet_locked(DrawingSession& s, const std::string& doc_id, const std::string& sheet_id,
                          const std::string& projection_in, double fixed_scale) {
    json& sheet = find_sheet(s.doc, sheet_id);
    const std::string sid = sheet.value("id", std::string());
    json tb = sheet.contains("title_block") && sheet["title_block"].is_object()
                  ? sheet["title_block"] : json::object();
    std::string projection = projection_in;
    if (projection.empty()) projection = tb.value("projection", std::string("third"));
    const bool first_angle = is_first_angle(projection);
    projection = first_angle ? "first" : "third";

    const json full = drawing_get_locked_unsafe(doc_id);
    std::map<std::string, json> geom_by_id;
    for (const json& sh : full.value("sheets", json::array()))
        if (sh.value("id", std::string()) == sid)
            for (const json& g : sh.value("views", json::array()))
                geom_by_id[g.value("view_id", std::string())] = g;

    struct Item {
        std::string vid;
        std::string type;
        std::string parent;
        double old_scale = 1.0, ratio = 1.0;
        std::array<double, 2> old_pos{};
        std::array<double, 4> mb{};  // bbox about the view origin at scale 1
        bool has_bb = false;
        int col = 0, row = 0;
        bool placed = false;
    };
    std::vector<Item> items;
    std::map<std::string, double> scale_of;
    for (json& v : sheet["views"]) {
        Item it;
        it.vid = v.value("id", std::string());
        it.type = v.value("type", std::string());
        it.parent = v.value("parent", std::string());
        it.old_scale = v.value("scale", 1.0);
        if (!(it.old_scale > 0.0)) it.old_scale = 1.0;
        it.old_pos = get_pos(v.value("position", json::array({0.0, 0.0})));
        scale_of[it.vid] = it.old_scale;
        auto g = geom_by_id.find(it.vid);
        std::array<double, 4> bb{};
        if (g != geom_by_id.end() && bbox4(g->second, bb)) {
            it.has_bb = true;
            it.mb = {(bb[0] - it.old_pos[0]) / it.old_scale, (bb[1] - it.old_pos[1]) / it.old_scale,
                     (bb[2] - it.old_pos[0]) / it.old_scale, (bb[3] - it.old_pos[1]) / it.old_scale};
        }
        items.push_back(it);
    }
    if (items.empty())
        return json{{"scale", fixed_scale > 0.0 ? fixed_scale : 1.0}, {"projection", projection}};
    for (Item& it : items) {
        if ((it.type == "section" || it.type == "detail") && scale_of.count(it.parent))
            it.ratio = it.old_scale / scale_of[it.parent];
    }

    // Lattice slots: standard orthographic views first, then the pictorial view in the
    // top-right corner, then derived views in their own column beside their parent's row.
    std::set<std::pair<int, int>> used;
    for (Item& it : items) {
        if (it.type != "base" && it.type != "projected") continue;
        const json* v = find_view_c(s.doc, it.vid);
        int c = 0, r = 0;
        if (!v || !standard_slot(v->value("orientation", std::string()), first_angle, c, r)) continue;
        while (used.count({c, r})) ++c;
        it.col = c; it.row = r; it.placed = true;
        used.insert({c, r});
    }
    int max_col = -1, min_row = 99;
    for (auto& u : used) { max_col = std::max(max_col, u.first); min_row = std::min(min_row, u.second); }
    if (used.empty()) { max_col = 0; min_row = 0; }
    for (Item& it : items) {
        if (it.placed || (it.type == "section" || it.type == "detail")) continue;
        int c = std::max(max_col, 1), r = min_row;
        if (!used.count({max_col + 1, r}) && used.count({c, r})) c = max_col + 1;
        while (used.count({c, r})) ++c;
        it.col = c; it.row = r; it.placed = true;
        used.insert({c, r});
    }
    for (auto& u : used) max_col = std::max(max_col, u.first);
    for (Item& it : items) {
        if (it.placed) continue;
        int r = 1;
        for (const Item& p : items)
            if (p.vid == it.parent && p.placed) r = p.row;
        int c = max_col + 1;
        while (used.count({c, r})) ++r;
        it.col = c; it.row = r; it.placed = true;
        used.insert({c, r});
    }

    // Free drawing area: the frame minus the title-block band and any placed tables.
    const std::array<double, 2> paper = sheet_dims(sheet.value("size", std::string("A3")));
    const auto inner = sheet_frame_inner(paper[0], paper[1]);
    const auto tbr = sheet_title_block_rect(paper[0], paper[1]);
    double ax0 = inner[0] + 4.0, ax1 = inner[2] - 4.0;
    double ay0 = tbr[3] + 4.0, ay1 = inner[3] - 4.0;
    for (const json& stored : sheet.value("tables", json::array())) {
        const json table = resolved_table(s, stored);
        const std::array<double, 2> anc = get_pos(table.value("anchor", json::array({25.0, 282.0})));
        double tw = 0.0;
        for (const json& c : table.value("columns", json::array()))
            tw += std::max(8.0, c.value("width", 30.0));
        const double th = table.value("row_height", 6.0) *
                          (1 + (table.value("title", std::string()).empty() ? 0 : 1) +
                           (double)table.value("rows", json::array()).size());
        const bool overlaps_y = anc[1] - th < ay1 && anc[1] > ay0;
        if (!overlaps_y) continue;
        if (anc[0] > (ax0 + ax1) * 0.5) ax1 = std::min(ax1, anc[0] - 5.0);
        else ax0 = std::max(ax0, anc[0] + tw + 5.0);
    }
    const double gap = 26.0;  // room for dimensions + captions between views
    const double pad_l = 22.0, pad_r = 6.0, pad_b = 20.0, pad_t = 6.0;

    // One lattice solve. With `float_pictorial` the iso view does not size the lattice; it is
    // placed afterwards in the largest free corner (the top-right cell in third angle), at the
    // sheet scale or one standard step smaller — so a tall pictorial never shrinks the views
    // that carry the dimensions.
    struct Solved {
        double scale = 1.0;
        std::map<int, int> col_ix, row_ix;
        std::vector<double> col_c, row_c;
        std::map<std::string, std::array<double, 3>> floating;  // vid -> {cx, cy, scale}
        bool ok = true;
    };
    auto solve = [&](bool float_pictorial) -> Solved {
        Solved r;
        auto floats = [&](const Item& it) { return float_pictorial && it.type == "iso"; };
        std::set<std::pair<int, int>> occ;
        for (const Item& it : items)
            if (!floats(it)) occ.insert({it.col, it.row});
        if (occ.empty()) {
            r.ok = false;
            return r;
        }
        for (auto& u : occ) { r.col_ix[u.first] = 0; r.row_ix[u.second] = 0; }
        { int i = 0; for (auto& c : r.col_ix) c.second = i++; }
        { int i = 0; for (auto& rr : r.row_ix) rr.second = i++; }
        std::vector<double> colm(r.col_ix.size(), 0.0), rowm(r.row_ix.size(), 0.0);
        for (const Item& it : items) {
            if (!it.has_bb || floats(it)) continue;
            const int ci = r.col_ix[it.col], ri = r.row_ix[it.row];
            colm[ci] = std::max(colm[ci], (it.mb[2] - it.mb[0]) * it.ratio);
            rowm[ri] = std::max(rowm[ri], (it.mb[3] - it.mb[1]) * it.ratio);
        }
        double sum_w = 0, sum_h = 0;
        for (double w : colm) sum_w += w;
        for (double h : rowm) sum_h += h;
        const double fixed_w = pad_l + pad_r + gap * (double)(colm.size() - 1);
        const double fixed_h = pad_b + pad_t + gap * (double)(rowm.size() - 1);
        double sc = fixed_scale;
        if (!(sc > 0.0)) {
            double fit = 1e30;
            if (sum_w > 1e-9) fit = std::min(fit, (ax1 - ax0 - fixed_w) / sum_w);
            if (sum_h > 1e-9) fit = std::min(fit, (ay1 - ay0 - fixed_h) / sum_h);
            sc = fit < 1e29 && fit > 0.0 ? snap_scale_down(fit)
                                         : (fit >= 1e29 ? 1.0 : standard_scales().back());
        }
        r.scale = sc;
        // Cell centres: columns left to right, rows top to bottom, arrangement centred.
        const double used_w = sum_w * sc + fixed_w - pad_l - pad_r;
        const double used_h = sum_h * sc + fixed_h - pad_b - pad_t;
        double x = ax0 + pad_l + std::max(0.0, (ax1 - ax0 - pad_l - pad_r - used_w) * 0.5);
        std::vector<double> col_l(colm.size()), row_t(rowm.size());
        r.col_c.resize(colm.size());
        r.row_c.resize(rowm.size());
        for (size_t i = 0; i < colm.size(); ++i) {
            col_l[i] = x;
            r.col_c[i] = x + colm[i] * sc * 0.5;
            x += colm[i] * sc + gap;
        }
        double y = ay1 - pad_t - std::max(0.0, (ay1 - ay0 - pad_t - pad_b - used_h) * 0.5);
        for (size_t i = 0; i < rowm.size(); ++i) {
            row_t[i] = y;
            r.row_c[i] = y - rowm[i] * sc * 0.5;
            y -= rowm[i] * sc + gap;
        }
        if (!float_pictorial) return r;
        // Free corner beside the top row: right of its last occupied cell, above row 2.
        const int top_row = r.row_ix.begin()->first;
        int last_col = -1;
        for (auto& u : occ)
            if (u.second == top_row) last_col = std::max(last_col, r.col_ix[u.first]);
        const double fx0 = last_col + 1 < (int)colm.size()
                               ? col_l[last_col + 1]
                               : col_l[last_col] + colm[last_col] * sc + gap;
        const double fy0 = rowm.size() > 1 ? row_t[1] + gap * 0.5 : ay0 + pad_b;
        const double fx1 = ax1 - pad_r, fy1 = ay1 - pad_t;
        for (const Item& it : items) {
            if (!floats(it)) continue;
            if (!it.has_bb) continue;
            const double w = it.mb[2] - it.mb[0], h = it.mb[3] - it.mb[1];
            double isc = sc;
            if (w * isc > fx1 - fx0 || h * isc > fy1 - fy0)
                isc = snap_scale_down(std::min((fx1 - fx0) / std::max(w, 1e-9),
                                               (fy1 - fy0) / std::max(h, 1e-9)));
            if (isc < sc * 0.45 || fx1 - fx0 < 15.0 || fy1 - fy0 < 15.0) {
                r.ok = false;
                return r;
            }
            r.floating[it.vid] = {(fx0 + fx1) * 0.5, (fy0 + fy1) * 0.5, isc};
        }
        return r;
    };
    Solved sol = solve(true);
    if (!sol.ok) sol = solve(false);
    const double scale = sol.scale;

    // Compute every target first (detail ratios read the parents' old scales), then apply.
    struct Target { std::string vid; double scale; std::array<double, 2> pos; bool move; };
    std::vector<Target> targets;
    for (const Item& it : items) {
        double ns = scale * it.ratio;
        double cx = 0, cy = 0;
        auto fl = sol.floating.find(it.vid);
        if (fl != sol.floating.end()) {
            ns = fl->second[2];
            cx = fl->second[0];
            cy = fl->second[1];
        } else if (it.has_bb) {
            cx = sol.col_c[sol.col_ix[it.col]];
            cy = sol.row_c[sol.row_ix[it.row]];
        }
        Target t{it.vid, ns, it.old_pos, false};
        if (it.has_bb) {
            const double mcx = (it.mb[0] + it.mb[2]) * 0.5, mcy = (it.mb[1] + it.mb[3]) * 0.5;
            t.pos = {cx - mcx * ns, cy - mcy * ns};
            t.move = true;
        }
        targets.push_back(t);
        // A pictorial drawn at its own scale says so under the view.
        if (json* v = find_view(s.doc, it.vid); v && it.type == "iso") {
            if (std::fabs(ns - scale) > 1e-12) (*v)["caption"] = "SCALE " + scale_text(ns);
            else v->erase("caption");
        }
    }
    for (const Target& t : targets) {
        json* v = find_view(s.doc, t.vid);
        if (!v) continue;
        apply_view_transform(s, t.vid, v, json(t.scale),
                             t.move ? json::array({t.pos[0], t.pos[1]}) : json(nullptr),
                             json(nullptr), json(nullptr));
    }
    json& sheet_after = find_sheet(s.doc, sid);
    if (!sheet_after.contains("title_block") || !sheet_after["title_block"].is_object())
        sheet_after["title_block"] = json::object();
    sheet_after["title_block"]["scale"] = scale_text(scale);
    sheet_after["title_block"]["projection"] = projection;
    s.revision += 1;
    return json{{"scale", scale}, {"scale_text", scale_text(scale)}, {"projection", projection}};
}

// Sheet-level auto-dimensioning: each model extent and each diameter group once, round
// outlines by their ⌀, centre marks / bolt circles / axis centre lines. Assemblies get the
// overall envelope only (their parts are identified by balloons + the BOM).
int auto_dimension_locked(DrawingSession& s, const std::string& doc_id, const std::string& sheet_id,
                          bool assembly) {
    json& sheet = find_sheet(s.doc, sheet_id);
    const std::string sid = sheet.value("id", std::string());
    const json full = drawing_get_locked_unsafe(doc_id);
    std::map<std::string, json> geom_by_id;
    for (const json& sh : full.value("sheets", json::array()))
        if (sh.value("id", std::string()) == sid)
            for (const json& g : sh.value("views", json::array()))
                geom_by_id[g.value("view_id", std::string())] = g;
    std::vector<AxisSeg> axes;
    if (!assembly) axes = cylinder_axes(source_shapes(s));
    const std::string units = doc_units(s.doc);
    static const char* kPriority[] = {"front", "top", "right", "left", "bottom", "back"};
    DimLedger ledger;
    int added = 0;
    json& sheet_ref = find_sheet(s.doc, sid);
    // Pre-pass: a round outline in ANY view claims its axes for the ⌀ before any view gets a
    // linear overall dimension along them.
    if (!assembly)
        for (json& v : sheet_ref["views"]) {
            const std::string type = v.value("type", std::string());
            if (type != "base" && type != "projected") continue;
            auto g = geom_by_id.find(v.value("id", std::string()));
            if (g == geom_by_id.end()) continue;
            Vec3 d, sx, sy;
            try { orient_frame(v.value("orientation", std::string()), d, sx, sy); }
            catch (const OpError&) { continue; }
            const auto r = round_outline(g->second);
            if (r[0]) ledger.axes[dominant_axis(sx)] = true;
            if (r[1]) ledger.axes[dominant_axis(sy)] = true;
        }
    for (const char* orient : kPriority)
        for (json& v : sheet_ref["views"]) {
            const std::string type = v.value("type", std::string());
            if ((type != "base" && type != "projected") ||
                v.value("orientation", std::string()) != orient)
                continue;
            auto g = geom_by_id.find(v.value("id", std::string()));
            if (g == geom_by_id.end()) continue;
            added += add_model_items(v, g->second, units, &ledger, assembly ? nullptr : &axes,
                                     assembly);
        }
    s.revision += 1;
    return added;
}
}  // namespace

json drawing_arrange(const std::string& doc_id, const std::string& sheet_id,
                     const std::string& projection, const nlohmann::json& scale) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    if (scale.is_number() && !(scale.get<double>() > 0.0 && std::isfinite(scale.get<double>())))
        throw OpError("invalid_op", "scale must be positive", {{"reason", "bad scale"}});
    json r = arrange_sheet_locked(s, doc_id, sheet_id, projection,
                                  scale.is_number() ? scale.get<double>() : 0.0);
    r["doc_id"] = doc_id;
    r["drawing"] = drawing_get_locked_unsafe(doc_id);
    return r;
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
    const std::string paper = canon_sheet_size(size);
    if (sheet_dims(paper)[0] <= 0.0)
        throw OpError("invalid_op", "unknown sheet size '" + size + "'", {{"reason", "bad size"}});
    sheet["size"] = paper;
    s.revision += 1;
    return json{{"doc_id", doc_id}, {"sheet_id", sheet.value("id", json(nullptr))},
                {"size", paper}, {"drawing", drawing_get_locked_unsafe(doc_id)}};
}
json drawing_add_table(const std::string& doc_id, const std::string& sheet_id,
                       const std::string& kind, const json& spec) {
    DrawingLock lk;
    DrawingSession& s = require_session(doc_id);
    json& sheet = find_sheet(s.doc, sheet_id);
    if (kind != "bom" && kind != "revision" && kind != "holes" &&
        kind != "notes" && kind != "cutlist")
        throw OpError("invalid_op",
                      "table kind '" + kind +
                          "' must be bom|revision|holes|notes|cutlist",
                      {{"reason", "bad table kind"}, {"kind", kind}});
    const json& cfg = spec.is_object() ? spec : json::object();
    const std::array<double, 2> mm = sheet_dims(sheet.value("size", std::string("A3")));
    json anchor = cfg.contains("anchor") ? cfg["anchor"] :
                  cfg.contains("position") ? cfg["position"] :
                  json::array({sheet_frame_inner(mm[0], mm[1])[0] + 5.0,
                               sheet_frame_inner(mm[0], mm[1])[3] - 5.0});
    if (!is_xy(anchor))
        throw OpError("invalid_op", "table position must be [x,y] sheet mm",
                      {{"reason", "bad position"}});
    static const std::map<std::string, std::string> titles = {
        {"bom", "BILL OF MATERIALS"}, {"revision", "REVISION HISTORY"},
        {"holes", "HOLE TABLE"}, {"notes", "GENERAL NOTES"},
        {"cutlist", "WELD CUT LIST"}};
    json table = {{"id", next_table_id(s.doc)}, {"kind", kind},
                  {"title", cfg.value("title", titles.at(kind))},
                  {"anchor", json::array({anchor[0].get<double>(), anchor[1].get<double>()})},
                  {"columns", cfg.contains("columns") ? cfg["columns"]
                                                       : default_table_columns(kind)},
                  {"rows", cfg.contains("rows") && cfg["rows"].is_array()
                               ? cfg["rows"] : json::array()},
                  {"row_height", cfg.value("row_height", 6.0)}};
    if (!sheet.contains("tables") || !sheet["tables"].is_array())
        sheet["tables"] = json::array();
    sheet["tables"].push_back(table);
    ++s.revision;
    json resolved = resolved_table(s, table);
    resolved["draw"] = compile_table_draw(resolved);
    return json{{"doc_id", doc_id}, {"sheet_id", sheet.value("id", json(nullptr))},
                {"table_id", table["id"]}, {"table", resolved},
                {"revision", s.revision}};
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
    const std::string projection =
        is_first_angle(s.value("projection", std::string("third"))) ? "first" : "third";

    std::vector<std::string> views;
    if (s.contains("views") && s["views"].is_array())
        for (const auto& v : s["views"])
            if (v.is_string()) views.push_back(v.get<std::string>());
    if (views.empty()) views = {"front", "top", "right", "iso"};

    bool assembly = false;
    bool general_arrangement = false;  // many bodies: no hidden lines, envelope dims only
    double provisional = 1.0;          // first projection scale, from the bounding box
    {
        DrawingLock lk;
        DrawingSession& ds = require_session(doc_id);
        assembly = !bom_rows_locked(ds).empty();
        const auto shapes = source_shapes(ds);
        general_arrangement = assembly || shapes.size() > 4;
        // Hidden-line removal is discretised in sheet mm, so projecting a 5 m model at 1:1
        // costs orders of magnitude more than at its real scale. Estimate the layout's scale
        // from the bounding box (front + right across, top + front down) so the first
        // projection is usually already the final one and the layout pass re-uses it.
        Bnd_Box box;
        for (const TopoDS_Shape& sh : shapes)
            if (!sh.IsNull()) BRepBndLib::Add(sh, box);
        if (!box.IsVoid()) {
            double x0, y0, z0, x1, y1, z1;
            box.Get(x0, y0, z0, x1, y1, z1);
            const double dx = x1 - x0, dy = y1 - y0, dz = z1 - z0;
            const std::string size =
                find_sheet(ds.doc, "s1").value("size", std::string("A3"));
            const auto paper = sheet_dims(size);
            const auto inner = sheet_frame_inner(paper[0], paper[1]);
            const auto tbr = sheet_title_block_rect(paper[0], paper[1]);
            const double aw = inner[2] - inner[0] - 8.0 - 28.0 - 26.0;
            const double ah = inner[3] - tbr[3] - 8.0 - 26.0 - 26.0;
            const double sw = dx + std::max(dy, 0.9 * (dx + dy) * 0.7071);
            const double sh = dy + dz;
            if (sw > 1e-9 && sh > 1e-9 && aw > 0.0 && ah > 0.0)
                provisional = snap_scale_down(std::min(aw / sw, ah / sh));
        }
    }
    if (s.contains("scale") && s["scale"].is_number() && s["scale"].get<double>() > 0.0)
        provisional = s["scale"].get<double>();

    // Create every view at the provisional scale (the layout pass picks the real scale and
    // positions). The first orthographic view is the base; the pictorial view carries no
    // hidden lines, and neither do the views of a many-body general arrangement (thousands
    // of dashes of other bodies' edges hide the information instead of adding it).
    static const std::set<std::string> kOrtho = {"front", "top", "right", "bottom", "left", "back"};
    bool used_base = false;
    for (const std::string& vn : views) {
        const bool iso = vn == "iso";
        if (!iso && !kOrtho.count(vn)) continue;  // unknown view name -> skip (never throw)
        std::string type = iso ? "iso" : (used_base ? "projected" : "base");
        if (!iso) used_base = true;
        try {
            drawing_add_view(doc_id, type, vn, "s1", provisional, json::array({0.0, 0.0}),
                             (iso || general_arrangement) ? json(false) : json(nullptr));
        } catch (const OpError&) { /* skip a view that fails to project */ }
    }
    // Assemblies: the parts list sits on the title block (ISO 7573), so the layout avoids it.
    if (assembly) {
        try {
            const json listed = drawing_bom(doc_id);
            const std::size_t n = listed.value("rows", json::array()).size();
            json probe = drawing_list_sheets(doc_id);
            std::string size = "A3";
            for (const json& sh : probe.value("sheets", json::array()))
                if (sh.value("id", std::string()) == "s1") size = sh.value("size", std::string("A3"));
            const auto paper = sheet_dims(size);
            const auto tbr = sheet_title_block_rect(paper[0], paper[1]);
            const double top = tbr[3] + 6.0 * (double)(n + 1) + 0.5;
            drawing_add_table(doc_id, "s1", "bom",
                              json{{"anchor", json::array({tbr[0], top})}});
        } catch (const OpError&) {}
    }

    // An explicit numeric spec scale fixes the sheet scale; otherwise the layout fits one.
    const json fixed_scale = s.contains("scale") && s["scale"].is_number() &&
                                     s["scale"].get<double>() > 0.0
                                 ? s["scale"] : json(nullptr);
    auto arrange = [&]() {
        try { drawing_arrange(doc_id, "s1", projection, fixed_scale); }
        catch (const OpError&) {}
    };
    arrange();

    // Auto-sections: each spec entry cuts a parent view through its centre along an axis, so an
    // LLM can request a section WITHOUT knowing sheet-mm coordinates. Entry:
    //   {"parent":"front"|<view_id>, "axis":"vertical"|"horizontal", "mode":"full"|"half",
    //    "reverse":bool, "label":"A"}
    if (s.contains("sections") && s["sections"].is_array() && !s["sections"].empty()) {
        json full = drawing_get(doc_id);
        auto find_parent = [&](const std::string& want, json& bbox, std::string& vid) -> bool {
            for (const auto& sh : full.value("sheets", json::array()))
                for (const auto& v : sh.value("views", json::array())) {
                    const std::string t = v.value("type", std::string());
                    if (t != "base" && t != "projected") continue;
                    std::string id = v.value("view_id", v.value("id", std::string()));
                    if (want == id || want == v.value("orientation", std::string())) {
                        const json& bb = v.value("bbox", json(nullptr));
                        if (bb.is_array() && bb.size() == 4 && bb[0].is_number()) {
                            bbox = bb;
                            vid = id;
                            return true;
                        }
                    }
                }
            return false;
        };
        bool any = false;
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
            double pad = 0.08 * std::max(x1 - x0, y1 - y0) + 4.0;
            json cut = (axis == "horizontal")
                           ? json::array({json::array({x0 - pad, cy}), json::array({x1 + pad, cy})})
                           : json::array({json::array({cx, y0 - pad}), json::array({cx, y1 + pad})});
            json opts = {{"mode", sec.value("mode", std::string("full"))},
                         {"reverse", sec.value("reverse", false)}};
            try {
                drawing_add_section(doc_id, vid, json(nullptr), cut,
                                    sec.contains("label") ? sec["label"] : json(nullptr),
                                    json(nullptr), json(nullptr), "s1", json(false), opts);
                any = true;
            } catch (const OpError&) { /* skip a section that fails to cut */ }
        }
        if (any) arrange();
    }

    const bool model_items = s.value("model_items", true);
    const bool hole_callouts = s.value("hole_callouts", false);
    if (model_items) {
        DrawingLock lk;
        DrawingSession& ds = require_session(doc_id);
        try { auto_dimension_locked(ds, doc_id, "s1", general_arrangement); } catch (const OpError&) {}
    }
    if (assembly)
        try { drawing_auto_balloon(doc_id); } catch (const OpError&) {}

    if (hole_callouts && !general_arrangement) {
        // ONE "N× ⌀d" callout per DISTINCT hole diameter on the sheet (D4). D-B1: diameters
        // the sheet-level model items already called out (in ANY view) are never repeated.
        json full = drawing_get(doc_id);
        std::vector<double> done;  // model diameters already called out
        for (const auto& sh : full.value("sheets", json::array()))
            for (const auto& v : sh.value("views", json::array()))
                for (const auto& an : v.value("annotations", json::array())) {
                    const std::string k = an.value("kind", an.value("type", std::string()));
                    if (k == "diameter_dim" || k == "hole_callout")
                        done.push_back(an.value("value", 0.0));
                }
        for (const auto& sh : full.value("sheets", json::array()))
            for (const auto& v : sh.value("views", json::array())) {
                std::string vid = v.value("view_id", v.value("id", std::string()));
                const std::string t = v.value("type", std::string());
                if (vid.empty() || (t != "base" && t != "projected")) continue;
                double vscale = v.value("scale", 1.0);
                if (!(vscale > 0.0)) vscale = 1.0;
                for (const Circ& c : view_circles(v)) {
                    const double dia = 2.0 * c.r / vscale;
                    bool dup = false;
                    for (double dd : done)
                        if (std::fabs(dd - dia) < 1e-3) { dup = true; break; }
                    if (dup) continue;
                    done.push_back(dia);
                    try {
                        drawing_add_callout(doc_id, vid, "hole", json::array({c.cx, c.cy}),
                                            json::object());
                    } catch (const OpError&) {}
                }
            }
    }

    // Title block: the caller's fields over sensible defaults — part name, material from the
    // source, today's date, units, the projection method. SCALE always reflects the layout's
    // real view scale (set by the arrange pass); an explicit caller scale still wins.
    json title = (s.contains("title") && s["title"].is_object()) ? s["title"] : json::object();
    {
        DrawingLock lk;
        DrawingSession& ds = require_session(doc_id);
        const json src = live_source_tree(ds);
        if (!title.contains("title") && src.is_object() && src.contains("name") &&
            src["name"].is_string())
            title["title"] = src["name"];
        if (!title.contains("material") && src.is_object() && src.contains("material")) {
            const json& m = src["material"];
            std::string mat = m.is_string() ? m.get<std::string>()
                              : m.is_object() ? m.value("name", m.value("key", std::string()))
                                              : std::string();
            if (!mat.empty()) title["material"] = mat;
        }
    }
    if (!title.contains("date")) {
        std::time_t now = std::time(nullptr);
        std::tm tmv{};
#ifdef _WIN32
        gmtime_s(&tmv, &now);
#else
        gmtime_r(&now, &tmv);
#endif
        char buf[16];
        std::strftime(buf, sizeof(buf), "%Y-%m-%d", &tmv);
        title["date"] = buf;
    }
    title["projection"] = projection;
    if (title.contains("scale") && !(title["scale"].is_string() &&
                                     !title["scale"].get<std::string>().empty()))
        title.erase("scale");
    try { drawing_set_titleblock(doc_id, "s1", title); } catch (const OpError&) {}

    return json{{"doc_id", doc_id}, {"drawing", drawing_get(doc_id)}};
}

bool drawing_close(const std::string& doc_id) {
    std::string owned_source;
    {
        DrawingLock lk;
        auto it = g_drawings.find(doc_id);
        if (it == g_drawings.end()) return false;
        if (it->second.owns_source) owned_source = it->second.source_ref_id;
        g_drawings.erase(it);
    }
    if (!owned_source.empty()) {
        try {
            (void)doc_close(owned_source);
        } catch (const std::exception&) {
            // The drawing is already closed; a busy source remains a normal live session.
        }
    }
    return true;
}

// ----------------------------------------------------------------- crash recovery
// Drop an orphaned g_mtx lock left held by THIS thread after a Windows structured
// exception (access violation) unwound past a DrawingLock without running its C++
// destructor under /EHsc. Mirrors force_release_doc_lock in core/doc/session.cpp —
// same pattern, same safety contract, same idempotency guarantee.
// Called from the libchudware C dispatch boundary's __except filter alongside
// force_release_doc_lock so ANY hardware fault releases ALL orphaned registry locks.
bool force_release_drawing_lock() {
    if (!g_mtx_held.load(std::memory_order_acquire)) return false;
    if (g_mtx_owner != std::this_thread::get_id()) return false;
    g_mtx_held.store(false, std::memory_order_release);
    g_mtx.unlock();
    return true;
}

}  // namespace chudware::doc
