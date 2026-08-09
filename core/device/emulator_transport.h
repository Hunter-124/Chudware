#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <string>
#include <string_view>
#include <vector>

#include "device/transport.h"

namespace chudware::device {

enum class EmulatorFault {
    Alarm,
    HardLimit,
    Door,
    Disconnect,
    GarbageLine,
    RxOverflow,
};

struct EmulatorTransportConfig {
    std::uint64_t fixed_tick_ns = 20'000'000;
    std::size_t rx_capacity = 128;
    std::string welcome_banner = "Grbl 1.1h ['$' for help]";
    std::string version = "chudware-emulator:1.0";
    std::string option_flags = "VN";
    std::uint32_t planner_blocks = 15;
    std::vector<std::pair<int, std::string>> settings = {
        {0, "10"}, {1, "25"}, {100, "250.000"}, {101, "250.000"},
        {102, "250.000"}, {130, "300.000"}, {131, "300.000"}, {132, "80.000"},
    };
};

// Deterministic, in-process Grbl 1.1 responder. poll() advances exactly one fixed
// controller tick; no wall clock or hardware is consulted.
class EmulatorTransport final : public ITransport {
public:
    explicit EmulatorTransport(EmulatorTransportConfig config = {});

    OpenResult open(const Endpoint& endpoint) override;
    void close() override;
    WriteResult write(std::span<const std::byte> bytes) override;
    PollResult poll(Deadline deadline) override;
    TransportKind kind() const override { return TransportKind::Emulator; }

    void injectFault(EmulatorFault fault);
    void setStatusResponsesEnabled(bool enabled) { status_responses_enabled_ = enabled; }

    [[nodiscard]] std::uint64_t tickCount() const { return tick_count_; }
    [[nodiscard]] std::uint64_t logicalTimeNs() const { return tick_count_ * config_.fixed_tick_ns; }
    [[nodiscard]] std::size_t rxUsed() const { return rx_used_; }
    [[nodiscard]] std::size_t acceptedProgramLines() const { return accepted_program_lines_; }
    [[nodiscard]] std::size_t acknowledgedProgramLines() const { return acknowledged_program_lines_; }
    [[nodiscard]] std::size_t statusQueryCount() const { return status_query_count_; }
    [[nodiscard]] std::size_t holdRequestCount() const { return hold_request_count_; }
    [[nodiscard]] std::size_t resumeRequestCount() const { return resume_request_count_; }
    [[nodiscard]] std::size_t resetRequestCount() const { return reset_request_count_; }
    [[nodiscard]] const std::array<double, 3>& position() const { return position_; }

private:
    enum class State { Idle, Run, Hold, Door, Alarm, Check, Home };

    struct QueuedLine {
        std::string text;
        std::size_t encoded_characters = 0;
    };

    void queueLine(std::string line);
    void queueEvent(TransportEvent event);
    void processCommand(std::string_view command, std::size_t encoded_characters);
    void processRealtime(std::uint8_t byte);
    void advanceTick();
    void applyFault(EmulatorFault fault);
    std::string statusLine() const;
    void updatePosition(std::string_view command);
    void resetController();

    EmulatorTransportConfig config_;
    Endpoint endpoint_;
    bool connected_ = false;
    bool status_responses_enabled_ = true;
    State state_ = State::Idle;
    std::uint64_t tick_count_ = 0;
    std::size_t rx_used_ = 0;
    std::size_t accepted_program_lines_ = 0;
    std::size_t acknowledged_program_lines_ = 0;
    std::size_t status_query_count_ = 0;
    std::size_t hold_request_count_ = 0;
    std::size_t resume_request_count_ = 0;
    std::size_t reset_request_count_ = 0;
    std::array<double, 3> position_{};
    std::string input_line_;
    std::deque<QueuedLine> program_queue_;
    std::deque<TransportEvent> output_;
    std::deque<EmulatorFault> faults_;
};

}  // namespace chudware::device
