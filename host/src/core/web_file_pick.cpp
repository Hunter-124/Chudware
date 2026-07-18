// Browser file picker + workspace browser — see web_file_pick.h.
#include "web_file_pick.h"

#include <cstdlib>
#include <cstring>
#include <string>

#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#include <emscripten/threading.h>
#endif

namespace billet {

#ifdef __EMSCRIPTEN__

// Shared result buffer filled from JS (main browser thread) and read on the
// pthread worker after the busy-wait completes.
struct PickState {
    volatile int done = 0;  // 0=pending, 1=ok, -1=cancel/error
    char         name[512] = {0};
    uint8_t*     data = nullptr;
    size_t       len  = 0;
    char         err[256] = {0};
};

static PickState g_pick;

// Workspace-browser result (path only — no file bytes).
struct BrowseState {
    volatile int done = 0;  // 0=pending, 1=ok, -1=cancel
    char         path[2048] = {0};
};

static BrowseState g_browse;

// Called from JS on the main thread once the user picks or cancels.
extern "C" EMSCRIPTEN_KEEPALIVE void billet_file_pick_done(
    int ok, const char* name, const uint8_t* data, int len, const char* errMsg) {
    if (g_pick.data) {
        free(g_pick.data);
        g_pick.data = nullptr;
        g_pick.len = 0;
    }
    if (ok && data && len > 0) {
        g_pick.data = static_cast<uint8_t*>(malloc(static_cast<size_t>(len)));
        if (g_pick.data) {
            std::memcpy(g_pick.data, data, static_cast<size_t>(len));
            g_pick.len = static_cast<size_t>(len);
        }
        if (name) {
            std::strncpy(g_pick.name, name, sizeof(g_pick.name) - 1);
            g_pick.name[sizeof(g_pick.name) - 1] = '\0';
        } else {
            g_pick.name[0] = '\0';
        }
        g_pick.err[0] = '\0';
        g_pick.done = 1;
    } else {
        g_pick.name[0] = '\0';
        if (errMsg) {
            std::strncpy(g_pick.err, errMsg, sizeof(g_pick.err) - 1);
            g_pick.err[sizeof(g_pick.err) - 1] = '\0';
        } else {
            g_pick.err[0] = '\0';
        }
        g_pick.done = -1;
    }
}

// Called from JS when the workspace browser finishes (ok=1 + path, or cancel).
extern "C" EMSCRIPTEN_KEEPALIVE void billet_ws_browse_done(int ok, const char* path) {
    if (ok && path && path[0]) {
        std::strncpy(g_browse.path, path, sizeof(g_browse.path) - 1);
        g_browse.path[sizeof(g_browse.path) - 1] = '\0';
        g_browse.done = 1;
    } else {
        g_browse.path[0] = '\0';
        g_browse.done = -1;
    }
}

// JS-library entry (host/wasm/billet_web_dialogs.js) — runs on main thread via __proxy:sync.
extern "C" void billet_ws_browse(int mode, const char* accept, const char* suggested);


bool pickFile(const char* accept, PickedFile& out, std::string& err) {
    err.clear();
    out = {};
    g_pick.done = 0;
    g_pick.name[0] = '\0';
    g_pick.err[0] = '\0';
    if (g_pick.data) {
        free(g_pick.data);
        g_pick.data = nullptr;
        g_pick.len = 0;
    }

    // Fire the <input type=file> on the browser main thread.
    MAIN_THREAD_EM_ASM({
        var accept = UTF8ToString($0);
        var input = document.createElement('input');
        input.type = 'file';
        if (accept && accept.length) input.accept = accept;
        input.style.display = 'none';
        document.body.appendChild(input);
        var finished = false;
        function finish(ok, name, data, len, errStr) {
            if (finished) return;
            finished = true;
            try { document.body.removeChild(input); } catch (e) {}
            if (ok && data && len > 0) {
                var ptr = Module._malloc(len);
                HEAPU8.set(data, ptr);
                var namePtr = 0;
                if (name) {
                    var nb = lengthBytesUTF8(name) + 1;
                    namePtr = Module._malloc(nb);
                    stringToUTF8(name, namePtr, nb);
                }
                Module._billet_file_pick_done(1, namePtr, ptr, len, 0);
                if (namePtr) Module._free(namePtr);
                Module._free(ptr);
            } else {
                var errPtr = 0;
                if (errStr) {
                    var eb = lengthBytesUTF8(errStr) + 1;
                    errPtr = Module._malloc(eb);
                    stringToUTF8(errStr, errPtr, eb);
                }
                Module._billet_file_pick_done(0, 0, 0, 0, errPtr);
                if (errPtr) Module._free(errPtr);
            }
        }
        input.addEventListener('change', function() {
            if (!input.files || input.files.length === 0) {
                finish(0, null, null, 0, null);
                return;
            }
            var f = input.files[0];
            var reader = new FileReader();
            reader.onload = function() {
                var u8 = new Uint8Array(reader.result);
                finish(1, f.name || 'upload.bin', u8, u8.length, null);
            };
            reader.onerror = function() {
                finish(0, null, null, 0, 'FileReader failed');
            };
            reader.readAsArrayBuffer(f);
        });
        // Cancel detection: focus returns without a change event.
        function onFocus() {
            setTimeout(function() {
                if (!finished) finish(0, null, null, 0, null);
                window.removeEventListener('focus', onFocus);
            }, 400);
        }
        window.addEventListener('focus', onFocus);
        input.click();
    }, accept ? accept : "");

    // Busy-wait: PROXY_TO_PTHREAD worker can block; main browser thread stays free
    // for the FileReader callbacks.
    while (g_pick.done == 0) {
        emscripten_thread_sleep(20);
    }

    if (g_pick.done < 0) {
        if (g_pick.err[0]) err = g_pick.err;
        return false;
    }
    out.name = g_pick.name;
    if (g_pick.data && g_pick.len > 0) {
        out.data.assign(g_pick.data, g_pick.data + g_pick.len);
    }
    if (g_pick.data) {
        free(g_pick.data);
        g_pick.data = nullptr;
        g_pick.len = 0;
    }
    return !out.name.empty();
}

std::string promptSaveFilename(const char* suggested) {
    char buf[512];
    buf[0] = '\0';
    const char* sug = (suggested && *suggested) ? suggested : "export.bin";
    // window.prompt must run on the browser main thread under PROXY_TO_PTHREAD.
    int ok = MAIN_THREAD_EM_ASM_INT({
        var sug = UTF8ToString($0);
        var r = window.prompt('Save as (server workspace filename):', sug);
        if (r === null || r === undefined) return 0;
        r = ('' + r).trim();
        if (!r) return 0;
        var n = lengthBytesUTF8(r) + 1;
        if (n > 511) n = 511;
        stringToUTF8(r, $1, n);
        return 1;
    }, sug, buf);
    if (!ok) return {};
    return std::string(buf);
}

std::string browseWorkspace(WorkspaceBrowseMode mode,
                            const char* accept,
                            const char* suggestedName) {
    g_browse.done = 0;
    g_browse.path[0] = '\0';
    const int modeInt = static_cast<int>(mode);
    const char* acc = (accept && *accept) ? accept : "";
    const char* sug = (suggestedName && *suggestedName) ? suggestedName : "";

    billet_ws_browse(modeInt, acc, sug);

    while (g_browse.done == 0) {
        emscripten_thread_sleep(20);
    }
    if (g_browse.done < 0 || !g_browse.path[0]) return {};
    return std::string(g_browse.path);
}

#else  // !__EMSCRIPTEN__

bool pickFile(const char* /*accept*/, PickedFile& out, std::string& err) {
    out = {};
    err = "pickFile is only available in the web build";
    return false;
}

std::string promptSaveFilename(const char* /*suggested*/) {
    return {};
}

std::string browseWorkspace(WorkspaceBrowseMode /*mode*/,
                            const char* /*accept*/,
                            const char* /*suggestedName*/) {
    return {};
}

#endif

}  // namespace billet
