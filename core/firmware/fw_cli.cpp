// fw_cli — standalone debug driver for the firmware RPC surface,
// mirroring sketch/sketch_cli.cpp. Takes the request JSON text
// {"method": ..., "params": {...}} as argv[1] (or reads it from stdin when "-" or
// absent) and prints the chudware::firmware::call result as one JSON line, or
// {"unhandled": true, "method": ...} when the method is not part of that surface.
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
    if (!chudware::firmware::call(method, params, result, err)) {
        std::cout << nlohmann::json{{"unhandled", true}, {"method", method}}.dump() << std::endl;
        return 0;
    }
    std::cout << result.dump() << std::endl;
    return 0;
}
