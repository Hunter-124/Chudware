#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <nlohmann/json.hpp>

namespace core { struct DocMirrorSnapshot; }

namespace ui {

enum class NestedDocumentState {
    Loading,
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
    std::string popupId;
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
    // Derived once with the immutable document, not once per visible component instance.
    std::unordered_map<std::string, size_t> featureById;
    std::unordered_map<std::string, std::vector<size_t>> childrenOf;
    std::unordered_set<std::string> nestedSketches;
    std::vector<size_t> references;
    std::unordered_map<std::string, std::string> folderIds;
};

// Canonicalization is memoized by input string (bounded cache shared with tree grouping).
std::string featureTreeCanonicalPath(const std::string& path);

// Stat checks and native-document parsing run on one background worker, at most once a second
// per path. An immutable handle keeps existing rows alive during refresh and cache eviction.
std::shared_ptr<const NestedDocument> featureTreeNestedDocument(const std::string& absPath);
// Prefer the live open document's immutable snapshot to the on-disk archive.
std::shared_ptr<const NestedDocument> featureTreeNestedDocument(
    const std::string& absPath, const core::DocMirrorSnapshot& snapshot);

// Force the next asynchronous refresh after an in-app save, retaining the last complete rows.
// Empty path refreshes all cached documents; obsolete in-flight results are discarded.
void invalidateFeatureTreeNestedDocument(const std::string& absPath = {});

}  // namespace ui
