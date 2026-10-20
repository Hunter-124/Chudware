#include "device/grbl_adapter.h"

#include <algorithm>
#include <chrono>
#include <limits>
#include <utility>

namespace chudware::device {
namespace {

constexpr std::uint64_t kFiveHzIntervalNs = 200'000'000;
// Leave a small receive window within the 200 ms status cadence. Socket transports
// enforce the deadline before recv; an already-expired poll cannot consume a banner.
constexpr auto kServicePollBudget = std::chrono::milliseconds(5);

ControllerSnapshot priorOrDefault(const mfg::FreshValue<ControllerSnapshot>& value) {
    if (value.state == mfg::FreshState::Ok && value.v) return *value.v;
    if (value.last_ok) return value.last_ok->v;
    return {};
}

constexpr std::string_view kRawTruncatedMarker = "...[truncated]";

std::string boundedRaw(std::string_view raw, bool known_truncated = false) {
    if (!known_truncated && raw.size() <= GrblAdapter::kMaxRetainedRawBytes)
        return std::string(raw);
    const std::size_t prefix_bytes =
        GrblAdapter::kMaxRetainedRawBytes - kRawTruncatedMarker.size();
    std::string result(raw.substr(0, std::min(raw.size(), prefix_bytes)));
    result += kRawTruncatedMarker;
    return result;
}

}  // namespace

GrblAdapter::GrblAdapter(ITransport& transport, MonotonicClock clock,
                         GrblAdapterConfig config, std::string lane_id)
    : transport_(transport),
      clock_(std::move(clock)),
      config_(std::move(config)),
      lane_epoch_(LaneEpoch::mint(std::move(lane_id))),
      accounting_(config_.rx_capacity) {
    if (!clock_) throw std::invalid_argument("GrblAdapter requires a monotonic clock");
    config_.poll_interval_ns = std::max(config_.poll_interval_ns, kFiveHzIntervalNs);
    if (config_.freshness_ttl_ms == 0) config_.freshness_ttl_ms = 1;
    if (config_.max_rx_line_bytes == 0) {
        config_.max_rx_line_bytes = config_.rx_capacity == 0
                                        ? GrblRxAccounting::kDefaultRxCapacity
                                        : config_.rx_capacity;
    }
    latest_.ttl_ms = config_.freshness_ttl_ms;
    latest_.why = "device lane has not opened";
}

OpenResult GrblAdapter::open(const Endpoint& endpoint) {
    if (open_) transport_.close();
    open_ = false;
    if (ever_opened_) ++lane_epoch_.epoch;
    resetProtocolState(false);
    endpoint_ = endpoint;
    source_ = sourceFor(endpoint);
    latest_.src = source_;

    const OpenResult result = transport_.open(endpoint);
    if (!result.ok) {
        ControllerSnapshot snapshot;
        snapshot.state = DeviceState::Disconnected;
        snapshot.raw = result.why;
        publish(std::move(snapshot), current_seq_, false);
        terminal_reason_ = "transport open failed: " + result.why;
        discontinuity_ = RunDiscontinuity::Disconnect;
        return result;
    }

    open_ = true;
    ever_opened_ = true;
    next_poll_ns_ = clock_();
    ControllerSnapshot snapshot;
    snapshot.state = DeviceState::Identifying;
    snapshot.raw = "awaiting Grbl 1.1 welcome banner";
    publish(std::move(snapshot), current_seq_, false);
    return result;
}

void GrblAdapter::close() {
    if (open_) transport_.close();
    open_ = false;
    markTerminal(DeviceState::Disconnected, RunDiscontinuity::Disconnect,
                 "transport closed; disconnect requires a new authored run", {}, current_seq_);
}

void GrblAdapter::service(Seq journal_seq) {
    current_seq_ = journal_seq;
    if (!open_) return;

    const std::uint64_t now = clock_();
    if (now >= next_poll_ns_) {
        const AdapterResult result = sendRealtime(RealtimeCmd::status_report);
        if (!result) {
            open_ = false;
            markTerminal(DeviceState::Disconnected, RunDiscontinuity::Disconnect,
                         "status poll write failed: " + result.why, {}, journal_seq);
            return;
        }
        next_poll_ns_ = now > std::numeric_limits<std::uint64_t>::max() - config_.poll_interval_ns
                            ? std::numeric_limits<std::uint64_t>::max()
                            : now + config_.poll_interval_ns;
    }

    const PollResult result =
        transport_.poll(std::chrono::steady_clock::now() + kServicePollBudget);
    for (const auto& event : result.events) {
        processEvent(event, journal_seq);
        if (!open_) break;
    }
    if (open_ && !program_paused_ && !program_halted_) pumpProgram();
}

AdapterResult GrblAdapter::startProgram(std::vector<std::string> lines) {
    if (!open_) return {false, "cannot stream while transport is disconnected"};
    if (!identified_) return {false, "cannot stream before Grbl identity is complete"};
    if (!terminal_reason_.empty()) return {false, terminal_reason_ + "; new authored run required"};
    if (program_active_) return {false, "a program is already active"};
    const auto current = snapshot();
    if (!current.v || current.v->state != DeviceState::Idle)
        return {false, "program start requires a fresh idle controller snapshot"};

    lines.erase(std::remove_if(lines.begin(), lines.end(), [](const std::string& line) {
        return line.empty();
    }), lines.end());
    if (lines.empty()) return {false, "program contains no commands"};
    for (const auto& line : lines) {
        if (line.find_first_of("\r\n") != std::string::npos)
            return {false, "program lines must not contain embedded terminators"};
        if (line.size() + 1 > config_.rx_capacity)
            return {false, "encoded program line exceeds configured Grbl RX capacity"};
    }

    accounting_ = GrblRxAccounting(config_.rx_capacity);
    program_ = std::move(lines);
    next_program_line_ = 0;
    probe_lines_.clear();
    probe_in_flight_ = false;
    program_active_ = true;
    program_paused_ = false;
    program_complete_ = false;
    program_halted_ = false;
    halt_reason_.clear();
    pumpProgram();
    return {true, {}};
}

AdapterResult GrblAdapter::requestHold() {
    if (!open_) return {false, "hold refused: transport is disconnected"};
    if (!terminal_reason_.empty()) return {false, "hold refused: " + terminal_reason_};
    if (!program_active_) return {false, "hold refused: no active program"};
    const AdapterResult result = sendRealtime(RealtimeCmd::feed_hold);
    if (result) program_paused_ = true;
    return result;
}

AdapterResult GrblAdapter::requestResume() {
    if (!terminal_reason_.empty()) return {false, "resume forbidden after " + terminal_reason_};
    if (!open_) return {false, "resume forbidden after disconnect"};
    const auto current = snapshot();
    if (current.v && current.v->state == DeviceState::Door)
        return {false, "resume refused: safety door is open"};
    if (!program_paused_ && (!current.v || current.v->state != DeviceState::Hold))
        return {false, "resume refused: controller is not held"};
    const AdapterResult result = sendRealtime(RealtimeCmd::cycle_start);
    if (result) program_paused_ = false;
    return result;
}

AdapterResult GrblAdapter::abort() {
    if (!open_) return {false, "abort refused: transport is disconnected"};
    const AdapterResult hold = sendRealtime(RealtimeCmd::feed_hold);
    if (!hold) return hold;
    const AdapterResult reset_result = reset();
    if (reset_result) {
        terminal_reason_ = "abort requested via feed hold and soft reset";
        halt_reason_ = terminal_reason_;
    }
    return reset_result;
}

AdapterResult GrblAdapter::reset() {
    if (!open_) return {false, "reset refused: transport is disconnected"};
    const AdapterResult result = sendRealtime(RealtimeCmd::soft_reset);
    if (!result) return result;

    ++lane_epoch_.epoch;
    resetProtocolState(true);
    discontinuity_ = RunDiscontinuity::Reset;
    terminal_reason_ = "reset";
    program_halted_ = true;
    halt_reason_ = "reset; transparent resume is forbidden";
    ControllerSnapshot snapshot;
    snapshot.state = DeviceState::Identifying;
    snapshot.raw = "soft reset sent; awaiting a new welcome banner";
    publish(std::move(snapshot), current_seq_, false);
    return {true, {}};
}

mfg::FreshValue<ControllerSnapshot> GrblAdapter::snapshot() const {
    auto result = latest_;
    const std::uint64_t now = clock_();
    result.age_ms = now >= result.t_mono_ns ? (now - result.t_mono_ns) / 1'000'000 : 0;
    if (open_ && last_status_ns_ && now >= *last_status_ns_ &&
        (now - *last_status_ns_) / 1'000'000 > config_.freshness_ttl_ms) {
        result.state = mfg::FreshState::Unknown;
        result.v.reset();
        result.why = "status poll gap exceeded " + std::to_string(config_.freshness_ttl_ms) + " ms";
    }
    result.normalize();
    return result;
}

GrblStreamProgress GrblAdapter::streamProgress() const {
    return {program_.size(), static_cast<std::size_t>(accounting_.sentLines()),
            static_cast<std::size_t>(accounting_.acknowledgedLines()),
            accounting_.outstandingCharacters(), program_complete_, program_halted_, halt_reason_};
}

CapabilityReport GrblAdapter::capabilityEvidence() const {
    CapabilityReport report;
    report.subject_id = device_id_ ? device_id_->str() : endpoint_.endpoint_id;
    report.emulator_stage_passed = EmulatorStage::E1;
    report.max_advertised = ever_opened_ ? CapabilityLevel::Transport : CapabilityLevel::Parse;

    for (const CapabilityLevel level : capability_levels) {
        CapabilityTruth truth;
        truth.level = level;
        truth.emulator_stage = EmulatorStage::E1;
        if (level == CapabilityLevel::Parse) {
            truth.truth = CapabilityTruthValue::Verified;
            truth.evidence_ref = {"builtin:grbl-1.1-codec"};
        } else if (level == CapabilityLevel::Transport && ever_opened_) {
            truth.truth = CapabilityTruthValue::Verified;
            truth.evidence_ref = {"endpoint:" + endpoint_.endpoint_id};
        } else {
            truth.truth = CapabilityTruthValue::Unsupported;
            truth.why = (level == CapabilityLevel::Execute)
                ? "adapter evidence never authorizes execute"
                : (level == CapabilityLevel::SafetyIntegration)
                    ? "Grbl software operations are not a safety integration"
                    : "this adapter emits only parse and transport evidence";
        }
        report.levels.emplace(level, std::move(truth));
    }
    return report;
}

void GrblAdapter::beginIdentification() {
    if (!open_ || identify_sent_) return;
    static constexpr std::string_view commands[] = {"$I\r", "$$\r"};
    for (const auto command : commands) {
        const auto* data = reinterpret_cast<const std::byte*>(command.data());
        const WriteResult result = transport_.write({data, command.size()});
        if (!result.ok || result.bytes_written != command.size()) {
            markTerminal(DeviceState::Disconnected, RunDiscontinuity::Disconnect,
                         "identify write failed: " + result.why, command, current_seq_);
            open_ = false;
            return;
        }
    }
    identify_sent_ = true;
    pending_control_acks_ = 2;
}

void GrblAdapter::finalizeIdentification() {
    if (identified_ || banner_.empty() || version_.empty() || build_options_.empty() || settings_.empty()) {
        if (pending_control_acks_ == 0 && !identified_)
            markTerminal(DeviceState::Fault, RunDiscontinuity::None,
                         "incomplete Grbl identity response", {}, current_seq_);
        return;
    }
    ControllerIdentitySnapshot identity;
    identity.firmware_banner = banner_;
    identity.build_options = build_options_ + ";VER=" + version_;
    identity.settings = settings_;
    identity.axis_count = settings_.contains("$102") ? 3U : 0U;
    identity.machine_class = config_.machine_class;
    identity.transport_kind = std::string(to_string(transport_.kind()));
    identity.transport_endpoint_id = endpoint_.endpoint_id;
    identity.installed_accessories = config_.installed_accessories;
    identity_ = std::move(identity);
    fingerprint_ = MachineFingerprint::from_identity(*identity_);
    device_id_ = DeviceId::mint(config_.machine_class, *fingerprint_);
    identified_ = true;

    ControllerSnapshot snapshot = priorOrDefault(latest_);
    snapshot.device_id = device_id_->str();
    snapshot.state = DeviceState::Identifying;
    snapshot.raw = "identity complete; awaiting status";
    publish(std::move(snapshot), current_seq_, false);
}

void GrblAdapter::processEvent(const TransportEvent& event, Seq journal_seq) {
    switch (event.kind) {
        case TransportEvent::Kind::Data:
            if (!event.data.empty()) processBytes(event.data, journal_seq);
            else if (!event.raw.empty()) processLine(event.raw, journal_seq);
            break;
        case TransportEvent::Kind::Closed:
            open_ = false;
            markTerminal(DeviceState::Disconnected, RunDiscontinuity::Disconnect,
                         event.why.empty() ? "disconnect" : "disconnect: " + event.why,
                         event.raw, journal_seq);
            break;
        case TransportEvent::Kind::Error:
            open_ = false;
            markTerminal(DeviceState::Fault, RunDiscontinuity::Disconnect,
                         event.why.empty() ? "transport error" : "transport error: " + event.why,
                         event.raw, journal_seq);
            break;
        case TransportEvent::Kind::Timeout:
            break;
    }
}

void GrblAdapter::processBytes(std::span<const std::byte> bytes, Seq journal_seq) {
    for (const std::byte byte : bytes) {
        const char ch = static_cast<char>(std::to_integer<unsigned char>(byte));
        if (ch == '\r' || ch == '\n') {
            if (input_bytes_.empty()) continue;
            std::string line = std::move(input_bytes_);
            input_bytes_.clear();
            processLine(line, journal_seq);
            if (!terminal_reason_.empty()) return;
            continue;
        }
        if (input_bytes_.size() >= config_.max_rx_line_bytes) {
            const std::string retained = boundedRaw(input_bytes_, true);
            input_bytes_.clear();
            markTerminal(
                DeviceState::Fault, RunDiscontinuity::None,
                "rx_overflow: inbound Grbl line exceeded " +
                    std::to_string(config_.max_rx_line_bytes) + " bytes (Grbl error 11)",
                retained, journal_seq);
            return;
        }
        input_bytes_.push_back(ch);
    }
}

void GrblAdapter::processLine(std::string_view line, Seq journal_seq) {
    if (line.size() > config_.max_rx_line_bytes) {
        markTerminal(
            DeviceState::Fault, RunDiscontinuity::None,
            "rx_overflow: inbound Grbl line exceeded " +
                std::to_string(config_.max_rx_line_bytes) + " bytes (Grbl error 11)",
            boundedRaw(line, true), journal_seq);
        return;
    }
    if (line.starts_with("<")) {
        const auto parsed = parseStatusReport(line);
        if (!parsed) {
            markTerminal(DeviceState::Fault, RunDiscontinuity::None,
                         "invalid Grbl status: " + std::string(parsed.reason), line, journal_seq);
            return;
        }
        processStatus(parsed.value, line, journal_seq);
        return;
    }

    const auto parsed = parsePushMessage(line);
    if (!parsed) {
        markTerminal(DeviceState::Fault, RunDiscontinuity::None,
                     "unparseable Grbl vendor line: " + std::string(parsed.reason), line, journal_seq);
        return;
    }
    processPush(parsed.value, line, journal_seq);
}

void GrblAdapter::processStatus(const GrblStatus& status, std::string_view raw, Seq journal_seq) {
    ControllerSnapshot snapshot;
    if (device_id_) snapshot.device_id = device_id_->str();
    const auto mapped = parse_device_state(canonicalDeviceState(status.state));
    snapshot.state = mapped.value_or(DeviceState::Unknown);
    if (status.state == GrblState::run && probe_in_flight_) snapshot.state = DeviceState::Probing;
    if (snapshot.state == DeviceState::Hold || snapshot.state == DeviceState::Door)
        program_paused_ = true;
    if (status.substate) snapshot.substate = std::to_string(*status.substate);
    if (status.machine_position)
        snapshot.mpos = {status.machine_position->x, status.machine_position->y, status.machine_position->z};
    if (status.work_position)
        snapshot.wpos = {status.work_position->x, status.work_position->y, status.work_position->z};
    if (status.work_coordinate_offset)
        snapshot.wco = {status.work_coordinate_offset->x, status.work_coordinate_offset->y,
                        status.work_coordinate_offset->z};
    if (status.machine_position && status.work_coordinate_offset) {
        for (std::size_t i = 0; i < 3; ++i) snapshot.wpos[i] = snapshot.mpos[i] - snapshot.wco[i];
    } else if (status.work_position && status.work_coordinate_offset) {
        for (std::size_t i = 0; i < 3; ++i) snapshot.mpos[i] = snapshot.wpos[i] + snapshot.wco[i];
    }
    if (status.rates) {
        snapshot.feed_rpm.feed = status.rates->feed;
        snapshot.feed_rpm.spindle = status.rates->spindle_rpm.value_or(0.0);
    }
    if (status.overrides) {
        snapshot.overrides = {static_cast<int>(status.overrides->feed_percent),
                              static_cast<int>(status.overrides->rapid_percent),
                              static_cast<int>(status.overrides->spindle_percent)};
    }
    if (status.pins) {
        snapshot.inputs = {status.pins->x_limit, status.pins->y_limit, status.pins->z_limit,
                           status.pins->probe, status.pins->door, status.pins->hold,
                           status.pins->soft_reset, status.pins->cycle_start};
    }
    if (status.buffer) {
        snapshot.buffer.planner = static_cast<int>(status.buffer->planner_available);
        snapshot.buffer.rx = static_cast<int>(status.buffer->rx_available);
    }
    if (status.line_number) snapshot.line = *status.line_number;
    snapshot.raw = boundedRaw(raw);

    if (snapshot.state == DeviceState::Alarm && terminal_reason_.empty()) {
        markTerminal(DeviceState::Alarm, RunDiscontinuity::Alarm,
                     "alarm status", raw, journal_seq);
        return;
    }
    publish(std::move(snapshot), journal_seq, true);
}

void GrblAdapter::processPush(const PushMessage& message, std::string_view raw, Seq journal_seq) {
    switch (message.kind) {
        case PushKind::welcome:
            banner_ = std::string(raw);
            beginIdentification();
            break;
        case PushKind::version:
            version_ = std::string(message.text);
            break;
        case PushKind::options:
            build_options_ = std::string(raw);
            config_.rx_capacity = message.options.rx_buffer_bytes;
            accounting_ = GrblRxAccounting(config_.rx_capacity);
            break;
        case PushKind::setting: {
            const auto equals = raw.find('=');
            if (equals != std::string_view::npos)
                settings_[std::string(raw.substr(0, equals))] = std::string(raw.substr(equals + 1));
            break;
        }
        case PushKind::acknowledgment:
            if (pending_control_acks_ > 0) {
                --pending_control_acks_;
                if (pending_control_acks_ == 0) finalizeIdentification();
            } else if (accounting_.outstandingLines() > 0) {
                const AccountingResult result = accounting_.acknowledge(message);
                if (!result) {
                    markTerminal(DeviceState::Fault, RunDiscontinuity::None,
                                 "stream accounting failure: " + std::string(result.reason), raw, journal_seq);
                    return;
                }
                if (!probe_lines_.empty()) probe_lines_.pop_front();
                probe_in_flight_ = std::find(probe_lines_.begin(), probe_lines_.end(), true) != probe_lines_.end();
                if (next_program_line_ == program_.size() && accounting_.outstandingLines() == 0) {
                    program_active_ = false;
                    program_complete_ = true;
                }
            }
            break;
        case PushKind::error: {
            if (pending_control_acks_ > 0) --pending_control_acks_;
            else if (accounting_.outstandingLines() > 0) (void)accounting_.acknowledge(message);
            const std::string reason = message.code == 11
                ? "RX overflow or maximum line length error: " + std::string(message.text)
                : "Grbl stream error " + std::to_string(message.code) + ": " + std::string(message.text);
            markTerminal(DeviceState::Fault, RunDiscontinuity::None, reason, raw, journal_seq);
            break;
        }
        case PushKind::alarm: {
            const bool hard_limit = message.code == 1;
            markTerminal(DeviceState::Alarm,
                         hard_limit ? RunDiscontinuity::HardLimit : RunDiscontinuity::Alarm,
                         hard_limit ? "hard limit ALARM:1" : "alarm ALARM:" + std::to_string(message.code),
                         raw, journal_seq);
            break;
        }
        default:
            break;
    }
}

void GrblAdapter::pumpProgram() {
    while (program_active_ && !program_paused_ && next_program_line_ < program_.size()) {
        const std::string& command = program_[next_program_line_];
        const std::size_t encoded_size = command.size() + 1;
        if (!accounting_.canSend(encoded_size)) break;
        std::string encoded = command;
        encoded.push_back('\r');
        const auto* data = reinterpret_cast<const std::byte*>(encoded.data());
        const WriteResult write = transport_.write({data, encoded.size()});
        if (!write.ok || write.bytes_written != encoded.size()) {
            open_ = false;
            markTerminal(DeviceState::Disconnected, RunDiscontinuity::Disconnect,
                         "program write disconnected: " + write.why, command, current_seq_);
            return;
        }
        const AccountingResult account = accounting_.accountSent(encoded_size);
        if (!account) {
            markTerminal(DeviceState::Fault, RunDiscontinuity::None,
                         "stream accounting failure: " + std::string(account.reason), command, current_seq_);
            return;
        }
        const bool probe = command.find("G38.") != std::string::npos;
        probe_lines_.push_back(probe);
        probe_in_flight_ = probe_in_flight_ || probe;
        ++next_program_line_;
    }
}

void GrblAdapter::publish(ControllerSnapshot value, Seq journal_seq, bool is_status) {
    const std::uint64_t now = clock_();
    latest_.v = std::move(value);
    latest_.state = mfg::FreshState::Ok;
    latest_.t_mono_ns = now;
    latest_.seq = journal_seq;
    latest_.age_ms = 0;
    latest_.ttl_ms = config_.freshness_ttl_ms;
    latest_.src = source_;
    latest_.why.clear();
    latest_.last_ok = mfg::LastOk<ControllerSnapshot>{*latest_.v, now, journal_seq};
    if (is_status) last_status_ns_ = now;
}

void GrblAdapter::markTerminal(DeviceState state, RunDiscontinuity discontinuity,
                               std::string reason, std::string_view raw, Seq journal_seq) {
    ControllerSnapshot snapshot = priorOrDefault(latest_);
    if (device_id_) snapshot.device_id = device_id_->str();
    snapshot.state = state;
    snapshot.raw = boundedRaw(raw);
    if (state == DeviceState::Alarm) snapshot.alarm = reason;
    publish(std::move(snapshot), journal_seq, false);
    discontinuity_ = discontinuity;
    terminal_reason_ = std::move(reason);
    program_active_ = false;
    program_paused_ = false;
    program_halted_ = true;
    halt_reason_ = terminal_reason_;
}

AdapterResult GrblAdapter::sendRealtime(RealtimeCmd command) {
    const std::byte byte = static_cast<std::byte>(encodeRealtime(command));
    const WriteResult result = transport_.write({&byte, 1});
    if (!result.ok || result.bytes_written != 1)
        return {false, result.why.empty() ? "short realtime write" : result.why};
    return {true, {}};
}

void GrblAdapter::resetProtocolState(bool preserve_terminal) {
    identify_sent_ = false;
    identified_ = false;
    pending_control_acks_ = 0;
    banner_.clear();
    version_.clear();
    build_options_.clear();
    settings_.clear();
    input_bytes_.clear();
    identity_.reset();
    fingerprint_.reset();
    device_id_.reset();
    last_status_ns_.reset();
    accounting_ = GrblRxAccounting(config_.rx_capacity);
    program_.clear();
    next_program_line_ = 0;
    probe_lines_.clear();
    probe_in_flight_ = false;
    program_active_ = false;
    program_paused_ = false;
    program_complete_ = false;
    if (!preserve_terminal) {
        discontinuity_ = RunDiscontinuity::None;
        terminal_reason_.clear();
        program_halted_ = false;
        halt_reason_.clear();
    }
}

mfg::FreshSource GrblAdapter::sourceFor(const Endpoint& endpoint) const {
    if (transport_.kind() == TransportKind::Emulator || !endpoint.endpoint_is_physical)
        return mfg::FreshSource::Simulated;
    return mfg::FreshSource::Probed;
}

}  // namespace chudware::device
