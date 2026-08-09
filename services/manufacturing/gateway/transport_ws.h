#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <span>
#include <string_view>

#include "device/device_id.h"
#include "device/transport.h"

namespace chudware::mfg::gateway {

class DeviceAllowlist;

struct WebSocketTransportConfig {
    std::chrono::milliseconds connect_timeout{3000};
    std::chrono::milliseconds handshake_timeout{3000};
    std::size_t max_frame_bytes = 1024 * 1024;
    std::size_t max_message_bytes = 4 * 1024 * 1024;
};

class WebSocketTransport final : public device::ITransport {
public:
    explicit WebSocketTransport(DeviceAllowlist& allowlist,
                                device::MachineClass machine_class,
                                WebSocketTransportConfig config = {});
    ~WebSocketTransport() override;

    WebSocketTransport(const WebSocketTransport&) = delete;
    WebSocketTransport& operator=(const WebSocketTransport&) = delete;
    WebSocketTransport(WebSocketTransport&&) noexcept;
    WebSocketTransport& operator=(WebSocketTransport&&) noexcept;

    device::OpenResult open(const device::Endpoint& endpoint) override;
    void close() override;
    device::WriteResult write(std::span<const std::byte> bytes) override;
    device::PollResult poll(device::Deadline deadline) override;
    device::TransportKind kind() const override { return device::TransportKind::WebSocket; }

    device::WriteResult writeText(std::string_view text);
    device::WriteResult writeBinary(std::span<const std::byte> bytes);
    device::WriteResult sendPing(std::span<const std::byte> payload = {});
    device::WriteResult closeWithStatus(std::uint16_t status_code,
                                        std::string_view reason = {});
    [[nodiscard]] std::optional<std::uint16_t> lastCloseCode() const;

    // Exposed only to pin the RFC 6455-only digest against published vectors.
    static std::string sha1Base64ForTest(std::string_view bytes);
    static std::string handshakeAcceptForTest(std::string_view client_key);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace chudware::mfg::gateway
