// core/parts/generate.cpp — emit document ops for a concrete standard part.
//
// Everything is built from the modeler's own op vocabulary so the result is an
// editable feature tree: primitives (cylinder/cone) combined by the running-body
// model (mode "boss" fuses, "cut" bores), and a sketch+extrude for hex prisms.
// No OCCT here — we only emit JSON; chudware::doc compiles the geometry.
#include "parts/parts.h"
#include "parts/parts_data.h"

#include <cmath>
#include <cstdlib>
#include <stdexcept>
#include <cctype>


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

double parse_leading_double(const std::string& s) {
    return std::strtod(s.c_str(), nullptr);
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
    (void)options;
    json ops = json::array();
    json specs;
    std::string name;

    if (family.empty())
        bad(std::string("missing family; valid families: ") + family_keys_csv());

    const std::string size = normalize_size(size_in);
    if (size.empty())
        bad("missing size/thread (e.g. \"M3\", \"1/4-20\", or bearing \"608\")");

    // Resolve a fastener row (metric first, then SAE) for the screw/nut/washer set.
    const data::MetricRow* m = data::find_metric(size);
    const data::SaeRow* sae = m ? nullptr : data::find_sae(size);

    auto need_len = [&](double dflt) -> double {
        return (length_mm > 0.0) ? length_mm : dflt;
    };
    if (family == "shcs") {
        double d, hd, hh;
        if (m) { d = m->d; hd = m->dk; hh = m->k; }
        else if (sae) { d = sae->d; hd = sae->shcs_d; hh = sae->shcs_h; }
        else bad("unknown size for shcs: " + size);
        const double L = need_len(10.0);
        ops.push_back(op_cylinder("shank", d / 2.0, L, 0.0, "boss"));
        ops.push_back(op_cylinder("head", hd / 2.0, hh, L, "boss"));
        specs = {{"thread_d", d}, {"head_d", hd}, {"head_h", hh}, {"length", L}};
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
        double d, af;
        if (m) { d = m->d; af = m->hex_af; }
        else if (sae) { d = sae->d; af = sae->hex_af; }
        else bad("unknown size for hex_bolt: " + size);
        const double L = need_len(16.0);
        const double head_h = 0.66 * d;
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
        const double d = parse_leading_double(size);
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
        ops.push_back(op_cylinder("bore", b->bore / 2.0, b->width + 2.0, -1.0, "cut"));
        specs = {{"bore", b->bore}, {"od", b->od}, {"width", b->width}};
        name = "Bearing " + size;
    } else {
        bad(std::string("unknown part family: \"") + family +
            "\"; valid families: " + family_keys_csv());
    }

    PartBuild pb;
    pb.name = name;
    pb.ops = std::move(ops);
    pb.meta = {{"family", family}, {"size", size}, {"length_mm", length_mm}, {"specs", specs}};
    return pb;
}

}  // namespace parts
}  // namespace chudware
