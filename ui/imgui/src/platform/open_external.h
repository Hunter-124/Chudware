// Open a file/URI with the OS default application (T-F16 preview pane "Open" / "Open externally").
// Non-blocking: fires the OS handler and returns immediately.
#pragma once

#include <string>

namespace ui {

void openPathExternally(const std::string& pathOrUri);

}  // namespace ui
