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
#include <cstdint>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <future>
#include <map>
#include <mutex>
#include <set>
#include <thread>

#include <TopLoc_Location.hxx>
#include <Standard_Failure.hxx>

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
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <gp_Circ.hxx>
#include <gp_Cone.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Sphere.hxx>
#include <gp_Trsf.hxx>
#include <gp_Pnt.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS_Iterator.hxx>

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
    mutable std::once_flag faces_once, edges_once, verts_once;
    mutable std::vector<TopoInfo> faces;          // populated only for mate resolution
    mutable std::vector<TopoInfo> edges;
    mutable std::vector<TopoInfo> verts;
    mutable std::vector<TopoDS_Shape> face_shapes;
    mutable std::vector<TopoDS_Shape> edge_shapes;
    std::vector<aicm::AppearanceRegion> appearance_regions;  // compound face IDs, not bodies
    std::vector<std::string> body_material_keys;  // per body; "" = no material assigned
    std::string error;                          // incomplete source; surviving geometry retained
    nlohmann::json build_state;                 // write-back report when compiled from features
};

// Keyed by resolved path + exact file identity (+ current descendants for native files).
// Shared futures single-flight top-level loads; recursive loads never wait on an
// unfinished future, since its owner may itself be waiting on this branch.
std::mutex g_cache_mtx;
struct LoadCacheEntry {
    std::string path;
    std::shared_future<std::shared_ptr<const LoadedComponent>> body;
    std::uint64_t inserted_at;
    std::uint64_t last_used;
};
std::map<std::string, LoadCacheEntry> g_load_cache;
std::uint64_t g_load_cache_clock = 0;  // protected by g_cache_mtx
constexpr std::size_t kComponentCacheLimit = 128;

// Caller holds g_cache_mtx. Keep pending single-flight loads until their
// promises settle, even if a newer file stamp has replaced their path.
void trim_load_cache(const std::string& path) {
    std::uint64_t newest = 0;
    for (const auto& item : g_load_cache)
        if (item.second.path == path) newest = std::max(newest, item.second.inserted_at);
    for (auto it = g_load_cache.begin(); it != g_load_cache.end();) {
        if (it->second.path == path && it->second.inserted_at != newest &&
            it->second.body.wait_for(std::chrono::seconds(0)) == std::future_status::ready)
            it = g_load_cache.erase(it);
        else
            ++it;
    }
    while (g_load_cache.size() > kComponentCacheLimit) {
        auto oldest = g_load_cache.end();
        for (auto it = g_load_cache.begin(); it != g_load_cache.end(); ++it) {
            if (it->second.body.wait_for(std::chrono::seconds(0)) != std::future_status::ready)
                continue;
            if (oldest == g_load_cache.end() || it->second.last_used < oldest->second.last_used)
                oldest = it;
        }
        if (oldest == g_load_cache.end()) break;
        g_load_cache.erase(oldest);
    }
}

std::string resolve_source_path(const std::string& path, const std::string& base_dir) {
    if (native_policy::rooted()) return native_policy::resolve_reference(path, base_dir);
    std::filesystem::path p(path);
    if (p.is_absolute()) return p.string();
    if (!base_dir.empty())
        return (std::filesystem::path(base_dir) / p).lexically_normal().string();
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

// Embedded imports are self-contained. Legacy imports read source_path directly
// (relative to the process directory, matching compile_document, not the container).
std::vector<std::string> external_import_paths(const json& doc) {
    std::vector<std::string> paths;
    if (!doc.contains("features") || !doc["features"].is_array()) return paths;
    const json* features = &doc["features"];
    const auto* config = resolve_active_config(doc).second;
    json configured;
    if (config && ((config->contains("param_overrides") && !(*config)["param_overrides"].empty()) ||
                   (config->contains("suppressions") && !(*config)["suppressions"].empty()))) {
        configured = config_effective_features(*features, config);
        features = &configured;
    }
    for (const auto& feature : *features) {
        if (!feature.is_object() || !feature.contains("type") || feature["type"] != "import")
            continue;
        const auto& params = feature.contains("params") && feature["params"].is_object()
                                 ? feature["params"] : feature;
        if ((feature.contains("suppressed") && feature["suppressed"] == true) ||
            (params.contains("suppressed") && params["suppressed"] == true)) continue;
        const auto field = [&](const char* key) -> const json* {
            auto it = params.find(key);
            if (it != params.end()) return &*it;
            it = feature.find(key);
            return it == feature.end() ? nullptr : &*it;
        };
        const auto* embedded = field("embedded_brep");
        if (embedded && embedded->is_string() &&
            !embedded->get_ref<const std::string&>().empty()) continue;
        const auto* path = field("source_path");
        if (path && path->is_string() && !path->get_ref<const std::string&>().empty())
            paths.push_back(path->get_ref<const std::string&>());
    }
    return paths;
}

// The recursion stack crosses synchronous nested assembly loads. Check it before
// consulting shared cache entries, including entries completed by a different root.
thread_local std::vector<std::string> loading_paths;

void append_load_error(std::string& error, const std::string& detail) {
    if (!error.empty()) error += "; ";
    error += detail;
}

// Load local-frame solids for a component file (port of assembly.py::_load_bodies).
std::vector<TopoDS_Shape> load_bodies(
    const std::string& abspath,
    std::vector<std::vector<aicm::AppearanceRegion>>& appearances,
    std::vector<std::string>& material_keys, std::string& error, json& build_state) {
    const std::string canonical = std::filesystem::weakly_canonical(abspath).string();
    if (std::find(loading_paths.begin(), loading_paths.end(), canonical) != loading_paths.end())
        throw OpError("import_failed", "cyclic component reference: " + canonical,
                      {{"path", canonical}});
    loading_paths.push_back(canonical);
    struct PopPath { ~PopPath() { loading_paths.pop_back(); } } pop_path;
    const std::string low = lower(abspath);
    if (ends_with(low, ".step+") || ends_with(low, ".stepplus")) {
        // Stamped before the read: a write-back of this compile must find the file unchanged.
        const NativeFileStamp stamp = stepplus_file_stamp(abspath);
        StepPlusDoc loaded = stepplus_open_component(abspath);  // throws on a bad file
        if (loaded.doc.value("schema", std::string()) == "aicad.asm/1") {
            AssemblyResult ar = compile_assembly(
                loaded.doc, std::filesystem::path(abspath).parent_path().string());
            for (const AsmComponentStatus& st : ar.component_status)
                if (!st.ok)
                    append_load_error(error, "component '" + st.id + "' in '" + abspath +
                                                "': " + st.error.value_or("no geometry"));
            for (const FeatureStatus& st : ar.feature_status)
                if (!st.ok)
                    append_load_error(error, "feature '" + st.id + "' in '" + abspath +
                                                "': " + st.error);
            for (const AsmMateStatus& st : ar.mate_status)
                if (st.status == "unresolved" || st.status == "conflicting") {
                    std::string detail = "mate '" + st.id + "' in '" + abspath +
                                         "' is " + st.status;
                    if (!st.reason.empty()) detail += ": " + st.reason;
                    for (const auto& ref : st.dangling_refs) detail += " " + ref;
                    append_load_error(error, detail);
                }
            if (!ar.converged)
                append_load_error(error, "assembly '" + abspath +
                                            "' did not converge; retained placements are provisional");
            std::vector<TopoDS_Shape> bodies;
            for (const AsmBodyOut& body : ar.bodies) {
                TopoDS_Shape world = placed_world_shape(body);
                TopExp_Explorer solids(world, TopAbs_SOLID);
                if (world.IsNull() || !solids.More()) {
                    append_load_error(error, "component '" + body.name + "' in '" + abspath +
                                                "' produced no solids");
                    if (world.IsNull()) continue;
                }
                appearances.push_back(body.appearance_regions);
                // A nested component is one body here: it keeps a material only when every
                // body inside it shares one.
                std::string key = body.body_material_keys.empty()
                                      ? std::string() : body.body_material_keys.front();
                for (const std::string& k : body.body_material_keys)
                    if (k != key) key.clear();
                material_keys.push_back(key);
                bodies.push_back(std::move(world));
            }
            if (bodies.empty())
                throw OpError("import_failed", error.empty()
                                  ? "assembly '" + abspath + "' produced no solids" : error,
                              {{"path", abspath}});
            return bodies;
        }
        auto resolve_appearances = [&](const std::vector<TopoDS_Shape>& bodies) {
            appearances.clear();
            appearances.reserve(bodies.size());
            material_keys.clear();
            uint32_t ordinal = 0;
            for (const auto& shape : bodies) {
                material_keys.push_back(doc_body_material_key(loaded.doc, ordinal + 1));
                TopTools_IndexedMapOfShape faces;
                TopExp::MapShapes(shape, TopAbs_FACE, faces);
                aicm::AppearanceRegion appearance;
                appearance.first_face = 1;
                appearance.face_count = static_cast<uint32_t>(faces.Extent());
                apply_doc_appearance(loaded.doc, ++ordinal, appearance.color,
                                     appearance.metallic, appearance.roughness);
                aicm::AicmBody styled;
                std::copy(appearance.color, appearance.color + 3, styled.color);
                styled.metallic = appearance.metallic;
                styled.roughness = appearance.roughness;
                apply_doc_face_appearances(loaded.doc, ordinal, shape, styled);
                if (styled.appearance_regions.empty()) appearances.push_back({appearance});
                else appearances.push_back(std::move(styled.appearance_regions));
            }
        };
        // Solved-bodies fast path: a part saved after a fully-ok rebuild embeds its compiled
        // bodies as cache.brep (BREP + triangulation) with a `solved` flag in the cache.json
        // header. When the engine key + doc hash still match, those bodies ARE the exact
        // compile_document output — load them directly and skip the rebuild AND the re-mesh.
        // Any mismatch/corruption falls through to the full compile.
        const bool self_contained = external_import_paths(loaded.doc).empty();
        if (self_contained) {
            const bool verified = stepplus_cache_verified(loaded.doc, loaded.extra, true);
            auto cj = loaded.extra.find("cache.json");
            auto cb = loaded.extra.find("cache.brep");
            if (cj != loaded.extra.end() && cb != loaded.extra.end() &&
                (verified || !native_policy::recomputing_cache)) {
                try {
                    json header = json::parse(cj->second);
                    if (header.value("engine", std::string()) == stepplus_cache_engine_key() &&
                        header.value("doc_hash", std::string()) ==
                            stepplus_doc_hash(loaded.doc) &&
                        header.value("solved", false)) {
                        std::vector<TopoDS_Shape> bodies = brep_cache_decode(cb->second);
                        if (!bodies.empty()) {
                            build_state = {{"cache_trust", verified ? "verified" : "unverified"}};
                            resolve_appearances(bodies);
                            return bodies;
                        }
                    }
                } catch (...) { /* stale/corrupt cache -> full compile below */ }
            }
        }
        // A self-contained part compiles like a part session and saves the build into its
        // file (stepplus.md §17): the saved state was stale, and every later load would pay
        // this rebuild again. Legacy file-backed imports never serve from saved state.
        CompileResult r = self_contained
            ? compile_component_with_build_state(abspath, loaded.doc, stamp, build_state)
            : compile_document(loaded.doc, env_from_doc(loaded.doc), nullptr,
                               ::geom::DisplayQuality::Fine, false);
        for (const FeatureStatus& st : r.statuses)
            if (!st.ok)
                append_load_error(error, "feature '" + st.id + "' in '" + abspath +
                                            "' failed to rebuild: " + st.error);
        if (r.shapes.empty())
            throw OpError("import_failed", error.empty()
                              ? "component '" + abspath + "' produced no bodies" : error,
                          {{"path", abspath}});
        resolve_appearances(r.shapes);
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
    material_keys.assign(bodies.size(), std::string());
    return bodies;
}

TopoDS_Shape compound_of(const std::vector<TopoDS_Shape>& bodies);  // defined below

// Build the stable local compound and its appearance-face identity map. Analytic
// descriptions are deferred until a mate selector needs them.
std::shared_ptr<const LoadedComponent> describe_component(
    std::vector<TopoDS_Shape> bodies,
    const std::vector<std::vector<aicm::AppearanceRegion>>& appearances,
    std::vector<std::string> material_keys, std::string error, json build_state) {
    auto result = std::make_shared<LoadedComponent>();
    LoadedComponent& lc = *result;
    lc.bodies = std::move(bodies);
    lc.body_material_keys = std::move(material_keys);
    lc.body_material_keys.resize(lc.bodies.size());
    lc.error = std::move(error);
    lc.build_state = std::move(build_state);
    lc.compound = compound_of(lc.bodies);
    // Use the tessellator's unique face map, not explorer counts: shared topology must
    // retain exactly the same IDs whether a component is styled, repeated or nested.
    TopTools_IndexedMapOfShape compound_faces;
    TopExp::MapShapes(lc.compound, TopAbs_FACE, compound_faces);
    uint32_t assigned_faces = 0;
    for (size_t bi = 0; bi < lc.bodies.size(); ++bi) {
        TopTools_IndexedMapOfShape faces;
        TopExp::MapShapes(lc.bodies[bi], TopAbs_FACE, faces);
        size_t region_index = 0;
        for (int fi = 1; fi <= faces.Extent(); ++fi) {
            const uint32_t compound_id =
                static_cast<uint32_t>(compound_faces.FindIndex(faces.FindKey(fi)));
            if (compound_id <= assigned_faces) continue;  // already-owned shared face
            aicm::AppearanceRegion appearance;
            if (bi < appearances.size()) {
                const auto& regions = appearances[bi];
                while (region_index < regions.size() &&
                       regions[region_index].first_face + regions[region_index].face_count <=
                           static_cast<uint32_t>(fi))
                    ++region_index;
                if (region_index < regions.size() &&
                    regions[region_index].first_face <= static_cast<uint32_t>(fi))
                    appearance = regions[region_index];
            }
            appearance.first_face = compound_id;
            appearance.face_count = 1;
            if (!lc.appearance_regions.empty()) {
                auto& previous = lc.appearance_regions.back();
                if (previous.first_face + previous.face_count == compound_id &&
                    std::equal(std::begin(previous.color), std::end(previous.color),
                               std::begin(appearance.color)) &&
                    previous.metallic == appearance.metallic &&
                    previous.roughness == appearance.roughness) {
                    ++previous.face_count;
                    assigned_faces = compound_id;
                    continue;
                }
            }
            lc.appearance_regions.push_back(appearance);
            assigned_faces = compound_id;
        }
    }
    return result;
}

// Analytic face/edge/vertex descriptions are expensive and have no display use.
// Repeated components share this once-only work only when a mate actually needs it.
void ensure_component_topology(const LoadedComponent& lc, const std::string& kind) {
    // A face mate never needs edge/vertex descriptions. Keep each kind once-only
    // independently so later edits can request additional kinds without rebuilding.
    if (kind == "face") std::call_once(lc.faces_once, [&] {
        // call_once permits retry after an exception: discard partial descriptions.
        lc.faces.clear(); lc.face_shapes.clear();
        int index = 0;
        for (const TopoDS_Shape& bo : lc.bodies) {
            auto faces = describe_faces(bo);
            for (auto& info : faces) { info.index = index; info.hint_index = index; ++index; }
            for (TopExp_Explorer ex(bo, TopAbs_FACE); ex.More(); ex.Next())
                lc.face_shapes.push_back(ex.Current());
            lc.faces.insert(lc.faces.end(), faces.begin(), faces.end());
        }
    });
    if (kind == "edge") std::call_once(lc.edges_once, [&] {
        lc.edges.clear(); lc.edge_shapes.clear();
        int index = 0;
        for (const TopoDS_Shape& bo : lc.bodies) {
            auto edges = describe_edges(bo);
            for (auto& info : edges) { info.index = index; info.hint_index = index; ++index; }
            for (TopExp_Explorer ex(bo, TopAbs_EDGE); ex.More(); ex.Next())
                lc.edge_shapes.push_back(ex.Current());
            lc.edges.insert(lc.edges.end(), edges.begin(), edges.end());
        }
    });
    if (kind == "vertex") std::call_once(lc.verts_once, [&] {
        lc.verts.clear();
        int index = 0;
        for (const TopoDS_Shape& bo : lc.bodies) {
            auto verts = describe_vertices(bo);
            for (auto& info : verts) { info.index = index; info.hint_index = index; ++index; }
            lc.verts.insert(lc.verts.end(), verts.begin(), verts.end());
        }
    });
}

std::string component_file_stamp(const std::string& abspath) {
    std::error_code ec;
    const auto time = std::filesystem::last_write_time(abspath, ec);
    // Explicit widths: file_time_type's rep and uintmax_t are `long long`/`unsigned long long`
    // on libc++ but not always the exact types std::to_string overloads on (ambiguous there).
    const long long mtime = ec ? 0 : static_cast<long long>(time.time_since_epoch().count());
    const auto bytes = std::filesystem::file_size(abspath, ec);
    return std::to_string(mtime) + ":" +
           std::to_string(ec ? 0ULL : static_cast<unsigned long long>(bytes));
}

// Cache only the parsed source, never a traversal result: every fingerprint request
// still stats every descendant, so edits below an unchanged assembly invalidate it.
std::shared_ptr<const json> component_document(const std::string& path) {
    struct Entry { std::string stamp; std::shared_ptr<const json> doc; std::uint64_t last_used; };
    static std::map<std::string, Entry> cache;
    static std::uint64_t clock = 0;  // protected by g_cache_mtx
    const std::string stamp = component_file_stamp(path);
    {
        std::lock_guard<std::mutex> lock(g_cache_mtx);
        auto it = cache.find(path);
        if (it != cache.end() && it->second.stamp == stamp) {
            it->second.last_used = ++clock;
            return it->second.doc;
        }
    }
    auto doc = std::make_shared<const json>(stepplus_read_document(path));
    if (component_file_stamp(path) == stamp) {
        std::lock_guard<std::mutex> lock(g_cache_mtx);
        cache[path] = {stamp, doc, ++clock};  // replaces the superseded stamp
        if (cache.size() > kComponentCacheLimit) {
            auto oldest = std::min_element(cache.begin(), cache.end(),
                [](const auto& a, const auto& b) {
                    return a.second.last_used < b.second.last_used;
                });
            cache.erase(oldest);
        }
    }
    return doc;
}

// stepplus_doc_hash of a component's document, memoized per file stamp: fingerprints
// carry it so a rewrite that keeps the document (a build-state refresh) still matches.
std::string component_document_hash(const std::string& path, const json& doc) {
    struct Entry { std::string stamp, hash; };
    static std::map<std::string, Entry> cache;  // protected by g_cache_mtx
    const std::string stamp = component_file_stamp(path);
    {
        std::lock_guard<std::mutex> lock(g_cache_mtx);
        auto it = cache.find(path);
        if (it != cache.end() && it->second.stamp == stamp) return it->second.hash;
    }
    std::string hash = stepplus_doc_hash(doc);
    if (component_file_stamp(path) == stamp) {
        std::lock_guard<std::mutex> lock(g_cache_mtx);
        if (cache.size() >= kComponentCacheLimit) cache.erase(cache.begin());
        cache[path] = {stamp, hash};
    }
    return hash;
}

std::string component_cache_key(const std::string& abspath) {
    std::string key = abspath + ":" + component_file_stamp(abspath);
    if (native_policy::recomputing_cache) key += ":verified-rebuild";
    const std::string low = lower(abspath);
    if (ends_with(low, ".step+") || ends_with(low, ".stepplus")) {
        const auto doc = component_document(abspath);
        if (doc->value("schema", std::string()) == "aicad.asm/1" ||
            !external_import_paths(*doc).empty())
            key += ":" + asm_component_fingerprints(
                *doc, std::filesystem::path(abspath).parent_path().string()).dump();
    }
    return key;
}

std::shared_ptr<const LoadedComponent> load_component(const std::string& path,
                                                     const std::string& base_dir) {
    // A relative desktop source needs a document/project base. In a rooted cell
    // the configured workspace supplies that base without consulting process cwd.
    if (std::filesystem::path(path).is_relative() && base_dir.empty() && !native_policy::rooted())
        throw OpError("component_source_unresolved",
                      "component source '" + path + "' is a relative path. The assembly has "
                      "no file and no project folder to resolve it. Give an absolute path, "
                      "or save the assembly first.",
                      {{"path", path}, {"reason", "no base folder"}});
    // Preserve the caller's directory when resolving a symlinked assembly's
    // relative children. Canonical identity is only for cycle detection.
    const std::string abspath = std::filesystem::absolute(
        resolve_source_path(path, base_dir)).string();
    if (!std::filesystem::exists(abspath)) {
        std::string message = "component source file does not exist: " + abspath + ".";
        if (std::filesystem::path(path).is_relative())
            message += " The stored path '" + path + "' is relative to the base folder '" +
                       base_dir + "'.";
        message += " Give the absolute path of an existing file.";
        throw OpError("component_source_missing", message,
                      {{"path", path}, {"resolved", abspath},
                       {"base", base_dir.empty() ? json(nullptr) : json(base_dir)}});
    }
    const std::string canonical = std::filesystem::weakly_canonical(abspath).string();
    if (std::find(loading_paths.begin(), loading_paths.end(), canonical) != loading_paths.end())
        throw OpError("import_failed", "cyclic component reference: " + canonical,
                      {{"path", canonical}});
    const std::string key = component_cache_key(abspath);
    std::shared_future<std::shared_ptr<const LoadedComponent>> fut;
    std::shared_ptr<std::promise<std::shared_ptr<const LoadedComponent>>> mine;
    std::uint64_t mine_age = 0;
    {
        std::lock_guard<std::mutex> lk(g_cache_mtx);
        auto it = g_load_cache.find(key);
        if (it != g_load_cache.end()) {
            fut = it->second.body;
            it->second.last_used = ++g_load_cache_clock;
        } else {
            mine = std::make_shared<std::promise<std::shared_ptr<const LoadedComponent>>>();
            fut = mine->get_future().share();
            mine_age = ++g_load_cache_clock;
            g_load_cache.emplace(key, LoadCacheEntry{abspath, fut, mine_age, mine_age});
            trim_load_cache(abspath);
        }
    }
    auto load = [&] {
        std::vector<std::vector<aicm::AppearanceRegion>> appearances;
        std::vector<std::string> material_keys;
        std::string error;
        json build_state;
        auto bodies = load_bodies(abspath, appearances, material_keys, error, build_state);
        return describe_component(std::move(bodies), appearances, std::move(material_keys),
                                  std::move(error), std::move(build_state));
    };
    // A→B and B→A can be owned by different prefetch workers. An unfinished
    // recursive hit must load locally (the path stack reports the eventual cycle),
    // not wait. Finished values, including leaf parts, are safe to share everywhere.
    if (!mine && !loading_paths.empty() &&
        fut.wait_for(std::chrono::seconds(0)) != std::future_status::ready)
        return load();
    if (mine) {
        try {
            auto lc = load();
            const bool cacheable = lc->error.empty() && component_cache_key(abspath) == key;
            mine->set_value(lc);
            std::lock_guard<std::mutex> lk(g_cache_mtx);
            if (!cacheable) {
                auto it = g_load_cache.find(key);
                if (it != g_load_cache.end() && it->second.inserted_at == mine_age)
                    g_load_cache.erase(it);
            }
            trim_load_cache(abspath);
        } catch (...) {
            mine->set_exception(std::current_exception());
            std::lock_guard<std::mutex> lk(g_cache_mtx);
            auto it = g_load_cache.find(key);
            if (it != g_load_cache.end() && it->second.inserted_at == mine_age)
                g_load_cache.erase(it);
            trim_load_cache(abspath);
        }
    }
    return fut.get();
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

// Resolve a selector against a component's LOCAL geometry into a MateGeom (port of
// assembly.py::_selector_to_mategeom). Returns false (out-param unset) on a dangle.
bool selector_to_mategeom(const json& selector, const LoadedComponent& lc,
                          chudware::mate::MateGeom& out) {
    const std::string kind = selector.value("kind", std::string());
    ensure_component_topology(lc, kind);
    double diag = bbox_diag(lc.bodies);

    if (kind == "face") {
        ResolveResult rr = resolve(selector, lc.faces, diag);
        if (!rr.index) return false;
        const TopoInfo& info = lc.faces[*rr.index];
        const TopoDS_Shape& fs = lc.face_shapes[*rr.index];
        const std::string surf = info.surf ? *info.surf : std::string();
        if (surf == "plane") {
            if (!info.axis) return false;
            out.kind = "plane";
            out.point = info.centroid;
            out.normal = *info.axis;
            return true;
        }
        if (surf == "cylinder" || surf == "cone") {
            try {
                BRepAdaptor_Surface ad(TopoDS::Face(fs));
                gp_Ax1 axis;
                double radius;
                if (surf == "cylinder" && ad.GetType() == GeomAbs_Cylinder) {
                    const gp_Cylinder cylinder = ad.Cylinder();
                    axis = cylinder.Axis();
                    radius = cylinder.Radius();
                } else if (surf == "cone" && ad.GetType() == GeomAbs_Cone) {
                    const gp_Cone cone = ad.Cone();
                    axis = cone.Axis();
                    radius = cone.RefRadius();
                } else {
                    return false;
                }
                const gp_Pnt& origin = axis.Location();
                const gp_Dir& direction = axis.Direction();
                const Eigen::Vector3d p(origin.X(), origin.Y(), origin.Z());
                const Eigen::Vector3d d(direction.X(), direction.Y(), direction.Z());
                out.kind = "axis";
                // Trimming displaces the area centroid radially away from the bore.
                // Retain its axial station (for hinge/distance mates), not that offset.
                out.point = p + d * (info.centroid - p).dot(d);
                out.direction = d;
                out.radius = radius;
                return true;
            } catch (const Standard_Failure&) {
                return false;
            }
        }
        if (surf == "sphere") {
            try {
                BRepAdaptor_Surface ad(TopoDS::Face(fs));
                if (ad.GetType() != GeomAbs_Sphere) return false;
                const gp_Sphere sphere = ad.Sphere();
                const gp_Pnt& center = sphere.Location();
                out.kind = "sphere";
                out.point = Eigen::Vector3d(center.X(), center.Y(), center.Z());
                out.radius = sphere.Radius();
                return true;
            } catch (const Standard_Failure&) {
                return false;
            }
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
            try {
                BRepAdaptor_Curve ad(TopoDS::Edge(es));
                if (ad.GetType() != GeomAbs_Circle) return false;
                const gp_Circ circle = ad.Circle();
                const gp_Pnt& center = circle.Location();
                const gp_Dir& direction = circle.Axis().Direction();
                out.kind = "axis";
                out.point = Eigen::Vector3d(center.X(), center.Y(), center.Z());
                out.direction = Eigen::Vector3d(direction.X(), direction.Y(), direction.Z());
                out.radius = circle.Radius();
                return true;
            } catch (const Standard_Failure&) {
                return false;
            }
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
// `placed` is the solved component bodies; returns ONLY the new instance bodies.
// Failures are reported per authored feature without discarding surviving instances.
std::vector<AsmBodyOut> compile_component_patterns(
    const json& doc, const std::vector<AsmBodyOut>& placed,
    std::vector<FeatureStatus>& statuses) {
    std::map<std::string, const AsmBodyOut*> by_cid;
    for (const AsmBodyOut& b : placed) by_cid[b.component_id] = &b;

    std::vector<AsmBodyOut> instances;
    if (!doc.contains("features") || !doc["features"].is_array()) return instances;
    int body_id = kPatternBodyBase;

    for (const json& feat : doc["features"]) {
        if (!feat.is_object() || feat.value("suppressed", false)) continue;
        const std::string ftype = feat.value("type", std::string());
        if (ftype != "asm_linear_pattern" && ftype != "asm_circular_pattern") continue;
        FeatureStatus status;
        status.id = feat.value("id", std::string());
        status.type = ftype;
        try {
        const json refs = feat.contains("refs") && feat["refs"].is_object() ? feat["refs"]
                                                                            : json::object();
        const json params = feat.contains("params") && feat["params"].is_object() ? feat["params"]
                                                                                  : json::object();
        std::vector<std::string> seed_cids;
        if (refs.contains("components") && refs["components"].is_array())
            for (const json& c : refs["components"]) {
                if (!c.is_string()) {
                    append_load_error(status.error, "pattern seed component is not an id");
                    continue;
                }
                const std::string cid = c.get<std::string>();
                if (by_cid.count(cid)) seed_cids.push_back(cid);
                else append_load_error(status.error, "pattern seed component '" + cid +
                                                         "' has no loaded geometry");
            }
        if (seed_cids.empty())
            throw OpError("invalid_ref", "pattern has no loaded seed components");

        // Build the additional instance transforms (k = 1..count-1; instance 0 = the seed).
        std::vector<Eigen::Matrix4d> offsets;
        if (ftype == "asm_linear_pattern") {
            int count1 = std::max(1, static_cast<int>(std::lround(feat_dim(params, "count1", 2))));
            double spacing1 = feat_dim(params, "spacing1", 10.0);
            Eigen::Vector3d d1;
            if (!world_axis_from_ref(refs.contains("dir1") ? refs["dir1"] : json(nullptr), d1))
                throw OpError("invalid_ref", "pattern direction dir1 is unresolved");
            for (int k = 1; k < count1; ++k) offsets.push_back(translate16(d1 * (spacing1 * k)));
        } else {  // asm_circular_pattern
            int count = std::max(1, static_cast<int>(std::lround(feat_dim(params, "count", 4))));
            double angle_total = feat_dim(params, "angle", 360.0);
            bool equal = params.value("equal_spacing", true);
            Eigen::Vector3d ax;
            if (!world_axis_from_ref(refs.contains("axis") ? refs["axis"] : json(nullptr), ax))
                throw OpError("invalid_ref", "pattern axis is unresolved");
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
                AsmBodyOut inst = *seed;
                inst.body_id = body_id++;
                inst.component_id = seed_cid + "#" + std::to_string(k);
                inst.name = seed->name + "#" + std::to_string(k);
                inst.transform = xform16_from_mat(inst_mat);
                for (auto& appearance : inst.appearance_regions)
                    apply_doc_appearance(doc, static_cast<uint32_t>(inst.body_id),
                                         appearance.color, appearance.metallic, appearance.roughness);
                if (!inst.appearance_regions.empty()) {
                    const auto& appearance = inst.appearance_regions.front();
                    std::copy(std::begin(appearance.color), std::end(appearance.color),
                              inst.color.begin());
                    inst.metallic = appearance.metallic;
                    inst.roughness = appearance.roughness;
                }
                instances.push_back(std::move(inst));
            }
        }
        } catch (const Standard_Failure& exc) {
            append_load_error(status.error, exc.GetMessageString()
                                               ? exc.GetMessageString() : "pattern kernel failure");
        } catch (const std::exception& exc) {
            append_load_error(status.error, exc.what());
        } catch (...) {
            append_load_error(status.error, "unknown pattern build failure");
        }
        status.ok = status.error.empty();
        statuses.push_back(std::move(status));
    }
    return instances;
}

// Why mate ref `i` did not resolve, named by index and component. Mirrors the wording of
// core/mate/mate.cpp unresolved_ref_reason, which names only refs[0] and refs[1]: an extra
// ref (index >= 2) that dangles made the mate unresolved with no reason (generic "mate
// references could not be resolved").
std::string dangling_ref_reason(const json& ref, int i, bool component_loaded) {
    const std::string at = "refs[" + std::to_string(i) + "]";
    const std::string cid = ref.is_object() && ref.contains("component") &&
                                    ref["component"].is_string()
        ? ref["component"].get<std::string>() : std::string();
    if (cid.empty()) return at + " names no component";
    if (!component_loaded)
        return at + " component '" + cid + "' is not in the solve. The component is "
               "missing, suppressed, or its source did not load";
    const json sel = ref.contains("selector") ? ref["selector"] : json(nullptr);
    if (!sel.is_object()) return at + " on component '" + cid + "' has no selector object";
    const std::string kind = sel.contains("kind") && sel["kind"].is_string()
        ? sel["kind"].get<std::string>() : std::string();
    const std::string sid = sel.contains("sid") && sel["sid"].is_string()
        ? sel["sid"].get<std::string>() : std::string();
    if (kind != "face" && kind != "edge" && kind != "vertex")
        return at + " on component '" + cid + "' has selector kind '" + kind +
               "'. A mate ref needs a face, edge or vertex selector";
    return at + " " + kind + (sid.empty() ? "" : " '" + sid + "'") +
           " did not resolve on component '" + cid + "'. No " + kind +
           " of that component matches the stored selector, or its surface type changed. "
           "This occurs when the component geometry changed after the mate was made. "
           "Make the mate again with asm_mate and {component, query}";
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

    std::map<std::string, std::set<std::string>> mate_components;
    for (const auto& mate : mates) {
        if (mate.value("suppressed", false) || !mate.contains("refs") || !mate["refs"].is_array())
            continue;
        for (const auto& ref : mate["refs"])
            if (ref.is_object() && ref.contains("selector") && ref["selector"].is_object())
                mate_components[ref.value("component", std::string())].insert(
                    ref["selector"].value("kind", std::string()));
    }
    // --- load every component -------------------------------------------
    // Prefetch pass: components are independent, so uncached loads (compile or cache.brep
    // decode, both CPU-bound) run in bounded top-level worker threads. Repeated
    // source paths share both successful loads and failures within this compilation.
    // Kill-switch: CHUDWARE_ASM_PARALLEL_LOAD=0.
    std::map<std::string, std::shared_ptr<const LoadedComponent>> prefetched;
    std::map<std::string, std::exception_ptr> prefetch_errors;
    {
        const char* env = std::getenv("CHUDWARE_ASM_PARALLEL_LOAD");
        const bool par = loading_paths.empty() && !(env && env[0] == '0');
        if (par && components.size() > 1) {
            std::vector<std::string> paths;
            std::set<std::string> unique_paths;
            std::map<std::string, std::set<std::string>> mate_paths;
            for (const json& comp : components) {
                const std::string p = comp.contains("source") && comp["source"].is_object()
                                          ? comp["source"].value("path", std::string())
                                          : std::string();
                if (!p.empty() && unique_paths.insert(p).second) paths.push_back(p);
                const auto needed = mate_components.find(comp.value("id", std::string()));
                if (needed != mate_components.end())
                    mate_paths[p].insert(needed->second.begin(), needed->second.end());
            }
            const unsigned hw = std::max(2u, std::thread::hardware_concurrency());
            std::vector<std::shared_ptr<const LoadedComponent>> pins(paths.size());
            std::vector<std::exception_ptr> errors(paths.size());
            const std::size_t n_workers = std::min<std::size_t>(paths.size(), hw);
            std::atomic<std::size_t> next{0};
            std::vector<std::thread> pool;
            pool.reserve(n_workers);
            for (std::size_t w = 0; w < n_workers; ++w)
                pool.emplace_back([&]() {
                    for (std::size_t i = next.fetch_add(1); i < paths.size();
                         i = next.fetch_add(1)) {
                        try {
                            auto component = load_component(paths[i], base_dir);
                            const auto needed = mate_paths.find(paths[i]);
                            if (needed != mate_paths.end())
                                for (const auto& kind : needed->second)
                                    ensure_component_topology(*component, kind);
                            pins[i] = std::move(component);
                        } catch (...) { errors[i] = std::current_exception(); }
                    }
                });
            for (std::thread& t : pool) t.join();
            for (std::size_t i = 0; i < paths.size(); ++i) {
                if (pins[i]) prefetched.emplace(paths[i], std::move(pins[i]));
                else if (errors[i]) prefetch_errors.emplace(paths[i], errors[i]);
            }
        }
    }
    std::map<std::string, std::shared_ptr<const LoadedComponent>> loaded;
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
            auto failure = prefetch_errors.find(path);
            if (failure != prefetch_errors.end()) std::rethrow_exception(failure->second);
            auto ready = prefetched.find(path);
            if (ready == prefetched.end())
                ready = prefetched.emplace(path, load_component(path, base_dir)).first;
            const auto needed = mate_components.find(cid);
            if (needed != mate_components.end())
                for (const auto& kind : needed->second)
                    ensure_component_topology(*ready->second, kind);
            loaded[cid] = ready->second;
            st.ok = loaded[cid]->error.empty();
            if (!st.ok) st.error = loaded[cid]->error;
            st.build_state = loaded[cid]->build_state;
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
    // mate_id -> (ref index, named reason) for each dangling ref
    std::map<std::string, std::vector<std::pair<int, std::string>>> dangling_why;
    for (const json& mate : mates) {
        if (mate.value("suppressed", false)) continue;
        const std::string mid = mate.value("id", std::string());
        const json refs = mate.contains("refs") && mate["refs"].is_array() ? mate["refs"]
                                                                           : json::array();
        for (int i = 0; i < static_cast<int>(refs.size()); ++i) {
            const json& ref = refs[i];
            const std::string cid = ref.is_object() ? ref.value("component", std::string())
                                                    : std::string();
            auto lit = loaded.find(cid);
            const json sel_obj = ref.is_object() && ref.contains("selector") ? ref["selector"]
                                                                            : json(nullptr);
            if (lit == loaded.end() || !sel_obj.is_object()) {
                dangling[mid].push_back("refs[" + std::to_string(i) + "]");
                dangling_why[mid].emplace_back(i, dangling_ref_reason(ref, i, lit != loaded.end()));
                continue;
            }
            chudware::mate::MateGeom geom;
            if (!selector_to_mategeom(sel_obj, *lit->second, geom)) {
                dangling[mid].push_back("refs[" + std::to_string(i) + "]");
                dangling_why[mid].emplace_back(i, dangling_ref_reason(ref, i, true));
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
        body.appearance_regions = lit->second->appearance_regions;
        body.body_material_keys = lit->second->body_material_keys;
        for (auto& appearance : body.appearance_regions)
            apply_doc_appearance(doc, static_cast<uint32_t>(ordinal), appearance.color,
                                 appearance.metallic, appearance.roughness);
        if (!body.appearance_regions.empty()) {
            const auto& appearance = body.appearance_regions.front();
            std::copy(std::begin(appearance.color), std::end(appearance.color), body.color.begin());
            body.metallic = appearance.metallic;
            body.roughness = appearance.roughness;
        }
        res.bodies.push_back(std::move(body));
    }

    // --- component patterns ---------------------------------------------
    // Expand asm_linear_pattern / asm_circular_pattern into synthetic instance bodies (NOT
    // components: excluded from the mate solve + interference/BOM). Keep surviving
    // instances and report failed authored features through their own status channel.
    std::vector<AsmBodyOut> instances =
        compile_component_patterns(doc, res.bodies, res.feature_status);
    for (AsmBodyOut& inst : instances) res.bodies.push_back(std::move(inst));

    // mate status (merge dangling + the per-mate residual report)
    std::map<std::string, double> mate_residual;
    for (const chudware::mate::MateResidual& r : solve_res.residuals)
        mate_residual[r.mate_id] = r.max_abs;
    for (const chudware::mate::MateStatus& m : solve_res.mate_status) {
        AsmMateStatus st;
        st.id = m.mate_id;
        st.type = m.type;
        st.status = m.status;
        st.reason = m.reason;
        auto rit = mate_residual.find(m.mate_id);
        if (rit != mate_residual.end()) st.residual = rit->second;
        auto dit = dangling.find(m.mate_id);
        if (dit != dangling.end()) {
            // The solver names refs[0]/refs[1] when it marks the mate unresolved. A dangling
            // extra ref (index >= 2) is known only here, so name every ref the reason lacks.
            // A solver reason for any other status no longer applies to an unresolved mate.
            const bool solver_named_pair = m.status == "unresolved" && !m.reason.empty();
            if (!solver_named_pair) st.reason.clear();
            for (const auto& [index, why] : dangling_why[m.mate_id]) {
                if (solver_named_pair && index < 2) continue;
                st.reason += (st.reason.empty() ? "" : "; ") + why;
            }
            st.status = "unresolved";
            st.dangling_refs = dit->second;
        }
        res.mate_status.push_back(std::move(st));
    }

    res.dof = solve_res.dof;
    res.over_defined = solve_res.over_defined;
    res.converged = solve_res.converged &&
        std::all_of(res.component_status.begin(), res.component_status.end(),
                    [](const AsmComponentStatus& st) { return st.ok; }) &&
        std::all_of(res.feature_status.begin(), res.feature_status.end(),
                    [](const FeatureStatus& st) { return st.ok; }) &&
        std::none_of(res.mate_status.begin(), res.mate_status.end(),
                     [](const AsmMateStatus& st) {
                         return st.status == "unresolved" || st.status == "conflicting";
                     });
    res.max_residual = solve_res.residuals.empty() ? solve_res.max_residual
                                                   : solve_res.residuals.front().max_abs;
    res.tolerance = solve_res.tolerance;
    res.worst_mate = solve_res.worst_mate;
    res.dof_summary = solve_res.summary;
    res.warm = solve_res.params;
    return res;
}

json asm_component_fingerprints(const json& doc, const std::string& base_dir) {
    json fp = json::array();
    const auto root = base_dir.empty() ? std::filesystem::path()
        : std::filesystem::absolute(base_dir).lexically_normal();
    auto append_path = [&](const std::string& source_path) {
        const auto absolute = std::filesystem::absolute(source_path).lexically_normal();
        const auto local = root.empty() ? std::filesystem::path()
            : absolute.lexically_relative(root);
        const auto first = local.begin();
        const std::string stored = !local.empty() && first != local.end() &&
            *first != ".." && *first != "." ? local.generic_string() : absolute.string();
        uint64_t size = 0;
        long long mtime = 0;
        std::error_code ec;
        auto sz = std::filesystem::file_size(absolute, ec);
        if (!ec) size = static_cast<uint64_t>(sz);
        auto ft = std::filesystem::last_write_time(absolute, ec);
        if (!ec) mtime = static_cast<long long>(ft.time_since_epoch().count());
        fp.push_back({{"path", stored}, {"size", size}, {"mtime", mtime}});
    };
    auto append_components = [&](const json& assembly, const std::string& directory) {
        if (assembly.contains("components") && assembly["components"].is_array())
        for (const json& c : assembly["components"]) {
            if (c.value("suppressed", false)) continue;
            const std::string path = c.contains("source") && c["source"].is_object()
                                         ? c["source"].value("path", std::string())
                                         : std::string();
            append_path(path.empty() ? path : resolve_source_path(path, directory));
        }
        for (const auto& path : external_import_paths(assembly))
            append_path(resolve_source_path(path, {}));
    };
    append_components(doc, base_dir);
    // Keep the original direct-component rows first, then append descendant rows in
    // breadth-first order. A child assembly's own mtime cannot fingerprint its sources.
    // A file symlink can open the same JSON in different relative-source directories.
    // Visit each canonical file + canonical parent context once; directory aliases
    // collapse, but distinct adjacent part files remain independent dependencies.
    std::set<std::pair<std::string, std::string>> visited;
    for (size_t i = 0; i < fp.size(); ++i) {
        const std::string path = resolve_source_path(
            fp[i].at("path").get<std::string>(), root.string());
        const std::string low = lower(path);
        if (!ends_with(low, ".step+") && !ends_with(low, ".stepplus")) continue;
        std::error_code ec;
        const auto canonical = std::filesystem::weakly_canonical(path, ec);
        const std::string identity = ec ? path : canonical.string();
        const auto parent = std::filesystem::weakly_canonical(
            std::filesystem::path(path).parent_path(), ec);
        const bool first_visit =
            visited.emplace(identity, ec ? std::filesystem::path(path).parent_path().string()
                                         : parent.string()).second;
        try {
            const auto child = component_document(identity);
            fp[i]["doc_hash"] = component_document_hash(identity, *child);
            if (first_visit)
                append_components(*child, std::filesystem::path(path).parent_path().string());
        } catch (...) {
            // Missing/unreadable sources retain their direct row (including zero size/mtime).
            // Compilation reports their component error; fingerprinting never hides them.
        }
    }
    return fp;
}

bool asm_fingerprints_match(const json& doc, const std::string& base_dir, const json& fp) {
    try {
        if (!fp.is_array()) return false;
        const json now = asm_component_fingerprints(doc, base_dir);
        if (now.size() != fp.size()) return false;
        for (std::size_t i = 0; i < now.size(); ++i) {
            const json& cur = now[i];
            const json& saved = fp[i];
            if (!saved.is_object() || cur.at("path") != saved.value("path", json())) return false;
            if (cur.at("size") == saved.value("size", json()) &&
                cur.at("mtime") == saved.value("mtime", json()))
                continue;
            // A rewrite that kept the document — its build state refreshed after a rebuild
            // (contracts/stepplus.md §17) — is the same source.
            const auto now_hash = cur.find("doc_hash");
            const auto saved_hash = saved.find("doc_hash");
            if (now_hash != cur.end() && saved_hash != saved.end() && *now_hash == *saved_hash)
                continue;
            return false;
        }
        return true;
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

namespace {
// A native part component compiled WITH owner attribution, for assembly pick resolution.
// The normal component load path is geometry-only (and may come straight from cache.brep),
// so it never carries owners; this is built lazily on the first pick and cached by the same
// file-stamp key as the geometry.
struct ComponentOwners {
    std::vector<TopoDS_Shape> shapes;
    std::vector<std::map<int, std::string>> faces, edges;
};
constexpr std::size_t kOwnerCacheLimit = 16;
std::mutex g_owner_mtx;
std::map<std::string, std::pair<std::uint64_t, std::shared_ptr<const ComponentOwners>>>
    g_owner_cache;
std::uint64_t g_owner_clock = 0;  // protected by g_owner_mtx

std::shared_ptr<const ComponentOwners> component_owners(const std::string& abspath) {
    const std::string key = component_cache_key(abspath);
    {
        std::lock_guard<std::mutex> lock(g_owner_mtx);
        auto it = g_owner_cache.find(key);
        if (it != g_owner_cache.end()) {
            it->second.first = ++g_owner_clock;
            return it->second.second;
        }
    }
    const auto doc = component_document(abspath);
    if (doc->value("schema", std::string()) == "aicad.asm/1") return nullptr;
    CompileResult r = compile_document(*doc, env_from_doc(*doc), nullptr,
                                       ::geom::DisplayQuality::Fine, false, /*with_owners=*/true);
    if (r.shape_face_owners.size() != r.shapes.size() ||
        r.shape_edge_owners.size() != r.shapes.size())
        return nullptr;
    auto owners = std::make_shared<ComponentOwners>();
    owners->shapes = std::move(r.shapes);
    owners->faces = std::move(r.shape_face_owners);
    owners->edges = std::move(r.shape_edge_owners);
    std::lock_guard<std::mutex> lock(g_owner_mtx);
    g_owner_cache[key] = {++g_owner_clock, owners};
    while (g_owner_cache.size() > kOwnerCacheLimit) {
        auto oldest = std::min_element(g_owner_cache.begin(), g_owner_cache.end(),
            [](const auto& a, const auto& b) { return a.second.first < b.second.first; });
        g_owner_cache.erase(oldest);
    }
    return owners;
}

// Area (face) / length (edge) + centroid: enough to confirm two shapes are the same
// face/edge of the same compiled part without trusting index order alone.
struct TopoProbe {
    double measure = 0.0;
    gp_Pnt centroid;
};
TopoProbe topo_probe(const TopoDS_Shape& s, bool face) {
    GProp_GProps g;
    if (face) BRepGProp::SurfaceProperties(s, g);
    else BRepGProp::LinearProperties(s, g);
    return {g.Mass(), g.CentreOfMass()};
}
bool same_probe(const TopoProbe& a, const TopoProbe& b, double diag) {
    return std::abs(a.measure - b.measure) <= 1e-6 * std::max(1.0, std::abs(a.measure)) &&
           a.centroid.Distance(b.centroid) <= 1e-6 * std::max(1.0, diag);
}
}  // namespace

std::string component_pick_owner(const json& asm_doc, const std::string& base_dir,
                                 const AsmBodyOut& body, const std::string& kind, int topo_id) {
    if ((kind != "face" && kind != "edge") || body.shape_local.IsNull() || topo_id < 1)
        return {};
    const bool face = kind == "face";
    const TopAbs_ShapeEnum te = face ? TopAbs_FACE : TopAbs_EDGE;
    // A pattern instance "<cid>#k" is a placed copy of its seed component.
    const std::string cid = body.component_id.substr(0, body.component_id.find('#'));
    std::string path;
    if (asm_doc.contains("components") && asm_doc["components"].is_array())
        for (const json& c : asm_doc["components"])
            if (c.is_object() && c.value("id", std::string()) == cid && c.contains("source") &&
                c["source"].is_object())
                path = c["source"].value("path", std::string());
    if (path.empty()) return {};
    try {
        const std::string abspath =
            std::filesystem::absolute(resolve_source_path(path, base_dir)).string();
        const std::string low = lower(abspath);
        if (!ends_with(low, ".step+") && !ends_with(low, ".stepplus")) return {};
        const auto owners = component_owners(abspath);
        if (!owners || owners->shapes.empty()) return {};

        TopTools_IndexedMapOfShape picked_map;
        TopExp::MapShapes(body.shape_local, te, picked_map);
        if (topo_id > picked_map.Extent()) return {};
        const TopoDS_Shape& picked = picked_map.FindKey(topo_id);
        // shape_local is compound_of(the component's bodies): the body itself for one body,
        // else a compound whose children are the bodies in compile order.
        std::vector<TopoDS_Shape> bodies;
        if (owners->shapes.size() == 1) {
            bodies.push_back(body.shape_local);
        } else {
            for (TopoDS_Iterator it(body.shape_local); it.More(); it.Next())
                bodies.push_back(it.Value());
        }
        if (bodies.size() != owners->shapes.size()) return {};
        for (std::size_t bi = 0; bi < bodies.size(); ++bi) {
            TopTools_IndexedMapOfShape local;
            TopExp::MapShapes(bodies[bi], te, local);
            const int li = local.FindIndex(picked);
            if (li == 0) continue;
            TopTools_IndexedMapOfShape compiled;
            TopExp::MapShapes(owners->shapes[bi], te, compiled);
            const double diag = bbox_diag({owners->shapes[bi]});
            const TopoProbe want = topo_probe(picked, face);
            int match = 0;
            if (li <= compiled.Extent() &&
                same_probe(want, topo_probe(compiled.FindKey(li), face), diag)) {
                match = li;
            } else {
                // Index order disagreed (e.g. a cache written by another compile): accept
                // only a UNIQUE geometric match.
                for (int j = 1; j <= compiled.Extent(); ++j) {
                    if (!same_probe(want, topo_probe(compiled.FindKey(j), face), diag)) continue;
                    if (match != 0) return {};
                    match = j;
                }
            }
            if (match == 0) return {};
            const auto& om = (face ? owners->faces : owners->edges)[bi];
            const auto it = om.find(match);
            if (it == om.end()) return {};
            return it->second.substr(0, it->second.find('#'));  // split piece -> its feature
        }
        return {};
    } catch (...) {
        return {};  // an unreadable/uncompilable component simply has no owner to report
    }
}

}  // namespace chudware::doc
