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
#include <string>
#include <vector>

namespace chudware {

// GET `url`, return response body bytes. On failure returns empty and sets err
// (HTTP non-2xx, network error, or native stub).
std::vector<uint8_t> webFetch(const std::string& url, std::string& err);

}  // namespace chudware
