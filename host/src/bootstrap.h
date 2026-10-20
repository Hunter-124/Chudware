// Development-only offline runtime provisioning from a trusted source payload,
// reviewed wheel lock and complete wheelhouse. Customer releases use compiled sidecars.
// Managed checks require a separately authenticated CHUDWARE_BASE_PYTHON.
// Explicit CHUDWARE_PYTHON overrides remain caller-owned, not managed proof.
#pragma once
#include <string>

std::string exeDir();

// Resolved python.exe the worker will launch: CHUDWARE_PYTHON override, the installed
// %APPDATA%/Chudware venv, or a portable runtime next to the exe — first that exists,
// else the canonical %APPDATA% path (for messaging).
std::string runtimePython();

// Resolved working dir that holds the engine/agent/services.mcp packages (cwd for the worker).
std::string appDir();

bool runtimeReady();                     // independent base proves executable/platform + locked package identities
bool ensureRuntime(std::string& log);    // provision if absent; true once ready
bool repairRuntime(std::string& log);    // force a clean re-provision (broken/partial venv)

// Developer builds default CHUDWARE_PROMPTS_DIR to the authored prompt tree.
// Packaged builds use compiled assets, so this is intentionally a no-op there.
void ensurePromptsDir();
