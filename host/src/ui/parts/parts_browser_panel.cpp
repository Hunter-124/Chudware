// Parts Library — embedded vendor-browser panel (GUI V2 phase C).
// Phase A stub: never active; drawPartsLibraryWorkspace swaps the results column for this
// panel when browserPanelActive() goes true in a later phase.
#include "parts_internal.h"

struct AppState;

namespace ui {
namespace parts {

bool browserPanelActive() { return false; }

void drawBrowserPanel(AppState&) {}

}  // namespace parts
}  // namespace ui
