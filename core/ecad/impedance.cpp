// Controlled-impedance calculators implementation — see impedance.h.
// Uses the same closed-form models as the Python oracle, with strict finite/domain
// validation so malformed geometry cannot be clamped into a zero-ohm success.
// OCCT-free; pure math.

#include "ecad/impedance.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <limits>
#include <string>
#include <utility>
#include <vector>

#include "ecad/ops.h"  // OpError

namespace chudware::ecad {

using nlohmann::json;

namespace {

constexpr double kPi = 3.14159265358979323846;

// Speed of light (mm/s); one-way vacuum delay in ps/mm. Match impedance.py exactly.
constexpr double kCmmPerS = 299792458000.0;
const double kPsPerMmVacuum = 1.0e12 / kCmmPerS;  // ~3.3356 ps/mm

[[noreturn]] void invalid_geometry(const std::string& where, const std::string& reason) {
    throw OpError("invalid_op", where + ": " + reason, {});
}

double getf(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}

bool finite_positive(double v) {
    return std::isfinite(v) && v > 0.0;
}

double delay_ps_per_mm(double eeff) {
    if (!finite_positive(eeff))
        invalid_geometry("delay_ps_per_mm", "effective permittivity must be finite and > 0");
    return kPsPerMmVacuum * std::sqrt(eeff);
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
    if (!std::isfinite(m) || m < 0.0 || m >= 1.0)
        invalid_geometry("coplanar_z0", "elliptic-integral parameter is outside [0, 1)");
    double a = 1.0;
    double b = std::sqrt(1.0 - m);
    for (int i = 0; i < 100; ++i) {
        double an = 0.5 * (a + b);
        double bn = std::sqrt(a * b);
        if (std::abs(an - bn) <= 1e-16 * an) {
            a = an;
            break;
        }
        a = an;
        b = bn;
    }
    return kPi / (2.0 * a);
}

// _ellip_ratio(k): K(k)/K(k'), parameters squared (scipy ellipk takes m=k^2).
double ellip_ratio(double k) {
    if (!std::isfinite(k) || k <= 0.0 || k >= 1.0)
        invalid_geometry("coplanar_z0", "conformal ratio must be strictly between 0 and 1");
    double kp = std::sqrt(1.0 - k * k);
    return ellipk_m(k * k) / ellipk_m(kp * kp);
}

double copper_thickness(const json& layer, double dflt = 0.035) {
    if (!layer.contains("thickness") || layer["thickness"].is_null()) return dflt;
    if (!layer["thickness"].is_number())
        invalid_geometry("track_impedance", "copper thickness must be numeric millimetres");
    double v = layer["thickness"].get<double>();
    if (!finite_positive(v))
        invalid_geometry("track_impedance", "copper thickness must be finite and > 0 mm");
    return v;
}

double one_track_length_mm(const json& tr) {
    if (!tr.is_object()) invalid_geometry("track_impedance", "track entry must be an object");
    if (tr.value("kind", std::string("segment")) == "arc") {
        double r = getf(tr.contains("radius") ? tr["radius"] : json());
        double a0 = getf(tr.contains("a0") ? tr["a0"] : json());
        double a1 = getf(tr.contains("a1") ? tr["a1"] : json());
        if (!finite_positive(r) || !std::isfinite(a0) || !std::isfinite(a1))
            invalid_geometry("track_impedance", "arc radius must be > 0 mm and angles finite");
        double length = std::abs(a1 - a0) * kPi / 180.0 * r;
        if (!finite_positive(length))
            invalid_geometry("track_impedance", "arc must span a non-zero angle");
        return length;
    }
    auto point = [&](const char* key) -> std::pair<double, double> {
        if (!tr.contains(key) || !tr[key].is_array() || tr[key].size() < 2 ||
            !tr[key][0].is_number() || !tr[key][1].is_number())
            invalid_geometry("track_impedance", std::string(key) + " must be a numeric [x, y] point");
        double x = tr[key][0].get<double>();
        double y = tr[key][1].get<double>();
        if (!std::isfinite(x) || !std::isfinite(y))
            invalid_geometry("track_impedance", std::string(key) + " coordinates must be finite");
        return {x, y};
    };
    auto p1 = point("p1");
    auto p2 = point("p2");
    double length = std::hypot(p2.first - p1.first, p2.second - p1.second);
    if (!finite_positive(length))
        invalid_geometry("track_impedance", "track segment must have non-zero length");
    return length;
}

double track_length_mm(const json& board, const json& net, const json& layer) {
    double total = 0.0;
    if (!board.contains("tracks") || !board["tracks"].is_array()) return total;
    for (const auto& tr : board["tracks"]) {
        if (!tr.is_object()) invalid_geometry("track_impedance", "track list contains a non-object");
        if (!net.is_null()) {
            json trnet = tr.contains("net") ? tr["net"] : json(nullptr);
            if (trnet != net) continue;
        }
        if (!layer.is_null()) {
            json trlayer = tr.contains("layer") ? tr["layer"] : json(nullptr);
            if (trlayer != layer) continue;
        }
        total += one_track_length_mm(tr);
    }
    return total;
}

// Classify a copper layer and pull controlled-impedance geometry from the stackup.
// Returns {kind, h, t, er, ref_planes}.
json stackup_geometry(const json& board, const std::string& layer) {
    if (!board.contains("stackup") || !board["stackup"].is_object() ||
        !board["stackup"].contains("layers") || !board["stackup"]["layers"].is_array() ||
        board["stackup"]["layers"].empty())
        invalid_geometry("track_impedance",
                         "board stackup.layers is required for controlled-impedance analysis");
    const json& layers = board["stackup"]["layers"];

    std::vector<size_t> copper_idx;
    for (size_t i = 0; i < layers.size(); ++i) {
        if (!layers[i].is_object())
            invalid_geometry("track_impedance", "stackup layer entries must be objects");
        if (layers[i].value("type", std::string()) == "copper") copper_idx.push_back(i);
    }
    if (copper_idx.size() < 2)
        invalid_geometry("track_impedance",
                         "stackup needs at least two copper layers to provide a reference plane");

    long pos = -1;
    for (size_t i = 0; i < layers.size(); ++i) {
        if (layers[i].value("id", std::string()) == layer &&
            layers[i].value("type", std::string()) == "copper") {
            pos = static_cast<long>(i);
            break;
        }
    }
    if (pos < 0)
        invalid_geometry("track_impedance",
                         "track layer '" + layer + "' is not a copper layer in stackup.layers");

    double t = copper_thickness(layers[static_cast<size_t>(pos)]);
    bool is_outer = static_cast<size_t>(pos) == copper_idx.front() ||
                    static_cast<size_t>(pos) == copper_idx.back();

    auto dielectric = [&](long i) -> const json& {
        if (i < 0 || i >= static_cast<long>(layers.size()) ||
            layers[static_cast<size_t>(i)].value("type", std::string()) == "copper")
            invalid_geometry("track_impedance",
                             "signal copper must be separated from each reference copper by dielectric");
        return layers[static_cast<size_t>(i)];
    };
    auto dielectric_values = [&](const json& d) -> std::pair<double, double> {
        if (!d.contains("thickness") || !d["thickness"].is_number())
            invalid_geometry("track_impedance", "dielectric thickness must be numeric millimetres");
        double h = d["thickness"].get<double>();
        double er = d.contains("er") && d["er"].is_number() ? d["er"].get<double>()
                                                            : std::numeric_limits<double>::quiet_NaN();
        if (!finite_positive(h))
            invalid_geometry("track_impedance", "dielectric thickness must be finite and > 0 mm");
        if (!std::isfinite(er) || er < 1.0)
            invalid_geometry("track_impedance", "dielectric er must be finite and >= 1");
        return {h, er};
    };
    auto copper_id = [&](long i) -> std::string {
        if (i < 0 || i >= static_cast<long>(layers.size()) ||
            layers[static_cast<size_t>(i)].value("type", std::string()) != "copper")
            invalid_geometry("track_impedance",
                             "no adjacent copper reference plane beyond the dielectric");
        std::string id = layers[static_cast<size_t>(i)].value("id", std::string());
        if (id.empty()) invalid_geometry("track_impedance", "reference copper layer needs an id");
        return id;
    };

    if (is_outer) {
        long dir = static_cast<size_t>(pos) == copper_idx.front() ? 1 : -1;
        auto [h, er] = dielectric_values(dielectric(pos + dir));
        std::string reference = copper_id(pos + 2 * dir);
        return json{{"kind", "microstrip"}, {"h", h}, {"t", t}, {"er", er},
                    {"ref_planes", 1}, {"reference_layers", json::array({reference})}};
    }

    auto [h_above, er_above] = dielectric_values(dielectric(pos - 1));
    auto [h_below, er_below] = dielectric_values(dielectric(pos + 1));
    std::string ref_above = copper_id(pos - 2);
    std::string ref_below = copper_id(pos + 2);
    double h = h_above + h_below;
    double er = (er_above * h_above + er_below * h_below) / h;
    return json{{"kind", "stripline"}, {"h", h}, {"t", t}, {"er", er},
                {"ref_planes", 2},
                {"reference_layers", json::array({ref_above, ref_below})}};
}

}  // namespace

json microstrip_z0(double w, double h, double t, double er) {
    if (!finite_positive(w) || !finite_positive(h) || !std::isfinite(t) || t < 0.0 ||
        !std::isfinite(er) || er < 1.0)
        invalid_geometry("microstrip_z0",
                         "w,h must be finite and > 0 mm, t finite and >= 0 mm, er finite and >= 1");
    double log_arg = 5.98 * h / (0.8 * w + t);
    if (!std::isfinite(log_arg) || log_arg <= 1.0)
        invalid_geometry("microstrip_z0",
                         "geometry is outside the IPC-2141 positive-impedance domain");
    double z0 = (87.0 / std::sqrt(er + 1.41)) * std::log(log_arg);
    double eeff = microstrip_eeff(w, h, t, er);
    if (!finite_positive(z0) || !finite_positive(eeff))
        invalid_geometry("microstrip_z0", "geometry produced a non-finite impedance");
    return json{{"z0", z0},
                {"eeff", eeff},
                {"delay_ps_per_mm", delay_ps_per_mm(eeff)},
                {"model", "microstrip-ipc2141"}};
}

json stripline_z0(double w, double h, double t, double er) {
    if (!finite_positive(w) || !finite_positive(h) || !std::isfinite(t) || t < 0.0 ||
        !std::isfinite(er) || er < 1.0)
        invalid_geometry("stripline_z0",
                         "w,h must be finite and > 0 mm, t finite and >= 0 mm, er finite and >= 1");
    double log_arg = 4.0 * h / (0.67 * kPi * (0.8 * w + t));
    if (!std::isfinite(log_arg) || log_arg <= 1.0)
        invalid_geometry("stripline_z0",
                         "geometry is outside the IPC-2141 positive-impedance domain");
    double z0 = (60.0 / std::sqrt(er)) * std::log(log_arg);
    if (!finite_positive(z0))
        invalid_geometry("stripline_z0", "geometry produced a non-finite impedance");
    return json{{"z0", z0},
                {"eeff", er},
                {"delay_ps_per_mm", delay_ps_per_mm(er)},
                {"model", "stripline-ipc2141"}};
}

json coplanar_z0(double w, double s, double h, double t, double er) {
    if (!finite_positive(w) || !finite_positive(s) || !finite_positive(h) ||
        !std::isfinite(t) || t < 0.0 || !std::isfinite(er) || er < 1.0)
        invalid_geometry("coplanar_z0",
                         "w,s,h must be finite and > 0 mm, t finite and >= 0 mm, er finite and >= 1");
    double we, se;
    if (t > 0.0) {
        double dw = (1.25 * t / kPi) * (1.0 + std::log(4.0 * kPi * w / t));
        if (!std::isfinite(dw))
            invalid_geometry("coplanar_z0", "finite-thickness correction is not finite");
        we = w + dw;
        se = s - dw;
        if (!finite_positive(we) || se <= 1e-9) {
            we = w;
            se = s;
        }
    } else {
        we = w;
        se = s;
    }
    double a = we / 2.0;
    double b = we / 2.0 + se;
    double r1 = ellip_ratio(a / b);
    double denom = std::tanh(kPi * b / (2.0 * h));
    if (!finite_positive(denom))
        invalid_geometry("coplanar_z0", "invalid substrate-height conformal mapping");
    double k3 = std::tanh(kPi * a / (2.0 * h)) / denom;
    double r3 = ellip_ratio(k3);
    double q = r3 / (r1 + r3);
    double eeff = 1.0 + q * (er - 1.0);
    double z0 = (60.0 * kPi / std::sqrt(eeff)) / (r1 + r3);
    if (!finite_positive(z0) || !finite_positive(eeff))
        invalid_geometry("coplanar_z0", "geometry produced a non-finite impedance");
    return json{{"z0", z0},
                {"eeff", eeff},
                {"delay_ps_per_mm", delay_ps_per_mm(eeff)},
                {"model", "cpwg-conformal"}};
}

json diff_pair_z0(double w, double s, double h, double t, double er, const std::string& kind_in) {
    if (!finite_positive(s))
        invalid_geometry("diff_pair_z0", "edge gap s must be finite and > 0 mm");
    std::string kind = kind_in;
    std::transform(kind.begin(), kind.end(), kind.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    json base;
    double coupling;
    std::string model;
    std::string out_kind;
    if (kind == "stripline" || kind == "strip") {
        base = stripline_z0(w, h, t, er);
        coupling = 1.0 - 0.347 * std::exp(-2.9 * (s / h));
        model = "diffpair-stripline";
        out_kind = kind;
    } else if (kind == "microstrip") {
        base = microstrip_z0(w, h, t, er);
        coupling = 1.0 - 0.48 * std::exp(-0.96 * (s / h));
        out_kind = "microstrip";
        model = "diffpair-microstrip";
    } else {
        invalid_geometry("diff_pair_z0", "kind must be 'microstrip' or 'stripline'");
    }
    if (!finite_positive(coupling))
        invalid_geometry("diff_pair_z0", "coupling factor is not finite and positive");
    double z0_se = base["z0"].get<double>();
    double z0_diff = 2.0 * z0_se * coupling;
    if (!finite_positive(z0_diff))
        invalid_geometry("diff_pair_z0", "geometry produced a non-finite impedance");
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
    bool selected_by_net = false;

    if (track_or_net.is_object()) {
        track = track_or_net;
        net = track.contains("net") ? track["net"] : json(nullptr);
    } else if (track_or_net.is_string()) {
        std::string selector = track_or_net.get<std::string>();
        if (selector.empty()) invalid_geometry("track_impedance", "track/net selector is empty");
        for (const auto& tr : tracks) {
            if (!tr.is_object()) invalid_geometry("track_impedance", "track list contains a non-object");
            if (tr.contains("id") && tr["id"] == track_or_net) {
                track = tr;
                net = tr.contains("net") ? tr["net"] : json(nullptr);
                break;
            }
        }
        if (track.is_null()) {
            net = track_or_net;
            selected_by_net = true;
            std::string selected_layer;
            for (const auto& tr : tracks) {
                if (!tr.is_object())
                    invalid_geometry("track_impedance", "track list contains a non-object");
                json trnet = tr.contains("net") ? tr["net"] : json(nullptr);
                if (trnet != net) continue;
                if (!tr.contains("layer") || !tr["layer"].is_string())
                    invalid_geometry("track_impedance", "selected track has no string copper layer");
                std::string candidate_layer = tr["layer"].get<std::string>();
                if (!selected_layer.empty() && candidate_layer != selected_layer)
                    invalid_geometry("track_impedance",
                                     "net spans multiple layers; pass a track id for one layer");
                selected_layer = candidate_layer;
                if (track.is_null()) track = tr;
            }
        }
    } else {
        invalid_geometry("track_impedance", "selector must be a track object, track id, or net name");
    }
    if (track.is_null())
        invalid_geometry("track_impedance", "no track found for " + track_or_net.dump());
    if (!track.is_object()) invalid_geometry("track_impedance", "selected track is not an object");
    if (!track.contains("width") || !track["width"].is_number())
        invalid_geometry("track_impedance", "track width must be numeric millimetres");
    double width = track["width"].get<double>();
    if (!finite_positive(width))
        invalid_geometry("track_impedance", "track width must be finite and > 0 mm");
    if (!track.contains("layer") || !track["layer"].is_string() ||
        track["layer"].get<std::string>().empty())
        invalid_geometry("track_impedance", "track layer must be a non-empty string");
    std::string layer = track["layer"].get<std::string>();
    json geo = stackup_geometry(board, layer);
    double h = geo["h"].get<double>();
    double t = geo["t"].get<double>();
    double er = geo["er"].get<double>();

    json z = geo["kind"] == "stripline" ? stripline_z0(width, h, t, er)
                                          : microstrip_z0(width, h, t, er);
    double length_mm = net.is_null() ? one_track_length_mm(track)
                                     : track_length_mm(board, net, json(layer));
    if (!finite_positive(length_mm))
        invalid_geometry("track_impedance", "selected route has no finite non-zero length");
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
                {"delay_ps", delay_ps},
                {"reference_layers", geo["reference_layers"]},
                {"reference_plane_assumption", "nearest copper layer(s) are continuous reference planes"},
                {"selected_by_net", selected_by_net}};
}

}  // namespace chudware::ecad
