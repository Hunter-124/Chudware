// context_ref.h — one model entity the user attached to a chat message with the composer's
// context picker (the "context cube"): a face/edge/vertex picked in the viewport, a feature or
// sketch row, an assembly component or a solid body. GL-free (host/tests/test_context_ref.cpp).
//
// The same record is the pending chip above the composer, the chip on the SENT user bubble
// (persisted with the conversation) and — through contextRefToJson — one element of the turn
// payload's `context_refs` array, which the worker resolves against the engine and renders into
// the prompt (services/agent/worker_rpc.py::_editor_context_block).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace core {

struct ContextRef {
    std::string kind;           // face | edge | vertex | body | component | feature | sketch
    std::string id;             // stable handle: face/edge sid, feature/component id, "b<N>"
    std::string docId;          // owning document; empty once it can no longer be trusted
    std::string docName;        // tab title when picked
    std::string docPath;        // saved path when picked ("" = never saved)
    std::string label;          // chip text ("Face of Boss", "Sketch1", "Wheel")
    std::string type;           // feature type for feature/sketch refs (extrude, sketch, …)
    std::string owner;          // owning feature id of a face/edge/vertex/body
    std::string ownerName;
    std::string component;      // assembly component the pick landed on
    std::string componentName;
    nlohmann::json selector;    // engine selector from doc_resolve_pick; null until resolved
    uint32_t bodyId = 0;        // viewport pick ids, valid at `revision` (0 = none)
    uint32_t topoId = 0;
    int revision = -1;
    // Transient (never persisted): a doc_resolve_pick for this ref is in flight / failed.
    bool resolving = false;
    std::string resolveError;
};

// Kinds whose identity is viewport topology (resolved through doc_resolve_pick).
bool contextRefIsTopology(const ContextRef& ref);

// Same entity: same kind in the same document, and the same pick ids at the same revision or
// the same non-empty handle. A pick that is still resolving (provisional id) therefore matches
// its own later re-pick.
bool sameContextEntity(const ContextRef& a, const ContextRef& b);

// Re-picking an attached entity detaches it. Returns true when `ref` was added.
bool toggleContextRef(std::vector<ContextRef>& refs, ContextRef ref);

// Wire/disk form (snake_case). `epoch` records the writing process: hostd doc ids are
// sequential per daemon and get reused after a restart, so an entry written by another process
// keeps its geometry but forgets docId and pick ids — the host rebinds it by docPath.
nlohmann::json contextRefToJson(const ContextRef& ref);
bool contextRefFromJson(const nlohmann::json& j, ContextRef& out);
nlohmann::json contextRefsToJson(const std::vector<ContextRef>& refs);
std::vector<ContextRef> contextRefsFromJson(const nlohmann::json& arr);

// Compact geometry of a resolved selector ("plane face, normal [0, 0, 1] (top), area 1200 mm²,
// centroid [0, 0, 20]"); empty when unresolved.
std::string contextRefGeometry(const ContextRef& ref);

// A model-facing "Context references:" block for the lanes the worker renderer does not reach
// (the explicit offline core route and a mid-run steer). Empty for no refs.
std::string contextRefsPromptText(const std::vector<ContextRef>& refs);

}  // namespace core
