// core/parts/parts_data.h — internal catalog data tables (not part of the public API).
// Dimensions are in millimetres except AS568 source dimensions, which are
// retained in thousandths of an inch until converted for geometry.
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
// insert outside Ø (0 = not offered); hex_h = ISO 4017 nominal head height.
struct MetricRow {
    const char* size;
    double d, pitch, dk, k, s, bh_d, bh_h, fh_d, hex_af, nut_t,
        wash_id, wash_od, wash_t, insert_od, hex_h;
};

// Imperial/SAE fastener row (Unified). d = major Ø; pitch from TPI; shcs_* =
// ASME B18.3 socket-head head Ø/height; hex_af = wrench size; nut_t = hex-nut
// thickness; wash_* = ASME B18.22.1 flat washer. All mm.
struct SaeRow {
    const char* size;
    double d, pitch, shcs_d, shcs_h, hex_af, nut_t, wash_id, wash_od, wash_t;
};

// Nominal key width and minimum full-hex depth used for the modeled socket.
struct SocketRow {
    const char* size;
    double key_width, depth;
};
const SocketRow* find_shcs_socket(const std::string& size);

// Deep-groove ball bearing envelope.
struct BearingRow {
    const char* size;  // bearing number, e.g. "608"
    double bore, od, width;
};

// SAE AS568B nominal dimensions from the adapter's dimensional table.
// Integer thousandths of an inch preserve every published source digit.
struct OringRow {
    std::string size;  // canonical designation, e.g. "AS568-001"
    int id_mils, cs_mils;
};
// Optional detail for standard-size servo housings. Zero means not modeled.
// Insets apply to each side of the centered case; heights are measured up from
// the case bottom or from the feature's supporting surface.
struct HobbyServoDetail {
    double slot_opening_w = 0.0, slot_row_pitch = 0.0;
    double cap_h = 0.0, cap_inset = 0.0;
    double bottom_h = 0.0, bottom_inset = 0.0;
    double boss_top_d = 0.0, boss_lower_h = 0.0;
    double gear_bump_d = 0.0, gear_bump_offset = 0.0, gear_bump_h = 0.0;
    double cable_l = 0.0, cable_w = 0.0, cable_h = 0.0, cable_bottom_z = 0.0;
    double spline_root_d = 0.0, retaining_bore_d = 0.0, retaining_bore_depth = 0.0;
};

// Nominal servo envelopes. z=0 at case bottom; tab bottom and all axial
// dimensions are measured upwards. A dimension not printed on the cited
// manufacturer's drawing is identified as a nominal fit dimension in notes.
struct HobbyServoRow {
    const char* size;
    double body_l, body_w, body_h;
    double tab_span, tab_thickness, tab_bottom_z;
    double hole_d, hole_pitch;
    double shaft_from_end, boss_d, boss_h, shaft_d, shaft_h;
    int spline_teeth;
    const char* source;
    const char* notes;
    HobbyServoDetail detail{};
};


const std::vector<MetricRow>& metric();
const std::vector<SaeRow>& sae();
const std::vector<BearingRow>& bearings();
const std::vector<double>& dowel_diameters();
const std::vector<HobbyServoRow>& hobby_servos();
const std::vector<OringRow>& orings_as568();
const OringRow* find_oring_as568(const std::string& size);
const HobbyServoRow* find_hobby_servo(const std::string& size);

// nullptr when not found.
const MetricRow* find_metric(const std::string& size);
const SaeRow* find_sae(const std::string& size);
const BearingRow* find_bearing(const std::string& size);

}  // namespace data
}  // namespace parts
}  // namespace chudware
