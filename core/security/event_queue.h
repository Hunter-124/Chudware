#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>
#include <utility>

namespace chudware::security {

inline constexpr std::size_t kSecurityQueueMaxBytes = 1024 * 1024;
inline constexpr std::size_t kSecurityQueueMaxEvents = 1000;
inline constexpr std::size_t kSecurityBatchMaxBytes = 64 * 1024;
inline constexpr std::size_t kSecurityBatchMaxEvents = 100;

struct SecurityEvent {
    std::string reason;
    std::int64_t occurredAt = 0;
    unsigned score = 0;
};

struct SecurityBatch {
    std::string batchId;
    std::uint64_t sequence = 0;
    std::string body;
    std::size_t eventCount = 0;
    bool heartbeat = false;
    explicit operator bool() const noexcept { return sequence != 0 && !body.empty(); }
};

class EventQueue {
public:
    explicit EventQueue(std::string dataDir);

    // Returns false rather than losing nonzero integrity evidence.
    bool append(const SecurityEvent& event, std::string* error = nullptr);
    // Sequence state is bound to the device subject it was issued under. A new subject (a
    // fresh device authorization, which the gateway starts at sequence 0) restarts the
    // sequence; queued evidence rows are kept and ride the next batch.
    SecurityBatch prepareBatch(std::string_view subject, std::string_view policyId,
                               std::string_view appVersion, std::string_view platform,
                               std::int64_t now, std::string* error = nullptr);
    bool accept(std::string_view batchId, std::uint64_t acceptedThrough,
                std::string* error = nullptr);
    // 0 when the stored sequence state belongs to a different subject.
    std::uint64_t acceptedThrough(std::string_view subject) const;
    std::size_t size() const;

private:
    std::string dataDir_;
};

class SecurityReporter {
public:
    explicit SecurityReporter(std::string dataDir) : queue_(std::move(dataDir)) {}
    bool report(std::string_view reason, std::int64_t occurredAt,
                std::string* error = nullptr);
    bool heartbeat(std::int64_t occurredAt, std::string* error = nullptr);
    EventQueue& queue() noexcept { return queue_; }
private:
    EventQueue queue_;
};
// Verifies a signed gateway acknowledgement bound to this batch. `status` receives
// "acknowledged", "contained" or "reauthorize". A reauthorize answer carries the gateway's
// current sequence (never below ours) instead of echoing the batch sequence.
bool verifySecurityAcknowledgement(std::string_view body, std::string_view batchId,
                                   std::string_view subject, std::string_view policyId,
                                   std::uint64_t sequence,
                                   std::string* enforcementState = nullptr,
                                   std::string* status = nullptr);
bool gzipSecurityPayload(std::string_view input, std::string& output);

unsigned securityEventScore(std::string_view reason) noexcept;
bool approvedSecurityReason(std::string_view reason) noexcept;

}  // namespace chudware::security
