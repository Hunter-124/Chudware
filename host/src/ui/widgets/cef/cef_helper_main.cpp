// cef/cef_helper_main.cpp — the CEF subprocess helper (chudware_cef_helper).
//
// SCAFFOLDING (docs/cef_webview.md phase 2); built only with CHUDWARE_ENABLE_CEF.
//
// CEF's render/GPU/utility processes re-exec THIS binary. Its whole job is to run
// CefExecuteProcess and return the code — it must never start the GUI. Keeping it
// a separate executable (rather than re-execing Chudware) avoids dragging GLFW/GL
// init into every subprocess.
#include "cef_app.h"

int main(int argc, char** argv) {
    return ui::cef::execute_subprocess(argc, argv);
}
