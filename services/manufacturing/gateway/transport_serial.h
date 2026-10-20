#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "device/capability.h"
#include "device/transport.h"
#include "manufacturing/fresh_value.h"
#include "manufacturing/gateway/device_allowlist.h"

namespace chudware::mfg::gateway {

struct SerialCandidate {
    EndpointIdentity identity;
    std::string display_name;
    bool endpoint_is_physical = true;
};

struct SerialTransportOptions {
    std::uint32_t baud = 115200;
    std::size_t max_read_bytes = 64 * 1024;
    std::chrono::milliseconds write_timeout{2000};
    std::string operator_note = "operator commissioned serial controller";
    std::string posix_by_id_root = "/dev/serial/by-id";
    std::string posix_sysfs_tty_root = "/sys/class/tty";
};

// Real OS serial I/O. Commissioning requires a complete USB VID/PID/serial tuple.
// POSIX binds validated sysfs device numbers to the opened character descriptor
// before configuration or traffic. The class owns one exclusive handle and
// releases every OS claim from close()/destruction.
class SerialTransport final : public device::ITransport {
public:
    SerialTransport(DeviceAllowlist& allowlist, device::MachineClass machine_class,
                    SerialTransportOptions options = {});
    ~SerialTransport() override;

    SerialTransport(const SerialTransport&) = delete;
    SerialTransport& operator=(const SerialTransport&) = delete;

    device::OpenResult open(const device::Endpoint& endpoint) override;
    void close() override;
    device::WriteResult write(std::span<const std::byte> bytes) override;
    device::PollResult poll(device::Deadline deadline) override;
    device::TransportKind kind() const override { return device::TransportKind::Serial; }

    [[nodiscard]] bool isOpen() const { return handle_ != invalid_handle; }
    [[nodiscard]] FreshSource evidenceSource() const { return source_; }
    [[nodiscard]] static FreshSource evidenceSourceForEndpoint(
        const device::Endpoint& endpoint);
    [[nodiscard]] device::CapabilityLevel maxCapability() const {
        return device::CapabilityLevel::Transport;
    }
    [[nodiscard]] device::CapabilityReport capabilityReport(
        std::string subject_id, std::string transport_evidence_ref = {}) const;

    // Enumeration never authorizes or opens. POSIX returns stable /dev/serial/by-id
    // names. Windows returns COM port plus USB VID/PID/serial when the OS exposes it.
    [[nodiscard]] static std::vector<SerialCandidate> enumerate(std::string* why = nullptr);

private:
    static constexpr std::intptr_t invalid_handle = -1;

    device::PollResult deviceLost(std::string why);
    bool configure(std::uint32_t baud, std::string* why);

    DeviceAllowlist& allowlist_;
    device::MachineClass machine_class_;
    SerialTransportOptions options_;
    std::intptr_t handle_ = invalid_handle;
    device::Endpoint endpoint_;
    FreshSource source_ = FreshSource::Simulated;
};

}  // namespace chudware::mfg::gateway
