#include "mfg_mirror.h"

#include <algorithm>
#include <array>
#include <sstream>
#include <utility>

namespace ui {
namespace {

using json = nlohmann::json;

constexpr std::array<const char*, 9> kCapabilityLevels = {
    "plan", "generate", "post", "parse", "simulate", "monitor",
    "transport", "execute", "safety_integration"};

std::uint64_t u64(const json& object, const char* key) {
    if (!object.is_object()) return 0;
    const auto it = object.find(key);
    if (it == object.end() || !it->is_number_unsigned()) {
        if (it != object.end() && it->is_number_integer() && it->get<std::int64_t>() >= 0)
            return static_cast<std::uint64_t>(it->get<std::int64_t>());
        return 0;
    }
    return it->get<std::uint64_t>();
}

std::string text(const json& object, const char* key) {
    if (!object.is_object()) return {};
    const auto it = object.find(key);
    return it != object.end() && it->is_string() ? it->get<std::string>() : std::string();
}

const json& objectAt(const json& object, const char* key) {
    static const json empty = json::object();
    if (!object.is_object()) return empty;
    const auto it = object.find(key);
    return it != object.end() && it->is_object() ? *it : empty;
}

const json& payloadOf(const json& wire) {
    if (wire.is_object()) {
        const auto it = wire.find("result");
        if (it != wire.end() && it->is_object()) return *it;
    }
    return wire;
}

std::string displayJson(const json& value) {
    if (value.is_string()) return value.get<std::string>();
    if (value.is_boolean()) return value.get<bool>() ? "true" : "false";
    if (value.is_number_float()) {
        std::ostringstream out;
        out.precision(6);
        out << value.get<double>();
        return out.str();
    }
    if (value.is_number_integer()) return std::to_string(value.get<std::int64_t>());
    if (value.is_number_unsigned()) return std::to_string(value.get<std::uint64_t>());
    return value.dump();
}

MfgFreshValue unknownFresh(const std::string& why) {
    MfgFreshValue value;
    value.why = why.empty() ? "not reported" : why;
    return value;
}

MfgCommandAffordance parseAffordance(const json& controls, const char* name) {
    MfgCommandAffordance value;
    if (!controls.is_object()) return value;
    const auto it = controls.find(name);
    if (it == controls.end() || !it->is_object()) return value;
    value.enabled = it->value("enabled", false);
    value.reason = text(*it, "reason");
    if (!value.enabled && value.reason.empty())
        value.reason = "The gateway disabled this command without an enablement decision.";
    if (value.enabled) value.reason.clear();
    return value;
}

}  // namespace

MfgFreshValue parseMfgFresh(const json& wire, bool telemetry) {
    if (!wire.is_object()) return unknownFresh("freshness envelope missing");

    MfgFreshValue out;
    const std::string state = text(wire, "state");
    if (state == "ok") out.state = MfgFreshState::Ok;
    else if (state == "stale") out.state = MfgFreshState::Stale;
    else if (state == "refused") out.state = MfgFreshState::Refused;
    else if (state == "unsupported" || state == "absent") out.state = MfgFreshState::Unsupported;
    else out.state = MfgFreshState::Unknown;

    out.tMonoNs = u64(wire, "t_mono_ns");
    out.seq = u64(wire, "seq");
    out.ageMs = u64(wire, "age_ms");
    out.source = text(wire, "src");
    out.why = text(wire, "why");

    const auto ttl = wire.find("ttl_ms");
    if (ttl != wire.end() && ttl->is_number_unsigned()) out.ttlMs = ttl->get<std::uint64_t>();
    else if (ttl != wire.end() && ttl->is_number_integer() && ttl->get<std::int64_t>() >= 0)
        out.ttlMs = static_cast<std::uint64_t>(ttl->get<std::int64_t>());

    const auto last = wire.find("last_ok");
    if (last != wire.end() && last->is_object()) {
        const auto v = last->find("v");
        if (v != last->end() && !v->is_null())
            out.lastGood = MfgLastGood{*v, u64(*last, "t_mono_ns"), u64(*last, "seq")};
    }

    const auto value = wire.find("v");
    if (out.state == MfgFreshState::Ok && value != wire.end() && !value->is_null()) {
        out.value = *value;
        out.hasValue = true;
    }

    // Enforce the wire invariant at the mirror boundary. A malformed current
    // row becomes unknown rather than letting a panel invent a display value.
    if (out.state == MfgFreshState::Ok && !out.hasValue) {
        out.state = MfgFreshState::Unknown;
        out.why = "state is ok but v is null";
    }
    if (out.state != MfgFreshState::Ok) {
        out.value = json();
        out.hasValue = false;
        if (out.why.empty()) out.why = state.empty() ? "unknown" : state;
    }
    if (out.state == MfgFreshState::Ok && out.ttlMs && out.ageMs > *out.ttlMs) {
        out.state = MfgFreshState::Stale;
        out.value = json();
        out.hasValue = false;
        out.why = "age exceeds ttl";
    }
    if (out.state == MfgFreshState::Ok && telemetry && !out.ttlMs) {
        out.state = MfgFreshState::Unknown;
        out.value = json();
        out.hasValue = false;
        out.why = "telemetry interval is unknown (ttl_ms is null)";
    }
    return out;
}

MfgFreshPresentation presentMfgFresh(const MfgFreshValue& fresh, const std::string& unit) {
    MfgFreshPresentation out;
    out.ok = fresh.current();
    out.stale = fresh.state == MfgFreshState::Stale;
    if (fresh.current()) {
        out.current = displayJson(fresh.value);
        if (!unit.empty()) out.current += " " + unit;
        if (!fresh.source.empty()) out.detail = "source: " + fresh.source;
        if (fresh.ageMs > 0) {
            if (!out.detail.empty()) out.detail += " · ";
            out.detail += "age " + std::to_string(fresh.ageMs) + " ms";
        }
        return out;
    }

    out.current = "—";
    out.detail = fresh.why.empty() ? "unknown" : fresh.why;
    if (fresh.lastGood) {
        out.detail += " · last good " + displayJson(fresh.lastGood->value);
        if (!unit.empty()) out.detail += " " + unit;
        out.detail += " at seq " + std::to_string(fresh.lastGood->seq);
    }
    return out;
}

MfgDevice* MfgMirror::device(const std::string& id) {
    const auto it = std::find_if(devices_.begin(), devices_.end(),
                                 [&](const MfgDevice& row) { return row.deviceId == id; });
    return it == devices_.end() ? nullptr : &*it;
}

const MfgDevice* MfgMirror::device(const std::string& id) const {
    const auto it = std::find_if(devices_.begin(), devices_.end(),
                                 [&](const MfgDevice& row) { return row.deviceId == id; });
    return it == devices_.end() ? nullptr : &*it;
}

MfgRun* MfgMirror::run(const std::string& id) {
    const auto it = std::find_if(runs_.begin(), runs_.end(),
                                 [&](const MfgRun& row) { return row.runId == id; });
    return it == runs_.end() ? nullptr : &*it;
}

const MfgRun* MfgMirror::run(const std::string& id) const {
    const auto it = std::find_if(runs_.begin(), runs_.end(),
                                 [&](const MfgRun& row) { return row.runId == id; });
    return it == runs_.end() ? nullptr : &*it;
}

MfgDevice& MfgMirror::upsertDevice(const std::string& id) {
    if (MfgDevice* existing = device(id)) return *existing;
    devices_.push_back(MfgDevice{});
    devices_.back().deviceId = id;
    devices_.back().state = unknownFresh("device state not reported");
    devices_.back().snapshot = unknownFresh("telemetry not reported");
    return devices_.back();
}

MfgRun& MfgMirror::upsertRun(const std::string& id) {
    if (MfgRun* existing = run(id)) return *existing;
    runs_.push_back(MfgRun{});
    runs_.back().runId = id;
    runs_.back().state = unknownFresh("run state not reported");
    runs_.back().progress = unknownFresh("progress not reported");
    runs_.back().snapshot = unknownFresh("telemetry not reported");
    return runs_.back();
}

void MfgMirror::ingestCapability(MfgDevice& target, const json& wire) {
    const json& report = wire.contains("capability") && wire["capability"].is_object()
                             ? wire["capability"] : wire;
    target.capabilities.clear();
    target.capabilities.reserve(kCapabilityLevels.size());
    for (const char* level : kCapabilityLevels)
        target.capabilities.push_back(MfgCapabilityLevel{level});

    const auto assign = [&](const std::string& level, const json& value) {
        auto it = std::find_if(target.capabilities.begin(), target.capabilities.end(),
                               [&](const MfgCapabilityLevel& row) { return row.level == level; });
        if (it == target.capabilities.end()) return;
        it->reported = true;
        if (value.is_string()) it->truth = value.get<std::string>();
        else if (value.is_object()) {
            it->truth = text(value, "truth");
            it->missingLevel = text(value, "missing_level");
            it->why = text(value, "why");
        }
    };

    const auto levels = report.find("levels");
    if (levels != report.end() && levels->is_array()) {
        for (const json& row : *levels) assign(text(row, "level"), row);
    } else if (levels != report.end() && levels->is_object()) {
        for (auto it = levels->begin(); it != levels->end(); ++it) assign(it.key(), it.value());
    }
}

void MfgMirror::ingestDevice(const json& row) {
    const std::string id = text(row, "device_id");
    if (id.empty()) return;
    MfgDevice& target = upsertDevice(id);
    const std::string machineClass = text(row, "machine_class");
    if (!machineClass.empty()) target.machineClass = machineClass;
    if (row.contains("state")) target.state = parseMfgFresh(row["state"], true);
    if (row.contains("snapshot")) target.snapshot = parseMfgFresh(row["snapshot"], true);

    const json& identity = objectAt(row, "identity");
    const std::string controller = text(identity, "firmware_banner");
    if (!controller.empty()) target.controller = controller;
    const std::string fingerprint = text(row, "fingerprint_sha256");
    if (!fingerprint.empty()) target.fingerprint = fingerprint;

    if (row.contains("transport") && row["transport"].is_object()) target.transport = row["transport"];
    if (row.contains("policy") && row["policy"].is_object()) {
        target.policy = row["policy"];
        const std::string ceiling = text(target.policy, "max_exercisable_by");
        if (!ceiling.empty()) target.maxExercisableBy = ceiling;
    }
    const json& lease = objectAt(row, "lease");
    const std::string owner = text(lease, "owner");
    if (!owner.empty()) target.commandOwner = owner;
    const std::string leaseId = text(lease, "lease_id");
    if (!leaseId.empty()) target.leaseId = leaseId;

    if (row.contains("capability") && row["capability"].is_object()) ingestCapability(target, row);
}

void MfgMirror::ingestGate(MfgGateReport& gate, const json& wire) {
    const json& report = wire.contains("gate_report") && wire["gate_report"].is_object()
                             ? wire["gate_report"]
                             : (wire.contains("report") && wire["report"].is_object()
                                    ? wire["report"] : wire);
    gate.reportRef = text(report, "report_ref");
    if (gate.reportRef.empty()) gate.reportRef = text(report, "gate_report_ref");
    if (gate.reportRef.empty()) gate.reportRef = text(report, "ref");
    gate.seq = u64(report, "seq");
    if (gate.seq == 0) gate.seq = u64(wire, "result_seq");

    const auto stage1 = report.find("stage1");
    if (stage1 != report.end() && stage1->is_string()) {
        gate.stage1 = stage1->get<std::string>();
    } else if (stage1 != report.end() && stage1->is_object()) {
        gate.stage1 = text(*stage1, "status");
        if (gate.stage1.empty()) gate.stage1 = text(*stage1, "verdict");
        gate.stage1Why = text(*stage1, "why");
    } else if (stage1 != report.end() && stage1->is_array()) {
        gate.stage1.clear();
        gate.stage1Why.clear();
        for (const json& admission : *stage1) {
            if (!gate.stage1.empty()) gate.stage1 += " · ";
            gate.stage1 += text(admission, "name") + "=" + text(admission, "admit");
            const std::string why = text(admission, "why");
            if (!why.empty()) {
                if (!gate.stage1Why.empty()) gate.stage1Why += " · ";
                gate.stage1Why += text(admission, "name") + ": " + why;
            }
        }
    }

    gate.guards.clear();
    const auto parseGuard = [&](const std::string& key, const json& row) {
        MfgGateGuard guard;
        guard.name = text(row, "guard");
        if (guard.name.empty()) guard.name = text(row, "name");
        if (guard.name.empty()) guard.name = key;
        guard.verdict = text(row, "verdict");
        if (guard.verdict.empty()) guard.verdict = text(row, "status");
        guard.why = text(row, "why");
        const json& waiver = objectAt(row, "waiver");
        if (!waiver.empty())
            guard.waiver = MfgWaiver{text(waiver, "by"), text(waiver, "reason"),
                                     u64(waiver, "at_seq")};
        gate.guards.push_back(std::move(guard));
    };

    const auto guards = report.find("stage2");
    const auto fallback = report.find("guards");
    const json* rows = guards != report.end() ? &*guards
                     : fallback != report.end() ? &*fallback : nullptr;
    if (rows && rows->is_array()) {
        for (const json& row : *rows) parseGuard({}, row);
    } else if (rows && rows->is_object()) {
        for (auto it = rows->begin(); it != rows->end(); ++it) parseGuard(it.key(), it.value());
    }
    const auto waivers = report.find("waivers");
    if (waivers != report.end() && waivers->is_array()) {
        for (const json& waiver : *waivers) {
            const std::string guardName = text(waiver, "guard");
            const auto guard = std::find_if(gate.guards.begin(), gate.guards.end(),
                [&](const MfgGateGuard& row) { return row.name == guardName; });
            if (guard != gate.guards.end())
                guard->waiver = MfgWaiver{text(waiver, "by"), text(waiver, "reason"),
                                          u64(waiver, "at_seq")};
        }
    }
}

void MfgMirror::ingestRun(const json& row) {
    const std::string id = text(row, "run_id");
    if (id.empty()) return;
    MfgRun& target = upsertRun(id);
    const std::string deviceId = text(row, "device_id");
    if (!deviceId.empty()) target.deviceId = deviceId;
    target.approvalId = text(row, "approval_id");
    target.proposalId = text(row, "proposal_id");
    target.artifactHash = text(row, "artifact_hash");
    const json& initiator = objectAt(row, "initiator");
    target.initiatedBy = text(initiator, "kind");
    if (row.contains("state") && row["state"].is_string()) {
        target.stateLabel = row["state"].get<std::string>();
    } else if (row.contains("state")) {
        target.state = parseMfgFresh(row["state"], true);
        const MfgFreshPresentation state = presentMfgFresh(target.state);
        target.stateLabel = state.current;
    }
    if (row.contains("progress")) target.progress = parseMfgFresh(row["progress"], true);
    if (row.contains("snapshot")) target.snapshot = parseMfgFresh(row["snapshot"], true);
    if (row.contains("gate_report") || row.contains("report")) ingestGate(target.gates, row);
    const std::string gateRef = text(row, "gate_report_ref");
    if (!gateRef.empty()) target.gates.reportRef = gateRef;
    const json& controls = objectAt(row, "controls");
    target.start = parseAffordance(controls, "start");
    target.hold = parseAffordance(controls, "hold");
}

void MfgMirror::ingestApproval(const json& row) {
    const json& approval = row.contains("approval") && row["approval"].is_object()
                               ? row["approval"]
                               : (row.contains("proposal") && row["proposal"].is_object()
                                      ? row["proposal"] : row);
    const std::string approvalId = text(approval, "approval_id");
    if (!approvalId.empty()) approval_.approvalId = approvalId;
    const std::string proposalId = text(approval, "proposal_id");
    if (!proposalId.empty()) approval_.proposalId = proposalId;
    const std::string status = text(approval, "status");
    if (!status.empty()) approval_.status = status;
    else if (!approvalId.empty()) approval_.status = "issued";
    std::string reason = text(approval, "blocked_reason");
    if (reason.empty()) reason = text(approval, "why");
    if (!reason.empty()) approval_.blockedReason = reason;
    if (approval.contains("binding") && approval["binding"].is_object())
        approval_.binding = approval["binding"];
    else approval_.binding = approval;
}

void MfgMirror::ingestRpc(const std::string& method, const json& wire) {
    const json& result = payloadOf(wire);
    gatewayReachable_ = true;
    gatewayWhy_.clear();
    const std::uint64_t seq = u64(result, "result_seq");
    if (seq > latestSeq_) latestSeq_ = seq;
    const std::uint64_t epoch = u64(result, "epoch");
    if (epoch != 0 && epoch_ != 0 && epoch != epoch_) invalidateTelemetry("gateway epoch changed");
    if (epoch != 0) epoch_ = epoch;

    if (method == "mfg_device_list") {
        const auto rows = result.find("devices");
        if (rows != result.end() && rows->is_array()) for (const json& row : *rows) ingestDevice(row);
    } else if (method == "mfg_device_describe") {
        ingestDevice(result.contains("device") && result["device"].is_object() ? result["device"] : result);
    } else if (method == "mfg_device_capability") {
        const json& capability = result.contains("capability") && result["capability"].is_object()
                                     ? result["capability"] : result;
        std::string id = text(result, "device_id");
        if (id.empty()) id = text(capability, "subject_id");
        if (!id.empty()) ingestCapability(upsertDevice(id), result);
    } else if (method == "mfg_telemetry_snapshot") {
        const std::string id = text(result, "device_id");
        if (!id.empty() && result.contains("snapshot")) upsertDevice(id).snapshot = parseMfgFresh(result["snapshot"], true);
    } else if (method == "mfg_run_list") {
        const auto rows = result.find("runs");
        if (rows != result.end() && rows->is_array()) for (const json& row : *rows) ingestRun(row);
    } else if (method == "mfg_run_describe") {
        ingestRun(result.contains("run") && result["run"].is_object() ? result["run"] : result);
    } else if (method == "mfg_gate_report" || method == "mfg_preflight_run") {
        const json& report = result.contains("gate_report") && result["gate_report"].is_object()
                                 ? result["gate_report"] : result;
        std::string runId = text(result, "run_id");
        if (runId.empty()) runId = text(report, "subject");
        if (!runId.empty()) ingestGate(upsertRun(runId).gates, result);
    } else if (method == "mfg_approval_propose" || method == "mfg_approval_create" ||
               method == "mfg_approval_inspect") {
        ingestApproval(result);
    }

    // Mutation acknowledgements may carry refreshed device/run rows. Consume
    // those projections without treating the acknowledgement as a transition.
    if (result.contains("device") && result["device"].is_object()) ingestDevice(result["device"]);
    if (result.contains("run") && result["run"].is_object()) ingestRun(result["run"]);
}

void MfgMirror::invalidateTelemetry(const std::string& why) {
    for (MfgDevice& row : devices_) {
        row.state = unknownFresh(why);
        row.snapshot = unknownFresh(why);
    }
    for (MfgRun& row : runs_) {
        row.state = unknownFresh(why);
        row.progress = unknownFresh(why);
        row.snapshot = unknownFresh(why);
    }
}

void MfgMirror::feedEvent(const json& event) {
    const std::string name = text(event, "event");
    const json& data = event.contains("data") && event["data"].is_object()
                           ? event["data"] : event;
    const std::uint64_t seq = std::max(u64(data, "seq"), u64(data, "at_seq"));
    if (seq > latestSeq_) latestSeq_ = seq;
    gatewayReachable_ = true;
    gatewayWhy_.clear();

    if (name == "mfg.gateway.epoch") {
        const std::uint64_t next = u64(data, "epoch");
        if (next != 0 && epoch_ != 0 && next != epoch_) invalidateTelemetry("gateway epoch changed");
        if (next != 0) epoch_ = next;
        return;
    }
    if (name == "mfg.device.resync") {
        invalidateTelemetry(text(data, "reason").empty() ? "device lane resync" : text(data, "reason"));
        return;
    }

    const std::string deviceId = text(data, "device_id");
    if (name == "mfg.device.state" && !deviceId.empty() && data.contains("state"))
        upsertDevice(deviceId).state = parseMfgFresh(data["state"], true);
    else if (name == "mfg.device.snapshot" && !deviceId.empty() && data.contains("snapshot"))
        upsertDevice(deviceId).snapshot = parseMfgFresh(data["snapshot"], true);
    else if (name == "mfg.device.capability_changed" && !deviceId.empty())
        ingestCapability(upsertDevice(deviceId), data);
    else if (name == "mfg.device.lease" && !deviceId.empty())
        ingestDevice(data);
    else if (name == "mfg.run.state")
        ingestRun(data);
    else if (name == "mfg.run.gate_report") {
        const json& report = data.contains("gate_report") && data["gate_report"].is_object()
                                 ? data["gate_report"] : data;
        std::string runId = text(data, "run_id");
        if (runId.empty()) runId = text(report, "subject");
        if (!runId.empty()) ingestGate(upsertRun(runId).gates, data);
    } else if (name == "mfg.approval.issued" || name == "mfg.approval.burned" ||
               name == "mfg.approval.revoked") {
        ingestApproval(data);
    }
}

void MfgMirror::markGatewayError(std::string why) {
    gatewayReachable_ = false;
    gatewayWhy_ = why.empty() ? "Manufacturing gateway unreachable." : std::move(why);
    invalidateTelemetry(gatewayWhy_);
}

void MfgMirror::seedManufactureShot() {
    devices_.clear();
    runs_.clear();
    gatewayReachable_ = true;
    gatewayWhy_.clear();
    epoch_ = 7;
    latestSeq_ = 1842;

    const auto fresh = [](json value, std::uint64_t seq, const char* source = "probed") {
        return json{{"v", std::move(value)}, {"state", "ok"}, {"t_mono_ns", 8100000000ULL},
                    {"seq", seq}, {"age_ms", 36}, {"ttl_ms", 500}, {"src", source}, {"why", ""}};
    };
    const auto level = [](const char* name, const char* truth, const char* why = "",
                          const char* missing = "") {
        json row{{"level", name}, {"truth", truth}, {"evidence_ref", json::array()},
                 {"why", why}};
        if (missing[0]) row["missing_level"] = missing;
        return row;
    };

    json levels = json::array({
        level("plan", "verified"), level("generate", "verified"),
        level("post", "declared", "Post profile was declared by commissioning."),
        level("parse", "verified"), level("simulate", "verified"),
        level("monitor", "verified"), level("transport", "verified"),
        level("execute", "assumed", "Execute has not been proven on this controller.", "execute"),
        level("safety_integration", "unsupported",
              "No software safety function. Hardwired E-stop, limits and interlocks are the only safety devices.")});
    ingestDevice({{"device_id", "dev:open_frame_router:2f7a91c40e12"},
                  {"machine_class", "open_frame_router"},
                  {"state", fresh("idle", 1827)},
                  {"snapshot", fresh({{"state", "idle"}, {"mpos", {12.4, -3.0, 18.2}},
                                       {"feed_rpm", {{"feed", 0}, {"spindle", 0}}}}, 1828)},
                  {"identity", {{"firmware_banner", "Grbl 1.1h"}}},
                  {"fingerprint_sha256", "2f7a91c40e12d90b"},
                  {"lease", {{"owner", ""}, {"lease_id", "lease:shot"}}},
                  {"transport", {{"kind", "emulator"}, {"endpoint_id", "shot"}, {"connected", true}}},
                  {"policy", {{"max_exercisable_by", "human_or_agent"}}},
                  {"capability", {{"levels", levels}}}});

    json guards = json::array({
        {{"guard", "controller_idle"}, {"verdict", "pass"}},
        {{"guard", "fixture_present"}, {"verdict", "fail"},
         {"why", "Fixture observation is unknown."},
         {"waiver", {{"by", "operator@example.com"},
                     {"reason", "Dry-run fixture clearance witnessed."}, {"at_seq", 1836}}}},
        {{"guard", "door_interlock"}, {"verdict", "not_applicable"},
         {"why", "Sensor unsupported for this open-frame machine class."}}});
    ingestRun({{"run_id", "run:shot:0042"},
               {"device_id", "dev:open_frame_router:2f7a91c40e12"},
               {"approval_id", ""}, {"proposal_id", "proposal:shot:9"},
               {"artifact_hash", "sha256:81c0bfa4e5317c64"},
               {"state", fresh("awaiting_approval", 1834)},
               {"progress", {{"v", nullptr}, {"state", "stale"}, {"t_mono_ns", 7900000000ULL},
                              {"seq", 1835}, {"age_ms", 4200}, {"ttl_ms", 500},
                              {"src", "probed"}, {"why", "telemetry sample expired"},
                              {"last_ok", {{"v", 0.42}, {"t_mono_ns", 7800000000ULL}, {"seq", 1833}}}}},
               {"gate_report", {{"report_ref", "result:mfg.gates:1836"}, {"seq", 1836},
                                {"stage1", {{"status", "admitted"}, {"why", "fresh probed evidence"}}},
                                {"stage2", guards}}},
               {"controls", {{"start", {{"enabled", false},
                                           {"reason", "Start unavailable — preflight gate fixture_present failed."}}},
                              {"hold", {{"enabled", true}}}}}});

    ingestApproval({{"approval", {{"proposal_id", "proposal:shot:9"},
                                   {"status", "waivers-present"},
                                   {"blocked_reason", ""},
                                   {"binding", {{"machine_fingerprint", "2f7a91c40e12d90b"},
                                                {"artifact_hash", "sha256:81c0bfa4e5317c64"},
                                                {"artifact_kind", "nc"},
                                                {"post_profile", "grbl-mm-v1"},
                                                {"job_revision", 12}, {"setup_revision", 4},
                                                {"gate_report_ref", "result:mfg.gates:1836"},
                                                {"evidence_set_digest", "sha256:1433ed7a"},
                                                {"max_starts", 1},
                                                {"exercisable_by", "human_only"}}}}}});
}

}  // namespace ui
