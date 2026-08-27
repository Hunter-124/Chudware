// Chudware Code workspace controller.
//
// Thin product adapter between ACP frames and the EXISTING maintained CAD
// copilot state/renderers. No alternate widgets live here: Code mode writes the
// same Conversation / ChatMsg / ActivityItem structures that drawAiWorkspace()
// already renders. That is what guarantees visual parity.
#pragma once

#include <string>

struct AppState;

namespace ui::code {

struct StartupOptions {
    std::string executable; // chudware-code CLI; empty => resolve from PATH
    std::string cwd;
};

// Process-level Code mode is selected before run_gui initializes AppState.
void setRequested(bool requested);
bool requested();

// GL-thread lifecycle. start() performs only process spawn + initialize request;
// all responses and notifications are applied by tick() on later frames.
bool start(AppState& st, const StartupOptions& options, std::string& err);
void tick(AppState& st);
void shutdown(AppState& st);
bool connected();

// Existing composer/session UI routes through these only in Code mode.
bool submit(AppState& st, const std::string& text, std::string& err);
bool cancel(AppState& st, std::string& err);
bool selectSession(AppState& st, const std::string& sessionId, std::string& err);
bool newSession(AppState& st, std::string& err);
bool forkSession(AppState& st, const std::string& sessionId, std::string& err);

// True capabilities in ACP v0.1. Existing buttons must be hidden/disabled when
// false; closeSession is resource release and MUST NOT be labeled delete.
struct Capabilities {
    bool list = false;
    bool load = false;
    bool resume = false;
    bool fork = false;
    bool close = false;
    bool images = false;
    bool embeddedContext = false;
};
const Capabilities& capabilities();

// Authoritative read-only values reported by ACP config options. The maintained
// composer renders these instead of stale CAD provider/model settings.
struct SessionConfiguration {
    std::string mode;
    std::string model;
    std::string thinking;
};
const SessionConfiguration& sessionConfiguration();

} // namespace ui::code
