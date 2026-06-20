// Simulation-study persistence + mutate RPCs (core/doc/session.cpp doc_sim_study_*).
//
// Studies (FEA/CFD setups) live as a top-level `studies` array in the document JSON
// so they persist inside the .step+ container automatically. The core treats each
// study as OPAQUE JSON — only the `id` is core-assigned — and NEVER rebuilds
// geometry. These assertions exercise the full lifecycle on a BLANK part:
//   add  -> a study_id is minted, the array grows, the revision bumps;
//   edit -> a field change persists and the id is preserved (can't be lost/changed);
//   save + open -> the study survives the .step+ round-trip verbatim;
//   delete -> the array empties.
// doc_new / doc_open compile the (empty) document through OCCT, so this target is
// gated on AICAD_WITH_OCCT exactly like test_document.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/session.h"

using json = nlohmann::json;
namespace doc = aicad::doc;

namespace {

// A representative study payload matching the host's JSON shape. The core never
// interprets these fields; we assert they round-trip verbatim.
json sample_study() {
    return json{
        {"type", "static"},
        {"name", "Static 1"},
        {"material", "steel_1018"},
        {"fixtures", json::array({json{{"selector", json{{"kind", "face"}, {"body", 1}, {"topo", 3}}}}})},
        {"loads", json::array({
            json{{"faces", json::array({json{{"kind", "face"}, {"body", 1}, {"topo", 4}}})},
                 {"type", "force"}, {"vec", json::array({0, 0, -100})}},
            json{{"faces", json::array({json{{"kind", "face"}, {"body", 1}, {"topo", 5}}})},
                 {"type", "pressure"}, {"magnitude", 0.1}},
        })},
        {"mesh_density", 0.5},
        {"n_modes", 6},
        {"flow", json{{"dir", 0}, {"speed", 10.0}, {"preset", 0}}},
        {"result_summary", ""},
    };
}

}  // namespace

TEST_CASE("sim study add/edit/list/save+open/delete round-trip") {
    // --- new blank part ------------------------------------------------------
    json created = doc::doc_new("part", "StudyHost");
    const std::string doc_id = created.value("doc_id", std::string());
    REQUIRE_FALSE(doc_id.empty());

    json empty = doc::doc_sim_study_list(doc_id);
    CHECK(empty["studies"].is_array());
    CHECK(empty["studies"].size() == 0);
    const long rev0 = empty.value("revision", -1L);

    // --- add -----------------------------------------------------------------
    json added = doc::doc_sim_study_add(doc_id, sample_study());
    const std::string study_id = added.value("study_id", std::string());
    CHECK_FALSE(study_id.empty());
    CHECK(study_id.rfind("study-", 0) == 0);
    CHECK(added["studies"].size() == 1);
    CHECK(added.value("revision", -1L) > rev0);  // revision bumped
    CHECK(added["studies"][0].value("id", std::string()) == study_id);
    // Opaque fields preserved verbatim.
    CHECK(added["studies"][0].value("name", std::string()) == "Static 1");
    CHECK(added["studies"][0]["loads"].size() == 2);

    // list reflects the add without bumping the revision.
    json listed = doc::doc_sim_study_list(doc_id);
    CHECK(listed["studies"].size() == 1);
    CHECK(listed.value("revision", -1L) == added.value("revision", -2L));

    // --- edit: change a field, id must survive --------------------------------
    json edited_payload = sample_study();
    edited_payload["name"] = "Static (revised)";
    edited_payload["mesh_density"] = 0.25;
    // Even if the caller fumbles the id, the core forces it back to study_id.
    edited_payload["id"] = "garbage-should-be-overwritten";
    json edited = doc::doc_sim_study_edit(doc_id, study_id, edited_payload);
    CHECK(edited.value("revision", -1L) > added.value("revision", -2L));
    CHECK(edited["studies"].size() == 1);
    CHECK(edited["studies"][0].value("id", std::string()) == study_id);  // id unchanged
    CHECK(edited["studies"][0].value("name", std::string()) == "Static (revised)");
    CHECK(edited["studies"][0].value("mesh_density", 0.0) == doctest::Approx(0.25));

    // editing an unknown study throws invalid_op.
    CHECK_THROWS_AS(doc::doc_sim_study_edit(doc_id, "study-deadbeef", sample_study()),
                    doc::OpError);

    // --- save + open: study must survive the .step+ round-trip ----------------
    namespace fs = std::filesystem;
    const auto stamp = std::chrono::high_resolution_clock::now().time_since_epoch().count();
    fs::path tmp = fs::temp_directory_path() /
                   ("aicad_studies_" + std::to_string(stamp) + ".step+");
    doc::doc_save(doc_id, tmp.string());
    REQUIRE(fs::exists(tmp));

    json opened = doc::doc_open(tmp.string());
    const std::string opened_id = opened.value("doc_id", std::string());
    REQUIRE_FALSE(opened_id.empty());
    json reloaded = doc::doc_sim_study_list(opened_id);
    REQUIRE(reloaded["studies"].size() == 1);
    const json& rs = reloaded["studies"][0];
    CHECK(rs.value("id", std::string()) == study_id);
    CHECK(rs.value("name", std::string()) == "Static (revised)");
    CHECK(rs.value("material", std::string()) == "steel_1018");
    CHECK(rs.value("mesh_density", 0.0) == doctest::Approx(0.25));
    CHECK(rs.value("n_modes", 0) == 6);
    CHECK(rs["loads"].size() == 2);
    CHECK(rs["flow"].value("speed", 0.0) == doctest::Approx(10.0));

    // --- delete: array empties ------------------------------------------------
    json deleted = doc::doc_sim_study_delete(opened_id, study_id);
    CHECK(deleted["studies"].is_array());
    CHECK(deleted["studies"].size() == 0);
    CHECK(deleted.value("revision", -1L) > reloaded.value("revision", -2L));

    // deleting again throws invalid_op.
    CHECK_THROWS_AS(doc::doc_sim_study_delete(opened_id, study_id), doc::OpError);

    // cleanup
    std::error_code ec;
    fs::remove(tmp, ec);
    doc::doc_close(doc_id);
    doc::doc_close(opened_id);
}
