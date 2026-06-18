// Controlled-impedance calculators implementation — see impedance.h.
// Mirrors engine/ecad/impedance.py formula-for-formula (math lib call form
// preserved) so the numeric result matches the Python/scipy oracle to full
// double precision. OCCT-free; pure math.

#include "ecad/impedance.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <string>
#include <vector>

#include "ecad/ops.h"  // OpError

namespace aicad::ecad {

using nlohmann::json;

namespace {

constexpr double kPi = 3.14159265358979323846;

// Speed of light (mm/s); one-way vacuum delay in ps/mm. Match impedance.py exactly.
constexpr double kCmmPerS = 299792458000.0;
const double kPsPerMmVacuum = 1.0e12 / kCmmPerS;  // ~3.3356 ps/mm

double getf(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}

double delay_ps_per_mm(double eeff) {
    return kPsPerMmVacuum * std::sqrt(std::max(eeff, 1.0));
}

double microstrip_eeff(double w, double h, double t, double er) {
    double u = w / h;
    double a = 1.0 + 12.0 / u;
    double eeff;
    if (u >= 1.0) {
        eeff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * std::pow(a, -0.5);
    } else {
        eeff = (er + 1.0) / 2.0 +
               (er - 1.0) / 2.0 * (std::pow(a, -0.5) + 0.04 * std::pow(1.0 - u, 2));
    }
    if (t > 0.0) eeff -= (er - 1.0) * (t / h) / (4.6 * std::sqrt(u));
    return std::max(eeff, 1.0);
}

// Complete elliptic integral K(m) with the scipy/Cephes parameter convention
// (m = k^2). K(m) = pi / (2 * AGM(1, sqrt(1-m))). The AGM converges quadratically
// to full double precision, matching scipy.special.ellipk within < 1e-12 rel.
double ellipk_m(double m) {
    if (m >= 1.0) return std::numeric_limits<double>::infinity();
    double a = 1.0;
    double b = std::sqrt(1.0 - m);
    for (int i = 0; i < 100; ++i) {
        double an = 0.5 * (a + b);
        double bn = std::sqrt(a * b);
        if (std::abs(an - bn) <= 1e-16 * an) {
            a = an;
            b = bn;
            break;
        }
        a = an;
        b = bn;
    }
    return kPi / (2.0 * a);
}

// _ellip_ratio(k): K(k)/K(k'), parameters squared (scipy ellipk takes m=k^2).
double ellip_ratio(double k) {
    k = std::min(std::max(k, 1e-9), 1.0 - 1e-12);
    double kp = std::sqrt(1.0 - k * k);
    return ellipk_m(k * k) / ellipk_m(kp * kp);
}

double copper_thickness(const json& layer, double dflt = 0.035) {
    if (layer.contains("thickness") && layer["thickness"].is_number()) {
        double v = layer["thickness"].get<double>();
        return v != 0.0 ? v : dflt;  // `or default` semantics: 0 -> default
    }
    return dflt;
}

double track_length_mm(const json& board, const json& net, const json& layer) {
    double total = 0.0;
    if (!board.contains("tracks") || !board["tracks"].is_array()) return total;
    for (const auto& tr : board["tracks"]) {
        if (!net.is_null()) {
            json trnet = tr.contains("net") ? tr["net"] : json(nullptr);
            if (trnet != net) continue;
        }
        if (!layer.is_null()) {
            json trlayer = tr.contains("layer") ? tr["layer"] : json(nullptr);
            if (trlayer != layer) continue;
        }
        if (tr.value("kind", std::string("segment")) == "arc") {
            double r = getf(tr.contains("radius") ? tr["radius"] : json(0.0));
            double a0 = getf(tr.contains("a0") ? tr["a0"] : json(0.0)) * kPi / 180.0;
            double a1 = getf(tr.contains("a1") ? tr["a1"] : json(0.0)) * kPi / 180.0;
            total += std::abs(a1 - a0) * r;
        } else {
            double p1x = 0, p1y = 0, p2x = 0, p2y = 0;
            if (tr.contains("p1") && tr["p1"].is_array()) {
                p1x = getf(tr["p1"][0]);
                p1y = getf(tr["p1"][1]);
            }
            if (tr.contains("p2") && tr["p2"].is_array()) {
                p2x = getf(tr["p2"][0]);
                p2y = getf(tr["p2"][1]);
            }
            total += std::hypot(p2x - p1x, p2y - p1y);
        }
    }
    return total;
}

// Classify a copper layer and pull controlled-impedance geometry from the stackup.
// Returns {kind, h, t, er, ref_planes}.
json stackup_geometry(const json& board, const std::string& layer) {
    json layers = json::array();
    if (board.contains("stackup") && board["stackup"].is_object() &&
        board["stackup"].contains("layers") && board["stackup"]["layers"].is_array())
        layers = board["stackup"]["layers"];

    std::vector<size_t> copper_idx;
    for (size_t i = 0; i < layers.size(); ++i)
        if (layers[i].value("type", std::string()) == "copper") copper_idx.push_back(i);

    long pos = -1;
    for (size_t i = 0; i < layers.size(); ++i) {
        if (layers[i].value("id", std::string()) == layer &&
            layers[i].value("type", std::string()) == "copper") {
            pos = (long)i;
            break;
        }
    }
    if (pos < 0)
        return json{{"kind", "microstrip"}, {"h", 0.2}, {"t", 0.035}, {"er", 4.3}, {"ref_planes", 1}};

    double t = copper_thickness(layers[pos]);
    bool is_outer = (size_t)pos == copper_idx.front() || (size_t)pos == copper_idx.back();

    auto diel = [&](long i) -> const json* {
        if (i >= 0 && i < (long)layers.size() && layers[i].value("type", std::string()) != "copper")
            return &layers[i];
        return nullptr;
    };

    if (is_outer) {
        const json* d = ((size_t)pos == copper_idx.front()) ? diel(pos + 1) : diel(pos - 1);
        double h = d ? getf(d->contains("thickness") ? (*d)["thickness"] : json(0.2), 0.2) : 0.2;
        double er = 4.3;
        if (d && d->contains("er") && !(*d)["er"].is_null()) er = getf((*d)["er"], 4.3);
        return json{{"kind", "microstrip"}, {"h", h}, {"t", t}, {"er", er}, {"ref_planes", 1}};
    }

    const json* above = diel(pos - 1);
    const json* below = diel(pos + 1);
    double h_above = above ? getf(above->contains("thickness") ? (*above)["thickness"] : json(0.2), 0.2) : 0.2;
    double h_below = below ? getf(below->contains("thickness") ? (*below)["thickness"] : json(0.2), 0.2) : 0.2;
    double er_above = 4.3, er_below = 4.3;
    if (above && above->contains("er") && !(*above)["er"].is_null()) er_above = getf((*above)["er"], 4.3);
    if (below && below->contains("er") && !(*below)["er"].is_null()) er_below = getf((*below)["er"], 4.3);
    double h = h_above + h_below;
    double er = h > 0 ? (er_above * h_above + er_below * h_below) / h : er_above;
    return json{{"kind", "stripline"}, {"h", h}, {"t", t}, {"er", er}, {"ref_planes", 2}};
}

}  // namespace

json microstrip_z0(double w, double h, double t, double er) {
    if (w <= 0 || h <= 0 || er < 1.0)
        throw OpError("invalid_op", "microstrip_z0: w,h must be > 0 and er >= 1", {});
    double z0 = (87.0 / std::sqrt(er + 1.41)) * std::log(5.98 * h / (0.8 * w + t));
    z0 = std::max(z0, 0.0);
    double eeff = microstrip_eeff(w, h, t, er);
    return json{{"z0", z0},
                {"eeff", eeff},
                {"delay_ps_per_mm", delay_ps_per_mm(eeff)},
                {"model", "microstrip-ipc2141"}};
}

json stripline_z0(double w, double h, double t, double er) {
    if (w <= 0 || h <= 0 || er < 1.0)
        throw OpError("invalid_op", "stripline_z0: w,h must be > 0 and er >= 1", {});
    double z0 = (60.0 / std::sqrt(er)) * std::log(4.0 * h / (0.67 * kPi * (0.8 * w + t)));
    z0 = std::max(z0, 0.0);
    double eeff = er;
    return json{{"z0", z0},
                {"eeff", eeff},
                {"delay_ps_per_mm", delay_ps_per_mm(eeff)},
                {"model", "stripline-ipc2141"}};
}

json coplanar_z0(double w, double s, double h, double t, double er) {
    if (w <= 0 || s <= 0 || h <= 0 || er < 1.0)
        throw OpError("invalid_op", "coplanar_z0: w,s,h must be > 0 and er >= 1", {});
    double we, se;
    if (t > 0.0) {
        double dw = (w > 0) ? (1.25 * t / kPi) * (1.0 + std::log(4.0 * kPi * w / t)) : 0.0;
        we = w + dw;
        se = s - dw;
        if (se <= 1e-9) {
            we = w;
            se = s;
        }
    } else {
        we = w;
        se = s;
    }
    double a = we / 2.0;
    double b = we / 2.0 + se;
    double k1 = a / b;
    double r1 = ellip_ratio(k1);
    double k3 = std::tanh(kPi * a / (2.0 * h)) / std::tanh(kPi * b / (2.0 * h));
    double r3 = ellip_ratio(k3);
    double q = r3 / (r1 + r3);
    double eeff = 1.0 + q * (er - 1.0);
    double z0 = (60.0 * kPi / std::sqrt(eeff)) / (r1 + r3);
    return json{{"z0", z0},
                {"eeff", eeff},
                {"delay_ps_per_mm", delay_ps_per_mm(eeff)},
                {"model", "cpwg-conformal"}};
}

json diff_pair_z0(double w, double s, double h, double t, double er, const std::string& kind_in) {
    std::string kind = kind_in;
    std::transform(kind.begin(), kind.end(), kind.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    json base;
    double coupling;
    std::string model;
    std::string out_kind;
    if (kind == "stripline" || kind == "strip") {
        base = stripline_z0(w, h, t, er);
        coupling = 1.0 - 0.347 * std::exp(-2.9 * (s / h));
        model = "diffpair-stripline";
        out_kind = kind;
    } else {
        base = microstrip_z0(w, h, t, er);
        coupling = 1.0 - 0.48 * std::exp(-0.96 * (s / h));
        out_kind = "microstrip";
        model = "diffpair-microstrip";
    }
    double z0_se = base["z0"].get<double>();
    double z0_diff = 2.0 * z0_se * coupling;
    return json{{"z0_se", z0_se},
                {"z0_diff", z0_diff},
                {"z0_odd", z0_diff / 2.0},
                {"eeff", base["eeff"]},
                {"delay_ps_per_mm", base["delay_ps_per_mm"]},
                {"kind", out_kind},
                {"model", model}};
}

json track_impedance(const json& board, const json& track_or_net) {
    json tracks = (board.contains("tracks") && board["tracks"].is_array()) ? board["tracks"]
                                                                           : json::array();
    json track(nullptr);
    json net(nullptr);

    if (track_or_net.is_object()) {
        track = track_or_net;
        net = track.contains("net") ? track["net"] : json(nullptr);
    } else if (track_or_net.is_string()) {
        for (const auto& tr : tracks) {
            if (tr.contains("id") && tr["id"] == track_or_net) {
                track = tr;
                net = tr.contains("net") ? tr["net"] : json(nullptr);
                break;
            }
        }
        if (track.is_null()) {
            net = track_or_net;
            for (const auto& tr : tracks) {
                json trnet = tr.contains("net") ? tr["net"] : json(nullptr);
                if (trnet == net) {
                    track = tr;
                    break;
                }
            }
        }
    }
    if (track.is_null())
        throw OpError("invalid_op", "track_impedance: no track found for " + track_or_net.dump(), {});

    double width = getf(track.contains("width") ? track["width"] : json(0.25), 0.25);
    std::string layer = track.value("layer", std::string("F.Cu"));
    json geo = stackup_geometry(board, layer);
    double h = geo["h"].get<double>();
    double t = geo["t"].get<double>();
    double er = geo["er"].get<double>();

    json z;
    if (geo["kind"] == "stripline")
        z = stripline_z0(width, h, t, er);
    else
        z = microstrip_z0(width, h, t, er);

    double length_mm = track_length_mm(board, net, json(layer));
    double delay_ps = z["delay_ps_per_mm"].get<double>() * length_mm;
    return json{{"z0", z["z0"]},
                {"eeff", z["eeff"]},
                {"delay_ps_per_mm", z["delay_ps_per_mm"]},
                {"kind", geo["kind"]},
                {"model", z["model"]},
                {"width", width},
                {"h", h},
                {"er", er},
                {"t", t},
                {"layer", layer},
                {"net", net},
                {"length_mm", length_mm},
                {"delay_ps", delay_ps}};
}

}  // namespace aicad::ecad
