#include "ui/ribbon_def.h"

#include <iostream>

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
          "measure remains enabled");
    check(ui::ribbonCommandCapability("display_wireframe") == CommandCapability::Inspect,
          "view remains enabled");
    check(ui::ribbonCommandCapability("sm_export_flat_dxf") == CommandCapability::Export,
          "flat export remains enabled");
    check(ui::ribbonCommandCapability("dwg_export_pdf") == CommandCapability::Export,
          "drawing export remains enabled");
    if (failures) return 1;
    std::cout << "entitlement command capabilities: PASS\n";
    return 0;
}
