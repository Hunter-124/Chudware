// Browser file picker + workspace browser — see web_file_pick.h.
#include "web_file_pick.h"
#include "fs_backend.h"

#include <cstdlib>
#include <cstring>
#include <string>

#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

namespace chudware {

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
extern "C" EMSCRIPTEN_KEEPALIVE void chudware_file_pick_done(
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
extern "C" EMSCRIPTEN_KEEPALIVE void chudware_ws_browse_done(int ok, const char* path) {
    std::string relative, err;
    if (ok && path && (std::strcmp(path, "__UPLOAD__") == 0 ||
                      workspaceRelativePath(path, relative, err)) &&
        std::strlen(path) < sizeof(g_browse.path)) {
        const std::string result = std::strcmp(path, "__UPLOAD__") == 0
                                       ? "__UPLOAD__" : (relative.empty() ? "." : relative);
        std::memcpy(g_browse.path, result.c_str(), result.size() + 1);
        g_browse.done = 1;
    } else {
        g_browse.path[0] = '\0';
        g_browse.done = -1;
    }
}

// JS-library entry (host/wasm/chudware_web_dialogs.js) — runs on main thread via __proxy:sync.
extern "C" void chudware_ws_browse(int mode, const char* accept, const char* suggested);


// Take a pick result already posted by JS (upload button / external deliver).
// Does not open a file picker and does not sleep.
bool consumePickedFile(PickedFile& out, std::string& err) {
    err.clear();
    out = {};
    if (g_pick.done == 0) {
        err = "no pending upload";
        return false;
    }
    if (g_pick.done < 0) {
        if (g_pick.err[0]) err = g_pick.err;
        g_pick.done = 0;
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
    g_pick.done = 0;
    g_pick.name[0] = '\0';
    return !out.name.empty() && !out.data.empty();
}

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

    // Already on the browser main thread (main-thread ImGui + ASYNCIFY, no
    // PROXY_TO_PTHREAD). MAIN_THREAD_EM_ASM from the main thread can re-enter
    // under ASYNCIFY and blow the stack — use plain EM_ASM (see app.cpp note).
    EM_ASM({
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
            try { window.removeEventListener('focus', onFocus); } catch (e) {}
            if (typeof Module === 'undefined' || !Module._chudware_file_pick_done) return;
            if (ok && data && len > 0) {
                var ptr = Module._malloc(len);
                if (!ptr) {
                    Module._chudware_file_pick_done(0, 0, 0, 0, 0);
                    return;
                }
                var heap = Module.HEAPU8;
                var CHUNK = 1 << 20;
                for (var off = 0; off < len; off += CHUNK) {
                    var end = off + CHUNK;
                    if (end > len) end = len;
                    heap.set(data.subarray(off, end), ptr + off);
                }
                var namePtr = 0;
                if (name) {
                    var nb = lengthBytesUTF8(name) + 1;
                    namePtr = Module._malloc(nb);
                    stringToUTF8(name, namePtr, nb);
                }
                Module._chudware_file_pick_done(1, namePtr, ptr, len, 0);
                if (namePtr) Module._free(namePtr);
                Module._free(ptr);
            } else {
                var errPtr = 0;
                if (errStr) {
                    var eb = lengthBytesUTF8(errStr) + 1;
                    errPtr = Module._malloc(eb);
                    stringToUTF8(errStr, errPtr, eb);
                }
                Module._chudware_file_pick_done(0, 0, 0, 0, errPtr);
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
        // Cancel detection: focus returns without a change event. Long delay so
        // slow native picker UI / FileReader start isn't raced (was 400ms).
        function onFocus() {
            setTimeout(function() {
                if (!finished) finish(0, null, null, 0, null);
                try { window.removeEventListener('focus', onFocus); } catch (e) {}
            }, 1500);
        }
        window.addEventListener('focus', onFocus);
        // May be blocked without a user gesture — prefer the workspace-dialog
        // upload button path which opens the input inside the click handler.
        try { input.click(); } catch (e) {
            finish(0, null, null, 0, 'File picker blocked');
        }
    }, accept ? accept : "");

    // ASYNCIFY yield (main-thread ImGui model — no PROXY_TO_PTHREAD): unwind back to the
    // browser event loop so the <input> change / FileReader callbacks can run, then resume.
    // emscripten_thread_sleep would spin the main thread and deadlock the picker.
    while (g_pick.done == 0) {
        emscripten_sleep(20);
    }

    return consumePickedFile(out, err);
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

    chudware_ws_browse(modeInt, acc, sug);

    // ASYNCIFY yield: let the overlay's fetch()/click handlers run on the main thread.
    while (g_browse.done == 0) {
        emscripten_sleep(20);
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

bool consumePickedFile(PickedFile& out, std::string& err) {
    out = {};
    err = "consumePickedFile is only available in the web build";
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

}  // namespace chudware
