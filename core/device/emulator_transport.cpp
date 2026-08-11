#include "device/emulator_transport.h"
#include "device/numeric_parse.h"

#include <algorithm>
#include <cstdio>

#include "device/grbl_protocol.h"

namespace chudware::device {
namespace {

std::vector<std::byte> bytesFor(std::string_view text) {
    std::vector<std::byte> bytes;
    bytes.reserve(text.size() + 1);
    for (const char c : text) bytes.push_back(static_cast<std::byte>(c));
    bytes.push_back(static_cast<std::byte>('\n'));
    return bytes;
}

bool parseAxis(std::string_view command, char axis, double& value) {
    for (std::size_t at = 0; at < command.size(); ++at) {
        if (command[at] != axis && command[at] != static_cast<char>(axis + ('a' - 'A'))) continue;
        const std::string_view candidate = command.substr(at + 1);
        double parsed = 0.0;
        std::size_t consumed = 0;
        if (parseFiniteDoublePrefix(candidate, parsed, consumed)) {
            value = parsed;
            return true;
        }
    }
    return false;
}

}  // namespace

EmulatorTransport::EmulatorTransport(EmulatorTransportConfig config)
    : config_(std::move(config)) {
    if (config_.fixed_tick_ns == 0) config_.fixed_tick_ns = 1;
    if (config_.rx_capacity == 0) config_.rx_capacity = 1;
}

OpenResult EmulatorTransport::open(const Endpoint& endpoint) {
    endpoint_ = endpoint;
    connected_ = true;
    input_line_.clear();
    program_queue_.clear();
    output_.clear();
    faults_.clear();
    rx_used_ = 0;
    tick_count_ = 0;
    accepted_program_lines_ = 0;
    acknowledged_program_lines_ = 0;
    status_query_count_ = 0;
    hold_request_count_ = 0;
    resume_request_count_ = 0;
    reset_request_count_ = 0;
    state_ = State::Idle;
    queueLine(config_.welcome_banner);
    return {true, {}};
}

void EmulatorTransport::close() {
    connected_ = false;
    input_line_.clear();
    program_queue_.clear();
    output_.clear();
    rx_used_ = 0;
    state_ = State::Idle;
}

WriteResult EmulatorTransport::write(std::span<const std::byte> bytes) {
    if (!connected_) return {false, 0, "emulator endpoint is disconnected"};

    std::size_t consumed = 0;
    for (const std::byte raw : bytes) {
        const auto byte = std::to_integer<std::uint8_t>(raw);
        ++consumed;
        const bool realtime = byte == encodeRealtime(RealtimeCmd::status_report) ||
                              byte == encodeRealtime(RealtimeCmd::cycle_start) ||
                              byte == encodeRealtime(RealtimeCmd::feed_hold) ||
                              byte == encodeRealtime(RealtimeCmd::soft_reset) ||
                              byte == encodeRealtime(RealtimeCmd::safety_door) ||
                              byte == encodeRealtime(RealtimeCmd::jog_cancel);
        if (realtime) {
            processRealtime(byte);
            continue;
        }
        if (byte == '\r' || byte == '\n') {
            if (!input_line_.empty()) {
                processCommand(input_line_, input_line_.size() + 1);
                input_line_.clear();
            }
            continue;
        }
        input_line_.push_back(static_cast<char>(byte));
    }
    return {true, consumed, {}};
}

PollResult EmulatorTransport::poll(Deadline) {
    if (!faults_.empty()) {
        const EmulatorFault fault = faults_.front();
        faults_.pop_front();
        applyFault(fault);
    }
    if (connected_) advanceTick();

    PollResult result;
    result.events.reserve(output_.size());
    while (!output_.empty()) {
        result.events.push_back(std::move(output_.front()));
        output_.pop_front();
    }
    if (result.events.empty()) result.events.push_back({TransportEvent::Kind::Timeout, {}, {}, {}});
    return result;
}

void EmulatorTransport::injectFault(EmulatorFault fault) {
    faults_.push_back(fault);
}

void EmulatorTransport::queueLine(std::string line) {
    TransportEvent event;
    event.kind = TransportEvent::Kind::Data;
    event.raw = line;
    event.data = bytesFor(line);
    output_.push_back(std::move(event));
}

void EmulatorTransport::queueEvent(TransportEvent event) {
    output_.push_back(std::move(event));
}

void EmulatorTransport::processCommand(std::string_view command, std::size_t encoded_characters) {
    if (command == "$I") {
        queueLine("[VER:" + config_.version + "]");
        queueLine("[OPT:" + config_.option_flags + "," +
                  std::to_string(config_.planner_blocks) + "," +
                  std::to_string(config_.rx_capacity) + "]");
        queueLine("ok");
        return;
    }
    if (command == "$$") {
        for (const auto& [id, value] : config_.settings)
            queueLine("$" + std::to_string(id) + "=" + value);
        queueLine("ok");
        return;
    }
    if (command == "$G") {
        queueLine("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]");
        queueLine("ok");
        return;
    }
    if (command == "$#") {
        queueLine("[G54:0.000,0.000,0.000]");
        queueLine("[PRB:0.000,0.000,0.000:0]");
        queueLine("ok");
        return;
    }
    if (command == "$C") {
        state_ = state_ == State::Check ? State::Idle : State::Check;
        queueLine("ok");
        return;
    }
    if (command == "$H") {
        state_ = State::Home;
        queueLine("ok");
        state_ = State::Idle;
        return;
    }

    if (state_ == State::Alarm) {
        queueLine("error:9");
        return;
    }
    if (state_ == State::Door) {
        queueLine("error:13");
        return;
    }
    if (encoded_characters > config_.rx_capacity ||
        rx_used_ > config_.rx_capacity - encoded_characters) {
        queueLine("error:11");
        return;
    }
    program_queue_.push_back({std::string(command), encoded_characters});
    rx_used_ += encoded_characters;
    ++accepted_program_lines_;
    if (state_ == State::Idle) state_ = State::Run;
}

void EmulatorTransport::processRealtime(std::uint8_t byte) {
    if (byte == encodeRealtime(RealtimeCmd::status_report)) {
        ++status_query_count_;
        if (status_responses_enabled_) queueLine(statusLine());
    } else if (byte == encodeRealtime(RealtimeCmd::feed_hold)) {
        ++hold_request_count_;
        if (state_ == State::Run) state_ = State::Hold;
    } else if (byte == encodeRealtime(RealtimeCmd::cycle_start)) {
        ++resume_request_count_;
        if (state_ == State::Hold) state_ = program_queue_.empty() ? State::Idle : State::Run;
    } else if (byte == encodeRealtime(RealtimeCmd::safety_door)) {
        state_ = State::Door;
    } else if (byte == encodeRealtime(RealtimeCmd::soft_reset)) {
        ++reset_request_count_;
        resetController();
    } else if (byte == encodeRealtime(RealtimeCmd::jog_cancel)) {
        if (state_ == State::Run) state_ = State::Idle;
    }
}

void EmulatorTransport::advanceTick() {
    ++tick_count_;
    if (state_ == State::Hold || state_ == State::Door || state_ == State::Alarm ||
        state_ == State::Check || program_queue_.empty()) return;

    state_ = State::Run;
    QueuedLine line = std::move(program_queue_.front());
    program_queue_.pop_front();
    rx_used_ -= line.encoded_characters;
    updatePosition(line.text);
    ++acknowledged_program_lines_;
    queueLine("ok");
    if (program_queue_.empty()) state_ = State::Idle;
}

void EmulatorTransport::applyFault(EmulatorFault fault) {
    switch (fault) {
        case EmulatorFault::Alarm:
            program_queue_.clear();
            rx_used_ = 0;
            state_ = State::Alarm;
            queueLine("ALARM:2");
            break;
        case EmulatorFault::HardLimit:
            program_queue_.clear();
            rx_used_ = 0;
            state_ = State::Alarm;
            queueLine("ALARM:1");
            break;
        case EmulatorFault::Door:
            state_ = State::Door;
            queueLine(statusLine());
            break;
        case EmulatorFault::Disconnect: {
            connected_ = false;
            program_queue_.clear();
            rx_used_ = 0;
            TransportEvent event;
            event.kind = TransportEvent::Kind::Closed;
            event.why = "injected emulator disconnect";
            queueEvent(std::move(event));
            break;
        }
        case EmulatorFault::GarbageLine:
            queueLine("%%%not-grbl%%%");
            break;
        case EmulatorFault::RxOverflow:
            program_queue_.clear();
            rx_used_ = 0;
            queueLine("error:11");
            break;
    }
}

std::string EmulatorTransport::statusLine() const {
    const char* state = "Idle";
    switch (state_) {
        case State::Idle: state = "Idle"; break;
        case State::Run: state = "Run"; break;
        case State::Hold: state = "Hold:0"; break;
        case State::Door: state = "Door:0"; break;
        case State::Alarm: state = "Alarm"; break;
        case State::Check: state = "Check"; break;
        case State::Home: state = "Home"; break;
    }
    char buffer[320];
    const int count = std::snprintf(
        buffer, sizeof(buffer),
        "<%s|MPos:%.3f,%.3f,%.3f|WCO:0.000,0.000,0.000|Bf:%u,%zu|FS:100.000,0|Ov:100,100,100%s>",
        state, position_[0], position_[1], position_[2], config_.planner_blocks,
        config_.rx_capacity - rx_used_, state_ == State::Door ? "|Pn:D" : "");
    if (count <= 0) return "<Idle|MPos:0,0,0>";
    return std::string(buffer, static_cast<std::size_t>(count));
}

void EmulatorTransport::updatePosition(std::string_view command) {
    parseAxis(command, 'X', position_[0]);
    parseAxis(command, 'Y', position_[1]);
    parseAxis(command, 'Z', position_[2]);
}

void EmulatorTransport::resetController() {
    program_queue_.clear();
    input_line_.clear();
    rx_used_ = 0;
    state_ = State::Idle;
    queueLine(config_.welcome_banner);
}

}  // namespace chudware::device
