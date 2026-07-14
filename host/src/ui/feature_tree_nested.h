#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui {

enum class NestedDocumentState {
    Ready,
    Missing,
    Unreadable,
    External,
};

struct NestedFeature {
    std::string id;
    std::string name;
    std::string type;
    std::string folder;
    nlohmann::json raw;
};

struct NestedComponent {
    std::string id;
    std::string name;
    std::string sourcePath;
    std::string sourceAbsPath;
    bool fixed = false;
    bool suppressed = false;
    bool hidden = false;
};

struct NestedDocument {
    NestedDocumentState state = NestedDocumentState::Missing;
    std::string path;
    std::string fileName;
    std::string name;
    std::string schema;
    std::string materialKey;
    std::string error;
    std::vector<NestedFeature> features;
    std::vector<NestedComponent> components;
};

// Cached native-document metadata for an assembly component. The cache is keyed by normalized
// absolute path and invalidated by file mtime/size, so expanding a component is allocation-free on
// steady frames while a save from an in-context edit is picked up immediately.
const NestedDocument& featureTreeNestedDocument(const std::string& absPath);

}  // namespace ui
