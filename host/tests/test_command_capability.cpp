#include "ui/ribbon_def.h"

#include <iostream>

// Ribbon command classification. CommandRouter::authorize blocks Mutate commands while an
// agent holds the active document, so an unknown command must classify as Mutate and
// inspection/export commands must stay available.
int main() {
    using ui::CommandCapability;
    int failures = 0;
    auto check = [&failures](bool condition, const char* label) {
        if (!condition) {
            std::cerr << "FAIL: " << label << '\n';
            ++failures;
        }
    };
    check(ui::ribbonCommandCapability("extrude_boss") == CommandCapability::Mutate,
          "edit command is mutating");
    check(ui::ribbonCommandCapability("new_command_added_later") == CommandCapability::Mutate,
          "unknown command fails closed");
    check(ui::ribbonCommandCapability("eval_measure") == CommandCapability::Inspect,
          "measure is inspect-only");
    check(ui::ribbonCommandCapability("display_wireframe") == CommandCapability::Inspect,
          "view is inspect-only");
    check(ui::ribbonCommandCapability("sm_export_flat_dxf") == CommandCapability::Export,
          "flat export is export");
    check(ui::ribbonCommandCapability("dwg_export_pdf") == CommandCapability::Export,
          "drawing export is export");
    if (failures) return 1;
    std::cout << "command capability classification: PASS\n";
    return 0;
}
