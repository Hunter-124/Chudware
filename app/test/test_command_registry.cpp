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
    assert(partWs.size() == 5);
    assert(partWs[0] == cmd::Workspace::Model);

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
