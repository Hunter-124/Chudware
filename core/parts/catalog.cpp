// core/parts/catalog.cpp — catalog data tables + family listing.
#include "parts/parts_data.h"
#include "parts/parts.h"

#include <algorithm>

namespace chudware {
namespace parts {
namespace data {

// Metric fasteners (ISO). See parts_data.h for column meanings.
const std::vector<MetricRow>& metric() {
    // ISO 4017 nominal head heights k (not socket-head k), table 1:
    // https://www.westfieldfasteners.co.uk/Standards/ScrewBolt-HexHd4017-M-THRfull.html
    //                size     d   pitch  dk     k     s    bh_d  bh_h  fh_d  hex_af nut_t wid   wod   wt   ins_od hex_h
    static const std::vector<MetricRow> t = {
        {"M1.6", 1.6, 0.35,  3.0,  1.6,  1.5,  3.0,  1.0,  3.6,  3.2,  1.3,  1.8,  4.0,  0.3,  0.0, 1.1},
        {"M2",   2.0, 0.40,  3.8,  2.0,  1.5,  3.8,  1.3,  4.4,  4.0,  1.6,  2.2,  5.5,  0.3,  3.6, 1.4},
        {"M2.5", 2.5, 0.45,  4.5,  2.5,  2.0,  4.7,  1.5,  5.5,  5.0,  2.0,  2.7,  6.0,  0.5,  4.0, 1.7},
        {"M3",   3.0, 0.50,  5.5,  3.0,  2.5,  5.7,  1.65, 6.3,  5.5,  2.4,  3.2,  7.0,  0.5,  4.6, 2.0},
        {"M4",   4.0, 0.70,  7.0,  4.0,  3.0,  7.6,  2.2,  8.4,  7.0,  3.2,  4.3,  9.0,  0.8,  5.9, 2.8},
        {"M5",   5.0, 0.80,  8.5,  5.0,  4.0,  9.5,  2.75, 10.4, 8.0,  4.0,  5.3,  10.0, 1.0,  6.4, 3.5},
        {"M6",   6.0, 1.00,  10.0, 6.0,  5.0,  10.5, 3.3,  12.6, 10.0, 5.0,  6.4,  12.0, 1.6,  8.1, 4.0},
        {"M8",   8.0, 1.25,  13.0, 8.0,  6.0,  14.0, 4.4,  17.3, 13.0, 6.5,  8.4,  16.0, 1.6,  0.0, 5.3},
        {"M10",  10.0,1.50,  16.0, 10.0, 8.0,  17.5, 5.5,  20.0, 16.0, 8.0,  10.5, 20.0, 2.0,  0.0, 6.4},
        {"M12",  12.0,1.75,  18.0, 12.0, 10.0, 21.0, 6.6,  24.0, 18.0, 10.0, 13.0, 24.0, 2.5,  0.0, 7.5},
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

const SocketRow* find_shcs_socket(const std::string& size) {
    // ISO 4762 / DIN 912 nominal key s and minimum socket depth t:
    // https://www.aramfix.com/content/files/i762caill/datasheet%20iso%204762.pdf
    // ASME B18.3 nominal key sizes and minimum t, inches converted to mm:
    // https://fullerfasteners.com/tech/ansi-asme-b18-3-specifications-hex-socket-head-cap-screws/
    // Exact fractional key sizes are used rather than rounded table decimals.
    static const SocketRow rows[] = {
        {"M1.6", 1.5, 0.7}, {"M2", 1.5, 1.0}, {"M2.5", 2.0, 1.1},
        {"M3", 2.5, 1.3}, {"M4", 3.0, 2.0}, {"M5", 4.0, 2.5},
        {"M6", 5.0, 3.0}, {"M8", 6.0, 4.0}, {"M10", 8.0, 5.0},
        {"M12", 10.0, 6.0},
        {"#6-32", 7.0/64.0*25.4, 0.064*25.4},
        {"#8-32", 9.0/64.0*25.4, 0.077*25.4},
        {"#10-24", 5.0/32.0*25.4, 0.090*25.4},
        {"1/4-20", 3.0/16.0*25.4, 0.120*25.4},
        {"5/16-18", 1.0/4.0*25.4, 0.151*25.4},
        {"3/8-16", 5.0/16.0*25.4, 0.182*25.4},
        {"1/2-13", 3.0/8.0*25.4, 0.245*25.4},
    };
    for (const auto& row : rows)
        if (size == row.size) return &row;
    return nullptr;
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

const std::vector<OringRow>& orings_as568() {
    // SAE AS568B nominal ID and section, transcribed from the same source table
    // as services/parts/adapters/orings_adapter.py (cross-checked there against
    // Parker ORD-5700 and other published charts). IDs below are thousandths of
    // an inch, in consecutive dash-number order, not rounded millimetres.
    static const std::vector<OringRow> rows = [] {
        constexpr int ids_001[] = {
            29, 42, 56, 70, 101, 114, 145, 176, 208, 239, 301, 364,
            426, 489, 551, 614, 676, 739, 801, 864, 926, 989, 1051, 1114,
            1176, 1239, 1301, 1364, 1489, 1614, 1739, 1864, 1989, 2114, 2239, 2364,
            2489, 2614, 2739, 2864, 2989, 3239, 3489, 3739, 3989, 4239, 4489, 4739,
            4989, 5239,
        };
        constexpr int ids_102[] = {
            49, 81, 112, 143, 174, 206, 237, 299, 362, 424, 487, 549,
            612, 674, 737, 799, 862, 924, 987, 1049, 1112, 1174, 1237, 1299,
            1362, 1424, 1487, 1549, 1612, 1674, 1737, 1799, 1862, 1925, 1987, 2050,
            2112, 2175, 2237, 2300, 2362, 2425, 2487, 2550, 2612, 2675, 2737, 2800,
            2862, 2987, 3237, 3487, 3737, 3987, 4237, 4487, 4737, 4987, 5237, 5487,
            5737, 5987, 6237, 6487, 6737, 6987, 7237, 7487, 7737, 7987, 8237, 8487,
            8737, 8987, 9237, 9487, 9737,
        };
        constexpr int ids_201[] = {
            171, 234, 296, 359, 421, 484, 546, 609, 671, 734, 796, 859,
            921, 984, 1046, 1109, 1171, 1234, 1296, 1359, 1421, 1484, 1609, 1734,
            1859, 1984, 2109, 2234, 2359, 2484, 2609, 2734, 2859, 2984, 3109, 3234,
            3359, 3484, 3609, 3734, 3859, 3984, 4109, 4234, 4359, 4484, 4609, 4734,
            4859, 4984, 5109, 5234, 5359, 5484, 5609, 5734, 5859, 5984, 6234, 6484,
            6734, 6984, 7234, 7484, 7734, 7984, 8234, 8484, 8734, 8984, 9234, 9484,
            9734, 9984, 10484, 10984, 11484, 11984, 12984, 13984, 14984, 15955, 16955, 17955,
        };
        constexpr int ids_309[] = {
            412, 475, 537, 600, 662, 725, 787, 850, 912, 975, 1034, 1100,
            1162, 1225, 1287, 1350, 1475, 1600, 1725, 1850, 1975, 2100, 2225, 2350,
            2475, 2600, 2725, 2850, 2975, 3100, 3225, 3350, 3475, 3600, 3725, 3850,
            3975, 4100, 4225, 4350, 4475, 4600, 4725, 4850, 4975, 5100, 5225, 5350,
            5475, 5600, 5725, 5850, 5975, 6225, 6475, 6725, 6975, 7225, 7475, 7725,
            7975, 8225, 8475, 8725, 8975, 9225, 9475, 9725, 9975, 10475, 10975, 11475,
            11975, 12975, 13975, 14975, 15955, 16955, 17955, 18955, 19955, 20955,
        };
        constexpr int ids_425[] = {
            4475, 4600, 4725, 4850, 4975, 5100, 5225, 5350, 5475, 5600, 5725, 5850,
            5975, 6225, 6475, 6725, 6975, 7225, 7475, 7725, 7975, 8475, 8975, 9475,
            9975, 10475, 10975, 11475, 11975, 12475, 12975, 13475, 13975, 14475, 14975, 15475,
            15955, 16455, 16955, 17455, 17955, 18455, 18955,
        };
        std::vector<OringRow> result;
        result.reserve(336);
        auto append_series = [&](int first, const auto& ids, int section_mils) {
            int dash = first;
            for (int id_mils : ids) {
                const std::string digits = std::to_string(dash);
                const int cs_mils = first == 1 && dash <= 3 ? 30 + 10 * dash : section_mils;
                result.push_back({"AS568-" + std::string(3 - digits.size(), '0') + digits,
                                  id_mils, cs_mils});
                ++dash;
            }
        };
        append_series(1, ids_001, 70);
        append_series(102, ids_102, 103);
        append_series(201, ids_201, 139);
        append_series(309, ids_309, 210);
        append_series(425, ids_425, 275);
        return result;
    }();
    return rows;
}

const OringRow* find_oring_as568(const std::string& size) {
    const auto& rows = orings_as568();
    const auto it = std::lower_bound(rows.begin(), rows.end(), size,
        [](const OringRow& row, const std::string& key) { return row.size < key; });
    return it != rows.end() && it->size == size ? &*it : nullptr;
}

const std::vector<HobbyServoRow>& hobby_servos() {
    // TowerPro product pages include the A–F mechanical drawing: B=case L,
    // D=case W, C=case H below output, E=outer tab span, F=tab underside
    // above base. A is overall height including the output spline.
    // Holes/boss details absent from those drawings are nominal fit geometry
    // (not claimed as manufacturer-toleranced dimensions).
    static const std::vector<HobbyServoRow> t = {
        {"SG90", 22.7, 12.2, 27.0, 32.3, 2.5, 17.0, 2.2, 28.1,
         5.6, 6.0, 2.0, 4.5, 1.3, 21,
         "https://towerpro.com.tw/product/sg90-analog/",
         "TowerPro drawing B/D/C/E/F/A=22.7/12.2/27/32.3/17/30.3 mm; hole pitch, diameter, boss and spline are nominal fit dimensions, verify on actual servo."},
        {"MG90S", 22.8, 12.4, 28.4, 32.1, 2.5, 18.5, 2.2, 28.0,
         5.6, 6.0, 2.5, 4.8, 1.6, 20,
         "https://towerpro.com.tw/product/mg90s-3/",
         "TowerPro drawing B/D/C/E/F/A=22.8/12.4/28.4/32.1/18.5/32.5 mm (product text rounds width to 12.2); hole pitch, diameter, boss and spline are nominal fit dimensions, verify on actual servo."},
        // MG996R / MG995 detail, mm (the latter uses the same nominal housing):
        // TowerPro A/B/C/D/E/F = 42.7/40.9/37/20/54/26.8:
        // https://towerpro.com.tw/product/mg996r/
        // https://torqpro.com/wp-content/uploads/2014/07/%E5%B0%8F%E9%A6%AC%E9%81%94%E5%B0%BA%E5%AF%B8%E6%A8%99%E7%A4%BA%E5%9C%96B.jpg
        // TowerPro text instead says 40.7 x 19.7 x 42.9; retain the existing
        // 42.9 overall and z=40 output datum for compatible part placement.
        // MG995 vendor sheet quotes 40.7 x 19.7 x 42.9; its remaining housing
        // details use the MG996R values above/below as proxies:
        // https://cdn.shopify.com/s/files/1/0058/3145/8904/files/MG995.PDF
        // Components101 depicts slots, steps and a low exit at the output end,
        // but gives different case dimensions (40.3 x 20 x 36.6, E=53.6, F=26.6):
        // https://components101.com/sites/default/files/inline-images/MG996R-Servo-Motor-2-D-Model.png
        // Futaba's S3003 establishes only the comparable standard-size envelope:
        // https://futabausa.com/wp-content/uploads/2019/07/S3003.pdf
        // The owner confirmed the existing 49.5 longitudinal slot-center pitch.
        // The existing 10.0 shaft-from-end and 2.9 shaft height are nominal
        // proxies, not drawing dimensions. Ø5.9 is a nominal approximation to
        // the community-measured ~Ø6.0 25T spline (not a tooth-standard drawing):
        // https://www.thingiverse.com/thing:4750282
        // Four slots and approximate transverse 10.0 pitch, Ø4.8 grommet hole,
        // 3.2 opening and 2.5 tab thickness are visually estimated from the
        // Components101 drawing above; they are not dimensioned there.
        // Other *estimated*, not manufacturer dimensions: cap H4.0/inset 0.25
        // per side; bottom H2.0/inset 0.4; gear boss Ø17/Ø14 lower H1.5 and total
        // H3.0; secondary bump Ø7, 11.5 toward -X from output, H0.8;
        // cable block 2 x 4 x 3 at z=2 on +X;
        // spline root Ø5.5; M3 nominal tap bore Ø2.5 x 4.5 deep. The photos
        // show the shapes but do not dimension them:
        // https://towerpro.com.tw/wp-content/uploads/2014/08/MG996R-3b.jpg
        // https://towerpro.com.tw/wp-content/uploads/2014/08/MG996R-b2.jpg
        // A vendor calls the arm screw M4, so check the actual thread:
        // https://www.handsontec.com/dataspecs/motor_fan/MG996R.pdf
        {"MG996R", 40.9, 20.0, 37.0, 54.0, 2.5, 26.8, 4.8, 49.5,
         10.0, 17.0, 3.0, 5.9, 2.9, 25,
         "https://towerpro.com.tw/product/mg996r/",
         "TowerPro drawing B/D/C/E/F=40.9/20/37/54/26.8 mm; A=42.7, but product text and this compatibility model use 42.9. Four slots, cap, gear bosses, cable exit and spline details are approximate; verify physical units, including counterfeits. Rubber grommets/eyelets are optional and not modeled.",
         {3.2, 10.0, 4.0, 0.25, 2.0, 0.4, 14.0, 1.5, 7.0, 11.5, 0.8,
          2.0, 4.0, 3.0, 2.0, 5.5, 2.5, 4.5}},
        {"MG995", 40.7, 19.7, 37.0, 54.0, 2.5, 26.8, 4.8, 49.5,
         10.0, 17.0, 3.0, 5.9, 2.9, 25,
         "https://cdn.shopify.com/s/files/1/0058/3145/8904/files/MG995.PDF",
         "MG995 sheet: approx. 40.7x19.7x42.9 overall. Case height, mounting and other housing details use the MG996R as nominal proxies; verify the actual servo, including counterfeits. Rubber grommets/eyelets are optional and not modeled.",
         {3.2, 10.0, 4.0, 0.25, 2.0, 0.4, 14.0, 1.5, 7.0, 11.5, 0.8,
          2.0, 4.0, 3.0, 2.0, 5.5, 2.5, 4.5}},
        {"DS3218", 40.0, 20.0, 36.5, 54.0, 3.0, 26.0, 3.2, 49.5,
         10.0, 12.0, 2.0, 5.9, 2.0, 25,
         "https://m.media-amazon.com/images/I/81p5zAoqJ0L.pdf",
         "ANNIMOS DS3218MG datasheet: 40x20x40.5 overall. Case-versus-output and tab/hole/boss details are nominal standard-size fit proxies; check the maker drawing before tooling."},
        {"micro_9g", 22.7, 12.2, 27.0, 32.3, 2.5, 17.0, 2.2, 28.1,
         5.6, 6.0, 2.0, 4.5, 1.3, 0,
         "https://towerpro.com.tw/product/sg90-analog/",
         "Generic micro 9g envelope based on TowerPro SG90; tooth count unspecified because generic servos do not share a spline standard."},
    };
    return t;
}

const HobbyServoRow* find_hobby_servo(const std::string& size) {
    for (const HobbyServoRow& r : hobby_servos())
        if (size == r.size) return &r;
    return nullptr;
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
std::vector<std::string> oring_sizes() {
    std::vector<std::string> v;
    const auto& rows = data::orings_as568();
    v.reserve(rows.size());
    for (const data::OringRow& r : rows) v.push_back(r.size);
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

std::vector<std::string> hobby_servo_sizes() {
    std::vector<std::string> v;
    for (const data::HobbyServoRow& r : data::hobby_servos()) v.push_back(r.size);
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
    f.push_back({"hex_bolt", "Hex Head Bolt", "Screws", "ISO 4017 nominal envelope (M); approximate envelope (in)",
                 "External hex head; simplified unthreaded geometry, no property-class certification.", true,
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
    f.push_back({"oring_as568", "AS568 O-Ring", "Seals", "SAE AS568B",
                 "Nominal circular-section O-ring; ID and section from the AS568B table, tolerances omitted.",
                 false, oring_sizes(), {}});
    f.push_back({"hobby_servo", "Hobby Servo", "Motors & Actuators", "Manufacturer nominal envelopes",
                 "Micro and standard-size hobby servo; editable case, mounting flange/holes and output axis.",
                 false, hobby_servo_sizes(), {}});
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
        if (fam.key == "hobby_servo") {
            j["size_sources"] = json::object();
            for (const data::HobbyServoRow& row : data::hobby_servos())
                j["size_sources"][row.size] = {{"source", row.source}, {"notes", row.notes}};
        }
        if (fam.key == "oring_as568") {
            j["size_sources"] = json::object();
            for (const data::OringRow& row : data::orings_as568())
                j["size_sources"][row.size] = {
                    {"source", "SAE AS568B nominal table; cross-checked against Parker ORD-5700 and other published charts"},
                    {"id_in", row.id_mils / 1000.0}, {"cs_in", row.cs_mils / 1000.0},
                    {"notes", "Nominal dimensions; tolerances omitted"}};
        }
        families.push_back(std::move(j));
    }
    json out;
    out["families"] = std::move(families);
    return out;
}

}  // namespace parts
}  // namespace chudware
