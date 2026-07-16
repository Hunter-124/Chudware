// GLSL source shim for the WASM/WebGL2 path (BILLET_GLES3).
// Desktop: zero-cost const-char* passthrough (no allocation).
// GLES: rewrite a leading "#version 330 core" to "#version 300 es" + highp precision.
#pragma once

#ifdef BILLET_GLES3

#include <cstring>
#include <string>

// Rewrite a desktop GLSL 330 core source for WebGL2 / GLES3 (#version 300 es).
// Sources that do not start with "#version 330 core" are returned unchanged (copied).
inline std::string shaderSrc(const char* body) {
    if (!body) return {};
    static constexpr char kLegacy[] = "#version 330 core";
    static constexpr size_t kLegacyLen = sizeof(kLegacy) - 1;
    static constexpr char kEs[] =
        "#version 300 es\n"
        "precision highp float;\n"
        "precision highp int;\n"
        "precision highp sampler2D;";
    if (std::strncmp(body, kLegacy, kLegacyLen) == 0) {
        std::string out;
        out.reserve(std::strlen(body) - kLegacyLen + sizeof(kEs) - 1);
        out.append(kEs);
        out.append(body + kLegacyLen);
        return out;
    }
    return std::string(body);
}

#else  // !BILLET_GLES3

// Zero-cost passthrough — return the original pointer, no allocation.
inline const char* shaderSrc(const char* body) noexcept {
    return body;
}

#endif  // BILLET_GLES3
