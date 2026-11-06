#pragma once

#include <chrono>
#include <cstddef>
#include <memory>
#include <functional>
#include <span>

#include <string>
#include <string_view>
#include <vector>
#include "device/device_id.h"

#include "device/transport.h"

namespace chudware::mfg::gateway {

class DeviceAllowlist;

class TcpTransport final : public device::ITransport {
public:
    using NameResolver = std::function<std::vector<std::string>(std::string_view)>;
    static constexpr std::size_t kMaxPollBytes = 256 * 1024;
    explicit TcpTransport(DeviceAllowlist& allowlist, device::MachineClass machine_class,
                          std::chrono::milliseconds connect_timeout =
                              std::chrono::milliseconds{3000},
                          device::TransportKind commissioning_kind =
                              device::TransportKind::Tcp,
                          NameResolver resolver = {});
    ~TcpTransport() override;

    TcpTransport(const TcpTransport&) = delete;
    TcpTransport& operator=(const TcpTransport&) = delete;
    TcpTransport(TcpTransport&&) noexcept;
    TcpTransport& operator=(TcpTransport&&) noexcept;

    device::OpenResult open(const device::Endpoint& endpoint) override;
    void close() override;
    device::WriteResult write(std::span<const std::byte> bytes) override;
    // An expired finite deadline performs no socket I/O; an already pending
    // terminal event is still delivered. Deadline::max() polls without waiting.
    // Final data precedes Closed, including across deadline/byte-budget boundaries.
    device::PollResult poll(device::Deadline deadline) override;
    device::TransportKind kind() const override { return device::TransportKind::Tcp; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace chudware::mfg::gateway
