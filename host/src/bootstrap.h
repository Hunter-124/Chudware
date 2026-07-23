// First-run provisioning: ensure the %APPDATA%/Chudware runtime exists, provisioning it from
// the payload next to the exe (app/ + wheelhouse/), the CHUDWARE_REPO source tree, or a dev
// checkout discovered by walking up from the exe.
#pragma once
#include <string>

std::string exeDir();

// Resolved python.exe the worker will launch: CHUDWARE_PYTHON override, the installed
// %APPDATA%/Chudware venv, or a portable runtime next to the exe — first that exists,
// else the canonical %APPDATA% path (for messaging).
std::string runtimePython();

// Resolved working dir that holds the engine/agent/mcp_server packages (cwd for the worker).
std::string appDir();

bool runtimeReady();                     // python file exists at runtimePython()
bool ensureRuntime(std::string& log);    // provision if absent; true once ready
bool repairRuntime(std::string& log);    // force a clean re-provision (broken/partial venv)

// Default CHUDWARE_PROMPTS_DIR (if unset) to the app's bundled agent/prompts so the C++ core
// finds its role prompts regardless of the process CWD. MUST be called unconditionally at
// startup (ensureRuntime is skipped when the runtime is already provisioned).
void ensurePromptsDir();
