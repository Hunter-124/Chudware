// core/parts/parts_data.h — internal catalog data tables (not part of the public API).
// Dimensions in millimetres; imperial sizes pre-converted. Mirrors
// agent/prompts/shared/cheatsheets/hardware.md.
#pragma once

#include <string>
#include <vector>

namespace chudware {
namespace parts {
namespace data {

// Metric fastener row. dk/k = ISO 4762 socket-head cap-screw head Ø/height;
// s = hex-key (metadata); bh_* = ISO 7380 button head; fh_d = ISO 10642 flat
// head Ø (90°); hex_af = hex-head/nut width across flats; nut_t = hex-nut
// thickness (ISO 4032); wash_* = ISO 7089 flat washer; insert_od = heat-set
// insert outside Ø (0 = not offered).
struct MetricRow {
    const char* size;
    double d, pitch, dk, k, s, bh_d, bh_h, fh_d, hex_af, nut_t,
        wash_id, wash_od, wash_t, insert_od;
};

// Imperial/SAE fastener row (Unified). d = major Ø; pitch from TPI; shcs_* =
// ASME B18.3 socket-head head Ø/height; hex_af = wrench size; nut_t = hex-nut
// thickness; wash_* = ASME B18.22.1 flat washer. All mm.
struct SaeRow {
    const char* size;
    double d, pitch, shcs_d, shcs_h, hex_af, nut_t, wash_id, wash_od, wash_t;
};

// Deep-groove ball bearing envelope.
struct BearingRow {
    const char* size;  // bearing number, e.g. "608"
    double bore, od, width;
};

const std::vector<MetricRow>& metric();
const std::vector<SaeRow>& sae();
const std::vector<BearingRow>& bearings();
const std::vector<double>& dowel_diameters();

// nullptr when not found.
const MetricRow* find_metric(const std::string& size);
const SaeRow* find_sae(const std::string& size);
const BearingRow* find_bearing(const std::string& size);

}  // namespace data
}  // namespace parts
}  // namespace chudware
