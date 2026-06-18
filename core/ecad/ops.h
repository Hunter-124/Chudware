// ECAD document ops — validate-then-apply the closed mutation vocabulary.
// C++ port of engine/ecad/ops.py. Pure JSON in/out (no OCCT). Atomic: the whole
// batch applies to a working copy and the RESULTING document must pass
// schema.validate; any op failure OR a validation failure raises OpError and
// leaves the input untouched (mirrors engine.ecad.ops.apply_ops + session).
#pragma once

#include <stdexcept>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad::ecad {

// Structured op error -> the host's {code, message, detail} envelope. Mirrors
// engine.ecad.ops.OpError (same shape as aicad::doc::OpError).
struct OpError : std::runtime_error {
    std::string code;
    nlohmann::json detail;
    OpError(std::string c, const std::string& msg, nlohmann::json d = nlohmann::json::object())
        : std::runtime_error(msg), code(std::move(c)), detail(std::move(d)) {}
    nlohmann::json envelope() const {
        return nlohmann::json{{"code", code}, {"message", what()}, {"detail", detail}};
    }
};

// Validate-then-apply `ops` against `doc`. Returns
// {"id_map":{...}, "warnings":[...], "doc": <mutated working copy>}.
nlohmann::json apply_ops(const nlohmann::json& doc, const nlohmann::json& ops);

}  // namespace aicad::ecad
