#include "manufacturing/lease.h"

#include <limits>

namespace chudware::mfg {
namespace {

uint64_t expiry_ns(uint64_t now_mono_ns, uint64_t ttl_ms) {
    constexpr uint64_t per_ms = 1000000;
    if (ttl_ms > (std::numeric_limits<uint64_t>::max() - now_mono_ns) / per_ms)
        return std::numeric_limits<uint64_t>::max();
    return now_mono_ns + ttl_ms * per_ms;
}

nlohmann::json owner_json(const LeaseOwner& owner) {
    return {{"client_kind", owner.client_kind}, {"seat", owner.seat}, {"session", owner.session}};
}

}  // namespace

void LeaseRegistry::expire_locked(uint64_t now_mono_ns) {
    for (auto it = by_device_.begin(); it != by_device_.end();) {
        if (now_mono_ns < it->second.expires_at_mono_ns) {
            ++it;
            continue;
        }
        const auto lease_id = it->second.lease_id;
        journal_.append("mfg.device.lease", it->first,
                        {{"lease_id", lease_id}, {"change", "expired"}});
        device_by_lease_.erase(lease_id);
        it = by_device_.erase(it);
    }
}

LeaseOutcome LeaseRegistry::claim(std::string device_id, LeaseOwner owner,
                                  uint64_t now_mono_ns, uint64_t ttl_ms,
                                  uint64_t heartbeat_ms, std::string transport_kind,
                                  std::string exclusive_endpoint) {
    std::lock_guard lock(mutex_);
    expire_locked(now_mono_ns);
    if (device_id.empty() || owner.client_kind.empty() || owner.seat.empty() ||
        owner.session.empty() || ttl_ms == 0 || heartbeat_ms == 0 || heartbeat_ms >= ttl_ms) {
        return {false, "lease.invalid_request",
                "device, complete owner identity, and heartbeat_ms < ttl_ms are required",
                std::nullopt, std::nullopt};
    }
    const auto occupied = by_device_.find(device_id);
    if (occupied != by_device_.end()) {
        return {false, "lease.takeover_refused",
                "device already has an unexpired commander lease",
                std::nullopt, occupied->second.owner};
    }

    CommandLease lease;
    lease.lease_id = device::LeaseId::mint().str();
    lease.device_id = std::move(device_id);
    lease.owner = std::move(owner);
    lease.ttl_ms = ttl_ms;
    lease.heartbeat_ms = heartbeat_ms;
    lease.granted_at_mono_ns = now_mono_ns;
    lease.expires_at_mono_ns = expiry_ns(now_mono_ns, ttl_ms);
    lease.transport_kind = std::move(transport_kind);
    lease.exclusive_endpoint = std::move(exclusive_endpoint);
    lease.granted_at_seq = journal_.append(
        "mfg.device.lease", lease.device_id,
        {{"lease_id", lease.lease_id}, {"change", "granted"},
         {"owner", owner_json(lease.owner)}, {"role", lease.role},
         {"ttl_ms", lease.ttl_ms}, {"heartbeat_ms", lease.heartbeat_ms}});
    device_by_lease_.emplace(lease.lease_id, lease.device_id);
    by_device_.emplace(lease.device_id, lease);
    return {true, {}, {}, lease, std::nullopt};
}

LeaseOutcome LeaseRegistry::renew(std::string_view lease_id, const LeaseOwner& owner,
                                  uint64_t now_mono_ns, uint64_t ttl_ms) {
    std::lock_guard lock(mutex_);
    expire_locked(now_mono_ns);
    const auto index = device_by_lease_.find(lease_id);
    if (index == device_by_lease_.end())
        return {false, "lease.not_found_or_expired", "lease is absent or expired", std::nullopt, std::nullopt};
    auto& lease = by_device_.at(index->second);
    if (!(lease.owner == owner))
        return {false, "lease.owner_mismatch", "only the current owner may renew the lease",
                std::nullopt, lease.owner};
    if (ttl_ms == 0)
        return {false, "lease.invalid_ttl", "renewal ttl_ms must be nonzero", std::nullopt, lease.owner};
    lease.ttl_ms = ttl_ms;
    lease.expires_at_mono_ns = expiry_ns(now_mono_ns, ttl_ms);
    journal_.append("mfg.device.lease", lease.device_id,
                    {{"lease_id", lease.lease_id}, {"change", "renewed"},
                     {"ttl_ms", ttl_ms}, {"owner", owner_json(owner)}});
    return {true, {}, {}, lease, std::nullopt};
}

LeaseOutcome LeaseRegistry::release(std::string_view lease_id, const LeaseOwner& owner,
                                    uint64_t now_mono_ns) {
    std::lock_guard lock(mutex_);
    expire_locked(now_mono_ns);
    const auto index = device_by_lease_.find(lease_id);
    if (index == device_by_lease_.end())
        return {false, "lease.not_found_or_expired", "lease is absent or expired", std::nullopt, std::nullopt};
    const auto lease = by_device_.at(index->second);
    if (!(lease.owner == owner))
        return {false, "lease.owner_mismatch", "only the current owner may release the lease",
                std::nullopt, lease.owner};
    journal_.append("mfg.device.lease", lease.device_id,
                    {{"lease_id", lease.lease_id}, {"change", "released"},
                     {"owner", owner_json(owner)}});
    by_device_.erase(index->second);
    device_by_lease_.erase(index);
    return {true, {}, {}, lease, std::nullopt};
}

bool LeaseRegistry::held_by(std::string_view device_id, const LeaseOwner& owner,
                            uint64_t now_mono_ns) {
    std::lock_guard lock(mutex_);
    expire_locked(now_mono_ns);
    const auto it = by_device_.find(device_id);
    return it != by_device_.end() && it->second.owner == owner;
}

std::optional<CommandLease> LeaseRegistry::describe(std::string_view device_id,
                                                    uint64_t now_mono_ns) {
    std::lock_guard lock(mutex_);
    expire_locked(now_mono_ns);
    const auto it = by_device_.find(device_id);
    if (it == by_device_.end()) return std::nullopt;
    return it->second;
}

}  // namespace chudware::mfg
