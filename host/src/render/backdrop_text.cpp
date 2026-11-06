#include "render/backdrop_text.h"

#include "gl_compat.h"
#include "imgui.h"
#include "imgui_internal.h"
#include "ui_theme.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <vector>

namespace ui {
namespace {

#if defined(__EMSCRIPTEN__) || defined(CHUDWARE_GLES3)
constexpr const char* kVersion = "#version 300 es\nprecision highp float;\nprecision highp int;\n";
#else
constexpr const char* kVersion = "#version 330 core\n";
#endif

// The backend enables its own VAO's attributes using locations queried from its
// linked program, including on native GLSL 330 (which uses its GLSL 130 shader).
// Bind these names to those *actual* locations before linking our program.
constexpr const char* kVertex = R"(
uniform mat4 ProjMtx;
in vec2 Position;
in vec2 UV;
in vec4 Color;
out vec2 Frag_UV;
out vec4 Frag_Color;
void main() {
    Frag_UV = UV;
    Frag_Color = Color;
    gl_Position = ProjMtx * vec4(Position, 0.0, 1.0);
}
)";

// The framebuffer and ImGui's blend inputs are display-encoded RGB. Compare the
// actual alpha-composited color in linear light, not the encoded RGB average.
// Coverage (atlas alpha) and vertex alpha both participate in that comparison.
constexpr const char* kFragment = R"(
uniform sampler2D Texture;
uniform sampler2D Backdrop;
uniform sampler2D GlyphMask;
uniform vec3 LightInk;
uniform vec3 DarkInk;
in vec2 Frag_UV;
in vec4 Frag_Color;
out vec4 Out_Color;
vec3 linearRGB(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)),
               step(vec3(0.04045), c));
}
float luminance(vec3 c) {
    return dot(linearRGB(c), vec3(0.2126, 0.7152, 0.0722));
}
float contrast(float a, float b) {
    return (max(a, b) + 0.05) / (min(a, b) + 0.05);
}
float paintedContrast(vec3 source, float alpha, vec3 background, float bgLum) {
    return contrast(luminance(mix(background, source, alpha)), bgLum);
}
void main() {
    vec4 texel = texture(Texture, Frag_UV);
    vec4 original = Frag_Color * texel;
    Out_Color = original;
    // The atlas mask contains only ordinary glyph rectangles. It excludes the
    // atlas white pixel, baked AA lines, custom image rectangles and colored
    // glyphs, so selection, the caret, icons and other primitives stay native.
    if (texture(GlyphMask, Frag_UV).r < 0.5 || original.a <= 0.0)
        return;
    vec3 background = texelFetch(Backdrop, ivec2(gl_FragCoord.xy), 0).rgb;
    float bgLum = luminance(background);
    float current = paintedContrast(original.rgb, original.a, background, bgLum);
    if (current < 4.5) {
        vec3 light = LightInk * texel.rgb;
        vec3 dark = DarkInk * texel.rgb;
        float lightContrast = paintedContrast(light, original.a, background, bgLum);
        float darkContrast = paintedContrast(dark, original.a, background, bgLum);
        float best = max(lightContrast, darkContrast);
        if (best > current)
            Out_Color.rgb = lightContrast > darkContrast ? light : dark;
    }
}
)";

struct TextureState {
    GLint active = GL_TEXTURE0;
    GLint texture1 = 0, texture2 = 0;
    GLint sampler1 = 0, sampler2 = 0;
};

struct CaptureRect { int x, y, w, h; };
struct CapturePayload {
    const ImDrawData* drawData;
    const ImFontAtlas* atlas;
    CaptureRect rect;
    std::uint64_t callbackEpoch;
};
struct GlyphRect { float u0, v0, u1, v1; };

struct BackdropTextState {
    GLuint program = 0;
    GLint backendProgram = 0;
    GLint failedBackendProgram = 0;
    GLint projectionSource = -1;
    GLint projection = -1, texture = -1, backdrop = -1, glyphMask = -1;
    GLint lightInk = -1, darkInk = -1;
    int projectionFrame = -1;
    const ImDrawData* projectionData = nullptr;
    std::uint64_t projectionEpoch = 0;
    float projectionMatrix[16] = {};

    GLuint captureTexture = 0, captureFbo = 0, maskTexture = 0;
    int captureWidth = 0, captureHeight = 0;
    GLenum captureFormat = 0;
    const ImFontAtlas* maskAtlas = nullptr;
    ImTextureID maskAtlasTexture = 0;
    int maskWidth = 0, maskHeight = 0, maskFontCount = 0;
    std::uintptr_t maskFontSignature = 0;
    const ImFontAtlas* regionAtlas = nullptr;
    ImTextureID regionAtlasTexture = 0;
    int regionWidth = 0, regionHeight = 0, regionFontCount = 0;
    std::uintptr_t regionFontSignature = 0;
    // One-time atlas index: pixel -> glyph rectangle. This lets the command
    // partitioner distinguish real glyph triangles from white-pixel geometry,
    // baked lines, and custom rectangles without per-frame allocations.
    std::vector<std::uint32_t> glyphRegions;
    std::vector<GlyphRect> glyphRects;
    std::vector<ImDrawCmd> commandScratch;
    std::vector<CapturePayload> payloads;
    ImGuiContext* payloadContext = nullptr;
    int payloadFrame = -1;

    bool active = false;
    GLint previousProgram = 0;
    TextureState textures;
};
BackdropTextState g;

void restoreTextures(const TextureState& state) {
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, static_cast<GLuint>(state.texture1));
    glBindSampler(1, static_cast<GLuint>(state.sampler1));
    glActiveTexture(GL_TEXTURE2);
    glBindTexture(GL_TEXTURE_2D, static_cast<GLuint>(state.texture2));
    glBindSampler(2, static_cast<GLuint>(state.sampler2));
    glActiveTexture(static_cast<GLenum>(state.active));
}

TextureState saveTextures() {
    TextureState state;
    glGetIntegerv(GL_ACTIVE_TEXTURE, &state.active);
    glActiveTexture(GL_TEXTURE1);
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &state.texture1);
    glGetIntegerv(GL_SAMPLER_BINDING, &state.sampler1);
    glActiveTexture(GL_TEXTURE2);
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &state.texture2);
    glGetIntegerv(GL_SAMPLER_BINDING, &state.sampler2);
    return state;
}

GLuint compileShader(GLenum type, const char* body) {
    const GLuint shader = glCreateShader(type);
    if (!shader) {
        std::fprintf(stderr, "backdrop_text: cannot create shader\n");
        return 0;
    }
    const char* source[] = {kVersion, body};
    glShaderSource(shader, 2, source, nullptr);
    glCompileShader(shader);
    GLint ok = GL_FALSE;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[2048] = {};
        glGetShaderInfoLog(shader, sizeof(log), nullptr, log);
        std::fprintf(stderr, "backdrop_text: shader compilation failed: %s\n", log);
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

bool ensureProgram(GLint backend) {
    if (backend == g.backendProgram && g.program)
        return true;
    if (backend == 0) {
        static bool reported = false;
        if (!reported) {
            std::fprintf(stderr, "backdrop_text: ImGui backend has no active shader program\n");
            reported = true;
        }
        return false;
    }
    if (backend == g.failedBackendProgram)
        return false;
    if (g.program) {
        glDeleteProgram(g.program);
        g.program = 0;
    }
    g.backendProgram = 0;
    g.failedBackendProgram = backend;
    g.projectionFrame = -1;

    const GLint pos = glGetAttribLocation(static_cast<GLuint>(backend), "Position");
    const GLint uv = glGetAttribLocation(static_cast<GLuint>(backend), "UV");
    const GLint color = glGetAttribLocation(static_cast<GLuint>(backend), "Color");
    const GLint sourceProjection = glGetUniformLocation(static_cast<GLuint>(backend), "ProjMtx");
    if (pos < 0 || uv < 0 || color < 0 || sourceProjection < 0 ||
        pos == uv || pos == color || uv == color) {
        std::fprintf(stderr, "backdrop_text: ImGui backend shader ABI unavailable (program %d)\n", backend);
        return false;
    }
    const GLuint vs = compileShader(GL_VERTEX_SHADER, kVertex);
    const GLuint fs = compileShader(GL_FRAGMENT_SHADER, kFragment);
    if (!vs || !fs) {
        if (vs) glDeleteShader(vs);
        if (fs) glDeleteShader(fs);
        return false;
    }
    const GLuint program = glCreateProgram();
    if (!program) {
        std::fprintf(stderr, "backdrop_text: cannot create program\n");
        glDeleteShader(vs);
        glDeleteShader(fs);
        return false;
    }
    glAttachShader(program, vs);
    glAttachShader(program, fs);
    glBindAttribLocation(program, static_cast<GLuint>(pos), "Position");
    glBindAttribLocation(program, static_cast<GLuint>(uv), "UV");
    glBindAttribLocation(program, static_cast<GLuint>(color), "Color");
    glLinkProgram(program);
    glDeleteShader(vs);
    glDeleteShader(fs);
    GLint linked = GL_FALSE;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    if (!linked) {
        char log[2048] = {};
        glGetProgramInfoLog(program, sizeof(log), nullptr, log);
        std::fprintf(stderr, "backdrop_text: shader link failed: %s\n", log);
        glDeleteProgram(program);
        return false;
    }

    g.projection = glGetUniformLocation(program, "ProjMtx");
    g.texture = glGetUniformLocation(program, "Texture");
    g.backdrop = glGetUniformLocation(program, "Backdrop");
    g.glyphMask = glGetUniformLocation(program, "GlyphMask");
    g.lightInk = glGetUniformLocation(program, "LightInk");
    g.darkInk = glGetUniformLocation(program, "DarkInk");
    if (g.projection < 0 || g.texture < 0 || g.backdrop < 0 || g.glyphMask < 0 ||
        g.lightInk < 0 || g.darkInk < 0) {
        std::fprintf(stderr, "backdrop_text: linked shader is missing required uniforms\n");
        glDeleteProgram(program);
        return false;
    }
    g.program = program;
    g.backendProgram = backend;
    g.failedBackendProgram = 0;
    g.projectionSource = sourceProjection;
    return true;
}

// Font atlases in this host are built at startup. Include the font storage and
// glyph counts in the key as well as the GL texture, to notice atlas rebuilds.
std::uintptr_t fontSignature(const ImFontAtlas& atlas) {
    std::uintptr_t result = 0;
    for (const ImFont* font : atlas.Fonts) {
        const std::uintptr_t p = reinterpret_cast<std::uintptr_t>(font->Glyphs.Data);
        result ^= p + static_cast<std::uintptr_t>(font->Glyphs.Size) * 0x9e3779b9u +
                  (result << 6) + (result >> 2);
    }
    return result;
}

struct UnpackState {
    GLint buffer = 0, alignment = 4, rowLength = 0, skipPixels = 0, skipRows = 0;
    UnpackState() {
        glGetIntegerv(GL_PIXEL_UNPACK_BUFFER_BINDING, &buffer);
        glGetIntegerv(GL_UNPACK_ALIGNMENT, &alignment);
        glGetIntegerv(GL_UNPACK_ROW_LENGTH, &rowLength);
        glGetIntegerv(GL_UNPACK_SKIP_PIXELS, &skipPixels);
        glGetIntegerv(GL_UNPACK_SKIP_ROWS, &skipRows);
        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0);
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
        glPixelStorei(GL_UNPACK_ROW_LENGTH, 0);
        glPixelStorei(GL_UNPACK_SKIP_PIXELS, 0);
        glPixelStorei(GL_UNPACK_SKIP_ROWS, 0);
    }
    ~UnpackState() {
        glPixelStorei(GL_UNPACK_ALIGNMENT, alignment);
        glPixelStorei(GL_UNPACK_ROW_LENGTH, rowLength);
        glPixelStorei(GL_UNPACK_SKIP_PIXELS, skipPixels);
        glPixelStorei(GL_UNPACK_SKIP_ROWS, skipRows);
        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, static_cast<GLuint>(buffer));
    }
};

void textureParameters() {
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}

bool ensureFontRegions(const ImFontAtlas& atlas) {
    const std::uintptr_t signature = fontSignature(atlas);
    if (g.regionAtlas == &atlas && g.regionAtlasTexture == atlas.TexID &&
        g.regionWidth == atlas.TexWidth && g.regionHeight == atlas.TexHeight &&
        g.regionFontCount == atlas.Fonts.Size && g.regionFontSignature == signature)
        return true;
    if (!atlas.TexReady || atlas.TexWidth <= 0 || atlas.TexHeight <= 0)
        return false;

    const int width = atlas.TexWidth, height = atlas.TexHeight;
    g.glyphRegions.assign(static_cast<size_t>(width) * static_cast<size_t>(height), 0);
    g.glyphRects.clear();
    g.glyphRects.push_back({}); // Region zero is not a glyph.
    for (const ImFont* font : atlas.Fonts) {
        for (const ImFontGlyph& glyph : font->Glyphs) {
            if (!glyph.Visible || glyph.Colored)
                continue;
            const int x0 = std::clamp(static_cast<int>(std::lround(glyph.U0 * width)), 0, width);
            const int x1 = std::clamp(static_cast<int>(std::lround(glyph.U1 * width)), 0, width);
            const int y0 = std::clamp(static_cast<int>(std::lround(glyph.V0 * height)), 0, height);
            const int y1 = std::clamp(static_cast<int>(std::lround(glyph.V1 * height)), 0, height);
            if (x1 <= x0 || y1 <= y0)
                continue;
            const auto id = static_cast<std::uint32_t>(g.glyphRects.size());
            g.glyphRects.push_back({glyph.U0, glyph.V0, glyph.U1, glyph.V1});
            for (int y = y0; y < y1; ++y) {
                auto row = g.glyphRegions.begin() + static_cast<size_t>(y) * static_cast<size_t>(width);
                std::fill(row + x0, row + x1, id);
            }
        }
    }
    g.regionAtlas = &atlas;
    g.regionAtlasTexture = atlas.TexID;
    g.regionWidth = width;
    g.regionHeight = height;
    g.regionFontCount = atlas.Fonts.Size;
    g.regionFontSignature = signature;
    return true;
}

bool ensureMask(const ImFontAtlas& atlas) {
    if (!ensureFontRegions(atlas))
        return false;
    const std::uintptr_t signature = g.regionFontSignature;
    if (g.maskTexture && g.maskAtlas == &atlas && g.maskAtlasTexture == atlas.TexID &&
        g.maskWidth == atlas.TexWidth && g.maskHeight == atlas.TexHeight &&
        g.maskFontCount == atlas.Fonts.Size && g.maskFontSignature == signature) {
        glBindTexture(GL_TEXTURE_2D, g.maskTexture);
        return true;
    }

    const int width = atlas.TexWidth, height = atlas.TexHeight;
    std::vector<unsigned char> pixels(g.glyphRegions.size());
    std::transform(g.glyphRegions.begin(), g.glyphRegions.end(), pixels.begin(),
                   [](std::uint32_t id) { return id ? 255 : 0; });
    if (!g.maskTexture)
        glGenTextures(1, &g.maskTexture);
    if (!g.maskTexture) {
        std::fprintf(stderr, "backdrop_text: cannot allocate glyph mask texture\n");
        return false;
    }
    glBindTexture(GL_TEXTURE_2D, g.maskTexture);
    textureParameters();
    {
        UnpackState unpack;
        glTexImage2D(GL_TEXTURE_2D, 0, GL_R8, width, height, 0, GL_RED, GL_UNSIGNED_BYTE,
                     pixels.data());
    }
    g.maskAtlas = &atlas;
    g.maskAtlasTexture = atlas.TexID;
    g.maskWidth = width;
    g.maskHeight = height;
    g.maskFontCount = atlas.Fonts.Size;
    g.maskFontSignature = signature;
    return true;
}

bool ensureCapture(int width, int height, GLenum format) {
    if (!g.captureTexture) {
        glGenTextures(1, &g.captureTexture);
        if (!g.captureTexture) {
            std::fprintf(stderr, "backdrop_text: cannot allocate snapshot texture\n");
            return false;
        }
        glBindTexture(GL_TEXTURE_2D, g.captureTexture);
        textureParameters();
    } else {
        glBindTexture(GL_TEXTURE_2D, g.captureTexture);
    }
    if (!g.captureFbo)
        glGenFramebuffers(1, &g.captureFbo);
    if (!g.captureFbo) {
        std::fprintf(stderr, "backdrop_text: cannot allocate snapshot framebuffer\n");
        return false;
    }
    if (width <= g.captureWidth && height <= g.captureHeight && g.captureFormat == format)
        return true;
    const int newWidth = std::max(width, g.captureWidth);
    const int newHeight = std::max(height, g.captureHeight);
    {
        UnpackState unpack;
        glTexImage2D(GL_TEXTURE_2D, 0, format, newWidth, newHeight, 0,
                     format == GL_RGB8 ? GL_RGB : GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    }
    GLint oldDraw = 0;
    glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &oldDraw);
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, g.captureFbo);
    glFramebufferTexture2D(GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                           g.captureTexture, 0);
    const GLenum status = glCheckFramebufferStatus(GL_DRAW_FRAMEBUFFER);
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, static_cast<GLuint>(oldDraw));
    if (status != GL_FRAMEBUFFER_COMPLETE) {
        std::fprintf(stderr, "backdrop_text: snapshot framebuffer incomplete (0x%x)\n", status);
        return false;
    }
    g.captureWidth = newWidth;
    g.captureHeight = newHeight;
    g.captureFormat = format;
    return true;
}

bool captureRect(const ImRect& r, const ImDrawData& data, CaptureRect& out) {
    const int fbWidth = static_cast<int>(data.DisplaySize.x * data.FramebufferScale.x);
    const int fbHeight = static_cast<int>(data.DisplaySize.y * data.FramebufferScale.y);
    if (fbWidth <= 0 || fbHeight <= 0 || data.FramebufferScale.x <= 0 ||
        data.FramebufferScale.y <= 0)
        return false;
    const float x0 = (r.Min.x - data.DisplayPos.x) * data.FramebufferScale.x;
    const float x1 = (r.Max.x - data.DisplayPos.x) * data.FramebufferScale.x;
    const float y0 = fbHeight - (r.Max.y - data.DisplayPos.y) * data.FramebufferScale.y;
    const float y1 = fbHeight - (r.Min.y - data.DisplayPos.y) * data.FramebufferScale.y;
    if (!std::isfinite(x0) || !std::isfinite(x1) || !std::isfinite(y0) || !std::isfinite(y1))
        return false;
    const int left = static_cast<int>(std::floor(std::clamp(x0, 0.0f, static_cast<float>(fbWidth))));
    const int right = static_cast<int>(std::ceil(std::clamp(x1, 0.0f, static_cast<float>(fbWidth))));
    const int bottom = static_cast<int>(std::floor(std::clamp(y0, 0.0f, static_cast<float>(fbHeight))));
    const int top = static_cast<int>(std::ceil(std::clamp(y1, 0.0f, static_cast<float>(fbHeight))));
    out = {left, bottom, right - left, top - bottom};
    return out.w > 0 && out.h > 0;
}

bool captureFramebuffer(const CaptureRect& r) {
    GLint oldRead = 0, oldDraw = 0, drawBuffer = 0;
    glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &oldRead);
    glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &oldDraw);
    glGetIntegerv(GL_DRAW_BUFFER0, &drawBuffer);
    if (drawBuffer == GL_NONE) {
        std::fprintf(stderr, "backdrop_text: current framebuffer has no color draw buffer\n");
        return false;
    }
    glBindFramebuffer(GL_READ_FRAMEBUFFER, static_cast<GLuint>(oldDraw));
    GLint sourceReadBuffer = 0;
    glGetIntegerv(GL_READ_BUFFER, &sourceReadBuffer);
    if (sourceReadBuffer != drawBuffer)
        glReadBuffer(static_cast<GLenum>(drawBuffer));
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, g.captureFbo);
    const GLboolean scissor = glIsEnabled(GL_SCISSOR_TEST);
    if (scissor) glDisable(GL_SCISSOR_TEST);
    // GLES/WebGL require identical source/destination coordinates for an MSAA
    // resolve. Allocate enough texture for those coordinates, but copy only
    // the clipped glyph-run rectangle, not the rest of the framebuffer.
    glBlitFramebuffer(r.x, r.y, r.x + r.w, r.y + r.h,
                      r.x, r.y, r.x + r.w, r.y + r.h, GL_COLOR_BUFFER_BIT, GL_NEAREST);
    if (scissor) glEnable(GL_SCISSOR_TEST);
    if (sourceReadBuffer != drawBuffer)
        glReadBuffer(static_cast<GLenum>(sourceReadBuffer));
    glBindFramebuffer(GL_READ_FRAMEBUFFER, static_cast<GLuint>(oldRead));
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, static_cast<GLuint>(oldDraw));
    return true;
}

void beginBackdrop(const ImDrawList*, const ImDrawCmd* cmd) {
    g.active = false;
    const std::uintptr_t encoded = reinterpret_cast<std::uintptr_t>(cmd->UserCallbackData);
    if (encoded == 0 || encoded - 1 >= g.payloads.size())
        return;
    const CapturePayload& payload = g.payloads[encoded - 1];
    if (!payload.drawData || !payload.atlas)
        return;
    const ImDrawData& drawData = *payload.drawData;
    const CaptureRect rect = payload.rect;
    GLint backend = 0;
    glGetIntegerv(GL_CURRENT_PROGRAM, &backend);
    if (!ensureProgram(backend))
        return;

    const TextureState textures = saveTextures();
    glActiveTexture(GL_TEXTURE2);
    glBindSampler(2, 0);
    const bool haveMask = ensureMask(*payload.atlas);
    glActiveTexture(GL_TEXTURE1);
    glBindSampler(1, 0);
    GLenum captureFormat = GL_RGBA8;
#if defined(__EMSCRIPTEN__) || defined(CHUDWARE_GLES3)
    // Multisample resolves also require matching color formats. An opaque
    // WebGL drawing buffer is RGB8 rather than RGBA8.
    GLint alphaBits = 0;
    glGetIntegerv(GL_ALPHA_BITS, &alphaBits);
    if (alphaBits == 0)
        captureFormat = GL_RGB8;
#endif
    const bool haveCapture = haveMask &&
        ensureCapture(rect.x + rect.w, rect.y + rect.h, captureFormat);
    if (!haveCapture || !captureFramebuffer(rect)) {
        restoreTextures(textures);
        return;
    }
    // ensureCapture bound the snapshot on unit 1; the renderer itself must go
    // back to unit 0 before it binds each draw command's atlas/image texture.
    glActiveTexture(static_cast<GLenum>(textures.active));
    if (g.projectionFrame != ImGui::GetFrameCount() || g.projectionData != &drawData ||
        g.projectionEpoch != payload.callbackEpoch) {
        glGetUniformfv(static_cast<GLuint>(backend), g.projectionSource, g.projectionMatrix);
        g.projectionFrame = ImGui::GetFrameCount();
        g.projectionData = &drawData;
        g.projectionEpoch = payload.callbackEpoch;
    }
    const ImVec4 light = adaptiveSurface(0.0f).textPrimary;
    const ImVec4 dark = adaptiveSurface(1.0f).textPrimary;
    glUseProgram(g.program);
    glUniformMatrix4fv(g.projection, 1, GL_FALSE, g.projectionMatrix);
    glUniform1i(g.texture, 0);
    glUniform1i(g.backdrop, 1);
    glUniform1i(g.glyphMask, 2);
    glUniform3f(g.lightInk, light.x, light.y, light.z);
    glUniform3f(g.darkInk, dark.x, dark.y, dark.z);
    g.previousProgram = backend;
    g.textures = textures;
    g.active = true;
}

void endBackdrop(const ImDrawList*, const ImDrawCmd*) {
    if (!g.active)
        return;
    glUseProgram(static_cast<GLuint>(g.previousProgram));
    restoreTextures(g.textures);
    g.active = false;
}

bool namedCopilotOwner(const char* name) {
    if (!name)
        return false;
    constexpr char copilot[] = "Copilot###AI";
    constexpr char workspaceChat[] = "##AIWorkspace/##ai_ws_conv_";
    constexpr size_t n = sizeof(copilot) - 1;
    return (std::strncmp(name, copilot, n) == 0 && (name[n] == '\0' || name[n] == '/')) ||
           std::strncmp(name, workspaceChat, sizeof(workspaceChat) - 1) == 0;
}

bool isCopilotDrawList(const ImDrawList& list) {
    if (namedCopilotOwner(list._OwnerName))
        return true;
    if (!list._OwnerName)
        return false;
    // Popups and the separately begun slash menu do not inherit the parent's
    // name. Their Begin stack does retain the actual Copilot/chat host.
    ImGuiWindow* window = ImGui::FindWindowByName(list._OwnerName);
    if (!window || window->DrawList != &list)
        return false;
    for (window = window->ParentWindowInBeginStack; window; window = window->ParentWindowInBeginStack)
        if (namedCopilotOwner(window->Name))
            return true;
    return false;
}

bool glyphTriangle(const ImDrawList& list, const ImDrawCmd& draw, unsigned int offset,
                   const ImRect& clip, ImRect& bounds) {
    const unsigned int ia = draw.VtxOffset + list.IdxBuffer[offset];
    const unsigned int ib = draw.VtxOffset + list.IdxBuffer[offset + 1];
    const unsigned int ic = draw.VtxOffset + list.IdxBuffer[offset + 2];
    if (ia >= static_cast<unsigned int>(list.VtxBuffer.Size) ||
        ib >= static_cast<unsigned int>(list.VtxBuffer.Size) ||
        ic >= static_cast<unsigned int>(list.VtxBuffer.Size))
        return false;
    const ImDrawVert& a = list.VtxBuffer[ia];
    const ImDrawVert& b = list.VtxBuffer[ib];
    const ImDrawVert& c = list.VtxBuffer[ic];
    if (((a.col | b.col | c.col) & IM_COL32_A_MASK) == 0)
        return false;
    const float u = (a.uv.x + b.uv.x + c.uv.x) / 3.0f;
    const float v = (a.uv.y + b.uv.y + c.uv.y) / 3.0f;
    if (!std::isfinite(u) || !std::isfinite(v) || u < 0.0f || u >= 1.0f ||
        v < 0.0f || v >= 1.0f)
        return false;
    const int x = static_cast<int>(u * g.regionWidth);
    const int y = static_cast<int>(v * g.regionHeight);
    const std::uint32_t id =
        g.glyphRegions[static_cast<size_t>(y) * static_cast<size_t>(g.regionWidth) + x];
    if (!id || id >= g.glyphRects.size())
        return false;
    const GlyphRect& glyph = g.glyphRects[id];
    constexpr float epsilon = 0.000001f;
    auto inGlyph = [&](const ImVec2& uv) {
        return uv.x >= glyph.u0 - epsilon && uv.x <= glyph.u1 + epsilon &&
               uv.y >= glyph.v0 - epsilon && uv.y <= glyph.v1 + epsilon;
    };
    // A centroid alone is insufficient: a custom image triangle can cross a
    // glyph cell. Require all of its UVs to belong to the same glyph rectangle.
    if (!inGlyph(a.uv) || !inGlyph(b.uv) || !inGlyph(c.uv))
        return false;
    if (!std::isfinite(a.pos.x) || !std::isfinite(a.pos.y) ||
        !std::isfinite(b.pos.x) || !std::isfinite(b.pos.y) ||
        !std::isfinite(c.pos.x) || !std::isfinite(c.pos.y))
        return false;
    bounds = ImRect(std::min({a.pos.x, b.pos.x, c.pos.x}),
                    std::min({a.pos.y, b.pos.y, c.pos.y}),
                    std::max({a.pos.x, b.pos.x, c.pos.x}),
                    std::max({a.pos.y, b.pos.y, c.pos.y}));
    bounds.ClipWith(clip);
    return bounds.Max.x > bounds.Min.x && bounds.Max.y > bounds.Min.y;
}

ImDrawCmd callbackBefore(const ImDrawCmd& next, ImDrawCallback callback, void* data) {
    ImDrawCmd result = next;
    result.ElemCount = 0;
    result.UserCallback = callback;
    result.UserCallbackData = data;
    result.UserCallbackDataSize = 0;
    result.UserCallbackDataOffset = -1;
    return result;
}

void appendRun(const ImDrawCmd& source, unsigned int start, unsigned int end, bool glyph,
               const ImRect& bounds, const ImDrawData& data, const ImFontAtlas& atlas,
               std::uint64_t callbackEpoch, bool& wrapped) {
    ImDrawCmd run = source;
    run.IdxOffset = start;
    run.ElemCount = end - start;
    CaptureRect rect{};
    if (glyph && captureRect(bounds, data, rect)) {
        // Store an index, not an element pointer: later vector growth cannot
        // invalidate callback data. The arena is retained through all replays
        // of this frame's draw data.
        const std::uintptr_t encoded = static_cast<std::uintptr_t>(g.payloads.size()) + 1;
        g.payloads.push_back(CapturePayload{&data, &atlas, rect, callbackEpoch});
        g.commandScratch.push_back(callbackBefore(run, beginBackdrop, reinterpret_cast<void*>(encoded)));
        g.commandScratch.push_back(run);
        g.commandScratch.push_back(callbackBefore(run, endBackdrop, nullptr));
        wrapped = true;
    } else {
        g.commandScratch.push_back(run);
    }
}

bool isExternalCallback(ImDrawCallback callback) {
    return callback && callback != beginBackdrop && callback != endBackdrop;
}

void countExternalCallbacks(const ImDrawList& list, std::uint64_t& epoch) {
    for (const ImDrawCmd& draw : list.CmdBuffer)
        if (isExternalCallback(draw.UserCallback))
            ++epoch;
}

void partitionDrawList(ImDrawList& list, const ImDrawData& data, const ImFontAtlas& atlas,
                       std::uint64_t& callbackEpoch) {
    if (list.VtxBuffer.empty() || list.IdxBuffer.empty()) {
        countExternalCallbacks(list, callbackEpoch);
        return;
    }
    for (const ImDrawCmd& draw : list.CmdBuffer) {
        if (draw.UserCallback == beginBackdrop) {
            countExternalCallbacks(list, callbackEpoch);
            return; // Idempotent even if the same draw data is rendered again.
        }
    }

    g.commandScratch.clear();
    g.commandScratch.reserve(static_cast<size_t>(list.CmdBuffer.Size));
    bool wrapped = false;
    const ImRect viewport(data.DisplayPos.x, data.DisplayPos.y,
                          data.DisplayPos.x + data.DisplaySize.x,
                          data.DisplayPos.y + data.DisplaySize.y);
    for (const ImDrawCmd& draw : list.CmdBuffer) {
        const unsigned int indexCount = static_cast<unsigned int>(list.IdxBuffer.Size);
        if (draw.UserCallback || draw.GetTexID() != atlas.TexID || draw.ElemCount < 3 ||
            draw.ElemCount % 3 != 0 || draw.IdxOffset > indexCount ||
            draw.ElemCount > indexCount - draw.IdxOffset) {
            // In particular, preserve existing user/reset callbacks verbatim
            // and in order; never run one inside our temporary render state.
            if (isExternalCallback(draw.UserCallback))
                ++callbackEpoch;
            g.commandScratch.push_back(draw);
            continue;
        }
        ImRect clip(draw.ClipRect);
        clip.ClipWith(viewport);
        if (!(clip.Max.x > clip.Min.x && clip.Max.y > clip.Min.y)) {
            g.commandScratch.push_back(draw);
            continue;
        }
        const unsigned int end = draw.IdxOffset + draw.ElemCount;
        unsigned int runStart = draw.IdxOffset;
        bool haveRun = false, runGlyph = false;
        ImRect runBounds;
        for (unsigned int i = draw.IdxOffset; i < end; i += 3) {
            ImRect triangleBounds;
            const bool glyph = glyphTriangle(list, draw, i, clip, triangleBounds);
            if (!haveRun || glyph != runGlyph) {
                if (haveRun)
                    appendRun(draw, runStart, i, runGlyph, runBounds, data, atlas, callbackEpoch, wrapped);
                runStart = i;
                runGlyph = glyph;
                runBounds = triangleBounds;
                haveRun = true;
            } else if (glyph) {
                runBounds.Add(triangleBounds);
            }
        }
        if (haveRun)
            appendRun(draw, runStart, end, runGlyph, runBounds, data, atlas, callbackEpoch, wrapped);
    }
    if (!wrapped)
        return;
    if (g.commandScratch.size() > static_cast<size_t>(std::numeric_limits<int>::max())) {
        std::fprintf(stderr, "backdrop_text: too many partitioned draw commands\n");
        return;
    }
    // Render has already resolved existing copied callback data. Only replace
    // commands; never touch the vertex/index buffers or callback-data storage.
    list.CmdBuffer.resize(static_cast<int>(g.commandScratch.size()));
    std::memcpy(list.CmdBuffer.Data, g.commandScratch.data(),
                g.commandScratch.size() * sizeof(ImDrawCmd));
}

}  // namespace

void adaptCopilotTextToBackdrop(ImDrawData* data) {
    ImGuiContext* context = ImGui::GetCurrentContext();
    if (!context || !data || !data->Valid || !context->IO.Fonts ||
        context->IO.Fonts->TexID == 0)
        return;
    const int frame = ImGui::GetFrameCount();
    if (g.payloadContext != context || g.payloadFrame != frame) {
        g.payloads.clear();
        g.payloadContext = context;
        g.payloadFrame = frame;
    }
    bool regionsReady = false;
    std::uint64_t callbackEpoch = 0;
    for (ImDrawList* list : data->CmdLists) {
        if (!list)
            continue;
        if (!isCopilotDrawList(*list)) {
            countExternalCallbacks(*list, callbackEpoch);
            continue;
        }
        if (!regionsReady) {
            if (!ensureFontRegions(*context->IO.Fonts))
                return;
            regionsReady = true;
        }
        partitionDrawList(*list, *data, *context->IO.Fonts, callbackEpoch);
    }
}

void shutdownBackdropText() {
    if (g.captureFbo) glDeleteFramebuffers(1, &g.captureFbo);
    if (g.captureTexture) glDeleteTextures(1, &g.captureTexture);
    if (g.maskTexture) glDeleteTextures(1, &g.maskTexture);
    if (g.program) glDeleteProgram(g.program);
    g = BackdropTextState{};
}

}  // namespace ui
