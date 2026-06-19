// mate_cli — tiny CLI wrapper around aicad::mate::solve_mates for the
// differential-test harness (tools/mate_diff.py).
//
// Usage:
//   mate_cli <fixture.json>     read the fixture from a file
//   mate_cli -                  read the fixture from stdin
//
// Input  : a JSON object
//   {
//     "components": [ {id, transform[16], fixed, suppressed}, ... ],
//     "mates":      [ {id, type, align, refs[{component}], value, suppressed}, ... ],
//     "resolved_geometry": [
//       { "mate_id": "m1", "ref_index": 0,
//         "geom": {kind, point[3], normal[3], direction[3], radius} }, ... ],
//     "warm":  { component_id: {"t":[3], "w":[3]}, ... }   (optional),
//     "drag":  { "component": id, "target":[3] }           (optional)
//   }
//   Any extra fields the harness fixtures carry ("name", "expect", ...) are ignored.
// Output : a JSON object printed to stdout, one line, full float precision:
//   {
//     "converged":    bool,
//     "dof":          int,
//     "over_defined": bool,
//     "max_residual": number,
//     "summary":      string,
//     "transforms":   { component_id: [16 floats], ... },
//     "params":       { component_id: {"t":[3], "w":[3]}, ... },
//     "mate_status":  [ {mate_id, type, status}, ... ]
//   }
//
// nlohmann::json serializes doubles round-trip (shortest exact representation),
// so the Python side parses back the same bit pattern. Exit code 0 on success, 2
// on a usage/IO/parse error.

#include "mate/mate.h"

#include <nlohmann/json.hpp>

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

using json = nlohmann::json;

namespace {

int fail(const std::string& msg) {
    std::cerr << "mate_cli: " << msg << "\n";
    return 2;
}

std::string read_all(std::istream& in) {
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        return fail("usage: mate_cli <fixture.json|->");
    }

    std::string text;
    const std::string arg = argv[1];
    if (arg == "-") {
        text = read_all(std::cin);
    } else {
        std::ifstream f(arg, std::ios::binary);
        if (!f) return fail("cannot open input file: " + arg);
        text = read_all(f);
    }

    json input;
    try {
        input = json::parse(text);
    } catch (const std::exception& e) {
        return fail(std::string("JSON parse error: ") + e.what());
    }

    json out;
    try {
        out = aicad::mate::solve_mates_json(input);
    } catch (const std::exception& e) {
        return fail(std::string("solve_mates threw: ") + e.what());
    }

    std::cout << out.dump() << std::endl;
    return 0;
}
