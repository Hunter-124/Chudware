#include "web_fetch.h"

#include <string>

#ifdef __EMSCRIPTEN__
#include <emscripten/fetch.h>

#include <cstring>
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
        if (fetch->data && fetch->numBytes > 0) {
            const auto* p = reinterpret_cast<const uint8_t*>(fetch->data);
            out.assign(p, p + static_cast<size_t>(fetch->numBytes));
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
