// core/harness/path_check.h — read-only checks of an authored harness centerline.
#pragma once

#include <nlohmann/json.hpp>

#include "harness/geometry.h"

namespace chudware::harness {

// Units are mm. Errors return {ok:false, code:"bad_args", error:...}; a successful
// check returns ok:true even when violations are found. Isolated turns are
// treated as kinks; sampled-arc detection and circumradii are heuristics, not
// proof of a smooth physical cable.
nlohmann::json harness_path_check(const nlohmann::json& params, const Scene& scene);

}  // namespace chudware::harness
