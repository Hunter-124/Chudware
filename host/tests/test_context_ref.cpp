// test_context_ref.cpp — ctest for the composer context-picker chip record (core::ContextRef).
// Links the REAL src/core/context_ref.cpp — no GL, ImGui or AppState.
//
// Acceptance:
//   ✓ a chip survives the draft/transcript round trip with its engine selector, owner, component
//     and pick ids; transient resolve state is never persisted
//   ✓ a chip written by ANOTHER process forgets its doc id and pick ids (hostd doc ids are
//     sequential per daemon and get reused) but keeps its path and geometry for rebinding
//   ✓ re-picking an entity detaches it — also after its provisional id became the engine sid
//   ✓ the inline prompt text (offline route / steer) names the sid, owner and geometry,
//     including a straight edge's endpoints

#include "core/context_ref.h"

#include <cstdio>
#include <string>
#include <vector>

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

static bool contains(const std::string& hay, const std::string& needle) {
    return hay.find(needle) != std::string::npos;
}

static core::ContextRef pickedFace() {
    core::ContextRef r;
    r.kind = "face";
    r.id = "Fa1b2c3";
    r.docId = "doc-cpp00000003";
    r.docName = "Bracket";
    r.docPath = "/work/Bracket.step+";
    r.label = "Face of Boss";
    r.owner = "f2";
    r.ownerName = "Boss";
    r.selector = {{"kind", "face"}, {"surf", "plane"}, {"centroid", {0.0, 0.0, 20.0}},
                  {"axis", {0.0, 0.0, 1.0}}, {"normal", {0.0, 0.0, 1.0}}, {"side", "top"},
                  {"area_or_len", 1200.0}, {"owner_feature", "f2"}, {"sid", "Fa1b2c3"}};
    r.bodyId = 1;
    r.topoId = 6;
    r.revision = 4;
    return r;
}

static void test_round_trip_keeps_everything_but_transient_state() {
    core::ContextRef r = pickedFace();
    r.component = "k1";
    r.componentName = "Wheel";
    r.type = "extrude";
    r.resolving = true;
    r.resolveError = "boom";
    core::ContextRef back;
    CHECK(core::contextRefFromJson(core::contextRefToJson(r), back));
    CHECK(back.kind == r.kind && back.id == r.id && back.label == r.label);
    CHECK(back.docId == r.docId && back.docName == r.docName && back.docPath == r.docPath);
    CHECK(back.owner == "f2" && back.ownerName == "Boss" && back.type == "extrude");
    CHECK(back.component == "k1" && back.componentName == "Wheel");
    CHECK(back.selector == r.selector);
    CHECK(back.bodyId == 1 && back.topoId == 6 && back.revision == 4);
    CHECK(!back.resolving && back.resolveError.empty());
    // The wire form exposes the stable sid at top level for the worker.
    CHECK(core::contextRefToJson(r).value("sid", std::string()) == "Fa1b2c3");
}

static void test_foreign_process_entry_forgets_doc_identity() {
    nlohmann::json j = core::contextRefToJson(pickedFace());
    j["epoch"] = "0000000000000000";   // written by an earlier app run
    core::ContextRef back;
    CHECK(core::contextRefFromJson(j, back));
    CHECK(back.docId.empty());
    CHECK(back.bodyId == 0 && back.topoId == 0 && back.revision == -1);
    CHECK(back.docPath == "/work/Bracket.step+");
    CHECK(back.selector.value("sid", std::string()) == "Fa1b2c3");

    // A pre-2026-09-27 draft entry (camelCase docId, no epoch) is foreign too.
    core::ContextRef legacy;
    CHECK(core::contextRefFromJson({{"kind", "feature"}, {"id", "f3"}, {"label", "Fillet1"},
                                    {"docId", "doc-cpp00000001"}}, legacy));
    CHECK(legacy.id == "f3" && legacy.docId.empty());

    std::vector<core::ContextRef> parsed = core::contextRefsFromJson(
        nlohmann::json::array({nlohmann::json::object(), 7, core::contextRefToJson(pickedFace())}));
    CHECK(parsed.size() == 1);
}

static void test_repick_detaches_even_after_resolve() {
    std::vector<core::ContextRef> refs;
    core::ContextRef provisional = pickedFace();
    provisional.id = "face:1:6";
    provisional.selector = nullptr;
    CHECK(core::toggleContextRef(refs, provisional));
    refs[0].id = "Fa1b2c3";   // doc_resolve_pick landed
    CHECK(!core::toggleContextRef(refs, provisional));   // same pick again -> detached
    CHECK(refs.empty());

    // After an edit the same face has new pick ids: a different entity until it resolves.
    CHECK(core::toggleContextRef(refs, pickedFace()));
    core::ContextRef later = pickedFace();
    later.id = "face:1:9";
    later.topoId = 9;
    later.revision = 5;
    CHECK(core::toggleContextRef(refs, later));
    CHECK(refs.size() == 2);

    // Same feature id in another document is another entity.
    core::ContextRef a;
    a.kind = "feature"; a.id = "f2"; a.docId = "doc-a";
    core::ContextRef b = a;
    b.docId = "doc-b";
    std::vector<core::ContextRef> feats;
    CHECK(core::toggleContextRef(feats, a));
    CHECK(core::toggleContextRef(feats, b));
    CHECK(!core::toggleContextRef(feats, a));
    CHECK(feats.size() == 1 && feats[0].docId == "doc-b");
}

static void test_prompt_text_carries_handles_and_geometry() {
    core::ContextRef edge;
    edge.kind = "edge";
    edge.id = "E0f0f0f";
    edge.label = "Edge of Block";
    edge.owner = "f1";
    edge.docId = "doc-cpp00000003";
    edge.selector = {{"kind", "edge"}, {"surf", "line"}, {"centroid", {0.0, -15.0, 20.0}},
                     {"axis", {1.0, 0.0, 0.0}}, {"area_or_len", 40.0}, {"sid", "E0f0f0f"}};
    core::ContextRef hole;
    hole.kind = "edge";
    hole.id = "E123456";
    hole.selector = {{"kind", "edge"}, {"surf", "circle"}, {"centroid", {5.0, 0.0, 20.0}},
                     {"axis", {0.0, 0.0, 1.0}}, {"radius", 4.0},
                     {"area_or_len", 25.132741228718345}, {"sid", "E123456"}};
    const std::string text = core::contextRefsPromptText({pickedFace(), edge, hole});
    CHECK(contains(text, "face Fa1b2c3 \"Face of Boss\""));
    CHECK(contains(text, "owner feature f2 \"Boss\""));
    CHECK(contains(text, "normal [0, 0, 1] (top)"));
    CHECK(contains(text, "area 1200 mm"));
    CHECK(contains(text, "length 40 mm, from [-20, -15, 20] to [20, -15, 20]"));
    CHECK(contains(text, "radius 4 mm (full circle)"));
    CHECK(contains(text, "center [5, 0, 20]"));
    CHECK(core::contextRefsPromptText({}).empty());

    core::ContextRef unresolved;
    unresolved.kind = "face";
    unresolved.id = "face:1:3";
    CHECK(core::contextRefGeometry(unresolved).empty());
}

int main() {
    test_round_trip_keeps_everything_but_transient_state();
    test_foreign_process_entry_forgets_doc_identity();
    test_repick_detaches_even_after_resolve();
    test_prompt_text_carries_handles_and_geometry();
    if (g_failures) {
        std::printf("test_context_ref: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_context_ref: all checks passed\n");
    return 0;
}
