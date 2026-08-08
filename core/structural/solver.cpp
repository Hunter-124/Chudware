// Direct-stiffness 3D frame solver — see solver.h.

#include "structural/solver.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <map>
#include <set>
#include <stdexcept>
#include <tuple>
#include <utility>

#include <Eigen/Dense>

namespace chudware::structural {
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

std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return value;
}

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
    std::array<bool, 6> restrained{};
};

struct Member {
    std::string name;
    int i_node, j_node;
    SectionProps props;
    std::array<bool, 12> releases{};
    std::vector<PtLoad> pt_loads;
    std::vector<DistLoad> dist_loads;

    double L = 0.0;
    Mat3 dir_cos{};  // rows: local member x/y/z in global coordinates

    double length() const { return L; }
};

struct Model {
    std::vector<Node> nodes;
    std::vector<Member> members;
};

// The document transform defines section roll: its local +Z column is the
// member axis and its local +Y column is the section major-depth direction.
// Solver local x/y/z therefore map to document +Z/+Y/-X respectively.
void compute_transform(Member& m, const StructuralElement& el, const Node& ni, const Node& nj) {
    const Vec3 delta = {nj.x - ni.x, nj.y - ni.y, nj.z - ni.z};
    m.L = std::sqrt(delta[0] * delta[0] + delta[1] * delta[1] + delta[2] * delta[2]);
    if (!std::isfinite(m.L) || m.L <= 0.0) {
        throw std::invalid_argument("element '" + el.id + "' has invalid transformed length");
    }
    const Vec3 x = normalized(delta);
    Vec3 y = normalized({el.transform[1], el.transform[5], el.transform[9]});
    double dot = x[0] * y[0] + x[1] * y[1] + x[2] * y[2];
    y = normalized({y[0] - dot * x[0], y[1] - dot * x[1], y[2] - dot * x[2]});
    const Vec3 z = normalized(cross(x, y));
    m.dir_cos = {x, y, z};
}

// Unreleased local 12x12 elastic stiffness. Requested member-end releases are
// statically condensed immediately after this matrix is formed.
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

std::vector<int> released_dofs(const Member& m) {
    std::vector<int> out;
    for (int i = 0; i < 12; ++i) {
        if (m.releases[i]) out.push_back(i);
    }
    return out;
}

Mat12 condense_stiffness(const Member& m, const Mat12& unc) {
    const std::vector<int> q = released_dofs(m);
    if (q.empty()) return unc;
    std::vector<int> r;
    for (int i = 0; i < 12; ++i) {
        if (!m.releases[i]) r.push_back(i);
    }
    Mat12 condensed{};
    if (r.empty()) return condensed;

    Eigen::MatrixXd Kqq(q.size(), q.size());
    Eigen::MatrixXd Kqr(q.size(), r.size());
    Eigen::MatrixXd Krq(r.size(), q.size());
    Eigen::MatrixXd Krr(r.size(), r.size());
    for (size_t i = 0; i < q.size(); ++i) {
        for (size_t j = 0; j < q.size(); ++j) Kqq(i, j) = unc[q[i]][q[j]];
        for (size_t j = 0; j < r.size(); ++j) Kqr(i, j) = unc[q[i]][r[j]];
    }
    for (size_t i = 0; i < r.size(); ++i) {
        for (size_t j = 0; j < q.size(); ++j) Krq(i, j) = unc[r[i]][q[j]];
        for (size_t j = 0; j < r.size(); ++j) Krr(i, j) = unc[r[i]][r[j]];
    }
    const Eigen::CompleteOrthogonalDecomposition<Eigen::MatrixXd> cod(Kqq);
    const Eigen::MatrixXd X = cod.solve(Kqr);
    if (!(Kqq * X - Kqr).allFinite() ||
        (Kqq * X - Kqr).norm() > 1e-9 * (1.0 + Kqr.norm())) {
        throw std::invalid_argument("member '" + m.name +
                                    "' releases cannot be statically condensed");
    }
    const Eigen::MatrixXd Kc = Krr - Krq * X;
    for (size_t i = 0; i < r.size(); ++i) {
        for (size_t j = 0; j < r.size(); ++j) condensed[r[i]][r[j]] = Kc(i, j);
    }
    return condensed;
}

Vec12 condense_fer(const Member& m, const Mat12& unc, const Vec12& fer_unc_vec) {
    const std::vector<int> q = released_dofs(m);
    if (q.empty()) return fer_unc_vec;
    std::vector<int> r;
    for (int i = 0; i < 12; ++i) {
        if (!m.releases[i]) r.push_back(i);
    }
    Vec12 condensed{};
    if (r.empty()) return condensed;

    Eigen::MatrixXd Kqq(q.size(), q.size());
    Eigen::MatrixXd Krq(r.size(), q.size());
    Eigen::VectorXd fq(q.size());
    for (size_t i = 0; i < q.size(); ++i) {
        fq(i) = fer_unc_vec[q[i]];
        for (size_t j = 0; j < q.size(); ++j) Kqq(i, j) = unc[q[i]][q[j]];
    }
    for (size_t i = 0; i < r.size(); ++i) {
        for (size_t j = 0; j < q.size(); ++j) Krq(i, j) = unc[r[i]][q[j]];
    }
    const Eigen::CompleteOrthogonalDecomposition<Eigen::MatrixXd> cod(Kqq);
    const Eigen::VectorXd x = cod.solve(fq);
    if (!(Kqq * x - fq).allFinite() ||
        (Kqq * x - fq).norm() > 1e-9 * (1.0 + fq.norm())) {
        throw std::invalid_argument("loads on member '" + m.name +
                                    "' act on an unsupported released DOF");
    }
    for (size_t i = 0; i < r.size(); ++i) {
        condensed[r[i]] = fer_unc_vec[r[i]] - Krq.row(i).dot(x);
    }
    return condensed;
}

void recover_released_displacements(const Member& m, const Mat12& unc,
                                    const Vec12& fer_unc_vec, Vec12& d) {
    const std::vector<int> q = released_dofs(m);
    if (q.empty()) return;
    std::vector<int> r;
    for (int i = 0; i < 12; ++i) {
        if (!m.releases[i]) r.push_back(i);
    }
    Eigen::MatrixXd Kqq(q.size(), q.size());
    Eigen::MatrixXd Kqr(q.size(), r.size());
    Eigen::VectorXd fq(q.size());
    Eigen::VectorXd dr(r.size());
    for (size_t i = 0; i < q.size(); ++i) {
        fq(i) = fer_unc_vec[q[i]];
        for (size_t j = 0; j < q.size(); ++j) Kqq(i, j) = unc[q[i]][q[j]];
        for (size_t j = 0; j < r.size(); ++j) Kqr(i, j) = unc[q[i]][r[j]];
    }
    for (size_t i = 0; i < r.size(); ++i) dr(i) = d[r[i]];
    const Eigen::VectorXd rhs = -(Kqr * dr + fq);
    const Eigen::CompleteOrthogonalDecomposition<Eigen::MatrixXd> cod(Kqq);
    const Eigen::VectorXd dq = cod.solve(rhs);
    if (!dq.allFinite() ||
        (Kqq * dq - rhs).norm() > 1e-9 * (1.0 + rhs.norm())) {
        throw std::runtime_error("released displacement recovery failed for member '" +
                                 m.name + "'");
    }
    for (size_t i = 0; i < q.size(); ++i) d[q[i]] = dq(i);
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

// One member per element, node merge at 1e-3 mm. Explicit supports use global
// DOFs at an element end; legacy documents retain footing/lowest-node support
// inference only when no explicit support list is present.
Model build_model(const StructureDoc& doc) {
    Model model;
    std::map<std::tuple<double, double, double>, int> node_index;
    std::map<std::string, size_t> member_index;

    auto get_node = [&](const Vec3& p) {
        const auto key = std::make_tuple(round3(p[0]), round3(p[1]), round3(p[2]));
        auto it = node_index.find(key);
        if (it != node_index.end()) return it->second;
        const int idx = static_cast<int>(model.nodes.size());
        node_index[key] = idx;
        model.nodes.push_back({p[0], p[1], p[2], {}});
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
        m.releases = el.end_releases;
        compute_transform(m, el, model.nodes[m.i_node], model.nodes[m.j_node]);
        if (el.element_type == "footing") {
            footing_nodes.insert(m.i_node);
            footing_nodes.insert(m.j_node);
        }
        member_index[m.name] = model.members.size();
        model.members.push_back(std::move(m));
    }

    if (!doc.supports.empty()) {
        for (const StructuralSupport& support : doc.supports) {
            const auto found = member_index.find(support.element_id);
            if (found == member_index.end()) {
                throw std::invalid_argument("support references unknown element '" +
                                            support.element_id + "'");
            }
            const Member& member = model.members[found->second];
            Node& node = model.nodes[support.at_j ? member.j_node : member.i_node];
            for (int d = 0; d < 6; ++d) node.restrained[d] = node.restrained[d] || support.restrained[d];
        }
    } else if (!footing_nodes.empty()) {
        for (int n : footing_nodes) model.nodes[n].restrained.fill(true);
    } else {
        int lowest = 0;
        for (int i = 1; i < static_cast<int>(model.nodes.size()); ++i) {
            const Node& a = model.nodes[i];
            const Node& b = model.nodes[lowest];
            if (std::tie(a.z, a.y, a.x) < std::tie(b.z, b.y, b.x)) lowest = i;
        }
        model.nodes[lowest].restrained.fill(true);
    }
    return model;
}

// Each contract load case applies to every non-footing member. `gravity` is a
// direction vector, so preserve all components and normalize exactly once.
void apply_load_cases(Model& model, const std::vector<nlohmann::json>& load_cases,
                      const std::set<std::string>& footing_ids) {
    static const char* kAxes = "XYZ";
    for (const nlohmann::json& lc : load_cases) {
        std::string kind = lower(lc.value("kind", std::string("dead")));
        if (kind != "dead" && kind != "live" && kind != "wind" && kind != "snow") {
            throw std::invalid_argument("unsupported load-case kind '" + kind + "'");
        }
        Vec3 g = {0.0, 0.0, -1.0};
        if (lc.contains("gravity")) {
            if (!lc["gravity"].is_array() || lc["gravity"].size() != 3) {
                throw std::invalid_argument("load-case gravity must contain exactly three numbers");
            }
            for (int i = 0; i < 3; ++i) {
                if (!lc["gravity"][i].is_number()) {
                    throw std::invalid_argument("load-case gravity entries must be numeric");
                }
                g[i] = lc["gravity"][i].get<double>();
            }
        }
        const double gnorm =
            std::sqrt(g[0] * g[0] + g[1] * g[1] + g[2] * g[2]);
        if (!std::isfinite(gnorm) || gnorm <= 0.0) {
            throw std::invalid_argument("load-case gravity must be a finite nonzero direction");
        }
        for (double& component : g) component /= gnorm;

        const double factor = lc.value("factor", 1.0);
        if (!std::isfinite(factor) || factor < 0.0) {
            throw std::invalid_argument("load-case factor must be finite and nonnegative");
        }
        const bool has_dist = lc.contains("distributed");
        const bool has_pt = lc.contains("point");
        if (!has_dist && !has_pt) {
            throw std::invalid_argument("load case '" + kind +
                                        "' must define a distributed or point load");
        }
        double w = 0.0, p = 0.0;
        if (has_dist) {
            if (!lc["distributed"].is_object() ||
                !lc["distributed"].contains("w_n_per_mm") ||
                !lc["distributed"]["w_n_per_mm"].is_number()) {
                throw std::invalid_argument("distributed load requires numeric w_n_per_mm");
            }
            w = lc["distributed"]["w_n_per_mm"].get<double>();
            if (!std::isfinite(w) || w < 0.0) {
                throw std::invalid_argument("distributed w_n_per_mm must be finite and nonnegative");
            }
        }
        if (has_pt) {
            if (!lc["point"].is_object() || !lc["point"].contains("p_n") ||
                !lc["point"]["p_n"].is_number()) {
                throw std::invalid_argument("point load requires numeric p_n");
            }
            p = lc["point"]["p_n"].get<double>();
            if (!std::isfinite(p) || p < 0.0) {
                throw std::invalid_argument("point p_n must be finite and nonnegative");
            }
        }

        for (Member& m : model.members) {
            if (footing_ids.count(m.name)) continue;
            double x = m.L;
            if (has_pt && lc["point"].contains("at_mm")) {
                if (!lc["point"]["at_mm"].is_number()) {
                    throw std::invalid_argument("point at_mm must be numeric");
                }
                x = lc["point"]["at_mm"].get<double>();
                if (!std::isfinite(x) || x < 0.0 || x > m.L) {
                    throw std::invalid_argument("point at_mm lies outside member '" + m.name + "'");
                }
            }
            for (int axis = 0; axis < 3; ++axis) {
                if (g[axis] == 0.0) continue;
                if (has_dist && w * factor != 0.0) {
                    const double mag = g[axis] * w * factor;
                    m.dist_loads.push_back({kAxes[axis], mag, mag, 0.0, m.L, kind});
                }
                if (has_pt && p * factor != 0.0) {
                    const double mag = g[axis] * p * factor;
                    m.pt_loads.push_back({kAxes[axis], mag, x, kind});
                }
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
    const double shear_y = max_mag({shear_max_y, shear_min_y});
    const double shear_z = max_mag({shear_max_z, shear_min_z});
    const double moment_z = max_mag({mom_max_z, mom_min_z});
    const double moment_y = max_mag({mom_max_y, mom_min_y});
    const double shear = max_mag({shear_y, shear_z});
    const double moment = max_mag({moment_z, moment_y});

    // max/min deflection — sampled at 100 stations, including both ends.
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
    const double defl_y = max_mag({dmax_y, dmin_y});
    const double defl_z = max_mag({dmax_z, dmin_z});
    const double defl = max_mag({defl_y, defl_z});

    return {m.name, combo, axial, shear, moment, defl,
            shear_y, shear_z, moment_y, moment_z, defl_y, defl_z};
}

}  // namespace

nlohmann::json AnalysisRow::to_dict() const {
    return {{"element_id", element_id},
            {"combo", combo},
            {"axial_n", axial_n},
            {"shear_n", shear_n},
            {"moment_nmm", moment_nmm},
            {"deflection_mm", deflection_mm},
            {"shear_y_n", shear_y_n},
            {"shear_z_n", shear_z_n},
            {"moment_y_nmm", moment_y_nmm},
            {"moment_z_nmm", moment_z_nmm},
            {"deflection_y_mm", deflection_y_mm},
            {"deflection_z_mm", deflection_z_mm},
            {"dc_ratio", nullptr},
            {"pass", nullptr}};
}

nlohmann::json ReactionRow::to_dict() const {
    return {{"combo", combo},
            {"x_mm", x_mm},
            {"y_mm", y_mm},
            {"z_mm", z_mm},
            {"fx_n", fx_n},
            {"fy_n", fy_n},
            {"fz_n", fz_n},
            {"mx_nmm", mx_nmm},
            {"my_nmm", my_nmm},
            {"mz_nmm", mz_nmm}};
}

std::vector<AnalysisRow> analyze_doc(
    const StructureDoc& doc, const std::vector<LoadCombo>& combos,
    std::vector<ReactionRow>* reactions) {
    if (combos.empty()) throw std::invalid_argument("at least one load combination is required");
    if (reactions) reactions->clear();
    Model model = build_model(doc);

    std::set<std::string> footing_ids;
    for (const StructuralElement& el : doc.elements) {
        if (el.element_type == "footing") footing_ids.insert(el.id);
    }
    apply_load_cases(model, doc.load_cases, footing_ids);

    const int n_nodes = static_cast<int>(model.nodes.size());
    std::vector<int> dof_map(6 * n_nodes, -1);
    int n_free = 0;
    for (int n = 0; n < n_nodes; ++n) {
        for (int d = 0; d < 6; ++d) {
            if (!model.nodes[n].restrained[d]) dof_map[6 * n + d] = n_free++;
        }
    }

    Eigen::MatrixXd K = Eigen::MatrixXd::Zero(n_free, n_free);
    std::vector<Mat12> k_unc_locals;
    std::vector<Mat12> k_locals;
    k_unc_locals.reserve(model.members.size());
    k_locals.reserve(model.members.size());
    for (const Member& m : model.members) {
        const Mat12 k_unc = local_stiffness(m);
        const Mat12 kl = condense_stiffness(m, k_unc);
        k_unc_locals.push_back(k_unc);
        k_locals.push_back(kl);
        const int nodes[2] = {m.i_node, m.j_node};
        for (int bi = 0; bi < 2; ++bi) {
            for (int bj = 0; bj < 2; ++bj) {
                for (int r = 0; r < 6; ++r) {
                    const int gr = 6 * nodes[bi] + r;
                    if (dof_map[gr] < 0) continue;
                    for (int c = 0; c < 6; ++c) {
                        const int gc = 6 * nodes[bj] + c;
                        if (dof_map[gc] < 0) continue;
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
    if (!K.allFinite()) throw std::runtime_error("assembled stiffness matrix is non-finite");

    // Released node rotations can be deliberately inactive. Remove only rows
    // with no assembled stiffness; rank-test every participating DOF so a
    // zero-load mechanism cannot hide behind a zero residual.
    std::vector<int> active;
    const double k_scale = n_free > 0 ? K.cwiseAbs().maxCoeff() : 0.0;
    const double inactive_tol = std::max(1e-12, k_scale * 1e-15);
    for (int i = 0; i < n_free; ++i) {
        if (K.row(i).cwiseAbs().maxCoeff() > inactive_tol) active.push_back(i);
    }
    Eigen::MatrixXd K_active(active.size(), active.size());
    for (size_t i = 0; i < active.size(); ++i) {
        for (size_t j = 0; j < active.size(); ++j) K_active(i, j) = K(active[i], active[j]);
    }
    Eigen::FullPivLU<Eigen::MatrixXd> lu(K_active);
    if (!active.empty() && lu.rank() < static_cast<Eigen::Index>(active.size())) {
        throw std::runtime_error(
            "The stiffness matrix is singular, which indicates that the structure is unstable.");
    }

    std::vector<AnalysisRow> results;
    for (const LoadCombo& combo : combos) {
        std::vector<std::array<double, 6>> nodal_reactions(n_nodes);
        const auto factors = combo.factors();
        Eigen::VectorXd rhs = Eigen::VectorXd::Zero(n_free);
        std::vector<Vec12> fer_unc_locals;
        std::vector<Vec12> fer_locals;
        fer_unc_locals.reserve(model.members.size());
        fer_locals.reserve(model.members.size());
        double applied_load_measure = 0.0;
        for (size_t mi = 0; mi < model.members.size(); ++mi) {
            const Member& m = model.members[mi];
            const Vec12 f_unc = fer_unc(m, factors);
            for (double value : f_unc) applied_load_measure += std::abs(value);
            fer_unc_locals.push_back(f_unc);
            fer_locals.push_back(condense_fer(m, k_unc_locals[mi], f_unc));
            const Vec12& fl = fer_locals.back();
            const int nodes[2] = {m.i_node, m.j_node};
            for (int bi = 0; bi < 2; ++bi) {
                for (int r = 0; r < 6; ++r) {
                    const int gr = 6 * nodes[bi] + r;
                    if (dof_map[gr] < 0) continue;
                    double sum = 0.0;
                    for (int a = 0; a < 6; ++a) {
                        if (a / 3 != r / 3) continue;
                        sum += m.dir_cos[a % 3][r % 3] * fl[6 * bi + a];
                    }
                    rhs(dof_map[gr]) -= sum;
                }
            }
        }
        if (!std::isfinite(applied_load_measure) || applied_load_measure == 0.0) {
            throw std::invalid_argument("load combination '" + combo.name +
                                        "' has no nonzero applicable loads");
        }

        std::vector<bool> is_active(n_free, false);
        for (int i : active) is_active[i] = true;
        for (int i = 0; i < n_free; ++i) {
            if (!is_active[i] && std::abs(rhs(i)) > 1e-9 * (1.0 + rhs.norm())) {
                throw std::runtime_error(
                    "A load acts on an unrestrained DOF with no member stiffness.");
            }
        }
        Eigen::VectorXd rhs_active(active.size());
        for (size_t i = 0; i < active.size(); ++i) rhs_active(i) = rhs(active[i]);
        Eigen::VectorXd D1 = Eigen::VectorXd::Zero(n_free);
        if (!active.empty()) {
            const Eigen::VectorXd solved = lu.solve(rhs_active);
            if (!solved.allFinite() ||
                (K_active * solved - rhs_active).norm() >
                    1e-8 * (1.0 + rhs_active.norm())) {
                throw std::runtime_error(
                    "The stiffness solve did not converge to equilibrium.");
            }
            for (size_t i = 0; i < active.size(); ++i) D1(active[i]) = solved(i);
        }

        for (size_t mi = 0; mi < model.members.size(); ++mi) {
            const Member& m = model.members[mi];
            Vec12 d{};
            const int nodes[2] = {m.i_node, m.j_node};
            for (int bi = 0; bi < 2; ++bi) {
                for (int r = 0; r < 6; ++r) {
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
            recover_released_displacements(m, k_unc_locals[mi], fer_unc_locals[mi], d);
            Vec12 f{};
            for (int r = 0; r < 12; ++r) {
                double sum = 0.0;
                for (int c = 0; c < 12; ++c) sum += k_unc_locals[mi][r][c] * d[c];
                f[r] = sum + fer_unc_locals[mi][r];
            }
            for (int bi = 0; bi < 2; ++bi) {
                const int node = nodes[bi];
                for (int global_dof = 0; global_dof < 6; ++global_dof) {
                    double value = 0.0;
                    for (int local_dof = 0; local_dof < 6; ++local_dof) {
                        if (local_dof / 3 != global_dof / 3) continue;
                        value += m.dir_cos[local_dof % 3][global_dof % 3] *
                                 f[6 * bi + local_dof];
                    }
                    nodal_reactions[node][global_dof] += value;
                }
            }
            const SegmentedMember sm =
                segment_member(m, f, fer_unc_locals[mi], d, factors);
            AnalysisRow row = member_extract(m, sm, combo.name);
            const nlohmann::json serialized = row.to_dict();
            for (const char* key : {"axial_n", "shear_n", "moment_nmm", "deflection_mm",
                                    "shear_y_n", "shear_z_n", "moment_y_nmm",
                                    "moment_z_nmm", "deflection_y_mm", "deflection_z_mm"}) {
                if (!std::isfinite(serialized[key].get<double>())) {
                    throw std::runtime_error("non-finite structural result for member '" + m.name +
                                             "'");
                }
            }
            results.push_back(std::move(row));
        }
        if (reactions) {
            for (int node_index = 0; node_index < n_nodes; ++node_index) {
                const Node& node = model.nodes[node_index];
                if (std::none_of(node.restrained.begin(), node.restrained.end(),
                                 [](bool value) { return value; })) {
                    continue;
                }
                const auto& force = nodal_reactions[node_index];
                ReactionRow reaction{combo.name, node.x, node.y, node.z};
                reaction.fx_n = node.restrained[0] ? force[0] : 0.0;
                reaction.fy_n = node.restrained[1] ? force[1] : 0.0;
                reaction.fz_n = node.restrained[2] ? force[2] : 0.0;
                reaction.mx_nmm = node.restrained[3] ? force[3] : 0.0;
                reaction.my_nmm = node.restrained[4] ? force[4] : 0.0;
                reaction.mz_nmm = node.restrained[5] ? force[5] : 0.0;
                reactions->push_back(std::move(reaction));
            }
        }
    }
    return results;
}

}  // namespace chudware::structural
