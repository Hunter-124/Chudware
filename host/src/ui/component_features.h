// Component feature-list cache (assembly tree expandable component nodes).
//
// Reads a component part's OWN feature list (id / name / type) out of its `.step+`
// container's `document.json` so the assembly FeatureManager can show each component's
// nested feature tree read-only. Parsing is done ONCE per (absolute-path, mtime) and
// memoized in a host-side cache — never unzip/parse in the per-frame draw without a
// cache hit, and never via a blocking worker RPC (GL-thread freeze rule).
//
// The reader is a tiny miniz-backed zip extract of a single small entry (document.json,
// a few KB) done on the GL thread at first expand; all state here is GL-thread-only, so
// no locking is required.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui {

struct ComponentFeature {
    std::string id;
    std::string name;
    std::string type;
    // Display-only folder tag (feature objects may carry a "folder" field; empty if absent).
    // Used by the nested renderer to group features into folders exactly as the part env does.
    std::string folder;
    // Full feature JSON object as parsed from the component's document.json "features" array.
    // We store the whole object (rather than a pre-computed consumedSketchIds list) so the
    // assembly nested renderer can run the EXACT same sketch-consumption analysis the part env
    // uses on live features: iterate features with type=="sketch" to collect sketch ids, then
    // recursively scan each feature's raw["refs"] for region selectors {"kind":"region",
    // "sketch":"fX"} and bare sketch-ids under *sketch*/profile keys. Storing raw is future-proof
    // (any new ref shape the part env learns is already carried) and keeps this data layer dumb.
    // A component's feature list is small (a handful of KB of JSON), so the cache cost is trivial.
    nlohmann::json raw;
};

// Return the cached feature list for the `.step+` at `absPath`, parsing + caching it on
// the first call (or when the file's mtime changes). `absPath` must already be resolved
// to an absolute path (see resolveComponentPath). Returns an empty vector on any error
// (missing file, not a zip, no document.json, malformed json) — the caller renders no
// nested rows in that case. `outErr` (optional) receives a one-line reason on failure.
const std::vector<ComponentFeature>& componentFeatures(const std::string& absPath,
                                                       std::string* outErr = nullptr);

} // namespace ui
