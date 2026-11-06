// GUI mirror of document-owned mesh references. All functions run on the GL thread.
#pragma once

#include <string>
#include <nlohmann/json.hpp>

struct AppState;

namespace ui {
struct MeshReferenceView {
    std::string docId;
    nlohmann::json refs = nlohmann::json::array();
    std::string error;
    bool loading = false;
    bool writing = false;
};

const MeshReferenceView& meshReferenceView();
bool meshReferenceLoaded(const std::string& id);
void tickMeshReferences(AppState& st);
void mutateMeshReference(AppState& st, const char* method, nlohmann::json params);
// Reconcile an out-of-band mutation (e.g. the reverse-engineering worker) with the mirror.
void refreshMeshReferences(AppState& st, const std::string& docId);
void meshReferenceMutationCompleted(AppState& st, const std::string& docId, int revision);
}  // namespace ui
