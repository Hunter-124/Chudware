// sketch_model_snap.h — model-geometry inference for the 2D sketcher (no GL / ImGui / AppState).
//
// While a sketch is open the solid's B-rep vertices, straight edges and circular edges are snap
// targets for every point-placing tool, like SolidWorks' inference of model geometry:
//   Vertex    a B-rep vertex, projected onto the sketch plane           -> coincident
//   Midpoint  the midpoint of a straight edge (projected)               -> midpoint
//   Center    the center of a circular edge (hole / boss / arc)         -> coincident to .center
//   OnEdge    the nearest point on a straight / circular edge           -> point_on
// The sketch keeps the relation associative: the snap is committed through the SAME external-
// reference mechanism as Convert Entities (`sketch_convert` + params.external_refs), never as
// baked coordinates. The index here only answers "what is under the cursor"; the ref itself is
// resolved at the click (model pick -> selector) and bound into the ops batch by modelref::bind.
//
// The candidate table is built once per (tessellation, sketch plane) from the parsed DocMesh and
// reused every frame; a frame only measures screen distances. Candidates are not filtered by
// occlusion up front (a part has thousands); the caller's `visible` probe confirms the few
// nearest ones against the id buffer, so hidden back-face geometry never wins. Geometry owned by
// a feature that depends on the sketch being edited (the extrude built FROM this very profile, say)
// is not offered: referencing it would make the sketch depend on its own output.
#pragma once

#include <array>
#include <cstdint>
#include <functional>
#include <set>
#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "../render/aicm_loader.h"

namespace ui {

enum class ModelSnapKind : uint8_t { Vertex, Midpoint, Center, OnEdge };

struct ModelSnapHit {
    ModelSnapKind kind = ModelSnapKind::Vertex;
    uint32_t body = 0;   // AICM body id == render::PickHit::body_id
    uint32_t topo = 0;   // B-rep vertex id (Vertex) or edge id (every other kind)
    float u = 0, v = 0;  // snapped sketch-plane coordinate (mm)
    // World points of the model geometry itself (the vertex; the edge midpoint; rim samples of a
    // circle; the on-edge point). The id buffer shows geometry only where it is visible, so the
    // snap is offered only if one of these probes resolves back to (body, topo) on screen.
    std::array<std::array<float, 3>, 4> probe{};
    int probes = 0;
};

// Affine sketch (u,v) -> screen map: the sketcher's fronto-parallel basis.
struct ModelSnapScreen {
    float ox = 0, oy = 0;   // screen position of sketch (0,0)
    float ux = 1, uy = 0;   // screen delta per +1 u
    float vx = 0, vy = 1;   // screen delta per +1 v
    float x(float u, float v) const { return ox + ux * u + vx * v; }
    float y(float u, float v) const { return oy + uy * u + vy * v; }
};

class SketchModelSnap {
public:
    using Visible = std::function<bool(const ModelSnapHit&)>;

    using EdgeFilter = std::function<bool(uint32_t body, uint32_t edge)>;

    // True while `mesh`, the plane `frame` and `filterKey` are what the table was built from.
    bool upToDate(const std::shared_ptr<const render::DocMesh>& mesh, const float frame[16],
                  uint64_t filterKey) const;
    // (Re)index `mesh` for the sketch plane `frame` (row-major 4x4, world = frame * [u v 0 1]).
    // `excluded(body, edge)` drops an edge (and, once no remaining edge ends there, its vertices);
    // `filterKey` names the filter's inputs so a changed answer rebuilds the table.
    void sync(const std::shared_ptr<const render::DocMesh>& mesh, const float frame[16],
              uint64_t filterKey = 0, const EdgeFilter& excluded = {});
    void clear();
    uint64_t builds() const { return builds_; }
    size_t keyCount() const { return keys_.size(); }
    size_t edgeCount() const { return edges_.size(); }

    // Best visible snap within `reach` screen px of (cx, cy). Vertices beat midpoints/centers, which
    // beat on-edge points; each group is ordered by distance. `inference` off leaves vertices only;
    // `onEdge` off skips the nearest-point-on-edge search (the caller already has a better guide).
    bool query(const ModelSnapScreen& s, float cx, float cy, float reach, bool inference,
               bool onEdge, const Visible& visible, ModelSnapHit& out) const;

private:
    struct Edge {
        uint32_t body = 0, topo = 0;
        size_t first = 0, count = 0;       // point range in uv_ / xyz_
        float umin = 0, vmin = 0, umax = 0, vmax = 0;
    };
    void build(const render::DocMesh& mesh, const float frame[16], const EdgeFilter& excluded);

    std::weak_ptr<const render::DocMesh> mesh_;   // identity only: never keeps a mesh alive
    float frame_[16] = {};
    uint64_t filterKey_ = 0;
    bool valid_ = false;
    uint64_t builds_ = 0;
    std::vector<ModelSnapHit> keys_;
    std::vector<Edge> edges_;
    std::vector<float> uv_;    // 2 per edge point, sketch coordinates
    std::vector<float> xyz_;   // 3 per edge point, world coordinates
};

// ---------------------------------------------------------------------------------------------
// Model-reference tokens. A sketch tool records a snap in its own grammar ("e3.p2", "mid:e4",
// "rim:e5", "e6.center"); a model snap uses the same grammar with the entity named by a token
// "@m<k>" (k indexes the sketcher's interned model references) because the reference entity does
// not exist until the placement commits. modelref::bind turns the tokens in a finished ops batch
// into real entity ids inside that ONE batch (a single undo step, no stranded references).
namespace modelref {

// Snap record for a model hit: "@m<k>.p" (vertex), "mid:@m<k>", "@m<k>.center", "rim:@m<k>".
// k < 0 leaves the index out ("@m.p"...) for the live hover form, before a ref is interned.
std::string snapRecord(ModelSnapKind kind, int k);
// The same record with a hover-form "@m" completed to "@m<k>".
std::string withRef(const std::string& hoverRecord, int k);

// Entity id of the first projected entity of a live external reference on sketch `sketchFid`
// whose source is `ref` (tree = the document tree), or "" when the sketch has none.
std::string convertedEntity(const nlohmann::json& tree, const std::string& sketchFid,
                            const nlohmann::json& ref);

// Replace every "@m<k>" in `ops` (a doc_apply_ops batch of sketch ops) with a sketch entity id.
// A reference already converted on the sketch (`lookup(refs[k])` non-empty) binds to that entity.
// Every other one gets a `sketch_convert` op (construction, fixed, associative) PREPENDED to the
// batch, so the batch's "$N" placeholders shift by the returned count; restoreIdMap undoes that
// shift in the response so callers keep reading "$0" as the first op they authored.
int bind(nlohmann::json& ops, const std::string& sketchFid,
         const std::vector<nlohmann::json>& refs,
         const std::function<std::string(const nlohmann::json&)>& lookup);

// True when any string in `ops` is a model-reference token.
bool uses(const nlohmann::json& ops);

// Drop the first `shift` ops' placeholder entries from a response id_map and renumber the rest.
nlohmann::json restoreIdMap(const nlohmann::json& idMap, int shift);

// Ids of the features that depend on sketch `sketchFid`: the ones that reference it (profile,
// path, section, plane...) and, transitively, the ones that reference those. `tree` = document tree.
std::set<std::string> downstreamOf(const nlohmann::json& tree, const std::string& sketchFid);

}  // namespace modelref

}  // namespace ui
