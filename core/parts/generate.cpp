// core/parts/generate.cpp — emit document ops for a concrete standard part.
//
// Everything is built from the modeler's own op vocabulary so the result is an
// editable feature tree: primitives (cylinder/cone) combined by the running-body
// model, sketches with extrusions for hex prisms, and a revolved O-ring section.
// No OCCT here — we only emit JSON; chudware::doc compiles the geometry.
#include "parts/parts.h"
#include "parts/parts_data.h"
#include "parts/fitting_geometry.h"
#include "parts/section_geometry.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <stdexcept>
#include <cctype>
#include <vector>


namespace chudware {
namespace parts {

namespace {

json dim(double v) {
    json j;
    j["value"] = v;
    j["expr"] = nullptr;
    return j;
}

// A primitive (cylinder/cone), base at z0 on +Z, combined via the running body.
// mode: "boss" (fuse, or start a new body when none) | "cut" (bore current body).
json op_cylinder(const std::string& name, double r, double h, double z0,
                 const std::string& mode) {
    json p;
    p["radius"] = dim(r);
    p["height"] = dim(h);
    p["at"] = json::array({0.0, 0.0, z0});
    p["centered"] = false;
    p["mode"] = mode;
    p["merge"] = true;
    json feat;
    feat["type"] = "cylinder";
    feat["name"] = name;
    feat["params"] = p;
    json op;
    op["op"] = "add_feature";
    op["feature"] = feat;
    return op;
}
// Servo case and cross-case mounting flange: false-centered boxes sit on z0,
// while x/y remain centered on the footprint.
json op_box(const std::string& name, double l, double w, double h, double z0,
            const std::string& mode) {
    json p;
    p["length"] = dim(l);
    p["width"] = dim(w);
    p["height"] = dim(h);
    p["at"] = json::array({-l / 2.0, -w / 2.0, z0});
    p["centered"] = false;
    p["mode"] = mode;
    p["merge"] = true;
    return {{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", name},
                                                 {"params", std::move(p)}}}};
}

json op_servo_cylinder(const std::string& name, double r, double h,
                       double x, double z0, const std::string& mode) {
    json op = op_cylinder(name, r, h, z0, mode);
    op["feature"]["params"]["at"][0] = x;
    return op;
}

// One editable sketch and extrusion rather than 25 separate boolean teeth. The
// 3-point-per-tooth polygon has a flat crest and a root between adjacent teeth;
// it is a fit approximation, not an involute spline specification.
void emit_servo_spline(json& ops, double x, double z, double h, double major_d,
                       double root_d, int teeth) {
    const int count = 3 * teeth;
    const double pi = 3.14159265358979323846;
    std::vector<std::array<double, 2>> vertices;
    vertices.reserve(count);
    for (int i = 0; i < count; ++i) {
        const double angle = 2.0 * pi * i / count;
        const double radius = (i % 3 == 0 ? root_d : major_d) / 2.0;
        vertices.push_back({x + radius * std::cos(angle), radius * std::sin(angle)});
    }
    json entities = json::array();
    json constraints = json::array();
    for (int i = 0; i < count; ++i) {
        const auto& a = vertices[i];
        const auto& b = vertices[(i + 1) % count];
        entities.push_back({{"id", "e" + std::to_string(i + 1)}, {"type", "line"},
                            {"p1", {a[0], a[1]}}, {"p2", {b[0], b[1]}}});
        constraints.push_back({{"id", "c" + std::to_string(i + 1)},
                               {"type", "coincident"},
                               {"refs", {"e" + std::to_string(i + 1) + ".p2",
                                         "e" + std::to_string((i + 1) % count + 1) + ".p1"}}});
    }
    const std::string sketch = "$" + std::to_string(ops.size());
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "sketch"}, {"name", "output spline profile"},
                                {"params", {{"entities", std::move(entities)},
                                            {"constraints", std::move(constraints)}, {"offset", dim(z)}}},
                                {"refs", {{"plane", "plane.top"}}}}}});
    const json region{{"kind", "region"}, {"sketch", sketch},
                      {"centroid", {x, 0.0}}, {"hint_index", 0}};
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "extrude"}, {"name", "output spline"},
                                {"params", {{"mode", "boss"}, {"end", "blind"},
                                            {"depth", dim(h)}}},
                                {"refs", {{"profile", json::array({region})}}}}}});
}


// Truncated cone, base radius r1 at z0, top radius r2 at z0+h (90° csink head etc.).
json op_cone(const std::string& name, double r1, double r2, double h, double z0,
             const std::string& mode) {
    json p;
    p["r1"] = dim(r1);
    p["r2"] = dim(r2);
    p["height"] = dim(h);
    p["at"] = json::array({0.0, 0.0, z0});
    p["centered"] = false;
    p["mode"] = mode;
    p["merge"] = true;
    json feat;
    feat["type"] = "cone";
    feat["name"] = name;
    feat["params"] = p;
    json op;
    op["op"] = "add_feature";
    op["feature"] = feat;
    return op;
}

// Append a hexagonal prism (across-flats `af`, height `h`) as the base body:
// a sketch on plane.top with 6 line entities + an extrude. Built at the origin so
// the region centroid is [0,0]. Leaves the prism as the current running body.
void emit_hex_prism(json& ops, const std::string& name, double af, double h) {
    const int sidx = static_cast<int>(ops.size());  // op index of the sketch
    const std::string sref = "$" + std::to_string(sidx);

    json skp;
    skp["entities"] = json::array();
    skp["constraints"] = json::array();
    json skfeat;
    skfeat["type"] = "sketch";
    skfeat["name"] = name + " profile";
    skfeat["params"] = skp;
    json skrefs;
    skrefs["plane"] = "plane.top";
    skfeat["refs"] = skrefs;
    json skop;
    skop["op"] = "add_feature";
    skop["feature"] = skfeat;
    ops.push_back(std::move(skop));

    // Regular hexagon, across-flats = af -> circumradius R = af / sqrt(3),
    // vertices at 30°,90°,...,330° so flats align with the axes.
    const double kPi = 3.14159265358979323846;
    const double R = af / std::sqrt(3.0);
    double vx[6], vy[6];
    for (int i = 0; i < 6; ++i) {
        const double a = (30.0 + 60.0 * i) * kPi / 180.0;
        vx[i] = R * std::cos(a);
        vy[i] = R * std::sin(a);
    }
    for (int i = 0; i < 6; ++i) {
        const int j = (i + 1) % 6;
        json ent;
        ent["type"] = "line";
        ent["p1"] = json::array({vx[i], vy[i]});
        ent["p2"] = json::array({vx[j], vy[j]});
        json eop;
        eop["op"] = "sketch_add_entity";
        eop["sketch"] = sref;
        eop["entity"] = std::move(ent);
        ops.push_back(std::move(eop));
    }

    json region;
    region["kind"] = "region";
    region["sketch"] = sref;
    region["centroid"] = json::array({0.0, 0.0});
    region["hint_index"] = 0;
    json exp;
    exp["mode"] = "new";
    exp["end"] = "blind";
    exp["depth"] = dim(h);
    json exrefs;
    exrefs["profile"] = json::array({region});
    json exfeat;
    exfeat["type"] = "extrude";
    exfeat["name"] = name;
    exfeat["params"] = exp;
    exfeat["refs"] = exrefs;
    json exop;
    exop["op"] = "add_feature";
    exop["feature"] = exfeat;
    ops.push_back(std::move(exop));
}

// Sketch on the socket floor and cut upwards to the top of the head. The
// full-depth hex is editable; the real broach point and corner radii are omitted.
void emit_hex_socket(json& ops, double af, double floor_z, double depth) {
    const double pi = 3.14159265358979323846;
    const double radius = af / std::sqrt(3.0);
    std::array<std::array<double, 2>, 6> points;
    for (int i = 0; i < 6; ++i) {
        const double angle = (30.0 + 60.0 * i) * pi / 180.0;
        points[i] = {radius * std::cos(angle), radius * std::sin(angle)};
    }
    json entities = json::array(), constraints = json::array();
    for (int i = 0; i < 6; ++i) {
        const int next = (i + 1) % 6;
        const std::string id = "e" + std::to_string(i + 1);
        entities.push_back({{"id", id}, {"type", "line"},
                            {"p1", {points[i][0], points[i][1]}},
                            {"p2", {points[next][0], points[next][1]}}});
        constraints.push_back({{"id", "c" + std::to_string(i + 1)},
                               {"type", "coincident"},
                               {"refs", {id + ".p2", "e" + std::to_string(next + 1) + ".p1"}}});
    }
    const std::string sketch = "$" + std::to_string(ops.size());
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "sketch"}, {"name", "hex socket profile"},
                                {"params", {{"entities", std::move(entities)},
                                            {"constraints", std::move(constraints)},
                                            {"offset", dim(floor_z)}}},
                                {"refs", {{"plane", "plane.top"}}}}}});
    const json region{{"kind", "region"}, {"sketch", sketch},
                      {"centroid", {0.0, 0.0}}, {"hint_index", 0}};
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "extrude"}, {"name", "hex socket"},
                                {"params", {{"mode", "cut"}, {"end", "blind"},
                                            {"depth", dim(depth)}}},
                                {"refs", {{"profile", json::array({region})}}}}}});
}

// The front sketch contains the Z datum axis. Revolving a closed, offset circle
// creates an exact torus. A construction diameter reaches the circle's inner
// rim, so the ID and the section diameter remain directly editable dimensions.
void emit_oring(json& ops, double id_mm, double section_mm) {
    const double section_radius = section_mm / 2.0;
    const double major_radius = (id_mm + section_mm) / 2.0;
    const std::string sketch = "$" + std::to_string(ops.size());
    json entities = json::array({
        json{{"id", "e1"}, {"type", "circle"}, {"construction", false},
             {"center", {major_radius, 0.0}}, {"radius", section_radius}},
        json{{"id", "e2"}, {"type", "line"}, {"construction", true},
             {"p1", {-id_mm / 2.0, 0.0}}, {"p2", {id_mm / 2.0, 0.0}}}
    });
    json constraints = json::array({
        json{{"id", "c1"}, {"type", "point_on"}, {"refs", {"e1.center", "axis.h"}}},
        json{{"id", "c2"}, {"type", "midpoint"}, {"refs", {"origin", "e2"}}},
        json{{"id", "c3"}, {"type", "horizontal"}, {"refs", {"e2"}}},
        json{{"id", "c4"}, {"type", "point_on"}, {"refs", {"e2.p2", "e1"}}},
        json{{"id", "c5"}, {"type", "length"}, {"refs", {"e2"}},
             {"value", dim(id_mm)}, {"driven", false}},
        json{{"id", "c6"}, {"type", "diameter"}, {"refs", {"e1"}},
             {"value", dim(section_mm)}, {"driven", false}}
    });
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "sketch"}, {"name", "O-ring section"},
                                {"params", {{"entities", std::move(entities)},
                                            {"constraints", std::move(constraints)}}},
                                {"refs", {{"plane", "plane.front"}}}}}});
    const json region{{"kind", "region"}, {"sketch", sketch},
                      {"centroid", {major_radius, 0.0}}, {"hint_index", 0}};
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "revolve"}, {"name", "O-ring"},
                                {"params", {{"mode", "boss"}, {"angle", 360}}},
                                {"refs", {{"profile", json::array({region})},
                                          {"axis", {{"kind", "datum"}, {"feature", "axis.z"}}}}}}}});
}

double parse_complete_double(const std::string& s) {
    char* end = nullptr;
    const double value = std::strtod(s.c_str(), &end);
    if (end == s.c_str() || end != s.c_str() + s.size() || !std::isfinite(value))
        return 0.0;
    return value;
}

// standoff / spacer outside diameter from the thread nominal Ø.
double standoff_od(double d) {
    if (d <= 3.0) return 6.0;
    if (d <= 4.0) return 7.0;
    if (d <= 5.0) return 8.0;
    return 2.0 * d;
}

[[noreturn]] void bad(const std::string& msg) { throw std::invalid_argument(msg); }
// Normalize a size token: trim, and canonicalise metric "m3"/"M3" -> "M3".
std::string normalize_size(std::string size) {
    // trim whitespace
    std::size_t b = 0;
    while (b < size.size() && std::isspace(static_cast<unsigned char>(size[b]))) ++b;
    std::size_t e = size.size();
    while (e > b && std::isspace(static_cast<unsigned char>(size[e - 1]))) --e;
    size = size.substr(b, e - b);
    if (size.size() >= 2 && (size[0] == 'm' || size[0] == 'M') &&
        std::isdigit(static_cast<unsigned char>(size[1]))) {
        size[0] = 'M';
    }
    return size;
}

}  // namespace

std::string family_keys_csv() {
    std::string out;
    for (const Family& fam : catalog()) {
        if (!out.empty()) out += ", ";
        out += fam.key;
    }
    return out;
}

PartBuild build(const std::string& family, const std::string& size_in,
                double length_mm, const json& options) {
    json ops = json::array();
    json specs;
    json output_axis;
    std::string name;

    if (family.empty())
        bad(std::string("missing family; valid families: ") + family_keys_csv());

    const std::string size = normalize_size(size_in);
    if (size.empty())
        bad("missing size/thread (e.g. \"M3\", \"1/4-20\", or bearing \"608\")");

    // The domain catalogs resolve their own exact identities and dimensions.
    if (family == "piping_fitting")
        return build_fitting_geometry(size, options);
    if (family == "structural_section") {
        if (!options.is_object() || !options.empty())
            bad("structural section does not accept parts options");
        return build_section_geometry(size, length_mm);
    }

    // Resolve a fastener row (metric first, then SAE) for the screw/nut/washer set.
    const data::MetricRow* m = data::find_metric(size);
    const data::SaeRow* sae = m ? nullptr : data::find_sae(size);

    if (!options.is_object()) bad("parts options must be an object");
    for (auto it = options.begin(); it != options.end(); ++it) {
        if (it.key() == "property_class")
            bad("unsupported option property_class: generated geometry does not certify fastener mechanical properties");
        if (it.key() != "standard")
            bad("unsupported parts option: " + it.key());
        // This validates the nominal envelope only, not threads, tolerances,
        // material, or certification. Other standard selections are not implemented.
        if (!it.value().is_string() || family != "hex_bolt" || !m ||
            (it.value() != "ISO 4017" && it.value() != "ISO4017"))
            bad("unsupported standard for " + family + " " + size +
                ": only metric hex_bolt ISO 4017 nominal dimensions can be selected");
    }

    auto need_len = [&](double dflt) -> double {
        return (length_mm > 0.0) ? length_mm : dflt;
    };
    if (family == "shcs") {
        double d, hd, hh;
        if (m) { d = m->d; hd = m->dk; hh = m->k; }
        else if (sae) { d = sae->d; hd = sae->shcs_d; hh = sae->shcs_h; }
        else bad("unknown size for shcs: " + size);
        const data::SocketRow* socket = data::find_shcs_socket(size);
        if (!socket) bad("socket dimensions unavailable for shcs: " + size);
        const double L = need_len(10.0);
        ops.push_back(op_cylinder("shank", d / 2.0, L, 0.0, "boss"));
        ops.push_back(op_cylinder("head", hd / 2.0, hh, L, "boss"));
        emit_hex_socket(ops, socket->key_width, L + hh - socket->depth, socket->depth);
        specs = {{"thread_d", d}, {"head_d", hd}, {"head_h", hh}, {"length", L},
                 {"socket_key_width", socket->key_width}, {"socket_depth", socket->depth},
                 {"socket_depth_basis", "minimum full-hex depth"}};
        name = "SHCS " + size + " x" + std::to_string((int)L);
    } else if (family == "bhcs") {
        if (!m) bad("unknown size for bhcs: " + size);
        const double L = need_len(10.0);
        ops.push_back(op_cylinder("shank", m->d / 2.0, L, 0.0, "boss"));
        ops.push_back(op_cylinder("head", m->bh_d / 2.0, m->bh_h, L, "boss"));
        specs = {{"thread_d", m->d}, {"head_d", m->bh_d}, {"head_h", m->bh_h}, {"length", L}};
        name = "BHCS " + size + " x" + std::to_string((int)L);
    } else if (family == "fhcs") {
        if (!m) bad("unknown size for fhcs: " + size);
        const double L = need_len(10.0);
        const double hh = (m->fh_d - m->d) / 2.0;  // 90° included angle
        ops.push_back(op_cylinder("shank", m->d / 2.0, L, 0.0, "boss"));
        ops.push_back(op_cone("head", m->d / 2.0, m->fh_d / 2.0, hh, L, "boss"));
        specs = {{"thread_d", m->d}, {"head_d", m->fh_d}, {"head_h", hh}, {"length", L}};
        name = "FHCS " + size + " x" + std::to_string((int)L);
    } else if (family == "hex_bolt") {
        double d, af, head_h;
        if (m) { d = m->d; af = m->hex_af; head_h = m->hex_h; }
        else if (sae) {
            d = sae->d; af = sae->hex_af;
            // Legacy approximate SAE envelope, NOT ASME B18.2.1 dimensional compliance.
            // The published table gives tolerance bounds for >=1/4 inch only:
            // https://boltport.com/standards/asme-b18.2.1/hex-bolts/
            // Numbered sizes in this catalog are outside that table's scope.
            head_h = 0.66 * d;
        }
        else bad("unknown size for hex_bolt: " + size);
        const double L = need_len(16.0);
        emit_hex_prism(ops, "head", af, head_h);
        ops.push_back(op_cylinder("shank", d / 2.0, L, head_h, "boss"));
        specs = {{"thread_d", d}, {"hex_af", af}, {"head_h", head_h}, {"length", L}};
        name = "Hex Bolt " + size + " x" + std::to_string((int)L);
    } else if (family == "hex_nut") {
        double d, af, t;
        if (m) { d = m->d; af = m->hex_af; t = m->nut_t; }
        else if (sae) { d = sae->d; af = sae->hex_af; t = sae->nut_t; }
        else bad("unknown size for hex_nut: " + size);
        emit_hex_prism(ops, "body", af, t);
        ops.push_back(op_cylinder("bore", d / 2.0, t + 2.0, -1.0, "cut"));
        specs = {{"thread_d", d}, {"hex_af", af}, {"thickness", t}};
        name = "Hex Nut " + size;
    } else if (family == "washer_flat") {
        double id, od, t;
        if (m) { id = m->wash_id; od = m->wash_od; t = m->wash_t; }
        else if (sae) { id = sae->wash_id; od = sae->wash_od; t = sae->wash_t; }
        else bad("unknown size for washer_flat: " + size);
        ops.push_back(op_cylinder("ring", od / 2.0, t, 0.0, "boss"));
        ops.push_back(op_cylinder("bore", id / 2.0, t + 2.0, -1.0, "cut"));
        specs = {{"id", id}, {"od", od}, {"thickness", t}};
        name = "Flat Washer " + size;
    } else if (family == "washer_lock") {
        if (!m) bad("unknown size for washer_lock: " + size);
        const double id = m->d + 0.5, od = 1.8 * m->d, t = m->d / 4.0;
        ops.push_back(op_cylinder("ring", od / 2.0, t, 0.0, "boss"));
        ops.push_back(op_cylinder("bore", id / 2.0, t + 2.0, -1.0, "cut"));
        specs = {{"id", id}, {"od", od}, {"thickness", t}};
        name = "Lock Washer " + size;
    } else if (family == "standoff_round") {
        if (!m) bad("unknown size for standoff_round: " + size);
        const double L = need_len(10.0);
        const double od = standoff_od(m->d), bore = (m->d - m->pitch);
        ops.push_back(op_cylinder("body", od / 2.0, L, 0.0, "boss"));
        ops.push_back(op_cylinder("bore", bore / 2.0, L + 2.0, -1.0, "cut"));
        specs = {{"thread_d", m->d}, {"od", od}, {"length", L}};
        name = "Round Standoff " + size + " x" + std::to_string((int)L);
    } else if (family == "standoff_hex") {
        if (!m) bad("unknown size for standoff_hex: " + size);
        const double L = need_len(10.0);
        const double bore = (m->d - m->pitch);
        emit_hex_prism(ops, "body", m->hex_af, L);
        ops.push_back(op_cylinder("bore", bore / 2.0, L + 2.0, -1.0, "cut"));
        specs = {{"thread_d", m->d}, {"hex_af", m->hex_af}, {"length", L}};
        name = "Hex Standoff " + size + " x" + std::to_string((int)L);
    } else if (family == "threaded_rod") {
        double d;
        if (m) d = m->d;
        else if (sae) d = sae->d;
        else bad("unknown size for threaded_rod: " + size);
        const double L = need_len(50.0);
        ops.push_back(op_cylinder("rod", d / 2.0, L, 0.0, "boss"));
        specs = {{"thread_d", d}, {"length", L}};
        name = "Threaded Rod " + size + " x" + std::to_string((int)L);
    } else if (family == "dowel_pin") {
        const double d = parse_complete_double(size);
        if (d <= 0.0) bad("unknown size for dowel_pin: " + size);
        const double L = need_len(10.0);
        ops.push_back(op_cylinder("pin", d / 2.0, L, 0.0, "boss"));
        specs = {{"diameter", d}, {"length", L}};
        name = "Dowel Pin " + size + " x" + std::to_string((int)L);
    } else if (family == "heat_set_insert") {
        if (!m) bad("unknown size for heat_set_insert: " + size);
        if (m->insert_od <= 0.0) bad("no heat-set insert offered for " + size);
        const double L = need_len(2.0 * m->d);
        const double bore = (m->d - m->pitch);
        ops.push_back(op_cylinder("body", m->insert_od / 2.0, L, 0.0, "boss"));
        ops.push_back(op_cylinder("bore", bore / 2.0, L + 2.0, -1.0, "cut"));
        specs = {{"thread_d", m->d}, {"od", m->insert_od}, {"length", L}};
        name = "Heat-Set Insert " + size + " x" + std::to_string((int)L);
    } else if (family == "bearing") {
        const data::BearingRow* b = data::find_bearing(size);
        if (!b) bad("unknown bearing: " + size);
        ops.push_back(op_cylinder("outer", b->od / 2.0, b->width, 0.0, "boss"));
        // Recess the shield between the inner and outer race on each face.
        // The inner race bosses overlap the core so they fuse into one editable solid.
        const double radial_width = (b->od - b->bore) / 2.0;
        const double inner_race_radius = b->bore / 2.0 + radial_width * 0.25;
        const double outer_race_inner_radius = b->od / 2.0 - radial_width * 0.25;
        const double recess = std::min(0.3, 0.04 * b->width);
        ops.push_back(op_cylinder("lower shield recess", outer_race_inner_radius, recess,
                                  0.0, "cut"));
        ops.push_back(op_cylinder("lower inner race", inner_race_radius, 2.0 * recess,
                                  0.0, "boss"));
        ops.push_back(op_cylinder("upper shield recess", outer_race_inner_radius, recess,
                                  b->width - recess, "cut"));
        ops.push_back(op_cylinder("upper inner race", inner_race_radius, 2.0 * recess,
                                  b->width - 2.0 * recess, "boss"));
        // Bore through the core and both inner race bosses.
        ops.push_back(op_cylinder("bore", b->bore / 2.0, b->width + 2.0, -1.0, "cut"));
        specs = {{"bore", b->bore}, {"od", b->od}, {"width", b->width}};
        name = "Bearing " + size;
    } else if (family == "oring_as568") {
        const data::OringRow* row = data::find_oring_as568(size);
        if (!row) bad("unknown AS568 O-ring: " + size);
        constexpr double mm_per_mil = 25.4 / 1000.0;
        const double id = row->id_mils * mm_per_mil;
        const double section = row->cs_mils * mm_per_mil;
        emit_oring(ops, id, section);
        specs = {{"id_mm", id}, {"cs_mm", section}, {"od_mm", id + 2.0 * section},
                 {"id_in", row->id_mils / 1000.0}, {"cs_in", row->cs_mils / 1000.0},
                 {"source", "SAE AS568B nominal ID/CS table, cross-checked against Parker ORD-5700 and other published charts"},
                 {"notes", "Nominal circular section; tolerances, material properties and deformation are not modeled."}};
        name = size + " O-Ring";
    } else if (family == "hobby_servo") {
        const data::HobbyServoRow* s = data::find_hobby_servo(size);
        if (!s) bad("unknown hobby servo: " + size);
        const auto& d = s->detail;
        const double shaft_x = s->body_l / 2.0 - s->shaft_from_end;
        if (d.bottom_h > 0.0) {
            ops.push_back(op_box("bottom cover", s->body_l - 2.0 * d.bottom_inset,
                                 s->body_w - 2.0 * d.bottom_inset, d.bottom_h, 0.0, "boss"));
        }
        ops.push_back(op_box("case body", s->body_l, s->body_w,
                             s->body_h - d.bottom_h - d.cap_h, d.bottom_h, "boss"));
        if (d.cap_h > 0.0) {
            ops.push_back(op_box("top cap", s->body_l - 2.0 * d.cap_inset,
                                 s->body_w - 2.0 * d.cap_inset,
                                 d.cap_h, s->body_h - d.cap_h, "boss"));
        }
        ops.push_back(op_box("mounting flange", s->tab_span, s->body_w,
                             s->tab_thickness, s->tab_bottom_z, "boss"));
        for (double x : {-s->hole_pitch / 2.0, s->hole_pitch / 2.0}) {
            const int rows = d.slot_row_pitch > 0.0 ? 2 : 1;
            for (int row = 0; row < rows; ++row) {
                const double y = rows == 1 ? 0.0 : (row == 0 ? -0.5 : 0.5) * d.slot_row_pitch;
                const std::string suffix = std::string(x < 0 ? " -X" : " +X") +
                    (rows == 1 ? "" : (y < 0 ? " -Y" : " +Y"));
                json hole = op_servo_cylinder("mount hole" + suffix, s->hole_d / 2.0,
                                               s->tab_thickness + 2.0, x,
                                               s->tab_bottom_z - 1.0, "cut");
                hole["feature"]["params"]["at"][1] = y;
                ops.push_back(std::move(hole));
                if (d.slot_opening_w > 0.0) {
                    // Continue from the hole centre past the end of the tab.
                    const double slot_l = (s->tab_span - s->hole_pitch) / 2.0 + 1.0;
                    json slot = op_box("slot opening" + suffix, slot_l, d.slot_opening_w,
                                       s->tab_thickness + 2.0, s->tab_bottom_z - 1.0, "cut");
                    slot["feature"]["params"]["at"][0] =
                        x < 0 ? -s->tab_span / 2.0 - 1.0 : x;
                    slot["feature"]["params"]["at"][1] = y - d.slot_opening_w / 2.0;
                    ops.push_back(std::move(slot));
                }
            }
        }
        const double lower_boss_h = d.boss_top_d > 0.0 ? d.boss_lower_h : s->boss_h;
        ops.push_back(op_servo_cylinder("output boss", s->boss_d / 2.0,
                                         lower_boss_h, shaft_x, s->body_h, "boss"));
        if (d.boss_top_d > 0.0) {
            ops.push_back(op_servo_cylinder("upper output boss", d.boss_top_d / 2.0,
                                             s->boss_h - lower_boss_h, shaft_x,
                                             s->body_h + lower_boss_h, "boss"));
        }
        if (d.gear_bump_d > 0.0) {
            ops.push_back(op_servo_cylinder("secondary gear bump", d.gear_bump_d / 2.0,
                                             d.gear_bump_h, shaft_x - d.gear_bump_offset,
                                             s->body_h, "boss"));
        }
        if (d.cable_l > 0.0) {
            json cable = op_box("cable strain relief", d.cable_l, d.cable_w, d.cable_h,
                                d.cable_bottom_z, "boss");
            cable["feature"]["params"]["at"][0] = s->body_l / 2.0;
            ops.push_back(std::move(cable));
        }
        if (d.spline_root_d > 0.0 && s->spline_teeth > 0) {
            emit_servo_spline(ops, shaft_x, s->body_h + s->boss_h, s->shaft_h,
                              s->shaft_d, d.spline_root_d, s->spline_teeth);
        } else {
            ops.push_back(op_servo_cylinder("output spline envelope", s->shaft_d / 2.0,
                                             s->shaft_h, shaft_x, s->body_h + s->boss_h, "boss"));
        }
        if (d.retaining_bore_d > 0.0) {
            const double top = s->body_h + s->boss_h + s->shaft_h;
            ops.push_back(op_servo_cylinder("output retaining screw tap bore",
                                             d.retaining_bore_d / 2.0,
                                             d.retaining_bore_depth + 1.0,
                                             shaft_x, top - d.retaining_bore_depth, "cut"));
        }
        output_axis = {{"origin", {shaft_x, 0.0, s->body_h + s->boss_h}},
                       {"axis", {0, 0, 1}}};
        specs = {{"body_l", s->body_l}, {"body_w", s->body_w}, {"body_h", s->body_h},
                 {"tab_span", s->tab_span}, {"tab_thickness", s->tab_thickness},
                 {"tab_bottom_z", s->tab_bottom_z}, {"hole_d", s->hole_d},
                 {"hole_pitch", s->hole_pitch}, {"shaft_from_end", s->shaft_from_end},
                 {"boss_d", s->boss_d}, {"boss_h", s->boss_h},
                 {"shaft_d", s->shaft_d}, {"shaft_h", s->shaft_h},
                 {"spline_teeth", s->spline_teeth},
                 {"source", s->source}, {"notes", s->notes},
                 {"detail", {{"slot_opening_w", d.slot_opening_w},
                             {"slot_row_pitch", d.slot_row_pitch},
                             {"cap_h", d.cap_h}, {"cap_inset", d.cap_inset},
                             {"bottom_h", d.bottom_h}, {"bottom_inset", d.bottom_inset},
                             {"boss_top_d", d.boss_top_d}, {"boss_lower_h", d.boss_lower_h},
                             {"gear_bump_d", d.gear_bump_d},
                             {"gear_bump_offset", d.gear_bump_offset},
                             {"gear_bump_h", d.gear_bump_h}, {"cable_l", d.cable_l},
                             {"cable_w", d.cable_w}, {"cable_h", d.cable_h},
                             {"cable_bottom_z", d.cable_bottom_z},
                             {"spline_root_d", d.spline_root_d},
                             {"retaining_bore_d", d.retaining_bore_d},
                             {"retaining_bore_depth", d.retaining_bore_depth}}}};
        name = "Hobby Servo " + size;
    } else {
        bad(std::string("unknown part family: \"") + family +
            "\"; valid families: " + family_keys_csv());
    }

    PartBuild pb;
    pb.name = name;
    pb.ops = std::move(ops);
    pb.meta = {{"family", family}, {"size", size}, {"length_mm", length_mm}, {"specs", specs}};
    if (!output_axis.is_null()) pb.meta["output_axis"] = std::move(output_axis);
    return pb;
}

}  // namespace parts
}  // namespace chudware
