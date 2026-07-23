// PLM Graph — force-directed layout engine (see graph_force.h).
// 2D by default; set3D(true) promotes every force to (x, y, z).
#include "graph_force.h"

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace ui {
namespace {

// Deterministic unit-ish jitter from a node index — only used when two nodes
// land on the exact same coordinate so repulsion has a defined direction.
inline void indexJitter(int i, float &jx, float &jy) {
  // Simple LCG-ish mix; result in roughly [-1, 1].
  std::uint32_t h = static_cast<std::uint32_t>(i) * 2654435761u;
  h ^= h >> 16;
  h *= 2246822519u;
  jx = (static_cast<float>(static_cast<int>(h & 0xFFFFu) - 32768)) *
       (1.f / 32768.f);
  h ^= h >> 13;
  jy = (static_cast<float>(static_cast<int>(h & 0xFFFFu) - 32768)) *
       (1.f / 32768.f);
  // Avoid exact zero.
  if (jx == 0.f && jy == 0.f) {
    jx = 1.f;
    jy = 0.f;
  }
}

inline void indexJitter3(int i, float &jx, float &jy, float &jz) {
  indexJitter(i, jx, jy);
  std::uint32_t h = static_cast<std::uint32_t>(i) * 2246822519u + 374761393u;
  h ^= h >> 15;
  h *= 2654435761u;
  jz = (static_cast<float>(static_cast<int>(h & 0xFFFFu) - 32768)) *
       (1.f / 32768.f);
  if (jx == 0.f && jy == 0.f && jz == 0.f)
    jz = 1.f;
}

} // namespace

ForceGraph::ForceGraph() = default;

float ForceGraph::clampf(float v, float lo, float hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

bool ForceGraph::finitef(float v) { return std::isfinite(v); }

std::uint64_t ForceGraph::packEdge(int a, int b) {
  const std::uint32_t lo = static_cast<std::uint32_t>(a < b ? a : b);
  const std::uint32_t hi = static_cast<std::uint32_t>(a < b ? b : a);
  return (static_cast<std::uint64_t>(lo) << 32) |
         static_cast<std::uint64_t>(hi);
}

bool ForceGraph::validNode(int i) const {
  return i >= 0 && i < static_cast<int>(nodes_.size());
}

void ForceGraph::clear() {
  nodes_.clear();
  edges_.clear();
  edgeKeys_.clear();
  grid_ = Grid{};
  fx_.clear();
  fy_.clear();
  fz_.clear();
  alpha_ = 1.f;
  dtAcc_ = 0.f;
}

int ForceGraph::addNode(float x, float y, float radius) {
  return addNode(x, y, 0.f, radius);
}

int ForceGraph::addNode(float x, float y, float z, float radius) {
  if (!finitef(x) || !finitef(y)) {
    x = 0.f;
    y = 0.f;
  }
  if (!finitef(z))
    z = 0.f;
  if (!finitef(radius) || radius <= 0.f)
    radius = 14.f;

  Node n;
  n.x = x;
  n.y = y;
  n.z = z;
  n.radius = radius;
  // Charge scales with radius so larger nodes push harder.
  n.charge = clampf(radius / 14.f, 0.25f, 8.f);
  nodes_.push_back(n);
  return static_cast<int>(nodes_.size()) - 1;
}

void ForceGraph::set3D(bool on) { threeD_ = on; }

bool ForceGraph::is3D() const { return threeD_; }

void ForceGraph::removeAllEdges() {
  edges_.clear();
  edgeKeys_.clear();
}

void ForceGraph::addEdge(int a, int b, float restLen, float strength) {
  if (!validNode(a) || !validNode(b) || a == b)
    return;

  const std::uint64_t key = packEdge(a, b);
  const auto it = std::lower_bound(edgeKeys_.begin(), edgeKeys_.end(), key);
  if (it != edgeKeys_.end() && *it == key)
    return; // duplicate
  edgeKeys_.insert(it, key);

  Edge e;
  e.a = a;
  e.b = b;
  if (!finitef(strength) || strength < 0.f)
    strength = 0.5f;
  e.strength = strength;

  if (!finitef(restLen) || restLen <= 0.f) {
    // Default rest length: linkDistance + radii contribution.
    restLen = linkDistance_ + 0.5f * (nodes_[static_cast<size_t>(a)].radius +
                                      nodes_[static_cast<size_t>(b)].radius);
  }
  e.restLen = restLen;
  edges_.push_back(e);
}

void ForceGraph::reheat(float alpha) {
  if (!finitef(alpha))
    alpha = 0.3f;
  alpha_ = clampf(alpha, alphaMin_ * 2.f, 1.f);
  dtAcc_ = 0.f;
}

bool ForceGraph::settled() const { return alpha_ <= 0.f; }

float ForceGraph::alpha() const { return alpha_; }

void ForceGraph::setPinned(int i, bool pinned) {
  if (!validNode(i))
    return;
  Node &n = nodes_[static_cast<size_t>(i)];
  n.pinned = pinned;
  if (pinned) {
    n.vx = 0.f;
    n.vy = 0.f;
    n.vz = 0.f;
  }
}

bool ForceGraph::isPinned(int i) const {
  if (!validNode(i))
    return false;
  return nodes_[static_cast<size_t>(i)].pinned;
}

void ForceGraph::setPos(int i, float x, float y) {
  if (!validNode(i))
    return;
  if (!finitef(x) || !finitef(y))
    return;
  Node &n = nodes_[static_cast<size_t>(i)];
  n.x = x;
  n.y = y;
  n.vx = 0.f;
  n.vy = 0.f;
}

float ForceGraph::z(int i) const {
  if (!validNode(i))
    return 0.f;
  return nodes_[static_cast<size_t>(i)].z;
}

void ForceGraph::setZ(int i, float zv) {
  if (!validNode(i) || !finitef(zv))
    return;
  Node &n = nodes_[static_cast<size_t>(i)];
  n.z = zv;
  n.vz = 0.f;
}

int ForceGraph::nodeCount() const { return static_cast<int>(nodes_.size()); }

float ForceGraph::x(int i) const {
  if (!validNode(i))
    return 0.f;
  return nodes_[static_cast<size_t>(i)].x;
}

float ForceGraph::y(int i) const {
  if (!validNode(i))
    return 0.f;
  return nodes_[static_cast<size_t>(i)].y;
}

void ForceGraph::setRepulsion(float k) {
  if (!finitef(k) || k < 0.f)
    return;
  repulsion_ = k;
}

void ForceGraph::setCentering(float k) {
  if (!finitef(k) || k < 0.f)
    return;
  centering_ = k;
}

void ForceGraph::setLinkDistance(float d) {
  if (!finitef(d) || d <= 0.f)
    return;
  linkDistance_ = d;
  // Keep cutoff ≈ 2× link distance so linked pairs always interact.
  cutoff_ = d * 1.6f;
  if (cutoff_ < 40.f)
    cutoff_ = 40.f;
}

void ForceGraph::setAlphaDecay(float d) {
  if (!finitef(d) || d <= 0.f || d >= 1.f)
    return;
  alphaDecay_ = d;
}

void ForceGraph::setCollision(float strength, float pad) {
  if (finitef(strength) && strength >= 0.f)
    collStrength_ = strength;
  if (finitef(pad) && pad >= 0.f)
    collPad_ = pad;
}

void ForceGraph::setVelocityDecay(float d) {
  if (!finitef(d))
    return;
  // Retain fraction of prior velocity each substep; (0, 1].
  velocityDecay_ = clampf(d, 1e-4f, 1.f);
}

void ForceGraph::setGroup(int i, int groupId) {
  if (!validNode(i))
    return;
  nodes_[static_cast<size_t>(i)].group = groupId;
}

void ForceGraph::setGroupPull(float k) {
  if (!finitef(k) || k < 0.f)
    return;
  groupPull_ = k;
}

// ---- spatial grid
// ------------------------------------------------------------

void ForceGraph::rebuildGrid(float cellSize) {
  const int n = static_cast<int>(nodes_.size());
  grid_.next.assign(static_cast<size_t>(n), -1);

  if (n == 0) {
    grid_.head.clear();
    grid_.cols = grid_.rows = 0;
    grid_.slices = 1;
    return;
  }

  float minX = nodes_[0].x, maxX = nodes_[0].x;
  float minY = nodes_[0].y, maxY = nodes_[0].y;
  float minZ = nodes_[0].z, maxZ = nodes_[0].z;
  for (int i = 1; i < n; ++i) {
    const Node &nd = nodes_[static_cast<size_t>(i)];
    if (nd.x < minX)
      minX = nd.x;
    if (nd.x > maxX)
      maxX = nd.x;
    if (nd.y < minY)
      minY = nd.y;
    if (nd.y > maxY)
      maxY = nd.y;
    if (nd.z < minZ)
      minZ = nd.z;
    if (nd.z > maxZ)
      maxZ = nd.z;
  }
  if (!threeD_) {
    minZ = 0.f;
    maxZ = 0.f;
  }

  // Pad by one cell so boundary nodes still see a full neighborhood.
  const float cell = (finitef(cellSize) && cellSize > 1.f) ? cellSize : 1.f;
  const float pad = cell;
  minX -= pad;
  minY -= pad;
  minZ -= pad;
  maxX += pad;
  maxY += pad;
  maxZ += pad;

  int cols = static_cast<int>(std::ceil((maxX - minX) / cell));
  int rows = static_cast<int>(std::ceil((maxY - minY) / cell));
  int slices = threeD_ ? static_cast<int>(std::ceil((maxZ - minZ) / cell)) : 1;
  if (cols < 1)
    cols = 1;
  if (rows < 1)
    rows = 1;
  if (slices < 1)
    slices = 1;
  // Cap grid size so a pathological spread can't allocate gigabytes.
  constexpr int kMaxDim = 512;
  if (cols > kMaxDim)
    cols = kMaxDim;
  if (rows > kMaxDim)
    rows = kMaxDim;
  if (slices > kMaxDim)
    slices = kMaxDim;

  grid_.originX = minX;
  grid_.originY = minY;
  grid_.originZ = minZ;
  grid_.cell = cell;
  grid_.cols = cols;
  grid_.rows = rows;
  grid_.slices = slices;

  const int cells = cols * rows * slices;
  grid_.head.assign(static_cast<size_t>(cells), -1);

  const float invCell = 1.f / cell;
  for (int i = 0; i < n; ++i) {
    const Node &nd = nodes_[static_cast<size_t>(i)];
    int cx = static_cast<int>((nd.x - minX) * invCell);
    int cy = static_cast<int>((nd.y - minY) * invCell);
    int cz = static_cast<int>((nd.z - minZ) * invCell);
    if (cx < 0)
      cx = 0;
    else if (cx >= cols)
      cx = cols - 1;
    if (cy < 0)
      cy = 0;
    else if (cy >= rows)
      cy = rows - 1;
    if (cz < 0)
      cz = 0;
    else if (cz >= slices)
      cz = slices - 1;
    const int c = (cz * rows + cy) * cols + cx;
    grid_.next[static_cast<size_t>(i)] = grid_.head[static_cast<size_t>(c)];
    grid_.head[static_cast<size_t>(c)] = i;
  }
}

// ---- forces
// ------------------------------------------------------------------

void ForceGraph::applyRepulsion(std::vector<float> &fx, std::vector<float> &fy,
                                std::vector<float> &fz) {
  const int n = static_cast<int>(nodes_.size());
  if (n < 2)
    return;

  rebuildGrid(cutoff_);

  const float cutoff = cutoff_;
  const float cutoff2 = cutoff * cutoff;
  const float k = repulsion_ * alpha_;
  const int cols = grid_.cols;
  const int rows = grid_.rows;
  const int slices = grid_.slices;

  for (int i = 0; i < n; ++i) {
    const Node &a = nodes_[static_cast<size_t>(i)];

    int cx = static_cast<int>((a.x - grid_.originX) / grid_.cell);
    int cy = static_cast<int>((a.y - grid_.originY) / grid_.cell);
    int cz = static_cast<int>((a.z - grid_.originZ) / grid_.cell);
    if (cx < 0)
      cx = 0;
    else if (cx >= cols)
      cx = cols - 1;
    if (cy < 0)
      cy = 0;
    else if (cy >= rows)
      cy = rows - 1;
    if (cz < 0)
      cz = 0;
    else if (cz >= slices)
      cz = slices - 1;

    const int x0 = cx > 0 ? cx - 1 : 0;
    const int x1 = cx + 1 < cols ? cx + 1 : cols - 1;
    const int y0 = cy > 0 ? cy - 1 : 0;
    const int y1 = cy + 1 < rows ? cy + 1 : rows - 1;
    const int z0 = cz > 0 ? cz - 1 : 0;
    const int z1 = cz + 1 < slices ? cz + 1 : slices - 1;

    float fxi = 0.f, fyi = 0.f, fzi = 0.f;
    const float qa = a.charge;

    for (int gz = z0; gz <= z1; ++gz) {
      for (int gy = y0; gy <= y1; ++gy) {
        for (int gx = x0; gx <= x1; ++gx) {
          int j = grid_.head[static_cast<size_t>((gz * rows + gy) * cols + gx)];
          while (j >= 0) {
            // Each unordered pair evaluated exactly once (j > i).
            if (j > i) {
              const Node &b = nodes_[static_cast<size_t>(j)];
              float dx = a.x - b.x;
              float dy = a.y - b.y;
              float dz = a.z - b.z;
              float d2 = dx * dx + dy * dy + dz * dz;

              if (d2 < cutoff2 && !(a.pinned && b.pinned)) {
                // Minimum separation floor from radii; also
                // handles exact coincidence via deterministic
                // jitter.
                const float minDist = 0.5f * (a.radius + b.radius) + 1.f;
                const float minDist2 = minDist * minDist;
                if (d2 < 1e-12f) {
                  float jx, jy, jz;
                  indexJitter3(i - j, jx, jy, jz);
                  dx = jx;
                  dy = jy;
                  dz = jz;
                  d2 = dx * dx + dy * dy + dz * dz;
                }
                if (d2 < minDist2)
                  d2 = minDist2;

                // F = k * qa * qb / r^2 along the separation.
                // Collision is post-integrate in resolveCollisions().
                const float invDist = 1.f / std::sqrt(d2);
                const float f = k * qa * b.charge * invDist * invDist;
                const float s = f * invDist;
                const float rfx = dx * s;
                const float rfy = dy * s;
                const float rfz = dz * s;

                // +f pushes a away from b; −f pushes b away from a.
                if (!a.pinned) {
                  fxi += rfx;
                  fyi += rfy;
                  fzi += rfz;
                }
                if (!b.pinned) {
                  fx[static_cast<size_t>(j)] -= rfx;
                  fy[static_cast<size_t>(j)] -= rfy;
                  fz[static_cast<size_t>(j)] -= rfz;
                }
              }
            }
            j = grid_.next[static_cast<size_t>(j)];
          }
        }
      }
    }

    fx[static_cast<size_t>(i)] += fxi;
    fy[static_cast<size_t>(i)] += fyi;
    fz[static_cast<size_t>(i)] += fzi;
  }
}

void ForceGraph::applySprings(std::vector<float> &fx, std::vector<float> &fy,
                              std::vector<float> &fz) {
  const float aMul = alpha_;
  for (const Edge &e : edges_) {
    Node &na = nodes_[static_cast<size_t>(e.a)];
    Node &nb = nodes_[static_cast<size_t>(e.b)];

    float dx = nb.x - na.x;
    float dy = nb.y - na.y;
    float dz = nb.z - na.z;
    float d2 = dx * dx + dy * dy + dz * dz;

    if (d2 < 1e-12f) {
      float jx, jy, jz;
      indexJitter3(e.a, jx, jy, jz);
      dx = jx;
      dy = jy;
      dz = jz;
      d2 = dx * dx + dy * dy + dz * dz;
    }

    const float dist = std::sqrt(d2);
    // Hooke: force magnitude = strength * (dist - rest) ; pull when stretched.
    const float displacement = dist - e.restLen;
    const float f = e.strength * displacement * aMul;
    const float inv = 1.f / dist;
    const float fxComp = dx * inv * f;
    const float fyComp = dy * inv * f;
    const float fzComp = dz * inv * f;

    if (!na.pinned) {
      fx[static_cast<size_t>(e.a)] += fxComp;
      fy[static_cast<size_t>(e.a)] += fyComp;
      fz[static_cast<size_t>(e.a)] += fzComp;
    }
    if (!nb.pinned) {
      fx[static_cast<size_t>(e.b)] -= fxComp;
      fy[static_cast<size_t>(e.b)] -= fyComp;
      fz[static_cast<size_t>(e.b)] -= fzComp;
    }
  }
}

void ForceGraph::applyCentering(std::vector<float> &fx, std::vector<float> &fy,
                                std::vector<float> &fz) {
  const int n = static_cast<int>(nodes_.size());
  if (n == 0 || centering_ <= 0.f)
    return;

  // Centroid of free (unpinned) nodes; fall back to all if everything is
  // pinned.
  double sx = 0.0, sy = 0.0, sz = 0.0;
  int count = 0;
  for (int i = 0; i < n; ++i) {
    const Node &nd = nodes_[static_cast<size_t>(i)];
    if (nd.pinned)
      continue;
    sx += nd.x;
    sy += nd.y;
    sz += nd.z;
    ++count;
  }
  if (count == 0)
    return;

  const float cx = static_cast<float>(sx / count);
  const float cy = static_cast<float>(sy / count);
  const float cz = static_cast<float>(sz / count);
  const float k = centering_ * alpha_;

  for (int i = 0; i < n; ++i) {
    const Node &nd = nodes_[static_cast<size_t>(i)];
    if (nd.pinned)
      continue;
    // Pull toward centroid: F = k * (c - p)
    fx[static_cast<size_t>(i)] += k * (cx - nd.x);
    fy[static_cast<size_t>(i)] += k * (cy - nd.y);
    fz[static_cast<size_t>(i)] += k * (cz - nd.z);
  }
}

void ForceGraph::applyGroupPull(std::vector<float> &fx, std::vector<float> &fy,
                                std::vector<float> &fz) {
  const int n = static_cast<int>(nodes_.size());
  if (n == 0 || groupPull_ <= 0.f || alpha_ <= 0.f)
    return;

  // Find group id range (groups are dense small ints from the UI).
  int maxG = -1;
  for (int i = 0; i < n; ++i) {
    const int g = nodes_[static_cast<size_t>(i)].group;
    if (g > maxG)
      maxG = g;
  }
  if (maxG < 0)
    return;

  const int G = maxG + 1;
  std::vector<double> sx(static_cast<size_t>(G), 0.0),
      sy(static_cast<size_t>(G), 0.0), sz(static_cast<size_t>(G), 0.0);
  std::vector<int> cnt(static_cast<size_t>(G), 0);
  for (int i = 0; i < n; ++i) {
    const Node &nd = nodes_[static_cast<size_t>(i)];
    if (nd.group < 0 || nd.pinned)
      continue;
    sx[static_cast<size_t>(nd.group)] += nd.x;
    sy[static_cast<size_t>(nd.group)] += nd.y;
    sz[static_cast<size_t>(nd.group)] += nd.z;
    cnt[static_cast<size_t>(nd.group)] += 1;
  }

  const float k = groupPull_ * alpha_;
  for (int i = 0; i < n; ++i) {
    const Node &nd = nodes_[static_cast<size_t>(i)];
    if (nd.pinned || nd.group < 0)
      continue;
    const int g = nd.group;
    if (cnt[static_cast<size_t>(g)] < 2)
      continue;
    const float cx = static_cast<float>(sx[static_cast<size_t>(g)] /
                                        cnt[static_cast<size_t>(g)]);
    const float cy = static_cast<float>(sy[static_cast<size_t>(g)] /
                                        cnt[static_cast<size_t>(g)]);
    const float cz = static_cast<float>(sz[static_cast<size_t>(g)] /
                                        cnt[static_cast<size_t>(g)]);
    fx[static_cast<size_t>(i)] += k * (cx - nd.x);
    fy[static_cast<size_t>(i)] += k * (cy - nd.y);
    fz[static_cast<size_t>(i)] += k * (cz - nd.z);
  }
}

void ForceGraph::resolveCollisions() {
  const int n = static_cast<int>(nodes_.size());
  if (n < 2 || collStrength_ <= 0.f)
    return;

  float maxR = 0.f;
  for (int i = 0; i < n; ++i) {
    const float r = nodes_[static_cast<size_t>(i)].radius;
    if (r > maxR)
      maxR = r;
  }
  // Cell must cover the largest possible collDist so pairs outside the
  // Coulomb cutoff still meet in the 3x3(x3) neighborhood.
  const float cell = std::max(2.f * (maxR + collPad_), 1.f);
  // Blend factor per pass: strength ~11 → near-hard separation (clamped).
  const float blend = clampf(collStrength_ * 0.1f, 0.f, 1.f);

  for (int pass = 0; pass < kCollIterations; ++pass) {
    rebuildGrid(cell);
    const int cols = grid_.cols;
    const int rows = grid_.rows;
    const int slices = grid_.slices;
    if (cols <= 0 || rows <= 0)
      return;

    for (int i = 0; i < n; ++i) {
      Node &a = nodes_[static_cast<size_t>(i)];
      int cx = static_cast<int>((a.x - grid_.originX) / grid_.cell);
      int cy = static_cast<int>((a.y - grid_.originY) / grid_.cell);
      int cz = static_cast<int>((a.z - grid_.originZ) / grid_.cell);
      if (cx < 0)
        cx = 0;
      else if (cx >= cols)
        cx = cols - 1;
      if (cy < 0)
        cy = 0;
      else if (cy >= rows)
        cy = rows - 1;
      if (cz < 0)
        cz = 0;
      else if (cz >= slices)
        cz = slices - 1;

      const int x0 = cx > 0 ? cx - 1 : 0;
      const int x1 = cx + 1 < cols ? cx + 1 : cols - 1;
      const int y0 = cy > 0 ? cy - 1 : 0;
      const int y1 = cy + 1 < rows ? cy + 1 : rows - 1;
      const int z0 = cz > 0 ? cz - 1 : 0;
      const int z1 = cz + 1 < slices ? cz + 1 : slices - 1;

      for (int gz = z0; gz <= z1; ++gz) {
        for (int gy = y0; gy <= y1; ++gy) {
          for (int gx = x0; gx <= x1; ++gx) {
            int j =
                grid_.head[static_cast<size_t>((gz * rows + gy) * cols + gx)];
            while (j >= 0) {
              if (j > i) {
                Node &b = nodes_[static_cast<size_t>(j)];
                if (a.pinned && b.pinned) {
                  j = grid_.next[static_cast<size_t>(j)];
                  continue;
                }
                float dx = b.x - a.x;
                float dy = b.y - a.y;
                float dz = b.z - a.z;
                float d2 = dx * dx + dy * dy + dz * dz;
                const float collDist = a.radius + b.radius + collPad_;
                const float collDist2 = collDist * collDist;
                if (d2 >= collDist2) {
                  j = grid_.next[static_cast<size_t>(j)];
                  continue;
                }
                float d;
                if (d2 < 1e-12f) {
                  float jx, jy, jz;
                  indexJitter3(i - j, jx, jy, jz);
                  dx = jx;
                  dy = jy;
                  dz = jz;
                  d = std::sqrt(dx * dx + dy * dy + dz * dz);
                } else {
                  d = std::sqrt(d2);
                }
                const float inv = d > 1e-6f ? 1.f / d : 1.f;
                const float nx = dx * inv;
                const float ny = dy * inv;
                // 2D mode never touches z (depth preserved).
                const float nz = threeD_ ? dz * inv : 0.f;
                const float overlap = collDist - d;
                const float corr = overlap * blend;

                // Gauss-Seidel: move free nodes apart immediately
                // so later pairs in this pass see updated
                // positions.
                if (a.pinned && !b.pinned) {
                  b.x += nx * corr;
                  b.y += ny * corr;
                  b.z += nz * corr;
                  b.vx += nx * corr;
                  b.vy += ny * corr;
                  b.vz += nz * corr;
                } else if (!a.pinned && b.pinned) {
                  a.x -= nx * corr;
                  a.y -= ny * corr;
                  a.z -= nz * corr;
                  a.vx -= nx * corr;
                  a.vy -= ny * corr;
                  a.vz -= nz * corr;
                } else if (!a.pinned && !b.pinned) {
                  const float half = 0.5f * corr;
                  a.x -= nx * half;
                  a.y -= ny * half;
                  a.z -= nz * half;
                  b.x += nx * half;
                  b.y += ny * half;
                  b.z += nz * half;
                  a.vx -= nx * half;
                  a.vy -= ny * half;
                  a.vz -= nz * half;
                  b.vx += nx * half;
                  b.vy += ny * half;
                  b.vz += nz * half;
                }
              }
              j = grid_.next[static_cast<size_t>(j)];
            }
          }
        }
      }
    }
  }
}

void ForceGraph::substep(float h) {
  const int n = static_cast<int>(nodes_.size());
  if (n == 0) {
    alpha_ *= (1.f - alphaDecay_);
    if (alpha_ < alphaMin_)
      alpha_ = 0.f;
    return;
  }

  fx_.assign(static_cast<size_t>(n), 0.f);
  fy_.assign(static_cast<size_t>(n), 0.f);
  fz_.assign(static_cast<size_t>(n), 0.f);

  applyRepulsion(fx_, fy_, fz_);
  applySprings(fx_, fy_, fz_);
  applyCentering(fx_, fy_, fz_);
  applyGroupPull(fx_, fy_, fz_);

  // Semi-implicit Euler + velocity damping. Mass = 1 for free nodes.
  const float damp = velocityDecay_;
  constexpr float kMaxSpeed = 2000.f; // hard cap to kill numerical blow-ups

  for (int i = 0; i < n; ++i) {
    Node &nd = nodes_[static_cast<size_t>(i)];
    if (nd.pinned) {
      nd.vx = 0.f;
      nd.vy = 0.f;
      nd.vz = 0.f;
      continue;
    }

    nd.vx = (nd.vx + fx_[static_cast<size_t>(i)] * h) * damp;
    nd.vy = (nd.vy + fy_[static_cast<size_t>(i)] * h) * damp;
    nd.vz = (nd.vz + fz_[static_cast<size_t>(i)] * h) * damp;

    // Speed clamp.
    const float sp2 = nd.vx * nd.vx + nd.vy * nd.vy + nd.vz * nd.vz;
    if (sp2 > kMaxSpeed * kMaxSpeed) {
      const float inv = kMaxSpeed / std::sqrt(sp2);
      nd.vx *= inv;
      nd.vy *= inv;
      nd.vz *= inv;
    }

    nd.x += nd.vx * h;
    nd.y += nd.vy * h;
    if (threeD_)
      nd.z += nd.vz * h;
    else
      nd.vz = 0.f; // 2D mode: depth is a static attribute

    // NaN/Inf guard — freeze the node rather than poison the whole graph.
    if (!finitef(nd.x) || !finitef(nd.y) || !finitef(nd.z) || !finitef(nd.vx) ||
        !finitef(nd.vy) || !finitef(nd.vz)) {
      nd.x = finitef(nd.x) ? nd.x : 0.f;
      nd.y = finitef(nd.y) ? nd.y : 0.f;
      nd.z = finitef(nd.z) ? nd.z : 0.f;
      nd.vx = 0.f;
      nd.vy = 0.f;
      nd.vz = 0.f;
    }
  }

  // Hard non-overlap after integrate (cooling-independent). Multi-pass so
  // dense packs separate even when springs briefly crush discs.
  resolveCollisions();

  // Simulated-annealing cool-down: geometric decay toward ZERO (d3-force
  // semantics), snapping settled once below the floor.
  alpha_ *= (1.f - alphaDecay_);
  if (alpha_ < alphaMin_)
    alpha_ = 0.f;
}

void ForceGraph::step(float dt) {
  if (!finitef(dt) || dt <= 0.f)
    return;
  if (settled()) {
    dtAcc_ = 0.f;
    return;
  }
  if (nodes_.empty()) {
    // Still cool alpha so settled() eventually trips on empty graphs.
    alpha_ = 0.f;
    return;
  }

  // Cap a single frame's catch-up so a hitch doesn't explode the sim.
  constexpr float kMaxDt = kSubstep * kMaxSubsteps;
  if (dt > kMaxDt)
    dt = kMaxDt;

  dtAcc_ += dt;
  int guard = 0;
  while (dtAcc_ >= kSubstep && guard < kMaxSubsteps) {
    substep(kSubstep);
    dtAcc_ -= kSubstep;
    ++guard;
    if (settled()) {
      dtAcc_ = 0.f;
      break;
    }
  }
  // Don't let residual accumulator grow without bound if we hit the guard.
  if (dtAcc_ > kSubstep)
    dtAcc_ = kSubstep;
}

} // namespace ui
