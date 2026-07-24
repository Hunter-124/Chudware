// Assembly compile + mate-solve + tessellation — see assembly.h.
//
// Mirrors engine/doc/assembly.py: load each non-suppressed component (.step+ container
// -> compile_document; bare STEP/IGES/BREP/mesh -> import_bodies), resolve each mate
// ref's selector into a local-frame MateGeom, solve the mates over the shared numeric
// core, and emit one placed AsmBodyOut per loaded component.

#include "doc/assembly.h"

#define _USE_MATH_DEFINES
#include <algorithm>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <future>
#include <map>
#include <mutex>
#include <thread>

#include <TopLoc_Location.hxx>

#include <BRep_Builder.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopExp_Explorer.hxx>
#include <TopAbs.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <gp_Circ.hxx>
#include <gp_Trsf.hxx>

#include "doc/document.h"
#include "doc/formats.h"
#include "doc/selectors.h"
#include "doc/stepplus.h"
#include "doc/session.h"  // OpError
#include "util/expr.h"

namespace chudware::doc {

using nlohmann::json;

namespace {

const std::vector<double> kIdent16 = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};

// ---------------------------------------------------------------- component cache
// Cached load of a component source file: local-frame bodies + a topology description
// (faces/edges/verts described once, parallel to the kernel sub-shape lists).
struct LoadedComponent {
    std::vector<TopoDS_Shape> bodies;             // LOCAL frame
    TopoDS_Shape compound;                        // compound_of(bodies), built ONCE — a stable
                                                  // TShape so downstream per-shape caches
                                                  // (assembly tess cache) hit across rebuilds
    std::vector<TopoInfo> faces;                  // across all bodies, flat
    std::vector<TopoInfo> edges;
    std::vector<TopoInfo> verts;
    std::vector<TopoDS_Shape> face_shapes;        // parallel kernel faces (for adaptor radii)
    std::vector<TopoDS_Shape> edge_shapes;
};

// Keyed by abspath:mtime. Values are shared_future-of-shared_ptr so concurrent loaders of
// the SAME file share one load (first caller populates, the rest wait), while loads of
// DIFFERENT files run fully in parallel — the mutex only guards the map itself.
std::mutex g_cache_mtx;
std::map<std::string, std::shared_future<std::shared_ptr<const LoadedComponent>>> g_load_cache;

std::string resolve_source_path(const std::string& path, const std::string& base_dir) {
    std::filesystem::path p(path);
    if (p.is_absolute()) return p.string();
    if (!base_dir.empty()) {
        std::filesystem::path cand = std::filesystem::path(base_dir) / p;
        std::error_code ec;
        if (std::filesystem::exists(cand, ec)) return cand.string();
    }
    std::error_code ec;
    return std::filesystem::absolute(p, ec).string();
}

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

bool ends_with(const std::string& s, const std::string& suf) {
    return s.size() >= suf.size() && s.compare(s.size() - suf.size(), suf.size(), suf) == 0;
}

// Equation env for compiling an embedded .step+ part doc (mirror session.cpp env_from_doc).
json env_from_doc(const json& doc) {
    json env = json::object();
    if (doc.contains("equations") && doc["equations"].is_object() &&
        doc["equations"].contains("globals") && doc["equations"]["globals"].is_array()) {
        std::map<std::string, double> acc;
        for (const json& g : doc["equations"]["globals"]) {
            const std::string name = g.value("name", std::string());
            const std::string expr = g.value("expr", std::string());
            if (name.empty()) continue;
            try {
                double v = chudware::util::evaluate(expr, acc);
                acc[name] = v;
                env[name] = v;
            } catch (...) { /* skip a bad global */ }
        }
    }
    return env;
}

// Load local-frame solids for a component file (port of assembly.py::_load_bodies).
std::vector<TopoDS_Shape> load_bodies(const std::string& abspath) {
    const std::string low = lower(abspath);
    if (ends_with(low, ".step+") || ends_with(low, ".stepplus")) {
        StepPlusDoc loaded = stepplus_open(abspath);  // throws OpError on a bad file
        // Solved-bodies fast path: a part saved after a fully-ok rebuild embeds its compiled
        // bodies as cache.brep (BREP + triangulation) with a `solved` flag in the cache.json
        // header. When the engine key + doc hash still match, those bodies ARE the exact
        // compile_document output — load them directly and skip the rebuild AND the re-mesh.
        // Any mismatch/corruption falls through to the full compile.
        {
            auto cj = loaded.extra.find("cache.json");
            auto cb = loaded.extra.find("cache.brep");
            if (cj != loaded.extra.end() && cb != loaded.extra.end()) {
                try {
                    json header = json::parse(cj->second);
                    if (header.value("engine", std::string()) == stepplus_cache_engine_key() &&
                        header.value("doc_hash", std::string()) ==
                            stepplus_doc_hash(loaded.doc) &&
                        header.value("solved", false)) {
                        std::vector<TopoDS_Shape> bodies = brep_cache_decode(cb->second);
                        if (!bodies.empty()) return bodies;
                    }
                } catch (...) { /* stale/corrupt cache -> full compile below */ }
            }
        }
        CompileResult r = compile_document(loaded.doc, env_from_doc(loaded.doc));
        if (r.shapes.empty())
            throw OpError("import_failed", "component '" + abspath + "' produced no bodies",
                          {{"path", abspath}});
        return r.shapes;
    }
    // bare neutral formats (step/iges/brep/stl/obj/ply/gltf/glb)
    std::string fmt = detect_format(abspath);
    std::vector<std::pair<std::string, TopoDS_Shape>> pairs = import_bodies(abspath, fmt);
    std::vector<TopoDS_Shape> bodies;
    for (auto& kv : pairs)
        if (!kv.second.IsNull()) bodies.push_back(kv.second);
    if (bodies.empty())
        throw OpError("import_failed", "component '" + abspath + "' produced no solid bodies",
                      {{"path", abspath}});
    return bodies;
}

TopoDS_Shape compound_of(const std::vector<TopoDS_Shape>& bodies);  // defined below

// Describe every face/edge/vertex of the component's bodies (local frame) for mate
// resolution (port of assembly.py::_describe_component). Flat across all bodies in
// body order; indices re-numbered contiguously.
LoadedComponent describe_component(std::vector<TopoDS_Shape> bodies) {
    LoadedComponent lc;
    lc.bodies = std::move(bodies);
    lc.compound = compound_of(lc.bodies);
    int fidx = 0, eidx = 0, vidx = 0;
    for (const TopoDS_Shape& bo : lc.bodies) {
        std::vector<TopoInfo> faces = describe_faces(bo);
        for (TopoInfo& info : faces) { info.index = fidx; info.hint_index = fidx; ++fidx; }
        // parallel kernel faces (TopExp order matches describe_faces)
        for (TopExp_Explorer ex(bo, TopAbs_FACE); ex.More(); ex.Next())
            lc.face_shapes.push_back(ex.Current());
        lc.faces.insert(lc.faces.end(), faces.begin(), faces.end());

        std::vector<TopoInfo> edges = describe_edges(bo);
        for (TopoInfo& info : edges) { info.index = eidx; info.hint_index = eidx; ++eidx; }
        for (TopExp_Explorer ex(bo, TopAbs_EDGE); ex.More(); ex.Next())
            lc.edge_shapes.push_back(ex.Current());
        lc.edges.insert(lc.edges.end(), edges.begin(), edges.end());

        std::vector<TopoInfo> verts = describe_vertices(bo);
        for (TopoInfo& info : verts) { info.index = vidx; info.hint_index = vidx; ++vidx; }
        lc.verts.insert(lc.verts.end(), verts.begin(), verts.end());
    }
    return lc;
}

std::string component_cache_key(const std::string& abspath) {
    double mtime = 0.0;
    std::error_code ec;
    auto ftime = std::filesystem::last_write_time(abspath, ec);
    if (!ec) mtime = static_cast<double>(ftime.time_since_epoch().count());
    return abspath + ":" + std::to_string(static_cast<long long>(mtime));
}

const LoadedComponent& load_component(const std::string& path, const std::string& base_dir) {
    const std::string abspath = resolve_source_path(path, base_dir);
    const std::string key = component_cache_key(abspath);

    // WHY: find-or-install-a-placeholder happens in ONE g_cache_mtx acquisition below (no
    // check-then-insert gap) — the first caller for this key installs a promise and loads
    // OUTSIDE the lock; every concurrent caller for the SAME key copies the shared_future
    // (never a bare reference into the map) WHILE STILL HOLDING the lock, then waits on
    // that copy instead of loading twice. Loads of DIFFERENT keys still run fully in parallel.
    std::shared_future<std::shared_ptr<const LoadedComponent>> fut;
    std::shared_ptr<std::promise<std::shared_ptr<const LoadedComponent>>> mine;
    {
        std::lock_guard<std::mutex> lk(g_cache_mtx);
        auto it = g_load_cache.find(key);
        if (it != g_load_cache.end()) {
            fut = it->second;
        } else {
            mine = std::make_shared<std::promise<std::shared_ptr<const LoadedComponent>>>();
            fut = g_load_cache[key] = mine->get_future().share();
        }
    }
    if (mine) {
        try {
            auto lc = std::make_shared<const LoadedComponent>(
                describe_component(load_bodies(abspath)));
            mine->set_value(lc);
        } catch (...) {
            mine->set_exception(std::current_exception());
            // A failed load is NOT cached (pre-existing semantics: a fixed file re-loads).
            // Waiters already holding `fut` still get the exception; the map just forgets it.
            std::lock_guard<std::mutex> lk(g_cache_mtx);
            g_load_cache.erase(key);
        }
    }
    return *fut.get();  // re-throws the loader's exception for every waiter
}

// ---------------------------------------------------------------- mate-ref geometry
double bbox_diag(const std::vector<TopoDS_Shape>& bodies) {
    if (bodies.empty()) return 100.0;
    Bnd_Box box;
    for (const TopoDS_Shape& b : bodies) BRepBndLib::Add(b, box);
    if (box.IsVoid()) return 100.0;
    double xmin, ymin, zmin, xmax, ymax, zmax;
    box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
    double d = std::sqrt((xmax - xmin) * (xmax - xmin) + (ymax - ymin) * (ymax - ymin) +
                         (zmax - zmin) * (zmax - zmin));
    return std::max(d, 1e-3);
}

double cyl_radius(const TopoDS_Shape& face_shape) {
    try {
        BRepAdaptor_Surface ad(TopoDS::Face(face_shape));
        if (ad.GetType() == GeomAbs_Cylinder) return ad.Cylinder().Radius();
        if (ad.GetType() == GeomAbs_Cone) return ad.Cone().RefRadius();
    } catch (...) {}
    return 0.0;
}

double sphere_radius(const TopoDS_Shape& face_shape) {
    try {
        BRepAdaptor_Surface ad(TopoDS::Face(face_shape));
        if (ad.GetType() == GeomAbs_Sphere) return ad.Sphere().Radius();
    } catch (...) {}
    return 0.0;
}

double circle_radius(const TopoDS_Shape& edge_shape) {
    try {
        BRepAdaptor_Curve ad(TopoDS::Edge(edge_shape));
        if (ad.GetType() == GeomAbs_Circle) return ad.Circle().Radius();
    } catch (...) {}
    return 0.0;
}

Eigen::Vector3d circle_centre(const TopoDS_Shape& edge_shape, const Eigen::Vector3d& fallback) {
    try {
        BRepAdaptor_Curve ad(TopoDS::Edge(edge_shape));
        if (ad.GetType() == GeomAbs_Circle) {
            gp_Pnt c = ad.Circle().Location();
            return Eigen::Vector3d(c.X(), c.Y(), c.Z());
        }
    } catch (...) {}
    return fallback;
}

// Resolve a selector against a component's LOCAL geometry into a MateGeom (port of
// assembly.py::_selector_to_mategeom). Returns false (out-param unset) on a dangle.
bool selector_to_mategeom(const json& selector, const LoadedComponent& lc,
                          chudware::mate::MateGeom& out) {
    const std::string kind = selector.value("kind", std::string());
    double diag = bbox_diag(lc.bodies);

    if (kind == "face") {
        ResolveResult rr = resolve(selector, lc.faces, diag);
        if (!rr.index) return false;
        const TopoInfo& info = lc.faces[*rr.index];
        const TopoDS_Shape& fs = lc.face_shapes[*rr.index];
        const std::string surf = info.surf ? *info.surf : std::string();
        if (surf == "plane") {
            out.kind = "plane";
            out.point = info.centroid;
            out.normal = info.axis ? *info.axis : Eigen::Vector3d(0, 0, 1);
            return true;
        }
        if (surf == "cylinder" || surf == "cone") {
            out.kind = "axis";
            out.point = info.centroid;
            out.direction = info.axis ? *info.axis : Eigen::Vector3d(0, 0, 1);
            out.radius = cyl_radius(fs);
            return true;
        }
        if (surf == "sphere") {
            out.kind = "sphere";
            out.point = info.centroid;
            out.radius = sphere_radius(fs);
            return true;
        }
        // other surfaces: a plane at the centroid with its best normal.
        out.kind = "plane";
        out.point = info.centroid;
        out.normal = info.axis ? *info.axis : Eigen::Vector3d(0, 0, 1);
        return true;
    }

    if (kind == "edge") {
        ResolveResult rr = resolve(selector, lc.edges, diag);
        if (!rr.index) return false;
        const TopoInfo& info = lc.edges[*rr.index];
        const TopoDS_Shape& es = lc.edge_shapes[*rr.index];
        if (info.surf && *info.surf == "circle") {
            out.kind = "axis";
            out.point = circle_centre(es, info.centroid);
            out.direction = info.axis ? *info.axis : Eigen::Vector3d(0, 0, 1);
            out.radius = circle_radius(es);
            return true;
        }
        out.kind = "axis";
        out.point = info.centroid;
        out.direction = info.axis ? *info.axis : Eigen::Vector3d(0, 0, 1);
        out.radius = 0.0;
        return true;
    }

    if (kind == "vertex") {
        ResolveResult rr = resolve(selector, lc.verts, diag);
        if (!rr.index) return false;
        const TopoInfo& info = lc.verts[*rr.index];
        out.kind = "point";
        out.point = info.centroid;
        return true;
    }
    return false;
}

// A distinct, pleasant colour per component ordinal (golden-angle hue walk; port of
// assembly.py::_component_color, which rounds to 4 decimals).
std::array<float, 3> component_color(int ordinal) {
    double h = std::fmod(0.58 + 0.61803398875 * ordinal, 1.0);
    double s = 0.45, v = 0.86;
    // colorsys.hsv_to_rgb
    int i = static_cast<int>(h * 6.0);
    double f = h * 6.0 - i;
    double p = v * (1.0 - s);
    double q = v * (1.0 - s * f);
    double t = v * (1.0 - s * (1.0 - f));
    double r = 0, g = 0, b = 0;
    switch (((i % 6) + 6) % 6) {
        case 0: r = v; g = t; b = p; break;
        case 1: r = q; g = v; b = p; break;
        case 2: r = p; g = v; b = t; break;
        case 3: r = p; g = q; b = v; break;
        case 4: r = t; g = p; b = v; break;
        case 5: r = v; g = p; b = q; break;
    }
    auto round4 = [](double x) { return static_cast<float>(std::round(x * 10000.0) / 10000.0); };
    return {round4(r), round4(g), round4(b)};
}

TopoDS_Shape compound_of(const std::vector<TopoDS_Shape>& bodies) {
    if (bodies.size() == 1) return bodies[0];
    TopoDS_Compound comp;
    BRep_Builder bb;
    bb.MakeCompound(comp);
    for (const TopoDS_Shape& b : bodies) bb.Add(comp, b);
    return comp;
}

gp_Trsf trsf_from_xform16(const std::vector<double>& m) {
    gp_Trsf t;
    if (m.size() == 16) {
        t.SetValues(m[0], m[1], m[2], m[3],
                    m[4], m[5], m[6], m[7],
                    m[8], m[9], m[10], m[11]);
    }
    return t;
}

// ---------------------------------------------------------------- component patterns
//
// Assembly component patterns (asm_linear_pattern / asm_circular_pattern) replicate one or more
// SEED components (already placed by the mate solve) along a direction (linear) or about an axis
// (circular), in the assembly WORLD frame. Each instance is a clone of the seed's local shape with
// an extra world-frame placement PRE-multiplied onto the seed's solved transform. (Port of
// assembly.py::_compile_component_patterns.)
//
// Instances are NOT components: no entry in doc["components"], no part in the mate solve, excluded
// from interference / BOM. They carry synthetic body_id / component_id values that cannot collide
// with real component ordinals (body_id >= kPatternBodyBase; component_id = "<seed>#<k>").

constexpr int kPatternBodyBase = 1000000;

// Read a feature param that is a {value, expr} dim or a bare number; fall back to default.
double feat_dim(const json& params, const std::string& key, double def) {
    if (!params.is_object() || !params.contains(key)) return def;
    const json& v = params[key];
    if (v.is_object()) {
        if (v.contains("value") && v["value"].is_number()) return v["value"].get<double>();
        return def;
    }
    if (v.is_number()) return v.get<double>();
    return def;
}

// Resolve a direction/axis ref to a unit WORLD vector (datum/world-axis strings, v1).
// Returns false on an unsupported / degenerate ref.
bool world_axis_from_ref(const json& ref, Eigen::Vector3d& out) {
    Eigen::Vector3d v(0, 0, 0);
    if (ref.is_array() && ref.size() == 3 && ref[0].is_number() && ref[1].is_number() &&
        ref[2].is_number()) {
        v = Eigen::Vector3d(ref[0].get<double>(), ref[1].get<double>(), ref[2].get<double>());
    } else if (ref.is_string()) {
        std::string s = lower(ref.get<std::string>());
        // trim
        while (!s.empty() && std::isspace(static_cast<unsigned char>(s.front()))) s.erase(s.begin());
        while (!s.empty() && std::isspace(static_cast<unsigned char>(s.back()))) s.pop_back();
        if (s == "axis.x" || s == "x") v = Eigen::Vector3d(1, 0, 0);
        else if (s == "axis.y" || s == "y") v = Eigen::Vector3d(0, 1, 0);
        else if (s == "axis.z" || s == "z") v = Eigen::Vector3d(0, 0, 1);
        // Datum-plane normals (Z-up: front=XZ→+Y, top=XY→+Z, right=YZ→+X).
        else if (s == "plane.front" || s == "front") v = Eigen::Vector3d(0, 1, 0);
        else if (s == "plane.top" || s == "top") v = Eigen::Vector3d(0, 0, 1);
        else if (s == "plane.right" || s == "right") v = Eigen::Vector3d(1, 0, 0);
        else return false;
    } else {
        return false;
    }
    double n = v.norm();
    if (n < 1e-9) return false;
    out = v / n;
    return true;
}

// 4x4 (row-major) <-> Eigen::Matrix4d helpers.
Eigen::Matrix4d mat_from_xform16(const std::vector<double>& m) {
    Eigen::Matrix4d M = Eigen::Matrix4d::Identity();
    if (m.size() == 16)
        for (int i = 0; i < 4; ++i)
            for (int j = 0; j < 4; ++j) M(i, j) = m[i * 4 + j];
    return M;
}

std::vector<double> xform16_from_mat(const Eigen::Matrix4d& M) {
    std::vector<double> out(16);
    for (int i = 0; i < 4; ++i)
        for (int j = 0; j < 4; ++j) out[i * 4 + j] = M(i, j);
    return out;
}

Eigen::Matrix4d translate16(const Eigen::Vector3d& vec) {
    Eigen::Matrix4d M = Eigen::Matrix4d::Identity();
    M(0, 3) = vec[0];
    M(1, 3) = vec[1];
    M(2, 3) = vec[2];
    return M;
}

// 4x4 matrix rotating by angle_deg about the WORLD line (axis_pt, axis_dir) — Rodrigues.
Eigen::Matrix4d rotation16_about_axis(const Eigen::Vector3d& axis_pt,
                                      const Eigen::Vector3d& axis_dir, double angle_deg) {
    double an = axis_dir.norm();
    Eigen::Vector3d a = an > 1e-12 ? (axis_dir / an) : Eigen::Vector3d(0, 0, 1);
    double th = angle_deg * 3.14159265358979323846 / 180.0;
    double c = std::cos(th), s = std::sin(th);
    double x = a[0], y = a[1], z = a[2];
    Eigen::Matrix3d Rm;
    Rm << c + x * x * (1 - c),     x * y * (1 - c) - z * s, x * z * (1 - c) + y * s,
          y * x * (1 - c) + z * s, c + y * y * (1 - c),     y * z * (1 - c) - x * s,
          z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c);
    Eigen::Matrix4d M = Eigen::Matrix4d::Identity();
    M.block<3, 3>(0, 0) = Rm;
    M.block<3, 1>(0, 3) = axis_pt - Rm * axis_pt;
    return M;
}

// Expand asm_linear_pattern / asm_circular_pattern features into synthetic instance bodies.
// `placed` is the solved component bodies; returns ONLY the new instance bodies. Patterns with a
// dangling direction/axis or unknown seeds are silently skipped (matches the oracle).
std::vector<AsmBodyOut> compile_component_patterns(const json& doc,
                                                   const std::vector<AsmBodyOut>& placed) {
    std::map<std::string, const AsmBodyOut*> by_cid;
    for (const AsmBodyOut& b : placed) by_cid[b.component_id] = &b;

    std::vector<AsmBodyOut> instances;
    if (!doc.contains("features") || !doc["features"].is_array()) return instances;
    int body_id = kPatternBodyBase;

    for (const json& feat : doc["features"]) {
        if (!feat.is_object() || feat.value("suppressed", false)) continue;
        const std::string ftype = feat.value("type", std::string());
        if (ftype != "asm_linear_pattern" && ftype != "asm_circular_pattern") continue;
        const json refs = feat.contains("refs") && feat["refs"].is_object() ? feat["refs"]
                                                                            : json::object();
        const json params = feat.contains("params") && feat["params"].is_object() ? feat["params"]
                                                                                  : json::object();
        std::vector<std::string> seed_cids;
        if (refs.contains("components") && refs["components"].is_array())
            for (const json& c : refs["components"]) {
                if (!c.is_string()) continue;
                const std::string cid = c.get<std::string>();
                if (by_cid.count(cid)) seed_cids.push_back(cid);
            }
        if (seed_cids.empty()) continue;

        // Build the additional instance transforms (k = 1..count-1; instance 0 = the seed).
        std::vector<Eigen::Matrix4d> offsets;
        if (ftype == "asm_linear_pattern") {
            int count1 = std::max(1, static_cast<int>(std::lround(feat_dim(params, "count1", 2))));
            double spacing1 = feat_dim(params, "spacing1", 10.0);
            Eigen::Vector3d d1;
            if (!world_axis_from_ref(refs.contains("dir1") ? refs["dir1"] : json(nullptr), d1) ||
                count1 < 2)
                continue;
            for (int k = 1; k < count1; ++k) offsets.push_back(translate16(d1 * (spacing1 * k)));
        } else {  // asm_circular_pattern
            int count = std::max(1, static_cast<int>(std::lround(feat_dim(params, "count", 4))));
            double angle_total = feat_dim(params, "angle", 360.0);
            bool equal = params.value("equal_spacing", true);
            Eigen::Vector3d ax;
            if (!world_axis_from_ref(refs.contains("axis") ? refs["axis"] : json(nullptr), ax) ||
                count < 2)
                continue;
            double step = equal ? (angle_total / count) : angle_total;
            for (int k = 1; k < count; ++k)
                offsets.push_back(rotation16_about_axis(Eigen::Vector3d::Zero(), ax, step * k));
        }

        for (const std::string& seed_cid : seed_cids) {
            const AsmBodyOut* seed = by_cid[seed_cid];
            Eigen::Matrix4d seed_mat = mat_from_xform16(seed->transform);
            int k = 0;
            for (const Eigen::Matrix4d& off : offsets) {
                ++k;
                Eigen::Matrix4d inst_mat = off * seed_mat;
                AsmBodyOut inst;
                inst.body_id = body_id++;
                inst.component_id = seed_cid + "#" + std::to_string(k);
                inst.name = seed->name + "#" + std::to_string(k);
                inst.shape_local = seed->shape_local;
                inst.transform = xform16_from_mat(inst_mat);
                inst.color = seed->color;
                instances.push_back(std::move(inst));
            }
        }
    }
    return instances;
}

}  // namespace

AssemblyResult compile_assembly(const json& doc, const std::string& base_dir,
                                const std::optional<chudware::mate::WarmMap>& warm,
                                const std::optional<chudware::mate::Drag>& drag) {
    AssemblyResult res;

    // non-suppressed components, in tree order
    std::vector<json> components;
    if (doc.contains("components") && doc["components"].is_array())
        for (const json& c : doc["components"])
            if (!c.value("suppressed", false)) components.push_back(c);
    const json mates = (doc.contains("mates") && doc["mates"].is_array())
                           ? doc["mates"] : json::array();

    // --- load every component -------------------------------------------
    // Prefetch pass: components are independent, so uncached loads (compile or cache.brep
    // decode, both CPU-bound) run in parallel worker threads; the per-key shared_future in
    // load_component dedups same-file components. The serial loop below then hits the cache.
    // Kill-switch: CHUDWARE_ASM_PARALLEL_LOAD=0.
    {
        const char* env = std::getenv("CHUDWARE_ASM_PARALLEL_LOAD");
        const bool par = !(env && env[0] == '0');
        if (par && components.size() > 1) {
            std::vector<std::string> paths;
            for (const json& comp : components) {
                const std::string p = comp.contains("source") && comp["source"].is_object()
                                          ? comp["source"].value("path", std::string())
                                          : std::string();
                if (!p.empty()) paths.push_back(p);
            }
            const unsigned hw = std::max(2u, std::thread::hardware_concurrency());
            const std::size_t n_workers = std::min<std::size_t>(paths.size(), hw);
            std::atomic<std::size_t> next{0};
            std::vector<std::thread> pool;
            pool.reserve(n_workers);
            for (std::size_t w = 0; w < n_workers; ++w)
                pool.emplace_back([&]() {
                    for (std::size_t i = next.fetch_add(1); i < paths.size();
                         i = next.fetch_add(1)) {
                        try {
                            load_component(paths[i], base_dir);
                        } catch (...) { /* reported per-component in the serial loop */ }
                    }
                });
            for (std::thread& t : pool) t.join();
        }
    }
    std::map<std::string, const LoadedComponent*> loaded;
    for (const json& comp : components) {
        const std::string cid = comp.value("id", std::string());
        AsmComponentStatus st;
        st.id = cid;
        try {
            const std::string path = comp.contains("source") && comp["source"].is_object()
                                         ? comp["source"].value("path", std::string())
                                         : std::string();
            if (path.empty())
                throw OpError("import_failed", "component '" + cid + "' has no source.path",
                              {{"component", cid}});
            const LoadedComponent& lc = load_component(path, base_dir);
            loaded[cid] = &lc;
            st.ok = true;
            st.error = std::nullopt;
        } catch (const std::exception& exc) {
            st.ok = false;
            st.error = std::string(exc.what());
        }
        st.ms = 0.0;
        res.component_status.push_back(std::move(st));
    }

    // --- resolve mate refs to local-frame MateGeom ----------------------
    chudware::mate::ResolvedGeometry resolved_geometry;
    std::map<std::string, std::vector<std::string>> dangling;  // mate_id -> refs[i] strings
    for (const json& mate : mates) {
        if (mate.value("suppressed", false)) continue;
        const std::string mid = mate.value("id", std::string());
        const json refs = mate.contains("refs") && mate["refs"].is_array() ? mate["refs"]
                                                                           : json::array();
        for (int i = 0; i < static_cast<int>(refs.size()) && i < 2; ++i) {
            const json& ref = refs[i];
            const std::string cid = ref.is_object() ? ref.value("component", std::string())
                                                    : std::string();
            auto lit = loaded.find(cid);
            const json sel_obj = ref.is_object() && ref.contains("selector") ? ref["selector"]
                                                                            : json(nullptr);
            if (lit == loaded.end() || !sel_obj.is_object()) {
                dangling[mid].push_back("refs[" + std::to_string(i) + "]");
                continue;
            }
            chudware::mate::MateGeom geom;
            if (!selector_to_mategeom(sel_obj, *lit->second, geom)) {
                dangling[mid].push_back("refs[" + std::to_string(i) + "]");
                continue;
            }
            resolved_geometry[{mid, i}] = geom;
            res.mate_geometry[mid].push_back(AsmMateRefGeom{i, cid, geom});
        }
    }

    // --- solve mates ----------------------------------------------------
    json solve_components = json::array();
    for (const json& c : components) {
        const std::string cid = c.value("id", std::string());
        if (!loaded.count(cid)) continue;
        json tf = (c.contains("transform") && !c["transform"].is_null()) ? c["transform"]
                                                                        : json(kIdent16);
        solve_components.push_back({{"id", cid}, {"transform", tf},
                                    {"fixed", c.value("fixed", false)}, {"suppressed", false}});
    }
    chudware::mate::MateSolveResult solve_res =
        chudware::mate::solve_mates(solve_components, mates, resolved_geometry, warm, drag);

    // --- build placed bodies --------------------------------------------
    int ordinal = 0;
    for (const json& comp : components) {
        ++ordinal;  // 1-based, counts ALL non-suppressed components (matches Python enumerate)
        const std::string cid = comp.value("id", std::string());
        auto lit = loaded.find(cid);
        if (lit == loaded.end()) continue;
        std::vector<double> xform;
        auto tit = solve_res.transforms.find(cid);
        if (tit != solve_res.transforms.end()) xform = tit->second;
        else if (comp.contains("transform") && comp["transform"].is_array())
            xform = comp["transform"].get<std::vector<double>>();
        else xform = kIdent16;
        res.transforms[cid] = xform;

        AsmBodyOut body;
        body.body_id = ordinal;
        body.component_id = cid;
        body.name = comp.contains("name") && comp["name"].is_string() && !comp["name"].get<std::string>().empty()
                        ? comp["name"].get<std::string>() : cid;
        body.shape_local = lit->second->compound;
        body.transform = xform;
        body.color = component_color(ordinal - 1);
        res.bodies.push_back(std::move(body));
    }

    // --- component patterns ---------------------------------------------
    // Expand asm_linear_pattern / asm_circular_pattern into synthetic instance bodies (NOT
    // components: excluded from the mate solve + interference/BOM). A bad pattern must never
    // sink the whole assembly rebuild.
    try {
        std::vector<AsmBodyOut> instances = compile_component_patterns(doc, res.bodies);
        for (AsmBodyOut& inst : instances) res.bodies.push_back(std::move(inst));
    } catch (...) {
    }

    // mate status (merge dangling)
    for (const chudware::mate::MateStatus& m : solve_res.mate_status) {
        AsmMateStatus st;
        st.id = m.mate_id;
        st.type = m.type;
        st.status = m.status;
        auto dit = dangling.find(m.mate_id);
        if (dit != dangling.end()) {
            st.status = "unresolved";
            st.dangling_refs = dit->second;
        }
        res.mate_status.push_back(std::move(st));
    }

    res.dof = solve_res.dof;
    res.over_defined = solve_res.over_defined;
    res.converged = solve_res.converged;
    res.dof_summary = solve_res.summary;
    res.warm = solve_res.params;
    return res;
}

json asm_component_fingerprints(const json& doc, const std::string& base_dir) {
    json fp = json::array();
    if (!doc.contains("components") || !doc["components"].is_array()) return fp;
    for (const json& c : doc["components"]) {
        if (c.value("suppressed", false)) continue;
        const std::string path = c.contains("source") && c["source"].is_object()
                                     ? c["source"].value("path", std::string())
                                     : std::string();
        const std::string abspath = path.empty() ? path : resolve_source_path(path, base_dir);
        uint64_t size = 0;
        long long mtime = 0;
        std::error_code ec;
        auto sz = std::filesystem::file_size(abspath, ec);
        if (!ec) size = static_cast<uint64_t>(sz);
        auto ft = std::filesystem::last_write_time(abspath, ec);
        if (!ec) mtime = static_cast<long long>(ft.time_since_epoch().count());
        fp.push_back({{"path", abspath}, {"size", size}, {"mtime", mtime}});
    }
    return fp;
}

bool asm_fingerprints_match(const json& doc, const std::string& base_dir, const json& fp) {
    try {
        return fp.is_array() && asm_component_fingerprints(doc, base_dir) == fp;
    } catch (...) {
        return false;
    }
}

std::map<std::string, Eigen::Vector3d> explode_offsets(const json& doc, double factor) {
    std::map<std::string, Eigen::Vector3d> out;
    if (!doc.contains("explode") || !doc["explode"].is_array()) return out;
    for (const json& step : doc["explode"]) {
        const std::string cid = step.value("component", std::string());
        Eigen::Vector3d vec(0, 0, 0);
        if (step.contains("vector") && step["vector"].is_array() && step["vector"].size() == 3)
            vec = Eigen::Vector3d(step["vector"][0].get<double>(), step["vector"][1].get<double>(),
                                  step["vector"][2].get<double>());
        double dist = step.value("distance", 0.0);
        double n = vec.norm();
        Eigen::Vector3d unit = n > 1e-9 ? (vec / n) : vec;
        out[cid] = unit * dist * factor;
    }
    return out;
}

gp_Trsf placed_world_trsf(const AsmBodyOut& body,
                          const std::optional<Eigen::Vector3d>& explode_offset) {
    gp_Trsf t = trsf_from_xform16(body.transform);
    if (explode_offset && explode_offset->cwiseAbs().maxCoeff() > 1e-9) {
        gp_Trsf shift;
        shift.SetTranslation(
            gp_Vec((*explode_offset)[0], (*explode_offset)[1], (*explode_offset)[2]));
        t = shift * t;  // Pos(off) * shape (explode applied in world)
    }
    return t;
}

TopoDS_Shape placed_world_shape(const AsmBodyOut& body,
                                const std::optional<Eigen::Vector3d>& explode_offset) {
    gp_Trsf t = placed_world_trsf(body, explode_offset);
    // Rigid placement via TopLoc_Location — NO geometry copy. BRepBuilderAPI_Transform with
    // copy=true minted fresh TShapes for every placement, so OCCT's per-face triangulation
    // cache could never hit and the whole assembly re-meshed on every open/re-solve/explode
    // (measured 50%+ of assembly-open time in BRepMesh). Mate transforms are rigid, so a
    // Moved() location is exact; anything non-rigid (scale/mirror) keeps the copying path.
    if (std::abs(t.ScaleFactor() - 1.0) < 1e-12 && !t.IsNegative())
        return body.shape_local.Moved(TopLoc_Location(t));
    BRepBuilderAPI_Transform xf(body.shape_local, t, true);
    return xf.Shape();
}

}  // namespace chudware::doc
