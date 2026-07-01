// Native file dialogs — portable API. OS backends live in dialogs_win32.cpp /
// dialogs_linux.cpp. Filter strings use the Win32 common-dialog form: pairs of
// (description, pattern) separated by embedded NULs, terminated by a double NUL
// when written as a C string literal.
#pragma once

#include <string>

namespace platform {

// "Open" dialog. The chosen file must exist. Returns UTF-8 path or "" on cancel.
std::string openFileDialog(const char* filter);

// "Save As" dialog. suggestedName prefills the file-name box (may be nullptr).
// defExt is appended when the user types a name with no extension.
std::string saveFileDialog(const char* filter, const char* defExt, const char* suggestedName);

// Modal Yes/No/Cancel: import mesh as solid (1), graphics mesh (0), cancel (-1).
int promptImportMeshAsSolid();

}  // namespace platform
