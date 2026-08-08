#pragma once

#include <chrono>
#include <cstddef>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace chudware::device {

enum class TransportKind {
    Serial,
    Tcp,
    WebSocket,
    Emulator,
    FileDrop,
    HttpAdapter,
    OpcUaRead,
};

inline std::string_view to_string(TransportKind value) {
    switch (value) {
        case TransportKind::Serial: return "serial";
        case TransportKind::Tcp: return "tcp";
        case TransportKind::WebSocket: return "websocket";
        case TransportKind::Emulator: return "emulator";
        case TransportKind::FileDrop: return "file_drop";
        case TransportKind::HttpAdapter: return "http_adapter";
        case TransportKind::OpcUaRead: return "opcua_read";
    }
    return "unknown";
}

struct Endpoint {
    std::string endpoint_id;
    std::string address;
    bool endpoint_is_physical = false;
};

using Deadline = std::chrono::steady_clock::time_point;

struct OpenResult {
    bool ok = false;
    std::string why;
};

struct TransportEvent {
    enum class Kind { Data, Closed, Error, Timeout } kind = Kind::Timeout;
    std::vector<std::byte> data;
    std::string raw;
    std::string why;
};

struct PollResult {
    std::vector<TransportEvent> events;
};

struct WriteResult {
    bool ok = false;
    size_t bytes_written = 0;
    std::string why;
};

class IReadTransport {
public:
    virtual ~IReadTransport() = default;
    virtual OpenResult open(const Endpoint& endpoint) = 0;
    virtual void close() = 0;
    virtual PollResult poll(Deadline deadline) = 0;
    virtual TransportKind kind() const = 0;
};

class ITransport : public IReadTransport {
public:
    ~ITransport() override = default;
    virtual WriteResult write(std::span<const std::byte> bytes) = 0;
};

}  // namespace chudware::device
