#include "worker_command_dispatcher.h"

#include "doc_mirror.h"
#include "worker_client.h"

#include "command_dispatcher.h"

#include <cstring>

using nlohmann::json;

namespace aicad::services {

static json dim(double v) { return json{{"value", v}, {"expr", nullptr}}; }

ui::ModelDiff WorkerCommandDispatcher::dispatch(const char* cmdId,
                                                const ui::VariantMap& /*params*/) {
    ui::ModelDiff diff;
    if (!cmdId || !*cmdId) {
        diff.error = "empty command";
        return diff;
    }
    std::string err;

    if (std::strcmp(cmdId, "file_new_part") == 0) {
        if (!mirror_.newPart(worker_, err)) {
            diff.error = err.empty() ? "doc_new failed" : err;
            return diff;
        }
        diff.ok = true;
        diff.newRevision = mirror_.revision();
        return diff;
    }
    if (std::strcmp(cmdId, "file_new_assembly") == 0) {
        if (!mirror_.newAssembly(worker_, err)) {
            diff.error = err.empty() ? "doc_new assembly failed" : err;
            return diff;
        }
        diff.ok = true;
        diff.newRevision = mirror_.revision();
        return diff;
    }
    if (std::strcmp(cmdId, "edit_undo") == 0) {
        auto r = mirror_.undo(worker_);
        diff.ok = r.ok;
        diff.newRevision = r.revision;
        if (!r.ok) diff.error = r.error_message;
        return diff;
    }
    if (std::strcmp(cmdId, "edit_redo") == 0) {
        auto r = mirror_.redo(worker_);
        diff.ok = r.ok;
        diff.newRevision = r.revision;
        if (!r.ok) diff.error = r.error_message;
        return diff;
    }
    if (std::strcmp(cmdId, "prim_box") == 0) {
        auto r = mirror_.applyOps(worker_, json::array({json{
            {"op", "add_feature"},
            {"feature", {{"type", "box"}, {"name", "Box"},
                          {"params", {{"length", dim(40)}, {"width", dim(30)}, {"height", dim(15)}}}}},
            {"before", nullptr}}}));
        diff.ok = r.ok;
        diff.newRevision = r.revision;
        if (!r.ok) diff.error = r.error_message;
        return diff;
    }

    diff.error = std::string("unhandled command: ") + cmdId;
    return diff;
}

}  // namespace aicad::services
