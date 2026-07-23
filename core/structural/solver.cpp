// Direct-stiffness 3D frame solver — see solver.h.

#include "structural/solver.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <map>
#include <set>
#include <stdexcept>
#include <tuple>

#include <Eigen/Dense>

namespace aicad::structural {
namespace {

using Vec3 = std::array<double, 3>;
using Mat3 = std::array<std::array<double, 3>, 3>;
using Vec12 = std::array<double, 12>;
using Mat12 = std::array<std::array<double, 12>, 12>;

// Python's round(x, 10) — round-half-even at the 10th decimal (IEEE default
// rounding mode gives ties-to-even through nearbyint).
double round10(double v) { return std::nearbyint(v * 1e10) / 1e10; }
// Python's round(x, 3) — node-merge coincidence key (1e-3 mm).
double round3(double v) { return std::nearbyint(v * 1e3) / 1e3; }

// math.isclose with the Python defaults rel_tol=1e-9, abs_tol=0.0.
bool py_isclose(double a, double b) {
    return std::abs(a - b) <= 1e-9 * std::max(std::abs(a), std::abs(b));
}

Vec3 cross(const Vec3& a, const Vec3& b) {
    return {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]};
}

Vec3 normalized(const Vec3& v) {
    const double n = std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
    return {v[0] / n, v[1] / n, v[2] / n};
}

// ---------------------------------------------------------------- loads

struct PtLoad {
    char axis;  // 'X' | 'Y' | 'Z' (global)
    double P, x;
    std::string case_name;
};

struct DistLoad {
    char axis;  // 'X' | 'Y' | 'Z' (global)
    double w1, w2, x1, x2;
    std::string case_name;
};

// ---------------------------------------------------------------- model

struct Node {
    double x, y, z;
    bool fixed = false;  // all 6 DOFs restrained (oracle v1 support model)
};

struct Member {
    std::string name;
    int i_node, j_node;
    SectionProps props;
    std::vector<PtLoad> pt_loads;
    std::vector<DistLoad> dist_loads;

    // Cached geometry
    double L = 0.0;
    Mat3 dir_cos{};  // rows: local x, y, z in global coordinates

    double length() const { return L; }
};

struct Model {
    std::vector<Node> nodes;
    std::vector<Member> members;
};

// Direction cosines — line-for-line port of Member3D.T() (rotation is always
// 0 on the oracle path: add_member is never given an aux node / roll angle).
void compute_transform(Member& m, const Node& ni, const Node& nj) {
    const double Xi = ni.x, Yi = ni.y, Zi = ni.z;
    const double Xj = nj.x, Yj = nj.y, Zj = nj.z;
    const double L = std::sqrt((Xj - Xi) * (Xj - Xi) + (Yj - Yi) * (Yj - Yi) + (Zj - Zi) * (Zj - Zi));
    m.L = L;
    const Vec3 x = {(Xj - Xi) / L, (Yj - Yi) / L, (Zj - Zi) / L};
    Vec3 y, z;
    if (py_isclose(Xi, Xj) && py_isclose(Zi, Zj)) {
        // Vertical members: keep the local y-axis in the XY plane.
        if (Yj > Yi) {
            y = {-1.0, 0.0, 0.0};
        } else {
            y = {1.0, 0.0, 0.0};
        }
        z = {0.0, 0.0, 1.0};
    } else if (py_isclose(Yi, Yj)) {
        // Horizontal members.
        y = {0.0, 1.0, 0.0};
        z = normalized(cross(x, y));
    } else {
        // Neither vertical nor horizontal.
        const Vec3 proj = {Xj - Xi, 0.0, Zj - Zi};
        if (Yj > Yi) {
            z = normalized(cross(proj, x));
        } else {
            z = normalized(cross(x, proj));
        }
        y = normalized(cross(z, x));
    }
    m.dir_cos = {x, y, z};
}

// Local 12x12 elastic stiffness — Member3D._ke_unc() (no end releases on the
// oracle path, so condensation is the identity and ke == _ke_unc).
Mat12 local_stiffness(const Member& m) {
    const double E = m.props.E, G = m.props.G;
    const double A = m.props.A, Iy = m.props.Iy, Iz = m.props.Iz, J = m.props.J;
    const double L = m.L;
    Mat12 k{};
    auto set = [&k](int r, int c, double v) { k[r][c] = v; };
    const double a1 = A * E / L;
    const double b1 = 12.0 * E * Iz / (L * L * L), b2 = 6.0 * E * Iz / (L * L);
    const double c1 = 12.0 * E * Iy / (L * L * L), c2 = 6.0 * E * Iy / (L * L);
    const double t1 = G * J / L;
    const double m1 = 4.0 * E * Iz / L, m2 = 2.0 * E * Iz / L;
    const double n1 = 4.0 * E * Iy / L, n2 = 2.0 * E * Iy / L;
    set(0, 0, a1); set(0, 6, -a1);
    set(1, 1, b1); set(1, 5, b2); set(1, 7, -b1); set(1, 11, b2);
    set(2, 2, c1); set(2, 4, -c2); set(2, 8, -c1); set(2, 10, -c2);
    set(3, 3, t1); set(3, 9, -t1);
    set(4, 2, -c2); set(4, 4, n1); set(4, 8, c2); set(4, 10, n2);
    set(5, 1, b2); set(5, 5, m1); set(5, 7, -b2); set(5, 11, m2);
    set(6, 0, -a1); set(6, 6, a1);
    set(7, 1, -b1); set(7, 5, -b2); set(7, 7, b1); set(7, 11, -b2);
    set(8, 2, -c1); set(8, 4, c2); set(8, 8, c1); set(8, 10, c2);
    set(9, 3, -t1); set(9, 9, t1);
    set(10, 2, -c2); set(10, 4, n2); set(10, 8, c2); set(10, 10, n1);
    set(11, 1, b2); set(11, 5, m2); set(11, 7, -b2); set(11, 11, m1);
    return k;
}

// ------------------------------------------------------------ fixed-end reactions
// Ports of FixedEndReactions.py. Index map: [Fx_i, Fy_i, Fz_i, Mx_i, My_i, Mz_i,
// Fx_j, Fy_j, Fz_j, Mx_j, My_j, Mz_j].

void fer_pt_load(Vec12& fer, double P, double x, double L, char dir /*'y'|'z'*/) {
    const double b = L - x;
    if (dir == 'y') {
        fer[1] += -P * b * b * (L + 2 * x) / (L * L * L);
        fer[5] += -P * x * b * b / (L * L);
        fer[7] += -P * x * x * (L + 2 * b) / (L * L * L);
        fer[11] += P * x * x * b / (L * L);
    } else {
        fer[2] += -P * b * b * (L + 2 * x) / (L * L * L);
        fer[4] += P * x * b * b / (L * L);
        fer[8] += -P * x * x * (L + 2 * b) / (L * L * L);
        fer[10] += -P * x * x * b / (L * L);
    }
}

void fer_axial_pt_load(Vec12& fer, double P, double x, double L) {
    fer[0] += -P * (L - x) / L;
    fer[6] += -P * x / L;
}

void fer_lin_load(Vec12& fer, double w1, double w2, double x1, double x2, double L, char dir) {
    const double L2 = L * L, L3 = L2 * L;
    const double x1_2 = x1 * x1, x2_2 = x2 * x2;
    const double x1_3 = x1_2 * x1, x2_3 = x2_2 * x2;
    const double r1 = (x1 - x2) * (10 * L3 * w1 + 10 * L3 * w2 - 15 * L * w1 * x1_2 - 10 * L * w1 * x1 * x2 -
                                   5 * L * w1 * x2_2 - 5 * L * w2 * x1_2 - 10 * L * w2 * x1 * x2 - 15 * L * w2 * x2_2 +
                                   8 * w1 * x1_3 + 6 * w1 * x1_2 * x2 + 4 * w1 * x1 * x2_2 + 2 * w1 * x2_3 +
                                   2 * w2 * x1_3 + 4 * w2 * x1_2 * x2 + 6 * w2 * x1 * x2_2 + 8 * w2 * x2_3) / (20 * L3);
    const double r2 = (x1 - x2) * (20 * L2 * w1 * x1 + 10 * L2 * w1 * x2 + 10 * L2 * w2 * x1 + 20 * L2 * w2 * x2 -
                                   30 * L * w1 * x1_2 - 20 * L * w1 * x1 * x2 - 10 * L * w1 * x2_2 - 10 * L * w2 * x1_2 -
                                   20 * L * w2 * x1 * x2 - 30 * L * w2 * x2_2 +
                                   12 * w1 * x1_3 + 9 * w1 * x1_2 * x2 + 6 * w1 * x1 * x2_2 + 3 * w1 * x2_3 +
                                   3 * w2 * x1_3 + 6 * w2 * x1_2 * x2 + 9 * w2 * x1 * x2_2 + 12 * w2 * x2_3) / (60 * L2);
    const double r3 = -(x1 - x2) * (-15 * L * w1 * x1_2 - 10 * L * w1 * x1 * x2 - 5 * L * w1 * x2_2 - 5 * L * w2 * x1_2 -
                                    10 * L * w2 * x1 * x2 - 15 * L * w2 * x2_2 +
                                    8 * w1 * x1_3 + 6 * w1 * x1_2 * x2 + 4 * w1 * x1 * x2_2 + 2 * w1 * x2_3 +
                                    2 * w2 * x1_3 + 4 * w2 * x1_2 * x2 + 6 * w2 * x1 * x2_2 + 8 * w2 * x2_3) / (20 * L3);
    const double r4 = (x1 - x2) * (-15 * L * w1 * x1_2 - 10 * L * w1 * x1 * x2 - 5 * L * w1 * x2_2 - 5 * L * w2 * x1_2 -
                                   10 * L * w2 * x1 * x2 - 15 * L * w2 * x2_2 +
                                   12 * w1 * x1_3 + 9 * w1 * x1_2 * x2 + 6 * w1 * x1 * x2_2 + 3 * w1 * x2_3 +
                                   3 * w2 * x1_3 + 6 * w2 * x1_2 * x2 + 9 * w2 * x1 * x2_2 + 12 * w2 * x2_3) / (60 * L2);
    if (dir == 'y') {
        fer[1] += r1; fer[5] += r2; fer[7] += r3; fer[11] += r4;
    } else {
        fer[2] += r1; fer[4] += -r2; fer[8] += r3; fer[10] += -r4;
    }
}

void fer_axial_lin_load(Vec12& fer, double p1, double p2, double x1, double x2, double L) {
    fer[0] += 1.0 / (6 * L) * (x1 - x2) * (3 * L * p1 + 3 * L * p2 - 2 * p1 * x1 - p1 * x2 - p2 * x1 - 2 * p2 * x2);
    fer[6] += 1.0 / (6 * L) * (x1 - x2) * (2 * p1 * x1 + p1 * x2 + p2 * x1 + 2 * p2 * x2);
}

// Transform a global-direction vector into local components (dirCos @ v).
Vec3 to_local(const Mat3& dc, double vx, double vy, double vz) {
    return {dc[0][0] * vx + dc[0][1] * vy + dc[0][2] * vz,
            dc[1][0] * vx + dc[1][1] * vy + dc[1][2] * vz,
            dc[2][0] * vx + dc[2][1] * vy + dc[2][2] * vz};
}

// Member3D._fer_unc(combo): uncondensed local fixed-end-reaction vector.
Vec12 fer_unc(const Member& m, const std::vector<std::pair<std::string, double>>& factors) {
    Vec12 fer{};
    for (const auto& [case_name, factor] : factors) {
        for (const PtLoad& pl : m.pt_loads) {
            if (pl.case_name != case_name) continue;
            const Vec3 f = to_local(m.dir_cos, pl.axis == 'X' ? pl.P : 0.0,
                                    pl.axis == 'Y' ? pl.P : 0.0, pl.axis == 'Z' ? pl.P : 0.0);
            fer_axial_pt_load(fer, factor * f[0], pl.x, m.L);
            fer_pt_load(fer, factor * f[1], pl.x, m.L, 'y');
            fer_pt_load(fer, factor * f[2], pl.x, m.L, 'z');
        }
        for (const DistLoad& dl : m.dist_loads) {
            if (dl.case_name != case_name) continue;
            const Vec3 w1 = to_local(m.dir_cos, dl.axis == 'X' ? dl.w1 : 0.0,
                                     dl.axis == 'Y' ? dl.w1 : 0.0, dl.axis == 'Z' ? dl.w1 : 0.0);
            const Vec3 w2 = to_local(m.dir_cos, dl.axis == 'X' ? dl.w2 : 0.0,
                                     dl.axis == 'Y' ? dl.w2 : 0.0, dl.axis == 'Z' ? dl.w2 : 0.0);
            fer_axial_lin_load(fer, factor * w1[0], factor * w2[0], dl.x1, dl.x2, m.L);
            fer_lin_load(fer, factor * w1[1], factor * w2[1], dl.x1, dl.x2, m.L, 'y');
            fer_lin_load(fer, factor * w1[2], factor * w2[2], dl.x1, dl.x2, m.L, 'z');
        }
    }
    return fer;
}

// ---------------------------------------------------------------- model build

// Mirrors analysis.build_pynite_model: one member per element (named by
// element id), node merge at 1e-3 mm, footings fixed / else lowest node fixed.
Model build_model(const StructureDoc& doc) {
    Model model;
    std::map<std::tuple<double, double, double>, int> node_index;

    auto get_node = [&](const Vec3& p) {
        const auto key = std::make_tuple(round3(p[0]), round3(p[1]), round3(p[2]));
        auto it = node_index.find(key);
        if (it != node_index.end()) return it->second;
        const int idx = static_cast<int>(model.nodes.size());
        node_index[key] = idx;
        model.nodes.push_back({p[0], p[1], p[2], false});
        return idx;
    };

    std::set<int> footing_nodes;
    for (const StructuralElement& el : doc.elements) {
        const auto [p0, p1] = element_endpoints(el);
        const auto k0 = std::make_tuple(round3(p0[0]), round3(p0[1]), round3(p0[2]));
        const auto k1 = std::make_tuple(round3(p1[0]), round3(p1[1]), round3(p1[2]));
        if (k0 == k1) {
            throw std::invalid_argument("element " + el.id + " is zero-length (length_mm=" +
                                        std::to_string(el.length_mm) + ") — no member");
        }
        Member m;
        m.name = el.id;
        m.i_node = get_node(p0);
        m.j_node = get_node(p1);
        m.props = section_properties(el.section_name, el.material, el.element_type);
        compute_transform(m, model.nodes[m.i_node], model.nodes[m.j_node]);
        if (el.element_type == "footing") {
            footing_nodes.insert(m.i_node);
            footing_nodes.insert(m.j_node);
        }
        model.members.push_back(std::move(m));
    }

    if (!footing_nodes.empty()) {
        for (int n : footing_nodes) model.nodes[n].fixed = true;
    } else if (!model.nodes.empty()) {
        // The single lowest node (min by z, then y, then x) fixed as ground.
        int lowest = 0;
        for (int i = 1; i < static_cast<int>(model.nodes.size()); ++i) {
            const Node& a = model.nodes[i];
            const Node& b = model.nodes[lowest];
            if (std::tie(a.z, a.y, a.x) < std::tie(b.z, b.y, b.x)) lowest = i;
        }
        model.nodes[lowest].fixed = true;
    }
    return model;
}

// Mirrors analysis.apply_load_cases (v1 mapping: each load case applies to
// every non-footing member, in the dominant gravity direction).
void apply_load_cases(Model& model, const std::vector<nlohmann::json>& load_cases,
                      const std::set<std::string>& footing_ids) {
    static const char* kAxes = "XYZ";
    for (const nlohmann::json& lc : load_cases) {
        // Python: lc.get("kind") or "dead" — null / missing / "" all map to "dead".
        std::string kind = "dead";
        if (lc.contains("kind") && lc["kind"].is_string() && !lc["kind"].get<std::string>().empty()) {
            kind = lc["kind"].get<std::string>();
        }
        Vec3 g = {0.0, 0.0, -1.0};
        if (lc.contains("gravity") && lc["gravity"].is_array() && lc["gravity"].size() == 3) {
            for (int i = 0; i < 3; ++i) g[i] = lc["gravity"][i].get<double>();
        }
        int idx = 0;
        for (int i = 1; i < 3; ++i) {
            if (std::abs(g[i]) > std::abs(g[idx])) idx = i;
        }
        double sign;
        if (g[idx] == 0.0) {
            idx = 2;
            sign = -1.0;
        } else {
            sign = g[idx] < 0 ? -1.0 : 1.0;
        }
        const char axis = kAxes[idx];
        const double factor = lc.value("factor", 1.0);

        const bool has_dist = lc.contains("distributed") && lc["distributed"].is_object() &&
                              lc["distributed"].contains("w_n_per_mm") &&
                              !lc["distributed"]["w_n_per_mm"].is_null();
        const bool has_pt = lc.contains("point") && lc["point"].is_object() &&
                            lc["point"].contains("p_n") && !lc["point"]["p_n"].is_null();

        for (Member& m : model.members) {
            if (footing_ids.count(m.name)) continue;
            if (has_dist) {
                const double w = lc["distributed"]["w_n_per_mm"].get<double>();
                const double mag = sign * std::abs(w) * factor;
                m.dist_loads.push_back({axis, mag, mag, 0.0, m.L, kind});
            }
            if (has_pt) {
                const double p = lc["point"]["p_n"].get<double>();
                double x = m.L;
                if (lc["point"].contains("at_mm") && !lc["point"]["at_mm"].is_null()) {
                    x = lc["point"]["at_mm"].get<double>();
                }
                const double mag = sign * std::abs(p) * factor;
                m.pt_loads.push_back({axis, mag, x, kind});
            }
        }
    }
}

// ---------------------------------------------------------------- beam segments
// Ports of BeamSegZ / BeamSegY with the fields _segment_member fills.

struct SegZ {
    double x1 = 0, x2 = 0;
    double w1 = 0, w2 = 0;   // transverse (local y) distributed load
    double p1 = 0, p2 = 0;   // axial distributed load
    double V1 = 0, M1 = 0, P1 = 0;
    double theta1 = 0, delta1 = 0, delta_x1 = 0;
    double EI = 0, EA = 0;

    double length() const { return x2 - x1; }

    double shear(double x) const {
        return V1 + w1 * x + x * x * (-w1 + w2) / (2 * length());
    }
    double moment(double x) const {
        return M1 - V1 * x - w1 * x * x / 2 - x * x * x * (-w1 + w2) / (6 * length());
    }
    double axial(double x) const {
        return P1 + (p2 - p1) / (2 * length()) * x * x + p1 * x;
    }
    double slope(double x) const {
        return theta1 - (-V1 * x * x / 2 - w1 * x * x * x / 6 + x * M1 +
                         x * x * x * x * (w1 - w2) / (24 * length())) / EI;
    }
    double deflection(double x) const {
        return delta1 + theta1 * x + V1 * x * x * x / (6 * EI) + w1 * x * x * x * x / (24 * EI) +
               x * x * (-M1) / (2 * EI) + x * x * x * x * x * (-w1 + w2) / (120 * EI * length());
    }
    double axial_deflection(double x) const {
        return delta_x1 - (P1 * x + p1 * x * x / 2 + (p2 - p1) * x * x * x / (6 * length())) / EA;
    }

    // Candidate-extremum scans — exact ports of BeamSegZ.max_shear/min_shear/
    // max_moment/min_moment.
    std::pair<double, double> shear_range() const {
        const double L = length();
        double x1 = (w1 - w2 == 0.0) ? 0.0 : w1 * L / (w1 - w2);
        if (round10(x1) < 0 || round10(x1) > round10(L)) x1 = 0.0;
        const double v1 = shear(x1), v2 = shear(0.0), v3 = shear(L);
        return {std::min({v1, v2, v3}), std::max({v1, v2, v3})};
    }
    std::pair<double, double> moment_range() const {
        const double L = length();
        const double a = -(w2 - w1) / (2 * L);
        const double b = -w1;
        const double c = -V1;
        double x1, x2;
        if (a == 0.0) {
            x1 = (b != 0.0) ? -c / b : 0.0;
            x2 = 0.0;
        } else if (b * b - 4 * a * c < 0) {
            x1 = 0.0;
            x2 = 0.0;
        } else {
            x1 = (-b + std::sqrt(b * b - 4 * a * c)) / (2 * a);
            x2 = (-b - std::sqrt(b * b - 4 * a * c)) / (2 * a);
        }
        if (round10(x1) < 0 || round10(x1) > round10(L)) x1 = 0.0;
        if (round10(x2) < 0 || round10(x2) > round10(L)) x2 = 0.0;
        const double m1 = moment(x1), m2 = moment(x2), m3 = moment(0.0), m4 = moment(L);
        return {std::min({m1, m2, m3, m4}), std::max({m1, m2, m3, m4})};
    }
};

struct SegY {
    double x1 = 0, x2 = 0;
    double w1 = 0, w2 = 0;
    double p1 = 0, p2 = 0;
    double V1 = 0, M1 = 0, P1 = 0;
    double theta1 = 0, delta1 = 0, delta_x1 = 0;
    double EI = 0, EA = 0;

    double length() const { return x2 - x1; }

    double shear(double x) const {
        return V1 + w1 * x + x * x * (-w1 + w2) / (2 * length());
    }
    double moment(double x) const {
        return -M1 - V1 * x - w1 * x * x / 2 - x * x * x * (-w1 + w2) / (6 * length());
    }
    double slope(double x) const {
        return theta1 + (-V1 * x * x / 2 - w1 * x * x * x / 6 + x * (-M1) +
                         x * x * x * x * (w1 - w2) / (24 * length())) / EI;
    }
    double deflection(double x) const {
        return delta1 - theta1 * x + V1 * x * x * x / (6 * EI) + w1 * x * x * x * x / (24 * EI) -
               x * x * (-M1) / (2 * EI) - x * x * x * x * x * (w1 - w2) / (120 * EI * length());
    }
    double axial_deflection(double x) const {
        return delta_x1 - (P1 * x + p1 * x * x / 2 + (p2 - p1) * x * x * x / (6 * length())) / EA;
    }
    std::pair<double, double> shear_range() const {
        const double L = length();
        double x1 = (w1 - w2 == 0.0) ? 0.0 : w1 * L / (w1 - w2);
        if (round10(x1) < 0 || round10(x1) > round10(L)) x1 = 0.0;
        const double v1 = shear(x1), v2 = shear(0.0), v3 = shear(L);
        return {std::min({v1, v2, v3}), std::max({v1, v2, v3})};
    }
    std::pair<double, double> moment_range() const {
        const double L = length();
        const double a = (w2 - w1) / (2 * L);
        const double b = w1;
        const double c = V1;
        double x1, x2;
        if (a == 0.0) {
            x1 = (b != 0.0) ? -c / b : 0.0;
            x2 = 0.0;
        } else if (b * b - 4 * a * c < 0) {
            x1 = 0.0;
            x2 = 0.0;
        } else {
            x1 = (-b + std::sqrt(b * b - 4 * a * c)) / (2 * a);
            x2 = (-b - std::sqrt(b * b - 4 * a * c)) / (2 * a);
        }
        if (round10(x1) < 0 || round10(x1) > round10(L)) x1 = 0.0;
        if (round10(x2) < 0 || round10(x2) > round10(L)) x2 = 0.0;
        const double m1 = moment(x1), m2 = moment(x2), m3 = moment(0.0), m4 = moment(L);
        return {std::min({m1, m2, m3, m4}), std::max({m1, m2, m3, m4})};
    }
};

struct SegmentedMember {
    std::vector<SegZ> segs_z;
    std::vector<SegY> segs_y;
};

// Member3D._segment_member(combo) — full port (global-direction loads only).
SegmentedMember segment_member(const Member& m, const Vec12& f, const Vec12& fer,
                               const Vec12& d,
                               const std::vector<std::pair<std::string, double>>& factors) {
    const double L = m.L;
    const double E = m.props.E;
    const double EIz = E * m.props.Iz, EIy = E * m.props.Iy;
    const double EA = E * m.props.A;

    std::vector<double> disconts = {0.0, L};
    for (const PtLoad& pl : m.pt_loads) disconts.push_back(pl.x);
    for (const DistLoad& dl : m.dist_loads) {
        disconts.push_back(dl.x1);
        disconts.push_back(dl.x2);
    }
    std::sort(disconts.begin(), disconts.end());
    disconts.erase(std::unique(disconts.begin(), disconts.end()), disconts.end());

    SegmentedMember sm;
    for (size_t i = 0; i + 1 < disconts.size(); ++i) {
        SegZ sz;
        sz.x1 = disconts[i];
        sz.x2 = disconts[i + 1];
        sz.EI = EIz;
        sz.EA = EA;
        sm.segs_z.push_back(sz);
        SegY sy;
        sy.x1 = disconts[i];
        sy.x2 = disconts[i + 1];
        sy.EI = EIy;
        sy.EA = EA;
        sm.segs_y.push_back(sy);
    }

    // Slope-deflection initialization (PyNite sign conventions preserved).
    const double m1z = f[5], m2z = f[11];
    const double m1y = -f[4], m2y = -f[10];
    const double fem1z = fer[5], fem2z = fer[11];
    const double fem1y = -fer[4], fem2y = -fer[10];
    sm.segs_z[0].delta1 = d[1];
    sm.segs_y[0].delta1 = d[2];
    sm.segs_z[0].theta1 = 1.0 / 3.0 * ((m1z - fem1z) * L / EIz - (m2z - fem2z) * L / (2 * EIz) +
                                       3 * (d[7] - d[1]) / L);
    sm.segs_y[0].theta1 = -1.0 / 3.0 * ((m1y - fem1y) * L / EIy - (m2y - fem2y) * L / (2 * EIy) +
                                        3 * (d[8] - d[2]) / L);
    sm.segs_z[0].delta_x1 = d[0];
    sm.segs_y[0].delta_x1 = d[0];

    for (size_t i = 0; i < sm.segs_z.size(); ++i) {
        const double x = sm.segs_z[i].x1;
        if (i > 0) {
            sm.segs_z[i].theta1 = sm.segs_z[i - 1].slope(sm.segs_z[i - 1].length());
            sm.segs_z[i].delta1 = sm.segs_z[i - 1].deflection(sm.segs_z[i - 1].length());
            sm.segs_z[i].delta_x1 = sm.segs_z[i - 1].axial_deflection(sm.segs_z[i - 1].length());
            sm.segs_y[i].theta1 = sm.segs_y[i - 1].slope(sm.segs_y[i - 1].length());
            sm.segs_y[i].delta1 = sm.segs_y[i - 1].deflection(sm.segs_y[i - 1].length());
            sm.segs_y[i].delta_x1 = sm.segs_y[i - 1].axial_deflection(sm.segs_y[i - 1].length());
        }

        // Beam end forces at the segment start.
        sm.segs_z[i].P1 = f[0];
        sm.segs_z[i].V1 = f[1];
        sm.segs_z[i].M1 = f[5] - f[1] * x;
        sm.segs_y[i].P1 = f[0];
        sm.segs_y[i].V1 = f[2];
        sm.segs_y[i].M1 = f[4] + f[2] * x;

        for (const auto& [case_name, factor] : factors) {
            // Point loads occurring prior to (or at) this segment.
            for (const PtLoad& pl : m.pt_loads) {
                if (!(round10(pl.x) <= round10(x) && case_name == pl.case_name)) continue;
                const Vec3 force = to_local(m.dir_cos, pl.axis == 'X' ? pl.P : 0.0,
                                            pl.axis == 'Y' ? pl.P : 0.0,
                                            pl.axis == 'Z' ? pl.P : 0.0);
                sm.segs_z[i].P1 += factor * force[0];
                sm.segs_z[i].V1 += factor * force[1];
                sm.segs_z[i].M1 -= factor * force[1] * (x - pl.x);
                sm.segs_y[i].V1 += factor * force[2];
                sm.segs_y[i].M1 += factor * force[2] * (x - pl.x);
            }

            // Distributed loads covering this segment.
            for (const DistLoad& dl : m.dist_loads) {
                if (dl.case_name != case_name) continue;
                double w1 = factor * dl.w1;
                double w2 = factor * dl.w2;
                const double x1 = dl.x1;
                double x2 = dl.x2;

                if (!(round10(x1) <= round10(x))) continue;

                Vec3 f1 = to_local(m.dir_cos, dl.axis == 'X' ? w1 : 0.0,
                                   dl.axis == 'Y' ? w1 : 0.0, dl.axis == 'Z' ? w1 : 0.0);
                Vec3 f2 = to_local(m.dir_cos, dl.axis == 'X' ? w2 : 0.0,
                                   dl.axis == 'Y' ? w2 : 0.0, dl.axis == 'Z' ? w2 : 0.0);

                if (round10(x2) > round10(x)) {
                    // Break the load at the segment start.
                    sm.segs_z[i].p1 += (f2[0] - f1[0]) / (x2 - x1) * (x - x1) + f1[0];
                    sm.segs_z[i].p2 += (f2[0] - f1[0]) / (x2 - x1) * (sm.segs_z[i].x2 - x1) + f1[0];
                    sm.segs_y[i].p1 += (f2[0] - f1[0]) / (x2 - x1) * (x - x1) + f1[0];
                    sm.segs_y[i].p2 += (f2[0] - f1[0]) / (x2 - x1) * (sm.segs_y[i].x2 - x1) + f1[0];
                    sm.segs_z[i].w1 += (f2[1] - f1[1]) / (x2 - x1) * (x - x1) + f1[1];
                    sm.segs_z[i].w2 += (f2[1] - f1[1]) / (x2 - x1) * (sm.segs_z[i].x2 - x1) + f1[1];
                    sm.segs_y[i].w1 += (f2[2] - f1[2]) / (x2 - x1) * (sm.segs_y[i].x1 - x1) + f1[2];
                    sm.segs_y[i].w2 += (f2[2] - f1[2]) / (x2 - x1) * (sm.segs_y[i].x2 - x1) + f1[2];

                    w2 = w1 + (w2 - w1) / (x2 - x1) * (x - x1);
                    f2 = to_local(m.dir_cos, dl.axis == 'X' ? w2 : 0.0,
                                  dl.axis == 'Y' ? w2 : 0.0, dl.axis == 'Z' ? w2 : 0.0);
                    x2 = x;
                }
                sm.segs_z[i].P1 += (f1[0] + f2[0]) / 2 * (x2 - x1);
                sm.segs_y[i].P1 += (f1[0] + f2[0]) / 2 * (x2 - x1);
                sm.segs_z[i].V1 += (f1[1] + f2[1]) / 2 * (x2 - x1);
                sm.segs_z[i].M1 -= (x1 - x2) * (2 * f1[1] * x1 - 3 * f1[1] * x + f1[1] * x2 +
                                                f2[1] * x1 - 3 * f2[1] * x + 2 * f2[1] * x2) / 6;
                sm.segs_y[i].V1 += (f1[2] + f2[2]) / 2 * (x2 - x1);
                sm.segs_y[i].M1 += (x1 - x2) * (2 * f1[2] * x1 - 3 * f1[2] * x + f1[2] * x2 +
                                                f2[2] * x1 - 3 * f2[2] * x + 2 * f2[2] * x2) / 6;
            }
        }
    }
    return sm;
}

// Segment dispatch for x — mirrors Member3D.axial()/deflection() range checks
// (round(x,10) vs segment bounds; isclose(x, L) → last segment).
template <typename Seg, typename Fn>
double seg_eval(const std::vector<Seg>& segs, double x, double L, Fn&& fn) {
    for (const Seg& s : segs) {
        if (round10(x) >= round10(s.x1) && round10(x) < round10(s.x2)) {
            return fn(s, x - s.x1);
        }
        if (py_isclose(x, L)) {
            return fn(segs.back(), x - segs.back().x1);
        }
    }
    return 0.0;  // unreachable on the oracle path (x is always 0 or L or sampled in [0, L])
}

double max_mag(std::initializer_list<double> vals) {
    double best = 0.0;
    for (double v : vals) {
        if (std::abs(v) > std::abs(best)) best = v;
    }
    return best;
}

// analysis._member_extract for one member under one combo.
AnalysisRow member_extract(const Member& m, const SegmentedMember& sm, const std::string& combo) {
    const double L = m.L;
    // axial: PyNite +compression at the member ends → negate for +tension.
    const double axial = -max_mag({seg_eval(sm.segs_z, 0.0, L, [](const SegZ& s, double xs) { return s.axial(xs); }),
                                   seg_eval(sm.segs_z, L, L, [](const SegZ& s, double xs) { return s.axial(xs); })});

    double shear_max_y = -1e300, shear_min_y = 1e300;
    double shear_max_z = -1e300, shear_min_z = 1e300;
    double mom_max_z = -1e300, mom_min_z = 1e300;
    double mom_max_y = -1e300, mom_min_y = 1e300;
    for (const SegZ& s : sm.segs_z) {
        const auto sr = s.shear_range();
        shear_max_y = std::max(shear_max_y, sr.second);
        shear_min_y = std::min(shear_min_y, sr.first);
        const auto mr = s.moment_range();
        mom_max_z = std::max(mom_max_z, mr.second);
        mom_min_z = std::min(mom_min_z, mr.first);
    }
    for (const SegY& s : sm.segs_y) {
        const auto sr = s.shear_range();
        shear_max_z = std::max(shear_max_z, sr.second);
        shear_min_z = std::min(shear_min_z, sr.first);
        const auto mr = s.moment_range();
        mom_max_y = std::max(mom_max_y, mr.second);
        mom_min_y = std::min(mom_min_y, mr.first);
    }
    const double shear = max_mag({shear_max_y, shear_min_y, shear_max_z, shear_min_z});
    const double moment = max_mag({mom_max_z, mom_min_z, mom_max_y, mom_min_y});

    // max/min deflection — PyNite samples 100 points (i/99·L, including both ends).
    double dmax_y = seg_eval(sm.segs_z, 0.0, L, [](const SegZ& s, double xs) { return s.deflection(xs); });
    double dmin_y = dmax_y;
    double dmax_z = seg_eval(sm.segs_y, 0.0, L, [](const SegY& s, double xs) { return s.deflection(xs); });
    double dmin_z = dmax_z;
    for (int i = 0; i < 100; ++i) {
        const double x = L * i / 99.0;
        const double dy = seg_eval(sm.segs_z, x, L, [](const SegZ& s, double xs) { return s.deflection(xs); });
        const double dz = seg_eval(sm.segs_y, x, L, [](const SegY& s, double xs) { return s.deflection(xs); });
        dmax_y = std::max(dmax_y, dy);
        dmin_y = std::min(dmin_y, dy);
        dmax_z = std::max(dmax_z, dz);
        dmin_z = std::min(dmin_z, dz);
    }
    const double defl = max_mag({dmax_y, dmin_y, dmax_z, dmin_z});

    return {m.name, combo, axial, shear, moment, defl};
}

}  // namespace

nlohmann::json AnalysisRow::to_dict() const {
    return {{"element_id", element_id}, {"combo", combo},       {"axial_n", axial_n},
            {"shear_n", shear_n},       {"moment_nmm", moment_nmm}, {"deflection_mm", deflection_mm},
            {"dc_ratio", nullptr},      {"pass", nullptr}};
}

std::vector<AnalysisRow> analyze_doc(const StructureDoc& doc,
                                     const std::vector<LoadCombo>& combos) {
    Model model = build_model(doc);

    std::set<std::string> footing_ids;
    for (const StructuralElement& el : doc.elements) {
        if (el.element_type == "footing") footing_ids.insert(el.id);
    }
    apply_load_cases(model, doc.load_cases, footing_ids);

    // DOF numbering: node insertion order (PyNite _renumber). Fixed support
    // DOFs are partitioned out (D2 = 0 with the v1 all-fixed support model).
    const int n_nodes = static_cast<int>(model.nodes.size());
    std::vector<int> dof_map(6 * n_nodes, -1);
    int n_free = 0;
    for (int n = 0; n < n_nodes; ++n) {
        if (model.nodes[n].fixed) continue;
        for (int d = 0; d < 6; ++d) dof_map[6 * n + d] = n_free++;
    }

    // Assemble the free-free global stiffness once (member elastic stiffness is
    // combo-independent).
    Eigen::MatrixXd K = Eigen::MatrixXd::Zero(n_free, n_free);
    std::vector<Mat12> k_locals;
    k_locals.reserve(model.members.size());
    for (const Member& m : model.members) {
        const Mat12 kl = local_stiffness(m);
        k_locals.push_back(kl);
        const int nodes[2] = {m.i_node, m.j_node};
        // Ke_global = T^T · ke · T (T block-diagonal with dirCos).
        for (int bi = 0; bi < 2; ++bi) {
            for (int bj = 0; bj < 2; ++bj) {
                for (int r = 0; r < 6; ++r) {
                    const int gr = 6 * nodes[bi] + r;
                    if (dof_map[gr] < 0) continue;
                    for (int c = 0; c < 6; ++c) {
                        const int gc = 6 * nodes[bj] + c;
                        if (dof_map[gc] < 0) continue;
                        // (T^T ke T)[6bi+r][6bj+c] with T block-diagonal:
                        // T[p][q] = dirCos[p%3][q%3] when p/3 == q/3, else 0 —
                        // so T[a][6bi+r] = dirCos[a%3][r%3] (transpose on the
                        // outer index).
                        double sum = 0.0;
                        for (int a = 0; a < 6; ++a) {
                            if (a / 3 != r / 3) continue;
                            const double Tar = m.dir_cos[a % 3][r % 3];
                            for (int b = 0; b < 6; ++b) {
                                if (b / 3 != c / 3) continue;
                                const double Tbc = m.dir_cos[b % 3][c % 3];
                                sum += Tar * kl[6 * bi + a][6 * bj + b] * Tbc;
                            }
                        }
                        K(dof_map[gr], dof_map[gc]) += sum;
                    }
                }
            }
        }
    }

    std::vector<AnalysisRow> results;
    for (const LoadCombo& combo : combos) {
        auto factors = combo.factors();
        if (factors.empty()) factors.emplace_back("dead", 1.0);

        // Global fixed-end-reaction load vector: P1 - FER1 (P1 = 0 — no nodal
        // loads on the oracle path). FER_global = T^T · fer_local.
        Eigen::VectorXd rhs = Eigen::VectorXd::Zero(n_free);
        std::vector<Vec12> fer_locals;
        fer_locals.reserve(model.members.size());
        for (const Member& m : model.members) {
            const Vec12 fl = fer_unc(m, factors);
            fer_locals.push_back(fl);
            const int nodes[2] = {m.i_node, m.j_node};
            for (int bi = 0; bi < 2; ++bi) {
                for (int r = 0; r < 6; ++r) {
                    const int gr = 6 * nodes[bi] + r;
                    if (dof_map[gr] < 0) continue;
                    double sum = 0.0;
                    for (int a = 0; a < 6; ++a) {
                        if (a / 3 != r / 3) continue;
                        // (T^T fer)[6bi+r] = Σ_a T[a][6bi+r]·fer[6bi+a]
                        sum += m.dir_cos[a % 3][r % 3] * fl[6 * bi + a];
                    }
                    rhs(dof_map[gr]) -= sum;
                }
            }
        }

        Eigen::VectorXd D1 = Eigen::VectorXd::Zero(n_free);
        if (n_free > 0) {
            Eigen::LDLT<Eigen::MatrixXd> ldlt(K);
            D1 = ldlt.solve(rhs);
            // Singularity / instability check (PyNite raises ValueError).
            if (!D1.allFinite() || (K * D1 - rhs).norm() > 1e-6 * (1.0 + rhs.norm())) {
                throw std::runtime_error(
                    "The stiffness matrix is singular, which indicates that the structure is unstable.");
            }
        }

        for (size_t mi = 0; mi < model.members.size(); ++mi) {
            const Member& m = model.members[mi];
            // Member global end displacements → local: d = T · D.
            Vec12 d{};
            const int nodes[2] = {m.i_node, m.j_node};
            for (int bi = 0; bi < 2; ++bi) {
                for (int r = 0; r < 6; ++r) {
                    // d_local[6bi+r] = Σ_a T[r][a]·D_global[6bi+a], T block-diagonal dirCos.
                    double sum = 0.0;
                    for (int a = 0; a < 6; ++a) {
                        if (a / 3 != r / 3) continue;
                        const int ga = 6 * nodes[bi] + a;
                        const double Da = dof_map[ga] < 0 ? 0.0 : D1(dof_map[ga]);
                        sum += m.dir_cos[r % 3][a % 3] * Da;
                    }
                    d[6 * bi + r] = sum;
                }
            }
            // f = ke · d + fer.
            Vec12 f{};
            for (int r = 0; r < 12; ++r) {
                double sum = 0.0;
                for (int c = 0; c < 12; ++c) sum += k_locals[mi][r][c] * d[c];
                f[r] = sum + fer_locals[mi][r];
            }
            const SegmentedMember sm = segment_member(m, f, fer_locals[mi], d, factors);
            results.push_back(member_extract(m, sm, combo.name));
        }
    }
    return results;
}

}  // namespace aicad::structural
