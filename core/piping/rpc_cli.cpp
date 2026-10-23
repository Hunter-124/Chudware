// core/piping/rpc_cli.cpp — dev harness driver (mirrors firmware/fw_cli.cpp):
// reads one {"method","params"} JSON object per line on stdin, dispatches to
// the hydraulic then piping cores, and prints the result as one JSON line.
// Used by a parity harness to diff C++ envelopes against the
// Python oracle — not linked into libchudware.
#include <iostream>
#include <string>

#include <nlohmann/json.hpp>

#include "hydraulic/hydraulic.h"
#include "piping/piping.h"

int main() {
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        nlohmann::ordered_json out;
        try {
            const nlohmann::ordered_json req = nlohmann::ordered_json::parse(line);
            const std::string method = req.value("method", std::string());
            const nlohmann::ordered_json params =
                req.contains("params") ? req["params"] : nlohmann::ordered_json::object();
            if (!chudware::hydraulic::call(method, params, out) &&
                !chudware::piping::call(method, params, out)) {
                out = nlohmann::ordered_json::object();
                out["ok"] = false;
                out["reason"] = "unknown method: " + method;
            }
        } catch (const std::exception& e) {
            out = nlohmann::ordered_json::object();
            out["ok"] = false;
            out["reason"] = std::string("rpc_cli exception: ") + e.what();
        }
        std::cout << out.dump() << "\n";
    }
    return 0;
}
