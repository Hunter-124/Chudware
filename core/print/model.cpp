#include "print/model.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>
#include <utility>

#include "manufacturing/result_registry.h"
#include "util/sha256.h"

namespace chudware::print {
namespace {

constexpr double kEpsilon = 1e-9;
constexpr double kPi = 3.14159265358979323846;

bool fail(std::string* why, std::string message) {
    if (why) *why = std::move(message);
    return false;
}

Vec3 rotate_xyz(Vec3 point, const Vec3& degrees) {
    const double rx = degrees.x * kPi / 180.0;
    const double ry = degrees.y * kPi / 180.0;
    const double rz = degrees.z * kPi / 180.0;
    const double cx = std::cos(rx), sx = std::sin(rx);
    const double cy = std::cos(ry), sy = std::sin(ry);
    const double cz = std::cos(rz), sz = std::sin(rz);

    point = {point.x, point.y * cx - point.z * sx, point.y * sx + point.z * cx};
    point = {point.x * cy + point.z * sy, point.y, -point.x * sy + point.z * cy};
    return {point.x * cz - point.y * sz, point.x * sz + point.y * cz, point.z};
}

bool positive_overlap(const Bounds3& a, const Bounds3& b) {
    return a.min.x < b.max.x - kEpsilon && a.max.x > b.min.x + kEpsilon &&
           a.min.y < b.max.y - kEpsilon && a.max.y > b.min.y + kEpsilon &&
           a.min.z < b.max.z - kEpsilon && a.max.z > b.min.z + kEpsilon;
}

bool point_in_polygon(const std::vector<Vec2>& polygon, Vec2 point) {
    bool inside = false;
    for (size_t i = 0, j = polygon.size() - 1; i < polygon.size(); j = i++) {
        const Vec2& a = polygon[i];
        const Vec2& b = polygon[j];
        const double cross = (point.x - a.x) * (b.y - a.y) -
                             (point.y - a.y) * (b.x - a.x);
        if (std::abs(cross) <= kEpsilon &&
            point.x >= std::min(a.x, b.x) - kEpsilon &&
            point.x <= std::max(a.x, b.x) + kEpsilon &&
            point.y >= std::min(a.y, b.y) - kEpsilon &&
            point.y <= std::max(a.y, b.y) + kEpsilon) return true;
        const bool crosses = ((a.y > point.y) != (b.y > point.y)) &&
            (point.x < (b.x - a.x) * (point.y - a.y) / (b.y - a.y) + a.x);
        if (crosses) inside = !inside;
    }
    return inside;
}

std::array<Vec2, 4> xy_corners(const Bounds3& bounds) {
    return {{{bounds.min.x, bounds.min.y}, {bounds.min.x, bounds.max.y},
             {bounds.max.x, bounds.min.y}, {bounds.max.x, bounds.max.y}}};
}

bool inside_build_volume(const BuildVolume& volume, const Bounds3& bounds) {
    if (bounds.min.z < -kEpsilon || bounds.max.z > volume.z + kEpsilon) return false;
    const auto corners = xy_corners(bounds);
    if (volume.shape == BuildVolumeShape::Rect) {
        const double min_x = volume.origin_center ? -volume.x * 0.5 : 0.0;
        const double min_y = volume.origin_center ? -volume.y * 0.5 : 0.0;
        const double max_x = min_x + volume.x;
        const double max_y = min_y + volume.y;
        return std::all_of(corners.begin(), corners.end(), [&](const Vec2& point) {
            return point.x >= min_x - kEpsilon && point.x <= max_x + kEpsilon &&
                   point.y >= min_y - kEpsilon && point.y <= max_y + kEpsilon;
        });
    }
    if (volume.shape == BuildVolumeShape::Circular) {
        const double center_x = volume.origin_center ? 0.0 : volume.x * 0.5;
        const double center_y = volume.origin_center ? 0.0 : volume.y * 0.5;
        const double radius_x = volume.x * 0.5;
        const double radius_y = volume.y * 0.5;
        return std::all_of(corners.begin(), corners.end(), [&](const Vec2& point) {
            const double nx = (point.x - center_x) / radius_x;
            const double ny = (point.y - center_y) / radius_y;
            return nx * nx + ny * ny <= 1.0 + kEpsilon;
        });
    }
    return std::all_of(corners.begin(), corners.end(), [&](const Vec2& point) {
        return point_in_polygon(volume.polygon, point);
    });
}

RunDecision reject(std::string code, std::string why) {
    return {false, std::move(code), std::move(why)};
}

RunDecision accept() { return {true, "ok", {}}; }

void require_sha(std::string_view value, std::string_view field) {
    if (!valid_sha256(value)) throw std::invalid_argument(std::string(field) + " must be sha256");
}

}  // namespace

bool Bounds3::valid() const {
    return std::isfinite(min.x) && std::isfinite(min.y) && std::isfinite(min.z) &&
           std::isfinite(max.x) && std::isfinite(max.y) && std::isfinite(max.z) &&
           min.x <= max.x && min.y <= max.y && min.z <= max.z;
}

std::string_view to_string(BuildVolumeShape value) {
    switch (value) {
        case BuildVolumeShape::Rect: return "rect";
        case BuildVolumeShape::Circular: return "circular";
        case BuildVolumeShape::Polygon: return "polygon";
    }
    throw std::invalid_argument("invalid build-volume shape");
}

std::optional<BuildVolumeShape> parse_build_volume_shape(std::string_view text) {
    if (text == "rect") return BuildVolumeShape::Rect;
    if (text == "circular") return BuildVolumeShape::Circular;
    if (text == "polygon") return BuildVolumeShape::Polygon;
    return std::nullopt;
}

bool BuildVolume::valid(std::string* why) const {
    if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z) ||
        x <= 0 || y <= 0 || z <= 0) return fail(why, "build-volume dimensions must be positive");
    if (shape == BuildVolumeShape::Polygon && polygon.size() < 3)
        return fail(why, "polygon build volume requires at least three vertices");
    for (const auto& exclusion : exclusions)
        if (!exclusion.valid()) return fail(why, "invalid build-volume exclusion");
    return true;
}

Bounds3 Plate::transformed_bounds(const ObjectInstance& instance) const {
    const Bounds3& source = instance.model.model_bounds;
    Bounds3 result{{std::numeric_limits<double>::infinity(),
                    std::numeric_limits<double>::infinity(),
                    std::numeric_limits<double>::infinity()},
                   {-std::numeric_limits<double>::infinity(),
                    -std::numeric_limits<double>::infinity(),
                    -std::numeric_limits<double>::infinity()}};
    for (const double x : {source.min.x, source.max.x}) {
        for (const double y : {source.min.y, source.max.y}) {
            for (const double z : {source.min.z, source.max.z}) {
                Vec3 point{x * instance.transform.scale.x,
                           y * instance.transform.scale.y,
                           z * instance.transform.scale.z};
                point = rotate_xyz(point, instance.transform.rotation_deg);
                point.x += instance.transform.translation.x;
                point.y += instance.transform.translation.y;
                point.z += instance.transform.translation.z;
                result.min.x = std::min(result.min.x, point.x);
                result.min.y = std::min(result.min.y, point.y);
                result.min.z = std::min(result.min.z, point.z);
                result.max.x = std::max(result.max.x, point.x);
                result.max.y = std::max(result.max.y, point.y);
                result.max.z = std::max(result.max.z, point.z);
            }
        }
    }
    return result;
}

PlacementDecision Plate::place(ObjectInstance instance) {
    std::string volume_error;
    if (!build_volume.valid(&volume_error)) return {false, "invalid_build_volume", volume_error};
    if (instance.id.empty()) return {false, "invalid_instance", "instance id is required"};
    if (!valid_sha256(instance.model.sha256))
        return {false, "invalid_model_hash", "model artifact must be referenced by sha256"};
    if (!instance.model.model_bounds.valid())
        return {false, "invalid_model_bounds", "model artifact bounds are invalid"};
    if (instance.transform.scale.x <= 0 || instance.transform.scale.y <= 0 ||
        instance.transform.scale.z <= 0)
        return {false, "invalid_transform", "scale components must be positive"};
    if (std::any_of(instances.begin(), instances.end(), [&](const ObjectInstance& existing) {
            return existing.id == instance.id;
        })) return {false, "duplicate_instance", "instance id already exists on plate"};

    const Bounds3 candidate = transformed_bounds(instance);
    if (!inside_build_volume(build_volume, candidate))
        return {false, "outside_build_volume", "transformed model bounds leave the build volume"};
    for (const auto& exclusion : build_volume.exclusions) {
        if (positive_overlap(candidate, exclusion))
            return {false, "plate_exclusion_collision", "placement intersects a plate exclusion"};
    }
    for (const auto& existing : instances) {
        if (positive_overlap(candidate, transformed_bounds(existing)))
            return {false, "instance_overlap", "placement overlaps an existing object"};
    }
    instances.push_back(std::move(instance));
    return {true, "ok", {}};
}

std::string_view to_string(ProfileKind value) {
    switch (value) {
        case ProfileKind::Printer: return "printer";
        case ProfileKind::Filament: return "filament";
        case ProfileKind::Process: return "process";
    }
    throw std::invalid_argument("invalid profile kind");
}

std::optional<ProfileKind> parse_profile_kind(std::string_view text) {
    if (text == "printer") return ProfileKind::Printer;
    if (text == "filament") return ProfileKind::Filament;
    if (text == "process") return ProfileKind::Process;
    return std::nullopt;
}

std::string ProfileDefinition::content_hash() const {
    json value = *this;
    return sha256_bytes(device::detail::canonical_json(value).dump());
}

ProfileRef ProfileDefinition::reference() const {
    if (id.empty()) throw std::invalid_argument("profile id is required");
    return {kind, id, content_hash()};
}

SliceArtifactRecord SliceArtifactRecord::from_bytes(
    std::string_view artifact_bytes, std::string request_id, std::string format,
    std::vector<ProfileRef> profiles, std::vector<ObjectSliceStats> per_object,
    device::Seq journal_seq) {
    if (!additive_result_kinds_registered())
        throw std::logic_error("additive result kinds are not registered");
    if (journal_seq == 0) throw std::invalid_argument("artifact requires gateway journal seq");
    if (request_id.empty() || format.empty())
        throw std::invalid_argument("artifact request id and format are required");
    for (const auto& profile : profiles) require_sha(profile.sha256, "profile reference");
    std::set<std::string> object_ids;
    for (const auto& object : per_object) {
        if (object.instance_id.empty() || !object_ids.insert(object.instance_id).second)
            throw std::invalid_argument("per-object stats require unique instance ids");
    }
    return {sha256_bytes(artifact_bytes), std::move(request_id), std::move(format),
            static_cast<uint64_t>(artifact_bytes.size()), std::move(profiles),
            std::move(per_object), journal_seq};
}

bool SliceArtifactRecord::matches_bytes(std::string_view artifact_bytes) const {
    return bytes == artifact_bytes.size() && artifact_sha256 == sha256_bytes(artifact_bytes);
}

PlacementDecision PrintProject::place(std::string_view plate_id, ObjectInstance instance) {
    const auto found = std::find_if(plates.begin(), plates.end(), [&](const Plate& plate) {
        return plate.id == plate_id;
    });
    if (found == plates.end()) return {false, "plate_not_found", "plate id does not exist"};
    auto decision = found->place(std::move(instance));
    if (decision.accepted) ++revision;
    return decision;
}

std::pair<std::optional<RunRecord>, RunDecision> RunRecord::create(
    RunHeader header, const SliceArtifactRecord& artifact,
    std::string_view immutable_artifact_bytes) {
    if (!additive_result_kinds_registered())
        return {std::nullopt, reject("unregistered_result_kind", "print durable kinds unavailable")};
    if (header.run_id.empty() || header.project_id.empty() || header.plate_id.empty())
        return {std::nullopt, reject("invalid_run_binding", "run, project, and plate ids are required")};
    if (artifact.seq == 0)
        return {std::nullopt, reject("unstamped_artifact", "artifact has no gateway journal seq")};
    if (!artifact.matches_bytes(immutable_artifact_bytes))
        return {std::nullopt, reject("artifact_hash_mismatch", "artifact bytes do not match sha256 binding")};
    if (header.artifact_sha256 != artifact.artifact_sha256 ||
        header.request_id != artifact.request_id)
        return {std::nullopt, reject("artifact_binding_mismatch", "run header does not bind this artifact")};
    if (header.approval)
        return {std::nullopt, reject("approval_must_bind_after_artifact", "create the immutable artifact binding first")};
    return {std::optional<RunRecord>(RunRecord(std::move(header))), accept()};
}

RunDecision RunRecord::replace_artifact_before_approval(
    const SliceArtifactRecord& artifact, std::string_view immutable_artifact_bytes) {
    if (approved()) return reject("run_binding_immutable", "approved run binding cannot change");
    if (!artifact.matches_bytes(immutable_artifact_bytes))
        return reject("artifact_hash_mismatch", "artifact bytes do not match sha256 binding");
    if (artifact.seq == 0) return reject("unstamped_artifact", "artifact has no gateway journal seq");
    header_.artifact_sha256 = artifact.artifact_sha256;
    header_.request_id = artifact.request_id;
    return accept();
}

RunDecision RunRecord::bind_approval(ApprovalBinding approval) {
    if (approved()) return reject("approval_already_bound", "approval binding is immutable");
    if (approval.approval_id.empty() || approval.approver.empty() || approval.expires_at.empty() ||
        approval.nonce.empty()) return reject("invalid_approval_binding", "approval audit fields are required");
    if (approval.exercisable_by != "human_only" && approval.exercisable_by != "human_or_agent")
        return reject("invalid_approval_binding", "invalid exercisable_by value");
    if (approval.max_starts != 1)
        return reject("invalid_approval_binding", "print run approval must bind max_starts=1");
    // This records the arbiter's result; it deliberately does not authorize execution.
    header_.approval = std::move(approval);
    return accept();
}

RunDecision RunRecord::append_monitoring(device::Seq journal_seq, MonitoringKind kind,
                                         std::string observed_at, json value) {
    if (journal_seq == 0)
        return reject("missing_journal_seq", "paintable monitoring rows require journal seq");
    if (!monitoring_.empty() && journal_seq <= monitoring_.back().seq)
        return reject("non_monotonic_journal_seq", "monitoring journal seq must advance");
    monitoring_.push_back({RecordIndex(monitoring_.size()), journal_seq, kind,
                           std::move(observed_at), std::move(value)});
    return accept();
}

json RunRecord::to_json() const {
    return {{"schema", "chudware.print_run/1"}, {"header", header_},
            {"monitoring", monitoring_}};
}

RunRecord RunRecord::from_json(const json& value) {
    if (value.at("schema") != "chudware.print_run/1")
        throw std::invalid_argument("invalid print run schema");
    RunRecord result(value.at("header").get<RunHeader>());
    result.monitoring_ = value.value("monitoring", std::vector<MonitoringRow>{});
    for (size_t index = 0; index < result.monitoring_.size(); ++index) {
        if (result.monitoring_[index].record_index.value() != index)
            throw std::invalid_argument("run record_index is truncated or reordered");
        if (result.monitoring_[index].seq == 0)
            throw std::invalid_argument("monitoring row lacks gateway journal seq");
        if (index > 0 && result.monitoring_[index - 1].seq >= result.monitoring_[index].seq)
            throw std::invalid_argument("monitoring journal seq is not monotonic");
    }
    return result;
}

std::string_view to_string(MonitoringKind value) {
    switch (value) {
        case MonitoringKind::State: return "state";
        case MonitoringKind::Progress: return "progress";
        case MonitoringKind::Temperature: return "temperature";
        case MonitoringKind::Fault: return "fault";
    }
    throw std::invalid_argument("invalid monitoring kind");
}

std::optional<MonitoringKind> parse_monitoring_kind(std::string_view text) {
    if (text == "state") return MonitoringKind::State;
    if (text == "progress") return MonitoringKind::Progress;
    if (text == "temperature") return MonitoringKind::Temperature;
    if (text == "fault") return MonitoringKind::Fault;
    return std::nullopt;
}

device::CapabilityReport printer_authoring_capabilities(
    std::string subject_id, std::vector<std::string> generate_evidence,
    std::vector<std::string> post_evidence) {
    using namespace device;
    CapabilityReport report;
    report.subject_id = std::move(subject_id);
    report.max_advertised = CapabilityLevel::Post;
    report.emulator_stage_passed = EmulatorStage::E3;
    auto verified = [&](CapabilityLevel level, std::vector<std::string> evidence) {
        report.levels[level] = {level, CapabilityTruthValue::Verified, std::move(evidence),
                                EmulatorStage::E3, std::nullopt, {}};
    };
    auto unsupported = [&](CapabilityLevel level, std::string why) {
        report.levels[level] = {level, CapabilityTruthValue::Unsupported, {}, EmulatorStage::E3,
                                std::nullopt, std::move(why)};
    };
    verified(CapabilityLevel::Plan, generate_evidence);
    verified(CapabilityLevel::Generate, std::move(generate_evidence));
    verified(CapabilityLevel::Post, std::move(post_evidence));
    unsupported(CapabilityLevel::Parse, "printer authoring report does not claim parser capability");
    unsupported(CapabilityLevel::Simulate, "twin conclusions are context, never printer authorization");
    unsupported(CapabilityLevel::Monitor, "hardware monitoring is deferred to a printer adapter pilot");
    unsupported(CapabilityLevel::Transport, "no printer transport exists in the additive model");
    report.levels[CapabilityLevel::Execute] = {
        CapabilityLevel::Execute, CapabilityTruthValue::Assumed, {}, EmulatorStage::E3,
        CapabilityLevel::Execute, "hardware pilot has not qualified physical execution"};
    unsupported(CapabilityLevel::SafetyIntegration,
                "software print control is not a safety integration");
    return report;
}

bool additive_result_kinds_registered() {
    const auto registry = mfg::ResultRegistry::manufacturingDefaults();
    static constexpr std::string_view required[] = {
        "print.slice_result", "print.artifact", "print.procedural_fact", "print.preflight",
        "print.run_link", "print.capability", "print.media_source"};
    return std::all_of(std::begin(required), std::end(required), [&](std::string_view kind) {
        return registry.validate(kind).accepted;
    });
}

bool valid_sha256(std::string_view value) {
    return value.size() == 64 && std::all_of(value.begin(), value.end(), [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    });
}

std::string sha256_bytes(std::string_view bytes) { return util::sha256_hex(bytes); }

void to_json(json& j, const Vec2& v) { j = {{"x", v.x}, {"y", v.y}}; }
void from_json(const json& j, Vec2& v) { j.at("x").get_to(v.x); j.at("y").get_to(v.y); }
void to_json(json& j, const Vec3& v) { j = {{"x", v.x}, {"y", v.y}, {"z", v.z}}; }
void from_json(const json& j, Vec3& v) {
    j.at("x").get_to(v.x); j.at("y").get_to(v.y); j.at("z").get_to(v.z);
}
void to_json(json& j, const Bounds3& v) { j = {{"min", v.min}, {"max", v.max}}; }
void from_json(const json& j, Bounds3& v) {
    j.at("min").get_to(v.min); j.at("max").get_to(v.max);
    if (!v.valid()) throw std::invalid_argument("invalid bounds");
}
void to_json(json& j, const Transform& v) {
    j = {{"translation", v.translation}, {"rotation_deg", v.rotation_deg}, {"scale", v.scale}};
}
void from_json(const json& j, Transform& v) {
    j.at("translation").get_to(v.translation); j.at("rotation_deg").get_to(v.rotation_deg);
    j.at("scale").get_to(v.scale);
}
void to_json(json& j, const BuildVolume& v) {
    j = {{"shape", to_string(v.shape)}, {"x", v.x}, {"y", v.y}, {"z", v.z},
         {"origin_center", v.origin_center}, {"polygon", v.polygon}, {"exclusions", v.exclusions}};
}
void from_json(const json& j, BuildVolume& v) {
    const auto shape = parse_build_volume_shape(j.at("shape").get<std::string>());
    if (!shape) throw std::invalid_argument("invalid build-volume shape");
    v.shape = *shape; j.at("x").get_to(v.x); j.at("y").get_to(v.y); j.at("z").get_to(v.z);
    v.origin_center = j.value("origin_center", false);
    v.polygon = j.value("polygon", std::vector<Vec2>{});
    v.exclusions = j.value("exclusions", std::vector<Bounds3>{});
    std::string why; if (!v.valid(&why)) throw std::invalid_argument(why);
}
void to_json(json& j, const ModelArtifactRef& v) {
    j = {{"sha256", v.sha256}, {"model_bounds", v.model_bounds}};
}
void from_json(const json& j, ModelArtifactRef& v) {
    j.at("sha256").get_to(v.sha256); require_sha(v.sha256, "model artifact");
    j.at("model_bounds").get_to(v.model_bounds);
}
void to_json(json& j, const ObjectInstance& v) {
    j = {{"id", v.id}, {"model", v.model}, {"transform", v.transform},
         {"extruder", v.extruder}, {"overrides", v.overrides}};
}
void from_json(const json& j, ObjectInstance& v) {
    j.at("id").get_to(v.id); j.at("model").get_to(v.model); j.at("transform").get_to(v.transform);
    v.extruder = j.value("extruder", 0); v.overrides = j.value("overrides", json::object());
}
void to_json(json& j, const Plate& v) {
    j = {{"id", v.id}, {"name", v.name}, {"build_volume", v.build_volume},
         {"sequential_print", v.sequential_print}, {"arrange_seed", v.arrange_seed},
         {"instances", v.instances}};
}
void from_json(const json& j, Plate& v) {
    j.at("id").get_to(v.id); j.at("name").get_to(v.name);
    j.at("build_volume").get_to(v.build_volume);
    v.sequential_print = j.value("sequential_print", false);
    v.arrange_seed = j.value("arrange_seed", uint64_t{0});
    v.instances = j.value("instances", std::vector<ObjectInstance>{});
}
void to_json(json& j, const ProfileRef& v) {
    j = {{"kind", to_string(v.kind)}, {"id", v.id}, {"sha256", v.sha256}};
}
void from_json(const json& j, ProfileRef& v) {
    const auto kind = parse_profile_kind(j.at("kind").get<std::string>());
    if (!kind) throw std::invalid_argument("invalid profile kind");
    v.kind = *kind; j.at("id").get_to(v.id); j.at("sha256").get_to(v.sha256);
    require_sha(v.sha256, "profile reference");
}
void to_json(json& j, const ProfileDefinition& v) {
    j = {{"kind", to_string(v.kind)}, {"id", v.id}, {"name", v.name},
         {"revision", v.revision}, {"fields", v.fields}};
}
void from_json(const json& j, ProfileDefinition& v) {
    const auto kind = parse_profile_kind(j.at("kind").get<std::string>());
    if (!kind) throw std::invalid_argument("invalid profile kind");
    v.kind = *kind; j.at("id").get_to(v.id); j.at("name").get_to(v.name);
    j.at("revision").get_to(v.revision); j.at("fields").get_to(v.fields);
}
void to_json(json& j, const ObjectSliceStats& v) {
    j = {{"instance_id", v.instance_id}, {"layer_count", v.layer_count},
         {"estimated_time_s", v.estimated_time_s}, {"filament_mm", v.filament_mm},
         {"filament_g", v.filament_g}, {"bounds", v.bounds}};
}
void from_json(const json& j, ObjectSliceStats& v) {
    j.at("instance_id").get_to(v.instance_id); j.at("layer_count").get_to(v.layer_count);
    j.at("estimated_time_s").get_to(v.estimated_time_s);
    j.at("filament_mm").get_to(v.filament_mm); j.at("filament_g").get_to(v.filament_g);
    j.at("bounds").get_to(v.bounds);
}
void to_json(json& j, const SliceArtifactRecord& v) {
    if (v.seq == 0) throw std::invalid_argument("artifact lacks gateway journal seq");
    require_sha(v.artifact_sha256, "artifact");
    j = {{"kind", "print.artifact"}, {"artifact_sha256", v.artifact_sha256},
         {"request_id", v.request_id}, {"format", v.format}, {"bytes", v.bytes},
         {"profiles", v.profiles}, {"per_object", v.per_object}, {"seq", v.seq}};
}
void from_json(const json& j, SliceArtifactRecord& v) {
    if (j.at("kind") != "print.artifact") throw std::invalid_argument("invalid artifact kind");
    j.at("artifact_sha256").get_to(v.artifact_sha256); require_sha(v.artifact_sha256, "artifact");
    j.at("request_id").get_to(v.request_id); j.at("format").get_to(v.format);
    j.at("bytes").get_to(v.bytes); j.at("profiles").get_to(v.profiles);
    j.at("per_object").get_to(v.per_object); j.at("seq").get_to(v.seq);
    if (v.seq == 0) throw std::invalid_argument("artifact lacks gateway journal seq");
}
void to_json(json& j, const PrintRunLink& v) {
    if (v.seq == 0) throw std::invalid_argument("run link lacks gateway journal seq");
    require_sha(v.artifact_sha256, "run link artifact");
    j = {{"run_id", v.run_id}, {"artifact_sha256", v.artifact_sha256},
         {"request_id", v.request_id}, {"project_id", v.project_id},
         {"project_revision", v.project_revision}, {"plate_id", v.plate_id},
         {"approval_id", v.approval_id}, {"device_id", v.device_id}, {"seq", v.seq}};
}
void from_json(const json& j, PrintRunLink& v) {
    j.at("run_id").get_to(v.run_id); j.at("artifact_sha256").get_to(v.artifact_sha256);
    require_sha(v.artifact_sha256, "run link artifact");
    j.at("request_id").get_to(v.request_id); j.at("project_id").get_to(v.project_id);
    j.at("project_revision").get_to(v.project_revision); j.at("plate_id").get_to(v.plate_id);
    j.at("approval_id").get_to(v.approval_id); j.at("device_id").get_to(v.device_id);
    j.at("seq").get_to(v.seq); if (v.seq == 0) throw std::invalid_argument("run link lacks seq");
}
void to_json(json& j, const PrintProject& v) {
    j = {{"schema", "chudware.print_project/1"}, {"id", v.id}, {"name", v.name},
         {"units", "mm"}, {"revision", v.revision}, {"printer", v.printer},
         {"filaments", v.filaments}, {"process", v.process}, {"plates", v.plates},
         {"artifacts", v.artifacts}, {"runs", v.runs}};
}
void from_json(const json& j, PrintProject& v) {
    if (j.at("schema") != "chudware.print_project/1" || j.at("units") != "mm")
        throw std::invalid_argument("invalid print project schema");
    j.at("id").get_to(v.id); j.at("name").get_to(v.name); j.at("revision").get_to(v.revision);
    j.at("printer").get_to(v.printer); j.at("filaments").get_to(v.filaments);
    j.at("process").get_to(v.process); j.at("plates").get_to(v.plates);
    j.at("artifacts").get_to(v.artifacts); j.at("runs").get_to(v.runs);
}
void to_json(json& j, const ApprovalBinding& v) {
    j = {{"approval_id", v.approval_id}, {"approver", v.approver},
         {"expires_at", v.expires_at}, {"exercisable_by", v.exercisable_by},
         {"nonce", v.nonce}, {"max_starts", v.max_starts}};
}
void from_json(const json& j, ApprovalBinding& v) {
    j.at("approval_id").get_to(v.approval_id); j.at("approver").get_to(v.approver);
    j.at("expires_at").get_to(v.expires_at); j.at("exercisable_by").get_to(v.exercisable_by);
    j.at("nonce").get_to(v.nonce); j.at("max_starts").get_to(v.max_starts);
}
void to_json(json& j, const RunHeader& v) {
    j = {{"run_id", v.run_id}, {"artifact_sha256", v.artifact_sha256},
         {"request_id", v.request_id}, {"project_id", v.project_id},
         {"project_revision", v.project_revision}, {"plate_id", v.plate_id},
         {"profiles", v.profiles}, {"approval", nullptr}};
    if (v.approval) j["approval"] = *v.approval;
}
void from_json(const json& j, RunHeader& v) {
    j.at("run_id").get_to(v.run_id); j.at("artifact_sha256").get_to(v.artifact_sha256);
    require_sha(v.artifact_sha256, "run artifact");
    j.at("request_id").get_to(v.request_id); j.at("project_id").get_to(v.project_id);
    j.at("project_revision").get_to(v.project_revision); j.at("plate_id").get_to(v.plate_id);
    j.at("profiles").get_to(v.profiles);
    v.approval.reset();
    if (!j.at("approval").is_null()) v.approval = j.at("approval").get<ApprovalBinding>();
}
void to_json(json& j, const MonitoringRow& v) {
    j = {{"record_index", v.record_index.value()}, {"seq", v.seq},
         {"kind", to_string(v.kind)}, {"observed_at", v.observed_at}, {"value", v.value}};
}
void from_json(const json& j, MonitoringRow& v) {
    v.record_index = RecordIndex(j.at("record_index").get<uint64_t>());
    j.at("seq").get_to(v.seq);
    const auto kind = parse_monitoring_kind(j.at("kind").get<std::string>());
    if (!kind) throw std::invalid_argument("invalid monitoring kind");
    v.kind = *kind; j.at("observed_at").get_to(v.observed_at); j.at("value").get_to(v.value);
}

}  // namespace chudware::print
