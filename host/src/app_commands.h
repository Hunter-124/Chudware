// app_commands.h — ribbon command router.
//
// Replaces the monolithic dispatch if-chain that used to live in app.cpp's dispatchCmd lambda.
// Workspace families register ordered rules (exact id / id prefix / match-any) mapping a command
// to a handler; dispatch walks the rules IN REGISTRATION ORDER and the first handler that returns
// true consumes the command. Registration order is semantically load-bearing: it mirrors the
// original if-chain top-to-bottom, so fall-through cases (a dwg_* command drawingHandleCommand
// declines still reaching PropertyManager::openForCommand, a sim_* command SimManager declines
// reaching the stub log) behave exactly as before.
//
// Deliberately a plain handler table — no framework, no view-model layer. App-level policy
// (prof_toggle, the Simulation lockout, PM-cancel-on-sketch_new, the unhandled-stub log) stays in
// app.cpp's dispatchCmd and runs before/after the router.
#pragma once

#include <functional>
#include <string>
#include <vector>

struct AppState;

using CommandHandler = std::function<bool(AppState&, const std::string&)>;

// Ordered ribbon-command rule table. Handlers take the live AppState (st.* subsystems are
// re-seated to the ACTIVE document on every tab switch, so handlers must resolve them through
// the AppState& argument at call time, never capture them) and return true when they consumed
// the command, false to let dispatch fall through to the next matching rule.
class CommandRouter {
public:
    using Handler = CommandHandler;

    void registerExact(const char* id, Handler h);      // matches cmd == id
    void registerPrefix(const char* prefix, Handler h); // matches cmd starting with prefix
    void registerAny(Handler h);                        // matches every command (try-first hooks)

    // Agent-lock choke point shared by ribbon, shortcut bar, and gesture wheel: mutating
    // commands are refused while an agent owns the active document. Unknown commands
    // classify as mutating and therefore fail closed.
    bool authorize(AppState& st, const char* cmdId) const;

    // Walks rules in registration order; returns true when a rule consumed the command.
    bool dispatch(AppState& st, const char* cmdId) const;

private:
    enum class Kind { Exact, Prefix, Any };
    struct Rule { Kind kind; std::string key; Handler handler; };
    std::vector<Rule> rules_;
};

// App-level family handlers live in app.cpp (app-level policy stays with the app, and the
// command-id literals there double as feature_surface_audit evidence). They are handed to the
// table builder as std::function hooks; ecadDispatch (the app.cpp ECAD helper the ecad_/sch_/pcb_
// prefix rule wraps) travels as a plain function pointer.
struct RibbonCommandHooks {
    CommandHandler editCopy, editCut, editPaste, editDelete, editSelectAll;  // Wave 4C/4D + 5-fill
    CommandHandler makeDrawing;       // "Make Drawing" -> Drawing tab
    CommandHandler displayStyle;      // Appearance tab: Display group
    CommandHandler appearance;        // Appearance tab: Material group
    CommandHandler flatPattern;       // W16: one-click Flatten
    CommandHandler flatDxf;           // W14: flat-pattern DXF
    void (*ecadDispatch)(AppState&, const std::string& cmd);  // ECAD command routing
};

// Builds the ribbon command table. Rule registration order reproduces the original dispatchCmd
// if-chain exactly — do not reorder without auditing the fall-through cases listed above.
CommandRouter buildRibbonCommandRouter(const RibbonCommandHooks& hooks);
