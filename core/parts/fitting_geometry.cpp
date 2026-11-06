// core/parts/fitting_geometry.cpp — catalog-backed native fitting recipes.
#include "parts/fitting_geometry.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>

#include "piping/catalog.h"

namespace chudware::parts {
namespace {

using piping::ojson;

[[noreturn]] void invalid_identity(const std::string& size) {
    throw std::invalid_argument("invalid fitting catalog identity: " + size);
}

// Catalog NPS tokens are decimal inches, not arbitrary strtod expressions.
// Accept e.g. the older NPS1 spelling as well as the adapter's NPS1.0, but
// reject signs, exponent notation, whitespace and any unconsumed characters.
double parse_nps(const std::string& token, const std::string& size) {
    if (token.empty()) invalid_identity(size);
    bool dot = false;
    for (std::size_t i = 0; i < token.size(); ++i) {
        const char c = token[i];
        if (c == '.') {
            if (dot || i == 0 || i + 1 == token.size()) invalid_identity(size);
            dot = true;
        } else if (c < '0' || c > '9') {
            invalid_identity(size);
        }
    }
    if (token.size() > 1 && token[0] == '0' && token[1] != '.') invalid_identity(size);
    try {
        const double nps = std::stod(token);
        if (std::isfinite(nps) && nps > 0.0) return nps;
    } catch (const std::exception&) {
    }
    invalid_identity(size);
}

int parse_class(const std::string& token, const std::string& size) {
    if (token.empty() || token[0] == '0') invalid_identity(size);
    int value = 0;
    for (char c : token) {
        if (c < '0' || c > '9' || value > (std::numeric_limits<int>::max() - (c - '0')) / 10)
            invalid_identity(size);
        value = value * 10 + (c - '0');
    }
    if (value == 0) invalid_identity(size);
    return value;
}

struct ResolvedFitting {
    json identity;
    ojson dimensions;
};

ResolvedFitting resolve_fitting(const std::string& size) {
    const std::size_t colon = size.find(':');
    if (colon == std::string::npos) invalid_identity(size);
    const std::string type = size.substr(0, colon);
    if (type != "elbow" && type != "tee" && type != "reducer" && type != "flange")
        invalid_identity(size);
    const std::string rest = size.substr(colon + 1);
    if (rest.compare(0, 3, "NPS") != 0) invalid_identity(size);
    const std::string tokens = rest.substr(3);
    std::string primary = tokens;
    ojson nps2 = nullptr;
    ojson flange_class = nullptr;
    if (type == "reducer") {
        const std::size_t x = tokens.find('x');
        if (x == std::string::npos) invalid_identity(size);
        primary = tokens.substr(0, x);
        nps2 = parse_nps(tokens.substr(x + 1), size);
    } else if (type == "flange") {
        const std::size_t cl = tokens.find(":cl");
        if (cl == std::string::npos) invalid_identity(size);
        primary = tokens.substr(0, cl);
        flange_class = parse_class(tokens.substr(cl + 3), size);
    }
    const double nps = parse_nps(primary, size);
    ojson dims = piping::fitting_dims(type, nps, nps2, flange_class);
    if (dims.contains("ok") && !dims.value("ok", true))
        throw std::invalid_argument("fitting catalog identity " + size + ": " +
                                    dims.value("reason", std::string("not found")));
    // fitting_dims currently performs exact lookups; check the returned identity
    // as well so an eventual nearest-size implementation cannot silently change it.
    if (dims.value("type", std::string()) != type || !dims.contains("nps") ||
        !dims["nps"].is_number() || dims["nps"].get<double>() != nps ||
        (!nps2.is_null() && (!dims["nps2"].is_number() || dims["nps2"] != nps2)) ||
        (!flange_class.is_null() &&
         (!dims["flange_class"].is_number() || dims["flange_class"] != flange_class)))
        throw std::invalid_argument("fitting catalog returned a different identity for " + size);
    json identity = {{"type", type}, {"nps", nps}};
    if (!nps2.is_null()) identity["nps2"] = nps2.get<double>();
    if (!flange_class.is_null()) identity["flange_class"] = flange_class.get<int>();
    return {std::move(identity), std::move(dims)};
}

double dimension(const ojson& source, const char* key) {
    const auto it = source.find(key);
    if (it == source.end() || !it->is_number())
        throw std::invalid_argument(std::string("fitting geometry requires catalog dimension ") + key);
    const double value = it->get<double>();
    if (!std::isfinite(value) || value <= 0.0)
        throw std::invalid_argument(std::string("invalid catalog dimension ") + key);
    return value;
}

struct Pipe {
    double od;
    double id;
    double wall;
};

Pipe pipe_for(double nps, const std::string& schedule) {
    const ojson found = piping::nearest_pipe(nps, schedule);
    if (!found.value("ok", false) || !found.contains("nps") ||
        !found["nps"].is_number() || found["nps"].get<double>() != nps ||
        found.value("schedule", std::string()) != schedule) {
        throw std::invalid_argument("no exact pipe dimensions for NPS " +
                                    pyutil::py_float(nps) + " schedule " + schedule);
    }
    const Pipe pipe{dimension(found, "outer_dia"), dimension(found, "inner_dia"),
                    dimension(found, "wall")};
    if (pipe.id >= pipe.od)
        throw std::invalid_argument("invalid catalog pipe bore and outside diameter");
    return pipe;
}

json dim(double value) { return {{"value", value}, {"expr", nullptr}}; }

json cylinder(const std::string& name, double radius, double height,
              const std::array<double, 3>& at, const std::array<double, 3>& axis,
              const char* mode) {
    const json params = {{"radius", dim(radius)}, {"height", dim(height)},
                         {"at", json::array({at[0], at[1], at[2]})},
                         {"axis", json::array({axis[0], axis[1], axis[2]})},
                         {"centered", false}, {"mode", mode}, {"merge", true}};
    return {{"op", "add_feature"},
            {"feature", {{"type", "cylinder"}, {"name", name}, {"params", params}}}};
}

json cone(const std::string& name, double r1, double r2, double height, double z,
          const char* mode) {
    const json params = {{"r1", dim(r1)}, {"r2", dim(r2)}, {"height", dim(height)},
                         {"at", json::array({0.0, 0.0, z})}, {"centered", false},
                         {"mode", mode}, {"merge", true}};
    return {{"op", "add_feature"},
            {"feature", {{"type", "cone"}, {"name", name}, {"params", params}}}};
}

void build_flange(json& ops, json& specs, const ojson& dims, const Pipe& pipe) {
    const double od = dimension(dims, "od");
    const double thickness = dimension(dims, "thickness");
    const double bcd = dimension(dims, "bolt_circle");
    const double hole_dia = dimension(dims, "bolt_dia");
    const double holes_value = dimension(dims, "bolt_holes");
    if (holes_value > std::numeric_limits<int>::max() || std::floor(holes_value) != holes_value)
        throw std::invalid_argument("invalid flange catalog bolt hole count");
    const int holes = static_cast<int>(holes_value);
    if (holes < 2 || bcd + hole_dia >= od || bcd - hole_dia <= pipe.id)
        throw std::invalid_argument("flange catalog dimensions cannot form separate through holes");
    const double pi = std::acos(-1.0);
    if (bcd * std::sin(pi / holes) <= hole_dia)
        throw std::invalid_argument("flange catalog bolt holes overlap");

    const std::array<double, 3> z_axis{0.0, 0.0, 1.0};
    ops.push_back(cylinder("Flange disk", od / 2.0, thickness, {0.0, 0.0, 0.0}, z_axis, "boss"));
    ops.push_back(cylinder("Assumed pipe bore", pipe.id / 2.0, thickness + 2.0,
                           {0.0, 0.0, -1.0}, z_axis, "cut"));
    // Clocking is an assumption, not a field in the catalog: straddle the axes.
    for (int i = 0; i < holes; ++i) {
        const double angle = 2.0 * pi * (i + 0.5) / holes;
        ops.push_back(cylinder("Bolt hole " + std::to_string(i + 1), hole_dia / 2.0,
                               thickness + 2.0,
                               {bcd / 2.0 * std::cos(angle), bcd / 2.0 * std::sin(angle), -1.0},
                               z_axis, "cut"));
    }
    specs["od"] = od;
    specs["thickness"] = thickness;
    specs["bolt_circle"] = bcd;
    specs["bolt_holes"] = holes;
    specs["bolt_dia"] = hole_dia;
    specs["bolt_clocking_deg"] = 180.0 / holes;
    specs["assumptions"].push_back("Flat annular flange; bore equals the selected pipe schedule's inner diameter.");
    specs["assumptions"].push_back("Bolt holes are equally spaced and straddle the principal axes; catalog does not specify clocking.");
    specs["omissions"].push_back("Flange style, hub, weld preparation and raised face are not specified by the fitting catalog.");
}

void build_elbow(json& ops, json& specs, const ojson& dims, const Pipe& pipe) {
    const double center_to_end = dimension(dims, "center_to_end");
    if (center_to_end <= pipe.od / 2.0)
        throw std::invalid_argument("elbow centerline radius is not larger than its pipe radius");
    // The 90-degree, constant-section bend is a modeling assumption: the row
    // supplies only center-to-end. Revolve an annular, constrained profile.
    const double outer_r = pipe.od / 2.0;
    const double inner_r = pipe.id / 2.0;
    const json entities = json::array({
        {{"id", "e1"}, {"type", "circle"}, {"center", {center_to_end, 0.0}}, {"radius", outer_r}},
        {{"id", "e2"}, {"type", "circle"}, {"center", {center_to_end, 0.0}}, {"radius", inner_r}},
    });
    const json constraints = json::array({
        {{"id", "c1"}, {"type", "point_on"}, {"refs", {"e1.center", "axis.h"}}},
        {{"id", "c2"}, {"type", "distance"}, {"refs", {"origin", "e1.center"}},
         {"axis", "h"}, {"value", dim(center_to_end)}, {"driven", false}},
        {{"id", "c3"}, {"type", "concentric"}, {"refs", {"e1", "e2"}}},
        {{"id", "c4"}, {"type", "radius"}, {"refs", {"e1"}}, {"value", dim(outer_r)}, {"driven", false}},
        {{"id", "c5"}, {"type", "radius"}, {"refs", {"e2"}}, {"value", dim(inner_r)}, {"driven", false}},
    });
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "sketch"}, {"name", "Elbow pipe section"},
                                {"params", {{"entities", entities}, {"constraints", constraints}}},
                                {"refs", {{"plane", "plane.top"}}}}}});
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "revolve"}, {"name", "90-degree bend"},
                                {"params", {{"mode", "new"}, {"angle", dim(90.0)}}},
                                {"refs", {{"profile", json::array({{{"kind", "region"}, {"sketch", "$0"}}})},
                                          {"axis", "axis.y"}}}}}});
    specs["center_to_end"] = center_to_end;
    specs["bend_radius_mm"] = center_to_end;
    specs["bend_angle_deg"] = 90.0;
    specs["assumptions"].push_back("90-degree elbow with centerline radius equal to catalog center-to-end and constant circular pipe section.");
    specs["omissions"].push_back("Catalog does not specify elbow angle, actual wall variation or end preparation.");
}

void build_tee(json& ops, json& specs, const ojson& dims, const Pipe& pipe) {
    const double center_to_end = dimension(dims, "center_to_end");
    if (center_to_end <= pipe.od / 2.0)
        throw std::invalid_argument("tee center-to-end cannot accommodate the pipe radius");
    const std::array<double, 3> x_axis{1.0, 0.0, 0.0}, z_axis{0.0, 0.0, 1.0};
    ops.push_back(cylinder("Tee run", pipe.od / 2.0, 2.0 * center_to_end,
                           {-center_to_end, 0.0, 0.0}, x_axis, "boss"));
    ops.push_back(cylinder("Tee branch", pipe.od / 2.0, center_to_end,
                           {0.0, 0.0, 0.0}, z_axis, "boss"));
    ops.push_back(cylinder("Run bore", pipe.id / 2.0, 2.0 * center_to_end + 2.0,
                           {-center_to_end - 1.0, 0.0, 0.0}, x_axis, "cut"));
    ops.push_back(cylinder("Branch bore", pipe.id / 2.0, center_to_end + 1.0,
                           {0.0, 0.0, 0.0}, z_axis, "cut"));
    specs["center_to_end"] = center_to_end;
    specs["assumptions"].push_back("Equal 90-degree tee: all three center-to-end lengths and pipe sections are the same.");
    specs["omissions"].push_back("Branch blend, reinforcement, actual wall variation and end preparation are not catalogued.");
}

void build_reducer(json& ops, json& specs, const ojson& dims, const Pipe& large, const Pipe& small) {
    const double length = dimension(dims, "length");
    if (large.od <= small.od || large.id <= small.id)
        throw std::invalid_argument("reducer needs a larger primary pipe than secondary pipe");
    const double r1 = large.id / 2.0, r2 = small.id / 2.0;
    const double slope = (r1 - r2) / length;
    // Extend the cutter beyond both end faces while retaining the exact bore
    // diameters at z=0 and z=length.
    const double extra = std::min({1.0, length / 100.0, r2 / (2.0 * slope)});
    ops.push_back(cone("Reducer outside", large.od / 2.0, small.od / 2.0, length, 0.0, "boss"));
    ops.push_back(cone("Reducer bore", r1 + slope * extra, r2 - slope * extra,
                       length + 2.0 * extra, -extra, "cut"));
    specs["length"] = length;
    specs["pipe2_od_mm"] = small.od;
    specs["pipe2_inner_dia_mm"] = small.id;
    specs["pipe2_wall_mm"] = small.wall;
    specs["assumptions"].push_back("Concentric reducer with linear outside and bore tapers between the selected pipe end dimensions.");
    specs["omissions"].push_back("Reducer eccentricity, transition profile, end preparation and actual wall variation are not catalogued.");
}

}  // namespace

json fitting_catalog_identity(const std::string& size) {
    return resolve_fitting(size).identity;
}

PartBuild build_fitting_geometry(const std::string& size) {
    return build_fitting_geometry(size, json::object());
}

PartBuild build_fitting_geometry(const std::string& size, const json& options) {
    if (!options.is_object()) throw std::invalid_argument("fitting options must be an object");
    for (auto it = options.begin(); it != options.end(); ++it)
        if (it.key() != "schedule") throw std::invalid_argument("unsupported fitting option: " + it.key());
    const bool schedule_assumed = !options.contains("schedule");
    if (!schedule_assumed && !options["schedule"].is_string())
        throw std::invalid_argument("fitting schedule must be a string");
    const std::string requested = schedule_assumed ? "40" : options["schedule"].get<std::string>();
    std::string alias = requested;
    std::transform(alias.begin(), alias.end(), alias.begin(),
                   [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
    const std::string schedule = alias == "STD" ? "40" : alias == "XS" ? "80" : requested;
    const ResolvedFitting fitting = resolve_fitting(size);
    const std::string type = fitting.identity.at("type").get<std::string>();
    const double nps = fitting.identity.at("nps").get<double>();
    const Pipe pipe = pipe_for(nps, schedule);

    json specs = {{"catalog_id", size}, {"type", type}, {"nps", nps},
                  {"schedule", schedule}, {"schedule_requested", requested},
                  {"schedule_assumed", schedule_assumed},
                  {"pipe_od_mm", pipe.od}, {"pipe_inner_dia_mm", pipe.id},
                  {"pipe_wall_mm", pipe.wall}, {"bore_mm", pipe.id},
                  {"catalog_source", "core/piping/fittings_data.h (bundled B16 dimensional facts)"},
                  {"pipe_source", "core/piping/pipe_tables.h (ASME B36 schedule tables)"},
                  {"geometry_quality", "nominal approximation; not a certified fitting"},
                  {"assumptions", json::array()},
                  {"omissions", json::array({"Tolerances, material, pressure rating and certification are not modeled."})}};
    if (schedule_assumed)
        specs["assumptions"].push_back("Pipe schedule 40 assumed because the fitting catalog does not specify a schedule.");
    else
        specs["assumptions"].push_back("Pipe dimensions use the selected schedule; the fitting catalog does not specify a schedule.");
    json ops = json::array();
    if (type == "flange") {
        specs["flange_class"] = fitting.identity.at("flange_class");
        build_flange(ops, specs, fitting.dimensions, pipe);
    } else if (type == "elbow") {
        build_elbow(ops, specs, fitting.dimensions, pipe);
    } else if (type == "tee") {
        build_tee(ops, specs, fitting.dimensions, pipe);
    } else {
        const double nps2 = fitting.identity.at("nps2").get<double>();
        specs["nps2"] = nps2;
        build_reducer(ops, specs, fitting.dimensions, pipe, pipe_for(nps2, schedule));
    }
    PartBuild part;
    part.name = "Piping fitting " + size;
    part.ops = std::move(ops);
    part.meta = {{"family", "piping_fitting"}, {"size", size}, {"specs", std::move(specs)}};
    return part;
}

}  // namespace chudware::parts
