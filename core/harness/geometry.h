// core/harness/geometry.h — vector maths, exact triangle distance queries and a
// triangle BVH shared by the harness router (harness_route) and the path checker
// (harness_path_check). Pure C++17, OCCT-free: the document glue converts the
// tessellated solids of a document into a `Scene` of world-space triangle soups
// (core/api/harness_rpc.cpp) and everything here works on that.
#pragma once

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

namespace chudware::harness {

constexpr double kInf = std::numeric_limits<double>::infinity();
constexpr double kPi = 3.14159265358979323846;

struct V3 {
    double x = 0.0, y = 0.0, z = 0.0;
    V3() = default;
    V3(double a, double b, double c) : x(a), y(b), z(c) {}
    double operator[](int i) const { return i == 0 ? x : (i == 1 ? y : z); }
    double& operator[](int i) { return i == 0 ? x : (i == 1 ? y : z); }
};

inline V3 operator+(const V3& a, const V3& b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline V3 operator-(const V3& a, const V3& b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline V3 operator*(const V3& a, double s) { return {a.x * s, a.y * s, a.z * s}; }
inline V3 operator*(double s, const V3& a) { return {a.x * s, a.y * s, a.z * s}; }
inline V3 operator-(const V3& a) { return {-a.x, -a.y, -a.z}; }
inline double dot(const V3& a, const V3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline V3 cross(const V3& a, const V3& b) {
    return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
inline double norm2(const V3& a) { return dot(a, a); }
inline double norm(const V3& a) { return std::sqrt(dot(a, a)); }
inline double dist(const V3& a, const V3& b) { return norm(a - b); }
inline V3 lerp(const V3& a, const V3& b, double t) { return a + (b - a) * t; }
// Unit vector; the zero vector for (near-)zero input.
inline V3 unit(const V3& a) {
    const double n = norm(a);
    return n > 1e-300 ? a * (1.0 / n) : V3();
}
inline bool finite(const V3& a) {
    return std::isfinite(a.x) && std::isfinite(a.y) && std::isfinite(a.z);
}

// ---- triangle / segment distance (exact) ---------------------------------------------
// Closest point of triangle (a,b,c) to p (Ericson, Real-Time Collision Detection 5.1.5).
V3 closest_point_on_triangle(const V3& p, const V3& a, const V3& b, const V3& c);
double point_tri_dist2(const V3& p, const V3& a, const V3& b, const V3& c);
// Closest points between segments p1q1 and p2q2 (Ericson 5.1.9); returns squared distance.
double seg_seg_closest(const V3& p1, const V3& q1, const V3& p2, const V3& q2, V3* c1, V3* c2);
// True when segment pq crosses triangle (a,b,c) at a single point (parallel/coplanar
// contact is reported through the edge distances of seg_tri_closest instead).
bool seg_tri_intersect(const V3& p, const V3& q, const V3& a, const V3& b, const V3& c,
                       V3* hit);
// Squared distance between segment pq and triangle abc (0 on crossing); on_seg/on_tri
// (optional) receive the closest pair.
double seg_tri_closest(const V3& p, const V3& q, const V3& a, const V3& b, const V3& c,
                       V3* on_seg, V3* on_tri);

// ---- scene ---------------------------------------------------------------------------
// One solid body of the document in WORLD coordinates (mm).
struct SceneBody {
    int id = 0;                    // document body id (doc_tree / asm_world_bodies body_id)
    std::string name;
    std::string component;         // assembly component id ("" for part bodies)
    std::vector<float> pos;        // 3 * n_vertices
    std::vector<uint32_t> idx;     // 3 * n_triangles
};
struct Scene {
    std::vector<SceneBody> bodies;
};

// ---- triangle BVH ----------------------------------------------------------------------
struct TriF {
    float v[9];     // a.xyz b.xyz c.xyz
    int owner = 0;  // obstacle owner index (body / keepout)
};

class TriBvh {
public:
    struct Nearest {
        double dist = kInf;
        int owner = -1;
        V3 on_query;  // closest point on the query point/segment
        V3 on_tri;
    };

    void build(std::vector<TriF> tris);
    bool empty() const { return tris_.empty(); }
    size_t size() const { return tris_.size(); }
    V3 lo() const { return lo_; }
    V3 hi() const { return hi_; }

    // True when some triangle is closer to p than thr[owner]. thr_max bounds thr[] from above.
    bool any_within_point(const V3& p, const std::vector<double>& thr, double thr_max) const;
    // True when some triangle is closer to segment ab than thr[owner].
    bool any_within_segment(const V3& a, const V3& b, const std::vector<double>& thr,
                            double thr_max) const;
    // Nearest triangle to p / segment ab strictly closer than `limit` (dist = kInf when none).
    Nearest nearest_point(const V3& p, double limit) const;
    Nearest nearest_segment(const V3& a, const V3& b, double limit) const;
    // First owner whose clearance threshold is exceeded, with the closest pair
    // on its triangle and the queried segment; owner = -1 when none.
    Nearest first_within_segment(const V3& a, const V3& b,
                                 const std::vector<double>& thr, double thr_max) const;
    // Owner index of the first body (lowest index) that strictly contains p (ray parity),
    // or -1. Only meaningful for closed meshes.
    int first_containing_owner(const V3& p, int n_owners) const;

private:
    struct Node {
        float lo[3];
        float hi[3];
        int32_t left = -1;    // internal: children left/right; leaf: left = -1
        int32_t right = -1;
        int32_t first = 0;    // leaf: first triangle index
        int32_t count = 0;    // leaf: triangle count (0 for internal)
    };
    int32_t build_range(std::vector<uint32_t>& ids, size_t lo, size_t hi,
                        const std::vector<float>& cen);

    std::vector<TriF> tris_;
    std::vector<Node> nodes_;
    V3 lo_, hi_;
};

}  // namespace chudware::harness
