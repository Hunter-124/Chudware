// First-run guided setup for copilot credentials, live test, and starting workspace.
#pragma once

struct AppState;

namespace ui {

inline constexpr int kOnboardingVersion = 2;

// Drawn every frame by drawSettingsModal. The gate is config.json
// onboarding.completed_version; bump kOnboardingVersion when a later flow must run again.
void drawOnboarding(AppState& state, bool appBusy);

// Locked read-merge-write through updateConfigJson. Kept public so the screenshot scenario can
// exercise the exact production persistence path against an isolated config root.
bool persistOnboardingCompletion(const char* startingWorkspace = nullptr);

// The config-corruption warning drain shares the writer's mutex; do not drain it on the
// frame thread while an interactive onboarding completion write is in flight.
bool onboardingCompletionWritePending();

// Screenshot-harness control: explicitly opens the wizard at one of its three steps, bypassing
// ambient config state without changing or faking provider state.
void forceOnboardingForScenario(int step);

}  // namespace ui
