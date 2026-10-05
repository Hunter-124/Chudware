// Standalone structural RPC debug CLI: read one {"method","params"} JSON
// request from stdin and print chudware::structural::call's result as one line.

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
    const bool handled = chudware::structural::call(
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
