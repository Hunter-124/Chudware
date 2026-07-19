// PLM Graph — standalone 2D force-directed layout engine.
//
// Pure C++/stdlib math (no ImGui, no engine/RPC). Drives the PLM Graph tab's
// animated layout for ~3200 nodes at 60 fps. Call step(dt) once per frame;
// fixed internal substeps keep motion framerate-independent.
//
// Forces (per substep, scaled by cooling alpha):
//   1. Node-node repulsion  — Coulomb-like  k_rep * q_i * q_j / r^2  along the
//      separation, with finite cutoff. Implemented via uniform spatial-grid
//      hashing rebuilt each substep → expected O(n * k) with k = avg neighbors
//      inside the cutoff cell neighborhood (≪ n for typical layouts). Each
//      unordered pair is evaluated exactly once (j > i).
//   2. Edge springs         — Hooke  strength * (r - restLen)  along the edge.
//      Springs and collision coexist: the UI should floor each edge rest length
//      at collDist = ra + rb + pad so springs do not crush discs against the
//      non-overlap constraint (this engine does not auto-clamp rest lengths).
//   3. Weak centering       — k_center * (centroid - p) pulls the cloud back
//      toward its own centroid so the graph doesn't drift off-screen.
//   4. Group / community pull — optional per-node group id; each free node is
//      pulled toward its group's centroid (O(N) per substep). Used by the UI
//      to cluster densely-connected components without O(N²) all-pairs work.
//   5. Collision            — radius-aware non-overlap (d3-force collide analogue).
//      After integration, 3 Gauss-Seidel passes push overlapping discs apart
//      along their separation (cooling-independent). Uses its own spatial grid
//      sized by max collDist so pairs collide even when outside the Coulomb
//      cutoff. Strength ~10–12 is a near-hard separation per pass.
//
// Integration: semi-implicit Euler (v += a*h; x += v*h) with per-step velocity
// damping. Pinned nodes have infinite mass (a=0, v=0); the UI teleports them
// via setPos. Cooling alpha decays geometrically toward zero each substep
// (d3-force semantics: ~300 substeps from alpha=1 to settled); reheat()
// bumps it after structural edits; settled() is true once alpha hits zero
// (step() early-outs).
//
// Units: positions/radii/rest lengths are abstract graph-space pixels. Time is
// seconds. Defaults target a pleasant d3-force-like feel for CAD project graphs.
// Deterministic: no unseeded RNG; index-hashed micro-jitter only when two nodes
// coincide exactly.
//
// Complexity notes (N nodes, E edges):
//   rebuild grid  O(N)   (once for repulsion, once per collision pass)
//   repulsion     O(N * avg bin occupancy in 3x3 neighborhood)  ≈ O(N) typical
//   springs       O(E)
//   centering     O(N)
//   collision     O(kCollIters * N * avg coll-bin occupancy)  ≈ O(N) typical
//   3200-node budget: one substep well under ~2 ms on a modern desktop CPU.
#pragma once

#include <cstdint>
#include <vector>

namespace ui {

class ForceGraph {
public:
    ForceGraph();
    ~ForceGraph() = default;

    ForceGraph(const ForceGraph&) = delete;
    ForceGraph& operator=(const ForceGraph&) = delete;

    // Drop every node and edge; reset cooling to a cold start.
    void clear();

    // Append a node at (x, y) with the given collision/radius weight.
    // Returns the stable node index (0 .. nodeCount()-1). Radius feeds both
    // the charge magnitude (∝ radius) and the minimum separation floor.
    int addNode(float x, float y, float radius);

    // Drop all edges; nodes and positions are kept.
    void removeAllEdges();

    // Spring between nodes a and b. restLen <= 0 uses the default link distance
    // (plus radii). strength is the Hooke coefficient (typical 0.2 … 1.0).
    // Duplicate undirected pairs are ignored (first wins). Self-edges ignored.
    // Prefer restLen >= ra+rb+collisionPad so springs do not fight collision.
    void addEdge(int a, int b, float restLen, float strength);

    // Advance the simulation by real frame dt (seconds). Internally runs a
    // fixed number of substeps so behavior is independent of frame rate.
    // Near-free when settled().
    void step(float dt);

    // Re-energize after a structural change (add/remove nodes or edges, bulk
    // setPos). alpha is clamped to (alphaMin, 1].
    void reheat(float alpha = 0.3f);

    // True when cooling has reached the settle floor — layout is effectively still.
    bool settled() const;

    float alpha() const;

    // Pin / unpin. Pinned nodes ignore forces (infinite mass); drag them with setPos.
    void setPinned(int i, bool pinned);
    bool isPinned(int i) const;

    // Teleport node i (e.g. preserve a seed layout or follow a pointer drag).
    // Clears velocity. No-op for out-of-range indices. NaN inputs ignored.
    void setPos(int i, float x, float y);

    int   nodeCount() const;
    float x(int i) const;
    float y(int i) const;

    // Tuning knobs (sane defaults applied in the ctor).
    void setRepulsion(float k);     // charge strength (default 900)
    void setCentering(float k);     // gravity toward centroid (default 0.02)
    void setLinkDistance(float d);  // default rest-length scale (default 60)
    void setAlphaDecay(float d);    // cooling rate per substep (default ~0.023)
    // Radius-aware non-overlap (d3 collide analogue). strength scales how hard
    // each collision pass separates overlap (default 11 ≈ near-hard at ~0.1×
    // blend per unit strength, clamped to 1); pad is the extra gap (default 3).
    void setCollision(float strength, float pad);
    // Velocity retain fraction after each substep integrate (default 0.6 → keep
    // 40% of prior velocity). Lower = snappier settle; higher = more glide.
    // Clamped to (0, 1].
    void setVelocityDecay(float d);
    // Community / component clustering. groupId < 0 means "no group".
    // Pull strength is cooling-scaled like centering (default 0 = off).
    void setGroup(int i, int groupId);
    void setGroupPull(float k);     // default 0; typical 0.04 … 0.12

private:
    struct Node {
        float x = 0.f, y = 0.f;
        float vx = 0.f, vy = 0.f;
        float radius = 14.f;
        float charge = 1.f;   // ∝ radius; used as q_i in repulsion
        bool  pinned = false;
        int   group = -1;     // community id for group-pull; -1 = none
    };

    struct Edge {
        int   a = 0, b = 0;
        float restLen = 60.f;
        float strength = 0.5f;
    };

    // Spatial hash: uniform grid over the current AABB, cell size = query cutoff.
    // Singly-linked lists per cell (head[cell] → node, next[node] → next).
    struct Grid {
        float originX = 0.f, originY = 0.f;
        float cell = 1.f;
        int   cols = 0, rows = 0;
        std::vector<int> head;
        std::vector<int> next;
    };

    void substep(float h);
    void applyRepulsion(std::vector<float>& fx, std::vector<float>& fy);
    void applySprings(std::vector<float>& fx, std::vector<float>& fy);
    void applyCentering(std::vector<float>& fx, std::vector<float>& fy);
    void applyGroupPull(std::vector<float>& fx, std::vector<float>& fy);
    // Multi-pass position/velocity non-overlap using a collDist-sized grid.
    void resolveCollisions();
    void rebuildGrid(float cellSize);
    bool validNode(int i) const;
    static float clampf(float v, float lo, float hi);
    static bool  finitef(float v);
    static std::uint64_t packEdge(int a, int b);

    std::vector<Node> nodes_;
    std::vector<Edge> edges_;
    // Sorted undirected edge keys ((min<<32)|max) for O(log E) duplicate reject.
    std::vector<std::uint64_t> edgeKeys_;

    Grid grid_;

    // Force accumulators reused across substeps (sized to N).
    std::vector<float> fx_;
    std::vector<float> fy_;

    float alpha_ = 1.f;
    float alphaMin_ = 0.001f;
    // ≈ 1 - pow(alphaMin, 1/300) — settles in ~300 substeps from alpha=1.
    float alphaDecay_ = 0.0228f;
    float velocityDecay_ = 0.6f;   // retain 40% of velocity each substep

    float repulsion_ = 900.f;
    float centering_ = 0.02f;
    float linkDistance_ = 60.f;
    float cutoff_ = 96.f;          // repulsion cutoff (≈ 1.6 × default link)
    float collStrength_ = 11.f;    // collision separation hardness (~10–12 hard)
    float collPad_ = 3.f;          // extra gap kept between node radii
    float groupPull_ = 0.f;        // pull toward per-group centroid (0 = off)

    // Fixed substep size and max substeps drained per step() call.
    static constexpr float kSubstep = 1.f / 60.f;
    static constexpr int   kMaxSubsteps = 4;
    // Gauss-Seidel collision passes after integrate (positions update between).
    static constexpr int   kCollIterations = 3;

    float dtAcc_ = 0.f;
};

}  // namespace ui
