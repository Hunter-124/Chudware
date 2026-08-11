// First-run guided setup for Chudware account, copilot credentials, live test, and workspace.
#pragma once

#include <functional>
#include <string>

struct AppState;

namespace ui {

inline constexpr int kOnboardingVersion = 1;

struct OnboardingAccountSummary {
    bool signedIn = false;
    bool pooledKeysAllowed = false;
    std::string plan;
    long long creditBalance = 0;
    std::string detail;
};

enum class OnboardingAccountAction {
    Refresh,
    BeginLogin,
    UseCredits,
};

// Phase 4.1 owns this single account-login seam. Its implementation must open the SYSTEM browser,
// refresh `summary` after the loopback authorization returns, and handle UseCredits when the plan
// permits pooled provider keys. Until 4.1 installs the callback, onboarding says account sign-in is
// unavailable in this build and offers Skip; it never fabricates an account or leaves a dead button.
using OnboardingAccountLoginSeam =
    std::function<bool(AppState&, OnboardingAccountAction, OnboardingAccountSummary&)>;
void setOnboardingAccountLoginSeam(OnboardingAccountLoginSeam seam);

// Drawn every frame by drawSettingsModal. The gate is config.json
// onboarding.completed_version; bump kOnboardingVersion when a later flow must run again.
void drawOnboarding(AppState& state, bool appBusy);

// Locked read-merge-write through updateConfigJson. Kept public so the screenshot scenario can
// exercise the exact production persistence path against an isolated config root.
bool persistOnboardingCompletion(const char* startingWorkspace = nullptr);

// Screenshot-harness control: explicitly opens the wizard at one of its four steps, bypassing
// ambient config state without changing or faking account/provider state.
void forceOnboardingForScenario(int step);

}  // namespace ui
