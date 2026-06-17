// MNA circuit simulator implementation — see spice.h.
//
// Linear DC operating point + linear transient (trapezoidal / backward-Euler companion
// models). The node-index construction, branch-row allocation, per-device stamps, the
// transient time-stepping loop (BE bootstrap, source breakpoints), and probe evaluation
// mirror engine/sim/circuit.py + devices.py. The dense PartialPivLU solve matches numpy's
// linalg.solve for the small systems these circuits produce. Nonlinear devices (D/Q/M +
// Newton), AC sweep, mutual inductance (K), and the netlist parser are deferred.

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

bool is_ground(const std::string& n) { return n == "0" || n == "gnd" || n == "GND"; }

std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper((unsigned char)c));
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

// Per-reactive-device transient history (uncoupled C / L).
struct TranState {
    double v = 0.0;      // previous terminal voltage
    double i = 0.0;      // previous branch/companion current
    double ldidt = 0.0;  // L*di/dt (inductor self term)
};

// Shared circuit setup: node index, branch rows, device list.
struct Setup {
    std::map<std::string, int> node_index;
    int n_nodes = 0;
    int dim = 0;
    std::map<std::string, int> branch_of;
    const json& devices;
    double gmin;

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

// Stamp the static linear devices common to DC and transient (R, V, I, E, G); the
// reactive devices (C, L) are stamped by the caller (DC-open/short vs companion).
void stamp_linear(const Setup& s, Mna& mna, const json& d, double t) {
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
        mna.I(br, source_value_t(d, t));
    } else if (kind == "I") {
        int a = N(0), b = N(1);
        double i = source_value_t(d, t);
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
    const json& devices = circuit.contains("devices") && circuit["devices"].is_array()
                              ? circuit["devices"] : empty_arr;
    const json& options = study.contains("options") && study["options"].is_object()
                              ? study["options"] : json::object();
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

    Setup s{{}, 0, 0, {}, devices, gmin};
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

    const json& analyses = study.contains("analyses") && study["analyses"].is_array()
                               ? study["analyses"] : empty_arr;
    const json& probes = study.contains("probes") && study["probes"].is_array()
                             ? study["probes"] : empty_arr;

    // ---- DC operating point (linear) ----
    auto run_op = [&]() -> std::vector<double> {
        Mna mna(dim);
        if (gmin > 0)
            for (int i = 0; i < s.n_nodes; ++i) mna.G(i, i, gmin);
        std::string err;
        auto N = [&](const json& d, int k) {
            const json& nodes = d["nodes"];
            return (k < (int)nodes.size()) ? s.n(nodes[k].get<std::string>()) : -1;
        };
        for (const json& d : devices) {
            std::string kind = d.value("kind", std::string());
            if (kind == "C") continue;  // open at DC
            if (kind == "L") {          // ideal short
                int a = N(d, 0), b = N(d, 1), br = s.branch_of.at(d.value("name", std::string()));
                mna.G(a, br, 1.0); mna.G(b, br, -1.0); mna.G(br, a, 1.0); mna.G(br, b, -1.0);
            } else if (kind == "R" || kind == "V" || kind == "I" || kind == "E" || kind == "G") {
                stamp_linear(s, mna, d, 0.0);
            } else {
                err = "device kind '" + kind + "' not yet ported to the C++ DC core";
                break;
            }
        }
        std::vector<double> x;
        if (!err.empty()) { log("op: " + err); return {}; }
        if (!solve_dense(mna, x)) { log("op: matrix singular"); return {}; }
        return x;
    };

    bool did_op = false, did_tran = false;
    for (const json& a : analyses) {
        std::string atype = a.value("type", std::string());
        if (atype == "op") {
            std::vector<double> x = run_op();
            bool ok = !x.empty() || dim == 0;
            result["convergence"]["op"] = ok;
            if (ok) {
                if (x.empty()) x.assign(dim, 0.0);
                log("op: converged (linear)");
                json op = json::object();
                for (const json& pr : probes) op[pr.get<std::string>()] = eval_probe(s, x, pr.get<std::string>());
                result["op"] = op;
            }
            did_op = true;
        } else if (atype == "tran") {
            // ---- linear transient (trapezoidal + BE bootstrap) ----
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
                x = run_op();
                if (x.empty() && dim != 0) {
                    log("tran: initial DC op failed");
                    result["convergence"]["tran"] = false;
                    did_tran = true;
                    continue;
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

            // Stamp a transient step at (dt, method='trap'|'be', t) into mna.
            auto stamp_tran = [&](Mna& mna, double dt, bool trap, double t) {
                mna.reset();
                if (gmin > 0)
                    for (int i = 0; i < s.n_nodes; ++i) mna.G(i, i, gmin);
                auto N = [&](const json& d, int k) {
                    const json& nodes = d["nodes"];
                    return (k < (int)nodes.size()) ? s.n(nodes[k].get<std::string>()) : -1;
                };
                for (const json& d : devices) {
                    std::string kind = d.value("kind", std::string());
                    std::string name = d.value("name", std::string());
                    if (kind == "C") {
                        int a = N(d, 0), b = N(d, 1);
                        double c = dev_value(d, 0.0);
                        const TranState& h = st[name];
                        double geq = trap ? 2.0 * c / dt : c / dt;
                        double ieq = trap ? geq * h.v + h.i : geq * h.v;
                        mna.G(a, a, geq); mna.G(b, b, geq); mna.G(a, b, -geq); mna.G(b, a, -geq);
                        mna.I(a, ieq); mna.I(b, -ieq);
                    } else if (kind == "L") {
                        int a = N(d, 0), b = N(d, 1), br = s.branch_of.at(name);
                        double L = dev_value(d, 0.0);
                        const TranState& h = st[name];
                        double req = trap ? 2.0 * L / dt : L / dt;
                        double veq = trap ? -(req * h.i + h.ldidt) : -req * h.i;
                        mna.G(a, br, 1.0); mna.G(b, br, -1.0);
                        mna.G(br, a, 1.0); mna.G(br, b, -1.0); mna.G(br, br, -req);
                        mna.I(br, veq);
                    } else {
                        stamp_linear(s, mna, d, t);  // R/V/I/E/G (time-varying source at t)
                    }
                }
            };
            // Advance reactive history after a converged step.
            auto update_state = [&](const std::vector<double>& xn, double dt, bool trap) {
                auto val = [&](int i) { return (i < 0 || i >= (int)xn.size()) ? 0.0 : xn[i]; };
                for (const json& d : devices) {
                    std::string kind = d.value("kind", std::string());
                    std::string name = d.value("name", std::string());
                    const json& nodes = d["nodes"];
                    auto Nn = [&](int k) { return (k < (int)nodes.size()) ? s.n(nodes[k].get<std::string>()) : -1; };
                    if (kind == "C") {
                        double c = dev_value(d, 0.0);
                        TranState& h = st[name];
                        double v = val(Nn(0)) - val(Nn(1));
                        double i_new = trap ? 2.0 * c / dt * (v - h.v) - h.i : c / dt * (v - h.v);
                        h.v = v; h.i = i_new;
                    } else if (kind == "L") {
                        double L = dev_value(d, 0.0);
                        TranState& h = st[name];
                        int br = s.branch_of.at(name);
                        double v = val(Nn(0)) - val(Nn(1));
                        double i = val(br);
                        double ldidt_new = trap ? 2.0 * L / dt * (i - h.i) - h.ldidt : L / dt * (i - h.i);
                        h.i = i; h.v = v; h.ldidt = ldidt_new;
                    }
                }
            };

            // Non-uic: seed reactive history from the op point (update_state with dt=tstep, trap).
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
                stamp_tran(mna, dt, trap, tnew);
                std::vector<double> xn;
                if (!solve_dense(mna, xn)) {
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
            did_tran = true;
        } else if (atype == "ac") {
            // ---- linear AC sweep: complex MNA (G + jwC) per frequency ----
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
            log(atype + ": analysis not yet ported to the C++ core (linear DC + transient only)");
        }
    }
    (void)did_op;
    (void)did_tran;
    return result;
}

}  // namespace aicad::sim
