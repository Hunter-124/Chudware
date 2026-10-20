#include "hostd/mfg_lane.h"
#include "device/emulator_transport.h"
#include "manufacturing/gateway/transport_serial.h"
#include "manufacturing/gateway/transport_tcp.h"
#include "manufacturing/gateway/transport_ws.h"
#include "util/sha256.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <limits>
#include <stdexcept>
#include <set>

namespace chudware::mfg {
namespace {

using nlohmann::json;

std::filesystem::path defaultStateDirectory() {
    std::filesystem::path base;
    if (const char* configured = std::getenv("CHUDWARE_MFG_STATE_DIR"); configured && *configured)
        return configured;  // explicit override is taken verbatim
    if (const char* state = std::getenv("XDG_STATE_HOME"); state && *state)
        base = std::filesystem::path(state) / "chudware" / "manufacturing";
    else if (const char* home = std::getenv("HOME"); home && *home)
        base = std::filesystem::path(home) / ".local" / "state" / "chudware" / "manufacturing";
    else
        base = std::filesystem::temp_directory_path() / "chudware-manufacturing";
    // Scope by instance key so two daemons (e.g. the desktop hostd and a
    // test-spawned one) never contend for the same journal writer.lock —
    // that contention used to terminate the second daemon at startup.
    // The default instance keeps the bare directory for continuity.
    if (const char* key = std::getenv("CHUDWARE_INSTANCE_KEY"); key && *key)
        base /= key;
    return base;
}

device::ControllerIdentitySnapshot advertisedIdentity() {
    device::ControllerIdentitySnapshot identity;
    identity.firmware_banner = "Grbl 1.1h ['$' for help]";
    identity.build_options = "[OPT:VN,15,128];VER=chudware-emulator:1.0";
    identity.settings = {{"$0", "10"}, {"$1", "25"}, {"$100", "250.000"},
                         {"$101", "250.000"}, {"$102", "250.000"},
                         {"$130", "300.000"}, {"$131", "300.000"},
                         {"$132", "80.000"}};
    identity.axis_count = 3;
    identity.machine_class = device::MachineClass::Emulated;
    identity.transport_kind = "emulator";
    identity.transport_endpoint_id = "mfg-emulator";
    return identity;
}

device::GrblAdapterConfig adapterConfig() {
    device::GrblAdapterConfig config;
    config.machine_class = device::MachineClass::Emulated;
    config.freshness_ttl_ms = 600;
    return config;
}

std::string candidateId(device::MachineClass machine_class,
                        const gateway::EndpointIdentity& endpoint) {
    const auto fingerprint =
        device::MachineFingerprint::from_identity(
            json{{"machine_class", device::to_string(machine_class)},
                 {"endpoint", endpoint.key()}});
    return device::DeviceId::mint(machine_class, fingerprint).str();
}

std::string endpointAddress(const gateway::AllowlistEntry& entry) {
    if (entry.endpoint.transport == device::TransportKind::Serial)
        return entry.endpoint.address;
    std::string address = entry.endpoint.address + ":" + std::to_string(entry.endpoint.port);
    if (entry.endpoint.transport == device::TransportKind::WebSocket) {
        const auto path = entry.connection.find("path");
        address = "ws://" + address +
                  (path == entry.connection.end() ? std::string("/") : path->second);
    }
    return address;
}

bool printerClass(device::MachineClass machine_class) {
    return machine_class == device::MachineClass::FdmPrinterOpen ||
           machine_class == device::MachineClass::FdmPrinterEnclosed;
}

std::string base64Encode(std::string_view input) {
    static constexpr char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve((input.size() + 2) / 3 * 4);
    std::uint32_t value = 0;
    int bits = -6;
    for (const unsigned char byte : input) {
        value = (value << 8) | byte;
        bits += 8;
        while (bits >= 0) {
            out.push_back(alphabet[(value >> bits) & 0x3f]);
            bits -= 6;
        }
    }
    if (bits > -6) out.push_back(alphabet[((value << 8) >> (bits + 8)) & 0x3f]);
    while (out.size() % 4) out.push_back('=');
    return out;
}

std::string_view discontinuityName(device::RunDiscontinuity value) {
    switch (value) {
        case device::RunDiscontinuity::None: return "none";
        case device::RunDiscontinuity::Reset: return "reset";
        case device::RunDiscontinuity::Alarm: return "alarm";
        case device::RunDiscontinuity::HardLimit: return "hard_limit";
        case device::RunDiscontinuity::Disconnect: return "disconnect";
        case device::RunDiscontinuity::PowerLoss: return "power_loss";
    }
    return "unknown";
}

void stripWorkerAuthority(json& value) {
    if (value.is_array()) {
        for (auto& item : value) stripWorkerAuthority(item);
        return;
    }
    if (!value.is_object()) return;
    static const std::set<std::string, std::less<>> forbidden{
        "seq", "at_seq", "result_seq", "approval", "approval_id", "lease",
        "lease_id", "authorization", "authorization_token", "capability"};
    for (auto it = value.begin(); it != value.end();) {
        if (forbidden.find(it.key()) != forbidden.end()) it = value.erase(it);
        else {
            stripWorkerAuthority(it.value());
            ++it;
        }
    }
}

json policyJson(const MachinePolicy& policy) {
    json attendance = json::array();
    for (const auto mode : policy.attendance_allowed) attendance.push_back(to_string(mode));
    return {{"machine_class", device::to_string(policy.machine_class)},
            {"attendance_allowed", std::move(attendance)},
            {"max_exercisable_by", to_string(policy.max_exercisable_by)},
            {"spindle_guard_required", policy.spindle_guard_required},
            {"feedback_relay_commissioned", policy.feedback_relay_commissioned},
            {"emulator_stage_passed", "E3"}, {"revision", policy.revision}};
}


json leaseJson(const CommandLease& lease) {
    return {{"lease_id", lease.lease_id}, {"device_id", lease.device_id},
            {"owner", {{"client_kind", lease.owner.client_kind}, {"seat", lease.owner.seat},
                       {"session", lease.owner.session}}},
            {"role", lease.role}, {"granted_at_seq", lease.granted_at_seq},
            {"ttl_ms", lease.ttl_ms}, {"heartbeat_ms", lease.heartbeat_ms},
            {"expires_at_mono_ns", lease.expires_at_mono_ns},
            {"transport_kind", lease.transport_kind},
            {"exclusive_endpoint", lease.exclusive_endpoint}};
}


json initiatorJson(const InitiatorProvenance& value) {
    json out{{"kind", value.kind == gates::InitiatorKind::Human ? "human" : "agent"},
             {"identity", value.identity}, {"seat", value.seat}, {"at_seq", value.at_seq}};
    if (value.agent_run_id) out["agent_run_id"] = *value.agent_run_id;
    return out;
}

}  // namespace
ManufacturingLane::SteadyClock::SteadyClock(std::function<uint64_t()> source)
    : source_(std::move(source)) {}


uint64_t ManufacturingLane::SteadyClock::mono_ns() const {
    const uint64_t base = source_
        ? source_()
        : static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
              std::chrono::steady_clock::now().time_since_epoch()).count());
    return base + offset_ns_.load();
}

ManufacturingLane::ManufacturingLane(std::filesystem::path state_directory,
                                     bool emulator_endpoint_is_physical,
                                     std::function<uint64_t()> monotonic_clock,
                                     ArtifactLimits artifact_limits)
    : epoch_(device::LaneEpoch::mint("manufacturing")),
      journal_((std::filesystem::create_directories(state_directory), state_directory), epoch_),
      allowlist_(state_directory / "device_allowlist.tsv"),
      observed_allowlist_epoch_(allowlist_.epoch()),
      clock_(std::move(monotonic_clock)),
      arbiter_(journal_, clock_, nonces_), leases_(journal_),
      result_registry_(ResultRegistry::manufacturingDefaults()),
      emulator_identity_(advertisedIdentity()),
      emulator_fingerprint_(device::MachineFingerprint::from_identity(emulator_identity_)),
      current_identity_(emulator_identity_),
      current_machine_fingerprint_(emulator_fingerprint_),
      device_id_(device::DeviceId::mint(device::MachineClass::Emulated, emulator_fingerprint_)),
      endpoint_{"mfg-emulator", "inproc://mfg-emulator", emulator_endpoint_is_physical},
      artifact_limits_(artifact_limits) {
    const ArtifactLimits ceiling;
    if (!artifact_limits_.max_objects || artifact_limits_.max_objects > ceiling.max_objects ||
        !artifact_limits_.max_retained_bytes ||
        artifact_limits_.max_retained_bytes > ceiling.max_retained_bytes ||
        !artifact_limits_.max_artifact_bytes ||
        artifact_limits_.max_artifact_bytes > ceiling.max_artifact_bytes ||
        !artifact_limits_.max_artifact_lines ||
        artifact_limits_.max_artifact_lines > ceiling.max_artifact_lines)
        throw std::invalid_argument("artifact limits must be positive and within server ceilings");
    DeviceCandidate emulator{device_id_.str(), device::MachineClass::Emulated,
                             device::TransportKind::Emulator, endpoint_, true, std::nullopt};
    selectTransport(emulator);
    appendEvent("mfg.gateway.epoch", "manufacturing",
        {{"epoch", epoch_}, {"allowlist_epoch", observed_allowlist_epoch_},
         {"process_start_id", epoch_.process_start_id},
         {"epoch_floor_seq", journal_.epochFloor()},
         {"reconciled", {{"falsified", 0}, {"superseded", 0}, {"invariant", 0}}}});
    restoreAttention();
}

ManufacturingLane::~ManufacturingLane() {
    if (isPrinter() && connected_ && worker_call_) {
        try {
            worker_call_("mfg_printer_adapter_close",
                         {{"adapter_id", device_id_.str()}});
        } catch (...) {
        }
    }
    if (adapter_) adapter_->close();
    else if (transport_) transport_->close();
}

void ManufacturingLane::setEventSink(EventSink sink) {
    std::lock_guard lock(mutex_);
    event_sink_ = std::move(sink);
}

void ManufacturingLane::setWorkerCall(WorkerCall call) {
    std::lock_guard lock(mutex_);
    worker_call_ = std::move(call);
}

std::optional<LaneRpcResult> ManufacturingLane::handle(std::string_view method,
                                                        const json& params,
                                                        const LeaseOwner& caller) {
    if (!method.starts_with("mfg_")) return std::nullopt;
    std::lock_guard lock(mutex_);
    try {
        if (!params.is_object()) return refuse("mfg_invalid_params", "params must be an object");
        std::string reload_why;
        if (!allowlist_.reload(&reload_why))
            return refuse("mfg_device_allowlist_invalid", reload_why);
        reconcileAllowlistEpoch();
        failExpiredClaimSafely();
        return route(method, params, caller);
    } catch (const std::invalid_argument& error) {
        return refuse("mfg_invalid_params", error.what());
    } catch (const json::exception& error) {
        return refuse("mfg_invalid_params", error.what());
    } catch (const std::exception& error) {
        return refuse("mfg_internal_error", error.what());
    }
}

LaneRpcResult ManufacturingLane::ok(json fields) const {
    if (!fields.is_object()) fields = json{{"value", std::move(fields)}};
    fields["result_seq"] = journal_.head();
    fields["epoch"] = epoch_;
    return {std::move(fields), false};
}

LaneRpcResult ManufacturingLane::refuse(std::string code, std::string message,
                                         json detail) const {
    json body{{"code", std::move(code)}, {"message", std::move(message)},
              {"result_seq", journal_.head()}, {"epoch", epoch_}};
    if (!detail.empty()) body["detail"] = std::move(detail);
    return {std::move(body), true};
}

std::vector<ManufacturingLane::DeviceCandidate> ManufacturingLane::deviceCandidates() const {
    std::vector<DeviceCandidate> candidates;
    candidates.push_back({device::DeviceId::mint(device::MachineClass::Emulated,
                                                  emulator_fingerprint_).str(),
                          device::MachineClass::Emulated, device::TransportKind::Emulator,
                          {"mfg-emulator", "inproc://mfg-emulator",
                           endpoint_.endpoint_is_physical &&
                               transport_kind_ == device::TransportKind::Emulator},
                          true, std::nullopt});

    std::set<std::string, std::less<>> endpoint_keys;
    for (const auto& entry : allowlist_.entries()) {
        endpoint_keys.insert(entry.endpoint.key());
        DeviceCandidate candidate;
        candidate.device_id = candidateId(entry.machine_class, entry.endpoint);
        candidate.machine_class = entry.machine_class;
        candidate.transport_kind = entry.endpoint.transport;
        candidate.endpoint = {candidate.device_id, endpointAddress(entry), true};
        candidate.allowlisted = true;
        candidate.allowlist_entry = entry;
        candidates.push_back(std::move(candidate));
    }

    for (const auto& serial : gateway::SerialTransport::enumerate()) {
        if (endpoint_keys.find(serial.identity.key()) != endpoint_keys.end()) continue;
        gateway::CommissioningCandidate observed;
        observed.endpoint = serial.identity;
        observed.machine_class = device::MachineClass::OpenFrameMill;
        const auto authorization = allowlist_.authorize(observed);
        DeviceCandidate candidate;
        candidate.machine_class = authorization.entry
            ? authorization.entry->machine_class : observed.machine_class;
        candidate.device_id = candidateId(candidate.machine_class, serial.identity);
        candidate.transport_kind = device::TransportKind::Serial;
        candidate.endpoint = {candidate.device_id, serial.identity.address,
                              serial.endpoint_is_physical};
        candidate.allowlisted = authorization.allowed;
        candidate.allowlist_entry = authorization.entry;
        candidates.push_back(std::move(candidate));
    }
    return candidates;
}

std::optional<ManufacturingLane::DeviceCandidate> ManufacturingLane::findCandidate(
    std::string_view requested) const {
    for (const auto& candidate : deviceCandidates())
        if (candidate.device_id == requested) return candidate;
    return std::nullopt;
}

void ManufacturingLane::selectTransport(const DeviceCandidate& candidate) {
    const std::string previous_device_id = device_id_.str();
    if (!previous_device_id.empty() && previous_device_id != candidate.device_id) {
        invalidateSelectedDeviceRecords(previous_device_id);
        reclaimUnreferencedArtifacts(previous_device_id);
    }
    if (adapter_) adapter_->close();
    else if (transport_) transport_->close();
    adapter_.reset();
    transport_.reset();
    connected_ = false;
    terminal_claim_ = false;
    printer_capability_ = json();
    selected_allowlist_entry_ = candidate.allowlist_entry;
    machine_class_ = candidate.machine_class;
    transport_kind_ = candidate.transport_kind;
    endpoint_ = candidate.endpoint;
    const auto parsed_id = device::DeviceId::parse(candidate.device_id);
    if (!parsed_id) throw std::invalid_argument("candidate has invalid device_id");
    device_id_ = *parsed_id;

    current_identity_ = emulator_identity_;
    current_identity_.machine_class = machine_class_;
    current_identity_.transport_kind = std::string(device::to_string(transport_kind_));
    current_identity_.transport_endpoint_id = endpoint_.endpoint_id;
    current_machine_fingerprint_ =
        transport_kind_ == device::TransportKind::Emulator
            ? std::optional<device::MachineFingerprint>(emulator_fingerprint_)
            : std::nullopt;

    switch (transport_kind_) {
        case device::TransportKind::Emulator:
            transport_ = std::make_unique<device::EmulatorTransport>();
            break;
        case device::TransportKind::Serial:
            transport_ = std::make_unique<gateway::SerialTransport>(allowlist_, machine_class_);
            break;
        case device::TransportKind::Tcp:
            transport_ = std::make_unique<gateway::TcpTransport>(allowlist_, machine_class_);
            break;
        case device::TransportKind::WebSocket:
            transport_ = std::make_unique<gateway::WebSocketTransport>(allowlist_, machine_class_);
            break;
        default:
            throw std::invalid_argument("allowlisted transport is not command-capable");
    }
    if (!isPrinter()) {
        auto config = adapterConfig();
        config.machine_class = machine_class_;
        adapter_ = std::make_unique<device::GrblAdapter>(
            *transport_, [this] { return clock_.mono_ns(); }, std::move(config),
            "manufacturing");
    }
}

void ManufacturingLane::reclaimUnreferencedArtifacts(std::string_view device_id) {
    for (auto it = artifacts_.begin(); it != artifacts_.end();) {
        if (it->second.device_id == device_id && it->second.run_references == 0) {
            retained_artifact_bytes_ -= it->second.retained_bytes;
            it = artifacts_.erase(it);
        } else {
            ++it;
        }
    }
}

void ManufacturingLane::invalidateSelectedDeviceRecords(
    std::string_view previous_device_id) {
    std::vector<std::string> affected;
    for (auto& [approval_id, approval] : approvals_) {
        if (approval.machine.device_id != previous_device_id) continue;
        approval.nonce.state = NonceState::Void;
        affected.push_back(approval_id);
    }
    for (auto& [run_id, run] : runs_) {
        if (run.device_id != previous_device_id) continue;
        const bool active = run.state == device::RunState::Running ||
                            run.state == device::RunState::Holding ||
                            run.state == device::RunState::Held ||
                            run.state == device::RunState::Aborting;
        if (active) arbiter_.record_discontinuity(
            run_id, device::RunDiscontinuity::Disconnect);
        run.discontinuity = active ? device::RunDiscontinuity::Disconnect
                                  : device::RunDiscontinuity::None;
        run.state = active ? device::RunState::Faulted : device::RunState::Abandoned;
        affected.push_back(run_id);
    }
    std::erase_if(proposals_, [&](const auto& item) {
        const auto run = runs_.find(item.second.run_id);
        return run != runs_.end() && run->second.device_id == previous_device_id;
    });
    if (affected.empty()) return;
    InvalidationNotice notice;
    notice.classification = InvalidationClass::Falsified;
    notice.reason = "selected_device_changed";
    notice.why = "device-scoped authorization was invalidated when command ownership selected another device";
    notice.affected = std::move(affected);
    notice.at_seq = journal_.head() + 1;
    notice.at_seq = appendEvent("mfg.result.falsified",
                                std::string(previous_device_id), notice);
    invalidations_.push_back(std::move(notice));
}

void ManufacturingLane::observeMachineIdentity(
    device::ControllerIdentitySnapshot identity,
    device::MachineFingerprint fingerprint) {
    if (current_machine_fingerprint_ &&
        current_machine_fingerprint_->str() != fingerprint.str()) {
        std::vector<std::string> affected;
        for (auto& [approval_id, approval] : approvals_) {
            if (approval.machine.device_id != device_id_.str() ||
                approval.nonce.state == NonceState::Void) continue;
            approval.nonce.state = NonceState::Void;
            affected.push_back(approval_id);
            const auto run = runs_.find(approval.run_id);
            if (run != runs_.end()) {
                run->second.approval_id.clear();
                run->second.gate_report_ref.clear();
                run->second.state = device::RunState::Draft;
                affected.push_back(run->first);
            }
        }
        if (!affected.empty()) {
            InvalidationNotice notice;
            notice.classification = InvalidationClass::Falsified;
            notice.reason = "observed_machine_fingerprint_changed";
            notice.why = "the controller identified at connect no longer matches the approved machine";
            notice.affected = std::move(affected);
            notice.at_seq = journal_.head() + 1;
            notice.at_seq = appendEvent("mfg.result.falsified",
                                        device_id_.str(), notice);
            invalidations_.push_back(std::move(notice));
        }
    }
    current_identity_ = std::move(identity);
    current_machine_fingerprint_ = std::move(fingerprint);
}

void ManufacturingLane::failExpiredClaimSafely() {
    if (!active_claim_) return;
    const auto current = leases_.describe(device_id_.str(), clock_.mono_ns());
    if (current && current->lease_id == active_claim_->lease_id) {
        active_claim_ = *current;
        return;
    }

    const CommandLease expired = *active_claim_;
    if (connected_) {
        if (isPrinter() && worker_call_) {
            try {
                worker_call_("mfg_printer_adapter_cancel",
                             {{"adapter_id", device_id_.str()}});
            } catch (...) {
            }
        } else if (adapter_) {
            (void)adapter_->abort();
        }
        closeActiveDevice("commander lease expired");
    }
    terminal_claim_ = true;
    std::vector<std::string> affected{expired.lease_id, device_id_.str()};
    for (auto& [run_id, run] : runs_) {
        if (run.device_id != device_id_.str()) continue;
        const bool active = run.state == device::RunState::Running ||
                            run.state == device::RunState::Holding ||
                            run.state == device::RunState::Held ||
                            run.state == device::RunState::Aborting;
        if (!active) continue;
        arbiter_.record_discontinuity(run_id, device::RunDiscontinuity::Disconnect);
        run.discontinuity = device::RunDiscontinuity::Disconnect;
        transitionRun(run, device::RunState::Faulted, "commander_lease_expired");
        affected.push_back(run_id);
    }
    InvalidationNotice notice;
    notice.classification = InvalidationClass::Falsified;
    notice.reason = "commander_lease_expired";
    notice.why = "the authenticated command owner stopped renewing its lease; the active device was failed closed";
    notice.affected = std::move(affected);
    notice.at_seq = journal_.head() + 1;
    notice.at_seq = appendEvent("mfg.result.falsified", expired.lease_id, notice);
    invalidations_.push_back(std::move(notice));
    active_claim_.reset();
    reclaimUnreferencedArtifacts(device_id_.str());
}

MachinePolicy ManufacturingLane::lanePolicy() const {
    auto policy = default_policy(machine_class_);
    if (!endpoint_.endpoint_is_physical)
        policy.max_exercisable_by = ExercisableBy::HumanOrAgent;
    return policy;
}

Initiator ManufacturingLane::authenticatedInitiator(const LeaseOwner& caller) const {
    Initiator out;
    // hostd classifies all raw IPC peers as `raw`; direct in-process callers may
    // supply an already-attested `human` owner in focused tests or future trusted UI glue.
    out.kind = caller.client_kind == "human"
        ? gates::InitiatorKind::Human : gates::InitiatorKind::Agent;
    out.identity = caller.seat;
    out.seat = caller.seat;
    if (out.kind == gates::InitiatorKind::Agent) out.agent_run_id = caller.session;
    return out;
}

bool ManufacturingLane::isPrinter() const {
    return printerClass(machine_class_);
}

json ManufacturingLane::workerParams() const {
    json result{{"adapter_id", device_id_.str()}};
    std::string adapter_kind = "moonraker";
    json auth{{"mode", "trusted_host"}};
    std::string address_class;
    std::string base_url = endpoint_.address;
    if (base_url.rfind("ws://", 0) == 0) base_url.replace(0, 5, "http://");
    else if (base_url.rfind("wss://", 0) == 0) base_url.replace(0, 6, "https://");
    else if (base_url.find("://") == std::string::npos) base_url = "http://" + base_url;
    if (selected_allowlist_entry_) {
        const auto& connection = selected_allowlist_entry_->connection;
        auto field = connection.find("adapter_kind");
        if (field == connection.end()) field = connection.find("adapter");
        if (field != connection.end()) adapter_kind = field->second;
        if ((field = connection.find("auth_mode")) != connection.end())
            auth["mode"] = field->second;
        if ((field = connection.find("api_key")) != connection.end())
            auth["api_key"] = field->second;
        if ((field = connection.find("username")) != connection.end())
            auth["username"] = field->second;
        if ((field = connection.find("password")) != connection.end())
            auth["password"] = field->second;
        if ((field = connection.find("address_class")) != connection.end())
            address_class = field->second;
    }
    result["kind"] = std::move(adapter_kind);
    result["endpoint"] = {{"base_url", base_url},
                          {"expected_base_url", std::move(base_url)},
                          {"endpoint_is_physical", endpoint_.endpoint_is_physical},
                          {"origin", endpoint_.endpoint_is_physical ? "live" : "virtual"}};
    if (!address_class.empty())
        result["endpoint"]["address_class"] = std::move(address_class);
    result["auth"] = std::move(auth);
    return result;
}

std::optional<json> ManufacturingLane::callWorker(const std::string& method,
                                                   const json& params,
                                                   LaneRpcResult& refusal) {
    if (!worker_call_) {
        refusal = refuse("mfg_printer_worker_unavailable",
                         method + " worker callback is not configured",
                         {{"method", method}});
        return std::nullopt;
    }
    json result;
    try {
        result = worker_call_(method, params);
    } catch (const std::exception& error) {
        refusal = refuse("mfg_printer_worker_failed", method + " failed",
                         {{"method", method}, {"why", error.what()}});
        return std::nullopt;
    }
    if (!result.is_object() || !result.value("ok", false)) {
        refusal = refuse("mfg_printer_worker_refused", method + " refused",
                         {{"method", method},
                          {"why", result.is_object()
                              ? result.value("message", result.value("error", "worker refusal"))
                              : "worker result was not an object"}});
        return std::nullopt;
    }
    return result;
}

void ManufacturingLane::closeActiveDevice(std::string_view why) {
    if (isPrinter() && connected_ && worker_call_) {
        try {
            worker_call_("mfg_printer_adapter_close", workerParams());
        } catch (...) {
        }
    }
    if (adapter_) adapter_->close();
    else if (transport_) transport_->close();
    connected_ = false;
    appendEvent("mfg.device.state", device_id_.str(),
                {{"device_id", device_id_.str()}, {"state", deviceState()},
                 {"raw", std::string(why)}});
}

void ManufacturingLane::reconcileAllowlistEpoch() {
    if (allowlist_.epoch() == observed_allowlist_epoch_) return;
    observed_allowlist_epoch_ = allowlist_.epoch();
    ++epoch_.epoch;
    const auto active = leases_.describe(device_id_.str(), clock_.mono_ns());
    if (active) {
        closeActiveDevice("allowlist epoch changed");
        const auto released = leases_.release(active->lease_id, active->owner, clock_.mono_ns());
        (void)released;
        active_claim_.reset();
        InvalidationNotice notice;
        notice.classification = InvalidationClass::Falsified;
        notice.reason = "allowlist_epoch_changed";
        notice.why = "commissioning authorization changed; the active claim and its evidence are false";
        notice.affected = {active->lease_id, device_id_.str()};
        notice.at_seq = journal_.head() + 1;
        notice.at_seq = appendEvent("mfg.result.falsified", active->lease_id, notice);
        invalidations_.push_back(std::move(notice));
    }
    appendEvent("mfg.gateway.epoch", "manufacturing",
                {{"epoch", epoch_}, {"allowlist_epoch", observed_allowlist_epoch_},
                 {"active_claim_falsified", active.has_value()}});
}

void ManufacturingLane::applyAttention(const JournalRecord& record) {
    if (record.kind != "mfg.attention" || !record.body.is_object()) return;
    const std::string action = record.body.value("action", std::string{});
    const std::string id = record.body.value("id", record.subject);
    if (id.empty()) return;
    auto found = attention_.find(id);
    if (found != attention_.end() &&
        record.seq <= found->second.value("last_event_seq", device::Seq{0})) return;
    if (action == "raised" || action == "occurred") {
        if (found == attention_.end() && attention_.size() >= 1024)
            throw std::length_error("attention store capacity exceeded");
        json item = record.body.contains("attention")
            ? record.body.at("attention") : record.body;
        item.erase("action");
        item["id"] = id;
        if (found != attention_.end()) {
            const auto count = found->second.value("occurrence_count", 1U);
            if (count >= 1024) throw std::length_error("attention occurrence capacity exceeded");
            item["raised_seq"] = found->second.at("raised_seq");
            item["occurrence_count"] = count + 1;
            item["deliveries"] = found->second.at("deliveries");
        } else {
            item["raised_seq"] = record.seq;
            item["occurrence_count"] = 1;
            item["deliveries"] = json::array();
        }
        item["delivery_count"] = item.at("deliveries").size();
        item["last_event_seq"] = record.seq;
        attention_[id] = std::move(item);
    } else if (found != attention_.end()) {
        if (action == "acknowledged") {
            if (!found->second.contains("acknowledged_seq")) {
                found->second["acknowledged_by"] = record.body.value("by", std::string{});
                found->second["acknowledged_seq"] = record.seq;
            }
        } else if (action == "resolved") {
            if (!found->second.contains("resolved_seq"))
                found->second["resolved_seq"] = record.seq;
        } else if (action == "delivery") {
            auto& deliveries = found->second["deliveries"];
            if (deliveries.size() >= 64)
                throw std::length_error("attention delivery capacity exceeded");
            json delivery = record.body.at("delivery");
            delivery["seq"] = record.seq;
            deliveries.push_back(std::move(delivery));
            found->second["delivery_count"] = deliveries.size();
        }
        found->second["last_event_seq"] = record.seq;
    }
}

void ManufacturingLane::restoreAttention() {
    attention_.clear();
    journal_.visit(1, [&](const JournalRecord& record) {
        applyAttention(record);
        return true;
    });
}

bool ManufacturingLane::validDevice(const json& params, LaneRpcResult& refusal) const {
    const std::string requested = params.value("device_id", device_id_.str());
    if (requested == device_id_.str()) return true;
    refusal = refuse("mfg_device_not_found", "device_id is not owned by this lane",
                     {{"device_id", requested}});
    return false;
}

bool ManufacturingLane::requireLease(const json& params, const LeaseOwner& caller,
                                     LaneRpcResult& refusal) {
    if (!validDevice(params, refusal)) return false;
    const std::string lease_id = params.value("lease_id", std::string{});
    if (lease_id.empty() ||
        !leases_.held_by(device_id_.str(), caller, clock_.mono_ns())) {
        refusal = refuse("lease.commander_held_by_caller",
                         "write requires the caller's commander lease",
                         {{"prerequisite", "lease.commander_held_by_caller"}});
        return false;
    }
    const auto active = leases_.describe(device_id_.str(), clock_.mono_ns());
    if (!active || active->lease_id != lease_id) {
        refusal = refuse("lease.commander_held_by_caller",
                         "lease is absent, expired, or not bound to this caller",
                         {{"prerequisite", "lease.commander_held_by_caller"}});
        return false;
    }
    return true;
}

bool ManufacturingLane::requireCommander(const LeaseOwner& caller,
                                          LaneRpcResult& refusal) {
    if (leases_.held_by(device_id_.str(), caller, clock_.mono_ns())) return true;
    refusal = refuse("lease.commander_held_by_caller",
                     "operation requires the caller's commander lease",
                     {{"prerequisite", "lease.commander_held_by_caller"}});
    return false;
}

void ManufacturingLane::publishEvent(std::string_view kind, std::string_view subject,
                                      device::Seq seq, const json& body) {
    if (!event_sink_) return;
    json data = body;
    if (!data.is_object()) data = json{{"value", data}};
    data["seq"] = seq;
    data["at_seq"] = seq;
    data["subject"] = subject;
    event_sink_(json{{"event", kind}, {"data", std::move(data)}}.dump());
}

device::Seq ManufacturingLane::appendEvent(std::string kind, std::string subject,
                                            json body, json raw) {
    if (kind == "mfg.attention" && body.is_object()) {
        const auto action = body.value("action", std::string{});
        if (action == "raised" || action == "occurred") {
            const auto id = body.value("id", subject);
            const auto existing = attention_.find(id);
            if (existing == attention_.end()) {
                if (attention_.size() >= 1024)
                    throw std::length_error("attention store capacity exceeded");
                body["occurrence_count"] = 1;
                body["raised_seq"] = journal_.head() + 1;
                body["deliveries"] = json::array();
            } else {
                const auto count = existing->second.value("occurrence_count", 1U);
                if (count >= 1024)
                    throw std::length_error("attention occurrence capacity exceeded");
                body["occurrence_count"] = count + 1;
                body["raised_seq"] = existing->second.at("raised_seq");
                body["deliveries"] = existing->second.at("deliveries");
            }
            body["delivery_count"] = body.at("deliveries").size();
        }
    }
    const auto seq = journal_.append(kind, subject, body, std::move(raw));
    publishEvent(kind, subject, seq, body);
    return seq;
}

void ManufacturingLane::transitionRun(RunRecord& run, device::RunState state,
                                      std::string_view cause) {
    const auto outcome = arbiter_.observe_state(run.run_id, state, cause);
    if (!outcome.ok)
        throw std::logic_error("run/arbiter state diverged: " + outcome.why);
    run.state = state;
    const auto records = journal_.replay(*outcome.at_seq);
    for (const auto& record : records)
        publishEvent(record.kind, record.subject, record.seq, record.body);
}

void ManufacturingLane::raiseRunAttention(std::string_view run_id,
                                          std::string_view reason,
                                          std::string_view why) {
    const std::string id = "attention:" + device::detail::mint_ulid();
    json attention{{"action", "raised"}, {"id", id}, {"severity", "critical"},
                   {"device", device_id_.str()}, {"run", run_id},
                   {"reason", reason}, {"why", why},
                   {"raised_seq", journal_.head() + 1},
                   {"occurrence_count", 1}, {"deliveries", json::array()}};
    const auto seq = appendEvent("mfg.attention", id, attention);
    JournalRecord record;
    record.seq = seq;
    record.kind = "mfg.attention";
    record.subject = id;
    record.body = std::move(attention);
    applyAttention(record);
}

void ManufacturingLane::serviceDevice() {
    if (!connected_ || !adapter_) return;
    adapter_->service(journal_.head());
    const auto discontinuity = adapter_->discontinuity();
    const auto progress = adapter_->streamProgress();
    const auto observed = adapter_->snapshot();
    if (discontinuity != device::RunDiscontinuity::None) {
        terminal_claim_ = true;
        if (discontinuity == device::RunDiscontinuity::Disconnect)
            connected_ = false;
    }
    for (auto& [run_id, run] : runs_) {
        const bool active = run.state == device::RunState::Running ||
                            run.state == device::RunState::Holding ||
                            run.state == device::RunState::Held ||
                            run.state == device::RunState::Aborting;
        if (!active) continue;
        if (discontinuity != device::RunDiscontinuity::None) {
            run.discontinuity = discontinuity;
            arbiter_.record_discontinuity(run_id, discontinuity);
            const auto state = run.state == device::RunState::Aborting &&
                                       discontinuity == device::RunDiscontinuity::Reset
                                   ? device::RunState::Aborted
                                   : device::RunState::Faulted;
            transitionRun(run, state, discontinuityName(discontinuity));
            continue;
        }
        if (run.state == device::RunState::Holding &&
            observed.state == FreshState::Ok && observed.v &&
            observed.v->state == device::DeviceState::Hold &&
            observed.v->substate == "0" &&
            observed.t_mono_ns > run.hold_requested_at_ns)
            transitionRun(run, device::RunState::Held, "hold_observed");
        if (run.state == device::RunState::Running && progress.complete) {
            if (!run.completion_observed) {
                run.completion_observed = true;
                continue;
            }
            transitionRun(run, device::RunState::Completing, "program_stream_complete");
            transitionRun(run, device::RunState::Completed, "controller_acknowledged_program");
        }
    }
}

device::CapabilityReport ManufacturingLane::capabilityReport() const {
    device::CapabilityReport report;
    report.subject_id = device_id_.str();
    report.max_advertised = endpoint_.endpoint_is_physical
        ? device::CapabilityLevel::Transport : device::CapabilityLevel::Execute;
    report.emulator_stage_passed = device::EmulatorStage::E3;
    for (const auto level : device::capability_levels) {
        device::CapabilityTruth truth;
        truth.level = level;
        truth.emulator_stage = device::EmulatorStage::E3;
        if (level == device::CapabilityLevel::SafetyIntegration) {
            truth.truth = device::CapabilityTruthValue::Unsupported;
            truth.why = "the endpoint has no commissioned physical safety integration";
        } else if (level == device::CapabilityLevel::Execute &&
                   endpoint_.endpoint_is_physical) {
            truth.truth = device::CapabilityTruthValue::Assumed;
            truth.missing_level = device::CapabilityLevel::Execute;
            truth.why = "physical execute requires an attended hardware pilot";
        } else {
            truth.truth = device::CapabilityTruthValue::Verified;
            truth.evidence_ref = transport_kind_ == device::TransportKind::Emulator
                ? std::vector<std::string>{"res:emulator_e3:built_in"}
                : std::vector<std::string>{"res:commissioned_transport"};
        }
        report.levels.emplace(level, std::move(truth));
    }
    return report;
}

json ManufacturingLane::capabilityJson() const {
    json capability = capabilityReport();
    if (isPrinter() && printer_capability_.is_object()) {
        capability = printer_capability_;
        capability.erase("seq");
        capability.erase("at_seq");
        capability.erase("approval");
        capability.erase("approval_id");
        capability.erase("lease");
        capability.erase("lease_id");
        capability["subject_id"] = device_id_.str();
        const json base = capabilityReport();
        if (capability.contains("levels") && capability["levels"].is_array()) {
            json normalized = json::object();
            for (const auto& item : capability["levels"]) {
                if (!item.is_object() || !item.contains("level") ||
                    !item["level"].is_string()) continue;
                normalized[item["level"].get<std::string>()] = item;
            }
            capability["levels"] = std::move(normalized);
        }
        if (!capability.contains("levels") || !capability["levels"].is_object())
            capability["levels"] = base["levels"];
        for (const auto level : device::capability_levels) {
            const std::string name(device::to_string(level));
            if (!capability["levels"].contains(name))
                capability["levels"][name] = base["levels"][name];
        }
    }
    if (endpoint_.endpoint_is_physical) {
        capability["max_advertised"] = "transport";
        capability["levels"]["execute"] =
            {{"level", "execute"}, {"truth", "assumed"}, {"evidence_ref", json::array()},
             {"emulator_stage", "E3"}, {"missing_level", "execute"},
             {"why", "physical execute requires an attended hardware pilot"}};
    } else {
        const json base = capabilityReport();
        capability["max_advertised"] = "execute";
        capability["levels"]["execute"] = base["levels"]["execute"];
    }
    capability["levels"]["safety_integration"] =
        {{"level", "safety_integration"}, {"truth", "unsupported"},
         {"evidence_ref", json::array()}, {"emulator_stage", "E3"},
         {"why", "the endpoint has no commissioned physical safety integration"}};
    return capability;
}

FreshValue<device::DeviceState> ManufacturingLane::deviceState() const {
    FreshValue<device::DeviceState> state;
    if (adapter_) {
        const auto snapshot = adapter_->snapshot();
        state.t_mono_ns = snapshot.t_mono_ns;
        state.seq = snapshot.seq;
        state.age_ms = snapshot.age_ms;
        state.ttl_ms = snapshot.ttl_ms;
        state.src = snapshot.src;
        state.state = snapshot.state;
        state.why = snapshot.why;
        if (snapshot.v) state.v = snapshot.v->state;
        if (snapshot.last_ok) state.last_ok = LastOk<device::DeviceState>{
            snapshot.last_ok->v.state, snapshot.last_ok->t_mono_ns, snapshot.last_ok->seq};
    } else if (connected_) {
        state.v = device::DeviceState::Idle;
        state.t_mono_ns = clock_.mono_ns();
        state.seq = journal_.head();
        state.age_ms = 0;
        state.ttl_ms = 5000;
        state.src = endpoint_.endpoint_is_physical ? FreshSource::Probed : FreshSource::Simulated;
        state.state = FreshState::Ok;
    }
    if (!connected_) {
        state.v.reset();
        state.state = FreshState::Unknown;
        state.src = FreshSource::Declared;
        state.ttl_ms.reset();
        state.why = std::string(device::to_string(transport_kind_)) +
                    " transport is not connected";
    }
    state.normalize();
    return state;
}

json ManufacturingLane::artifactJson(const ArtifactRecord& artifact) const {
    return {{"artifact_id", artifact.artifact_id}, {"device_id", artifact.device_id},
            {"kind", tier0::to_string(artifact.kind)}, {"hash_sha256", artifact.hash_sha256},
            {"bytes", artifact.bytes}, {"program_lines", artifact.program_lines.size()},
            {"post_profile_id", artifact.post_profile_id},
            {"post_profile_hash", artifact.post_profile_hash},
            {"slicer_profile_hash", artifact.slicer_profile_hash}, {"seq", artifact.seq}};
}

json ManufacturingLane::runJson(const RunRecord& run) const {
    json value{{"run_id", run.run_id}, {"device_id", run.device_id},
               {"artifact_id", run.artifact_id}, {"state", device::to_string(run.state)},
               {"approval_id", run.approval_id.empty() ? json(nullptr) : json(run.approval_id)},
               {"gate_report_ref", run.gate_report_ref.empty() ? json(nullptr) : json(run.gate_report_ref)},
               {"artifact_hash", artifacts_.at(run.artifact_id).hash_sha256},
               {"discontinuity", discontinuityName(run.discontinuity)},
               {"timeline_from_seq", run.created_at_seq}, {"seq", run.created_at_seq}};
    value["job"] = {{"job_id", run.job.job_id}, {"setup_revision", run.job.setup_revision},
                    {"setup_revision_seq", run.job.setup_revision_seq},
                    {"quantity", run.job.quantity}};
    if (run.initiator) value["initiator"] = initiatorJson(*run.initiator);
    else value["initiator"] = nullptr;
    return value;
}

json ManufacturingLane::proposalJson(const ApprovalProposal& proposal) const {
    return {{"proposal_id", proposal.proposal_id}, {"run_id", proposal.run_id},
            {"status", "unapproved"}, {"exercisable_by", to_string(proposal.exercisable_by)},
            {"expiry_ttl_ms", proposal.expiry_ttl_ms},
            {"attendance", {{"mode", to_string(proposal.attendance.mode)},
                            {"attested_by", proposal.attendance.attested_by},
                            {"at_seq", proposal.attendance.at_seq}}},
            {"spindle_authority", to_string(proposal.spindle_authority)}, {"seq", proposal.seq}};
}

json ManufacturingLane::approvalJson(const ApprovalBinding& approval) const {
    json value{{"approval_id", approval.approval_id}, {"run_id", approval.run_id},
               {"issued_at_seq", approval.issued_at_seq},
               {"machine", {{"device_id", approval.machine.device_id},
                             {"fingerprint_sha256", approval.machine.fingerprint_sha256}}},
               {"artifact", {{"hash_sha256", approval.artifact.hash_sha256},
                              {"bytes", approval.artifact.bytes},
                              {"kind", tier0::to_string(approval.artifact.kind)},
                              {"post_profile_id", approval.artifact.post_profile_id},
                              {"post_profile_hash", approval.artifact.post_profile_hash},
                              {"slicer_profile_hash", approval.artifact.slicer_profile_hash}}},
               {"job", {{"job_id", approval.job.job_id},
                         {"setup_revision", approval.job.setup_revision},
                         {"setup_revision_seq", approval.job.setup_revision_seq},
                         {"quantity", approval.job.quantity}}},
               {"preflight", {{"evidence", approval.preflight.evidence},
                               {"gate_report", approval.preflight.gate_report},
                               {"established_at_seq", approval.preflight.established_at_seq}}},
               {"approver", {{"identity", approval.approver.identity},
                              {"auth", approval.approver.auth},
                              {"at_mono_ns", approval.approver.at_mono_ns}}},
               {"expiry", {{"t_mono_ns", approval.expiry.t_mono_ns},
                            {"ttl_ms", approval.expiry.ttl_ms}}},
               {"exercisable_by", to_string(approval.exercisable_by)},
               {"nonce", {{"state", approval.nonce.state == NonceState::Unburned ? "unburned" :
                                             approval.nonce.state == NonceState::Burned ? "burned" : "void"},
                           {"burned_at_seq", approval.nonce.burned_at_seq
                               ? json(*approval.nonce.burned_at_seq) : json(nullptr)}}},
               {"max_starts", approval.max_starts},
               {"attendance", {{"mode", to_string(approval.attendance.mode)},
                                {"attested_by", approval.attendance.attested_by},
                                {"at_seq", approval.attendance.at_seq}}},
               {"spindle_authority", to_string(approval.spindle_authority)},
               {"policy_revision", approval.policy_revision}, {"seq", approval.issued_at_seq}};
    if (approval.initiator) value["initiator"] = initiatorJson(*approval.initiator);
    else value["initiator"] = nullptr;
    return value;
}

json ManufacturingLane::gateReportJson(const gates::GateReport& report) const {
    json stage1 = json::array();
    for (const auto& check : report.stage1) {
        stage1.push_back({{"name", check.name},
                          {"admission", gates::to_string(check.result.cls)},
                          {"admit", check.result.admit == gates::Admit::Admitted ? "admitted" : "rejected"},
                          {"why", check.result.why}});
    }
    json stage2 = json::array();
    for (const auto& guard : report.stage2)
        stage2.push_back({{"guard", gates::to_string(guard.guard)},
                          {"verdict", gates::to_string(guard.verdict)}, {"why", guard.why}});
    json required = json::array();
    for (const auto guard : report.required_set) required.push_back(gates::to_string(guard));
    json waivers = json::array();
    for (const auto& waiver : report.waivers)
        waivers.push_back({{"guard", gates::to_string(waiver.guard)}, {"by", waiver.by},
                           {"reason", waiver.reason}, {"at_seq", waiver.at_seq}});
    return {{"ref", report.ref}, {"seq", report.seq}, {"subject", report.subject},
            {"stage1", std::move(stage1)}, {"stage2", std::move(stage2)},
            {"required_set", std::move(required)}, {"waivers", std::move(waivers)},
            {"verdict", report.passed ? "pass" : "fail"}, {"rejecting", report.rejecting}};
}


gates::GateReport ManufacturingLane::computePreflight(RunRecord& run,
                                                        const ArtifactRecord& artifact) {
    gates::GateInput input;
    const auto authorization_machine_class = endpoint_.endpoint_is_physical
        ? machine_class_ : device::MachineClass::Emulated;
    input.machine_class = authorization_machine_class;
    input.artifact_kind = artifact.kind;
    FreshValue<device::ControllerSnapshot> snapshot;
    if (adapter_) snapshot = adapter_->snapshot();
    else {
        device::ControllerSnapshot value;
        value.device_id = device_id_.str();
        value.state = device::DeviceState::Idle;
        value.raw = "printer adapter probe";
        snapshot.v = std::move(value);
        snapshot.t_mono_ns = clock_.mono_ns();
        snapshot.seq = journal_.head();
        snapshot.age_ms = 0;
        snapshot.ttl_ms = 5000;
        snapshot.src = endpoint_.endpoint_is_physical ? FreshSource::Probed
                                                      : FreshSource::Simulated;
        snapshot.state = FreshState::Ok;
        snapshot.normalize();
    }
    const auto execute_truth = endpoint_.endpoint_is_physical
        ? device::CapabilityTruthValue::Assumed
        : device::CapabilityTruthValue::Verified;
    gates::Stage1Check controller;
    controller.name = "controller_snapshot";
    controller.subject = {device_id_.str(), "dev", epoch_.epoch, true, true,
                          device_id_.str(), authorization_machine_class};
    controller.candidate.offered_as = gates::Admission::ProbedEvidence;
    controller.candidate.fresh = gates::metadata(
        snapshot, std::string(device::to_string(transport_kind_)), "manufacturing");
    controller.candidate.execute_truth = execute_truth;
    controller.candidate.approval_device_id = device_id_.str();
    controller.accepts = {{"dev"}, {gates::Admission::ProbedEvidence}};
    input.stage1.push_back(std::move(controller));
    auto& facts = input.facts;
    const std::string fingerprint =
        current_machine_fingerprint_ ? current_machine_fingerprint_->str() : std::string();
    const bool simulated_qualification = !endpoint_.endpoint_is_physical;
    facts.artifact_hash = artifact.hash_sha256;
    facts.approved_artifact_hash = artifact.hash_sha256;
    facts.machine_fingerprint = fingerprint;
    facts.approved_machine_fingerprint = fingerprint;
    facts.envelope_min = {0, 0, 0};
    facts.envelope_max = {0, 0, 0};
    facts.travel_min = {0, 0, 0};
    facts.travel_max = simulated_qualification
        ? std::array<double, 3>{300, 300, 80}
        : std::array<double, 3>{0, 0, 0};
    facts.wcs_evidence_seq = std::max<device::Seq>(run.job.setup_revision_seq, 1);
    facts.setup_revision_seq = run.job.setup_revision_seq;
    facts.attendance_policy_satisfied = simulated_qualification;
    facts.spindle_authority_satisfied = simulated_qualification;
    facts.alarm_after_preflight = false;
    facts.nonce_unburned = true;
    facts.exercisable_by = ExercisableBy::HumanOnly;
    facts.initiator_kind = gates::InitiatorKind::Human;
    facts.now_mono_ns = clock_.mono_ns();
    facts.approval_expiry_mono_ns = facts.now_mono_ns + 300000000000ULL;
    facts.emulator_stage_passed = device::EmulatorStage::E3;
    facts.emulator_stage_required = device::EmulatorStage::E3;
    facts.interlock_closed = simulated_qualification;
    facts.feedhold_ack_hold0_observed = simulated_qualification;
    facts.metrology_tier = simulated_qualification ? 2 : 0;
    facts.slicer_profile_hash = artifact.slicer_profile_hash;
    facts.approved_slicer_profile_hash = artifact.slicer_profile_hash;

    const std::string ref = device::ResultRef::mint("gate_report").str();
    const auto expected = journal_.head() + 1;
    auto report = gates::evaluate(input, run.run_id, ref, expected);
    const auto seq = appendEvent("mfg.run.gate_report", run.run_id, gateReportJson(report));
    report.seq = seq;
    run.gate_report_ref = ref;
    gate_reports_.emplace(ref, report);
    return report;
}

RunAuthorization ManufacturingLane::authorizationFor(const RunRecord& run,
                                                       const ArtifactRecord& artifact,
                                                       const ApprovalBinding& approval) const {
    RunAuthorization auth;
    auth.run_id = run.run_id;
    auth.approval_id = approval.approval_id;
    auth.state = device::RunState::Approved;
    auth.current_inputs = {
        {device_id_.str(), current_machine_fingerprint_
                               ? current_machine_fingerprint_->str() : std::string()},
        approval.artifact, approval.job, approval.snapshot,
        approval.preflight, approval.policy_revision, true};
    auth.gate_report_ref = run.gate_report_ref;
    auto& context = auth.tier0_context;
    context.action = tier0::Action::RunStart;
    context.artifact_kind = artifact.kind;
    context.machine_class = endpoint_.endpoint_is_physical
        ? machine_class_ : device::MachineClass::Emulated;
    context.policy = lanePolicy();
    context.policy.revision = approval.policy_revision;
    context.execute_capability = {
        device::CapabilityLevel::Execute,
        endpoint_.endpoint_is_physical
            ? device::CapabilityTruthValue::Assumed
            : device::CapabilityTruthValue::Verified,
        endpoint_.endpoint_is_physical
            ? std::vector<std::string>{}
            : std::vector<std::string>{transport_kind_ == device::TransportKind::Emulator
                  ? "res:emulator_e3:built_in" : "res:virtual_qualification"},
        device::EmulatorStage::E3,
        endpoint_.endpoint_is_physical
            ? std::optional<device::CapabilityLevel>(device::CapabilityLevel::Execute)
            : std::nullopt,
        endpoint_.endpoint_is_physical
            ? "physical execute requires an attended hardware pilot" : ""};
    context.emulator_stage_passed = device::EmulatorStage::E3;
    context.commander_lease_held_by_caller = true;
    context.wcs_established_this_setup = !endpoint_.endpoint_is_physical;
    context.bed_origin_declared_by_firmware = !endpoint_.endpoint_is_physical;

    auto& input = auth.gate_input;
    const auto authorization_machine_class = endpoint_.endpoint_is_physical
        ? machine_class_ : device::MachineClass::Emulated;
    input.machine_class = authorization_machine_class;
    input.artifact_kind = artifact.kind;
    FreshValue<device::ControllerSnapshot> snapshot;
    if (adapter_) snapshot = adapter_->snapshot();
    else {
        device::ControllerSnapshot value;
        value.device_id = device_id_.str();
        value.state = device::DeviceState::Idle;
        value.raw = "printer adapter probe";
        snapshot.v = std::move(value);
        snapshot.t_mono_ns = clock_.mono_ns();
        snapshot.seq = journal_.head();
        snapshot.age_ms = 0;
        snapshot.ttl_ms = 5000;
        snapshot.src = endpoint_.endpoint_is_physical ? FreshSource::Probed
                                                      : FreshSource::Simulated;
        snapshot.state = FreshState::Ok;
        snapshot.normalize();
    }
    const auto execute_truth = endpoint_.endpoint_is_physical
        ? device::CapabilityTruthValue::Assumed
        : device::CapabilityTruthValue::Verified;
    gates::Stage1Check controller;
    controller.name = "controller_snapshot";
    controller.subject = {device_id_.str(), "dev", epoch_.epoch, true, true,
                          device_id_.str(), authorization_machine_class};
    controller.candidate.offered_as = gates::Admission::ProbedEvidence;
    controller.candidate.fresh = gates::metadata(
        snapshot, std::string(device::to_string(transport_kind_)), "manufacturing");
    controller.candidate.execute_truth = execute_truth;
    controller.candidate.approval_device_id = device_id_.str();
    controller.accepts = {{"dev"}, {gates::Admission::ProbedEvidence}};
    input.stage1.push_back(std::move(controller));
    auto& facts = input.facts;
    const bool simulated_qualification = !endpoint_.endpoint_is_physical;
    facts.artifact_hash = artifact.hash_sha256;
    facts.approved_artifact_hash = approval.artifact.hash_sha256;
    facts.machine_fingerprint =
        current_machine_fingerprint_ ? current_machine_fingerprint_->str() : std::string();
    facts.approved_machine_fingerprint = approval.machine.fingerprint_sha256;
    facts.envelope_min = {0, 0, 0};
    facts.envelope_max = {0, 0, 0};
    facts.travel_min = {0, 0, 0};
    facts.travel_max = simulated_qualification
        ? std::array<double, 3>{300, 300, 80}
        : std::array<double, 3>{0, 0, 0};
    facts.wcs_evidence_seq = std::max<device::Seq>(approval.job.setup_revision_seq, 1);
    facts.setup_revision_seq = approval.job.setup_revision_seq;
    facts.attendance_policy_satisfied = simulated_qualification;
    facts.spindle_authority_satisfied = simulated_qualification;
    facts.alarm_after_preflight = false;
    facts.nonce_unburned = approval.nonce.state == NonceState::Unburned;
    facts.exercisable_by = approval.exercisable_by;
    facts.now_mono_ns = clock_.mono_ns();
    facts.approval_expiry_mono_ns = approval.expiry.t_mono_ns;
    facts.emulator_stage_passed = device::EmulatorStage::E3;
    facts.emulator_stage_required = device::EmulatorStage::E3;
    facts.interlock_closed = simulated_qualification;
    facts.feedhold_ack_hold0_observed = simulated_qualification;
    facts.metrology_tier = simulated_qualification ? 2 : 0;
    facts.slicer_profile_hash = artifact.slicer_profile_hash;
    facts.approved_slicer_profile_hash = approval.artifact.slicer_profile_hash;
    return auth;
}

tier0::ArtifactKind ManufacturingLane::parseArtifactKind(std::string_view value) {
    if (value == "nc") return tier0::ArtifactKind::Nc;
    if (value == "gcode") return tier0::ArtifactKind::Gcode;
    if (value == "print_job") return tier0::ArtifactKind::PrintJob;
    throw std::invalid_argument("artifact.kind must be nc, gcode, or print_job");
}

ExercisableBy ManufacturingLane::parseExercisableBy(std::string_view value) {
    if (value == "human_only") return ExercisableBy::HumanOnly;
    if (value == "human_or_agent") return ExercisableBy::HumanOrAgent;
    throw std::invalid_argument("exercisable_by must be human_only or human_or_agent");
}

AttendanceMode ManufacturingLane::parseAttendanceMode(std::string_view value) {
    if (value == "physical_attended") return AttendanceMode::PhysicalAttended;
    if (value == "remote_attended") return AttendanceMode::RemoteAttended;
    if (value == "unattended") return AttendanceMode::Unattended;
    throw std::invalid_argument("invalid attendance mode");
}

SpindleAuthority ManufacturingLane::parseSpindleAuthority(std::string_view value) {
    if (value == "not_applicable") return SpindleAuthority::NotApplicable;
    if (value == "manual_confirmed") return SpindleAuthority::ManualConfirmed;
    if (value == "commanded_feedback") return SpindleAuthority::CommandedFeedback;
    throw std::invalid_argument("invalid spindle_authority");
}


LaneRpcResult ManufacturingLane::route(std::string_view method, const json& params,
                                       const LeaseOwner& caller) {
    LaneRpcResult failure;

    if (method == "mfg_device_list") {
        serviceDevice();
        json rows = json::array();
        for (const auto& candidate : deviceCandidates()) {
            const bool selected = candidate.device_id == device_id_.str();
            const auto lease = selected
                ? leases_.describe(candidate.device_id, clock_.mono_ns()) : std::nullopt;
            json lease_view = json::object();
            if (lease) {
                lease_view = leaseJson(*lease);
                if (!leases_.held_by(candidate.device_id, caller, clock_.mono_ns()))
                    lease_view.erase("lease_id");
            }
            json state;
            if (selected) state = deviceState();
            else {
                FreshValue<device::DeviceState> unknown;
                unknown.why = "candidate has not been claimed";
                state = unknown;
            }
            rows.push_back(
                {{"device_id", candidate.device_id},
                 {"machine_class", device::to_string(candidate.machine_class)},
                 {"transport", {{"kind", device::to_string(candidate.transport_kind)},
                                {"endpoint_id", candidate.endpoint.endpoint_id},
                                {"address", candidate.endpoint.address},
                                {"endpoint_is_physical", candidate.endpoint.endpoint_is_physical}}},
                 {"allowlisted", candidate.allowlisted},
                 {"state", std::move(state)},
                 {"capability_summary",
                  {{"max_advertised", candidate.endpoint.endpoint_is_physical
                                               ? "transport" : "execute"},
                   {"emulator_stage_passed", "E3"}}},
                 {"lease", std::move(lease_view)}});
        }
        return ok({{"devices", std::move(rows)}});
    }
    if (method == "mfg_device_describe") {
        if (!validDevice(params, failure)) return failure;
        serviceDevice();
        return ok({{"identity", current_identity_.canonical()},
                   {"fingerprint_sha256", current_machine_fingerprint_
                        ? json(current_machine_fingerprint_->str()) : json(nullptr)},
                   {"capability", capabilityJson()},
                   {"transport", {{"kind", device::to_string(transport_kind_)},
                                  {"endpoint_id", endpoint_.endpoint_id},
                                  {"address", endpoint_.address},
                                  {"endpoint_is_physical", endpoint_.endpoint_is_physical},
                                  {"connected", connected_}}},
                   {"allowlisted", transport_kind_ == device::TransportKind::Emulator ||
                                         selected_allowlist_entry_.has_value()},
                   {"policy", policyJson(lanePolicy())}});
    }
    if (method == "mfg_device_capability") {
        if (!validDevice(params, failure)) return failure;
        return ok({{"capability", capabilityJson()}});
    }
    if (method == "mfg_device_claim") {
        const std::string requested = params.value("device_id", std::string{});
        const auto candidate = findCandidate(requested);
        if (!candidate || !candidate->allowlisted) {
            return refuse("mfg_device_not_allowlisted",
                          "device claim refused: endpoint is not allowlisted",
                          {{"device_id", requested}});
        }
        const auto current = leases_.describe(device_id_.str(), clock_.mono_ns());
        if (current && requested != device_id_.str())
            return refuse("lease.takeover_refused",
                          "the lane already owns a different active device claim",
                          {{"current_owner", {{"client_kind", current->owner.client_kind},
                                              {"seat", current->owner.seat},
                                              {"session", current->owner.session}}}});
        const std::string role = params.value("role", "commander");
        if (role != "commander")
            return refuse("lease.observer_has_no_command_lease",
                          "observer clients read without owning the command lease");
        if (!current) selectTransport(*candidate);
        const LeaseOwner& owner = caller;
        const uint64_t ttl = params.value("ttl_ms", CommandLease::default_ttl_ms);
        if (ttl == 0 || ttl > CommandLease::max_ttl_ms)
            return refuse("lease.invalid_ttl",
                          "ttl_ms must be within the server-owned lease limit",
                          {{"max_ttl_ms", CommandLease::max_ttl_ms}});
        const uint64_t heartbeat = std::min<uint64_t>(3000, std::max<uint64_t>(1, ttl / 3));
        const auto claimed = leases_.claim(device_id_.str(), owner, clock_.mono_ns(), ttl,
                                           heartbeat,
                                           std::string(device::to_string(transport_kind_)),
                                           endpoint_.endpoint_id);
        if (!claimed.ok)
            return refuse(claimed.code, claimed.why,
                          {{"current_owner", claimed.current_owner
                              ? json{{"client_kind", claimed.current_owner->client_kind},
                                     {"seat", claimed.current_owner->seat},
                                     {"session", claimed.current_owner->session}}
                              : json(nullptr)}});
        active_claim_ = *claimed.lease;
        const auto seq = journal_.head();
        json public_lease = leaseJson(*claimed.lease);
        public_lease.erase("lease_id");
        publishEvent("mfg.device.lease", device_id_.str(), seq,
                     {{"device_id", device_id_.str()}, {"lease", std::move(public_lease)},
                      {"change", "granted"}});
        return ok({{"lease", leaseJson(*claimed.lease)},
                   {"transport", device::to_string(transport_kind_)}});
    }
    if (method == "mfg_device_heartbeat") {
        if (!validDevice(params, failure)) return failure;
        const std::string lease_id = params.value("lease_id", std::string{});
        if (!active_claim_ || lease_id.empty())
            return refuse("lease.not_found_or_expired",
                          "heartbeat requires an active commander lease");
        const auto renewed = leases_.renew(
            lease_id, caller, clock_.mono_ns(), active_claim_->ttl_ms);
        if (!renewed.ok)
            return refuse(renewed.code, renewed.why,
                          {{"current_owner", renewed.current_owner
                              ? json{{"client_kind", renewed.current_owner->client_kind},
                                     {"seat", renewed.current_owner->seat},
                                     {"session", renewed.current_owner->session}}
                              : json(nullptr)}});
        active_claim_ = *renewed.lease;
        const auto seq = journal_.head();
        json public_lease = leaseJson(*renewed.lease);
        public_lease.erase("lease_id");
        publishEvent("mfg.device.lease", device_id_.str(), seq,
                     {{"device_id", device_id_.str()}, {"lease", std::move(public_lease)},
                      {"change", "renewed"}});
        return ok({{"lease", leaseJson(*renewed.lease)}});
    }
    if (method == "mfg_device_release") {
        if (!requireLease(params, caller, failure)) return failure;
        const auto active = leases_.describe(device_id_.str(), clock_.mono_ns());
        // An explicit release has a live commander; require that commander
        // to finish or abort, rather than orphaning an active machine.
        for (const auto& [id, run] : runs_) {
            if (run.device_id == device_id_.str() &&
                (run.state == device::RunState::Running ||
                 run.state == device::RunState::Holding ||
                 run.state == device::RunState::Held ||
                 run.state == device::RunState::Aborting))
                return refuse("mfg_device_release_active_run",
                              "abort or finish the active run before releasing its commander lease",
                              {{"run_id", id}, {"state", device::to_string(run.state)}});
        }
        if (connected_ && isPrinter()) {
            if (!callWorker("mfg_printer_adapter_close", workerParams(), failure))
                return failure;
            connected_ = false;
            appendEvent("mfg.device.state", device_id_.str(),
                        {{"device_id", device_id_.str()}, {"state", deviceState()},
                         {"raw", "commander released device"}});
        } else if (connected_) {
            closeActiveDevice("commander released device");
        }
        const auto released = leases_.release(active->lease_id, caller, clock_.mono_ns());
        if (!released.ok) return refuse(released.code, released.why);
        active_claim_.reset();
        reclaimUnreferencedArtifacts(device_id_.str());
        const auto seq = journal_.head();
        json public_lease = leaseJson(*active);
        public_lease.erase("lease_id");
        publishEvent("mfg.device.lease", device_id_.str(), seq,
                     {{"device_id", device_id_.str()}, {"lease", std::move(public_lease)},
                      {"change", "released"}});
        return ok({{"released", true}});
    }
    if (method == "mfg_device_connect") {
        if (!requireLease(params, caller, failure)) return failure;
        if (terminal_claim_)
            return refuse("mfg_transport_terminal",
                          "connection loss, alarm, or reset made this claim terminal; release it and author a new run");
        if (connected_) return ok({{"connected", true}, {"already_connected", true}});
        if (isPrinter()) {
            auto open = callWorker("mfg_printer_adapter_open", workerParams(), failure);
            if (!open) return failure;
            auto probe = callWorker("mfg_printer_adapter_probe",
                                    {{"adapter_id", device_id_.str()}}, failure);
            if (!probe) {
                LaneRpcResult ignored;
                (void)callWorker("mfg_printer_adapter_close",
                                 {{"adapter_id", device_id_.str()}}, ignored);
                return failure;
            }
            printer_capability_ = probe->value("report", json::object());
            stripWorkerAuthority(printer_capability_);
            const json observed_identity{
                {"machine_class", device::to_string(machine_class_)},
                {"transport_kind", device::to_string(transport_kind_)},
                {"transport_endpoint_id", endpoint_.endpoint_id},
                {"printer_subject_id",
                 printer_capability_.value("subject_id", std::string{})}};
            observeMachineIdentity(
                current_identity_,
                device::MachineFingerprint::from_identity(observed_identity));
            connected_ = true;
            appendEvent("mfg.device.state", device_id_.str(),
                        {{"device_id", device_id_.str()}, {"state", deviceState()},
                         {"raw", "printer adapter probed"}});
            return ok({{"connected", true}, {"device_id", device_id_.str()},
                       {"fingerprint_sha256", current_machine_fingerprint_->str()},
                       {"capability", capabilityJson()}});
        }
        const auto opened = adapter_->open(endpoint_);
        if (!opened.ok) return refuse("mfg_transport_open_refused", opened.why);
        connected_ = true;
        const auto state_seq = appendEvent("mfg.device.state", device_id_.str(),
            {{"device_id", device_id_.str()}, {"state", deviceState()}, {"raw", "connecting"}});
        for (int i = 0; i < 64 && !adapter_->fingerprint(); ++i)
            adapter_->service(state_seq);
        if (!adapter_->fingerprint()) {
            connected_ = false;
            adapter_->close();
            return refuse("mfg_device_identification_failed",
                          "controller identity did not complete");
        }
        clock_.advance_ns(200'000'000);
        bool idle = false;
        for (int i = 0; i < 64; ++i) {
            adapter_->service(state_seq);
            const auto current_snapshot = adapter_->snapshot();
            if (current_snapshot.v && current_snapshot.v->state == device::DeviceState::Idle) {
                idle = true;
                break;
            }
        }
        if (!idle) {
            connected_ = false;
            adapter_->close();
            return refuse("mfg_device_identification_failed",
                          "controller did not publish an idle snapshot");
        }
        observeMachineIdentity(*adapter_->identity(), *adapter_->fingerprint());
        const auto snapshot = adapter_->snapshot();
        appendEvent("mfg.device.snapshot", device_id_.str(),
                    {{"device_id", device_id_.str()}, {"snapshot", snapshot}});
        return ok({{"connected", true}, {"device_id", device_id_.str()},
                   {"fingerprint_sha256", current_machine_fingerprint_->str()}});
    }
    if (method == "mfg_device_disconnect") {
        if (!requireLease(params, caller, failure)) return failure;
        for (const auto& [id, run] : runs_) {
            if (run.device_id == device_id_.str() &&
                (run.state == device::RunState::Running ||
                 run.state == device::RunState::Holding ||
                 run.state == device::RunState::Held ||
                 run.state == device::RunState::Aborting))
                return refuse("mfg_device_disconnect_active_run",
                              "abort or finish the active run before disconnecting its device",
                              {{"run_id", id}, {"state", device::to_string(run.state)}});
        }
        if (isPrinter() && connected_) {
            auto closed = callWorker("mfg_printer_adapter_close",
                                     {{"adapter_id", device_id_.str()}}, failure);
            if (!closed) return failure;
        } else if (connected_ && adapter_) {
            adapter_->close();
        }
        connected_ = false;
        for (auto& [id, run] : runs_) {
            if (run.state == device::RunState::Held) arbiter_.record_discontinuity(
                id, device::RunDiscontinuity::Disconnect);
        }
        appendEvent("mfg.device.state", device_id_.str(),
                    {{"device_id", device_id_.str()}, {"state", deviceState()},
                     {"raw", "transport closed"}});
        reclaimUnreferencedArtifacts(device_id_.str());
        return ok({{"disconnected", true}});
    }
    if (method == "mfg_telemetry_snapshot") {
        if (!validDevice(params, failure)) return failure;
        if (isPrinter()) {
            if (!connected_) return ok({{"snapshot", deviceState()}, {"raw", nullptr}});
            auto result = callWorker("mfg_printer_adapter_snapshot",
                                     {{"adapter_id", device_id_.str()}}, failure);
            if (!result) return failure;
            json rows = result->value("rows", json::array());
            stripWorkerAuthority(rows);
            std::optional<device::RunDiscontinuity> terminal;
            std::string terminal_state;
            std::string job_state;
            uint64_t job_observed_ns = 0;
            uint64_t hold_observed_ns = 0;
            for (const auto& row : rows) {
                if (!row.is_object() || !row.contains("v") || !row.at("v").is_string())
                    continue;
                const std::string field = row.value("field", std::string{});
                const std::string state = row.at("v").get<std::string>();
                if (field == "device_state") {
                    if (state == "disconnected") {
                        terminal = device::RunDiscontinuity::Disconnect;
                        terminal_state = state;
                    } else if (state == "alarm" || state == "fault") {
                        terminal = device::RunDiscontinuity::Alarm;
                        terminal_state = state;
                    } else if (state == "hold" &&
                               row.value("state", std::string{}) == "ok") {
                        const uint64_t observed = row.value("t_mono_ns", uint64_t{0});
                        const uint64_t now = clock_.mono_ns();
                        const uint64_t ttl_ms = row.value("ttl_ms", uint64_t{0});
                        if (observed && observed <= now && ttl_ms &&
                            (now - observed) / 1000000ULL <= ttl_ms)
                            hold_observed_ns = observed;
                    }
                } else if (field == "job_state" &&
                           row.value("state", std::string{}) == "ok") {
                    const uint64_t observed = row.value("t_mono_ns", uint64_t{0});
                    const uint64_t now = clock_.mono_ns();
                    const uint64_t ttl_ms = row.value("ttl_ms", uint64_t{0});
                    if (observed && observed <= now && ttl_ms &&
                        (now - observed) / 1000000ULL <= ttl_ms) {
                        job_state = state;
                        job_observed_ns = observed;
                    }
                }
            }
            if (terminal) {
                terminal_claim_ = true;
                connected_ = false;
                std::string affected_run;
                for (auto& [id, run] : runs_) {
                    const bool active = run.state == device::RunState::Running ||
                                        run.state == device::RunState::Holding ||
                                        run.state == device::RunState::Held ||
                                        run.state == device::RunState::Aborting;
                    if (!active) continue;
                    affected_run = id;
                    run.discontinuity = *terminal;
                    arbiter_.record_discontinuity(id, *terminal);
                    transitionRun(run, device::RunState::Faulted,
                                  discontinuityName(*terminal));
                }
                try {
                    worker_call_("mfg_printer_adapter_close",
                                 {{"adapter_id", device_id_.str()}});
                } catch (...) {
                }
                appendEvent(terminal_state == "alarm" || terminal_state == "fault"
                                ? "mfg.device.alarm" : "mfg.device.state",
                            device_id_.str(),
                            {{"device_id", device_id_.str()},
                             {"terminal", true}, {"raw", terminal_state}});
                const std::string attention_id =
                    "attention:printer:" +
                    util::sha256_hex(device_id_.str() + affected_run +
                                               terminal_state).substr(0, 16);
                json attention{{"action", "raised"}, {"id", attention_id},
                               {"severity", "critical"}, {"device", device_id_.str()},
                               {"run", affected_run},
                               {"reason", terminal_state == "disconnected"
                                              ? "printer_connection_lost"
                                              : "printer_terminal_fault"},
                               {"why", "printer telemetry entered terminal state " +
                                           terminal_state},
                               {"raised_seq", journal_.head() + 1},
                               {"occurrence_count", 1}, {"deliveries", json::array()}};
                const auto attention_seq =
                    appendEvent("mfg.attention", attention_id, attention);
                JournalRecord projected;
                projected.seq = attention_seq;
                projected.kind = "mfg.attention";
                projected.subject = attention_id;
                projected.body = std::move(attention);
                applyAttention(projected);
            }
            if (!terminal) {
                for (auto& [id, run] : runs_) {
                    if (run.device_id != device_id_.str()) continue;
                    const bool active = run.state == device::RunState::Running ||
                                        run.state == device::RunState::Holding ||
                                        run.state == device::RunState::Held ||
                                        run.state == device::RunState::Aborting;
                    if (!active) continue;
                    if (run.state == device::RunState::Holding &&
                        ((job_state == "held" &&
                          job_observed_ns > run.hold_requested_at_ns) ||
                         hold_observed_ns > run.hold_requested_at_ns))
                        transitionRun(run, device::RunState::Held, "hold_observed");
                    if (job_state == "completed" && run.state == device::RunState::Running) {
                        transitionRun(run, device::RunState::Completing, "print_completed_observed");
                        transitionRun(run, device::RunState::Completed, "print_completed_observed");
                    } else if (job_state == "aborted" || job_state == "cancelled") {
                        if (run.state == device::RunState::Running ||
                            run.state == device::RunState::Holding ||
                            run.state == device::RunState::Held)
                            transitionRun(run, device::RunState::Aborting,
                                          "print_cancelled_observed");
                        if (run.state == device::RunState::Aborting)
                            transitionRun(run, device::RunState::Aborted,
                                          "print_cancelled_observed");
                    } else if (job_state == "faulted" || job_state == "error" ||
                               (job_state == "completed" &&
                                run.state != device::RunState::Completed)) {
                        transitionRun(run, device::RunState::Faulted,
                                      "print_job_" + job_state + "_observed");
                    }
                }
            }
            const auto snapshot_seq = journal_.head() + 1;
            for (auto& row : rows)
                if (row.is_object()) row["seq"] = snapshot_seq;
            std::string snapshot_src =
                endpoint_.endpoint_is_physical ? "probed" : "simulated";
            for (const auto& row : rows) {
                if (!row.is_object() || !row.contains("src") ||
                    !row.at("src").is_string()) continue;
                const std::string row_src = row.at("src").get<std::string>();
                if (row_src == "simulated") {
                    snapshot_src = "simulated";
                    break;
                }
                if (row_src == "probed") snapshot_src = "probed";
                else if (snapshot_src != "probed") snapshot_src = row_src;
            }
            json snapshot{{"v", {{"device_id", device_id_.str()}, {"rows", rows}}},
                          {"t_mono_ns", clock_.mono_ns()}, {"seq", snapshot_seq},
                          {"age_ms", 0}, {"ttl_ms", 5000},
                          {"src", snapshot_src},
                          {"state", "ok"}, {"why", ""}};
            appendEvent("mfg.device.snapshot", device_id_.str(),
                        {{"device_id", device_id_.str()}, {"snapshot", snapshot}});
            return ok({{"snapshot", std::move(snapshot)}, {"raw", std::move(rows)}});
        }
        serviceDevice();
        auto snapshot = adapter_->snapshot();
        if (!connected_) {
            snapshot = {};
            snapshot.state = FreshState::Unknown;
            snapshot.src = FreshSource::Declared;
            snapshot.why = std::string(device::to_string(transport_kind_)) +
                           " transport is not connected";
            snapshot.ttl_ms.reset();
            snapshot.normalize();
        }
        return ok({{"snapshot", snapshot},
                   {"raw", snapshot.v ? json(snapshot.v->raw) : json(nullptr)}});
    }
    if (method == "mfg_artifact_register") {
        if (!requireLease(params, caller, failure)) return failure;
        if (!params.contains("artifact") || !params.at("artifact").is_object())
            return refuse("mfg_artifact_invalid", "artifact object is required");
        const auto& input = params.at("artifact");
        uint64_t bytes = 0;
        size_t line_count = 0;
        size_t stored_lines = 0;
        std::string_view text;
        const auto byteRefusal = [&] {
            return refuse("mfg_artifact_byte_limit", "artifact exceeds the per-artifact byte limit",
                          {{"max_bytes", artifact_limits_.max_artifact_bytes}});
        };
        const auto lineRefusal = [&] {
            return refuse("mfg_artifact_line_limit", "artifact exceeds the per-artifact line limit",
                          {{"max_lines", artifact_limits_.max_artifact_lines}});
        };
        if (input.contains("lines")) {
            const auto& lines = input.at("lines");
            if (!lines.is_array())
                return refuse("mfg_artifact_invalid", "artifact lines must be an array of strings");
            line_count = stored_lines = lines.size();
            if (line_count > artifact_limits_.max_artifact_lines) return lineRefusal();
            for (const auto& line : lines) {
                if (!line.is_string())
                    return refuse("mfg_artifact_invalid", "artifact lines must be strings");
                const auto size = line.get_ref<const std::string&>().size();
                if (size >= artifact_limits_.max_artifact_bytes - bytes) return byteRefusal();
                bytes += size + 1;  // Canonical bytes include one newline per array element.
            }
        } else if (input.contains("content")) {
            if (!input.at("content").is_string())
                return refuse("mfg_artifact_invalid", "artifact content must be a string");
            text = input.at("content").get_ref<const std::string&>();
            bytes = text.size();
            if (bytes > artifact_limits_.max_artifact_bytes) return byteRefusal();
            for (size_t start = 0; start < text.size();) {
                const auto end = text.find('\n', start);
                auto line = text.substr(start, end == std::string_view::npos
                                                  ? text.size() - start : end - start);
                if (++line_count > artifact_limits_.max_artifact_lines) return lineRefusal();
                if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
                if (!line.empty()) ++stored_lines;
                start = end == std::string_view::npos ? text.size() : end + 1;
            }
        } else {
            return refuse("mfg_artifact_content_missing", "artifact requires content or lines");
        }
        if (!bytes) return refuse("mfg_artifact_empty", "artifact content is empty");
        uint64_t metadata_bytes = 80;  // Default profile ID and SHA-256.
        for (const auto* field : {"post_profile_id", "post_profile_hash", "slicer_profile_hash"}) {
            if (!input.contains(field)) continue;
            const auto& value = input.at(field);
            if (!value.is_string() || value.get_ref<const std::string&>().size() > 4096)
                return refuse("mfg_artifact_metadata_limit", "artifact profile fields must be strings of at most 4096 bytes");
            metadata_bytes += value.get_ref<const std::string&>().size();
        }
        // Charge line object storage and map/identity overhead, not just program characters.
        const uint64_t retained_bytes = bytes + metadata_bytes + sizeof(ArtifactRecord) + 512 +
                                        stored_lines * (sizeof(std::string) + 1);
        if (artifacts_.size() >= artifact_limits_.max_objects ||
            retained_bytes > artifact_limits_.max_retained_bytes - retained_artifact_bytes_)
            return refuse("mfg_artifact_quota_exceeded", "retained artifact quota is exhausted",
                          {{"max_objects", artifact_limits_.max_objects},
                           {"retained_objects", artifacts_.size()},
                           {"max_retained_bytes", artifact_limits_.max_retained_bytes},
                           {"retained_bytes", retained_artifact_bytes_},
                           {"requested_bytes", retained_bytes}});
        ArtifactRecord artifact;
        artifact.artifact_id = device::ResultRef::mint("artifact").str();
        artifact.device_id = device_id_.str();
        if (input.contains("kind") && !input.at("kind").is_string())
            return refuse("mfg_artifact_invalid", "artifact kind must be a string");
        artifact.kind = parseArtifactKind(input.contains("kind")
            ? std::string_view(input.at("kind").get_ref<const std::string&>()) : std::string_view{});
        artifact.bytes = bytes;
        artifact.retained_bytes = retained_bytes;
        artifact.program_lines.reserve(stored_lines);
        util::Sha256 hash;
        if (input.contains("lines")) {
            for (const auto& line : input.at("lines")) {
                const auto& value = line.get_ref<const std::string&>();
                artifact.program_lines.push_back(value);
                hash.update(reinterpret_cast<const uint8_t*>(value.data()), value.size());
                const uint8_t newline = '\n';
                hash.update(&newline, 1);
            }
        } else {
            hash.update(reinterpret_cast<const uint8_t*>(text.data()), text.size());
            for (size_t start = 0; start < text.size();) {
                const auto end = text.find('\n', start);
                auto line = text.substr(start, end == std::string_view::npos
                                                  ? text.size() - start : end - start);
                if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
                if (!line.empty()) artifact.program_lines.emplace_back(line);
                start = end == std::string_view::npos ? text.size() : end + 1;
            }
        }
        artifact.hash_sha256 = hash.finish_hex();
        if (input.contains("hash_sha256") && input.at("hash_sha256") != artifact.hash_sha256)
            return refuse("mfg_artifact_hash_mismatch", "caller hash does not match artifact bytes");
        artifact.post_profile_id = input.value("post_profile_id", "emulator-default");
        artifact.post_profile_hash = input.value("post_profile_hash",
                                                 util::sha256_hex("emulator-default"));
        artifact.slicer_profile_hash = input.value("slicer_profile_hash", std::string{});
        const auto expected = journal_.head() + 1;
        artifact.seq = expected;
        const auto seq = appendEvent("mfg.artifact.registered", artifact.artifact_id,
                                     artifactJson(artifact));
        artifact.seq = seq;
        auto [stored, inserted] = artifacts_.try_emplace(artifact.artifact_id, std::move(artifact));
        if (inserted) retained_artifact_bytes_ += retained_bytes;
        return ok({{"artifact", artifactJson(stored->second)}});
    }
    if (method == "mfg_preflight_run") {
        if (!requireLease(params, caller, failure)) return failure;
        if (!connected_) return refuse("mfg_transport_not_connected",
                                       "preflight requires a connected identified device");
        serviceDevice();
        const std::string artifact_id = params.value("artifact_id", std::string{});
        const auto artifact_it = artifacts_.find(artifact_id);
        if (artifact_it == artifacts_.end())
            return refuse("mfg_artifact_not_found", "artifact_id is not registered");
        if (artifact_it->second.device_id != device_id_.str())
            return refuse("mfg_artifact_device_mismatch",
                          "artifact is bound to a different selected device");
        if (!current_machine_fingerprint_)
            return refuse("mfg_device_identification_required",
                          "preflight requires an observed machine fingerprint");
        RunRecord* run = nullptr;
        const std::string requested_run = params.value("run_id", std::string{});
        if (!requested_run.empty()) {
            const auto existing = runs_.find(requested_run);
            if (existing == runs_.end()) return refuse("mfg_run_not_found", "run_id is not registered");
            if (existing->second.device_id != device_id_.str())
                return refuse("mfg_run_device_mismatch",
                              "run is bound to a different selected device");
            if (existing->second.artifact_id != artifact_id)
                return refuse("mfg_run_artifact_mismatch", "run is bound to a different artifact");
            run = &existing->second;
        } else {
            RunRecord created;
            created.run_id = device::RunId::mint().str();
            created.device_id = device_id_.str();
            created.artifact_id = artifact_id;
            const auto job = params.value("job", json::object());
            created.job.job_id = job.value("job_id", "job:" + device::detail::mint_ulid());
            created.job.setup_revision = job.value("setup_revision", 1ULL);
            created.job.setup_revision_seq = journal_.head();
            created.job.quantity = job.value("quantity", 1U);
            const auto snap = params.value("snapshot", json::object());
            created.snapshot.tool_table_hash = snap.value("tool_table_hash", "emulator-tools");
            created.snapshot.fixture_hash = snap.value("fixture_hash", "emulator-fixture");
            const auto wcs = snap.value("wcs", json::object());
            created.snapshot.wcs.system = wcs.value("system", "G54");
            created.snapshot.wcs.offset = wcs.value("offset", std::vector<double>{0, 0, 0});
            created.snapshot.wcs.established_by = wcs.value("established_by", "emulator-origin");
            created.created_at_seq = appendEvent("mfg.run.state", created.run_id,
                {{"run_id", created.run_id}, {"state", "draft"}, {"cause", "preflight_created"}});
            auto [it, inserted] = runs_.emplace(created.run_id, std::move(created));
            if (inserted) ++artifact_it->second.run_references;
            run = &it->second;
        }
        tier0::Context tier;
        tier.action = tier0::Action::RunSubmit;
        tier.artifact_kind = artifact_it->second.kind;
        tier.machine_class = machine_class_;
        tier.artifact_present = true;
        tier.artifact_hashed = !artifact_it->second.hash_sha256.empty();
        tier.post_profile_bound = artifact_it->second.kind == tier0::ArtifactKind::PrintJob ||
                                  !artifact_it->second.post_profile_hash.empty();
        tier.policy = lanePolicy();
        const auto refusals = tier0::evaluate(tier);
        json tier_json = json::array();
        for (const auto& refusal : refusals)
            tier_json.push_back({{"action", refusal.action}, {"prerequisite", refusal.prerequisite},
                                 {"why", refusal.why}, {"missing_level", refusal.missing_level},
                                 {"remedy", refusal.remedy},
                                 {"artifact_dependent", refusal.artifact_dependent}});
        const auto report = computePreflight(*run, artifact_it->second);
        return ok({{"run_id", run->run_id}, {"state", device::to_string(run->state)},
                   {"tier0", std::move(tier_json)}, {"gate_report", gateReportJson(report)}});
    }
    if (method == "mfg_gate_report") {
        const std::string ref = params.value("gate_report_ref", std::string{});
        const auto it = gate_reports_.find(ref);
        if (it == gate_reports_.end())
            return refuse("mfg_gate_report_not_found", "gate_report_ref is not registered");
        return ok({{"gate_report", gateReportJson(it->second)}});
    }
    if (method == "mfg_approval_propose") {
        if (!requireLease(params, caller, failure)) return failure;
        const std::string run_id = params.value("run_id", std::string{});
        const auto run = runs_.find(run_id);
        if (run == runs_.end()) return refuse("mfg_run_not_found", "run_id is not registered");
        if (run->second.device_id != device_id_.str())
            return refuse("mfg_run_device_mismatch",
                          "approval proposal run is bound to a different selected device");
        if (run->second.gate_report_ref.empty())
            return refuse("mfg_preflight_missing", "approval proposal requires a preflight report");
        ApprovalProposal proposal;
        proposal.proposal_id = "proposal:" + device::detail::mint_ulid();
        proposal.run_id = run_id;
        proposal.exercisable_by = parseExercisableBy(params.value("exercisable_by", "human_only"));
        proposal.expiry_ttl_ms = params.value("expiry_ttl_ms", 300000ULL);
        const auto attendance = params.value("attendance", json::object());
        proposal.attendance.mode = parseAttendanceMode(attendance.value("mode", "physical_attended"));
        proposal.attendance.attested_by = caller.seat;
        proposal.spindle_authority = parseSpindleAuthority(
            params.value("spindle_authority", "not_applicable"));
        const auto policy = lanePolicy();
        if (!permits(policy, proposal.exercisable_by)) {
            return refuse("tier0:exercisable_by_exceeds_policy",
                          "requested exercisable_by exceeds commissioning policy");
        }
        proposal.seq = journal_.head() + 1;
        proposal.attendance.at_seq = proposal.seq;
        proposal.seq = appendEvent("mfg.approval.proposed", proposal.proposal_id,
                                   proposalJson(proposal));
        proposals_.emplace(proposal.proposal_id, proposal);
        return ok({{"proposal", proposalJson(proposal)}});
    }
    if (method == "mfg_approval_create") {
        if (!requireLease(params, caller, failure)) return failure;
        const std::string proposal_id = params.value("proposal_id", std::string{});
        const auto proposal = proposals_.find(proposal_id);
        if (proposal == proposals_.end())
            return refuse("mfg_approval_proposal_not_found", "proposal_id is not registered");
        if (params.contains("approver_auth"))
            return refuse("mfg_approval_identity_assertion_forbidden",
                          "approver identity is derived from the authenticated connection, not request parameters");
        if (authenticatedInitiator(caller).kind != gates::InitiatorKind::Human)
            return refuse("mfg_approval_human_auth_required",
                          "approval creation requires an unforgeable human-presence attestation");
        const auto run_it = runs_.find(proposal->second.run_id);
        if (run_it == runs_.end() || run_it->second.device_id != device_id_.str())
            return refuse("mfg_run_device_mismatch",
                          "approval run is bound to a different selected device");
        const auto artifact_it = artifacts_.find(run_it->second.artifact_id);
        if (artifact_it == artifacts_.end() ||
            artifact_it->second.device_id != device_id_.str())
            return refuse("mfg_artifact_device_mismatch",
                          "approval artifact is bound to a different selected device");
        if (!current_machine_fingerprint_)
            return refuse("mfg_device_identification_required",
                          "approval requires an observed machine fingerprint");
        const auto report_it = gate_reports_.find(run_it->second.gate_report_ref);
        ApprovalBinding approval;
        approval.approval_id = device::ApprovalId::mint().str();
        approval.run_id = run_it->second.run_id;
        approval.issued_at_seq = journal_.head() + 1;
        approval.machine = {device_id_.str(), current_machine_fingerprint_->str()};
        approval.artifact = {artifact_it->second.hash_sha256, artifact_it->second.bytes,
                             artifact_it->second.kind, artifact_it->second.post_profile_id,
                             artifact_it->second.post_profile_hash,
                             artifact_it->second.slicer_profile_hash};
        approval.job = run_it->second.job;
        approval.snapshot = run_it->second.snapshot;
        approval.preflight = {{run_it->second.gate_report_ref}, run_it->second.gate_report_ref,
                              report_it->second.seq};
        approval.approver = {caller.seat, "hostd_authenticated_connection",
                             clock_.mono_ns()};
        approval.expiry = {approval.approver.at_mono_ns +
                               proposal->second.expiry_ttl_ms * 1000000ULL,
                           proposal->second.expiry_ttl_ms};
        approval.exercisable_by = proposal->second.exercisable_by;
        approval.nonce = {device::detail::mint_ulid(), NonceState::Unburned, std::nullopt};
        approval.max_starts = 1;
        approval.attendance = proposal->second.attendance;
        approval.spindle_authority = proposal->second.spindle_authority;
        approval.policy_revision = lanePolicy().revision;
        std::string why;
        const auto before = journal_.head();
        if (!arbiter_.register_approval(approval, &why))
            return refuse("mfg_approval_create_refused", why);
        approval.issued_at_seq = journal_.head();
        approvals_.emplace(approval.approval_id, approval);
        run_it->second.approval_id = approval.approval_id;
        run_it->second.state = device::RunState::Approved;
        if (!arbiter_.register_run(authorizationFor(run_it->second, artifact_it->second, approval), &why))
            return refuse("mfg_run_authorization_registration_refused", why);
        for (const auto& record : journal_.replay(before + 1))
            publishEvent(record.kind, record.subject, record.seq, record.body);
        publishEvent("mfg.run.state", run_it->second.run_id, journal_.head(),
                     {{"run_id", run_it->second.run_id}, {"state", "approved"},
                      {"cause", "human_approval_created"}});
        return ok({{"approval", approvalJson(approval)}});
    }
    if (method == "mfg_approval_inspect") {
        const std::string approval_id = params.value("approval_id", std::string{});
        if (!approval_id.empty()) {
            const auto it = approvals_.find(approval_id);
            if (it == approvals_.end()) return refuse("mfg_approval_not_found", "approval_id is not registered");
            auto value = approvalJson(it->second);
            value["status"] = it->second.nonce.state == NonceState::Burned ? "burned" :
                              it->second.nonce.state == NonceState::Void ? "revoked" : "active";
            return ok({{"approval", std::move(value)}});
        }
        const std::string proposal_id = params.value("proposal_id", std::string{});
        const auto proposal = proposals_.find(proposal_id);
        if (proposal == proposals_.end())
            return refuse("mfg_approval_not_found", "approval_id or proposal_id is required");
        return ok({{"approval", proposalJson(proposal->second)}});
    }
    if (method == "mfg_approval_revoke") {
        if (!requireLease(params, caller, failure)) return failure;
        const std::string approval_id = params.value("approval_id", std::string{});
        const auto approval = approvals_.find(approval_id);
        if (approval == approvals_.end()) return refuse("mfg_approval_not_found", "approval_id is not registered");
        const std::string reason = params.value("reason", std::string{});
        if (reason.empty()) return refuse("mfg_revoke_reason_required", "revocation requires a reason");
        InvalidationNotice notice;
        notice.classification = InvalidationClass::Falsified;
        notice.reason = "approval_revoked";
        notice.why = reason;
        notice.affected = {approval_id, approval->second.run_id};
        notice.at_seq = journal_.head() + 1;
        const auto seq = appendEvent("mfg.result.falsified", approval_id, notice);
        notice.at_seq = seq;
        invalidations_.push_back(notice);
        appendEvent("mfg.approval.revoked", approval_id,
                    {{"approval_id", approval_id}, {"run_id", approval->second.run_id},
                     {"at_seq", seq}, {"reason", reason}});
        approval->second.nonce.state = NonceState::Void;
        return ok({{"revoked", true}, {"at_seq", seq}});
    }
    if (method == "mfg_run_start") {
        if (!requireLease(params, caller, failure)) return failure;
        if (endpoint_.endpoint_is_physical && !connected_)
            return refuse("mfg_physical_execute_not_qualified",
                          "physical execute is not advertised or enabled");
        if (!connected_) return refuse("mfg_transport_not_connected", "run start requires a connected device");
        const std::string run_id = params.value("run_id", std::string{});
        const std::string approval_id = params.value("approval_id", std::string{});
        const auto run = runs_.find(run_id);
        if (run == runs_.end()) return refuse("mfg_run_not_found", "run_id is not registered");
        if (run->second.device_id != device_id_.str())
            return refuse("mfg_run_device_mismatch",
                          "run is bound to a different selected device");
        const auto artifact = artifacts_.find(run->second.artifact_id);
        if (endpoint_.endpoint_is_physical)
            return refuse("mfg_physical_execute_not_qualified",
                          "physical execute is not advertised or enabled");
        if (artifact == artifacts_.end() ||
            artifact->second.device_id != device_id_.str() ||
            artifact->second.device_id != run->second.device_id)
            return refuse("mfg_artifact_device_mismatch",
                          "run artifact is not bound to the selected device");
        const auto approval = approvals_.find(approval_id);
        if (approval == approvals_.end() || approval->second.nonce.state == NonceState::Void)
            return refuse("approval.revoked_or_missing", "approval is absent or revoked");
        if (approval->second.run_id != run_id ||
            run->second.approval_id != approval_id)
            return refuse("mfg_run_approval_mismatch",
                          "approval is not bound to this run");
        if (approval->second.machine.device_id != device_id_.str())
            return refuse("mfg_approval_device_mismatch",
                          "approval is bound to a different selected device");
        if (!current_machine_fingerprint_ ||
            approval->second.machine.fingerprint_sha256 !=
                current_machine_fingerprint_->str())
            return refuse("mfg_approval_machine_fingerprint_mismatch",
                          "approval is bound to a different observed controller fingerprint");
        if (params.contains("initiator"))
            return refuse("mfg_initiator_identity_assertion_forbidden",
                          "initiator identity is derived from the authenticated connection, not request parameters");
        if (isPrinter() && !worker_call_)
            return refuse("mfg_printer_worker_unavailable",
                          "mfg_printer_adapter_start worker callback is not configured",
                          {{"method", "mfg_printer_adapter_start"}});
        serviceDevice();
        // Rebuild the authorization from a current transport snapshot immediately
        // before arbitration. This refresh and the gate evaluation both precede
        // the one-shot nonce burn.
        std::string refresh_why;
        if (!arbiter_.replace_authorization(
                run_id, authorizationFor(run->second, artifact->second, approval->second),
                &refresh_why))
            return refuse("mfg_run_authorization_refresh_refused", refresh_why);
        const auto initiator = authenticatedInitiator(caller);
        const auto before = journal_.head();
        const auto outcome = arbiter_.start(run_id, approval_id, initiator);
        for (const auto& record : journal_.replay(before + 1))
            publishEvent(record.kind, record.subject, record.seq, record.body);
        if (!outcome.ok) {
            json detail{{"state", device::to_string(outcome.state)}};
            if (outcome.gate_report) detail["gate_report"] = gateReportJson(*outcome.gate_report);
            return refuse(outcome.code, outcome.why, std::move(detail));
        }
        run->second.state = outcome.state;
        const auto described = arbiter_.describe_run(run_id);
        if (described && described->initiator) run->second.initiator = described->initiator;
        approval->second.nonce.state = NonceState::Burned;
        approval->second.nonce.burned_at_seq = outcome.nonce_burned_at_seq;
        approval->second.initiator = run->second.initiator;
        const auto commandUnknown = [&](std::string code, std::string why,
                                        json detail = json::object()) {
            transitionRun(run->second, device::RunState::Faulted, "command_outcome_unknown");
            raiseRunAttention(run_id, "command_outcome_unknown",
                              "start command outcome is unknown after nonce burn: " + why);
            detail["nonce_burned_at_seq"] = *outcome.nonce_burned_at_seq;
            return refuse(std::move(code), std::move(why), std::move(detail));
        };
        if (isPrinter()) {
            const auto& artifact = artifacts_.at(run->second.artifact_id);
            std::string content;
            for (const auto& line : artifact.program_lines) content += line + "\n";
            auto uploaded = callWorker(
                "mfg_printer_adapter_upload",
                {{"adapter_id", device_id_.str()}, {"artifact_b64", base64Encode(content)},
                 {"original_name", "chudware-" + artifact.artifact_id + ".gcode"},
                 {"sha256", artifact.hash_sha256}},
                failure);
            if (!uploaded)
                return commandUnknown("mfg_printer_upload_refused_after_nonce_burn",
                                      failure.body.value("message", "printer upload refused"));
            json remote = uploaded->value(
                "remote_artifact",
                json{{"artifact_id", artifact.artifact_id}, {"sha256", artifact.hash_sha256}});
            const std::string token = "run-token:" + device::detail::mint_ulid();
            auto started = callWorker(
                "mfg_printer_adapter_start",
                {{"adapter_id", device_id_.str()}, {"remote_artifact", std::move(remote)},
                 {"authorization_token", token}},
                failure);
            if (!started)
                return commandUnknown("mfg_program_start_refused_after_nonce_burn",
                                      failure.body.value("message", "printer start refused"));
        } else {
            auto started = adapter_->startProgram(
                artifacts_.at(run->second.artifact_id).program_lines);
            if (!started.ok)
                return commandUnknown("mfg_program_start_refused_after_nonce_burn",
                                      started.why, {{"snapshot", adapter_->snapshot()}});
        }
        return ok({{"state", device::to_string(outcome.state)},
                   {"nonce_burned_at_seq", *outcome.nonce_burned_at_seq},
                   {"gate_report_ref", outcome.gate_report ? outcome.gate_report->ref : run->second.gate_report_ref}});
    }
    if (method == "mfg_run_hold") {
        if (!requireLease(params, caller, failure)) return failure;
        if (params.contains("initiator"))
            return refuse("mfg_initiator_identity_assertion_forbidden",
                          "initiator identity is derived from the authenticated connection, not request parameters");
        const auto initiator = authenticatedInitiator(caller);
        const std::string run_id = params.value("run_id", std::string{});
        const auto lane_run = runs_.find(run_id);
        if (lane_run != runs_.end() &&
            (lane_run->second.state == device::RunState::Completed ||
             lane_run->second.state == device::RunState::Aborted ||
             lane_run->second.state == device::RunState::Faulted ||
             lane_run->second.state == device::RunState::Abandoned))
            return refuse("run.terminal", "terminal run requires a new RunId",
                          {{"state", device::to_string(lane_run->second.state)},
                           {"discontinuity",
                            discontinuityName(lane_run->second.discontinuity)}});
        HoldKind kind = HoldKind::FeedHold;
        const std::string kind_name = params.value("kind", "feed_hold");
        if (kind_name == "controlled_stop") kind = HoldKind::ControlledStop;
        else if (kind_name == "door_suspend") kind = HoldKind::DoorSuspend;
        else if (kind_name != "feed_hold") return refuse("mfg_hold_kind_invalid", "invalid hold kind");
        if (isPrinter() && !worker_call_)
            return refuse("mfg_printer_worker_unavailable",
                          "mfg_printer_adapter_pause worker callback is not configured",
                          {{"method", "mfg_printer_adapter_pause"}});
        const auto before = journal_.head();
        const auto outcome = arbiter_.hold(run_id, initiator, kind);
        for (const auto& record : journal_.replay(before + 1)) publishEvent(record.kind, record.subject, record.seq, record.body);
        if (!outcome.ok) return refuse(outcome.code, outcome.why);
        runs_.at(run_id).state = outcome.state;
        runs_.at(run_id).hold_requested_at_ns = clock_.mono_ns();
        if (isPrinter()) {
            auto paused = callWorker("mfg_printer_adapter_pause",
                                     {{"adapter_id", device_id_.str()}}, failure);
            if (!paused) {
                raiseRunAttention(run_id, "hold_command_outcome_unknown",
                                  "printer pause was not confirmed by the worker");
                return failure;
            }
        } else {
            const auto device_outcome = adapter_->requestHold();
            if (!device_outcome.ok) {
                raiseRunAttention(run_id, "hold_command_outcome_unknown",
                                  "controller feed hold write failed: " + device_outcome.why);
                return refuse("mfg_device_hold_refused", device_outcome.why);
            }
        }
        return ok({{"state", device::to_string(outcome.state)}, {"at_seq", *outcome.at_seq}});
    }
    if (method == "mfg_run_resume") {
        if (!requireLease(params, caller, failure)) return failure;
        if (params.contains("initiator"))
            return refuse("mfg_initiator_identity_assertion_forbidden",
                          "initiator identity is derived from the authenticated connection, not request parameters");
        const auto initiator = authenticatedInitiator(caller);
        const std::string run_id = params.value("run_id", std::string{});
        serviceDevice();
        const auto lane_run = runs_.find(run_id);
        if (lane_run == runs_.end())
            return refuse("run.not_found", "run is not registered");
        const auto arbiter_run = arbiter_.describe_run(run_id);
        if (!arbiter_run) return refuse("run.not_found", "run is not registered");
        const auto observed_discontinuity =
            lane_run->second.discontinuity != device::RunDiscontinuity::None
                ? lane_run->second.discontinuity
                : arbiter_run->discontinuity_since_hold;
        std::string discontinuity(discontinuityName(observed_discontinuity));
        std::string code = "resume_requires_new_authored_run";
        switch (observed_discontinuity) {
            case device::RunDiscontinuity::None: break;
            case device::RunDiscontinuity::Reset:
                code = "resume_forbidden_after_reset"; break;
            case device::RunDiscontinuity::Alarm:
                code = "no_alarm_since_hold"; break;
            case device::RunDiscontinuity::HardLimit:
                code = "resume_forbidden_after_hard_limit"; break;
            case device::RunDiscontinuity::Disconnect:
                code = "resume_forbidden_after_disconnect"; break;
            case device::RunDiscontinuity::PowerLoss:
                code = "resume_forbidden_after_power_loss"; break;
        }
        if (observed_discontinuity == device::RunDiscontinuity::None &&
            arbiter_run->state != device::RunState::Held &&
            arbiter_run->state != device::RunState::Holding)
            return refuse("run.not_held", "resume refusal applies only to a held or holding run");
        const auto seq = appendEvent("mfg.arbiter.attempt", run_id,
            {{"action", "resume"},
             {"initiator", {{"kind", initiator.kind == gates::InitiatorKind::Human ? "human" : "agent"},
                            {"identity", initiator.identity}, {"seat", initiator.seat}}},
             {"outcome", "refused"}, {"reason", code}, {"discontinuity", discontinuity}});
        return refuse(code,
            "resume is disabled after hold (discontinuity=" + discontinuity +
                "); author a new run with fresh inspection, preflight, and approval",
            {{"at_seq", seq}, {"discontinuity", discontinuity},
             {"required", "new authored run with fresh approval"}});
    }
    if (method == "mfg_run_abort") {
        if (!requireLease(params, caller, failure)) return failure;
        if (params.contains("initiator"))
            return refuse("mfg_initiator_identity_assertion_forbidden",
                          "initiator identity is derived from the authenticated connection, not request parameters");
        const auto initiator = authenticatedInitiator(caller);
        const std::string run_id = params.value("run_id", std::string{});
        if (isPrinter() && !worker_call_)
            return refuse("mfg_printer_worker_unavailable",
                          "mfg_printer_adapter_cancel worker callback is not configured",
                          {{"method", "mfg_printer_adapter_cancel"}});
        const auto before = journal_.head();
        const auto outcome = arbiter_.abort(run_id, initiator);
        for (const auto& record : journal_.replay(before + 1)) publishEvent(record.kind, record.subject, record.seq, record.body);
        if (!outcome.ok) return refuse(outcome.code, outcome.why);
        if (isPrinter()) {
            auto cancelled = callWorker("mfg_printer_adapter_cancel",
                                        {{"adapter_id", device_id_.str()}}, failure);
            if (!cancelled) return failure;
        } else {
            const auto device_outcome = adapter_->abort();
            if (!device_outcome.ok)
                return refuse("mfg_device_abort_refused", device_outcome.why);
        }
        runs_.at(run_id).state = outcome.state;
        arbiter_.record_discontinuity(run_id, device::RunDiscontinuity::Reset);
        return ok({{"state", device::to_string(outcome.state)}, {"at_seq", *outcome.at_seq}});
    }
    if (method == "mfg_run_list") {
        json rows = json::array();
        const std::string device_filter = params.value("device_id", std::string{});
        const std::string state_filter = params.value("state", std::string{});
        const size_t limit = params.value("limit", size_t{100});
        for (auto it = runs_.rbegin(); it != runs_.rend() && rows.size() < limit; ++it) {
            if (!device_filter.empty() && it->second.device_id != device_filter) continue;
            if (!state_filter.empty() && device::to_string(it->second.state) != state_filter) continue;
            rows.push_back(runJson(it->second));
        }
        return ok({{"runs", std::move(rows)}});
    }
    if (method == "mfg_run_describe") {
        serviceDevice();
        const std::string run_id = params.value("run_id", std::string{});
        const auto run = runs_.find(run_id);
        if (run == runs_.end()) return refuse("mfg_run_not_found", "run_id is not registered");
        return ok({{"run", runJson(run->second)}});
    }
    if (method == "mfg_attention_raise") {
        if (!requireCommander(caller, failure)) return failure;
        const std::string reason = params.value("reason", std::string{});
        const std::string why = params.value("why", std::string{});
        const std::string severity = params.value("severity", "warning");
        if (reason.size() > 128 || why.size() > 2048)
            return refuse("mfg_attention_invalid",
                          "attention reason is limited to 128 bytes and why to 2048 bytes");
        if (reason.empty() || why.empty())
            return refuse("mfg_attention_invalid", "attention reason and why are required");
        if (severity != "info" && severity != "warning" && severity != "critical")
            return refuse("mfg_attention_invalid", "severity must be info, warning, or critical");
        const std::string id = params.value(
            "id", "attention:" + device::detail::mint_ulid());
        if (id.empty() || id.size() > 128)
            return refuse("mfg_attention_invalid", "attention id must be 1..128 bytes");
        for (const auto* field : {"device", "run"}) {
            if (params.contains(field) &&
                (!params.at(field).is_string() ||
                 params.at(field).get_ref<const std::string&>().size() > 128))
                return refuse("mfg_attention_invalid", "attention device/run must be bounded strings");
        }
        if (attention_.find(id) == attention_.end() && attention_.size() >= 1024)
            return refuse("mfg_attention_capacity",
                          "attention store reached its 1024-item bound");
        auto existing = attention_.find(id);
        const bool occurrence = existing != attention_.end() &&
                                !existing->second.contains("resolved_seq");
        if (existing != attention_.end() &&
            existing->second.value("occurrence_count", 1U) >= 1024)
            return refuse("mfg_attention_capacity",
                          "attention occurrence count reached its 1024-entry bound");
        json body{{"action", occurrence ? "occurred" : "raised"}, {"id", id},
                  {"severity", severity}, {"device", params.value("device", device_id_.str())},
                  {"run", params.value("run", std::string{})}, {"reason", reason},
                  {"why", why}, {"deliveries", json::array()}};
        if (existing != attention_.end()) {
            body["raised_seq"] = existing->second.at("raised_seq");
            body["occurrence_count"] =
                existing->second.value("occurrence_count", 1U) + 1U;
        } else {
            body["raised_seq"] = journal_.head() + 1;
            body["occurrence_count"] = 1;
        }
        const auto seq = appendEvent("mfg.attention", id, body);
        JournalRecord projected;
        projected.seq = seq;
        projected.kind = "mfg.attention";
        projected.subject = id;
        projected.body = std::move(body);
        applyAttention(projected);
        return ok({{"attention", attention_.at(id)}});
    }
    if (method == "mfg_attention_list") {
        const bool open_only = params.value("open_only", false);
        json rows = json::array();
        for (const auto& [id, item] : attention_) {
            if (open_only && (item.contains("acknowledged_seq") ||
                              item.contains("resolved_seq"))) continue;
            rows.push_back(item);
        }
        return ok({{"attention", std::move(rows)}});
    }
    if (method == "mfg_attention_ack") {
        if (!requireCommander(caller, failure)) return failure;
        const std::string id = params.value("id", std::string{});
        const std::string by = caller.seat;
        const auto found = attention_.find(id);
        if (found == attention_.end())
            return refuse("mfg_attention_not_found", "attention id is not registered",
                          {{"id", id}});
        if (by.empty())
            return refuse("mfg_attention_acknowledger_required",
                          "attention acknowledgement requires by");
        if (found->second.contains("acknowledged_seq"))
            return ok({{"attention", found->second}});
        const auto seq = appendEvent("mfg.attention", id,
                                     {{"action", "acknowledged"}, {"id", id}, {"by", by}});
        found->second["acknowledged_by"] = by;
        found->second["acknowledged_seq"] = seq;
        found->second["last_event_seq"] = seq;
        return ok({{"attention", found->second}});
    }
    if (method == "mfg_attention_delivery") {
        if (!requireCommander(caller, failure)) return failure;
        const std::string id = params.value("id", std::string{});
        const auto found = attention_.find(id);
        if (found == attention_.end())
            return refuse("mfg_attention_not_found", "attention id is not registered",
                          {{"id", id}});
        if (!params.contains("delivery") || !params.at("delivery").is_object())
            return refuse("mfg_attention_delivery_invalid", "delivery must be an object");
        const auto& supplied = params.at("delivery");
        for (const auto& [key, value] : supplied.items()) {
            if (key == "attempt") {
                if (!value.is_number_integer() || value < 1 || value > 1024)
                    return refuse("mfg_attention_delivery_invalid", "delivery attempt must be 1..1024");
            } else if (key == "channel" || key == "status" || key == "error") {
                const size_t bound = key == "error" ? 512 : 64;
                if (!value.is_string() || value.get_ref<const std::string&>().size() > bound)
                    return refuse("mfg_attention_delivery_invalid", "delivery text exceeds its bound");
            } else {
                return refuse("mfg_attention_delivery_invalid", "unknown delivery field");
            }
        }
        if (!supplied.contains("channel") || !supplied.contains("status") ||
            supplied.at("channel").get_ref<const std::string&>().empty() ||
            supplied.at("status").get_ref<const std::string&>().empty())
            return refuse("mfg_attention_delivery_invalid", "delivery channel and status are required");
        json delivery = supplied;
        delivery["attempt"] = delivery.value("attempt", 1U);
        delivery["error"] = delivery.value("error", std::string{});
        for (const auto& prior : found->second.at("deliveries")) {
            if (prior.at("channel") == delivery.at("channel") &&
                prior.at("attempt") == delivery.at("attempt") &&
                prior.at("status") == delivery.at("status") &&
                prior.value("error", std::string{}) ==
                    delivery.at("error").get_ref<const std::string&>())
                return ok({{"attention", found->second}});
        }
        if (found->second.at("deliveries").size() >= 64)
            return refuse("mfg_attention_delivery_capacity",
                          "attention delivery history reached its 64-record bound");
        delivery["seq"] = journal_.head() + 1;
        const auto seq = appendEvent(
            "mfg.attention", id,
            {{"action", "delivery"}, {"id", id}, {"delivery", delivery}});
        delivery["seq"] = seq;
        found->second["deliveries"].push_back(std::move(delivery));
        found->second["delivery_count"] = found->second.at("deliveries").size();
        found->second["last_event_seq"] = seq;
        return ok({{"attention", found->second}});
    }
    if (method == "mfg_attention_resolve") {
        if (!requireCommander(caller, failure)) return failure;
        const std::string id = params.value("id", std::string{});
        const auto found = attention_.find(id);
        if (found == attention_.end())
            return refuse("mfg_attention_not_found", "attention id is not registered",
                          {{"id", id}});
        if (found->second.contains("resolved_seq"))
            return ok({{"attention", found->second}});
        const auto seq = appendEvent("mfg.attention", id,
                                     {{"action", "resolved"}, {"id", id}});
        found->second["resolved_seq"] = seq;
        found->second["last_event_seq"] = seq;
        return ok({{"attention", found->second}});
    }
    if (method == "mfg_journal_query") {
        if (!requireCommander(caller, failure)) return failure;
        const auto from = params.value("from_seq", device::Seq{1});
        const auto subject = params.value("subject", std::string{});
        const auto kinds = params.value("kinds", std::vector<std::string>{});
        const size_t limit = std::min(params.value("limit", size_t{1000}), size_t{1000});
        json records = json::array();
        size_t bytes = 0;
        device::Seq next = from;
        bool more = false;
        if (limit != 0) journal_.visit(from, [&](const JournalRecord& record) {
            next = record.seq == std::numeric_limits<device::Seq>::max()
                ? record.seq : record.seq + 1;
            if (!subject.empty() && record.subject != subject) return true;
            if (!kinds.empty() && std::find(kinds.begin(), kinds.end(), record.kind) == kinds.end())
                return true;
            const auto size = json(record).dump().size();
            if (records.size() >= limit || size > journal_.limits().replay_bytes - bytes) {
                next = record.seq;
                more = true;
                return false;
            }
            bytes += size;
            records.push_back(record);
            return true;
        });
        return ok({{"records", std::move(records)}, {"head_seq", journal_.head()},
                   {"next_seq", next}, {"has_more", more},
                   {"waiver_rates", json::object()}});
    }
    if (method == "mfg_result_invalidations") {
        const auto from = params.value("from_seq", device::Seq{1});
        const std::string affected = params.value("affected", std::string{});
        const size_t limit = std::min(params.value("limit", size_t{1000}), size_t{1000});
        json notices = json::array();
        size_t bytes = 0;
        device::Seq next = from;
        bool more = false;
        if (limit != 0) journal_.visit(from, [&](const JournalRecord& record) {
            next = record.seq == std::numeric_limits<device::Seq>::max()
                ? record.seq : record.seq + 1;
            if (record.kind != "mfg.result.falsified" && record.kind != "mfg.result.superseded")
                return true;
            json notice = record.body;
            notice["at_seq"] = record.seq;
            if (!affected.empty()) {
                const auto rows = notice.value("affected", std::vector<std::string>{});
                if (std::find(rows.begin(), rows.end(), affected) == rows.end()) return true;
            }
            const auto size = notice.dump().size();
            if (notices.size() >= limit || size > journal_.limits().replay_bytes - bytes) {
                next = record.seq;
                more = true;
                return false;
            }
            bytes += size;
            notices.push_back(std::move(notice));
            return true;
        });
        return ok({{"invalidations", std::move(notices)}, {"head_seq", journal_.head()},
                   {"next_seq", next}, {"has_more", more}});
    }
    if (method == "mfg_emulator_fault_inject") {
        if (!requireLease(params, caller, failure)) return failure;
        if (endpoint_.endpoint_is_physical)
            return refuse("mfg_emulator_fault_physical_endpoint_forbidden",
                          "emulator fault injection is forbidden for a physical endpoint");
        if (!connected_)
            return refuse("mfg_transport_not_connected",
                          "fault injection requires a connected emulator transport");
        const std::string fault = params.value("fault", std::string{});
        device::EmulatorFault value;
        device::RunDiscontinuity discontinuity = device::RunDiscontinuity::None;
        if (fault == "alarm") { value = device::EmulatorFault::Alarm; discontinuity = device::RunDiscontinuity::Alarm; }
        else if (fault == "hard_limit") { value = device::EmulatorFault::HardLimit; discontinuity = device::RunDiscontinuity::HardLimit; }
        else if (fault == "disconnect") { value = device::EmulatorFault::Disconnect; discontinuity = device::RunDiscontinuity::Disconnect; }
        else if (fault == "door") value = device::EmulatorFault::Door;
        else return refuse("mfg_emulator_fault_invalid", "fault must be alarm, hard_limit, disconnect, or door");
        auto* emulator = dynamic_cast<device::EmulatorTransport*>(transport_.get());
        if (!emulator || !adapter_)
            return refuse("mfg_emulator_transport_unavailable",
                          "the active claim does not own an emulator transport");
        emulator->injectFault(value);
        serviceDevice();
        const auto snapshot = adapter_->snapshot();
        appendEvent(fault == "alarm" || fault == "hard_limit" ? "mfg.device.alarm" : "mfg.device.state",
                    device_id_.str(), {{"device_id", device_id_.str()}, {"snapshot", snapshot},
                                       {"fault", fault}});
        return ok({{"injected", true}, {"fault", fault}, {"snapshot", snapshot}});
    }

    return refuse("mfg_unknown_method", "unknown manufacturing RPC method",
                  {{"method", method}});
}

ManufacturingLane* tryLane(std::string* init_error) {
    // Journal construction can throw (e.g. another daemon holds writer.lock).
    // That must disable the manufacturing lane, not kill the whole daemon —
    // CAD editing does not depend on the manufacturing journal.
    static std::string error;
    static ManufacturingLane* instance = []() -> ManufacturingLane* {
        try {
            static ManufacturingLane lane(defaultStateDirectory());
            return &lane;
        } catch (const std::exception& e) {
            error = e.what();
            return nullptr;
        }
    }();
    if (init_error) *init_error = error;
    return instance;
}

}  // namespace chudware::mfg
