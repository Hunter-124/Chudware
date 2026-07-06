// Registry smoke tests — wire into app/CMakeLists.txt in Phase 2 (not built in Phase 1).
#include "ui/command_registry.h"
#include <cassert>
#include <cstring>

int main() {
    const cmd::Command* extrude = cmd::find("extrude_boss");
    assert(extrude != nullptr);
    assert(extrude->kind == cmd::Kind::Top);
    assert(extrude->ws == cmd::Workspace::Model);
    assert(cmd::find("__no_such_cmd__") == nullptr);

    auto partWs = cmd::workspacesFor(cmd::DocType::Part);
    // Part doc-scoped workspaces: Model, Sketch3D, SheetMetal, Weldment, Surface, Simulate
    assert(partWs.size() == 6);
    assert(partWs[0] == cmd::Workspace::Model);

    // Global workspaces always available from any doc type (second cluster in the bar).
    auto gws = cmd::globalWorkspaces();
    assert(!gws.empty());
    bool hasManufacture = false, hasPartsLibrary = false;
    for (auto w : gws) {
        if (w == cmd::Workspace::Manufacture)  hasManufacture  = true;
        if (w == cmd::Workspace::PartsLibrary) hasPartsLibrary = true;
    }
    assert(hasManufacture);
    assert(hasPartsLibrary);

    // workspaceKind: Manufacture=OverlayHosting, PartsLibrary=FullViewport
    assert(cmd::workspaceKind(cmd::Workspace::Manufacture)  == cmd::WorkspaceKind::OverlayHosting);
    assert(cmd::workspaceKind(cmd::Workspace::PartsLibrary) == cmd::WorkspaceKind::FullViewport);

    // palette() dedupes by id (stable table order).
    int extrudeCount = 0;
    for (const cmd::Command* c : cmd::palette())
        if (std::strcmp(c->id, "extrude_boss") == 0) ++extrudeCount;
    assert(extrudeCount == 1);

    auto fly = cmd::flyoutChildren("prim_box");
    assert(fly.size() >= 3);

    auto groups = cmd::groupsFor(cmd::Workspace::Sketch);
    assert(!groups.empty());
    assert(groups[0] == cmd::Group::Draw);

    return 0;
}
