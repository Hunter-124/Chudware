#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "device/capability.h"
#include "device/device_id.h"

namespace chudware::print {

using nlohmann::json;

struct Vec2 {
    double x = 0;
    double y = 0;
    friend bool operator==(const Vec2&, const Vec2&) = default;
};

struct Vec3 {
    double x = 0;
    double y = 0;
    double z = 0;
    friend bool operator==(const Vec3&, const Vec3&) = default;
};

struct Bounds3 {
    Vec3 min;
    Vec3 max;
    bool valid() const;
    friend bool operator==(const Bounds3&, const Bounds3&) = default;
};

struct Transform {
    Vec3 translation;
    Vec3 rotation_deg;
    Vec3 scale{1, 1, 1};
    friend bool operator==(const Transform&, const Transform&) = default;
};

enum class BuildVolumeShape { Rect, Circular, Polygon };
std::string_view to_string(BuildVolumeShape value);
std::optional<BuildVolumeShape> parse_build_volume_shape(std::string_view text);

struct BuildVolume {
    BuildVolumeShape shape = BuildVolumeShape::Rect;
    double x = 0;
    double y = 0;
    double z = 0;
    bool origin_center = false;
    std::vector<Vec2> polygon;
    std::vector<Bounds3> exclusions;

    bool valid(std::string* why = nullptr) const;
    friend bool operator==(const BuildVolume&, const BuildVolume&) = default;
};

struct ModelArtifactRef {
    std::string sha256;
    Bounds3 model_bounds;
    friend bool operator==(const ModelArtifactRef&, const ModelArtifactRef&) = default;
};

struct ObjectInstance {
    std::string id;
    ModelArtifactRef model;
    Transform transform;
    int extruder = 0;
    json overrides = json::object();
    friend bool operator==(const ObjectInstance&, const ObjectInstance&) = default;
};

struct PlacementDecision {
    bool accepted = false;
    std::string code;
    std::string why;
};

struct Plate {
    std::string id;
    std::string name;
    BuildVolume build_volume;
    bool sequential_print = false;
    uint64_t arrange_seed = 0;
    std::vector<ObjectInstance> instances;

    PlacementDecision place(ObjectInstance instance);
    Bounds3 transformed_bounds(const ObjectInstance& instance) const;
    friend bool operator==(const Plate&, const Plate&) = default;
};

enum class ProfileKind { Printer, Filament, Process };
std::string_view to_string(ProfileKind value);
std::optional<ProfileKind> parse_profile_kind(std::string_view text);

struct ProfileRef {
    ProfileKind kind = ProfileKind::Printer;
    std::string id;
    std::string sha256;
    friend bool operator==(const ProfileRef&, const ProfileRef&) = default;
};

// The slicer owns its preset vocabulary. `fields` is the complete, opaque profile
// bundle; identity and revision are hashed with it so any authored field change
// necessarily creates a different reference.
struct ProfileDefinition {
    ProfileKind kind = ProfileKind::Printer;
    std::string id;
    std::string name;
    uint64_t revision = 0;
    json fields = json::object();

    std::string content_hash() const;
    ProfileRef reference() const;
    friend bool operator==(const ProfileDefinition&, const ProfileDefinition&) = default;
};

struct ObjectSliceStats {
    std::string instance_id;
    uint64_t layer_count = 0;
    double estimated_time_s = 0;
    std::vector<double> filament_mm;
    std::vector<double> filament_g;
    Bounds3 bounds;
    friend bool operator==(const ObjectSliceStats&, const ObjectSliceStats&) = default;
};

struct SliceArtifactRecord {
    std::string artifact_sha256;
    std::string request_id;
    std::string format;
    uint64_t bytes = 0;
    std::vector<ProfileRef> profiles;
    std::vector<ObjectSliceStats> per_object;
    device::Seq seq = 0;  // establishing journal sequence; never locally minted

    static SliceArtifactRecord from_bytes(std::string_view artifact_bytes,
                                          std::string request_id,
                                          std::string format,
                                          std::vector<ProfileRef> profiles,
                                          std::vector<ObjectSliceStats> per_object,
                                          device::Seq journal_seq);
    bool matches_bytes(std::string_view artifact_bytes) const;
    friend bool operator==(const SliceArtifactRecord&, const SliceArtifactRecord&) = default;
};

struct PrintRunLink {
    std::string run_id;
    std::string artifact_sha256;
    std::string request_id;
    std::string project_id;
    uint64_t project_revision = 0;
    std::string plate_id;
    std::string approval_id;
    std::string device_id;
    device::Seq seq = 0;
    friend bool operator==(const PrintRunLink&, const PrintRunLink&) = default;
};

struct PrintProject {
    std::string id;
    std::string name;
    uint64_t revision = 0;
    ProfileRef printer;
    std::vector<ProfileRef> filaments;
    ProfileRef process;
    std::vector<Plate> plates;
    std::vector<SliceArtifactRecord> artifacts;
    std::vector<PrintRunLink> runs;

    PlacementDecision place(std::string_view plate_id, ObjectInstance instance);
    friend bool operator==(const PrintProject&, const PrintProject&) = default;
};

struct ApprovalBinding {
    std::string approval_id;
    std::string approver;
    std::string expires_at;
    std::string exercisable_by = "human_only";
    std::string nonce;
    uint32_t max_starts = 1;
    friend bool operator==(const ApprovalBinding&, const ApprovalBinding&) = default;
};

struct RunHeader {
    std::string run_id;
    std::string artifact_sha256;
    std::string request_id;
    std::string project_id;
    uint64_t project_revision = 0;
    std::string plate_id;
    std::vector<ProfileRef> profiles;
    std::optional<ApprovalBinding> approval;
    friend bool operator==(const RunHeader&, const RunHeader&) = default;
};

class RecordIndex {
public:
    explicit constexpr RecordIndex(uint64_t value = 0) : value_(value) {}
    constexpr uint64_t value() const { return value_; }
    friend bool operator==(RecordIndex, RecordIndex) = default;
private:
    uint64_t value_;
};

enum class MonitoringKind { State, Progress, Temperature, Fault };
std::string_view to_string(MonitoringKind value);
std::optional<MonitoringKind> parse_monitoring_kind(std::string_view text);

struct MonitoringRow {
    RecordIndex record_index;
    device::Seq seq = 0;  // supplied by the gateway journal
    MonitoringKind kind = MonitoringKind::State;
    std::string observed_at;
    json value;
    friend bool operator==(const MonitoringRow&, const MonitoringRow&) = default;
};

struct RunDecision {
    bool accepted = false;
    std::string code;
    std::string why;
};

// Authoring/audit record only. It has no transport dependency and no device
// commands. Approval validation remains the gateway arbiter's responsibility.
class RunRecord {
public:
    static std::pair<std::optional<RunRecord>, RunDecision>
    create(RunHeader header, const SliceArtifactRecord& artifact,
           std::string_view immutable_artifact_bytes);

    const RunHeader& header() const { return header_; }
    const std::vector<MonitoringRow>& monitoring() const { return monitoring_; }
    bool approved() const { return header_.approval.has_value(); }

    RunDecision replace_artifact_before_approval(const SliceArtifactRecord& artifact,
                                                 std::string_view immutable_artifact_bytes);
    RunDecision bind_approval(ApprovalBinding approval);
    RunDecision append_monitoring(device::Seq journal_seq, MonitoringKind kind,
                                  std::string observed_at, json value);

    json to_json() const;
    static RunRecord from_json(const json& value);

private:
    explicit RunRecord(RunHeader header) : header_(std::move(header)) {}
    RunHeader header_;
    std::vector<MonitoringRow> monitoring_;
};

// Complete authoring-only report: physical execution remains unavailable until
// a hardware pilot supplies execute evidence.
device::CapabilityReport printer_authoring_capabilities(
    std::string subject_id, std::vector<std::string> generate_evidence,
    std::vector<std::string> post_evidence);

bool additive_result_kinds_registered();
bool valid_sha256(std::string_view value);
std::string sha256_bytes(std::string_view bytes);

void to_json(json& j, const Vec2& value);
void from_json(const json& j, Vec2& value);
void to_json(json& j, const Vec3& value);
void from_json(const json& j, Vec3& value);
void to_json(json& j, const Bounds3& value);
void from_json(const json& j, Bounds3& value);
void to_json(json& j, const Transform& value);
void from_json(const json& j, Transform& value);
void to_json(json& j, const BuildVolume& value);
void from_json(const json& j, BuildVolume& value);
void to_json(json& j, const ModelArtifactRef& value);
void from_json(const json& j, ModelArtifactRef& value);
void to_json(json& j, const ObjectInstance& value);
void from_json(const json& j, ObjectInstance& value);
void to_json(json& j, const Plate& value);
void from_json(const json& j, Plate& value);
void to_json(json& j, const ProfileRef& value);
void from_json(const json& j, ProfileRef& value);
void to_json(json& j, const ProfileDefinition& value);
void from_json(const json& j, ProfileDefinition& value);
void to_json(json& j, const ObjectSliceStats& value);
void from_json(const json& j, ObjectSliceStats& value);
void to_json(json& j, const SliceArtifactRecord& value);
void from_json(const json& j, SliceArtifactRecord& value);
void to_json(json& j, const PrintRunLink& value);
void from_json(const json& j, PrintRunLink& value);
void to_json(json& j, const PrintProject& value);
void from_json(const json& j, PrintProject& value);
void to_json(json& j, const ApprovalBinding& value);
void from_json(const json& j, ApprovalBinding& value);
void to_json(json& j, const RunHeader& value);
void from_json(const json& j, RunHeader& value);
void to_json(json& j, const MonitoringRow& value);
void from_json(const json& j, MonitoringRow& value);

}  // namespace chudware::print
