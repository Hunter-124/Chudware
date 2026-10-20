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
// gated on CHUDWARE_WITH_OCCT exactly like test_document.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <cstdlib>
#include <cstdio>
#include <filesystem>
#include <string>
#include <set>

#include <nlohmann/json.hpp>

#include "doc/session.h"
#include "doc/stepplus.h"

using json = nlohmann::json;
namespace doc = chudware::doc;

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

std::filesystem::path temporary_study_path(const std::string& label) {
    const auto stamp = std::chrono::high_resolution_clock::now().time_since_epoch().count();
    return std::filesystem::temp_directory_path() /
           ("chudware_studies_" + label + "_" + std::to_string(stamp) + ".step+");
}

json summary(const std::string& doc_id) {
    const json listed = doc::doc_list();
    for (const json& row : listed.at("docs"))
        if (row.value("doc_id", std::string()) == doc_id) return row;
    return json::object();
}

std::string study_token(unsigned long long n) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "study-%08llx", n);
    return std::string(buf);
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
                   ("chudware_studies_" + std::to_string(stamp) + ".step+");
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

TEST_CASE("loaded study id collisions never create new duplicates or edit another analysis") {
    const std::string seed = doc::doc_new("part", "LegacyStudyIds").at("doc_id");
    const std::string probe = doc::doc_sim_study_add(seed, sample_study()).at("study_id");
    const unsigned long long next = std::stoull(probe.substr(6), nullptr, 16) + 1;
    const std::string collision = study_token(next);
    const std::string occupied = study_token(next + 1);
    json tree = doc::doc_tree(seed).at("tree");
    tree["studies"] = json::array({
        {{"id", collision}, {"type", "fea_static"}, {"name", "Static"}},
        {{"id", collision}, {"type", "motion"}, {"name", "Motion: m75"}, {"stop", 10}},
        {{"id", collision}, {"type", "motion"}, {"name", "Motion: m84"}, {"stop", 20}},
        {{"id", collision}, {"type", "motion"}, {"name", "Motion: m75"}, {"stop", 15}},
        {{"id", occupied}, {"type", "fea_modal"}, {"name", "Modal"}},
    });
    const auto path = temporary_study_path("legacy_ids");
    doc::stepplus_save(tree, path.string(), {});
    doc::doc_close(seed);
    const std::string id = doc::doc_open(path.string(), false, true).at("doc_id");

    const json added = doc::doc_sim_study_add(id, sample_study());
    const std::string added_id = added.at("study_id");
    CHECK(added_id != collision);
    CHECK(added_id != occupied);
    const json explicit_added = doc::doc_sim_study_add(id, {
        {"id", collision}, {"type", "motion"}, {"name", "Motion: m66"}});
    const std::string explicit_id = explicit_added.at("study_id");
    CHECK(explicit_id != collision);
    CHECK(explicit_id != occupied);
    CHECK(explicit_id != added_id);

    const json updated = doc::doc_sim_study_edit(id, collision, {
        {"type", "motion"}, {"name", "Motion: m75"}, {"stop", 80}});
    const std::string updated_id = updated.at("study_id");
    CHECK(updated_id != collision);
    CHECK(updated["studies"][0] == tree["studies"][0]);
    CHECK(updated["studies"][2] == tree["studies"][2]);
    CHECK(updated["studies"][3] == tree["studies"][3]);
    CHECK(updated["studies"][1]["stop"] == 80);
    CHECK(updated["studies"][1]["id"] == updated_id);
    const std::set<std::string> new_ids = {added_id, explicit_id, updated_id, occupied};
    CHECK(new_ids.size() == 4);

    const json repeated = doc::doc_sim_study_edit(id, updated_id, {
        {"type", "motion"}, {"name", "Renamed motion"}, {"stop", 40}});
    CHECK(repeated.at("study_id") == updated_id);
    CHECK(repeated["studies"][1]["stop"] == 40);
    CHECK(repeated["studies"].size() == 7);
    const json before_refusal = doc::doc_sim_study_list(id);
    CHECK_THROWS_AS(doc::doc_sim_study_edit(id, collision, {
        {"type", "motion"}, {"name", "Missing study"}}), doc::OpError);
    CHECK(doc::doc_sim_study_list(id) == before_refusal);

    doc::doc_save(id, path.string());
    doc::doc_close(id);
    const std::string reopened = doc::doc_open(path.string()).at("doc_id");
    CHECK(doc::doc_sim_study_list(reopened)["studies"] == repeated["studies"]);
    doc::doc_close(reopened);
    std::error_code ec;
    std::filesystem::remove(path, ec);
}

TEST_CASE("detached native source authority excludes study revisions but includes authored edits") {
    const auto source = temporary_study_path("source_authority");
    const auto destination = temporary_study_path("explicit_save");
    const std::string seed = doc::doc_new("part", "SourceAuthority").at("doc_id");
    doc::doc_save(seed, source.string());
    doc::doc_close(seed);
    const std::string id = doc::doc_open(source.string(), false, true).at("doc_id");
    CHECK(summary(id)["native_source_edited"] == false);
    CHECK(summary(id)["path"].is_null());
    const std::string sid = doc::doc_sim_study_add(id, sample_study()).at("study_id");
    CHECK(summary(id)["native_source_edited"] == false);
    doc::doc_sim_study_edit(id, sid, sample_study());
    CHECK(summary(id)["native_source_edited"] == false);
    doc::doc_sim_study_delete(id, sid);
    CHECK(summary(id)["native_source_edited"] == false);

    const auto dim = [](double value) { return json{{"value", value}, {"expr", nullptr}}; };
    const json authored_feature = {
        {"type", "box"}, {"name", "Authored block"},
        {"params", {{"length", dim(10)}, {"width", dim(10)}, {"height", dim(10)}}},
    };
    doc::doc_apply_ops(id, -1, json::array({
        json{{"op", "add_feature"}, {"feature", authored_feature}},
        json{{"op", "set_material"}, {"key", "pla"}}}), false);
    CHECK(summary(id)["native_source_edited"] == true);
    const std::string after = doc::doc_sim_study_add(id, sample_study()).at("study_id");
    doc::doc_sim_study_edit(id, after, sample_study());
    CHECK(summary(id)["native_source_edited"] == true);
    doc::doc_save(id, destination.string());
    CHECK(summary(id)["path"] == destination.string());
    doc::doc_close(id);

    const std::string reopened = doc::doc_open(destination.string()).at("doc_id");
    CHECK(summary(reopened)["native_source_path"].is_null());
    CHECK(summary(reopened)["native_source_edited"] == false);
    CHECK(summary(reopened)["path"] == destination.string());
    CHECK(doc::doc_sim_study_list(reopened)["studies"].size() == 1);
    doc::doc_close(reopened);
    std::error_code ec;
    std::filesystem::remove(source, ec);
    std::filesystem::remove(destination, ec);
}

TEST_CASE("explicit history operations are not mistaken for scratch study revisions") {
    const auto source = temporary_study_path("history_authority");
    const std::string seed = doc::doc_new("part", "HistoryAuthority").at("doc_id");
    doc::doc_save(seed, source.string());
    doc::doc_close(seed);
    const std::string id = doc::doc_open(source.string(), false, true).at("doc_id");
    const json checkpoint = doc::doc_checkpoint(id);
    doc::doc_sim_study_add(id, sample_study());
    CHECK(summary(id)["native_source_edited"] == false);
    SUBCASE("undo advances non-study authoring history") {
        doc::doc_undo(id);
    }
    SUBCASE("revert advances non-study authoring history") {
        doc::doc_revert(id, checkpoint.at("checkpoint"));
    }
    CHECK(summary(id)["native_source_edited"] == true);
    doc::doc_close(id);
    std::error_code ec;
    std::filesystem::remove(source, ec);
}
