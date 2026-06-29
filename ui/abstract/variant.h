#pragma once
// Lightweight variant map for command dispatch parameters and results.
// JSON-serializable at the services/worker boundary; kept framework-neutral here.

#include <map>
#include <string>
#include <variant>
#include <vector>

namespace aicad::ui {

using Variant = std::variant<std::monostate, bool, int, double, std::string>;
using VariantMap = std::map<std::string, Variant>;

struct ModelDiff {
    bool ok = false;
    std::string error;
    int newRevision = -1;          // -1 = unchanged
    std::string invalidateDocId;   // empty = current doc
    VariantMap payload;            // optional structured result for the UI
};

} // namespace aicad::ui
