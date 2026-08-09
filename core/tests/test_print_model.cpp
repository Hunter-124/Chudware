#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <concepts>
#include <string>
#include <tuple>
#include <type_traits>

#include <nlohmann/json.hpp>

#include "print/model.h"

namespace {

using namespace chudware::print;

const std::string kModelHash(64, 'a');

ObjectInstance cube(std::string id, Vec3 translation = {}) {
    return {std::move(id), {kModelHash, {{0, 0, 0}, {20, 20, 20}}},
            {translation, {}, {1, 1, 1}}, 0, nlohmann::json::object()};
}

ProfileDefinition profile(ProfileKind kind, std::string id) {
    return {kind, std::move(id), "Owned profile", 7,
            {{"temperature_c", 215}, {"nested", {{"speed_mm_s", 50}, {"fan_pct", 40}}}}};
}

SliceArtifactRecord artifact(std::string_view bytes = "G1 X1\n", chudware::device::Seq seq = 41) {
    return SliceArtifactRecord::from_bytes(
        bytes, "request-1", "gcode",
        {profile(ProfileKind::Printer, "printer-a").reference(),
         profile(ProfileKind::Filament, "pla-a").reference(),
         profile(ProfileKind::Process, "quality-a").reference()},
        {{"cube-a", 100, 3600, {1200}, {3.5}, {{0, 0, 0}, {20, 20, 20}}}}, seq);
}

template <typename T>
concept OpensTransport = requires(T value) { value.open_transport(); };

template <typename T>
concept UploadsArtifact = requires(T value) { value.upload(); };

template <typename T>
concept StartsPrint = requires(T value) { value.start(); };

template <typename T>
concept PausesPrint = requires(T value) { value.pause(); };

template <typename T>
concept CancelsPrint = requires(T value) { value.cancel(); };

template <typename T>
constexpr bool authoring_only_surface = !OpensTransport<T> && !UploadsArtifact<T> &&
    !StartsPrint<T> && !PausesPrint<T> && !CancelsPrint<T>;

using AdditivePublicTypes = std::tuple<PrintProject, Plate, SliceArtifactRecord, RunRecord>;

static_assert(authoring_only_surface<PrintProject>);
static_assert(authoring_only_surface<Plate>);
static_assert(authoring_only_surface<SliceArtifactRecord>);
static_assert(authoring_only_surface<RunRecord>);
static_assert(!std::convertible_to<RecordIndex, chudware::device::Seq>);
static_assert(std::tuple_size_v<AdditivePublicTypes> == 4);

}  // namespace

TEST_CASE("plate placement rejects outside volume, exclusions, and object overlap") {
    Plate plate{"plate-a", "Build plate",
                {BuildVolumeShape::Rect, 100, 100, 100, false, {},
                 {{{70, 70, 0}, {90, 90, 30}}}},
                false, 123, {}};

    CHECK(plate.place(cube("inside", {10, 10, 0})).accepted);

    const auto outside = plate.place(cube("outside", {90, 10, 0}));
    CHECK_FALSE(outside.accepted);
    CHECK(outside.code == "outside_build_volume");

    const auto overlap = plate.place(cube("overlap", {15, 15, 0}));
    CHECK_FALSE(overlap.accepted);
    CHECK(overlap.code == "instance_overlap");

    const auto exclusion = plate.place(cube("excluded", {72, 72, 0}));
    CHECK_FALSE(exclusion.accepted);
    CHECK(exclusion.code == "plate_exclusion_collision");

    CHECK(plate.instances.size() == 1);
}

TEST_CASE("transforms participate in build-volume placement") {
    Plate plate{"round", "Round plate",
                {BuildVolumeShape::Circular, 100, 100, 100, true, {}, {}},
                false, 0, {}};
    auto rotated = cube("rotated", {-5, -5, 0});
    rotated.transform.rotation_deg.z = 45;
    CHECK(plate.place(rotated).accepted);

    auto scaled = cube("scaled", {20, 20, 0});
    scaled.transform.scale = {2, 2, 1};
    CHECK_FALSE(plate.place(scaled).accepted);
}

TEST_CASE("profile hash changes on every authored field and ignores key order") {
    const auto original = profile(ProfileKind::Printer, "printer-a");
    const auto hash = original.content_hash();
    CHECK(chudware::print::valid_sha256(hash));
    CHECK(original.reference().id == "printer-a");
    CHECK(original.reference().sha256 == hash);

    auto changed = original;
    changed.kind = ProfileKind::Process;
    CHECK(changed.content_hash() != hash);
    changed = original;
    changed.id = "printer-b";
    CHECK(changed.content_hash() != hash);
    changed = original;
    changed.name = "Renamed";
    CHECK(changed.content_hash() != hash);
    changed = original;
    ++changed.revision;
    CHECK(changed.content_hash() != hash);
    changed = original;
    changed.fields["temperature_c"] = 216;
    CHECK(changed.content_hash() != hash);
    changed = original;
    changed.fields["nested"]["fan_pct"] = 41;
    CHECK(changed.content_hash() != hash);

    auto reordered = original;
    reordered.fields = {{"nested", {{"fan_pct", 40}, {"speed_mm_s", 50}}},
                        {"temperature_c", 215}};
    CHECK(reordered.content_hash() == hash);
}

TEST_CASE("slice artifacts bind exact bytes and retain per-object statistics") {
    const auto value = artifact();
    CHECK(value.matches_bytes("G1 X1\n"));
    CHECK_FALSE(value.matches_bytes("G1 X2\n"));
    CHECK(value.artifact_sha256 == chudware::print::sha256_bytes("G1 X1\n"));
    REQUIRE(value.per_object.size() == 1);
    CHECK(value.per_object[0].instance_id == "cube-a");
    CHECK(value.per_object[0].layer_count == 100);
    CHECK(value.per_object[0].filament_g == std::vector<double>{3.5});
    CHECK(nlohmann::json(value).get<SliceArtifactRecord>() == value);
    CHECK(chudware::print::additive_result_kinds_registered());
}

TEST_CASE("artifact mismatch is refused before immutable run binding") {
    const auto value = artifact();
    RunHeader header{"run:print-1", value.artifact_sha256, value.request_id,
                     "project-a", 12, "plate-a", value.profiles, std::nullopt};

    auto [mismatch, refusal] = RunRecord::create(header, value, "modified bytes");
    CHECK_FALSE(mismatch.has_value());
    CHECK_FALSE(refusal.accepted);
    CHECK(refusal.code == "artifact_hash_mismatch");

    header.artifact_sha256 = std::string(64, 'b');
    auto [wrong_binding, binding_refusal] = RunRecord::create(header, value, "G1 X1\n");
    CHECK_FALSE(wrong_binding.has_value());
    CHECK(binding_refusal.code == "artifact_binding_mismatch");
}

TEST_CASE("approval freezes the run artifact binding") {
    const auto first = artifact("G1 X1\n", 41);
    auto second = SliceArtifactRecord::from_bytes("G1 X2\n", "request-2", "gcode",
                                                  first.profiles, first.per_object, 42);
    RunHeader header{"run:print-2", first.artifact_sha256, first.request_id,
                     "project-a", 12, "plate-a", first.profiles, std::nullopt};
    auto [record, decision] = RunRecord::create(header, first, "G1 X1\n");
    REQUIRE(decision.accepted);
    REQUIRE(record.has_value());

    CHECK(record->replace_artifact_before_approval(second, "G1 X2\n").accepted);
    const auto frozen_hash = record->header().artifact_sha256;
    const auto frozen_request = record->header().request_id;
    CHECK(record->bind_approval({"appr:1", "operator@example.test", "2026-08-09T00:00:00Z",
                                 "human_only", "single-use-nonce", 1}).accepted);

    const auto refused = record->replace_artifact_before_approval(first, "G1 X1\n");
    CHECK_FALSE(refused.accepted);
    CHECK(refused.code == "run_binding_immutable");
    CHECK(record->header().artifact_sha256 == frozen_hash);
    CHECK(record->header().request_id == frozen_request);
    CHECK_FALSE(record->bind_approval({"appr:2", "other", "later", "human_only", "n2", 1}).accepted);
}

TEST_CASE("record_index is a file ordinal and never substitutes for journal seq") {
    const auto value = artifact();
    RunHeader header{"run:print-3", value.artifact_sha256, value.request_id,
                     "project-a", 12, "plate-a", value.profiles, std::nullopt};
    auto [record, decision] = RunRecord::create(header, value, "G1 X1\n");
    REQUIRE(decision.accepted);
    REQUIRE(record.has_value());

    CHECK_FALSE(record->append_monitoring(0, MonitoringKind::Progress, "t0", 0.1).accepted);
    CHECK(record->append_monitoring(900, MonitoringKind::State, "t1", "printing").accepted);
    CHECK(record->append_monitoring(904, MonitoringKind::Temperature, "t2",
                                    {{"nozzle_c", 214.8}, {"bed_c", 59.9}}).accepted);
    REQUIRE(record->monitoring().size() == 2);
    CHECK(record->monitoring()[0].record_index.value() == 0);
    CHECK(record->monitoring()[0].seq == 900);
    CHECK(record->monitoring()[1].record_index.value() == 1);
    CHECK(record->monitoring()[1].seq == 904);

    const nlohmann::json encoded = record->to_json();
    CHECK(encoded["monitoring"][0]["record_index"] == 0);
    CHECK(encoded["monitoring"][0]["seq"] == 900);
    const auto decoded = RunRecord::from_json(encoded);
    CHECK(decoded.header() == record->header());
    CHECK(decoded.monitoring() == record->monitoring());

    auto reordered = encoded;
    reordered["monitoring"][1]["record_index"] = 3;
    CHECK_THROWS_AS(RunRecord::from_json(reordered), std::invalid_argument);
}

TEST_CASE("print project round trips as a sibling domain document") {
    const auto printer = profile(ProfileKind::Printer, "printer-a").reference();
    const auto filament = profile(ProfileKind::Filament, "pla-a").reference();
    const auto process = profile(ProfileKind::Process, "quality-a").reference();
    PrintProject project{"project-a", "Bracket plate", 0, printer, {filament}, process,
                         {{"plate-a", "Plate", {BuildVolumeShape::Rect, 200, 200, 200,
                                                  false, {}, {}}, false, 5, {}}}, {}, {}};
    CHECK(project.place("plate-a", cube("cube-a", {10, 10, 0})).accepted);
    CHECK(project.revision == 1);
    project.artifacts.push_back(artifact());
    project.runs.push_back({"run:print-4", project.artifacts[0].artifact_sha256, "request-1",
                            project.id, project.revision, "plate-a", "appr:4",
                            "dev:fdm_printer_open:0123456789ab", 77});

    const nlohmann::json encoded = project;
    CHECK(encoded.at("schema") == "chudware.print_project/1");
    CHECK(encoded.at("units") == "mm");
    CHECK(encoded.get<PrintProject>() == project);
}

TEST_CASE("printer capabilities stop at post and physical execute remains assumed") {
    using namespace chudware::device;
    const auto report = printer_authoring_capabilities(
        "printer-profile-a", {"res:print.procedural_fact:GENERATE"},
        {"res:print.procedural_fact:POST"});
    CHECK(report.complete());
    CHECK(report.max_advertised == CapabilityLevel::Post);
    CHECK(report.levels.at(CapabilityLevel::Generate).truth == CapabilityTruthValue::Verified);
    CHECK(report.levels.at(CapabilityLevel::Post).truth == CapabilityTruthValue::Verified);
    const auto& execute = report.levels.at(CapabilityLevel::Execute);
    CHECK(execute.truth == CapabilityTruthValue::Assumed);
    CHECK(execute.missing_level == CapabilityLevel::Execute);
    CHECK_FALSE(execute.enables_action());
    CHECK(report.levels.at(CapabilityLevel::SafetyIntegration).truth ==
          CapabilityTruthValue::Unsupported);
}

TEST_CASE("additive public model has no transport or command entry point") {
    // Policy boundary: this module is authoring/audit-only. A future hardware lane
    // must live behind the gateway rather than adding any of these methods here.
    CHECK(authoring_only_surface<PrintProject>);
    CHECK(authoring_only_surface<Plate>);
    CHECK(authoring_only_surface<SliceArtifactRecord>);
    CHECK(authoring_only_surface<RunRecord>);
}
