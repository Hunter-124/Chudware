// Document model implementation — see document.h for the design rationale (the
// simplified id-ref model vs. Python selectors, and the never-raise contract).
//
// The rebuild is a single linear pass over the feature list. Each feature reads
// its inputs from a results map keyed by feature id, compiles via the geom kernel,
// and stores its own result back under its id. Two result KINDS are tracked:
//   - SKETCH : a std::vector<TopoDS_Face> + the SketchPlane it was solved on
//              (so extrude/revolve can default the prism direction to the plane
//              normal). A sketch is NOT a body — it never tessellates on its own.
//   - SOLID  : a TopoDS_Shape body, the unit booleans/dress/extrude consume & make.
// A `consumed` set records solids that were merged/subtracted/replaced by a later
// feature (boolean inputs, the fillet/chamfer source) — those drop out of the
// surviving-body set, exactly as a SolidWorks feature tree shows only the current
// state of the solid.
//
// Every per-feature compile is wrapped in try/catch so an OCCT throw degrades to an
// error status, never a crash — the compile_document never-raises contract.

#include "doc/document.h"
#include "doc/native_policy.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <functional>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <variant>
#include <vector>

#include <Eigen/Dense>

#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepBndLib.hxx>
#include <BRepCheck_Analyzer.hxx>  // sweep self-intersection validity check (Bug B3)
#include <BRepCheck_ListOfStatus.hxx>  // heal: name the statuses ShapeFix cleared
#include <BRepCheck_Result.hxx>
#include <BRepCheck_Status.hxx>
#include <BOPAlgo_CheckerSI.hxx>
#include <BOPDS_DS.hxx>
#include <BOPDS_MapOfPair.hxx>
#include <BOPDS_Pair.hxx>
#include <Message_ProgressIndicator.hxx>
#include <Message_ProgressScope.hxx>
#include <BRepGProp.hxx>
#include <BRepClass_FaceClassifier.hxx>  // hole world_points: does the bore axis hit the face?
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>  // thread draft-preview band tool
#include <BRepAlgoAPI_Cut.hxx>           // thread draft-preview band boolean
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepTools.hxx>
#include <BRepTools_History.hxx>
#include <BinTools.hxx>                // cache.ckpt: one shared-topology shape set
#include <BinTools_FormatVersion.hxx>
#include <BinTools_ShapeSet.hxx>
#include <TopLoc_Location.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>  // seam-aware open-shell test (S2)
#include <TopTools_ListOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopoDS_Vertex.hxx>
#include <Bnd_Box.hxx>
#include <Precision.hxx>
#include <GCPnts_TangentialDeflection.hxx>
#include <Standard_Failure.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopAbs.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Solid.hxx>  // ShapeFix_Solid heal of an inverted body (Bug N2)
#include <gp_Ax1.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Trsf.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>
#include <Geom_Surface.hxx>
#include <Geom_Curve.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <GeomAPI_ProjectPointOnSurf.hxx>
#include <GeomAPI_ProjectPointOnCurve.hxx>
#include <GeomLProp_SLProps.hxx>
#include <ShapeAnalysis.hxx>  // FindBounds: free ends of an open profile wire (revolve auto-close)
#include <ShapeFix_Shape.hxx>  // the `heal` feature (ShapeFix repair of an imported body)
#include <ShapeFix_Solid.hxx>  // heal an inverted/negative-volume solid (Bug N2) + `heal`
#include <ShapeFix_Wireframe.hxx>  // `heal` fix_small_edges: drop tiny edges / close wire gaps
#include <BOPAlgo_MakerVolume.hxx>  // `heal` rebuild_solid: re-derive the solid from its faces
#include <BRepBuilderAPI_MakeSolid.hxx>  // `heal` sew/orientation: shell -> solid
#include <BRepBuilderAPI_Sewing.hxx>  // `heal` sew_tolerance_mm: re-derive edge sharing
#include <BRepLib.hxx>  // `heal` fix_orientation: BRepLib::OrientClosedSolid
#include <ShapeUpgrade_UnifySameDomain.hxx>  // `heal` unify_faces: merge same-domain faces
#include <TopTools_ListOfShape.hxx>  // BOPAlgo_MakerVolume arguments

#include "doc/formats.h"  // import_bodies / detect_format (the import feature)
#include "doc/picks.h"
#include "doc/selectors.h"
#include "doc/session.h"  // OpError (plan_push_pull's structured out-of-range errors)
#include "geom/booleans.h"
#include "geom/dress.h"
#include "geom/features.h"
#include "geom/op_history.h"
#include "geom/hole.h"
#include "geom/plane.h"
#include "geom/face_normal.h"
#include "geom/profile.h"
#include "geom/sheetmetal.h"
#include "geom/weldment.h"
#include "geom/surfacing.h"
#include "geom/curves.h"
#include "geom/thread.h"
#include "geom/text.h"
#include "geom/tessellate.h"
#include "geom/transform.h"
#include "geom/aicm.h"
#include "sketch/sketch.h"
#include "sketch/sketch3d.h"
#include "geom/profile3d.h"
#include "util/expr.h"

namespace chudware::doc {

using nlohmann::json;
using EnvMap = std::map<std::string, double>;

namespace {

constexpr double TWO_PI = 6.28318530717958647692;
constexpr double PI = 3.14159265358979323846;

// Degrees -> radians. The circular_pattern angle is in DEGREES (SolidWorks +
// pattern.py convention), so its per-step rotation is converted here before the
// kernel's rotate_copy (which, like revolve_face, takes radians).
inline double radians(double deg) { return deg * PI / 180.0; }

// -------------------------------------------------------------------- result kinds
// A sketch result: the planar faces solved for the sketch, plus the plane they live
// on (downstream extrude/revolve default their direction to the plane normal).
struct SketchResult {
    std::vector<TopoDS_Face> faces;
    chudware::geom::SketchPlane plane;
    nlohmann::json entities;  // solved entities (for a sweep path / rib trace wire)
};

// A solid-body result.
struct SolidResult {
    TopoDS_Shape shape;
};

// A datum-plane result: a SketchPlane a later sketch can be built on (e.g. an
// offset plane for a loft section). Never tessellates; not a body. Also carries a
// `csys` flag so a csys datum (which is geometrically a Plane) round-trips its kind.
struct PlaneResult {
    chudware::geom::SketchPlane plane;
    bool is_csys = false;
};

// A datum-axis result: (point on axis, unit direction). Consumed by a downstream
// revolve / circular_pattern. Port of engine/doc/features/datum.py compile_axis.
struct AxisResult {
    Eigen::Vector3d point;
    Eigen::Vector3d dir;  // unit
};

// A datum-point result: a 3-D world point. Consumed by three_point / two_points /
// parallel_point datums. Port of engine/doc/features/datum.py compile_point.
struct PointResult {
    Eigen::Vector3d pt;
};

// A curve result: a reference WIRE (a helix / spiral) in a LOCAL frame plus the base
// plane it is lifted through. Consumed by a downstream sweep as its path (mirrors the
// _sketch_regions path contract). Never a body. Port of curves.py.
struct CurveResult {
    TopoDS_Wire wire;                 // local-frame curve
    chudware::geom::SketchPlane base;    // lift frame
};

// A 3D free-space sketch result: the solved entities chained into WORLD-space
// wires (open or closed). Never a body; consumed by a downstream sweep as its
// spine/path or guide curve (and by a future loft-guides pass). `wires` is
// deterministically ordered (wire 0 = the chain containing the earliest-authored
// entity — see geom::wires_from_sketch3d).
struct Sketch3DResult {
    std::vector<TopoDS_Wire> wires;
    nlohmann::json entities;  // solved entities (id -> param dict)
};

using FeatureResult = std::variant<SketchResult, SolidResult, PlaneResult, AxisResult,
                                   PointResult, CurveResult, Sketch3DResult>;

// Shared feature-result slot. Checkpoints share unchanged entries via
// shared_ptr so capture_ckpt is O(entries) pointer traffic, not O(N²) value-copies
// of SketchResult json / shape vectors. Mutable (non-const) so sanitize_solid can
// heal an inverted solid in place before the result is frozen into a checkpoint.
using FeatureResultPtr = std::shared_ptr<FeatureResult>;

// Map wrapper: operator[] assignment always allocates a FRESH shared_ptr so a
// later put never mutates a FeatureResult still shared with a prior checkpoint.
// find/at/count match std::map so existing access sites only need to dereference
// the shared_ptr (it->second / *results.at(id)).
class FeatureResultMap {
public:
    using map_t = std::map<std::string, FeatureResultPtr>;
    using iterator = map_t::iterator;
    using const_iterator = map_t::const_iterator;

    struct Slot {
        FeatureResultPtr& ref;
        template <typename T>
        Slot& operator=(T&& v) {
            ref = std::make_shared<FeatureResult>(FeatureResult(std::forward<T>(v)));
            return *this;
        }
    };

    Slot operator[](const std::string& id) { return Slot{m_[id]}; }
    Slot operator[](std::string&& id) { return Slot{m_[std::move(id)]}; }
    // Install an existing frozen slot (persisted-cache restore shares one decoded result
    // across every checkpoint that held it, exactly as live checkpoints do).
    void put_shared(const std::string& id, FeatureResultPtr p) { m_[id] = std::move(p); }

    iterator find(const std::string& id) { return m_.find(id); }
    const_iterator find(const std::string& id) const { return m_.find(id); }
    iterator end() { return m_.end(); }
    const_iterator end() const { return m_.end(); }
    iterator begin() { return m_.begin(); }
    const_iterator begin() const { return m_.begin(); }
    std::size_t count(const std::string& id) const { return m_.count(id); }
    FeatureResultPtr& at(const std::string& id) { return m_.at(id); }
    const FeatureResultPtr& at(const std::string& id) const { return m_.at(id); }
    void clear() { m_.clear(); }
    std::size_t size() const { return m_.size(); }

private:
    map_t m_;
};

// Import-cache key: FNV-1a of the embedded_brep blob. Keying by the
// multi-MB blob string itself forced every checkpoint map node to carry a full
// copy of the key; a 64-bit hash is enough for content-addressing within one
// compile (identical blobs → identical shapes).
using ImportCache = std::map<uint64_t, std::vector<TopoDS_Shape>>;

// Per-solid face/edge owner maps: sid -> (topo_index -> owning feature id).
// Folded into CompileCkpt so incremental rebuilds don't re-diff the whole history.
using OwnerTopoMap = std::map<std::string, std::map<int, std::string>>;

// ------------------------------------------------ incremental-rebuild prefix cache
// One checkpoint = the compiler's complete loop-carried state after feature i ran.
// Results / statuses / import_cache / owner maps are shared_ptr-backed so a checkpoint
// capture is O(entries) pointer-sized copies of UNCHANGED slots.
// If compile_document grows a NEW piece of loop-carried state, it MUST be added here
// and to the capture/restore sites below — that invariant is what keeps a restored
// prefix bit-identical to re-executing it.
struct CompileCkpt {
    FeatureResultMap results;
    std::set<std::string> consumed;
    std::map<std::string, std::string> superseded_by;
    std::vector<std::string> solid_order;
    std::map<std::string, std::string> body_kinds;
    std::map<std::string, std::string> source_of;
    std::map<std::string, TopoDS_Shape> tool_body_of;
    std::map<std::string, std::string> feature_op_of;
    std::set<std::string> seen_names;
    // sheet-metal context (flattened SheetCtx) + bend history
    bool sheet_active = false;
    double sheet_thickness = 0.0, sheet_k_factor = 0.0, sheet_default_bend_radius = 0.0;
    std::vector<::geom::BendRecord> bend_records;
    ImportCache import_cache;
    // Per-status shared_ptr so appending status N does not re-copy statuses 0..N-1
    // into every subsequent checkpoint.
    std::vector<std::shared_ptr<const FeatureStatus>> statuses;
    std::vector<ToolRec> tool_recs;
    nlohmann::json weld_members;
    nlohmann::json sheet_metal_flat;
    // Incremental owner maps — face/edge attribution per solid id.
    OwnerTopoMap face_owners;
    OwnerTopoMap edge_owners;
};

// FNV-1a over a string, chainable through `seed` (h[i+1] = fnv(feature_json, h[i])).
static uint64_t cc_fnv1a(const std::string& s, uint64_t seed = 1469598103934665603ULL) {
    uint64_t h = seed;
    for (unsigned char c : s) {
        h ^= c;
        h *= 1099511628211ULL;
    }
    return h;
}

// Above this many features we skip checkpointing (memory guard; a doc this deep is
// far outside every real workload — the cache degrades to the old full rebuild).
constexpr std::size_t kCompileCacheMaxFeatures = 1024;

// The compile-input chain (see CompileCache): h0 folds env, active configuration,
// display tier and compile mode; h[i+1] folds feature i's json into h[i]. Shared by
// compile_document and the persisted-cache codec so both key checkpoints identically.
std::vector<uint64_t> cc_chain(const json& features, const json& env_json,
                               const std::string& active_configuration,
                               ::geom::DisplayQuality quality, bool with_display,
                               bool with_owners) {
    const std::size_t n = features.is_array() ? features.size() : 0;
    std::vector<uint64_t> chain;
    chain.reserve(n + 1);
    uint64_t h = cc_fnv1a(env_json.is_object() && !env_json.empty() ? env_json.dump()
                                                                    : std::string());
    h = cc_fnv1a(active_configuration, h);
    // Display quality bakes into the AICM this result carries, so it belongs in the
    // chain seed: switching quality is a cache MISS (full and prefix) and re-meshes.
    h = cc_fnv1a(::geom::display_quality_name(quality), h);
    h = cc_fnv1a(with_display ? "display" : "component_geometry", h);
    if (with_owners) h = cc_fnv1a("owners", h);
    chain.push_back(h);
    for (std::size_t i = 0; i < n; ++i)
        chain.push_back(cc_fnv1a(features[i].dump(), chain.back()));
    return chain;
}

// Full-result key: the chain tail folded with the doc's body + face appearances (they
// bake into the post-loop AICM colors only).
uint64_t cc_full_key(const json& doc, uint64_t chain_tail) {
    return cc_fnv1a(
        doc.contains("appearances") ? doc["appearances"].dump() : std::string(),
        cc_fnv1a(doc.contains("face_appearances") ? doc["face_appearances"].dump() : std::string(),
                 chain_tail));
}

std::atomic<uint64_t> g_cc_stamp{0};
uint64_t cc_next_stamp() { return g_cc_stamp.fetch_add(1, std::memory_order_relaxed) + 1; }

// Process-wide cache accounting for the profiler (all docs, since engine start).
static std::atomic<uint64_t> g_cc_compiles{0};
static std::atomic<uint64_t> g_cc_full_hits{0};
static std::atomic<uint64_t> g_cc_features_reused{0};
static std::atomic<uint64_t> g_cc_features_executed{0};
static std::atomic<uint64_t> g_cc_bodies_reused{0};    // tessellation+stats served from body cache
static std::atomic<uint64_t> g_cc_bodies_tessellated{0};

}  // namespace (hop out: CompileCache is the chudware::doc::CompileCache the header
   // forward-declares — inside the anonymous namespace it would be a DIFFERENT,
   // ambiguous type. Its CompileCkpt members are internal-linkage types, which is
   // fine: this TU is the only place the struct is ever complete.)

// A fully-tessellated body + its mass/area stats, cached across compiles so an
// UNCHANGED surviving body (same TShape) skips BRepMesh + extraction + the
// VolumeProperties/area Gauss integrations + AICM-body build on the next compile.
// Geometry-only (mesh + stats depend solely on the TShape + appearance), so this is
// bit-identical to recomputing. body_id is re-stamped to the current ordinal on reuse.
struct CachedBody {
    aicm::AicmBody body;  // body_id is a placeholder; the reuse site re-stamps it
    BodyInfo info;        // name is re-stamped to the reusing feature's id
};

struct CompileCache {
    // chain[0] = env+config seed; chain[i+1] = chain[i] folded with feature i's json.
    std::vector<uint64_t> chain;
    // snaps[i] = state after the first i features (snaps[0] stays null — empty state).
    // Entries are shared_ptr so an unchanged prefix is carried forward across compiles
    // without copying.
    std::vector<std::shared_ptr<const CompileCkpt>> snaps;
    // Full-compile fast path: the last CompileResult + its key (chain tail folded with
    // the doc's appearances, which only affect post-loop tessellation colors).
    std::shared_ptr<const CompileResult> last_result;
    uint64_t full_key = 0;
    // Per-body tessellation+stats cache. Key = (TShape*, is_surface, appearance hash).
    // Only bodies at an IDENTITY top-level location are cached (the part-compile path;
    // located assembly instances are recomputed). Replaced wholesale each compile with
    // the set that survived, so a discarded body's mesh is freed.
    std::map<std::tuple<const void*, bool, uint64_t>,
             std::shared_ptr<const CachedBody>> body_cache;
    // Process-unique contents stamp (compile_cache_stamp): renewed on every commit and
    // restore, copied with the struct, so an encoding of unchanged contents is reusable.
    uint64_t stamp = 0;
};

std::shared_ptr<CompileCache> make_compile_cache() { return std::make_shared<CompileCache>(); }

std::shared_ptr<CompileCache> copy_compile_cache(const CompileCache& source) {
    return std::make_shared<CompileCache>(source);
}

nlohmann::json compile_cache_stats() {
    return {{"compiles", g_cc_compiles.load(std::memory_order_relaxed)},
            {"full_hits", g_cc_full_hits.load(std::memory_order_relaxed)},
            {"features_reused", g_cc_features_reused.load(std::memory_order_relaxed)},
            {"features_executed", g_cc_features_executed.load(std::memory_order_relaxed)},
            {"bodies_reused", g_cc_bodies_reused.load(std::memory_order_relaxed)},
            {"bodies_tessellated", g_cc_bodies_tessellated.load(std::memory_order_relaxed)}};
}

namespace {  // hop back into the file-local helpers

// -------------------------------------------------------------------- env parsing
// Flatten the env json object into name -> double. Non-numeric entries are skipped
// (forward-tolerant); a non-object env yields an empty map.
EnvMap parse_env(const json& env) {
    EnvMap m;
    if (!env.is_object()) return m;
    for (auto it = env.begin(); it != env.end(); ++it) {
        if (it.value().is_number()) m[it.key()] = it.value().get<double>();
    }
    return m;
}

// Total-instance cap shared by every replicating feature (linear/circular pattern,
// bolt_circle). A runaway count used to build tens of thousands of boolean ops and
// HANG the whole MCP server; the cap rejects it loudly before any geometry runs. It
// is generous — far above any real part — so legitimate patterns are never blocked.
constexpr long long kPatternInstanceLimit = 4000;

// A numeric param that may be (a) a raw number, (b) a string expression, or (c) the
// CANONICAL dim object {"value": <num>, "expr": <string|null>} the host sends for
// every dimensioned param (radius/length/diameter/angle/depth/count/spacing...).
// Mirrors the Python ctx.dim resolve contract: prefer a non-empty expr (evaluated
// against env), else the numeric value. Throws on a bad expression / wrong shape —
// the per-feature try/catch turns that into an error status.
double num_or_expr(const json& v, const EnvMap& env) {
    if (v.is_number()) return v.get<double>();
    if (v.is_string()) return chudware::util::evaluate(v.get<std::string>(), env);
    if (v.is_object()) {
        auto e = v.find("expr");
        if (e != v.end() && e->is_string() && !e->get<std::string>().empty())
            return chudware::util::evaluate(e->get<std::string>(), env);
        auto val = v.find("value");
        if (val != v.end() && val->is_number()) return val->get<double>();
        throw std::runtime_error("dim object has neither a usable 'expr' nor numeric 'value'");
    }
    throw std::runtime_error("param is neither a number, expression, nor a dim object");
}

// Optional [x,y,z] -> Eigen::Vector3d. Returns nullopt when the key is absent or
// not a 3-array (caller supplies a default direction).
std::optional<Eigen::Vector3d> opt_vec3(const json& feat, const char* key) {
    auto it = feat.find(key);
    if (it == feat.end() || !it->is_array() || it->size() != 3) return std::nullopt;
    return Eigen::Vector3d((*it)[0].get<double>(), (*it)[1].get<double>(),
                           (*it)[2].get<double>());
}

// Required [x,y,z] -> Eigen::Vector3d. Throws (-> error status) when missing/malformed.
Eigen::Vector3d req_vec3(const json& feat, const char* key) {
    auto v = opt_vec3(feat, key);
    if (!v) throw std::runtime_error(std::string("missing/invalid vec3 param '") + key + "'");
    return *v;
}

// Map a plane name to a SketchPlane factory. Defaults to top() for an unknown/absent
// name (forward-tolerant), matching the most natural work plane.
chudware::geom::SketchPlane plane_from_name(const std::string& name) {
    if (name == "front") return chudware::geom::SketchPlane::front();
    if (name == "right") return chudware::geom::SketchPlane::right();
    return chudware::geom::SketchPlane::top();  // "top" or anything else
}

// Look up a referenced feature result by id, requiring it be a SOLID body. Throws a
// descriptive error (consumed by the per-feature try/catch) when the id is missing
// or refers to a sketch rather than a body.
const TopoDS_Shape& require_solid(const FeatureResultMap& results,
                                  const std::string& ref_id, const char* slot) {
    auto it = results.find(ref_id);
    if (it == results.end())
        throw std::runtime_error(std::string("ref '") + slot + "' -> '" + ref_id +
                                 "' not found");
    if (!std::holds_alternative<SolidResult>(*it->second))
        throw std::runtime_error(std::string("ref '") + slot + "' -> '" + ref_id +
                                 "' is a sketch, not a solid body");
    const TopoDS_Shape& s = std::get<SolidResult>(*it->second).shape;
    // Bug N1 (defensive) — a body that compiled to a NULL shape must not be handed to a
    // downstream kernel op (face/edge resolvers and OCCT booleans dereference the
    // TShape; a null one is a likely access-violation source). Fail loudly instead.
    if (s.IsNull())
        throw std::runtime_error(std::string("ref '") + slot + "' -> '" + ref_id +
                                 "' is an empty (null) body");
    return s;
}

// Look up a referenced SKETCH result by id (for extrude/revolve profiles). Throws
// when missing or it refers to a solid body rather than a sketch.
const SketchResult& require_sketch(const FeatureResultMap& results,
                                   const std::string& ref_id, const char* slot) {
    auto it = results.find(ref_id);
    if (it == results.end())
        throw std::runtime_error(std::string("profile '") + slot + "' -> '" + ref_id +
                                 "' not found");
    if (!std::holds_alternative<SketchResult>(*it->second))
        throw std::runtime_error(std::string("profile '") + slot + "' -> '" + ref_id +
                                 "' is a solid body, not a sketch");
    return std::get<SketchResult>(*it->second);
}

// Pick ONE wire chain out of a Sketch3DResult for a sweep path/guide ref. A plain
// feature-id ref takes the single chain (and rejects an ambiguous multi-chain
// sketch loudly); {"feature":fid,"wire":k} selects chain k explicitly. `what` is
// "path"/"guide" for the error text.
TopoDS_Wire sketch3d_ref_wire(const Sketch3DResult& s3, const json& ref,
                              const char* what) {
    if (s3.wires.empty())
        throw std::runtime_error(std::string("sweep: ") + what +
                                 " sketch3d produced no wire — its non-construction "
                                 "entities do not form any connected chain");
    int widx = -1;
    if (ref.is_object() && ref.contains("wire") && ref["wire"].is_number_integer())
        widx = ref["wire"].get<int>();
    if (widx < 0) {
        if (s3.wires.size() == 1) return s3.wires.front();
        throw std::runtime_error(std::string("sweep: ") + what + " sketch3d has " +
                                 std::to_string(s3.wires.size()) +
                                 " wire chains — pick one with "
                                 "{\"feature\":\"<fid>\",\"wire\":<k>}");
    }
    if (widx >= static_cast<int>(s3.wires.size()))
        throw std::runtime_error(std::string("sweep: ") + what + " sketch3d wire index " +
                                 std::to_string(widx) + " out of range (has " +
                                 std::to_string(s3.wires.size()) + " chains)");
    return s3.wires[widx];
}

// Close an OPEN revolve profile back to its axis so a bare outer-wall curve can still
// make a solid of revolution. Weak local models routinely sketch the profile curve but
// forget the closing line down the axis; revolving an open wire yields a surface, not a
// solid (status no_geometry_built). We take the open profile wire, project its two free
// ends onto the axis line, and add the perpendicular + along-axis edges that close the
// region, then build a planar face. Returns empty if it cannot be closed into a planar
// face (the caller then errors exactly as before). This is ONLY consulted when the
// sketch produced NO closed region, so it can never alter a model that already closed
// its own profile — it only turns a guaranteed failure into a likely success.
std::vector<TopoDS_Face> close_profile_to_axis(
    const nlohmann::json& solved_entities, const chudware::geom::SketchPlane& plane,
    const Eigen::Vector3d& axis_pt, const Eigen::Vector3d& axis_dir) {
    std::vector<TopoDS_Face> out;
    TopoDS_Wire open = chudware::geom::wire_from_solved_sketch(solved_entities, plane);
    if (open.IsNull()) return out;
    TopoDS_Vertex va, vb;
    ShapeAnalysis::FindBounds(open, va, vb);          // free ends of the open wire
    if (va.IsNull() || vb.IsNull() || va.IsSame(vb)) return out;  // already closed/degenerate
    gp_Pnt pa = BRep_Tool::Pnt(va), pb = BRep_Tool::Pnt(vb);
    Eigen::Vector3d ea(pa.X(), pa.Y(), pa.Z()), eb(pb.X(), pb.Y(), pb.Z());
    const Eigen::Vector3d d = axis_dir.normalized();
    auto proj = [&](const Eigen::Vector3d& p) { return axis_pt + d * (p - axis_pt).dot(d); };
    const Eigen::Vector3d aa = proj(ea), ab = proj(eb);
    const double tol = 1e-7;
    auto mkedge = [&](const Eigen::Vector3d& a, const Eigen::Vector3d& b) -> TopoDS_Edge {
        if ((a - b).norm() < tol) return TopoDS_Edge();  // skip degenerate (end already on axis)
        return BRepBuilderAPI_MakeEdge(gp_Pnt(a.x(), a.y(), a.z()),
                                       gp_Pnt(b.x(), b.y(), b.z())).Edge();
    };
    // Close: end_b -> axis(end_b) -> axis(end_a) -> end_a. MakeWire stitches by proximity.
    BRepBuilderAPI_MakeWire mw;
    mw.Add(open);
    for (const TopoDS_Edge& e : {mkedge(eb, ab), mkedge(ab, aa), mkedge(aa, ea)})
        if (!e.IsNull()) mw.Add(e);
    if (!mw.IsDone()) return out;
    BRepBuilderAPI_MakeFace mf(mw.Wire(), /*OnlyPlane=*/true);
    if (!mf.IsDone()) return out;
    out.push_back(mf.Face());
    return out;
}

// W2 (hole-frame determinism) — the [u,v] frame that hole/bolt_circle `positions` are
// measured in. Two fixes over the old (area-centroid + gp_Pln::XAxis) frame:
//   origin: the area centroid of the face's OUTER boundary only. The whole-face centroid
//     MOVES every time a bore removes area, so a second hole feature on the same face
//     landed offset from where the first one's coordinates said (measured 0.57 mm drift
//     on a 60x65 plate after one Ø22.5 bore). The outer boundary is unchanged by holes,
//     so the frame is stable no matter how many features already cut the face.
//   axes: SketchPlane::from's deterministic convention — u = world +X projected into the
//     plane (falls back to +Y when the plane is ⟂X), v = n×u, right-handed around the
//     OUTWARD normal. gp_Pln::XAxis() is an arbitrary artifact of surface
//     parameterization (one extrude's front face measured u→+Z,v→−X while a primitive's
//     top face measured u→+X,v→+Y) — unplaceable without trial and error.
static chudware::geom::SketchPlane hole_face_frame(const TopoDS_Face& face,
                                                const Eigen::Vector3d& outward_normal) {
    const TopoDS_Face* measured = &face;
    TopoDS_Face outer_only;
    TopoDS_Wire ow = BRepTools::OuterWire(face);
    if (!ow.IsNull()) {
        BRepAdaptor_Surface s(face);
        if (s.GetType() == GeomAbs_Plane) {
            BRepBuilderAPI_MakeFace mf(s.Plane(), ow);
            if (mf.IsDone()) {
                outer_only = mf.Face();
                measured = &outer_only;
            }
        }
    }
    GProp_GProps props;
    BRepGProp::SurfaceProperties(*measured, props);
    gp_Pnt c = props.CentreOfMass();
    return chudware::geom::SketchPlane::from(Eigen::Vector3d(c.X(), c.Y(), c.Z()),
                                          outward_normal);
}

// A hole/bolt_circle WORLD point names the bore AXIS — the line through it along the
// drilled face's normal — not necessarily a point on the face: models write the body
// centre (origin:[0,0,0] + direction) or z=0 on a centred plate, and both mean "the bore
// whose axis passes here". So the point is projected along the normal onto the face plane
// (`at`) and mapped to the hole_face_frame: u = (p-origin)·uAxis, v = (p-origin)·vAxis. A
// point more than kHoleWorldPointNoteMm off the plane carries a `note` naming that distance
// and the projected point (the feature warning).
//
// Which face the axis enters: when refs.face was a WORD query ({"of":"face","side":"top"})
// the query names a DIRECTION, and its extreme face is only one of the faces facing that
// way — a post standing on a plate wins "top", yet a bore at the plate centre is drilled
// through the plate's own top. So `body` is non-null for a word query, and when the axis
// misses the resolved face every planar face of the body with the SAME outward normal
// (within kHoleSameNormalRad) whose interior holds the axis crossing is a candidate; the
// one furthest along the normal wins — the face the drill (along -normal) enters first —
// and the substitution is named in the note. Several points of one feature may thus land
// on different faces (four boss tops, at one height or several). A concrete pick (`body`
// null) and a substitution search that finds nothing are refused by name: the axis misses,
// so drilling it would remove nothing or cut where nobody asked.
constexpr double kHoleWorldPointNoteMm = 0.01;
constexpr double kHoleSameNormalRad = 1e-6;

struct HoleWorldPoint {
    Eigen::Vector3d at = Eigen::Vector3d::Zero();  // the axis' crossing of the entered face
    std::string note;  // projection / face-substitution report; empty when neither happened
};

// `what` names the param in the refusal/note ("hole: world_points[1]"). `require_on_face`
// is false for a bolt_circle centre, which legitimately sits in an opening (a ring around
// an existing bore); the ring's own holes are still guarded by its removed-material check.
// `body` (non-null only for a word-query face) enables the same-normal face substitution.
static HoleWorldPoint hole_world_point(const TopoDS_Face& face,
                                       const chudware::geom::SketchPlane& fr,
                                       const json& p, const std::string& what,
                                       const EnvMap& env, bool require_on_face,
                                       const TopoDS_Shape* body) {
    if (!p.is_array() || p.size() != 3)
        throw std::runtime_error(what + " must be a world point [x, y, z] in mm (got " +
                                 p.dump() + ")");
    const Eigen::Vector3d w(num_or_expr(p[0], env), num_or_expr(p[1], env),
                            num_or_expr(p[2], env));
    const Eigen::Vector3d& n = fr.normal;
    const double off = (w - fr.origin).dot(n);
    HoleWorldPoint out;
    out.at = w - off * n;  // the axis' crossing of the resolved face's plane
    double entered_off = off;
    auto inside = [](const TopoDS_Face& f, const Eigen::Vector3d& q, bool in_only) {
        const TopAbs_State s =
            BRepClass_FaceClassifier(f, gp_Pnt(q.x(), q.y(), q.z()), 1e-6).State();
        return in_only ? s == TopAbs_IN : s != TopAbs_OUT;
    };
    std::string substituted;
    if (require_on_face && !inside(face, out.at, false)) {
        bool found = false;
        if (body != nullptr) {
            double best_h = -std::numeric_limits<double>::infinity();
            TopTools_IndexedMapOfShape faces;
            TopExp::MapShapes(*body, TopAbs_FACE, faces);
            for (int i = 1; i <= faces.Extent(); ++i) {
                const TopoDS_Face& cand = TopoDS::Face(faces(i));
                if (cand.IsSame(face)) continue;
                BRepAdaptor_Surface cs(cand);
                if (cs.GetType() != GeomAbs_Plane) continue;
                const gp_Pln cp = cs.Plane();
                const gp_Dir cd = ::chudware::geom::plane_outward_normal(cand, cp);
                const Eigen::Vector3d cn(cd.X(), cd.Y(), cd.Z());
                if (std::atan2(cn.cross(n).norm(), cn.dot(n)) > kHoleSameNormalRad) continue;
                const Eigen::Vector3d loc(cp.Location().X(), cp.Location().Y(),
                                          cp.Location().Z());
                const double h = loc.dot(n);
                const double coff = (w - loc).dot(n);
                const Eigen::Vector3d q = w - coff * n;
                if (h <= best_h || !inside(cand, q, true)) continue;
                best_h = h;
                out.at = q;
                entered_off = coff;
                found = true;
                GProp_GProps props;
                BRepGProp::SurfaceProperties(cand, props);
                const gp_Pnt c = props.CentreOfMass();
                char buf[400];
                std::snprintf(buf, sizeof(buf),
                    "%s: its axis misses the face refs.face resolved to (centre [%.6g, %.6g, "
                    "%.6g]), so it is drilled through the face with the same outward normal "
                    "that the drill enters first, centre [%.6g, %.6g, %.6g].",
                    what.c_str(), fr.origin.x(), fr.origin.y(), fr.origin.z(), c.X(), c.Y(),
                    c.Z());
                substituted = buf;
            }
        }
        if (!found) {
            char buf[900];
            std::snprintf(buf, sizeof(buf),
                "%s [%.6g, %.6g, %.6g]: the bore axis through it crosses the drilled face's "
                "plane at [%.6g, %.6g, %.6g] (%.6g mm along the face normal), which is OUTSIDE "
                "the face (face centre [%.6g, %.6g, %.6g], outward normal [%.6g, %.6g, %.6g])%s "
                "— nothing there to drill. Give a point whose axis passes through refs.face, "
                "or name the face the axis does pass through.",
                what.c_str(), w.x(), w.y(), w.z(), out.at.x(), out.at.y(), out.at.z(),
                std::abs(off), fr.origin.x(), fr.origin.y(), fr.origin.z(), n.x(), n.y(), n.z(),
                body != nullptr ? " and inside no other face of the body facing the same way"
                                : "");
            throw std::runtime_error(buf);
        }
    }
    out.note = substituted;
    if (std::abs(entered_off) > kHoleWorldPointNoteMm) {
        char buf[512];
        std::snprintf(buf, sizeof(buf),
            "%s [%.6g, %.6g, %.6g] is %.6g mm off the drilled face's plane — read as a point "
            "on the bore axis and projected along the face normal to [%.6g, %.6g, %.6g].",
            what.c_str(), w.x(), w.y(), w.z(), std::abs(entered_off), out.at.x(), out.at.y(),
            out.at.z());
        out.note += (out.note.empty() ? "" : " ") + std::string(buf);
    }
    return out;
}

// Read a string field, with a default for an absent key. Throws if present but not a
// string (a malformed feature dict -> error status).
std::string str_field(const json& feat, const char* key, const std::string& dflt) {
    auto it = feat.find(key);
    if (it == feat.end()) return dflt;
    if (!it->is_string())
        throw std::runtime_error(std::string("field '") + key + "' must be a string");
    return it->get<std::string>();
}

// Required string ref id. Throws when absent/non-string.
std::string req_str(const json& feat, const char* key) {
    auto it = feat.find(key);
    if (it == feat.end() || !it->is_string())
        throw std::runtime_error(std::string("missing string ref '") + key + "'");
    return it->get<std::string>();
}

// The `heal` feature's evidence: every distinct non-OK BRepCheck status name on a shape,
// so the recorded op result can say WHAT was wrong before and what (if anything) survived
// the ShapeFix pass. The numeric enum is useless to a reading agent — the live defect that
// motivated the op reported `bad_orientation_of_subshape`, and that string is the whole
// diagnosis — so unnamed statuses fall back to "brep_check_<n>" rather than being dropped.
// Deliberately duplicated in spirit (not code) by session.cpp::health_status_text, which is
// a file-local static there and covers only the orientation subset eval_solid_health needs.
const char* heal_status_name(BRepCheck_Status st) {
    switch (st) {
        case BRepCheck_NoError:                     return nullptr;
        case BRepCheck_InvalidPointOnCurve:         return "invalid_point_on_curve";
        case BRepCheck_InvalidPointOnCurveOnSurface: return "invalid_point_on_curve_on_surface";
        case BRepCheck_InvalidPointOnSurface:       return "invalid_point_on_surface";
        case BRepCheck_No3DCurve:                   return "no_3d_curve";
        case BRepCheck_Multiple3DCurve:             return "multiple_3d_curve";
        case BRepCheck_Invalid3DCurve:              return "invalid_3d_curve";
        case BRepCheck_NoCurveOnSurface:            return "no_curve_on_surface";
        case BRepCheck_InvalidCurveOnSurface:       return "invalid_curve_on_surface";
        case BRepCheck_InvalidCurveOnClosedSurface: return "invalid_curve_on_closed_surface";
        case BRepCheck_InvalidSameRangeFlag:        return "invalid_same_range_flag";
        case BRepCheck_InvalidSameParameterFlag:    return "invalid_same_parameter_flag";
        case BRepCheck_InvalidDegeneratedFlag:      return "invalid_degenerated_flag";
        case BRepCheck_FreeEdge:                    return "free_edge";
        case BRepCheck_InvalidMultiConnexity:       return "invalid_multi_connexity";
        case BRepCheck_InvalidRange:                return "invalid_range";
        case BRepCheck_EmptyWire:                   return "empty_wire";
        case BRepCheck_RedundantEdge:               return "redundant_edge";
        case BRepCheck_SelfIntersectingWire:        return "self_intersecting_wire";
        case BRepCheck_NoSurface:                   return "no_surface";
        case BRepCheck_InvalidWire:                 return "invalid_wire";
        case BRepCheck_RedundantWire:               return "redundant_wire";
        case BRepCheck_IntersectingWires:           return "intersecting_wires";
        case BRepCheck_InvalidImbricationOfWires:   return "invalid_imbrication_of_wires";
        case BRepCheck_EmptyShell:                  return "empty_shell";
        case BRepCheck_RedundantFace:               return "redundant_face";
        case BRepCheck_UnorientableShape:           return "unorientable_shape";
        case BRepCheck_NotClosed:                   return "not_closed";
        case BRepCheck_NotConnected:                return "not_connected";
        case BRepCheck_SubshapeNotInShape:          return "subshape_not_in_shape";
        case BRepCheck_BadOrientation:              return "bad_orientation";
        case BRepCheck_BadOrientationOfSubshape:    return "bad_orientation_of_subshape";
        case BRepCheck_InvalidPolygonOnTriangulation:
            return "invalid_polygon_on_triangulation";
        case BRepCheck_InvalidToleranceValue:       return "invalid_tolerance_value";
        case BRepCheck_EnclosedRegion:              return "enclosed_region";
        case BRepCheck_CheckFail:                   return "check_fail";
        default:                                    return nullptr;
    }
}

// Distinct BRepCheck issue names on `shape`, in first-seen order (deterministic across
// rebuilds). Empty for a valid shape. Never throws — a kernel failure inside the analyzer
// is reported as the single issue "check_fail", because "heal found nothing wrong" and
// "the checker itself blew up" must not read the same.
std::vector<std::string> heal_brep_issues(const TopoDS_Shape& shape) {
    std::vector<std::string> out;
    if (shape.IsNull()) return out;
    auto note = [&out](const char* name, int raw) {
        std::string s = name ? std::string(name)
                             : ("brep_check_" + std::to_string(raw));
        if (std::find(out.begin(), out.end(), s) == out.end()) out.push_back(std::move(s));
    };
    try {
        BRepCheck_Analyzer analyzer(shape);
        if (analyzer.IsValid()) return out;
        // Walk every sub-shape the analyzer holds a result for, not just the root: a
        // reversed face inside an otherwise-fine shell reports on the SHELL, and the root
        // solid's own status list is empty.
        TopTools_IndexedMapOfShape all;
        TopExp::MapShapes(shape, all);
        std::vector<TopoDS_Shape> targets;
        targets.push_back(shape);
        for (int i = 1; i <= all.Extent(); ++i) targets.push_back(all.FindKey(i));
        for (const TopoDS_Shape& sub : targets) {
            // Result() raises on a sub-shape the analyzer never recorded, so one bad
            // lookup must not discard the findings already collected.
            try {
                const Handle(BRepCheck_Result)& res = analyzer.Result(sub);
                if (res.IsNull()) continue;
                // Two distinct status lists per result, and the interesting ones live in
                // the SECOND. `Status()` holds defects of the sub-shape in isolation;
                // defects of the sub-shape IN A CONTEXT — which is where a shell's
                // `bad_orientation_of_subshape` inside its solid is filed — are only
                // reachable through the context iterator. Reading `Status()` alone
                // reported the motivating live defect as an unhelpful "check_fail".
                for (BRepCheck_ListOfStatus::Iterator it(res->Status()); it.More(); it.Next())
                    if (it.Value() != BRepCheck_NoError)
                        note(heal_status_name(it.Value()), static_cast<int>(it.Value()));
                for (res->InitContextIterator(); res->MoreShapeInContext();
                     res->NextShapeInContext()) {
                    for (BRepCheck_ListOfStatus::Iterator it(res->StatusOnShape());
                         it.More(); it.Next())
                        if (it.Value() != BRepCheck_NoError)
                            note(heal_status_name(it.Value()), static_cast<int>(it.Value()));
                }
            } catch (const Standard_Failure&) {
                continue;
            }
        }
        // The analyzer said INVALID, so an empty list would be a lie about what it found.
        if (out.empty()) note("check_fail", static_cast<int>(BRepCheck_CheckFail));
    } catch (const Standard_Failure&) {
        out.clear();
        note("check_fail", static_cast<int>(BRepCheck_CheckFail));
    }
    return out;
}

// Enclosed volume of a shape, 0.0 for anything without a solid (an open shell body).
double heal_volume(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return 0.0;
    try {
        GProp_GProps props;
        BRepGProp::VolumeProperties(shape, props);
        return props.Mass();
    } catch (const Standard_Failure&) {
        return 0.0;
    }
}

// Total surface area of a shape. Unlike the volume integral this is INDEPENDENT of
// face orientation, which makes it the only trustworthy "did the heal rebuild
// geometry?" witness on a body whose orientation flags are the defect.
double heal_area(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return 0.0;
    try {
        GProp_GProps props;
        BRepGProp::SurfaceProperties(shape, props);
        return props.Mass();
    } catch (const Standard_Failure&) {
        return 0.0;
    }
}

// Axis-aligned bounds as [minx,miny,minz,maxx,maxy,maxz]; all zeros for a null/void
// shape. The envelope is what proves a repair did not MOVE the part.
std::array<double, 6> heal_bbox(const TopoDS_Shape& shape) {
    std::array<double, 6> out{0, 0, 0, 0, 0, 0};
    if (shape.IsNull()) return out;
    try {
        Bnd_Box box;
        BRepBndLib::Add(shape, box);
        if (box.IsVoid()) return out;
        box.Get(out[0], out[1], out[2], out[3], out[4], out[5]);
    } catch (const Standard_Failure&) {
    }
    return out;
}

// Number of edges whose two adjacent faces traverse them in the SAME direction.
//
// This is exactly the test BRepCheck_Shell::Orientation applies to a shell inside a
// solid, and therefore exactly what `bad_orientation_of_subshape` means: a closed
// orientable shell must present every non-degenerate edge twice, once forward and
// once reversed. 0 for a sound body (verified against MakeBox/MakeCylinder and a
// boolean-cut tube); the live rover spine arrived with 144 of its 344 edges bad.
// Reported alongside the issue names because "one issue kind" hides whether a repair
// went from 144 bad edges to 68 or to 0.
int heal_bad_edge_count(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return 0;
    try {
        TopTools_IndexedMapOfShape emap;
        TopExp::MapShapes(shape, TopAbs_EDGE, emap);
        // Per edge index: the traversal orientations it takes across all faces.
        std::vector<std::array<TopAbs_Orientation, 2>> seen(
            static_cast<std::size_t>(emap.Extent()) + 1,
            {TopAbs_INTERNAL, TopAbs_INTERNAL});
        std::vector<int> uses(static_cast<std::size_t>(emap.Extent()) + 1, 0);
        for (TopExp_Explorer fe(shape, TopAbs_FACE); fe.More(); fe.Next()) {
            // TopExp_Explorer composes parent orientations, so Orientation() already
            // IS the sense in which the oriented face traverses the edge.
            for (TopExp_Explorer ee(fe.Current(), TopAbs_EDGE); ee.More(); ee.Next()) {
                const int i = emap.FindIndex(ee.Current());
                if (i < 1) continue;
                const std::size_t k = static_cast<std::size_t>(i);
                if (uses[k] < 2) seen[k][uses[k]] = ee.Current().Orientation();
                ++uses[k];
            }
        }
        int bad = 0;
        for (int i = 1; i <= emap.Extent(); ++i) {
            const std::size_t k = static_cast<std::size_t>(i);
            if (uses[k] != 2 || seen[k][0] != seen[k][1]) continue;
            if (BRep_Tool::Degenerated(TopoDS::Edge(emap.FindKey(i)))) continue;
            ++bad;
        }
        return bad;
    } catch (const Standard_Failure&) {
        return 0;
    }
}

// A subtractive op can only remove material. An exact volume that GROWS means the kernel
// returned a broken solid (c259: a slot cut came back as 3 shells, +44 cm^3, BRepCheck
// invalid, reported ok:true). Refuse it so the feature fails and the prior body stands.
void require_cut_removed_material(const std::string& what, double before_mm3,
                                  double after_mm3) {
    const double tol = 1e-6 * std::max(1.0, std::abs(before_mm3));
    if (after_mm3 <= before_mm3 + tol) return;
    std::ostringstream os;
    os.precision(6);
    os << what << " produced a body LARGER than its target (" << before_mm3 << " -> "
       << after_mm3 << " mm^3): the kernel boolean returned an invalid solid. Nothing was "
       "changed. Move the tool off coincident/tangent faces (offset it by ~0.05 mm), or "
       "split the cut into two features that each cross fewer existing faces.";
    throw std::runtime_error(os.str());
}

// Wrap closed shells as a solid, orienting each shell OUTWARD (positive enclosed
// volume) first. BRepLib::OrientClosedSolid only flips the SOLID, which leaves the
// shell's own flag — and therefore BRepCheck's shell-in-solid verdict — untouched;
// reversing the shell is what actually fixes an inside-out body.
TopoDS_Shape heal_solid_from_shells(const TopoDS_Shape& shape) {
    BRepBuilderAPI_MakeSolid mk;
    int n = 0;
    for (TopExp_Explorer se(shape, TopAbs_SHELL); se.More(); se.Next()) {
        TopoDS_Shell shell = TopoDS::Shell(se.Current());
        GProp_GProps props;
        BRepGProp::VolumeProperties(shell, props);
        if (props.Mass() < 0.0) shell.Reverse();
        mk.Add(shell);
        ++n;
    }
    if (n == 0 || !mk.IsDone()) return TopoDS_Shape();
    TopoDS_Solid solid = mk.Solid();
    try {
        BRepLib::OrientClosedSolid(solid);
    } catch (const Standard_Failure&) {
    }
    return solid;
}

// Flood-fill the face-orientation graph of every shell: two faces sharing an edge
// must traverse it in opposite senses, so flipping one face flips that constraint.
// Returns the re-oriented shape and reports how many faces were flipped and how many
// constraints could NOT be satisfied (`conflicts` > 0 means the input's edge sharing
// is self-contradictory — flag repair alone cannot fix it, and rebuild_solid is the
// next escalation). Purely topological: no surface is rebuilt, so areas and analytic
// surface types are preserved exactly.
TopoDS_Shape heal_flood_orientation(const TopoDS_Shape& shape, int& flipped,
                                    int& conflicts) {
    flipped = 0;
    conflicts = 0;
    std::vector<TopoDS_Shape> faces;
    for (TopExp_Explorer fe(shape, TopAbs_FACE); fe.More(); fe.Next())
        faces.push_back(fe.Current());
    const int nf = static_cast<int>(faces.size());
    if (nf == 0) return shape;

    TopTools_IndexedMapOfShape emap;
    TopExp::MapShapes(shape, TopAbs_EDGE, emap);
    // Per edge: the (face, +1 forward / -1 reversed) uses, capped at two.
    struct Use { int face = -1; int sense = 0; };
    std::vector<std::array<Use, 2>> uses(static_cast<std::size_t>(emap.Extent()) + 1);
    std::vector<int> count(static_cast<std::size_t>(emap.Extent()) + 1, 0);
    for (int i = 0; i < nf; ++i)
        for (TopExp_Explorer ee(faces[i], TopAbs_EDGE); ee.More(); ee.Next()) {
            const int e = emap.FindIndex(ee.Current());
            if (e < 1) continue;
            if (BRep_Tool::Degenerated(TopoDS::Edge(emap.FindKey(e)))) continue;
            const std::size_t k = static_cast<std::size_t>(e);
            if (count[k] < 2)
                uses[k][count[k]] = Use{i, ee.Current().Orientation() == TopAbs_FORWARD
                                                ? 1 : -1};
            ++count[k];
        }
    // flip[a] * flip[b] == need  for every manifold edge shared by two DISTINCT faces.
    // A seam edge (same face twice) carries no constraint between faces.
    std::vector<std::vector<std::pair<int, int>>> adj(static_cast<std::size_t>(nf));
    for (int e = 1; e <= emap.Extent(); ++e) {
        const std::size_t k = static_cast<std::size_t>(e);
        if (count[k] != 2) continue;
        const Use& a = uses[k][0];
        const Use& b = uses[k][1];
        if (a.face == b.face) continue;
        const int need = -a.sense * b.sense;
        adj[static_cast<std::size_t>(a.face)].push_back({b.face, need});
        adj[static_cast<std::size_t>(b.face)].push_back({a.face, need});
    }

    std::vector<int> flip(static_cast<std::size_t>(nf), 0);
    std::vector<int> queue;
    for (int s = 0; s < nf; ++s) {
        if (flip[static_cast<std::size_t>(s)] != 0) continue;
        flip[static_cast<std::size_t>(s)] = 1;
        queue.clear();
        queue.push_back(s);
        for (std::size_t qi = 0; qi < queue.size(); ++qi) {
            const int u = queue[qi];
            for (const auto& [v, need] : adj[static_cast<std::size_t>(u)]) {
                const int want = need * flip[static_cast<std::size_t>(u)];
                int& fv = flip[static_cast<std::size_t>(v)];
                if (fv == 0) {
                    fv = want;
                    queue.push_back(v);
                } else if (fv != want) {
                    ++conflicts;
                }
            }
        }
    }
    conflicts /= 2;  // every unsatisfiable constraint is met from both ends

    // Rebuild each shell with the flood-filled orientations, then re-wrap as a solid.
    BRep_Builder bb;
    TopoDS_Compound comp;
    bb.MakeCompound(comp);
    int fi = 0;
    int shells = 0;
    for (TopExp_Explorer se(shape, TopAbs_SHELL); se.More(); se.Next()) {
        TopoDS_Shell out;
        bb.MakeShell(out);
        for (TopExp_Explorer fe(se.Current(), TopAbs_FACE); fe.More(); fe.Next(), ++fi) {
            const bool rev = flip[static_cast<std::size_t>(fi)] < 0;
            if (rev) ++flipped;
            bb.Add(out, rev ? faces[static_cast<std::size_t>(fi)].Reversed()
                            : faces[static_cast<std::size_t>(fi)]);
        }
        out.Closed(BRep_Tool::IsClosed(out));
        bb.Add(comp, out);
        ++shells;
    }
    if (shells == 0) return shape;
    const TopoDS_Shape solid = heal_solid_from_shells(comp);
    return solid.IsNull() ? TopoDS_Shape(comp) : solid;
}

// Re-derive the solid from its FACES with BOPAlgo_MakerVolume: the faces are
// intersected against each other and every enclosed cell is rebuilt with a
// geometrically-derived orientation, so a body whose stored flags are
// self-contradictory (see heal_flood_orientation's `conflicts`) is recovered.
// Self-intersecting input splits into several cells; the principal (largest) lump is
// the body and the rest are the artefacts the intersection exposed — both are
// reported so the caller judges the loss instead of trusting it.
//
// `fuzzy` (mm, <= 0 = OCCT's own tolerance) is the intersection's merge distance and it
// decides whether the result is MESHABLE. At the default tolerance every pair of
// near-coincident faces in the input yields its own intersection line, so the rebuild
// hands back razor strips: body 47 came out valid but carrying two crescent faces
// 0.011 mm wide, and polymesh refused the whole fill ("CAD face 5 has no aligned
// delivered boundary patch", the quoted distance being exactly that width) at h=8 mm and
// again at 4.8 — no element size resolves an 11 micron strip. At fuzzy 0.05 mm the same
// faces merge during the intersection and the strips are never created.
TopoDS_Shape heal_rebuild_solid(const TopoDS_Shape& shape, int& lumps,
                                double& kept_volume, double& discarded_volume,
                                double fuzzy) {
    lumps = 0;
    kept_volume = 0.0;
    discarded_volume = 0.0;
    TopTools_ListOfShape args;
    for (TopExp_Explorer fe(shape, TopAbs_FACE); fe.More(); fe.Next())
        args.Append(fe.Current());
    if (args.IsEmpty()) return TopoDS_Shape();
    BOPAlgo_MakerVolume mv;
    mv.SetArguments(args);
    mv.SetIntersect(Standard_True);
    mv.SetAvoidInternalShapes(Standard_True);
    if (fuzzy > 0.0) mv.SetFuzzyValue(fuzzy);
    mv.Perform();
    if (mv.HasErrors()) return TopoDS_Shape();
    const TopoDS_Shape cells = mv.Shape();
    if (cells.IsNull()) return TopoDS_Shape();

    TopoDS_Shape best;
    double total = 0.0;
    for (TopExp_Explorer se(cells, TopAbs_SOLID); se.More(); se.Next()) {
        const double v = heal_volume(se.Current());
        total += v;
        ++lumps;
        if (best.IsNull() || v > kept_volume) {
            kept_volume = v;
            best = se.Current();
        }
    }
    if (best.IsNull()) return TopoDS_Shape();
    discarded_volume = total - kept_volume;
    return best;
}

// The width of the NARROWEST face of a shape (mm): min over faces of 2*area/perimeter,
// the mean width of a strip. A rebuild can hand back a perfectly valid solid carrying a
// face 0.011 mm across, which no volume mesher can resolve at any element size — the
// number that predicts that is this one, so `heal` reports it instead of leaving the
// caller to discover it in a mesher refusal.
double heal_narrowest_face(const TopoDS_Shape& shape) {
    double narrowest = 0.0;
    if (shape.IsNull()) return narrowest;
    try {
        for (TopExp_Explorer fe(shape, TopAbs_FACE); fe.More(); fe.Next()) {
            GProp_GProps ap;
            BRepGProp::SurfaceProperties(fe.Current(), ap);
            double perimeter = 0.0;
            for (TopExp_Explorer ee(fe.Current(), TopAbs_EDGE); ee.More(); ee.Next()) {
                GProp_GProps lp;
                BRepGProp::LinearProperties(ee.Current(), lp);
                perimeter += lp.Mass();
            }
            if (!(perimeter > 1e-12)) continue;
            const double width = 2.0 * ap.Mass() / perimeter;
            if (narrowest <= 0.0 || width < narrowest) narrowest = width;
        }
    } catch (const Standard_Failure&) {
    }
    return narrowest;
}


// A reserved axis datum ("axis.x/y/z", or {"kind":"datum","feature":"axis.z"}) ->
// (point on axis, unit direction). nullopt for anything else (a topology selector
// axis is not yet supported here). All reserved axes pass through the world origin
// (engine/doc/features/datum.py reserved_datums), Z-up.
std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> axis_from_datum(const json& ref) {
    std::string name;
    if (ref.is_string()) {
        name = ref.get<std::string>();
    } else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
               ref.contains("feature") && ref["feature"].is_string()) {
        name = ref["feature"].get<std::string>();
    } else {
        return std::nullopt;
    }
    const Eigen::Vector3d o(0, 0, 0);
    if (name == "axis.x") return std::make_pair(o, Eigen::Vector3d(1, 0, 0));
    if (name == "axis.y") return std::make_pair(o, Eigen::Vector3d(0, 1, 0));
    if (name == "axis.z") return std::make_pair(o, Eigen::Vector3d(0, 0, 1));
    return std::nullopt;
}

// An EXPLICIT axis spec: an object that carries a direction vector under `axis:[x,y,z]`
// (and an optional through-point under `centroid`/`point`/`origin`, default origin).
// Small local models, asked for a revolve/pattern axis, routinely hand us the axis
// geometry directly but wrap it in an invented `kind` (e.g. {"kind":"axis",...} or an
// edge-selector with a bogus `surf`) instead of the documented datum ref. The geometry
// is unambiguous, so we accept it -> (point, unit dir). nullopt if there is no usable
// 3-vector direction. (A reserved datum is handled by axis_from_datum first.)
std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> axis_from_explicit(const json& ref) {
    if (!ref.is_object()) return std::nullopt;
    auto dir = opt_vec3(ref, "axis");
    if (!dir || !(dir->norm() > 1e-9)) return std::nullopt;
    Eigen::Vector3d pt(0, 0, 0);
    for (const char* k : {"centroid", "point", "origin"}) {
        if (auto p = opt_vec3(ref, k)) { pt = *p; break; }
    }
    return std::make_pair(pt, dir->normalized());
}

// A reserved plane datum ("plane.top/front/right" or the bare "top"/"front"/"right",
// or {"kind":"datum","feature":"plane.top"}) -> (point, normal), reusing the
// SketchPlane frames so the signs match the rest of the C++ (top=+Z, front=-Y,
// right=+X). nullopt for anything else (a face-selector plane is handled separately).
std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> plane_pt_normal_from_datum(
    const json& ref) {
    std::string name;
    if (ref.is_string()) {
        name = ref.get<std::string>();
    } else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
               ref.contains("feature") && ref["feature"].is_string()) {
        name = ref["feature"].get<std::string>();
    } else {
        return std::nullopt;
    }
    if (name.rfind("plane.", 0) == 0) name = name.substr(6);
    if (name != "top" && name != "front" && name != "right") return std::nullopt;
    chudware::geom::SketchPlane p = plane_from_name(name);
    return std::make_pair(p.origin, p.normal);
}

// Extract the owning feature id from a body/region selector entry or a bare id
// string. A canonical body selector is {"kind":"body","owner_feature":"fN"}; a
// region selector carries "sketch"; the simplified model passes a bare id string.
std::string selector_owner_id(const json& sel) {
    if (sel.is_string()) return sel.get<std::string>();
    if (sel.is_object()) {
        if (sel.contains("owner_feature") && sel["owner_feature"].is_string())
            return sel["owner_feature"].get<std::string>();
        if (sel.contains("sketch") && sel["sketch"].is_string())
            return sel["sketch"].get<std::string>();
        // A {kind:"path",feature:fid} ref (sweep/loft path picked from the tree or
        // the viewport curve picker) names the owning sketch / curve feature.
        if (sel.contains("feature") && sel["feature"].is_string())
            return sel["feature"].get<std::string>();
    }
    return std::string();
}

// Owning sketch feature id from a (possibly already-normalized) profile ref:
// bare string, region object, or list of either. Empty if unresolvable.
std::string profile_sketch_id(const json& profile) {
    if (profile.is_string()) return profile.get<std::string>();
    if (profile.is_object()) return selector_owner_id(profile);
    if (profile.is_array()) {
        for (const json& s : profile) {
            std::string id = s.is_string() ? s.get<std::string>() : selector_owner_id(s);
            if (!id.empty()) return id;
        }
    }
    return std::string();
}

// Face surface-area (mm^2) for stable region ordering (matches doc_sketch_regions'
// area from BRepGProp::SurfaceProperties).
double face_surface_area(const TopoDS_Face& f) {
    GProp_GProps props;
    BRepGProp::SurfaceProperties(f, props);
    return props.Mass();
}

// Project a face's area-centroid into the sketch plane's (u,v) frame — same frame
// doc_sketch_regions stamps on region descriptors (session.cpp::reg_face_descriptor).
Eigen::Vector2d face_uv_centroid(const TopoDS_Face& f,
                                 const chudware::geom::SketchPlane& plane) {
    GProp_GProps props;
    BRepGProp::SurfaceProperties(f, props);
    gp_Pnt cm = props.CentreOfMass();
    Eigen::Vector3d diff(cm.X() - plane.origin.x(), cm.Y() - plane.origin.y(),
                         cm.Z() - plane.origin.z());
    return Eigen::Vector2d(diff.dot(plane.uAxis), diff.dot(plane.vAxis));
}

// Resolve a feature's `profile` ref to the planar faces that should be extruded /
// revolved / swept / sheetmetal-base-flanged / surfaced. Shared by every consumer
// that previously did `req_str(profile) + require_sketch + src.faces`.
//
// Semantics (backward-compatible with the simplified string model):
//   - bare string sketch id, OR region selector(s) WITHOUT index/centroid/hint_index
//     => ALL material faces of the sketch (even-odd outer regions WITH holes —
//        concentric circles become one washer face).
//   - region selector(s) carrying ONLY hint_index (the seed cad_doc_tools.py injects
//     for "extrude this sketch") => hint_index addresses CONNECTED MATERIAL REGIONS
//     (geom::connected_material_regions): 0 is the sketch's WHOLE material profile
//     (so the injected default keeps meaning "the whole sketch" and never silently
//     drops a region), k>0 narrows to connected region k, and an out-of-range k is
//     its OWN error naming the sketch and the region count. See the guard comment
//     below for the live run that made this load-bearing.
//   - region selector(s) carrying centroid (prefer) and/or index:
//       * centroid present => pick the material face whose (u,v) centroid is
//         nearest the requested centroid (robust vs. reordering).
//       * else index => order material faces the same way doc_sketch_regions
//         orders regions (by centroid u then v) and take that index among the
//         material faces. The GUI region picker emits material-region indices
//         for profile wells (void/hole regions are not selectable as profiles).
//         NB `index` addresses the per-LOOP doc_sketch_regions space (so a washer's
//         void inner disc stays addressable); `hint_index` addresses the coarser
//         connected-material-region space the agent readback narrates.
//   Multiple selectors union their matched faces (de-duplicated by row index).
std::vector<TopoDS_Face> resolve_profile_faces(const FeatureResultMap& results,
                                               const json& feat) {
    auto it = feat.find("profile");
    if (it == feat.end())
        throw std::runtime_error("missing string ref 'profile'");
    const json& profile = *it;

    // Collect region selector objects (or invent a whole-sketch one from a bare id).
    std::vector<json> sels;
    std::string sketch_id;
    if (profile.is_string()) {
        sketch_id = profile.get<std::string>();
        sels.push_back(json{{"kind", "region"}, {"sketch", sketch_id}});
    } else if (profile.is_object()) {
        sels.push_back(profile);
        sketch_id = selector_owner_id(profile);
    } else if (profile.is_array()) {
        for (const json& s : profile) {
            if (s.is_string()) {
                if (sketch_id.empty()) sketch_id = s.get<std::string>();
                sels.push_back(json{{"kind", "region"}, {"sketch", s.get<std::string>()}});
            } else if (s.is_object()) {
                sels.push_back(s);
                if (sketch_id.empty()) sketch_id = selector_owner_id(s);
            }
        }
    }
    if (sketch_id.empty())
        throw std::runtime_error("missing string ref 'profile'");

    // All selectors must name the same sketch (multi-sketch profiles not supported).
    for (const json& s : sels) {
        const std::string sid = selector_owner_id(s);
        if (!sid.empty() && sid != sketch_id)
            throw std::runtime_error("profile: multi-sketch region picks are not supported");
    }

    const SketchResult& src = require_sketch(results, sketch_id, sketch_id.c_str());
    if (src.faces.empty())
        throw std::runtime_error("profile sketch has no closed face");

    // Does ANY selector carry a region target (index and/or centroid)?
    bool any_targeted = false;
    for (const json& s : sels) {
        if (!s.is_object()) continue;
        if (s.contains("index") && !s["index"].is_null()) { any_targeted = true; break; }
        if (s.contains("centroid") && s["centroid"].is_array() && s["centroid"].size() >= 2)
            { any_targeted = true; break; }
    }
    if (!any_targeted) {
        // Untargeted, or targeted ONLY by hint_index. `hint_index` is the seed the tool
        // layer injects for a plain "extrude this sketch" (cad_doc_tools.py), so index 0
        // must keep meaning the sketch's WHOLE material profile — every material face,
        // with holes already nested by faces_from_solved_sketch.
        //
        // A NON-ZERO hint is a real request for the k-th CONNECTED material region, and
        // an out-of-range hint gets its OWN error. Live run run-42508173312214
        // ("lexus_f_keychain"): the sketch readback narrated "3 material regions" for
        // three OVERLAPPING rectangles (three closed LOOPS, one connected region), the
        // model dutifully asked for regions 1 and 2, hint_index was silently IGNORED
        // here, and both re-extruded the whole 264 mm^2 union into the body it was
        // already fused into — surfacing as "extrude boss added no material" with a
        // fabricated plane.top diagnosis. Whatever region count the readback reports,
        // hint_index 0..N-1 must each resolve, and N.. must say exactly why it cannot.
        std::vector<int> hints;
        for (const json& s : sels)
            if (s.is_object() && s.contains("hint_index") && s["hint_index"].is_number())
                hints.push_back(static_cast<int>(s["hint_index"].get<double>()));
        bool only_zero = true;
        for (int h : hints)
            if (h != 0) { only_zero = false; break; }
        if (hints.empty() || only_zero) return src.faces;  // whole material profile

        std::vector<std::vector<TopoDS_Face>> groups =
            chudware::geom::connected_material_regions(src.entities, src.plane);
        const int n_regions = static_cast<int>(groups.size());
        std::vector<TopoDS_Face> out;
        std::set<int> taken_group;
        for (int h : hints) {
            if (h < 0 || h >= n_regions) {
                std::string msg =
                    "profile: sketch \"" + sketch_id + "\" has " +
                    std::to_string(n_regions) + " connected material region" +
                    (n_regions == 1 ? "" : "s") + ", addressable as hint_index 0" +
                    (n_regions == 1 ? "" : ".." + std::to_string(n_regions - 1)) +
                    " — hint_index " + std::to_string(h) + " does not exist.";
                if (n_regions == 1)
                    msg += " Closed loops that OVERLAP or TOUCH form ONE material region "
                           "and the extrude already unions them, so there is no second "
                           "region left to add: hint_index 0 is the whole profile. To add "
                           "material elsewhere, sketch a profile that does not overlap "
                           "this one.";
                else
                    msg += " hint_index 0 is the whole profile; 1.." +
                           std::to_string(n_regions - 1) +
                           " each narrow to one connected region.";
                throw std::runtime_error(msg);
            }
            if (h == 0) {  // the whole profile, unioned with any narrowed picks
                for (const TopoDS_Face& f : src.faces) out.push_back(f);
                continue;
            }
            if (!taken_group.insert(h).second) continue;  // de-dup repeated picks
            for (const TopoDS_Face& f : groups[static_cast<size_t>(h)])
                out.push_back(f);
        }
        if (out.empty()) return src.faces;
        return out;
    }

    // Targeted pick: build the FULL region face list (material + void), matching
    // doc_sketch_regions so index/centroid can address the inner disc of a washer.
    std::vector<TopoDS_Face> all_faces =
        chudware::geom::all_region_faces_from_solved_sketch(src.entities, src.plane);
    if (all_faces.empty())
        throw std::runtime_error("profile sketch has no closed face");

    // Build (uv_centroid, area, face, uv bbox) rows, sorted by (u, v) — same order as
    // doc_sketch_regions (session.cpp ~10039-10048) so `index` matches the GUI.
    struct FaceRow {
        Eigen::Vector2d uv;
        double area;
        TopoDS_Face face;
        Eigen::Vector2d lo, hi;  // (u,v) bounding box of the face
    };
    std::vector<FaceRow> rows;
    rows.reserve(all_faces.size());
    for (const TopoDS_Face& f : all_faces) {
        FaceRow r;
        r.face = f;
        r.uv = face_uv_centroid(f, src.plane);
        r.area = face_surface_area(f);
        r.lo = r.hi = r.uv;
        Bnd_Box box;
        BRepBndLib::Add(f, box, Standard_False);
        if (!box.IsVoid()) {
            double x0, y0, z0, x1, y1, z1;
            box.Get(x0, y0, z0, x1, y1, z1);
            for (int k = 0; k < 8; ++k) {  // project the 3D box corners into (u,v): conservative
                const Eigen::Vector3d d(((k & 1) ? x1 : x0) - src.plane.origin.x(),
                                        ((k & 2) ? y1 : y0) - src.plane.origin.y(),
                                        ((k & 4) ? z1 : z0) - src.plane.origin.z());
                const Eigen::Vector2d p(d.dot(src.plane.uAxis), d.dot(src.plane.vAxis));
                r.lo = r.lo.cwiseMin(p);
                r.hi = r.hi.cwiseMax(p);
            }
        }
        rows.push_back(std::move(r));
    }
    std::sort(rows.begin(), rows.end(), [](const FaceRow& a, const FaceRow& b) {
        if (a.uv.x() != b.uv.x()) return a.uv.x() < b.uv.x();
        if (a.uv.y() != b.uv.y()) return a.uv.y() < b.uv.y();
        // Stable when centroids coincide (concentric): smaller area first so the
        // inner disc gets a lower index than the washer when both share a center.
        return a.area < b.area;
    });

    std::vector<TopoDS_Face> out;
    std::vector<bool> taken(rows.size(), false);
    auto take = [&](size_t i) {
        if (i >= rows.size() || taken[i]) return;
        taken[i] = true;
        out.push_back(rows[i].face);
    };

    // A centroid pick that no longer lands on its region is an ERROR, never a silent
    // snap to the nearest survivor. Live 2026-10-10 6-axis run (forearm_lower, sketch
    // f63 / cut f64): an edit_feature replaced six slots' coincident relations with
    // `fix` only, four slots reopened by their 2.9e-5 mm authored rounding gaps, and the
    // six centroid picks snapped onto the two surviving slots (de-duplicated below) —
    // the cut lost 4 of 6 slots with ok:true. Every centroid pick must (a) lie inside
    // the (u,v) box of the region it resolves to (grown by a margin for hand-rounded
    // centroids), and (b) not share its region with a pick that points elsewhere.
    auto num = [](double v) {
        std::ostringstream os;
        os.precision(6);
        os << v;
        return os.str();
    };
    auto pt = [&](double u, double v) { return "[" + num(u) + ", " + num(v) + "]"; };
    const std::string region_count =
        "The sketch now has " + std::to_string(src.faces.size()) + " material region" +
        (src.faces.size() == 1 ? "" : "s") + " (" + std::to_string(rows.size()) +
        " closed loop region" + (rows.size() == 1 ? "" : "s") + ").";
    std::vector<int> taker(rows.size(), -1);           // selector index that took the row
    std::vector<Eigen::Vector2d> taker_at(rows.size());  // ...and the centroid it asked for

    for (size_t si = 0; si < sels.size(); ++si) {
        const json& s = sels[si];
        if (!s.is_object()) continue;
        // Prefer centroid when present (more robust than index).
        if (s.contains("centroid") && s["centroid"].is_array() && s["centroid"].size() >= 2) {
            const double cu = s["centroid"][0].get<double>();
            const double cv = s["centroid"][1].get<double>();
            // Nearest face centroid. When several faces share a centroid (concentric
            // circles both report centre (0,0)), prefer the LARGER-area face so the
            // default agent/guide pick {"centroid":[0,0],"hint_index":0} resolves to
            // the washer (material), not the void inner disc. An explicit index still
            // addresses the inner disc. hint_index is only a seed; a strictly closer
            // face still wins.
            const bool has_hint = s.contains("hint_index") && s["hint_index"].is_number();
            int best = -1;
            double best_d2 = 0.0;
            double best_area = 0.0;
            if (has_hint) {
                int hi = static_cast<int>(s["hint_index"].get<double>());
                if (hi >= 0 && hi < static_cast<int>(rows.size())) {
                    best = hi;
                    double du = rows[static_cast<size_t>(hi)].uv.x() - cu;
                    double dv = rows[static_cast<size_t>(hi)].uv.y() - cv;
                    best_d2 = du * du + dv * dv;
                    best_area = rows[static_cast<size_t>(hi)].area;
                }
            }
            for (size_t i = 0; i < rows.size(); ++i) {
                double du = rows[i].uv.x() - cu;
                double dv = rows[i].uv.y() - cv;
                double d2 = du * du + dv * dv;
                if (best < 0 || d2 < best_d2 - 1e-18 ||
                    (std::abs(d2 - best_d2) <= 1e-18 && rows[i].area > best_area)) {
                    best = static_cast<int>(i);
                    best_d2 = d2;
                    best_area = rows[i].area;
                }
            }
            if (best < 0) continue;
            const FaceRow& br = rows[static_cast<size_t>(best)];
            const std::string where = "profile[" + std::to_string(si) + "] (centroid " +
                                      pt(cu, cv) + ") of sketch \"" + sketch_id + "\"";
            // Without a hint_index seed, a tie between regions at DIFFERENT centroids has
            // no correct answer: refuse instead of taking the first row.
            if (!has_hint) {
                for (size_t i = 0; i < rows.size(); ++i) {
                    if (static_cast<int>(i) == best) continue;
                    const double du = rows[i].uv.x() - cu, dv = rows[i].uv.y() - cv;
                    if (std::abs(du * du + dv * dv - best_d2) <= 1e-9 * std::max(1.0, best_d2) &&
                        (rows[i].uv - br.uv).squaredNorm() > 1e-12)
                        throw std::runtime_error(
                            "profile: " + where + " is equidistant from the regions at " +
                            pt(br.uv.x(), br.uv.y()) + " and " +
                            pt(rows[i].uv.x(), rows[i].uv.y()) +
                            ". Give the centroid of one region from the sketch readback.");
                }
            }
            const Eigen::Vector2d size = br.hi - br.lo;
            const double margin = std::max(0.5, 0.25 * std::max(size.x(), size.y()));
            if (cu < br.lo.x() - margin || cu > br.hi.x() + margin ||
                cv < br.lo.y() - margin || cv > br.hi.y() + margin)
                throw std::runtime_error(
                    "profile: " + where + " is outside the region nearest to it (centroid " +
                    pt(br.uv.x(), br.uv.y()) + "). The selected region is not in the sketch. " +
                    region_count +
                    " If a loop of the sketch is open, close it with coincident relations. "
                    "Then read the sketch regions again and update the profile.");
            const size_t bi = static_cast<size_t>(best);
            if (taker[bi] >= 0 && (taker_at[bi] - Eigen::Vector2d(cu, cv)).squaredNorm() > 1e-12)
                throw std::runtime_error(
                    "profile: " + where + " and profile[" + std::to_string(taker[bi]) +
                    "] (centroid " + pt(taker_at[bi].x(), taker_at[bi].y()) +
                    ") point to different locations but resolve to the same region (centroid " +
                    pt(br.uv.x(), br.uv.y()) + "). " + region_count +
                    " If an edit merged or removed regions, read the sketch regions again "
                    "and update the profile.");
            if (taker[bi] < 0) {
                taker[bi] = static_cast<int>(si);
                taker_at[bi] = Eigen::Vector2d(cu, cv);
            }
            take(bi);
            continue;
        }
        if (s.contains("index") && s["index"].is_number()) {
            int idx = static_cast<int>(s["index"].get<double>());
            if (idx < 0 || idx >= static_cast<int>(rows.size()))
                throw std::runtime_error("profile: region index " + std::to_string(idx) +
                                         " out of range (sketch has " +
                                         std::to_string(rows.size()) + " region(s))");
            take(static_cast<size_t>(idx));
            continue;
        }
        // Untargeted selector mixed with targeted ones: contribute material faces
        // only (same as whole-sketch). Never pull void/hole regions into a profile.
        for (const TopoDS_Face& mf : src.faces) {
            // Match by area+uv against rows to avoid double-adding.
            const double a = face_surface_area(mf);
            const Eigen::Vector2d uv = face_uv_centroid(mf, src.plane);
            for (size_t i = 0; i < rows.size(); ++i) {
                if (taken[i]) continue;
                if (std::abs(rows[i].area - a) <= 1e-9 * std::max(1.0, a) &&
                    (rows[i].uv - uv).squaredNorm() <= 1e-12) {
                    take(i);
                    break;
                }
            }
        }
    }

    if (out.empty())
        throw std::runtime_error("profile: no region face matched the selector(s)");
    return out;
}


// A canonical extrude/revolve `profile` ref is a SEL_LIST of region selectors
// {"kind":"region","sketch":fid, "index"?, "centroid"?, "hint_index"?}
// (contracts/feature_params.md). The simplified id-ref model also accepts a bare
// sketch feature-id string. We PRESERVE the full selector payload (index/centroid)
// so resolve_profile_faces can pick specific regions; only the sketch id is
// extracted when the profile is a bare string or a selector without any region
// targeting fields. Other selector kinds (face profiles) are not yet supported here.
json canonical_profile_to_id(const json& profile) {
    if (profile.is_string()) return profile;  // already simplified
    // List of region selectors — keep the list (or a single selector object) so
    // index / centroid survive into the feature compiler. A bare sketch-id list
    // collapses to that string.
    if (profile.is_array()) {
        if (profile.empty()) return profile;
        // All entries strings -> first id (simplified multi-id not used).
        bool all_str = true;
        for (const json& s : profile)
            if (!s.is_string()) { all_str = false; break; }
        if (all_str) return profile.front();
        // Single region/object selector -> keep the object (or the whole list if >1
        // so multi-region picks can union their faces).
        if (profile.size() == 1 && profile.front().is_object()) return profile.front();
        return profile;  // multi-selector list, preserve as-is
    }
    if (profile.is_object() && profile.contains("sketch") && profile["sketch"].is_string())
        return profile;  // keep object (may carry index/centroid)
    return profile;  // leave as-is -> the profile resolver will raise a clear error
}

// Metric fastener designation (e.g. "M3", "M2.5", "M3x0.5") -> hole bore diameter in mm.
// Clearance (ISO 273 "medium" fit) by default; tap-drill diameter when `tapped`. Returns 0 for an
// unrecognized size. This is a DETERMINISTIC standard lookup (a hole wizard does exactly this), not
// a guess — it lets a model spec a hole by fastener size the way an engineer would ("M3 clearance
// bore") instead of pre-computing 3.4mm. The feature still records the resolved numeric diameter.
double metric_hole_diameter(const std::string& designation, bool tapped) {
    size_t i = designation.find_first_of("Mm");
    if (i == std::string::npos) return 0.0;
    size_t j = i + 1;
    std::string num;
    while (j < designation.size() &&
           (std::isdigit((unsigned char)designation[j]) || designation[j] == '.'))
        num += designation[j++];
    if (num.empty()) return 0.0;
    double m = std::atof(num.c_str());
    struct Row { double m, clear, tap; };
    static const Row tbl[] = {{1.6, 1.8, 1.25}, {2, 2.4, 1.6},  {2.5, 2.9, 2.05}, {3, 3.4, 2.5},
                              {4, 4.5, 3.3},     {5, 5.5, 4.2},  {6, 6.6, 5.0},    {8, 9.0, 6.8},
                              {10, 11.0, 8.5},   {12, 13.5, 10.2}};
    for (const auto& r : tbl)
        if (std::abs(r.m - m) < 1e-6) return tapped ? r.tap : r.clear;
    return 0.0;
}

// Comma-joined list of an object's keys, for teaching error messages ("Got params: a, b, c")
// so a model that omitted/misnamed a required key can see what it DID send and self-correct.
std::string json_key_list(const json& o) {
    if (!o.is_object() || o.empty()) return "(none)";
    std::string s;
    for (auto it = o.begin(); it != o.end(); ++it) {
        if (!s.empty()) s += ", ";
        s += it.key();
    }
    return s;
}

// A primitive's `axis` value: an explicit [x,y,z] vector OR an axis WORD. Matches the
// spellings picks.cpp::parse_axis_vec accepts and ADDS the SIGN — for a placement the
// direction matters (axis:"-x" grows the primitive toward -X), where a parallel/
// perpendicular pick filter only cares about the axis LINE and can ignore it. Returns
// nullopt when the value is neither form (the caller raises a naming error); the result
// is NOT normalized, so the caller can still reject a zero-length vector.
std::optional<Eigen::Vector3d> parse_signed_axis(const json& j) {
    if (j.is_array() && j.size() >= 3 && j[0].is_number() && j[1].is_number() &&
        j[2].is_number())
        return Eigen::Vector3d(j[0].get<double>(), j[1].get<double>(), j[2].get<double>());
    if (j.is_string()) {
        std::string s;
        for (char c : j.get<std::string>()) {
            const unsigned char u = static_cast<unsigned char>(c);
            if (!std::isspace(u)) s += static_cast<char>(std::tolower(u));
        }
        if (s == "x" || s == "+x") return Eigen::Vector3d(1, 0, 0);
        if (s == "-x")             return Eigen::Vector3d(-1, 0, 0);
        if (s == "y" || s == "+y") return Eigen::Vector3d(0, 1, 0);
        if (s == "-y")             return Eigen::Vector3d(0, -1, 0);
        if (s == "z" || s == "+z" || s == "vertical") return Eigen::Vector3d(0, 0, 1);
        if (s == "-z")             return Eigen::Vector3d(0, 0, -1);
    }
    return std::nullopt;
}

// SCHEMA CONVERGENCE: accept BOTH the host's
// canonical feature shape — {id, type, params:{...}, refs:{slot:<sel|datum>}} per
// contracts/doc_schema.md — and the simplified flat shape the early C++ tests use.
// We normalize to the flat shape the per-feature compilers already read: params
// and refs are merged to top level, and the two structural divergences are
// translated (plane datum "plane.top" -> "top"; region-selector profile list ->
// sketch id). A feature already in the flat shape (no params/refs) passes through
// unchanged, so the existing tests stay byte-identical. This lets the parity
// dashboard + the host feed REAL canonical trees into the ported subset.
json normalize_feature(const json& raw) {
    json f = json::object();
    const std::string type = raw.value("type", std::string());
    // 1. Copy top-level scalar/other keys (the flat shape & metadata like id/type).
    for (auto it = raw.begin(); it != raw.end(); ++it) {
        if (it.key() == "params" || it.key() == "refs") continue;
        f[it.key()] = it.value();
    }
    // 2. Merge canonical params -> top level. A param named "type" (the sm_hem
    // sub-type "closed"/"open"/"teardrop") would clobber the feature's own "type"
    // key — remap it to "hem_type" so the feature type survives the flatten.
    if (raw.contains("params") && raw["params"].is_object()) {
        for (auto it = raw["params"].begin(); it != raw["params"].end(); ++it) {
            if (it.key() == "type" || it.key() == "id") {
                f["hem_type"] = it.value();
                continue;
            }
            f[it.key()] = it.value();
        }
    }
    // 3. Merge / translate canonical refs -> top level.
    if (raw.contains("refs") && raw["refs"].is_object()) {
        const json& r = raw["refs"];
        for (auto it = r.begin(); it != r.end(); ++it) {
            const std::string& slot = it.key();
            const json& val = it.value();
            if (slot == "profile") {
                f["profile"] = canonical_profile_to_id(val);
            } else if (slot == "axis") {
                // revolve / circular_pattern axis: a reserved datum, OR an explicit
                // axis spec (a direction vector the model handed us under an invented
                // wrapper) -> axis_pt/axis_dir. Both revolve and circular_pattern read
                // axis_pt/axis_dir, so this one conversion serves both.
                auto ax = axis_from_datum(val);
                if (!ax) ax = axis_from_explicit(val);
                if (ax) {
                    f["axis_pt"] = {ax->first.x(), ax->first.y(), ax->first.z()};
                    f["axis_dir"] = {ax->second.x(), ax->second.y(), ax->second.z()};
                } else {
                    f["axis"] = val;  // leave for a clearer error / future selector port
                }
            } else if (slot == "dir1" || slot == "dir2") {
                // linear_pattern direction: a reserved axis datum OR an explicit axis
                // vector -> a unit vec3 (the compiler reads dir1/dir2 via req_vec3).
                auto ax = axis_from_datum(val);
                if (!ax) ax = axis_from_explicit(val);
                if (ax)
                    f[slot] = {ax->second.x(), ax->second.y(), ax->second.z()};
                else
                    f[slot] = val;
            } else if (slot == "targets") {
                if (val.is_array() && !val.empty()) f["a"] = selector_owner_id(val.front());
            } else if (slot == "tools") {
                if (val.is_array() && !val.empty()) f["b"] = selector_owner_id(val.front());
            } else if (slot == "features") {
                // pattern/mirror replicate a LIST of feature ids; the simplified C++
                // model patterns a single seed -> take the first. Accept BOTH a list
                // and a bare id string (Bug N4 — symmetric with how `profile` accepts
                // either shape). By this point session.cpp has already rewritten any
                // same-batch "$<index>" placeholder to its "fN" id, so this only needs
                // to map the (already-resolved) reference onto `seed`.
                if (val.is_array() && !val.empty()) f["seed"] = selector_owner_id(val.front());
                else if (val.is_string()) f["seed"] = val;
            } else if (slot == "plane") {
                if (type == "sketch" && val.is_string()) {
                    // sketch plane datum "plane.top" -> the plane NAME "top".
                    std::string p = val.get<std::string>();
                    if (p.rfind("plane.", 0) == 0) p = p.substr(6);
                    f["plane"] = p;
                } else if (auto pn = plane_pt_normal_from_datum(val)) {
                    // mirror/draft about a reserved datum plane -> point + normal.
                    f["plane_point"] = {pn->first.x(), pn->first.y(), pn->first.z()};
                    f["plane_normal"] = {pn->second.x(), pn->second.y(), pn->second.z()};
                } else {
                    f["plane"] = val;  // a face-selector object passes through
                }
            } else if (slot == "neutral") {
                // draft neutral plane: a reserved datum -> point+normal; a face
                // selector passes through for the branch to resolve.
                if (auto pn = plane_pt_normal_from_datum(val)) {
                    f["plane_point"] = {pn->first.x(), pn->first.y(), pn->first.z()};
                    f["plane_normal"] = {pn->second.x(), pn->second.y(), pn->second.z()};
                } else {
                    f["neutral"] = val;
                }
            } else if (slot == "pull") {
                if (auto ax = axis_from_datum(val))
                    f["pull_dir"] = {ax->second.x(), ax->second.y(), ax->second.z()};
                else
                    f["pull"] = val;
            } else if (slot == "body") {
                // transform/fillet/chamfer/shell/hole `body` target. A canonical
                // selector is {"kind":"body","owner_feature":"fN"}; extract the
                // owning feature id so body_ref() (which only accepts a STRING)
                // targets the named body. Without this the selector object fell
                // through unparsed and body_ref() silently defaulted to the
                // current/last body — the assembly `transform body:"fN"`
                // mistargeting bug (every batched per-body move landed on the
                // last-imported body). A bare id string passes through unchanged.
                std::string bid = selector_owner_id(val);
                f["body"] = bid.empty() ? val : json(bid);
            } else if (slot == "face" || slot == "edges" || slot == "remove" ||
                       slot == "faces" || slot == "sections" || slot == "edge") {
                f[slot] = val;  // selector(-list) refs pass through to their branch
            } else {
                f[slot] = val;  // a/b/seed/... pass through
            }
        }
    }
    // 4. Param vocabulary translation: the canonical boolean op names map to the
    // kernel's fuse/cut/common; chamfer's canonical "length" is the C++ "distance".
    if (type == "boolean" && f.contains("op") && f["op"].is_string()) {
        const std::string op = f["op"].get<std::string>();
        if (op == "combine") f["op"] = "fuse";
        else if (op == "subtract") f["op"] = "cut";
        else if (op == "intersect") f["op"] = "common";
    }
    if (type == "chamfer" && f.contains("length") && !f.contains("distance"))
        f["distance"] = f["length"];
    // hole: the canonical explicit [u,v] list is refs.points; the C++ hole branch
    // reads "positions" (refs.positions in the canonical schema is a vertex-selector
    // list, a different thing — not yet ported).
    if (type == "hole" && f.contains("points") && !f.contains("positions"))
        f["positions"] = f["points"];
    // hole / bolt_circle bore-diameter SYNONYMS: small local models routinely name the bore
    // something other than the canonical "diameter" (dia/d/bore...). Accept the unambiguous
    // synonyms so a valid op isn't rejected over a key name; a truly-absent size still becomes a
    // loud teaching error at compile (we never invent a diameter here — no silent wrong geometry).
    if ((type == "hole" || type == "bolt_circle") &&
        (!f.contains("diameter") || f["diameter"].is_null())) {
        for (const char* k : {"dia", "d", "bore_diameter", "bore_dia", "hole_diameter",
                              "hole_dia", "bore"})
            if (f.contains(k) && !f[k].is_null()) { f["diameter"] = f[k]; break; }
        // A radius synonym -> diameter (×2). Number only (an expr string is left for the branch).
        if (!f.contains("diameter"))
            for (const char* k : {"radius", "r", "bore_radius"})
                if (f.contains(k) && f[k].is_number()) {
                    f["diameter"] = f[k].get<double>() * 2.0;
                    break;
                }
        // Fastener-standard sizing: a model may spec the bore by thread size ("standard":"M3")
        // instead of a number — the natural way to ask for a clearance/tap hole. Resolve it from
        // the metric table (clearance by default; tap-drill when the hole is tapped). A `tap` field
        // value (e.g. "M3") both names the size AND marks it tapped.
        if (!f.contains("diameter")) {
            std::string desig;
            bool tapped = (f.value("style", std::string()) == "tap");
            for (const char* k : {"standard", "thread", "screw", "fastener", "bolt", "tap", "size"}) {
                if (f.contains(k) && f[k].is_string() && !f[k].get<std::string>().empty()) {
                    desig = f[k].get<std::string>();
                    if (std::string(k) == "tap") tapped = true;
                    break;
                }
            }
            double d = metric_hole_diameter(desig, tapped);
            if (d > 0.0) f["diameter"] = d;
        }
    }
    return f;
}

// SEAM-AWARE closed-solid test (S2 / S1) — a mirror of session.cpp::eval_is_closed_solid
// (kept private there, so duplicated here for the dress-op post-check). A valid closed
// solid has every edge shared by >= 2 faces; the only single-face exceptions are SEAMS
// (BRep_Tool::IsClosed — the 0/2pi closure of a cylinder/sphere/revolution skin) and
// DEGENERATE edges (a cone apex / sphere pole). Any OTHER edge with < 2 incident faces is
// a genuine FREE edge -> the shell is open (the part is not watertight). This is the case
// the N2 negative-volume guard does NOT catch: a partial fillet/chamfer (some edges too
// tight) or a freeform shell can hand back a body whose volume is positive but whose B-rep
// is an open shell — eval_check then reports free_edges and STEP re-validation fails, yet
// the feature committed ok:true. Using the seam-aware rule means a perfectly valid
// cylinder/sphere/revolved body (which legitimately has seam edges) is NOT rejected.
bool dress_is_closed_solid(const TopoDS_Shape& shape) {
    try {
        if (shape.IsNull()) return false;
        TopTools_IndexedDataMapOfShapeListOfShape anc;
        TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc);
        TopTools_IndexedMapOfShape edges;
        TopExp::MapShapes(shape, TopAbs_EDGE, edges);
        if (edges.Extent() == 0) return false;  // no edges -> not a solid
        for (int ei = 1; ei <= edges.Extent(); ++ei) {
            const TopoDS_Shape& es = edges.FindKey(ei);
            const int nf = anc.Contains(es) ? anc.FindFromKey(es).Extent() : 0;
            if (nf >= 2) continue;  // shared by two faces -> fine
            const TopoDS_Edge& e = TopoDS::Edge(es);
            if (BRep_Tool::Degenerated(e)) continue;  // apex/pole -> legitimate
            bool seam = false;
            if (anc.Contains(es)) {
                const TopTools_ListOfShape& faces = anc.FindFromKey(es);
                for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next())
                    if (BRep_Tool::IsClosed(e, TopoDS::Face(it.Value()))) { seam = true; break; }
            }
            if (!seam) return false;  // a real free edge -> open shell
        }
        return true;
    } catch (const Standard_Failure&) {
        return false;  // conservative: an OCCT error means we can't vouch for closure
    }
}

// Closure alone cannot detect a blend whose trimmed faces overlap or collapse.
// Match eval_solid_health's non-destructive, per-solid CheckerSI policy, but run
// it only for an actual dress result, before consuming the preceding body.
class DressValidityBudget : public Message_ProgressIndicator {
public:
    DressValidityBudget()
        : deadline_(std::chrono::steady_clock::now() + std::chrono::seconds(20)) {}
    bool broke() const { return broke_.load(std::memory_order_relaxed); }
protected:
    Standard_Boolean UserBreak() override {
        if (std::chrono::steady_clock::now() < deadline_) return Standard_False;
        broke_.store(true, std::memory_order_relaxed);
        return Standard_True;
    }
    void Show(const Message_ProgressScope&, const Standard_Boolean) override {}
private:
    std::chrono::steady_clock::time_point deadline_;
    std::atomic<bool> broke_{false};
};

std::string dress_geometry_rejection(const TopoDS_Shape& shape) {
    const std::vector<std::string> issues = heal_brep_issues(shape);
    if (!issues.empty()) {
        std::string reason = "BRepCheck failed: ";
        for (const std::string& issue : issues)
            reason += (reason.back() == ' ' ? "" : ", ") + issue;
        return reason;
    }
    try {
        TopTools_IndexedMapOfShape faces;
        TopExp::MapShapes(shape, TopAbs_FACE, faces);
        for (int fi = 1; fi <= faces.Extent(); ++fi) {
            GProp_GProps area;
            BRepGProp::SurfaceProperties(faces(fi), area);
            if (!std::isfinite(area.Mass()) || area.Mass() <= Precision::SquareConfusion())
                return "zero-area face at face index " + std::to_string(fi - 1);
        }
        TopTools_IndexedMapOfShape solids;
        TopExp::MapShapes(shape, TopAbs_SOLID, solids);
        if (solids.IsEmpty()) return "result contains no solid";
        Handle(DressValidityBudget) budget = new DressValidityBudget;
        for (int si = 1; si <= solids.Extent(); ++si) {
            BOPAlgo_CheckerSI checker;
            TopTools_ListOfShape one;
            one.Append(solids(si));
            checker.SetArguments(one);
            checker.SetLevelOfCheck(9);
            checker.SetNonDestructive(Standard_True);
            checker.SetRunParallel(Standard_False);
            checker.Perform(budget->Start());
            if (budget->broke())
                return "self-intersection validation incomplete (20 s budget exceeded)";
            if (checker.HasErrors()) {
                std::ostringstream errors;
                checker.DumpErrors(errors);
                return "self-intersection validation failed: " + errors.str();
            }
            if (checker.PDS() == nullptr)
                return "self-intersection validation returned no data structure";
            const BOPDS_DS& ds = *checker.PDS();
            for (BOPDS_MapOfPair::Iterator it(ds.Interferences()); it.More(); it.Next()) {
                Standard_Integer a = 0, b = 0;
                it.Value().Indices(a, b);
                if (ds.IsNewShape(a) || ds.IsNewShape(b)) continue;
                std::ostringstream reason;
                reason << "self-intersection between ";
                TopAbs::Print(ds.Shape(a).ShapeType(), reason);
                reason << " sub-shape index " << a << " and ";
                TopAbs::Print(ds.Shape(b).ShapeType(), reason);
                reason << " sub-shape index " << b;
                return reason.str();
            }
        }
    } catch (const Standard_Failure& failure) {
        const char* message = failure.GetMessageString();
        return std::string("geometry validation failed: ") +
               (message && *message ? message : failure.DynamicType()->Name());
    }
    return {};
}

}  // namespace

// ----------------------------------------------------------------- compile_document
// --------------------------------------------------------------------------- configurations
// Pick the active configuration: {name, config-or-null}. A config-less doc compiles as
// the implicit "Default" (null config). Otherwise active_configuration selects by name;
// a missing/unknown pointer falls back to the FIRST config (SolidWorks always has one
// active). Mirrors compiler.py::resolve_active_config.
std::pair<std::string, const json*> resolve_active_config(const json& doc) {
    static const char* kDefault = "Default";
    if (!doc.contains("configurations") || !doc["configurations"].is_array() ||
        doc["configurations"].empty())
        return {kDefault, nullptr};
    const json& configs = doc["configurations"];
    std::string active = doc.value("active_configuration", std::string());
    if (!active.empty()) {
        for (const json& c : configs)
            if (c.is_object() && c.value("name", std::string()) == active) return {active, &c};
    }
    const json& first = configs[0];
    std::string name = first.is_object() ? first.value("name", std::string()) : std::string();
    return {name.empty() ? kDefault : name, first.is_object() ? &first : nullptr};
}

// Overlay the active config onto `features` and return a NEW effective list: per
// feature, suppressed |= config.suppressions[fid] and params[key] replaced by
// config.param_overrides["fid.key"]. config==null returns features unchanged.
// Mirrors compiler.py::_config_effective_features.
json config_effective_features(const json& features, const json* config) {
    if (!config) return features;
    const json overrides = config->contains("param_overrides") && (*config)["param_overrides"].is_object()
                               ? (*config)["param_overrides"] : json::object();
    const json suppressions = config->contains("suppressions") && (*config)["suppressions"].is_object()
                                  ? (*config)["suppressions"] : json::object();
    if (overrides.empty() && suppressions.empty()) return features;

    // Bucket overrides by feature id: {fid: {param_key: value}}.
    std::map<std::string, json> by_fid;
    for (auto it = overrides.begin(); it != overrides.end(); ++it) {
        const std::string& key = it.key();
        auto dot = key.find('.');
        if (dot == std::string::npos) continue;
        by_fid[key.substr(0, dot)][key.substr(dot + 1)] = it.value();
    }
    json out = json::array();
    for (const json& feat : features) {
        if (!feat.is_object()) { out.push_back(feat); continue; }
        const std::string fid = feat.value("id", std::string());
        auto pit = by_fid.find(fid);
        const bool has_param = pit != by_fid.end();
        const bool has_sup = suppressions.contains(fid);
        if (!has_param && !has_sup) { out.push_back(feat); continue; }
        json ef = feat;
        if (has_sup)
            ef["suppressed"] = feat.value("suppressed", false) || suppressions[fid].get<bool>();
        if (has_param) {
            json params = feat.contains("params") && feat["params"].is_object() ? feat["params"]
                                                                                : json::object();
            for (auto it = pit->second.begin(); it != pit->second.end(); ++it)
                params[it.key()] = it.value();
            ef["params"] = params;
        }
        out.push_back(ef);
    }
    return out;
}

void apply_doc_appearance(const json& doc, uint32_t body_num,
                          float color[3], float& metallic, float& roughness) {
    if (!doc.contains("appearances") || !doc["appearances"].is_object()) return;
    const json& ap = doc["appearances"];
    const std::string key = "b" + std::to_string(body_num);
    if (!ap.contains(key)) return;
    const json& v = ap.at(key);
    auto c01 = [](double x) { return (float)(x < 0.0 ? 0.0 : (x > 1.0 ? 1.0 : x)); };
    auto read_rgb = [&](const json& a) {
        if (a.is_array() && a.size() >= 3 && a[0].is_number())
            for (int i = 0; i < 3; ++i) color[i] = c01(a[i].get<double>());
    };
    if (v.is_array()) {
        read_rgb(v);  // bare [r,g,b]
    } else if (v.is_object()) {
        if (v.contains("color")) read_rgb(v.at("color"));
        if (v.contains("metallic") && v.at("metallic").is_number())
            metallic = c01(v.at("metallic").get<double>());
        if (v.contains("roughness") && v.at("roughness").is_number())
            roughness = c01(v.at("roughness").get<double>());
    }
}

std::string doc_body_material_key(const json& doc, uint32_t body_num) {
    auto key_of = [](const json& m) {
        return m.is_object() && m.contains("key") && m.at("key").is_string()
                   ? m.at("key").get<std::string>() : std::string();
    };
    if (doc.contains("body_materials") && doc.at("body_materials").is_object()) {
        const json& bm = doc.at("body_materials");
        const std::string id = "b" + std::to_string(body_num);
        if (bm.contains(id)) {
            const std::string k = key_of(bm.at(id));
            if (!k.empty()) return k;
        }
    }
    return doc.contains("material") ? key_of(doc.at("material")) : std::string();
}

FaceSidInventory face_sid_inventory(const TopoDS_Shape& shape) {
    FaceSidInventory inventory;
    const auto descriptors = describe_faces(shape);
    TopTools_IndexedMapOfShape faces;
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    size_t ordinal = 0;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next(), ++ordinal) {
        // describe_faces enumerates occurrences, while AICM enumerates unique topology.
        const uint32_t id = static_cast<uint32_t>(faces.FindIndex(ex.Current()));
        json selector = make_selector(descriptors.at(ordinal));
        selector.erase("hint_index");
        selector.erase("owner_feature");
        // Derived readback (cylinder/cone axis point, torus surface); persisted finish
        // fingerprints predate it, so keeping it out leaves every saved face_appearances
        // record matching exactly as before.
        for (const char* key : {"axis_point", "major_radius", "minor_radius", "torus_axis",
                                "torus_center"})
            selector.erase(key);
        auto& candidates = inventory[selector.at("sid").get<std::string>()];
        if (std::none_of(candidates.begin(), candidates.end(),
                         [id](const auto& candidate) { return candidate.first == id; }))
            candidates.emplace_back(id, std::move(selector));
    }
    return inventory;
}

void apply_doc_face_appearances(const json& doc, uint32_t body_num,
                                const TopoDS_Shape& shape, aicm::AicmBody& body) {
    body.appearance_regions.clear();
    const auto all = doc.find("face_appearances");
    const std::string key = "b" + std::to_string(body_num);
    if (all == doc.end() || !all->is_object() || !all->contains(key)) return;
    const auto& records = all->at(key);
    if (!records.is_object() || records.empty()) return;
    const auto inventory = face_sid_inventory(shape);
    std::map<uint32_t, const json*> overrides;
    for (auto it = records.begin(); it != records.end(); ++it) {
        const auto& record = it.value();
        if (!record.is_object() || !record.contains("selector") ||
            !record["selector"].is_object() || !record.contains("appearance")) continue;
        const auto match = inventory.find(record["selector"].value("sid", std::string()));
        if (match == inventory.end()) continue;
        uint32_t face_id = 0;
        size_t matches = 0;
        for (const auto& candidate : match->second)
            if (record["selector"] == candidate.second) { face_id = candidate.first; ++matches; }
        if (matches == 1) overrides.emplace(face_id, &record["appearance"]);
    }
    if (overrides.empty()) return;
    // Full coverage keeps the existing range consumers simple and preserves the
    // body's default finish on unselected faces (including nested assembly sources).
    TopTools_IndexedMapOfShape faces;
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    for (uint32_t id = 1; id <= static_cast<uint32_t>(faces.Extent()); ++id) {
        aicm::AppearanceRegion region;
        region.first_face = id;
        region.face_count = 1;
        std::copy(body.color, body.color + 3, region.color);
        region.metallic = body.metallic;
        region.roughness = body.roughness;
        const auto override = overrides.find(id);
        if (override != overrides.end()) {
            const auto& ap = *override->second;
            const auto& color = ap.is_array() ? ap : ap.at("color");
            for (int k = 0; k < 3; ++k) region.color[k] = color.at(k).get<float>();
            if (ap.is_object()) {
                region.metallic = ap.value("metallic", region.metallic);
                region.roughness = ap.value("roughness", region.roughness);
            }
        }
        if (!body.appearance_regions.empty()) {
            auto& previous = body.appearance_regions.back();
            if (std::equal(previous.color, previous.color + 3, region.color) &&
                previous.metallic == region.metallic && previous.roughness == region.roughness) {
                ++previous.face_count;
                continue;
            }
        }
        body.appearance_regions.push_back(region);
    }
}

CompileResult compile_document(const json& doc, const json& env_json, CompileCache* cache,
                               ::geom::DisplayQuality quality, bool with_display,
                               bool with_owners) {
    CompileResult result;
    const EnvMap env = parse_env(env_json);
    // Face/edge ownership is tracked for the host owner_map (display) or for a component
    // owner lookup (with_owners); a plain geometry-only component compile skips it.
    const bool attribute_owners = with_display || with_owners;

    // The shared rebuild context: per-feature results keyed by id, plus the set of
    // solids consumed by a later feature (they no longer survive as standalone bodies).
    // FeatureResultMap = shared_ptr slots so checkpoints share unchanged entries.
    FeatureResultMap results;
    std::set<std::string> consumed;
    // Lineage of an in-place transform: when a `copy:false` transform CONSUMES a source body
    // and produces a moved replacement, record source-id -> new-id here. A LATER transform that
    // targets the same originating id then composes on the current descendant instead of
    // re-reading the (still-in-results) original and spawning a duplicate body (Issue 4: "a
    // second transform on an already-moved body duplicates it"). Only in-place transforms add
    // links; a copy:true transform leaves the source live (instancing) and adds no link.
    std::map<std::string, std::string> superseded_by;
    // Surviving solid ids in first-produced order, so the final body_id assignment
    // (1..N) and AICM body order are deterministic across rebuilds.
    std::vector<std::string> solid_order;

    // Body KIND per feature id ("solid" default; "surface" for open sheet bodies the
    // surf_* compilers produce). The surviving-bodies loop reads this to report AREA +
    // face/edge counts (surfaces) vs VOLUME (solids), the surfacing parity-gate signal.
    std::map<std::string, std::string> body_kinds;

    // The body each MODIFYING feature derived from (fillet/shell/boolean/... -> its
    // input solid id). Empty/absent for a CREATING feature (extrude boss/revolve/
    // sweep/loft/primitive build a fresh body). Drives the owner-map topology diff.
    std::map<std::string, std::string> source_of;

    // The TOOL BODY + boolean OP each patternable feature contributed (the C++
    // analogue of the oracle's ctx.tool_bodies / feature_ops — engine/doc/compiler.py).
    // A pattern/mirror REPLICATES the SEED feature's tool body re-applying its op, so a
    // patterned CUT subtracts N times (N holes) and a patterned boss fuses N times —
    // exactly like SolidWorks, and matching engine/doc/features/pattern.py. Without this
    // a pattern that fused copies of the post-cut SOLID would give a totally wrong body.
    // tool body is stored at its instance-0 pose (the cut tool / boss prism / drilled
    // tool union); a pattern/mirror records its WHOLE result (op "add") so nesting works.
    std::map<std::string, TopoDS_Shape> tool_body_of;
    std::map<std::string, std::string> feature_op_of;  // "add" | "cut"

    // B1 — feature-overlap detection: record each feature's actual pre-boolean tool
    // solid + its add/cut role into result.tool_recs (see ToolRec in document.h).
    // Deliberately independent of tool_body_of/feature_op_of above: those are tuned
    // for pattern/mirror REPLAY (an additive pattern's tool_body_of is its whole
    // grown body, so an outer pattern can re-fuse it), whereas doc_eval_feature_
    // overlap needs the real incremental geometry a feature added or removed so a
    // pairwise BRepAlgoAPI_Common between two records measures genuine overlap.
    // A null shape is never recorded (defensive; every callsite already guards
    // IsNull before this point).
    auto record_tool = [&](const std::string& fid, const char* role, const TopoDS_Shape& shp) {
        if (!shp.IsNull()) result.tool_recs.push_back(ToolRec{fid, role, shp});
    };

    // Feature DISPLAY names seen so far (Bug K9): a duplicate name in one doc is
    // confusing (the tree shows two "Boss-Extrude1"). We don't hard-error (it would
    // break existing flows) — instead the SECOND+ occurrence carries a status.warning.
    std::set<std::string> seen_names;

    // --- sheet-metal part state (the C++ analogue of the oracle's ctx.datums
    // _sheet_metal / _sheet_metal_bends). A base flange establishes the part as
    // sheet metal (thickness / k-factor / default bend radius); later edge flanges /
    // hems / jogs inherit it and each appends a bend record the flat pattern reads. ---
    struct SheetCtx {
        bool active = false;
        double thickness = 0.0;
        double k_factor = 0.0;
        double default_bend_radius = 0.0;
    } sheet_ctx;
    std::vector<::geom::BendRecord> bend_records;

    // The "current top body": the most-recently-produced solid not yet consumed.
    // Canonical features that omit an explicit body/seed ref (fillet/chamfer/hole/
    // shell/draft/mirror/pattern, and extrude-cut) operate on it (SolidWorks "the
    // running body"). Returns "" when no solid exists yet.
    auto current_solid_id = [&]() -> std::string {
        for (auto it = solid_order.rbegin(); it != solid_order.rend(); ++it)
            if (!consumed.count(*it)) return *it;
        return std::string();
    };
    // Shared solid-feature body policy; see contracts/doc_schema.md "Solid feature modes".
    auto solid_mode = [](const json& feat, const char* fallback) {
        std::string mode = feat.value("mode", std::string(fallback));
        if (mode != "new" && mode != "boss" && mode != "add" && mode != "cut")
            throw std::runtime_error(feat.value("type", std::string("feature")) +
                ": unknown mode '" + mode + "'. Solid feature modes: new, add, boss, cut");
        return feat.value("merge", true) ? mode : std::string("new");
    };
    struct BodyTargets {
        std::vector<std::string> ids;
        TopoDS_Shape shape;
    };
    auto solid_targets = [&](const TopoDS_Shape& tool, const std::string& mode) {
        BodyTargets targets;
        if (mode == "new") return targets;  // Never even test existing geometry.
        if (mode == "cut") {
            const std::string id = current_solid_id();
            if (!id.empty()) {
                targets.ids.push_back(id);
                targets.shape = require_solid(results, id, "target");
            }
            return targets;
        }
        Bnd_Box tool_box;
        BRepBndLib::Add(tool, tool_box, Standard_False);
        for (const std::string& id : solid_order) {
            if (consumed.count(id)) continue;
            const TopoDS_Shape& body = require_solid(results, id, "target");
            Bnd_Box body_box;
            BRepBndLib::Add(body, body_box, Standard_False);
            if (tool_box.IsOut(body_box)) continue;
            BRepExtrema_DistShapeShape distance(body, tool);
            if (!distance.IsDone())
                throw std::runtime_error("add: could not determine contact with body '" + id + "'");
            if (distance.Value() > Precision::Confusion()) continue;
            targets.ids.push_back(id);
            if (targets.shape.IsNull()) {
                targets.shape = body;
            } else {
                if (targets.ids.size() == 2) {
                    TopoDS_Compound compound;
                    BRep_Builder builder;
                    builder.MakeCompound(compound);
                    builder.Add(compound, targets.shape);
                    targets.shape = compound;
                }
                BRep_Builder().Add(targets.shape, body);
            }
        }
        return targets;
    };
    // The body a dress/hole/mirror/transform feature targets: an explicit "body"
    // ref or, for canonical features that carry no body ref, the current top body.
    // The body ref may arrive as a bare id string ("f3") OR a canonical body
    // selector object {"kind":"body","owner_feature":"f3"} — accept BOTH via
    // selector_owner_id. Previously this only honored an is_string() body, so a
    // selector OBJECT (top-level or via refs) silently fell through to the current
    // body — the assembly `transform body:"fN"` mistargeting bug where every
    // batched per-body move stacked onto the last-imported body instead. Empty
    // when neither resolves -> the branch raises a clear error.
    auto body_ref = [&](const json& feat) -> std::string {
        if (feat.contains("body")) {
            const json& bval = feat["body"];
            std::string bid = selector_owner_id(bval);
            if (!bid.empty()) return bid;
            // E10: body is present but did not resolve to a feature id — the caller passed
            // something that looks like a numeric index (body:2) or an unrecognised selector
            // object. Silently falling through to current_solid_id() would move the WRONG
            // body (the assembly mistargeting pattern). Return a sentinel that causes the
            // caller's require_solid to fire a clear, actionable error instead.
            // We encode the sentinel as a special string so require_solid's "not found"
            // message carries the original value for diagnosis.
            std::string raw;
            if (bval.is_number_integer()) raw = std::to_string(bval.get<int>());
            else if (bval.is_number()) raw = std::to_string(bval.get<double>());
            else if (bval.is_string()) raw = bval.get<std::string>();
            else raw = bval.dump();
            // Return an impossible feature id so require_solid throws:
            //   ref 'body' -> '<body_ref_invalid: 2>' not found
            // The message tells the caller exactly what to fix.
            return "<body_ref_invalid: use a feature id like 'f3' (from get_part), "
                   "not: " + raw + ">";
        }
        return current_solid_id();
    };
    // Resolve a pattern/mirror SEED feature to its (tool body, op). Mirrors the oracle's
    // pattern._gather_seeds: a seed must be an earlier feature that contributed
    // patternable geometry (boss/cut/hole/revolve, or another pattern/mirror). Raises a
    // dangling-style error otherwise, so the per-feature try/catch fails the pattern the
    // SAME way the oracle does (e.g. when the seed id points at a sketch).
    auto seed_tool_op = [&](const json& feat, const char* what)
        -> std::pair<TopoDS_Shape, std::string> {
        const std::string seed_id = req_str(feat, "seed");
        auto it = tool_body_of.find(seed_id);
        if (it == tool_body_of.end() || it->second.IsNull())
            throw std::runtime_error(
                std::string(what) + ": feature '" + seed_id + "' has no patternable "
                "geometry (must be an earlier boss/cut/hole, and listed before this feature)");
        const std::string op =
            feature_op_of.count(seed_id) ? feature_op_of[seed_id] : std::string("add");
        return {it->second, op};
    };

    // --- surfacing helpers (engine/doc/features/surf_*.py) -----------------------
    // Resolve a `profile` ref (sketch id or region selector after normalize_feature)
    // to its planar faces — same region semantics as extrude/revolve/sweep.
    auto surf_profile_faces = [&](const json& feat) -> std::vector<TopoDS_Face> {
        return resolve_profile_faces(results, feat);
    };
    // Planar-face normal (build123d face.normal_at() analogue): the geometric surface
    // normal at the face's UV midpoint, used to set the surf_extrude sweep direction.
    auto src_face_normal = [&](const TopoDS_Face& f) -> Eigen::Vector3d {
        Standard_Real u0, u1, v0, v1;
        BRepTools::UVBounds(f, u0, u1, v0, v1);
        Handle(Geom_Surface) gs = BRep_Tool::Surface(f);
        Eigen::Vector3d n(0, 0, 1);
        if (!gs.IsNull()) {
            gp_Pnt p;
            gp_Vec du, dv;
            gs->D1((u0 + u1) / 2.0, (v0 + v1) / 2.0, p, du, dv);
            gp_Vec nv = du.Crossed(dv);
            if (nv.Magnitude() > 1e-12) {
                nv.Normalize();
                n = Eigen::Vector3d(nv.X(), nv.Y(), nv.Z());
            }
        }
        if (f.Orientation() == TopAbs_REVERSED) n = -n;
        return n;
    };

    // --- datum resolvers (engine/doc/features/datum.py) --------------------------
    // Resolve a ref to a 3-D POINT: a reserved "origin" datum, a point-datum feature
    // id, or a {"kind":"datum","feature":id} selector pointing at a point feature.
    // Throws (-> error status) on anything not resolvable to a point here (geometry
    // vertex selectors are not ported — they raise a clear, contained error).
    auto resolve_point = [&](const json& ref, const char* slot) -> Eigen::Vector3d {
        std::string fid;
        if (ref.is_string()) fid = ref.get<std::string>();
        else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
                 ref.contains("feature") && ref["feature"].is_string())
            fid = ref["feature"].get<std::string>();
        if (fid == "origin" || fid == "point.origin") return Eigen::Vector3d(0, 0, 0);
        if (!fid.empty()) {
            auto it = results.find(fid);
            if (it != results.end() && std::holds_alternative<PointResult>(*it->second))
                return std::get<PointResult>(*it->second).pt;
        }
        throw std::runtime_error(std::string(slot) +
            ": expected a point datum (geometry vertex selectors not yet ported here)");
    };
    // Resolve a ref to a datum AXIS (point, unit dir): a reserved axis.x/y/z, an
    // axis-datum feature id, or a {"kind":"datum",...} selector. nullopt otherwise.
    auto resolve_axis_ref =
        [&](const json& ref) -> std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> {
        if (auto ax = axis_from_datum(ref)) return ax;     // reserved axis.x/y/z
        if (auto ax = axis_from_explicit(ref)) return ax;  // explicit {axis:[..],point?}
        std::string fid;
        if (ref.is_string()) fid = ref.get<std::string>();
        else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
                 ref.contains("feature") && ref["feature"].is_string())
            fid = ref["feature"].get<std::string>();
        if (!fid.empty()) {
            auto it = results.find(fid);
            if (it != results.end() && std::holds_alternative<AxisResult>(*it->second)) {
                const AxisResult& a = std::get<AxisResult>(*it->second);
                return std::make_pair(a.point, a.dir);
            }
        }
        return std::nullopt;
    };
    // Resolve a ref to a datum PLANE (origin, normal): a reserved plane.top/front/
    // right, OR a plane/csys datum feature id (its SketchPlane). nullopt otherwise.
    auto resolve_plane_ref =
        [&](const json& ref) -> std::optional<chudware::geom::SketchPlane> {
        std::string fid;
        if (ref.is_string()) fid = ref.get<std::string>();
        else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
                 ref.contains("feature") && ref["feature"].is_string())
            fid = ref["feature"].get<std::string>();
        if (!fid.empty()) {
            auto it = results.find(fid);
            if (it != results.end() && std::holds_alternative<PlaneResult>(*it->second))
                return std::get<PlaneResult>(*it->second).plane;
        }
        std::string name = fid;
        if (name.rfind("plane.", 0) == 0) name = name.substr(6);
        if (name == "top" || name == "front" || name == "right")
            return plane_from_name(name);
        return std::nullopt;
    };

    // --- selector-driven datum helpers (engine/doc/features/datum.py) -------------
    // The oracle distinguishes a DATUM ref (a reserved/feature-id string, or a
    // {"kind":"datum",...} selector) from a geometry TOPOLOGY selector
    // ({"kind":"face|edge|vertex",...}); the latter resolves against the last body via
    // the selectors/picks machinery. Port of datum.py::_is_datum_ref.
    auto is_datum_ref = [](const json& ref) -> bool {
        return ref.is_string() ||
               (ref.is_object() && ref.value("kind", std::string()) == "datum");
    };
    // The running body a geometry selector resolves against (datum.py::_last_body =
    // ctx.bodies[-1]). Throws (-> contained error status) when no solid exists yet.
    auto last_body_shape = [&](const char* what) -> const TopoDS_Shape& {
        const std::string bid = current_solid_id();
        if (bid.empty())
            throw std::runtime_error(std::string(what) +
                                     ": no body to resolve a geometry selector against");
        return require_solid(results, bid, what);
    };
    // Resolve a FACE selector against the running body -> the unique TopoDS_Face, via
    // the SAME resolve_face_picks path the dress/hole compilers use. Throws with the
    // dangling reason on a non-resolving selector (datum.py::_resolve_sel "face").
    auto resolve_face_sel = [&](const json& sel, const char* slot) -> TopoDS_Face {
        const TopoDS_Shape& body = last_body_shape(slot);
        // A plain-language geometry QUERY ({"of":"face","side":"top",...}) resolves the same
        // way a dressed feature's query does — so any face REF (sketch plane, hole target,
        // draft neutral, mirror plane, datum base) can be named "the top face" in words
        // instead of carrying a clicked centroid selector. (A real selector has no "of".)
        if (chudware::doc::is_geom_query(sel)) {
            chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(body, sel);
            if (!qp.error.empty())
                throw std::runtime_error(std::string(slot) + ": " + qp.error);
            if (qp.faces.empty())
                throw std::runtime_error(std::string(slot) + ": query matched no face");
            return qp.faces.front();
        }
        chudware::doc::FacePicks fp = chudware::doc::resolve_face_picks(body, sel, slot);
        if (fp.faces.empty()) {
            std::string reason = fp.dangling.empty() ? std::string("unresolved")
                                                     : fp.dangling.front();
            throw std::runtime_error(std::string(slot) + ": face selector dangling — " + reason);
        }
        return fp.faces.front();
    };
    // Resolve an EDGE selector against the running body -> the unique TopoDS_Edge.
    auto resolve_edge_sel = [&](const json& sel, const char* slot) -> TopoDS_Edge {
        const TopoDS_Shape& body = last_body_shape(slot);
        if (chudware::doc::is_geom_query(sel)) {  // word-vocabulary query (see resolve_face_sel)
            chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(body, sel);
            if (!qp.error.empty())
                throw std::runtime_error(std::string(slot) + ": " + qp.error);
            if (qp.edges.empty())
                throw std::runtime_error(std::string(slot) + ": query matched no edge");
            return qp.edges.front();
        }
        chudware::doc::EdgePicks ep = chudware::doc::resolve_edge_picks(body, sel);
        if (ep.edges.empty()) {
            std::string reason = ep.dangling.empty() ? std::string("unresolved")
                                                     : ep.dangling.front();
            throw std::runtime_error(std::string(slot) + ": edge selector dangling — " + reason);
        }
        return ep.edges.front();
    };
    // Resolve a curve ref for the ruled/fill/boundary surface forms to a wire. Two ref
    // shapes are accepted:
    //   (1) a SKETCH/region ref (bare sketch id or {kind:region,sketch:fN}) — build a
    //       wire from the whole solved sketch (the MCP path + profile-well GUI picks);
    //   (2) a BODY-EDGE selector (the K_EDGE ribbon wells emit these) — resolve the
    //       picked edge against its body and lift it into a single-edge wire. This is the
    //       SolidWorks-style "ruled/boundary surface between body edges" UX the forms
    //       advertise; previously the engine rejected it ("… is a solid body, not a
    //       sketch"). Edge resolution reuses resolve_edge_sel (geom-query + dangling
    //       errors + last_body_shape), so cross-body picks resolve against the most-recent
    //       body — the v1 limit; a single-body ruled/fill/boundary is the common case.
    // Discriminator: owner resolves to a SketchResult -> sketch path; ANYTHING ELSE
    // (including a null-owner raw-geometry selector, selector_owner_id() == "") -> edge
    // path, so a body-edge pick is never misrouted into require_sketch.
    auto surf_sketch_wire = [&](const json& ref, const char* slot) -> TopoDS_Wire {
        const std::string sk = selector_owner_id(ref);
        auto it = sk.empty() ? results.end() : results.find(sk);
        if (it != results.end() && std::holds_alternative<SketchResult>(*it->second)) {
            const SketchResult& src = std::get<SketchResult>(*it->second);
            TopoDS_Wire w = chudware::geom::wire_from_solved_sketch(src.entities, src.plane);
            if (w.IsNull())
                throw std::runtime_error(std::string("surf: ") + slot + " sketch produced no wire");
            return w;
        }
        TopoDS_Edge e = resolve_edge_sel(ref, slot);
        TopoDS_Wire w = BRepBuilderAPI_MakeWire(e).Wire();
        if (w.IsNull())
            throw std::runtime_error(std::string("surf: ") + slot + " edge produced no wire");
        return w;
    };
    // Validate that any FEATURE id a selector names (refs.<slot>.feature / owner_feature)
    // refers to a feature that actually exists in this compile (Bug K4). The picks
    // machinery scores a selector by its stamped geometry (centroid/length/normal) and a
    // feature OWNER is only a soft tiebreak — so a selector that names a NON-EXISTENT
    // feature ("f999") still resolves to whatever geometry scores best and the dress
    // silently mutilates the wrong edge (an open shell that reported ok=true). Reject the
    // dangling feature reference LOUDLY before the selector is resolved. Recurses into a
    // selector list. (A reserved/word selector carries no feature id and passes through.)
    std::function<void(const json&, const char*)> validate_selector_refs =
        [&](const json& sel, const char* slot) {
        if (sel.is_array()) {
            for (const json& e : sel) validate_selector_refs(e, slot);
            return;
        }
        if (!sel.is_object()) return;
        for (const char* key : {"feature", "owner_feature"}) {
            auto it = sel.find(key);
            if (it != sel.end() && it->is_string()) {
                const std::string fid = it->get<std::string>();
                if (!fid.empty() && fid != "origin" && results.find(fid) == results.end())
                    throw std::runtime_error(std::string(slot) +
                        ": selector references unknown feature " + fid);
            }
        }
    };
    // (point, unit direction) of a straight model edge — datum.py::_edge_line_dir
    // (endpoint chord). Throws on a degenerate edge.
    auto edge_line_dir =
        [](const TopoDS_Edge& edge, const char* slot)
        -> std::pair<Eigen::Vector3d, Eigen::Vector3d> {
        BRepAdaptor_Curve ad(edge);
        gp_Pnt p0 = ad.Value(ad.FirstParameter());
        gp_Pnt p1 = ad.Value(ad.LastParameter());
        Eigen::Vector3d a(p0.X(), p0.Y(), p0.Z());
        Eigen::Vector3d b(p1.X(), p1.Y(), p1.Z());
        Eigen::Vector3d d = b - a;
        if (d.norm() < 1e-9)
            throw std::runtime_error(std::string(slot) + ": degenerate (zero-length) edge");
        return {a, d};
    };
    // Axis (location, unit direction) of a cylindrical/conical face — datum.py::_face_axis.
    auto face_axis =
        [](const TopoDS_Face& face, const char* slot)
        -> std::pair<Eigen::Vector3d, Eigen::Vector3d> {
        BRepAdaptor_Surface ad(face);
        GeomAbs_SurfaceType st = ad.GetType();
        gp_Ax1 ax;
        if (st == GeomAbs_Cylinder) ax = ad.Cylinder().Axis();
        else if (st == GeomAbs_Cone) ax = ad.Cone().Axis();
        else throw std::runtime_error(std::string(slot) +
                 ": face is not cylindrical/conical (axis undefined)");
        gp_Pnt loc = ax.Location();
        gp_Dir dr = ax.Direction();
        return {Eigen::Vector3d(loc.X(), loc.Y(), loc.Z()),
                Eigen::Vector3d(dr.X(), dr.Y(), dr.Z())};
    };
    // (point, unit tangent) on a model edge's underlying curve — datum.py
    // ::_curve_point_tangent. Landing on the curve (first non-null wins): a raw curve
    // parameter (clamped to the edge range), a 0..1 fraction along that range, or the
    // nearest foot of a 3-D point projected onto the curve. Uses BRepAdaptor_Curve D1
    // (point + first derivative); falls back to a chord toward the far end when the
    // derivative is degenerate. Throws on a fully degenerate tangent.
    auto curve_point_tangent =
        [](const TopoDS_Edge& edge, std::optional<double> fraction,
           std::optional<double> param, std::optional<Eigen::Vector3d> near_pt,
           const char* slot) -> std::pair<Eigen::Vector3d, Eigen::Vector3d> {
        BRepAdaptor_Curve ad(edge);
        const double u0 = ad.FirstParameter();
        const double u1 = ad.LastParameter();
        double u;
        if (param) {
            u = std::max(u0, std::min(u1, *param));
        } else if (near_pt) {
            u = 0.5 * (u0 + u1);
            try {
                Handle(Geom_Curve) geom = ad.Curve().Curve();
                if (!geom.IsNull()) {
                    GeomAPI_ProjectPointOnCurve proj(
                        gp_Pnt(near_pt->x(), near_pt->y(), near_pt->z()), geom);
                    if (proj.NbPoints() > 0) u = proj.LowerDistanceParameter();
                }
            } catch (...) { /* keep midpoint fallback */ }
            u = std::max(u0, std::min(u1, u));
        } else {
            double fr = fraction ? *fraction : 0.5;
            fr = std::max(0.0, std::min(1.0, fr));
            u = u0 + fr * (u1 - u0);
        }
        gp_Pnt p;
        gp_Vec dv;
        ad.D1(u, p, dv);
        Eigen::Vector3d point(p.X(), p.Y(), p.Z());
        Eigen::Vector3d tangent(dv.X(), dv.Y(), dv.Z());
        if (tangent.norm() < 1e-9) {
            gp_Pnt far_pt = ad.Value(std::abs(u - u0) < std::abs(u - u1) ? u1 : u0);
            tangent = Eigen::Vector3d(far_pt.X() - point.x(), far_pt.Y() - point.y(),
                                      far_pt.Z() - point.z());
        }
        if (tangent.norm() < 1e-9)
            throw std::runtime_error(std::string(slot) +
                ": cannot evaluate a curve tangent (degenerate curve)");
        return {point, tangent.normalized()};
    };
    // (surface point, unit outward normal) on a face nearest `near` — datum.py
    // ::_surface_point_normal. Projects `near` onto the face's Geom_Surface
    // (GeomAPI_ProjectPointOnSurf), evaluates the normal at the projected UV via
    // GeomLProp_SLProps, then flips it to agree with the face's stored orientation
    // (a reversed face still yields its true outward normal). For a planar face this
    // is the constant plane normal regardless of `near`.
    auto surface_point_normal =
        [](const TopoDS_Face& face, const Eigen::Vector3d& near_pt, const char* slot)
        -> std::pair<Eigen::Vector3d, Eigen::Vector3d> {
        Handle(Geom_Surface) surf = BRep_Tool::Surface(face);
        if (surf.IsNull())
            throw std::runtime_error(std::string(slot) + ": face has no surface");
        GeomAPI_ProjectPointOnSurf proj(gp_Pnt(near_pt.x(), near_pt.y(), near_pt.z()), surf);
        if (!proj.IsDone() || proj.NbPoints() < 1)
            throw std::runtime_error(std::string(slot) +
                ": could not project the point onto the surface");
        Standard_Real u, v;
        proj.LowerDistanceParameters(u, v);
        gp_Pnt sp = proj.NearestPoint();
        Eigen::Vector3d point(sp.X(), sp.Y(), sp.Z());
        GeomLProp_SLProps sl(surf, u, v, 1, 1e-7);
        if (!sl.IsNormalDefined())
            throw std::runtime_error(std::string(slot) +
                ": surface normal is undefined at the projected point");
        gp_Dir n = sl.Normal();
        Eigen::Vector3d normal(n.X(), n.Y(), n.Z());
        if (normal.norm() < 1e-9)
            throw std::runtime_error(std::string(slot) + ": degenerate surface normal");
        if (face.Orientation() == TopAbs_REVERSED) normal = -normal;
        return {point, normal.normalized()};
    };
    // The meeting point of two TopoDS shapes via BRepExtrema_DistShapeShape — datum.py
    // ::_shape_shape_point. Respects edge/face TRIMMING exactly: when the shapes touch
    // the minimal distance is ~0 and the solution point IS the intersection; a gap above
    // `touch_tol` (1e-4 mm) means the shapes do not actually meet -> contained error.
    // Returns PointOnShape1(1), matching the oracle.
    auto shape_shape_point =
        [](const TopoDS_Shape& a, const TopoDS_Shape& b, const char* slot,
           const char* what) -> Eigen::Vector3d {
        constexpr double kTouchTol = 1e-4;
        BRepExtrema_DistShapeShape d(a, b);
        if (!d.IsDone() || d.NbSolution() < 1)
            throw std::runtime_error(std::string(slot) + ": " + what +
                                     " have no common point");
        if (d.Value() > kTouchTol)
            throw std::runtime_error(std::string(slot) + ": " + what +
                                     " do not intersect");
        gp_Pnt p = d.PointOnShape1(1);
        return Eigen::Vector3d(p.X(), p.Y(), p.Z());
    };
    // A planar face -> (origin, outward normal), respecting the face orientation, so a
    // datum plane can be built on it. Mirrors datum.py::_plane_of's Plane(face) path:
    // build123d uses the face centroid as the plane origin + the orientation-aware
    // surface normal as z_dir. Throws on a non-planar face.
    auto face_plane = [&](const TopoDS_Face& face, const char* slot)
        -> chudware::geom::SketchPlane {
        BRepAdaptor_Surface ad(face);
        if (ad.GetType() != GeomAbs_Plane)
            throw std::runtime_error(std::string(slot) + ": face is not planar");
        Eigen::Vector3d n = src_face_normal(face);  // orientation-aware unit normal
        GProp_GProps props;
        BRepGProp::SurfaceProperties(face, props);
        gp_Pnt cm = props.CentreOfMass();
        return chudware::geom::SketchPlane::from(Eigen::Vector3d(cm.X(), cm.Y(), cm.Z()), n);
    };
    // Resolve a "planar datum OR planar face selector" ref to a SketchPlane
    // (datum.py::_plane_of). A datum ref goes through resolve_plane_ref; a face
    // selector resolves against the last body.
    auto plane_of = [&](const json& ref, const char* slot) -> chudware::geom::SketchPlane {
        if (is_datum_ref(ref)) {
            auto p = resolve_plane_ref(ref);
            if (!p)
                throw std::runtime_error(std::string(slot) +
                                         ": datum is not a planar datum");
            return *p;
        }
        return face_plane(resolve_face_sel(ref, slot), slot);
    };
    // A 3-D point from a vertex selector OR a point/vertex datum ref
    // (datum.py::_vertex_point). A datum ref goes through resolve_point; a vertex
    // selector resolves against the last body (describe_vertices + resolve).
    auto vertex_point = [&](const json& ref, const char* slot) -> Eigen::Vector3d {
        if (is_datum_ref(ref)) return resolve_point(ref, slot);
        const TopoDS_Shape& body = last_body_shape(slot);
        std::vector<chudware::doc::TopoInfo> infos = chudware::doc::describe_vertices(body);
        const double diag = chudware::doc::bbox_diag(body);
        std::optional<int> idx = chudware::doc::resolve_with_hint_fallback(ref, infos, diag);
        if (!idx) {
            chudware::doc::ResolveResult rr = chudware::doc::resolve(ref, infos, diag);
            throw std::runtime_error(std::string(slot) + ": vertex selector dangling — " +
                                     rr.dangling_reason.value_or(std::string("unresolved")));
        }
        // describe_vertices enumerates one descriptor per VERTEX occurrence (a box has
        // 48 for 8 corners); map the resolved descriptor's centroid to the nearest
        // unique vertex of the body, the same de-dup the edge/face pick path uses.
        const Eigen::Vector3d& target = infos[*idx].centroid;
        TopTools_IndexedMapOfShape uniq;
        TopExp::MapShapes(body, TopAbs_VERTEX, uniq);
        Eigen::Vector3d best(0, 0, 0);
        double best_d2 = std::numeric_limits<double>::infinity();
        for (int i = 1; i <= uniq.Extent(); ++i) {
            gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(uniq(i)));
            Eigen::Vector3d v(p.X(), p.Y(), p.Z());
            double d2 = (v - target).squaredNorm();
            if (d2 < best_d2) { best_d2 = d2; best = v; }
        }
        if (!std::isfinite(best_d2))
            throw std::runtime_error(std::string(slot) + ": body has no vertices");
        return best;
    };

    // --- post-compile solid sanity (Bug N2) -------------------------------------
    // A deep feature tree could silently commit a B-rep with INVERTED shell
    // orientation: VolumeProperties returns a NEGATIVE mass, mass/CoM/inertia all
    // come out wrong, yet every feature reported ok:true with no geometry-check
    // issue. A committed solid must NEVER have negative volume. After a feature
    // produces/updates a solid we validate it here:
    //   1. measure the signed volume.
    //   2. if >= 0 (and finite) it is fine — return unchanged.
    //   3. if NEGATIVE, try to HEAL the orientation:
    //        a. ShapeFix_Solid (re-orients shells so the solid encloses a positive
    //           volume — the kosher fix), then
    //        b. as a fallback, Reversed() (flip the whole solid's orientation),
    //      re-measuring after each. The first that yields a positive volume wins
    //      and REPLACES the stored shape (with a status.warning recording the heal).
    //   4. if neither heals it, FAIL the feature LOUDLY (caught by the per-feature
    //      try/catch -> error status). Never silently commit the corrupt solid.
    // Returns true on a healed solid (so the caller can flag a warning); false when
    // the solid was already valid. Throws when the solid is invalid AND unhealable.
    auto sanitize_solid = [&](const std::string& sid, const std::string& fname) -> bool {
        auto rit = results.find(sid);
        if (rit == results.end() || !std::holds_alternative<SolidResult>(*rit->second))
            return false;
        // SURFACE (open sheet) bodies have no meaningful enclosed volume — the signed
        // value VolumeProperties returns for an open shell is not an inversion signal.
        // Skip them; only closed SOLID bodies are validated for negative volume.
        auto kit = body_kinds.find(sid);
        if (kit != body_kinds.end() && kit->second == "surface") return false;
        TopoDS_Shape& shape = std::get<SolidResult>(*rit->second).shape;
        if (shape.IsNull()) return false;

        auto signed_vol = [](const TopoDS_Shape& s) -> double {
            GProp_GProps gp;
            BRepGProp::VolumeProperties(s, gp);
            return gp.Mass();
        };
        const double v0 = signed_vol(shape);
        // A finite, non-negative volume is healthy. (A zero-volume body is its own
        // problem handled by the producing branch; we only police NEGATIVE here.)
        if (std::isfinite(v0) && v0 >= 0.0) return false;

        // --- try to heal an inverted solid back to a positive volume ---
        // Candidate 1: ShapeFix_Solid (proper shell re-orientation).
        try {
            if (shape.ShapeType() == TopAbs_SOLID) {
                ShapeFix_Solid sfx(TopoDS::Solid(shape));
                sfx.Perform();
                TopoDS_Shape fixed = sfx.Solid();
                if (!fixed.IsNull()) {
                    double v = signed_vol(fixed);
                    if (std::isfinite(v) && v > 0.0) { shape = fixed; return true; }
                }
            }
        } catch (...) { /* fall through to the orientation flip */ }
        // Candidate 2: flip the whole solid's orientation (Reversed). This directly
        // un-inverts a solid whose shells are inside-out.
        try {
            TopoDS_Shape rev = shape.Reversed();
            if (!rev.IsNull()) {
                double v = signed_vol(rev);
                if (std::isfinite(v) && v > 0.0) { shape = rev; return true; }
            }
        } catch (...) { /* unhealable -> error below */ }

        // Unhealable: refuse to commit a negative-volume solid.
        throw std::runtime_error(
            "feature " + (fname.empty() ? sid : fname) +
            " produced an inverted/invalid solid (negative volume " +
            std::to_string(v0) + " mm^3) — refusing to commit a corrupt B-rep");
    };

    const json empty = json::array();
    const json& base_features = doc.contains("features") ? doc.at("features") : empty;

    // Configuration overlay: compile the ACTIVE config's effective features (param
    // overrides + suppression). A config-less doc returns the base list unchanged.
    auto [active_cfg_name, active_cfg] = resolve_active_config(doc);
    result.active_configuration = active_cfg_name;
    const json features = config_effective_features(base_features.is_array() ? base_features : empty,
                                                    active_cfg);

    // Per-compile cache of deserialized embedded-BREP imports, keyed by FNV-1a of the
    // blob — not the multi-MB blob string itself. An assembly that imports
    // the same part N times (e.g. 4 identical wheels) carries N byte-identical blobs;
    // deserializing once and SHARING the resulting TShape across all N imports means
    // the later transform/tessellate/mass-prop steps operate on shared geometry and
    // dedup automatically. The shapes are only ever read or rigidly relocated
    // downstream (never mutated in place), so sharing is safe.
    ImportCache import_cache;
    // Incremental owner maps — rebuilt only for solids produced by executed
    // features; restored from the checkpoint for the reused prefix.
    OwnerTopoMap face_owners, edge_owners;
    // Status chain as shared_ptrs so capture_ckpt does not re-copy prior statuses.
    std::vector<std::shared_ptr<const FeatureStatus>> status_ptrs;

    // ------------------------------------------ incremental-rebuild cache probe
    // Hash the compile INPUTS into a chain (h0 = env + active config; h[i+1] = h[i]
    // folded with feature i's json — the loop's only per-feature inputs, verified: the
    // loop body reads nothing else from `doc`). Then (a) an identical chain + identical
    // appearances returns the previous CompileResult outright, and (b) otherwise the
    // deepest checkpoint whose chain prefix still matches is RESTORED and the loop
    // starts after it — editing feature k recompiles only k..N. Content-addressed:
    // undo/redo/config switches/rolled-back failed compiles all just hash to whatever
    // prefix genuinely still matches.
    const std::size_t n_features = features.is_array() ? features.size() : 0;
    const bool use_cache = cache != nullptr && n_features > 0 &&
                           n_features <= kCompileCacheMaxFeatures;
    g_cc_compiles.fetch_add(1, std::memory_order_relaxed);
    std::vector<uint64_t> chain;
    std::size_t start_at = 0;
    if (use_cache) {
        chain = cc_chain(features, env_json, result.active_configuration, quality,
                         with_display, with_owners);
        // (a) FULL match — same env/config/features and same appearances (they bake into
        // the post-loop AICM colors) -> serve the whole previous result.
        const uint64_t full_key = cc_full_key(doc, chain.back());
        if (cache->last_result && cache->full_key == full_key &&
            cache->chain == chain) {
            CompileResult out = *cache->last_result;
            for (FeatureStatus& fst : out.statuses) fst.reused = true;
            out.features_reused = static_cast<int>(out.statuses.size());
            out.features_executed = 0;
            g_cc_full_hits.fetch_add(1, std::memory_order_relaxed);
            g_cc_features_reused.fetch_add(out.statuses.size(), std::memory_order_relaxed);
            return out;
        }
        // (b) PREFIX match — longest i with chain[i] equal and a live checkpoint.
        std::size_t match = 0;
        const std::size_t lim = std::min(chain.size(), cache->chain.size());
        for (std::size_t i = 0; i < lim; ++i) {
            if (chain[i] != cache->chain[i]) break;
            match = i;  // chain[i] matched => the state after i features is reusable
        }
        while (match > 0 && (match >= cache->snaps.size() || !cache->snaps[match]))
            --match;
        if (match > 0) {
            const CompileCkpt& ck = *cache->snaps[match];
            results = ck.results;  // shared_ptr map — O(entries) pointer copies
            consumed = ck.consumed;
            superseded_by = ck.superseded_by;
            solid_order = ck.solid_order;
            body_kinds = ck.body_kinds;
            source_of = ck.source_of;
            tool_body_of = ck.tool_body_of;
            feature_op_of = ck.feature_op_of;
            seen_names = ck.seen_names;
            sheet_ctx.active = ck.sheet_active;
            sheet_ctx.thickness = ck.sheet_thickness;
            sheet_ctx.k_factor = ck.sheet_k_factor;
            sheet_ctx.default_bend_radius = ck.sheet_default_bend_radius;
            bend_records = ck.bend_records;
            import_cache = ck.import_cache;
            face_owners = ck.face_owners;
            edge_owners = ck.edge_owners;
            status_ptrs = ck.statuses;
            result.statuses.clear();
            result.statuses.reserve(status_ptrs.size());
            for (const auto& sp : status_ptrs) {
                FeatureStatus fst = *sp;
                fst.reused = true;
                result.statuses.push_back(std::move(fst));
            }
            result.tool_recs = ck.tool_recs;
            result.weld_members = ck.weld_members;
            result.sheet_metal_flat = ck.sheet_metal_flat;
            start_at = match;
        }
    }
    // Checkpoints for THIS compile: the reused prefix is carried forward by pointer;
    // each executed feature appends its own snapshot at the end of its iteration.
    std::vector<std::shared_ptr<const CompileCkpt>> new_snaps;
    if (use_cache) {
        new_snaps.resize(n_features + 1);
        for (std::size_t i = 1; i <= start_at && i < cache->snaps.size(); ++i)
            new_snaps[i] = cache->snaps[i];
    }
    // Feature order for "the earliest feature wins" when several source faces merge into one
    // (UnifySameDomain). A split piece "<fid>#k" ranks as its feature.
    std::unordered_map<std::string, std::size_t> feature_rank;
    for (std::size_t i = 0; i < n_features; ++i)
        if (features[i].is_object())
            feature_rank.emplace(features[i].value("id", std::string()), i);
    auto owner_before = [&](const std::string& a, const std::string& b) {
        auto rank = [&](const std::string& o) {
            auto it = feature_rank.find(o.substr(0, o.find('#')));
            return it == feature_rank.end() ? n_features : it->second;
        };
        const std::size_t ra = rank(a), rb = rank(b);
        return ra != rb ? ra < rb : a < b;
    };
    // Attribute one topology kind of solid `sid` against its source solid. A source face/edge
    // passes its owner to every image it has in the new solid: itself when untouched, or what
    // the feature's recorded op histories (booleans, UnifySameDomain, fillet/chamfer) say it
    // was Modified into. Anything not reached is GENERATED by this feature (blend faces, boss
    // walls, cut walls) and owned by `sid`. When no recorded history reaches the new solid at
    // all (an op without a history hook, or a post-pass that rebuilt every TShape) the fallback
    // is underlying-geometry identity: same Geom_Surface/Geom_Curve handle at the same location.
    auto attribute = [&](const TopTools_IndexedMapOfShape& src_map,
                         const TopTools_IndexedMapOfShape& new_map,
                         const std::map<int, std::string>* src_owners, const std::string& src,
                         const std::string& sid, TopAbs_ShapeEnum kind,
                         const std::vector<Handle(BRepTools_History)>& histories) {
        std::vector<std::string> best(static_cast<std::size_t>(new_map.Extent()) + 1);
        auto offer = [&](int j, const std::string& owner) {
            std::string& cur = best[static_cast<std::size_t>(j)];
            if (cur.empty() || owner_before(owner, cur)) cur = owner;
        };
        auto owner_of = [&](int pi) -> const std::string& {
            if (src_owners) {
                auto it = src_owners->find(pi);
                if (it != src_owners->end()) return it->second;
            }
            return src;
        };
        bool traced = false;
        for (int pi = 1; pi <= src_map.Extent(); ++pi) {
            TopTools_ListOfShape images;
            ::geom::op_history_images(src_map.FindKey(pi), histories, images);
            for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next())
                if (const int j = new_map.FindIndex(it.Value()); j > 0) {
                    offer(j, owner_of(pi));
                    traced = true;
                }
        }
        if (histories.empty() || !traced) {
            // Geometry-identity fallback keyed by (Geom handle, location).
            auto geom_key = [kind](const TopoDS_Shape& s, TopLoc_Location& loc) -> const void* {
                if (kind == TopAbs_FACE) {
                    Handle(Geom_Surface) g = BRep_Tool::Surface(TopoDS::Face(s), loc);
                    return g.get();
                }
                const TopoDS_Edge& e = TopoDS::Edge(s);
                if (BRep_Tool::Degenerated(e)) return nullptr;
                double f = 0.0, l = 0.0;
                Handle(Geom_Curve) g = BRep_Tool::Curve(e, loc, f, l);
                return g.get();
            };
            std::multimap<const void*, std::pair<TopLoc_Location, int>> by_geom;
            for (int pi = 1; pi <= src_map.Extent(); ++pi) {
                TopLoc_Location loc;
                if (const void* g = geom_key(src_map.FindKey(pi), loc))
                    by_geom.emplace(g, std::make_pair(loc, pi));
            }
            for (int j = 1; j <= new_map.Extent(); ++j) {
                if (!best[static_cast<std::size_t>(j)].empty()) continue;
                TopLoc_Location loc;
                const void* g = geom_key(new_map.FindKey(j), loc);
                if (!g) continue;
                auto [lo, hi] = by_geom.equal_range(g);
                for (auto it = lo; it != hi; ++it)
                    if (it->second.first.IsEqual(loc)) offer(j, owner_of(it->second.second));
            }
        }
        std::map<int, std::string> out;
        for (int j = 1; j <= new_map.Extent(); ++j) {
            const std::string& o = best[static_cast<std::size_t>(j)];
            out[j] = o.empty() ? sid : o;
        }
        return out;
    };
    // Owner attribution for one solid id against source_of[sid]. Idempotent —
    // re-running for a re-executed solid overwrites face_owners[sid]/edge_owners[sid]. The
    // checkpoint carries only the resulting maps; histories never outlive their feature.
    auto update_owners_for = [&](const std::string& sid,
                                 const std::vector<Handle(BRepTools_History)>& histories) {
        auto rit = results.find(sid);
        if (rit == results.end() || !std::holds_alternative<SolidResult>(*rit->second))
            return;
        const TopoDS_Shape& shape = std::get<SolidResult>(*rit->second).shape;
        if (shape.IsNull()) return;
        TopTools_IndexedMapOfShape fm, em;
        TopExp::MapShapes(shape, TopAbs_FACE, fm);
        TopExp::MapShapes(shape, TopAbs_EDGE, em);
        const std::string src = source_of.count(sid) ? source_of[sid] : std::string();
        const bool has_src = !src.empty() && results.count(src) &&
                             std::holds_alternative<SolidResult>(*results.at(src)) &&
                             !std::get<SolidResult>(*results.at(src)).shape.IsNull();
        TopTools_IndexedMapOfShape sfm, sem;
        if (has_src) {
            const TopoDS_Shape& ss = std::get<SolidResult>(*results.at(src)).shape;
            TopExp::MapShapes(ss, TopAbs_FACE, sfm);
            TopExp::MapShapes(ss, TopAbs_EDGE, sem);
        }
        auto owners_of = [&](const OwnerTopoMap& m) -> const std::map<int, std::string>* {
            auto it = m.find(src);
            return it == m.end() ? nullptr : &it->second;
        };
        std::map<int, std::string> fo =
            attribute(sfm, fm, owners_of(face_owners), src, sid, TopAbs_FACE, histories);
        std::map<int, std::string> eo =
            attribute(sem, em, owners_of(edge_owners), src, sid, TopAbs_EDGE, histories);
        face_owners[sid] = std::move(fo);
        edge_owners[sid] = std::move(eo);
    };

    // Capture is O(map size) of SHARED pointers for results/statuses —
    // unchanged FeatureResult entries are not deep-copied into each checkpoint.
    auto capture_ckpt = [&](std::size_t after_idx) {
        if (!use_cache) return;
        auto ck = std::make_shared<CompileCkpt>();
        ck->results = results;
        ck->consumed = consumed;
        ck->superseded_by = superseded_by;
        ck->solid_order = solid_order;
        ck->body_kinds = body_kinds;
        ck->source_of = source_of;
        ck->tool_body_of = tool_body_of;
        ck->feature_op_of = feature_op_of;
        ck->seen_names = seen_names;
        ck->sheet_active = sheet_ctx.active;
        ck->sheet_thickness = sheet_ctx.thickness;
        ck->sheet_k_factor = sheet_ctx.k_factor;
        ck->sheet_default_bend_radius = sheet_ctx.default_bend_radius;
        ck->bend_records = bend_records;
        ck->import_cache = import_cache;
        ck->statuses = status_ptrs;
        ck->tool_recs = result.tool_recs;
        ck->weld_members = result.weld_members;
        ck->sheet_metal_flat = result.sheet_metal_flat;
        ck->face_owners = face_owners;
        ck->edge_owners = edge_owners;
        new_snaps[after_idx] = std::move(ck);
    };
    result.features_reused = static_cast<int>(start_at);
    result.features_executed = static_cast<int>(n_features - start_at);
    g_cc_features_reused.fetch_add(start_at, std::memory_order_relaxed);
    g_cc_features_executed.fetch_add(n_features - start_at, std::memory_order_relaxed);

    const auto _loop_t0 = std::chrono::steady_clock::now();
    for (std::size_t _fi = start_at; _fi < n_features; ++_fi) {
        const json& raw = features[_fi];
        // Accept the host's canonical {params,refs} shape OR the simplified flat
        // shape, normalizing to flat so the compilers below read either (see
        // normalize_feature above / contracts/doc_schema.md schema convergence).
        const json feat = normalize_feature(raw);
        const std::string id = feat.value("id", std::string("?"));
        const std::string type = feat.value("type", std::string(""));

        FeatureStatus st;
        st.id = id;
        st.type = type;
        // Per-feature rebuild timing (surfaced in ops_response.feature_status[].ms). Cheap:
        // one steady_clock read per feature — feature compiles are milliseconds-scale.
        const auto _feat_t0 = std::chrono::steady_clock::now();

        // A SUPPRESSED feature contributes no geometry but is OK (mirrors the Python
        // compiler: status ok, no body, topology unchanged). Skip it cleanly.
        if (raw.value("suppressed", false) || feat.value("suppressed", false)) {
            st.ok = true;
            status_ptrs.push_back(std::make_shared<const FeatureStatus>(st));
            result.statuses.push_back(std::move(st));
            capture_ckpt(_fi + 1);
            continue;
        }

        // Solid ids already committed before this feature ran — anything pushed past
        // this mark is a body THIS feature produced, validated post-compile (Bug N2).
        const std::size_t solids_before = solid_order.size();
        // ALIVE (surviving, not-yet-consumed) solids before this feature ran. Used by the
        // systemic "no silent vanish / null body / emptied document" backstop below (Bug
        // B6 class): a modifying feature (fillet/chamfer/sweep/cut/...) that CONSUMES the
        // only body but produces nothing must never silently leave the document empty.
        std::size_t alive_before = 0;
        for (const std::string& s : solid_order)
            if (!consumed.count(s)) ++alive_before;
        // Snapshot the CONSUMED set so the systemic backstop can ROLL IT BACK before it
        // throws. The per-branch guards all throw BEFORE they mutate `consumed`/
        // `solid_order`, so an erroring feature leaves the prior body intact. The backstop
        // runs AFTER the branch already committed (it is a last-resort net for a degenerate
        // result a branch's own guard missed), so a bare throw there would have ORPHANED
        // the body this feature consumed — restoring `consumed` keeps the prior body alive
        // in the surviving-bodies tessellation (the feature simply fails, geometry intact).
        const std::set<std::string> consumed_snapshot = consumed;
        // Record the op histories of THIS feature's geom ops (owner attribution below).
        const ::geom::OpHistoryScope op_histories(attribute_owners);

        // Per-feature compile wrapped so an OCCT/eval throw becomes an error status,
        // never a crash (compile_document never-raises contract).
        try {
            if (type == "sketch") {
                // Solve the 2D sketch, then build planar faces on the named plane.
                json sk;
                sk["entities"] = feat.value("entities", json::array());
                sk["constraints"] = feat.value("constraints", json::array());
                chudware::sketch::SketchSolution sol = chudware::sketch::solve_sketch(sk, env_json);
                if (!sol.converged)
                    throw std::runtime_error(chudware::sketch::nonconvergence_reason(sk, sol));

                // The sketch plane: a reserved datum NAME ("plane.top"/...), a prior
                // datum-plane feature id, OR a geometry reference to an existing planar
                // FACE ("sketch on the top face"). All three resolve through plane_of —
                // the same helper datum planes use — so a face query/selector lifts the
                // sketch onto that face's OWN frame (2D origin at the face centroid). A
                // spatially-limited model can then draw in 2D on a face without tracking
                // world Z. (plane_of's face path needs a body; base datums need none.)
                const json pref = feat.contains("plane") && !feat.at("plane").is_null()
                                      ? feat.at("plane") : json("plane.top");
                chudware::geom::SketchPlane plane = plane_of(pref, "sketch plane");
                // Optional `offset`: shift the resolved plane along its normal (a sketch N
                // mm above a datum/face), matching the loft-on-offset cheatsheet usage.
                if (feat.contains("offset") && !feat.at("offset").is_null())
                    plane.origin += plane.normal * num_or_expr(feat.at("offset"), env);
                std::vector<TopoDS_Face> faces =
                    chudware::geom::faces_from_solved_sketch(sol.entities, plane);
                // NB: an open sketch (no closed loop) is NOT an error here — it is a
                // valid path/trace for a sweep/rib. A consuming feature that needs a
                // face (extrude/revolve) raises if faces is empty.
                results[id] = SketchResult{std::move(faces), plane, sol.entities};
                st.ok = true;
                // Associative Convert Entities: a stale external ref (source
                // selector no longer resolves; last-good projected geometry kept) is a
                // WARNING, never a failure — the sketch still compiles and downstream
                // features build on the last-good converted curves.
                if (feat.contains("external_refs") && feat["external_refs"].is_array()) {
                    int n_stale = 0;
                    std::string first_stale;
                    for (const json& xr : feat["external_refs"])
                        if (xr.is_object() && xr.value("stale", false)) {
                            if (first_stale.empty())
                                first_stale = xr.value("id", std::string());
                            ++n_stale;
                        }
                    if (n_stale > 0)
                        st.warning = std::to_string(n_stale) +
                                     " external reference(s) stale (e.g. '" + first_stale +
                                     "'): source geometry no longer resolves; converted "
                                     "entities kept at their last-good positions";
                }

            } else if (type == "sketch3d") {
                // 3D free-space sketch (doc_schema §3B): solve the constraint
                // system in world coordinates, then chain the solved entities
                // into 3D wires a downstream sweep consumes as spine or guide.
                json sk;
                sk["entities"] = feat.value("entities", json::array());
                sk["constraints"] = feat.value("constraints", json::array());
                // External model references (doc_schema §3B): each
                // resolved external_ref (refresh_sketch3d_external_refs already
                // wrote last_point/stale onto the DOC before this compile ran —
                // see session.cpp) becomes a fixed construction point entity with
                // the ref's own id, plus an implicit fix constraint pinning it, so
                // ordinary constraints (coincident/distance/point_on) can target
                // "x1" exactly like any other fixed handle. A stale ref with no
                // last_point yet is skipped (not injected) rather than failing the
                // whole sketch3d — any constraint that references it will simply
                // fail to resolve, forward-tolerantly, like an unknown handle.
                int n_stale_refs = 0;
                std::string first_stale_ref;
                if (feat.contains("external_refs") && feat["external_refs"].is_array()) {
                    for (const json& xr : feat["external_refs"]) {
                        if (!xr.is_object()) continue;
                        const std::string rid = xr.value("id", std::string());
                        if (xr.value("stale", false)) {
                            ++n_stale_refs;
                            if (first_stale_ref.empty()) first_stale_ref = rid;
                        }
                        if (rid.empty() || !xr.contains("last_point") ||
                            !xr["last_point"].is_array() || xr["last_point"].size() < 3)
                            continue;  // never resolved even once — nothing to ground
                        sk["entities"].push_back(json{{"id", rid}, {"type", "point"},
                                                      {"p", xr["last_point"]},
                                                      {"construction", true}});
                        sk["constraints"].push_back(
                            json{{"id", "__extref_fix_" + rid},
                                {"type", "fix"},
                                {"refs", json::array({rid})}});
                    }
                }
                chudware::sketch3d::Sketch3DSolution sol =
                    chudware::sketch3d::solve_sketch3d(sk, env_json);
                if (!sol.converged)
                    throw std::runtime_error("sketch3d did not converge");
                std::vector<TopoDS_Wire> wires =
                    chudware::geom::wires_from_sketch3d(sol.entities);
                // NB: zero wires is NOT an error here — a points-only skeleton is
                // valid; a consuming sweep raises if it needs a wire.
                results[id] = Sketch3DResult{std::move(wires), sol.entities};
                st.ok = true;
                if (n_stale_refs > 0)
                    st.warning = std::to_string(n_stale_refs) +
                                 " external reference(s) stale (e.g. '" + first_stale_ref +
                                 "'): source vertex no longer resolves; ground point(s) "
                                 "kept at their last-good position";

            } else if (type == "plane") {
                // Datum plane (engine/doc/features/datum.py compile_plane). Ported
                // methods: offset (datum-only, from a base plane normal), midplane
                // (between two planar datums OR picked planar faces), angle (rotate a
                // base plane — datum or face — about a datum-axis OR picked-edge hinge),
                // three_point (through three point datums OR picked vertices),
                // parallel_point (base datum/face orientation through a point datum or
                // picked vertex), normal_to_curve (z_dir = a curve tangent at a landing
                // point, via D1 / curve-projection), on_surface (z_dir = a surface normal
                // at a projected point, via GeomAPI_ProjectPointOnSurf + GeomLProp_SLProps).
                // Stores a SketchPlane keyed by id; a downstream sketch+extrude gates it.
                const std::string method = feat.value("method", std::string("offset"));

                if (method == "offset") {
                    // base is a planar datum (origin plane / upstream datum) OR a picked
                    // planar model face — SolidWorks offsets a new plane from either. The
                    // host's ref-plane "Reference" well (K_PLANE) routes an origin-plane
                    // click as a datum string and a planar-face click as a face selector;
                    // plane_of resolves both. (The Python oracle keeps offset datum-only;
                    // accepting a face here is a deliberate SW-parity superset that does not
                    // affect datum-offset parity.)
                    const json bref = feat.contains("base") ? feat.at("base") : json("plane.top");
                    chudware::geom::SketchPlane bp = plane_of(bref, "base");
                    const double off =
                        feat.contains("offset") ? num_or_expr(feat.at("offset"), env) : 0.0;
                    chudware::geom::SketchPlane p = bp;
                    p.origin = bp.origin + bp.normal * off;
                    results[id] = PlaneResult{p};

                } else if (method == "midplane") {
                    // base/second are planar datums OR picked planar faces (datum.py
                    // _plane_of). mid origin = (a.origin + b.origin)/2 ; orientation = a's frame.
                    const json aref = feat.contains("base") ? feat.at("base") : json("plane.top");
                    const json bref = feat.contains("second") ? feat.at("second") : json("plane.top");
                    chudware::geom::SketchPlane a = plane_of(aref, "base");
                    chudware::geom::SketchPlane b = plane_of(bref, "second");
                    chudware::geom::SketchPlane p = a;
                    p.origin = (a.origin + b.origin) * 0.5;
                    results[id] = PlaneResult{p};

                } else if (method == "angle") {
                    const json bref = feat.contains("base") ? feat.at("base") : json("plane.top");
                    chudware::geom::SketchPlane base = plane_of(bref, "base");
                    if (!feat.contains("second"))
                        throw std::runtime_error("plane angle: refs.second (a hinge edge) is required");
                    // The hinge is a datum axis OR a picked model edge (datum.py: edge
                    // endpoints give the hinge point + direction).
                    std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> hinge;
                    const json& href = feat.at("second");
                    if (is_datum_ref(href)) {
                        hinge = resolve_axis_ref(href);
                        if (!hinge)
                            throw std::runtime_error("plane angle: datum hinge must be an axis");
                    } else {
                        hinge = edge_line_dir(resolve_edge_sel(href, "second"), "second");
                    }
                    double ang = feat.contains("angle") ? num_or_expr(feat.at("angle"), env) : 45.0;
                    if (feat.value("flip", false)) ang = -ang;
                    // Rotate the base plane's origin + axes about the hinge (point, dir).
                    gp_Trsf trsf;
                    trsf.SetRotation(
                        gp_Ax1(gp_Pnt(hinge->first.x(), hinge->first.y(), hinge->first.z()),
                               gp_Dir(hinge->second.x(), hinge->second.y(), hinge->second.z())),
                        radians(ang));
                    auto xf_pt = [&](const Eigen::Vector3d& v) {
                        gp_Pnt q(v.x(), v.y(), v.z()); q.Transform(trsf);
                        return Eigen::Vector3d(q.X(), q.Y(), q.Z());
                    };
                    auto xf_dir = [&](const Eigen::Vector3d& v) {
                        gp_Vec q(v.x(), v.y(), v.z()); q.Transform(trsf);
                        return Eigen::Vector3d(q.X(), q.Y(), q.Z());
                    };
                    Eigen::Vector3d o = xf_pt(base.origin);
                    Eigen::Vector3d nrm = xf_dir(base.normal);
                    Eigen::Vector3d uref = xf_dir(base.uAxis);
                    results[id] = PlaneResult{chudware::geom::SketchPlane::from(o, nrm, uref)};

                } else if (method == "three_point") {
                    auto pit = feat.find("points");
                    if (pit == feat.end() || !pit->is_array() || pit->size() < 3)
                        throw std::runtime_error(
                            "plane three_point: refs.points must be three point datums");
                    // Each point is a point datum OR a picked vertex (datum.py _vertex_point).
                    Eigen::Vector3d p0 = vertex_point((*pit)[0], "points[0]");
                    Eigen::Vector3d p1 = vertex_point((*pit)[1], "points[1]");
                    Eigen::Vector3d p2 = vertex_point((*pit)[2], "points[2]");
                    Eigen::Vector3d v1 = p1 - p0, v2 = p2 - p0;
                    Eigen::Vector3d n = v1.cross(v2);
                    if (n.norm() < 1e-9 || v1.norm() < 1e-9 || v2.norm() < 1e-9)
                        throw std::runtime_error(
                            "plane three_point: the three points are colinear (no unique plane)");
                    results[id] = PlaneResult{chudware::geom::SketchPlane::from(p0, n, v1)};

                } else if (method == "parallel_point") {
                    // base is a planar datum OR a picked planar face; the through-point is
                    // a point datum OR a picked vertex (datum.py parallel_point).
                    const json bref = feat.contains("base") ? feat.at("base") : json("plane.top");
                    chudware::geom::SketchPlane base = plane_of(bref, "base");
                    auto pit = feat.find("points");
                    if (pit == feat.end() || !pit->is_array() || pit->empty())
                        throw std::runtime_error(
                            "plane parallel_point: refs.points[0] (a through-point) is required");
                    Eigen::Vector3d thru = vertex_point((*pit)[0], "points[0]");
                    chudware::geom::SketchPlane p = base;
                    p.origin = thru;
                    results[id] = PlaneResult{p};

                } else if (method == "point_normal") {
                    // Arbitrary origin + normal: the direct SolidWorks-
                    // style "plane through a point, normal to a vector" constructor. No
                    // existing method covered this exactly (three_point needs 3 points;
                    // parallel_point inherits its orientation from a base plane/face). This
                    // is what closes the sketch3d_pipe auto-profile gap (MCP
                    // sketch3d_tools.py used to guess an axis-dominant tilt when no plane
                    // existed at the spine start — now it can request an exact plane here).
                    // `origin`: a point/vertex datum ref, a picked-vertex selector, or a
                    // literal [x,y,z]. `normal`: a literal [x,y,z] (need not be unit length).
                    if (!feat.contains("origin"))
                        throw std::runtime_error("plane point_normal: refs.origin is required");
                    Eigen::Vector3d origin;
                    const json& oref = feat.at("origin");
                    if (oref.is_array() && oref.size() >= 3)
                        origin = Eigen::Vector3d(oref[0].get<double>(), oref[1].get<double>(),
                                                 oref[2].get<double>());
                    else
                        origin = vertex_point(oref, "origin");
                    if (!feat.contains("normal"))
                        throw std::runtime_error("plane point_normal: refs.normal is required");
                    Eigen::Vector3d normal = req_vec3(feat, "normal");
                    if (normal.norm() < 1e-12)
                        throw std::runtime_error("plane point_normal: normal is zero-length");
                    results[id] = PlaneResult{chudware::geom::SketchPlane::from(origin, normal)};

                } else if (method == "normal_to_curve") {
                    // A plane whose NORMAL is the tangent of a curve at a landing point
                    // (datum.py normal_to_curve). The curve is refs.second: a datum axis
                    // (origin/dir straight from the axis, optionally re-origined to
                    // refs.points[0]) OR a picked model edge (point+tangent via D1, landed
                    // by points[0] projected onto the curve / params.fraction / params.t).
                    if (!feat.contains("second"))
                        throw std::runtime_error(
                            "plane normal_to_curve: refs.second (the curve edge) is required");
                    const json& cref = feat.at("second");
                    Eigen::Vector3d origin, normal;
                    auto pit = feat.find("points");
                    const bool have_pt =
                        pit != feat.end() && pit->is_array() && !pit->empty();
                    if (is_datum_ref(cref)) {
                        auto ax = resolve_axis_ref(cref);
                        if (!ax)
                            throw std::runtime_error(
                                "plane normal_to_curve: datum curve must be an axis");
                        origin = ax->first;
                        normal = ax->second;
                        if (have_pt) origin = vertex_point((*pit)[0], "points[0]");
                    } else {
                        TopoDS_Edge edge = resolve_edge_sel(cref, "second");
                        std::optional<Eigen::Vector3d> near_pt;
                        if (have_pt) near_pt = vertex_point((*pit)[0], "points[0]");
                        std::optional<double> fraction;
                        if (feat.contains("fraction"))
                            fraction = num_or_expr(feat.at("fraction"), env);
                        std::optional<double> param;
                        if (feat.contains("t")) param = num_or_expr(feat.at("t"), env);
                        std::tie(origin, normal) =
                            curve_point_tangent(edge, fraction, param, near_pt, "second");
                    }
                    results[id] = PlaneResult{chudware::geom::SketchPlane::from(origin, normal)};

                } else if (method == "on_surface") {
                    // A plane TANGENT to a surface at a point: z_dir = the surface normal at
                    // the projected point, origin = that surface point (datum.py on_surface).
                    // refs.base is the face; refs.points[0] is the point to project (defaults
                    // to the face area centroid).
                    if (!feat.contains("base") || is_datum_ref(feat.at("base")))
                        throw std::runtime_error(
                            "plane on_surface: refs.base (a surface face) is required");
                    TopoDS_Face face = resolve_face_sel(feat.at("base"), "base");
                    Eigen::Vector3d near_pt;
                    auto pit = feat.find("points");
                    if (pit != feat.end() && pit->is_array() && !pit->empty()) {
                        near_pt = vertex_point((*pit)[0], "points[0]");
                    } else {
                        GProp_GProps props;
                        BRepGProp::SurfaceProperties(face, props);
                        gp_Pnt cm = props.CentreOfMass();
                        near_pt = Eigen::Vector3d(cm.X(), cm.Y(), cm.Z());
                    }
                    auto [origin, normal] = surface_point_normal(face, near_pt, "base");
                    results[id] = PlaneResult{chudware::geom::SketchPlane::from(origin, normal)};

                } else {
                    // Class-5 fix: name the accepted vocabulary — the if/else chain above
                    // IS the plane method list, so a typo ("midpoint" for "midplane")
                    // used to get only "not supported" with nothing to compare it against.
                    throw std::runtime_error(
                        "plane: method '" + method + "' not supported. Plane methods: "
                        "offset, midplane, angle, three_point, parallel_point, "
                        "point_normal, normal_to_curve, on_surface");
                }
                st.ok = true;

            } else if (type == "axis") {
                // Datum axis (engine/doc/features/datum.py compile_axis). Ported:
                // two_points (line through two point datums OR picked vertices), cylinder
                // (a picked cylindrical/conical face's revolve axis OR a datum-axis pass-
                // through), edge (a picked straight model edge OR a datum-axis pass-
                // through), plane_intersect (the line shared by two planar datums/faces),
                // on_surface (a face's revolve axis / planar normal, OR the line through
                // two on-surface points). Stores an AxisResult; a downstream revolve about
                // this axis gates it.
                const std::string method = feat.value("method", std::string("two_points"));
                if (method == "two_points") {
                    Eigen::Vector3d a = vertex_point(feat.at("a"), "a");
                    Eigen::Vector3d b = vertex_point(feat.at("b"), "b");
                    Eigen::Vector3d d = b - a;
                    if (d.norm() < 1e-9)
                        throw std::runtime_error("axis two_points: coincident points");
                    results[id] = AxisResult{a, d.normalized()};
                } else if (method == "cylinder") {
                    const json& aref = feat.at("a");
                    if (is_datum_ref(aref)) {
                        auto ax = resolve_axis_ref(aref);
                        if (!ax)
                            throw std::runtime_error("axis cylinder: datum ref.a must be an axis");
                        results[id] = AxisResult{ax->first, ax->second};
                    } else {
                        auto [loc, dir] = face_axis(resolve_face_sel(aref, "a"), "a");
                        if (dir.norm() < 1e-9)
                            throw std::runtime_error("axis cylinder: degenerate face axis");
                        results[id] = AxisResult{loc, dir.normalized()};
                    }
                } else if (method == "edge") {
                    const json& aref = feat.at("a");
                    if (is_datum_ref(aref)) {
                        auto ax = resolve_axis_ref(aref);
                        if (!ax)
                            throw std::runtime_error("axis edge: datum ref.a must be an axis");
                        results[id] = AxisResult{ax->first, ax->second};
                    } else {
                        auto [pt, dir] = edge_line_dir(resolve_edge_sel(aref, "a"), "a");
                        results[id] = AxisResult{pt, dir.normalized()};
                    }
                } else if (method == "plane_intersect") {
                    // Line shared by two planes (datum or picked planar face). direction =
                    // na × nb; a point on the line solves [na; nb; dir]·x = [na·oa; nb·ob; 0].
                    chudware::geom::SketchPlane pa = plane_of(feat.at("a"), "a");
                    chudware::geom::SketchPlane pb = plane_of(feat.at("b"), "b");
                    Eigen::Vector3d na = pa.normal, nb = pb.normal;
                    Eigen::Vector3d dir = na.cross(nb);
                    if (dir.norm() < 1e-9)
                        throw std::runtime_error(
                            "axis plane_intersect: planes are parallel (no intersection line)");
                    Eigen::Matrix3d A;
                    A.row(0) = na; A.row(1) = nb; A.row(2) = dir;
                    Eigen::Vector3d rhs(na.dot(pa.origin), nb.dot(pb.origin), 0.0);
                    Eigen::Vector3d pt = A.fullPivLu().solve(rhs);
                    results[id] = AxisResult{pt, dir.normalized()};
                } else if (method == "on_surface") {
                    // An axis derived from a surface (datum.py axis on_surface). Two forms:
                    //   * two-point — refs.a is a point/vertex datum (or refs.b is present):
                    //     the axis is the line through the two on-surface points.
                    //   * face — refs.a is a face: a cylinder/cone gives its revolve axis
                    //     through the face axis location; a planar face gives its normal
                    //     through the face area centroid.
                    if (!feat.contains("a"))
                        throw std::runtime_error(
                            "axis on_surface: refs.a (a surface face, or a point with refs.b) "
                            "is required");
                    const json& aref = feat.at("a");
                    const bool has_b = feat.contains("b");
                    bool a_is_point;
                    if (is_datum_ref(aref)) {
                        // a point datum resolves; an axis/plane datum does not.
                        std::string fid;
                        if (aref.is_string()) fid = aref.get<std::string>();
                        else if (aref.is_object() && aref.contains("feature") &&
                                 aref["feature"].is_string())
                            fid = aref["feature"].get<std::string>();
                        a_is_point = (fid == "origin");
                        if (!a_is_point && !fid.empty()) {
                            auto it = results.find(fid);
                            a_is_point = it != results.end() &&
                                std::holds_alternative<PointResult>(*it->second);
                        }
                    } else {
                        a_is_point = aref.is_object() &&
                            aref.value("kind", std::string()) == "vertex";
                    }
                    if (a_is_point || has_b) {
                        Eigen::Vector3d pa = vertex_point(aref, "a");
                        if (!has_b)
                            throw std::runtime_error(
                                "axis on_surface: a point ref.a needs refs.b (a second point)");
                        Eigen::Vector3d pb = vertex_point(feat.at("b"), "b");
                        Eigen::Vector3d d = pb - pa;
                        if (d.norm() < 1e-9)
                            throw std::runtime_error("axis on_surface: coincident points");
                        results[id] = AxisResult{pa, d.normalized()};
                    } else {
                        if (is_datum_ref(aref))
                            throw std::runtime_error(
                                "axis on_surface: datum ref.a must be a point (use refs.b "
                                "for a 2nd)");
                        TopoDS_Face face = resolve_face_sel(aref, "a");
                        BRepAdaptor_Surface ad(face);
                        GeomAbs_SurfaceType st = ad.GetType();
                        Eigen::Vector3d loc, dir;
                        if (st == GeomAbs_Cylinder || st == GeomAbs_Cone) {
                            std::tie(loc, dir) = face_axis(face, "a");
                        } else if (st == GeomAbs_Plane) {
                            GProp_GProps props;
                            BRepGProp::SurfaceProperties(face, props);
                            gp_Pnt cm = props.CentreOfMass();
                            Eigen::Vector3d center(cm.X(), cm.Y(), cm.Z());
                            auto pn = surface_point_normal(face, center, "a");
                            loc = center;
                            dir = pn.second;
                        } else {
                            throw std::runtime_error(
                                "axis on_surface: face must be planar (normal) or "
                                "cylindrical/conical (revolve axis)");
                        }
                        if (dir.norm() < 1e-9)
                            throw std::runtime_error("axis on_surface: degenerate axis");
                        results[id] = AxisResult{loc, dir.normalized()};
                    }
                } else {
                    // accept a datum-axis pass-through for any other method, else flag a
                    // clear unsupported-method error.
                    auto ax = feat.contains("a") ? resolve_axis_ref(feat.at("a"))
                                                 : std::nullopt;
                    if (ax) {
                        results[id] = AxisResult{ax->first, ax->second};
                    } else {
                        throw std::runtime_error(
                            "axis: method '" + method + "' needs a geometry selector (not yet ported)");
                    }
                }
                st.ok = true;

            } else if (type == "point") {
                // Datum point (engine/doc/features/datum.py compile_point). Ported:
                // coordinate (explicit XYZ), a point/vertex-datum target pass-through,
                // and the selector-driven methods vertex (a picked vertex), center (a
                // picked circular edge's arc centre or a picked face's centre), centroid
                // (the whole-body volume centroid, or a picked face's area centroid),
                // on_face (a face's Plane(face) uv lift, or its centre), on_curve (a point
                // at params.t / params.fraction along a curve), and intersection (the
                // nearest-approach point of a curve with a second curve or a surface).
                // Stores a PointResult. The datum-point feature is now fully ported.
                const std::string method = feat.value("method", std::string("vertex"));
                auto tit = feat.find("target");
                const bool target_is_datum =
                    tit != feat.end() && is_datum_ref(*tit);
                if (method == "coordinate") {
                    Eigen::Vector3d p(0, 0, 0);
                    auto cit = feat.find("coordinate");
                    if (cit != feat.end() && cit->is_array() && cit->size() >= 3) {
                        p = Eigen::Vector3d((*cit)[0].get<double>(), (*cit)[1].get<double>(),
                                            (*cit)[2].get<double>());
                    } else {
                        p = Eigen::Vector3d(
                            feat.contains("x") ? num_or_expr(feat.at("x"), env) : 0.0,
                            feat.contains("y") ? num_or_expr(feat.at("y"), env) : 0.0,
                            feat.contains("z") ? num_or_expr(feat.at("z"), env) : 0.0);
                    }
                    results[id] = PointResult{p};
                } else if (method == "centroid" &&
                           (feat.value("body", false) ||
                            (tit != feat.end() && tit->is_object() &&
                             tit->value("kind", std::string()) == "body"))) {
                    // Whole-body volume centre of mass (datum.py _body_centroid). Needs no
                    // target — the running solid.
                    const TopoDS_Shape& body = last_body_shape("point centroid");
                    GProp_GProps props;
                    BRepGProp::VolumeProperties(body, props);
                    gp_Pnt cm = props.CentreOfMass();
                    results[id] = PointResult{Eigen::Vector3d(cm.X(), cm.Y(), cm.Z())};
                } else if (target_is_datum) {
                    // A point/vertex datum target resolves directly regardless of method.
                    results[id] = PointResult{resolve_point(*tit, "target")};
                } else if (tit == feat.end()) {
                    throw std::runtime_error("point: refs.target is required");
                } else if (method == "vertex") {
                    results[id] = PointResult{vertex_point(*tit, "target")};
                } else if (method == "center") {
                    const std::string kind = tit->is_object()
                        ? tit->value("kind", std::string()) : std::string();
                    if (kind == "edge") {
                        // Centre of a circular edge (datum.py: edge.arc_center).
                        TopoDS_Edge e = resolve_edge_sel(*tit, "target");
                        BRepAdaptor_Curve ad(e);
                        if (ad.GetType() != GeomAbs_Circle)
                            throw std::runtime_error("point center: edge is not circular");
                        gp_Pnt c = ad.Circle().Location();
                        results[id] = PointResult{Eigen::Vector3d(c.X(), c.Y(), c.Z())};
                    } else if (kind == "face") {
                        // Centre of a face (datum.py: face.center() — area centroid).
                        TopoDS_Face f = resolve_face_sel(*tit, "target");
                        GProp_GProps props;
                        BRepGProp::SurfaceProperties(f, props);
                        gp_Pnt cm = props.CentreOfMass();
                        results[id] = PointResult{Eigen::Vector3d(cm.X(), cm.Y(), cm.Z())};
                    } else {
                        throw std::runtime_error(
                            "point center: target must be a circular edge or a face");
                    }
                } else if (method == "centroid") {
                    // Area centre of mass of a picked face (the body centroid is handled
                    // earlier). datum.py _face_centroid.
                    if (!tit->is_object() || tit->value("kind", std::string()) != "face")
                        throw std::runtime_error(
                            "point centroid: target must be a face, or pass params.body");
                    TopoDS_Face f = resolve_face_sel(*tit, "target");
                    GProp_GProps props;
                    BRepGProp::SurfaceProperties(f, props);
                    gp_Pnt cm = props.CentreOfMass();
                    results[id] = PointResult{Eigen::Vector3d(cm.X(), cm.Y(), cm.Z())};
                } else if (method == "on_face") {
                    // A point on a face: with params.uv -> the in-plane (u,v) lift of the
                    // face's Plane(face) frame (origin = area centroid); without uv -> the
                    // face centre. A non-planar face has no Plane(face) and falls back to
                    // the centre regardless of uv. Mirrors datum.py compile_point on_face.
                    TopoDS_Face f = resolve_face_sel(*tit, "target");
                    auto uvit = feat.find("uv");
                    const bool have_uv =
                        uvit != feat.end() && uvit->is_array() && uvit->size() >= 2;
                    Eigen::Vector3d p;
                    BRepAdaptor_Surface ad(f);
                    if (ad.GetType() == GeomAbs_Plane && have_uv) {
                        // Planar face + uv -> lift on the orientation-aware plane frame
                        // (face_plane builds origin=centroid, axes matching Plane(face)).
                        chudware::geom::SketchPlane pl = face_plane(f, "target");
                        const double u = (*uvit)[0].get<double>();
                        const double v = (*uvit)[1].get<double>();
                        p = pl.lift(Eigen::Vector2d(u, v));
                    } else {
                        // No uv (or non-planar) -> the face area centroid (face.center()).
                        GProp_GProps props;
                        BRepGProp::SurfaceProperties(f, props);
                        gp_Pnt cm = props.CentreOfMass();
                        p = Eigen::Vector3d(cm.X(), cm.Y(), cm.Z());
                    }
                    results[id] = PointResult{p};
                } else if (method == "on_curve") {
                    // A point AT a parameter / fraction along a model edge's curve. params.t
                    // (a raw curve parameter, clamped) wins; else params.fraction (0..1 along
                    // the edge range, default 0.5 = midpoint). datum.py _curve_point_tangent.
                    TopoDS_Edge e = resolve_edge_sel(*tit, "target");
                    std::optional<double> param;
                    if (feat.contains("t")) param = num_or_expr(feat.at("t"), env);
                    std::optional<double> fraction;
                    if (feat.contains("fraction"))
                        fraction = num_or_expr(feat.at("fraction"), env);
                    auto [pt, tan] =
                        curve_point_tangent(e, fraction, param, std::nullopt, "target");
                    (void)tan;
                    results[id] = PointResult{pt};
                } else if (method == "intersection") {
                    // Intersection of refs.target (a curve edge) with params.other (a second
                    // curve edge OR a surface face), via BRepExtrema_DistShapeShape nearest
                    // approach (trimming-respecting; errors if they do not meet within tol).
                    // datum.py _curve_curve_point / _curve_surface_point.
                    TopoDS_Edge e = resolve_edge_sel(*tit, "target");
                    auto oit = feat.find("other");
                    if (oit == feat.end() || !oit->is_object())
                        throw std::runtime_error(
                            "point intersection: params.other (a second edge or a face "
                            "selector) is required");
                    const std::string okind = oit->value("kind", std::string());
                    Eigen::Vector3d p;
                    if (okind == "edge") {
                        TopoDS_Edge e2 = resolve_edge_sel(*oit, "other");
                        p = shape_shape_point(e, e2, "other", "curves");
                    } else if (okind == "face") {
                        TopoDS_Face f = resolve_face_sel(*oit, "other");
                        p = shape_shape_point(e, f, "other", "curve and surface");
                    } else {
                        throw std::runtime_error(
                            "point intersection: params.other must be an edge or face selector");
                    }
                    results[id] = PointResult{p};
                } else {
                    throw std::runtime_error(
                        "point: method '" + method + "' needs a geometry selector (not yet ported)");
                }
                st.ok = true;

            } else if (type == "csys") {
                // Datum coordinate system (engine/doc/features/datum.py compile_csys).
                // Geometrically a Plane: origin from a point/vertex datum or a picked
                // vertex (default "origin"); optional x_dir/y_dir from datum axes OR picked
                // model edges (Gram-Schmidt orthonormal frame). Stored as a csys
                // PlaneResult so a sketch can be built on it (gates origin + frame).
                const json oref = feat.contains("origin") ? feat.at("origin") : json("origin");
                Eigen::Vector3d o = vertex_point(oref, "origin");
                // A direction from a datum axis OR a picked model edge (datum.py _dir_from).
                auto dir_from = [&](const char* slot) -> std::optional<Eigen::Vector3d> {
                    if (!feat.contains(slot)) return std::nullopt;
                    const json& ref = feat.at(slot);
                    if (is_datum_ref(ref)) {
                        auto ax = resolve_axis_ref(ref);
                        if (!ax) throw std::runtime_error(
                            std::string("csys ") + slot + ": datum must be an axis");
                        return ax->second;
                    }
                    auto [pt, d] = edge_line_dir(resolve_edge_sel(ref, slot), slot);
                    return d;
                };
                std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> xax;
                if (auto xd = dir_from("x_dir")) xax = std::make_pair(o, *xd);
                std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> yax;
                if (auto yd = dir_from("y_dir")) yax = std::make_pair(o, *yd);
                chudware::geom::SketchPlane p;
                if (!xax) {
                    p = chudware::geom::SketchPlane::top();
                    p.origin = o;
                } else {
                    // Gram-Schmidt: x = x_dir; y = y hint ⟂ x; z = x×y (matches datum.py
                    // _orthonormal_frame, then Plane(origin, x_dir=x, z_dir=z)).
                    Eigen::Vector3d x = xax->second.normalized();
                    Eigen::Vector3d y;
                    bool have_y = false;
                    if (yax) {
                        Eigen::Vector3d yh = yax->second;
                        Eigen::Vector3d proj = yh - x * x.dot(yh);
                        if (proj.norm() >= 1e-9) { y = proj.normalized(); have_y = true; }
                    }
                    if (!have_y) {
                        Eigen::Vector3d ref = (std::abs(x.z()) < 0.9) ? Eigen::Vector3d(0, 0, 1)
                                                                      : Eigen::Vector3d(1, 0, 0);
                        y = (ref - x * x.dot(ref)).normalized();
                    }
                    Eigen::Vector3d z = x.cross(y).normalized();
                    p = chudware::geom::SketchPlane::from(o, z, x);
                }
                results[id] = PlaneResult{p, true};
                st.ok = true;

            } else if (type == "helix") {
                // Curve feature (engine/doc/features/curves.py): build a helix / flat
                // spiral / variable-pitch wire in the base local frame. Ported:
                // constant-pitch helix (incl. taper + start_angle + handedness), the
                // flat Archimedean spiral, and variable-pitch (pitch_table).
                // refs.base = a datum plane / csys (origin + axis) OR a picked planar model
                // face (plane_of, same as the ref-plane offset base); standalone helices fall
                // back to the `radius` param. Stores a CurveResult a sweep consumes.
                const json bref = feat.contains("base") ? feat.at("base") : json("plane.top");
                chudware::geom::SketchPlane base = plane_of(bref, "base");

                const double start_angle =
                    feat.contains("start_angle") ? num_or_expr(feat.at("start_angle"), env) : 0.0;
                const bool ccw = feat.value("ccw", true);
                const bool is_spiral = feat.value("spiral", false) ||
                    (feat.contains("shape") && feat["shape"].is_string() &&
                     feat["shape"].get<std::string>() == "spiral");

                TopoDS_Wire local_wire;
                if (feat.contains("pitch_table")) {
                    // Variable pitch — mirror curves.py _parse_pitch_table /
                    // _interp_radius / _integrate_height + _build_variable_pitch.
                    // Rows are {turn,pitch,dia|diameter|radius} mappings OR bare
                    // [turn,pitch,dia?] lists; each scalar may be a {value,expr} dim
                    // (matching _scalar: read 'value' if object, else a bare number).
                    const double default_radius =
                        feat.contains("radius") ? num_or_expr(feat.at("radius"), env) : 10.0;
                    const json& table = feat.at("pitch_table");
                    if (!table.is_array() || table.empty())
                        throw std::runtime_error("helix: pitch_table must be a non-empty list of rows");

                    // _scalar: a dim {value,...} reads its numeric 'value'; otherwise a bare number.
                    auto cell_scalar = [](const json& v) -> double {
                        const json& src = v.is_object() ? v.value("value", json()) : v;
                        if (!src.is_number())
                            throw std::runtime_error("helix: expected a numeric value in pitch_table");
                        return src.get<double>();
                    };

                    // (turn, pitch, radius_or_nan) before fill.
                    std::vector<std::array<double, 3>> raw_rows;
                    for (size_t i = 0; i < table.size(); ++i) {
                        const json& row = table[i];
                        double turn, pitch, rad = std::numeric_limits<double>::quiet_NaN();
                        if (row.is_object()) {
                            if (!row.contains("turn") || !row.contains("pitch"))
                                throw std::runtime_error(
                                    "helix: pitch_table row needs 'turn' and 'pitch' keys");
                            turn = cell_scalar(row.at("turn"));
                            pitch = cell_scalar(row.at("pitch"));
                            if (row.contains("dia") && !row.at("dia").is_null())
                                rad = cell_scalar(row.at("dia")) / 2.0;
                            else if (row.contains("diameter") && !row.at("diameter").is_null())
                                rad = cell_scalar(row.at("diameter")) / 2.0;
                            else if (row.contains("radius") && !row.at("radius").is_null())
                                rad = cell_scalar(row.at("radius"));
                        } else if (row.is_array()) {
                            if (row.size() < 2)
                                throw std::runtime_error(
                                    "helix: pitch_table row needs at least [turn, pitch]");
                            turn = cell_scalar(row[0]);
                            pitch = cell_scalar(row[1]);
                            if (row.size() >= 3 && !row[2].is_null())
                                rad = cell_scalar(row[2]) / 2.0;
                        } else {
                            throw std::runtime_error(
                                "helix: pitch_table row must be a mapping or list");
                        }
                        if (!(pitch > 0.0))
                            throw std::runtime_error("helix: pitch_table row pitch must be > 0");
                        if (turn < 0.0)
                            throw std::runtime_error("helix: pitch_table row turn must be >= 0");
                        raw_rows.push_back({turn, pitch, rad});
                    }

                    // Stable sort by turn (matches Python list.sort stability).
                    std::stable_sort(raw_rows.begin(), raw_rows.end(),
                                     [](const std::array<double, 3>& a,
                                        const std::array<double, 3>& b) { return a[0] < b[0]; });
                    // Collapse exact-duplicate turn keys to the last row given for that turn.
                    std::vector<std::array<double, 3>> rows;
                    for (const auto& r : raw_rows) {
                        if (!rows.empty() && std::abs(rows.back()[0] - r[0]) < 1e-12)
                            rows.back() = r;
                        else
                            rows.push_back(r);
                    }
                    if (rows.size() < 2)
                        throw std::runtime_error(
                            "helix: pitch_table needs at least two distinct turn rows");
                    if (rows[0][0] > 1e-9)
                        rows.insert(rows.begin(), {0.0, rows[0][1], rows[0][2]});

                    // Forward-fill radii. Seed = first explicit radius (if any) else default.
                    double first_explicit = std::numeric_limits<double>::quiet_NaN();
                    for (const auto& r : rows)
                        if (!std::isnan(r[2])) { first_explicit = r[2]; break; }
                    double running = std::isnan(first_explicit) ? default_radius : first_explicit;
                    for (auto& r : rows) {
                        if (!std::isnan(r[2])) {
                            if (!(r[2] > 0.0))
                                throw std::runtime_error(
                                    "helix: pitch_table radius/diameter must be > 0");
                            running = r[2];
                        }
                        r[2] = running;
                    }

                    const double total_turns = rows.back()[0];
                    if (!(total_turns > 0.0))
                        throw std::runtime_error(
                            "helix: pitch_table must span a positive number of turns");

                    // Piecewise-linear radius at `turn` (clamped to table range).
                    auto interp_radius = [&rows](double turn) -> double {
                        if (turn <= rows.front()[0]) return rows.front()[2];
                        if (turn >= rows.back()[0]) return rows.back()[2];
                        for (size_t k = 0; k + 1 < rows.size(); ++k) {
                            double t0 = rows[k][0], t1 = rows[k + 1][0];
                            if (t0 <= turn && turn <= t1) {
                                if (t1 - t0 < 1e-12) return rows[k + 1][2];
                                double f = (turn - t0) / (t1 - t0);
                                return rows[k][2] + f * (rows[k + 1][2] - rows[k][2]);
                            }
                        }
                        return rows.back()[2];
                    };
                    // Cumulative axial height = trapezoidal integral of pitch over turn.
                    auto integrate_height = [&rows](double turn) -> double {
                        if (turn <= rows.front()[0]) return 0.0;
                        double z = 0.0;
                        for (size_t k = 0; k + 1 < rows.size(); ++k) {
                            double t0 = rows[k][0], t1 = rows[k + 1][0];
                            double p0 = rows[k][1], p1 = rows[k + 1][1];
                            if (turn <= t0) break;
                            double seg_end = std::min(turn, t1);
                            if (seg_end <= t0) continue;
                            double span = seg_end - t0;
                            double p_end = (t1 - t0 < 1e-12)
                                ? p1 : p0 + (seg_end - t0) / (t1 - t0) * (p1 - p0);
                            z += 0.5 * (p0 + p_end) * span;
                        }
                        if (turn > rows.back()[0])
                            z += rows.back()[1] * (turn - rows.back()[0]);
                        return z;
                    };

                    const double sign = ccw ? 1.0 : -1.0;
                    const double a0 = radians(start_angle);
                    int n = std::max(2, (int)std::ceil(total_turns * 48));
                    std::vector<std::array<double, 3>> pts;
                    pts.reserve(n + 1);
                    for (int i = 0; i <= n; ++i) {
                        double turn = total_turns * i / n;
                        double ang = a0 + sign * 2.0 * PI * turn;
                        double r = interp_radius(turn);
                        double z = integrate_height(turn);
                        pts.push_back({r * std::cos(ang), r * std::sin(ang), z});
                    }
                    local_wire = ::geom::make_spline_wire(pts);
                } else if (is_spiral) {
                    double pitch = feat.contains("pitch") ? num_or_expr(feat.at("pitch"), env) : 5.0;
                    if (!(pitch > 0.0)) throw std::runtime_error("helix: spiral pitch must be > 0");
                    double turns = feat.contains("turns") ? num_or_expr(feat.at("turns"), env) : 0.0;
                    if (!(turns > 0.0)) throw std::runtime_error("helix: spiral needs turns > 0");
                    double start_radius =
                        feat.contains("start_radius") ? num_or_expr(feat.at("start_radius"), env)
                        : (feat.contains("radius") ? num_or_expr(feat.at("radius"), env) : 10.0);
                    if (start_radius < 0.0)
                        throw std::runtime_error("helix: spiral start_radius must be >= 0");
                    const double sign = ccw ? 1.0 : -1.0;
                    const double a0 = radians(start_angle);
                    const double b = pitch / (2.0 * PI);
                    const double total_theta = 2.0 * PI * turns;
                    int n = std::max(2, (int)std::ceil(turns * 48));
                    std::vector<std::array<double, 3>> pts;
                    pts.reserve(n + 1);
                    for (int i = 0; i <= n; ++i) {
                        double theta = total_theta * i / n;
                        double r = start_radius + b * theta;
                        double ang = a0 + sign * theta;
                        pts.push_back({r * std::cos(ang), r * std::sin(ang), 0.0});
                    }
                    local_wire = ::geom::make_spline_wire(pts);
                } else {
                    double pitch = feat.contains("pitch") ? num_or_expr(feat.at("pitch"), env) : 5.0;
                    if (!(pitch > 0.0)) throw std::runtime_error("helix: pitch must be > 0");
                    double height = feat.contains("height") ? num_or_expr(feat.at("height"), env) : 20.0;
                    double taper = feat.contains("taper") ? num_or_expr(feat.at("taper"), env) : 0.0;
                    if (feat.contains("turns")) {
                        double turns = num_or_expr(feat.at("turns"), env);
                        if (!(turns > 0.0)) throw std::runtime_error("helix: turns must be > 0");
                        height = turns * pitch;
                    } else if (!(height > 0.0)) {
                        throw std::runtime_error("helix: height must be > 0");
                    }
                    double radius = feat.contains("radius") ? num_or_expr(feat.at("radius"), env) : 10.0;
                    if (!(radius > 0.0)) throw std::runtime_error("helix: radius must be > 0");
                    local_wire = ::geom::make_helix(pitch, height, radius, taper, start_angle, !ccw);
                }
                if (local_wire.IsNull())
                    throw std::runtime_error("helix: produced no curve geometry");
                results[id] = CurveResult{local_wire, base};
                st.ok = true;

            } else if (type == "loft") {
                // Loft a solid through >= 2 section profiles (region selectors naming
                // sketches). Each section -> its first face's outer wire -> ThruSections.
                auto sit = feat.find("sections");
                if (sit == feat.end() || !sit->is_array() || sit->size() < 2)
                    throw std::runtime_error("loft needs >= 2 sections");
                std::vector<TopoDS_Wire> wires;
                for (const json& s : *sit) {
                    const std::string sk = selector_owner_id(s);
                    if (sk.empty())
                        throw std::runtime_error("loft: malformed section selector");
                    const SketchResult& src = require_sketch(results, sk, sk.c_str());
                    if (src.faces.empty())
                        throw std::runtime_error("loft: section '" + sk + "' has no face");
                    TopoDS_Wire w = BRepTools::OuterWire(src.faces.front());
                    if (w.IsNull())
                        throw std::runtime_error("loft: section '" + sk + "' has no outer wire");
                    wires.push_back(w);
                }
                const bool ruled = feat.value("ruled", false);
                TopoDS_Shape solid = ::geom::loft_sections(wires, ruled);
                if (solid.IsNull())
                    throw std::runtime_error("loft produced an empty solid");
                // Guard the silent-degenerate case: if every section sits on the SAME plane
                // (a model forgot to offset the 2nd sketch), ThruSections yields a flat /
                // zero-volume shape and we'd report success with vol=0. Fail loudly and tell
                // the model how to fix it instead of emitting an invisible body.
                {
                    GProp_GProps lg;
                    BRepGProp::VolumeProperties(solid, lg);
                    if (!(lg.Mass() > 1e-6))
                        throw std::runtime_error(
                            "loft produced a zero-volume solid — the sections are coplanar; "
                            "put each section on its own plane (offset the later sketch, e.g. "
                            "params.offset, or use a different datum plane)");
                }

                const std::string mode = solid_mode(feat, "new");
                const BodyTargets targets = solid_targets(solid, mode);
                TopoDS_Shape out = solid;
                if (!targets.ids.empty()) {
                    out = mode == "cut" ? ::geom::cut_unify(targets.shape, solid)
                                        : ::geom::fuse_unify(targets.shape, solid);
                    if (out.IsNull())
                        throw std::runtime_error("loft " + mode + " produced an empty shape");
                    if (mode == "cut") {
                        GProp_GProps before, after;
                        BRepGProp::VolumeProperties(targets.shape, before);
                        BRepGProp::VolumeProperties(out, after);
                        if (std::abs(before.Mass() - after.Mass()) < 1e-9)
                            throw std::runtime_error("loft cut removed no material");
                        require_cut_removed_material("loft cut", before.Mass(), after.Mass());
                    }
                    consumed.insert(targets.ids.begin(), targets.ids.end());
                    source_of[id] = targets.ids.front();
                } else if (mode == "cut") {
                    throw std::runtime_error("loft: cut mode requires an existing body");
                }
                results[id] = SolidResult{out};
                tool_body_of[id] = solid;
                feature_op_of[id] = mode == "cut" ? "cut" : "add";
                record_tool(id, mode == "cut" ? "cut" : "add", solid);
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "box" || type == "cylinder" || type == "sphere" ||
                       type == "cone" || type == "torus") {
                // Base primitives, centered on the origin and built along their natural
                // local +Z by default. An optional `at` [x,y,z] param MOVES the primitive
                // and an optional `axis` ORIENTS it, so a boss/cut/hole can sit off-origin
                // pointing any direction (e.g. a cylinder cut at a corner = an off-center
                // hole; axis:[1,0,0] = a cross-bore) — see the orientation/placement block
                // after the solid is built. Port of engine/doc/features/primitives.py.
                TopoDS_Shape solid;
                if (type == "box") {
                    // Validate the box dims UP FRONT (Bug A6): a 0/negative length, width
                    // or height used to fall through to make_box_centered and surface late
                    // as the opaque "box produced an empty solid". Reject with a clear,
                    // dimension-named message instead.
                    const double bl = num_or_expr(feat.at("length"), env);
                    const double bw = num_or_expr(feat.at("width"), env);
                    const double bh = num_or_expr(feat.at("height"), env);
                    if (!(bl > 0.0 && bw > 0.0 && bh > 0.0))
                        throw std::runtime_error("box: length/width/height must be > 0");
                    // SUB-TOLERANCE guard (Bug K7): a dim above 0 but below OCCT's modelling
                    // tolerance (1e-7) builds a degenerate solid that surfaces late as the
                    // opaque "unknown error during feature compile". Name the offending dim.
                    {
                        const double kMinDim = 1e-7;  // Precision::Confusion
                        const double bad = std::min({bl, bw, bh});
                        if (bad < kMinDim) {
                            char buf[160];
                            std::snprintf(buf, sizeof(buf),
                                "box: dimension %.3g is below the minimum tolerance (%.0e mm)",
                                bad, kMinDim);
                            throw std::runtime_error(buf);
                        }
                    }
                    solid = ::geom::make_box_centered(bl, bw, bh);
                } else if (type == "cylinder") {
                    // Sibling primitive on the SAME code path: validate up front so a
                    // 0/negative radius or height errors clearly, not as "empty solid".
                    const double cr = num_or_expr(feat.at("radius"), env);
                    const double ch = num_or_expr(feat.at("height"), env);
                    if (!(cr > 0.0 && ch > 0.0))
                        throw std::runtime_error("cylinder: radius/height must be > 0");
                    solid = ::geom::make_cylinder_centered(cr, ch);
                } else if (type == "sphere") {
                    const double sr = num_or_expr(feat.at("radius"), env);
                    if (!(sr > 0.0))
                        throw std::runtime_error("sphere: radius must be > 0");
                    solid = ::geom::make_sphere(sr);
                } else if (type == "cone") {
                    // A cone allows ONE radius to be 0 (a pointed apex), but not both, and
                    // the height must be positive.
                    const double cr1 = num_or_expr(feat.at("r1"), env);
                    const double cr2 = num_or_expr(feat.at("r2"), env);
                    const double cch = num_or_expr(feat.at("height"), env);
                    if (!(cr1 >= 0.0 && cr2 >= 0.0 && (cr1 > 0.0 || cr2 > 0.0)))
                        throw std::runtime_error(
                            "cone: r1/r2 must be >= 0 and not both 0");
                    if (!(cch > 0.0))
                        throw std::runtime_error("cone: height must be > 0");
                    solid = ::geom::make_cone_centered(cr1, cr2, cch);
                } else {  // torus
                    const double mr = num_or_expr(feat.at("major_r"), env);
                    const double nr = num_or_expr(feat.at("minor_r"), env);
                    if (!(mr > 0.0 && nr > 0.0))
                        throw std::runtime_error("torus: major_r/minor_r must be > 0");
                    solid = ::geom::make_torus(mr, nr);
                }
                if (solid.IsNull())
                    throw std::runtime_error(type + " produced an empty solid");

                // ORIENTATION + PLACEMENT. `axis`, `at`, `at_offset` and `centered` stay
                // NORMAL, auditable feature params — plain editable values shown in the
                // tree and patchable via edit_feature; there is no hidden or baked
                // transform. An `at` [x,y,z] point translates the centered primitive so a
                // boss/cut/hole can sit off-origin, and `axis` rotates the primitive's
                // natural local +Z onto an arbitrary direction so a sideways cylinder no
                // longer needs a sketch+extrude. With `centered:false` the primitive's
                // natural base/corner sits at `at` (SolidWorks convention): box corner,
                // cylinder/cone base center; sphere/torus have no corner so they stay
                // centered on `at`.
                //
                // NORMATIVE ORDER: build centred -> rotate local +Z onto `axis` about the
                // ORIGIN -> add the `centered:false` base offset ALONG THE ROTATED AXIS ->
                // translate by `at` -> add `at_offset`. `axis` defaults to [0,0,1], where
                // the rotation is skipped outright so every pre-existing primitive keeps
                // bit-identical topology. A sphere is rotation-invariant, so it accepts
                // `axis` as a documented geometric no-op rather than erroring.
                {
                    // (1) The requested axis. `axis` is the canonical feature-level key
                    // (session.cpp hoists it into params, normalize_feature merges params
                    // back to the top level — exactly how `at`/`centered` arrive). `axis_dir`
                    // is what normalize_feature leaves behind when the model routed the
                    // direction through the refs.axis slot instead (a reserved datum
                    // "axis.x", or an explicit {"axis":[x,y,z]} wrapper) — honour that form
                    // too rather than dropping it silently.
                    Eigen::Vector3d paxis(0, 0, 1);
                    // An explicit null means "not specified" (same tolerance `plane`/
                    // `offset` already give), not a malformed direction.
                    const bool has_axis = feat.contains("axis") && !feat.at("axis").is_null();
                    const bool has_axis_dir =
                        feat.contains("axis_dir") && !feat.at("axis_dir").is_null();
                    const char* akey = has_axis ? "axis" : has_axis_dir ? "axis_dir" : nullptr;
                    if (akey) {
                        const json& av = feat.at(akey);
                        std::optional<Eigen::Vector3d> pa = parse_signed_axis(av);
                        if (!pa)
                            throw std::runtime_error(
                                type + ": axis must be [x,y,z] or one of \"x\", \"+x\", "
                                "\"-x\", \"y\", \"+y\", \"-y\", \"z\", \"+z\", \"-z\" (got " +
                                av.dump() + ")");
                        if (!pa->allFinite() || !(pa->norm() > 1e-9))
                            throw std::runtime_error(
                                type + ": axis " + av.dump() + " is zero-length or "
                                "non-finite — give a real direction, e.g. axis:[1,0,0]");
                        paxis = pa->normalized();
                    }

                    Eigen::Vector3d at(0, 0, 0);
                    bool placed = false;
                    auto at_it = feat.find("at");
                    // (2) RELATIONAL placement: `at` may be a geometry QUERY ({"of":"face",
                    // "side":"top"}) instead of a coordinate — place the primitive at the
                    // centroid of the matched face/edge on the current body, so a model can
                    // say "a boss on the top face" with no coordinates.
                    if (at_it != feat.end() && at_it->is_object() &&
                        chudware::doc::is_geom_query(*at_it)) {
                        const TopoDS_Shape& abody = last_body_shape("at");
                        chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(abody, *at_it);
                        if (!qp.error.empty())
                            throw std::runtime_error(std::string("at: ") + qp.error);
                        if (qp.selectors.empty())
                            throw std::runtime_error("at: query matched no geometry");
                        const json& c = qp.selectors.front().at("centroid");
                        at = Eigen::Vector3d(c[0].get<double>(), c[1].get<double>(),
                                             c[2].get<double>());
                        placed = true;
                    } else if (at_it != feat.end() && at_it->is_array() && at_it->size() >= 3 &&
                        (*at_it)[0].is_number()) {
                        at = Eigen::Vector3d((*at_it)[0].get<double>(),
                                             (*at_it)[1].get<double>(),
                                             (*at_it)[2].get<double>());
                        placed = true;
                    }
                    // (3) The `centered:false` base offset, measured on the UNROTATED
                    // centred solid's unpadded bbox, then carried through the rotation
                    // with the body.
                    Eigen::Vector3d base_off(0, 0, 0);
                    bool has_base = false;
                    if (!feat.value("centered", true)) {
                        Bnd_Box bb;
                        BRepBndLib::Add(solid, bb);
                        if (!bb.IsVoid()) {
                            double xmn, ymn, zmn, xmx, ymx, zmx;
                            // Bnd_Box's tolerance padding is not geometry: including it
                            // lifts a nominally touching base off the target face.
                            bb.SetGap(0.0);
                            bb.Get(xmn, ymn, zmn, xmx, ymx, zmx);
                            if (type == "box") {
                                base_off = Eigen::Vector3d((xmx - xmn) / 2.0, (ymx - ymn) / 2.0,
                                                           (zmx - zmn) / 2.0);
                            } else if (type == "cylinder" || type == "cone") {
                                base_off.z() = (zmx - zmn) / 2.0;
                            }
                            has_base = true;
                        }
                    }
                    // (4) Rotate local +Z onto `axis` about the origin, BEFORE any
                    // translation, and carry the base offset through the same rotation so
                    // "base sits at `at`" still holds on the rotated axis. The default +Z
                    // takes no transform at all (identity fast path). The antiparallel case
                    // (-Z) has a degenerate cross product, so spin a half turn about +X.
                    const double cosang = paxis.z();  // paxis is unit; dot with +Z
                    if (cosang < 1.0 - 1e-12) {
                        Eigen::Vector3d rot_axis(1, 0, 0);
                        double angle = PI;
                        if (cosang > -1.0 + 1e-12) {
                            rot_axis = Eigen::Vector3d(0, 0, 1).cross(paxis).normalized();
                            angle = std::acos(std::max(-1.0, std::min(1.0, cosang)));
                        }
                        solid = ::geom::rotate_copy(solid, Eigen::Vector3d::Zero(), rot_axis,
                                                    angle);
                        if (solid.IsNull())
                            throw std::runtime_error(type +
                                                     ": axis rotation produced an empty solid");
                        base_off = Eigen::AngleAxisd(angle, rot_axis) * base_off;
                    }
                    if (has_base) {
                        at += base_off;
                        placed = true;
                    }
                    // (5) Optional `at_offset:[dx,dy,dz]` — a world-axis nudge on top of
                    // either an absolute or a relational `at` (e.g. a boss centred on the
                    // top face then raised: at={of:face,side:top}, at_offset:[0,0,10]).
                    if (feat.contains("at_offset") && feat.at("at_offset").is_array() &&
                        feat.at("at_offset").size() >= 3 && feat.at("at_offset")[0].is_number()) {
                        const json& o = feat.at("at_offset");
                        at += Eigen::Vector3d(o[0].get<double>(), o[1].get<double>(),
                                              o[2].get<double>());
                        placed = true;
                    }
                    if (placed) solid = ::geom::translate_copy(solid, at);
                }

                const std::string pmode = solid_mode(feat, "boss");
                const BodyTargets targets = solid_targets(solid, pmode);
                const std::string ptgt = targets.ids.empty() ? std::string() : targets.ids.front();
                if (ptgt.empty()) {
                    if (pmode == "cut")
                        throw std::runtime_error(type + ": cut mode requires an existing body");
                    results[id] = SolidResult{solid};
                    record_tool(id, "add", solid);
                } else {
                    const TopoDS_Shape& target = targets.shape;
                    // Additive booleans run build123d's clean() (UnifySameDomain) like the
                    // extrude/loft/mirror/pattern boss paths: a plain fuse leaves every flush
                    // or overlapping primitive's coplanar faces split, and the viewport draws
                    // those splits as seams that read as "not fused" (6-axis arm, 2026-09-27:
                    // upper_arm had 125 faces where 57 describe the solid).
                    TopoDS_Shape out = (pmode == "cut") ? ::geom::cut_unify(target, solid)
                                                        : ::geom::fuse_unify(target, solid);
                    if (out.IsNull())
                        throw std::runtime_error("primitive boolean " + pmode +
                                                 " produced an empty shape");
                    if (pmode == "cut") {
                        GProp_GProps gb, ga;
                        BRepGProp::VolumeProperties(target, gb);
                        BRepGProp::VolumeProperties(out, ga);
                        if (std::abs(ga.Mass() - gb.Mass()) < 1e-9) {
                            // Bare "primitive cut removed no material" told the model
                            // WHAT happened and nothing about WHY. Live 2026-08-04
                            // (replay repro-holster-2) it re-aimed the same drain cut
                            // five times — a quarter of the turn's step budget — because
                            // nothing said the tool was sitting outside the body. Report
                            // both bounding boxes: the overlap is then arithmetic, not a
                            // guess. Same shape as the boss guard below.
                            auto box_of = [](const TopoDS_Shape& s) {
                                Bnd_Box bb;
                                BRepBndLib::Add(s, bb);
                                if (bb.IsVoid()) return std::string("(empty)");
                                double xa, ya, za, xb, yb, zb;
                                bb.Get(xa, ya, za, xb, yb, zb);
                                std::ostringstream os;
                                os.precision(4);
                                os << "[" << xa << "," << ya << "," << za << "]..["
                                   << xb << "," << yb << "," << zb << "]";
                                return os.str();
                            };
                            throw std::runtime_error(
                                type + " cut removed no material — the tool and the body "
                                "do not overlap. Body bbox " + box_of(target) +
                                ", cut tool bbox " + box_of(solid) +
                                ". Primitives are CENTRED on the origin unless you place "
                                "them: give at:{\"of\":\"face\",\"side\":\"top\"} or an "
                                "explicit position inside the body bbox above, and make "
                                "the tool long enough to pass through. Move the tool "
                                "before changing its size.");
                        }
                        require_cut_removed_material(type + " cut", gb.Mass(), ga.Mass());
                    } else {
                        // Symmetric buried-boss guard: a primitive boss merged into a body
                        // that adds NO volume lies entirely inside it. Report the
                        // OBSERVATION and list the plausible causes — do not assert one of
                        // them as fact (same lesson as the extrude-boss guard below, live
                        // run run-42508173312214: a single-cause message sent the model
                        // three calls deep fixing something that was never wrong).
                        GProp_GProps gb, ga;
                        BRepGProp::VolumeProperties(target, gb);
                        BRepGProp::VolumeProperties(out, ga);
                        if (ga.Mass() <= gb.Mass() + 1e-9)
                            throw std::runtime_error(
                                type + " boss added no material — the fuse with the current "
                                "body gained zero volume, so this primitive lies entirely "
                                "inside it. Plausible causes: (a) it is smaller than, and "
                                "sits inside, the body it merged into; (b) primitives are "
                                "CENTRED on the origin, so it needs placing on the surface, "
                                "e.g. at:{\"of\":\"face\",\"side\":\"top\"}, or "
                                "centered:false; (c) mode:\"cut\" was meant instead of a "
                                "boss. Check the placement before changing anything else.");
                    }
                    record_tool(id, pmode == "cut" ? "cut" : "add", solid);
                    consumed.insert(targets.ids.begin(), targets.ids.end());
                    source_of[id] = ptgt;  // owner-map: result derives from the target body
                    results[id] = SolidResult{out};
                }
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "import") {
                // Import a neutral B-rep file as frozen body/bodies (engine/doc/features/
                // import_feature.py). PREFER geometry BAKED into the feature itself
                // (`embedded_brep`, written by doc_import_file): that makes a saved .step+
                // self-contained — it no longer depends on the original source file
                // surviving on disk (Bug N-8). Fall back to re-reading `source_path` for
                // legacy features / when no blob is present. Each imported body becomes
                // its OWN body ("<id>#k"), so a multi-body STEP keeps its bodies separate
                // (Bug N-5) instead of being merged into one compound.
                std::vector<TopoDS_Shape> solids;
                // Body-name roster, parallel to `solids` ("" where the source names
                // nothing). It is what keep_bodies/exclude_bodies match strings against,
                // and what an unknown-name error lists back to the caller. Until now
                // `embedded_body_names` (written by doc_import_file) was never read by the
                // compiler at all.
                std::vector<std::string> body_names;
                std::string src_desc;
                const std::string source_reference = str_field(feat, "source_path", "");
                if (native_policy::rooted() && !source_reference.empty())
                    native_policy::resolve_reference(source_reference);
                const std::string embedded = str_field(feat, "embedded_brep", "");
                if (!embedded.empty()) {
                    src_desc = "embedded geometry";
                    // Key by content hash, not the multi-MB blob string.
                    const uint64_t emb_key = cc_fnv1a(embedded);
                    auto cit = import_cache.find(emb_key);
                    if (cit != import_cache.end()) {
                        solids = cit->second;  // SHARE the cached TShape(s) across identical imports
                    } else {
                        // DXF/DWG/SAT imports bake one compound child per imported body
                        // (`embedded_bodies` of them): keep that grouping, as the source
                        // re-read below does, so a drawing's 2D profile (faces + construction
                        // wires) stays one body beside its ACIS solids instead of splitting
                        // into face-less wire bodies. Other formats split into solids +
                        // remnants as before.
                        const TopoDS_Shape baked = brep_b64_to_shape(embedded);
                        const std::string bfmt = str_field(feat, "format", "");
                        const auto nit = feat.find("embedded_bodies");
                        const long long baked_n =
                            nit != feat.end() && nit->is_number_integer() ? nit->get<long long>() : -1;
                        std::vector<TopoDS_Shape> children;
                        if ((bfmt == "dxf" || bfmt == "dwg" || bfmt == "sat") && !baked.IsNull() &&
                            baked.ShapeType() == TopAbs_COMPOUND)
                            for (TopoDS_Iterator it(baked); it.More(); it.Next())
                                children.push_back(it.Value());
                        solids = baked_n > 0 && static_cast<long long>(children.size()) == baked_n
                                     ? children
                                     : split_import_bodies(baked);
                        import_cache.emplace(emb_key, solids);
                    }
                }
                if (solids.empty()) {
                    // No usable embedded blob — read the source file (legacy path).
                    const std::string src_path = native_policy::resolve_reference(
                        str_field(feat, "source_path", ""));
                    if (src_path.empty())
                        throw std::runtime_error(
                            "import feature has neither embedded geometry nor 'source_path'");
                    src_desc = src_path;
                    std::string ifmt = str_field(feat, "format", "");
                    if (ifmt.empty()) ifmt = detect_format(src_path);
                    std::vector<std::pair<std::string, TopoDS_Shape>> imported;
                    std::vector<std::string> import_warnings;
                    try {
                        imported = import_bodies(src_path, ifmt, &import_warnings);
                    } catch (const OpError& oe) {
                        throw std::runtime_error("import failed for '" + src_path +
                                                 "': " + oe.what());
                    }
                    for (const std::string& warning : import_warnings)
                        st.warning = st.warning.empty() ? warning : st.warning + "; " + warning;
                    for (auto& [name, shp] : imported)
                        if (!shp.IsNull()) {
                            solids.push_back(shp);
                            body_names.push_back(name);
                        }
                }
                if (solids.empty())
                    throw std::runtime_error("import: no usable bodies in '" + src_desc + "'");
                if (body_names.empty()) {
                    if (auto nit = feat.find("embedded_body_names");
                        nit != feat.end() && nit->is_array())
                        for (const auto& nv : *nit)
                            body_names.push_back(nv.is_string() ? nv.get<std::string>()
                                                                : std::string());
                }
                body_names.resize(solids.size());

                // --- keep_bodies / exclude_bodies: sub-select which of a multi-body
                // import actually becomes a body. Merging independently sourced CAD means
                // importing a vendor STEP that carries the whole assembly when only one
                // part of it is wanted; without this the extra bodies land in the document
                // and every mass/interference number afterwards is wrong. Entries are
                // 1-based ordinals (matching the "<fid>#k" body order) or body-name
                // strings matched against the roster above. An unknown entry is a COMPILE
                // ERROR listing the roster — a silently ignored filter is how the wrong
                // body ships. Dropped bodies are never registered, so a downstream
                // reference to one fails through the normal require_solid path.
                {
                    auto roster = [&]() {
                        std::ostringstream os;
                        for (std::size_t k = 0; k < solids.size(); ++k) {
                            if (k) os << ", ";
                            os << (k + 1);
                            if (!body_names[k].empty()) os << ":\"" << body_names[k] << "\"";
                        }
                        return os.str();
                    };
                    std::vector<char> keep(solids.size(), 1);
                    auto apply_list = [&](const json& list, const char* pname, char on) {
                        for (const auto& e : list) {
                            if (e.is_number_integer()) {
                                const long long ord = e.get<long long>();
                                if (ord < 1 || ord > static_cast<long long>(solids.size()))
                                    throw std::runtime_error(
                                        std::string("import: ") + pname + " has no body " +
                                        std::to_string(ord) + " — ordinals are 1-based and '" +
                                        src_desc + "' has " + std::to_string(solids.size()) +
                                        " bodies: " + roster());
                                keep[static_cast<std::size_t>(ord) - 1] = on;
                            } else if (e.is_string()) {
                                const std::string want = e.get<std::string>();
                                bool hit = false;
                                for (std::size_t k = 0; k < solids.size(); ++k)
                                    if (body_names[k] == want) {
                                        keep[k] = on;
                                        hit = true;
                                    }
                                if (!hit)
                                    throw std::runtime_error(
                                        std::string("import: ") + pname + " names no body '" +
                                        want + "' — '" + src_desc + "' has " +
                                        std::to_string(solids.size()) + " bodies: " + roster());
                            } else {
                                throw std::runtime_error(
                                    std::string("import: ") + pname +
                                    " entries must be a 1-based body ordinal (int) or a body "
                                    "name (string); bodies here: " + roster());
                            }
                        }
                    };
                    auto kit = feat.find("keep_bodies");
                    if (kit != feat.end() && kit->is_array()) {
                        if (kit->empty())
                            throw std::runtime_error(
                                "import: keep_bodies is empty — omit it to keep every body. "
                                "Bodies here: " + roster());
                        std::fill(keep.begin(), keep.end(), 0);
                        apply_list(*kit, "keep_bodies", 1);
                    }
                    auto xit = feat.find("exclude_bodies");
                    if (xit != feat.end() && xit->is_array())
                        apply_list(*xit, "exclude_bodies", 0);

                    if (std::find(keep.begin(), keep.end(), 1) == keep.end())
                        throw std::runtime_error(
                            "import: keep_bodies/exclude_bodies dropped every body of '" +
                            src_desc + "' — an import must yield at least one body. Bodies "
                            "here: " + roster());
                    std::vector<TopoDS_Shape> kept;
                    std::vector<std::string> kept_names;
                    for (std::size_t k = 0; k < solids.size(); ++k)
                        if (keep[k]) {
                            kept.push_back(solids[k]);
                            kept_names.push_back(body_names[k]);
                        }
                    solids = std::move(kept);
                    body_names = std::move(kept_names);
                }

                // Register each surviving body as a separate body (id = "<fid>" then
                // "<fid>#k"), renumbered over the KEPT set.
                for (std::size_t k = 0; k < solids.size(); ++k) {
                    std::string pid = (k == 0) ? id : id + "#" + std::to_string(k);
                    results[pid] = SolidResult{solids[k]};
                    if (!TopExp_Explorer(solids[k], TopAbs_SOLID).More())
                        body_kinds[pid] = "surface";
                    solid_order.push_back(pid);
                }
                st.ok = true;

            } else if (type == "split") {
                // Split the running solid(s) by a planar tool into pieces (engine/doc/
                // features/split.py). PLANAR tool only here (a datum plane / reserved
                // plane; non-planar BOP surface split is deferred). keep="both" yields
                // both half-pieces, "above"/"below" keep one side along +normal. Each
                // resulting piece is a separate body — gated on exact piece volume.
                const std::string keep = feat.value("keep", std::string("both"));
                if (keep != "both" && keep != "above" && keep != "below")
                    throw std::runtime_error(
                        "split: unknown keep '" + keep + "'. keep must be one of "
                        "\"both\", \"above\", \"below\"");
                if (!feat.contains("tool"))
                    throw std::runtime_error("split: refs.tool is required (a planar datum)");

                // --- non-planar / surface split (split.py::_surface_split) ----------
                // A FACE selector whose best match across the running bodies is a CURVED
                // surface takes the general BOP splitter path (BRepAlgoAPI_Splitter);
                // planar faces, datum planes, and reserved planes fall through to the
                // half-space planar path below (behaviour for planar tools unchanged).
                // Mirrors split.py::_tool_is_curved_face -> _surface_split exactly.
                bool nonplanar_done = false;
                {
                    const json& tool = feat.at("tool");
                    bool tool_is_face_sel = tool.is_object() &&
                                            tool.value("kind", std::string()) == "face";
                    TopoDS_Face tool_face;
                    std::string owner_id;  // id of the body that OWNS the tool face
                    bool tool_resolved = false;
                    bool tool_is_curved = false;
                    if (tool_is_face_sel) {
                        // Resolve the face pick against every surviving solid body (the
                        // oracle scans ctx.bodies and keeps the best-scoring match); take
                        // the first body that resolves it, tracking the owner id so the
                        // owning body is never split by its own face.
                        for (const std::string& cand : solid_order) {
                            if (consumed.count(cand)) continue;
                            if (!std::holds_alternative<SolidResult>(*results.at(cand))) continue;
                            const TopoDS_Shape& cb = std::get<SolidResult>(*results.at(cand)).shape;
                            if (cb.IsNull()) continue;
                            chudware::doc::FacePicks fp =
                                chudware::doc::resolve_face_picks(cb, tool, "tool");
                            if (!fp.faces.empty()) {
                                tool_face = fp.faces.front();
                                owner_id = cand;
                                tool_resolved = true;
                                BRepAdaptor_Surface ad(tool_face);
                                tool_is_curved = (ad.GetType() != GeomAbs_Plane);
                                break;
                            }
                        }
                    }
                    if (tool_resolved && tool_is_curved) {
                        // Gather surviving solids the tool acts on (the running bodies).
                        std::vector<std::string> targets;
                        for (const std::string& sid : solid_order)
                            if (!consumed.count(sid) &&
                                std::holds_alternative<SolidResult>(*results.at(sid)))
                                targets.push_back(sid);
                        if (targets.empty())
                            throw std::runtime_error("split: no body to split");

                        std::vector<std::pair<std::string, TopoDS_Shape>> new_pieces;
                        int split_count = 0;
                        for (const std::string& sid : targets) {
                            // A body is never split by one of its OWN faces (degenerate:
                            // BOP returns it whole) — pass the owner through unchanged.
                            if (sid == owner_id) continue;
                            const TopoDS_Shape& body =
                                std::get<SolidResult>(*results.at(sid)).shape;
                            std::vector<TopoDS_Shape> kept =
                                ::geom::split_solid_by_surface(body, tool_face, keep);
                            if (kept.empty()) continue;  // surface missed/tangent
                            ++split_count;
                            consumed.insert(sid);
                            for (const TopoDS_Shape& piece : kept)
                                new_pieces.emplace_back(sid, piece);
                        }
                        if (split_count == 0)
                            throw std::runtime_error(
                                "split: the tool surface does not intersect/divide any body");

                        // Register each kept piece as a new solid (id = "<fid>#k").
                        int k = 0;
                        for (auto& [src_id, shp] : new_pieces) {
                            std::string pid =
                                (k == 0 && new_pieces.size() == 1)
                                    ? id
                                    : id + "#" + std::to_string(k);
                            results[pid] = SolidResult{shp};
                            solid_order.push_back(pid);
                            source_of[pid] = src_id;
                            ++k;
                        }
                        st.ok = true;
                        nonplanar_done = true;  // skip the planar half-space path
                    }
                }

                if (!nonplanar_done) {
                auto tp = resolve_plane_ref(feat.at("tool"));
                if (!tp)
                    throw std::runtime_error(
                        "split: tool is not a planar datum (face-selector tools not yet ported)");
                const Eigen::Vector3d point = tp->origin;
                const Eigen::Vector3d normal = tp->normal.normalized();

                // Gather surviving solids (the running bodies the plane acts on).
                std::vector<std::string> targets;
                for (const std::string& sid : solid_order)
                    if (!consumed.count(sid) &&
                        std::holds_alternative<SolidResult>(*results.at(sid)))
                        targets.push_back(sid);
                if (targets.empty())
                    throw std::runtime_error("split: no body to split");

                // Reach must cover every body; half-space box near-face on the plane,
                // body extending +normal (mirrors split.py::_half_space exactly).
                double reach = 100.0;
                for (const std::string& sid : targets) {
                    Bnd_Box bb;
                    BRepBndLib::Add(std::get<SolidResult>(*results.at(sid)).shape, bb);
                    if (bb.IsVoid()) continue;
                    double xmn, ymn, zmn, xmx, ymx, zmx;
                    bb.Get(xmn, ymn, zmn, xmx, ymx, zmx);
                    Eigen::Vector3d d(xmx - xmn, ymx - ymn, zmx - zmn);
                    reach = std::max(reach, std::max(d.norm(), 1e-3));
                }
                reach = reach * 2.0 + 10.0;

                // Half-space box: a 2*reach cube spanning u,v∈[-reach,reach] and
                // n∈[0,2*reach] in the plane's local frame, so its near face IS the
                // cutting plane and it fills the +normal side (mirrors split.py's
                // make_box(2r,2r,2r).translate(-r,-r,0) placed by the cut plane).
                chudware::geom::SketchPlane cut = chudware::geom::SketchPlane::from(point, normal);
                Eigen::Vector3d corner = cut.origin - cut.uAxis * reach - cut.vAxis * reach;
                gp_Ax2 box_frame(gp_Pnt(corner.x(), corner.y(), corner.z()),
                                 gp_Dir(cut.normal.x(), cut.normal.y(), cut.normal.z()),
                                 gp_Dir(cut.uAxis.x(), cut.uAxis.y(), cut.uAxis.z()));
                TopoDS_Shape half_space =
                    BRepPrimAPI_MakeBox(box_frame, 2 * reach, 2 * reach, 2 * reach).Shape();
                if (half_space.IsNull())
                    throw std::runtime_error("split: failed to build the half-space tool");

                std::vector<std::pair<std::string, TopoDS_Shape>> new_pieces;  // (src_id, shape)
                int split_count = 0;
                for (const std::string& sid : targets) {
                    const TopoDS_Shape& body = std::get<SolidResult>(*results.at(sid)).shape;
                    TopoDS_Shape above = ::geom::common(body, half_space);
                    TopoDS_Shape below = ::geom::cut(body, half_space);
                    auto vol = [](const TopoDS_Shape& s) -> double {
                        if (s.IsNull()) return 0.0;
                        GProp_GProps g;
                        BRepGProp::VolumeProperties(s, g);
                        return g.Mass();
                    };
                    bool has_above = !above.IsNull() && vol(above) > 1e-9;
                    bool has_below = !below.IsNull() && vol(below) > 1e-9;
                    if (!(has_above && has_below)) {
                        // Plane misses / is tangent to this body: it survives unchanged.
                        continue;
                    }
                    ++split_count;
                    consumed.insert(sid);
                    if (keep == "above") {
                        new_pieces.emplace_back(sid, above);
                    } else if (keep == "below") {
                        new_pieces.emplace_back(sid, below);
                    } else {  // both
                        new_pieces.emplace_back(sid, above);
                        new_pieces.emplace_back(sid, below);
                    }
                }
                if (split_count == 0)
                    throw std::runtime_error("split: the tool plane does not intersect any body");

                // Register each kept piece as a new solid result (id = "<fid>#k").
                int k = 0;
                for (auto& [src_id, shp] : new_pieces) {
                    std::string pid = (k == 0 && new_pieces.size() == 1) ? id
                                                                         : id + "#" + std::to_string(k);
                    results[pid] = SolidResult{shp};
                    solid_order.push_back(pid);
                    source_of[pid] = src_id;  // owner-map: piece derives from the split body
                    ++k;
                }
                st.ok = true;
                }  // end planar half-space path (!nonplanar_done)

            } else if (type == "extrude") {
                // Profile faces honor region selectors (index/centroid) via the shared
                // resolver; a bare sketch id still means whole-sketch (all material faces).
                auto pit = feat.find("profile");
                if (pit == feat.end())
                    throw std::runtime_error("missing string ref 'profile'");
                const std::string prof = profile_sketch_id(*pit);
                if (prof.empty())
                    throw std::runtime_error("missing string ref 'profile'");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                const std::string mode = solid_mode(feat, "new");
                const bool is_cut = (mode == "cut");
                // Canonical end condition: "blind" (depth), "through_all" (penetrate
                // the body both ways), "mid_plane" (centered). Simplified shape uses
                // depth + optional mid_plane bool.
                const std::string end = feat.value("end", std::string("blind"));
                bool mid_plane = feat.value("mid_plane", false) || end == "mid_plane";
                std::optional<Eigen::Vector3d> explicit_dir = opt_vec3(feat, "dir");
                const bool dir_was_explicit = explicit_dir.has_value();
                Eigen::Vector3d dir = explicit_dir.value_or(src.plane.normal);
                // Reverse direction checkbox: negate the resolved dir after plane normal /
                // explicit dir so drag and PM share one truth. Must run before start-shift
                // and depth so offsets follow the flipped side.
                const bool flip = feat.value("flip", false);
                if (flip) dir = -dir;

                // For a cut / through_all we size against the current body.
                const std::string tgt_id = is_cut ? current_solid_id() : std::string();

                // --- thin feature: replace each filled profile face with its thin-wall
                // ring (port of extrude.py::_thin_profile_face). thin_side: one (inside,
                // default) | two/mid (1/2 each side) | outside.
                std::vector<TopoDS_Face> faces = resolve_profile_faces(results, feat);
                const bool thin = feat.value("thin", false);
                if (thin) {
                    const double thin_thk = feat.contains("thin_thickness")
                        ? num_or_expr(feat.at("thin_thickness"), env) : 1.0;
                    if (!(thin_thk > 0.0))
                        throw std::runtime_error("extrude: thin thickness must be > 0");
                    std::string side = feat.value("thin_side", std::string("one"));
                    double outer_off, inner_off;
                    if (side == "two" || side == "both" || side == "mid") {
                        outer_off = thin_thk / 2.0; inner_off = -thin_thk / 2.0;
                    } else if (side == "outside" || side == "outer") {
                        outer_off = thin_thk; inner_off = 0.0;
                    } else {  // one / inside (default)
                        outer_off = 0.0; inner_off = -thin_thk;
                    }
                    std::vector<TopoDS_Face> rings;
                    for (const TopoDS_Face& f : faces) {
                        TopoDS_Face ring = ::geom::thin_ring_face(f, outer_off, inner_off);
                        if (ring.IsNull())
                            throw std::runtime_error("extrude: thin wall offset failed "
                                                     "(thickness too large for the profile)");
                        rings.push_back(ring);
                    }
                    faces = std::move(rings);
                }

                // --- draft taper (degrees). SW "draft inward" (default) = positive taper
                // (narrows toward the direction); draft_outward flips the sign. Applies to
                // blind / mid_plane only — greyed out for through_all / up_to_* ends.
                double taper_deg = feat.contains("draft")
                    ? num_or_expr(feat.at("draft"), env) : 0.0;
                if (feat.value("draft_outward", false)) taper_deg = -taper_deg;
                const bool draftable = (end == "blind" || end == "mid_plane");
                if (!draftable) taper_deg = 0.0;

                // --- direction-1 linear amount + the dir-2 slab. The geometry-driven ends
                // (through_all / up_to_vertex) reduce to a blind amount here; up_to_vertex
                // reads the stamped vertex centroid from the `up_to` selector and projects
                // it onto the direction (extrude.py::_up_to_vertex_amount).
                auto bbox_diag_of_face = [](const TopoDS_Face& f) -> double {
                    Bnd_Box bb; BRepBndLib::Add(f, bb);
                    if (bb.IsVoid()) return 100.0;
                    double xmn, ymn, zmn, xmx, ymx, zmx;
                    bb.Get(xmn, ymn, zmn, xmx, ymx, zmx);
                    Eigen::Vector3d d(xmx - xmn, ymx - ymn, zmx - zmn);
                    return std::max(d.norm(), 1e-3);
                };
                auto face_center = [](const TopoDS_Face& f) -> Eigen::Vector3d {
                    GProp_GProps g; BRepGProp::SurfaceProperties(f, g);
                    gp_Pnt c = g.CentreOfMass();
                    return Eigen::Vector3d(c.X(), c.Y(), c.Z());
                };
                // --- start condition (SW "From"): shift the profile along `dir` so the
                // extrude can begin at an offset, at a reference plane, or plane+offset
                // (default sketch_plane = no shift). A reference is a datum plane OR a
                // planar model face — resolved by plane_of (datum.py::_plane_of), which
                // is what lets the user pick a reference PLANE, not just a model face.
                {
                    const std::string start =
                        feat.value("start", std::string("sketch_plane"));
                    const double soff = feat.contains("start_offset")
                        ? num_or_expr(feat.at("start_offset"), env) : 0.0;
                    double start_shift = 0.0;
                    if (start == "offset") {
                        start_shift = soff;
                    } else if (start == "from_reference" ||
                               start == "offset_from_reference") {
                        if (!feat.contains("start_ref"))
                            throw std::runtime_error("extrude: " + start +
                                " needs a `start_ref` plane/surface reference");
                        chudware::geom::SketchPlane sp =
                            plane_of(feat.at("start_ref"), "start_ref");
                        start_shift = dir.dot(sp.origin - face_center(faces[0]));
                        if (start == "offset_from_reference") start_shift += soff;
                    }
                    if (std::abs(start_shift) > 1e-12) {
                        Eigen::Vector3d sv = dir * start_shift;
                        for (TopoDS_Face& f : faces)
                            f = TopoDS::Face(::geom::translate_copy(f, sv));
                    }
                }

                // Up-to-plane end state: when set, the prism is built with an overshoot
                // depth and then trimmed to `trim_plane` (+ optional offset) after the
                // build, so the end cap conforms to the reference plane/planar surface.
                bool trim_to_plane = false;
                chudware::geom::SketchPlane trim_plane;
                double end_offset_val = 0.0;

                double depth;
                if (end == "through_all") {
                    if (!tgt_id.empty()) {
                        const TopoDS_Shape& tb = require_solid(results, tgt_id, "target");
                        const double diag = chudware::doc::bbox_diag(tb);
                        if (!(diag > 0.0))
                            throw std::runtime_error("extrude through_all: body has no extent");
                        depth = diag * 2.2;   // span the whole body...
                        mid_plane = true;     // ...both directions from the sketch plane
                    } else {
                        // No prior body: bbox fallback (extrude.py through_all branch) —
                        // blind extrude of 1.05 * (2 * profile bbox diag), one direction.
                        depth = 1.05 * (bbox_diag_of_face(faces[0]) * 2.0);
                        mid_plane = false;
                    }
                } else if (end == "up_to_vertex") {
                    auto up_it = feat.find("up_to");
                    if (up_it == feat.end() || !up_it->is_object())
                        throw std::runtime_error("extrude: up_to_vertex needs an `up_to` vertex selector");
                    auto cit = up_it->find("centroid");
                    if (cit == up_it->end() || !cit->is_array() || cit->size() != 3)
                        throw std::runtime_error("extrude: up_to_vertex selector has no centroid");
                    Eigen::Vector3d vp((*cit)[0].get<double>(), (*cit)[1].get<double>(),
                                       (*cit)[2].get<double>());
                    Eigen::Vector3d pc = face_center(faces[0]);
                    depth = dir.dot(vp - pc);
                    if (!(depth > 1e-9))
                        throw std::runtime_error("extrude: up_to_vertex is behind the profile");
                    mid_plane = false;
                } else if (end == "up_to_surface" || end == "offset_from_surface" ||
                           end == "up_to_reference" || end == "offset_from_reference") {
                    // Up to a reference PLANE or planar surface (+ optional offset). The
                    // surface/plane forms read the `up_to` well; the *_reference forms
                    // read `end_ref`. Both resolve a datum plane or a planar face.
                    const char* slot =
                        (end == "up_to_reference" || end == "offset_from_reference")
                            ? "end_ref" : "up_to";
                    if (!feat.contains(slot))
                        throw std::runtime_error(std::string("extrude: ") + end +
                            " needs an `" + slot + "` plane/surface reference");
                    trim_plane = plane_of(feat.at(slot), slot);
                    trim_to_plane = true;
                    if (end == "offset_from_surface")
                        end_offset_val = feat.contains("offset")
                            ? num_or_expr(feat.at("offset"), env) : 0.0;
                    else if (end == "offset_from_reference")
                        end_offset_val = feat.contains("end_offset")
                            ? num_or_expr(feat.at("end_offset"), env) : 0.0;
                    // Orient `dir` toward the plane, then overshoot far enough to fully
                    // cross it; the post-build half-space trim conforms the end cap to
                    // the plane (handles parallel AND angled references).
                    Eigen::Vector3d pc = face_center(faces[0]);
                    double along = dir.dot(trim_plane.origin - pc);
                    if (along < 0) { dir = -dir; along = -along; }
                    depth = along + bbox_diag_of_face(faces[0]) * 2.0 +
                            std::abs(end_offset_val) + 10.0;
                    mid_plane = false;
                } else {
                    depth = num_or_expr(feat.at("depth"), env);
                    if (!(depth > 0.0))
                        throw std::runtime_error(
                            "extrude depth must be > 0. Depth is a length, not a direction. "
                            "To extrude in the opposite direction, set flip to true.");
                }

                // --- second direction (end2 != "none"): a blind slab of depth2 along the
                // NEGATED direction, unioned into the tool (extrude.py Dir2, blind subset).
                const std::string end2 = feat.value("end2", std::string("none"));
                double depth2 = 0.0;
                if (end2 != "none") {
                    if (end2 == "blind") {
                        depth2 = feat.contains("depth2")
                            ? num_or_expr(feat.at("depth2"), env) : 10.0;
                    } else if (end2 == "through_all") {
                        depth2 = !tgt_id.empty()
                            ? chudware::doc::bbox_diag(require_solid(results, tgt_id, "target")) * 2.2
                            : 1.05 * (bbox_diag_of_face(faces[0]) * 2.0);
                    } else {
                        throw std::runtime_error(
                            "extrude: end2 '" + end2 + "' not supported. end2 must be "
                            "\"none\", \"blind\", or \"through_all\"");
                    }
                    if (!(depth2 > 0.0))
                        throw std::runtime_error("extrude: effective depth2 must be > 0");
                }

                // Extrude each region face; fuse multiple regions into one solid. Draft
                // routes through the tapered loft; otherwise the straight prism. Dir2 adds
                // a second straight slab along -dir.
                TopoDS_Shape solid;
                for (const TopoDS_Face& f : faces) {
                    TopoDS_Shape prism = (std::abs(taper_deg) > 1e-9)
                        ? ::geom::extrude_face_tapered(f, dir, depth, taper_deg, mid_plane)
                        : ::geom::extrude_face(f, dir, depth, mid_plane);
                    if (prism.IsNull())
                        throw std::runtime_error("extrude produced an empty solid");
                    if (end2 != "none") {
                        TopoDS_Shape slab2 = ::geom::extrude_face(f, -dir, depth2, false);
                        if (slab2.IsNull())
                            throw std::runtime_error("extrude: dir2 slab build failed");
                        // build123d's piece + piece2 runs .clean() (UnifySameDomain), so the
                        // sketch-plane seam between the two direction slabs collapses to one face.
                        prism = ::geom::fuse_unify(prism, slab2);
                        if (prism.IsNull())
                            throw std::runtime_error("extrude: dir2 fuse failed");
                    }
                    solid = solid.IsNull() ? prism : ::geom::fuse_unify(solid, prism);
                    if (solid.IsNull())
                        throw std::runtime_error("extrude region fuse failed");
                }
                if (solid.IsNull())
                    throw std::runtime_error("extrude produced no solid");

                // --- up-to-plane trim: cut away the overshoot beyond the reference
                // plane (+ offset) so the end cap lands exactly on the plane. The
                // half-space box's near face IS the cut plane; it fills the beyond-plane
                // (overshoot) side, so subtracting it keeps profile -> plane.
                if (trim_to_plane) {
                    Eigen::Vector3d pc = face_center(faces[0]);
                    Eigen::Vector3d n = trim_plane.normal.normalized();
                    Eigen::Vector3d q = trim_plane.origin;
                    // removalN points from the plane toward the overshoot (away from the
                    // profile); a positive offset extends material that far past the plane.
                    Eigen::Vector3d removalN = ((pc - q).dot(n) >= 0) ? -n : n;
                    Eigen::Vector3d cutPoint = q + removalN * end_offset_val;
                    double reach = chudware::doc::bbox_diag(solid) * 2.0 + 50.0;
                    chudware::geom::SketchPlane cut =
                        chudware::geom::SketchPlane::from(cutPoint, removalN);
                    Eigen::Vector3d corner =
                        cut.origin - cut.uAxis * reach - cut.vAxis * reach;
                    gp_Ax2 box_frame(gp_Pnt(corner.x(), corner.y(), corner.z()),
                                     gp_Dir(cut.normal.x(), cut.normal.y(), cut.normal.z()),
                                     gp_Dir(cut.uAxis.x(), cut.uAxis.y(), cut.uAxis.z()));
                    TopoDS_Shape hs =
                        BRepPrimAPI_MakeBox(box_frame, 2 * reach, 2 * reach, 2 * reach).Shape();
                    if (hs.IsNull())
                        throw std::runtime_error("extrude up-to: failed to build the trim half-space");
                    TopoDS_Shape trimmed = ::geom::cut(solid, hs);
                    if (trimmed.IsNull())
                        throw std::runtime_error("extrude up-to: trim to the reference plane failed");
                    GProp_GProps gtrim; BRepGProp::VolumeProperties(trimmed, gtrim);
                    if (!(gtrim.Mass() > 1e-9))
                        throw std::runtime_error(
                            "extrude up-to: the reference plane/surface does not intersect the extrusion");
                    TopoDS_Shape unified = ::geom::unify_same_domain(trimmed);
                    solid = unified.IsNull() ? trimmed : unified;
                }

                if (is_cut) {
                    if (tgt_id.empty())
                        throw std::runtime_error("extrude cut requires an existing body");
                    const TopoDS_Shape& tgt = require_solid(results, tgt_id, "target");
                    TopoDS_Shape out = ::geom::cut_unify(tgt, solid);
                    if (out.IsNull())
                        throw std::runtime_error("extrude cut produced an empty shape");
                    GProp_GProps gb, ga;
                    BRepGProp::VolumeProperties(tgt, gb);
                    BRepGProp::VolumeProperties(out, ga);
                    const double base_mass = gb.Mass();
                    double cut_mass = ga.Mass();
                    // BLIND-CUT DIRECTION FALLBACK (Bug J5): a blind cut whose sketch sits
                    // ON a body face extrudes along the face's OUTWARD normal by default,
                    // so the prism leaves the body and removes nothing. When the direction
                    // was NOT given explicitly and this is a plain one-sided blind cut (no
                    // taper / dir2 / up-to trim / thin / mid_plane), re-build the prism the
                    // OTHER way (into the material) and re-cut. through_all already spans
                    // both ways (mid_plane), so it is unaffected. Only flips when the first
                    // try removed nothing AND the flip removes something — never weakens a
                    // legitimately-directed cut.
                    // Also skip when the user set Reverse direction (`flip`): that is an
                    // explicit side choice and must not be silently undone by this heuristic.
                    const bool simple_blind = !mid_plane && !trim_to_plane &&
                        end2 == "none" && std::abs(taper_deg) <= 1e-9 && !thin;
                    if (std::abs(cut_mass - base_mass) < 1e-9 &&
                        !dir_was_explicit && !flip && simple_blind) {
                        TopoDS_Shape flipped;
                        for (const TopoDS_Face& f : faces) {
                            TopoDS_Shape pr = ::geom::extrude_face(f, -dir, depth, false);
                            if (pr.IsNull()) { flipped.Nullify(); break; }
                            flipped = flipped.IsNull() ? pr : ::geom::fuse_unify(flipped, pr);
                            if (flipped.IsNull()) break;
                        }
                        if (!flipped.IsNull()) {
                            TopoDS_Shape out2 = ::geom::cut_unify(tgt, flipped);
                            if (!out2.IsNull()) {
                                GProp_GProps ga2;
                                BRepGProp::VolumeProperties(out2, ga2);
                                if (std::abs(ga2.Mass() - base_mass) >= 1e-9) {
                                    out = out2;
                                    cut_mass = ga2.Mass();
                                    solid = flipped;  // the patternable tool follows the flip
                                }
                            }
                        }
                    }
                    if (std::abs(cut_mass - base_mass) < 1e-9)
                        throw std::runtime_error("extrude cut removed no material");
                    require_cut_removed_material("extrude cut", base_mass, cut_mass);
                    consumed.insert(tgt_id);
                    source_of[id] = tgt_id;  // owner-map: cut derives from the target body
                    results[id] = SolidResult{out};
                    // Patternable tool: the cut PRISM at its instance-0 pose, op "cut" —
                    // so a later pattern/mirror re-subtracts it per instance (N holes).
                    tool_body_of[id] = solid;
                    feature_op_of[id] = "cut";
                    record_tool(id, "cut", solid);
                } else {
                    const BodyTargets targets = solid_targets(solid, mode);
                    const std::string mtgt = targets.ids.empty() ? std::string() : targets.ids.front();
                    if (!mtgt.empty()) {
                        const TopoDS_Shape& tgt = targets.shape;
                        TopoDS_Shape out = ::geom::fuse_unify(tgt, solid);
                        if (out.IsNull())
                            throw std::runtime_error("extrude boss fuse produced an empty shape");
                        // Zero-volume boss guard. The OBSERVATION is only "the fused solid
                        // added nothing, so it lies entirely within the existing body"; the
                        // CAUSE has several candidates, and this message used to assert one
                        // of them (the centred-primitive mid-plane story) as fact. Live run
                        // run-42508173312214 ("lexus_f_keychain"): the sketch was correctly
                        // on the top FACE, the real cause was a profile region an earlier
                        // extrude had already consumed, and the fabricated plane.top
                        // diagnosis burned three consecutive calls editing a plane that was
                        // never wrong before the run died on the worker timeout. So: name
                        // the earlier consumer when there is one (decisive, cheap, and
                        // otherwise invisible to the model), else report what was observed
                        // and LIST the causes — keeping the mid-plane hint, which is still
                        // the right diagnosis for the centred-primitive case it was written
                        // for (see session.cpp's plane.top guidance).
                        GProp_GProps gbb, gab;
                        BRepGProp::VolumeProperties(tgt, gbb);
                        BRepGProp::VolumeProperties(out, gab);
                        if (gab.Mass() <= gbb.Mass() + 1e-9) {
                            // FIRST earlier feature that consumed this same sketch as its
                            // profile — the one that placed the material already there.
                            std::string prior;
                            for (std::size_t k = 0; k < _fi && prior.empty(); ++k) {
                                const json pf = normalize_feature(features[k]);
                                if (features[k].value("suppressed", false) ||
                                    pf.value("suppressed", false)) continue;
                                const std::string pt = pf.value("type", std::string());
                                if (pt != "extrude" && pt != "revolve" && pt != "sweep")
                                    continue;
                                auto ppit = pf.find("profile");
                                if (ppit == pf.end()) continue;
                                if (profile_sketch_id(*ppit) != prof) continue;
                                prior = pf.value("id", std::string("?"));
                                const std::string pn = pf.value("name", std::string());
                                if (!pn.empty()) prior += " (\"" + pn + "\")";
                            }
                            std::string msg =
                                "extrude boss added no material — the new solid lies entirely "
                                "within the existing body, so fusing it changed nothing. ";
                            if (!prior.empty())
                                msg += "Profile sketch \"" + prof + "\" was ALREADY extruded "
                                       "by " + prior + ", which consumed that material "
                                       "region; re-extruding the same region cannot add "
                                       "anything. Use a profile that does not overlap it, or "
                                       "edit " + prior + " instead of adding a feature.";
                            else
                                msg += "Plausible causes: (a) an earlier feature already "
                                       "extruded this profile region; (b) the boss grows INTO "
                                       "the body rather than away from it — flip params.flip "
                                       "or set refs/params dir; (c) the sketch sits on a "
                                       "MID-plane instead of the surface: primitives are "
                                       "centred on the origin, so plane.top is a centred "
                                       "block's mid-plane — sketch on the actual face, e.g. "
                                       "refs.plane {\"of\":\"face\",\"side\":\"top\"}, not "
                                       "plane.top. Check which one applies before editing.";
                            throw std::runtime_error(msg);
                        }
                        consumed.insert(targets.ids.begin(), targets.ids.end());
                        source_of[id] = mtgt;  // owner-map: merged boss derives from the body
                        results[id] = SolidResult{out};
                    } else {
                        // A standalone NEW body must be a real, positive-volume solid. A
                        // self-intersecting / zero-area profile (a "bowtie" / figure-8 loop)
                        // extrudes to a degenerate ZERO-volume solid that OCCT still hands
                        // back as a non-null shape with faces — so it slips past the
                        // null/empty backstop and commits a body that reads vol=0 / mass=0
                        // yet shows up in the viewport. Reject it loudly (parity with the
                        // loft/revolve/sweep zero-volume guards). (cut and boss-merge are
                        // already covered by their "removed/added no material" checks.)
                        GProp_GProps gnew;
                        BRepGProp::VolumeProperties(solid, gnew);
                        if (!(std::abs(gnew.Mass()) > 1e-7))
                            throw std::runtime_error(
                                "extrude produced a zero-volume solid — the profile is "
                                "self-intersecting or encloses no area (a 'bowtie' / "
                                "figure-8 loop). Fix the sketch so the profile is a simple, "
                                "non-crossing closed region.");
                        results[id] = SolidResult{solid};
                    }
                    // Patternable tool: the boss PRISM (instance-0 pose), op "add" — a
                    // later pattern/mirror replicates just this prism, NOT the whole
                    // running body (matches the oracle's tool_body for a merged boss).
                    tool_body_of[id] = solid;
                    feature_op_of[id] = "add";
                    record_tool(id, "add", solid);
                }
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "revolve") {
                auto rpit = feat.find("profile");
                if (rpit == feat.end())
                    throw std::runtime_error("missing string ref 'profile'");
                const std::string prof = profile_sketch_id(*rpit);
                if (prof.empty())
                    throw std::runtime_error("missing string ref 'profile'");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                // Axis: either pre-translated reserved-datum vecs (axis_pt/axis_dir)
                // or a datum-axis FEATURE ref left in feat["axis"] (resolve here so a
                // revolve can spin about an authored `axis` datum — the datum's gate).
                Eigen::Vector3d axis_pt, axis_dir;
                if (feat.contains("axis_pt") && feat.contains("axis_dir")) {
                    axis_pt = req_vec3(feat, "axis_pt");
                    axis_dir = req_vec3(feat, "axis_dir");
                } else if (feat.contains("axis")) {
                    auto ax = resolve_axis_ref(feat.at("axis"));
                    if (!ax) throw std::runtime_error("revolve: axis is not a datum axis");
                    axis_pt = ax->first;
                    axis_dir = ax->second;
                } else {
                    throw std::runtime_error("revolve: missing axis");
                }
                // Canonical revolve angle is in DEGREES (SolidWorks/Python); the
                // kernel revolve_face takes radians. (No existing test drives the
                // revolve branch, so aligning to the canonical degree contract is safe.)
                const double angle_deg =
                    feat.contains("angle") ? num_or_expr(feat.at("angle"), env) : 360.0;
                const double angle = radians(angle_deg);
                if (!(std::abs(angle) > 0.0))
                    throw std::runtime_error("revolve angle must be non-zero");

                // Profile faces: region-resolved sketch faces, or — if the model left the
                // profile OPEN (a common weak-model mistake) — a region synthesised by
                // closing the outer curve back to the axis. The fallback only runs when
                // there is no closed region, so a properly closed profile is untouched.
                // resolve_profile_faces throws on empty; catch open-profile case by probing
                // src.faces first for the auto-close path.
                std::vector<TopoDS_Face> rev_faces;
                if (src.faces.empty()) {
                    rev_faces = {};  // will auto-close below
                } else {
                    rev_faces = resolve_profile_faces(results, feat);
                }
                // Track whether we had to synthesise the region by closing an OPEN profile
                // to the axis, so we can surface it as a warning (Bug B4): the auto-close is
                // useful, but a model that left the profile open should be told its disc was
                // closed for it (it may have intended a thin wall / a different shape).
                const bool auto_closed = rev_faces.empty();
                if (rev_faces.empty())
                    rev_faces = close_profile_to_axis(src.entities, src.plane, axis_pt, axis_dir);
                if (rev_faces.empty())
                    throw std::runtime_error(
                        "revolve: profile has no closed region (close the profile back to the "
                        "axis with a line)");
                if (auto_closed)
                    st.warning = "revolve: profile was open; auto-closed to the axis";

                // Build the revolve tool. Single region: one MakeRevol (no boolean). Multi-
                // region: revolve each face then fuse_many in ONE BOPDS pass (not pairwise
                // O(N²) fuse fold — same pattern as hole/bolt_circle tool unions).
                // Normalize axis once for thin offset + revolve_face (revolve_face also
                // normalizes, but we need the unit for the thin radial shift).
                const Eigen::Vector3d axis_dir_u = axis_dir.normalized();
                std::vector<TopoDS_Shape> revs;
                revs.reserve(rev_faces.size());
                for (const TopoDS_Face& f : rev_faces) {
                    TopoDS_Shape rev = ::geom::revolve_face(f, axis_pt, axis_dir_u, angle);
                    if (rev.IsNull())
                        throw std::runtime_error("revolve produced an empty solid");
                    revs.push_back(std::move(rev));
                }
                TopoDS_Shape tool;
                if (revs.size() == 1) {
                    tool = std::move(revs.front());
                } else {
                    tool = ::geom::fuse_many(
                        revs.front(),
                        std::vector<TopoDS_Shape>(revs.begin() + 1, revs.end()));
                    if (tool.IsNull())
                        throw std::runtime_error("revolve region fuse failed");
                }
                if (tool.IsNull())
                    throw std::runtime_error("revolve produced no solid");
                // Guard the silent-degenerate case: a profile that straddles / lies on the
                // axis, or a ~0deg angle, yields a null/sliver solid that OCCT still hands
                // back as a valid (but ~0-volume) shape. Fail loudly instead of reporting a
                // successful body with no material (Bug N-9 class).
                {
                    GProp_GProps rv;
                    BRepGProp::VolumeProperties(tool, rv);
                    if (!(rv.Mass() > 1e-9))
                        throw std::runtime_error(
                            "revolve produced a zero-volume solid — the profile must sit to "
                            "ONE side of the axis (not cross or touch it) and the angle must "
                            "be > 0 (got " + std::to_string(angle * 180.0 / M_PI) + " deg)");
                }

                // --- thin feature (revolve.py): subtract a revolve of each profile face
                // offset toward the axis by thin_thickness, leaving a thin wall. side
                // inside (default) | outside | both (1/2 each side, halved here).
                if (feat.value("thin", false)) {
                    const double thin_thk = feat.contains("thin_thickness")
                        ? num_or_expr(feat.at("thin_thickness"), env) : 0.0;
                    if (!(thin_thk > 0.0))
                        throw std::runtime_error("revolve: thin requires a positive thin_thickness");
                    const std::string side = feat.value("thin_side", std::string("inside"));
                    const double per_t = (side == "both") ? thin_thk / 2.0 : thin_thk;
                    std::vector<TopoDS_Shape> inrevs;
                    inrevs.reserve(rev_faces.size());
                    for (const TopoDS_Face& f : rev_faces) {
                        // Radial direction = (face centroid - axis) projected perpendicular
                        // to the axis; shift the face TOWARD the axis by per_t (matches
                        // _offset_face_toward_axis, which uses -thickness for every side).
                        GProp_GProps g; BRepGProp::SurfaceProperties(f, g);
                        gp_Pnt cm = g.CentreOfMass();
                        Eigen::Vector3d c(cm.X(), cm.Y(), cm.Z());
                        Eigen::Vector3d diff = c - axis_pt;
                        Eigen::Vector3d radial = diff - axis_dir_u * diff.dot(axis_dir_u);
                        if (!(radial.norm() > 1e-9)) continue;
                        radial.normalize();
                        Eigen::Vector3d shift = radial * (-per_t);
                        gp_Trsf tr; tr.SetTranslation(gp_Vec(shift.x(), shift.y(), shift.z()));
                        TopoDS_Face inf = TopoDS::Face(
                            BRepBuilderAPI_Transform(f, tr, true).Shape());
                        TopoDS_Shape inrev = ::geom::revolve_face(inf, axis_pt, axis_dir_u, angle);
                        if (inrev.IsNull()) continue;
                        inrevs.push_back(std::move(inrev));
                    }
                    TopoDS_Shape inner;
                    if (inrevs.size() == 1) {
                        inner = std::move(inrevs.front());
                    } else if (inrevs.size() > 1) {
                        inner = ::geom::fuse_many(
                            inrevs.front(),
                            std::vector<TopoDS_Shape>(inrevs.begin() + 1, inrevs.end()));
                    }
                    if (!inner.IsNull()) {
                        TopoDS_Shape hollow = ::geom::cut(tool, inner);
                        if (!hollow.IsNull()) {
                            GProp_GProps gh, gt;
                            BRepGProp::VolumeProperties(hollow, gh);
                            BRepGProp::VolumeProperties(tool, gt);
                            // Accept only if material was actually removed (a wall remains).
                            if (gh.Mass() > 1e-9 && gh.Mass() < gt.Mass() - 1e-9)
                                tool = hollow;
                        }
                    }
                }

                const std::string rmode = solid_mode(feat, "boss");
                const BodyTargets targets = solid_targets(tool, rmode);
                const std::string rtgt = targets.ids.empty() ? std::string() : targets.ids.front();

                if (rtgt.empty()) {
                    if (rmode == "cut")
                        throw std::runtime_error(
                            "revolve: cut mode requires an existing body to subtract from");
                    results[id] = SolidResult{tool};
                } else {
                    const TopoDS_Shape& target = targets.shape;
                    TopoDS_Shape out = (rmode == "cut")
                        ? ::geom::cut_unify(target, tool)
                        : ::geom::fuse_unify(target, tool);
                    if (out.IsNull())
                        throw std::runtime_error(
                            std::string("revolve boolean ") + rmode + " produced an empty shape");
                    if (rmode == "cut") {
                        GProp_GProps gb, ga;
                        BRepGProp::VolumeProperties(target, gb);
                        BRepGProp::VolumeProperties(out, ga);
                        if (std::abs(ga.Mass() - gb.Mass()) < 1e-9)
                            throw std::runtime_error("revolve cut removed no material");
                        require_cut_removed_material("revolve cut", gb.Mass(), ga.Mass());
                    }
                    consumed.insert(targets.ids.begin(), targets.ids.end());
                    source_of[id] = rtgt;  // owner-map: result derives from the target body
                    results[id] = SolidResult{out};
                }
                // Patternable tool: the revolved solid (instance-0 pose); op follows mode.
                tool_body_of[id] = tool;
                feature_op_of[id] = (rmode == "cut") ? "cut" : "add";
                record_tool(id, (rmode == "cut") ? "cut" : "add", tool);
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "sweep") {
                // Sweep profile region face(s) along a path sketch's raw wire
                // (BRepOffsetAPI_MakePipe). Port of engine/doc/features/sweep.py's
                // MakePipe fast path (guides/twist/orientation/thin deferred).
                auto spit = feat.find("profile");
                if (spit == feat.end())
                    throw std::runtime_error("missing string ref 'profile'");
                std::vector<TopoDS_Face> sw_faces = resolve_profile_faces(results, feat);
                if (sw_faces.empty())
                    throw std::runtime_error("sweep: profile sketch has no closed face");

                auto pit = feat.find("path");
                if (pit == feat.end())
                    throw std::runtime_error("sweep: a 'path' ref is required");
                const std::string path_id = selector_owner_id(*pit);
                if (path_id.empty())
                    throw std::runtime_error("sweep: malformed path ref");
                // The path may be a SKETCH (raw wire) OR a CURVE feature (a helix /
                // spiral, lifted from its local frame through the curve's base plane).
                TopoDS_Wire spine;
                auto path_it = results.find(path_id);
                if (path_it != results.end() &&
                    std::holds_alternative<CurveResult>(*path_it->second)) {
                    const CurveResult& cr = std::get<CurveResult>(*path_it->second);
                    // Lift local (x,y,z) -> origin + x*u + y*v + z*n (the base frame).
                    const chudware::geom::SketchPlane& b = cr.base;
                    gp_Trsf lift;
                    lift.SetValues(
                        b.uAxis.x(), b.vAxis.x(), b.normal.x(), b.origin.x(),
                        b.uAxis.y(), b.vAxis.y(), b.normal.y(), b.origin.y(),
                        b.uAxis.z(), b.vAxis.z(), b.normal.z(), b.origin.z());
                    spine = TopoDS::Wire(BRepBuilderAPI_Transform(cr.wire, lift, true).Shape());
                } else if (path_it != results.end() &&
                           std::holds_alternative<Sketch3DResult>(*path_it->second)) {
                    // A 3D free-space sketch: its wires are already in world
                    // coordinates — no lift. Wire choice per sketch3d_ref_wire.
                    spine = sketch3d_ref_wire(std::get<Sketch3DResult>(*path_it->second),
                                              *pit, "path");
                } else {
                    const SketchResult& pathsk = require_sketch(results, path_id, "path");
                    spine = chudware::geom::wire_from_solved_sketch(pathsk.entities, pathsk.plane);
                }
                if (spine.IsNull())
                    throw std::runtime_error(
                        "sweep: path sketch produced no wire — the path edges do not "
                        "form a single connected chain. Check that consecutive entities "
                        "share endpoints (line end == next arc start, etc.); a mixed "
                        "line+arc path now stitches across small float gaps, so a failure "
                        "here means a real break or a stray/closed entity in the path.");

                // Profile orientation law along the path (see geom::sweep_profile).
                int orient = 0;  // follow_path (CorrectedFrenet) — SolidWorks default
                if (feat.contains("orientation") && feat.at("orientation").is_string()) {
                    const std::string o = feat.at("orientation").get<std::string>();
                    if (o == "frenet") orient = 1;
                    else if (o == "keep_normal") orient = 2;
                }

                // Optional guide curve(s): each resolves to a wire the SAME way the
                // path does — a whole sketch's raw wire, or a curve/helix datum lifted
                // from its local frame through its base plane. A guide is an auxiliary
                // spine that steers the profile's orientation + scaling as it sweeps
                // (SolidWorks "Guide Curves"). MakePipeShell drives ONE auxiliary
                // spine, so a single guide is supported; more than one is rejected
                // loudly rather than silently dropped.
                std::vector<TopoDS_Wire> guides;
                if (auto git = feat.find("guides");
                    git != feat.end() && git->is_array() && !git->empty()) {
                    if (git->size() > 1)
                        throw std::runtime_error(
                            "sweep: only a single guide curve is supported — remove the "
                            "extra guide(s) (multi-guide sweeps are not implemented yet)");
                    const nlohmann::json& gsel = git->front();
                    const std::string gid = selector_owner_id(gsel);
                    if (gid.empty())
                        throw std::runtime_error("sweep: malformed guide-curve ref");
                    if (gid == path_id)
                        throw std::runtime_error(
                            "sweep: the guide curve must be a different sketch/curve "
                            "than the path");
                    TopoDS_Wire gw;
                    auto git2 = results.find(gid);
                    if (git2 != results.end() &&
                        std::holds_alternative<CurveResult>(*git2->second)) {
                        const CurveResult& cr = std::get<CurveResult>(*git2->second);
                        const chudware::geom::SketchPlane& b = cr.base;
                        gp_Trsf lift;
                        lift.SetValues(
                            b.uAxis.x(), b.vAxis.x(), b.normal.x(), b.origin.x(),
                            b.uAxis.y(), b.vAxis.y(), b.normal.y(), b.origin.y(),
                            b.uAxis.z(), b.vAxis.z(), b.normal.z(), b.origin.z());
                        gw = TopoDS::Wire(BRepBuilderAPI_Transform(cr.wire, lift, true).Shape());
                    } else if (git2 != results.end() &&
                               std::holds_alternative<Sketch3DResult>(*git2->second)) {
                        gw = sketch3d_ref_wire(std::get<Sketch3DResult>(*git2->second),
                                               gsel, "guide");
                    } else {
                        const SketchResult& gsk = require_sketch(results, gid, "guide");
                        gw = chudware::geom::wire_from_solved_sketch(gsk.entities, gsk.plane);
                    }
                    if (gw.IsNull())
                        throw std::runtime_error(
                            "sweep: guide-curve sketch produced no wire — its edges must "
                            "form a single connected chain (check consecutive entities "
                            "share endpoints)");
                    guides.push_back(gw);
                }

                TopoDS_Shape solid;
                for (const TopoDS_Face& f : sw_faces) {
                    TopoDS_Shape pipe = ::geom::sweep_profile(f, spine, orient, guides);
                    if (pipe.IsNull())
                        throw std::runtime_error("sweep produced an empty solid "
                            "(check the profile sits on/near the path start, roughly "
                            "perpendicular to it)");
                    solid = solid.IsNull() ? pipe : ::geom::fuse_unify(solid, pipe);
                    if (solid.IsNull())
                        throw std::runtime_error("sweep region fuse failed");
                }
                if (solid.IsNull())
                    throw std::runtime_error("sweep produced no solid");
                // Guard the silent-degenerate case: when the profile plane is not aligned
                // with the START of the path (the profile sits off the path's first point),
                // MakePipe sweeps a collapsed/near-zero section and OCCT still returns a
                // valid-but-empty solid. Fail loudly rather than report a successful body
                // with ~0 volume (Bug N-9).
                double sweep_volume = 0.0;
                {
                    GProp_GProps sv;
                    BRepGProp::VolumeProperties(solid, sv);
                    sweep_volume = sv.Mass();
                    if (!(sweep_volume > 1e-9)) {
                        // ACCURATE DIAGNOSTIC (Bug H6): the dominant cause of a zero-volume
                        // sweep is the profile not sitting on the path START — e.g. a profile
                        // on plane.front but a path on plane.right whose start coordinate does
                        // not coincide with the profile centroid. Measure the actual gap and,
                        // when it is significant, name the exact distance so the user fixes the
                        // right coordinate instead of guessing.
                        GProp_GProps pc; BRepGProp::SurfaceProperties(sw_faces.front(), pc);
                        gp_Pnt pcm = pc.CentreOfMass();
                        BRepAdaptor_Curve spc(TopoDS::Edge(
                            TopExp_Explorer(spine, TopAbs_EDGE).Current()));
                        gp_Pnt p0 = spc.Value(spc.FirstParameter());
                        const double gap = pcm.Distance(p0);
                        if (gap > 1e-6) {
                            char buf[256];
                            std::snprintf(buf, sizeof(buf),
                                "sweep produced a zero-volume solid — the profile centre is %.4g mm "
                                "from the path START (%.4g, %.4g, %.4g). Move the path's first point "
                                "to the profile centre, or the profile onto the path start (a "
                                "cross-plane sweep needs the profile plane's origin to coincide "
                                "with the path's first point).",
                                gap, p0.X(), p0.Y(), p0.Z());
                            throw std::runtime_error(buf);
                        }
                        throw std::runtime_error(
                            "sweep produced a zero-volume solid — the profile must sit ON the "
                            "START of the path and be roughly perpendicular to it (anchor the "
                            "path's first point at the profile, e.g. start the path at the "
                            "profile's plane origin)");
                    }
                }

                // SELF-INTERSECTION guard (Bug B3): when the profile is larger than the
                // path's bend radius, MakePipe folds the tube through itself and OCCT hands
                // back a CORRUPT B-rep that still reports ok=true with no geometry-check
                // issues. Detect it two ways and combine them so a legitimate curved sweep
                // (whose swept BSpline skin can trip BRepCheck on a harmless tolerance nit)
                // is NOT wrongly rejected:
                //   (1) BRepCheck_Analyzer validity — invalid is the strong signal.
                //   (2) a volume sanity estimate: a straight drag of the profile along the
                //       path has volume ~ profile_area * path_length; a self-overlap loses a
                //       large fraction of that. A real curved sweep stays close to it.
                // REJECT only when BOTH agree (invalid AND grossly under-volume) — that is
                // the self-intersecting case. If only one fires, surface a WARNING rather
                // than fail, so a fussy-but-valid coil still compiles.
                const bool brep_invalid = !BRepCheck_Analyzer(solid).IsValid();
                double vol_estimate = 0.0;
                {
                    GProp_GProps pa;
                    double profile_area = 0.0;
                    for (const TopoDS_Face& f : sw_faces) {
                        BRepGProp::SurfaceProperties(f, pa);
                        profile_area += pa.Mass();
                    }
                    GProp_GProps pl;
                    BRepGProp::LinearProperties(spine, pl);
                    vol_estimate = profile_area * pl.Mass();
                }
                // A guide curve legitimately scales the cross-section along the path,
                // so the profile_area * path_length estimate no longer bounds the
                // volume — skip this heuristic for guided sweeps to avoid false
                // rejects (the zero-volume guard above still catches an empty result).
                const bool gross_under_volume =
                    guides.empty() &&
                    vol_estimate > 1e-9 && sweep_volume < 0.5 * vol_estimate;
                if (brep_invalid && gross_under_volume)
                    throw std::runtime_error(
                        "sweep: result is self-intersecting/invalid (profile too large for "
                        "the path bend radius) — shrink the profile or ease the path's "
                        "curvature");
                if (brep_invalid)
                    st.warning =
                        "sweep: the result fails a B-rep validity check (the swept surface "
                        "may self-intersect — profile large for the path's bend radius); "
                        "verify the geometry";
                else if (gross_under_volume)
                    st.warning =
                        "sweep: result volume is well below profile_area * path_length — "
                        "the pipe may self-intersect (profile large for the path's bend "
                        "radius); verify the geometry";

                const std::string smode = solid_mode(feat, "boss");
                const BodyTargets targets = solid_targets(solid, smode);
                const std::string stgt = targets.ids.empty() ? std::string() : targets.ids.front();
                if (!stgt.empty()) {
                    const TopoDS_Shape& tgt = targets.shape;
                    TopoDS_Shape out = (smode == "cut")
                        ? ::geom::cut_unify(tgt, solid)
                        : ::geom::fuse_unify(tgt, solid);
                    if (out.IsNull())
                        throw std::runtime_error(
                            std::string("sweep ") + smode + " produced an empty shape");
                    if (smode == "cut") {
                        GProp_GProps gsb, gsa;
                        BRepGProp::VolumeProperties(tgt, gsb);
                        BRepGProp::VolumeProperties(out, gsa);
                        if (std::abs(gsa.Mass() - gsb.Mass()) < 1e-9)
                            throw std::runtime_error("sweep cut removed no material");
                        require_cut_removed_material("sweep cut", gsb.Mass(), gsa.Mass());
                    }
                    consumed.insert(targets.ids.begin(), targets.ids.end());
                    source_of[id] = stgt;  // owner-map: result derives from the body
                    results[id] = SolidResult{out};
                } else {
                    if (smode == "cut")
                        throw std::runtime_error("sweep: cut mode requires an existing body");
                    results[id] = SolidResult{solid};
                }
                // Patternable tool: the swept solid (instance-0 pose), op follows the
                // mode — so a later pattern/mirror replicates JUST the swept tool, and a
                // loft/sweep boss can be a pattern seed (Bug J3).
                tool_body_of[id] = solid;
                feature_op_of[id] = (smode == "cut") ? "cut" : "add";
                record_tool(id, (smode == "cut") ? "cut" : "add", solid);
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "rib") {
                // Thin support wall grown from an OPEN sketch contour: silhouette
                // dropped in-plane toward the body, thickened across the sketch
                // normal, trimmed to the body bbox, fused. Port of the parallel/
                // centered path of engine/doc/features/rib.py.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("rib: no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double thickness =
                    feat.contains("thickness") ? num_or_expr(feat.at("thickness"), env) : 3.0;
                if (!(thickness > 0.0))
                    throw std::runtime_error("rib thickness must be > 0");

                // Rib uses the OPEN contour of the profile sketch (not closed regions).
                auto rib_pit = feat.find("profile");
                if (rib_pit == feat.end())
                    throw std::runtime_error("missing string ref 'profile'");
                const std::string prof = profile_sketch_id(*rib_pit);
                if (prof.empty())
                    throw std::runtime_error("missing string ref 'profile'");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                TopoDS_Wire contour =
                    chudware::geom::wire_from_solved_sketch(src.entities, src.plane);
                if (contour.IsNull())
                    throw std::runtime_error("rib: profile sketch produced no contour");

                // Body bbox -> center (for the inward drop) + a clip box (+5 margin).
                Bnd_Box bb;
                BRepBndLib::Add(body, bb);
                double xmn, ymn, zmn, xmx, ymx, zmx;
                bb.Get(xmn, ymn, zmn, xmx, ymx, zmx);
                Eigen::Vector3d bc((xmn + xmx) / 2, (ymn + ymx) / 2, (zmn + zmx) / 2);

                TopoDS_Vertex v1, v2;
                TopExp::Vertices(contour, v1, v2);
                if (v1.IsNull() || v2.IsNull())
                    throw std::runtime_error("rib: contour is closed (a rib needs an open contour)");
                gp_Pnt q1 = BRep_Tool::Pnt(v1), q2 = BRep_Tool::Pnt(v2);
                Eigen::Vector3d mid((q1.X() + q2.X()) / 2, (q1.Y() + q2.Y()) / 2,
                                    (q1.Z() + q2.Z()) / 2);
                Eigen::Vector3d toward = bc - mid;
                const double reach = chudware::doc::bbox_diag(body) * 1.5;

                TopoDS_Shape slab = ::geom::make_rib_slab(contour, src.plane.normal,
                                                          toward, thickness, reach);
                if (slab.IsNull())
                    throw std::runtime_error("rib: could not build the rib slab");

                // Trim the slab to the body bbox (+5) so the drop doesn't dangle.
                TopoDS_Shape clip = ::geom::make_box_at(xmn - 5, ymn - 5, zmn - 5,
                                                        (xmx - xmn) + 10, (ymx - ymn) + 10,
                                                        (zmx - zmn) + 10);
                TopoDS_Shape trimmed = ::geom::common(slab, clip);
                if (trimmed.IsNull()) trimmed = slab;  // keep untrimmed; fuse still works

                GProp_GProps gb, ga;
                BRepGProp::VolumeProperties(body, gb);
                TopoDS_Shape out = ::geom::fuse_unify(body, trimmed);
                if (out.IsNull())
                    throw std::runtime_error("rib: fusing the rib to the body failed");
                BRepGProp::VolumeProperties(out, ga);
                if (ga.Mass() - gb.Mass() <= 1e-6)
                    throw std::runtime_error("rib added no material (contour must bridge the body)");
                record_tool(id, "add", trimmed);

                consumed.insert(body_id);
                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "boolean") {
                const std::string op = req_str(feat, "op");
                const std::string a_id = req_str(feat, "a");
                const std::string b_id = req_str(feat, "b");
                // NO SILENT WRONG GEOMETRY: a boolean operand whose body was already CONSUMED by
                // an earlier merge (a default-mode "boss" primitive fuses into the running body
                // and consumes it) no longer exists as a standalone solid — results[id] now holds
                // the MERGED body, so common/cut would quietly operate on the wrong shape (e.g.
                // common(A, A∪B) = A). Error loudly and tell the caller to keep the operand
                // separate with merge:false, instead of returning a plausible-but-wrong solid.
                for (const std::string& opnd : {a_id, b_id})
                    if (consumed.count(opnd))
                        throw std::runtime_error(
                            "boolean: operand '" + opnd + "' was already merged into another body "
                            "(a default primitive fuses with mode:\"boss\") — build that body with "
                            "\"merge\":false so it stays a separate solid for the boolean");
                const TopoDS_Shape& a = require_solid(results, a_id, "a");
                const TopoDS_Shape& b = require_solid(results, b_id, "b");

                TopoDS_Shape out;
                if (op == "fuse")        out = ::geom::fuse_unify(a, b);  // skip unify if no section edges
                else if (op == "cut")    out = ::geom::cut_unify(a, b);
                else if (op == "common") out = ::geom::common(a, b);
                else throw std::runtime_error(
                    "unknown boolean op '" + op + "'. Boolean ops: fuse, cut, common");
                if (out.IsNull())
                    throw std::runtime_error("boolean '" + op + "' produced an empty shape");

                if (op == "fuse") {
                    // NO SILENT DISJOINT FUSE (issue #8): the BOOLEAN contract in
                    // ops_cheatsheet.md promises a fuse produces ONE connected solid. Two
                    // operands that don't intersect or touch still pass BRepAlgoAPI_Fuse's
                    // IsDone() — OCCT happily returns a >1-solid compound — so mirror the
                    // disjoint check the cut/intersect branch below already applies instead of
                    // silently accepting a multi-body "fuse".
                    int nsolids = 0;
                    for (TopExp_Explorer sx(out, TopAbs_SOLID); sx.More(); sx.Next()) ++nsolids;
                    if (nsolids != 1)
                        throw std::runtime_error(
                            "boolean: fuse produced " + std::to_string(nsolids) +
                            " disjoint solids (operands must intersect or touch)");
                } else {
                    // subtract / intersect: the oracle (boolean.py) calls getattr(result,
                    // "volume", 0.0) <= 1e-9 -> FAIL. build123d's `volume` attribute is
                    // None/0 on a multi-solid ShapeList, so a cut/intersect that yields >1
                    // DISJOINT solid is treated by the oracle as an empty result. Reproduce
                    // exactly: a disjoint subtract/intersect fails (leaving the inputs).
                    int nsolids = 0;
                    for (TopExp_Explorer sx(out, TopAbs_SOLID); sx.More(); sx.Next()) ++nsolids;
                    if (nsolids != 1)
                        throw std::runtime_error(
                            std::string("boolean: ") + (op == "cut" ? "subtract" : "intersect") +
                            " produced an empty body");
                }
                if (op == "fuse") record_tool(id, "add", b);
                else if (op == "cut") record_tool(id, "cut", b);

                // a and b were merged/subtracted — they no longer survive standalone.
                consumed.insert(a_id);
                consumed.insert(b_id);
                source_of[id] = a_id;  // owner-map: result derives from the first target

                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "fillet" || type == "chamfer") {
                // normalize_feature stores params.type in hem_type to preserve the
                // feature's top-level type. An absent subtype is the legacy path.
                const std::string blend_type = feat.value("hem_type", std::string());
                if (type == "fillet" && !blend_type.empty() &&
                    blend_type != "constant" && blend_type != "variable")
                    throw std::runtime_error("fillet type '" + blend_type + "' is not supported");
                if (type == "chamfer" && !blend_type.empty() &&
                    blend_type != "symmetric" && blend_type != "asymmetric" &&
                    blend_type != "distance_angle" && blend_type != "vertex")
                    throw std::runtime_error("chamfer type '" + blend_type + "' is not supported");
                if (type == "fillet" && blend_type == "variable" &&
                    (!feat.contains("radius2") || feat.at("radius2").is_null()))
                    throw std::runtime_error("fillet: radius2 is required for variable type");
                if (type == "chamfer" && blend_type == "asymmetric" &&
                    (!feat.contains("length2") || feat.at("length2").is_null()))
                    throw std::runtime_error("chamfer: length2 is required for asymmetric type");
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                std::optional<Eigen::Vector3d> dir = opt_vec3(feat, "dir");

                // An "edges" key (a single selector object, or a non-empty json array of
                // selectors) routes to the edge-SELECTOR path: each selector is resolved
                // against the body's live B-rep topology (doc/picks::resolve_edge_picks).
                // A dangling selector throws (-> per-feature error status, the never-raise
                // contract). When "edges" is absent the legacy `dir`-filter path runs
                // unchanged.
                auto edges_it = feat.find("edges");
                const bool has_edge_sel =
                    edges_it != feat.end() &&
                    (edges_it->is_object() ||
                     (edges_it->is_array() && !edges_it->empty()));
                if (!blend_type.empty() && blend_type != "vertex" && !has_edge_sel)
                    throw std::runtime_error(type + ": refs.edges required for type '" +
                                             blend_type + "'");
                // Reject a selector that names a non-existent feature (Bug K4) before any
                // resolution — a stale owner/feature ref would otherwise score against the
                // wrong edge and silently mutilate the body (an open shell that reports ok).
                if (has_edge_sel) validate_selector_refs(*edges_it, type.c_str());

                std::optional<std::vector<TopoDS_Edge>> picked;
                int n_dangling = 0;   // picks that couldn't be located on the body (skipped + warned)
                // A semantic geometry QUERY ({"of":"edge",...}) lets the agent name edges
                // it cannot click — "all edges", "the vertical edges", "the top rim". It
                // expands against the LIVE body to concrete unique edges (doc/picks).
                // A LIST may MIX queries and plain selectors — e.g. "chamfer both ends" =
                // [{"of":"edge","side":"+z"},{"of":"edge","side":"-z"}]. Detect any query
                // element and resolve each entry appropriately, unioning (deduped) the edges.
                const bool list_has_query =
                    has_edge_sel && edges_it->is_array() &&
                    std::any_of(edges_it->begin(), edges_it->end(),
                                [](const json& e) { return chudware::doc::is_geom_query(e); });
                if (has_edge_sel && chudware::doc::is_geom_query(*edges_it)) {
                    chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(body, *edges_it);
                    if (!qp.error.empty())
                        throw std::runtime_error(type + ": " + qp.error);
                    if (qp.edges.empty())
                        throw std::runtime_error(type + ": edge query matched no edges");
                    picked = std::move(qp.edges);
                } else if (list_has_query) {
                    std::vector<TopoDS_Edge> all;
                    TopTools_IndexedMapOfShape seen;
                    json plain = json::array();
                    for (const json& el : *edges_it) {
                        if (chudware::doc::is_geom_query(el)) {
                            chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(body, el);
                            if (!qp.error.empty())
                                throw std::runtime_error(type + ": " + qp.error);
                            for (TopoDS_Edge& e : qp.edges)
                                if (seen.Add(e)) all.push_back(e);
                        } else {
                            plain.push_back(el);  // batch the concrete selectors below
                        }
                    }
                    if (!plain.empty()) {
                        chudware::doc::EdgePicks picks = chudware::doc::resolve_edge_picks(body, plain);
                        n_dangling += (int)picks.dangling.size();
                        for (TopoDS_Edge& e : picks.edges)
                            if (seen.Add(e)) all.push_back(e);
                    }
                    if (all.empty())
                        throw std::runtime_error(type + ": edge query/selectors matched no edges");
                    picked = std::move(all);
                } else if (has_edge_sel) {
                    chudware::doc::EdgePicks picks =
                        chudware::doc::resolve_edge_picks(body, *edges_it);
                    n_dangling = (int)picks.dangling.size();
                    // Don't fail the WHOLE feature when one freshly-picked selector is stale (a very
                    // common case while editing a fillet to add edges — the pick is captured against
                    // the live preview, which the round has trimmed). Round the edges that DID
                    // resolve and report the rest as a non-fatal warning. Only a total miss errors.
                    if (picks.edges.empty()) {
                        std::string msg = type + ": none of the selected edges could be located on "
                            "the body";
                        if (n_dangling)
                            msg += " (" + std::to_string(n_dangling) + " selector(s) dangling)";
                        throw std::runtime_error(msg);
                    }
                    picked = std::move(picks.edges);
                }
                std::vector<TopoDS_Vertex> picked_vertices;
                if (type == "chamfer" && blend_type == "vertex") {
                    auto vit = feat.find("vertices");
                    if (vit == feat.end() ||
                        !(vit->is_object() || (vit->is_array() && !vit->empty())))
                        throw std::runtime_error("chamfer: refs.vertices required for type 'vertex'");
                    validate_selector_refs(*vit, "chamfer");
                    TopTools_IndexedMapOfShape vertices;
                    TopExp::MapShapes(body, TopAbs_VERTEX, vertices);
                    std::vector<chudware::doc::TopoInfo> infos;
                    infos.reserve(vertices.Extent());
                    for (int vi = 1; vi <= vertices.Extent(); ++vi) {
                        const gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(vertices(vi)));
                        chudware::doc::TopoInfo info;
                        info.kind = "vertex";
                        info.index = vi - 1;
                        info.hint_index = vi - 1;
                        info.centroid = Eigen::Vector3d(p.X(), p.Y(), p.Z());
                        infos.push_back(std::move(info));
                    }
                    const double diag = chudware::doc::bbox_diag(body);
                    TopTools_IndexedMapOfShape selected_vertices;
                    auto add_vertex = [&](const json& sel) {
                        auto idx = chudware::doc::resolve_with_hint_fallback(sel, infos, diag);
                        if (!idx)
                            throw std::runtime_error("chamfer: vertex selector dangling");
                        selected_vertices.Add(vertices(*idx + 1));
                    };
                    if (vit->is_array()) {
                        for (const json& sel : *vit) add_vertex(sel);
                    } else {
                        add_vertex(*vit);
                    }
                    picked_vertices.reserve(selected_vertices.Extent());
                    for (int vi = 1; vi <= selected_vertices.Extent(); ++vi)
                        picked_vertices.push_back(TopoDS::Vertex(selected_vertices(vi)));
                }

                // EMPTY-QUERY guard (Bug A4): if an edge selector/query was given but
                // resolved to NO edges, fail with a distinct "no edges matched" message
                // FIRST — BEFORE the size-feasibility path below, whose "N mm is too large
                // for the selected edge(s)" message is misleading when in fact nothing was
                // selected. (The query paths above already reject most empty cases; this is
                // the catch-all so an empty resolved set can never surface as "too large".)
                if (picked && picked->empty())
                    throw std::runtime_error(type + ": no edges matched the query");

                TopoDS_Shape out;
                int n_dropped = 0;      // explicit picks that couldn't be blended at this size
                std::string contact_why;  // why a fillet could not join bodies at a shared edge
                double blend_size = 0.0;  // radius / distance, for the warning text
                // Read the requested size first so a SUB-TOLERANCE blend can be detected
                // BEFORE OCCT runs (Bug K6): a positive radius/distance below the modelling
                // tolerance (1e-7) removes no detectable material — OCCT no-ops it or hands
                // back the body unchanged, which used to report a silent success. Warn,
                // pass the body through unchanged, and skip the (pointless) kernel call.
                const char* size_key = type == "fillet" ? "radius" : "distance";
                if (!feat.contains(size_key) || feat.at(size_key).is_null())
                    throw std::runtime_error(type + ": missing required " +
                                             (type == "fillet" ? std::string("radius")
                                                               : std::string("distance/length")));
                blend_size = num_or_expr(feat.at(size_key), env);
                if (!(blend_size > 0.0))
                    throw std::runtime_error(
                        type + (type == "fillet" ? " radius must be > 0" : " distance must be > 0"));
                double chamfer_angle = 0.0;
                std::optional<TopoDS_Face> chamfer_reference_face;
                if (type == "chamfer" && blend_type == "distance_angle") {
                    if (!feat.contains("angle") || feat.at("angle").is_null())
                        throw std::runtime_error("chamfer: angle is required for distance_angle");
                    const double angle_degrees = num_or_expr(feat.at("angle"), env);
                    if (!(angle_degrees > 0.0 && angle_degrees < 90.0))
                        throw std::runtime_error("chamfer: angle must be between 0 and 90 degrees");
                    chamfer_angle = radians(angle_degrees);
                    auto ref_it = feat.find("reference_face");
                    if (ref_it != feat.end() && !ref_it->is_null()) {
                        validate_selector_refs(*ref_it, "reference_face");
                        TopoDS_Face face;
                        if (chudware::doc::is_geom_query(*ref_it)) {
                            chudware::doc::QueryPicks qp =
                                chudware::doc::resolve_geom_query(body, *ref_it);
                            if (!qp.error.empty() || qp.faces.size() != 1)
                                throw std::runtime_error("chamfer: reference_face must resolve to one face");
                            face = qp.faces.front();
                        } else {
                            chudware::doc::FacePicks fp =
                                chudware::doc::resolve_face_picks(body, *ref_it, "reference_face");
                            if (!fp.dangling.empty() || fp.faces.size() != 1)
                                throw std::runtime_error("chamfer: reference_face must resolve to one face");
                            face = fp.faces.front();
                        }
                        TopTools_IndexedDataMapOfShapeListOfShape adjacent;
                        TopExp::MapShapesAndAncestors(body, TopAbs_EDGE, TopAbs_FACE, adjacent);
                        for (const TopoDS_Edge& edge : *picked) {
                            bool found = false;
                            if (adjacent.Contains(edge)) {
                                const TopTools_ListOfShape& faces = adjacent.FindFromKey(edge);
                                for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next())
                                    if (it.Value().IsSame(face)) { found = true; break; }
                            }
                            if (!found)
                                throw std::runtime_error(
                                    "chamfer: reference_face must be adjacent to every selected edge");
                        }
                        chamfer_reference_face = face;
                    }
                }
                bool sub_tolerance = blend_size < 1e-7;
                if (sub_tolerance) {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                        "%s %.3g below tolerance; no material removed", type.c_str(), blend_size);
                    st.warning = buf;
                    out = body;  // pass through unchanged — the blend is a no-op
                } else if (type == "fillet") {
                    const double radius = blend_size;
                    // Variable-radius fillet (dress.py): a linear law r1->r2 along the edge,
                    // active when radius2 > 0 and differs from radius. Requires picked edges
                    // (the variable law is per-edge; the dir-filter path stays uniform).
                    double radius2 = ((blend_type.empty() || blend_type == "variable") &&
                                      feat.contains("radius2"))
                        ? num_or_expr(feat.at("radius2"), env) : 0.0;
                    const bool variable = (blend_type.empty() || blend_type == "variable") &&
                                          picked && radius2 > 0.0 &&
                                          std::abs(radius2 - radius) > 1e-9;
                    if (variable)
                        out = ::geom::fillet_edges_variable(body, radius, radius2, *picked, &n_dropped);
                    else
                        out = picked ? ::geom::fillet_edges(body, radius, *picked, &n_dropped,
                                                            &contact_why)
                                     : ::geom::fillet_edges(body, radius, dir);
                } else {
                    const double distance = blend_size;
                    // Two-distance (asymmetric) chamfer (dress.py): leg d1 on one face, d2
                    // on the other, active when length2 > 0 and differs from length. Needs
                    // picked edges (the asymmetric Add references a specific incident face).
                    double length2 = ((blend_type.empty() || blend_type == "asymmetric") &&
                                      feat.contains("length2"))
                        ? num_or_expr(feat.at("length2"), env) : 0.0;
                    const bool asym = (blend_type.empty() || blend_type == "asymmetric") &&
                                      picked && length2 > 0.0 &&
                                      std::abs(length2 - distance) > 1e-9;
                    if (blend_type == "vertex") {
                        std::string vertex_diag;
                        out = ::geom::chamfer_vertices(body, distance, picked_vertices, &vertex_diag);
                        if (out.IsNull())
                            throw std::runtime_error("chamfer: " +
                                (vertex_diag.empty() ? std::string("vertex corner cut failed")
                                                     : vertex_diag));
                    } else if (blend_type == "distance_angle")
                        out = ::geom::chamfer_edges_distance_angle(body, distance, chamfer_angle,
                                                                    *picked, chamfer_reference_face,
                                                                    &n_dropped);
                    else if (asym)
                        out = ::geom::chamfer_edges_two_distance(body, distance, length2, *picked,
                                                                 &n_dropped);
                    else
                        out = picked ? ::geom::chamfer_edges(body, distance, *picked, &n_dropped)
                                     : ::geom::chamfer_edges(body, distance, dir);
                }
                if (out.IsNull()) {
                    // A shared edge between two bodies the joining fillet can't serve:
                    // name the reason rather than calling the radius too large.
                    if (!contact_why.empty())
                        throw std::runtime_error(type + ": " + contact_why);
                    // A null kernel result does not establish radius feasibility:
                    // unsupported edge topology and failed corner construction also land here.
                    if (picked && !picked->empty()) {
                        char buf[256];
                        std::snprintf(buf, sizeof(buf),
                            "%s: could not construct a valid closed blend at %.4g mm on the "
                            "selected edge(s); size feasibility is undetermined. Inspect edge "
                            "continuity and supporting faces, or change the selection or %s",
                            type.c_str(), blend_size,
                            type == "fillet" ? "radius" : "distance");
                        throw std::runtime_error(buf);
                    }
                    throw std::runtime_error(type + " produced an empty shape");
                }
                // "Too large for the BODY" guard. Best-effort dropping is meant to skip a FEW
                // too-tight edges (a tight rim, a small boss) while rounding the rest — NOT to
                // quietly commit a heavily-reduced body when the size is infeasible for MOST of
                // the selection. If more than half the picked edges had to be dropped, the size
                // is simply too large here: fail loudly and leave the body intact (the user's
                // intent on an oversize "round everything" — B6-2). The watertight drop-blender
                // still handles the genuine "round 149 of 190, skip 41" case, where only a small
                // fraction drops, by committing the closed partial with a warning.
                if (!sub_tolerance && picked && n_dropped * 2 > static_cast<int>(picked->size())) {
                    if (!contact_why.empty())
                        throw std::runtime_error(type + ": " + contact_why);
                    char tbuf[224];
                    std::snprintf(tbuf, sizeof(tbuf),
                        "%s: %.4g mm is too large for this body — %d of %d selected edge(s) "
                        "couldn't be %s. Reduce the %s or select fewer edges.",
                        type.c_str(), blend_size, n_dropped, static_cast<int>(picked->size()),
                        type == "fillet" ? "rounded" : "beveled",
                        type == "fillet" ? "radius" : "distance");
                    throw std::runtime_error(tbuf);
                }
                // Partial success: some selected edges were too tight for the size, and/or some
                // selectors couldn't be located on the body (stale preview pick). The feature still
                // applies to the edges that worked — surface the rest as a non-fatal warning so the
                // user knows to lower the size or re-pick, rather than silently leaving them sharp.
                if (!sub_tolerance && picked && (n_dropped > 0 || n_dangling > 0)) {
                    const int requested = (int)picked->size() + n_dangling;
                    const int rounded = (int)picked->size() - n_dropped;
                    char buf[240];
                    const char* sz = type == "fillet" ? "radius" : "distance";
                    if (n_dangling > 0 && n_dropped > 0)
                        std::snprintf(buf, sizeof(buf),
                            "rounded %d of %d edge(s) at %.4g mm; %d too tight, %d couldn't be "
                            "located - lower the %s or re-pick those edge(s)",
                            rounded, requested, blend_size, n_dropped, n_dangling, sz);
                    else if (n_dropped > 0)
                        std::snprintf(buf, sizeof(buf),
                            "rounded %d of %d edge(s) at %.4g mm; %d too tight - lower the %s or "
                            "deselect those edge(s)",
                            rounded, requested, blend_size, n_dropped, sz);
                    else
                        std::snprintf(buf, sizeof(buf),
                            "rounded %d of %d edge(s); %d selector(s) couldn't be located - re-pick "
                            "those edge(s)",
                            rounded, requested, n_dangling);
                    st.warning = contact_why.empty() ? std::string(buf)
                                                     : std::string(buf) + " (" + contact_why + ")";
                }

                // OPEN-SHELL guard (Bug S2): the drop-failing blender rounds the FEASIBLE
                // edges and skips the too-tight ones — but if a skipped edge was geometry
                // OCCT couldn't reseal, the committed B-rep can be an OPEN shell (genuine
                // free edges) even though its volume is positive (so the N2 negative-volume
                // guard passes). eval_check would then report free_edges and STEP-export
                // re-validation would fail, yet the feature reported ok:true. A dress op
                // must NEVER ship an open body: validate the result is a CLOSED solid using
                // the SEAM-AWARE free-edge test (so a valid cylinder/sphere/revolved body's
                // seam edges are NOT mistaken for free edges). A sub-tolerance pass-through
                // (out == body) is the unchanged input, already closed — skip the check.
                // SURFACE (open sheet) source bodies are legitimately open — skip the
                // closed-solid post-check for them (mirrors sanitize_solid's surface exemption).
                {
                    auto bk_src = body_kinds.find(body_id);
                    const bool src_is_surface =
                        bk_src != body_kinds.end() && bk_src->second == "surface";
                    if (!src_is_surface && !sub_tolerance && !dress_is_closed_solid(out)) {
                        char obuf[256];
                        std::snprintf(obuf, sizeof(obuf),
                            "%s produced an open/invalid body — some edges are too tight at %.4g mm "
                            "to reseal the surface (the result has free edges / is not a closed "
                            "solid). Reduce the %s or select fewer edges.",
                            type.c_str(), blend_size,
                            type == "fillet" ? "radius" : "distance");
                        throw std::runtime_error(obuf);
                    }
                    if (!src_is_surface && !sub_tolerance) {
                        const std::string rejection = dress_geometry_rejection(out);
                        if (!rejection.empty())
                            throw std::runtime_error(
                                type + " at " + std::to_string(blend_size) +
                                " mm produced invalid geometry: " + rejection +
                                ". Reduce the " + (type == "fillet" ? "radius" : "distance") +
                                " or revise the selected edges; the preceding body is unchanged.");
                    }
                }

                // Dress-up REPLACES the source body (SolidWorks-style): the rounded /
                // beveled solid is the current state of that body in the tree. Recording
                // `source_of` here (it was previously omitted) is what lets the owner-map
                // diff keep the unchanged faces with the ORIGINAL feature instead of
                // attributing the whole body to this dress feature (P14).
                consumed.insert(body_id);

                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "linear_pattern") {
                // Replicate the SEED feature's TOOL BODY on a 1-D row (or 2-D grid),
                // re-applying its boolean to the running body — so a patterned boss fuses
                // N times and a patterned cut/hole subtracts N times (N holes), exactly
                // like SolidWorks. Instance (0,0) IS the seed already baked into the
                // running body, so we only place the additional cells. Port of
                // engine/doc/features/pattern.py's geometry strategy in the id-ref model
                // (no skip / vary / bidirectional).
                std::pair<TopoDS_Shape, std::string> so =
                    seed_tool_op(feat, "linear pattern");
                const TopoDS_Shape seed_tool = so.first;
                const std::string seed_op = so.second;
                // The running body that already contains instance (0,0) (= bodies[-1]).
                const std::string tgt_id = current_solid_id();
                if (tgt_id.empty())
                    throw std::runtime_error("linear pattern: no body to pattern");
                TopoDS_Shape res = require_solid(results, tgt_id, "target");

                const int count1 =
                    static_cast<int>(std::lround(num_or_expr(feat.at("count1"), env)));
                if (count1 < 1)
                    throw std::runtime_error("linear pattern count1 must be >= 1");
                const double spacing1 = num_or_expr(feat.at("spacing1"), env);
                // Non-zero spacing guard (Bug B2): spacing1 == 0 with count1 >= 2 stacks
                // every copy on top of the seed (coincident bodies) — accepted silently
                // before. Mirror the count1 >= 1 guard above.
                if (count1 >= 2 && !(std::abs(spacing1) > 0.0))
                    throw std::runtime_error(
                        "linear_pattern: spacing must be non-zero when count >= 2");
                Eigen::Vector3d dir1 = req_vec3(feat, "dir1");
                if (!(dir1.norm() > 1e-12))
                    throw std::runtime_error("linear pattern dir1 has zero length");
                dir1.normalize();

                // Optional second direction -> a 2-D grid. Absent dir2 keeps count2 = 1.
                const bool has_dir2 = feat.contains("dir2");
                int count2 = 1;
                double spacing2 = 0.0;
                Eigen::Vector3d dir2(0.0, 0.0, 0.0);
                if (has_dir2) {
                    count2 = feat.contains("count2")
                        ? static_cast<int>(std::lround(num_or_expr(feat.at("count2"), env)))
                        : 1;
                    if (count2 < 1)
                        throw std::runtime_error("linear pattern count2 must be >= 1");
                    spacing2 = feat.contains("spacing2")
                        ? num_or_expr(feat.at("spacing2"), env) : 0.0;
                    // Non-zero spacing guard for the second direction (Bug B2).
                    if (count2 >= 2 && !(std::abs(spacing2) > 0.0))
                        throw std::runtime_error(
                            "linear_pattern: spacing must be non-zero when count >= 2");
                    dir2 = req_vec3(feat, "dir2");
                    if (!(dir2.norm() > 1e-12))
                        throw std::runtime_error("linear pattern dir2 has zero length");
                    dir2.normalize();
                }

                if (count1 * count2 < 2)
                    throw std::runtime_error("linear pattern needs at least 2 instances");
                // INSTANCE CAP (Bug K1): a runaway count (e.g. 100x100 = 10000 cells)
                // builds tens of thousands of boolean ops and used to HANG the whole MCP
                // server (wiping every session doc). Reject an over-large field LOUDLY and
                // BEFORE any expensive geometry runs. The cap is generous (4000) — far
                // above any real part — so legitimate patterns are never blocked.
                {
                    const long long total = static_cast<long long>(count1) * count2;
                    if (total > kPatternInstanceLimit)
                        throw std::runtime_error(
                            "pattern: " + std::to_string(total) +
                            " instances exceeds the limit (" +
                            std::to_string(kPatternInstanceLimit) + "); reduce the count");
                }

                // Lay copies of the seed TOOL at i*spacing1*dir1 (+ j*spacing2*dir2),
                // re-applying the seed's boolean. Instance (0,0) is the seed, already in
                // the running body. We ALSO accumulate the union of every seed tool
                // instance (instance (0,0) at its base pose included) into pattern_tool —
                // this is the nesting tool an OUTER pattern of THIS pattern replicates when
                // the seed is subtractive (Bug J1): a linear_pattern of a circular_pattern
                // of CUTS must re-SUBTRACT the whole ring at each outer cell, not fuse the
                // grown body (which would ADD material and leave one ring).
                // Lay down every copy first, then apply ONE multi-argument boolean for the
                // whole field (a single BOPDS pass) instead of N pairwise booleans that each
                // re-traverse the growing body (~O(N²)). pattern_tool — the nesting tool an
                // outer pattern replicates for a subtractive seed — is the union of instance
                // (0,0) + every copy, built the same batched way.
                std::vector<TopoDS_Shape> copies;
                copies.reserve((size_t)count1 * count2);
                for (int i = 0; i < count1; ++i) {
                    for (int j = 0; j < count2; ++j) {
                        if (i == 0 && j == 0) continue;  // the seed instance
                        Eigen::Vector3d offset = dir1 * (i * spacing1);
                        if (has_dir2) offset += dir2 * (j * spacing2);
                        copies.push_back(::geom::translate_copy(seed_tool, offset));
                    }
                }
                res = (seed_op == "cut") ? ::geom::cut_many(res, copies)
                                         : ::geom::fuse_many(res, copies);
                if (res.IsNull())
                    throw std::runtime_error("linear pattern instance "
                                             + std::string(seed_op == "cut" ? "cut" : "fuse")
                                             + " failed");
                TopoDS_Shape pattern_tool = seed_tool;  // instance (0,0)
                if (seed_op == "cut") {
                    TopoDS_Shape pu = ::geom::fuse_many(seed_tool, copies);
                    if (!pu.IsNull()) pattern_tool = pu;
                }

                // build123d's `+` runs .clean() (UnifySameDomain), so abutting instances
                // that fuse flush merge their coplanar seam faces — match it on the
                // additive path so the face count agrees (the cut path leaves seams, like
                // build123d's `-`). A disjoint compound has no shared domain -> a no-op.
                if (seed_op != "cut") {
                    TopoDS_Shape u = ::geom::unify_same_domain(res);
                    if (!u.IsNull()) res = u;
                }

                // The running body is consumed; the pattern result is its new state. The
                // pattern's nesting tool/op honour the seed's op (Bug J1): a SUBTRACTIVE
                // pattern exposes the UNION of its cut tools as op "cut", so an outer
                // pattern of it re-subtracts the whole field per cell; an ADDITIVE pattern
                // exposes its whole grown result as op "add" (matches the oracle, so a
                // pattern of a boss-pattern replicates the full body).
                consumed.insert(tgt_id);
                source_of[id] = tgt_id;  // owner-map: pattern derives from the running body
                if (seed_op == "cut") {
                    tool_body_of[id] = pattern_tool;
                    feature_op_of[id] = "cut";
                } else {
                    tool_body_of[id] = res;
                    feature_op_of[id] = "add";
                }
                {
                    // Additive pattern: tool_body_of/res above intentionally holds the
                    // whole GROWN body (so an outer pattern can re-fuse it wholesale);
                    // for overlap detection we want only the material THIS feature
                    // added, so build the seed+copies union the same way the cut branch
                    // already does for pattern_tool.
                    TopoDS_Shape overlap_tool = pattern_tool;
                    if (seed_op != "cut") {
                        TopoDS_Shape pu = ::geom::fuse_many(seed_tool, copies);
                        if (!pu.IsNull()) overlap_tool = pu;
                    }
                    record_tool(id, seed_op == "cut" ? "cut" : "add", overlap_tool);
                }

                results[id] = SolidResult{res};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "circular_pattern") {
                // Replicate the SEED feature's TOOL BODY around an axis, re-applying its
                // boolean. k=0 is the seed (already in the running body); copies
                // k=1..count-1 are rotated by step_deg*k. A patterned cut/hole bores N
                // radial holes; a patterned boss fuses N copies. Mirrors pattern.py.
                std::pair<TopoDS_Shape, std::string> so =
                    seed_tool_op(feat, "circular pattern");
                const TopoDS_Shape seed_tool = so.first;
                const std::string seed_op = so.second;
                const std::string tgt_id = current_solid_id();
                if (tgt_id.empty())
                    throw std::runtime_error("circular pattern: no body to pattern");
                TopoDS_Shape res = require_solid(results, tgt_id, "target");

                const int count =
                    static_cast<int>(std::lround(num_or_expr(feat.at("count"), env)));
                if (count < 2)
                    throw std::runtime_error("circular pattern count must be >= 2");
                // INSTANCE CAP (Bug K1): reject a runaway count before the build.
                if (static_cast<long long>(count) > kPatternInstanceLimit)
                    throw std::runtime_error(
                        "pattern: " + std::to_string(count) +
                        " instances exceeds the limit (" +
                        std::to_string(kPatternInstanceLimit) + "); reduce the count");

                // Angle is in DEGREES (SolidWorks + pattern.py convention) — note this
                // DIFFERS from revolve's angle, which is radians.
                const double angle_deg = feat.contains("angle")
                    ? num_or_expr(feat.at("angle"), env) : 360.0;
                const bool equal_spacing = feat.value("equal_spacing", true);
                // equal_spacing: instances fill the total angle (a full 360 wraps
                // cleanly); otherwise `angle` is the increment between instances.
                const double step_deg = equal_spacing ? angle_deg / count : angle_deg;

                const Eigen::Vector3d axis_pt = req_vec3(feat, "axis_pt");
                const Eigen::Vector3d axis_dir = req_vec3(feat, "axis_dir");

                // Accumulate every instance's tool (k=0 at base pose included) into a
                // pattern_tool — the nesting tool an outer pattern of THIS pattern
                // replicates when the seed is subtractive (Bug J1).
                // Lay down every rotated copy, then ONE multi-argument boolean for the whole
                // ring (single BOPDS pass) instead of N pairwise booleans over the growing
                // body (~O(N²)). pattern_tool (nesting tool for a subtractive seed) is the
                // union of k=0 + every copy, built the same batched way.
                std::vector<TopoDS_Shape> copies;
                copies.reserve((size_t)(count > 1 ? count - 1 : 0));
                for (int k = 1; k < count; ++k)
                    copies.push_back(::geom::rotate_copy(
                        seed_tool, axis_pt, axis_dir, radians(step_deg * k)));
                res = (seed_op == "cut") ? ::geom::cut_many(res, copies)
                                         : ::geom::fuse_many(res, copies);
                if (res.IsNull())
                    throw std::runtime_error("circular pattern instance "
                                             + std::string(seed_op == "cut" ? "cut" : "fuse")
                                             + " failed");
                TopoDS_Shape pattern_tool = seed_tool;  // k = 0
                if (seed_op == "cut") {
                    TopoDS_Shape pu = ::geom::fuse_many(seed_tool, copies);
                    if (!pu.IsNull()) pattern_tool = pu;
                }

                // Match build123d's `+` .clean() on the additive path (merge coplanar
                // seams from abutting instances); the cut path leaves seams (like `-`).
                if (seed_op != "cut") {
                    TopoDS_Shape u = ::geom::unify_same_domain(res);
                    if (!u.IsNull()) res = u;
                }

                // The running body is consumed; the pattern result is its new state. The
                // nesting tool/op honour the seed's op (Bug J1): a SUBTRACTIVE pattern
                // exposes the union of its cut tools as op "cut" so an outer pattern of it
                // re-subtracts the whole ring per cell; an additive pattern exposes its
                // grown body as op "add".
                consumed.insert(tgt_id);
                source_of[id] = tgt_id;  // owner-map: pattern derives from the running body
                if (seed_op == "cut") {
                    tool_body_of[id] = pattern_tool;
                    feature_op_of[id] = "cut";
                } else {
                    tool_body_of[id] = res;
                    feature_op_of[id] = "add";
                }
                {
                    TopoDS_Shape overlap_tool = pattern_tool;
                    if (seed_op != "cut") {
                        TopoDS_Shape pu = ::geom::fuse_many(seed_tool, copies);
                        if (!pu.IsNull()) overlap_tool = pu;
                    }
                    record_tool(id, seed_op == "cut" ? "cut" : "add", overlap_tool);
                }

                results[id] = SolidResult{res};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "mirror") {
                // Reflect a referenced solid body across a plane, then either FUSE the
                // reflection into the body (merge:true, the default — SolidWorks "Mirror
                // bodies" with Merge on) or keep it as a SEPARATE new body (merge:false).
                // The plane comes from a FACE SELECTOR ({"kind":"face",...} resolved
                // against the body's live topology) or an explicit plane_point/normal —
                // the simplified id-ref port of engine/doc/features/pattern.py::compile_mirror.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");

                // Resolve the mirror plane to (point, unit normal).
                Eigen::Vector3d point, normal;
                auto plane_it = feat.find("plane");
                const bool face_plane =
                    plane_it != feat.end() && plane_it->is_object() &&
                    plane_it->value("kind", std::string()) == "face";
                if (face_plane) {
                    validate_selector_refs(*plane_it, "mirror plane");  // Bug K4
                    chudware::doc::FacePicks fp =
                        chudware::doc::resolve_face_picks(body, *plane_it, "plane");
                    if (!fp.dangling.empty()) {
                        std::string msg = "mirror: plane selector dangling: ";
                        for (std::size_t k = 0; k < fp.dangling.size(); ++k) {
                            if (k) msg += "; ";
                            msg += fp.dangling[k];
                        }
                        throw std::runtime_error(msg);
                    }
                    if (fp.faces.empty())
                        throw std::runtime_error("mirror: plane face not resolved");
                    BRepAdaptor_Surface surf(fp.faces[0]);
                    if (surf.GetType() != GeomAbs_Plane)
                        throw std::runtime_error("mirror: selected face is not planar");
                    gp_Pln pln = surf.Plane();
                    gp_Pnt loc = pln.Location();
                    gp_Dir dir = pln.Axis().Direction();
                    point = Eigen::Vector3d(loc.X(), loc.Y(), loc.Z());
                    normal = Eigen::Vector3d(dir.X(), dir.Y(), dir.Z());
                } else {
                    point = req_vec3(feat, "plane_point");
                    normal = req_vec3(feat, "plane_normal");
                    if (!(normal.norm() > 1e-12))
                        throw std::runtime_error("mirror: plane_normal has zero length");
                    normal.normalize();
                }

                if (feat.contains("seed")) {
                    // FEATURE MIRROR (SolidWorks "Features to Mirror", the oracle's
                    // refs.features path): reflect the SEED feature's TOOL body across the
                    // plane and re-apply its boolean to the running body — a mirrored cut
                    // bores the reflected hole, a mirrored boss fuses the reflected boss.
                    // (A whole-body reflect+fuse would be wrong for a subtractive seed.)
                    std::pair<TopoDS_Shape, std::string> so = seed_tool_op(feat, "mirror");
                    TopoDS_Shape rtool = ::geom::mirror_copy(so.first, point, normal);
                    if (rtool.IsNull())
                        throw std::runtime_error("mirror: reflecting the seed feature produced "
                                                 "an empty shape");
                    TopoDS_Shape out = (so.second == "cut")
                        ? ::geom::cut_unify(body, rtool)
                        : ::geom::fuse_unify(body, rtool);
                    if (out.IsNull())
                        throw std::runtime_error("mirror: feature mirror "
                            + std::string(so.second == "cut" ? "cut" : "fuse")
                            + " produced an empty shape");
                    consumed.insert(body_id);
                    source_of[id] = body_id;
                    tool_body_of[id] = out;
                    feature_op_of[id] = "add";
                    record_tool(id, so.second == "cut" ? "cut" : "add", rtool);
                    results[id] = SolidResult{out};
                    solid_order.push_back(id);
                    st.ok = true;
                } else {
                    TopoDS_Shape reflected = ::geom::mirror_copy(body, point, normal);
                    if (reflected.IsNull())
                        throw std::runtime_error("mirror produced an empty reflection");

                    const bool merge = feat.value("merge", true);
                    if (merge) {
                        // Union the reflection into the body; the body is consumed (the
                        // merged solid is its new current state in the tree). build123d's
                        // mirror merge runs .clean() (UnifySameDomain), so the seam where
                        // the two flush halves meet collapses — match the face count.
                        TopoDS_Shape out = ::geom::fuse_unify(body, reflected);
                        if (out.IsNull())
                            throw std::runtime_error("mirror union (fuse) produced an empty shape");
                        consumed.insert(body_id);
                        results[id] = SolidResult{out};
                        record_tool(id, "add", reflected);
                    } else {
                        // The reflection is a NEW standalone body; the original body is
                        // NOT consumed, so BOTH survive.
                        results[id] = SolidResult{reflected};
                        record_tool(id, "add", reflected);
                    }
                    solid_order.push_back(id);
                    st.ok = true;
                }

            } else if (type == "transform") {
                // Move/rotate a body in space — translate + rotate (axis+angle about a pivot).
                // Works on ANY solid (a lofted fuselage, an extruded wing), not just primitives'
                // `at`, so separate components can be placed into a shared frame for assembly
                // (fixes the "no way to position a non-primitive body" gap). `body` targets an
                // earlier feature id; default is the current running body. `copy:true` keeps the
                // original and adds a transformed DUPLICATE as a new body (assembly instancing);
                // default moves the body in place (the source is consumed).
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
                // Compose on the body's CURRENT descendant (Issue 4). If a prior in-place
                // transform already moved this source body, `body_id` (the original feature id)
                // now points at a consumed shape; chase superseded_by to the live replacement so
                // a second transform moves the placed body rather than re-reading the original
                // and instancing a duplicate. A self-referential or broken chain stops at the
                // last resolvable link (guarded against cycles).
                std::string eff_id = body_id;
                {
                    std::set<std::string> guard;
                    while (consumed.count(eff_id) && superseded_by.count(eff_id) &&
                           guard.insert(eff_id).second)
                        eff_id = superseded_by[eff_id];
                }
                if (consumed.count(eff_id))
                    throw std::runtime_error("transform: body '" + body_id +
                        "' is consumed by a later feature; target its live body owner");
                const TopoDS_Shape& body = require_solid(results, eff_id, "body");

                Eigen::Vector3d translate(0, 0, 0);
                if (auto t = opt_vec3(feat, "translate")) translate = *t;

                const json rotation = feat.value("rotate", json());
                const bool rotation_object = rotation.is_object();
                const double angle_deg = rotation_object && rotation.contains("angle")
                    ? num_or_expr(rotation.at("angle"), env)
                    : feat.contains("angle") ? num_or_expr(feat.at("angle"), env) : 0.0;
                Eigen::Vector3d axis_dir(0, 0, 1);
                if (rotation_object) {
                    if (auto a = opt_vec3(rotation, "axis")) axis_dir = *a;
                } else if (auto a = opt_vec3(feat, "rotate")) axis_dir = *a;
                else if (auto a2 = opt_vec3(feat, "axis")) axis_dir = *a2;
                Eigen::Vector3d pivot(0, 0, 0);
                if (auto pv = opt_vec3(feat, "pivot")) pivot = *pv;

                // Bug A2 — a zero-motion transform (translate:[0,0,0], no rotation) is a
                // harmless NO-OP that SUCCEEDS: it records the body at its current pose
                // (useful for explicitly placing an at-origin component in an assembly).
                // It must NOT error (the old "no motion" rejection was a P0-deadlock
                // trigger via the rollback). The body is committed unchanged below; with
                // copy:true it is duplicated in place (a coincident assembly instance).
                const bool zero_motion =
                    !(translate.norm() > 1e-12) && std::abs(angle_deg) <= 1e-9;

                // Rotate first (about the pivot), then translate — the intuitive "orient then
                // place" order. Placed as a shared TopLoc_Location (instance), NOT a baked copy:
                // the moved body keeps sharing its source TShape, so N identical placed parts
                // (e.g. the 4 wheels of an assembly imported from the same file) deserialize +
                // tessellate + mass-prop ONCE, not N times. A rigid move (rotate+translate) is
                // all `transform` ever does, so a location is geometrically exact.
                if (std::abs(angle_deg) > 1e-9 && !(axis_dir.norm() > 1e-12))
                    throw std::runtime_error(type + ": rotate axis has zero length");
                TopoDS_Shape out = ::geom::rigid_instance(
                    body, pivot, axis_dir,
                    std::abs(angle_deg) > 1e-9 ? radians(angle_deg) : 0.0, translate);
                if (out.IsNull())
                    throw std::runtime_error(type + ": transform produced an empty shape");

                const bool copy = feat.value("copy", false);
                if (!copy) {
                    // The overlap and attachment checks read result.tool_recs. A tool that
                    // built this body was recorded at the body's OLD pose, so after the move
                    // it no longer matches the part (seen live: f56/f66 reported as a
                    // boss_overlap/cut_into_boss at their pre-transform z). Move every tool of
                    // the body's lineage by the same rigid motion. Lineage = the source_of
                    // chain plus earlier in-place transforms/heals (superseded_by). A tool whose
                    // feature is in no live body's lineage (a secondary merge target) belongs
                    // to this body when it touches the pre-move body. A tool shared with a
                    // body that stays (a split piece) keeps its record and gets a moved copy.
                    if (!zero_motion && !result.tool_recs.empty()) {
                        std::map<std::string, std::string> supersedes;
                        for (const auto& link : superseded_by) supersedes[link.second] = link.first;
                        auto lineage_of = [&](const std::string& root) {
                            std::set<std::string> seen;
                            std::vector<std::string> stack{root};
                            while (!stack.empty()) {
                                const std::string x = stack.back();
                                stack.pop_back();
                                if (!seen.insert(x).second) continue;
                                if (auto it = source_of.find(x); it != source_of.end())
                                    stack.push_back(it->second);
                                if (auto it = supersedes.find(x); it != supersedes.end())
                                    stack.push_back(it->second);
                            }
                            return seen;
                        };
                        const std::set<std::string> moved = lineage_of(eff_id);
                        std::set<std::string> stays;
                        std::vector<TopoDS_Shape> stay_bodies;
                        for (const std::string& sid : solid_order) {
                            if (sid == eff_id || consumed.count(sid) || !results.count(sid))
                                continue;
                            const auto* sr = std::get_if<SolidResult>(&*results.at(sid));
                            if (!sr || sr->shape.IsNull()) continue;
                            stay_bodies.push_back(sr->shape);
                            for (const std::string& f : lineage_of(sid)) stays.insert(f);
                        }
                        auto touches = [](const TopoDS_Shape& a, const TopoDS_Shape& b) {
                            try {
                                Bnd_Box ba, bb;
                                BRepBndLib::Add(a, ba, Standard_False);
                                BRepBndLib::Add(b, bb, Standard_False);
                                ba.Enlarge(1e-6);
                                if (ba.IsOut(bb)) return false;
                                BRepExtrema_DistShapeShape dss(a, b);
                                return dss.IsDone() && dss.NbSolution() > 0 &&
                                       dss.Value() <= 1e-6;
                            } catch (const Standard_Failure&) {
                                return false;
                            }
                        };
                        const double move_rad =
                            std::abs(angle_deg) > 1e-9 ? radians(angle_deg) : 0.0;
                        std::vector<ToolRec> moved_copies;
                        for (ToolRec& tr : result.tool_recs) {
                            bool mine = moved.count(tr.feature_id) > 0;
                            bool shared = mine && stays.count(tr.feature_id) > 0;
                            if (!mine && !stays.count(tr.feature_id) && touches(tr.tool, body)) {
                                mine = true;
                                for (const TopoDS_Shape& other : stay_bodies)
                                    if (touches(tr.tool, other)) { shared = true; break; }
                            }
                            if (!mine) continue;
                            TopoDS_Shape placed = ::geom::rigid_instance(
                                tr.tool, pivot, axis_dir, move_rad, translate);
                            if (placed.IsNull()) continue;
                            if (shared)
                                moved_copies.push_back(ToolRec{tr.feature_id, tr.role, placed});
                            else
                                tr.tool = placed;
                        }
                        for (ToolRec& tr : moved_copies) result.tool_recs.push_back(std::move(tr));
                    }
                    consumed.insert(eff_id);            // move in place: source replaced
                    superseded_by[eff_id] = id;         // a later transform composes on `id`
                }
                tool_body_of[id] = out;                // patternable/mirrorable downstream
                feature_op_of[id] = "add";
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                // A zero-motion transform succeeds (Bug A2) but is worth flagging so the
                // caller knows the body was recorded in place rather than actually moved.
                if (zero_motion)
                    st.warning = "transform recorded the body at its current pose "
                                 "(zero motion — no translate/rotate given)";
                st.ok = true;

            } else if (type == "heal") {
                // REPAIR a topologically broken solid — the op that had no answer before.
                // A live frame assembly's imported STEP spine failed health with
                // `bad_orientation_of_subshape`, and because nothing in the tree could fix
                // shape topology the agent tried booleans, BREP round-trips and STL sewing
                // in turn (all wrong, all silently lossy).
                //
                // ShapeFix is the first answer and often the only one needed, but it is NOT
                // sufficient: the live rover spine (body 47 of CD-100 Stage 5) arrived with
                // 144 of its 344 edges traversed the SAME way by both adjacent faces, and a
                // five-point ShapeFix tolerance ladder (1e-7 … 0.1 mm) either did nothing or,
                // at 0.1 mm, destroyed 99.21 mm^3 and invented three new defect kinds. So the
                // op offers three escalating repairs beyond ShapeFix, each opt-in and each
                // recorded, and REFUSES outright when the result is worse than the input.
                //
                // Params:
                //   tolerance        precision handed to ShapeFix (mm). Default 1e-7 —
                //                    OCCT's Confusion; raise it (0.01-0.1) for a coarse
                //                    imported mesh/STEP whose gaps are real.
                //   fix_small_edges  also run ShapeFix_Wireframe (drop tiny edges, close
                //                    wire gaps). Off by default: it REMOVES topology, so a
                //                    body whose small edges are intentional (a 0.05 mm
                //                    fillet band) must opt in.
                //   sew_tolerance_mm re-sew the faces at this tolerance (BRepBuilderAPI_
                //                    Sewing): edge sharing is re-derived from GEOMETRY, which
                //                    fixes a body whose stored sharing is wrong. Null = off.
                //   fix_orientation  flood-fill the face-orientation graph (every edge must be
                //                    traversed oppositely by its two faces), then re-wrap the
                //                    shells outward. Purely topological — no surface is
                //                    rebuilt, so areas and analytic surface types survive
                //                    exactly. This is the right first escalation and the whole
                //                    fix for a merely inside-out import.
                //   rebuild_solid    re-derive the solid from its faces with
                //                    BOPAlgo_MakerVolume: the faces are intersected and every
                //                    enclosed cell rebuilt with a geometrically-derived
                //                    orientation. The escalation for a body whose edge sharing
                //                    is self-CONTRADICTORY (fix_orientation reports
                //                    `orientation.conflicts` > 0) — the case ShapeFix, sewing
                //                    and flag repair all provably cannot reach. Self-
                //                    intersecting input splits into cells; the principal lump
                //                    is kept and the discarded ones are reported, budgeted by
                //                    max_volume_change_pct.
                //   rebuild_fuzzy_mm the rebuild's intersection merge distance (mm). Null =
                //                    OCCT's own tolerance, which gives every pair of near-
                //                    coincident input faces its own intersection line and so
                //                    leaves RAZOR STRIP faces behind — valid geometry that no
                //                    volume mesher can resolve (body 47's rebuild carried two
                //                    0.011 mm crescents and polymesh refused the fill at every
                //                    element size). 0.05 merges them during the intersection.
                //                    Only meaningful with rebuild_solid.
                //   unify_faces      ShapeUpgrade_UnifySameDomain: merge the same-domain faces
                //                    a rebuild leaves split.
                //   max_volume_change_pct
                //                    refusal budget, default 0.5 (%). See the guard block.
                // Ref: `body` targets an earlier feature id; default is the current running
                // body. The healed body REPLACES the input (copy:false semantics, like
                // `transform`) — a repair that left the broken original alive would keep
                // exporting the broken one.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
                // Compose on the body's CURRENT descendant, same chase as `transform`: a
                // prior in-place transform/heal leaves `body_id` pointing at a consumed
                // shape, and healing the stale original would resurrect it as a duplicate.
                std::string eff_id = body_id;
                {
                    std::set<std::string> guard;
                    while (consumed.count(eff_id) && superseded_by.count(eff_id) &&
                           guard.insert(eff_id).second)
                        eff_id = superseded_by[eff_id];
                }
                const TopoDS_Shape& body = require_solid(results, eff_id, "body");

                double tol = 1e-7;  // Precision::Confusion
                if (feat.contains("tolerance") && !feat.at("tolerance").is_null())
                    tol = num_or_expr(feat.at("tolerance"), env);
                if (!(tol > 0.0))
                    throw std::runtime_error(type + ": tolerance must be > 0");
                const bool fix_small_edges = feat.value("fix_small_edges", false);
                const bool fix_orientation = feat.value("fix_orientation", false);
                const bool rebuild_solid = feat.value("rebuild_solid", false);
                const bool unify_faces = feat.value("unify_faces", false);
                double sew_tol = 0.0;  // 0 = sewing off
                if (feat.contains("sew_tolerance_mm") && !feat.at("sew_tolerance_mm").is_null()) {
                    sew_tol = num_or_expr(feat.at("sew_tolerance_mm"), env);
                    if (!(sew_tol > 0.0))
                        throw std::runtime_error(type + ": sew_tolerance_mm must be > 0 "
                                                        "(omit it to skip the sewing pass)");
                }
                double rebuild_fuzzy = 0.0;  // 0 = OCCT's own intersection tolerance
                if (feat.contains("rebuild_fuzzy_mm") &&
                    !feat.at("rebuild_fuzzy_mm").is_null()) {
                    rebuild_fuzzy = num_or_expr(feat.at("rebuild_fuzzy_mm"), env);
                    if (!(rebuild_fuzzy > 0.0))
                        throw std::runtime_error(type + ": rebuild_fuzzy_mm must be > 0 "
                                                        "(omit it for OCCT's own tolerance)");
                }
                double max_vol_pct = 0.5;
                if (feat.contains("max_volume_change_pct") &&
                    !feat.at("max_volume_change_pct").is_null()) {
                    max_vol_pct = num_or_expr(feat.at("max_volume_change_pct"), env);
                    if (!(max_vol_pct >= 0.0))
                        throw std::runtime_error(type + ": max_volume_change_pct must be >= 0");
                }

                auto count_of = [](const TopoDS_Shape& s, TopAbs_ShapeEnum t) {
                    TopTools_IndexedMapOfShape m;
                    TopExp::MapShapes(s, t, m);
                    return m.Extent();
                };
                const std::vector<std::string> before_issues = heal_brep_issues(body);
                const bool before_valid = before_issues.empty();
                const double volume_before = heal_volume(body);
                const double area_before = heal_area(body);
                const std::array<double, 6> bbox_before = heal_bbox(body);
                const int bad_edges_before = heal_bad_edge_count(body);
                const int faces_before = count_of(body, TopAbs_FACE);
                const int solids_before_heal = count_of(body, TopAbs_SOLID);
                // A signed volume integral over a shell whose faces disagree about which
                // side is material is NOT a volume — reversing one face of a 24000 mm^3 box
                // makes it read 16000. So the raw `volume_before` is only a trustworthy
                // baseline when the input carries no orientation defect.
                auto reading_trustworthy = [](const std::vector<std::string>& issues) {
                    for (const std::string& s : issues)
                        if (s.find("orientation") != std::string::npos ||
                            s == "unorientable_shape")
                            return false;
                    return true;
                };
                const bool volume_reading_trustworthy = reading_trustworthy(before_issues);
                // The budget still has to apply to that input — it is the whole reason this
                // op exists — so an untrustworthy reading is NORMALIZED instead of excused:
                // flood-orient a COPY (topology only, not one surface rebuilt) and integrate
                // that. The result is the material the shape was MEANT to enclose, and it is
                // computed the same way on both sides of the repair, so the comparison is
                // like-for-like whether or not the defect survived. Excusing it instead left
                // the live spine's -0.93% raw drift completely unguarded.
                auto material_volume = [&](const TopoDS_Shape& s, bool trustworthy) {
                    if (trustworthy) return heal_volume(s);
                    try {
                        int f = 0, c = 0;
                        const TopoDS_Shape oriented = heal_flood_orientation(s, f, c);
                        const double v = std::abs(heal_volume(oriented));
                        if (v > 1e-12) return v;
                    } catch (const Standard_Failure&) {
                    }
                    return heal_volume(s);
                };
                const double material_before =
                    material_volume(body, volume_reading_trustworthy);

                // --- the ShapeFix pass ------------------------------------------------
                // MaxTolerance is what actually lets the fixer close a gap: with precision
                // alone it refuses to move anything further than `tol`. 100x precision (at
                // least 0.01 mm) is the OCCT-idiomatic headroom used by our own importer.
                TopoDS_Shape out = body;
                try {
                    ShapeFix_Shape fixer(out);
                    fixer.SetPrecision(tol);
                    fixer.SetMaxTolerance(std::max(tol * 100.0, 1e-2));
                    fixer.SetMinTolerance(tol);
                    fixer.Perform();
                    const TopoDS_Shape fixed = fixer.Shape();
                    if (!fixed.IsNull()) out = fixed;
                } catch (const Standard_Failure&) {
                    out = body;  // a fixer crash leaves the body exactly as it came in
                }
                if (fix_small_edges) {
                    try {
                        ShapeFix_Wireframe wf(out);
                        wf.SetPrecision(tol);
                        wf.SetMaxTolerance(std::max(tol * 100.0, 1e-2));
                        wf.ModeDropSmallEdges() = Standard_True;
                        wf.FixSmallEdges();
                        wf.FixWireGaps();
                        const TopoDS_Shape wfixed = wf.Shape();
                        if (!wfixed.IsNull()) out = wfixed;
                    } catch (const Standard_Failure&) {
                        // keep whatever ShapeFix_Shape produced
                    }
                }
                // Solid/shell orientation is the class of defect that motivated the op, and
                // ShapeFix_Shape's solid tool does not always reach it on an imported body —
                // run the dedicated tool on each solid explicitly. A compound of solids is
                // rebuilt from the healed members so a multi-lump import is fixed whole.
                try {
                    if (out.ShapeType() == TopAbs_SOLID) {
                        ShapeFix_Solid sfx(TopoDS::Solid(out));
                        sfx.SetPrecision(tol);
                        sfx.Perform();
                        const TopoDS_Shape sfixed = sfx.Solid();
                        if (!sfixed.IsNull()) out = sfixed;
                    } else {
                        TopTools_IndexedMapOfShape smap;
                        TopExp::MapShapes(out, TopAbs_SOLID, smap);
                        if (smap.Extent() > 0) {
                            BRep_Builder bb;
                            TopoDS_Compound comp;
                            bb.MakeCompound(comp);
                            bool any = false;
                            for (int si = 1; si <= smap.Extent(); ++si) {
                                TopoDS_Shape member = smap.FindKey(si);
                                ShapeFix_Solid sfx(TopoDS::Solid(member));
                                sfx.SetPrecision(tol);
                                sfx.Perform();
                                const TopoDS_Shape sfixed = sfx.Solid();
                                bb.Add(comp, sfixed.IsNull() ? member : sfixed);
                                any = true;
                            }
                            if (any) out = comp;
                        }
                    }
                } catch (const Standard_Failure&) {
                    // keep the ShapeFix_Shape result
                }
                if (out.IsNull())
                    throw std::runtime_error(type + ": ShapeFix produced an empty shape");

                // --- escalation 1: re-sew ---------------------------------------------
                // Adding every FACE (not Load()ing the shape) is what forces the sewer to
                // re-derive edge sharing from geometry instead of preserving the stored one.
                if (sew_tol > 0.0) {
                    try {
                        BRepBuilderAPI_Sewing sew(sew_tol);
                        sew.SetNonManifoldMode(Standard_False);
                        for (TopExp_Explorer fe(out, TopAbs_FACE); fe.More(); fe.Next())
                            sew.Add(fe.Current());
                        sew.Perform();
                        const TopoDS_Shape sewn = sew.SewedShape();
                        if (!sewn.IsNull()) {
                            const TopoDS_Shape solid = heal_solid_from_shells(sewn);
                            out = solid.IsNull() ? sewn : solid;
                        }
                    } catch (const Standard_Failure&) {
                        // keep the ShapeFix result
                    }
                }

                // --- escalation 2: topological face-orientation flood fill ------------
                int orient_flipped = 0, orient_conflicts = 0;
                if (fix_orientation) {
                    try {
                        const TopoDS_Shape fixed =
                            heal_flood_orientation(out, orient_flipped, orient_conflicts);
                        if (!fixed.IsNull()) out = fixed;
                    } catch (const Standard_Failure&) {
                        orient_flipped = 0;
                        orient_conflicts = 0;
                    }
                }

                // --- escalation 3: rebuild the solid from its faces -------------------
                int rebuild_lumps = 0;
                double rebuild_kept = 0.0, rebuild_discarded = 0.0;
                if (rebuild_solid) {
                    try {
                        const TopoDS_Shape rebuilt = heal_rebuild_solid(
                            out, rebuild_lumps, rebuild_kept, rebuild_discarded, rebuild_fuzzy);
                        if (rebuilt.IsNull())
                            throw std::runtime_error(
                                type + ": rebuild_solid failed — BOPAlgo_MakerVolume could not "
                                       "enclose a volume with this body's faces. The faces do "
                                       "not bound a closed region; fix the import.");
                        out = rebuilt;
                    } catch (const Standard_Failure& ex) {
                        throw std::runtime_error(type + ": rebuild_solid raised in the kernel: " +
                                                 std::string(ex.GetMessageString() ?
                                                             ex.GetMessageString() : "?"));
                    }
                }

                if (unify_faces) {
                    try {
                        ShapeUpgrade_UnifySameDomain uni(out, Standard_True, Standard_True,
                                                         Standard_True);
                        uni.Build();
                        const TopoDS_Shape unified = uni.Shape();
                        if (!unified.IsNull()) out = unified;
                    } catch (const Standard_Failure&) {
                        // keep the un-unified result
                    }
                }
                if (out.IsNull())
                    throw std::runtime_error(type + ": the repair produced an empty shape");

                const std::vector<std::string> after_issues = heal_brep_issues(out);
                const bool after_valid = after_issues.empty();
                const double volume_after = heal_volume(out);
                const double area_after = heal_area(out);
                const std::array<double, 6> bbox_after = heal_bbox(out);
                const int bad_edges_after = heal_bad_edge_count(out);
                const bool volume_reading_trustworthy_after = reading_trustworthy(after_issues);
                const double material_after =
                    material_volume(out, volume_reading_trustworthy_after);

                double bbox_shift = 0.0;
                for (int k = 0; k < 6; ++k)
                    bbox_shift = std::max(bbox_shift, std::abs(bbox_after[k] - bbox_before[k]));
                double diag = 0.0;
                for (int k = 0; k < 3; ++k) {
                    const double d = bbox_before[k + 3] - bbox_before[k];
                    diag += d * d;
                }
                diag = std::sqrt(diag);
                // "The envelope did not move": ten times the LARGEST merge distance the
                // caller authorized (ShapeFix precision, sewing, the rebuild's fuzzy value),
                // floored at 1e-6 mm and at 1e-5 of the part's own diagonal so a metre-scale
                // body is not judged on a micron it never resolved. The fuzzy value belongs in
                // there: asking the intersection to merge geometry within 0.05 mm authorizes
                // movement on that scale, and leaving it out refused body 47's own fix over an
                // 18 micron shift on an 887 mm part.
                const double bbox_budget =
                    std::max({1e-6, 10.0 * std::max({tol, sew_tol, rebuild_fuzzy}),
                              1e-5 * diag});

                auto pct = [](double from, double to) {
                    return std::abs(from) > 1e-12 ? 100.0 * (to - from) / std::abs(from) : 0.0;
                };
                const double volume_change_pct = pct(volume_before, volume_after);
                // The verdict number: material in vs material out, both normalized the same
                // way, so a surviving orientation defect cannot fake a drift and a corrected
                // reading cannot fake a loss.
                const double material_change_pct = pct(material_before, material_after);
                const double area_change_pct = pct(area_before, area_after);
                const double discarded_pct =
                    (rebuild_kept + rebuild_discarded) > 1e-12
                        ? 100.0 * rebuild_discarded / (rebuild_kept + rebuild_discarded)
                        : 0.0;

                json before_j = json::array();
                for (const std::string& s : before_issues) before_j.push_back(s);
                json after_j = json::array();
                for (const std::string& s : after_issues) after_j.push_back(s);
                st.data = json{
                    {"fixed", !before_valid && after_valid},
                    {"before", {{"valid", before_valid},
                                {"issues", before_j},
                                {"bad_edges", bad_edges_before},
                                {"faces", faces_before},
                                {"solids", solids_before_heal},
                                {"area_mm2", area_before},
                                {"bbox", json::array({bbox_before[0], bbox_before[1],
                                                      bbox_before[2], bbox_before[3],
                                                      bbox_before[4], bbox_before[5]})}}},
                    {"after", {{"valid", after_valid},
                               {"issues", after_j},
                               {"bad_edges", bad_edges_after},
                               {"faces", count_of(out, TopAbs_FACE)},
                               {"solids", count_of(out, TopAbs_SOLID)},
                               {"area_mm2", area_after},
                               {"bbox", json::array({bbox_after[0], bbox_after[1],
                                                     bbox_after[2], bbox_after[3],
                                                     bbox_after[4], bbox_after[5]})}}},
                    {"volume_before", volume_before},
                    {"volume_after", volume_after},
                    {"volume_change_pct", volume_change_pct},
                    {"volume_reading_trustworthy_before", volume_reading_trustworthy},
                    {"volume_reading_trustworthy_after", volume_reading_trustworthy_after},
                    // Orientation-normalized volumes: what the budget is actually judged on.
                    {"material_volume_before", material_before},
                    {"material_volume_after", material_after},
                    {"material_change_pct", material_change_pct},
                    {"area_change_pct", area_change_pct},
                    {"bbox_shift_mm", bbox_shift},
                    {"tolerance", tol},
                    {"fix_small_edges", fix_small_edges},
                    {"sew_tolerance_mm", sew_tol > 0.0 ? json(sew_tol) : json(nullptr)},
                    {"fix_orientation", fix_orientation},
                    {"rebuild_solid", rebuild_solid},
                    {"rebuild_fuzzy_mm",
                     rebuild_fuzzy > 0.0 ? json(rebuild_fuzzy) : json(nullptr)},
                    {"unify_faces", unify_faces},
                    {"max_volume_change_pct", max_vol_pct}};
                if (fix_orientation)
                    st.data["orientation"] = json{{"faces_flipped", orient_flipped},
                                                  {"conflicts", orient_conflicts}};
                if (rebuild_solid)
                    st.data["rebuild"] = json{{"lumps", rebuild_lumps},
                                              {"kept_volume_mm3", rebuild_kept},
                                              {"discarded_volume_mm3", rebuild_discarded},
                                              {"discarded_pct", discarded_pct},
                                              // The narrowest face left behind: a strip below
                                              // the mesher's reach is what makes an otherwise
                                              // valid rebuild unmeshable, so it is REPORTED.
                                              {"narrowest_face_mm", heal_narrowest_face(out)}};

                // --- the refusal guard -------------------------------------------------
                // A heal that hands back something worse than it was given is the exact
                // failure this op exists to prevent, so it FAILS the feature: the batch is
                // rolled back and the document is untouched. `st.data` above survives the
                // throw, so the caller still gets every number the verdict was made on.
                auto refuse = [&](const std::string& why) {
                    throw std::runtime_error(
                        type + " REFUSED and the body was left untouched: " + why +
                        " [before: valid=" + (before_valid ? "true" : "false") + ", " +
                        std::to_string(before_issues.size()) + " issue kind(s), " +
                        std::to_string(bad_edges_before) + " bad edges, volume " +
                        std::to_string(volume_before) + " mm^3, area " +
                        std::to_string(area_before) + " mm^2 | after: valid=" +
                        (after_valid ? "true" : "false") + ", " +
                        std::to_string(after_issues.size()) + " issue kind(s), " +
                        std::to_string(bad_edges_after) + " bad edges, volume " +
                        std::to_string(volume_after) + " mm^3, area " +
                        std::to_string(area_after) + " mm^2]");
                };
                if (after_issues.size() > before_issues.size())
                    refuse("the repair left MORE defect kinds than it found (" +
                           std::to_string(before_issues.size()) + " -> " +
                           std::to_string(after_issues.size()) + "; new: " +
                           [&] {
                               std::string s;
                               for (const std::string& a : after_issues)
                                   if (std::find(before_issues.begin(), before_issues.end(), a) ==
                                       before_issues.end())
                                       s += (s.empty() ? "" : ", ") + a;
                               return s;
                           }() +
                           "). Lower `tolerance` — a tolerance above the part's real feature "
                           "size merges geometry instead of repairing it");
                if (bbox_shift > bbox_budget)
                    refuse("the bounding box MOVED by " + std::to_string(bbox_shift) +
                           " mm (budget " + std::to_string(bbox_budget) +
                           " mm) — the repair rebuilt geometry rather than repairing "
                           "topology, so the part is no longer the part");
                if (discarded_pct > max_vol_pct)
                    refuse("rebuild_solid would discard " +
                           std::to_string(rebuild_discarded) + " mm^3 across " +
                           std::to_string(rebuild_lumps > 0 ? rebuild_lumps - 1 : 0) +
                           " secondary lump(s) = " + std::to_string(discarded_pct) +
                           "% of the enclosed volume, over the " +
                           std::to_string(max_vol_pct) + "% budget. The faces enclose "
                           "genuinely disjoint regions; this body needs fixing upstream, "
                           "not here");
                if (std::abs(material_change_pct) > max_vol_pct)
                    refuse("the enclosed MATERIAL changed by " +
                           std::to_string(material_change_pct) + "% (" +
                           std::to_string(material_before) + " -> " +
                           std::to_string(material_after) + " mm^3, budget " +
                           std::to_string(max_vol_pct) +
                           "%) — that is lost or invented material, not a corrected volume "
                           "reading" +
                           (volume_reading_trustworthy
                                ? std::string(" (the input's own reading was already "
                                              "trustworthy: no orientation defect)")
                                : std::string(" (both sides measured with their face "
                                              "orientations flood-corrected first, so a "
                                              "wrong-signed integral cannot explain it)")));
                if (!volume_reading_trustworthy && rebuild_lumps <= 1 &&
                    std::abs(area_change_pct) > max_vol_pct)
                    refuse("the input's signed volume integral was not a volume (it carries an "
                           "orientation defect), so the drift budget falls on surface AREA, "
                           "which changed by " + std::to_string(area_change_pct) +
                           "% (budget " + std::to_string(max_vol_pct) +
                           "%) — an orientation repair must not touch a single surface");

                if (!before_valid && !after_valid) {
                    st.warning =
                        "heal: the repair did not clear every defect — still " +
                        std::to_string(after_issues.size()) + " issue kind(s) (" +
                        after_issues.front() + (after_issues.size() > 1 ? ", …" : "") +
                        "), " + std::to_string(bad_edges_after) + " badly-oriented edge(s) " +
                        "(was " + std::to_string(bad_edges_before) + "). Escalate: " +
                        (!fix_orientation ? "try fix_orientation:true"
                                          : (orient_conflicts > 0 && !rebuild_solid
                                                 ? "the face-orientation graph is self-"
                                                   "contradictory (" +
                                                       std::to_string(orient_conflicts) +
                                                       " conflicts) — try rebuild_solid:true"
                                                 : "or fix the import that produced the body"));
                }
                // A VALID body can still be unusable: at OCCT's own intersection tolerance
                // the rebuild gives every pair of near-coincident input faces its own
                // intersection line, and the leftovers are razor strips. Body 47's rebuild
                // came out BRep-valid carrying two 0.011 mm crescents, and polymesh then
                // refused the whole fill at 8 mm and again at 4.8 — the refusal names a face
                // index and a distance, nothing a caller can act on. So the strip is called
                // out HERE, where the fix is one parameter. Threshold: 1e-4 of the body's own
                // diagonal (0.089 mm on the 887 mm spine, 0.005 mm on a 50 mm bracket) — a
                // face that narrow is an artefact of the rebuild, never a designed feature.
                if (rebuild_solid) {
                    const double narrowest = st.data["rebuild"]["narrowest_face_mm"].get<double>();
                    const double strip_floor = 1e-4 * diag;
                    if (narrowest > 0.0 && narrowest < strip_floor) {
                        const std::string note =
                            "heal: the rebuild left a razor face " +
                            std::to_string(narrowest) + " mm wide (under " +
                            std::to_string(strip_floor) + " mm = 1e-4 of the body's " +
                            std::to_string(diag) + " mm diagonal). The body is BRep-valid but "
                            "no volume mesher can resolve a strip that narrow at any element "
                            "size. Re-run with rebuild_fuzzy_mm around 0.05 so the "
                            "near-coincident faces merge during the intersection instead.";
                        st.warning = st.warning.empty() ? note : st.warning + " " + note;
                    }
                }

                consumed.insert(eff_id);        // the healed body REPLACES the input
                superseded_by[eff_id] = id;     // a later transform/heal composes on `id`
                source_of[id] = eff_id;
                tool_body_of[id] = out;         // patternable/mirrorable downstream
                feature_op_of[id] = "add";
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "hole") {
                // Drill simple / cbore / csink hole(s) into a referenced solid at
                // position(s) on a FACE-SELECTOR-resolved planar face. One tool per
                // position is built, all unioned, then cut from the body in a single
                // boolean (one feature, N holes — SolidWorks Hole Wizard, simplified:
                // no thread tables / cosmetic threads / grid / sketch positions). Port
                // of engine/doc/features/hole.py's geometry core in the id-ref model.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");

                // --- resolve the target planar face (a {"kind":"face"} selector) ---
                auto face_it = feat.find("face");
                if (face_it == feat.end() || !face_it->is_object())
                    throw std::runtime_error(
                        "hole: 'face' selector is required (the planar face to drill into)");
                validate_selector_refs(*face_it, "hole face");  // Bug K4
                TopoDS_Face face;
                // A WORD query names a direction, not one face — world_points may then land
                // on another face of the body facing the same way (hole_world_point). A sid
                // query or a concrete pick names exactly one face and stays strict.
                const bool word_query_face =
                    chudware::doc::is_geom_query(*face_it) && !face_it->contains("sid");
                if (chudware::doc::is_geom_query(*face_it)) {
                    // word query: "drill the top face" -> {"of":"face","side":"top"}.
                    chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(body, *face_it);
                    if (!qp.error.empty()) throw std::runtime_error("hole: " + qp.error);
                    if (qp.faces.empty()) throw std::runtime_error("hole: query matched no face");
                    face = qp.faces.front();
                } else {
                    chudware::doc::FacePicks fp =
                        chudware::doc::resolve_face_picks(body, *face_it, "face");
                    if (!fp.dangling.empty()) {
                        std::string msg = "hole: face selector dangling: ";
                        for (std::size_t k = 0; k < fp.dangling.size(); ++k) {
                            if (k) msg += "; ";
                            msg += fp.dangling[k];
                        }
                        throw std::runtime_error(msg);
                    }
                    if (fp.faces.empty())
                        throw std::runtime_error("hole: target face not resolved");
                    face = fp.faces.front();
                }
                // Bug N1 (defensive) — a resolver can hand back a NULL face; BRepAdaptor_
                // Surface / BRep_Tool::Surface would then dereference it (access violation).
                if (face.IsNull())
                    throw std::runtime_error("hole: resolved face is null");

                BRepAdaptor_Surface surf(face);
                if (surf.GetType() != GeomAbs_Plane)
                    throw std::runtime_error("hole: target face is not planar");
                gp_Pln pln = surf.Plane();

                // --- face frame: see hole_face_frame — origin at the OUTER-boundary area
                // centroid (stable under previously-cut holes; the whole-face centroid is
                // NOT — and gp_Pln::Location() is worse, often a CORNER: a Ø10 hole came
                // out looking like Ø5), axes = the deterministic SketchPlane convention
                // (u = world +X projected, v = n×u) instead of the kernel's arbitrary
                // surface parameterization.
                // OUTWARD normal (geom/face_normal.h: plane-frame handedness + face
                // orientation), so drill_dir points INTO the material (the watch-point: get
                // this wrong and the cut removes nothing — a revolved shaft's x=0 end face has
                // a left-handed plane frame, and Axis() alone drilled it outward into air).
                const gp_Dir nd = ::chudware::geom::plane_outward_normal(face, pln);
                const Eigen::Vector3d normal(nd.X(), nd.Y(), nd.Z());
                const chudware::geom::SketchPlane fframe = hole_face_frame(face, normal);
                const Eigen::Vector3d origin = fframe.origin;
                const Eigen::Vector3d xdir = fframe.uAxis;
                const Eigen::Vector3d ydir = fframe.vAxis;
                const Eigen::Vector3d drill_dir = -normal;

                // --- positions: [u,v] face-local mm pairs, OR world_points: [x,y,z] world
                // mm points on each bore AXIS, projected along the face normal onto the face
                // the drill enters (see hole_world_point: a projection more than
                // kHoleWorldPointNoteMm or a same-normal face substitution becomes the feature
                // warning; an axis that enters no such face is refused by name). The two are
                // mutually exclusive. Neither = one hole at the face centre. `mouths` holds
                // each bore's world point ON its entry face.
                std::vector<Eigen::Vector3d> mouths;
                std::string projection_notes;  // appended to st.warning once the cut lands
                auto pos_it = feat.find("positions");
                auto wpt_it = feat.find("world_points");
                const bool has_pos =
                    pos_it != feat.end() && pos_it->is_array() && !pos_it->empty();
                const bool has_wpt = wpt_it != feat.end() && !wpt_it->is_null();
                if (has_pos && has_wpt)
                    throw std::runtime_error(
                        "hole: give positions (face-local [u, v]) OR world_points (world "
                        "[x, y, z]), not both — they place the same holes two different ways");
                if (has_wpt) {
                    if (!wpt_it->is_array() || wpt_it->empty())
                        throw std::runtime_error(
                            "hole: world_points must be a non-empty list of world points "
                            "[[x, y, z], ...] in mm (got " + wpt_it->dump() + ")");
                    for (std::size_t k = 0; k < wpt_it->size(); ++k) {
                        const HoleWorldPoint hp = hole_world_point(
                            face, fframe, (*wpt_it)[k],
                            "hole: world_points[" + std::to_string(k) + "]", env, true,
                            word_query_face ? &body : nullptr);
                        mouths.push_back(hp.at);
                        if (!hp.note.empty())
                            projection_notes += (projection_notes.empty() ? "" : " ") + hp.note;
                    }
                } else if (has_pos) {
                    for (std::size_t k = 0; k < pos_it->size(); ++k) {
                        const json& p = (*pos_it)[k];
                        if (!p.is_array() || p.size() < 2)
                            throw std::runtime_error(
                                "hole: positions[" + std::to_string(k) + "] must be [u, v]");
                        mouths.push_back(origin + p[0].get<double>() * xdir +
                                         p[1].get<double>() * ydir);
                    }
                } else {
                    mouths.push_back(origin);  // back-compat single hole at face centre
                }

                // --- style. "tap" is a TAPPED hole: the oracle (hole.py) drills a plain
                // bore (the cosmetic thread is metadata, not cut geometry) — so tap maps to
                // the Simple bore. cbore/csink add the mouth pocket/cone.
                const std::string style_s = feat.value("style", std::string("simple"));
                ::geom::HoleStyle style;
                if (style_s == "simple" || style_s == "tap") style = ::geom::HoleStyle::Simple;
                else if (style_s == "cbore")  style = ::geom::HoleStyle::Cbore;
                else if (style_s == "csink")  style = ::geom::HoleStyle::Csink;
                else throw std::runtime_error("hole: unknown style '" + style_s + "'");

                // --- dims. Mirrors hole.py::_resolve_dims for standard=="custom": the bore
                // diameter is params.diameter; cbore/csink defaults derive from it (cbore_d =
                // diameter*1.8, csink_d = diameter*2.0) when not explicitly provided. The
                // canonical cbore_d/csink_d keys are read if present; the audit's *_dia keys
                // are NOT (the oracle ignores them too), so both engines use the defaults.
                ::geom::HoleDims dims;
                if (!feat.contains("diameter") || feat.at("diameter").is_null())
                    throw std::runtime_error(
                        "hole feature needs a bore 'diameter' in mm — e.g. "
                        "{\"type\":\"hole\",\"diameter\":3.4,...} for an M3 clearance hole "
                        "(synonyms dia/d/bore_diameter/radius are also accepted). Got params: " +
                        json_key_list(feat));
                dims.diameter = num_or_expr(feat.at("diameter"), env);
                if (!(dims.diameter > 0.0))
                    throw std::runtime_error("hole: 'diameter' must be > 0 (got " +
                                             feat.at("diameter").dump() + ")");
                if (style == ::geom::HoleStyle::Cbore) {
                    dims.cbore_d = feat.contains("cbore_d")
                        ? num_or_expr(feat.at("cbore_d"), env) : dims.diameter * 1.8;
                    dims.cbore_depth = feat.contains("cbore_depth")
                        ? num_or_expr(feat.at("cbore_depth"), env) : dims.diameter;
                    if (!(dims.cbore_d > dims.diameter))
                        throw std::runtime_error("hole: cbore_d must be larger than diameter");
                    if (!(dims.cbore_depth > 0.0))
                        throw std::runtime_error("hole: cbore_depth must be > 0");
                } else if (style == ::geom::HoleStyle::Csink) {
                    dims.csink_d = feat.contains("csink_d")
                        ? num_or_expr(feat.at("csink_d"), env) : dims.diameter * 2.0;
                    dims.csink_angle_deg = feat.contains("csink_angle")
                        ? num_or_expr(feat.at("csink_angle"), env) : 90.0;
                    if (!(dims.csink_d > dims.diameter))
                        throw std::runtime_error("hole: csink_d must be larger than diameter");
                    if (!(dims.csink_angle_deg > 0.0 && dims.csink_angle_deg < 180.0))
                        throw std::runtime_error("hole: csink_angle must be in (0, 180)");
                }

                // --- end condition -> shaft depth ---
                const std::string end = feat.value("end", std::string("through_all"));
                double depth;
                if (end == "through_all") {
                    // Over-length so the bore fully penetrates; the cut only removes the
                    // in-body part, so over-length is harmless.
                    depth = chudware::doc::bbox_diag(body) * 1.1;
                } else if (end == "blind") {
                    depth = num_or_expr(feat.at("depth"), env);
                    if (!(depth > 0.0))
                        throw std::runtime_error("hole: blind depth must be > 0");
                } else {
                    throw std::runtime_error("hole: unknown end '" + end + "'");
                }

                // --- build every position's tool, union into one, then cut ---
                const double lift = 0.05;  // tool mouth sits just above the face
                ::geom::HoleDims tool_dims = dims;
                tool_dims.depth = depth + lift;
                // Match hole.py: the cbore pocket / csink cone are over-driven by `lift`
                // too (cb_h = cbore_depth + lift; cone_h gets +lift inside make_hole_tool
                // via cbore_depth/csink_lift), so the mouth pieces clear the face cleanly.
                if (style == ::geom::HoleStyle::Cbore) tool_dims.cbore_depth = dims.cbore_depth + lift;
                tool_dims.csink_lift = lift;

                // Build every position's tool, then union them in ONE BOPDS pass (fuse_many)
                // rather than folding pairwise fuse over a growing union (~O(N²) for a
                // multi-position hole). A single position skips the boolean entirely.
                std::vector<TopoDS_Shape> tools;
                tools.reserve(mouths.size());
                for (const Eigen::Vector3d& world : mouths) {
                    Eigen::Vector3d lifted = world + lift * normal;  // just above the face
                    TopoDS_Shape t =
                        ::geom::make_hole_tool(lifted, drill_dir, style, tool_dims);
                    if (t.IsNull())
                        throw std::runtime_error("hole: failed to build a hole tool");
                    tools.push_back(t);
                }
                if (tools.empty())
                    throw std::runtime_error("hole: no positions to drill");
                TopoDS_Shape tool_union = ::geom::fuse_many(
                    tools.front(), std::vector<TopoDS_Shape>(tools.begin() + 1, tools.end()));
                if (tool_union.IsNull())
                    throw std::runtime_error("hole: tool union (fuse) failed");

                TopoDS_Shape out = ::geom::cut(body, tool_union);
                if (out.IsNull())
                    throw std::runtime_error("hole: boolean cut produced an empty shape");

                // MATERIAL-REMOVED GUARD (mirror hole.py): a cut that removed nothing
                // means the positions miss the face/body or the drill went the wrong
                // way — surface it as a clear per-feature error, not a silent no-op.
                GProp_GProps gp_before, gp_after;
                BRepGProp::VolumeProperties(body, gp_before);
                BRepGProp::VolumeProperties(out, gp_after);
                if (std::abs(gp_after.Mass() - gp_before.Mass()) < 1e-9)
                    throw std::runtime_error(
                        "hole removed no material (positions may fall outside the face/body)");
                require_cut_removed_material("hole", gp_before.Mass(), gp_after.Mass());

                // BLIND-DEPTH-EXCEEDS-STOCK warning (Bug B1): a blind hole whose requested
                // depth runs PAST the far face of the stock is silently clamped by the
                // boolean (the cut only removes the in-body part). Detect it by drilling a
                // far-overlength "through" tool at the SAME positions/style and comparing
                // the material removed: if the blind cut already removed as much as a
                // full-penetration cut, the blind depth met or exceeded the available stock
                // and was effectively clamped — surface it as a loud warning rather than
                // silently producing a through hole. (Warning, not error: a blind depth that
                // happens to equal the stock exactly is legitimate, so we never reject it.)
                if (end == "blind") {
                    ::geom::HoleDims thru_dims = tool_dims;
                    thru_dims.depth = chudware::doc::bbox_diag(body) * 1.1 + lift;
                    if (style == ::geom::HoleStyle::Cbore)
                        thru_dims.cbore_depth = dims.cbore_depth + lift;
                    TopoDS_Shape thru_union;
                    bool thru_ok = true;
                    std::vector<TopoDS_Shape> thru_tools;
                    thru_tools.reserve(mouths.size());
                    for (const Eigen::Vector3d& world : mouths) {
                        Eigen::Vector3d lifted = world + lift * normal;
                        TopoDS_Shape t =
                            ::geom::make_hole_tool(lifted, drill_dir, style, thru_dims);
                        if (t.IsNull()) { thru_ok = false; break; }
                        thru_tools.push_back(t);
                    }
                    if (thru_ok && !thru_tools.empty()) {
                        thru_union = ::geom::fuse_many(
                            thru_tools.front(),
                            std::vector<TopoDS_Shape>(thru_tools.begin() + 1, thru_tools.end()));
                        if (thru_union.IsNull()) thru_ok = false;
                    }
                    if (thru_ok && !thru_union.IsNull()) {
                        TopoDS_Shape thru_out = ::geom::cut(body, thru_union);
                        if (!thru_out.IsNull()) {
                            GProp_GProps gp_thru;
                            BRepGProp::VolumeProperties(thru_out, gp_thru);
                            // Both cuts removed the same material => the blind bore already
                            // ran clean through the stock (clamped).
                            const double removed_blind = gp_before.Mass() - gp_after.Mass();
                            const double removed_thru = gp_before.Mass() - gp_thru.Mass();
                            if (removed_thru > 1e-9 &&
                                std::abs(removed_blind - removed_thru) < 1e-6 * removed_thru) {
                                char buf[200];
                                std::snprintf(buf, sizeof(buf),
                                    "hole: blind depth %.4g exceeds the available stock at the "
                                    "hole — it was clamped to a through hole. Use "
                                    "end:through_all or reduce the depth.", depth);
                                st.warning = buf;
                            }
                        }
                    }
                }
                if (!projection_notes.empty())
                    st.warning = st.warning.empty() ? projection_notes
                                                    : st.warning + " " + projection_notes;

                // The drilled solid REPLACES the body (SolidWorks-style): the bored
                // solid is the body's new current state in the tree.
                consumed.insert(body_id);
                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
                // Patternable tool: the union of drilled hole tools (instance-0 pose),
                // op "cut" — a later pattern/mirror re-drills it per instance.
                tool_body_of[id] = tool_union;
                feature_op_of[id] = "cut";
                record_tool(id, "cut", tool_union);
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "bolt_circle") {
                // INTENT MACRO: a ring of holes (bolt circle) in ONE feature. The model
                // gives a face (default: the top face), the bolt-circle diameter `pcd`, and
                // a `count`; we drill N evenly-spaced holes around the face CENTROID — no
                // per-hole trig for the model to get wrong. Reuses the Hole-Wizard tool
                // builder; style/dims mirror `hole` exactly.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");

                // Target planar face (word query OR clicked selector); default = top face.
                const json face_ref = (feat.contains("face") && !feat.at("face").is_null())
                                          ? feat.at("face")
                                          : json{{"of", "face"}, {"side", "top"}};
                validate_selector_refs(face_ref, "bolt_circle face");  // Bug K4
                TopoDS_Face face;
                if (chudware::doc::is_geom_query(face_ref)) {
                    chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(body, face_ref);
                    if (!qp.error.empty()) throw std::runtime_error("bolt_circle: " + qp.error);
                    if (qp.faces.empty())
                        throw std::runtime_error("bolt_circle: query matched no face");
                    face = qp.faces.front();
                } else {
                    chudware::doc::FacePicks fp =
                        chudware::doc::resolve_face_picks(body, face_ref, "face");
                    if (fp.faces.empty())
                        throw std::runtime_error("bolt_circle: target face not resolved");
                    face = fp.faces.front();
                }
                // Bug N1 (defensive) — guard a null face before the kernel dereferences it.
                if (face.IsNull())
                    throw std::runtime_error("bolt_circle: resolved face is null");
                BRepAdaptor_Surface bc_surf(face);
                if (bc_surf.GetType() != GeomAbs_Plane)
                    throw std::runtime_error("bolt_circle: target face is not planar");

                // Frame centred on the OUTER-boundary area centroid with deterministic
                // axes — see hole_face_frame (W2): stable when the face already carries
                // holes, and the same u/v convention as the `hole` feature.
                Eigen::Vector3d normal = src_face_normal(face);
                chudware::geom::SketchPlane fr = hole_face_frame(face, normal);
                const Eigen::Vector3d drill_dir = -normal;
                // Ring centre: the face centre, or `world_center` [x,y,z] (world mm) — a point
                // on the ring's central axis, projected along the face normal exactly like a
                // hole `world_points` entry (noted when projected; refused when it misses).
                Eigen::Vector3d ring_c = fr.origin;
                if (feat.contains("world_center") && !feat.at("world_center").is_null()) {
                    const HoleWorldPoint hp = hole_world_point(
                        face, fr, feat.at("world_center"), "bolt_circle: world_center", env,
                        false, nullptr);
                    ring_c = hp.at;
                    if (!hp.note.empty())
                        st.warning = st.warning.empty() ? hp.note : st.warning + " " + hp.note;
                }

                // Ring geometry.
                const int count = static_cast<int>(std::llround(num_or_expr(feat.at("count"), env)));
                if (count < 1) throw std::runtime_error("bolt_circle: count must be >= 1");
                // INSTANCE CAP (Bug K1): reject a runaway ring count before the build.
                if (static_cast<long long>(count) > kPatternInstanceLimit)
                    throw std::runtime_error(
                        "pattern: " + std::to_string(count) +
                        " instances exceeds the limit (" +
                        std::to_string(kPatternInstanceLimit) + "); reduce the count");
                double pcd;
                if (feat.contains("pcd")) pcd = num_or_expr(feat.at("pcd"), env);
                else if (feat.contains("bolt_circle_d")) pcd = num_or_expr(feat.at("bolt_circle_d"), env);
                else throw std::runtime_error("bolt_circle: 'pcd' (bolt-circle diameter) is required");
                if (!(pcd > 0.0)) throw std::runtime_error("bolt_circle: pcd must be > 0");
                const double ring_r = pcd / 2.0;
                const double PI = std::acos(-1.0);
                const double a0 = feat.contains("start_angle")
                                      ? num_or_expr(feat.at("start_angle"), env) * PI / 180.0 : 0.0;

                // Hole style/dims (same contract as `hole`).
                const std::string style_s = feat.value("style", std::string("simple"));
                ::geom::HoleStyle style;
                if (style_s == "simple" || style_s == "tap") style = ::geom::HoleStyle::Simple;
                else if (style_s == "cbore") style = ::geom::HoleStyle::Cbore;
                else if (style_s == "csink") style = ::geom::HoleStyle::Csink;
                else throw std::runtime_error("bolt_circle: unknown style '" + style_s + "'");
                ::geom::HoleDims dims;
                if (!feat.contains("diameter") || feat.at("diameter").is_null())
                    throw std::runtime_error(
                        "bolt_circle feature needs the hole 'diameter' in mm — e.g. "
                        "{\"type\":\"bolt_circle\",\"diameter\":3.4,\"pcd\":...,\"count\":...} "
                        "(synonyms dia/d/bore_diameter/radius are also accepted). Got params: " +
                        json_key_list(feat));
                dims.diameter = num_or_expr(feat.at("diameter"), env);
                if (!(dims.diameter > 0.0)) throw std::runtime_error("bolt_circle: 'diameter' must be > 0");
                if (style == ::geom::HoleStyle::Cbore) {
                    dims.cbore_d = feat.contains("cbore_d")
                        ? num_or_expr(feat.at("cbore_d"), env) : dims.diameter * 1.8;
                    dims.cbore_depth = feat.contains("cbore_depth")
                        ? num_or_expr(feat.at("cbore_depth"), env) : dims.diameter;
                } else if (style == ::geom::HoleStyle::Csink) {
                    dims.csink_d = feat.contains("csink_d")
                        ? num_or_expr(feat.at("csink_d"), env) : dims.diameter * 2.0;
                    dims.csink_angle_deg = feat.contains("csink_angle")
                        ? num_or_expr(feat.at("csink_angle"), env) : 90.0;
                }
                const std::string end = feat.value("end", std::string("through_all"));
                double depth;
                if (end == "through_all") depth = chudware::doc::bbox_diag(body) * 1.1;
                else if (end == "blind") {
                    depth = num_or_expr(feat.at("depth"), env);
                    if (!(depth > 0.0)) throw std::runtime_error("bolt_circle: blind depth must be > 0");
                } else throw std::runtime_error("bolt_circle: unknown end '" + end + "'");

                const double lift = 0.05;  // tool mouth just above the face (matches hole)
                ::geom::HoleDims tool_dims = dims;
                tool_dims.depth = depth + lift;
                if (style == ::geom::HoleStyle::Cbore) tool_dims.cbore_depth = dims.cbore_depth + lift;
                tool_dims.csink_lift = lift;

                // Build every hole tool first, then union them in ONE BOPDS pass (fuse_many)
                // instead of folding pairwise fuse over a growing union (~O(N²) for N holes —
                // this is the bolt-ring hot path). tool_union is kept (not just cut straight
                // through) because it's stored as the nesting tool body an outer pattern reuses.
                std::vector<TopoDS_Shape> tools;
                tools.reserve((size_t)count);
                for (int k = 0; k < count; ++k) {
                    const double ang = a0 + 2.0 * PI * static_cast<double>(k) / static_cast<double>(count);
                    Eigen::Vector3d world = ring_c + (ring_r * std::cos(ang)) * fr.uAxis +
                                                     (ring_r * std::sin(ang)) * fr.vAxis;
                    Eigen::Vector3d lifted = world + lift * normal;
                    TopoDS_Shape t = ::geom::make_hole_tool(lifted, drill_dir, style, tool_dims);
                    if (t.IsNull()) throw std::runtime_error("bolt_circle: failed to build a hole tool");
                    tools.push_back(t);
                }
                TopoDS_Shape tool_union = ::geom::fuse_many(
                    tools.front(), std::vector<TopoDS_Shape>(tools.begin() + 1, tools.end()));
                if (tool_union.IsNull()) throw std::runtime_error("bolt_circle: tool union (fuse) failed");
                TopoDS_Shape out = ::geom::cut(body, tool_union);
                if (out.IsNull())
                    throw std::runtime_error("bolt_circle: boolean cut produced an empty shape");
                GProp_GProps gp_before, gp_after;
                BRepGProp::VolumeProperties(body, gp_before);
                BRepGProp::VolumeProperties(out, gp_after);
                if (std::abs(gp_after.Mass() - gp_before.Mass()) < 1e-9)
                    throw std::runtime_error(
                        "bolt_circle removed no material (the ring may fall outside the face)");
                require_cut_removed_material("bolt_circle", gp_before.Mass(), gp_after.Mass());
                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{out};
                tool_body_of[id] = tool_union;
                feature_op_of[id] = "cut";
                record_tool(id, "cut", tool_union);
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "shell") {
                // Hollow the current body to a wall thickness, opening any `remove`
                // faces (face selectors). Port of engine/doc/features/dress.py shell.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("shell: no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double t = num_or_expr(feat.at("thickness"), env);
                if (!(t > 0.0))
                    throw std::runtime_error("shell thickness must be > 0");

                std::vector<TopoDS_Face> remove;
                auto rm = feat.find("remove");
                if (rm != feat.end() && !rm->is_null())
                    validate_selector_refs(*rm, "shell remove");  // Bug K4
                if (rm != feat.end() && chudware::doc::is_geom_query(*rm)) {
                    // Query the open face(s) by name: {"of":"face","side":"+z"} = open top.
                    chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(body, *rm);
                    if (!qp.error.empty())
                        throw std::runtime_error("shell: " + qp.error);
                    if (qp.faces.empty())
                        throw std::runtime_error("shell: remove face query matched no faces");
                    remove = std::move(qp.faces);
                } else if (rm != feat.end() &&
                    (rm->is_object() || (rm->is_array() && !rm->empty()))) {
                    chudware::doc::FacePicks fp =
                        chudware::doc::resolve_face_picks(body, *rm, "remove");
                    if (!fp.dangling.empty()) {
                        std::string msg = "shell: remove selector dangling: ";
                        for (std::size_t k = 0; k < fp.dangling.size(); ++k) {
                            if (k) msg += "; ";
                            msg += fp.dangling[k];
                        }
                        throw std::runtime_error(msg);
                    }
                    remove = std::move(fp.faces);
                }

                // CLOSED shell = no faces opened (a fully-sealed hollow: a hollow ball, a
                // sealed enclosure). Routes through geom::shell_solid's offset+cut path and
                // needs a different volume guard below (a sealed wall is SMALLER than the
                // original solid even when thickened OUTWARD — it's just the wall, no core).
                const bool closed_shell = remove.empty();
                // REMOVE-ALL-FACES guard (Bug A3): opening EVERY face leaves no skin to
                // hollow — OCCT's BRepOffsetAPI_MakeThickSolid silently hands back the
                // ORIGINAL solid (ok=true, geometry unchanged). Count the body's faces and
                // reject up front when the remove-set covers them all.
                if (!remove.empty()) {
                    int n_body_faces = 0;
                    for (TopExp_Explorer fx(body, TopAbs_FACE); fx.More(); fx.Next())
                        ++n_body_faces;
                    if (static_cast<int>(remove.size()) >= n_body_faces)
                        throw std::runtime_error(
                            "shell: cannot remove all faces (no skin would remain to "
                            "hollow) — leave at least one face closed");
                }

                const bool outward = feat.value("outward", false);
                // Capture the input volume so we can prove the shell actually hollowed the
                // part. An impossible shell (thickness >= the wall) and the remove-all case
                // both make BRepOffsetAPI_MakeThickSolid return the ORIGINAL shape (a valid
                // solid) with ok=true — the volume is the tell-tale (Bug A2/A3).
                GProp_GProps gp_in;
                BRepGProp::VolumeProperties(body, gp_in);
                const double vol_in = gp_in.Mass();

                std::string shell_diag;
                TopoDS_Shape out = ::geom::shell_solid(body, t, outward, remove, &shell_diag);
                if (out.IsNull()) {
                    // shell_solid walked every join/tolerance strategy and accepted none
                    // (each result must be BRepCheck-valid and watertight there — the open-
                    // shell commit of Bug S1/S2 can no longer reach this branch), so name the
                    // kernel's ACTUAL reason instead of a guess (Bug L-1). A freeform skin
                    // keeps the actionable "build it into the profile" refusal; a revolved /
                    // swept body whose offset validates never gets here.
                    if (::geom::has_freeform_faces(body))
                        throw std::runtime_error(
                            "shell is not supported on this lofted / swept / revolved "
                            "(B-spline / freeform) body — " + shell_diag + ". Build the "
                            "wall thickness INTO the profile instead: sketch a thin closed "
                            "wall cross-section (offset inner + outer curves) and "
                            "loft/revolve THAT, or loft a second set of sections inset by "
                            "the wall and cut it, rather than shelling a solid one.");
                    throw std::runtime_error("shell produced an empty shape — " + shell_diag +
                                             " — reduce the thickness or open a face");
                }
                // A VALID inward shell ALWAYS reduces the solid volume (it carves a
                // cavity); a valid OUTWARD shell grows it. Either way the volume must
                // CHANGE — when it doesn't, BRepOffsetAPI_MakeThickSolid handed back the
                // ORIGINAL solid because the wall is too thin for the requested thickness
                // (or every face was opened), and the "shell" is a silent no-op (Bug
                // A2/A3). The check is volume-essentially-unchanged within a tiny relative
                // epsilon so a real, material-moving shell is never wrongly rejected.
                {
                    GProp_GProps gp_out;
                    BRepGProp::VolumeProperties(out, gp_out);
                    const double vol_out = gp_out.Mass();
                    const double eps = 1e-6 * std::max(1.0, std::abs(vol_in));
                    // Inward (default): the result must be SMALLER than the input. Outward:
                    // it must be LARGER. Both reduce to "the volume essentially changed".
                    const bool reduced = vol_out < vol_in - eps;
                    const bool grew = vol_out > vol_in + eps;
                    // A CLOSED shell (offset+cut path) always yields just the sealed WALL —
                    // smaller than the original solid for BOTH inward and outward (outward
                    // grows the outer surface but the result is still the wall, not the full
                    // core), so the "grew" test never applies; it must simply be a real,
                    // positive, reduced-volume wall (a 40^3 box -> 17344, not the 64000 solid).
                    const bool ok_change = closed_shell ? reduced : (outward ? grew : reduced);
                    if (!(vol_out > eps) || !ok_change) {
                        char buf[200];
                        std::snprintf(buf, sizeof(buf),
                            "shell: thickness %.4g mm too large for the wall (would consume "
                            "the part) — reduce the thickness", t);
                        throw std::runtime_error(buf);
                    }
                }
                consumed.insert(body_id);
                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "draft") {
                const std::string draft_mode = feat.value("mode", std::string());
                if (!draft_mode.empty() && draft_mode != "neutral_plane")
                    throw std::runtime_error("draft mode '" + draft_mode + "' is not supported");
                // Taper the selected faces about a neutral plane (datum or face) by an
                // angle (degrees). Port of engine/doc/features/draft.py neutral_plane mode.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("draft: no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double angle = radians(num_or_expr(feat.at("angle"), env));
                if (!(std::abs(angle) > 0.0))
                    throw std::runtime_error("draft angle must be non-zero");

                // Neutral plane: a reserved datum (normalize set plane_point/normal) or
                // a face selector under "neutral".
                Eigen::Vector3d npt, nnl;
                if (feat.contains("plane_point") && feat.contains("plane_normal")) {
                    npt = req_vec3(feat, "plane_point");
                    nnl = req_vec3(feat, "plane_normal");
                } else {
                    auto nit = feat.find("neutral");
                    if (nit == feat.end() || !nit->is_object())
                        throw std::runtime_error("draft: a 'neutral' plane (datum or face) is required");
                    // Resolve the neutral face through the SAME dual path the draft FACES
                    // use below (Bug A1): a plain-English geometry QUERY
                    // ({"of":"face","side":"bottom"}) goes through resolve_geom_query; a
                    // concrete pick selector goes through resolve_face_picks. Previously the
                    // neutral slot sent EVERY payload to resolve_face_picks, so a face query
                    // always dangled ("neutral face not resolved") even though the identical
                    // query resolves via doc_query and via the faces slot. (Bug A1, neutral.)
                    TopoDS_Face neutral_face;
                    if (chudware::doc::is_geom_query(*nit)) {
                        chudware::doc::QueryPicks nq =
                            chudware::doc::resolve_geom_query(body, *nit);
                        if (!nq.error.empty())
                            throw std::runtime_error("draft: neutral " + nq.error);
                        if (nq.faces.empty())
                            throw std::runtime_error("draft: neutral face query matched no faces");
                        neutral_face = nq.faces[0];
                    } else {
                        chudware::doc::FacePicks fp =
                            chudware::doc::resolve_face_picks(body, *nit, "neutral");
                        if (!fp.dangling.empty() || fp.faces.empty())
                            throw std::runtime_error("draft: neutral face not resolved");
                        neutral_face = fp.faces[0];
                    }
                    BRepAdaptor_Surface surf(neutral_face);
                    if (surf.GetType() != GeomAbs_Plane)
                        throw std::runtime_error("draft: neutral face is not planar");
                    gp_Pln pln = surf.Plane();
                    gp_Pnt loc = pln.Location();
                    gp_Dir nd = pln.Axis().Direction();
                    npt = Eigen::Vector3d(loc.X(), loc.Y(), loc.Z());
                    nnl = Eigen::Vector3d(nd.X(), nd.Y(), nd.Z());
                }
                // Pull defaults to the neutral normal (SolidWorks default).
                Eigen::Vector3d pull = opt_vec3(feat, "pull_dir").value_or(nnl);

                auto fit = feat.find("faces");
                if (fit == feat.end() ||
                    !(fit->is_object() || (fit->is_array() && !fit->empty())))
                    throw std::runtime_error("draft: 'faces' selector(s) required");
                // Resolve the draft faces through the SAME path shell's `remove` uses
                // (above): a plain-English geometry QUERY ({"of":"face","side":"front"})
                // goes through resolve_geom_query, ELSE concrete pick selectors go through
                // resolve_face_picks. Previously draft sent EVERY payload — query or not —
                // straight to resolve_face_picks, so a face QUERY always dangled ("no
                // candidates of matching kind") even though the identical query resolves
                // via query_geometry and via fillet's refs.edges. (Bug A1.)
                std::vector<TopoDS_Face> draft_faces;
                if (chudware::doc::is_geom_query(*fit)) {
                    chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(body, *fit);
                    if (!qp.error.empty())
                        throw std::runtime_error("draft: " + qp.error);
                    if (qp.faces.empty())
                        throw std::runtime_error("draft: faces query matched no faces");
                    draft_faces = std::move(qp.faces);
                } else {
                    chudware::doc::FacePicks fp =
                        chudware::doc::resolve_face_picks(body, *fit, "faces");
                    if (!fp.dangling.empty()) {
                        std::string msg = "draft: faces selector dangling: ";
                        for (std::size_t k = 0; k < fp.dangling.size(); ++k) {
                            if (k) msg += "; ";
                            msg += fp.dangling[k];
                        }
                        throw std::runtime_error(msg);
                    }
                    if (fp.faces.empty())
                        throw std::runtime_error("draft: no faces resolved");
                    draft_faces = std::move(fp.faces);
                }

                std::string draft_diag;
                TopoDS_Shape out =
                    ::geom::draft_faces(body, draft_faces, npt, nnl, pull, angle, &draft_diag);
                if (out.IsNull())
                    throw std::runtime_error(
                        "draft: " + (draft_diag.empty()
                                         ? std::string("the taper could not be applied")
                                         : draft_diag));
                consumed.insert(body_id);
                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "sm_base_flange") {
                // Base flange — extrude a sketch region by `thickness` to make the
                // base sheet. Establishes the part as sheet metal (stores thickness /
                // k-factor / default bend radius so later flanges inherit them). Port
                // of engine/doc/features/sheetmetal.py::compile_sm_base_flange.
                auto sm_pit = feat.find("profile");
                if (sm_pit == feat.end())
                    throw std::runtime_error("missing string ref 'profile'");
                const std::string prof = profile_sketch_id(*sm_pit);
                if (prof.empty())
                    throw std::runtime_error("missing string ref 'profile'");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                std::vector<TopoDS_Face> sm_faces = resolve_profile_faces(results, feat);
                if (sm_faces.empty())
                    throw std::runtime_error("sm_base_flange: profile sketch has no closed face");

                const double thickness = num_or_expr(feat.at("thickness"), env);
                const double k_factor = feat.contains("k_factor")
                    ? num_or_expr(feat.at("k_factor"), env) : 0.45;
                const double default_bend_radius = feat.contains("default_bend_radius")
                    ? num_or_expr(feat.at("default_bend_radius"), env) : thickness;
                if (!(thickness > 0.0))
                    throw std::runtime_error("sm_base_flange: thickness must be > 0");
                if (!(k_factor >= 0.0 && k_factor <= 1.0))
                    throw std::runtime_error("sm_base_flange: k_factor must be in 0..1");

                // Extrude each region face along its plane normal by thickness, union.
                TopoDS_Shape sheet;
                for (const TopoDS_Face& fc : sm_faces) {
                    TopoDS_Shape piece =
                        ::geom::extrude_face(fc, src.plane.normal, thickness, false);
                    if (piece.IsNull())
                        throw std::runtime_error("sm_base_flange: extrude failed");
                    sheet = sheet.IsNull() ? piece : ::geom::fuse(sheet, piece);
                    if (sheet.IsNull())
                        throw std::runtime_error("sm_base_flange: region fuse failed");
                }
                if (sheet.IsNull())
                    throw std::runtime_error("sm_base_flange: produced no solid");

                sheet_ctx.active = true;
                sheet_ctx.thickness = thickness;
                sheet_ctx.k_factor = k_factor;
                sheet_ctx.default_bend_radius = default_bend_radius;

                results[id] = SolidResult{sheet};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "sm_edge_flange" || type == "sm_hem" ||
                       type == "sm_jog") {
                // Bent walls off a straight bend edge of the sheet's thickness face.
                // Each resolves the SAME bend-edge frame (sm_bend_frame) and builds a
                // true bend+wall (edge flange) or constant-thickness strip (hem / jog)
                // solid, fused to the running sheet, then appends a bend record for
                // the flat pattern. Ports of compile_sm_edge_flange / _hem / _jog.
                if (!sheet_ctx.active)
                    throw std::runtime_error(type + ": part is not sheet metal — add a base flange first");
                const std::string body_id = current_solid_id();
                if (body_id.empty())
                    throw std::runtime_error(type + ": no sheet body — add a base flange first");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double thickness = sheet_ctx.thickness;
                const double k_default = sheet_ctx.k_factor;
                const double radius_default = sheet_ctx.default_bend_radius;

                // Resolve the single straight bend edge (reuse the dress edge picker).
                auto edge_it = feat.find("edge");
                if (edge_it == feat.end())
                    throw std::runtime_error(type + ": refs.edge is required (the bend line)");
                chudware::doc::EdgePicks picks =
                    chudware::doc::resolve_edge_picks(body, *edge_it);
                if (!picks.dangling.empty()) {
                    std::string msg = type + ": edge selector dangling: ";
                    for (std::size_t k = 0; k < picks.dangling.size(); ++k) {
                        if (k) msg += "; ";
                        msg += picks.dangling[k];
                    }
                    throw std::runtime_error(msg);
                }
                if (picks.edges.empty())
                    throw std::runtime_error(type + ": bend edge not resolved");

                ::geom::BendFrame fr =
                    ::geom::sm_bend_frame(body, picks.edges[0], thickness);
                if (!fr.ok) throw std::runtime_error(fr.error);

                const bool flip = feat.value("flip", false);
                Eigen::Vector3d fold_dir = fr.n_broad * (flip ? -1.0 : 1.0);
                fold_dir.normalize();

                TopoDS_Shape tool;
                double rec_angle = 0.0, rec_radius = 0.0, rec_wall = 0.0;
                std::string rec_position = "material_inside";
                // A jog appends TWO bend records (the up bend + the back bend).
                std::vector<::geom::BendRecord> new_recs;

                if (type == "sm_edge_flange") {
                    const double angle_deg = feat.contains("angle")
                        ? num_or_expr(feat.at("angle"), env) : 90.0;
                    const double wall = feat.contains("length")
                        ? num_or_expr(feat.at("length"), env) : 20.0;
                    const double inner_radius = feat.contains("radius")
                        ? num_or_expr(feat.at("radius"), env) : radius_default;
                    const double k_factor = feat.contains("k_factor")
                        ? num_or_expr(feat.at("k_factor"), env) : k_default;
                    const std::string position = feat.value("position", std::string("material_inside"));
                    if (!(inner_radius > 0.0))
                        throw std::runtime_error("sm_edge_flange: bend radius must be > 0");
                    if (!(wall > 0.0))
                        throw std::runtime_error("sm_edge_flange: flange length must be > 0");
                    if (!(std::abs(angle_deg) >= 1e-6 && std::abs(angle_deg) < 180.0))
                        throw std::runtime_error("sm_edge_flange: angle must be in (0, 180) degrees");

                    tool = ::geom::sm_edge_flange_solid(
                        fr.p_start, fr.e_dir, fr.length_dir, fold_dir, fr.edge_length,
                        thickness, inner_radius, angle_deg, wall, position);
                    rec_angle = angle_deg; rec_radius = inner_radius; rec_wall = wall;
                    rec_position = position;
                    ::geom::BendRecord rec;
                    rec.angle = angle_deg; rec.radius = inner_radius;
                    rec.thickness = thickness;
                    rec.bend_allowance = ::geom::sm_bend_allowance(angle_deg, inner_radius,
                                                                   thickness, k_factor);
                    rec.wall_length = wall; rec.position = position;
                    rec.p_start = fr.p_start; rec.p_end = fr.p_end;
                    rec.e_dir = fr.e_dir; rec.length_dir = fr.length_dir;
                    new_recs.push_back(rec);

                } else if (type == "sm_hem") {
                    const std::string htype = feat.value("hem_type", std::string("closed"));
                    if (htype != "closed" && htype != "open" && htype != "teardrop")
                        throw std::runtime_error("sm_hem: type must be closed|open|teardrop");
                    double length = feat.contains("length")
                        ? num_or_expr(feat.at("length"), env) : 5.0 * thickness;
                    const double radius_default_hem =
                        (htype == "closed") ? (thickness / 2.0) : thickness;
                    const double inner_radius = feat.contains("radius")
                        ? num_or_expr(feat.at("radius"), env) : radius_default_hem;
                    const double k_factor = feat.contains("k_factor")
                        ? num_or_expr(feat.at("k_factor"), env) : k_default;
                    if (!(inner_radius > 0.0))
                        throw std::runtime_error("sm_hem: bend radius must be > 0");
                    length = std::max(length, 1.5 * thickness);

                    const double bend_angle = (htype == "teardrop") ? 235.0 : 180.0;
                    const double Rc = inner_radius + thickness / 2.0;
                    const double start_x = -(inner_radius + thickness);
                    const double start_y = -thickness / 2.0;
                    const double pre_run = inner_radius + thickness;
                    std::vector<::geom::StripOp> ops = {
                        {::geom::StripOp::Run, pre_run, 0.0, true},
                        {::geom::StripOp::Bend, bend_angle, Rc, true},
                        {::geom::StripOp::Run, length, 0.0, true}};
                    tool = ::geom::sm_strip_from_centerline(
                        fr.p_start, fr.e_dir, fr.length_dir, fold_dir, fr.edge_length,
                        thickness, ops, start_x, start_y);
                    rec_angle = bend_angle; rec_radius = inner_radius; rec_wall = length;
                    ::geom::BendRecord rec;
                    rec.angle = bend_angle; rec.radius = inner_radius;
                    rec.thickness = thickness;
                    rec.bend_allowance = ::geom::sm_bend_allowance(bend_angle, inner_radius,
                                                                   thickness, k_factor);
                    rec.wall_length = length; rec.position = "material_inside";
                    rec.p_start = fr.p_start; rec.p_end = fr.p_end;
                    rec.e_dir = fr.e_dir; rec.length_dir = fr.length_dir;
                    new_recs.push_back(rec);

                } else {  // sm_jog
                    const double angle_deg = feat.contains("angle")
                        ? num_or_expr(feat.at("angle"), env) : 90.0;
                    const double offset = feat.contains("offset")
                        ? num_or_expr(feat.at("offset"), env) : 10.0;
                    const double wall = feat.contains("length")
                        ? num_or_expr(feat.at("length"), env) : 20.0;
                    const double inner_radius = feat.contains("radius")
                        ? num_or_expr(feat.at("radius"), env) : radius_default;
                    const double k_factor = feat.contains("k_factor")
                        ? num_or_expr(feat.at("k_factor"), env) : k_default;
                    const std::string position = feat.value("position", std::string("material_inside"));
                    if (!(inner_radius > 0.0))
                        throw std::runtime_error("sm_jog: bend radius must be > 0");
                    if (!(wall > 0.0))
                        throw std::runtime_error("sm_jog: flange length must be > 0");
                    if (!(std::abs(angle_deg) >= 1e-6 && std::abs(angle_deg) <= 90.0 + 1e-6))
                        throw std::runtime_error("sm_jog: angle must be in (0, 90] degrees");

                    const double ang = radians(angle_deg);
                    const double Rc = inner_radius + thickness / 2.0;
                    const double bend_rise = 2.0 * Rc * (1.0 - std::cos(ang));
                    const double needed = offset - bend_rise;
                    double connector = (std::sin(ang) > 1e-6) ? needed / std::sin(ang) : 0.0;
                    if (connector < 0.0) connector = 0.05 * thickness;

                    double x0;
                    if (position == "material_inside")       x0 = -(inner_radius + thickness);
                    else if (position == "material_outside") x0 = -inner_radius;
                    else                                     x0 = 0.0;
                    double pre_run = -x0;
                    if (pre_run < 0) pre_run = 0.0;
                    const double start_x = x0;
                    const double start_y = -thickness / 2.0;
                    std::vector<::geom::StripOp> ops = {
                        {::geom::StripOp::Run, pre_run + thickness, 0.0, true},
                        {::geom::StripOp::Bend, angle_deg, Rc, true},
                        {::geom::StripOp::Run, connector, 0.0, true},
                        {::geom::StripOp::Bend, angle_deg, Rc, false},
                        {::geom::StripOp::Run, wall, 0.0, true}};
                    tool = ::geom::sm_strip_from_centerline(
                        fr.p_start, fr.e_dir, fr.length_dir, fold_dir, fr.edge_length,
                        thickness, ops, start_x, start_y);
                    rec_angle = angle_deg; rec_radius = inner_radius; rec_wall = wall;
                    rec_position = position;
                    // up bend (wall = connector) + back bend (wall = length).
                    for (double w : {connector, wall}) {
                        ::geom::BendRecord rec;
                        rec.angle = angle_deg; rec.radius = inner_radius;
                        rec.thickness = thickness;
                        rec.bend_allowance = ::geom::sm_bend_allowance(angle_deg, inner_radius,
                                                                       thickness, k_factor);
                        rec.wall_length = w; rec.position = position;
                        rec.p_start = fr.p_start; rec.p_end = fr.p_end;
                        rec.e_dir = fr.e_dir; rec.length_dir = fr.length_dir;
                        new_recs.push_back(rec);
                    }
                }
                (void)rec_angle; (void)rec_radius; (void)rec_wall; (void)rec_position;

                if (tool.IsNull())
                    throw std::runtime_error(type + ": geometry build failed");
                GProp_GProps gt;
                BRepGProp::VolumeProperties(tool, gt);
                if (gt.Mass() <= 1e-9)
                    throw std::runtime_error(type + ": produced an empty solid");

                TopoDS_Shape fused = ::geom::fuse(body, tool);
                if (fused.IsNull())
                    throw std::runtime_error(type + ": fuse to sheet failed");
                // A valid wall shares a face with the sheet -> ONE connected solid.
                int nsolids = 0;
                for (TopExp_Explorer sx(fused, TopAbs_SOLID); sx.More(); sx.Next()) ++nsolids;
                if (nsolids > 1)
                    throw std::runtime_error(type + ": result is not connected to the sheet");
                GProp_GProps gb, ga;
                BRepGProp::VolumeProperties(body, gb);
                BRepGProp::VolumeProperties(fused, ga);
                if (ga.Mass() <= gb.Mass() + 1e-9)
                    throw std::runtime_error(type + ": added no material (degenerate placement?)");

                for (const ::geom::BendRecord& r : new_recs) bend_records.push_back(r);
                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{fused};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "sm_flat_pattern") {
                // Unfold the running sheet-metal body into its FLAT PATTERN (developed
                // blank): rebuild the flat base from the largest broad face + add one
                // developed strip (BA + wall) per bend record. Port of
                // engine/doc/features/sheetmetal_flat.py::compile_sm_flat_pattern.
                if (!sheet_ctx.active)
                    throw std::runtime_error("sm_flat_pattern: the active body is not sheet metal");
                const std::string body_id = current_solid_id();
                if (body_id.empty())
                    throw std::runtime_error("sm_flat_pattern: no body to flatten");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                if (bend_records.empty())
                    throw std::runtime_error("sm_flat_pattern: the part has no bends to unfold");

                ::geom::FlatPattern fpat =
                    ::geom::sm_flat_pattern(body, sheet_ctx.thickness, bend_records);
                const TopoDS_Shape& flat = fpat.solid;
                if (flat.IsNull())
                    throw std::runtime_error("sm_flat_pattern: produced an empty flat body");
                GProp_GProps gf;
                BRepGProp::VolumeProperties(flat, gf);
                if (gf.Mass() <= 1e-9)
                    throw std::runtime_error("sm_flat_pattern: produced an empty flat body");

                // Surface the flat-blank metadata export_flat_dxf needs (base normal +
                // FLAT-SPACE bend seam lines); the 2D outline + holes are read back off the
                // flat solid's broad face. Parity: CompileResult.sheet_metal_flat.
                {
                    json bls = json::array();
                    for (const auto& seg : fpat.bend_lines)
                        bls.push_back(json::array(
                            {json::array({seg[0].x(), seg[0].y(), seg[0].z()}),
                             json::array({seg[1].x(), seg[1].y(), seg[1].z()})}));
                    result.sheet_metal_flat[id] =
                        json{{"n_base", json::array({fpat.n_base.x(), fpat.n_base.y(),
                                                     fpat.n_base.z()})},
                             {"bend_lines", bls}};
                }

                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{flat};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "weld_member") {
                // Structural weldment members: sweep a library profile perpendicular
                // to each selected sketch segment (BRepOffsetAPI_MakePipe), one
                // SEPARATE body per segment (an assembly of members). Port of
                // engine/doc/features/weldment.py::compile_weld_member.
                //
                // Corner trimming (miter/butt/cope) is ported: where straight members
                // meet at a shared vertex or a T-joint they are trimmed to match the
                // oracle (see ::geom::weld_corner_trim). trim_corners=false leaves each
                // member's volume the exact swept prism area*length.
                const std::string profile = feat.value("profile", std::string("square_tube"));
                if (!::geom::weld_profile_known(profile))
                    throw std::runtime_error("weld_member: unknown profile '" + profile + "'");
                const std::string justify = feat.value("justify", std::string("center"));
                if (justify != "center" && justify != "corner")
                    throw std::runtime_error("weld_member: unknown justify '" + justify + "'");
                const double angle = feat.contains("angle") && !feat.at("angle").is_null()
                    ? num_or_expr(feat.at("angle"), env) : 0.0;
                const bool trim_corners = feat.value("trim_corners", true);
                const std::string joint = feat.value("joint", std::string("miter"));
                if (joint != "miter" && joint != "butt" && joint != "cope")
                    throw std::runtime_error("weld_member: unknown joint '" + joint +
                                             "' (miter|butt|cope)");

                // The source sketch (its raw segments become the member paths). The
                // `sketch` ref is SelOrDatum (validate.cpp), so accept a bare feature-id
                // string OR a selector that names the owning sketch (region {sketch:fid} /
                // {owner_feature} / {feature}) via selector_owner_id — rather than demanding
                // a bare string, which dangled "missing string ref 'sketch'" on a selector
                // object even though every other ref slot takes one (Bug W-1).
                std::string sid;
                if (feat.contains("sketch")) sid = selector_owner_id(feat.at("sketch"));
                if (sid.empty())
                    throw std::runtime_error(
                        "weld_member: a 'sketch' ref is required (the path sketch's feature id, "
                        "or a selector naming it, e.g. {\"kind\":\"region\",\"sketch\":\"f1\"})");
                const SketchResult& src = require_sketch(results, sid, sid.c_str());
                // Optional segment filter: a list of entity ids (default = all).
                std::set<std::string> wanted;
                bool have_filter = false;
                auto seg_it = feat.find("segments");
                if (seg_it != feat.end() && seg_it->is_array()) {
                    have_filter = true;
                    for (const json& s : *seg_it)
                        if (s.is_string()) wanted.insert(s.get<std::string>());
                }

                // Build the profile face once (local frame); reused per segment.
                double area = 0.0, env_w = 0.0, env_h = 0.0;
                std::string perr;
                TopoDS_Face face_local = ::geom::weld_profile_face(
                    profile, feat, justify, angle, area, perr, &env_w, &env_h);
                if (face_local.IsNull())
                    throw std::runtime_error("weld_member: " + perr);

                // Resolve (entity_id, world-space edge) pairs to sweep along. Straight
                // LINE segments AND ARC segments are swept (a closed circle or a
                // construction entity is skipped, exactly like the oracle's
                // _segment_edges which keeps any OPEN line/arc edge). `t_start`/`t_end`
                // are the path tangents at the ends (parallel for a line), `length` is
                // the swept-axis length — the CHORD for a line but the true ARC LENGTH
                // for an arc (oracle reads edge.length), and `straight` flags a line
                // member (only straight members are corner-trimmed).
                struct Seg {
                    std::string eid; TopoDS_Edge edge;
                    Eigen::Vector3d start, end, t_start, t_end;
                    double length = 0.0; bool straight = true;
                };
                std::vector<Seg> segs;
                const json& ents = src.entities;
                if (ents.is_object()) {
                    for (auto eit = ents.begin(); eit != ents.end(); ++eit) {
                        const json& ent = eit.value();
                        if (!ent.is_object()) continue;
                        if (ent.value("construction", false)) continue;
                        const std::string eid = ent.value("id", eit.key());
                        if (have_filter && !wanted.count(eid)) continue;
                        const std::string etype = ent.value("type", std::string());
                        if (etype == "line") {
                            if (!ent.contains("p1") || !ent.contains("p2")) continue;
                            Eigen::Vector2d a(ent["p1"][0].get<double>(), ent["p1"][1].get<double>());
                            Eigen::Vector2d b(ent["p2"][0].get<double>(), ent["p2"][1].get<double>());
                            if ((a - b).norm() < 1e-9) continue;
                            Eigen::Vector3d wa = src.plane.lift(a), wb = src.plane.lift(b);
                            BRepBuilderAPI_MakeEdge me(gp_Pnt(wa.x(), wa.y(), wa.z()),
                                                       gp_Pnt(wb.x(), wb.y(), wb.z()));
                            if (!me.IsDone()) continue;
                            Seg sg;
                            sg.eid = eid;
                            sg.edge = me.Edge();
                            sg.start = wa;
                            sg.end = wb;
                            sg.t_start = (wb - wa).normalized();
                            sg.t_end = sg.t_start;  // a line: start/end tangents parallel
                            sg.length = (wb - wa).norm();
                            sg.straight = true;
                            segs.push_back(std::move(sg));
                        } else if (etype == "arc") {
                            // Build the arc identically to build123d's Edge.make_circle
                            // (oracle _entity_to_edges): a CCW arc of the circle in the
                            // sketch frame; if ccw=false the start/end angles swap.
                            // The angles are DEGREES (matching the entity store + the
                            // build123d API). Lifted into the sketch plane's world frame
                            // (circle axis = plane normal, x-ref = plane uAxis), so the
                            // swept geometry + arc length match the oracle.
                            if (!ent.contains("center") || !ent.contains("radius")) continue;
                            Eigen::Vector2d c(ent["center"][0].get<double>(),
                                              ent["center"][1].get<double>());
                            double r = ent.value("radius", 0.0);
                            if (r <= 1e-9) continue;
                            double sa = ent.value("start", 0.0);  // degrees
                            double ea = ent.value("end", 0.0);
                            bool ccw = ent.value("ccw", true);
                            if (std::abs(ea - sa) < 1e-9) continue;  // degenerate (no span)
                            double a1 = (ccw ? sa : ea) * PI / 180.0;
                            double a2 = (ccw ? ea : sa) * PI / 180.0;
                            Eigen::Vector3d cw = src.plane.lift(c);
                            gp_Ax2 ax(gp_Pnt(cw.x(), cw.y(), cw.z()),
                                      gp_Dir(src.plane.normal.x(), src.plane.normal.y(),
                                             src.plane.normal.z()),
                                      gp_Dir(src.plane.uAxis.x(), src.plane.uAxis.y(),
                                             src.plane.uAxis.z()));
                            gp_Circ circ(ax, r);
                            GC_MakeArcOfCircle mkarc(circ, a1, a2, Standard_True);  // CCW
                            if (!mkarc.IsDone()) continue;
                            BRepBuilderAPI_MakeEdge me(mkarc.Value());
                            if (!me.IsDone()) continue;
                            Seg sg;
                            sg.eid = eid;
                            sg.edge = me.Edge();
                            // Endpoints + end tangents from the curve (D1 = forward
                            // derivative); length = the true arc length (LinearProperties,
                            // the same measure as build123d's edge.length).
                            BRepAdaptor_Curve curve(sg.edge);
                            gp_Pnt p0, p1; gp_Vec d0, d1;
                            curve.D1(curve.FirstParameter(), p0, d0);
                            curve.D1(curve.LastParameter(), p1, d1);
                            sg.start = Eigen::Vector3d(p0.X(), p0.Y(), p0.Z());
                            sg.end = Eigen::Vector3d(p1.X(), p1.Y(), p1.Z());
                            sg.t_start = Eigen::Vector3d(d0.X(), d0.Y(), d0.Z());
                            if (sg.t_start.norm() > 1e-12) sg.t_start.normalize();
                            sg.t_end = Eigen::Vector3d(d1.X(), d1.Y(), d1.Z());
                            if (sg.t_end.norm() > 1e-12) sg.t_end.normalize();
                            GProp_GProps lp;
                            BRepGProp::LinearProperties(sg.edge, lp);
                            sg.length = lp.Mass();
                            sg.straight = false;  // an arc is not corner-trimmed
                            if (sg.length < 1e-9) continue;
                            segs.push_back(std::move(sg));
                        } else {
                            continue;  // circle/other closed or unsupported path: skipped
                        }
                    }
                }
                if (segs.empty())
                    throw std::runtime_error("weld_member: sketch has no usable segments to sweep along");

                // Sweep one member solid per segment, carrying the straight-axis
                // geometry the corner trim needs (mirrors weldment.py building the
                // `members` dicts before _mitre_trim / _butt_cope_trim).
                std::vector<::geom::WeldMember> members;
                members.reserve(segs.size());
                // Parallel cut-list metadata per member (the oracle's member dict keys
                // not carried on WeldMember): the source segment id + the section area.
                std::vector<std::string> member_eids;
                member_eids.reserve(segs.size());
                double max_len = 1.0;
                for (const Seg& sg : segs) {
                    // Orient the section perpendicular to the path at the START using the
                    // start tangent (oracle _sweep_member sweeps along the edge from its
                    // start tangent) — for a line this is the segment direction, for an
                    // arc the curve tangent at the first endpoint.
                    TopoDS_Shape member =
                        ::geom::weld_sweep_member(face_local, sg.edge, sg.start, sg.t_start);
                    if (member.IsNull())
                        throw std::runtime_error("weld_member: sweep failed for segment '" + sg.eid + "'");
                    GProp_GProps gm;
                    BRepGProp::VolumeProperties(member, gm);
                    if (gm.Mass() <= 1e-9)
                        throw std::runtime_error("weld_member: produced an empty member solid");
                    ::geom::WeldMember wm;
                    wm.solid = member;
                    wm.start = sg.start;
                    wm.end = sg.end;
                    wm.t_start = sg.t_start;
                    wm.t_end = sg.t_end;
                    // Swept-axis length: the chord for a line, the true arc length for an
                    // arc (the oracle stores edge.length on each member record).
                    wm.length = sg.length;
                    wm.straight = sg.straight;  // arcs are flagged non-straight (not trimmed)
                    wm.env_w = env_w;
                    wm.env_h = env_h;
                    max_len = std::max(max_len, wm.length);
                    members.push_back(std::move(wm));
                    member_eids.push_back(sg.eid);
                }

                // Trim shared corners / joints (best-effort; never fails the feature).
                // reach = max_len*4 matches the oracle's half-space sizing.
                if (trim_corners && members.size() > 1) {
                    try {
                        ::geom::weld_corner_trim(members, max_len * 4.0, joint);
                    } catch (...) {
                        // leave members untrimmed on any trim error (oracle parity)
                    }
                }

                // Resolved profile dims (= the oracle's build_profile_face dims): the
                // key the cut list groups + describes members by.
                json wm_dims = ::geom::weld_profile_dims(profile, feat);

                // One SEPARATE surviving body per member. Store each under a synthetic
                // id (feature_id#k) so the surviving-body set + count + total volume
                // match the oracle (which appends each member to ctx.bodies). At the
                // same time build the cut-list side-channel record per member, mirroring
                // weldment.py's member_records (start/end/area/volume captured POST-trim,
                // exactly like the oracle reads m["solid"].volume after the trim pass).
                json wm_member_records = json::array();
                int k = 0;
                for (::geom::WeldMember& wm : members) {
                    std::string mid = id + "#" + std::to_string(k);
                    results[mid] = SolidResult{wm.solid};
                    solid_order.push_back(mid);
                    GProp_GProps gv;
                    BRepGProp::VolumeProperties(wm.solid, gv);
                    wm_member_records.push_back(json{
                        {"segment_id", member_eids[static_cast<std::size_t>(k)]},
                        {"profile", profile},
                        {"length", wm.length},
                        {"start", {wm.start.x(), wm.start.y(), wm.start.z()}},
                        {"end", {wm.end.x(), wm.end.y(), wm.end.z()}},
                        {"area", area},
                        {"volume", gv.Mass()},
                    });
                    ++k;
                }
                result.weld_members[id] = json{
                    {"profile", profile}, {"dims", wm_dims},
                    {"members", std::move(wm_member_records)},
                };
                st.ok = true;

            } else if (type == "weld_end_cap" || type == "weld_gusset") {
                // Dress weldment features: a plate resolved from picked planar face(s)
                // on the EXISTING member bodies. Both are SEPARATE new bodies appended
                // to the rolling list (the members are NOT consumed). Ports of
                // compile_weld_end_cap / compile_weld_gusset.
                //
                // Face picks resolve against ALL current surviving bodies (members are
                // kept separate), keeping the candidate with the smallest centroid
                // distance — the C++ analogue of weldment.py::_resolve_face_any_body.
                std::vector<std::string> live;
                for (const std::string& s : solid_order)
                    if (!consumed.count(s)) live.push_back(s);
                if (live.empty())
                    throw std::runtime_error(type + ": no member bodies — add weld_member first");

                // Resolve ONE face selector against every live body, returning the
                // best (smallest selector-centroid distance) face.
                auto resolve_face_any = [&](const json& sel, const char* slot) -> TopoDS_Face {
                    if (!sel.is_object() || sel.value("kind", std::string()) != "face")
                        throw std::runtime_error(std::string(slot) + ": a planar face selector is required");
                    std::array<double, 3> want{0, 0, 0};
                    bool have_want = false;
                    auto cit = sel.find("centroid");
                    if (cit != sel.end() && cit->is_array() && cit->size() == 3) {
                        want = {(*cit)[0].get<double>(), (*cit)[1].get<double>(), (*cit)[2].get<double>()};
                        have_want = true;
                    }
                    TopoDS_Face best;
                    double best_d = std::numeric_limits<double>::max();
                    for (const std::string& bid : live) {
                        const TopoDS_Shape& body = std::get<SolidResult>(*results.at(bid)).shape;
                        chudware::doc::FacePicks fp = chudware::doc::resolve_face_picks(body, sel, slot);
                        for (const TopoDS_Face& fc : fp.faces) {
                            double d = 0.0;
                            if (have_want) {
                                GProp_GProps gp;
                                BRepGProp::SurfaceProperties(fc, gp);
                                gp_Pnt c = gp.CentreOfMass();
                                d = std::sqrt((c.X() - want[0]) * (c.X() - want[0]) +
                                              (c.Y() - want[1]) * (c.Y() - want[1]) +
                                              (c.Z() - want[2]) * (c.Z() - want[2]));
                            }
                            if (d < best_d) { best_d = d; best = fc; }
                        }
                    }
                    if (best.IsNull())
                        throw std::runtime_error(std::string(slot) + ": face selector did not resolve on any member body");
                    return best;
                };

                // Planar face normal (build123d normal_at: geometric normal, flipped
                // when the face is REVERSED) — matches the oracle's _require_planar.
                auto face_normal = [&](const TopoDS_Face& fc, const char* what) -> Eigen::Vector3d {
                    BRepAdaptor_Surface surf(fc);
                    if (surf.GetType() != GeomAbs_Plane)
                        throw std::runtime_error(std::string(what) + ": the picked face is not planar");
                    const gp_Dir d = ::chudware::geom::plane_outward_normal(fc, surf.Plane());
                    Eigen::Vector3d n(d.X(), d.Y(), d.Z());
                    if (n.norm() < 1e-9)
                        throw std::runtime_error(std::string(what) + ": degenerate face normal");
                    return n.normalized();
                };
                auto face_center = [&](const TopoDS_Face& fc) -> Eigen::Vector3d {
                    GProp_GProps gp;
                    BRepGProp::SurfaceProperties(fc, gp);
                    gp_Pnt c = gp.CentreOfMass();
                    return Eigen::Vector3d(c.X(), c.Y(), c.Z());
                };

                TopoDS_Shape plate;
                if (type == "weld_end_cap") {
                    const double thickness = feat.contains("thickness")
                        ? num_or_expr(feat.at("thickness"), env) : 5.0;
                    const std::string cap_type = feat.value("hem_type", feat.value("type", std::string("outer")));
                    const double offset = feat.contains("offset") && !feat.at("offset").is_null()
                        ? num_or_expr(feat.at("offset"), env) : 0.0;
                    const double overhang = feat.contains("overhang") && !feat.at("overhang").is_null()
                        ? num_or_expr(feat.at("overhang"), env) : 0.0;
                    if (!(thickness > 0.0))
                        throw std::runtime_error("weld_end_cap: thickness must be > 0");
                    if (cap_type != "outer" && cap_type != "inner")
                        throw std::runtime_error("weld_end_cap: type must be 'outer' or 'inner'");
                    if (overhang < 0.0)
                        throw std::runtime_error("weld_end_cap: overhang must be >= 0");

                    auto fsit = feat.find("face");
                    if (fsit == feat.end())
                        throw std::runtime_error("weld_end_cap: refs.face is required (a planar member END face)");
                    json fsel = *fsit;
                    if (fsel.is_array()) {
                        if (fsel.size() != 1)
                            throw std::runtime_error("weld_end_cap: refs.face takes exactly one face selector");
                        fsel = fsel.front();
                    }
                    TopoDS_Face end_face = resolve_face_any(fsel, "face");
                    Eigen::Vector3d n = face_normal(end_face, "weld_end_cap");
                    std::string cerr;
                    plate = ::geom::weld_end_cap_plate(end_face, n, cap_type, thickness,
                                                       offset, overhang, cerr);
                    if (plate.IsNull())
                        throw std::runtime_error("weld_end_cap: " + cerr);

                } else {  // weld_gusset
                    const double thickness = feat.contains("thickness")
                        ? num_or_expr(feat.at("thickness"), env) : 5.0;
                    const double d1 = feat.contains("d1") ? num_or_expr(feat.at("d1"), env) : 30.0;
                    const double d2 = feat.contains("d2") ? num_or_expr(feat.at("d2"), env) : 30.0;
                    const std::string gtype = feat.value("hem_type", feat.value("type", std::string("triangular")));
                    const std::string location = feat.value("location", std::string("centered"));
                    const double offset = feat.contains("offset") && !feat.at("offset").is_null()
                        ? num_or_expr(feat.at("offset"), env) : 0.0;
                    if (!(thickness > 0.0))
                        throw std::runtime_error("weld_gusset: thickness must be > 0");
                    if (!(d1 > 0.0 && d2 > 0.0))
                        throw std::runtime_error("weld_gusset: legs d1 and d2 must be > 0");
                    if (gtype != "triangular" && gtype != "polygonal")
                        throw std::runtime_error("weld_gusset: type must be 'triangular' or 'polygonal'");
                    if (location != "centered" && location != "inner" && location != "outer")
                        throw std::runtime_error("weld_gusset: location must be 'centered', 'inner' or 'outer'");

                    auto fsit = feat.find("faces");
                    if (fsit == feat.end() || !fsit->is_array() || fsit->size() != 2)
                        throw std::runtime_error("weld_gusset: refs.faces must be exactly two planar face selectors");
                    TopoDS_Face f1 = resolve_face_any((*fsit)[0], "faces[0]");
                    TopoDS_Face f2 = resolve_face_any((*fsit)[1], "faces[1]");
                    Eigen::Vector3d n1 = face_normal(f1, "weld_gusset");
                    Eigen::Vector3d n2 = face_normal(f2, "weld_gusset");
                    Eigen::Vector3d c1 = face_center(f1), c2 = face_center(f2);
                    double len1 = 0.0, len2 = 0.0;
                    Eigen::Vector3d axis1 = ::geom::weld_longest_edge_dir(f1, len1);
                    Eigen::Vector3d axis2 = ::geom::weld_longest_edge_dir(f2, len2);
                    if (len1 < 1e-6 || len2 < 1e-6)
                        throw std::runtime_error("weld_gusset: could not recover a member axis from a picked face");
                    if (std::abs(axis1.dot(axis2)) > 0.999)
                        throw std::runtime_error("weld_gusset: the two picked faces are parallel/collinear members");

                    Eigen::Vector3d apex =
                        ::geom::weld_closest_point_between_lines(c1, axis1, c2, axis2);

                    // Gusset plane normal: average of the two face normals when they
                    // share a plane; else axis1 x axis2 (oracle).
                    Eigen::Vector3d gn;
                    if (std::abs(n1.dot(n2)) > 0.5) {
                        gn = n1 + (n1.dot(n2) > 0 ? n2 : -n2);
                        if (gn.norm() < 1e-9) gn = axis1.cross(axis2);
                    } else {
                        gn = axis1.cross(axis2);
                    }
                    if (gn.norm() < 1e-9)
                        throw std::runtime_error("weld_gusset: could not determine the gusset plane normal");
                    gn.normalize();

                    auto toward = [](const Eigen::Vector3d& apex, const Eigen::Vector3d& target,
                                     const Eigen::Vector3d& axis) -> Eigen::Vector3d {
                        Eigen::Vector3d v = target - apex;
                        return axis.dot(v) >= 0 ? axis : -axis;
                    };
                    auto in_plane = [](const Eigen::Vector3d& v, const Eigen::Vector3d& nn) -> Eigen::Vector3d {
                        return v - nn * v.dot(nn);
                    };
                    Eigen::Vector3d leg1 = in_plane(toward(apex, c1, axis1), gn);
                    Eigen::Vector3d leg2 = in_plane(toward(apex, c2, axis2), gn);
                    if (leg1.norm() < 1e-9 || leg2.norm() < 1e-9)
                        throw std::runtime_error("weld_gusset: degenerate leg direction at the corner");
                    leg1.normalize();
                    leg2.normalize();

                    Eigen::Vector3d p_a = apex;
                    Eigen::Vector3d p_b = apex + leg1 * d1;
                    Eigen::Vector3d p_c = apex + leg2 * d2;
                    std::vector<Eigen::Vector3d> corners;
                    if (gtype == "triangular") {
                        corners = {p_a, p_b, p_c};
                    } else {
                        Eigen::Vector3d pb2 = apex + leg1 * (d1 * 0.7);
                        Eigen::Vector3d pc2 = apex + leg2 * (d2 * 0.7);
                        corners = {p_a, p_b, pb2, pc2, p_c};
                    }

                    double loc_off = offset;
                    if (location == "inner") loc_off -= thickness / 2.0;
                    else if (location == "outer") loc_off += thickness / 2.0;

                    plate = ::geom::weld_make_solid_plate(corners, gn, thickness, loc_off);
                    if (plate.IsNull())
                        throw std::runtime_error("weld_gusset: plate build failed");
                }

                GProp_GProps gp;
                BRepGProp::VolumeProperties(plate, gp);
                if (gp.Mass() <= 1e-9)
                    throw std::runtime_error(type + ": produced an empty/degenerate plate");
                // The plate is a SEPARATE new body (members survive untouched).
                results[id] = SolidResult{plate};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "surf_planar") {
                // The bounding sketch FACE is the surface body (no thickness).
                std::vector<TopoDS_Face> faces = surf_profile_faces(feat);
                results[id] = SolidResult{faces.front()};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_extrude") {
                std::vector<TopoDS_Face> faces = surf_profile_faces(feat);
                const std::string end = feat.value("end", std::string("blind"));
                const bool flip = feat.value("flip", false);
                const double depth = feat.contains("depth")
                    ? num_or_expr(feat.at("depth"), env) : 10.0;
                // Direction = first profile face normal (flip negates). Sketch plane
                // normal is the exact analogue of build123d face.normal_at() here.
                Eigen::Vector3d normal = src_face_normal(faces.front());
                if (flip) normal = -normal;
                double amount = depth;
                bool mid = (end == "mid_plane");
                if (end == "through_all") {
                    Bnd_Box bb;
                    BRepBndLib::Add(faces.front(), bb);
                    double x0, y0, z0, x1, y1, z1;
                    bb.Get(x0, y0, z0, x1, y1, z1);
                    double diag = std::sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0) +
                                            (z1 - z0) * (z1 - z0));
                    amount = 1.05 * std::max(diag, 1e-3) * 2.0;
                }
                if (!(amount > 0.0))
                    throw std::runtime_error("surf_extrude: effective depth must be > 0");
                TopoDS_Shape shell = ::geom::surf_extrude_walls(faces, normal, amount, mid);
                if (shell.IsNull())
                    throw std::runtime_error("surf_extrude: side-wall build failed");
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_revolve") {
                std::vector<TopoDS_Face> faces = surf_profile_faces(feat);
                const Eigen::Vector3d axis_pt = req_vec3(feat, "axis_pt");
                const Eigen::Vector3d axis_dir = req_vec3(feat, "axis_dir");
                double angle_deg = feat.contains("angle")
                    ? num_or_expr(feat.at("angle"), env) : 360.0;
                if (std::abs(angle_deg) < 1e-6)
                    throw std::runtime_error("surf_revolve: angle must be non-zero");
                if (std::abs(angle_deg) > 360.0 + 1e-6)
                    angle_deg = std::copysign(360.0, angle_deg);
                TopoDS_Shape shell = ::geom::surf_revolve_walls(
                    faces, axis_pt, axis_dir, radians(std::abs(angle_deg)));
                if (shell.IsNull())
                    throw std::runtime_error("surf_revolve: MakeRevol failed");
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_sweep") {
                std::vector<TopoDS_Face> faces = surf_profile_faces(feat);
                auto pit = feat.find("path");
                if (pit == feat.end())
                    throw std::runtime_error("surf_sweep: a 'path' ref is required");
                const std::string path_id = selector_owner_id(*pit);
                const SketchResult& pathsk = require_sketch(results, path_id, "path");
                TopoDS_Wire spine =
                    chudware::geom::wire_from_solved_sketch(pathsk.entities, pathsk.plane);
                if (spine.IsNull())
                    throw std::runtime_error("surf_sweep: path produced no wire");
                TopoDS_Shape shell = ::geom::surf_sweep_walls(faces, spine);
                if (shell.IsNull())
                    throw std::runtime_error("surf_sweep: build failed");
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_loft") {
                auto sit = feat.find("sections");
                if (sit == feat.end() || !sit->is_array() || sit->size() < 2)
                    throw std::runtime_error("surf_loft: needs >= 2 sections");
                std::vector<TopoDS_Wire> wires;
                for (const json& s : *sit) {
                    const std::string sk = selector_owner_id(s);
                    const SketchResult& src = require_sketch(results, sk, sk.c_str());
                    if (src.faces.empty())
                        throw std::runtime_error("surf_loft: section '" + sk + "' has no face");
                    TopoDS_Wire w = BRepTools::OuterWire(src.faces.front());
                    if (w.IsNull())
                        throw std::runtime_error("surf_loft: section '" + sk + "' has no wire");
                    wires.push_back(w);
                }
                const bool ruled = feat.value("ruled", false);
                TopoDS_Shape shell = ::geom::surf_loft_shell(wires, ruled);
                if (shell.IsNull())
                    throw std::runtime_error("surf_loft: ThruSections failed");
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_ruled") {
                if (!feat.contains("edge_a") || !feat.contains("edge_b"))
                    throw std::runtime_error("surf_ruled: edge_a and edge_b are required");
                TopoDS_Wire wa = surf_sketch_wire(feat.at("edge_a"), "edge_a");
                TopoDS_Wire wb = surf_sketch_wire(feat.at("edge_b"), "edge_b");
                TopoDS_Shape surf = ::geom::surf_ruled_surface(wa, wb);
                if (surf.IsNull())
                    throw std::runtime_error("surf_ruled: build failed");
                results[id] = SolidResult{surf};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_offset") {
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("surf_offset: requires an existing body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double distance = feat.contains("distance")
                    ? num_or_expr(feat.at("distance"), env) : 5.0;
                if (std::abs(distance) < 1e-9)
                    throw std::runtime_error("surf_offset: distance must be non-zero");
                auto fit = feat.find("faces");
                if (fit == feat.end())
                    throw std::runtime_error("surf_offset: refs.faces is required");
                chudware::doc::FacePicks picks =
                    chudware::doc::resolve_face_picks(body, *fit, "faces");
                if (!picks.dangling.empty())
                    throw std::runtime_error("surf_offset: face selector(s) dangling");
                if (picks.faces.empty())
                    throw std::runtime_error("surf_offset: no faces resolved");
                TopoDS_Shape surf = ::geom::surf_offset_surface(picks.faces, distance);
                if (surf.IsNull())
                    throw std::runtime_error("surf_offset: offset build failed");
                results[id] = SolidResult{surf};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_fill" || type == "surf_boundary") {
                // Boundary edges come from sketch region curve(s). For surf_fill the
                // ref slot is "edges"; for surf_boundary it is "dir1" (+ optional dir2).
                std::vector<TopoDS_Edge> edges;
                auto add_wire_edges = [&](const json& ref, const char* slot) {
                    TopoDS_Wire w = surf_sketch_wire(ref, slot);
                    for (TopExp_Explorer ex(w, TopAbs_EDGE); ex.More(); ex.Next())
                        edges.push_back(TopoDS::Edge(ex.Current()));
                };
                if (type == "surf_fill") {
                    auto eit = feat.find("edges");
                    if (eit == feat.end() || !eit->is_array() || eit->empty())
                        throw std::runtime_error("surf_fill: refs.edges is required");
                    for (const json& s : *eit) add_wire_edges(s, "edges");
                } else {
                    auto d1 = feat.find("dir1");
                    if (d1 == feat.end())
                        throw std::runtime_error("surf_boundary: refs.dir1 is required");
                    if (d1->is_array()) { for (const json& s : *d1) add_wire_edges(s, "dir1"); }
                    else add_wire_edges(*d1, "dir1");
                    auto d2 = feat.find("dir2");
                    if (d2 != feat.end()) {
                        if (d2->is_array()) { for (const json& s : *d2) add_wire_edges(s, "dir2"); }
                        else add_wire_edges(*d2, "dir2");
                    }
                }
                const int cont = (feat.value("continuity", std::string("g0")) == "g1") ? 1 : 0;
                TopoDS_Shape patch = ::geom::surf_fill_patch(edges, cont);
                if (patch.IsNull())
                    throw std::runtime_error(type + ": BRepFill_Filling failed");
                results[id] = SolidResult{patch};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_knit") {
                auto sit = feat.find("surfaces");
                if (sit == feat.end() || !sit->is_array() || sit->size() < 2)
                    throw std::runtime_error("surf_knit: needs >= 2 surface bodies");
                std::vector<TopoDS_Shape> inputs;
                std::vector<std::string> consumed_ids;
                for (const json& s : *sit) {
                    const std::string owner = selector_owner_id(s);
                    if (owner.empty())
                        throw std::runtime_error("surf_knit: malformed surface selector");
                    inputs.push_back(require_solid(results, owner, "surfaces"));
                    consumed_ids.push_back(owner);
                }
                // Sew tolerance: 1e-3 * combined bbox diagonal, clamped [1e-4, 0.1].
                Bnd_Box bb;
                for (const TopoDS_Shape& s : inputs) BRepBndLib::Add(s, bb);
                double tol = 1e-3;
                if (!bb.IsVoid()) {
                    double x0, y0, z0, x1, y1, z1;
                    bb.Get(x0, y0, z0, x1, y1, z1);
                    double diag = std::sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0) +
                                            (z1 - z0) * (z1 - z0));
                    tol = std::max(1e-4, std::min(0.1, diag * 1e-3));
                }
                bool is_solid = false;
                TopoDS_Shape knit = ::geom::surf_knit_bodies(inputs, tol, is_solid);
                if (knit.IsNull())
                    throw std::runtime_error("surf_knit: sewing failed");
                for (const std::string& cid : consumed_ids) consumed.insert(cid);
                results[id] = SolidResult{knit};
                solid_order.push_back(id);
                body_kinds[id] = is_solid ? "solid" : "surface";
                st.ok = true;

            } else if (type == "surf_delete_face") {
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("surf_delete_face: requires an existing body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                auto fit = feat.find("faces");
                if (fit == feat.end())
                    throw std::runtime_error("surf_delete_face: refs.faces is required");
                chudware::doc::FacePicks picks =
                    chudware::doc::resolve_face_picks(body, *fit, "faces");
                if (!picks.dangling.empty())
                    throw std::runtime_error("surf_delete_face: face selector(s) dangling");
                if (picks.faces.empty())
                    throw std::runtime_error("surf_delete_face: no faces resolved");
                std::vector<Eigen::Vector3d> cents;
                for (const TopoDS_Face& f : picks.faces)
                    cents.push_back(::geom::face_centroid(f));
                double tol = std::max(chudware::doc::bbox_diag(body), 1.0) * 1e-4;
                TopoDS_Shape shell = ::geom::surf_delete_faces(body, cents, tol);
                if (shell.IsNull())
                    throw std::runtime_error("surf_delete_face: nothing left after delete");
                // REPLACE the solid with the opened shell (same body, now a surface).
                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_untrim") {
                // UNTRIM: recover the full underlying surface patch of each selected face,
                // dropping its trim wires (incl. inner holes), as a NEW surface body. The
                // source body is LEFT INTACT (untrim extracts geometry, it does not modify
                // the part) — so this is "give me the underlying surfaces of these faces".
                // (Bug S-1: surf_untrim was validator-accepted but had NO compiler, so it
                // always failed at build with "no compiler for feature type".)
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("surf_untrim: requires an existing body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                auto fit = feat.find("faces");
                if (fit == feat.end())
                    throw std::runtime_error("surf_untrim: refs.faces is required");
                chudware::doc::FacePicks picks =
                    chudware::doc::resolve_face_picks(body, *fit, "faces");
                if (!picks.dangling.empty())
                    throw std::runtime_error("surf_untrim: face selector(s) dangling");
                if (picks.faces.empty())
                    throw std::runtime_error("surf_untrim: no faces resolved");
                const double utol = std::max(chudware::doc::bbox_diag(body), 1.0) * 1e-6;
                BRep_Builder ubb;
                TopoDS_Compound ucomp;
                ubb.MakeCompound(ucomp);
                int un = 0;
                for (const TopoDS_Face& f : picks.faces) {
                    Handle(Geom_Surface) surf = BRep_Tool::Surface(f);
                    if (surf.IsNull()) continue;
                    double umin, umax, vmin, vmax;
                    BRepTools::UVBounds(f, umin, umax, vmin, vmax);
                    if (!(umax > umin) || !(vmax > vmin)) continue;
                    BRepBuilderAPI_MakeFace mk(surf, umin, umax, vmin, vmax, utol);
                    if (!mk.IsDone()) continue;
                    ubb.Add(ucomp, mk.Face());
                    ++un;
                }
                if (un == 0)
                    throw std::runtime_error(
                        "surf_untrim: no face could be untrimmed (the underlying surface "
                        "could not be rebuilt over its parameter bounds)");
                // One face -> commit the bare face; several -> the compound of patches.
                TopoDS_Shape uout = ucomp;
                if (un == 1) {
                    TopExp_Explorer uex(ucomp, TopAbs_FACE);
                    if (uex.More()) uout = uex.Current();
                }
                results[id] = SolidResult{uout};   // NEW body; source NOT consumed
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_trim") {
                auto ssel = feat.find("surface");
                if (ssel == feat.end())
                    throw std::runtime_error("surf_trim: refs.surface is required");
                const std::string surf_owner = selector_owner_id(*ssel);
                const TopoDS_Shape& surface = require_solid(results, surf_owner, "surface");
                auto tit = feat.find("tool");
                if (tit == feat.end())
                    throw std::runtime_error("surf_trim: refs.tool is required");
                // The tool is a FACE selector (split._resolve_tool_face searches every
                // running body), a sketch id whose face is the planar tool, or a body
                // owner whose picked face is the tool. Try, in order: a named sketch
                // face, then a face pick against every surviving solid body.
                TopoDS_Face tool_face;
                bool tool_ok = false;
                const std::string tool_owner = selector_owner_id(*tit);
                if (!tool_owner.empty() && results.count(tool_owner) &&
                    std::holds_alternative<SketchResult>(*results.at(tool_owner))) {
                    const SketchResult& tsk = std::get<SketchResult>(*results.at(tool_owner));
                    if (!tsk.faces.empty()) { tool_face = tsk.faces.front(); tool_ok = true; }
                }
                if (!tool_ok && tit->is_object() &&
                    tit->value("kind", std::string()) == "face") {
                    // Resolve the face pick against every surviving solid body (the split
                    // tool-face resolver scans all bodies); take the first that resolves.
                    for (const std::string& cand : solid_order) {
                        if (consumed.count(cand)) continue;
                        if (!std::holds_alternative<SolidResult>(*results.at(cand))) continue;
                        const TopoDS_Shape& cb = std::get<SolidResult>(*results.at(cand)).shape;
                        if (cb.IsNull()) continue;
                        chudware::doc::FacePicks tp =
                            chudware::doc::resolve_face_picks(cb, *tit, "tool");
                        if (!tp.faces.empty()) { tool_face = tp.faces.front(); tool_ok = true; break; }
                    }
                }
                if (!tool_ok)
                    throw std::runtime_error("surf_trim: tool face did not resolve");
                const std::string keep = feat.value("keep", std::string("both"));
                int split_count = 0;
                TopoDS_Shape trimmed =
                    ::geom::surf_trim_surface(surface, tool_face, keep, split_count);
                if (trimmed.IsNull() || split_count == 0)
                    throw std::runtime_error("surf_trim: tool did not divide the surface");
                consumed.insert(surf_owner);
                source_of[id] = surf_owner;
                results[id] = SolidResult{trimmed};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_replace_face") {
                // The modification target is the last SOLID body (a surface body in the
                // list is skipped — replace_face heals a solid, not a sheet).
                std::string body_id;
                if (feat.contains("body") && feat["body"].is_string()) {
                    body_id = feat["body"].get<std::string>();
                } else {
                    for (auto it = solid_order.rbegin(); it != solid_order.rend(); ++it) {
                        if (consumed.count(*it)) continue;
                        auto kit = body_kinds.find(*it);
                        if (kit != body_kinds.end() && kit->second == "surface") continue;
                        body_id = *it;
                        break;
                    }
                }
                if (body_id.empty())
                    throw std::runtime_error("surf_replace_face: requires an existing solid");
                const TopoDS_Shape& solid = require_solid(results, body_id, "body");
                auto fit = feat.find("faces");
                if (fit == feat.end())
                    throw std::runtime_error("surf_replace_face: refs.faces is required");
                chudware::doc::FacePicks picks =
                    chudware::doc::resolve_face_picks(solid, *fit, "faces");
                if (picks.faces.empty())
                    throw std::runtime_error("surf_replace_face: no target faces resolved");
                auto ssel = feat.find("surface");
                if (ssel == feat.end())
                    throw std::runtime_error("surf_replace_face: refs.surface is required");
                // The replacement surface is a BODY selector (a whole surface body) or a
                // FACE selector (a single face on any running body). Resolve either.
                TopoDS_Shape replacement;
                const std::string surf_owner = selector_owner_id(*ssel);
                if (!surf_owner.empty() && results.count(surf_owner) &&
                    std::holds_alternative<SolidResult>(*results.at(surf_owner)) &&
                    surf_owner != body_id) {
                    replacement = std::get<SolidResult>(*results.at(surf_owner)).shape;
                } else if (ssel->is_object() &&
                           ssel->value("kind", std::string()) == "face") {
                    for (const std::string& cand : solid_order) {
                        if (cand == body_id || consumed.count(cand)) continue;
                        if (!std::holds_alternative<SolidResult>(*results.at(cand))) continue;
                        const TopoDS_Shape& cb = std::get<SolidResult>(*results.at(cand)).shape;
                        if (cb.IsNull()) continue;
                        chudware::doc::FacePicks rp =
                            chudware::doc::resolve_face_picks(cb, *ssel, "surface");
                        if (!rp.faces.empty()) { replacement = rp.faces.front(); break; }
                    }
                }
                if (replacement.IsNull())
                    throw std::runtime_error("surf_replace_face: replacement surface did not resolve");
                std::vector<Eigen::Vector3d> cents;
                for (const TopoDS_Face& f : picks.faces)
                    cents.push_back(::geom::face_centroid(f));
                TopoDS_Shape healed =
                    ::geom::surf_replace_face_solid(solid, cents, replacement,
                                                    chudware::doc::bbox_diag(solid));
                if (healed.IsNull())
                    throw std::runtime_error("surf_replace_face: could not heal a solid");
                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{healed};
                solid_order.push_back(id);
                body_kinds[id] = "solid";
                st.ok = true;

            } else if (type == "surf_thicken") {
                auto ssel = feat.find("surface");
                if (ssel == feat.end())
                    throw std::runtime_error("surf_thicken: refs.surface is required");
                const std::string surf_owner = selector_owner_id(*ssel);
                const TopoDS_Shape& surface = require_solid(results, surf_owner, "surface");
                const double thickness = feat.contains("thickness")
                    ? num_or_expr(feat.at("thickness"), env) : 2.0;
                if (!(thickness > 0.0))
                    throw std::runtime_error("surf_thicken: thickness must be > 0");
                const std::string side = feat.value("side", std::string("one"));
                const bool flip = feat.value("flip", false);
                TopoDS_Shape solid =
                    ::geom::surf_thicken_solid(surface, thickness, side, flip);
                if (solid.IsNull())
                    throw std::runtime_error("surf_thicken: thickening failed");
                // The solid REPLACES the consumed surface body.
                consumed.insert(surf_owner);
                source_of[id] = surf_owner;
                results[id] = SolidResult{solid};
                solid_order.push_back(id);
                body_kinds[id] = "solid";
                st.ok = true;

            } else if (type == "thread") {
                // One-op THREAD: cut a real helical ISO-60° thread into a cylindrical
                // face (external shaft thread or internal bore thread). Params:
                //   size       "M8" -> ISO coarse pitch + major Ø (table below); OR a
                //              bare major_dia + pitch (custom thread).
                //   length     axial run of the thread (mm, from the face axis location).
                //   starts     thread starts (default 1; multi-start multiplies the lead).
                //   handedness "right" (default) / "left".
                //   style      "external" (default — shaft) / "internal" (bore).
                //   turns      optional hard cap on swept turns (robustness).
                //   mode       "cut" (default — real geometry) / "cosmetic" (no geometry,
                //              a lightweight representation that always succeeds).
                // Ref: target_face = the cylindrical face to thread (its axis + radius).
                //
                // ROBUSTNESS: helical booleans are fragile. cut_thread caps the turns,
                // uses a rounded (not razor-sharp) profile, BRepCheck-validates the cut,
                // and on ANY failure leaves the body UNTHREADED (the plain cylinder) with
                // a clear warning rather than breaking the part.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("thread: no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");

                // -- size: M-table lookup OR bare major_dia + pitch -------------------
                // ISO metric COARSE pitch + nominal major Ø (mm). Mirrors the catalog
                // (core/parts/catalog.cpp find_metric) for M1.6–M12 and extends the
                // standard coarse series through M24. A self-contained table keeps the
                // thread feature in the geom/doc layer without a new catalog link dep.
                struct MThread { const char* size; double major; double pitch; };
                // ISO 261 metric COARSE pitch series (major Ø mm, pitch mm), M1.6..M24.
                static const MThread kMetric[] = {
                    {"M1.6", 1.6, 0.35}, {"M2", 2.0, 0.40}, {"M2.5", 2.5, 0.45},
                    {"M3", 3.0, 0.50},   {"M4", 4.0, 0.70}, {"M5", 5.0, 0.80},
                    {"M6", 6.0, 1.00},   {"M8", 8.0, 1.25}, {"M10", 10.0, 1.50},
                    {"M12", 12.0, 1.75}, {"M14", 14.0, 2.00}, {"M16", 16.0, 2.00},
                    {"M18", 18.0, 2.50}, {"M20", 20.0, 2.50}, {"M22", 22.0, 2.50},
                    {"M24", 24.0, 3.00},
                };
                // ASME B1.1 Unified inch series — major Ø + pitch already in MM
                // (major_in * 25.4; pitch = 25.4 / TPI). Both UNC (coarse) and UNF (fine)
                // for the common #-number and fractional sizes. Match by the exact SAE
                // designation string ("1/4-20", "#10-32", "1/2-13", ...). The numbers are
                // cross-checked against research_thread_spec (ASME B1.1).
                static const MThread kUnified[] = {
                    // # number sizes (major = (0.060 + 0.013*num) in)
                    {"#0-80", 1.524, 0.31750}, {"#1-64", 1.854, 0.39688},
                    {"#2-56", 2.184, 0.45357}, {"#3-48", 2.515, 0.52917},
                    {"#4-40", 2.845, 0.63500}, {"#5-40", 3.175, 0.63500},
                    {"#6-32", 3.505, 0.79375}, {"#8-32", 4.166, 0.79375},
                    {"#10-24", 4.826, 1.05833}, {"#10-32", 4.826, 0.79375},
                    {"#12-24", 5.486, 1.05833}, {"#12-28", 5.486, 0.90714},
                    // fractional UNC
                    {"1/4-20", 6.350, 1.27000},  {"5/16-18", 7.938, 1.41111},
                    {"3/8-16", 9.525, 1.58750},  {"7/16-14", 11.113, 1.81429},
                    {"1/2-13", 12.700, 1.95385}, {"9/16-12", 14.288, 2.11667},
                    {"5/8-11", 15.875, 2.30909}, {"3/4-10", 19.050, 2.54000},
                    {"7/8-9", 22.225, 2.82222},  {"1-8", 25.400, 3.17500},
                    // fractional UNF
                    {"1/4-28", 6.350, 0.90714},  {"5/16-24", 7.938, 1.05833},
                    {"3/8-24", 9.525, 1.05833},  {"7/16-20", 11.113, 1.27000},
                    {"1/2-20", 12.700, 1.27000}, {"9/16-18", 14.288, 1.41111},
                    {"5/8-18", 15.875, 1.41111}, {"3/4-16", 19.050, 1.58750},
                    {"7/8-14", 22.225, 1.81429}, {"1-12", 25.400, 2.11667},
                };
                double major_dia = 0.0, pitch = 0.0;
                if (feat.contains("size") && feat.at("size").is_string()) {
                    std::string sz = feat.at("size").get<std::string>();
                    // Strip whitespace + a trailing " UNC"/" UNF"/" UN" series tag so
                    // "1/4-20 UNC" and "1/4-20" both match. Canonicalize a leading 'm'.
                    {
                        size_t sp = sz.find(' ');
                        if (sp != std::string::npos) sz = sz.substr(0, sp);
                    }
                    if (!sz.empty() && (sz[0] == 'm')) sz[0] = 'M';
                    bool found = false;
                    // Metric FINE: "M8x1", "M10X1.25" — major from the M-number, pitch
                    // given after the x. Handles any metric fine callout without a table.
                    if (!found && !sz.empty() && sz[0] == 'M') {
                        size_t xp = sz.find_first_of("xX");
                        if (xp != std::string::npos) {
                            try {
                                double maj = std::stod(sz.substr(1, xp - 1));
                                double pit = std::stod(sz.substr(xp + 1));
                                if (maj > 0.0 && pit > 0.0) {
                                    major_dia = maj; pitch = pit; found = true;
                                }
                            } catch (...) { /* fall through to table match */ }
                        }
                    }
                    for (const MThread& m : kMetric)
                        if (!found && sz == m.size) { major_dia = m.major; pitch = m.pitch; found = true; break; }
                    for (const MThread& m : kUnified)
                        if (!found && sz == m.size) { major_dia = m.major; pitch = m.pitch; found = true; break; }
                    if (!found)
                        throw std::runtime_error(
                            "thread: unknown size '" + sz + "' — use an ISO metric size "
                            "(M1.6..M24 coarse, or fine e.g. M8x1), a Unified inch size "
                            "(e.g. \"1/4-20\", \"#10-32\", \"1/2-13 UNC\"), or pass "
                            "major_dia + pitch directly");
                } else {
                    if (!feat.contains("major_dia") && !feat.contains("pitch"))
                        throw std::runtime_error(
                            "thread: provide a 'size' (e.g. \"M8\") OR both 'major_dia' and 'pitch'");
                    major_dia = feat.contains("major_dia") ? num_or_expr(feat.at("major_dia"), env) : 0.0;
                    pitch = feat.contains("pitch") ? num_or_expr(feat.at("pitch"), env) : 0.0;
                    if (!(major_dia > 0.0) || !(pitch > 0.0))
                        throw std::runtime_error("thread: major_dia and pitch must be > 0");
                }

                // LENGTH is OPTIONAL: blank / <= 0 ⇒ the thread spans the WHOLE cylindrical
                // face (its full axial extent, computed once the face is resolved below). A
                // positive value is an explicit run length (mm) from the start plane.
                double length =
                    feat.contains("length") ? num_or_expr(feat.at("length"), env) : 0.0;
                int starts = 1;
                if (feat.contains("starts")) {
                    double sv = num_or_expr(feat.at("starts"), env);
                    starts = std::max(1, static_cast<int>(std::lround(sv)));
                }
                const std::string handed = feat.value("handedness", std::string("right"));
                const bool lefthand = (handed == "left" || handed == "lh" || handed == "left_hand");
                const std::string style = feat.value("style", std::string("external"));
                const bool internal = (style == "internal" || style == "tapped" || style == "bore");
                // Thread FORM (cross-section): "v"/"iso"/"unified"/"metric" -> 60° V
                // (default); "acme" -> 29° trapezoid; "trap"/"trapezoidal" -> metric 30°
                // trapezoidal; "square". Accepts `form` or `profile`.
                ::geom::ThreadForm tform = ::geom::ThreadForm::V60;
                {
                    std::string fs = feat.contains("form") ? feat.value("form", std::string())
                                   : feat.value("profile", std::string());
                    for (auto& c : fs) c = static_cast<char>(std::tolower(c));
                    if (fs == "acme") tform = ::geom::ThreadForm::ACME;
                    else if (fs == "trap" || fs == "trapezoidal" || fs == "metric_trap")
                        tform = ::geom::ThreadForm::TRAP;
                    else if (fs == "square" || fs == "sq") tform = ::geom::ThreadForm::SQUARE;
                    // else V60 (also for "v", "iso", "unified", "un", "metric", "", unknown)
                }

                std::string tmode = feat.value("mode", std::string("cut"));
                // RAISED / EXTRUDE thread: fuse a helical ridge instead of cutting a
                // groove. In this mode the selected cylinder is the ROOT (minor) Ø and the
                // thread grows OUTWARD to the nominal major. Aliases: add/extrude/raised/
                // boss/external_raised -> raised geometry (the boolean mode is still "cut"
                // vs "cosmetic" for the run path below).
                bool raised = false;
                if (tmode == "add" || tmode == "extrude" || tmode == "raised" ||
                    tmode == "raise" || tmode == "boss" || tmode == "protrude" ||
                    tmode == "proud") {
                    raised = true;
                    tmode = "cut";  // real-geometry path (fuse selected via `raised`)
                }
                const double turns_cap =
                    feat.contains("turns") ? num_or_expr(feat.at("turns"), env) : 0.0;

                // COSMETIC: a lightweight representation — no geometry change. Succeed with
                // a warning (full drawing-callout integration is a separate follow-up).
                if (tmode == "cosmetic") {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                        "cosmetic thread %s%s (no geometry)",
                        (feat.contains("size") && feat.at("size").is_string())
                            ? feat.at("size").get<std::string>().c_str()
                            : "custom",
                        internal ? " internal" : "");
                    st.warning = buf;
                    // The body passes through UNCHANGED (a new id pointing at the same shape).
                    consumed.insert(body_id);
                    source_of[id] = body_id;
                    results[id] = SolidResult{body};
                    solid_order.push_back(id);
                    st.ok = true;

                } else {
                    if (tmode != "cut")
                        throw std::runtime_error(
                            "thread: unknown mode '" + tmode + "' (use \"cut\" or \"cosmetic\")");

                    // Resolve the target cylindrical face -> axis (loc, dir) + radius.
                    const json& fref = feat.contains("target_face") ? feat.at("target_face")
                                     : (feat.contains("face") ? feat.at("face") : json());
                    if (fref.is_null())
                        throw std::runtime_error(
                            "thread: a 'target_face' ref (the cylindrical face to thread) is required");
                    TopoDS_Face tface = resolve_face_sel(fref, "target_face");
                    BRepAdaptor_Surface ad(tface);
                    if (ad.GetType() != GeomAbs_Cylinder)
                        throw std::runtime_error(
                            "thread: target_face must be a CYLINDRICAL face (a shaft's outer "
                            "cylinder for external, the bore for internal)");
                    const gp_Ax1 cyl_axis = ad.Cylinder().Axis();
                    const double cyl_radius = ad.Cylinder().Radius();
                    gp_Pnt axis_loc = cyl_axis.Location();
                    gp_Dir axis_dir = cyl_axis.Direction();

                    // ── Thread START anchor, RUN direction, and LENGTH ──────────────────
                    // The thread runs from a start plane along the cylinder axis for
                    // `length` mm. Three optional inputs shape it:
                    //   from / start_ref  a vertex / edge / face / datum whose axial position
                    //                     is the thread START (default: the lower face end).
                    //   flip / reverse    run toward the OTHER end (start from the far end
                    //                     when there's no explicit `from`, reversing the dir);
                    //                     lets the user flip when the start reference isn't an
                    //                     auto-termination end of the face.
                    //   length            explicit run (mm); blank ⇒ span to the far face end.
                    // cut_thread clamps the band to the body, so an over-long run is safe.
                    const gp_Pnt cyl_loc = axis_loc;             // a point on the axis
                    const gp_Vec ad_v(axis_dir);
                    // Resolve a start-reference selector (vertex/edge/face/datum) to a point.
                    auto thread_ref_point = [&](const json& sref, gp_Pnt& out) -> bool {
                        auto center_of = [&](const TopoDS_Shape& sh) -> bool {
                            Bnd_Box b; BRepBndLib::Add(sh, b);
                            if (b.IsVoid()) return false;
                            double x0, y0, z0, x1, y1, z1; b.Get(x0, y0, z0, x1, y1, z1);
                            out = gp_Pnt((x0 + x1) * 0.5, (y0 + y1) * 0.5, (z0 + z1) * 0.5);
                            return true;
                        };
                        const std::string of = (sref.is_object() && sref.contains("of"))
                                                   ? sref.value("of", std::string())
                                                   : std::string();
                        try {
                            if (of == "edge")   return center_of(resolve_edge_sel(sref, "from"));
                            if (of == "face")   return center_of(resolve_face_sel(sref, "from"));
                            if (of == "vertex" || is_datum_ref(sref)) {
                                Eigen::Vector3d v = vertex_point(sref, "from");
                                out = gp_Pnt(v.x(), v.y(), v.z()); return true;
                            }
                            // Unknown/absent kind: try vertex, then edge, then face.
                            try { Eigen::Vector3d v = vertex_point(sref, "from");
                                  out = gp_Pnt(v.x(), v.y(), v.z()); return true; } catch (...) {}
                            try { return center_of(resolve_edge_sel(sref, "from")); } catch (...) {}
                            return center_of(resolve_face_sel(sref, "from"));
                        } catch (...) { return false; }
                    };
                    // Face axial extent [s_min, s_max] (signed distance along axis_dir from cyl_loc).
                    double s_min = 1e300, s_max = -1e300;
                    {
                        Bnd_Box fb;
                        BRepBndLib::Add(tface, fb);
                        double x0, y0, z0, x1, y1, z1;
                        fb.Get(x0, y0, z0, x1, y1, z1);
                        for (int i = 0; i < 8; ++i) {
                            gp_Pnt c((i & 1) ? x1 : x0, (i & 2) ? y1 : y0, (i & 4) ? z1 : z0);
                            const double s = gp_Vec(cyl_loc, c).Dot(ad_v);
                            s_min = std::min(s_min, s); s_max = std::max(s_max, s);
                        }
                    }
                    // Optional START reference → clamped axial coordinate.
                    bool have_from = false; double s_from = 0.0;
                    {
                        const json& sref = feat.contains("from") ? feat.at("from")
                                         : (feat.contains("start_ref") ? feat.at("start_ref")
                                         : (feat.contains("start") ? feat.at("start") : json()));
                        gp_Pnt rp;
                        if (!sref.is_null() && thread_ref_point(sref, rp)) {
                            s_from = gp_Vec(cyl_loc, rp).Dot(ad_v);
                            s_from = std::max(s_min, std::min(s_max, s_from));
                            have_from = true;
                        }
                    }
                    const bool flip = feat.value("flip", false) ||
                                      feat.value("reverse", false) ||
                                      feat.value("flip_direction", false);
                    double s_start; gp_Dir run_dir;
                    if (!flip) {
                        run_dir = axis_dir;
                        s_start = have_from ? s_from : s_min;
                        if (!(length > 0.0)) length = s_max - s_start;   // span to the far end
                    } else {
                        run_dir = axis_dir.Reversed();
                        s_start = have_from ? s_from : s_max;
                        if (!(length > 0.0)) length = s_start - s_min;   // span to the far end
                    }
                    if (!(length > 1e-6))
                        throw std::runtime_error(
                            "thread: computed a zero-length run — the start reference sits at "
                            "the end of the face, or the face has no axial extent");
                    axis_loc = cyl_loc.Translated(ad_v.Multiplied(s_start));

                    // DRAFT PREVIEW (params.preview / draft = true): the GUI's live preview
                    // path. The real helical boolean costs 1–3 s (the profiled OCCT floor),
                    // which is unusable per-drag-tick. Instead show the thread's EXTENT as a
                    // plain turned band — cylinder/plane booleans, ~ms — so start/flip/length
                    // edits respond live. The PM re-applies WITHOUT the flag on OK, so real
                    // geometry always replaces this before the feature is kept.
                    const bool draft_preview =
                        feat.value("preview", false) || feat.value("draft", false);
                    if (draft_preview) {
                        // Visual depth ≈ the V-thread cut depth, clamped so a mismatched
                        // size can't eat the whole shaft.
                        const double vd = std::min(0.6134 * pitch, 0.45 * cyl_radius);
                        const gp_Ax2 band_ax(axis_loc, run_dir);
                        TopoDS_Shape out;
                        try {
                            if (raised) {
                                // Raised: band grows outward to the nominal major Ø.
                                const double r = std::max(major_dia * 0.5, cyl_radius + 0.05);
                                TopoDS_Shape tool =
                                    BRepPrimAPI_MakeCylinder(band_ax, r, length).Shape();
                                out = BRepAlgoAPI_Fuse(body, tool).Shape();
                            } else if (internal) {
                                // Bore: enlarge the hole over the band (solid tool — the
                                // bore's void core intersects nothing extra).
                                TopoDS_Shape tool = BRepPrimAPI_MakeCylinder(
                                    band_ax, cyl_radius + vd, length).Shape();
                                out = BRepAlgoAPI_Cut(body, tool).Shape();
                            } else {
                                // Shaft: turn the band down to the approximate minor Ø. The
                                // tool is an ANNULUS (outer > shaft, inner = minor) so the
                                // core of the shaft is untouched.
                                TopoDS_Shape outer = BRepPrimAPI_MakeCylinder(
                                    band_ax, cyl_radius + vd + 1.0, length).Shape();
                                TopoDS_Shape inner = BRepPrimAPI_MakeCylinder(
                                    band_ax, std::max(cyl_radius - vd, 1e-3), length).Shape();
                                TopoDS_Shape ring = BRepAlgoAPI_Cut(outer, inner).Shape();
                                out = BRepAlgoAPI_Cut(body, ring).Shape();
                            }
                        } catch (...) { out.Nullify(); }
                        if (out.IsNull()) out = body;  // never break the preview chain
                        consumed.insert(body_id);
                        source_of[id] = body_id;
                        results[id] = SolidResult{out};
                        solid_order.push_back(id);
                        st.ok = true;
                    } else {
                    ::geom::ThreadResult tr = ::geom::cut_thread(
                        body, axis_loc, run_dir, cyl_radius, pitch, major_dia, length,
                        starts, lefthand, internal, raised, turns_cap, tform);
                    if (tr.shape.IsNull())
                        throw std::runtime_error(
                            tr.warning.empty() ? std::string("thread: produced an empty shape")
                                               : tr.warning);
                    if (!tr.warning.empty()) st.warning = tr.warning;

                    // The threaded (or, on a fall-back, unthreaded) body REPLACES the input.
                    consumed.insert(body_id);
                    source_of[id] = body_id;
                    results[id] = SolidResult{tr.shape};
                    solid_order.push_back(id);
                    st.ok = true;
                    }
                }

            } else if (type == "text") {
                // One-op TEXT: emboss (raise) or engrave (recess) a string onto a PLANAR
                // face. Params:
                //   text     the string to mark (required). Aliases: string, value.
                //   height   cap/ascender height of the text (mm, default 5).
                //   depth    emboss height / engrave depth (mm, default 0.5).
                //   mode     "engrave" (default — recessed) / "emboss" (raised). Aliases:
                //            cut/recess/sunk -> engrave; raised/raise/boss/proud -> emboss.
                //   font     optional font name ("rubik"/"mono"/"bold"/"medium") or an
                //            absolute .ttf path; defaults to the bundled Rubik-Regular.
                //   halign   "left"/"center"(default)/"right"; valign "baseline"/
                //            "center"(default)/"top".
                //   offset   optional [du, dv] in-plane shift (mm) from the face centre.
                // Ref: target_face = the planar face to mark.
                //
                // ROBUSTNESS: font rendering + booleans are fragile. geom::emboss_text
                // BRepCheck-validates the result and, on ANY failure, leaves the body
                // UNMARKED (the clean part) with a clear warning rather than corrupting it.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("text: no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");

                std::string str;
                for (const char* k : {"text", "string", "value"})
                    if (feat.contains(k) && feat.at(k).is_string()) {
                        str = feat.at(k).get<std::string>();
                        break;
                    }
                if (str.empty())
                    throw std::runtime_error("text: a non-empty 'text' string is required");

                const double height =
                    feat.contains("height")   ? num_or_expr(feat.at("height"), env)
                    : feat.contains("size")   ? num_or_expr(feat.at("size"), env)
                                              : 5.0;
                const double depth =
                    feat.contains("depth") ? num_or_expr(feat.at("depth"), env) : 0.5;
                if (!(height > 0.0))
                    throw std::runtime_error("text: 'height' (mm) must be > 0");
                if (!(depth > 0.0))
                    throw std::runtime_error("text: 'depth' (mm) must be > 0");

                const std::string mode = feat.value("mode", std::string("engrave"));
                const bool engrave = !(mode == "emboss" || mode == "raised" ||
                                       mode == "raise" || mode == "boss" ||
                                       mode == "proud" || mode == "out" ||
                                       mode == "extrude");

                auto align_code = [](const std::string& s, int dflt) -> int {
                    if (s == "left" || s == "baseline" || s == "bottom") return 0;
                    if (s == "center" || s == "centre" || s == "middle") return 1;
                    if (s == "right" || s == "top") return 2;
                    return dflt;
                };
                const int halign = align_code(feat.value("halign", std::string()), 1);
                const int valign = align_code(feat.value("valign", std::string()), 1);

                // Resolve the planar target face -> outward frame (origin, normal, xdir).
                const json& fref = feat.contains("target_face") ? feat.at("target_face")
                                 : (feat.contains("face") ? feat.at("face") : json());
                if (fref.is_null())
                    throw std::runtime_error(
                        "text: a 'target_face' ref (the planar face to mark) is required");
                TopoDS_Face tface = resolve_face_sel(fref, "target_face");
                BRepAdaptor_Surface ad(tface);
                if (ad.GetType() != GeomAbs_Plane)
                    throw std::runtime_error(
                        "text: target_face must be a PLANAR face (emboss/engrave marks a "
                        "flat surface)");
                gp_Pln pln = ad.Plane();
                gp_Dir normal = ::chudware::geom::plane_outward_normal(tface, pln);
                gp_Dir xdir = pln.XAxis().Direction();
                // READING DIRECTION. A planar face's parametric X axis is arbitrary — on a
                // VERTICAL wall (a box side) OCCT's X often points along world Z, which would
                // lay the text out VERTICALLY and overflow a short wall (e.g. "Chudware" on a
                // 60x10 front face -> detached/overhang). For any non-horizontal face, override
                // xdir to the HORIZONTAL in-plane direction so the text reads left-to-right with
                // "up" toward +Z, the way a person letters a side face. Horizontal faces
                // (normal ~ +/-Z) keep the parametric X (already world-X on a box top).
                {
                    const gp_Dir worldZ(0.0, 0.0, 1.0);
                    const double ndotz = normal.Dot(worldZ);
                    if (std::abs(ndotz) < 0.999) {
                        gp_Vec up(gp_Vec(worldZ) - gp_Vec(normal).Multiplied(ndotz));
                        if (up.Magnitude() > 1e-7) {
                            up.Normalize();
                            gp_Vec xd = up.Crossed(gp_Vec(normal));  // xdir = up x normal
                            if (xd.Magnitude() > 1e-7) xdir = gp_Dir(xd);
                        }
                    }
                }

                GProp_GProps fprops;
                BRepGProp::SurfaceProperties(tface, fprops);
                gp_Pnt origin = fprops.CentreOfMass();

                // Optional in-plane offset [du, dv] from the face centre (dv along the
                // right-handed up = normal x xdir, matching the glyph builder's +Y).
                if (feat.contains("offset") && feat.at("offset").is_array() &&
                    feat.at("offset").size() >= 2) {
                    const double du = num_or_expr(feat.at("offset").at(0), env);
                    const double dv = num_or_expr(feat.at("offset").at(1), env);
                    gp_Vec vx(xdir), vy(gp_Vec(normal).Crossed(gp_Vec(xdir)));
                    origin.Translate(vx.Multiplied(du).Added(vy.Multiplied(dv)));
                }

                const std::string font_path =
                    ::geom::resolve_font_path(feat.value("font", std::string()));

                ::geom::TextResult tx = ::geom::emboss_text(
                    body, origin, normal, xdir, str, font_path, height, depth, engrave,
                    halign, valign);
                if (tx.shape.IsNull())
                    throw std::runtime_error(
                        tx.warning.empty() ? std::string("text: produced an empty shape")
                                           : tx.warning);
                if (!tx.warning.empty()) st.warning = tx.warning;

                // The marked (or, on a fall-back, unmarked) body REPLACES the input.
                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{tx.shape};
                solid_order.push_back(id);
                st.ok = true;

            } else {
                // Forward-tolerant: an unknown feature type errors but the rebuild
                // continues (mirrors the Python's "no compiler for type" path).
                throw std::runtime_error("no compiler for feature type '" + type + "'");
            }

            // Bug N2 — no committed solid may have negative volume. Validate (and, if
            // possible, heal) every solid body THIS feature produced. An unhealable
            // inverted solid throws here -> the per-feature catch turns it into a loud
            // error status instead of silently committing a corrupt B-rep.
            for (std::size_t k = solids_before; k < solid_order.size(); ++k) {
                const std::string& nsid = solid_order[k];
                if (consumed.count(nsid)) continue;  // already superseded in this iteration
                if (sanitize_solid(nsid, raw.value("name", id)))
                    st.warning = st.warning.empty()
                        ? std::string("healed an inverted solid (shell orientation fixed)")
                        : (st.warning + "; healed an inverted solid (shell orientation fixed)");
            }

            // -- SYSTEMIC BACKSTOP (Bug B6 class): no feature may VANISH or silently
            // empty the document. The per-branch guards above each catch their own
            // degenerate case (fillet open-shell S2, sweep zero-volume N-9, buried boss,
            // ...), but a NEW branch (or an OCCT quirk a branch's guard missed) could
            // still (a) commit a NULL / face-less B-rep, or (b) consume the only body and
            // leave the document with ZERO solids. This final, branch-independent check
            // turns BOTH into a LOUD error status (caught just below) instead of a silent
            // "document has no bodies" surfacing later in eval_mass / eval_check. It is
            // deliberately TRIGGERED ONLY by a null/empty RESULT or a was-non-empty ->
            // now-empty transition (never a blanket volume/shape rule) so it cannot reject
            // a valid feature (e.g. a legitimate concave-edge fillet that grows volume).
            {
                // On a violation, RESTORE the pre-feature topology bookkeeping
                // (`consumed` set + `solid_order` length) so the prior body stays alive in
                // the surviving-bodies tessellation, THEN throw -> the per-feature catch
                // records a loud error status. Without this restore the failing feature
                // would orphan the body it had already consumed.
                auto fail_backstop = [&](const std::string& msg) {
                    consumed = consumed_snapshot;
                    if (solid_order.size() > solids_before)
                        solid_order.resize(solids_before);
                    throw std::runtime_error(msg);
                };
                // (a) Any SOLID this feature produced (and did not itself supersede) must
                //     be a real, non-empty B-rep. A null shape or a zero-face shell is a
                //     degenerate commit — fail loudly rather than ship an empty body that
                //     later reads as "no bodies".
                for (std::size_t k = solids_before; k < solid_order.size(); ++k) {
                    const std::string& nsid = solid_order[k];
                    if (consumed.count(nsid)) continue;
                    auto rit = results.find(nsid);
                    if (rit == results.end() ||
                        !std::holds_alternative<SolidResult>(*rit->second))
                        continue;
                    const TopoDS_Shape& sh = std::get<SolidResult>(*rit->second).shape;
                    // An import may legitimately carry reference curves/points (a STEP's
                    // construction wires, a DXF of open lines): face-less but not empty.
                    const bool imported_reference =
                        type == "import" && !sh.IsNull() &&
                        (TopExp_Explorer(sh, TopAbs_EDGE).More() ||
                         TopExp_Explorer(sh, TopAbs_VERTEX).More());
                    if (sh.IsNull() || (::geom::shape_face_count(sh) == 0 && !imported_reference))
                        fail_backstop(
                            type + " produced an empty/degenerate body (no geometry) — "
                            "refusing to commit a null result that would leave the "
                            "document with a vanished body");
                }
                // (b) A feature that left the document with ZERO alive solids when there
                //     was at least one before has dissolved the part. Prior features'
                //     consumptions are already reflected in alive_before (a body a PRIOR
                //     feature consumed is not counted alive), so the only way alive can
                //     drop to zero here is THIS feature consuming the last alive body
                //     without producing a surviving replacement (features only ever ADD to
                //     the consumed set). This is the was-non-empty -> now-empty trigger;
                //     it never fires on a doc that was already empty, nor on a feature that
                //     produced a valid new body.
                std::size_t alive_after = 0;
                for (const std::string& s : solid_order)
                    if (!consumed.count(s)) ++alive_after;
                if (alive_before > 0 && alive_after == 0)
                    fail_backstop(
                        type + " emptied the document — it removed the last body without "
                        "producing a valid replacement (a dress/sweep/cut must never leave "
                        "zero bodies). Check the feature's size/placement.");
            }
        } catch (const std::exception& ex) {
            st.ok = false;
            st.error = ex.what();
        } catch (...) {
            st.ok = false;
            st.error = "unknown error during feature compile";
        }

        // DUPLICATE-NAME warning (Bug K9): a second feature with the same display name is
        // accepted (no hard error — that would break existing flows), but flagged so the
        // user/host can disambiguate the tree. Only a feature that compiled is tracked;
        // an existing feature-specific warning is preserved (the name note is appended).
        if (st.ok) {
            const std::string fname = raw.value("name", id);
            if (!fname.empty()) {
                if (!seen_names.insert(fname).second) {
                    const std::string note =
                        "duplicate feature name '" + fname + "' in this document";
                    st.warning = st.warning.empty() ? note : (st.warning + "; " + note);
                }
            }
        }

        st.ms = std::chrono::duration<double, std::milli>(
                    std::chrono::steady_clock::now() - _feat_t0).count();
        status_ptrs.push_back(std::make_shared<const FeatureStatus>(st));
        result.statuses.push_back(std::move(st));
        // Attribute owners only for solids THIS feature produced (the
        // reused prefix already carries face_owners/edge_owners from its checkpoint).
        // A plain geometry-only component compilation never consumes picking ownership.
        if (attribute_owners && st.ok) {
            for (std::size_t k = solids_before; k < solid_order.size(); ++k)
                update_owners_for(solid_order[k], op_histories.histories());
        }
        capture_ckpt(_fi + 1);
    }

    result.loop_ms = std::chrono::duration<double, std::milli>(
                         std::chrono::steady_clock::now() - _loop_t0).count();

    // -------------------------------------------------- tessellate surviving bodies
    // Surviving = solid results whose id was never consumed by a later feature, in
    // first-produced order. body_id runs 1..N; the feature id is the body name.
    const auto _tess_t0 = std::chrono::steady_clock::now();
    ::geom::tess_timing_reset();
    std::vector<aicm::AicmBody> bodies;
    const float color[3] = {0.71f, 0.76f, 0.82f};  // default dielectric grey
    uint32_t body_num = 1;
    const bool has_appearances = doc.contains("appearances") && doc["appearances"].is_object();
    const auto face_appearances = doc.find("face_appearances");
    // Per-TShape stats cache: volume/area/face-count/edge-count are geometry-only (location-
    // invariant), so instanced bodies that SHARE a TShape (e.g. the 4 wheels) compute the
    // mass props ONCE. Keyed by (TShape*, is_surface) so a surface vs solid use of the same
    // geometry never crosses wires. (Tessellation dedups for free — BRepMesh is incremental,
    // so meshing the second instance finds the shared faces already triangulated and skips.)
    std::map<std::pair<const void*, bool>, std::tuple<double, double, int, int>> body_stats_cache;
    // Cross-compile per-body cache (see CachedBody/CompileCache). `use_cache` already
    // gates the whole incremental machinery; CHUDWARE_TESS_CACHE=0 disables just this layer
    // for A/B. new_body_cache holds the bodies THIS compile produced/reused (replaces the
    // old set wholesale so a discarded body's mesh is freed).
    static const bool tess_cache_on = []() {
        const char* v = std::getenv("CHUDWARE_TESS_CACHE");
        return !(v && v[0] == '0' && v[1] == '\0');
    }();
    CompileCache* bc = (use_cache && tess_cache_on && with_display) ? cache : nullptr;
    std::map<std::tuple<const void*, bool, uint64_t>,
             std::shared_ptr<const CachedBody>> new_body_cache;
    // Bit-exact hash of a body's baked appearance (color+metal+rough) PLUS the display
    // quality tier, so a set_appearance change or a display-quality change is a cache
    // miss while geometry-identical bodies with identical appearance+quality still hit.
    auto appearance_hash = [quality](const float col[3], float metal, float rough) -> uint64_t {
        uint64_t h = 1469598103934665603ULL;
        const float vals[5] = {col[0], col[1], col[2], metal, rough};
        for (float f : vals) {
            uint32_t bits;
            std::memcpy(&bits, &f, sizeof(bits));
            h = (h ^ bits) * 1099511628211ULL;
        }
        h = (h ^ static_cast<uint64_t>(quality)) * 1099511628211ULL;
        return h;
    };
    // with_owners: per-shape owner maps, index-aligned with result.shapes.
    auto note_shape_owners = [&](const std::string& sid) {
        if (!with_owners) return;
        auto fo = face_owners.find(sid);
        auto eo = edge_owners.find(sid);
        result.shape_face_owners.push_back(fo == face_owners.end() ? std::map<int, std::string>{}
                                                                   : fo->second);
        result.shape_edge_owners.push_back(eo == edge_owners.end() ? std::map<int, std::string>{}
                                                                   : eo->second);
    };
    for (const std::string& sid : solid_order) {
        if (consumed.count(sid)) continue;
        const TopoDS_Shape& shape = std::get<SolidResult>(*results.at(sid)).shape;
        if (shape.IsNull()) continue;
        try {
            if (!with_display) {
                // Components need the same per-body Fine triangulation as the
                // editable part, but never consume this part's display packet.
                const auto defl = ::geom::display_deflections(quality, shape);
                ::geom::mesh_body(shape, defl.lin, defl.ang);
                result.shapes.push_back(shape);
                note_shape_owners(sid);
                ++body_num;
                continue;
            }
            // Per-body appearance (C26 colour + C32-A material) baked into the AICM so the host
            // render + doc_render_png reflect a set_appearance op. Default dielectric grey unless
            // doc["appearances"]["b<body_num>"] overrides.
            float bcolor[3] = {color[0], color[1], color[2]};
            float bmetal = 0.0f, brough = 0.5f;
            if (has_appearances) apply_doc_appearance(doc, body_num, bcolor, bmetal, brough);

            auto kit = body_kinds.find(sid);
            const std::string kind = (kit != body_kinds.end()) ? kit->second : std::string("solid");
            const bool is_surface = (kind == "surface");

            // Cacheable iff we have a cache AND the body sits at an identity top-level
            // location with forward orientation (the part-compile norm) — so its extracted
            // mesh (which bakes per-face locations) is a pure function of the TShape.
            const bool cacheable = bc != nullptr && shape.Location().IsIdentity() &&
                                   shape.Orientation() == TopAbs_FORWARD;
            std::tuple<const void*, bool, uint64_t> ck;
            if (cacheable)
                ck = std::make_tuple(static_cast<const void*>(shape.TShape().get()),
                                     is_surface, cc_fnv1a(
                                         face_appearances != doc.end() &&
                                                 face_appearances->contains("b" + std::to_string(body_num))
                                             ? face_appearances->at("b" + std::to_string(body_num)).dump()
                                             : std::string(),
                                         appearance_hash(bcolor, bmetal, brough)));

            std::shared_ptr<const CachedBody> hit;
            if (cacheable) {
                auto it = bc->body_cache.find(ck);
                if (it != bc->body_cache.end()) hit = it->second;
            }

            if (hit) {
                // Reuse: COW mesh buffers — assignment shares pos/nrm/idx;
                // only body_id/name are re-stamped (cheap scalars/string).
                aicm::AicmBody b = hit->body;
                b.body_id = body_num;
                b.name = sid;
                bodies.push_back(std::move(b));
                result.shapes.push_back(shape);
                note_shape_owners(sid);
                BodyInfo bi = hit->info;
                bi.name = sid;
                result.volume += bi.volume;
                result.bodies_info.push_back(bi);
                new_body_cache.emplace(ck, hit);
                g_cc_bodies_reused.fetch_add(1, std::memory_order_relaxed);
                ++body_num;
                continue;
            }

            // Fresh tessellate + stats.
            // Display-quality tier (`quality`, default Fine) picks BOTH BRepMesh
            // tolerances: lin = clamp(bbox_diag * rel, floor, ceil) mm and ang in
            // radians (see geom::display_deflections for the per-tier numbers). This
            // replaced a hardcoded lin=auto / ang=0.35 rad, which faceted imported
            // real B-rep visibly — a 2.6 m frame meshed at a 1.0 mm chord with ~18
            // segments per circle. The tier is folded into the body-cache key above,
            // so re-compiling at a new quality re-meshes instead of reusing.
            g_cc_bodies_tessellated.fetch_add(1, std::memory_order_relaxed);
            const ::geom::DisplayDeflections defl =
                ::geom::display_deflections(quality, shape);
            aicm::AicmBody b = ::geom::tessellate_body(shape, body_num, sid, bcolor,
                                                       defl.lin, defl.ang,
                                                       bmetal, brough);
            apply_doc_face_appearances(doc, body_num, shape, b);
            result.shapes.push_back(shape);  // AICM order: shapes[i] <-> body ordinal i+1
            note_shape_owners(sid);
            ++body_num;
            // Per-body stats: SURFACE bodies report meaningful AREA + face/edge counts
            // (volume reported as 0, matching the oracle — VolumeProperties on an OPEN
            // shell yields a meaningless signed value); SOLID bodies report VOLUME. The
            // surfacing gate reads these (tools/cpp_surf_parity.py). Additive — existing
            // solid fixtures are unaffected (default kind "solid" -> volume accumulates).
            BodyInfo bi;
            bi.name = sid;
            bi.kind = kind;
            double vol = 0.0, area = 0.0;
            int nf = 0, ne = 0;
            const auto bkey = std::make_pair(
                static_cast<const void*>(shape.TShape().get()), is_surface);
            auto bsit = body_stats_cache.find(bkey);
            if (bsit != body_stats_cache.end()) {
                std::tie(vol, area, nf, ne) = bsit->second;  // shared geometry — reuse
            } else {
                if (!is_surface) {
                    GProp_GProps gp;
                    // Prefer triangulation written by tessellate_body just above:
                    // ~3× faster than exact Gauss VolumeProperties on meshed solids.
                    // Falls back to exact geometry
                    // when no triangulation is attached. Relative error ≪ 0.1% —
                    // display/stats only; mass eval still uses exact VolumeProperties.
                    BRepGProp::VolumeProperties(shape, gp,
                                                /*OnlyClosed=*/Standard_False,
                                                /*SkipShared=*/Standard_False,
                                                /*UseTriangulation=*/Standard_True);
                    vol = gp.Mass();
                }
                area = ::geom::shape_area(shape);
                nf = ::geom::shape_face_count(shape);
                ne = ::geom::shape_edge_count(shape);
                body_stats_cache.emplace(bkey, std::make_tuple(vol, area, nf, ne));
            }
            result.volume += vol;  // only solids contribute to the total volume signal
            bi.volume = vol;
            bi.area = area;
            bi.n_faces = nf;
            bi.n_edges = ne;
            if (cacheable) {
                auto cbdy = std::make_shared<CachedBody>();
                cbdy->body = b;  // copy the just-tessellated mesh before it is moved out
                cbdy->info = bi;
                new_body_cache.emplace(ck, std::move(cbdy));
            }
            bodies.push_back(std::move(b));
            result.bodies_info.push_back(std::move(bi));
        } catch (...) {
            // A tessellation failure must not crash the rebuild; skip this body.
        }
    }
    if (bc) bc->body_cache = std::move(new_body_cache);

    result.body_count = static_cast<int>(with_display ? bodies.size() : result.shapes.size());
    const auto _write_t0 = std::chrono::steady_clock::now();
    if (with_display) result.aicm = aicm::write_aicm(bodies);
    result.tess_write_ms = std::chrono::duration<double, std::milli>(
                               std::chrono::steady_clock::now() - _write_t0).count();
    result.tess_ms = std::chrono::duration<double, std::milli>(
                         std::chrono::steady_clock::now() - _tess_t0).count();
    {
        const ::geom::TessTiming tt = ::geom::tess_timing_read();
        result.tess_mesh_ms = tt.mesh_ms;
        result.tess_extract_ms = tt.extract_ms;
    }
    const auto _owner_t0 = std::chrono::steady_clock::now();

    // -------------------------------------------------- owner map (host picking)
    // face_owners / edge_owners are maintained incrementally and restored
    // from CompileCkpt for the reused prefix — this phase only INVERTS them into the
    // host-facing {fid:{bodies,faces,edges}} map for surviving AICM bodies. On a
    // pure-prefix edit owner_ms collapses to this cheap inversion.
    if (with_display) {
        json om = json::object();
        auto ensure = [&](const std::string& fid) -> json& {
            if (!om.contains(fid))
                om[fid] = {{"bodies", json::array()}, {"faces", json::array()}, {"edges", json::array()}};
            return om[fid];
        };
        for (const aicm::AicmBody& b : bodies) {  // ordinal = b.body_id (AICM order)
            const std::string& sid = b.name;
            std::set<std::string> body_owners;
            auto fo_it = face_owners.find(sid);
            if (fo_it != face_owners.end()) {
                for (const auto& kv : fo_it->second) {
                    ensure(kv.second)["faces"].push_back({b.body_id, kv.first});
                    body_owners.insert(kv.second);
                }
            }
            auto eo_it = edge_owners.find(sid);
            if (eo_it != edge_owners.end()) {
                for (const auto& kv : eo_it->second)
                    ensure(kv.second)["edges"].push_back({b.body_id, kv.first});
            }
            for (const std::string& fid : body_owners)
                ensure(fid)["bodies"].push_back(b.body_id);
        }
        result.owner_map = std::move(om);
    }
    result.owner_ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - _owner_t0).count();

    // --- displayable reference geometry (datum axis/plane/point/csys) -----------
    // Datums build no body, so they never reach the AICM. Surface each resolved datum's geometry
    // (keyed by feature, in tree order) so the host can draw it as a reference overlay. A feature
    // that errored has no entry in `results`, so it is simply skipped (no datum drawn).
    for (const json& raw : with_display && features.is_array() ? features : empty) {
        if (!raw.is_object()) continue;
        const std::string id = raw.value("id", std::string());
        if (id.empty()) continue;
        auto rit = results.find(id);
        if (rit == results.end()) continue;
        json d;
        if (std::holds_alternative<AxisResult>(*rit->second)) {
            const AxisResult& a = std::get<AxisResult>(*rit->second);
            d = {{"kind", "axis"},
                 {"point", {a.point.x(), a.point.y(), a.point.z()}},
                 {"dir",   {a.dir.x(),   a.dir.y(),   a.dir.z()}}};
        } else if (std::holds_alternative<PlaneResult>(*rit->second)) {
            const PlaneResult& p = std::get<PlaneResult>(*rit->second);
            const chudware::geom::SketchPlane& pl = p.plane;
            d = {{"kind", p.is_csys ? "csys" : "plane"},
                 {"origin", {pl.origin.x(), pl.origin.y(), pl.origin.z()}},
                 {"x_dir",  {pl.uAxis.x(),  pl.uAxis.y(),  pl.uAxis.z()}},
                 {"y_dir",  {pl.vAxis.x(),  pl.vAxis.y(),  pl.vAxis.z()}},
                 {"normal", {pl.normal.x(), pl.normal.y(), pl.normal.z()}}};
        } else if (std::holds_alternative<PointResult>(*rit->second)) {
            const PointResult& pt = std::get<PointResult>(*rit->second);
            d = {{"kind", "point"}, {"point", {pt.pt.x(), pt.pt.y(), pt.pt.z()}}};
        } else if (std::holds_alternative<CurveResult>(*rit->second)) {
            // A curve feature (helix / spiral / spline) builds no body, so ship it
            // out-of-band like a datum: lift its local wire into world space and
            // sample it into a polyline the host can draw AND pick as a sweep path.
            const CurveResult& cr = std::get<CurveResult>(*rit->second);
            if (cr.wire.IsNull()) continue;
            const chudware::geom::SketchPlane& b = cr.base;
            gp_Trsf lift;
            lift.SetValues(
                b.uAxis.x(), b.vAxis.x(), b.normal.x(), b.origin.x(),
                b.uAxis.y(), b.vAxis.y(), b.normal.y(), b.origin.y(),
                b.uAxis.z(), b.vAxis.z(), b.normal.z(), b.origin.z());
            TopoDS_Shape lifted = BRepBuilderAPI_Transform(cr.wire, lift, true).Shape();
            json pts = json::array();
            for (TopExp_Explorer ex(lifted, TopAbs_EDGE); ex.More(); ex.Next()) {
                try {
                    BRepAdaptor_Curve crv(TopoDS::Edge(ex.Current()));
                    GCPnts_TangentialDeflection disc(crv, 0.05, 0.05);
                    for (int ni = 1; ni <= disc.NbPoints(); ++ni) {
                        gp_Pnt p = disc.Value(ni);
                        pts.push_back({p.X(), p.Y(), p.Z()});
                    }
                } catch (const Standard_Failure&) {}
            }
            if (pts.empty()) continue;
            d = {{"kind", "curve"}, {"points", std::move(pts)}};
        } else {
            continue;  // sketch / solid results are not reference datums
        }
        d["id"] = id;
        d["name"] = raw.value("name", id);
        result.datums.push_back(std::move(d));
    }

    // ok iff every feature compiled cleanly.
    result.ok = true;
    for (const FeatureStatus& s : result.statuses)
        if (!s.ok) { result.ok = false; break; }

    // Commit this compile into the cache: the new chain + per-feature checkpoints
    // replace the old wholesale (content-addressed, so replacing never loses anything
    // a future compile could still legally reuse — a diverged prefix can't match), and
    // the full result is kept for the identical-inputs fast path. Stored statuses are
    // scrubbed of the `reused` flag so a later restore re-marks exactly its own prefix.
    if (use_cache) {
        auto stored = std::make_shared<CompileResult>(result);
        for (FeatureStatus& fst : stored->statuses) fst.reused = false;
        stored->features_reused = 0;
        stored->features_executed = static_cast<int>(n_features);
        cache->chain = std::move(chain);
        cache->snaps = std::move(new_snaps);
        cache->full_key = cc_full_key(doc, cache->chain.back());
        cache->last_result = std::move(stored);
        cache->stamp = cc_next_stamp();
    }

    return result;
}

// ----------------------------------------------------------------- resolve_pick
// Overload over an ALREADY-compiled document — lets a caller that holds a cached CompileResult
// (e.g. an open session) resolve a pick without recompiling the whole feature tree per click.
nlohmann::json resolve_pick(const CompileResult& r, int body_id,
                            const std::string& kind, int topo_id) {
    if (body_id < 1 || body_id > static_cast<int>(r.shapes.size()))
        return json(nullptr);
    const TopoDS_Shape& shape = r.shapes[body_id - 1];

    // Owner = the feature that owns (body_id, topo_id) of this kind, from the map.
    std::optional<std::string> owner;
    const char* arrkey = (kind == "face") ? "faces" : (kind == "edge") ? "edges" : nullptr;
    if (arrkey && r.owner_map.is_object()) {
        for (auto it = r.owner_map.begin(); it != r.owner_map.end() && !owner; ++it) {
            const json& rec = it.value();
            if (!rec.contains(arrkey) || !rec[arrkey].is_array()) continue;
            for (const json& pr : rec[arrkey])
                if (pr.is_array() && pr.size() == 2 &&
                    pr[0].get<int>() == body_id && pr[1].get<int>() == topo_id) {
                    owner = it.key();
                    break;
                }
        }
    }
    return chudware::doc::make_pick_selector(shape, kind, topo_id, owner);
}

nlohmann::json resolve_pick(const json& doc, const json& env, int body_id,
                            const std::string& kind, int topo_id) {
    return resolve_pick(compile_document(doc, env), body_id, kind, topo_id);
}

// All UNIQUE edge topo_ids (tessellator/host edge ids) bordering face `face_topo_id` of body
// `body_id`. Powers SolidWorks "select a face to fillet all its edges": the host expands a face
// pick into individual edge selectors. Empty on a bad body/face id. Overload over an
// already-compiled document so a caller with a cached CompileResult skips the recompile.
std::vector<int> face_edges(const CompileResult& r, int body_id, int face_topo_id) {
    std::vector<int> out;
    if (body_id < 1 || body_id > static_cast<int>(r.shapes.size())) return out;
    const TopoDS_Shape& shape = r.shapes[body_id - 1];
    TopTools_IndexedMapOfShape face_map, edge_map;
    TopExp::MapShapes(shape, TopAbs_FACE, face_map);
    TopExp::MapShapes(shape, TopAbs_EDGE, edge_map);   // same unique order as the tessellator
    if (face_topo_id < 1 || face_topo_id > face_map.Extent()) return out;
    TopoDS_Face face = TopoDS::Face(face_map.FindKey(face_topo_id));
    TopTools_IndexedMapOfShape fe;
    TopExp::MapShapes(face, TopAbs_EDGE, fe);
    for (int i = 1; i <= fe.Extent(); ++i) {
        int idx = edge_map.FindIndex(fe.FindKey(i));   // -> the host's edge topo_id
        if (idx >= 1) out.push_back(idx);
    }
    return out;
}

std::vector<int> face_edges(const json& doc, const json& env, int body_id, int face_topo_id) {
    return face_edges(compile_document(doc, env), body_id, face_topo_id);
}

// ============================================================ persisted build state
// The `.step+` `cache.ckpt` codec (contracts/stepplus.md §4). Layout: the 8-byte magic
// "AICCKPT1" + one CBOR document. Every shape referenced by any persisted checkpoint or
// the full result is written into ONE BinTools shape set (no
// triangulation — restored triangulation sends BRepMesh into runaway refinement, see
// brep_cache_encode), so TShape/geometry/location sharing between checkpoints, results
// and tool bodies is reproduced exactly: TopExp ordinals, the owner maps keyed by them,
// and the geometry-identity owner fallback of the NEXT executed feature all behave as
// they would against the live checkpoint. Shared slots (feature results, statuses, owner
// maps, tool records, JSON side channels, ids) are pooled and referenced by index.
namespace {

constexpr char kCkptMagic[] = "AICCKPT1";
constexpr std::size_t kCkptMagicLen = 8;
constexpr int kCkptFormat = 1;

uint64_t ckpt_bytes_hash(const unsigned char* p, std::size_t n) {
    uint64_t h = 1469598103934665603ULL;
    for (std::size_t i = 0; i < n; ++i) {
        h ^= p[i];
        h *= 1099511628211ULL;
    }
    return h;
}

json ckpt_plane(const chudware::geom::SketchPlane& p) {
    return json::array({p.origin.x(), p.origin.y(), p.origin.z(),
                        p.uAxis.x(), p.uAxis.y(), p.uAxis.z(),
                        p.vAxis.x(), p.vAxis.y(), p.vAxis.z(),
                        p.normal.x(), p.normal.y(), p.normal.z()});
}

Eigen::Vector3d ckpt_vec(const json& j, std::size_t k) {
    return Eigen::Vector3d(j.at(k).get<double>(), j.at(k + 1).get<double>(),
                           j.at(k + 2).get<double>());
}

chudware::geom::SketchPlane ckpt_plane_from(const json& j) {
    chudware::geom::SketchPlane p;
    p.origin = ckpt_vec(j, 0);
    p.uAxis = ckpt_vec(j, 3);
    p.vAxis = ckpt_vec(j, 6);
    p.normal = ckpt_vec(j, 9);
    return p;
}

json ckpt_vec_json(const Eigen::Vector3d& v) { return json::array({v.x(), v.y(), v.z()}); }

json ckpt_status(const FeatureStatus& st) {
    return json::array({st.id, st.type, st.ok, st.error, st.warning, st.ms, st.data});
}

FeatureStatus ckpt_status_from(const json& j) {
    FeatureStatus st;
    st.id = j.at(0).get<std::string>();
    st.type = j.at(1).get<std::string>();
    st.ok = j.at(2).get<bool>();
    st.error = j.at(3).get<std::string>();
    st.warning = j.at(4).get<std::string>();
    st.ms = j.at(5).get<double>();
    st.data = j.at(6);
    return st;
}

// Encoder state for one cache.ckpt payload.
class CkptWriter {
public:
    int shape(const TopoDS_Shape& s) {
        if (s.IsNull()) return -1;
        auto& bucket = shape_index_[{static_cast<const void*>(s.TShape().get()),
                                     static_cast<int>(s.Orientation())}];
        for (const auto& [loc, idx] : bucket)
            if (loc.IsEqual(s.Location())) return idx;
        const int idx = static_cast<int>(shapes_.size());
        shapes_.push_back(s);
        bucket.emplace_back(s.Location(), idx);
        return idx;
    }
    int str(const std::string& s) {
        auto [it, fresh] = string_index_.emplace(s, static_cast<int>(strings_.size()));
        if (fresh) strings_.push_back(s);
        return it->second;
    }
    int blob(const json& j) {
        auto [it, fresh] = blob_index_.emplace(j.dump(), static_cast<int>(blobs_.size()));
        if (fresh) blobs_.push_back(j);
        return it->second;
    }
    int status(const std::shared_ptr<const FeatureStatus>& st) {
        auto [it, fresh] = status_index_.emplace(st.get(), static_cast<int>(statuses_.size()));
        if (fresh) statuses_.push_back(ckpt_status(*st));
        return it->second;
    }
    int tool_rec(const ToolRec& tr) {
        const auto key = std::make_tuple(str(tr.feature_id), str(tr.role), shape(tr.tool));
        auto [it, fresh] = tool_index_.emplace(key, static_cast<int>(tool_recs_.size()));
        if (fresh)
            tool_recs_.push_back(json::array({std::get<0>(key), std::get<1>(key), std::get<2>(key)}));
        return it->second;
    }
    int owners(const std::map<int, std::string>& m) {
        uint64_t h = 1469598103934665603ULL;
        for (const auto& [k, v] : m) h = cc_fnv1a(v, cc_fnv1a(std::to_string(k), h));
        auto& bucket = owner_index_[h];
        for (int idx : bucket)
            if (*owner_reps_[static_cast<std::size_t>(idx)] == m) return idx;
        const int idx = static_cast<int>(owner_reps_.size());
        owner_reps_.push_back(&m);
        bucket.push_back(idx);
        json enc = json::array();
        for (const auto& [k, v] : m) enc.push_back(json::array({k, str(v)}));
        owner_pool_.push_back(std::move(enc));
        return idx;
    }
    int result(const FeatureResultPtr& r) {
        auto [it, fresh] = result_index_.emplace(r.get(), static_cast<int>(results_.size()));
        if (!fresh) return it->second;
        results_.push_back(nullptr);  // reserve the slot before nested interning
        json enc;
        std::visit([&](const auto& v) {
            using T = std::decay_t<decltype(v)>;
            if constexpr (std::is_same_v<T, SketchResult>) {
                json faces = json::array();
                for (const TopoDS_Face& f : v.faces) faces.push_back(shape(f));
                enc = {{"k", 0}, {"faces", std::move(faces)}, {"plane", ckpt_plane(v.plane)},
                       {"entities", v.entities}};
            } else if constexpr (std::is_same_v<T, SolidResult>) {
                enc = {{"k", 1}, {"shape", shape(v.shape)}};
            } else if constexpr (std::is_same_v<T, PlaneResult>) {
                enc = {{"k", 2}, {"plane", ckpt_plane(v.plane)}, {"csys", v.is_csys}};
            } else if constexpr (std::is_same_v<T, AxisResult>) {
                enc = {{"k", 3}, {"p", ckpt_vec_json(v.point)}, {"d", ckpt_vec_json(v.dir)}};
            } else if constexpr (std::is_same_v<T, PointResult>) {
                enc = {{"k", 4}, {"p", ckpt_vec_json(v.pt)}};
            } else if constexpr (std::is_same_v<T, CurveResult>) {
                enc = {{"k", 5}, {"wire", shape(v.wire)}, {"plane", ckpt_plane(v.base)}};
            } else {
                static_assert(std::is_same_v<T, Sketch3DResult>);
                json wires = json::array();
                for (const TopoDS_Wire& w : v.wires) wires.push_back(shape(w));
                enc = {{"k", 6}, {"wires", std::move(wires)}, {"entities", v.entities}};
            }
        }, *r);
        results_[static_cast<std::size_t>(it->second)] = std::move(enc);
        return it->second;
    }

    json snap(std::size_t index, const CompileCkpt& ck) {
        auto str_pairs = [&](const std::map<std::string, std::string>& m) {
            json a = json::array();
            for (const auto& [k, v] : m) a.push_back(json::array({str(k), str(v)}));
            return a;
        };
        auto str_list = [&](const auto& seq) {
            json a = json::array();
            for (const std::string& s : seq) a.push_back(str(s));
            return a;
        };
        auto owner_list = [&](const OwnerTopoMap& m) {
            json a = json::array();
            for (const auto& [sid, om] : m) a.push_back(json::array({str(sid), owners(om)}));
            return a;
        };
        json res = json::array();
        for (const auto& [id, ptr] : ck.results)
            res.push_back(json::array({str(id), ptr ? result(ptr) : -1}));
        json tool_bodies = json::array();
        for (const auto& [id, s] : ck.tool_body_of)
            tool_bodies.push_back(json::array({str(id), shape(s)}));
        json bends = json::array();
        for (const ::geom::BendRecord& b : ck.bend_records)
            bends.push_back(json::array({b.angle, b.radius, b.thickness, b.bend_allowance,
                                         b.wall_length, b.position, ckpt_vec_json(b.p_start),
                                         ckpt_vec_json(b.p_end), ckpt_vec_json(b.e_dir),
                                         ckpt_vec_json(b.length_dir)}));
        json imports = json::array();
        for (const auto& [h, shapes] : ck.import_cache) {
            json idx = json::array();
            for (const TopoDS_Shape& s : shapes) idx.push_back(shape(s));
            imports.push_back(json::array({h, std::move(idx)}));
        }
        json sts = json::array();
        for (const auto& st : ck.statuses) sts.push_back(status(st));
        json trs = json::array();
        for (const ToolRec& tr : ck.tool_recs) trs.push_back(tool_rec(tr));
        return {{"i", index},
                {"r", std::move(res)},
                {"c", str_list(ck.consumed)},
                {"sb", str_pairs(ck.superseded_by)},
                {"so", str_list(ck.solid_order)},
                {"bk", str_pairs(ck.body_kinds)},
                {"src", str_pairs(ck.source_of)},
                {"tb", std::move(tool_bodies)},
                {"op", str_pairs(ck.feature_op_of)},
                {"sn", str_list(ck.seen_names)},
                {"sh", json::array({ck.sheet_active, ck.sheet_thickness, ck.sheet_k_factor,
                                    ck.sheet_default_bend_radius})},
                {"br", std::move(bends)},
                {"ic", std::move(imports)},
                {"st", std::move(sts)},
                {"tr", std::move(trs)},
                {"wm", blob(ck.weld_members)},
                {"sm", blob(ck.sheet_metal_flat)},
                {"fo", owner_list(ck.face_owners)},
                {"eo", owner_list(ck.edge_owners)}};
    }

    json full(const CompileResult& r, uint64_t full_key) {
        json sts = json::array();
        for (const FeatureStatus& st : r.statuses) sts.push_back(ckpt_status(st));
        json infos = json::array();
        for (const BodyInfo& b : r.bodies_info)
            infos.push_back(json::array({b.name, b.kind, b.volume, b.area, b.n_faces, b.n_edges}));
        json shapes = json::array();
        for (const TopoDS_Shape& s : r.shapes) shapes.push_back(shape(s));
        json trs = json::array();
        for (const ToolRec& tr : r.tool_recs) trs.push_back(tool_rec(tr));
        return {{"full_key", full_key},
                {"statuses", std::move(sts)},
                {"body_count", r.body_count},
                {"volume", r.volume},
                {"bodies_info", std::move(infos)},
                {"owner_map", r.owner_map},
                {"shapes", std::move(shapes)},
                {"tool_recs", std::move(trs)},
                {"wm", blob(r.weld_members)},
                {"sm", blob(r.sheet_metal_flat)},
                {"datums", r.datums},
                {"ok", r.ok},
                {"active_configuration", r.active_configuration},
                {"timing", json::array({r.loop_ms, r.tess_ms, r.owner_ms, r.tess_mesh_ms,
                                        r.tess_extract_ms, r.tess_write_ms})},
                {"aicm_size", static_cast<uint64_t>(r.aicm.size())},
                {"aicm_hash", ckpt_bytes_hash(r.aicm.data(), r.aicm.size())}};
    }

    std::string finish(json root) {
        // One set, then one reference per table entry (BinTools::Write's own layout,
        // repeated): never wraps the live shapes in a compound, which would freeze them.
        BinTools_ShapeSet set;
        set.SetWithTriangles(Standard_False);
        set.SetWithNormals(Standard_False);
        set.SetFormatNb(BinTools_FormatVersion_VERSION_3);
        for (const TopoDS_Shape& s : shapes_) set.Add(s);
        std::ostringstream os(std::ios::binary);
        set.Write(os);
        for (const TopoDS_Shape& s : shapes_) set.Write(s, os);
        const std::string brep = os.str();
        root["nshapes"] = shapes_.size();
        root["brep"] = json::binary(std::vector<std::uint8_t>(brep.begin(), brep.end()));
        root["strings"] = strings_;
        root["blobs"] = std::move(blobs_);
        root["statuses"] = std::move(statuses_);
        root["tool_recs"] = std::move(tool_recs_);
        root["owners"] = std::move(owner_pool_);
        root["results"] = std::move(results_);
        std::string out(kCkptMagic, kCkptMagicLen);
        json::to_cbor(root, out);
        return out;
    }

private:
    std::map<std::pair<const void*, int>, std::vector<std::pair<TopLoc_Location, int>>> shape_index_;
    std::vector<TopoDS_Shape> shapes_;
    std::unordered_map<std::string, int> string_index_;
    std::vector<std::string> strings_;
    std::unordered_map<std::string, int> blob_index_;
    json blobs_ = json::array();
    std::unordered_map<const FeatureStatus*, int> status_index_;
    json statuses_ = json::array();
    std::map<std::tuple<int, int, int>, int> tool_index_;
    json tool_recs_ = json::array();
    std::unordered_map<uint64_t, std::vector<int>> owner_index_;
    std::vector<const std::map<int, std::string>*> owner_reps_;
    json owner_pool_ = json::array();
    std::unordered_map<const FeatureResult*, int> result_index_;
    json results_ = json::array();
};

// Decoder pools for one cache.ckpt payload. Any malformed field throws; the public
// entry point turns every throw into a `decode_error` cache miss.
class CkptReader {
public:
    explicit CkptReader(const json& root) : root_(root) {
        const auto& bin = root.at("brep").get_binary();
        std::istringstream is(std::string(bin.begin(), bin.end()), std::ios::binary);
        BinTools_ShapeSet set;
        set.SetWithTriangles(Standard_True);  // as BinTools::Read: honors the stream's flag
        set.Read(is);
        const std::size_t count = root.at("nshapes").get<std::size_t>();
        shapes_.reserve(count);
        for (std::size_t i = 0; i < count; ++i) {
            TopoDS_Shape s;
            set.ReadSubs(s, is, set.NbShapes());
            if (s.IsNull() || !is) throw std::runtime_error("cache.ckpt: truncated shape set");
            shapes_.push_back(std::move(s));
        }
        if (shapes_.size() != root.at("nshapes").get<std::size_t>())
            throw std::runtime_error("cache.ckpt: shape count mismatch");
        strings_ = root.at("strings").get<std::vector<std::string>>();
        for (const json& j : root.at("blobs")) blobs_.push_back(j);
        for (const json& j : root.at("statuses"))
            statuses_.push_back(std::make_shared<const FeatureStatus>(ckpt_status_from(j)));
        for (const json& j : root.at("tool_recs"))
            tool_recs_.push_back(ToolRec{str(j.at(0)), str(j.at(1)), shape(j.at(2))});
        for (const json& j : root.at("owners")) {
            std::map<int, std::string> m;
            for (const json& kv : j) m.emplace(kv.at(0).get<int>(), str(kv.at(1)));
            owners_.push_back(std::move(m));
        }
        results_.resize(root.at("results").size());
    }

    TopoDS_Shape shape(const json& j) const {
        const long long i = j.get<long long>();
        if (i < 0) return TopoDS_Shape();
        return shapes_.at(static_cast<std::size_t>(i));
    }
    const std::string& str(const json& j) const { return strings_.at(j.get<std::size_t>()); }
    const json& blob(const json& j) const { return blobs_.at(j.get<std::size_t>()); }
    const ToolRec& tool_rec(const json& j) const { return tool_recs_.at(j.get<std::size_t>()); }

    FeatureResultPtr result(const json& j) {
        const long long i = j.get<long long>();
        if (i < 0) return nullptr;
        FeatureResultPtr& slot = results_.at(static_cast<std::size_t>(i));
        if (slot) return slot;
        const json& e = root_.at("results").at(static_cast<std::size_t>(i));
        switch (e.at("k").get<int>()) {
            case 0: {
                SketchResult v;
                for (const json& f : e.at("faces")) v.faces.push_back(TopoDS::Face(shape(f)));
                v.plane = ckpt_plane_from(e.at("plane"));
                v.entities = e.at("entities");
                slot = std::make_shared<FeatureResult>(std::move(v));
                break;
            }
            case 1: slot = std::make_shared<FeatureResult>(SolidResult{shape(e.at("shape"))}); break;
            case 2:
                slot = std::make_shared<FeatureResult>(
                    PlaneResult{ckpt_plane_from(e.at("plane")), e.at("csys").get<bool>()});
                break;
            case 3:
                slot = std::make_shared<FeatureResult>(
                    AxisResult{ckpt_vec(e.at("p"), 0), ckpt_vec(e.at("d"), 0)});
                break;
            case 4: slot = std::make_shared<FeatureResult>(PointResult{ckpt_vec(e.at("p"), 0)}); break;
            case 5: {
                const TopoDS_Shape w = shape(e.at("wire"));
                slot = std::make_shared<FeatureResult>(
                    CurveResult{w.IsNull() ? TopoDS_Wire() : TopoDS::Wire(w),
                                ckpt_plane_from(e.at("plane"))});
                break;
            }
            case 6: {
                Sketch3DResult v;
                for (const json& w : e.at("wires")) v.wires.push_back(TopoDS::Wire(shape(w)));
                v.entities = e.at("entities");
                slot = std::make_shared<FeatureResult>(std::move(v));
                break;
            }
            default: throw std::runtime_error("cache.ckpt: unknown result kind");
        }
        return slot;
    }

    std::shared_ptr<const CompileCkpt> snap(const json& e) {
        auto ck = std::make_shared<CompileCkpt>();
        auto pairs_into = [&](const json& a, std::map<std::string, std::string>& m) {
            for (const json& kv : a) m.emplace(str(kv.at(0)), str(kv.at(1)));
        };
        auto owners_into = [&](const json& a, OwnerTopoMap& m) {
            for (const json& kv : a) m.emplace(str(kv.at(0)), owners_.at(kv.at(1).get<std::size_t>()));
        };
        for (const json& kv : e.at("r")) ck->results.put_shared(str(kv.at(0)), result(kv.at(1)));
        for (const json& s : e.at("c")) ck->consumed.insert(str(s));
        pairs_into(e.at("sb"), ck->superseded_by);
        for (const json& s : e.at("so")) ck->solid_order.push_back(str(s));
        pairs_into(e.at("bk"), ck->body_kinds);
        pairs_into(e.at("src"), ck->source_of);
        for (const json& kv : e.at("tb")) ck->tool_body_of.emplace(str(kv.at(0)), shape(kv.at(1)));
        pairs_into(e.at("op"), ck->feature_op_of);
        for (const json& s : e.at("sn")) ck->seen_names.insert(str(s));
        const json& sh = e.at("sh");
        ck->sheet_active = sh.at(0).get<bool>();
        ck->sheet_thickness = sh.at(1).get<double>();
        ck->sheet_k_factor = sh.at(2).get<double>();
        ck->sheet_default_bend_radius = sh.at(3).get<double>();
        for (const json& b : e.at("br")) {
            ::geom::BendRecord r;
            r.angle = b.at(0).get<double>();
            r.radius = b.at(1).get<double>();
            r.thickness = b.at(2).get<double>();
            r.bend_allowance = b.at(3).get<double>();
            r.wall_length = b.at(4).get<double>();
            r.position = b.at(5).get<std::string>();
            r.p_start = ckpt_vec(b.at(6), 0);
            r.p_end = ckpt_vec(b.at(7), 0);
            r.e_dir = ckpt_vec(b.at(8), 0);
            r.length_dir = ckpt_vec(b.at(9), 0);
            ck->bend_records.push_back(std::move(r));
        }
        for (const json& kv : e.at("ic")) {
            std::vector<TopoDS_Shape> shapes;
            for (const json& s : kv.at(1)) shapes.push_back(shape(s));
            ck->import_cache.emplace(kv.at(0).get<uint64_t>(), std::move(shapes));
        }
        for (const json& s : e.at("st")) ck->statuses.push_back(statuses_.at(s.get<std::size_t>()));
        for (const json& t : e.at("tr")) ck->tool_recs.push_back(tool_rec(t));
        ck->weld_members = blob(e.at("wm"));
        ck->sheet_metal_flat = blob(e.at("sm"));
        owners_into(e.at("fo"), ck->face_owners);
        owners_into(e.at("eo"), ck->edge_owners);
        return ck;
    }

    std::shared_ptr<const CompileResult> full(const json& e, std::vector<uint8_t> aicm) const {
        auto r = std::make_shared<CompileResult>();
        for (const json& s : e.at("statuses")) r->statuses.push_back(ckpt_status_from(s));
        r->aicm = std::move(aicm);
        r->body_count = e.at("body_count").get<int>();
        r->volume = e.at("volume").get<double>();
        for (const json& b : e.at("bodies_info"))
            r->bodies_info.push_back(BodyInfo{b.at(0).get<std::string>(), b.at(1).get<std::string>(),
                                              b.at(2).get<double>(), b.at(3).get<double>(),
                                              b.at(4).get<int>(), b.at(5).get<int>()});
        r->owner_map = e.at("owner_map");
        for (const json& s : e.at("shapes")) r->shapes.push_back(shape(s));
        for (const json& t : e.at("tool_recs")) r->tool_recs.push_back(tool_rec(t));
        r->weld_members = blob(e.at("wm"));
        r->sheet_metal_flat = blob(e.at("sm"));
        r->datums = e.at("datums");
        r->ok = e.at("ok").get<bool>();
        r->active_configuration = e.at("active_configuration").get<std::string>();
        const json& t = e.at("timing");
        r->loop_ms = t.at(0).get<double>();
        r->tess_ms = t.at(1).get<double>();
        r->owner_ms = t.at(2).get<double>();
        r->tess_mesh_ms = t.at(3).get<double>();
        r->tess_extract_ms = t.at(4).get<double>();
        r->tess_write_ms = t.at(5).get<double>();
        r->features_reused = 0;
        r->features_executed = static_cast<int>(r->statuses.size());
        return r;
    }

private:
    const json& root_;
    std::vector<TopoDS_Shape> shapes_;
    std::vector<std::string> strings_;
    std::vector<json> blobs_;
    std::vector<std::shared_ptr<const FeatureStatus>> statuses_;
    std::vector<ToolRec> tool_recs_;
    std::vector<std::map<int, std::string>> owners_;
    std::vector<FeatureResultPtr> results_;
};

// The compile inputs of the session-display lane for `doc` (what compile_session hashes).
struct CkptInputs {
    json features;
    std::string active_configuration;
};

CkptInputs ckpt_inputs(const json& doc) {
    static const json empty = json::array();
    const json& base = doc.contains("features") && doc.at("features").is_array()
                           ? doc.at("features") : empty;
    auto [cfg_name, cfg] = resolve_active_config(doc);
    return {config_effective_features(base, cfg), cfg_name};
}

}  // namespace

std::shared_ptr<const CompileResult> compile_cache_full_result(const CompileCache& cache) {
    return cache.last_result;
}

uint64_t compile_cache_stamp(const CompileCache& cache) { return cache.stamp; }

std::string compile_cache_encode(const CompileCache& cache, const json& doc, const json& env,
                                 ::geom::DisplayQuality quality, const std::string& engine_key,
                                 const std::string& doc_hash, std::size_t budget_bytes,
                                 json& report) {
    report = {{"written", false}, {"bytes", 0}, {"checkpoints_total", 0},
              {"checkpoints_written", 0}, {"checkpoints_dropped", 0}, {"full_result", false}};
    const CkptInputs in = ckpt_inputs(doc);
    const std::size_t n = in.features.size();
    if (n == 0) {
        report["reason"] = "no_features";
        return {};
    }
    if (n > kCompileCacheMaxFeatures) {
        report["reason"] = "too_many_features";
        return {};
    }
    const std::vector<uint64_t> chain =
        cc_chain(in.features, env, in.active_configuration, quality, true, false);
    // Deepest index whose chain still matches the cached compile: only those checkpoints
    // describe THIS document (the cache holds the last compile, which doc_save just ran).
    std::size_t m = 0;
    bool any = false;
    for (std::size_t i = 0; i < std::min(chain.size(), cache.chain.size()); ++i) {
        if (chain[i] != cache.chain[i]) break;
        m = i;
        any = true;
    }
    if (!any) {
        report["reason"] = "not_built_this_session";
        return {};
    }
    // Checkpoints only after an unbroken run of successful features: a feature that
    // failed is re-attempted on reopen instead of replaying its recorded failure.
    std::size_t ok_prefix = m;
    for (std::size_t i = m; i >= 1; --i) {
        if (i < cache.snaps.size() && cache.snaps[i]) {
            const auto& sts = cache.snaps[i]->statuses;
            for (std::size_t k = 0; k < sts.size() && k < ok_prefix; ++k)
                if (!sts[k]->ok) { ok_prefix = k; break; }
            break;
        }
    }
    std::vector<std::size_t> keep;
    for (std::size_t i = 1; i <= ok_prefix && i < cache.snaps.size(); ++i)
        if (cache.snaps[i]) keep.push_back(i);
    const std::size_t total = keep.size();
    bool full = m == n && cache.last_result && cache.last_result->ok &&
                cache.full_key == cc_full_key(doc, chain.back());
    std::string reason;
    if (ok_prefix < m) {
        reason = "feature_failed";
        report["omitted_after_failure"] = m - ok_prefix;
    } else if (m < n) {
        reason = "partial_build";
    }
    if (keep.empty() && !full) {
        report["reason"] = reason.empty() ? "no_checkpoints" : reason;
        return {};
    }
    report["checkpoints_total"] = total;
    for (;;) {
        CkptWriter w;
        json root = {{"format", kCkptFormat},
                     {"engine", engine_key},
                     {"doc_hash", doc_hash},
                     {"quality", ::geom::display_quality_name(quality)},
                     {"chain", std::vector<uint64_t>(cache.chain.begin(),
                                                     cache.chain.begin() + static_cast<std::ptrdiff_t>(m + 1))}};
        json snaps = json::array();
        for (std::size_t i : keep) snaps.push_back(w.snap(i, *cache.snaps[i]));
        root["snaps"] = std::move(snaps);
        root["full"] = full ? w.full(*cache.last_result, cache.full_key) : json(nullptr);
        std::string out = w.finish(std::move(root));
        if (out.size() <= budget_bytes) {
            report["written"] = true;
            report["bytes"] = out.size();
            report["checkpoints_written"] = keep.size();
            report["checkpoints_dropped"] = total - keep.size();
            report["full_result"] = full;
            if (!reason.empty()) report["reason"] = reason;
            return out;
        }
        // Over the container budget: drop the OLDEST half of the kept checkpoints (the
        // deepest ones serve the common tail edit), then the full result.
        reason = "budget";
        if (!keep.empty()) {
            keep.erase(keep.begin(), keep.begin() + static_cast<std::ptrdiff_t>((keep.size() + 1) / 2));
            continue;
        }
        if (full) {
            full = false;
            continue;
        }
        report["checkpoints_dropped"] = total;
        report["reason"] = reason;
        return {};
    }
}

std::shared_ptr<CompileCache> compile_cache_decode(const std::string& blob, const json& doc,
                                                   const json& env, ::geom::DisplayQuality quality,
                                                   const std::string& engine_key,
                                                   const std::string* aicm, json& report) {
    report = {{"restored", false}, {"checkpoints", 0}, {"matched_features", 0},
              {"full_result", false}};
    try {
        if (blob.size() < kCkptMagicLen || blob.compare(0, kCkptMagicLen, kCkptMagic) != 0) {
            report["reason"] = "decode_error";
            return nullptr;
        }
        const json root = json::from_cbor(blob.begin() + kCkptMagicLen, blob.end());
        if (root.at("format").get<int>() != kCkptFormat) {
            report["reason"] = "decode_error";
            return nullptr;
        }
        if (root.at("engine").get<std::string>() != engine_key) {
            report["reason"] = "engine_key";
            return nullptr;
        }
        ::geom::DisplayQuality saved_quality = quality;
        if (!::geom::display_quality_parse(root.at("quality").get<std::string>(), saved_quality)) {
            report["reason"] = "decode_error";
            return nullptr;
        }
        const CkptInputs in = ckpt_inputs(doc);
        const std::size_t n = in.features.size();
        if (n == 0 || n > kCompileCacheMaxFeatures) {
            report["reason"] = "chain_mismatch";
            return nullptr;
        }
        const auto stored = root.at("chain").get<std::vector<uint64_t>>();
        const std::vector<uint64_t> saved_chain =
            cc_chain(in.features, env, in.active_configuration, saved_quality, true, false);
        std::size_t m = 0;
        bool any = false;
        for (std::size_t i = 0; i < std::min(stored.size(), saved_chain.size()); ++i) {
            if (stored[i] != saved_chain[i]) break;
            m = i;
            any = true;
        }
        if (!any) {
            report["reason"] = "chain_mismatch";
            return nullptr;
        }
        // Re-key the matching prefix to the session's display tier: checkpoints carry exact
        // B-rep only, so a different tier simply re-meshes the surviving bodies.
        std::vector<uint64_t> chain = saved_quality == quality
            ? saved_chain
            : cc_chain(in.features, env, in.active_configuration, quality, true, false);
        chain.resize(m + 1);

        CkptReader rd(root);
        auto cache = std::make_shared<CompileCache>();
        cache->snaps.resize(m + 1);
        std::size_t restored = 0;
        for (const json& e : root.at("snaps")) {
            const std::size_t i = e.at("i").get<std::size_t>();
            if (i == 0 || i > m) continue;
            cache->snaps[i] = rd.snap(e);
            ++restored;
        }
        cache->chain = std::move(chain);
        report["matched_features"] = m;
        report["features"] = n;
        report["checkpoints"] = restored;

        const json& full = root.at("full");
        std::string why_not_full;
        if (full.is_null()) why_not_full = "not_saved";
        else if (m != n) why_not_full = "chain_mismatch";
        else if (saved_quality != quality) why_not_full = "display_quality";
        else if (full.at("full_key").get<uint64_t>() != cc_full_key(doc, cache->chain.back()))
            why_not_full = "appearance_changed";
        else if (!aicm || aicm->size() != full.at("aicm_size").get<uint64_t>() ||
                 ckpt_bytes_hash(reinterpret_cast<const unsigned char*>(aicm->data()),
                                 aicm->size()) != full.at("aicm_hash").get<uint64_t>())
            why_not_full = "mesh_mismatch";
        if (why_not_full.empty()) {
            cache->last_result = rd.full(full, std::vector<uint8_t>(aicm->begin(), aicm->end()));
            cache->full_key = full.at("full_key").get<uint64_t>();
            report["full_result"] = true;
        } else {
            report["full_result_reason"] = why_not_full;
        }
        if (restored == 0 && !cache->last_result) {
            report["reason"] = m < n ? "chain_mismatch" : why_not_full;
            return nullptr;
        }
        if (m < n) report["reason"] = "chain_mismatch";  // partial: only the matching prefix
        cache->stamp = cc_next_stamp();
        report["restored"] = true;
        return cache;
    } catch (...) {
        report = {{"restored", false}, {"checkpoints", 0}, {"matched_features", 0},
                  {"full_result", false}, {"reason", "decode_error"}};
        return nullptr;
    }
}

void tessellate_solved_bodies(const json& doc, CompileResult& r, ::geom::DisplayQuality quality,
                              bool tessellate) {
    if (r.bodies_info.size() != r.shapes.size())
        throw std::runtime_error("tessellate_solved_bodies: one body record per shape required");
    const auto t0 = std::chrono::steady_clock::now();
    if (tessellate) ::geom::tess_timing_reset();
    std::vector<aicm::AicmBody> bodies;
    const float color[3] = {0.71f, 0.76f, 0.82f};  // default dielectric grey (as compile)
    const bool has_appearances = doc.contains("appearances") && doc["appearances"].is_object();
    r.volume = 0.0;
    for (std::size_t i = 0; i < r.shapes.size(); ++i) {
        const TopoDS_Shape& shape = r.shapes[i];
        BodyInfo& bi = r.bodies_info[i];
        const bool is_surface = bi.kind == "surface";
        const uint32_t body_num = static_cast<uint32_t>(i + 1);
        if (tessellate) {
            float bcolor[3] = {color[0], color[1], color[2]};
            float bmetal = 0.0f, brough = 0.5f;
            if (has_appearances) apply_doc_appearance(doc, body_num, bcolor, bmetal, brough);
            const ::geom::DisplayDeflections defl = ::geom::display_deflections(quality, shape);
            aicm::AicmBody b = ::geom::tessellate_body(shape, body_num, bi.name, bcolor,
                                                       defl.lin, defl.ang, bmetal, brough);
            apply_doc_face_appearances(doc, body_num, shape, b);
            bodies.push_back(std::move(b));
        }
        double vol = 0.0;
        if (!is_surface) {
            GProp_GProps gp;
            BRepGProp::VolumeProperties(shape, gp, /*OnlyClosed=*/Standard_False,
                                        /*SkipShared=*/Standard_False,
                                        /*UseTriangulation=*/tessellate ? Standard_True
                                                                        : Standard_False);
            vol = gp.Mass();
        }
        bi.volume = vol;
        bi.area = ::geom::shape_area(shape);
        bi.n_faces = ::geom::shape_face_count(shape);
        bi.n_edges = ::geom::shape_edge_count(shape);
        r.volume += vol;
    }
    r.body_count = static_cast<int>(r.shapes.size());
    if (!tessellate) return;
    const auto tw = std::chrono::steady_clock::now();
    r.aicm = aicm::write_aicm(bodies);
    const auto t1 = std::chrono::steady_clock::now();
    r.tess_write_ms = std::chrono::duration<double, std::milli>(t1 - tw).count();
    r.tess_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
    const ::geom::TessTiming tt = ::geom::tess_timing_read();
    r.tess_mesh_ms = tt.mesh_ms;
    r.tess_extract_ms = tt.extract_ms;
}

// --------------------------------------------------------------- push-pull (Instant3D)
// Port of engine/doc/instant3d.py plan_edit + helpers. NEVER mutates the doc.
namespace {

constexpr double kPpMinDim = 0.1;        // smallest length/depth/radius a drag may leave (mm)
constexpr double kPpParallelTol = 0.966;  // |cos| >= this (~15deg) counts a normal as axis-aligned

std::optional<double> pp_dim_value(const json& feat, const std::string& key) {
    if (!feat.contains("params") || !feat["params"].is_object()) return std::nullopt;
    const json& p = feat["params"];
    auto it = p.find(key);
    if (it == p.end()) return std::nullopt;
    const json* val = it->is_object() ? (it->contains("value") ? &(*it)["value"] : nullptr) : &*it;
    if (!val || val->is_boolean() || !val->is_number()) return std::nullopt;
    return val->get<double>();
}

bool pp_has_expr(const json& feat, const std::string& key) {
    if (!feat.contains("params") || !feat["params"].is_object()) return false;
    auto it = feat["params"].find(key);
    if (it == feat["params"].end() || !it->is_object()) return false;
    auto e = it->find("expr");
    return e != it->end() && e->is_string() && !e->get<std::string>().empty();
}

std::string pp_axis_label(const std::optional<Eigen::Vector3d>& axis) {
    if (!axis) return "";
    Eigen::Vector3d a = *axis;
    double n = a.norm();
    if (n > 1e-12) a /= n;
    const std::pair<const char*, Eigen::Vector3d> bases[] = {
        {"x", {1, 0, 0}}, {"y", {0, 1, 0}}, {"z", {0, 0, 1}}};
    for (const auto& [lbl, base] : bases)
        if (std::abs(a.dot(base)) >= kPpParallelTol) return lbl;
    return "";
}

const json* pp_feature_by_id(const json& doc, const std::string& fid) {
    if (!doc.contains("features") || !doc["features"].is_array()) return nullptr;
    for (const json& f : doc["features"])
        if (f.is_object() && f.value("id", std::string()) == fid) return &f;
    return nullptr;
}

// The extrude direction (= owning sketch's plane normal) for a blind extrude, or
// nullopt when it can't be resolved cheaply (a datum-plane sketch) — in which case
// the caller skips the cap/side-wall test, exactly as the Python _extrude_axis does.
std::optional<Eigen::Vector3d> pp_extrude_axis(const json& doc, const json& feat) {
    if (!feat.contains("refs") || !feat["refs"].is_object()) return std::nullopt;
    const json& refs = feat["refs"];
    if (!refs.contains("profile") || !refs["profile"].is_array()) return std::nullopt;
    std::string sketch_fid;
    for (const json& p : refs["profile"])
        if (p.is_object() && p.value("kind", std::string()) == "region" &&
            p.contains("sketch") && p["sketch"].is_string()) {
            sketch_fid = p["sketch"].get<std::string>();
            break;
        }
    if (sketch_fid.empty()) return std::nullopt;
    const json* sfeat = pp_feature_by_id(doc, sketch_fid);
    if (!sfeat) return std::nullopt;
    std::string pref = "top";
    if (sfeat->contains("refs") && (*sfeat)["refs"].is_object() &&
        (*sfeat)["refs"].value("plane", json()).is_string())
        pref = (*sfeat)["refs"]["plane"].get<std::string>();
    else if (sfeat->contains("params") && (*sfeat)["params"].is_object() &&
             (*sfeat)["params"].value("plane", json()).is_string())
        pref = (*sfeat)["params"]["plane"].get<std::string>();
    if (pref.rfind("plane.", 0) == 0) pref = pref.substr(6);
    if (pref != "top" && pref != "front" && pref != "right") return std::nullopt;
    return plane_from_name(pref).normal;
}

// Fill new_value/clamped for a length-style param (instant3d.py _length_plan); sets
// draggable=false+reason when the param is expr-driven.
void pp_length_plan(PushPullPlan& plan, const json& feat, const std::string& key, double old,
                    double distance, const std::string& axis_lbl) {
    if (pp_has_expr(feat, key)) {
        plan.draggable = false;
        plan.reason = "param '" + key + "' is equation-driven (expr) — edit the equation instead";
        return;
    }
    double nv = old + distance;
    bool clamped = false;
    if (nv < kPpMinDim) { nv = kPpMinDim; clamped = true; }
    plan.draggable = true;
    plan.param = key;
    plan.old_value = old;
    plan.new_value = nv;
    plan.clamped = clamped;
    plan.axis = axis_lbl;
    plan.has_axis = !axis_lbl.empty();
}

void pp_set_not_draggable(PushPullPlan& plan, const std::string& reason) {
    plan.draggable = false;
    plan.reason = reason;
}

// plan_edit dispatch on the face surface + feature type -> the single drivable param.
void pp_plan_edit(const json& doc, const json& feat, const TopoInfo& info, double distance,
                  PushPullPlan& plan) {
    const std::string& ftype = plan.ftype;
    const std::string surf = info.surf.value_or("");

    if (surf == "plane") {
        // An offset/midplane datum's planar face moves with its offset param.
        if (ftype == "plane" && pp_dim_value(feat, "offset").has_value()) {
            const std::string key = "offset";
            if (pp_has_expr(feat, key)) {
                pp_set_not_draggable(plan, "param 'offset' is equation-driven (expr)");
                return;
            }
            double old = *pp_dim_value(feat, key);
            plan.draggable = true;
            plan.param = key;
            plan.old_value = old;
            plan.new_value = old + distance;  // offset is unclamped (can go negative)
            plan.clamped = false;
            plan.axis = "normal";
            plan.has_axis = true;
            return;
        }
        // _plan_for_planar
        std::string axis_lbl = pp_axis_label(info.axis);
        if (ftype == "box") {
            if (axis_lbl.empty()) {
                pp_set_not_draggable(plan, "box face normal is not aligned to a principal axis");
                return;
            }
            const std::string key = axis_lbl == "x" ? "length" : axis_lbl == "y" ? "width" : "height";
            double old = pp_dim_value(feat, key).value_or(20.0);
            pp_length_plan(plan, feat, key, old, distance, axis_lbl);
            return;
        }
        if (ftype == "cylinder" || ftype == "cone") {
            double old = pp_dim_value(feat, "height").value_or(20.0);
            pp_length_plan(plan, feat, "height", old, distance, axis_lbl.empty() ? "axial" : axis_lbl);
            return;
        }
        if (ftype == "extrude") {
            const json& p = feat.contains("params") && feat["params"].is_object() ? feat["params"]
                                                                                  : json::object();
            std::string end = p.is_object() ? p.value("end", std::string("blind")) : "blind";
            if (end != "blind") {
                pp_set_not_draggable(
                    plan, "extrude end condition '" + end + "' has no single blind-depth param to drag");
                return;
            }
            std::optional<Eigen::Vector3d> ext_axis = pp_extrude_axis(doc, feat);
            if (ext_axis && info.axis) {
                Eigen::Vector3d a = *info.axis;
                double n = a.norm();
                if (n > 1e-12) a /= n;
                if (std::abs(a.dot(*ext_axis)) < kPpParallelTol) {
                    pp_set_not_draggable(plan,
                        "dragged face is a side wall of the extrude (normal perpendicular to the "
                        "extrude axis), not a depth cap");
                    return;
                }
            }
            double old = pp_dim_value(feat, "depth").value_or(10.0);
            pp_length_plan(plan, feat, "depth", old, distance, axis_lbl.empty() ? "n" : axis_lbl);
            return;
        }
        pp_set_not_draggable(plan, "planar face of feature type '" + ftype + "' is not push-pull drivable");
        return;
    }
    if (surf == "cylinder") {
        if (ftype == "cylinder") {
            const std::string key = "radius";
            if (pp_has_expr(feat, key)) {
                pp_set_not_draggable(plan, "param 'radius' is equation-driven (expr)");
                return;
            }
            double old = pp_dim_value(feat, key).value_or(10.0);
            double nv = old + distance;
            bool clamped = false;
            if (nv < kPpMinDim) { nv = kPpMinDim; clamped = true; }
            plan.draggable = true;
            plan.param = key;
            plan.old_value = old;
            plan.new_value = nv;
            plan.clamped = clamped;
            plan.axis = "radial";
            plan.has_axis = true;
            return;
        }
        if (ftype == "hole") {
            const json& p = feat.contains("params") && feat["params"].is_object() ? feat["params"]
                                                                                  : json::object();
            std::string standard = p.is_object() && p.contains("standard") && p["standard"].is_string()
                                       ? p["standard"].get<std::string>() : "custom";
            if (standard != "custom") {
                pp_set_not_draggable(plan,
                    "hole bore is driven by standard '" + standard + "', not a free diameter");
                return;
            }
            const std::string key = "diameter";
            if (pp_has_expr(feat, key)) {
                pp_set_not_draggable(plan, "param 'diameter' is equation-driven (expr)");
                return;
            }
            double old = pp_dim_value(feat, key).value_or(3.4);
            double nv = old + 2.0 * distance;  // radial change doubles the diameter
            bool clamped = false;
            if (nv < kPpMinDim) { nv = kPpMinDim; clamped = true; }
            plan.draggable = true;
            plan.param = key;
            plan.old_value = old;
            plan.new_value = nv;
            plan.clamped = clamped;
            plan.axis = "radial";
            plan.has_axis = true;
            return;
        }
        pp_set_not_draggable(plan, "cylindrical face of feature type '" + ftype + "' is not push-pull drivable");
        return;
    }
    if (ftype == "revolve") {
        pp_set_not_draggable(plan, "revolve is angle-driven; dragging a revolved face is not a push-pull");
        return;
    }
    pp_set_not_draggable(plan, "face surface '" + surf + "' of feature '" + ftype +
                                   "' maps to no single push-pull param");
}

}  // namespace

PushPullPlan plan_push_pull(const json& doc, const json& env, int body_id, int topo_id,
                            double distance) {
    PushPullPlan plan;
    if (doc.is_object() && doc.value("schema", std::string()) == "aicad.asm/1")
        throw OpError("invalid_op", "doc_push_pull: not supported on assemblies", {{"reason", "assembly"}});

    CompileResult r = compile_document(doc, env);
    const int nbodies = static_cast<int>(r.shapes.size());
    if (body_id < 1 || body_id > nbodies)
        throw OpError("invalid_op",
                      "body_id " + std::to_string(body_id) + " out of range (1.." +
                          std::to_string(nbodies) + ")",
                      {{"reason", "bad body_id"}});
    const TopoDS_Shape& shape = r.shapes[body_id - 1];
    std::vector<TopoInfo> infos = describe_faces(shape);
    const int nfaces = static_cast<int>(infos.size());
    if (topo_id < 1 || topo_id > nfaces)
        throw OpError("invalid_op",
                      "topo_id " + std::to_string(topo_id) + " out of range (1.." +
                          std::to_string(nfaces) + ") for faces",
                      {{"reason", "bad topo_id"}});
    const TopoInfo& info = infos[topo_id - 1];

    // Owner = the feature owning (body_id, topo_id) per the owner map (same scan as
    // resolve_pick; FACE describe order == MapShapes order, so topo_id aligns).
    std::optional<std::string> owner;
    if (r.owner_map.is_object()) {
        for (auto it = r.owner_map.begin(); it != r.owner_map.end() && !owner; ++it) {
            const json& rec = it.value();
            if (!rec.contains("faces") || !rec["faces"].is_array()) continue;
            for (const json& pr : rec["faces"])
                if (pr.is_array() && pr.size() == 2 && pr[0].get<int>() == body_id &&
                    pr[1].get<int>() == topo_id) {
                    owner = it.key();
                    break;
                }
        }
    }
    if (!owner) {
        pp_set_not_draggable(plan, "picked face has no owning feature in the owner map");
        return plan;
    }
    plan.owner_fid = *owner;
    const json* feat = pp_feature_by_id(doc, *owner);
    if (!feat) {
        pp_set_not_draggable(plan, "owner feature '" + *owner + "' not found in the document");
        return plan;
    }
    plan.ftype = feat->value("type", std::string());
    pp_plan_edit(doc, *feat, info, distance, plan);
    return plan;
}

}  // namespace chudware::doc
