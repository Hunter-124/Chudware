// Bounded triangle-mesh harness routing, in world millimetres.
#pragma once

#include <nlohmann/json.hpp>
#include "harness/geometry.h"

namespace chudware::harness {
// start/end contain pos and optional outward dir. Returns sampled path and
// circular-bend metadata, or {ok:false, code, error}; never mutates scene.
nlohmann::json harness_route(const nlohmann::json& params, const Scene& scene);
}
