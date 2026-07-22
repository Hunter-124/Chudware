// fw_cli — differential-harness driver for the firmware RPC surface (T-RF15),
// mirroring sketch/sketch_cli.cpp. Reads {"method": ..., "params": {...}} from
// argv[1] (or stdin when "-") and prints the aicad::firmware::call result as
// one JSON line (or {"unhandled": true} when the method falls through, so the
// parity script can assert the Python oracle still owns it).
#include "firmware/firmware_rpc.h"

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include <nlohmann/json.hpp>

int main(int argc, char** argv) {
    std::string input;
    if (argc > 1 && std::string(argv[1]) != "-") {
        input = argv[1];
    } else {
        std::stringstream ss;
        ss << std::cin.rdbuf();
        input = ss.str();
    }
    nlohmann::json req = nlohmann::json::parse(input, nullptr, false);
    if (req.is_discarded() || !req.is_object()) {
        std::cout << R"({"error":"invalid request JSON"})" << std::endl;
        return 1;
    }
    std::string method = req.value("method", std::string());
    nlohmann::json params = req.contains("params") && req["params"].is_object()
                                ? req["params"]
                                : nlohmann::json::object();
    nlohmann::json result;
    std::string err;
    if (!aicad::firmware::call(method, params, result, err)) {
        std::cout << nlohmann::json{{"unhandled", true}, {"method", method}}.dump() << std::endl;
        return 0;
    }
    std::cout << result.dump() << std::endl;
    return 0;
}
