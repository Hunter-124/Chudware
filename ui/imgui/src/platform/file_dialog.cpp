#include "platform/file_dialog.h"

#include <platform/dialogs.h>

#include <cstdlib>
#include <string>

namespace platform {
namespace {

const char* kCadFilter =
    "AI-CAD / CAD files\0*.step+;*.step;*.stp;*.iges;*.igs;*.brep;*.stl;*.3mf;*.obj\0"
    "AI-CAD native (.step+)\0*.step+\0"
    "All files\0*.*\0";

bool headlessDialogs() { return std::getenv("AI_CAD_NO_FILE_DIALOGS") != nullptr; }

}  // namespace

std::string openCadFile() {
    if (headlessDialogs()) return {};
    return openFileDialog(kCadFilter);
}

std::string saveCadFile(const std::string& defaultName) {
    if (headlessDialogs()) return {};
    std::string suggested = defaultName.empty() ? "Part1.step+" : defaultName;
    return saveFileDialog(kCadFilter, "step+", suggested.c_str());
}

}  // namespace platform
