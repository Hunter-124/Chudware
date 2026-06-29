#pragma once
#include "command_dispatcher.h"
#include "variant.h"
#include <string>
#include <unordered_map>

namespace aicad::ui {

enum class ActionSource { Toolbar, Palette, Menu, Shortcut, Agent, Viewport };

struct Action {
    std::string cmdId;
    VariantMap params;
    ActionSource source = ActionSource::Toolbar;
};

// Routes UI input events to registered handlers or the command dispatcher.
class ActionBus {
public:
    void setDispatcher(ICommandDispatcher* dispatcher) { dispatcher_ = dispatcher; }

    void registerHandler(const char* cmdId, CommandHandler handler) {
        handlers_[cmdId] = std::move(handler);
    }

    ModelDiff submit(const Action& action) {
        auto it = handlers_.find(action.cmdId);
        if (it != handlers_.end()) {
            it->second(action.cmdId.c_str(), action.params);
            return ModelDiff{true};
        }
        if (dispatcher_)
            return dispatcher_->dispatch(action.cmdId.c_str(), action.params);
        return ModelDiff{false, "no handler and no dispatcher"};
    }

private:
    ICommandDispatcher* dispatcher_ = nullptr;
    std::unordered_map<std::string, CommandHandler> handlers_;
};

} // namespace aicad::ui
