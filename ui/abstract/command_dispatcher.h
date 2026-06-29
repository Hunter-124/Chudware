#pragma once
#include "variant.h"
#include <functional>
#include <string>

namespace aicad::ui {

// Dispatches a stable engine cmd_id (+ optional params) to the worker seam.
// ImGui/Qt backends call this; the implementation lives in services/worker (N4a).
class ICommandDispatcher {
public:
    virtual ~ICommandDispatcher() = default;
    virtual ModelDiff dispatch(const char* cmdId,
                               const VariantMap& params = {}) = 0;
};

// Callback type for action-bus subscribers (palette, toolbar, menus share this).
using CommandHandler = std::function<void(const char* cmdId, const VariantMap& params)>;

} // namespace aicad::ui
