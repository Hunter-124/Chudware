// core/parts/catalog.cpp — catalog data tables + family listing.
#include "parts/parts_data.h"
#include "parts/parts.h"

namespace chudware {
namespace parts {
namespace data {

// Metric fasteners (ISO). See parts_data.h for column meanings.
const std::vector<MetricRow>& metric() {
    //                size     d   pitch  dk     k     s    bh_d  bh_h  fh_d  hex_af nut_t wid   wod   wt   ins_od
    static const std::vector<MetricRow> t = {
        {"M1.6", 1.6, 0.35,  3.0,  1.6,  1.5,  3.0,  1.0,  3.6,  3.2,  1.3,  1.8,  4.0,  0.3,  0.0},
        {"M2",   2.0, 0.40,  3.8,  2.0,  1.5,  3.8,  1.3,  4.4,  4.0,  1.6,  2.2,  5.5,  0.3,  3.6},
        {"M2.5", 2.5, 0.45,  4.5,  2.5,  2.0,  4.7,  1.5,  5.5,  5.0,  2.0,  2.7,  6.0,  0.5,  4.0},
        {"M3",   3.0, 0.50,  5.5,  3.0,  2.5,  5.7,  1.65, 6.3,  5.5,  2.4,  3.2,  7.0,  0.5,  4.6},
        {"M4",   4.0, 0.70,  7.0,  4.0,  3.0,  7.6,  2.2,  8.4,  7.0,  3.2,  4.3,  9.0,  0.8,  5.9},
        {"M5",   5.0, 0.80,  8.5,  5.0,  4.0,  9.5,  2.75, 10.4, 8.0,  4.0,  5.3,  10.0, 1.0,  6.4},
        {"M6",   6.0, 1.00,  10.0, 6.0,  5.0,  10.5, 3.3,  12.6, 10.0, 5.0,  6.4,  12.0, 1.6,  8.1},
        {"M8",   8.0, 1.25,  13.0, 8.0,  6.0,  14.0, 4.4,  17.3, 13.0, 6.5,  8.4,  16.0, 1.6,  0.0},
        {"M10",  10.0,1.50,  16.0, 10.0, 8.0,  17.5, 5.5,  20.0, 16.0, 8.0,  10.5, 20.0, 2.0,  0.0},
        {"M12",  12.0,1.75,  18.0, 12.0, 10.0, 21.0, 6.6,  24.0, 18.0, 10.0, 13.0, 24.0, 2.5,  0.0},
    };
    return t;
}

// Imperial / SAE (Unified). Major Ø + head/wrench/washer dims in mm.
const std::vector<SaeRow>& sae() {
    //                 size       d      pitch   shcs_d shcs_h hex_af nut_t  wid    wod    wt
    static const std::vector<SaeRow> t = {
        {"#6-32",   3.51,  0.794,  5.74,  3.51,  7.94,  2.70,  3.96,  9.53,  1.24},
        {"#8-32",   4.17,  0.794,  6.86,  4.17,  8.73,  3.30,  4.78,  11.13, 1.24},
        {"#10-24",  4.83,  1.058,  7.92,  4.83,  9.53,  3.30,  5.16,  12.70, 1.24},
        {"1/4-20",  6.35,  1.270,  9.53,  6.35,  11.11, 5.56,  7.14,  15.88, 1.65},
        {"5/16-18", 7.94,  1.411,  11.91, 7.94,  12.70, 6.75,  8.74,  17.46, 1.65},
        {"3/8-16",  9.53,  1.5875, 14.27, 9.53,  14.29, 8.33,  10.31, 20.64, 1.65},
        {"1/2-13",  12.70, 1.954,  19.05, 12.70, 19.05, 11.11, 13.49, 26.97, 2.41},
    };
    return t;
}

const std::vector<BearingRow>& bearings() {
    // ISO 15 deep-groove envelopes (bore, OD, width) mm. Keep in lockstep with
    // engine_oracle/data/bolts/bearings.blt.yaml so make_standard_part finds every
    // advertised size (incl. 6205 = 25×52×15).
    static const std::vector<BearingRow> t = {
        {"623",  3.0,  10.0, 4.0},  {"624",  4.0,  13.0, 5.0},  {"625",  5.0,  16.0, 5.0},
        {"626",  6.0,  19.0, 6.0},  {"608",  8.0,  22.0, 7.0},
        {"6000", 10.0, 26.0, 8.0},  {"6001", 12.0, 28.0, 8.0},  {"6002", 15.0, 32.0, 9.0},
        {"6003", 17.0, 35.0, 10.0}, {"6004", 20.0, 42.0, 12.0}, {"6005", 25.0, 47.0, 12.0},
        {"6006", 30.0, 55.0, 13.0}, {"6007", 35.0, 62.0, 14.0}, {"6008", 40.0, 68.0, 15.0},
        {"6200", 10.0, 30.0, 9.0},  {"6201", 12.0, 32.0, 10.0}, {"6202", 15.0, 35.0, 11.0},
        {"6203", 17.0, 40.0, 12.0}, {"6204", 20.0, 47.0, 14.0}, {"6205", 25.0, 52.0, 15.0},
        {"6206", 30.0, 62.0, 16.0}, {"6207", 35.0, 72.0, 17.0}, {"6208", 40.0, 80.0, 18.0},
        {"6300", 10.0, 35.0, 11.0}, {"6301", 12.0, 37.0, 12.0}, {"6302", 15.0, 42.0, 13.0},
        {"6303", 17.0, 47.0, 14.0}, {"6304", 20.0, 52.0, 15.0}, {"6305", 25.0, 62.0, 17.0},
        {"6800", 10.0, 19.0, 5.0},  {"6801", 12.0, 21.0, 5.0},  {"6802", 15.0, 24.0, 5.0},
        {"6900", 10.0, 22.0, 6.0},  {"6901", 12.0, 24.0, 6.0},
    };
    return t;
}

const std::vector<double>& dowel_diameters() {
    static const std::vector<double> t = {1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0};
    return t;
}

const MetricRow* find_metric(const std::string& size) {
    for (const MetricRow& r : metric())
        if (size == r.size) return &r;
    return nullptr;
}
const SaeRow* find_sae(const std::string& size) {
    for (const SaeRow& r : sae())
        if (size == r.size) return &r;
    return nullptr;
}
const BearingRow* find_bearing(const std::string& size) {
    for (const BearingRow& r : bearings())
        if (size == r.size) return &r;
    return nullptr;
}

}  // namespace data

namespace {

// Common length menus (mm).
const std::vector<double> kScrewLengths = {4, 5, 6, 8, 10, 12, 16, 20, 25, 30, 35, 40, 50, 60};
const std::vector<double> kStandoffLengths = {6, 8, 10, 12, 15, 20, 25, 30, 40};
const std::vector<double> kRodLengths = {25, 50, 100, 150, 200, 300};
const std::vector<double> kInsertLengths = {3, 4, 5, 6, 8};

// Metric-size menus by sub-range.
std::vector<std::string> metric_sizes(double lo, double hi) {
    std::vector<std::string> v;
    for (const data::MetricRow& r : data::metric())
        if (r.d >= lo - 1e-9 && r.d <= hi + 1e-9) v.push_back(r.size);
    return v;
}
std::vector<std::string> sae_sizes() {
    std::vector<std::string> v;
    for (const data::SaeRow& r : data::sae()) v.push_back(r.size);
    return v;
}
std::vector<std::string> metric_plus_sae(double lo, double hi) {
    std::vector<std::string> v = metric_sizes(lo, hi);
    for (const std::string& s : sae_sizes()) v.push_back(s);
    return v;
}
std::vector<std::string> bearing_sizes() {
    std::vector<std::string> v;
    for (const data::BearingRow& r : data::bearings()) v.push_back(r.size);
    return v;
}
std::vector<std::string> dowel_sizes() {
    std::vector<std::string> v;
    for (double d : data::dowel_diameters()) {
        // pretty integer when whole (e.g. "3 mm"), else one decimal ("2.5 mm").
        if (d == static_cast<int>(d)) v.push_back(std::to_string(static_cast<int>(d)) + " mm");
        else v.push_back(std::to_string(d).substr(0, 3) + " mm");
    }
    return v;
}

}  // namespace

std::vector<Family> catalog() {
    std::vector<Family> f;
    f.push_back({"shcs", "Socket Head Cap Screw", "Screws", "ISO 4762 / DIN 912 (M), ASME B18.3 (in)",
                 "Cylindrical socket head; the default machine screw.", true,
                 metric_plus_sae(2, 12), kScrewLengths});
    f.push_back({"bhcs", "Button Head Cap Screw", "Screws", "ISO 7380",
                 "Low domed head, hex socket; cosmetic / clearance use.", true,
                 metric_sizes(2, 10), kScrewLengths});
    f.push_back({"fhcs", "Flat Head Cap Screw", "Screws", "ISO 10642 / DIN 7991",
                 "90° countersunk head; sits flush in a csink.", true,
                 metric_sizes(2.5, 8), kScrewLengths});
    f.push_back({"hex_bolt", "Hex Head Bolt", "Screws", "ISO 4017 (M), ASME B18.2.1 (in)",
                 "External hex head; wrench-driven.", true,
                 metric_plus_sae(3, 12), kScrewLengths});
    f.push_back({"hex_nut", "Hex Nut", "Nuts", "ISO 4032 (M), ASME B18.2.2 (in)",
                 "Standard hex nut.", false, metric_plus_sae(2, 12), {}});
    f.push_back({"washer_flat", "Flat Washer", "Washers", "ISO 7089 (M), ASME B18.22.1 (in)",
                 "Plain flat washer (ring).", false, metric_plus_sae(2, 12), {}});
    f.push_back({"washer_lock", "Split Lock Washer", "Washers", "DIN 127B",
                 "Split spring lock washer (modelled as a ring).", false, metric_sizes(3, 12), {}});
    f.push_back({"standoff_round", "Round Standoff", "Spacers", "—",
                 "Round threaded standoff / spacer with a through bore.", true,
                 metric_sizes(2.5, 6), kStandoffLengths});
    f.push_back({"standoff_hex", "Hex Standoff", "Spacers", "—",
                 "Hex threaded standoff with a through bore.", true,
                 metric_sizes(2.5, 6), kStandoffLengths});
    f.push_back({"threaded_rod", "Threaded Rod", "Rod", "—",
                 "Plain cylindrical rod at the thread nominal Ø.", true,
                 metric_plus_sae(3, 12), kRodLengths});
    f.push_back({"dowel_pin", "Dowel Pin", "Pins", "ISO 2338 (h8)",
                 "Cylindrical locating pin.", true, dowel_sizes(), kScrewLengths});
    f.push_back({"heat_set_insert", "Heat-Set Insert", "Inserts", "—",
                 "Brass threaded insert (knurl simplified to a plain OD).", true,
                 metric_sizes(2, 6), kInsertLengths});
    f.push_back({"bearing", "Deep-Groove Ball Bearing", "Bearings", "ISO 15 (deep groove)",
                 "Ball-bearing envelope (outer ring × bore × width).", false, bearing_sizes(), {}});
    return f;
}

json catalog_json() {
    json families = json::array();
    for (const Family& fam : catalog()) {
        json j;
        j["key"] = fam.key;
        j["name"] = fam.name;
        j["category"] = fam.category;
        j["standard"] = fam.standard;
        j["description"] = fam.description;
        j["needs_length"] = fam.needs_length;
        j["sizes"] = fam.sizes;
        j["lengths_mm"] = fam.lengths_mm;
        families.push_back(std::move(j));
    }
    json out;
    out["families"] = std::move(families);
    return out;
}

}  // namespace parts
}  // namespace chudware
