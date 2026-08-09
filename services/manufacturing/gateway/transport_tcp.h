#pragma once

#include <chrono>
#include <memory>
#include <span>

#include "device/device_id.h"

#include "device/transport.h"

namespace chudware::mfg::gateway {

class DeviceAllowlist;

class TcpTransport final : public device::ITransport {
public:
    explicit TcpTransport(DeviceAllowlist& allowlist, device::MachineClass machine_class,
                          std::chrono::milliseconds connect_timeout =
                              std::chrono::milliseconds{3000},
                          device::TransportKind commissioning_kind =
                              device::TransportKind::Tcp);
    ~TcpTransport() override;

    TcpTransport(const TcpTransport&) = delete;
    TcpTransport& operator=(const TcpTransport&) = delete;
    TcpTransport(TcpTransport&&) noexcept;
    TcpTransport& operator=(TcpTransport&&) noexcept;

    device::OpenResult open(const device::Endpoint& endpoint) override;
    void close() override;
    device::WriteResult write(std::span<const std::byte> bytes) override;
    device::PollResult poll(device::Deadline deadline) override;
    device::TransportKind kind() const override { return device::TransportKind::Tcp; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace chudware::mfg::gateway
