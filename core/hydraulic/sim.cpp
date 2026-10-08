// core/hydraulic/sim.cpp — hydraulic_simulate: lumped-parameter transient
// solve of an aicad.hydraulic/1 circuit (contract: hydraulic_schema.md §8).
//
// Model
//   * Every hose (or set of hoses sharing a component port) is one pressure
//     node. Node capacitance C = V/β_eff(p) + V_hose/β_wall, where V is hose
//     bore volume plus the attached actuator chamber volumes and β_eff is the
//     oil bulk modulus softened by entrained air (polytropic air bubbles), so
//     pressure can never fall below absolute zero.
//   * A node touching a reservoir is a fixed-pressure boundary. Unconnected
//     pump suction ports and the vented rod side of a single-acting cylinder
//     draw from / vent to an implicit tank at 0 bar(g).
//   * Orifices: Q = CdA·sqrt(2/ρ)·Δp / (Δp² + Δp_tr²)^¼ — turbulent
//     square-root law with a laminar transition set by Re_crit and viscosity.
//   * Pump: flow source Q = D·n·ηv (+ optional pressure compensator).
//     Motor: Q = D·ω/ηv, T = D·Δp·ηm (efficiency losses switch sides with the
//     sign of Δp/ω). Cylinders/rotary actuators: mass/inertia, constant load,
//     Coulomb + viscous friction, penalty end stops.
//   * Relief: linear override above cracking pressure. Check: spring cracking
//     pressure + opening band. Accumulator: polytropic gas p·V^n = const.
//
// Integration: adaptive implicit (backward) Euler — L-stable for the stiff
// oil-spring / end-stop / orifice dynamics — with Newton iterations on a
// finite-difference Jacobian and a local-error estimate h/2·|f(n+1) − f(n)|.
// Steps land on every valve-ramp breakpoint. Energy bookkeeping integrates the
// element powers at the accepted (n+1) state, so the balance
//   pump = useful + heat + stored + boundary
// holds to Newton tolerance.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI
#include "hydraulic/sim.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <functional>
#include <initializer_list>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "hydraulic/schema.h"

namespace chudware::hydraulic {
namespace {

using pyutil::py_repr;
using pyutil::pyround;

constexpr double kBar = 1e5;
constexpr double kPatm = 101325.0;
constexpr double kLpm = 1.0 / 60000.0;  // 1 L/min in m³/s
constexpr double kAirPoly = 1.4;        // entrained-air polytropic exponent
constexpr double kReCrit = 100.0;       // orifice laminar/turbulent transition
constexpr double kCdDefault = 0.7;
constexpr double kOmegaSmooth = 0.5;    // rad/s — Coulomb/efficiency switching
constexpr double kVelSmooth = 1e-4;     // m/s
constexpr double kDpSmooth = 1.0 * kBar;
constexpr double kRunawayPa = 5000.0 * kBar;
constexpr double kRtol = 1e-3;
constexpr long kMaxSteps = 2000000;
constexpr size_t kMaxSamples = 200001;
constexpr int kAbsent = -2;  // unconnected port: reads as the implicit 0 bar(g) tank
constexpr int kMaxSeries = 8;

enum Role { RP = 0, RT = 1, RA = 2, RB = 3, RL = 4, kRoles = 5 };
enum class Kind {
    Pump, Cylinder, Rotary, Motor, DirValve, Check, Relief, FlowCtl, Restriction,
    Accumulator, Reservoir
};
enum class Center { Closed, Tandem, Open, Float };

struct Refuse {
    ojson env;
};

[[noreturn]] void refuse(const std::string& reason, const std::string& hint,
                         ojson extra = ojson::object()) {
    ojson out = ojson::object();
    out["ok"] = false;
    out["reason"] = reason;
    out["hint"] = hint;
    for (auto it = extra.begin(); it != extra.end(); ++it) out[it.key()] = it.value();
    throw Refuse{std::move(out)};
}

std::string upper(std::string s) {
    for (auto& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return s;
}

std::optional<Kind> kind_of(const std::string& type) {
    static const std::map<std::string, Kind> k = {
        {"pump", Kind::Pump},
        {"cylinder_linear", Kind::Cylinder},
        {"cylinder_rotary", Kind::Rotary},
        {"motor_hydraulic", Kind::Motor},
        {"valve_directional", Kind::DirValve},
        {"valve_check", Kind::Check},
        {"valve_relief", Kind::Relief},
        {"valve_flow_control", Kind::FlowCtl},
        {"filter", Kind::Restriction},
        {"heat_exchanger", Kind::Restriction},
        {"accumulator", Kind::Accumulator},
        {"reservoir", Kind::Reservoir},
    };
    auto it = k.find(type);
    if (it == k.end()) return std::nullopt;
    return it->second;
}

bool two_port(Kind k) {
    return k == Kind::Check || k == Kind::FlowCtl || k == Kind::Restriction;
}

const char* role_name(Kind k, int role) {
    if (two_port(k)) return role == RP ? "IN" : "OUT";
    static const char* names[kRoles] = {"P", "T", "A", "B", "L"};
    return names[role];
}

const char* port_names_hint(Kind k) {
    switch (k) {
        case Kind::Pump: return "P (outlet), T (suction; optional), L (drain; optional)";
        case Kind::Cylinder: return "A (cap side), B (rod side)";
        case Kind::Rotary:
        case Kind::Motor: return "A, B (A->B flow = positive rotation), L (drain; optional)";
        case Kind::DirValve: return "P, T, A, B (3-way: P, A, T; 2-way: P, A)";
        case Kind::Relief: return "P (inlet), T (outlet)";
        case Kind::Check:
        case Kind::FlowCtl:
        case Kind::Restriction: return "IN, OUT";
        case Kind::Accumulator:
        case Kind::Reservoir: return "any port name";
    }
    return "";
}

// Resolve an (upper-cased) port name / port_type to a simulator role for a kind.
int resolve_alias(Kind k, const std::string& u) {
    auto in = [&](std::initializer_list<const char*> l) {
        for (const char* s : l)
            if (u == s) return true;
        return false;
    };
    switch (k) {
        case Kind::Pump:
            if (in({"P", "PRESSURE", "OUT", "OUTLET", "DISCHARGE"})) return RP;
            if (in({"T", "S", "TANK", "SUCTION", "IN", "INLET"})) return RT;
            if (in({"L", "DRAIN"})) return RL;
            return -1;
        case Kind::Motor:
        case Kind::Rotary:
            if (in({"A", "WORK_A", "PRESSURE", "IN", "INLET"})) return RA;
            if (in({"B", "WORK_B", "TANK", "OUT", "OUTLET"})) return RB;
            if (in({"L", "DRAIN"})) return RL;
            return -1;
        case Kind::Cylinder:
            if (in({"A", "WORK_A", "CAP", "BORE", "PRESSURE"})) return RA;
            if (in({"B", "WORK_B", "ROD", "ANNULUS", "TANK"})) return RB;
            return -1;
        case Kind::DirValve:
            if (in({"P", "PRESSURE"})) return RP;
            if (in({"T", "TANK", "R"})) return RT;
            if (in({"A", "WORK_A"})) return RA;
            if (in({"B", "WORK_B"})) return RB;
            return -1;
        case Kind::Relief:
            if (in({"P", "PRESSURE", "IN", "INLET", "A", "WORK_A"})) return RP;
            if (in({"T", "TANK", "OUT", "OUTLET", "B", "WORK_B"})) return RT;
            return -1;
        case Kind::Check:
        case Kind::FlowCtl:
        case Kind::Restriction:
            if (in({"IN", "INLET", "P", "PRESSURE", "A", "WORK_A", "1"})) return RP;
            if (in({"OUT", "OUTLET", "T", "TANK", "B", "WORK_B", "2"})) return RT;
            return -1;
        case Kind::Accumulator: return RP;
        case Kind::Reservoir: return RT;
    }
    return -1;
}

double smoothstep(double u) {
    if (u <= 0.0) return 0.0;
    if (u >= 1.0) return 1.0;
    return u * u * (3.0 - 2.0 * u);
}

double softplus(double x, double w) {
    const double z = x / w;
    if (z > 40.0) return x;
    if (z < -40.0) return 0.0;
    return w * std::log1p(std::exp(z));
}

struct Fluid {
    double rho = 870.0;
    double beta = 15000.0 * kBar;
    double nu = 32e-6;
    double air = 0.002;
};

// Orifice flow a->b for pressure difference dp (Pa) and effective area CdA (m²).
double orifice_q(double dp, double cda, const Fluid& fl) {
    if (cda <= 0.0) return 0.0;
    const double dh = std::sqrt(4.0 * (cda / kCdDefault) / M_PI);
    const double vtr = kReCrit * fl.nu / (kCdDefault * dh);
    const double dptr = std::max(0.5 * fl.rho * vtr * vtr, 1.0);
    return cda * std::sqrt(2.0 / fl.rho) * dp / std::pow(dp * dp + dptr * dptr, 0.25);
}

struct Node {
    std::string id;
    std::vector<std::string> members;
    bool boundary = false;
    int owner = -1;  // reservoir component index for boundary nodes
    double p_fixed = 0.0;
    double v_static = 0.0;  // hose bore volume, m³
    double c_wall = 0.0;    // hose wall compliance, m³/Pa
    double p_init = 0.0;
    bool p_init_set = false;
    int si = -1;
};

struct Comp {
    std::string id, type;
    Kind kind = Kind::Pump;
    std::array<int, kRoles> nd{kAbsent, kAbsent, kAbsent, kAbsent, kAbsent};
    int si = -1;
    std::vector<std::string> series;
    // efficiencies
    double eta_v = 1.0, eta_m = 1.0;
    // pump
    double q_th = 0.0, comp_p = -1.0, comp_band = 0.0, max_p = 0.0;
    // linear / rotary actuators and motors (mass ≡ inertia for rotary kinds)
    double area_a = 0.0, area_b = 0.0, stroke = 0.0, mass = 0.0;
    double f_load = 0.0, f_fric = 0.0, c_visc = 0.0;
    double dead_a = 0.0, dead_b = 0.0, k_stop = 0.0, c_stop = 0.0;
    double x0 = 0.0, v0 = 0.0, d_rad = 0.0, max_w = 0.0;
    bool single_acting = false;
    // directional valve
    int ways = 4, positions = 3;
    Center center = Center::Closed;
    double cda = 0.0, s0 = 0.0, response_s = 0.03;
    std::vector<std::array<double, 4>> segs;  // {t0, s0, t1, s1}
    // check
    double crack = 0.0, band = 0.0;
    // relief
    double p_set = 0.0, gain = 0.0, smooth_w = 0.0, q_rated = 0.0;
    // flow control
    bool fc_comp = false, reverse_free = false;
    double q_set = 0.0, dp_c = 0.0, cda_rev = 0.0;
    // accumulator
    double v_gas0 = 0.0, p0_abs = 0.0, poly_n = 1.4, vl0 = 0.0, v_eps = 0.0;
    // reservoir
    double p_res = 0.0;
};

struct Rates {
    double pump = 0.0, shaft = 0.0, heat = 0.0, useful = 0.0, store = 0.0, boundary = 0.0;
};

using Obs = std::vector<std::array<double, kMaxSeries>>;

struct Model {
    Fluid fl;
    std::vector<Node> nodes;
    std::vector<Comp> comps;
    int n = 0;
    std::vector<double> atol, typ;
    std::vector<double> p, qn, vn;  // scratch

    double spool(const Comp& c, double t) const {
        double s = c.s0;
        for (const auto& g : c.segs) {
            if (t < g[0]) break;
            if (t >= g[2] || g[2] <= g[0]) {
                s = g[3];
            } else {
                s = g[1] + (g[3] - g[1]) * (t - g[0]) / (g[2] - g[0]);
            }
        }
        return s;
    }

    double beta_inv_eff(double pg) const {
        // Below 1000 Pa(abs) continue smoothly towards zero so compliance keeps
        // growing: a vapour/air cavity never yields an absolute pressure < 0.
        double pa = pg + kPatm;
        if (pa < 1000.0) pa = std::max(1000.0 * std::exp(pa / 1000.0 - 1.0), 1e-3);
        const double r = std::pow(kPatm / pa, 1.0 / kAirPoly);
        const double num = (1.0 - fl.air) / fl.beta + fl.air * r / (kAirPoly * pa);
        const double den = (1.0 - fl.air) + fl.air * r;
        return num / den;
    }

    // Gas pressure (gauge) for oil volume vl; an empty accumulator sits at
    // its pre-charge with the anti-extrusion poppet closed.
    double acc_pressure(const Comp& c, double vl) const {
        const double vg = std::max(c.v_gas0 - std::max(vl, 0.0), 1e-3 * c.v_gas0);
        return c.p0_abs * std::pow(c.v_gas0 / vg, c.poly_n) - kPatm;
    }

    void eval(double t, const double* y, double* dy, Rates* R, Obs* obs) {
        const size_t nn = nodes.size();
        for (size_t i = 0; i < nn; ++i) {
            const Node& nd = nodes[i];
            p[i] = nd.boundary ? nd.p_fixed : y[nd.si];
            qn[i] = 0.0;
            vn[i] = nd.v_static;
        }
        auto pr = [&](int idx) { return idx >= 0 ? p[idx] : 0.0; };
        auto flow = [&](int from, int to, double q) {
            if (from >= 0) qn[from] -= q;
            if (to >= 0) qn[to] += q;
        };
        Rates r;
        for (size_t ci = 0; ci < comps.size(); ++ci) {
            const Comp& c = comps[ci];
            double* o = obs ? (*obs)[ci].data() : nullptr;
            switch (c.kind) {
                case Kind::Pump: {
                    const double ps = pr(c.nd[RT]);
                    const double pp = pr(c.nd[RP]);
                    const double dp = pp - ps;
                    const double disp =
                        c.comp_p >= 0.0 ? smoothstep((c.comp_p + c.comp_band - pp) / c.comp_band)
                                        : 1.0;
                    const double leak = (1.0 - c.eta_v) * std::tanh(std::max(dp, 0.0) / kDpSmooth);
                    const double q = c.q_th * disp * (1.0 - leak);
                    flow(c.nd[RT], c.nd[RP], q);
                    const double hyd = dp * q;
                    const double shaft = dp >= 0.0 ? dp * c.q_th * disp / c.eta_m
                                                   : dp * c.q_th * disp * c.eta_m;
                    r.pump += hyd;
                    r.shaft += shaft;
                    if (o) {
                        o[0] = q / kLpm;
                        o[1] = pp / kBar;
                        o[2] = hyd / 1e3;
                        o[3] = shaft / 1e3;
                    }
                    break;
                }
                case Kind::Cylinder: {
                    const double x = y[c.si];
                    const double v = y[c.si + 1];
                    const double pa = pr(c.nd[RA]);
                    const double pb = pr(c.nd[RB]);
                    const double fhyd = pa * c.area_a - pb * c.area_b;
                    if (c.nd[RA] >= 0) {
                        vn[c.nd[RA]] += std::max(c.dead_a + c.area_a * x, 0.5 * c.dead_a);
                        qn[c.nd[RA]] -= c.area_a * v;
                    }
                    if (c.nd[RB] >= 0) {
                        vn[c.nd[RB]] +=
                            std::max(c.dead_b + c.area_b * (c.stroke - x), 0.5 * c.dead_b);
                        qn[c.nd[RB]] += c.area_b * v;
                    }
                    double fstop = 0.0;
                    if (x < 0.0) fstop += std::max(c.k_stop * (-x) - c.c_stop * v, 0.0);
                    if (x > c.stroke)
                        fstop -= std::max(c.k_stop * (x - c.stroke) + c.c_stop * v, 0.0);
                    const double ffric = c.f_fric * std::tanh(v / kVelSmooth) + c.c_visc * v;
                    const double acc = (fhyd - c.f_load - ffric + fstop) / c.mass;
                    if (dy) {
                        dy[c.si] = v;
                        dy[c.si + 1] = acc;
                    }
                    r.store += c.mass * v * acc;
                    r.useful += c.f_load * v;
                    r.heat += ffric * v - fstop * v;
                    if (o) {
                        o[0] = x * 1e3;
                        o[1] = v * 1e3;
                        o[2] = pa / kBar;
                        o[3] = pb / kBar;
                        o[4] = fhyd;
                        o[5] = c.area_a * v / kLpm;
                    }
                    break;
                }
                case Kind::Rotary: {
                    const double th = y[c.si];
                    const double w = y[c.si + 1];
                    const double pa = pr(c.nd[RA]);
                    const double pb = pr(c.nd[RB]);
                    const double dp = pa - pb;
                    vn[c.nd[RA]] += std::max(c.dead_a + c.d_rad * th, 0.5 * c.dead_a);
                    qn[c.nd[RA]] -= c.d_rad * w;
                    vn[c.nd[RB]] += std::max(c.dead_b + c.d_rad * (c.stroke - th), 0.5 * c.dead_b);
                    qn[c.nd[RB]] += c.d_rad * w;
                    const double tm = c.d_rad * dp -
                                      (1.0 - c.eta_m) * c.d_rad * std::abs(dp) *
                                          std::tanh(w / kOmegaSmooth);
                    double tstop = 0.0;
                    if (th < 0.0) tstop += std::max(c.k_stop * (-th) - c.c_stop * w, 0.0);
                    if (th > c.stroke)
                        tstop -= std::max(c.k_stop * (th - c.stroke) + c.c_stop * w, 0.0);
                    const double tfric = c.f_fric * std::tanh(w / kOmegaSmooth) + c.c_visc * w;
                    const double acc = (tm - c.f_load - tfric + tstop) / c.mass;
                    if (dy) {
                        dy[c.si] = w;
                        dy[c.si + 1] = acc;
                    }
                    r.heat += (c.d_rad * dp - tm) * w + tfric * w - tstop * w;
                    r.store += c.mass * w * acc;
                    r.useful += c.f_load * w;
                    if (o) {
                        o[0] = th * 180.0 / M_PI;
                        o[1] = w * 60.0 / (2.0 * M_PI);
                        o[2] = pa / kBar;
                        o[3] = pb / kBar;
                        o[4] = tm;
                        o[5] = c.d_rad * w / kLpm;
                    }
                    break;
                }
                case Kind::Motor: {
                    const double w = y[c.si];
                    const double dp = pr(c.nd[RA]) - pr(c.nd[RB]);
                    const double q = c.d_rad * w + (1.0 / c.eta_v - 1.0) * c.d_rad * std::abs(w) *
                                                       std::tanh(dp / kDpSmooth);
                    flow(c.nd[RA], c.nd[RB], q);
                    const double tm = c.d_rad * dp -
                                      (1.0 - c.eta_m) * c.d_rad * std::abs(dp) *
                                          std::tanh(w / kOmegaSmooth);
                    const double tfric = c.f_fric * std::tanh(w / kOmegaSmooth) + c.c_visc * w;
                    const double acc = (tm - c.f_load - tfric) / c.mass;
                    if (dy) dy[c.si] = acc;
                    r.heat += dp * q - tm * w + tfric * w;
                    r.store += c.mass * w * acc;
                    r.useful += c.f_load * w;
                    if (o) {
                        o[0] = w * 60.0 / (2.0 * M_PI);
                        o[1] = tm;
                        o[2] = q / kLpm;
                        o[3] = dp / kBar;
                        o[4] = tm * w / 1e3;
                    }
                    break;
                }
                case Kind::DirValve: {
                    const double s = spool(c, t);
                    const double fpos = std::max(s, 0.0);
                    const double fneg = std::max(-s, 0.0);
                    const double fc = c.positions == 3 ? 1.0 - std::abs(s) : 0.0;
                    double q_p = 0.0, q_t = 0.0;
                    auto path = [&](int ra, int rb, double f) {
                        if (f <= 0.0) return;
                        const int a = c.nd[ra], b = c.nd[rb];
                        const double dp = pr(a) - pr(b);
                        const double q = orifice_q(dp, f * c.cda, fl);
                        flow(a, b, q);
                        r.heat += dp * q;
                        if (ra == RP) q_p += q;
                        if (rb == RT) q_t += q;
                    };
                    if (c.ways == 4) {
                        path(RP, RA, fpos);
                        path(RB, RT, fpos);
                        path(RP, RB, fneg);
                        path(RA, RT, fneg);
                        switch (c.center) {
                            case Center::Closed: break;
                            case Center::Tandem: path(RP, RT, fc); break;
                            case Center::Open:
                                path(RP, RT, fc);
                                path(RA, RT, fc);
                                path(RB, RT, fc);
                                break;
                            case Center::Float:
                                path(RA, RT, fc);
                                path(RB, RT, fc);
                                break;
                        }
                    } else if (c.ways == 3) {
                        path(RP, RA, fpos);
                        path(RA, RT, fneg);
                        switch (c.center) {
                            case Center::Closed: break;
                            case Center::Tandem: path(RP, RT, fc); break;
                            case Center::Open:
                                path(RP, RT, fc);
                                path(RA, RT, fc);
                                break;
                            case Center::Float: path(RA, RT, fc); break;
                        }
                    } else {
                        path(RP, RA, fpos);
                    }
                    if (o) {
                        o[0] = s;
                        o[1] = q_p / kLpm;
                        o[2] = q_t / kLpm;
                    }
                    break;
                }
                case Kind::Relief: {
                    const double pin = pr(c.nd[RP]);
                    const double dp = pin - pr(c.nd[RT]);
                    const double q = c.gain * softplus(dp - c.p_set, c.smooth_w);
                    flow(c.nd[RP], c.nd[RT], q);
                    r.heat += dp * q;
                    if (o) {
                        o[0] = q / kLpm;
                        o[1] = pin / kBar;
                    }
                    break;
                }
                case Kind::Check:
                case Kind::FlowCtl:
                case Kind::Restriction: {
                    const double dp = pr(c.nd[RP]) - pr(c.nd[RT]);
                    double q = 0.0;
                    if (c.kind == Kind::Check) {
                        const double f = smoothstep((dp - c.crack) / c.band);
                        q = orifice_q(dp, f * c.cda, fl);
                    } else if (c.kind == Kind::FlowCtl && c.fc_comp) {
                        q = (dp < 0.0 && c.reverse_free) ? orifice_q(dp, c.cda_rev, fl)
                                                         : c.q_set * std::tanh(dp / c.dp_c);
                    } else {
                        q = orifice_q(dp, c.cda, fl);
                    }
                    flow(c.nd[RP], c.nd[RT], q);
                    r.heat += dp * q;
                    if (o) {
                        o[0] = q / kLpm;
                        o[1] = dp / kBar;
                    }
                    break;
                }
                case Kind::Accumulator: {
                    const double vl = y[c.si];
                    const double pacc = acc_pressure(c, vl);
                    const double pn = pr(c.nd[RP]);
                    // Outflow closes with the poppet over the last v_eps of oil.
                    const double qo = orifice_q(pn - pacc, c.cda, fl);
                    const double q = qo < 0.0 ? qo * smoothstep(vl / c.v_eps) : qo;
                    qn[c.nd[RP]] -= q;
                    if (dy) dy[c.si] = q;
                    r.heat += (pn - pacc) * q;
                    r.store += pacc * q;
                    if (o) {
                        o[0] = pacc / kBar;
                        o[1] = (c.v_gas0 - std::max(vl, 0.0)) * 1e3;
                        o[2] = q / kLpm;
                    }
                    break;
                }
                case Kind::Reservoir: break;  // boundary; flow filled below
            }
        }
        for (size_t i = 0; i < nn; ++i) {
            const Node& nd = nodes[i];
            if (nd.boundary) {
                r.boundary += nd.p_fixed * qn[i];
                continue;
            }
            const double cap = vn[i] * beta_inv_eff(p[i]) + nd.c_wall;
            if (dy) dy[nd.si] = qn[i] / cap;
            r.store += p[i] * qn[i];
        }
        if (obs) {
            for (size_t ci = 0; ci < comps.size(); ++ci) {
                if (comps[ci].kind != Kind::Reservoir) continue;
                double q = 0.0;
                for (size_t i = 0; i < nn; ++i)
                    if (nodes[i].owner == static_cast<int>(ci)) q += qn[i];
                (*obs)[ci][0] = q / kLpm;
            }
        }
        if (R) *R = r;
    }
};

// ── parameter parsing ─────────────────────────────────────────────────────

struct Parser {
    std::vector<std::string> missing, invalid, warnings;

    std::optional<double> num(const ojson& obj, const char* key, const std::string& owner) {
        if (!obj.is_object()) return std::nullopt;
        auto it = obj.find(key);
        if (it == obj.end() || it->is_null()) return std::nullopt;
        if (!it->is_number() || !std::isfinite(it->get<double>())) {
            invalid.push_back(owner + "." + key + " must be a finite number");
            return std::nullopt;
        }
        return it->get<double>();
    }
    bool has(const ojson& obj, const char* key) const {
        return obj.is_object() && obj.contains(key) && !obj[key].is_null();
    }
    double req(const ojson& obj, const char* key, const std::string& owner) {
        auto v = num(obj, key, owner);
        if (v) return *v;
        if (!has(obj, key)) missing.push_back(owner + "." + key);
        return std::numeric_limits<double>::quiet_NaN();
    }
    double opt(const ojson& obj, const char* key, const std::string& owner, double def) {
        auto v = num(obj, key, owner);
        return v ? *v : def;
    }
    void positive(double v, const std::string& name) {
        if (std::isfinite(v) && !(v > 0.0)) invalid.push_back(name + " must be > 0");
    }
    void nonneg(double v, const std::string& name) {
        if (std::isfinite(v) && v < 0.0) invalid.push_back(name + " must be >= 0");
    }
    void fraction(double v, const std::string& name) {
        if (std::isfinite(v) && !(v > 0.0 && v <= 1.0))
            invalid.push_back(name + " must be in (0, 1]");
    }

    // Flow capacity → effective orifice area CdA (m²). Kv > Cv >
    // orifice_area_mm2 (+cd) > rated_flow_lpm (+rated_dp_bar); otherwise a
    // documented default with a warning.
    double capacity(const ojson& prm, const std::string& owner, const Fluid& fl,
                    double def_flow_m3s, double def_dp_bar) {
        if (auto kv = num(prm, "Kv", owner)) {
            positive(*kv, owner + ".Kv");
            return *kv / 3600.0 / std::sqrt(2.0 * kBar / 1000.0);
        }
        if (auto cv = num(prm, "Cv", owner)) {
            positive(*cv, owner + ".Cv");
            return 0.865 * *cv / 3600.0 / std::sqrt(2.0 * kBar / 1000.0);
        }
        if (auto area = num(prm, "orifice_area_mm2", owner)) {
            positive(*area, owner + ".orifice_area_mm2");
            const double cd = opt(prm, "cd", owner, kCdDefault);
            positive(cd, owner + ".cd");
            return cd * *area * 1e-6;
        }
        const double dp = opt(prm, "rated_dp_bar", owner, def_dp_bar);
        positive(dp, owner + ".rated_dp_bar");
        double q;
        if (auto rq = num(prm, "rated_flow_lpm", owner)) {
            positive(*rq, owner + ".rated_flow_lpm");
            q = *rq * kLpm;
        } else {
            q = def_flow_m3s;
            warnings.push_back(owner + ": no flow capacity (Kv | Cv | orifice_area_mm2 | "
                               "rated_flow_lpm) given; assumed " +
                               pyutil::fmt_f(q / kLpm, 2) + " L/min at " +
                               pyutil::fmt_f(dp, 2) + " bar");
        }
        if (!(dp > 0.0) || !(q > 0.0)) return 0.0;
        return q / std::sqrt(2.0 * dp * kBar / fl.rho);
    }
};

// Valve position: "a" → +1, "b" → −1, "center" → 0, number in [−1, 1].
std::optional<double> parse_position(const ojson& v) {
    if (v.is_string()) {
        const std::string s = upper(v.get<std::string>());
        if (s == "A") return 1.0;
        if (s == "B") return -1.0;
        if (s == "CENTER" || s == "CENTRE" || s == "0") return 0.0;
        return std::nullopt;
    }
    if (v.is_number() && std::isfinite(v.get<double>())) {
        const double x = v.get<double>();
        if (x < -1.0 || x > 1.0) return std::nullopt;
        return x;
    }
    return std::nullopt;
}

struct Settings {
    double duration = 5.0, output_dt = 0.01, max_step = 0.01;
};

double setting(const ojson& params, const char* key, double def) {
    if (!params.contains(key) || params[key].is_null()) return def;
    const ojson& v = params[key];
    if (!v.is_number() || !std::isfinite(v.get<double>()) || !(v.get<double>() > 0.0)) {
        refuse("invalid_settings", std::string(key) + " must be a finite number > 0");
    }
    return v.get<double>();
}

struct Built {
    Model m;
    std::vector<double> y0;
    std::vector<double> breakpoints;
    bool has_relief = false;
    std::vector<std::string> warnings;
};

Built build(const ojson& params, const Settings& st) {
    Built B;
    Model& m = B.m;
    Parser ps;

    // fluid
    const ojson fluid = params.value("fluid", ojson::object());
    if (!fluid.is_null() && !fluid.is_object()) refuse("invalid_settings", "fluid must be an object");
    {
        Parser fp;
        m.fl.rho = fp.opt(fluid, "density_kgm3", "fluid", 870.0);
        m.fl.beta = fp.opt(fluid, "bulk_modulus_bar", "fluid", 15000.0) * kBar;
        m.fl.nu = fp.opt(fluid, "viscosity_cst", "fluid", 32.0) * 1e-6;
        m.fl.air = fp.opt(fluid, "air_fraction", "fluid", 0.002);
        fp.positive(m.fl.rho, "fluid.density_kgm3");
        fp.positive(m.fl.beta, "fluid.bulk_modulus_bar");
        fp.positive(m.fl.nu, "fluid.viscosity_cst");
        if (!(m.fl.air >= 0.0 && m.fl.air < 0.2))
            fp.invalid.push_back("fluid.air_fraction must be in [0, 0.2)");
        if (!fp.invalid.empty()) {
            refuse("invalid_settings", fp.invalid.front(),
                   ojson::object({{"errors", fp.invalid}}));
        }
    }

    const ojson& doc = params["doc"];
    const ojson jcomps = doc.value("components", ojson::array());
    const ojson hoses = doc.value("hoses", ojson::array());

    std::map<std::string, int> comp_index;
    for (size_t i = 0; i < jcomps.size(); ++i) {
        const ojson& jc = jcomps[i];
        Comp c;
        c.id = jc.value("id", std::string());
        c.type = jc.value("type", std::string());
        c.kind = *kind_of(c.type);
        comp_index[c.id] = static_cast<int>(i);
        m.comps.push_back(std::move(c));
    }
    const size_t nc = m.comps.size();

    // ── ports → endpoints → nodes ────────────────────────────────────────
    std::vector<int> parent(nc * kRoles);
    for (size_t i = 0; i < parent.size(); ++i) parent[i] = static_cast<int>(i);
    std::function<int(int)> find = [&](int a) {
        while (parent[a] != a) {
            parent[a] = parent[parent[a]];
            a = parent[a];
        }
        return a;
    };
    std::vector<char> used(nc * kRoles, 0);
    std::set<std::pair<int, std::string>> referenced;  // (comp, port string)
    std::vector<int> hose_first_ep;
    std::vector<std::string> hose_ids;
    std::vector<std::pair<double, double>> hose_vol;  // (V m³, C_wall)
    std::vector<int> ep_order;
    for (size_t hi = 0; hi < hoses.size(); ++hi) {
        const ojson& h = hoses[hi];
        const std::string hid = h.value("id", std::string());
        const std::string hown = hid.empty() ? "hoses[" + std::to_string(hi) + "]" : hid;
        const double id_mm = ps.opt(h, "id_mm", hown, 12.7);
        const double len_mm = ps.opt(h, "length_mm", hown, 1000.0);
        ps.positive(id_mm, hown + ".id_mm");
        ps.nonneg(len_mm, hown + ".length_mm");
        const double vol = M_PI / 4.0 * std::pow(id_mm * 1e-3, 2) * std::max(len_mm, 0.0) * 1e-3;
        double cw = 0.0;
        if (auto bw = ps.num(h, "wall_bulk_modulus_bar", hown)) {
            ps.positive(*bw, hown + ".wall_bulk_modulus_bar");
            if (*bw > 0.0) cw = vol / (*bw * kBar);
        }
        int first = -1;
        for (const auto& conn : h["connections"]) {
            const std::string cid = conn["component_id"].get<std::string>();
            const std::string pstr = conn["port"].get<std::string>();
            const int ci = comp_index.at(cid);
            const Comp& c = m.comps[ci];
            const ojson& jc = jcomps[ci];
            int role = -1;
            for (const auto& dp : jc.value("ports", ojson::array())) {
                if (dp.is_object() && dp.value("id", ojson(nullptr)) == pstr) {
                    const std::string pt = dp.value("port_type", std::string());
                    if (pt != "generic") role = resolve_alias(c.kind, upper(pt));
                    break;
                }
            }
            if (role < 0) role = resolve_alias(c.kind, upper(pstr));
            if (role >= 0 && c.kind == Kind::DirValve) {
                const ojson prm = jc.value("params", ojson::object());
                const ojson wv = prm.is_object() ? prm.value("ways", ojson(4)) : ojson(4);
                const int ways = wv.is_number() ? static_cast<int>(wv.get<double>()) : 4;
                if ((ways == 3 && role == RB) || (ways == 2 && (role == RB || role == RT)))
                    role = -1;
            }
            if (role < 0) {
                refuse("unknown_port",
                       hown + ": component " + cid + " (" + c.type + ") has no simulator port " +
                           py_repr(ojson(pstr)) + "; valid ports: " + port_names_hint(c.kind) +
                           " (or declare it in component.ports with a port_type)",
                       ojson::object({{"hose_id", hid}, {"component_id", cid}, {"port", pstr}}));
            }
            referenced.insert({ci, pstr});
            const int ep = ci * kRoles + role;
            if (!used[ep]) ep_order.push_back(ep);
            used[ep] = 1;
            if (first < 0) {
                first = ep;
            } else {
                parent[find(ep)] = find(first);
            }
        }
        hose_first_ep.push_back(first);
        hose_ids.push_back(hown);
        hose_vol.push_back({vol, cw});
    }
    std::map<int, int> root_node;
    for (size_t hi = 0; hi < hose_first_ep.size(); ++hi) {
        const int root = find(hose_first_ep[hi]);
        auto it = root_node.find(root);
        int ni;
        if (it == root_node.end()) {
            ni = static_cast<int>(m.nodes.size());
            root_node[root] = ni;
            Node nd;
            nd.id = hose_ids[hi];
            m.nodes.push_back(nd);
        } else {
            ni = it->second;
        }
        m.nodes[ni].v_static += hose_vol[hi].first;
        m.nodes[ni].c_wall += hose_vol[hi].second;
    }
    std::map<std::string, int> hose_node;
    for (size_t hi = 0; hi < hose_first_ep.size(); ++hi)
        hose_node[hose_ids[hi]] = root_node.at(find(hose_first_ep[hi]));
    for (const int ep : ep_order) {
        const int ni = root_node.at(find(ep));
        Comp& c = m.comps[ep / kRoles];
        c.nd[ep % kRoles] = ni;
        m.nodes[ni].members.push_back(c.id + "." + role_name(c.kind, ep % kRoles));
    }

    // ── component parameters ─────────────────────────────────────────────
    double total_pump_q = 0.0;
    for (size_t ci = 0; ci < nc; ++ci) {
        Comp& c = m.comps[ci];
        if (c.kind != Kind::Pump) continue;
        const ojson prm = jcomps[ci].value("params", ojson::object());
        c.eta_v = ps.opt(prm, "vol_efficiency", c.id, 0.95);
        c.eta_m = ps.opt(prm, "mech_efficiency", c.id, 0.9);
        ps.fraction(c.eta_v, c.id + ".vol_efficiency");
        ps.fraction(c.eta_m, c.id + ".mech_efficiency");
        if (ps.has(prm, "displacement_cc_rev") || !ps.has(prm, "flow_lpm")) {
            const double d = ps.req(prm, "displacement_cc_rev", c.id);
            const double n = ps.req(prm, "speed_rpm", c.id);
            ps.positive(d, c.id + ".displacement_cc_rev");
            ps.positive(n, c.id + ".speed_rpm");
            c.q_th = d * 1e-6 * n / 60.0;
            if (ps.has(prm, "flow_lpm"))
                ps.warnings.push_back(c.id + ": flow_lpm ignored (displacement_cc_rev × "
                                             "speed_rpm defines the pump flow)");
        } else {
            const double q = ps.req(prm, "flow_lpm", c.id);
            ps.positive(q, c.id + ".flow_lpm");
            c.q_th = q * kLpm / c.eta_v;
        }
        if (auto cp = ps.num(prm, "compensator_bar", c.id)) {
            ps.positive(*cp, c.id + ".compensator_bar");
            c.comp_p = *cp * kBar;
            c.comp_band = ps.opt(prm, "compensator_band_bar", c.id,
                                 std::max(2.0, 0.03 * *cp)) * kBar;
            ps.positive(c.comp_band, c.id + ".compensator_band_bar");
        }
        c.max_p = ps.opt(prm, "max_pressure_bar", c.id, 0.0) * kBar;
        c.series = {"flow_lpm", "pressure_bar", "power_kw", "shaft_power_kw"};
        if (std::isfinite(c.q_th) && std::isfinite(c.eta_v)) total_pump_q += c.q_th * c.eta_v;
    }
    const double def_flow = total_pump_q > 0.0 ? std::max(total_pump_q, kLpm) : 60.0 * kLpm;

    for (size_t ci = 0; ci < nc; ++ci) {
        Comp& c = m.comps[ci];
        const ojson prm = jcomps[ci].value("params", ojson::object());
        const std::string& id = c.id;
        switch (c.kind) {
            case Kind::Pump: break;
            case Kind::Cylinder: {
                const double bore = ps.req(prm, "bore_mm", id);
                const double stroke = ps.req(prm, "stroke_mm", id);
                const double rod = ps.opt(prm, "rod_mm", id, 0.0);
                ps.positive(bore, id + ".bore_mm");
                ps.positive(stroke, id + ".stroke_mm");
                ps.nonneg(rod, id + ".rod_mm");
                if (std::isfinite(bore) && rod >= bore)
                    ps.invalid.push_back(id + ".rod_mm must be < bore_mm");
                c.single_acting = prm.value("actuator_type", std::string()) == "single_acting";
                c.area_a = M_PI / 4.0 * std::pow(bore * 1e-3, 2);
                c.area_b = M_PI / 4.0 * (std::pow(bore * 1e-3, 2) - std::pow(rod * 1e-3, 2));
                c.stroke = stroke * 1e-3;
                const double def_mass =
                    std::max(0.5, 7850.0 * (M_PI / 4.0 * std::pow(rod * 1e-3, 2) * c.stroke +
                                            M_PI / 4.0 * std::pow(bore * 1e-3, 3) * 0.5));
                c.mass = ps.opt(prm, "piston_mass_kg", id, def_mass);
                ps.positive(c.mass, id + ".piston_mass_kg");
                c.f_fric = ps.opt(prm, "friction_n", id, 0.0);
                c.c_visc = ps.opt(prm, "viscous_ns_m", id, 0.0);
                ps.nonneg(c.f_fric, id + ".friction_n");
                ps.nonneg(c.c_visc, id + ".viscous_ns_m");
                const double dead_ml = ps.opt(prm, "dead_volume_ml", id, -1.0);
                if (dead_ml >= 0.0) {
                    c.dead_a = c.dead_b = std::max(dead_ml * 1e-6, 1e-7);
                } else {
                    c.dead_a = std::max(c.area_a * 0.01, 1e-7);
                    c.dead_b = std::max(c.area_b * 0.01, 1e-7);
                }
                c.series = {"position_mm", "velocity_mms", "pressure_a_bar", "pressure_b_bar",
                            "force_n", "flow_lpm"};
                break;
            }
            case Kind::Rotary: {
                const double d = ps.req(prm, "displacement_cc_rev", id);
                const double amax = ps.req(prm, "max_angle_deg", id);
                ps.positive(d, id + ".displacement_cc_rev");
                ps.positive(amax, id + ".max_angle_deg");
                c.d_rad = d * 1e-6 / (2.0 * M_PI);
                c.stroke = amax * M_PI / 180.0;
                c.eta_m = ps.opt(prm, "mech_efficiency", id, 0.95);
                ps.fraction(c.eta_m, id + ".mech_efficiency");
                c.mass = ps.opt(prm, "inertia_kgm2", id, 1e-3);
                ps.positive(c.mass, id + ".inertia_kgm2");
                c.f_fric = ps.opt(prm, "friction_nm", id, 0.0);
                ps.nonneg(c.f_fric, id + ".friction_nm");
                const double dead_ml = ps.opt(prm, "dead_volume_ml", id, -1.0);
                c.dead_a = c.dead_b =
                    dead_ml >= 0.0 ? std::max(dead_ml * 1e-6, 1e-7) : std::max(c.d_rad * 0.1, 1e-7);
                c.series = {"angle_deg", "speed_rpm", "pressure_a_bar", "pressure_b_bar",
                            "torque_nm", "flow_lpm"};
                break;
            }
            case Kind::Motor: {
                const double d = ps.req(prm, "displacement_cc_rev", id);
                ps.positive(d, id + ".displacement_cc_rev");
                c.d_rad = d * 1e-6 / (2.0 * M_PI);
                c.eta_v = ps.opt(prm, "vol_efficiency", id, 0.95);
                c.eta_m = ps.opt(prm, "mech_efficiency", id, 0.9);
                ps.fraction(c.eta_v, id + ".vol_efficiency");
                ps.fraction(c.eta_m, id + ".mech_efficiency");
                c.mass = ps.opt(prm, "inertia_kgm2", id, 5e-4 + 2e-5 * (std::isfinite(d) ? d : 0.0));
                ps.positive(c.mass, id + ".inertia_kgm2");
                c.f_fric = ps.opt(prm, "friction_nm", id, 0.0);
                ps.nonneg(c.f_fric, id + ".friction_nm");
                c.max_w = ps.opt(prm, "max_speed_rpm", id, 0.0) * 2.0 * M_PI / 60.0;
                c.max_p = ps.opt(prm, "max_pressure_bar", id, 0.0) * kBar;
                c.series = {"speed_rpm", "torque_nm", "flow_lpm", "pressure_bar", "power_kw"};
                break;
            }
            case Kind::DirValve: {
                c.ways = static_cast<int>(ps.opt(prm, "ways", id, 4.0));
                c.positions = static_cast<int>(ps.opt(prm, "positions", id, 3.0));
                if (c.ways != 2 && c.ways != 3 && c.ways != 4)
                    ps.invalid.push_back(id + ".ways must be 2, 3 or 4");
                if (c.positions != 2 && c.positions != 3)
                    ps.invalid.push_back(id + ".positions must be 2 or 3");
                const std::string cc = prm.value("center_condition", std::string("closed"));
                if (cc == "closed") c.center = Center::Closed;
                else if (cc == "tandem") c.center = Center::Tandem;
                else if (cc == "open") c.center = Center::Open;
                else if (cc == "float") c.center = Center::Float;
                else ps.invalid.push_back(id + ".center_condition must be closed|tandem|open|float");
                c.cda = ps.capacity(prm, id, m.fl, def_flow, 5.0);
                c.response_s = ps.opt(prm, "response_ms", id, 30.0) * 1e-3;
                ps.nonneg(c.response_s, id + ".response_ms");
                c.s0 = c.positions == 3 ? 0.0 : -1.0;
                if (prm.contains("initial_position") && !prm["initial_position"].is_null()) {
                    auto s = parse_position(prm["initial_position"]);
                    if (!s || (c.positions == 2 && *s == 0.0))
                        ps.invalid.push_back(id + ".initial_position must be \"a\", \"b\"" +
                                             std::string(c.positions == 3 ? ", \"center\"" : "") +
                                             " or a number in [-1, 1]");
                    else
                        c.s0 = *s;
                }
                c.series = {"spool", "flow_lpm", "flow_t_lpm"};
                break;
            }
            case Kind::Check: {
                c.crack = ps.opt(prm, "cracking_pressure_bar", id, 0.5) * kBar;
                c.band = ps.opt(prm, "opening_band_bar", id, 1.0) * kBar;
                ps.nonneg(c.crack, id + ".cracking_pressure_bar");
                ps.positive(c.band, id + ".opening_band_bar");
                c.cda = ps.capacity(prm, id, m.fl, def_flow, 1.0);
                c.series = {"flow_lpm", "dp_bar"};
                break;
            }
            case Kind::Relief: {
                const double set = ps.req(prm, "set_pressure_bar", id);
                ps.positive(set, id + ".set_pressure_bar");
                const double ovr = ps.opt(prm, "override_bar", id, 0.1 * (std::isfinite(set) ? set : 0.0));
                ps.positive(ovr, id + ".override_bar");
                double qr = def_flow;
                if (auto q = ps.num(prm, "rated_flow_lpm", id)) {
                    ps.positive(*q, id + ".rated_flow_lpm");
                    qr = *q * kLpm;
                }
                c.p_set = set * kBar;
                c.q_rated = qr;
                c.gain = (ovr > 0.0) ? qr / (ovr * kBar) : 0.0;
                c.smooth_w = std::max(0.02 * ovr * kBar, 100.0);
                c.series = {"flow_lpm", "pressure_bar"};
                B.has_relief = true;
                break;
            }
            case Kind::FlowCtl: {
                if (auto qs = ps.num(prm, "set_flow_lpm", id)) {
                    ps.positive(*qs, id + ".set_flow_lpm");
                    c.fc_comp = true;
                    c.q_set = *qs * kLpm;
                    c.dp_c = ps.opt(prm, "compensator_dp_bar", id, 3.0) * kBar;
                    ps.positive(c.dp_c, id + ".compensator_dp_bar");
                    c.reverse_free = prm.value("reverse_free_flow", false);
                    c.cda_rev = c.q_set / std::sqrt(2.0 * kBar / m.fl.rho);
                } else if (ps.has(prm, "Kv") || ps.has(prm, "Cv") ||
                           ps.has(prm, "orifice_area_mm2")) {
                    c.cda = ps.capacity(prm, id, m.fl, def_flow, 5.0);
                } else if (!ps.has(prm, "set_flow_lpm")) {
                    ps.missing.push_back(id + ".set_flow_lpm|Kv|Cv|orifice_area_mm2");
                }
                c.series = {"flow_lpm", "dp_bar"};
                break;
            }
            case Kind::Restriction: {
                c.cda = ps.capacity(prm, id, m.fl, def_flow,
                                    c.type == "filter" ? 1.0 : 0.5);
                c.series = {"flow_lpm", "dp_bar"};
                break;
            }
            case Kind::Accumulator: {
                const double vol = ps.req(prm, "volume_l", id);
                const double pre = ps.req(prm, "pre_charge_bar", id);
                ps.positive(vol, id + ".volume_l");
                ps.positive(pre, id + ".pre_charge_bar");
                c.v_gas0 = vol * 1e-3;
                c.p0_abs = pre * kBar + kPatm;
                c.poly_n = ps.opt(prm, "polytropic_n", id, 1.4);
                if (!(c.poly_n >= 1.0 && c.poly_n <= 1.67))
                    ps.invalid.push_back(id + ".polytropic_n must be in [1.0, 1.67]");
                const double area = ps.opt(prm, "port_area_mm2", id, 80.0);
                const double cd = ps.opt(prm, "cd", id, kCdDefault);
                ps.positive(area, id + ".port_area_mm2");
                ps.positive(cd, id + ".cd");
                c.cda = area * 1e-6 * cd;
                c.v_eps = std::max(1e-3 * c.v_gas0, 1e-9);
                c.series = {"pressure_bar", "gas_volume_l", "flow_lpm"};
                break;
            }
            case Kind::Reservoir: {
                c.p_res = ps.opt(prm, "pressure_bar", id, 0.0) * kBar;
                c.series = {"flow_lpm"};
                break;
            }
        }
    }
    if (!ps.invalid.empty()) {
        refuse("invalid_param", ps.invalid.front(), ojson::object({{"errors", ps.invalid}}));
    }
    if (!ps.missing.empty()) {
        std::string list;
        for (const auto& s : ps.missing) list += (list.empty() ? "" : ", ") + s;
        refuse("missing_params", "set the missing component params: " + list,
               ojson::object({{"missing", ps.missing}}));
    }

    // ── connectivity ─────────────────────────────────────────────────────
    for (size_t ci = 0; ci < nc; ++ci) {
        const Comp& c = m.comps[ci];
        std::vector<int> required;
        switch (c.kind) {
            case Kind::Pump: required = {RP}; break;
            case Kind::Cylinder:
                required = c.single_acting ? std::vector<int>{RA} : std::vector<int>{RA, RB};
                break;
            case Kind::Rotary:
            case Kind::Motor: required = {RA, RB}; break;
            case Kind::DirValve:
                required = c.ways == 4   ? std::vector<int>{RP, RT, RA, RB}
                           : c.ways == 3 ? std::vector<int>{RP, RA, RT}
                                         : std::vector<int>{RP, RA};
                break;
            case Kind::Relief:
            case Kind::Check:
            case Kind::FlowCtl:
            case Kind::Restriction: required = {RP, RT}; break;
            case Kind::Accumulator: required = {RP}; break;
            case Kind::Reservoir: break;
        }
        for (const int role : required) {
            if (c.nd[role] == kAbsent) {
                const std::string pname = c.id + "." + role_name(c.kind, role);
                refuse("unconnected_port",
                       "port " + pname + " (" + c.type +
                           ") is not connected to any hose; add a hose (ports: " +
                           port_names_hint(c.kind) + ")",
                       ojson::object({{"component_id", c.id}, {"port", role_name(c.kind, role)}}));
            }
        }
        if (c.kind == Kind::Reservoir) continue;
        for (const auto& dp : jcomps[ci].value("ports", ojson::array())) {
            if (!dp.is_object() || !dp.contains("id") || !dp["id"].is_string()) continue;
            const std::string pid = dp["id"].get<std::string>();
            if (referenced.count({static_cast<int>(ci), pid})) continue;
            const std::string pt = dp.value("port_type", std::string());
            if (pt == "drain") continue;
            const int role = resolve_alias(c.kind, upper(pt == "generic" ? pid : pt));
            if (role == RL) continue;
            if (c.kind == Kind::Pump && role == RT) continue;  // implicit tank suction
            if (c.kind == Kind::Cylinder && role == RB && c.single_acting) continue;
            if (role >= 0 && c.nd[role] != kAbsent) continue;  // role connected via alias
            refuse("unconnected_port",
                   "declared port " + c.id + "." + pid + " is not connected to any hose",
                   ojson::object({{"component_id", c.id}, {"port", pid}}));
        }
    }

    // boundaries
    for (size_t ci = 0; ci < nc; ++ci) {
        const Comp& c = m.comps[ci];
        if (c.kind != Kind::Reservoir || c.nd[RT] < 0) continue;
        Node& nd = m.nodes[c.nd[RT]];
        if (nd.boundary) {
            if (nd.p_fixed != c.p_res)
                ps.warnings.push_back("node " + nd.id + " touches reservoirs at different "
                                      "pressures; using " + m.comps[nd.owner].id);
            continue;
        }
        nd.boundary = true;
        nd.owner = static_cast<int>(ci);
        nd.p_fixed = c.p_res;
    }

    // ── loads ────────────────────────────────────────────────────────────
    const ojson loads = params.value("loads", ojson::object());
    if (!loads.is_null() && !loads.is_object())
        refuse("invalid_load", "loads must be an object keyed by component id");
    if (loads.is_object()) {
        for (auto it = loads.begin(); it != loads.end(); ++it) {
            auto ci = comp_index.find(it.key());
            if (ci == comp_index.end())
                refuse("unknown_component", "loads references unknown component " + it.key(),
                       ojson::object({{"component_id", it.key()}}));
            Comp& c = m.comps[ci->second];
            const ojson& L = it.value();
            const bool linear = c.kind == Kind::Cylinder;
            const bool rotary = c.kind == Kind::Motor || c.kind == Kind::Rotary;
            if (!(linear || rotary) || !L.is_object())
                refuse("invalid_load", "loads." + c.id + ": loads apply to cylinder_linear "
                                       "(force_n, mass_kg, friction_n, viscous_ns_m) or "
                                       "motor_hydraulic / cylinder_rotary (torque_nm, "
                                       "inertia_kgm2, friction_nm, viscous_nms)");
            const std::set<std::string> allowed =
                linear ? std::set<std::string>{"force_n", "mass_kg", "friction_n", "viscous_ns_m"}
                       : std::set<std::string>{"torque_nm", "inertia_kgm2", "friction_nm",
                                               "viscous_nms"};
            for (auto k = L.begin(); k != L.end(); ++k) {
                if (!allowed.count(k.key()))
                    refuse("invalid_load", "loads." + c.id + "." + k.key() +
                                               " is not a load key for " + c.type);
            }
            Parser lp;
            const std::string own = "loads." + c.id;
            if (linear) {
                c.f_load = lp.opt(L, "force_n", own, 0.0);
                const double mk = lp.opt(L, "mass_kg", own, 0.0);
                const double fr = lp.opt(L, "friction_n", own, 0.0);
                const double cv = lp.opt(L, "viscous_ns_m", own, 0.0);
                lp.nonneg(mk, own + ".mass_kg");
                lp.nonneg(fr, own + ".friction_n");
                lp.nonneg(cv, own + ".viscous_ns_m");
                c.mass += mk;
                c.f_fric += fr;
                c.c_visc += cv;
            } else {
                c.f_load = lp.opt(L, "torque_nm", own, 0.0);
                const double j = lp.opt(L, "inertia_kgm2", own, 0.0);
                const double fr = lp.opt(L, "friction_nm", own, 0.0);
                const double cv = lp.opt(L, "viscous_nms", own, 0.0);
                lp.nonneg(j, own + ".inertia_kgm2");
                lp.nonneg(fr, own + ".friction_nm");
                lp.nonneg(cv, own + ".viscous_nms");
                c.mass += j;
                c.f_fric += fr;
                c.c_visc += cv;
            }
            if (!lp.invalid.empty())
                refuse("invalid_load", lp.invalid.front(), ojson::object({{"errors", lp.invalid}}));
        }
    }

    // ── initial conditions ───────────────────────────────────────────────
    const ojson initial = params.value("initial", ojson::object());
    if (!initial.is_null() && !initial.is_object())
        refuse("invalid_initial", "initial must be an object keyed by component or hose id");
    std::map<int, double> acc_init;  // comp → pressure Pa
    if (initial.is_object()) {
        for (auto it = initial.begin(); it != initial.end(); ++it) {
            const ojson& I = it.value();
            Parser ip;
            const std::string own = "initial." + it.key();
            auto hn = hose_node.find(it.key());
            auto ci = comp_index.find(it.key());
            if (ci == comp_index.end() && hn == hose_node.end())
                refuse("unknown_component",
                       "initial references unknown component or hose " + it.key(),
                       ojson::object({{"component_id", it.key()}}));
            if (!I.is_object()) refuse("invalid_initial", own + " must be an object");
            if (ci == comp_index.end()) {
                auto pb = ip.num(I, "pressure_bar", own);
                if (!pb) refuse("invalid_initial", own + ".pressure_bar (finite number) required");
                Node& nd = m.nodes[hn->second];
                nd.p_init = *pb * kBar;
                nd.p_init_set = true;
                continue;
            }
            Comp& c = m.comps[ci->second];
            if (c.kind == Kind::Cylinder) {
                const double x = ip.opt(I, "position_mm", own, 0.0) * 1e-3;
                if (!(x >= 0.0 && x <= c.stroke + 1e-12))
                    refuse("invalid_initial", own + ".position_mm must be within [0, stroke_mm]");
                c.x0 = x;
            } else if (c.kind == Kind::Rotary) {
                const double th = ip.opt(I, "angle_deg", own, 0.0) * M_PI / 180.0;
                if (!(th >= 0.0 && th <= c.stroke + 1e-12))
                    refuse("invalid_initial", own + ".angle_deg must be within [0, max_angle_deg]");
                c.x0 = th;
            } else if (c.kind == Kind::Motor) {
                c.v0 = ip.opt(I, "speed_rpm", own, 0.0) * 2.0 * M_PI / 60.0;
            } else if (c.kind == Kind::Accumulator) {
                auto pb = ip.num(I, "pressure_bar", own);
                if (!pb) refuse("invalid_initial", own + ".pressure_bar (finite number) required");
                acc_init[ci->second] = *pb * kBar;
            } else {
                refuse("invalid_initial", own + ": initial state applies to cylinder_linear "
                                                "(position_mm), cylinder_rotary (angle_deg), "
                                                "motor_hydraulic (speed_rpm), accumulator "
                                                "(pressure_bar) or a hose id (pressure_bar)");
            }
            if (!ip.invalid.empty()) refuse("invalid_initial", ip.invalid.front());
        }
    }

    // ── valve schedule → spool ramp segments ─────────────────────────────
    const ojson sched = params.value("valve_schedule", ojson::array());
    if (!sched.is_null() && !sched.is_array())
        refuse("invalid_schedule", "valve_schedule must be a list of {t_s, component_id, position}");
    struct Ev {
        double t;
        int ci;
        double s;
    };
    std::vector<Ev> events;
    if (sched.is_array()) {
        for (size_t k = 0; k < sched.size(); ++k) {
            const ojson& e = sched[k];
            const std::string loc = "valve_schedule[" + std::to_string(k) + "]";
            if (!e.is_object()) refuse("invalid_schedule", loc + " must be an object");
            const ojson cid = e.value("component_id", ojson(nullptr));
            if (!cid.is_string() || !comp_index.count(cid.get<std::string>()))
                refuse("unknown_component", loc + ".component_id " + py_repr(cid) +
                                                " is not a component in the circuit",
                       ojson::object({{"component_id", cid}}));
            const int ci = comp_index.at(cid.get<std::string>());
            const Comp& c = m.comps[ci];
            if (c.kind != Kind::DirValve)
                refuse("invalid_schedule", loc + ": " + c.id + " is a " + c.type +
                                               "; only valve_directional can be scheduled");
            const ojson tv = e.value("t_s", ojson(nullptr));
            if (!tv.is_number() || !std::isfinite(tv.get<double>()) || tv.get<double>() < 0.0)
                refuse("invalid_schedule", loc + ".t_s must be a finite number >= 0");
            auto s = parse_position(e.value("position", ojson(nullptr)));
            if (!s || (c.positions == 2 && *s == 0.0))
                refuse("invalid_schedule",
                       loc + ".position must be \"a\", \"b\"" +
                           std::string(c.positions == 3 ? ", \"center\"" : "") +
                           " or a number in [-1, 1] (+ = a side)");
            events.push_back({tv.get<double>(), ci, *s});
        }
    }
    std::stable_sort(events.begin(), events.end(),
                     [](const Ev& a, const Ev& b) { return a.t < b.t; });
    for (const Ev& e : events) {
        if (e.t > st.duration) continue;
        Comp& c = m.comps[e.ci];
        const double s_now = m.spool(c, e.t);
        // An interrupted ramp ends where it was when the new command lands.
        if (!c.segs.empty() && c.segs.back()[2] > e.t) {
            c.segs.back()[2] = e.t;
            c.segs.back()[3] = s_now;
        }
        const double t1 = e.t + std::abs(e.s - s_now) * c.response_s;
        c.segs.push_back({e.t, s_now, t1, e.s});
        B.breakpoints.push_back(e.t);
        if (t1 > e.t) B.breakpoints.push_back(t1);
    }
    std::sort(B.breakpoints.begin(), B.breakpoints.end());

    // ── sources ──────────────────────────────────────────────────────────
    bool has_source = false;
    for (const auto& c : m.comps)
        if (c.kind == Kind::Pump || (c.kind == Kind::Accumulator)) has_source = true;
    for (const auto& nd : m.nodes)
        if (nd.p_init_set && nd.p_init != 0.0) has_source = true;
    if (!has_source)
        refuse("no_pressure_source",
               "add a pump (displacement_cc_rev + speed_rpm) or an accumulator to the circuit");

    // ── state layout and initial state ───────────────────────────────────
    int n = 0;
    for (auto& nd : m.nodes) {
        if (nd.boundary) continue;
        nd.si = n++;
        m.atol.push_back(0.02 * kBar);
        m.typ.push_back(1.0 * kBar);
    }
    for (auto& c : m.comps) {
        switch (c.kind) {
            case Kind::Cylinder:
                c.si = n;
                n += 2;
                m.atol.insert(m.atol.end(), {1e-6, 1e-5});
                m.typ.insert(m.typ.end(), {1e-2, 1e-2});
                c.k_stop = 1e10;
                c.c_stop = 1.4 * std::sqrt(c.k_stop * c.mass);
                break;
            case Kind::Rotary:
                c.si = n;
                n += 2;
                m.atol.insert(m.atol.end(), {1e-5, 1e-3});
                m.typ.insert(m.typ.end(), {1e-1, 1.0});
                c.k_stop = 1e7;
                c.c_stop = 1.4 * std::sqrt(c.k_stop * c.mass);
                break;
            case Kind::Motor:
                c.si = n++;
                m.atol.push_back(1e-3);
                m.typ.push_back(10.0);
                break;
            case Kind::Accumulator:
                c.si = n++;
                m.atol.push_back(1e-8 + 1e-6 * c.v_gas0);
                m.typ.push_back(std::max(1e-2 * c.v_gas0, 1e-7));
                break;
            default: break;
        }
    }
    m.n = n;
    m.p.assign(m.nodes.size(), 0.0);
    m.qn.assign(m.nodes.size(), 0.0);
    m.vn.assign(m.nodes.size(), 0.0);

    B.y0.assign(n, 0.0);
    for (auto& [ci, pacc] : acc_init) {
        Comp& c = m.comps[ci];
        const double pabs = pacc + kPatm;
        c.vl0 = pabs > c.p0_abs ? c.v_gas0 * (1.0 - std::pow(c.p0_abs / pabs, 1.0 / c.poly_n))
                                : 0.0;
        Node& nd = m.nodes[c.nd[RP]];
        if (!nd.boundary && !nd.p_init_set) {
            nd.p_init = pacc;
            nd.p_init_set = true;
        }
    }
    for (const auto& nd : m.nodes)
        if (!nd.boundary) B.y0[nd.si] = nd.p_init;
    for (const auto& c : m.comps) {
        switch (c.kind) {
            case Kind::Cylinder:
            case Kind::Rotary:
                B.y0[c.si] = c.x0;
                B.y0[c.si + 1] = 0.0;
                break;
            case Kind::Motor: B.y0[c.si] = c.v0; break;
            case Kind::Accumulator: B.y0[c.si] = c.vl0; break;
            default: break;
        }
    }
    if (!B.has_relief && std::any_of(m.comps.begin(), m.comps.end(), [](const Comp& c) {
            return c.kind == Kind::Pump && c.comp_p < 0.0;
        })) {
        ps.warnings.push_back("circuit has a fixed-displacement pump but no valve_relief; "
                              "dead-heading will refuse with pressure_runaway");
    }
    B.warnings = std::move(ps.warnings);
    return B;
}

// Dense LU with partial pivoting (in place). Returns false when singular.
bool lu_factor(std::vector<double>& a, std::vector<int>& piv, int n) {
    for (int k = 0; k < n; ++k) {
        int pr = k;
        double best = std::abs(a[k * n + k]);
        for (int i = k + 1; i < n; ++i) {
            const double v = std::abs(a[i * n + k]);
            if (v > best) {
                best = v;
                pr = i;
            }
        }
        if (!(best > 0.0) || !std::isfinite(best)) return false;
        piv[k] = pr;
        if (pr != k)
            for (int j = 0; j < n; ++j) std::swap(a[k * n + j], a[pr * n + j]);
        const double d = a[k * n + k];
        for (int i = k + 1; i < n; ++i) {
            const double f = a[i * n + k] / d;
            a[i * n + k] = f;
            if (f == 0.0) continue;
            for (int j = k + 1; j < n; ++j) a[i * n + j] -= f * a[k * n + j];
        }
    }
    return true;
}

void lu_solve(const std::vector<double>& a, const std::vector<int>& piv, int n, double* b) {
    for (int k = 0; k < n; ++k) {
        if (piv[k] != k) std::swap(b[k], b[piv[k]]);
        for (int i = k + 1; i < n; ++i) b[i] -= a[i * n + k] * b[k];
    }
    for (int i = n - 1; i >= 0; --i) {
        double s = b[i];
        for (int j = i + 1; j < n; ++j) s -= a[i * n + j] * b[j];
        b[i] = s / a[i * n + i];
    }
}

struct CylTrack {
    bool moved = false, end_reached = false;
    double t_start = 0.0, t_end = -1.0, vmax = 0.0;
};

ojson round_series(const std::vector<double>& v, int nd) {
    ojson a = ojson::array();
    for (double x : v) a.push_back(pyround(x, nd));
    return a;
}

ojson run(const ojson& params) {
    if (!params.is_object()) refuse("invalid_settings", "params must be an object");
    if (!params.contains("doc") || params["doc"].is_null())
        refuse("invalid_circuit", "pass doc=<aicad.hydraulic/1 circuit>");
    const auto errs = validate_hydraulic(params["doc"]);
    if (!errs.empty()) {
        ojson e = ojson::array();
        for (const auto& s : errs) e.push_back(s);
        refuse("invalid_circuit", "fix the circuit's schema errors (see errors)",
               ojson::object({{"errors", e}}));
    }
    Settings st;
    st.duration = setting(params, "duration_s", 5.0);
    st.output_dt = setting(params, "output_dt_s", 0.01);
    st.max_step = setting(params, "max_step_s", std::min(0.01, st.duration));
    if (st.duration > 3600.0) refuse("invalid_settings", "duration_s must be <= 3600");
    if (st.duration / st.output_dt + 1.0 > static_cast<double>(kMaxSamples))
        refuse("invalid_settings", "duration_s / output_dt_s must give <= " +
                                       std::to_string(kMaxSamples - 1) + " output samples");

    Built B = build(params, st);
    Model& m = B.m;
    const int N = m.n;
    const size_t nn = m.nodes.size(), nc = m.comps.size();

    // output grid
    std::vector<double> t_out;
    const long kout = static_cast<long>(std::floor(st.duration / st.output_dt + 1e-9));
    for (long k = 0; k <= kout; ++k) t_out.push_back(static_cast<double>(k) * st.output_dt);
    if (st.duration - t_out.back() > 1e-9) t_out.push_back(st.duration);

    std::vector<std::vector<double>> node_out(nn);
    std::vector<std::vector<std::vector<double>>> comp_out(nc);
    for (size_t ci = 0; ci < nc; ++ci) comp_out[ci].resize(m.comps[ci].series.size());
    Obs obs(nc);
    std::vector<double> yi(N);
    auto sample = [&](double t, const double* y) {
        for (auto& o : obs) o.fill(0.0);
        m.eval(t, y, nullptr, nullptr, &obs);
        for (size_t i = 0; i < nn; ++i) node_out[i].push_back(m.p[i] / kBar);
        for (size_t ci = 0; ci < nc; ++ci)
            for (size_t s = 0; s < comp_out[ci].size(); ++s) comp_out[ci][s].push_back(obs[ci][s]);
    };

    std::vector<double> y = B.y0, y1(N), f0(N), f1(N), fb(N), res(N), yp(N), J(N * N), M(N * N);
    std::vector<int> piv(N);
    m.eval(0.0, y.data(), f0.data(), nullptr, nullptr);
    size_t next_out = 0;
    sample(0.0, y.data());
    next_out = 1;

    auto jacobian = [&](double t1, const std::vector<double>& yy, double h) {
        m.eval(t1, yy.data(), fb.data(), nullptr, nullptr);
        yp = yy;
        for (int j = 0; j < N; ++j) {
            const double d = 1e-7 * std::max(std::abs(yy[j]), m.typ[j]);
            yp[j] = yy[j] + d;
            m.eval(t1, yp.data(), f1.data(), nullptr, nullptr);
            yp[j] = yy[j];
            for (int i = 0; i < N; ++i) J[i * N + j] = (f1[i] - fb[i]) / d;
        }
        for (int i = 0; i < N; ++i)
            for (int j = 0; j < N; ++j) M[i * N + j] = (i == j ? 1.0 : 0.0) - h * J[i * N + j];
        return lu_factor(M, piv, N);
    };
    auto wnorm = [&](const std::vector<double>& d, const std::vector<double>& ref) {
        double mx = 0.0;
        for (int j = 0; j < N; ++j) {
            const double w = m.atol[j] + kRtol * std::abs(ref[j]);
            mx = std::max(mx, std::abs(d[j]) / w);
        }
        return mx;
    };
    auto newton = [&](double t1, double h) {
        y1 = y;
        if (N == 0) return true;
        if (!jacobian(t1, y1, h)) return false;
        bool refreshed = false;
        for (int it = 0; it < 12; ++it) {
            m.eval(t1, y1.data(), f1.data(), nullptr, nullptr);
            for (int i = 0; i < N; ++i) res[i] = -(y1[i] - y[i] - h * f1[i]);
            lu_solve(M, piv, N, res.data());
            for (int i = 0; i < N; ++i) y1[i] += res[i];
            const double nrm = wnorm(res, y1);
            if (!std::isfinite(nrm)) return false;
            if (nrm < 1e-3) return true;
            if (it == 4 && !refreshed) {
                refreshed = true;
                if (!jacobian(t1, y1, h)) return false;
            }
        }
        return false;
    };

    // summary trackers
    Rates acc_r;
    double peak_p = -1e300, min_p = 1e300, relief_open = 0.0;
    for (size_t i = 0; i < nn; ++i) {
        peak_p = std::max(peak_p, m.p[i]);
        min_p = std::min(min_p, m.p[i]);
    }
    std::vector<CylTrack> cyl(nc);
    std::vector<double> pump_p_int(nc, 0.0), pump_e(nc, 0.0), pump_pmax(nc, 0.0);
    std::vector<double> motor_wmax(nc, 0.0), motor_pmax(nc, 0.0);
    const double tol_end_frac = 0.002;

    double t = 0.0, h = std::min(1e-5, st.max_step);
    // After a Newton failure the step may only regrow slowly past the size
    // that failed (non-smooth events — end stops, valve cracking, cavitation —
    // otherwise make the controller oscillate between growth and failure).
    double h_cap = std::numeric_limits<double>::infinity();
    const double hmin = 1e-12;
    long steps = 0, rejected = 0, newton_fail = 0;
    size_t bp_idx = 0;
    Rates rr;
    while (t < st.duration - 1e-12) {
        while (bp_idx < B.breakpoints.size() && B.breakpoints[bp_idx] <= t + 1e-12) ++bp_idx;
        double hlim = std::min(st.max_step, st.duration - t);
        if (bp_idx < B.breakpoints.size()) hlim = std::min(hlim, B.breakpoints[bp_idx] - t);
        h = std::min(h, hlim);
        if (hlim - h < 1e-12) h = hlim;
        const double t1 = t + h;
        if (!newton(t1, h)) {
            ++newton_fail;
            h_cap = 0.5 * h;
            h *= 0.25;
            if (h < hmin)
                refuse("solver_failed", "Newton iteration failed to converge at t=" +
                                            pyutil::fmt_f(t, 6) + " s; check for an isolated "
                                            "or inconsistent sub-circuit",
                       ojson::object({{"t_s", t}}));
            continue;
        }
        m.eval(t1, y1.data(), f1.data(), nullptr, nullptr);
        double err = 0.0;
        for (int j = 0; j < N; ++j) {
            const double e = 0.5 * h * (f1[j] - f0[j]);
            const double w = m.atol[j] + kRtol * std::max(std::abs(y[j]), std::abs(y1[j]));
            err = std::max(err, std::abs(e) / w);
        }
        if (!std::isfinite(err)) err = 1e6;
        if (err > 1.0) {
            ++rejected;
            h *= std::max(0.2, 0.9 / std::sqrt(err));
            if (h < hmin)
                refuse("solver_failed", "step size underflow at t=" + pyutil::fmt_f(t, 6) + " s",
                       ojson::object({{"t_s", t}}));
            continue;
        }
        // accepted: samples inside (t, t1]
        while (next_out < t_out.size() && t_out[next_out] <= t1 + 1e-12) {
            const double ts = t_out[next_out];
            const double a = h > 0.0 ? std::clamp((ts - t) / h, 0.0, 1.0) : 1.0;
            for (int j = 0; j < N; ++j) yi[j] = y[j] + a * (y1[j] - y[j]);
            sample(ts, yi.data());
            ++next_out;
        }
        for (auto& o : obs) o.fill(0.0);
        m.eval(t1, y1.data(), nullptr, &rr, &obs);
        acc_r.pump += rr.pump * h;
        acc_r.shaft += rr.shaft * h;
        acc_r.heat += rr.heat * h;
        acc_r.useful += rr.useful * h;
        acc_r.store += rr.store * h;
        acc_r.boundary += rr.boundary * h;
        bool relief_is_open = false;
        for (size_t ci = 0; ci < nc; ++ci) {
            const Comp& c = m.comps[ci];
            switch (c.kind) {
                case Kind::Relief:
                    if (obs[ci][0] * kLpm > 0.01 * c.q_rated) relief_is_open = true;
                    break;
                case Kind::Pump:
                    pump_p_int[ci] += obs[ci][1] * h;
                    pump_e[ci] += obs[ci][2] * h;
                    pump_pmax[ci] = std::max(pump_pmax[ci], obs[ci][1] * kBar);
                    break;
                case Kind::Motor:
                    motor_wmax[ci] = std::max(motor_wmax[ci], std::abs(y1[c.si]));
                    motor_pmax[ci] = std::max(motor_pmax[ci], std::abs(obs[ci][3]) * kBar);
                    break;
                case Kind::Cylinder:
                case Kind::Rotary: {
                    CylTrack& ct = cyl[ci];
                    const double x = y1[c.si];
                    ct.vmax = std::max(ct.vmax, std::abs(y1[c.si + 1]));
                    const double tol = std::max(c.kind == Kind::Cylinder ? 5e-4 : 5e-3,
                                                tol_end_frac * c.stroke);
                    if (!ct.moved && std::abs(x - c.x0) > std::min(1e-4, 0.5 * tol)) {
                        ct.moved = true;
                        ct.t_start = t1;
                    }
                    if (ct.moved && !ct.end_reached) {
                        const bool at_hi = x >= c.stroke - tol && std::abs(c.x0 - c.stroke) > tol;
                        const bool at_lo = x <= tol && c.x0 > tol;
                        if (at_hi || at_lo) {
                            ct.end_reached = true;
                            ct.t_end = t1;
                        }
                    }
                    break;
                }
                default: break;
            }
        }
        if (relief_is_open) relief_open += h;
        for (size_t i = 0; i < nn; ++i) {
            peak_p = std::max(peak_p, m.p[i]);
            min_p = std::min(min_p, m.p[i]);
            if (std::abs(m.p[i]) > kRunawayPa) {
                refuse("pressure_runaway",
                       "node " + m.nodes[i].id + " exceeded 5000 bar at t=" + pyutil::fmt_f(t1, 4) +
                           " s; add a valve_relief (or a pressure-compensated pump) on the "
                           "pressure line",
                       ojson::object({{"node_id", m.nodes[i].id}, {"t_s", t1}}));
            }
        }
        t = t1;
        y.swap(y1);
        f0 = f1;
        m.eval(t, y.data(), f0.data(), nullptr, nullptr);
        if (++steps > kMaxSteps)
            refuse("solver_failed", "step limit exceeded; raise max_step_s or shorten duration_s");
        h *= std::min(4.0, std::max(0.2, 0.9 / std::sqrt(std::max(err, 1e-8))));
        h_cap *= 1.05;
        h = std::min(h, h_cap);
    }
    while (next_out < t_out.size()) {  // float slack on the final sample
        sample(t_out[next_out], y.data());
        ++next_out;
    }

    // ── assemble ─────────────────────────────────────────────────────────
    std::vector<std::string> warnings = B.warnings;
    ojson out = ojson::object();
    out["ok"] = true;
    out["time_s"] = round_series(t_out, 6);
    ojson jnodes = ojson::object();
    for (size_t i = 0; i < nn; ++i) {
        ojson nd = ojson::object();
        nd["pressure_bar"] = round_series(node_out[i], 4);
        nd["members"] = m.nodes[i].members;
        nd["boundary"] = m.nodes[i].boundary;
        jnodes[m.nodes[i].id] = std::move(nd);
    }
    out["nodes"] = std::move(jnodes);
    ojson jcomps = ojson::object();
    for (size_t ci = 0; ci < nc; ++ci) {
        const Comp& c = m.comps[ci];
        ojson series = ojson::object();
        for (size_t s = 0; s < c.series.size(); ++s)
            series[c.series[s]] = round_series(comp_out[ci][s], c.series[s] == "gas_volume_l" ? 6 : 4);
        jcomps[c.id] = ojson::object({{"kind", c.type}, {"series", std::move(series)}});
    }
    out["components"] = std::move(jcomps);

    const size_t ns = t_out.size();
    const size_t tail0 = ns - std::max<size_t>(1, ns / 10);
    auto tail_mean = [&](const std::vector<double>& v) {
        double s = 0.0;
        for (size_t k = tail0; k < ns; ++k) s += v[k];
        return s / static_cast<double>(ns - tail0);
    };
    ojson cyls = ojson::object(), motors = ojson::object(), pumps = ojson::object();
    for (size_t ci = 0; ci < nc; ++ci) {
        const Comp& c = m.comps[ci];
        if (c.kind == Kind::Cylinder || c.kind == Kind::Rotary) {
            const CylTrack& ct = cyl[ci];
            ojson j = ojson::object();
            j["stroke_time_s"] =
                ct.end_reached ? ojson(pyround(ct.t_end - ct.t_start, 4)) : ojson(nullptr);
            j["end_reached_at_s"] = ct.end_reached ? ojson(pyround(ct.t_end, 4)) : ojson(nullptr);
            if (c.kind == Kind::Cylinder) {
                j["max_velocity_mms"] = pyround(ct.vmax * 1e3, 4);
            } else {
                j["max_speed_rpm"] = pyround(ct.vmax * 60.0 / (2.0 * M_PI), 4);
            }
            j["end_reached"] = ct.end_reached;
            cyls[c.id] = std::move(j);
        } else if (c.kind == Kind::Motor) {
            motors[c.id] = ojson::object(
                {{"steady_speed_rpm", pyround(tail_mean(comp_out[ci][0]), 4)},
                 {"steady_torque_nm", pyround(tail_mean(comp_out[ci][1]), 4)},
                 {"max_speed_rpm", pyround(motor_wmax[ci] * 60.0 / (2.0 * M_PI), 4)}});
            if (c.max_w > 0.0 && motor_wmax[ci] > c.max_w)
                warnings.push_back(c.id + ": speed " +
                                   pyutil::fmt_f(motor_wmax[ci] * 60.0 / (2.0 * M_PI), 0) +
                                   " rpm exceeds max_speed_rpm");
            if (c.max_p > 0.0 && motor_pmax[ci] > c.max_p)
                warnings.push_back(c.id + ": differential pressure " +
                                   pyutil::fmt_f(motor_pmax[ci] / kBar, 1) +
                                   " bar exceeds max_pressure_bar");
        } else if (c.kind == Kind::Pump) {
            pumps[c.id] = ojson::object(
                {{"mean_pressure_bar", pyround(pump_p_int[ci] / st.duration, 4)},
                 {"mean_power_kw", pyround(pump_e[ci] / st.duration, 4)},
                 {"peak_pressure_bar", pyround(pump_pmax[ci] / kBar, 4)}});
            if (c.max_p > 0.0 && pump_pmax[ci] > c.max_p)
                warnings.push_back(c.id + ": outlet pressure " +
                                   pyutil::fmt_f(pump_pmax[ci] / kBar, 1) +
                                   " bar exceeds max_pressure_bar");
        }
    }
    if (min_p < -0.5 * kBar)
        warnings.push_back("a node fell to " + pyutil::fmt_f(min_p / kBar, 2) +
                           " bar(g): cavitation / air release likely (check meter-out or "
                           "anti-cavitation valves)");

    const double pump_kj = acc_r.pump / 1e3, useful_kj = acc_r.useful / 1e3,
                 heat_kj = acc_r.heat / 1e3, store_kj = acc_r.store / 1e3,
                 bnd_kj = acc_r.boundary / 1e3;
    const double scale = std::max({std::abs(pump_kj), std::abs(useful_kj) + std::abs(heat_kj) +
                                                          std::abs(store_kj) + std::abs(bnd_kj),
                                   1e-12});
    ojson summary = ojson::object();
    summary["peak_pressure_bar"] = pyround(peak_p / kBar, 4);
    summary["pump_energy_kj"] = pyround(pump_kj, 6);
    summary["shaft_energy_kj"] = pyround(acc_r.shaft / 1e3, 6);
    summary["useful_work_kj"] = pyround(useful_kj, 6);
    summary["heat_kj"] = pyround(heat_kj, 6);
    summary["stored_energy_change_kj"] = pyround(store_kj, 6);
    summary["boundary_energy_kj"] = pyround(bnd_kj, 6);
    summary["energy_balance_error_pct"] =
        pyround(100.0 * std::abs(pump_kj - useful_kj - heat_kj - store_kj - bnd_kj) / scale, 6);
    summary["efficiency"] =
        pump_kj > 1e-12 ? ojson(pyround(useful_kj / pump_kj, 6)) : ojson(nullptr);
    summary["relief_open_time_s"] = pyround(relief_open, 4);
    summary["cylinders"] = std::move(cyls);
    summary["motors"] = std::move(motors);
    summary["pumps"] = std::move(pumps);
    out["summary"] = std::move(summary);
    out["solver"] = ojson::object(
        {{"method", "adaptive implicit Euler (L-stable), Newton + finite-difference Jacobian"},
         {"steps", steps},
         {"rejected_steps", rejected},
         {"newton_failures", newton_fail},
         {"states", N},
         {"rtol", kRtol}});
    out["warnings"] = warnings;
    out["hint"] = "Time series are sampled every output_dt_s; summary energies integrate every "
                  "solver step.";
    out["next_actions"] = ojson::array(
        {"compare summary.cylinders / summary.motors against the duty-cycle targets",
         "resize with hydraulic_size_cylinder / hydraulic_size_pump / hydraulic_size_motor if "
         "pressures or speeds miss",
         "persist the run as a hydraulic_circuit study (sim_study_add)"});
    return out;
}

}  // namespace

ojson simulate(const ojson& params) {
    try {
        return run(params);
    } catch (const Refuse& r) {
        return r.env;
    } catch (const nlohmann::json::exception& e) {
        // A param of the wrong JSON type (e.g. a string where a bool/number
        // belongs) surfaces here; name it rather than crash the dispatch.
        return pyutil::jerr("invalid_param",
                            std::string("a circuit or settings field has the wrong type: ") +
                                e.what());
    }
}

}  // namespace chudware::hydraulic
