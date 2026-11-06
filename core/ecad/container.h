// Read-only legacy .ecad+ ZIP container migration.
#pragma once

#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::ecad {

// The result of opening a .ecad+ container.
struct EcadContainerDoc {
    nlohmann::json doc;                            // the parsed document.json tree
    std::vector<std::string> warnings;             // non-fatal load notes
    std::map<std::string, std::string> extra;      // unknown entries (name -> raw bytes)
};


// Read a .ecad+ container at `path`. Validates the container version (major == 1).
// Throws OpError("io_error", ...) if the file is missing/corrupt or not a .ecad+ .
EcadContainerDoc ecad_container_open(const std::string& path);

}  // namespace chudware::ecad
