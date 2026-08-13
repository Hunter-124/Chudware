#include "windows_update_helper.h"

#if defined(_WIN32)
#include <windows.h>

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    return chudware::update::runWindowsUpdateHelper(__argc, __wargv);
}
#endif
