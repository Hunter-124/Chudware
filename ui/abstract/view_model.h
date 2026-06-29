#pragma once
// Framework-agnostic presentation model for the AI-CAD UI seam.
// Backends (ui/imgui/, future ui/qt/) read these structs; they never talk
// to the engine directly — mutations go through ICommandDispatcher.

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace aicad::ui {

enum class DocKind : uint8_t { Part, Assembly, Drawing, Schematic, Pcb };

struct TreeNode {
    std::string id;
    std::string label;
    std::string type;
    bool suppressed = false;
    bool rolledBack = false;
    bool hasError = false;
    std::vector<TreeNode> children;
};

struct TreeModel {
    std::vector<TreeNode> roots;
    std::string selectedId;
    std::vector<std::string> multiSelection;
};

struct InspectorField {
    std::string id;
    std::string label;
    std::string kind;   // dim | combo | check | int | datum_ref | selector | text
    bool visible = true;
    bool enabled = true;
    std::string valueJson;  // serialized current value
};

struct InspectorModel {
    std::string toolId;
    std::string title;
    std::vector<InspectorField> fields;
    bool active = false;
};

struct SelectionHit {
    int bodyId = 0;
    std::string kind;   // face | edge | vertex | body
    int topoId = 0;
};

struct SelectionState {
    std::vector<SelectionHit> hits;
    std::string summary;  // status-bar text
};

enum class WorkspaceMode : uint8_t {
    Model, Sketch, SheetMetal, Weldment, Surface, Assemble, Drafting, Simulate,
    Schematic, Board
};

struct WorkspaceState {
    DocKind docKind = DocKind::Part;
    WorkspaceMode active = WorkspaceMode::Model;
};

struct DocTabView {
    std::string id;
    std::string title;
    DocKind kind = DocKind::Part;
    bool dirty = false;
};

struct DocViewModel {
    std::vector<DocTabView> tabs;
    int activeTab = 0;
    std::string docId;
    int revision = 0;
    TreeModel tree;
    InspectorModel inspector;
    SelectionState selection;
    WorkspaceState workspace;
    std::string statusMessage;
    float rebuildProgress = 1.f;  // 0..1, 1 = idle/complete
};

} // namespace aicad::ui
