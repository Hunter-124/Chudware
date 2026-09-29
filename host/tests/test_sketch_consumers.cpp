// test_sketch_consumers.cpp — ctest for core::consumedSketches (host/src/core/sketch_consumers.h),
// the one rule the FeatureManager (nest a consumed sketch) and the viewport ghosts (don't draw
// a consumed sketch) share.
//
// Regression under test (live, 2026-09-29): an agent's extrudes referenced their sketches in
// the engine's list form refs.profile = ["f11"]. The ghost pass only knew the region-selector
// form, so both profile sketches kept drawing over the finished keychain — the part looked
// see-through and "not merged".
//
// Acceptance:
//   ✓ refs.profile as a list of sketch ids, as a single id, and as region selectors consume
//   ✓ *_sketch keys, sweep path/guides and loft sections consume
//   ✓ a pattern/mirror "features" list does NOT consume, nor does a plane ref or a non-sketch id
//   ✓ a sketch feature never consumes (sketch-on-sketch references)

#include "core/sketch_consumers.h"

#include <cstdio>
#include <initializer_list>
#include <string>
#include <unordered_set>

using nlohmann::json;

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

static std::unordered_set<std::string> ids(std::initializer_list<const char*> v) {
    std::unordered_set<std::string> out;
    for (const char* s : v) out.insert(s);
    return out;
}

int main() {
    const json features = json::parse(R"([
      {"id":"f11","type":"sketch","params":{"entities":[{}]},"refs":{"plane":"plane.top"}},
      {"id":"f12","type":"extrude","refs":{"profile":["f11"]}},
      {"id":"f14","type":"sketch","params":{"offset":3.5},"refs":{"plane":"plane.top"}},
      {"id":"f15","type":"extrude","refs":{"profile":"f14"}},
      {"id":"f16","type":"sketch","refs":{"plane":"plane.front"}},
      {"id":"f17","type":"revolve","refs":{"profile":[{"kind":"region","sketch":"f16","hint_index":0}]}},
      {"id":"f18","type":"sketch","refs":{"plane":"plane.top"}},
      {"id":"f19","type":"hole","refs":{"face":{"of":"face","side":"top"},"positions_sketch":"f18"}},
      {"id":"f20","type":"sketch","refs":{"plane":"plane.right"}},
      {"id":"f21","type":"sketch","refs":{"plane":"plane.top"}},
      {"id":"f22","type":"sweep","refs":{"profile":["f20"],"path":"f21"}},
      {"id":"f23","type":"loft","refs":{"sections":["f11","f14"]}},
      {"id":"f24","type":"linear_pattern","refs":{"features":["f11","f12"]}},
      {"id":"f25","type":"sketch","refs":{"plane":"plane.top","convert":{"sketch":"f11"}}},
      {"id":"f26","type":"extrude","refs":{"profile":["f12"],"plane":"f14"}}
    ])");
    const auto sketches = core::sketchIdsOf(features);
    CHECK(sketches == ids({"f11", "f14", "f16", "f18", "f20", "f21", "f25"}));

    auto of = [&](const char* fid) {
        for (const auto& f : features)
            if (f.value("id", std::string()) == fid) return core::consumedSketches(f, sketches);
        return std::unordered_set<std::string>{};
    };
    CHECK(of("f12") == ids({"f11"}));             // the keychain regression: profile list
    CHECK(of("f15") == ids({"f14"}));             // profile as a single id
    CHECK(of("f17") == ids({"f16"}));             // region selector
    CHECK(of("f19") == ids({"f18"}));             // positions_sketch
    CHECK(of("f22") == ids({"f20", "f21"}));      // sweep profile + path
    CHECK(of("f23") == ids({"f11", "f14"}));      // loft sections
    CHECK(of("f24").empty());                     // patterns never absorb sketches
    CHECK(of("f25").empty());                     // a sketch is not a consumer
    CHECK(of("f26").empty());                     // non-sketch id in a profile; plane ref

    if (g_failures) {
        std::printf("%d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_sketch_consumers: all passed\n");
    return 0;
}
