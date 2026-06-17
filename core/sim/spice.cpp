// MNA circuit simulator implementation — see spice.h.
//
// Linear DC operating point. The node-index construction (ground aliases -> -1), the
// branch-row allocation for V/L/E, and the per-device DC stamps mirror engine/sim/
// circuit.py::_Sim + devices.py exactly. Probe evaluation (V(node)/V(a,b)/I(name))
// follows circuit.py::_eval_probe. The dense PartialPivLU solve matches numpy's
// linalg.solve (LAPACK partial-pivot LU) for the small systems these circuits produce.

#include "sim/spice.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <map>
#include <string>
#include <vector>

#include <Eigen/Dense>

namespace aicad::sim {

using nlohmann::json;

namespace {

bool is_ground(const std::string& n) { return n == "0" || n == "gnd" || n == "GND"; }

std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper((unsigned char)c));
    return s;
}

double as_num(const json& v, double dflt = 0.0) {
    if (v.is_number()) return v.get<double>();
    return dflt;
}

// A device's numeric `value` (the Circuit dict stores values already parsed to numbers).
double dev_value(const json& d, double dflt = 0.0) {
    if (d.contains("value") && d["value"].is_number()) return d["value"].get<double>();
    return dflt;
}

// The DC source value: a constant `value`, or params.dc, or a waveform's DC level.
double source_value_dc(const json& d) {
    double base = dev_value(d, 0.0);
    const json& p = d.contains("params") && d["params"].is_object() ? d["params"] : json::object();
    const json* wf = p.contains("waveform") && p["waveform"].is_object() ? &p["waveform"] : nullptr;
    if (!wf) {
        if (p.contains("dc")) return as_num(p["dc"], base);
        return base;
    }
    std::string kind = upper(wf->value("type", std::string("DC")));
    if (kind == "DC") return as_num((*wf).value("v", json(base)), base);
    // PULSE/SIN/PWL at t=0 -> their initial level (used by the DC op-point bootstrap).
    if (kind == "PULSE") return as_num((*wf).value("v1", json(0.0)), 0.0);
    if (kind == "SIN") return as_num((*wf).value("vo", json(0.0)), 0.0);
    return base;
}

// The MNA accumulator (real/DC path only in this increment).
struct Mna {
    const std::map<std::string, int>& node_index;
    int n_nodes;
    int dim;
    std::vector<std::vector<double>> A;  // dense dim x dim
    std::vector<double> z;

    explicit Mna(const std::map<std::string, int>& ni, int nn, int total)
        : node_index(ni), n_nodes(nn), dim(total),
          A(total, std::vector<double>(total, 0.0)), z(total, 0.0) {}

    int n(const std::string& name) const {
        auto it = node_index.find(name);
        return it == node_index.end() ? -1 : it->second;
    }
    void G(int i, int j, double v) {
        if (i < 0 || j < 0 || v == 0.0) return;
        A[i][j] += v;
    }
    void I(int i, double v) {
        if (i < 0) return;
        z[i] += v;
    }
};

}  // namespace

json spice_simulate(const json& circuit, const json& study) {
    json result = {{"log", json::array()}, {"convergence", json::object()}};
    auto log = [&](const std::string& s) { result["log"].push_back(s); };

    const json devices = circuit.contains("devices") && circuit["devices"].is_array()
                             ? circuit["devices"] : json::array();
    const json options = study.contains("options") && study["options"].is_object()
                             ? study["options"] : json::object();
    const double gmin = as_num(options.value("gmin", json(1e-12)), 1e-12);

    // --- node index (ground aliases -> -1; others -> 0..n-1, in first-seen order) ----
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

    std::map<std::string, int> node_index;
    int idx = 0;
    for (const std::string& nm : raw)
        node_index[nm] = is_ground(nm) ? -1 : idx++;
    for (const char* a : {"0", "gnd", "GND"})
        node_index.emplace(a, -1);
    const int n_nodes = idx;

    // --- branch-row allocation (V/L/E each need one), in device order -----------------
    std::map<std::string, int> branch_of;  // device name -> branch row
    int dim = n_nodes;
    for (const json& d : devices) {
        std::string kind = d.value("kind", std::string());
        if (kind == "V" || kind == "L" || kind == "E")
            branch_of[d.value("name", std::string())] = dim++;
    }

    Mna mna(node_index, n_nodes, dim);

    // gmin: a tiny conductance from every non-ground node to ground.
    if (gmin > 0)
        for (int i = 0; i < n_nodes; ++i) mna.G(i, i, gmin);

    // --- DC stamps (linear device set) ------------------------------------------------
    std::string stamp_err;
    for (const json& d : devices) {
        const std::string kind = d.value("kind", std::string());
        const std::string name = d.value("name", std::string());
        const json& nodes = d.contains("nodes") ? d["nodes"] : json::array();
        auto N = [&](int k) {
            return (k < (int)nodes.size()) ? mna.n(nodes[k].get<std::string>()) : -1;
        };
        if (kind == "R") {
            double r = dev_value(d, 0.0);
            if (std::abs(r) < 1e-12) r = 1e-9;
            double g = 1.0 / r;
            int a = N(0), b = N(1);
            mna.G(a, a, g); mna.G(b, b, g); mna.G(a, b, -g); mna.G(b, a, -g);
        } else if (kind == "C") {
            // Open circuit at DC — nothing to stamp.
        } else if (kind == "L") {
            int a = N(0), b = N(1), br = branch_of[name];
            mna.G(a, br, 1.0); mna.G(b, br, -1.0);
            mna.G(br, a, 1.0); mna.G(br, b, -1.0);  // V(a)-V(b)=0 (ideal short)
        } else if (kind == "V") {
            int a = N(0), b = N(1), br = branch_of[name];
            mna.G(a, br, 1.0); mna.G(b, br, -1.0);
            mna.G(br, a, 1.0); mna.G(br, b, -1.0);
            mna.I(br, source_value_dc(d));
        } else if (kind == "I") {
            int a = N(0), b = N(1);
            double i = source_value_dc(d);
            mna.I(a, -i); mna.I(b, i);
        } else if (kind == "E") {  // VCVS: nodes=[out+,out-,ctrl+,ctrl-], value=gain
            int op = N(0), on = N(1), cp = N(2), cn = N(3), br = branch_of[name];
            double gain = dev_value(d, 1.0);
            mna.G(op, br, 1.0); mna.G(on, br, -1.0);
            mna.G(br, op, 1.0); mna.G(br, on, -1.0);
            mna.G(br, cp, -gain); mna.G(br, cn, gain);
        } else if (kind == "G") {  // VCCS: nodes=[out+,out-,ctrl+,ctrl-], value=gm
            int op = N(0), on = N(1), cp = N(2), cn = N(3);
            double gm = dev_value(d, 1.0);
            mna.G(op, cp, gm); mna.G(op, cn, -gm);
            mna.G(on, cp, -gm); mna.G(on, cn, gm);
        } else {
            stamp_err = "device kind '" + kind + "' (" + name + ") not yet ported to the C++ DC core";
            break;
        }
    }

    // --- solve A x = z (dense partial-pivot LU = numpy.linalg.solve) -------------------
    std::vector<double> x(dim, 0.0);
    bool op_ok = false;
    if (!stamp_err.empty()) {
        log("op: " + stamp_err);
    } else if (dim == 0) {
        op_ok = true;
    } else {
        Eigen::MatrixXd Am(dim, dim);
        Eigen::VectorXd zv(dim);
        for (int i = 0; i < dim; ++i) {
            zv(i) = mna.z[i];
            for (int j = 0; j < dim; ++j) Am(i, j) = mna.A[i][j];
        }
        Eigen::VectorXd xv = Am.partialPivLu().solve(zv);
        if (xv.allFinite()) {
            for (int i = 0; i < dim; ++i) x[i] = xv(i);
            op_ok = true;
            log("op: converged (linear)");
        } else {
            log("op: matrix singular (non-finite solution)");
        }
    }
    result["convergence"]["op"] = op_ok;

    // --- probe evaluation -------------------------------------------------------------
    auto val = [&](int i) -> double { return (i < 0 || i >= dim) ? 0.0 : x[i]; };
    auto node_row = [&](const std::string& nm) -> int {
        auto it = node_index.find(nm);
        return it == node_index.end() ? -2 : it->second;  // -2 = unknown node
    };
    auto eval_probe = [&](const std::string& probe) -> json {
        std::string p = probe;
        // trim
        while (!p.empty() && std::isspace((unsigned char)p.front())) p.erase(p.begin());
        while (!p.empty() && std::isspace((unsigned char)p.back())) p.pop_back();
        std::string up = upper(p);
        if (up.rfind("V(", 0) == 0 && !p.empty() && p.back() == ')') {
            std::string inner = p.substr(2, p.size() - 3);
            auto comma = inner.find(',');
            if (comma == std::string::npos) {
                int i = node_row(inner);
                if (i == -2) return json(nullptr);
                return json(val(i));
            }
            std::string a = inner.substr(0, comma), b = inner.substr(comma + 1);
            auto trim = [](std::string s) {
                while (!s.empty() && std::isspace((unsigned char)s.front())) s.erase(s.begin());
                while (!s.empty() && std::isspace((unsigned char)s.back())) s.pop_back();
                return s;
            };
            return json(val(node_row(trim(a))) - val(node_row(trim(b))));
        }
        if (up.rfind("I(", 0) == 0 && !p.empty() && p.back() == ')') {
            std::string name = p.substr(2, p.size() - 3);
            // Branch current (V/L/E): its MNA branch row.
            auto bit = branch_of.find(name);
            if (bit != branch_of.end()) return json(val(bit->second));
            // Resistor current: (V(a)-V(b))/R.
            for (const json& d : devices)
                if (d.value("name", std::string()) == name && d.value("kind", std::string()) == "R") {
                    int a = mna.n(d["nodes"][0].get<std::string>());
                    int b = mna.n(d["nodes"][1].get<std::string>());
                    double r = dev_value(d, 0.0);
                    if (std::abs(r) < 1e-12) r = 1e-9;
                    return json((val(a) - val(b)) / r);
                }
            return json(nullptr);
        }
        // Bare node name -> node voltage.
        int i = node_row(p);
        return i == -2 ? json(nullptr) : json(val(i));
    };

    // --- run the requested analyses ---------------------------------------------------
    const json analyses = study.contains("analyses") && study["analyses"].is_array()
                              ? study["analyses"] : json::array();
    const json probes = study.contains("probes") && study["probes"].is_array()
                            ? study["probes"] : json::array();
    bool saw_op = false;
    for (const json& a : analyses) {
        std::string atype = a.value("type", std::string());
        if (atype == "op") {
            saw_op = true;
            json op = json::object();
            for (const json& pr : probes) op[pr.get<std::string>()] = eval_probe(pr.get<std::string>());
            result["op"] = op;
        } else {
            result["convergence"][atype] = false;
            log(atype + ": analysis not yet ported to the C++ core (linear DC only)");
        }
    }
    // A bare op (no analyses listed) still reports the operating point if probes exist.
    if (!saw_op && analyses.empty() && op_ok) {
        json op = json::object();
        for (const json& pr : probes) op[pr.get<std::string>()] = eval_probe(pr.get<std::string>());
        result["op"] = op;
        result["convergence"]["op"] = op_ok;
    }
    return result;
}

}  // namespace aicad::sim
