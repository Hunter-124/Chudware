// Topology geometry artifacts — see sim/topopt_surface.h.
#include "sim/topopt_surface.h"

#include "geom/aicm.h"
#include "sim/overlay.h" // kMaxOverlayTris (constant only, no link dependency)
#include "util/resource_governor.h"
#include "util/sha256.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <limits>
#include <numeric>
#include <string_view>
#include <system_error>
#include <unordered_map>
#include <utility>

#if defined(_WIN32)
#include <io.h>
#else
#include <unistd.h>
#endif

namespace chudware {
namespace sim {
namespace topology {

namespace {

[[noreturn]] void fail(const std::string &code, const std::string &message) {
  throw ModelError(code, message);
}

std::string num(double v) {
  char buf[64];
  const auto r = std::to_chars(buf, buf + sizeof(buf), v);
  return std::string(buf, r.ptr);
}

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();
constexpr double kPi = 3.14159265358979323846;

struct UnionFind {
  std::vector<int> parent;
  explicit UnionFind(int n) : parent(static_cast<std::size_t>(n)) {
    std::iota(parent.begin(), parent.end(), 0);
  }
  int find(int x) {
    while (parent[x] != x) {
      parent[x] = parent[parent[x]];
      x = parent[x];
    }
    return x;
  }
  void unite(int a, int b) {
    a = find(a);
    b = find(b);
    if (a != b)
      parent[std::max(a, b)] = std::min(a, b);
  }
};

void admit_or_fail(double bytes, const char *what) {
  const double cap =
      static_cast<double>(std::numeric_limits<std::uint64_t>::max());
  const std::uint64_t estimate = bytes >= cap
                                     ? std::numeric_limits<std::uint64_t>::max()
                                     : static_cast<std::uint64_t>(bytes);
  std::string why;
  if (!governor::admit(estimate, what, why))
    fail("resource_limit", why);
}

void check_threshold(double t, const char *what) {
  if (!(std::isfinite(t) && t > 0.0 && t < 1.0))
    fail("invalid_input",
         std::string(what) + " threshold must lie in (0,1), got " + num(t));
}

void check_options(const SurfaceOptions &o) {
  check_threshold(o.threshold, "surface");
  const double limit = 0.5 * std::min(o.threshold, 1.0 - o.threshold);
  if (!(std::isfinite(o.threshold_snap) && o.threshold_snap >= 0.0 &&
        o.threshold_snap <= 0.01 && o.threshold_snap < limit))
    fail("invalid_input",
         "threshold_snap must lie in [0, min(0.01, min(threshold, "
         "1-threshold)/2)), got " +
             num(o.threshold_snap));
}

void check_element_density(const TetMesh &m, const Eigen::VectorXd &rho) {
  if (rho.size() != m.element_count())
    fail("invalid_density", "density has " + std::to_string(rho.size()) +
                                " entries, expected " +
                                std::to_string(m.element_count()));
  for (Eigen::Index e = 0; e < rho.size(); ++e)
    if (!(std::isfinite(rho[e]) && rho[e] >= 0.0 && rho[e] <= 1.0))
      fail("invalid_density", "element " + std::to_string(e) + " density " +
                                  num(rho[e]) + " outside [0,1]");
}

void check_nodal_density(const TetMesh &m, const Eigen::VectorXd &d) {
  if (d.size() != m.node_count())
    fail("invalid_density", "nodal density has " + std::to_string(d.size()) +
                                " entries, expected " +
                                std::to_string(m.node_count()));
  for (int n = 0; n < m.node_count(); ++n)
    if (m.node_referenced[n] &&
        !(std::isfinite(d[n]) && d[n] >= 0.0 && d[n] <= 1.0))
      fail("invalid_density", "node " + std::to_string(n) + " density " +
                                  num(d[n]) + " outside [0,1]");
}

void check_surface_shape(const SurfaceMesh &s) {
  const int V = s.vertex_count(), T = s.triangle_count();
  if (static_cast<int>(s.vertex_source.size()) != V ||
      s.triangle_element.size() != T ||
      static_cast<int>(s.triangle_origin.size()) != T)
    fail("invalid_input", "surface arrays disagree on vertex/triangle counts");
  for (int t = 0; t < T; ++t)
    for (int k = 0; k < 3; ++k)
      if (s.triangles(k, t) < 0 || s.triangles(k, t) >= V)
        fail("invalid_input", "surface triangle " + std::to_string(t) +
                                  " references vertex " +
                                  std::to_string(s.triangles(k, t)) +
                                  " outside [0," + std::to_string(V) + ")");
}

std::uint64_t pair_key(int a, int b) {
  return (static_cast<std::uint64_t>(static_cast<std::uint32_t>(a)) << 32) |
         static_cast<std::uint32_t>(b);
}

// Rotation of a triangle to start at its smallest id; parity +1 when the
// remaining two ascend (even permutation of the sorted triple).
struct TriKey {
  std::array<int, 3> sorted;
  int parity;
};
TriKey tri_key(int a, int b, int c) {
  if (b < a && b < c) {
    const int t = a;
    a = b;
    b = c;
    c = t;
  } else if (c < a && c < b) {
    const int t = c;
    c = b;
    b = a;
    a = t;
  }
  TriKey k;
  k.parity = b < c ? 1 : -1;
  k.sorted = {a, std::min(b, c), std::max(b, c)};
  return k;
}

// ── Exclusive, streamed, hashed artifact file
// ────────────────────────────────── Creates `path` exclusively ("wbx": an
// existing file or symlink is refused, never truncated). Until finish()
// succeeds, destruction or a failed write removes the partial file this object
// created — and only that file.
class ArtifactWriter {
public:
  ArtifactWriter(const std::string &path, const char *what) : path_(path) {
    namespace fs = std::filesystem;
    if (path.empty())
      fail("invalid_input", std::string(what) + " path is empty");
    const fs::path p(path);
    std::error_code ec;
    if (fs::exists(fs::symlink_status(p, ec)))
      fail("artifact_exists",
           path + " already exists; artifacts are never overwritten");
    if (p.has_parent_path()) {
      ec.clear();
      fs::create_directories(p.parent_path(), ec);
      if (ec && !fs::is_directory(p.parent_path()))
        fail("artifact_io", "cannot create directory " +
                                p.parent_path().string() + ": " + ec.message());
    }
    errno = 0;
    file_ = std::fopen(path.c_str(), "wbx");
    if (!file_) {
      const int err = errno;
      if (err == EEXIST)
        fail("artifact_exists",
             path + " already exists; artifacts are never overwritten");
      fail("artifact_io", "cannot create " + path + ": " + std::strerror(err));
    }
    buffer_.reserve(kBuffer);
  }
  ArtifactWriter(const ArtifactWriter &) = delete;
  ArtifactWriter &operator=(const ArtifactWriter &) = delete;
  ~ArtifactWriter() {
    if (file_) {
      std::fclose(file_);
      std::error_code ec;
      std::filesystem::remove(path_, ec);
    }
  }

  void write(const void *data, std::size_t n) {
    const auto *p = static_cast<const unsigned char *>(data);
    sha_.update(p, n);
    bytes_ += n;
    while (n > 0) {
      const std::size_t take = std::min(n, kBuffer - buffer_.size());
      buffer_.insert(buffer_.end(), p, p + take);
      p += take;
      n -= take;
      if (buffer_.size() == kBuffer)
        drain();
    }
  }
  void write(std::string_view s) { write(s.data(), s.size()); }

  ArtifactFile finish(const char *format) {
    drain();
    if (std::fflush(file_) != 0)
      io_fail("flush");
#if defined(_WIN32)
    if (_commit(_fileno(file_)) != 0)
      io_fail("sync");
#else
    if (::fsync(::fileno(file_)) != 0)
      io_fail("sync");
#endif
    FILE *f = file_;
    file_ = nullptr;
    if (std::fclose(f) != 0) {
      const int err = errno;
      std::error_code ec;
      std::filesystem::remove(path_, ec);
      fail("artifact_io", "close " + path_ + " failed: " + std::strerror(err));
    }
    ArtifactFile a;
    a.path = path_;
    a.format = format;
    a.bytes = bytes_;
    a.sha256 = sha_.finish_hex();
    return a;
  }

private:
  static constexpr std::size_t kBuffer = std::size_t(1) << 20;
  void drain() {
    if (buffer_.empty())
      return;
    if (std::fwrite(buffer_.data(), 1, buffer_.size(), file_) != buffer_.size())
      io_fail("write");
    buffer_.clear();
  }
  [[noreturn]] void io_fail(const char *op) {
    const int err = errno;
    std::fclose(file_);
    file_ = nullptr;
    std::error_code ec;
    std::filesystem::remove(path_, ec);
    fail("artifact_io",
         std::string(op) + " " + path_ + " failed: " + std::strerror(err));
  }

  std::string path_;
  FILE *file_ = nullptr;
  std::vector<unsigned char> buffer_;
  util::Sha256 sha_;
  std::uint64_t bytes_ = 0;
};

// Text staging for the XML writer: numbers go through std::to_chars (shortest
// round-trip, locale-independent) into a reused buffer.
class TextOut {
public:
  explicit TextOut(ArtifactWriter &w) : w_(w) { s_.reserve(kChunk + 256); }
  TextOut &operator<<(std::string_view v) {
    s_.append(v.data(), v.size());
    spill();
    return *this;
  }
  TextOut &operator<<(char c) {
    s_.push_back(c);
    spill();
    return *this;
  }
  void real(double v) {
    char buf[64];
    const auto r = std::to_chars(buf, buf + sizeof(buf), v);
    s_.append(buf, r.ptr);
    spill();
  }
  void integer(long long v) {
    char buf[32];
    const auto r = std::to_chars(buf, buf + sizeof(buf), v);
    s_.append(buf, r.ptr);
    spill();
  }
  void flush() {
    if (!s_.empty())
      w_.write(s_);
    s_.clear();
  }

private:
  static constexpr std::size_t kChunk = 64 * 1024;
  void spill() {
    if (s_.size() >= kChunk)
      flush();
  }
  ArtifactWriter &w_;
  std::string s_;
};

std::string xml_escape(const std::string &s) {
  std::string out;
  out.reserve(s.size());
  for (char c : s) {
    switch (c) {
    case '&':
      out += "&amp;";
      break;
    case '<':
      out += "&lt;";
      break;
    case '>':
      out += "&gt;";
      break;
    case '"':
      out += "&quot;";
      break;
    case '\'':
      out += "&apos;";
      break;
    default:
      out.push_back(c);
    }
  }
  return out;
}

void put_u16(unsigned char *p, std::uint16_t v) {
  p[0] = static_cast<unsigned char>(v & 0xffu);
  p[1] = static_cast<unsigned char>(v >> 8);
}
void put_u32(unsigned char *p, std::uint32_t v) {
  for (int i = 0; i < 4; ++i)
    p[i] = static_cast<unsigned char>((v >> (8 * i)) & 0xffu);
}
void put_f32(unsigned char *p, float f) {
  std::uint32_t u;
  std::memcpy(&u, &f, 4);
  put_u32(p, u);
}

bool finite_f32(double x) {
  return std::isfinite(x) &&
         std::abs(x) <= static_cast<double>(std::numeric_limits<float>::max());
}

// Surface positions as float32 millimetres (shared by the STL and AICM
// writers).
std::vector<float> positions_mm_f32(const SurfaceMesh &s,
                                   bool refuse_invalid = true) {
  std::vector<float> out(static_cast<std::size_t>(s.vertex_count()) * 3);
  for (int v = 0; v < s.vertex_count(); ++v)
    for (int k = 0; k < 3; ++k) {
      const double mm = units::m_to_mm(s.points_m(k, v));
      if (!finite_f32(mm) && refuse_invalid)
        fail("invalid_input", "surface vertex " + std::to_string(v) +
                                  " is not finite in float32 millimetres");
      out[static_cast<std::size_t>(3 * v + k)] =
          finite_f32(mm) ? static_cast<float>(mm)
                         : std::numeric_limits<float>::quiet_NaN();
    }
  return out;
}

bool valid_f32_triangle(const SurfaceMesh &s, const std::vector<float> &f, int t) {
  const auto corner = [&](int v) {
    const std::size_t i = static_cast<std::size_t>(v) * 3;
    return Eigen::Vector3d(f[i], f[i + 1], f[i + 2]);
  };
  const Eigen::Vector3d a = corner(s.triangles(0, t));
  const double n2 = (corner(s.triangles(1, t)) - a)
                        .cross(corner(s.triangles(2, t)) - a)
                        .squaredNorm();
  return std::isfinite(n2) && n2 > 0;
}

// Every triangle must keep a nonzero area in the float32 millimetre positions
// a writer serializes (`f` from positions_mm_f32). Checked before any file is
// created; coordinates are never rebased, so a collapse is a named refusal.
void check_f32_triangles(const SurfaceMesh &s, const std::vector<float> &f,
                         const char *code, const char *format) {
  for (int t = 0; t < s.triangle_count(); ++t) {
    if (!valid_f32_triangle(s, f, t))
      fail(code, "triangle " + std::to_string(t) +
                     " has coincident or collinear corners in float32 "
                     "millimetres; the " +
                     format + " artifact would collapse it");
  }
}

// ── Marching-tet extraction (shared by surface-only and clipped-volume)
// ───────
struct Build {
  // Vertices: positions (m), sources and canonical order keys (node n ->
  // (n,n); edge u<v -> (u,v)), independent of creation order.
  std::vector<double> pts;
  std::vector<SurfaceVertexSource> src;
  std::vector<std::uint64_t> order;
  // Surface triangles.
  std::vector<int> tris, tri_elem;
  std::vector<TriangleOrigin> tri_origin;
  // Volume tets (positively oriented) and their parents.
  std::vector<int> tets, tet_parent;
  int dropped_degenerate_tets = 0;
};

class Extractor {
public:
  Extractor(const TetMesh &m, const Eigen::VectorXd &d, double t,
            ExtractionStats &stats, Build &b)
      : m_(m), d_(d), t_(t), st_(stats), b_(b),
        node_vertex_(static_cast<std::size_t>(m.node_count()), -1) {}

  bool solid(int n) const { return d_[n] >= t_; }

  int node_vertex(int n) {
    int &id = node_vertex_[static_cast<std::size_t>(n)];
    if (id < 0)
      id = add(m_.nodes_m.col(n), {n, -1, 0.0}, pair_key(n, n));
    return id;
  }
  // Crossing on the mesh edge from solid node `in` to void node `out`.
  int edge_vertex(int in, int out) {
    if (d_[in] == t_)
      return node_vertex(in);
    const std::uint64_t key = pair_key(std::min(in, out), std::max(in, out));
    auto it = edge_vertex_.find(key);
    if (it != edge_vertex_.end())
      return it->second;
    const double w = (d_[in] - t_) / (d_[in] - d_[out]);
    const Eigen::Vector3d xi = m_.nodes_m.col(in);
    const Eigen::Vector3d p =
        xi + w * (Eigen::Vector3d(m_.nodes_m.col(out)) - xi);
    const int id = add(p, {in, out, w}, key);
    edge_vertex_.emplace(key, id);
    return id;
  }

  Eigen::Vector3d point(int id) const {
    const std::size_t i = static_cast<std::size_t>(id) * 3;
    return {b_.pts[i], b_.pts[i + 1], b_.pts[i + 2]};
  }

  // Convex polygon (<= 4 ids, cyclic). Consecutive duplicates (threshold
  // welds) are merged; a remaining point/segment is a collapse. Quads split on
  // the diagonal through their lowest-keyed vertex. `down` (the density
  // gradient) orients isosurface triangles; null keeps the given winding.
  void polygon(const int *ids, int count, int element, TriangleOrigin origin,
               const Eigen::Vector3d *down) {
    int q[4];
    int k = 0;
    for (int i = 0; i < count; ++i)
      if (k == 0 || q[k - 1] != ids[i])
        q[k++] = ids[i];
    while (k > 1 && q[k - 1] == q[0])
      --k;
    if (k < 3) {
      ++st_.collapsed_polygons;
      return;
    }
    for (int i = 0; i < k; ++i)
      for (int j = i + 1; j < k; ++j)
        if (q[i] == q[j])
          throw std::logic_error(
              "topology surface: non-consecutive repeated polygon vertex");
    if (k == 3) {
      triangle(q[0], q[1], q[2], element, origin, down);
      return;
    }
    int lowest = 0;
    for (int i = 1; i < 4; ++i)
      if (b_.order[q[i]] < b_.order[q[lowest]])
        lowest = i;
    if (lowest % 2 == 0) {
      triangle(q[0], q[1], q[2], element, origin, down);
      triangle(q[0], q[2], q[3], element, origin, down);
    } else {
      triangle(q[0], q[1], q[3], element, origin, down);
      triangle(q[1], q[2], q[3], element, origin, down);
    }
  }

  void tet(int a, int b, int c, int d, int parent) {
    if (a == b || a == c || a == d || b == c || b == d || c == d)
      return; // collapsed at threshold welds: zero volume by construction
    const Eigen::Vector3d pa = point(a);
    double det = (point(b) - pa).dot((point(c) - pa).cross(point(d) - pa));
    if (det < 0) {
      std::swap(c, d);
      det = -det;
    }
    if (!(det > 0)) {
      ++b_.dropped_degenerate_tets;
      return;
    }
    b_.tets.insert(b_.tets.end(), {a, b, c, d});
    b_.tet_parent.push_back(parent);
  }

  // Prism v[0..2] / v[3..5] with v[i] <-> v[i+3]; every quad split through its
  // lowest-keyed vertex (Dompierre et al. rule), so neighbours conform.
  void prism(const int v[6], int parent) {
    static constexpr int kRot[6][6] = {{0, 1, 2, 3, 4, 5}, {1, 2, 0, 4, 5, 3},
                                       {2, 0, 1, 5, 3, 4}, {3, 5, 4, 0, 2, 1},
                                       {4, 3, 5, 1, 0, 2}, {5, 4, 3, 2, 1, 0}};
    int m = 0;
    for (int i = 1; i < 6; ++i)
      if (b_.order[v[i]] < b_.order[v[m]])
        m = i;
    int r[6];
    for (int i = 0; i < 6; ++i)
      r[i] = v[kRot[m][i]];
    const auto key = [&](int i) { return b_.order[r[i]]; };
    if (std::min(key(1), key(5)) < std::min(key(2), key(4))) {
      tet(r[0], r[1], r[2], r[5], parent);
      tet(r[0], r[1], r[5], r[4], parent);
      tet(r[0], r[4], r[5], r[3], parent);
    } else {
      tet(r[0], r[1], r[2], r[4], parent);
      tet(r[0], r[4], r[2], r[5], parent);
      tet(r[0], r[4], r[5], r[3], parent);
    }
  }

  void run(bool volume) {
    const TetMesh &m = m_;
    for (int e = 0; e < m.element_count(); ++e) {
      int in[4], out[4], ni = 0, no = 0;
      for (int a = 0; a < 4; ++a) {
        const int n = m.tets(a, e);
        if (solid(n))
          in[ni++] = n;
        else
          out[no++] = n;
      }
      if (ni == 0)
        continue;
      if (ni == 4) {
        if (volume) {
          const int v[4] = {
              node_vertex(m.tets(0, e)), node_vertex(m.tets(1, e)),
              node_vertex(m.tets(2, e)), node_vertex(m.tets(3, e))};
          tet(v[0], v[1], v[2], v[3], e);
        }
        continue;
      }
      Eigen::Vector3d grad = Eigen::Vector3d::Zero();
      const auto g = m.element_gradients(e);
      for (int a = 0; a < 4; ++a)
        grad += d_[m.tets(a, e)] * g.col(a);
      int ids[4];
      if (ni == 1) {
        ids[0] = edge_vertex(in[0], out[0]);
        ids[1] = edge_vertex(in[0], out[1]);
        ids[2] = edge_vertex(in[0], out[2]);
        polygon(ids, 3, e, TriangleOrigin::Isosurface, &grad);
        if (volume)
          tet(node_vertex(in[0]), ids[0], ids[1], ids[2], e);
      } else if (ni == 3) {
        ids[0] = edge_vertex(in[0], out[0]);
        ids[1] = edge_vertex(in[1], out[0]);
        ids[2] = edge_vertex(in[2], out[0]);
        polygon(ids, 3, e, TriangleOrigin::Isosurface, &grad);
        if (volume) {
          const int v[6] = {node_vertex(in[0]),
                            node_vertex(in[1]),
                            node_vertex(in[2]),
                            ids[0],
                            ids[1],
                            ids[2]};
          prism(v, e);
        }
      } else {
        ids[0] = edge_vertex(in[0], out[0]);
        ids[1] = edge_vertex(in[0], out[1]);
        ids[2] = edge_vertex(in[1], out[1]);
        ids[3] = edge_vertex(in[1], out[0]);
        polygon(ids, 4, e, TriangleOrigin::Isosurface, &grad);
        if (volume) {
          const int v[6] = {node_vertex(in[0]), ids[0], ids[1],
                            node_vertex(in[1]), ids[3], ids[2]};
          prism(v, e);
        }
      }
    }
    // Caps: Sutherland-Hodgman clip of each exterior triangle (outward order).
    for (int f = 0; f < m.surface_count(); ++f) {
      int ids[4], cnt = 0;
      for (int i = 0; i < 3; ++i) {
        const int a = m.surface_nodes(i, f),
                  b = m.surface_nodes((i + 1) % 3, f);
        const bool sa = solid(a), sb = solid(b);
        if (sa)
          ids[cnt++] = node_vertex(a);
        if (sa != sb)
          ids[cnt++] = sa ? edge_vertex(a, b) : edge_vertex(b, a);
      }
      if (cnt > 0)
        polygon(ids, cnt, m.surface_element[f], TriangleOrigin::DomainBoundary,
                nullptr);
    }
  }

private:
  int add(const Eigen::Vector3d &p, SurfaceVertexSource s, std::uint64_t key) {
    b_.pts.insert(b_.pts.end(), {p.x(), p.y(), p.z()});
    b_.src.push_back(s);
    b_.order.push_back(key);
    return static_cast<int>(b_.src.size()) - 1;
  }
  void triangle(int a, int b, int c, int element, TriangleOrigin origin,
                const Eigen::Vector3d *down) {
    if (down) {
      const Eigen::Vector3d pa = point(a);
      const Eigen::Vector3d n = (point(b) - pa).cross(point(c) - pa);
      if (n.dot(*down) > 0)
        std::swap(b, c);
    }
    b_.tris.insert(b_.tris.end(), {a, b, c});
    b_.tri_elem.push_back(element);
    b_.tri_origin.push_back(origin);
  }

  const TetMesh &m_;
  const Eigen::VectorXd &d_;
  const double t_;
  ExtractionStats &st_;
  Build &b_;
  std::vector<int> node_vertex_;
  std::unordered_map<std::uint64_t, int> edge_vertex_;
};

// Remove coincident triangle pairs of opposite orientation (zero-volume
// sheets). Returns the keep mask over b.tris.
std::vector<std::uint8_t> cancel_sheets(const Build &b, ExtractionStats &st) {
  const int T = static_cast<int>(b.tri_elem.size());
  struct Rec {
    TriKey key;
    int tri;
  };
  std::vector<Rec> recs(static_cast<std::size_t>(T));
  for (int t = 0; t < T; ++t)
    recs[t] = {tri_key(b.tris[3 * t], b.tris[3 * t + 1], b.tris[3 * t + 2]), t};
  std::sort(recs.begin(), recs.end(), [](const Rec &x, const Rec &y) {
    return x.key.sorted != y.key.sorted ? x.key.sorted < y.key.sorted
                                        : x.tri < y.tri;
  });
  std::vector<std::uint8_t> keep(static_cast<std::size_t>(T), 1);
  std::vector<int> pos, neg;
  for (std::size_t i = 0; i < recs.size();) {
    std::size_t j = i;
    pos.clear();
    neg.clear();
    while (j < recs.size() && recs[j].key.sorted == recs[i].key.sorted) {
      (recs[j].key.parity > 0 ? pos : neg).push_back(recs[j].tri);
      ++j;
    }
    const std::size_t pairs = std::min(pos.size(), neg.size());
    for (std::size_t k = 0; k < pairs; ++k) {
      keep[pos[k]] = 0;
      keep[neg[k]] = 0;
    }
    st.cancelled_sheet_triangles += static_cast<int>(2 * pairs);
    i = j;
  }
  return keep;
}

void snap_and_classify(const TetMesh &m, Eigen::VectorXd &d,
                       const SurfaceOptions &o, ExtractionStats &st) {
  st.threshold = o.threshold;
  st.threshold_snap = o.threshold_snap;
  for (int n = 0; n < m.node_count(); ++n) {
    if (!m.node_referenced[n])
      continue;
    double &v = d[n];
    if (v != o.threshold && std::abs(v - o.threshold) <= o.threshold_snap) {
      v = o.threshold;
      ++st.snapped_nodes;
    }
    if (v == o.threshold)
      ++st.threshold_nodes;
    if (v >= o.threshold)
      ++st.solid_nodes;
  }
}

void admit_extraction(const TetMesh &m, bool volume) {
  const double N = m.node_count(), E = m.element_count(), F = m.surface_count();
  const double tris = 2 * E + 2 * F;
  if (3 * tris > static_cast<double>(std::numeric_limits<int>::max()))
    fail("surface_too_large", "mesh too large for 32-bit surface indexing");
  double bytes = 32 * N + 3 * tris * 48 /*vertices incl. hash*/ +
                 tris * (12 + 4 + 1 + 3 * 24 /*edge records*/ +
                         3 * 16 /*vertex links*/ + 40 /*cancel records*/);
  if (volume)
    bytes += 3 * E * (16 + 4 + 4 * 24 /*face records*/) + N * 24;
  admit_or_fail(bytes, volume ? "topology clipped-domain extraction"
                              : "topology surface extraction");
}

// Compact surviving surface triangles + their vertices into a SurfaceMesh.
// old_to_new receives the Build vertex -> surface vertex map (-1 unused).
SurfaceMesh compact_surface(const Build &b,
                            const std::vector<std::uint8_t> &keep,
                            std::vector<int> &old_to_new) {
  const int T0 = static_cast<int>(b.tri_elem.size());
  const int V0 = static_cast<int>(b.src.size());
  old_to_new.assign(static_cast<std::size_t>(V0), -1);
  int T = 0;
  for (int t = 0; t < T0; ++t)
    if (keep[t]) {
      ++T;
      for (int k = 0; k < 3; ++k)
        old_to_new[b.tris[3 * t + k]] = 0;
    }
  int V = 0;
  for (int v = 0; v < V0; ++v)
    if (old_to_new[v] == 0)
      old_to_new[v] = V++;
  SurfaceMesh s;
  s.points_m.resize(3, V);
  s.vertex_source.resize(static_cast<std::size_t>(V));
  for (int v = 0; v < V0; ++v)
    if (old_to_new[v] >= 0) {
      const int nv = old_to_new[v];
      for (int k = 0; k < 3; ++k)
        s.points_m(k, nv) = b.pts[3 * static_cast<std::size_t>(v) + k];
      s.vertex_source[nv] = b.src[v];
    }
  s.triangles.resize(3, T);
  s.triangle_element.resize(T);
  s.triangle_origin.resize(static_cast<std::size_t>(T));
  int nt = 0;
  for (int t = 0; t < T0; ++t)
    if (keep[t]) {
      for (int k = 0; k < 3; ++k)
        s.triangles(k, nt) = old_to_new[b.tris[3 * t + k]];
      s.triangle_element[nt] = b.tri_elem[t];
      s.triangle_origin[nt] = b.tri_origin[t];
      ++nt;
    }
  return s;
}

// Faces of the clipped tets; checks conformity and the boundary == surface
// identity in Build vertex numbering, then compacts the volume.
ClippedDomain finish_volume(const TetMesh &m, const Build &b,
                            const std::vector<std::uint8_t> &keep,
                            const std::vector<int> &surface_old_to_new,
                            const SurfaceResult &surface,
                            const SurfaceOptions &o) {
  ClippedDomain c;
  c.threshold = o.threshold;
  c.threshold_snap = o.threshold_snap;
  c.domain_volume_m3 = m.total_volume_m3;
  c.dropped_degenerate_tets = b.dropped_degenerate_tets;
  const int K = static_cast<int>(b.tet_parent.size());
  const auto P = [&](int id) {
    const std::size_t i = static_cast<std::size_t>(id) * 3;
    return Eigen::Vector3d(b.pts[i], b.pts[i + 1], b.pts[i + 2]);
  };

  c.min_tet_volume_m3 = K ? std::numeric_limits<double>::infinity() : 0.0;
  c.min_tet_shape = K ? std::numeric_limits<double>::infinity() : 0.0;
  for (int k = 0; k < K; ++k) {
    const int *v = &b.tets[4 * static_cast<std::size_t>(k)];
    const Eigen::Vector3d p0 = P(v[0]);
    const double det = (P(v[1]) - p0).dot((P(v[2]) - p0).cross(P(v[3]) - p0));
    double lmax = 0;
    for (int i = 0; i < 4; ++i)
      for (int j = i + 1; j < 4; ++j)
        lmax = std::max(lmax, (P(v[i]) - P(v[j])).norm());
    c.volume_m3 += det / 6.0;
    c.min_tet_volume_m3 = std::min(c.min_tet_volume_m3, det / 6.0);
    c.min_tet_shape = std::min(c.min_tet_shape, det / (lmax * lmax * lmax));
  }

  // Oriented faces: tets are positive, so kTetFaces gives outward triangles.
  struct FaceRec {
    TriKey key;
    int tet;
  };
  std::vector<FaceRec> faces;
  faces.reserve(4 * static_cast<std::size_t>(K));
  for (int k = 0; k < K; ++k) {
    const int *v = &b.tets[4 * static_cast<std::size_t>(k)];
    for (const auto &f : kTetFaces)
      faces.push_back({tri_key(v[f[0]], v[f[1]], v[f[2]]), k});
  }
  const auto by_key = [](const FaceRec &x, const FaceRec &y) {
    return x.key.sorted != y.key.sorted ? x.key.sorted < y.key.sorted
                                        : x.key.parity < y.key.parity;
  };
  std::sort(faces.begin(), faces.end(), by_key);
  UnionFind uf(K);
  std::vector<TriKey> boundary;
  for (std::size_t i = 0; i < faces.size();) {
    std::size_t j = i;
    while (j < faces.size() && faces[j].key.sorted == faces[i].key.sorted) {
      uf.unite(faces[i].tet, faces[j].tet);
      ++j;
    }
    const std::size_t uses = j - i;
    if (uses == 1)
      boundary.push_back(faces[i].key);
    else if (uses > 2 || faces[i].key.parity == faces[i + 1].key.parity)
      ++c.nonconforming_faces;
    i = j;
  }
  for (int k = 0; k < K; ++k)
    if (uf.find(k) == k)
      ++c.face_components;
  c.boundary_faces = static_cast<int>(boundary.size());

  std::vector<TriKey> surf;
  for (int t = 0; t < static_cast<int>(b.tri_elem.size()); ++t)
    if (keep[t])
      surf.push_back(
          tri_key(b.tris[3 * t], b.tris[3 * t + 1], b.tris[3 * t + 2]));
  const auto less = [](const TriKey &x, const TriKey &y) {
    return x.sorted != y.sorted ? x.sorted < y.sorted : x.parity < y.parity;
  };
  std::sort(boundary.begin(), boundary.end(), less);
  std::sort(surf.begin(), surf.end(), less);
  std::size_t i = 0, j = 0;
  while (i < boundary.size() || j < surf.size()) {
    if (j == surf.size() ||
        (i < boundary.size() && less(boundary[i], surf[j]))) {
      ++c.unmatched_boundary_faces;
      ++i;
    } else if (i == boundary.size() || less(surf[j], boundary[i])) {
      ++c.unmatched_surface_triangles;
      ++j;
    } else {
      ++i;
      ++j;
    }
  }
  const double vol_tol =
      1e-9 * std::max(c.domain_volume_m3, std::abs(c.volume_m3));
  c.boundary_matches_surface =
      c.unmatched_boundary_faces == 0 && c.unmatched_surface_triangles == 0 &&
      c.nonconforming_faces == 0 && c.dropped_degenerate_tets == 0 &&
      std::abs(c.volume_m3 - surface.evidence.signed_volume_m3) <= vol_tol;

  // Compact the volume vertices (ascending Build ids).
  const int V0 = static_cast<int>(b.src.size());
  std::vector<int> vol_new(static_cast<std::size_t>(V0), -1);
  for (int id : b.tets)
    vol_new[id] = 0;
  int V = 0;
  for (int v = 0; v < V0; ++v)
    if (vol_new[v] == 0)
      vol_new[v] = V++;
  c.points_mm.resize(3 * static_cast<std::size_t>(V));
  c.vertex_source.resize(static_cast<std::size_t>(V));
  for (int v = 0; v < V0; ++v)
    if (vol_new[v] >= 0) {
      for (int k = 0; k < 3; ++k)
        c.points_mm[3 * static_cast<std::size_t>(vol_new[v]) + k] =
            units::m_to_mm(b.pts[3 * static_cast<std::size_t>(v) + k]);
      c.vertex_source[vol_new[v]] = b.src[v];
    }
  c.tets.resize(b.tets.size());
  for (std::size_t k = 0; k < b.tets.size(); ++k)
    c.tets[k] = vol_new[b.tets[k]];
  c.tet_parent = b.tet_parent;
  c.surface_vertex.assign(static_cast<std::size_t>(surface.mesh.vertex_count()),
                          -1);
  for (int v = 0; v < V0; ++v)
    if (surface_old_to_new[v] >= 0)
      c.surface_vertex[surface_old_to_new[v]] = vol_new[v];
  return c;
}

ClippedResult extract_impl(const TetMesh &m, Eigen::VectorXd d,
                           const SurfaceOptions &o, bool volume) {
  admit_extraction(m, volume);
  ClippedResult r;
  snap_and_classify(m, d, o, r.surface.stats);
  Build b;
  Extractor x(m, d, o.threshold, r.surface.stats, b);
  x.run(volume);
  const std::vector<std::uint8_t> keep = cancel_sheets(b, r.surface.stats);
  std::vector<int> old_to_new;
  r.surface.mesh = compact_surface(b, keep, old_to_new);
  r.surface.evidence = validate_surface(r.surface.mesh);
  if (volume)
    r.volume = finish_volume(m, b, keep, old_to_new, r.surface, o);
  return r;
}

// Solid angle of triangle (a,b,c) seen from p (Van Oosterom & Strackee).
double solid_angle(const Eigen::Vector3d &p, const Eigen::Vector3d &a,
                   const Eigen::Vector3d &b, const Eigen::Vector3d &c) {
  const Eigen::Vector3d A = a - p, B = b - p, C = c - p;
  const double la = A.norm(), lb = B.norm(), lc = C.norm();
  const double numer = A.dot(B.cross(C));
  const double denom =
      la * lb * lc + A.dot(B) * lc + A.dot(C) * lb + B.dot(C) * la;
  return 2.0 * std::atan2(numer, denom);
}

// ── Shell contact and nesting (validate_surface)
// ──────────────────────────────

Eigen::Vector3d tri_corner(const SurfaceMesh &s, int t, int k) {
  return s.points_m.col(s.triangles(k, t));
}

// Bounding-volume hierarchy over triangle boxes: median split on the longest
// centroid extent, at most kLeaf triangles per leaf. Boxes are padded by the
// rounding bound of the coordinate scale (64 eps) so overlap and slab rounding
// never drop a candidate; the triangle tests below decide.
class TriangleBvh {
public:
  explicit TriangleBvh(const SurfaceMesh &s) {
    const int T = s.triangle_count();
    lo_.resize(static_cast<std::size_t>(T));
    hi_.resize(static_cast<std::size_t>(T));
    double scale = 0;
    for (int t = 0; t < T; ++t) {
      Eigen::Vector3d lo = tri_corner(s, t, 0), hi = lo;
      for (int k = 1; k < 3; ++k) {
        lo = lo.cwiseMin(tri_corner(s, t, k));
        hi = hi.cwiseMax(tri_corner(s, t, k));
      }
      lo_[t] = lo;
      hi_[t] = hi;
      scale =
          std::max({scale, lo.cwiseAbs().maxCoeff(), hi.cwiseAbs().maxCoeff()});
    }
    const double pad = 64 * std::numeric_limits<double>::epsilon() * scale;
    for (int t = 0; t < T; ++t) {
      lo_[t].array() -= pad;
      hi_[t].array() += pad;
    }
    order_.resize(static_cast<std::size_t>(T));
    std::iota(order_.begin(), order_.end(), 0);
    nodes_.reserve(2 * static_cast<std::size_t>(T / kLeaf + 1));
    if (T > 0)
      build(0, T);
  }
  const Eigen::Vector3d &lo(int t) const { return lo_[t]; }
  const Eigen::Vector3d &hi(int t) const { return hi_[t]; }

  // Every triangle whose padded box meets the closed box [lo, hi].
  template <class Visit>
  void overlapping(const Eigen::Vector3d &lo, const Eigen::Vector3d &hi,
                   Visit &&visit) const {
    const auto meets = [&](const Eigen::Vector3d &a, const Eigen::Vector3d &b) {
      return (a.array() <= hi.array()).all() && (b.array() >= lo.array()).all();
    };
    traverse(meets, [&](int t) {
      if (meets(lo_[t], hi_[t]))
        visit(t);
    });
  }
  // Every triangle of every leaf whose box the ray p + t d (t >= 0) meets; d
  // has no zero component.
  template <class Visit>
  void along_ray(const Eigen::Vector3d &p, const Eigen::Vector3d &d,
                 Visit &&visit) const {
    const Eigen::Vector3d inv = d.cwiseInverse();
    const auto meets = [&](const Eigen::Vector3d &a, const Eigen::Vector3d &b) {
      double t0 = 0, t1 = std::numeric_limits<double>::infinity();
      for (int k = 0; k < 3; ++k) {
        double u = (a[k] - p[k]) * inv[k], w = (b[k] - p[k]) * inv[k];
        if (u > w)
          std::swap(u, w);
        t0 = std::max(t0, u);
        t1 = std::min(t1, w);
      }
      return t0 <= t1;
    };
    traverse(meets, visit);
  }

private:
  static constexpr int kLeaf = 4;
  struct Node {
    Eigen::Vector3d lo, hi;
    int first = 0, count = 0; // leaf: order_[first, first + count)
    int right = -1;           // inner: children self + 1 and right
  };

  void build(int begin, int end) {
    const int id = static_cast<int>(nodes_.size());
    nodes_.emplace_back();
    Eigen::Vector3d lo = lo_[order_[begin]], hi = hi_[order_[begin]];
    Eigen::Vector3d clo = lo + hi, chi = clo;
    for (int i = begin + 1; i < end; ++i) {
      const int t = order_[i];
      lo = lo.cwiseMin(lo_[t]);
      hi = hi.cwiseMax(hi_[t]);
      const Eigen::Vector3d c = lo_[t] + hi_[t];
      clo = clo.cwiseMin(c);
      chi = chi.cwiseMax(c);
    }
    nodes_[id].lo = lo;
    nodes_[id].hi = hi;
    if (end - begin <= kLeaf) {
      nodes_[id].first = begin;
      nodes_[id].count = end - begin;
      return;
    }
    int axis = 0;
    (chi - clo).maxCoeff(&axis);
    const int mid = begin + (end - begin) / 2;
    std::nth_element(order_.begin() + begin, order_.begin() + mid,
                     order_.begin() + end, [&](int a, int b) {
                       return lo_[a][axis] + hi_[a][axis] <
                              lo_[b][axis] + hi_[b][axis];
                     });
    build(begin, mid);
    const int right = static_cast<int>(nodes_.size());
    build(mid, end);
    nodes_[id].right = right;
  }

  // Depth-first; median splits keep the depth below 32 for int counts.
  template <class Enter, class Visit>
  void traverse(Enter &&enter, Visit &&visit) const {
    if (nodes_.empty())
      return;
    std::array<int, 64> stack;
    int top = 0;
    stack[top++] = 0;
    while (top > 0) {
      const int id = stack[--top];
      const Node &n = nodes_[id];
      if (!enter(n.lo, n.hi))
        continue;
      if (n.count > 0) {
        for (int i = n.first; i < n.first + n.count; ++i)
          visit(order_[i]);
        continue;
      }
      stack[top++] = n.right;
      stack[top++] = id + 1;
    }
  }

  std::vector<Eigen::Vector3d> lo_, hi_; // padded triangle boxes
  std::vector<int> order_;
  std::vector<Node> nodes_;
};

// Closed triangles P and Q share a point: no separating axis among the two
// normals, the nine edge-edge cross products and the six in-plane edge normals
// (complete for coplanar pairs). A gap within the rounding bound of the
// projections counts as contact.
bool triangles_touch(const SurfaceMesh &s, int tp, int tq) {
  constexpr double eps = std::numeric_limits<double>::epsilon();
  const Eigen::Vector3d o = tri_corner(s, tp, 0);
  Eigen::Vector3d p[3], q[3];
  double scale = 0;
  for (int k = 0; k < 3; ++k) {
    p[k] = tri_corner(s, tp, k) - o;
    q[k] = tri_corner(s, tq, k) - o;
    scale = std::max(
        {scale, p[k].cwiseAbs().maxCoeff(), q[k].cwiseAbs().maxCoeff()});
  }
  const Eigen::Vector3d ep[3] = {p[1] - p[0], p[2] - p[1], p[0] - p[2]};
  const Eigen::Vector3d eq[3] = {q[1] - q[0], q[2] - q[1], q[0] - q[2]};
  const Eigen::Vector3d np = ep[0].cross(ep[1]), nq = eq[0].cross(eq[1]);
  const auto separates = [&](const Eigen::Vector3d &axis) {
    const double len = axis.norm();
    if (!(len > 0))
      return false;
    double pmin = std::numeric_limits<double>::infinity(), pmax = -pmin;
    double qmin = pmin, qmax = -pmin;
    for (int k = 0; k < 3; ++k) {
      const double a = p[k].dot(axis), b = q[k].dot(axis);
      pmin = std::min(pmin, a);
      pmax = std::max(pmax, a);
      qmin = std::min(qmin, b);
      qmax = std::max(qmax, b);
    }
    const double tol = 64 * eps * scale * len;
    return pmax + tol < qmin || qmax + tol < pmin;
  };
  if (separates(np) || separates(nq))
    return false;
  for (int i = 0; i < 3; ++i) {
    for (int j = 0; j < 3; ++j)
      if (separates(ep[i].cross(eq[j])))
        return false;
    if (separates(np.cross(ep[i])) || separates(nq.cross(eq[i])))
      return false;
  }
  return true;
}

// Moller triangle-plane intervals for non-coplanar pairs; convex polygon
// clipping for coplanar pairs. Only the actual shared simplex is exempt.
bool triangles_cross_beyond_shared(const SurfaceMesh &s, int a, int b) {
  if (!triangles_touch(s, a, b))
    return false;
  Eigen::Vector3d shared[3];
  int common = 0;
  for (int i = 0; i < 3; ++i)
    for (int j = 0; j < 3; ++j)
      if (s.triangles(i, a) == s.triangles(j, b))
        shared[common++] = tri_corner(s, a, i);
  if (common == 0 || common == 3)
    return true;
  const Eigen::Vector3d origin = shared[0];
  Eigen::Vector3d p[3], q[3];
  double scale = 0;
  for (int k = 0; k < 3; ++k) {
    p[k] = tri_corner(s, a, k) - origin;
    q[k] = tri_corner(s, b, k) - origin;
    scale = std::max({scale, p[k].norm(), q[k].norm()});
  }
  const double tol = 256 * std::numeric_limits<double>::epsilon() * scale;
  const Eigen::Vector3d np = (p[1] - p[0]).cross(p[2] - p[0]).normalized();
  const Eigen::Vector3d nq = (q[1] - q[0]).cross(q[2] - q[0]).normalized();
  const Eigen::Vector3d line = np.cross(nq);
  if (line.norm() > 256 * std::numeric_limits<double>::epsilon()) {
    const Eigen::Vector3d axis = line.normalized();
    auto interval = [&](const Eigen::Vector3d (&v)[3], int triangle,
                        int other, const Eigen::Vector3d &normal) {
      double lo = std::numeric_limits<double>::infinity(), hi = -lo;
      auto add = [&](const Eigen::Vector3d &x) {
        const double t = x.dot(axis);
        lo = std::min(lo, t);
        hi = std::max(hi, t);
      };
      // Shared vertices lie on both planes by incidence. Re-evaluating their
      // signed distance can invent a sign and a false edge-plane crossing.
      auto distance = [&](int k) {
        for (int j = 0; j < 3; ++j)
          if (s.triangles(k, triangle) == s.triangles(j, other))
            return 0.0;
        return normal.dot(v[k]);
      };
      for (int k = 0; k < 3; ++k) {
        const int j = (k + 1) % 3;
        const double u = distance(k), w = distance(j);
        if (u == 0)
          add(v[k]);
        if ((u < 0 && w > 0) || (u > 0 && w < 0))
          add(v[k] + (u / (u - w)) * (v[j] - v[k]));
      }
      return std::pair<double, double>{lo, hi};
    };
    const auto ip = interval(p, a, b, nq), iq = interval(q, b, a, np);
    const double lo = std::max(ip.first, iq.first);
    const double hi = std::min(ip.second, iq.second);
    if (lo > hi + tol)
      return false;
    double allowed_lo = (shared[0] - origin).dot(axis), allowed_hi = allowed_lo;
    if (common == 2) {
      const double t = (shared[1] - origin).dot(axis);
      allowed_lo = std::min(allowed_lo, t);
      allowed_hi = std::max(allowed_hi, t);
    }
    return lo < allowed_lo - tol || hi > allowed_hi + tol;
  }
  int drop = 0;
  np.cwiseAbs().maxCoeff(&drop);
  const int x = (drop + 1) % 3, y = (drop + 2) % 3;
  auto project = [&](const Eigen::Vector3d &v) {
    return Eigen::Vector2d(v[x], v[y]);
  };
  auto cross = [](const Eigen::Vector2d &u, const Eigen::Vector2d &v) {
    return u.x() * v.y() - u.y() * v.x();
  };
  std::array<Eigen::Vector2d, 12> polygon, next;
  for (int k = 0; k < 3; ++k)
    polygon[k] = project(p[k]);
  int count = 3;
  const double sign = cross(project(q[1] - q[0]), project(q[2] - q[0])) >= 0
                          ? 1.0 : -1.0;
  for (int edge = 0; edge < 3 && count > 0; ++edge) {
    const Eigen::Vector2d start = project(q[edge]);
    const Eigen::Vector2d delta = project(q[(edge + 1) % 3]) - start;
    int n = 0;
    for (int i = 0; i < count; ++i) {
      const Eigen::Vector2d u = polygon[i], v = polygon[(i + 1) % count];
      const double du = sign * cross(delta, u - start);
      const double dv = sign * cross(delta, v - start);
      if (du >= 0)
        next[n++] = u;
      if ((du < 0 && dv > 0) || (du > 0 && dv < 0))
        next[n++] = u + (du / (du - dv)) * (v - u);
    }
    for (int i = 0; i < n; ++i)
      polygon[i] = next[i];
    count = n;
  }
  const Eigen::Vector2d contact = project(shared[0] - origin);
  const Eigen::Vector2d edge =
      common == 2 ? project(shared[1] - shared[0]) : Eigen::Vector2d::Zero();
  for (int i = 0; i < count; ++i) {
    Eigen::Vector2d delta = polygon[i] - contact;
    if (common == 2 && edge.squaredNorm() > 0)
      delta -= std::clamp(delta.dot(edge) / edge.squaredNorm(), 0.0, 1.0) * edge;
    if (delta.norm() > tol)
      return true;
  }
  return false;
}

// Per shell: shares a vertex with, or has a triangle touching or crossing a
// triangle of, another shell. Bounding-box broadphase, exact pair test.
std::vector<std::uint8_t> touching_shells(const SurfaceMesh &s,
                                          const TriangleBvh &bvh,
                                          const std::vector<int> &shell_of,
                                          int shells) {
  std::vector<std::uint8_t> touching(static_cast<std::size_t>(shells), 0);
  std::vector<int> vertex_shell(static_cast<std::size_t>(s.vertex_count()), -1);
  const int T = s.triangle_count();
  for (int t = 0; t < T; ++t)
    for (int k = 0; k < 3; ++k) {
      int &owner = vertex_shell[s.triangles(k, t)];
      if (owner < 0)
        owner = shell_of[t];
      else if (owner != shell_of[t])
        touching[owner] = touching[shell_of[t]] = 1;
    }
  for (int t = 0; t < T; ++t) {
    const int a = shell_of[t];
    bvh.overlapping(bvh.lo(t), bvh.hi(t), [&](int u) {
      const int b = shell_of[u];
      // A vertex shared across shells already marked both.
      if (u <= t || a == b || (touching[a] && touching[b]))
        return;
      if (triangles_touch(s, t, u))
        touching[a] = touching[b] = 1;
    });
  }
  return touching;
}

// Winding number at p of every shell except `skip`: signed crossings of a ray
// with their non-degenerate triangles (+1 leaving through the outward side).
// A ray within rounding of an edge, vertex or plane is ambiguous and the next
// direction is tried; if all are, the solid-angle sum over the shells whose
// box holds p decides (a closed shell contributes nothing outside its box).
int winding_of_others(const SurfaceMesh &s, const TriangleBvh &bvh,
                      const std::vector<int> &shell_of,
                      const std::vector<std::uint8_t> &degenerate,
                      const std::vector<Eigen::Vector3d> &shell_lo,
                      const std::vector<Eigen::Vector3d> &shell_hi, int skip,
                      const Eigen::Vector3d &p) {
  static constexpr double kDirs[6][3] = {
      {0.5390, 0.6271, 0.5622},   {-0.6614, 0.3317, 0.6726},
      {0.2961, -0.8127, 0.5019},  {-0.4473, -0.5196, -0.7280},
      {0.8341, -0.2410, -0.4962}, {-0.1871, 0.9035, -0.3855}};
  // Static: MSVC (C3493) treats naming a local constexpr double inside a
  // capture-less lambda as an implicit capture; GCC/Clang accept either.
  static constexpr double rel = 1e-10;
  const auto sign = [](double x, double scale) {
    return x > rel * scale ? 1 : x < -rel * scale ? -1 : 0;
  };
  for (const auto &dir : kDirs) {
    const Eigen::Vector3d d(dir[0], dir[1], dir[2]);
    int w = 0;
    bool ambiguous = false;
    bvh.along_ray(p, d, [&](int t) {
      if (ambiguous || shell_of[t] == skip || degenerate[t])
        return;
      const Eigen::Vector3d A = tri_corner(s, t, 0) - p;
      const Eigen::Vector3d B = tri_corner(s, t, 1) - p;
      const Eigen::Vector3d C = tri_corner(s, t, 2) - p;
      const double la = A.norm(), lb = B.norm(), lc = C.norm();
      // d = alpha A + beta B + gamma C: these are alpha, beta, gamma times
      // det(A, B, C); all of one sign means the line crosses the interior.
      const int s0 = sign(d.dot(B.cross(C)), lb * lc);
      const int s1 = sign(d.dot(C.cross(A)), lc * la);
      const int s2 = sign(d.dot(A.cross(B)), la * lb);
      if ((s0 > 0 || s1 > 0 || s2 > 0) && (s0 < 0 || s1 < 0 || s2 < 0))
        return; // the line misses the triangle
      const int det = sign(A.dot(B.cross(C)), la * lb * lc);
      if (s0 == 0 || s1 == 0 || s2 == 0 || det == 0) {
        ambiguous = true;
        return;
      }
      if (s0 == det) // crossing at t > 0; sign(d . n) = s0
        w += s0;
    });
    if (!ambiguous)
      return w;
  }
  double winding = 0;
  for (int t = 0; t < s.triangle_count(); ++t) {
    const int sh = shell_of[t];
    if (sh == skip || (p.array() < shell_lo[sh].array()).any() ||
        (p.array() > shell_hi[sh].array()).any())
      continue;
    winding += solid_angle(p, tri_corner(s, t, 0), tri_corner(s, t, 1),
                           tri_corner(s, t, 2));
  }
  return static_cast<int>(std::llround(winding / (4 * kPi)));
}

void add_issue(SurfaceEvidence &ev, const char *code, int count) {
  if (count > 0)
    ev.issues.emplace_back(code);
}

nlohmann::json evidence_counts(const SurfaceEvidence &e) {
  return {{"open_edges", e.open_edges},
          {"nonmanifold_edges", e.nonmanifold_edges},
          {"orientation_conflicts", e.orientation_conflicts},
          {"nonmanifold_vertices", e.nonmanifold_vertices},
          {"degenerate_triangles", e.degenerate_triangles},
          {"isolated_vertices", e.isolated_vertices}};
}

nlohmann::json finite_or_null(double v) {
  return std::isfinite(v) ? nlohmann::json(v) : nlohmann::json();
}

} // namespace

// ── Public API
// ─────────────────────────────────────────────────────────────────

const char *to_string(GeometryState s) {
  switch (s) {
  case GeometryState::Valid:
    return "valid";
  case GeometryState::Disconnected:
    return "disconnected";
  case GeometryState::Empty:
    return "empty";
  case GeometryState::Unresolved:
    return "unresolved";
  }
  return "unresolved";
}

Eigen::VectorXd nodal_average(const TetMesh &m, const Eigen::VectorXd &values,
                              int components) {
  if (components < 1)
    fail("invalid_input", "nodal_average needs components >= 1");
  const int E = m.element_count(), N = m.node_count();
  if (values.size() != static_cast<Eigen::Index>(E) * components)
    fail("invalid_field",
         "element values have " + std::to_string(values.size()) +
             " entries, expected " +
             std::to_string(static_cast<long long>(E) * components));
  if (!values.allFinite())
    fail("invalid_field", "element values must be finite");
  Eigen::VectorXd sum =
      Eigen::VectorXd::Zero(static_cast<Eigen::Index>(N) * components);
  Eigen::VectorXd weight = Eigen::VectorXd::Zero(N);
  for (int e = 0; e < E; ++e) {
    const double V = m.volume_m3[e];
    for (int a = 0; a < 4; ++a) {
      const int n = m.tets(a, e);
      weight[n] += V;
      for (int k = 0; k < components; ++k)
        sum[static_cast<Eigen::Index>(n) * components + k] +=
            V * values[static_cast<Eigen::Index>(e) * components + k];
    }
  }
  for (int n = 0; n < N; ++n) {
    auto seg =
        sum.segment(static_cast<Eigen::Index>(n) * components, components);
    if (weight[n] > 0)
      seg /= weight[n];
    else
      seg.setConstant(kNaN);
  }
  return sum;
}

SurfaceResult extract_density_surface(const TetMesh &m,
                                      const Eigen::VectorXd &rho,
                                      const SurfaceOptions &o) {
  check_options(o);
  check_element_density(m, rho);
  return extract_impl(m, nodal_average(m, rho, 1), o, false).surface;
}

SurfaceResult extract_nodal_surface(const TetMesh &m, const Eigen::VectorXd &d,
                                    const SurfaceOptions &o) {
  check_options(o);
  check_nodal_density(m, d);
  return extract_impl(m, d, o, false).surface;
}

ClippedResult clip_density_domain(const TetMesh &m, const Eigen::VectorXd &rho,
                                  const SurfaceOptions &o) {
  check_options(o);
  check_element_density(m, rho);
  return extract_impl(m, nodal_average(m, rho, 1), o, true);
}

ClippedResult clip_nodal_domain(const TetMesh &m, const Eigen::VectorXd &d,
                                const SurfaceOptions &o) {
  check_options(o);
  check_nodal_density(m, d);
  return extract_impl(m, d, o, true);
}

SurfaceMesh domain_boundary_surface(const TetMesh &m) {
  const int F = m.surface_count();
  std::vector<int> vid(static_cast<std::size_t>(m.node_count()), -1);
  std::vector<int> order;
  for (int f = 0; f < F; ++f)
    for (int k = 0; k < 3; ++k) {
      const int n = m.surface_nodes(k, f);
      if (vid[n] < 0) {
        vid[n] = 0;
        order.push_back(n);
      }
    }
  std::sort(order.begin(), order.end());
  SurfaceMesh s;
  s.points_m.resize(3, static_cast<Eigen::Index>(order.size()));
  s.vertex_source.resize(order.size());
  for (std::size_t i = 0; i < order.size(); ++i) {
    vid[order[i]] = static_cast<int>(i);
    s.points_m.col(static_cast<Eigen::Index>(i)) = m.nodes_m.col(order[i]);
    s.vertex_source[i] = {order[i], -1, 0.0};
  }
  s.triangles.resize(3, F);
  s.triangle_element = m.surface_element;
  s.triangle_origin.assign(static_cast<std::size_t>(F),
                           TriangleOrigin::DomainBoundary);
  for (int f = 0; f < F; ++f)
    for (int k = 0; k < 3; ++k)
      s.triangles(k, f) = vid[m.surface_nodes(k, f)];
  return s;
}

SurfaceEvidence validate_surface(const SurfaceMesh &s) {
  check_surface_shape(s);
  SurfaceEvidence ev;
  const int V = s.vertex_count(), T = s.triangle_count();
  ev.vertices = V;
  ev.triangles = T;
  for (TriangleOrigin o : s.triangle_origin) {
    if (o == TriangleOrigin::Isosurface)
      ++ev.isosurface_triangles;
    else if (o == TriangleOrigin::DomainBoundary)
      ++ev.boundary_triangles;
    else
      ++ev.facet_triangles;
  }
  std::vector<std::uint8_t> used(static_cast<std::size_t>(V), 0);
  for (int t = 0; t < T; ++t)
    for (int k = 0; k < 3; ++k)
      used[s.triangles(k, t)] = 1;
  for (int v = 0; v < V; ++v)
    if (!used[v])
      ++ev.isolated_vertices;
  if (T == 0) {
    ev.state = GeometryState::Empty;
    add_issue(ev, "isolated_vertices", ev.isolated_vertices);
    return ev;
  }

  // Origin at the bounding-box centre keeps volume sums well conditioned.
  Eigen::Vector3d lo =
      Eigen::Vector3d::Constant(std::numeric_limits<double>::infinity());
  Eigen::Vector3d hi = -lo;
  for (int v = 0; v < V; ++v)
    if (used[v]) {
      lo = lo.cwiseMin(s.points_m.col(v));
      hi = hi.cwiseMax(s.points_m.col(v));
    }
  const Eigen::Vector3d origin = 0.5 * (lo + hi);

  ev.min_triangle_area_m2 = std::numeric_limits<double>::infinity();
  ev.min_edge_m = std::numeric_limits<double>::infinity();
  std::vector<std::uint8_t> degenerate(static_cast<std::size_t>(T), 0);
  for (int t = 0; t < T; ++t) {
    const int a = s.triangles(0, t), b = s.triangles(1, t),
              c = s.triangles(2, t);
    const Eigen::Vector3d pa = s.points_m.col(a), pb = s.points_m.col(b),
                          pc = s.points_m.col(c);
    const double area = 0.5 * (pb - pa).cross(pc - pa).norm();
    if (a == b || b == c || a == c || !(std::isfinite(area) && area > 0)) {
      degenerate[t] = 1;
      ++ev.degenerate_triangles;
    }
    ev.area_m2 += std::isfinite(area) ? area : 0.0;
    ev.min_triangle_area_m2 =
        std::min(ev.min_triangle_area_m2, std::isfinite(area) ? area : 0.0);
    ev.min_edge_m = std::min(
        {ev.min_edge_m, (pb - pa).norm(), (pc - pb).norm(), (pa - pc).norm()});
  }

  // Edge uses.
  struct EdgeUse {
    std::uint64_t key;
    int tri;
    int dir;
  };
  std::vector<EdgeUse> edges;
  edges.reserve(3 * static_cast<std::size_t>(T));
  for (int t = 0; t < T; ++t)
    for (int k = 0; k < 3; ++k) {
      const int u = s.triangles(k, t), w = s.triangles((k + 1) % 3, t);
      if (u == w)
        continue;
      edges.push_back(
          {pair_key(std::min(u, w), std::max(u, w)), t, u < w ? 1 : -1});
    }
  std::sort(edges.begin(), edges.end(), [](const EdgeUse &x, const EdgeUse &y) {
    return x.key != y.key ? x.key < y.key : x.tri < y.tri;
  });
  UnionFind shells(T);
  for (std::size_t i = 0; i < edges.size();) {
    std::size_t j = i;
    while (j < edges.size() && edges[j].key == edges[i].key) {
      shells.unite(edges[i].tri, edges[j].tri);
      ++j;
    }
    const std::size_t uses = j - i;
    if (uses == 1)
      ++ev.open_edges;
    else if (uses == 2) {
      if (edges[i].dir == edges[i + 1].dir)
        ++ev.orientation_conflicts;
    } else {
      ++ev.nonmanifold_edges;
    }
    i = j;
  }

  // Vertex fans: link edges (next, prev) around each vertex must form one
  // connected piece.
  std::vector<int> offsets(static_cast<std::size_t>(V) + 1, 0);
  for (int t = 0; t < T; ++t)
    if (!degenerate[t])
      for (int k = 0; k < 3; ++k)
        ++offsets[s.triangles(k, t) + 1];
  for (int v = 0; v < V; ++v)
    offsets[v + 1] += offsets[v];
  std::vector<std::pair<int, int>> links(static_cast<std::size_t>(offsets[V]));
  {
    std::vector<int> fill(offsets.begin(), offsets.end() - 1);
    for (int t = 0; t < T; ++t)
      if (!degenerate[t])
        for (int k = 0; k < 3; ++k)
          links[fill[s.triangles(k, t)]++] = {s.triangles((k + 1) % 3, t),
                                              s.triangles((k + 2) % 3, t)};
  }
  std::vector<int> local_ids, local_parent;
  const auto local = [&](int id) {
    for (std::size_t i = 0; i < local_ids.size(); ++i)
      if (local_ids[i] == id)
        return static_cast<int>(i);
    local_ids.push_back(id);
    local_parent.push_back(static_cast<int>(local_parent.size()));
    return static_cast<int>(local_ids.size()) - 1;
  };
  const auto root = [&](int x) {
    while (local_parent[x] != x)
      x = local_parent[x] = local_parent[local_parent[x]];
    return x;
  };
  for (int v = 0; v < V; ++v) {
    if (offsets[v] == offsets[v + 1])
      continue;
    local_ids.clear();
    local_parent.clear();
    for (int i = offsets[v]; i < offsets[v + 1]; ++i) {
      const int a = root(local(links[i].first));
      const int b = root(local(links[i].second));
      if (a != b)
        local_parent[std::max(a, b)] = std::min(a, b);
    }
    int pieces = 0;
    for (std::size_t i = 0; i < local_parent.size(); ++i)
      if (root(static_cast<int>(i)) == static_cast<int>(i))
        ++pieces;
    if (pieces > 1)
      ++ev.nonmanifold_vertices;
  }

  UnionFind vcomp(V);
  for (int t = 0; t < T; ++t) {
    vcomp.unite(s.triangles(0, t), s.triangles(1, t));
    vcomp.unite(s.triangles(0, t), s.triangles(2, t));
  }
  for (int v = 0; v < V; ++v)
    if (used[v] && vcomp.find(v) == v)
      ++ev.vertex_components;

  // Shell volumes with a rounding bound.
  std::vector<int> shell_of(static_cast<std::size_t>(T));
  std::vector<int> root_to_shell(static_cast<std::size_t>(T), -1);
  struct Acc {
    ShellEvidence ev;
    double abs_sum = 0;
    int largest_tri = -1;
    double largest_area = -1;
    Eigen::Vector3d lo, hi;
  };
  std::vector<Acc> acc;
  for (int t = 0; t < T; ++t) {
    const int r = shells.find(t);
    if (root_to_shell[r] < 0) {
      root_to_shell[r] = static_cast<int>(acc.size());
      Acc a;
      a.lo = Eigen::Vector3d::Constant(std::numeric_limits<double>::infinity());
      a.hi = -a.lo;
      acc.push_back(a);
    }
    const int sh = root_to_shell[r];
    shell_of[t] = sh;
    Acc &a = acc[sh];
    const Eigen::Vector3d pa = s.points_m.col(s.triangles(0, t)) - origin;
    const Eigen::Vector3d pb = s.points_m.col(s.triangles(1, t)) - origin;
    const Eigen::Vector3d pc = s.points_m.col(s.triangles(2, t)) - origin;
    const double term = pa.dot(pb.cross(pc)) / 6.0;
    const double area = 0.5 * (pb - pa).cross(pc - pa).norm();
    a.ev.triangles += 1;
    a.ev.signed_volume_m3 += term;
    a.ev.area_m2 += area;
    a.abs_sum += std::abs(term);
    if (area > a.largest_area) {
      a.largest_area = area;
      a.largest_tri = t;
    }
    for (const Eigen::Vector3d *p : {&pa, &pb, &pc}) {
      a.lo = a.lo.cwiseMin(*p + origin);
      a.hi = a.hi.cwiseMax(*p + origin);
    }
  }
  ev.shells = static_cast<int>(acc.size());
  constexpr double eps = std::numeric_limits<double>::epsilon();
  double body_total = 0, body_max = 0;
  std::vector<std::uint8_t> zero_shell(acc.size(), 0);
  for (std::size_t i = 0; i < acc.size(); ++i) {
    const double vol = acc[i].ev.signed_volume_m3;
    ev.signed_volume_m3 += vol;
    if (std::abs(vol) <= 64 * eps * acc[i].abs_sum) {
      ++ev.zero_volume_shells;
      zero_shell[i] = 1;
    } else if (vol > 0) {
      ++ev.bodies;
      body_total += vol;
      body_max = std::max(body_max, vol);
    }
  }
  // Shell contact and nesting (see SurfaceEvidence). Closed shells that do
  // not touch partition space, so the other shells' winding number is
  // constant over each shell and one point of it decides; the material
  // winding must stay in {0,1} on both sides of every shell.
  if (acc.size() > 1) {
    admit_or_fail(160.0 * T + 8.0 * V, "topology surface shell checks");
    const TriangleBvh bvh(s);
    const std::vector<std::uint8_t> touching =
        touching_shells(s, bvh, shell_of, static_cast<int>(acc.size()));
    std::vector<Eigen::Vector3d> shell_lo(acc.size()), shell_hi(acc.size());
    for (std::size_t i = 0; i < acc.size(); ++i) {
      shell_lo[i] = acc[i].lo;
      shell_hi[i] = acc[i].hi;
    }
    for (std::size_t i = 0; i < acc.size(); ++i) {
      if (touching[i]) {
        ++ev.intersecting_shells;
        continue;
      }
      if (zero_shell[i])
        continue;
      const int t = acc[i].largest_tri;
      const Eigen::Vector3d p =
          (tri_corner(s, t, 0) + tri_corner(s, t, 1) + tri_corner(s, t, 2)) /
          3.0;
      const int w = winding_of_others(s, bvh, shell_of, degenerate, shell_lo,
                                      shell_hi, static_cast<int>(i), p);
      if (acc[i].ev.signed_volume_m3 < 0) {
        if (w == 1)
          ++ev.cavities;
        else if (w == 0)
          ++ev.unenclosed_cavities;
        else
          ++ev.inconsistent_nesting;
      } else if (w != 0) {
        ++ev.inconsistent_nesting;
      }
    }
  } else if (!zero_shell[0] && acc[0].ev.signed_volume_m3 < 0) {
    ++ev.unenclosed_cavities; // a lone negative shell encloses nothing
  }
  ev.largest_body_volume_fraction =
      body_total > 0 ? body_max / body_total : 0.0;
  std::vector<ShellEvidence> details;
  details.reserve(acc.size());
  for (const Acc &a : acc)
    details.push_back(a.ev);
  std::sort(details.begin(), details.end(),
            [](const ShellEvidence &x, const ShellEvidence &y) {
              return std::abs(x.signed_volume_m3) >
                     std::abs(y.signed_volume_m3);
            });
  if (details.size() > 32)
    details.resize(32);
  ev.shell_details = std::move(details);

  ev.watertight = ev.open_edges == 0 && ev.nonmanifold_edges == 0 &&
                  ev.orientation_conflicts == 0;
  ev.manifold = ev.watertight && ev.nonmanifold_vertices == 0;
  add_issue(ev, "open_edges", ev.open_edges);
  add_issue(ev, "nonmanifold_edges", ev.nonmanifold_edges);
  add_issue(ev, "orientation_conflicts", ev.orientation_conflicts);
  add_issue(ev, "nonmanifold_vertices", ev.nonmanifold_vertices);
  add_issue(ev, "degenerate_triangles", ev.degenerate_triangles);
  add_issue(ev, "isolated_vertices", ev.isolated_vertices);
  add_issue(ev, "zero_volume_shells", ev.zero_volume_shells);
  add_issue(ev, "intersecting_shells", ev.intersecting_shells);
  add_issue(ev, "unenclosed_cavities", ev.unenclosed_cavities);
  add_issue(ev, "inconsistent_nesting", ev.inconsistent_nesting);
  add_issue(ev, "nonpositive_volume", ev.signed_volume_m3 > 0 ? 0 : 1);
  if (!ev.issues.empty())
    ev.state = GeometryState::Unresolved;
  else
    ev.state =
        ev.bodies > 1 ? GeometryState::Disconnected : GeometryState::Valid;
  return ev;
}

bool surface_self_intersects(const SurfaceMesh &s,
                              std::vector<int> *crossing_triangles) {
  check_surface_shape(s);
  const int T = s.triangle_count();
  std::vector<std::uint8_t> marked;
  if (crossing_triangles) {
    crossing_triangles->clear();
    marked.assign(static_cast<std::size_t>(T), 0);
  }
  bool found = false;
  const bool finite = s.points_m.allFinite();
  for (int t = 0; t < T; ++t) {
    const double n2 = (tri_corner(s, t, 1) - tri_corner(s, t, 0))
                          .cross(tri_corner(s, t, 2) - tri_corner(s, t, 0))
                          .squaredNorm();
    if (!(std::isfinite(n2) && n2 > 0)) {
      if (!crossing_triangles)
        return true;
      marked[t] = 2;
      found = true;
    }
  }
  if (finite) {
    const TriangleBvh bvh(s);
    for (int t = 0; t < T && (!found || crossing_triangles); ++t) {
      if (crossing_triangles && marked[t] == 2)
        continue;
      bvh.overlapping(bvh.lo(t), bvh.hi(t), [&](int other) {
        if (other <= t || (found && !crossing_triangles))
          return;
        if (crossing_triangles &&
            (marked[other] == 2 || (marked[t] && marked[other])))
          return;
        if (triangles_cross_beyond_shared(s, t, other)) {
          found = true;
          if (crossing_triangles)
            marked[t] = marked[other] = 1;
        }
      });
    }
  }
  if (crossing_triangles)
    for (int t = 0; t < T; ++t)
      if (marked[t])
        crossing_triangles->push_back(t);
  return found || !finite;
}

SurfaceMesh smooth_print_surface(const SurfaceMesh &raw,
                                 const PrintSurfaceOptions &options,
                                 PrintSurfaceEvidence &evidence) {
  check_surface_shape(raw);
  evidence = PrintSurfaceEvidence{};
  evidence.iterations_requested = options.iterations;
  if (options.iterations < 1 || !std::isfinite(options.lambda) ||
      !std::isfinite(options.mu) || options.lambda <= 0 || options.lambda >= 1 ||
      options.mu >= 0 || options.mu <= -1)
    fail("invalid_input", "invalid print-surface Taubin smoothing parameters");
  const int V = raw.vertex_count();
  std::vector<std::uint8_t> movable(static_cast<std::size_t>(V), 1);
  std::vector<std::vector<int>> neighbors(static_cast<std::size_t>(V));
  for (int t = 0; t < raw.triangle_count(); ++t)
    for (int k = 0; k < 3; ++k) {
      const int v = raw.triangles(k, t);
      if (raw.triangle_origin[t] != TriangleOrigin::Isosurface)
        movable[v] = 0;
      neighbors[v].push_back(raw.triangles((k + 1) % 3, t));
      neighbors[v].push_back(raw.triangles((k + 2) % 3, t));
    }
  for (int v = 0; v < V; ++v) {
    auto &adjacent = neighbors[v];
    std::sort(adjacent.begin(), adjacent.end());
    adjacent.erase(std::unique(adjacent.begin(), adjacent.end()), adjacent.end());
    if (adjacent.empty())
      movable[v] = 0;
    if (movable[v])
      ++evidence.smoothed_vertices;
    else
      ++evidence.pinned_vertices;
  }
  const double raw_volume = validate_surface(raw).signed_volume_m3;
  SurfaceMesh candidate = raw;
  Eigen::Matrix3Xd next(3, V);
  std::vector<int> bad_triangles;
  std::vector<std::uint8_t> rollback(static_cast<std::size_t>(V), 0);
  for (int iterations = options.iterations; iterations >= 1; iterations /= 2) {
    for (int round = 0; round <= 8; ++round) {
      candidate.points_m = raw.points_m;
      for (int i = 0; i < iterations; ++i)
        for (double factor : {options.lambda, options.mu}) {
          next = candidate.points_m;
          for (int v = 0; v < V; ++v) {
            if (!movable[v])
              continue;
            Eigen::Vector3d mean = Eigen::Vector3d::Zero();
            for (int n : neighbors[v])
              mean += candidate.points_m.col(n);
            mean /= static_cast<double>(neighbors[v].size());
            next.col(v) += factor * (mean - candidate.points_m.col(v));
          }
          candidate.points_m.swap(next);
        }
      const auto geometry = validate_surface(candidate);
      evidence.self_intersections_checked = true;
      const bool intersects = surface_self_intersects(candidate, &bad_triangles);
      const std::vector<float> f32 = positions_mm_f32(candidate, false);
      for (int t = 0; t < candidate.triangle_count(); ++t)
        if (!valid_f32_triangle(candidate, f32, t))
          bad_triangles.push_back(t);
      if (!bad_triangles.empty()) {
        evidence.reason = intersects ? "smoothed triangles self-intersect"
                                    : "smoothed triangles collapse in STL float32";
        if (round == 8)
          break;
        std::fill(rollback.begin(), rollback.end(), 0);
        for (int t : bad_triangles)
          for (int k = 0; k < 3; ++k) {
            const int v = raw.triangles(k, t);
            rollback[v] = 1;
            for (int n : neighbors[v])
              rollback[n] = 1;
          }
        int added = 0;
        for (int v = 0; v < V; ++v)
          if (rollback[v] && movable[v]) {
            movable[v] = 0;
            ++added;
          }
        if (added == 0)
          break;
        evidence.rollback_vertices += added;
        evidence.smoothed_vertices -= added;
        ++evidence.rollback_rounds;
        continue;
      }
      if (geometry.state != GeometryState::Valid || intersects) {
        evidence.reason = "smoothed surface state " +
                          std::string(to_string(geometry.state));
        break;
      }
      evidence.state = "valid";
      evidence.iterations_used = iterations;
      evidence.reason.clear();
      for (int v = 0; v < V; ++v)
        evidence.max_displacement_mm = std::max(
            evidence.max_displacement_mm,
            1e3 * (candidate.points_m.col(v) - raw.points_m.col(v)).norm());
      evidence.volume_change_fraction =
          raw_volume > 0 ? (geometry.signed_volume_m3 - raw_volume) / raw_volume : 0;
      return candidate;
    }
  }
  evidence.state = "refused";
  fail("print_surface_invalid", evidence.reason);
}

nlohmann::json to_json(const PrintSurfaceEvidence &e) {
  nlohmann::json out = {
      {"state", e.state},
      {"iterations_requested", e.iterations_requested},
      {"iterations_used", e.iterations_used},
      {"rollback_vertices", e.rollback_vertices},
      {"rollback_rounds", e.rollback_rounds},
      {"pinned_vertices", e.pinned_vertices},
      {"smoothed_vertices", e.smoothed_vertices},
      {"max_displacement_mm", e.max_displacement_mm},
      {"volume_change_fraction", e.volume_change_fraction},
      {"self_intersections_checked", e.self_intersections_checked}};
  if (!e.reason.empty())
    out["reason"] = e.reason;
  return out;
}

SurfaceOverhang measure_surface_overhang(const SurfaceMesh &s,
                                         const Eigen::Vector3d &direction,
                                         double angle_deg,
                                         double plate_height_m,
                                         double plate_tolerance_m) {
  check_surface_shape(s);
  if (!direction.allFinite() || !(direction.norm() > 0) ||
      !(angle_deg > 0 && angle_deg < 90) ||
      !std::isfinite(plate_height_m) || !(plate_tolerance_m >= 0))
    fail("invalid_input", "invalid surface overhang settings");
  const Eigen::Vector3d d = direction.normalized();
  const double limit = std::sin(angle_deg * kPi / 180.0);
  constexpr double cos_two_degrees = 0.9993908270190958;
  SurfaceOverhang out;
  out.vertex_fraction = Eigen::VectorXd::Zero(s.vertex_count());
  Eigen::VectorXd area = Eigen::VectorXd::Zero(s.vertex_count());
  for (int t = 0; t < s.triangle_count(); ++t) {
    const Eigen::Vector3d cross = (tri_corner(s, t, 1) - tri_corner(s, t, 0))
                                    .cross(tri_corner(s, t, 2) - tri_corner(s, t, 0));
    const double twice_area = cross.norm();
    if (!(twice_area > 0))
      continue;
    const double a = 0.5 * twice_area, nd = cross.dot(d) / twice_area;
    bool on_plate = nd < -cos_two_degrees;
    for (int k = 0; k < 3; ++k)
      on_plate = on_plate &&
                 std::abs(tri_corner(s, t, k).dot(d) - plate_height_m) <=
                     plate_tolerance_m;
    const bool unsupported = !on_plate && -nd > limit;
    out.total_area_m2 += a;
    if (unsupported)
      out.unsupported_area_m2 += a;
    for (int k = 0; k < 3; ++k) {
      const int v = s.triangles(k, t);
      area[v] += a;
      if (unsupported)
        out.vertex_fraction[v] += a;
    }
  }
  for (int v = 0; v < s.vertex_count(); ++v)
    if (area[v] > 0)
      out.vertex_fraction[v] /= area[v];
  return out;
}

Eigen::VectorXd interpolate_nodal_field(const SurfaceMesh &s,
                                        const Eigen::VectorXd &values,
                                        int components) {
  if (components < 1 || values.size() % components != 0)
    fail("invalid_field", "nodal field size " + std::to_string(values.size()) +
                              " is not a multiple of components " +
                              std::to_string(components));
  const Eigen::Index N = values.size() / components;
  Eigen::VectorXd out(static_cast<Eigen::Index>(s.vertex_count()) * components);
  for (int v = 0; v < s.vertex_count(); ++v) {
    const SurfaceVertexSource &src = s.vertex_source[v];
    if (src.node_a < 0 || src.node_a >= N || src.node_b >= N)
      fail("invalid_field", "surface vertex " + std::to_string(v) +
                                " references a node outside the field");
    for (int k = 0; k < components; ++k) {
      const double fa =
          values[static_cast<Eigen::Index>(src.node_a) * components + k];
      double f = fa;
      if (src.node_b >= 0)
        f = fa +
            src.weight *
                (values[static_cast<Eigen::Index>(src.node_b) * components +
                        k] -
                 fa);
      if (!std::isfinite(f))
        fail("invalid_field",
             "field is not finite at surface vertex " + std::to_string(v));
      out[static_cast<Eigen::Index>(v) * components + k] = f;
    }
  }
  return out;
}

Eigen::VectorXd element_field_on_triangles(const SurfaceMesh &s,
                                           const Eigen::VectorXd &values,
                                           int components) {
  if (components < 1 || values.size() % components != 0)
    fail("invalid_field",
         "element field size " + std::to_string(values.size()) +
             " is not a multiple of components " + std::to_string(components));
  const Eigen::Index E = values.size() / components;
  Eigen::VectorXd out(static_cast<Eigen::Index>(s.triangle_count()) *
                      components);
  for (int t = 0; t < s.triangle_count(); ++t) {
    const int e = s.triangle_element[t];
    if (e < 0 || e >= E)
      fail("invalid_field", "surface triangle " + std::to_string(t) +
                                " references an element outside the field");
    for (int k = 0; k < components; ++k) {
      const double f = values[static_cast<Eigen::Index>(e) * components + k];
      if (!std::isfinite(f))
        fail("invalid_field",
             "field is not finite on element " + std::to_string(e));
      out[static_cast<Eigen::Index>(t) * components + k] = f;
    }
  }
  return out;
}

RetainedDomain retained_domain(const TetMesh &m, const Eigen::VectorXd &rho,
                               double threshold) {
  check_threshold(threshold, "retained-domain");
  check_element_density(m, rho);
  const int E = m.element_count(), N = m.node_count();
  admit_or_fail(40.0 * N + 200.0 * E, "topology retained-domain extraction");
  RetainedDomain r;
  r.threshold = threshold;
  r.domain_volume_m3 = m.total_volume_m3;
  std::vector<std::uint8_t> keep(static_cast<std::size_t>(E), 0);
  r.node_to_compact.assign(static_cast<std::size_t>(N), -1);
  for (int e = 0; e < E; ++e)
    if (rho[e] >= threshold) {
      keep[e] = 1;
      r.elements.push_back(e);
      r.volume_m3 += m.volume_m3[e];
      for (int a = 0; a < 4; ++a)
        r.node_to_compact[m.tets(a, e)] = 0;
    }
  for (int n = 0; n < N; ++n)
    if (r.node_to_compact[n] == 0) {
      r.node_to_compact[n] = static_cast<int>(r.nodes.size());
      r.nodes.push_back(n);
    }
  r.points_mm.resize(3 * r.nodes.size());
  for (std::size_t i = 0; i < r.nodes.size(); ++i)
    for (int k = 0; k < 3; ++k)
      r.points_mm[3 * i + k] = units::m_to_mm(m.nodes_m(k, r.nodes[i]));
  r.tets.reserve(4 * r.elements.size());
  for (int e : r.elements)
    for (int a = 0; a < 4; ++a)
      r.tets.push_back(r.node_to_compact[m.tets(a, e)]);

  // Face components and the faceted boundary (outward from retained tets).
  std::vector<int> local(static_cast<std::size_t>(E), -1);
  for (std::size_t i = 0; i < r.elements.size(); ++i)
    local[r.elements[i]] = static_cast<int>(i);
  UnionFind uf(static_cast<int>(r.elements.size()));
  std::vector<int> vid(static_cast<std::size_t>(N), -1);
  std::vector<int> tri, tri_elem, vnodes;
  std::vector<TriangleOrigin> origin;
  for (int e : r.elements)
    for (int a = 0; a < 4; ++a) {
      const int nb = m.face_neighbor(a, e);
      if (nb >= 0 && keep[nb]) {
        uf.unite(local[e], local[nb]);
        continue;
      }
      for (int k = 0; k < 3; ++k) {
        const int n = m.tets(kTetFaces[a][k], e);
        if (vid[n] < 0) {
          vid[n] = static_cast<int>(vnodes.size());
          vnodes.push_back(n);
        }
        tri.push_back(vid[n]);
      }
      tri_elem.push_back(e);
      origin.push_back(nb < 0 ? TriangleOrigin::DomainBoundary
                              : TriangleOrigin::ThresholdFacet);
    }
  for (std::size_t i = 0; i < r.elements.size(); ++i)
    if (uf.find(static_cast<int>(i)) == static_cast<int>(i))
      ++r.face_components;
  SurfaceMesh &s = r.boundary;
  s.points_m.resize(3, static_cast<Eigen::Index>(vnodes.size()));
  s.vertex_source.resize(vnodes.size());
  for (std::size_t i = 0; i < vnodes.size(); ++i) {
    s.points_m.col(static_cast<Eigen::Index>(i)) = m.nodes_m.col(vnodes[i]);
    s.vertex_source[i] = {vnodes[i], -1, 0.0};
  }
  const Eigen::Index T = static_cast<Eigen::Index>(tri_elem.size());
  s.triangles.resize(3, T);
  for (Eigen::Index t = 0; t < T; ++t)
    for (int k = 0; k < 3; ++k)
      s.triangles(k, t) = tri[static_cast<std::size_t>(3 * t + k)];
  s.triangle_element = Eigen::Map<const Eigen::VectorXi>(tri_elem.data(), T);
  s.triangle_origin = std::move(origin);
  r.boundary_evidence = validate_surface(s);
  return r;
}

RetainedNodeSet remap_to_retained(const RetainedDomain &r,
                                  const std::vector<int> &mesh_nodes) {
  RetainedNodeSet out;
  for (int n : mesh_nodes) {
    if (n < 0 || n >= static_cast<int>(r.node_to_compact.size()))
      fail("invalid_input",
           "node " + std::to_string(n) + " is outside the mesh");
    const int c = r.node_to_compact[n];
    (c >= 0 ? out.nodes : out.dropped).push_back(c >= 0 ? c : n);
  }
  for (auto *v : {&out.nodes, &out.dropped}) {
    std::sort(v->begin(), v->end());
    v->erase(std::unique(v->begin(), v->end()), v->end());
  }
  return out;
}

RetainedNodeSet remap_to_clipped(const TetMesh &m, const ClippedDomain &c,
                                 const std::vector<int> &mesh_nodes) {
  const int N = m.node_count();
  std::vector<std::uint8_t> selected(static_cast<std::size_t>(N), 0);
  for (int n : mesh_nodes) {
    if (n < 0 || n >= N)
      fail("invalid_input",
           "node " + std::to_string(n) + " is outside the mesh");
    selected[n] = 1;
  }
  // Edges of exterior triangles whose three corners are all selected.
  std::vector<std::uint64_t> patch_edges;
  for (int f : surface_faces_with_nodes(m, mesh_nodes))
    for (int k = 0; k < 3; ++k) {
      const int a = m.surface_nodes(k, f), b = m.surface_nodes((k + 1) % 3, f);
      patch_edges.push_back(pair_key(std::min(a, b), std::max(a, b)));
    }
  std::sort(patch_edges.begin(), patch_edges.end());
  RetainedNodeSet out;
  std::vector<std::uint8_t> present(static_cast<std::size_t>(N), 0);
  for (std::size_t v = 0; v < c.vertex_source.size(); ++v) {
    const SurfaceVertexSource &s = c.vertex_source[v];
    if (s.node_a < 0 || s.node_a >= N || s.node_b >= N)
      fail("invalid_input", "clipped vertex " + std::to_string(v) +
                                " references a node outside the mesh");
    if (s.node_b < 0) {
      if (selected[s.node_a]) {
        out.nodes.push_back(static_cast<int>(v));
        present[s.node_a] = 1;
      }
    } else if (std::binary_search(patch_edges.begin(), patch_edges.end(),
                                  pair_key(std::min(s.node_a, s.node_b),
                                           std::max(s.node_a, s.node_b)))) {
      out.nodes.push_back(static_cast<int>(v));
    }
  }
  for (int n : mesh_nodes)
    if (!present[n])
      out.dropped.push_back(n);
  for (auto *v : {&out.nodes, &out.dropped}) {
    std::sort(v->begin(), v->end());
    v->erase(std::unique(v->begin(), v->end()), v->end());
  }
  return out;
}

DiscretizationComparison compare_discretizations(const RetainedDomain &r,
                                                 const SurfaceResult &s) {
  DiscretizationComparison c;
  c.retained_volume_m3 = r.volume_m3;
  c.surface_volume_m3 = s.evidence.signed_volume_m3;
  c.difference_m3 = c.surface_volume_m3 - c.retained_volume_m3;
  c.relative_to_retained =
      r.volume_m3 > 0 ? c.difference_m3 / r.volume_m3 : kNaN;
  c.relative_to_domain =
      r.domain_volume_m3 > 0 ? c.difference_m3 / r.domain_volume_m3 : kNaN;
  c.retained_state = r.boundary_evidence.state;
  c.surface_state = s.evidence.state;
  return c;
}

// ── Writers
// ────────────────────────────────────────────────────────────────────

ArtifactFile write_stl(const std::string &path, const SurfaceMesh &s,
                       const std::string &label) {
  check_surface_shape(s);
  const int T = s.triangle_count();
  if (T == 0)
    fail("empty_surface", "no triangles to write to " + path);
  const std::vector<float> f = positions_mm_f32(s);
  const auto corner = [&](int v) {
    const std::size_t i = static_cast<std::size_t>(v) * 3;
    return Eigen::Vector3d(f[i], f[i + 1], f[i + 2]);
  };
  // Verify every float32 triangle before creating the file.
  check_f32_triangles(s, f, "stl_precision_loss", "STL");
  ArtifactWriter out(path, "STL");
  unsigned char header[84] = {};
  const std::string text = "chudware topology surface; units=mm; " + label;
  std::memcpy(header, text.data(), std::min<std::size_t>(80, text.size()));
  put_u32(header + 80, static_cast<std::uint32_t>(T));
  out.write(header, sizeof(header));
  unsigned char rec[50];
  for (int t = 0; t < T; ++t) {
    const Eigen::Vector3d a = corner(s.triangles(0, t));
    const Eigen::Vector3d n = (corner(s.triangles(1, t)) - a)
                                  .cross(corner(s.triangles(2, t)) - a)
                                  .normalized();
    for (int k = 0; k < 3; ++k)
      put_f32(rec + 4 * k, static_cast<float>(n[k]));
    for (int c = 0; c < 3; ++c)
      for (int k = 0; k < 3; ++k)
        put_f32(rec + 12 + 12 * c + 4 * k,
                f[static_cast<std::size_t>(s.triangles(c, t)) * 3 + k]);
    put_u16(rec + 48, 0);
    out.write(rec, sizeof(rec));
  }
  ArtifactFile a = out.finish("stl_binary");
  a.points = s.vertex_count();
  a.triangles = T;
  return a;
}

ArtifactFile write_surface_aicm(const std::string &path, const SurfaceMesh &s,
                                const std::vector<SurfaceScalar> &scalars,
                                const std::string &body_name) {
  check_surface_shape(s);
  const int V = s.vertex_count(), T = s.triangle_count();
  if (T == 0)
    fail("empty_surface", "no triangles to write to " + path);
  if (static_cast<std::size_t>(T) > kMaxOverlayTris)
    fail("surface_too_large",
         std::to_string(T) +
             " triangles exceed the viewport result-surface "
             "budget of " +
             std::to_string(kMaxOverlayTris));
  // The float32 millimetre triangles the viewport draws, verified before any
  // file is created.
  const std::vector<float> pos = positions_mm_f32(s);
  check_f32_triangles(s, pos, "aicm_precision_loss", "AICM");
  const auto corner = [&](int v) {
    const std::size_t i = static_cast<std::size_t>(v) * 3;
    return Eigen::Vector3d(pos[i], pos[i + 1], pos[i + 2]);
  };
  std::vector<std::vector<std::uint8_t>> chunks;
  nlohmann::json arrays = nlohmann::json::array();
  for (std::size_t i = 0; i < scalars.size(); ++i) {
    const SurfaceScalar &sc = scalars[i];
    if (sc.name.empty() || sc.name.size() > 0xFFFF)
      fail("invalid_field", "AICM scalar names must be 1..65535 bytes");
    for (std::size_t j = 0; j < i; ++j)
      if (scalars[j].name == sc.name)
        fail("invalid_field", "AICM scalar " + sc.name + " repeats");
    if (sc.values.size() != V)
      fail("invalid_field", "AICM scalar " + sc.name + " has " +
                                std::to_string(sc.values.size()) +
                                " values, expected " + std::to_string(V));
    std::vector<float> f32(static_cast<std::size_t>(V));
    double lo = std::numeric_limits<double>::infinity(), hi = -lo;
    for (int v = 0; v < V; ++v) {
      const double x = sc.values[v];
      if (!finite_f32(x))
        fail("invalid_field", "AICM scalar " + sc.name +
                                  " is not finite in float32 at vertex " +
                                  std::to_string(v));
      f32[v] = static_cast<float>(x);
      lo = std::min(lo, x);
      hi = std::max(hi, x);
    }
    float vmin = static_cast<float>(lo), vmax = static_cast<float>(hi);
    if (!(vmax > vmin)) { // legend ranges are never degenerate
      if (vmin == 0.0f) {
        vmax = 1.0f;
      } else {
        vmin = std::nextafter(vmin, -std::numeric_limits<float>::infinity());
        vmax = std::nextafter(vmax, std::numeric_limits<float>::infinity());
      }
    }
    if (sc.name == "overhang") {
      vmin = 0;
      vmax = 1;
    }
    chunks.push_back(aicm::scalar_chunk(1, sc.name, f32, vmin, vmax));
    arrays.push_back({{"name", sc.name},
                      {"location", "vertex"},
                      {"unit", sc.unit},
                      {"components", 1}});
  }

  aicm::AicmBody body;
  body.body_id = 1;
  body.name = body_name;
  body.pos.insert_end(pos.begin(), pos.end());
  std::vector<double> accum(3 * static_cast<std::size_t>(V), 0.0);
  std::vector<double> best_area(static_cast<std::size_t>(V), 0.0);
  std::vector<Eigen::Vector3d> best_normal(static_cast<std::size_t>(V),
                                           Eigen::Vector3d::Zero());
  body.idx.reserve(3 * static_cast<std::size_t>(T));
  for (int t = 0; t < T; ++t) {
    const Eigen::Vector3d a = corner(s.triangles(0, t));
    const Eigen::Vector3d n =
        (corner(s.triangles(1, t)) - a).cross(corner(s.triangles(2, t)) - a);
    const double area2 = n.norm();
    for (int k = 0; k < 3; ++k) {
      const int v = s.triangles(k, t);
      body.idx.push_back(static_cast<std::uint32_t>(v));
      for (int c = 0; c < 3; ++c)
        accum[3 * static_cast<std::size_t>(v) + c] += n[c];
      if (area2 > best_area[v]) {
        best_area[v] = area2;
        best_normal[v] = n / area2;
      }
    }
  }
  body.nrm.resize(3 * static_cast<std::size_t>(V));
  for (int v = 0; v < V; ++v) {
    Eigen::Vector3d n(accum[3 * v], accum[3 * v + 1], accum[3 * v + 2]);
    const double len = n.norm();
    if (std::isfinite(len) && len > 0) {
      n /= len;
    } else if (best_area[v] > 0) {
      n = best_normal[v]; // opposing fan cancels: largest incident face
    } else {
      fail("invalid_input", "surface vertex " + std::to_string(v) +
                                " has no non-degenerate incident triangle");
    }
    for (int c = 0; c < 3; ++c)
      body.nrm[3 * static_cast<std::size_t>(v) + c] = static_cast<float>(n[c]);
  }
  aicm::FaceRec face;
  face.face_id = 1;
  face.first_tri = 0;
  face.tri_count = static_cast<std::uint32_t>(T);
  face.surf_type = 6;
  body.faces.push_back(face);

  const std::vector<std::uint8_t> blob = aicm::write_aicm({body}, chunks);
  ArtifactWriter out(path, "AICM");
  out.write(blob.data(), blob.size());
  ArtifactFile a = out.finish("aicm_v1");
  a.points = V;
  a.triangles = T;
  a.arrays = std::move(arrays);
  return a;
}

ArtifactFile write_density_vtu(const std::string &path, const TetMesh &m,
                               const Eigen::VectorXd &rho, double threshold,
                               const std::vector<Field> &fields) {
  check_threshold(threshold, "VTU retained");
  check_element_density(m, rho);
  const int N = m.node_count(), E = m.element_count();
  static const char *kReserved[] = {"density", "retained", "nodal_density",
                                    "node_id"};
  for (std::size_t i = 0; i < fields.size(); ++i) {
    const Field &f = fields[i];
    if (f.name.empty())
      fail("invalid_field", "VTU field names must be non-empty");
    for (const char *r : kReserved)
      if (f.name == r)
        fail("invalid_field", "VTU field name " + f.name + " is reserved");
    for (std::size_t j = 0; j < i; ++j)
      if (fields[j].name == f.name)
        fail("invalid_field", "VTU field " + f.name + " repeats");
    if (f.components < 1 || f.components > 9)
      fail("invalid_field", "VTU field " + f.name + " needs 1..9 components");
    const long long count = (f.location == FieldLocation::Node ? N : E) *
                            static_cast<long long>(f.components);
    if (f.values.size() != count)
      fail("invalid_field", "VTU field " + f.name + " has " +
                                std::to_string(f.values.size()) +
                                " values, expected " + std::to_string(count));
  }
  const Eigen::VectorXd nodal = nodal_average(m, rho, 1);
  std::vector<int> compact(static_cast<std::size_t>(N), -1);
  std::vector<int> points;
  for (int n = 0; n < N; ++n)
    if (m.node_referenced[n]) {
      compact[n] = static_cast<int>(points.size());
      points.push_back(n);
    }
  // Every written value must be finite; check before creating the file.
  for (const Field &f : fields) {
    if (f.location == FieldLocation::Element) {
      if (!f.values.allFinite())
        fail("invalid_field", "VTU field " + f.name + " has non-finite values");
      continue;
    }
    for (int n : points)
      for (int k = 0; k < f.components; ++k)
        if (!std::isfinite(
                f.values[static_cast<Eigen::Index>(n) * f.components + k]))
          fail("invalid_field", "VTU field " + f.name +
                                    " is not finite at node " +
                                    std::to_string(n));
  }

  ArtifactWriter writer(path, "VTU");
  TextOut out(writer);
  out << "<?xml version=\"1.0\"?>\n"
         "<VTKFile type=\"UnstructuredGrid\" version=\"0.1\" "
         "byte_order=\"LittleEndian\">\n  <UnstructuredGrid>\n    <Piece "
         "NumberOfPoints=\"";
  out.integer(static_cast<long long>(points.size()));
  out << "\" NumberOfCells=\"";
  out.integer(E);
  out << "\">\n      <Points>\n        <DataArray type=\"Float64\" "
         "NumberOfComponents=\"3\" format=\"ascii\">\n";
  for (int n : points) {
    for (int k = 0; k < 3; ++k) {
      out << ' ';
      out.real(units::m_to_mm(m.nodes_m(k, n)));
    }
    out << '\n';
  }
  out << "        </DataArray>\n      </Points>\n      <Cells>\n        "
         "<DataArray type=\"Int32\" Name=\"connectivity\" format=\"ascii\">\n";
  for (int e = 0; e < E; ++e) {
    for (int a = 0; a < 4; ++a) {
      out << ' ';
      out.integer(compact[m.tets(a, e)]);
    }
    out << '\n';
  }
  out << "        </DataArray>\n        <DataArray type=\"Int32\" "
         "Name=\"offsets\" format=\"ascii\">\n";
  for (int e = 1; e <= E; ++e) {
    out << ' ';
    out.integer(4LL * e);
    if (e % 20 == 0 || e == E)
      out << '\n';
  }
  out << "        </DataArray>\n        <DataArray type=\"UInt8\" "
         "Name=\"types\" format=\"ascii\">\n";
  for (int e = 1; e <= E; ++e) {
    out << " 10";
    if (e % 40 == 0 || e == E)
      out << '\n';
  }
  out << "        </DataArray>\n      </Cells>\n";

  const auto open_array = [&](const char *type, const std::string &name,
                              int components) {
    out << "        <DataArray type=\"" << type << "\" Name=\""
        << xml_escape(name) << "\" NumberOfComponents=\"";
    out.integer(components);
    out << "\" format=\"ascii\">\n";
  };
  out << "      <PointData Scalars=\"nodal_density\">\n";
  open_array("Int32", "node_id", 1);
  for (int n : points) {
    out << ' ';
    out.integer(n);
    out << '\n';
  }
  out << "        </DataArray>\n";
  open_array("Float64", "nodal_density", 1);
  for (int n : points) {
    out << ' ';
    out.real(nodal[n]);
    out << '\n';
  }
  out << "        </DataArray>\n";
  for (const Field &f : fields) {
    if (f.location != FieldLocation::Node)
      continue;
    open_array("Float64", f.name, f.components);
    for (int n : points) {
      for (int k = 0; k < f.components; ++k) {
        out << ' ';
        out.real(f.values[static_cast<Eigen::Index>(n) * f.components + k]);
      }
      out << '\n';
    }
    out << "        </DataArray>\n";
  }
  out << "      </PointData>\n      <CellData Scalars=\"density\">\n";
  open_array("Float64", "density", 1);
  for (int e = 0; e < E; ++e) {
    out << ' ';
    out.real(rho[e]);
    out << '\n';
  }
  out << "        </DataArray>\n";
  open_array("UInt8", "retained", 1);
  for (int e = 0; e < E; ++e)
    out << (rho[e] >= threshold ? " 1\n" : " 0\n");
  out << "        </DataArray>\n";
  for (const Field &f : fields) {
    if (f.location != FieldLocation::Element)
      continue;
    open_array("Float64", f.name, f.components);
    for (int e = 0; e < E; ++e) {
      for (int k = 0; k < f.components; ++k) {
        out << ' ';
        out.real(f.values[static_cast<Eigen::Index>(e) * f.components + k]);
      }
      out << '\n';
    }
    out << "        </DataArray>\n";
  }
  out << "      </CellData>\n    </Piece>\n  </UnstructuredGrid>\n</VTKFile>\n";
  out.flush();
  ArtifactFile a = writer.finish("vtu_ascii");
  a.points = static_cast<std::int64_t>(points.size());
  a.cells = E;
  a.arrays = nlohmann::json::array({{{"name", "node_id"},
                                     {"location", "node"},
                                     {"unit", "-"},
                                     {"components", 1}},
                                    {{"name", "nodal_density"},
                                     {"location", "node"},
                                     {"unit", "1"},
                                     {"components", 1}},
                                    {{"name", "density"},
                                     {"location", "element"},
                                     {"unit", "1"},
                                     {"components", 1}},
                                    {{"name", "retained"},
                                     {"location", "element"},
                                     {"unit", "-"},
                                     {"components", 1}}});
  for (const Field &f : fields)
    a.arrays.push_back(
        {{"name", f.name},
         {"location", f.location == FieldLocation::Node ? "node" : "element"},
         {"unit", f.unit},
         {"components", f.components}});
  return a;
}

// ── JSON summaries
// ─────────────────────────────────────────────────────────────

nlohmann::json to_json(const ExtractionStats &s) {
  return {{"threshold", s.threshold},
          {"threshold_snap", s.threshold_snap},
          {"snapped_nodes", s.snapped_nodes},
          {"threshold_nodes", s.threshold_nodes},
          {"solid_nodes", s.solid_nodes},
          {"collapsed_polygons", s.collapsed_polygons},
          {"cancelled_sheet_triangles", s.cancelled_sheet_triangles}};
}

nlohmann::json to_json(const SurfaceEvidence &e) {
  nlohmann::json shells = nlohmann::json::array();
  for (const ShellEvidence &s : e.shell_details)
    shells.push_back({{"triangles", s.triangles},
                      {"signed_volume_m3", s.signed_volume_m3},
                      {"area_m2", s.area_m2}});
  nlohmann::json j = evidence_counts(e);
  j["state"] = to_string(e.state);
  j["issues"] = e.issues;
  j["vertices"] = e.vertices;
  j["triangles"] = e.triangles;
  j["isosurface_triangles"] = e.isosurface_triangles;
  j["boundary_triangles"] = e.boundary_triangles;
  j["facet_triangles"] = e.facet_triangles;
  j["watertight"] = e.watertight;
  j["manifold"] = e.manifold;
  j["shells"] = e.shells;
  j["bodies"] = e.bodies;
  j["cavities"] = e.cavities;
  j["unenclosed_cavities"] = e.unenclosed_cavities;
  j["intersecting_shells"] = e.intersecting_shells;
  j["inconsistent_nesting"] = e.inconsistent_nesting;
  j["zero_volume_shells"] = e.zero_volume_shells;
  j["vertex_components"] = e.vertex_components;
  j["shell_details"] = std::move(shells);
  j["signed_volume_m3"] = e.signed_volume_m3;
  j["area_m2"] = e.area_m2;
  j["min_triangle_area_m2"] = finite_or_null(e.min_triangle_area_m2);
  j["min_edge_m"] = finite_or_null(e.min_edge_m);
  j["largest_body_volume_fraction"] = e.largest_body_volume_fraction;
  return j;
}

nlohmann::json to_json(const DiscretizationComparison &c) {
  return {{"retained_volume_m3", c.retained_volume_m3},
          {"surface_volume_m3", c.surface_volume_m3},
          {"difference_m3", c.difference_m3},
          {"relative_to_retained", finite_or_null(c.relative_to_retained)},
          {"relative_to_domain", finite_or_null(c.relative_to_domain)},
          {"retained_state", to_string(c.retained_state)},
          {"surface_state", to_string(c.surface_state)}};
}

nlohmann::json to_json(const ArtifactFile &a) {
  return {{"path", a.path},
          {"format", a.format},
          {"sha256", a.sha256},
          {"bytes", a.bytes},
          {"length_unit", a.length_unit},
          {"points", a.points},
          {"triangles", a.triangles},
          {"cells", a.cells},
          {"arrays", a.arrays}};
}

nlohmann::json summary_json(const RetainedDomain &r) {
  return {
      {"threshold", r.threshold},
      {"elements", r.elements.size()},
      {"nodes", r.nodes.size()},
      {"volume_m3", r.volume_m3},
      {"domain_volume_m3", r.domain_volume_m3},
      {"volume_fraction",
       finite_or_null(r.domain_volume_m3 > 0 ? r.volume_m3 / r.domain_volume_m3
                                             : kNaN)},
      {"face_components", r.face_components},
      {"boundary", to_json(r.boundary_evidence)}};
}

nlohmann::json summary_json(const ClippedDomain &c) {
  return {{"threshold", c.threshold},
          {"threshold_snap", c.threshold_snap},
          {"vertices", c.vertex_source.size()},
          {"tets", c.tet_parent.size()},
          {"volume_m3", c.volume_m3},
          {"domain_volume_m3", c.domain_volume_m3},
          {"min_tet_volume_m3", c.min_tet_volume_m3},
          {"min_tet_shape", c.min_tet_shape},
          {"dropped_degenerate_tets", c.dropped_degenerate_tets},
          {"face_components", c.face_components},
          {"nonconforming_faces", c.nonconforming_faces},
          {"boundary_faces", c.boundary_faces},
          {"unmatched_boundary_faces", c.unmatched_boundary_faces},
          {"unmatched_surface_triangles", c.unmatched_surface_triangles},
          {"boundary_matches_surface", c.boundary_matches_surface}};
}

} // namespace topology
} // namespace sim
} // namespace chudware
