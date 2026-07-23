// sketch_cli — tiny CLI wrapper around chudware::sketch::solve_sketch for the
// differential-test harness (tools/solver_diff.py).
//
// Usage:
//   sketch_cli <fixture.json>     read the fixture from a file
//   sketch_cli -                  read the fixture from stdin
//
// Input  : a JSON object { "sketch": {...}, "env": {...} }  (env optional).
//          Any extra fields the harness fixtures carry ("name", "expect", ...)
//          are ignored.
// Output : a JSON object printed to stdout, one line, full float precision:
//   {
//     "converged":   bool,
//     "dof":         int,
//     "redundant":   [constraint id, ...],
//     "conflicting": [constraint id, ...],
//     "statuses":    { entity id: "under"|"full"|"over", ... },
//     "entities":    { entity id: <solved param dict>, ... }
//   }
//
// Float formatting is deterministic and lossless: nlohmann::json serializes
// doubles round-trip (shortest exact representation), so the Python side parses
// back the same bit pattern the solver produced. Exit code 0 on success, 2 on a
// usage/IO/parse error.

#include "sketch/sketch.h"

#include <nlohmann/json.hpp>

#include <fstream>
#include <iostream>
#include <iterator>
#include <optional>
#include <sstream>
#include <string>

using json = nlohmann::json;

namespace {

int fail(const std::string& msg) {
    std::cerr << "sketch_cli: " << msg << "\n";
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
        return fail("usage: sketch_cli <fixture.json|->");
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

    // Measure mode (tools/measure_diff.py): input
    //   {"mode":"measure","constraint":{...},"entity_coords":{...}}
    // -> {"value": <number|null>}. The default (absent/"solve") mode is unchanged.
    if (input.value("mode", std::string("solve")) == "measure") {
        const json constraint = input.value("constraint", json::object());
        const json entity_coords = input.value("entity_coords", json::object());
        std::optional<double> v;
        try {
            v = chudware::sketch::measure_constraint(constraint, entity_coords);
        } catch (const std::exception& e) {
            return fail(std::string("measure_constraint threw: ") + e.what());
        }
        json out;
        out["value"] = v ? json(*v) : json(nullptr);
        std::cout << out.dump() << std::endl;
        return 0;
    }

    // Warm-start mode (tools/warm_diff.py): input
    //   {"mode":"warm","sketch":{...},"after":{...optional},"env":{...}}
    // cold-solves `sketch` to build a state, then warm-solves `after` (defaults to
    // `sketch`) from that state. Output:
    //   {"warm":{entities,converged}, "cold":{entities,converged},
    //    "decision":{entity id -> "warm"|"cold"}}
    if (input.value("mode", std::string("solve")) == "warm") {
        const json env = input.value("env", json::object());
        const json base = input.value("sketch", json::object());
        const json after = input.contains("after") ? input["after"] : base;
        try {
            // 1) cold-solve the base sketch (no warm).
            auto cold0 = chudware::sketch::solve_sketch(base, env);
            // 2) cold-solve `after` standalone (the reference answer).
            auto cold_after = chudware::sketch::solve_sketch(after, env);
            // 3) warm-solve `after` from the base state.
            auto warm = chudware::sketch::solve_sketch(after, env, cold0.state);
            auto decision = chudware::sketch::warm_gate_decision(after, cold0.state);

            json out;
            out["warm"] = {{"entities", warm.entities}, {"converged", warm.converged}};
            out["cold"] = {{"entities", cold_after.entities},
                           {"converged", cold_after.converged}};
            json dec = json::object();
            for (const auto& kv : decision) dec[kv.first] = kv.second;
            out["decision"] = dec;
            std::cout << out.dump() << std::endl;
            return 0;
        } catch (const std::exception& e) {
            return fail(std::string("warm solve threw: ") + e.what());
        }
    }

    const json sketch = input.value("sketch", json::object());
    const json env = input.value("env", json::object());

    chudware::sketch::SketchSolution sol;
    try {
        sol = chudware::sketch::solve_sketch(sketch, env);
    } catch (const std::exception& e) {
        return fail(std::string("solve_sketch threw: ") + e.what());
    }

    // statuses is a std::map<string,string> -> ordered JSON object.
    json statuses = json::object();
    for (const auto& kv : sol.statuses) statuses[kv.first] = kv.second;

    json out;
    out["converged"] = sol.converged;
    out["dof"] = sol.dof;
    out["redundant"] = sol.redundant;
    out["conflicting"] = sol.conflicting;
    out["statuses"] = statuses;
    out["entities"] = sol.entities;

    // dump() emits doubles at shortest round-trip precision — deterministic and
    // lossless across the Python boundary.
    std::cout << out.dump() << std::endl;
    return 0;
}
