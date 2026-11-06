// Synchronous HTTP GET for the Emscripten/web client.
//
// Wraps emscripten_fetch with EMSCRIPTEN_FETCH_LOAD_TO_MEMORY |
// EMSCRIPTEN_FETCH_SYNCHRONOUS — legal off the browser main thread
// (PROXY_TO_PTHREAD worker). Relative same-origin URLs are fine.
//
// Native builds get a stub that always fails so call sites can share
// a header; they must not rely on it outside #ifdef __EMSCRIPTEN__.
#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace chudware {

// GET `url`, return response body bytes. On failure returns empty and sets err
// (HTTP non-2xx, network error, or native stub).
std::vector<uint8_t> webFetch(const std::string& url, std::string& err);

// Async GET for the browser UI thread. Callbacks run on that thread; completion
// receives owned bytes and an empty error on success. Progress is -1 if unknown.
using WebFetchComplete = std::function<void(std::vector<uint8_t>, std::string)>;
using WebFetchProgress = std::function<void(float)>;
bool webFetchAsync(const std::string& url, WebFetchComplete complete,
                   WebFetchProgress progress = {});

}  // namespace chudware
