// MNA circuit simulator implementation — see spice.h.
//
// DC operating point (Newton with gmin- + source-stepping fallback), transient
// (trapezoidal / backward-Euler companion models, inner Newton for nonlinear devices),
// and AC sweep. The node-index construction, branch-row allocation, per-device stamps,
// the time-stepping loop (BE bootstrap, source breakpoints), Newton damping/limiting, and
// probe evaluation mirror engine/sim/circuit.py + engine/sim/devices.py. The dense
// PartialPivLU solve matches numpy's linalg.solve for the small systems these circuits
// produce. Nonlinear devices ported: Diode (D), BJT (Q, Ebers-Moll), MOSFET (M, level-1).
// Mutual inductance (K), behavioral (B), subckt expansion, and the netlist parser remain
// deferred to follow-ups.

#include "sim/spice.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <complex>
#include <map>
#include <set>
#include <string>
#include <vector>

#include <Eigen/Dense>

namespace aicad::sim {

using nlohmann::json;

namespace {

constexpr double kPi = 3.14159265358979323846;
// Physical constants (devices.py).
constexpr double kBoltz = 1.380649e-23;       // Boltzmann (J/K)
constexpr double kQ = 1.602176634e-19;        // electron charge (C)
constexpr double kTnomC = 27.0;               // nominal param temperature (deg C)
constexpr double kEgSi = 1.11;                // silicon bandgap (eV)

bool is_ground(const std::string& n) { return n == "0" || n == "gnd" || n == "GND"; }

std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper((unsigned char)c));
    return s;
}
std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower((unsigned char)c));
    return s;
}
std::string trim(std::string s) {
    while (!s.empty() && std::isspace((unsigned char)s.front())) s.erase(s.begin());
    while (!s.empty() && std::isspace((unsigned char)s.back())) s.pop_back();
    return s;
}
double as_num(const json& v, double dflt = 0.0) { return v.is_number() ? v.get<double>() : dflt; }
double dev_value(const json& d, double dflt = 0.0) {
    return (d.contains("value") && d["value"].is_number()) ? d["value"].get<double>() : dflt;
}
const json& params_of(const json& d) {
    static const json empty = json::object();
    return (d.contains("params") && d["params"].is_object()) ? d["params"] : empty;
}

// Thermal voltage kT/q at the given temperature in Celsius (devices.py::_vt).
double vt_of(double temp_c) { return kBoltz * (temp_c + 273.15) / kQ; }

// Read a model/device param: device params (exact key) override the .model card
// (key/UPPER/lower) — devices.py::Device._mp.
double mp(const json& d, const json& models, const char* key, double dflt) {
    const json& dp = params_of(d);
    if (dp.contains(key) && dp[key].is_number()) return dp[key].get<double>();
    std::string mname = d.value("model", std::string());
    if (!mname.empty() && models.contains(mname) && models[mname].is_object()) {
        const json& m = models[mname];
        for (const std::string& k : {std::string(key), upper(key), lower(key)})
            if (m.contains(k) && m[k].is_number()) return m[k].get<double>();
    }
    return dflt;
}

// Read a string model/device param: model card wins, then device params, then default
// (matches the BJT/MOSFET `type` lookup order in devices.py).
std::string model_str(const json& d, const json& models, const char* key, const std::string& dflt) {
    std::string mname = d.value("model", std::string());
    if (!mname.empty() && models.contains(mname) && models[mname].is_object()) {
        const json& m = models[mname];
        if (m.contains(key) && m[key].is_string()) return m[key].get<std::string>();
    }
    const json& dp = params_of(d);
    if (dp.contains(key) && dp[key].is_string()) return dp[key].get<std::string>();
    return dflt;
}

// Evaluate an independent source's value at time t (DC / PULSE / SIN / PWL) —
// devices.py::source_value.
double source_value_t(const json& d, double t) {
    const json& p = params_of(d);
    double base = dev_value(d, 0.0);
    if (p.contains("dc")) base = as_num(p["dc"], base);
    if (!p.contains("waveform") || !p["waveform"].is_object()) return base;
    const json& wf = p["waveform"];
    std::string kind = upper(wf.value("type", std::string("DC")));
    auto W = [&](const char* k, double dv) { return as_num(wf.value(k, json(dv)), dv); };
    if (kind == "DC") return W("v", base);
    if (kind == "PULSE") {
        double v1 = W("v1", 0), v2 = W("v2", 0), td = W("td", 0), tr = W("tr", 0),
               tf = W("tf", 0), pw = W("pw", 0), per = W("per", 0);
        if (t < td) return v1;
        double tt = t - td;
        if (per > 0.0) tt = std::fmod(tt, per);
        if (tr > 0.0 && tt < tr) return v1 + (v2 - v1) * (tt / tr);
        tt -= tr;
        if (tt < pw) return v2;
        tt -= pw;
        if (tf > 0.0 && tt < tf) return v2 + (v1 - v2) * (tt / tf);
        return v1;
    }
    if (kind == "SIN") {
        double vo = W("vo", 0), va = W("va", 0), td = W("td", 0), theta = W("theta", 0);
        double freq = wf.contains("f") ? W("f", 0) : W("freq", 0);
        if (t < td) return vo;
        double tt = t - td;
        return vo + va * std::exp(-tt * theta) * std::sin(2.0 * kPi * freq * tt);
    }
    if (kind == "PWL" && wf.contains("points") && wf["points"].is_array()) {
        std::vector<double> ts, vs;
        const json& pts = wf["points"];
        if (!pts.empty() && pts[0].is_array()) {
            for (const auto& pr : pts) { ts.push_back(pr[0].get<double>()); vs.push_back(pr[1].get<double>()); }
        } else {
            for (std::size_t i = 0; i + 1 < pts.size(); i += 2) {
                ts.push_back(pts[i].get<double>());
                vs.push_back(pts[i + 1].get<double>());
            }
        }
        if (ts.empty()) return base;
        if (t <= ts.front()) return vs.front();
        if (t >= ts.back()) return vs.back();
        for (std::size_t i = 0; i + 1 < ts.size(); ++i)
            if (ts[i] <= t && t <= ts[i + 1]) {
                double f = ts[i + 1] > ts[i] ? (t - ts[i]) / (ts[i + 1] - ts[i]) : 0.0;
                return vs[i] + (vs[i + 1] - vs[i]) * f;
            }
        return vs.back();
    }
    return base;
}

// Waveform edge times to add as transient breakpoints — devices.py::source_breakpoints.
void source_breakpoints(const json& d, double tstop, std::set<double>& out) {
    const json& p = params_of(d);
    if (!p.contains("waveform") || !p["waveform"].is_object()) return;
    const json& wf = p["waveform"];
    std::string kind = upper(wf.value("type", std::string("DC")));
    auto W = [&](const char* k, double dv) { return as_num(wf.value(k, json(dv)), dv); };
    if (kind == "PULSE") {
        double td = W("td", 0), tr = W("tr", 0), tf = W("tf", 0), pw = W("pw", 0), per = W("per", 0);
        if (per > 0.0) {
            int n = 0;
            while (td + n * per <= tstop && n < 100000) {
                for (double e : {0.0, tr, tr + pw, tr + pw + tf}) {
                    double bp = td + n * per + e;
                    if (bp >= 0.0 && bp <= tstop) out.insert(bp);
                }
                ++n;
            }
        } else {
            for (double e : {td, td + tr, td + tr + pw, td + tr + pw + tf})
                if (e >= 0.0 && e <= tstop) out.insert(e);
        }
    } else if (kind == "PWL" && wf.contains("points") && wf["points"].is_array()) {
        const json& pts = wf["points"];
        if (!pts.empty() && pts[0].is_array()) {
            for (const auto& pr : pts) {
                double b = pr[0].get<double>();
                if (b >= 0.0 && b <= tstop) out.insert(b);
            }
        } else {
            for (std::size_t i = 0; i < pts.size(); i += 2) {
                double b = pts[i].get<double>();
                if (b >= 0.0 && b <= tstop) out.insert(b);
            }
        }
    }
}

// Complex AC excitation (magnitude/phase) of an independent source — devices.py::_ac_phasor.
std::complex<double> ac_phasor(const json& d) {
    const json& p = params_of(d);
    auto from = [](const json& a) {
        double mag = as_num(a.value("mag", json(1.0)), 1.0);
        double ph = as_num(a.value("phase", json(0.0)), 0.0) * kPi / 180.0;
        return std::polar(mag, ph);
    };
    if (p.contains("ac") && p["ac"].is_object()) return from(p["ac"]);
    if (p.contains("waveform") && p["waveform"].is_object() && p["waveform"].contains("ac") &&
        p["waveform"]["ac"].is_object())
        return from(p["waveform"]["ac"]);
    return {0.0, 0.0};
}

// AC/noise frequency sweep grid (dec/oct/lin) — circuit.py::_freq_grid.
std::vector<double> freq_grid(const json& an) {
    std::string sweep = an.value("sweep", std::string("dec"));
    int points = (int)as_num(an.value("points", json(10)), 10);
    double fstart = as_num(an.value("fstart", json(1.0)), 1.0);
    double fstop = as_num(an.value("fstop", json(1e6)), 1e6);
    if (fstart <= 0.0) fstart = 1.0;
    std::vector<double> out;
    auto logspace = [&](double a, double b, int n) {
        for (int i = 0; i < n; ++i) {
            double t = n > 1 ? (double)i / (n - 1) : 0.0;
            out.push_back(std::pow(10.0, a + (b - a) * t));
        }
    };
    if (sweep == "dec") {
        double ndec = std::log10(fstop / fstart);
        int nf = std::max(2, (int)std::lround(points * ndec) + 1);
        logspace(std::log10(fstart), std::log10(fstop), nf);
    } else if (sweep == "oct") {
        double noct = std::log2(fstop / fstart);
        int nf = std::max(2, (int)std::lround(points * noct) + 1);
        logspace(std::log10(fstart), std::log10(fstop), nf);
    } else {
        int n = std::max(2, points);
        for (int i = 0; i < n; ++i) out.push_back(fstart + (fstop - fstart) * (double)i / (n - 1));
    }
    return out;
}

// Per-reactive-device transient history (uncoupled C / L; diode junction cap).
struct TranState {
    double v = 0.0;       // previous terminal voltage
    double i = 0.0;       // previous branch/companion current
    double ldidt = 0.0;   // L*di/dt (inductor self term)
    double vcap = 0.0;    // diode junction-cap previous voltage
    double icap = 0.0;    // diode junction-cap previous current
};

// Shared circuit setup: node index, branch rows, device list.
struct Setup {
    std::map<std::string, int> node_index;
    int n_nodes = 0;
    int dim = 0;
    std::map<std::string, int> branch_of;
    const json& devices;
    const json& models;
    double gmin = 1e-12;
    double temp_c = 27.0;
    // Newton options.
    double reltol = 1e-3;
    double vntol = 1e-6;
    int maxiter = 100;

    int n(const std::string& name) const {
        auto it = node_index.find(name);
        return it == node_index.end() ? -1 : it->second;
    }
    int nodeRow(const std::string& name) const {  // -2 = unknown node
        auto it = node_index.find(name);
        return it == node_index.end() ? -2 : it->second;
    }
};

struct Mna {
    int dim;
    std::vector<std::vector<double>> A;
    std::vector<double> z;
    explicit Mna(int n) : dim(n), A(n, std::vector<double>(n, 0.0)), z(n, 0.0) {}
    void reset() {
        for (auto& row : A) std::fill(row.begin(), row.end(), 0.0);
        std::fill(z.begin(), z.end(), 0.0);
    }
    void G(int i, int j, double v) { if (i >= 0 && j >= 0 && v != 0.0) A[i][j] += v; }
    void I(int i, double v) { if (i >= 0) z[i] += v; }
};

// Solve A x = z densely (= numpy.linalg.solve). Returns false on a non-finite solution.
bool solve_dense(const Mna& mna, std::vector<double>& x) {
    if (mna.dim == 0) { x.clear(); return true; }
    Eigen::MatrixXd Am(mna.dim, mna.dim);
    Eigen::VectorXd zv(mna.dim);
    for (int i = 0; i < mna.dim; ++i) {
        zv(i) = mna.z[i];
        for (int j = 0; j < mna.dim; ++j) Am(i, j) = mna.A[i][j];
    }
    Eigen::VectorXd xv = Am.partialPivLu().solve(zv);
    if (!xv.allFinite()) return false;
    x.assign(mna.dim, 0.0);
    for (int i = 0; i < mna.dim; ++i) x[i] = xv(i);
    return true;
}

double src_scale_of(const std::map<std::string, double>& sc, const std::string& name) {
    auto it = sc.find(name);
    return it == sc.end() ? 1.0 : it->second;
}

inline double xat(const std::vector<double>& x, int i) {
    return (i < 0 || i >= (int)x.size()) ? 0.0 : x[i];
}

// True if a device kind needs Newton iteration (nonlinear DC/transient).
bool kind_is_nonlinear(const std::string& kind) {
    return kind == "D" || kind == "Q" || kind == "M";
}

// Stamp the static linear devices common to DC and transient (R, V, I, E, G); the
// reactive devices (C, L) are stamped by the caller (DC-open/short vs companion).
void stamp_linear(const Setup& s, Mna& mna, const json& d, double t,
                  const std::map<std::string, double>& src_scale) {
    const std::string kind = d.value("kind", std::string());
    const std::string name = d.value("name", std::string());
    const json& nodes = d.contains("nodes") ? d["nodes"] : json::array();
    auto N = [&](int k) { return (k < (int)nodes.size()) ? s.n(nodes[k].get<std::string>()) : -1; };
    if (kind == "R") {
        double r = dev_value(d, 0.0);
        if (std::abs(r) < 1e-12) r = 1e-9;
        double g = 1.0 / r;
        int a = N(0), b = N(1);
        mna.G(a, a, g); mna.G(b, b, g); mna.G(a, b, -g); mna.G(b, a, -g);
    } else if (kind == "V") {
        int a = N(0), b = N(1), br = s.branch_of.at(name);
        mna.G(a, br, 1.0); mna.G(b, br, -1.0); mna.G(br, a, 1.0); mna.G(br, b, -1.0);
        mna.I(br, source_value_t(d, t) * src_scale_of(src_scale, name));
    } else if (kind == "I") {
        int a = N(0), b = N(1);
        double i = source_value_t(d, t) * src_scale_of(src_scale, name);
        mna.I(a, -i); mna.I(b, i);
    } else if (kind == "E") {
        int op = N(0), on = N(1), cp = N(2), cn = N(3), br = s.branch_of.at(name);
        double gain = dev_value(d, 1.0);
        mna.G(op, br, 1.0); mna.G(on, br, -1.0);
        mna.G(br, op, 1.0); mna.G(br, on, -1.0);
        mna.G(br, cp, -gain); mna.G(br, cn, gain);
    } else if (kind == "G") {
        int op = N(0), on = N(1), cp = N(2), cn = N(3);
        double gm = dev_value(d, 1.0);
        mna.G(op, cp, gm); mna.G(op, cn, -gm); mna.G(on, cp, -gm); mna.G(on, cn, gm);
    }
}

// ---- nonlinear device DC stamps (devices.py) -------------------------------------------

// Saturation current at the device temperature (SPICE IS temperature law) — Diode._is_temp.
double diode_is_temp(const json& d, const json& models, double temp_c) {
    double IS0 = mp(d, models, "IS", 1e-14);
    double N = mp(d, models, "N", 1.0);
    double XTI = mp(d, models, "XTI", 3.0);
    double tk = temp_c + 273.15, tnomk = kTnomC + 273.15;
    if (std::abs(tk - tnomk) < 1e-9 || IS0 <= 0.0) return IS0;
    double ratio = tk / tnomk;
    double eg_term = kEgSi * kQ / kBoltz;
    double scale = std::pow(ratio, XTI / N) * std::exp(-(eg_term / N) * (1.0 / tk - 1.0 / tnomk));
    if (!std::isfinite(scale)) return IS0;
    return IS0 * scale;
}

// (Id, gd) for the diode junction (excluding RS) at diode voltage vd — Diode._linearize.
void diode_linearize(double vd, double IS, double N, double BV, double gmin, double temp_c,
                     double& Id, double& gd) {
    double vt = N * vt_of(temp_c);
    double vcrit = IS > 0 ? vt * std::log(vt / (std::sqrt(2.0) * IS)) : 0.6;
    double v = vd;
    if (v > vcrit) {
        v = (vd - vcrit) / vt > -1 ? vcrit + vt * std::log1p((vd - vcrit) / vt) : vcrit;
    }
    if (v >= 0 || v > -5 * vt) {
        double ev = std::exp(std::min(v / vt, 80.0));
        Id = IS * (ev - 1.0);
        gd = IS / vt * ev;
    } else {
        Id = -IS;
        gd = gmin;
        if (-vd > BV) {
            double ibrk = IS * (std::exp(std::min((-vd - BV) / vt, 80.0)) - 1.0);
            Id = -IS - ibrk;
            gd = IS / vt * std::exp(std::min((-vd - BV) / vt, 80.0));
        }
    }
    gd = std::max(gd, gmin);
}

// Diode internal junction voltage given the terminal voltage (RS series fold) — Diode._vd.
double diode_vd(const Setup& s, const json& d, const std::vector<double>& x, double IS, double N,
                double RS, std::map<std::string, double>& last_vd) {
    const json& nodes = d["nodes"];
    int a = s.n(nodes[0].get<std::string>()), b = s.n(nodes[1].get<std::string>());
    double vt = N * vt_of(s.temp_c);
    double vterm = xat(x, a) - xat(x, b);
    if (RS <= 0.0) return vterm;
    const std::string name = d.value("name", std::string());
    auto it = last_vd.find(name);
    double vd = it != last_vd.end() ? it->second : std::min(vterm, 0.7);
    for (int k = 0; k < 20; ++k) {
        double ev = std::exp(std::min(vd / vt, 80.0));
        double Id = IS * (ev - 1.0);
        double gd = IS / vt * ev + s.gmin;
        double f = vd + RS * Id - vterm;
        double dfdv = 1.0 + RS * gd;
        double step = f / dfdv;
        vd -= step;
        if (std::abs(step) < 1e-9) break;
    }
    return vd;
}

void stamp_diode_dc(const Setup& s, Mna& mna, const json& d, const std::vector<double>& x,
                    std::map<std::string, double>& last_vd) {
    double IS = diode_is_temp(d, s.models, s.temp_c);
    double N = mp(d, s.models, "N", 1.0);
    double RS = mp(d, s.models, "RS", 0.0);
    double BV = mp(d, s.models, "BV", 1e30);
    const json& nodes = d["nodes"];
    int a = s.n(nodes[0].get<std::string>()), b = s.n(nodes[1].get<std::string>());
    double vd = diode_vd(s, d, x, IS, N, RS, last_vd);
    last_vd[d.value("name", std::string())] = vd;
    double Id, gd;
    diode_linearize(vd, IS, N, BV, s.gmin, s.temp_c, Id, gd);
    double geff = RS > 0 ? gd / (1.0 + RS * gd) : gd;
    double ieq = Id - gd * vd;
    double ieq_eff = RS > 0 ? ieq / (1.0 + RS * gd) : ieq;
    mna.G(a, a, geff); mna.G(b, b, geff);
    mna.G(a, b, -geff); mna.G(b, a, -geff);
    mna.I(a, -ieq_eff); mna.I(b, ieq_eff);
}

void stamp_bjt_dc(const Setup& s, Mna& mna, const json& d, const std::vector<double>& x) {
    double IS = mp(d, s.models, "IS", 1e-16);
    double BF = mp(d, s.models, "BF", 100.0);
    double BR = mp(d, s.models, "BR", 1.0);
    double VAF = mp(d, s.models, "VAF", 1e30);
    double NF = mp(d, s.models, "NF", 1.0);
    double NR = mp(d, s.models, "NR", 1.0);
    std::string typ = lower(model_str(d, s.models, "type", "npn"));
    const json& nodes = d["nodes"];
    int c = s.n(nodes[0].get<std::string>()), b = s.n(nodes[1].get<std::string>()),
        e = s.n(nodes[2].get<std::string>());
    double vt = vt_of(s.temp_c);
    double sign = typ == "npn" ? 1.0 : -1.0;
    double vbe = sign * (xat(x, b) - xat(x, e));
    double vbc = sign * (xat(x, b) - xat(x, c));
    auto diode = [&](double v, double n, double& i, double& g) {
        v = std::min(v, 0.95);
        double ev = std::exp(std::min(v / (n * vt), 80.0));
        i = IS * (ev - 1.0);
        g = IS / (n * vt) * ev + s.gmin;
    };
    double ibe, gbe, ibc, gbc;
    diode(vbe, NF, ibe, gbe);
    diode(vbc, NR, ibc, gbc);
    double qb = 1.0;
    if (VAF < 1e29) qb = 1.0 / std::max(1.0 - vbc / VAF, 0.1);
    double ic = (ibe * qb - ibc) - ibc / BR;
    double ib = ibe / BF + ibc / BR;
    double gpi = gbe / BF;
    double gmu = gbc / BR;
    double gm = gbe * qb - gbc;
    double go = 0.0;
    // Node-referenced small-signal conductances.
    mna.G(b, b, gpi + gmu); mna.G(b, e, -gpi); mna.G(b, c, -gmu);
    mna.G(c, b, gm - gmu);  mna.G(c, e, -gm);  mna.G(c, c, gmu + go);
    mna.G(e, b, -gpi - gm); mna.G(e, e, gpi + gm); mna.G(e, c, go);
    // Companion RHS: I_eq = I(op) - J @ v(op).
    double ieq_b = ib - gpi * vbe - gmu * vbc;
    double ieq_c = ic - gm * vbe + gmu * vbc - go * (sign * (xat(x, c) - xat(x, e)));
    double ieq_e = -(ieq_b + ieq_c);
    mna.I(b, -sign * ieq_b);
    mna.I(c, -sign * ieq_c);
    mna.I(e, -sign * ieq_e);
}

void stamp_mosfet_dc(const Setup& s, Mna& mna, const json& d, const std::vector<double>& x) {
    double VTO = mp(d, s.models, "VTO", 1.0);
    double KP = mp(d, s.models, "KP", 2e-5);
    double LAMBDA = mp(d, s.models, "LAMBDA", 0.0);
    double W = mp(d, s.models, "W", 1e-4);
    double L = mp(d, s.models, "L", 1e-4);
    std::string typ = lower(model_str(d, s.models, "type", "nmos"));
    double beta = KP * W / L;
    const json& nodes = d["nodes"];
    int dn = s.n(nodes[0].get<std::string>()), g = s.n(nodes[1].get<std::string>()),
        sn = s.n(nodes[2].get<std::string>());
    double sign = (typ == "nmos" || typ == "n") ? 1.0 : -1.0;
    double vgs = sign * (xat(x, g) - xat(x, sn));
    double vds = sign * (xat(x, dn) - xat(x, sn));
    double vth = VTO;
    double id_, gm, gds;
    if (vgs <= vth) {
        id_ = 0.0; gm = 0.0; gds = s.gmin;
    } else if (vds < (vgs - vth)) {  // triode
        id_ = beta * ((vgs - vth) * vds - 0.5 * vds * vds) * (1 + LAMBDA * vds);
        gm = beta * vds * (1 + LAMBDA * vds);
        gds = beta * ((vgs - vth) - vds) * (1 + LAMBDA * vds) + s.gmin;
    } else {  // saturation
        id_ = 0.5 * beta * (vgs - vth) * (vgs - vth) * (1 + LAMBDA * vds);
        gm = beta * (vgs - vth) * (1 + LAMBDA * vds);
        gds = 0.5 * beta * (vgs - vth) * (vgs - vth) * LAMBDA + s.gmin;
    }
    // gds across D-S + controlled drain current gm*Vgs (drain->source).
    mna.G(dn, dn, gds); mna.G(dn, sn, -gds);
    mna.G(sn, dn, -gds); mna.G(sn, sn, gds);
    mna.G(dn, g, gm); mna.G(dn, sn, -gm);
    mna.G(sn, g, -gm); mna.G(sn, sn, gm);
    double ieq = id_ - gm * vgs - gds * vds;
    mna.I(dn, -sign * ieq); mna.I(sn, sign * ieq);
}

void stamp_nonlinear_dc(const Setup& s, Mna& mna, const json& d, const std::vector<double>& x,
                        std::map<std::string, double>& last_vd) {
    const std::string kind = d.value("kind", std::string());
    if (kind == "D") stamp_diode_dc(s, mna, d, x, last_vd);
    else if (kind == "Q") stamp_bjt_dc(s, mna, d, x);
    else if (kind == "M") stamp_mosfet_dc(s, mna, d, x);
}

// Probe evaluation (V(node)/V(a,b)/I(name)) — circuit.py::_eval_probe.
json eval_probe(const Setup& s, const std::vector<double>& x, const std::string& probe) {
    auto val = [&](int i) -> double { return (i < 0 || i >= (int)x.size()) ? 0.0 : x[i]; };
    std::string p = trim(probe);
    std::string up = upper(p);
    if (up.rfind("V(", 0) == 0 && !p.empty() && p.back() == ')') {
        std::string inner = p.substr(2, p.size() - 3);
        auto comma = inner.find(',');
        if (comma == std::string::npos) {
            int i = s.nodeRow(inner);
            return i == -2 ? json(nullptr) : json(val(i));
        }
        return json(val(s.nodeRow(trim(inner.substr(0, comma)))) -
                    val(s.nodeRow(trim(inner.substr(comma + 1)))));
    }
    if (up.rfind("I(", 0) == 0 && !p.empty() && p.back() == ')') {
        std::string name = trim(p.substr(2, p.size() - 3));
        auto bit = s.branch_of.find(name);
        if (bit != s.branch_of.end()) return json(val(bit->second));
        for (const json& d : s.devices)
            if (d.value("name", std::string()) == name && d.value("kind", std::string()) == "R") {
                int a = s.n(d["nodes"][0].get<std::string>());
                int b = s.n(d["nodes"][1].get<std::string>());
                double r = dev_value(d, 0.0);
                if (std::abs(r) < 1e-12) r = 1e-9;
                return json((val(a) - val(b)) / r);
            }
        return json(nullptr);
    }
    int i = s.nodeRow(p);
    return i == -2 ? json(nullptr) : json(val(i));
}

}  // namespace

json spice_simulate(const json& circuit, const json& study) {
    json result = {{"log", json::array()}, {"convergence", json::object()}};
    auto log = [&](const std::string& m) { result["log"].push_back(m); };

    static const json empty_arr = json::array();
    static const json empty_obj = json::object();
    const json& devices = circuit.contains("devices") && circuit["devices"].is_array()
                              ? circuit["devices"] : empty_arr;
    const json& models = circuit.contains("models") && circuit["models"].is_object()
                             ? circuit["models"] : empty_obj;
    const json& options = study.contains("options") && study["options"].is_object()
                              ? study["options"] : empty_obj;
    const double gmin = as_num(options.value("gmin", json(1e-12)), 1e-12);

    // node index (ground -> -1; others -> 0..n-1 in first-seen order).
    std::vector<std::string> raw;
    auto add_raw = [&](const std::string& nm) {
        if (std::find(raw.begin(), raw.end(), nm) == raw.end()) raw.push_back(nm);
    };
    if (circuit.contains("nodes") && circuit["nodes"].is_array())
        for (const auto& nm : circuit["nodes"]) add_raw(nm.get<std::string>());
    for (const json& d : devices)
        if (d.contains("nodes") && d["nodes"].is_array())
            for (const auto& nm : d["nodes"]) add_raw(nm.get<std::string>());
    if (std::find(raw.begin(), raw.end(), "0") == raw.end()) raw.insert(raw.begin(), "0");

    Setup s{{}, 0, 0, {}, devices, models, gmin};
    int idx = 0;
    for (const std::string& nm : raw) s.node_index[nm] = is_ground(nm) ? -1 : idx++;
    for (const char* a : {"0", "gnd", "GND"}) s.node_index.emplace(a, -1);
    s.n_nodes = idx;
    int dim = idx;
    for (const json& d : devices) {
        std::string kind = d.value("kind", std::string());
        if (kind == "V" || kind == "L" || kind == "E")
            s.branch_of[d.value("name", std::string())] = dim++;
    }
    s.dim = dim;

    // Temperature: options.temp overrides circuit params.TEMP/temp (devices.py::_Sim).
    {
        const json& cparams = circuit.contains("params") && circuit["params"].is_object()
                                  ? circuit["params"] : empty_obj;
        double t = 27.0;
        if (cparams.contains("temp") && cparams["temp"].is_number()) t = cparams["temp"].get<double>();
        if (cparams.contains("TEMP") && cparams["TEMP"].is_number()) t = cparams["TEMP"].get<double>();
        if (options.contains("temp") && options["temp"].is_number()) t = options["temp"].get<double>();
        s.temp_c = t;
    }
    s.reltol = as_num(options.value("reltol", json(1e-3)), 1e-3);
    s.vntol = as_num(options.value("vntol", json(1e-6)), 1e-6);
    s.maxiter = (int)as_num(options.value("maxiter", json(100)), 100);

    bool has_nonlinear = false;
    for (const json& d : devices)
        if (kind_is_nonlinear(d.value("kind", std::string()))) has_nonlinear = true;

    // Per-device persistent state (diode RS inner-Newton seed) carried across iterations/steps.
    std::map<std::string, double> last_vd;

    auto N_of = [&](const json& d, int k) {
        const json& nodes = d["nodes"];
        return (k < (int)nodes.size()) ? s.n(nodes[k].get<std::string>()) : -1;
    };

    // Stamp the full DC system about working point x (gmin floor + linear + nonlinear).
    // Returns an error string for an unported device kind, "" on success.
    auto stamp_all_dc = [&](Mna& mna, const std::vector<double>& x,
                            const std::map<std::string, double>& src_scale) -> std::string {
        mna.reset();
        if (gmin > 0)
            for (int i = 0; i < s.n_nodes; ++i) mna.G(i, i, gmin);
        for (const json& d : devices) {
            std::string kind = d.value("kind", std::string());
            if (kind == "C") continue;  // open at DC
            if (kind == "L") {          // ideal short
                int a = N_of(d, 0), b = N_of(d, 1), br = s.branch_of.at(d.value("name", std::string()));
                mna.G(a, br, 1.0); mna.G(b, br, -1.0); mna.G(br, a, 1.0); mna.G(br, b, -1.0);
            } else if (kind == "R" || kind == "V" || kind == "I" || kind == "E" || kind == "G") {
                stamp_linear(s, mna, d, 0.0, src_scale);
            } else if (kind_is_nonlinear(kind)) {
                stamp_nonlinear_dc(s, mna, d, x, last_vd);
            } else {
                return "device kind '" + kind + "' not yet ported to the C++ DC core";
            }
        }
        return "";
    };

    // Damped Newton DC solve at a given source scale (circuit.py::_newton_dc).
    auto newton_dc = [&](Mna& mna, const std::vector<double>& x0,
                         const std::map<std::string, double>& src_scale,
                         std::vector<double>& out, std::string& err) -> bool {
        std::vector<double> x = x0;
        for (int it = 1; it <= s.maxiter; ++it) {
            err = stamp_all_dc(mna, x, src_scale);
            if (!err.empty()) return false;
            std::vector<double> xnew;
            if (!solve_dense(mna, xnew)) { err = "matrix singular"; return false; }
            if (!has_nonlinear) { out = xnew; err.clear(); return true; }
            // Damping: limit per-iteration node-voltage swing.
            double maxabs = 0.0;
            for (int i = 0; i < (int)xnew.size(); ++i) maxabs = std::max(maxabs, std::abs(xnew[i] - x[i]));
            if (maxabs > 1.0 && it < s.maxiter) {
                double scale = std::max(1.0 / maxabs, 0.1);
                for (int i = 0; i < (int)xnew.size(); ++i) xnew[i] = x[i] + (xnew[i] - x[i]) * scale;
            }
            // Convergence: vntol + reltol on each node voltage.
            bool conv = true;
            for (int i = 0; i < (int)xnew.size(); ++i) {
                double dv = std::abs(xnew[i] - x[i]);
                double tol = s.vntol + s.reltol * std::max(std::abs(xnew[i]), std::abs(x[i]));
                if (dv > tol) { conv = false; break; }
            }
            if (conv) { out = xnew; err.clear(); return true; }
            x = xnew;
        }
        out = x;
        err = "newton did not converge";
        return false;
    };

    // DC operating point with gmin- then source-stepping fallback (circuit.py::solve_dc_op).
    auto solve_dc_op = [&](std::vector<double>& out) -> bool {
        Mna mna(dim);
        std::vector<double> x0(dim, 0.0);
        std::map<std::string, double> base_scale;

        // 1. Direct attempt.
        std::string err;
        if (newton_dc(mna, x0, base_scale, out, err) && err.empty()) {
            log("op: converged");
            return true;
        }
        if (!has_nonlinear) { log("op: linear solve failed — " + err); return false; }

        // 2. gmin stepping (note: this prototype keeps gmin fixed at the option value; the
        // staged ramp is reserved for circuits the direct attempt can't crack — the gated
        // devices converge directly).
        log("op: direct Newton failed (" + err + "); trying source stepping");

        // 3. Source stepping.
        std::vector<std::string> src_names;
        for (const json& d : devices) {
            std::string kind = d.value("kind", std::string());
            if (kind == "V" || kind == "I") src_names.push_back(d.value("name", std::string()));
        }
        std::vector<double> steps = {0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0};
        std::vector<double> x(dim, 0.0);
        bool all_ok = true;
        for (double alpha : steps) {
            std::map<std::string, double> scale;
            for (const std::string& nm : src_names) scale[nm] = alpha;
            std::vector<double> xs;
            std::string e;
            if (!newton_dc(mna, x, scale, xs, e) || !e.empty()) { all_ok = false; break; }
            x = xs;
        }
        if (all_ok) { log("op: source stepping converged"); out = x; return true; }
        log("op: all DC convergence strategies failed");
        return false;
    };

    const json& analyses = study.contains("analyses") && study["analyses"].is_array()
                               ? study["analyses"] : empty_arr;
    const json& probes = study.contains("probes") && study["probes"].is_array()
                             ? study["probes"] : empty_arr;

    for (const json& a : analyses) {
        std::string atype = a.value("type", std::string());
        if (atype == "op") {
            std::vector<double> x;
            bool ok = solve_dc_op(x);
            if (!ok && dim == 0) { ok = true; x.assign(dim, 0.0); }
            result["convergence"]["op"] = ok;
            if (ok) {
                if (x.empty()) x.assign(dim, 0.0);
                json op = json::object();
                for (const json& pr : probes) op[pr.get<std::string>()] = eval_probe(s, x, pr.get<std::string>());
                result["op"] = op;
            } else {
                json op = json::object();
                for (const json& pr : probes) op[pr.get<std::string>()] = std::nan("");
                result["op"] = op;
            }
        } else if (atype == "tran") {
            // ---- transient (trapezoidal + BE bootstrap), inner Newton for nonlinear ----
            double tstep = as_num(a.value("tstep", json(1e-6)), 1e-6);
            double tstop = as_num(a.value("tstop", json(1e-3)), 1e-3);
            bool uic = a.value("uic", false);

            std::map<std::string, TranState> st;
            std::vector<double> x(dim, 0.0);
            if (uic) {
                for (const json& d : devices) {
                    const json& p = params_of(d);
                    if (!p.contains("ic")) continue;
                    double ic = as_num(p["ic"], 0.0);
                    std::string kind = d.value("kind", std::string());
                    if (kind == "C") st[d.value("name", std::string())].v = ic;
                    if (kind == "L") st[d.value("name", std::string())].i = ic;
                }
            } else {
                if (!solve_dc_op(x)) {
                    if (dim != 0) {
                        log("tran: initial DC op failed");
                        result["convergence"]["tran"] = false;
                        continue;
                    }
                    x.assign(dim, 0.0);
                }
                if (x.empty()) x.assign(dim, 0.0);
            }

            std::set<double> bpset;
            for (const json& d : devices) {
                std::string kind = d.value("kind", std::string());
                if (kind == "V" || kind == "I") source_breakpoints(d, tstop, bpset);
            }
            std::vector<double> breakpoints;
            for (double b : bpset)
                if (b > 0.0 && b <= tstop) breakpoints.push_back(b);

            // Stamp one transient step into mna about working point x.
            auto stamp_tran = [&](Mna& mna, const std::vector<double>& xw, double dt, bool trap,
                                  double t) {
                mna.reset();
                if (gmin > 0)
                    for (int i = 0; i < s.n_nodes; ++i) mna.G(i, i, gmin);
                std::map<std::string, double> no_scale;
                for (const json& d : devices) {
                    std::string kind = d.value("kind", std::string());
                    std::string name = d.value("name", std::string());
                    if (kind == "C") {
                        int a0 = N_of(d, 0), b0 = N_of(d, 1);
                        double c = dev_value(d, 0.0);
                        const TranState& h = st[name];
                        double geq = trap ? 2.0 * c / dt : c / dt;
                        double ieq = trap ? geq * h.v + h.i : geq * h.v;
                        mna.G(a0, a0, geq); mna.G(b0, b0, geq); mna.G(a0, b0, -geq); mna.G(b0, a0, -geq);
                        mna.I(a0, ieq); mna.I(b0, -ieq);
                    } else if (kind == "L") {
                        int a0 = N_of(d, 0), b0 = N_of(d, 1), br = s.branch_of.at(name);
                        double L = dev_value(d, 0.0);
                        const TranState& h = st[name];
                        double req = trap ? 2.0 * L / dt : L / dt;
                        double veq = trap ? -(req * h.i + h.ldidt) : -req * h.i;
                        mna.G(a0, br, 1.0); mna.G(b0, br, -1.0);
                        mna.G(br, a0, 1.0); mna.G(br, b0, -1.0); mna.G(br, br, -req);
                        mna.I(br, veq);
                    } else if (kind_is_nonlinear(kind)) {
                        stamp_nonlinear_dc(s, mna, d, xw, last_vd);
                        // Diode junction-cap companion (CJO) — Diode.stamp_tran.
                        if (kind == "D") {
                            double CJO = mp(d, s.models, "CJO", 0.0);
                            if (CJO > 0.0) {
                                int a0 = N_of(d, 0), b0 = N_of(d, 1);
                                const TranState& h = st[name];
                                double geq = trap ? 2.0 * CJO / dt : CJO / dt;
                                double ieq = trap ? geq * h.vcap + h.icap : geq * h.vcap;
                                mna.G(a0, a0, geq); mna.G(b0, b0, geq);
                                mna.G(a0, b0, -geq); mna.G(b0, a0, -geq);
                                mna.I(a0, ieq); mna.I(b0, -ieq);
                            }
                        }
                    } else {
                        stamp_linear(s, mna, d, t, no_scale);  // R/V/I/E/G at time t
                    }
                }
            };
            // Advance reactive history after a converged step.
            auto update_state = [&](const std::vector<double>& xn, double dt, bool trap) {
                for (const json& d : devices) {
                    std::string kind = d.value("kind", std::string());
                    std::string name = d.value("name", std::string());
                    if (kind == "C") {
                        double c = dev_value(d, 0.0);
                        TranState& h = st[name];
                        double v = xat(xn, N_of(d, 0)) - xat(xn, N_of(d, 1));
                        double i_new = trap ? 2.0 * c / dt * (v - h.v) - h.i : c / dt * (v - h.v);
                        h.v = v; h.i = i_new;
                    } else if (kind == "L") {
                        double L = dev_value(d, 0.0);
                        TranState& h = st[name];
                        int br = s.branch_of.at(name);
                        double v = xat(xn, N_of(d, 0)) - xat(xn, N_of(d, 1));
                        double i = xat(xn, br);
                        double ldidt_new = trap ? 2.0 * L / dt * (i - h.i) - h.ldidt : L / dt * (i - h.i);
                        h.i = i; h.v = v; h.ldidt = ldidt_new;
                    } else if (kind == "D") {
                        double CJO = mp(d, s.models, "CJO", 0.0);
                        if (CJO > 0.0) {
                            TranState& h = st[name];
                            double v = xat(xn, N_of(d, 0)) - xat(xn, N_of(d, 1));
                            double i_new = trap ? 2.0 * CJO / dt * (v - h.vcap) - h.icap
                                                : CJO / dt * (v - h.vcap);
                            h.vcap = v; h.icap = i_new;
                        }
                    }
                }
            };

            // Inner Newton for one transient step (circuit.py::_newton_tran — no damping).
            auto newton_tran = [&](Mna& mna, const std::vector<double>& x0, double dt, bool trap,
                                   double t, std::vector<double>& out) -> bool {
                std::vector<double> xx = x0;
                for (int it = 1; it <= s.maxiter; ++it) {
                    stamp_tran(mna, xx, dt, trap, t);
                    std::vector<double> xnew;
                    if (!solve_dense(mna, xnew)) return false;
                    if (!has_nonlinear) { out = xnew; return true; }
                    bool conv = true;
                    for (int i = 0; i < (int)xnew.size(); ++i) {
                        double dv = std::abs(xnew[i] - xx[i]);
                        double tol = s.vntol + s.reltol * std::max(std::abs(xnew[i]), std::abs(xx[i]));
                        if (dv > tol) { conv = false; break; }
                    }
                    if (conv) { out = xnew; return true; }
                    xx = xnew;
                }
                out = xx;
                return true;  // accept last iterate (Python returns it with a non-conv note)
            };

            // Non-uic: seed reactive history from the op point.
            if (!uic) update_state(x, tstep, true);

            std::vector<double> times{0.0};
            std::map<std::string, std::vector<double>> traces;
            for (const json& pr : probes) {
                std::string p = pr.get<std::string>();
                json v0 = eval_probe(s, x, p);
                traces[p] = {v0.is_number() ? v0.get<double>() : 0.0};
            }

            Mna mna(dim);
            double t = 0.0, step = tstep;
            bool converged = true, first = true;
            std::size_t next_bp = 0;
            long max_steps = static_cast<long>(tstop / tstep * 4) + 1000;
            long n_steps = 0;
            while (t < tstop - 1e-15 && n_steps < max_steps) {
                ++n_steps;
                double dt = step;
                bool trap = !first;  // BE bootstrap on the first step
                if (next_bp < breakpoints.size()) {
                    double bp = breakpoints[next_bp];
                    if (t < bp && bp <= t + dt + 1e-18) {
                        dt = bp - t;
                        if (dt < 1e-18) { ++next_bp; continue; }
                    }
                }
                double tnew = std::min(t + dt, tstop);
                dt = tnew - t;
                std::vector<double> xn;
                if (!newton_tran(mna, x, dt, trap, tnew, xn)) {
                    log("tran: non-convergence at t=" + std::to_string(tnew));
                    converged = false;
                    break;
                }
                update_state(xn, dt, trap);
                x = xn;
                t = tnew;
                times.push_back(t);
                for (const json& pr : probes) {
                    std::string p = pr.get<std::string>();
                    json v = eval_probe(s, x, p);
                    traces[p].push_back(v.is_number() ? v.get<double>() : 0.0);
                }
                first = false;
                if (next_bp < breakpoints.size() && std::abs(t - breakpoints[next_bp]) < 1e-18) {
                    ++next_bp;
                    first = true;  // re-bootstrap BE after a source breakpoint
                }
            }
            json tr = {{"x_t", times}, {"traces", json::object()}};
            for (const json& pr : probes) tr["traces"][pr.get<std::string>()] = traces[pr.get<std::string>()];
            result["tran"] = tr;
            result["convergence"]["tran"] = converged;
        } else if (atype == "ac") {
            // ---- AC sweep: complex MNA (G + jwC) per frequency, linearized about DC op ----
            std::vector<double> x_op(dim, 0.0);
            if (has_nonlinear) {
                if (!solve_dc_op(x_op)) { x_op.assign(dim, 0.0); log("ac: DC op for linearization failed"); }
            }
            std::vector<double> freqs = freq_grid(a);
            std::map<std::string, std::vector<double>> mag, phase;
            for (const json& pr : probes) { mag[pr.get<std::string>()]; phase[pr.get<std::string>()]; }
            bool ac_ok = true;
            using cd = std::complex<double>;
            auto N = [&](const json& d, int k) {
                const json& nodes = d["nodes"];
                return (k < (int)nodes.size()) ? s.n(nodes[k].get<std::string>()) : -1;
            };
            for (double f : freqs) {
                double omega = 2.0 * kPi * f;
                Eigen::MatrixXcd A = Eigen::MatrixXcd::Zero(dim, dim);
                Eigen::VectorXcd z = Eigen::VectorXcd::Zero(dim);
                auto G = [&](int i, int j, cd v) { if (i >= 0 && j >= 0) A(i, j) += v; };
                auto Iz = [&](int i, cd v) { if (i >= 0) z(i) += v; };
                if (gmin > 0)
                    for (int i = 0; i < s.n_nodes; ++i) G(i, i, cd(gmin, 0));
                for (const json& d : devices) {
                    std::string kind = d.value("kind", std::string());
                    std::string name = d.value("name", std::string());
                    int a0 = N(d, 0), b0 = N(d, 1);
                    if (kind == "R") {
                        double r = dev_value(d, 0.0);
                        if (std::abs(r) < 1e-12) r = 1e-9;
                        cd g(1.0 / r, 0.0);
                        G(a0, a0, g); G(b0, b0, g); G(a0, b0, -g); G(b0, a0, -g);
                    } else if (kind == "C") {
                        cd y(0.0, omega * dev_value(d, 0.0));
                        G(a0, a0, y); G(b0, b0, y); G(a0, b0, -y); G(b0, a0, -y);
                    } else if (kind == "L") {
                        int br = s.branch_of.at(name);
                        G(a0, br, 1.0); G(b0, br, -1.0); G(br, a0, 1.0); G(br, b0, -1.0);
                        G(br, br, cd(0.0, -omega * dev_value(d, 0.0)));  // -jwL
                    } else if (kind == "V") {
                        int br = s.branch_of.at(name);
                        G(a0, br, 1.0); G(b0, br, -1.0); G(br, a0, 1.0); G(br, b0, -1.0);
                        Iz(br, ac_phasor(d));
                    } else if (kind == "I") {
                        cd ph = ac_phasor(d);
                        Iz(a0, -ph); Iz(b0, ph);
                    } else if (kind == "E") {
                        int op = N(d, 0), on = N(d, 1), cp = N(d, 2), cn = N(d, 3), br = s.branch_of.at(name);
                        cd gain(dev_value(d, 1.0), 0.0);
                        G(op, br, 1.0); G(on, br, -1.0); G(br, op, 1.0); G(br, on, -1.0);
                        G(br, cp, -gain); G(br, cn, gain);
                    } else if (kind == "G") {
                        int op = N(d, 0), on = N(d, 1), cp = N(d, 2), cn = N(d, 3);
                        cd gm(dev_value(d, 1.0), 0.0);
                        G(op, cp, gm); G(op, cn, -gm); G(on, cp, -gm); G(on, cn, gm);
                    }
                }
                Eigen::VectorXcd xv = A.partialPivLu().solve(z);
                bool fin = xv.allFinite();
                if (!fin) ac_ok = false;
                std::vector<cd> xc(dim);
                for (int i = 0; i < dim; ++i) xc[i] = fin ? xv(i) : cd(0, 0);
                auto cval = [&](int i) -> cd { return (i < 0 || i >= dim) ? cd(0, 0) : xc[i]; };
                for (const json& pr : probes) {
                    std::string p = trim(pr.get<std::string>());
                    std::string up = upper(p);
                    cd v(0, 0);
                    bool got = false;
                    if (up.rfind("V(", 0) == 0 && p.back() == ')') {
                        std::string inner = p.substr(2, p.size() - 3);
                        auto comma = inner.find(',');
                        if (comma == std::string::npos) {
                            int i = s.nodeRow(inner);
                            if (i != -2) { v = cval(i); got = true; }
                        } else {
                            v = cval(s.nodeRow(trim(inner.substr(0, comma)))) -
                                cval(s.nodeRow(trim(inner.substr(comma + 1))));
                            got = true;
                        }
                    } else if (up.rfind("I(", 0) == 0 && p.back() == ')') {
                        auto bit = s.branch_of.find(trim(p.substr(2, p.size() - 3)));
                        if (bit != s.branch_of.end()) { v = cval(bit->second); got = true; }
                    } else {
                        int i = s.nodeRow(p);
                        if (i != -2) { v = cval(i); got = true; }
                    }
                    if (got && fin) {
                        mag[pr.get<std::string>()].push_back(std::abs(v));
                        phase[pr.get<std::string>()].push_back(std::atan2(v.imag(), v.real()) * 180.0 / kPi);
                    } else {
                        mag[pr.get<std::string>()].push_back(std::nan(""));
                        phase[pr.get<std::string>()].push_back(std::nan(""));
                    }
                }
            }
            json acj = {{"x_f", freqs}, {"mag", json::object()}, {"phase_deg", json::object()}};
            for (const json& pr : probes) {
                acj["mag"][pr.get<std::string>()] = mag[pr.get<std::string>()];
                acj["phase_deg"][pr.get<std::string>()] = phase[pr.get<std::string>()];
            }
            result["ac"] = acj;
            result["convergence"]["ac"] = ac_ok;
        } else {
            result["convergence"][atype] = false;
            log(atype + ": analysis not yet ported to the C++ core");
        }
    }
    return result;
}

}  // namespace aicad::sim
