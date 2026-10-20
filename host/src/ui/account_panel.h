#pragma once

#include <cstdint>
#include <mutex>
#include <string>
#include <utility>

struct AppState;

namespace ui {

namespace account_detail {

enum class Operation { None, Refresh, Login };

struct PendingResult {
    enum class Kind { None, Fresh, Refused, Unreachable, Invalid } kind = Kind::None;
    std::string token;
    std::string detail;
    std::string securityPolicy;
    std::string enforcementState;
    std::uint64_t generation = 0;
    Operation operation = Operation::None;
    std::string acknowledgedEnforcement;
    std::string batchId;
    std::uint64_t batchSequence = 0;
};

// The UI thread owns launch/invalidate/consume and every session side effect. Workers
// only call current/complete. This mutex protects mailbox metadata, never network,
// credential storage, queue I/O, GUI mutation or joining a live worker.
class ResultMailbox {
public:
    std::uint64_t begin(Operation operation) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (operation == Operation::None || running_ == Operation::Login ||
            (operation == Operation::Refresh &&
             (running_ != Operation::None || pending_.kind != PendingResult::Kind::None)))
            return 0;
        ++generation_;
        running_ = operation;
        pending_ = {};
        return generation_;
    }

    void invalidate() {
        std::lock_guard<std::mutex> lock(mutex_);
        ++generation_;
        running_ = Operation::None;
        pending_ = {};
    }

    bool current(std::uint64_t generation) const {
        std::lock_guard<std::mutex> lock(mutex_);
        return generation == generation_;
    }

    Operation running() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return running_;
    }

    bool complete(PendingResult result) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (result.generation != generation_ || running_ == Operation::None)
            return false;
        result.operation = running_;
        pending_ = std::move(result);
        running_ = Operation::None;
        return true;
    }

    template <typename Apply>
    bool consume(Apply&& apply) {
        PendingResult result;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (pending_.kind == PendingResult::Kind::None) return false;
            result = std::move(pending_);
            pending_ = {};
        }
        // launch/invalidate and this callback are serialized by their UI-thread owner.
        // No worker can change the generation while current results are committed.
        std::forward<Apply>(apply)(std::move(result));
        return true;
    }

private:
    mutable std::mutex mutex_;
    std::uint64_t generation_ = 0;
    Operation running_ = Operation::None;
    PendingResult pending_;
};

}  // namespace account_detail

// Loads and independently verifies the stored token, installs the onboarding
// seam, and starts the launch refresh. Call once after app data paths exist.
void initializeAccountPanel(AppState& state);
// Applies only current-generation background results and schedules the 12-hour check-in.
void tickAccountPanel(AppState& state);
// Read-only screenshot-harness observation. Call after tick: idle means every
// worker was actually reaped, not merely invalidated by sign-out.
bool desktopAccountRequestsIdle();
// Settings > General account controls.
void drawAccountSettings(AppState& state);
// Read-only entitlement notice. This is RESERVED CHROME, not an overlay: app.cpp measures it
// with entitlementBannerHeight(), folds that height into dockTopY before ui::setOverlayBand(),
// and paints the strip itself between the ribbon and the dockspace. It was previously a floating
// decoration-less window anchored inside the overlay band, which meant it covered whatever
// happened to be at the top-left of the active workspace — on the PLM hub that was the project
// breadcrumb, the Refresh button and the search field. Reserving the height is the only fix that
// holds for EVERY workspace, including ones with no pinned columns to hide behind.
//
// entitlementBannerHeight() returns 0 when the account is in good standing, and must be callable
// before any panel has drawn. drawEntitlementBanner() paints the strip at the given rect.
float entitlementBannerHeight(const AppState& state);
void  drawEntitlementBanner(AppState& state, float x, float y, float w, float h);
// System-browser loopback login. Supersedes old refresh work without waiting for its network I/O.
bool beginDesktopAccountLogin(AppState& state);
// Forgets this device's stored subscription session (sealed token, subject, signed security
// policy) and invalidates in-flight results before erasing storage. Workers cannot persist
// session state. The device identity survives; read-only mode resumes immediately.
void signOutDesktopAccount(AppState& state);

}  // namespace ui
