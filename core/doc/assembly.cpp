// Assembly compile + mate-solve + tessellation — see assembly.h.
//
// Mirrors engine/doc/assembly.py: load each non-suppressed component (.step+ container
// -> compile_document; bare STEP/IGES/BREP/mesh -> import_bodies), resolve each mate
// ref's selector into a local-frame MateGeom, solve the mates over the shared numeric
// core, and emit one placed AsmBodyOut per loaded component.

#include "doc/assembly.h"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <map>
#include <mutex>

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

namespace aicad::doc {

using nlohmann::json;

namespace {

const std::vector<double> kIdent16 = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};

// ---------------------------------------------------------------- component cache
// Cached load of a component source file: local-frame bodies + a topology description
// (faces/edges/verts described once, parallel to the kernel sub-shape lists).
struct LoadedComponent {
    std::vector<TopoDS_Shape> bodies;             // LOCAL frame
    std::vector<TopoInfo> faces;                  // across all bodies, flat
    std::vector<TopoInfo> edges;
    std::vector<TopoInfo> verts;
    std::vector<TopoDS_Shape> face_shapes;        // parallel kernel faces (for adaptor radii)
    std::vector<TopoDS_Shape> edge_shapes;
};

std::mutex g_cache_mtx;
std::map<std::string, LoadedComponent> g_load_cache;

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
                double v = aicad::util::evaluate(expr, acc);
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

// Describe every face/edge/vertex of the component's bodies (local frame) for mate
// resolution (port of assembly.py::_describe_component). Flat across all bodies in
// body order; indices re-numbered contiguously.
LoadedComponent describe_component(std::vector<TopoDS_Shape> bodies) {
    LoadedComponent lc;
    lc.bodies = std::move(bodies);
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

const LoadedComponent& load_component(const std::string& path, const std::string& base_dir) {
    std::string abspath = resolve_source_path(path, base_dir);
    double mtime = 0.0;
    std::error_code ec;
    auto ftime = std::filesystem::last_write_time(abspath, ec);
    if (!ec) mtime = static_cast<double>(ftime.time_since_epoch().count());
    std::string key = abspath + ":" + std::to_string(static_cast<long long>(mtime));

    std::lock_guard<std::mutex> lk(g_cache_mtx);
    auto it = g_load_cache.find(key);
    if (it != g_load_cache.end()) return it->second;
    LoadedComponent lc = describe_component(load_bodies(abspath));
    return g_load_cache[key] = std::move(lc);
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
                          aicad::mate::MateGeom& out) {
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

}  // namespace

void clear_component_cache() {
    std::lock_guard<std::mutex> lk(g_cache_mtx);
    g_load_cache.clear();
}

AssemblyResult compile_assembly(const json& doc, const std::string& base_dir,
                                const std::optional<aicad::mate::WarmMap>& warm,
                                const std::optional<aicad::mate::Drag>& drag) {
    AssemblyResult res;

    // non-suppressed components, in tree order
    std::vector<json> components;
    if (doc.contains("components") && doc["components"].is_array())
        for (const json& c : doc["components"])
            if (!c.value("suppressed", false)) components.push_back(c);
    const json mates = (doc.contains("mates") && doc["mates"].is_array())
                           ? doc["mates"] : json::array();

    // --- load every component -------------------------------------------
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
    aicad::mate::ResolvedGeometry resolved_geometry;
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
            aicad::mate::MateGeom geom;
            if (!selector_to_mategeom(sel_obj, *lit->second, geom)) {
                dangling[mid].push_back("refs[" + std::to_string(i) + "]");
                continue;
            }
            resolved_geometry[{mid, i}] = geom;
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
    aicad::mate::MateSolveResult solve_res =
        aicad::mate::solve_mates(solve_components, mates, resolved_geometry, warm, drag);

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
        body.shape_local = compound_of(lit->second->bodies);
        body.transform = xform;
        body.color = component_color(ordinal - 1);
        res.bodies.push_back(std::move(body));
    }

    // mate status (merge dangling)
    for (const aicad::mate::MateStatus& m : solve_res.mate_status) {
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

TopoDS_Shape placed_world_shape(const AsmBodyOut& body,
                                const std::optional<Eigen::Vector3d>& explode_offset) {
    gp_Trsf t = trsf_from_xform16(body.transform);
    if (explode_offset && explode_offset->cwiseAbs().maxCoeff() > 1e-9) {
        gp_Trsf shift;
        shift.SetTranslation(gp_Vec((*explode_offset)[0], (*explode_offset)[1], (*explode_offset)[2]));
        t = shift * t;  // Pos(off) * shape (explode applied in world)
    }
    BRepBuilderAPI_Transform xf(body.shape_local, t, true);
    return xf.Shape();
}

}  // namespace aicad::doc
