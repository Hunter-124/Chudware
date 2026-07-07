#pragma once
// img_tex.h — decode JPEG/PNG to an OpenGL texture (RE-08)
// Requires an active GL context. Uses stb_image (compiled via stb_impl.cpp).
#include <cstdint>
#include <string>
#include <vector>

namespace reveng {

// Load a JPEG or PNG from disk into an RGBA GL_TEXTURE_2D.
// Returns the texture id (>0) on success; 0 on failure.
// Caller must call freeTexture() when done.
unsigned int loadTexFromFile(const std::string& path, int* outW = nullptr, int* outH = nullptr);

// Load a JPEG or PNG from an in-memory buffer.
unsigned int loadTexFromMemory(const unsigned char* data, int dataLen,
                               int* outW = nullptr, int* outH = nullptr);

// Decode JPEG/PNG from disk into a raw RGBA pixel buffer (no GL).
// Returns false on failure. Pixels are stbi-allocated; call freePixels().
bool decodeFile(const std::string& path, std::vector<unsigned char>& pixels,
                int& w, int& h);

// Decode JPEG/PNG from memory into a raw RGBA pixel buffer (no GL).
bool decodeMemory(const unsigned char* data, int dataLen,
                  std::vector<unsigned char>& pixels, int& w, int& h);

// Delete a GL texture previously returned by loadTex*.
void freeTexture(unsigned int texId);

} // namespace reveng
