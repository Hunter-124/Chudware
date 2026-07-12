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

namespace ui {

struct ComponentFeature {
    std::string id;
    std::string name;
    std::string type;
};

// Return the cached feature list for the `.step+` at `absPath`, parsing + caching it on
// the first call (or when the file's mtime changes). `absPath` must already be resolved
// to an absolute path (see resolveComponentPath). Returns an empty vector on any error
// (missing file, not a zip, no document.json, malformed json) — the caller renders no
// nested rows in that case. `outErr` (optional) receives a one-line reason on failure.
const std::vector<ComponentFeature>& componentFeatures(const std::string& absPath,
                                                       std::string* outErr = nullptr);

} // namespace ui
