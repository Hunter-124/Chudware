#include "web_fetch.h"

#include <string>

#ifdef __EMSCRIPTEN__
#include <emscripten/fetch.h>
#include <emscripten/heap.h>
#include <cstddef>

#include <cstring>
#include <limits>
#endif

namespace chudware {

#ifdef __EMSCRIPTEN__

std::vector<uint8_t> webFetch(const std::string& url, std::string& err) {
    err.clear();
    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "GET");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY | EMSCRIPTEN_FETCH_SYNCHRONOUS;

    emscripten_fetch_t* fetch = emscripten_fetch(&attr, url.c_str());
    if (!fetch) {
        err = "emscripten_fetch failed: " + url;
        return {};
    }

    std::vector<uint8_t> out;
    const int status = fetch->status;
    if (status >= 200 && status < 300) {
        if (fetch->numBytes > 0) {
            // Emscripten reports uint64 byte counts even on wasm32. Validate
            // the complete response range before narrowing or making a copy.
            const size_t heap_bytes = emscripten_get_heap_size();
            const uintptr_t begin = reinterpret_cast<uintptr_t>(fetch->data);
            if (!fetch->data || fetch->dataOffset != 0 ||
                (fetch->totalBytes && fetch->numBytes != fetch->totalBytes) ||
                begin > heap_bytes || fetch->numBytes > heap_bytes - begin ||
                fetch->numBytes > out.max_size() ||
                fetch->numBytes > static_cast<uint64_t>(std::numeric_limits<std::ptrdiff_t>::max())) {
                err = "Invalid or oversized HTTP response byte range for " + url;
            } else {
                const auto* p = reinterpret_cast<const uint8_t*>(fetch->data);
                const auto count = static_cast<size_t>(fetch->numBytes);
                out.assign(p, p + count);
            }
        }
    } else {
        err = "HTTP " + std::to_string(status) + " for " + url;
    }
    emscripten_fetch_close(fetch);
    return out;
}

#else  // !__EMSCRIPTEN__

std::vector<uint8_t> webFetch(const std::string& url, std::string& err) {
    (void)url;
    err = "webFetch is only available in the web build";
    return {};
}

#endif

}  // namespace chudware
