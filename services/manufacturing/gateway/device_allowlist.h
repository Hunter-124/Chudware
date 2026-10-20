#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "device/device_id.h"
#include "device/transport.h"

namespace chudware::mfg::gateway {

// A stable identity used for commissioning. Serial endpoints require address
// plus a complete USB VID/PID/serial tuple on every platform. Network endpoints
// use kind/address/port; DNS resolution happens only after authorization.
struct EndpointIdentity {
    device::TransportKind transport = device::TransportKind::Serial;
    std::string address;
    std::uint16_t port = 0;
    std::string usb_vid;
    std::string usb_pid;
    std::string usb_serial;

    static EndpointIdentity serial(std::string stable_path_or_port,
                                   std::string usb_vid = {},
                                   std::string usb_pid = {},
                                   std::string usb_serial = {});
    static EndpointIdentity network(device::TransportKind kind, std::string host,
                                    std::uint16_t port);

    [[nodiscard]] bool valid(std::string* why = nullptr) const;
    [[nodiscard]] std::string key() const;
    bool operator==(const EndpointIdentity&) const = default;
};

struct CommissioningCandidate {
    EndpointIdentity endpoint;
    device::MachineClass machine_class = device::MachineClass::OpenFrameMill;
    // Transport-specific, human-authored settings. Serial uses "baud"; WebSocket
    // uses "path". A candidate's supplied keys must exactly match stored values.
    std::map<std::string, std::string> connection;
    std::string operator_note;
};

using AllowlistEntry = CommissioningCandidate;

struct Authorization {
    bool allowed = false;
    std::optional<AllowlistEntry> entry;
    std::string why;
    // Populated on refusal. This is the canonical, complete line the operator
    // can add to the allowlist (or edit before adding when using the Endpoint overload).
    std::string required_line;
};

// Durable, deny-by-default commissioning data. The file is escaped TSV: the
// transport kind is column one and all remaining columns are key=value. Comments
// and blank lines are accepted on input. Writes are atomic and synchronized.
class DeviceAllowlist {
public:
    explicit DeviceAllowlist(std::filesystem::path path);

    [[nodiscard]] const std::filesystem::path& path() const { return path_; }
    [[nodiscard]] std::uint64_t epoch() const { return epoch_; }
    [[nodiscard]] const std::vector<AllowlistEntry>& entries() const { return entries_; }

    // Reloading semantically changed entries increments epoch exactly once.
    bool reload(std::string* why = nullptr);

    // Candidate authorization compares stable endpoint identity, machine class,
    // and every connection key supplied by the candidate. Operator note is prose,
    // not identity. Refusal always includes formatLine(candidate).
    [[nodiscard]] Authorization authorize(const CommissioningCandidate& candidate) const;

    // Convenience for enumeration/display callers. It authorizes only when the
    // endpoint key resolves to one unambiguous entry. Transports that know the
    // machine class must use the candidate overload.
    [[nodiscard]] Authorization authorize(const device::Endpoint& endpoint,
                                          device::TransportKind kind) const;
    [[nodiscard]] bool allowed(const device::Endpoint& endpoint,
                               device::TransportKind kind) const {
        return authorize(endpoint, kind).allowed;
    }

    // Upsert is keyed by stable endpoint identity. Durable storage succeeds before
    // in-memory state and epoch change. A byte-for-byte semantic no-op does not bump.
    bool upsert(const AllowlistEntry& entry, std::string* why = nullptr);
    bool erase(const EndpointIdentity& endpoint, std::string* why = nullptr);

    [[nodiscard]] static std::string formatLine(const AllowlistEntry& entry);
    [[nodiscard]] static std::optional<AllowlistEntry> parseLine(std::string_view line,
                                                                 std::string* why = nullptr);

private:
    bool persist(const std::vector<AllowlistEntry>& entries, std::string* why) const;

    std::filesystem::path path_;
    std::vector<AllowlistEntry> entries_;
    std::uint64_t epoch_ = 0;
};

}  // namespace chudware::mfg::gateway
