// sketch_consumers.h — which sketches a document's features consume (header-only; no GL /
// imgui / AppState). Shared by the FeatureManager tree (a consumed sketch nests under its
// feature) and the viewport's ghost sketches (a consumed sketch is not drawn), so both agree.
//
// Regression (live, 2026-09-29): an agent built a keychain as sketch f11 -> extrude f12 and
// sketch f14 -> extrude f15, writing the engine's list form refs.profile = ["f11"]. The ghost
// pass only recognised a region selector {"kind":"region","sketch":"f11"}, so both profiles
// stayed drawn over the finished solid — an overlay without depth, so the part read as
// see-through and "not merged".
#pragma once

#include <string>
#include <unordered_set>

#include <nlohmann/json.hpp>

namespace core {

namespace detail {

// A string under one of these keys names a sketch the feature builds FROM (profile, sweep
// path/guides, loft sections). Any key containing "sketch" (positions_sketch, sketch) too.
inline bool keyConsumesSketch(const std::string& key) {
    return key == "profile" || key == "profiles" || key == "sections" || key == "path" ||
           key == "guides" || key == "guide" || key.find("sketch") != std::string::npos;
}

inline void scanSketchRefs(const nlohmann::json& node, const std::string& key,
                           const std::unordered_set<std::string>& sketchIds,
                           std::unordered_set<std::string>& out) {
    if (node.is_object()) {
        auto kind = node.find("kind");
        auto sk = node.find("sketch");
        if (kind != node.end() && kind->is_string() && kind->get<std::string>() == "region" &&
            sk != node.end() && sk->is_string() && sketchIds.count(sk->get<std::string>()))
            out.insert(sk->get<std::string>());
        for (auto it = node.begin(); it != node.end(); ++it)
            scanSketchRefs(it.value(), it.key(), sketchIds, out);
    } else if (node.is_array()) {
        // A list inherits its key: refs.profile = ["f11", "f12"].
        for (const auto& e : node) scanSketchRefs(e, key, sketchIds, out);
    } else if (node.is_string()) {
        const std::string v = node.get<std::string>();
        if (keyConsumesSketch(key) && sketchIds.count(v)) out.insert(v);
    }
}

}  // namespace detail

// The sketches ONE feature's refs consume. A bare sketch id in a generic list such as a
// pattern/mirror "features" list is deliberately NOT consumption: patterns do not absorb
// sketches, and hiding them there would lose the user's geometry.
inline std::unordered_set<std::string> consumedSketches(
    const nlohmann::json& feature, const std::unordered_set<std::string>& sketchIds) {
    std::unordered_set<std::string> out;
    if (!feature.is_object()) return out;
    auto type = feature.find("type");
    if (type != feature.end() && type->is_string() && type->get<std::string>() == "sketch")
        return out;  // a sketch is never a consumer
    auto refs = feature.find("refs");
    if (refs != feature.end()) detail::scanSketchRefs(*refs, std::string(), sketchIds, out);
    return out;
}

// Ids of every sketch feature in a document tree's "features" array.
inline std::unordered_set<std::string> sketchIdsOf(const nlohmann::json& features) {
    std::unordered_set<std::string> ids;
    if (!features.is_array()) return ids;
    for (const auto& f : features) {
        if (!f.is_object()) continue;
        auto type = f.find("type");
        auto id = f.find("id");
        if (type != f.end() && type->is_string() && type->get<std::string>() == "sketch" &&
            id != f.end() && id->is_string())
            ids.insert(id->get<std::string>());
    }
    return ids;
}

}  // namespace core
