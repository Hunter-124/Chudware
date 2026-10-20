// Deterministic, OCCT-free ECAD phase benchmark. See --help for fixture options.
// Keep zonefill.cpp as a separate linker input before libchudware_ecad.a to
// benchmark an archived implementation without rebuilding the release library.
#include "ecad/drc.h"
#include "ecad/schema.h"
#include "ecad/zonefill.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

using nlohmann::json;
using namespace chudware::ecad;

namespace {

struct Options {
    std::string fixture = "dense";
    int side = 50;
    double resolution = 0.25;
    int iterations = 1;
    std::string output;
};

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

int integer(const std::string& text, const std::string& flag) {
    size_t consumed = 0;
    const int result = std::stoi(text, &consumed);
    require(consumed == text.size(), flag + " must be an integer");
    return result;
}

Options options(int argc, char** argv) {
    Options out;
    for (int i = 1; i < argc; ++i) {
        const std::string flag = argv[i];
        if (flag == "--help" || flag == "-h") {
            std::cout << "Usage: ecad_profile [--fixture dense|mixed|boundary] [--side N]\n"
                         "                    [--resolution MM] [--iterations N] [--output FILE]\n"
                         "dense: audit grid, circle pads, one GND zone and horizontal tracks.\n"
                         "mixed: circles/rotated boxes, diagonal tracks, two copper layers,\n"
                         "       through-hole pads and four-spoke thermal relief.\n"
                         "boundary: mixed board at a negative, fractional origin, with a\n"
                         "          concave outline clipping an oversized zone polygon.\n"
                         "Defaults: dense, side=50 (50x50 pads), resolution=0.25 mm, iterations=1.\n"
                         "stdout is JSON phase timings and FNV-1a-64 of the complete results.\n"
                         "--output writes canonical JSON {filled_board,drc,ratsnest}, excluding\n"
                         "timings, for exact baseline/optimized cmp. FILE=- writes it to stdout\n"
                         "and sends timings to stderr. Iterations must be byte-identical.\n"
                         "The unrouted grid intentionally reports airwires; rasterized zone\n"
                         "clearance/island findings are retained, not suppressed. Unexpected\n"
                         "DRC rules, invalid geometry, schema errors and I/O errors fail.\n";
            return Options{"", 0, 0.0, 0, ""};
        }
        require(i + 1 < argc, "missing value for " + flag);
        const std::string value = argv[++i];
        if (flag == "--fixture") out.fixture = value;
        else if (flag == "--side") out.side = integer(value, flag);
        else if (flag == "--iterations") out.iterations = integer(value, flag);
        else if (flag == "--output") out.output = value;
        else if (flag == "--resolution") {
            size_t consumed = 0;
            out.resolution = std::stod(value, &consumed);
            require(consumed == value.size(), flag + " must be a number");
        } else throw std::runtime_error("unknown option " + flag);
    }
    require(out.fixture == "dense" || out.fixture == "mixed" || out.fixture == "boundary",
            "fixture must be dense, mixed or boundary");
    require(out.side >= 5 && out.side <= 200, "side must be in [5,200]");
    require(out.iterations > 0, "iterations must be positive");
    require(std::isfinite(out.resolution) && out.resolution > 0.0 && out.resolution <= 0.5,
            "resolution must be finite and in (0,0.5] mm");
    require((2.0 * out.side + 2.0) / out.resolution < std::numeric_limits<int>::max(),
            "resolution would overflow the zone-fill grid dimensions");
    return out;
}

json xy(double x, double y) { return json::array({x, y}); }

json rectangle(double x0, double y0, double x1, double y1) {
    return json::array({xy(x0, y0), xy(x1, y0), xy(x1, y1), xy(x0, y1)});
}

json make_board(const Options& opt) {
    const bool mixed = opt.fixture != "dense";
    const bool boundary = opt.fixture == "boundary";
    const double ox = boundary ? -opt.side - 0.375 : 0.0;
    const double oy = boundary ? -0.75 * opt.side - 0.125 : 0.0;
    const double extent = 2.0 * opt.side;
    json b = schema::new_board("ECAD profile " + opt.fixture);
    b["id"] = "ecad-profile";
    b["origin"] = xy(ox, oy);
    for (int k = 0; k < 20; ++k)
        b["nets"].push_back(json{{"name", "N" + std::to_string(k)}, {"class", "default"}});

    for (int row = 0; row < opt.side; ++row) {
        for (int col = 0; col < opt.side; ++col) {
            const int net_index = (col * 7 + row * 3) % 21;
            const std::string net = net_index == 0 ? "GND" : "N" + std::to_string(net_index - 1);
            const std::string id = "p" + std::to_string(row * opt.side + col);
            const bool through_hole = mixed && net_index <= 1;
            const bool box = mixed && (row + col) % 3 != 0 && !through_hole;
            json pad{{"id", id}, {"net", net}, {"type", through_hole ? "tht" : "smd"},
                     {"shape", box ? "rect" : "circle"},
                     {"pos", xy(ox + 2.0 * col + 1.0, oy + 2.0 * row + 1.0)},
                     {"size", xy(0.8, box ? 0.6 : 0.8)},
                     {"layers", json::array({"F.Cu"})},
                     {"angle", box ? ((row + col) % 2 ? 45.0 : 15.0) : 0.0}};
            if (through_hole) {
                pad["drill"] = 0.4;
                pad["layers"] = json::array({"F.Cu", "B.Cu"});
            }
            b["footprints"].push_back(json{{"id", "fp" + std::to_string(row * opt.side + col)},
                                          {"pos", xy(0.0, 0.0)}, {"angle", 0.0}, {"side", "top"},
                                          {"pads", json::array({std::move(pad)})}});
        }
    }
    for (int row = 0; row < opt.side - 1; ++row) {
        const double y = oy + 2.0 * row + 2.0;
        b["tracks"].push_back(json{{"id", "t" + std::to_string(row)}, {"net", "N0"},
                                   {"layer", "F.Cu"}, {"width", 0.3}, {"kind", "segment"},
                                   {"p1", xy(ox + 1.0, y)},
                                   {"p2", xy(ox + extent - 1.0, y + (mixed && row % 2 ? 0.125 : 0.0))}});
    }
    const json polygon = boundary
        ? rectangle(ox - 0.375, oy - 0.125, ox + extent + 0.625, oy + extent + 0.375)
        : rectangle(ox, oy, ox + extent, oy + extent);
    auto add_zone = [&](const std::string& id, const std::string& net, const std::string& layer) {
        json z{{"id", id}, {"net", net}, {"layer", layer}, {"fill", "solid"},
               {"clearance", 0.2}, {"polygon", polygon}, {"filled_polys", json::array()}};
        if (mixed) z["thermal"] = json{{"gap", 0.35}, {"spoke_width", 0.25}};
        b["zones"].push_back(std::move(z));
    };
    add_zone("z1", "GND", "F.Cu");
    if (mixed) add_zone("z2", "N0", "B.Cu");
    if (boundary) {
        // A shallow notch in the right edge lies between pad columns; the grid
        // origin is fractional, so clipping cannot rely on zero-origin casts.
        const double notch_y = oy + 2.0 * std::floor(opt.side / 2.0);
        const json outline = json::array({xy(ox, oy), xy(ox + extent, oy),
            xy(ox + extent, notch_y - 0.125),
            xy(ox + extent - 0.5, notch_y - 0.125),
            xy(ox + extent - 0.5, notch_y + 0.125),
            xy(ox + extent, notch_y + 0.125),
            xy(ox + extent, oy + extent), xy(ox, oy + extent)});
        for (size_t i = 0; i < outline.size(); ++i)
            b["outline"]["shapes"].push_back(json{{"type", "line"}, {"p1", outline[i]},
                                                 {"p2", outline[(i + 1) % outline.size()]}});
        b["outline"]["closed"] = true;
    }
    return b;
}

size_t expected_airwires(const json& b) {
    std::map<std::string, size_t> terminals;
    for (const auto& fp : b.at("footprints"))
        for (const auto& p : fp.at("pads")) ++terminals[p.at("net").get<std::string>()];
    size_t count = 0;
    for (const auto& [net, size] : terminals) count += size - 1;
    // Track endpoints lie between pad rows; none joins terminals. Zone copper
    // is not part of ratsnest's endpoint-connectivity model.
    return count;
}

bool inside(const json& polys, double x, double y) {
    for (const auto& poly : polys) {
        bool hit = false;
        for (size_t i = 0, j = poly.size() - 1; i < poly.size(); j = i++) {
            const double xi = poly[i][0].get<double>(), yi = poly[i][1].get<double>();
            const double xj = poly[j][0].get<double>(), yj = poly[j][1].get<double>();
            if ((yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi)
                hit = !hit;
        }
        if (hit) return true;
    }
    return false;
}

void check_fixture(const json& b, const Options& opt) {
    const auto errors = schema::validate_board(b);
    require(errors.empty(), "fixture schema errors: " + json(errors).dump());
    require(b.at("footprints").size() == static_cast<size_t>(opt.side) * opt.side,
            "fixture pad count mismatch");
    require(b.at("tracks").size() == static_cast<size_t>(opt.side - 1), "fixture track count mismatch");
    const auto shapes = copper_shapes(b);
    std::set<std::string> kinds;
    for (const auto& shape : shapes) kinds.insert(shape.kind);
    require(kinds.count("circle") && kinds.count("seg"), "fixture lacks circles or segments");
    if (opt.fixture != "dense") require(kinds.count("box"), "mixed fixture lacks boxes");
}

void check_results(const json& b, const json& filled, const json& drc, const json& rn) {
    require(schema::validate_board(filled).empty(), "filled fixture failed schema validation");
    require(filled.at("zones").size() == b.at("zones").size(), "fill changed the zone count");
    for (const auto& z : filled.at("zones")) {
        const auto& polys = z.at("filled_polys");
        require(polys.is_array() && !polys.empty(), "fixture zone unexpectedly has no copper");
        for (const auto& poly : polys) {
            require(poly.is_array() && poly.size() >= 3, "fill returned a degenerate polygon");
            for (const auto& p : poly)
                require(p.is_array() && p.size() == 2 && p[0].is_number() && p[1].is_number() &&
                        std::isfinite(p[0].get<double>()) && std::isfinite(p[1].get<double>()),
                        "fill returned an invalid coordinate");
        }
    }
    // The first two pads have different nets, so the GND pour must retain the
    // first pad centre and clear the second. This checks real filled geometry.
    const auto& pads = filled.at("footprints");
    const auto [gx, gy] = schema::pad_world(pads[0], pads[0].at("pads")[0]);
    const auto [vx, vy] = schema::pad_world(pads[1], pads[1].at("pads")[0]);
    const auto& front = filled.at("zones")[0].at("filled_polys");
    require(inside(front, gx, gy), "GND zone lost its own pad copper");
    require(!inside(front, vx, vy), "GND zone did not clear a different-net pad");
    require(drc.is_array() && rn.is_array(), "DRC or ratsnest did not return an array");
    require(rn.size() == expected_airwires(b), "ratsnest differs from fixture connectivity");
    size_t unrouted = 0;
    for (const auto& violation : drc) {
        const std::string rule = violation.at("rule").get<std::string>();
        require(rule == "unrouted" || rule == "zone_clearance" || rule == "zone_island",
                "unexpected fixture DRC finding: " + violation.dump());
        if (rule == "unrouted") ++unrouted;
    }
    require(unrouted == rn.size(), "DRC unrouted count differs from ratsnest");
}

template <class F>
double milliseconds(F&& f) {
    const auto start = std::chrono::steady_clock::now();
    f();
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
}

std::string digest(const std::string& bytes) {
    uint64_t hash = UINT64_C(14695981039346656037);
    for (const unsigned char c : bytes) {
        hash ^= c;
        hash *= UINT64_C(1099511628211);
    }
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << hash;
    return out.str();
}

json timing_summary(std::vector<double> samples) {
    std::vector<double> sorted = samples;
    std::sort(sorted.begin(), sorted.end());
    const size_t n = sorted.size();
    const double median = n % 2 ? sorted[n / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0;
    return json{{"samples_ms", std::move(samples)}, {"min_ms", sorted.front()}, {"median_ms", median}};
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const Options opt = options(argc, argv);
        if (opt.fixture.empty()) return 0;
        const json b = make_board(opt);
        check_fixture(b, opt);
        std::vector<double> fill_times, drc_times, ratsnest_times;
        json reference;
        std::string bytes;
        for (int i = 0; i < opt.iterations; ++i) {
            json filled, drc, rn;
            fill_times.push_back(milliseconds([&] { filled = fill_zones(b, opt.resolution); }));
            drc_times.push_back(milliseconds([&] { drc = run_drc(filled); }));
            ratsnest_times.push_back(milliseconds([&] { rn = ratsnest(filled); }));
            check_results(b, filled, drc, rn);
            json result{{"filled_board", std::move(filled)}, {"drc", std::move(drc)},
                        {"ratsnest", std::move(rn)}};
            std::string current = result.dump();
            if (i == 0) {
                reference = std::move(result);
                bytes = std::move(current);
            } else require(current == bytes, "phase output changed across identical iterations");
        }
        if (!opt.output.empty() && opt.output != "-") {
            std::ofstream stream(opt.output, std::ios::binary | std::ios::trunc);
            require(stream.is_open(), "cannot open output " + opt.output);
            stream << bytes << '\n';
            stream.close();
            require(!stream.fail(), "cannot write output " + opt.output);
        }
        std::map<std::string, size_t> drc_rules;
        for (const auto& v : reference.at("drc")) ++drc_rules[v.at("rule").get<std::string>()];
        const json report{{"fixture", opt.fixture}, {"side", opt.side}, {"resolution_mm", opt.resolution},
                          {"iterations", opt.iterations}, {"pads", opt.side * opt.side},
                          {"tracks", b.at("tracks").size()}, {"zones", b.at("zones").size()},
                          {"fill_zones", timing_summary(std::move(fill_times))},
                          {"run_drc", timing_summary(std::move(drc_times))},
                          {"ratsnest", timing_summary(std::move(ratsnest_times))},
                          {"drc_rules", drc_rules}, {"airwires", reference.at("ratsnest").size()},
                          {"output_bytes", bytes.size()}, {"output_fnv1a64", digest(bytes)}};
        if (opt.output == "-") {
            std::cout << bytes << '\n';
            std::cerr << report.dump() << '\n';
        } else std::cout << report.dump() << '\n';
        require(std::cout.good(), "stdout write failed");
        return 0;
    } catch (const std::exception& e) {
        std::cerr << json{{"error", e.what()}}.dump() << '\n';
        return 1;
    }
}
