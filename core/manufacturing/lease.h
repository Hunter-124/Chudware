#pragma once

#include <cstdint>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>

#include "device/device_id.h"
#include "manufacturing/journal.h"

namespace chudware::mfg {

struct LeaseOwner {
    std::string client_kind;
    std::string seat;
    std::string session;

    friend bool operator==(const LeaseOwner&, const LeaseOwner&) = default;
};

struct CommandLease {
    static constexpr uint64_t default_ttl_ms = 15000;
    static constexpr uint64_t max_ttl_ms = 60000;
    std::string lease_id;
    std::string device_id;
    LeaseOwner owner;
    std::string role = "commander";
    device::Seq granted_at_seq = 0;
    uint64_t granted_at_mono_ns = 0;
    uint64_t ttl_ms = default_ttl_ms;
    uint64_t heartbeat_ms = 3000;
    uint64_t expires_at_mono_ns = 0;
    std::string transport_kind;
    std::string exclusive_endpoint;
};

struct LeaseOutcome {
    bool ok = false;
    std::string code;
    std::string why;
    std::optional<CommandLease> lease;
    std::optional<LeaseOwner> current_owner;
};

class LeaseRegistry {
public:
    explicit LeaseRegistry(Journal& journal) : journal_(journal) {}

    LeaseOutcome claim(std::string device_id, LeaseOwner owner, uint64_t now_mono_ns,
                       uint64_t ttl_ms = CommandLease::default_ttl_ms, uint64_t heartbeat_ms = 3000,
                       std::string transport_kind = {}, std::string exclusive_endpoint = {});
    LeaseOutcome renew(std::string_view lease_id, const LeaseOwner& owner,
                       uint64_t now_mono_ns, uint64_t ttl_ms);
    LeaseOutcome release(std::string_view lease_id, const LeaseOwner& owner,
                         uint64_t now_mono_ns);
    bool held_by(std::string_view device_id, const LeaseOwner& owner,
                 uint64_t now_mono_ns);
    std::optional<CommandLease> describe(std::string_view device_id,
                                        uint64_t now_mono_ns);

private:
    void expire_locked(uint64_t now_mono_ns);

    Journal& journal_;
    std::mutex mutex_;
    std::map<std::string, CommandLease, std::less<>> by_device_;
    std::map<std::string, std::string, std::less<>> device_by_lease_;
};

}  // namespace chudware::mfg
