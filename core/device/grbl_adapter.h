#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "device/capability.h"
#include "device/controller_snapshot.h"
#include "device/device_id.h"
#include "device/grbl_protocol.h"
#include "device/transport.h"
#include "manufacturing/fresh_value.h"

namespace chudware::device {

struct AdapterResult {
    bool ok = false;
    std::string why;

    explicit operator bool() const { return ok; }
};

struct GrblStreamProgress {
    std::size_t total_lines = 0;
    std::size_t sent_lines = 0;
    std::size_t acknowledged_lines = 0;
    std::size_t outstanding_characters = 0;
    bool complete = false;
    bool halted = false;
    std::string halt_reason;
};

struct GrblAdapterConfig {
    MachineClass machine_class = MachineClass::Emulated;
    std::uint64_t poll_interval_ns = 200'000'000;  // 5 Hz maximum.
    std::uint64_t freshness_ttl_ms = 600;
    std::size_t rx_capacity = GrblRxAccounting::kDefaultRxCapacity;
    // Zero derives the inbound line ceiling from rx_capacity.
    std::size_t max_rx_line_bytes = 0;
    std::vector<std::string> installed_accessories;
};

// Protocol owner over an injected transport and clock. It has no syscall and has
// no approval authority; callers may only reach transport/parse capability here.
class GrblAdapter {
public:
    static constexpr std::size_t kMaxRetainedRawBytes = 256;
    using MonotonicClock = std::function<std::uint64_t()>;

    GrblAdapter(ITransport& transport, MonotonicClock clock,
                GrblAdapterConfig config = {}, std::string lane_id = "grbl");

    OpenResult open(const Endpoint& endpoint);
    void close();
    void service(Seq journal_seq);

    AdapterResult startProgram(std::vector<std::string> lines);
    AdapterResult requestHold();
    AdapterResult requestResume();
    AdapterResult abort();
    AdapterResult reset();

    [[nodiscard]] mfg::FreshValue<ControllerSnapshot> snapshot() const;
    [[nodiscard]] const std::optional<ControllerIdentitySnapshot>& identity() const {
        return identity_;
    }
    [[nodiscard]] const std::optional<MachineFingerprint>& fingerprint() const {
        return fingerprint_;
    }
    [[nodiscard]] const std::optional<DeviceId>& deviceId() const { return device_id_; }
    [[nodiscard]] const LaneEpoch& laneEpoch() const { return lane_epoch_; }
    [[nodiscard]] RunDiscontinuity discontinuity() const { return discontinuity_; }
    [[nodiscard]] const std::string& terminalReason() const { return terminal_reason_; }
    [[nodiscard]] GrblStreamProgress streamProgress() const;
    [[nodiscard]] CapabilityReport capabilityEvidence() const;

private:
    void beginIdentification();
    void finalizeIdentification();
    void processEvent(const TransportEvent& event, Seq journal_seq);
    void processBytes(std::span<const std::byte> bytes, Seq journal_seq);
    void processLine(std::string_view line, Seq journal_seq);
    void processStatus(const GrblStatus& status, std::string_view raw, Seq journal_seq);
    void processPush(const PushMessage& message, std::string_view raw, Seq journal_seq);
    void pumpProgram();
    void publish(ControllerSnapshot value, Seq journal_seq, bool is_status);
    void markTerminal(DeviceState state, RunDiscontinuity discontinuity,
                      std::string reason, std::string_view raw, Seq journal_seq);
    AdapterResult sendRealtime(RealtimeCmd command);
    void resetProtocolState(bool preserve_terminal);
    mfg::FreshSource sourceFor(const Endpoint& endpoint) const;

    ITransport& transport_;
    MonotonicClock clock_;
    GrblAdapterConfig config_;
    Endpoint endpoint_;
    bool open_ = false;
    bool ever_opened_ = false;
    bool identify_sent_ = false;
    bool identified_ = false;
    std::size_t pending_control_acks_ = 0;
    std::string banner_;
    std::string version_;
    std::string build_options_;
    std::map<std::string, std::string> settings_;
    std::string input_bytes_;
    std::optional<ControllerIdentitySnapshot> identity_;
    std::optional<MachineFingerprint> fingerprint_;
    std::optional<DeviceId> device_id_;
    LaneEpoch lane_epoch_;
    RunDiscontinuity discontinuity_ = RunDiscontinuity::None;
    std::string terminal_reason_;
    mfg::FreshSource source_ = mfg::FreshSource::Declared;
    mfg::FreshValue<ControllerSnapshot> latest_;
    std::optional<std::uint64_t> last_status_ns_;
    std::uint64_t next_poll_ns_ = 0;
    Seq current_seq_ = 0;

    GrblRxAccounting accounting_;
    std::vector<std::string> program_;
    std::size_t next_program_line_ = 0;
    std::deque<bool> probe_lines_;
    bool probe_in_flight_ = false;
    bool program_active_ = false;
    bool program_paused_ = false;
    bool program_complete_ = false;
    bool program_halted_ = false;
    std::string halt_reason_;
};

}  // namespace chudware::device
