#pragma once
// Per-document parked host state (selection + visibility) — no UI subsystem types.

#include <cstdint>
#include <string>
#include <unordered_set>
#include <vector>

#include "pick_hit.h"
#include "selection.h"

namespace core {

struct DocSessionState {
    std::vector<SelItem> selItems;
    worker::PickHit hover;
    std::string pickStatus;
    std::vector<uint8_t> bodyVisible;
    std::vector<uint32_t> docBodyIds;
    std::unordered_set<std::string> hiddenFeatures;
    std::string selectedFeatureId;
    bool treeScrollToSelection = false;
    std::vector<uint64_t> featureHighlightFaces;
    bool sketchPlanePickActive = false;
    std::string cachedAicmPath;
    int cachedRevision = -1;
};

}  // namespace core
