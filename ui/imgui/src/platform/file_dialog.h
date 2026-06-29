// Native file dialogs (Windows: comdlg32). Returns a UTF-8 path, or empty if cancelled.
#pragma once
#include <string>
namespace platform {
std::string openCadFile();                      // Open: .step+/.step/.iges/.stl/...
std::string saveCadFile(const std::string& defaultName);  // Save As: defaults to .step+
}
