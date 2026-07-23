// struct_cli: differential-harness driver (same role as fw_cli / sketch_cli) —
// reads one {"method","params"} JSON document on stdin and prints the
// aicad::structural::call result as one JSON line. Used by the T-RF19 parity
// diffs against the Python oracle.

#include <iostream>
#include <sstream>
#include <string>

#include "structural/structural_rpc.h"

int main() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    nlohmann::json req = nlohmann::json::parse(ss.str(), nullptr, false);
    if (req.is_discarded() || !req.contains("method")) {
        std::cout << R"({"code":"bad_request","message":"expected {\"method\",\"params\"}","detail":""})"
                  << std::endl;
        return 1;
    }
    nlohmann::json result;
    std::string err;
    const bool handled = aicad::structural::call(
        req["method"].get<std::string>(),
        req.contains("params") ? req["params"] : nlohmann::json::object(), result, err);
    if (!handled) {
        std::cout << R"({"code":"unknown_method","message":"not a ported struct_* method","detail":""})"
                  << std::endl;
        return 1;
    }
    std::cout << result.dump() << std::endl;
    return 0;
}
