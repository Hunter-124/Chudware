#pragma once
#include "command_dispatcher.h"
#include "variant.h"

class WorkerClient;
namespace core { class DocMirror; }

namespace aicad::services {

// Routes stable cmd_id values to WorkerClient + DocMirror (libaicad with Python fallback).
class WorkerCommandDispatcher : public ui::ICommandDispatcher {
public:
    WorkerCommandDispatcher(WorkerClient& worker, core::DocMirror& mirror)
        : worker_(worker), mirror_(mirror) {}

    ui::ModelDiff dispatch(const char* cmdId,
                           const ui::VariantMap& params = {}) override;

    WorkerClient& worker() { return worker_; }
    core::DocMirror& mirror() { return mirror_; }

private:
    WorkerClient& worker_;
    core::DocMirror& mirror_;
};

}  // namespace aicad::services
