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
//      inside the cutoff cell neighborhood (≪ n for typical layouts).
//   2. Edge springs         — Hooke  strength * (r - restLen)  along the edge.
//   3. Weak centering       — k_center * (centroid - p) pulls the cloud back
//      toward its own centroid so the graph doesn't drift off-screen.
//
// Integration: semi-implicit Euler (v += a*h; x += v*h) with per-step velocity
// damping. Pinned nodes have infinite mass (a=0, v=0); the UI teleports them
// via setPos. Cooling alpha decays each substep toward alphaMin; reheat()
// bumps it after structural edits; settled() is true when alpha <= alphaMin
// (step() early-outs).
//
// Units: positions/radii/rest lengths are abstract graph-space pixels. Time is
// seconds. Defaults target a pleasant d3-force-like feel for CAD project graphs.
// Deterministic: no unseeded RNG; index-hashed micro-jitter only when two nodes
// coincide exactly.
//
// Complexity notes (N nodes, E edges):
//   rebuild grid  O(N)
//   repulsion     O(N * avg bin occupancy in 3x3 neighborhood)  ≈ O(N) typical
//   springs       O(E)
//   centering     O(N)
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

private:
    struct Node {
        float x = 0.f, y = 0.f;
        float vx = 0.f, vy = 0.f;
        float radius = 14.f;
        float charge = 1.f;   // ∝ radius; used as q_i in repulsion
        bool  pinned = false;
    };

    struct Edge {
        int   a = 0, b = 0;
        float restLen = 60.f;
        float strength = 0.5f;
    };

    // Spatial hash: uniform grid over the current AABB, cell size = cutoff.
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
    void rebuildGrid();
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
    float cutoff_ = 120.f;         // repulsion cutoff (≈ 2 × default link)

    // Fixed substep size and max substeps drained per step() call.
    static constexpr float kSubstep = 1.f / 60.f;
    static constexpr int   kMaxSubsteps = 4;

    float dtAcc_ = 0.f;
};

}  // namespace ui
