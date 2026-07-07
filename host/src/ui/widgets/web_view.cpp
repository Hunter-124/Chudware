// ui::WebView implementation — see web_view.h for the contract.
#include "web_view.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <vector>

#include <glad/gl.h>

#include "stb_image.h"

#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"

namespace ui {

// ---- small local helpers -------------------------------------------------------------

namespace {

// Minimal base64 decode (kept local so widgets/ stays independent of parts/).
std::vector<unsigned char> b64decode(const std::string& in) {
    static int tab[256];
    static bool init = false;
    if (!init) {
        for (int i = 0; i < 256; ++i) tab[i] = -1;
        const char* a = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        for (int i = 0; i < 64; ++i) tab[(unsigned char)a[i]] = i;
        init = true;
    }
    std::vector<unsigned char> out;
    out.reserve(in.size() * 3 / 4);
    int val = 0, bits = 0;
    for (unsigned char c : in) {
        int d = tab[c];
        if (d < 0) continue;  // skip padding / whitespace
        val = (val << 6) | d;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back((unsigned char)((val >> bits) & 0xFF));
        }
    }
    return out;
}

void appendUtf8(std::string& s, unsigned int c) {
    if (c < 0x80) {
        s += (char)c;
    } else if (c < 0x800) {
        s += (char)(0xC0 | (c >> 6));
        s += (char)(0x80 | (c & 0x3F));
    } else if (c < 0x10000) {
        s += (char)(0xE0 | (c >> 12));
        s += (char)(0x80 | ((c >> 6) & 0x3F));
        s += (char)(0x80 | (c & 0x3F));
    } else {
        s += (char)(0xF0 | (c >> 18));
        s += (char)(0x80 | ((c >> 12) & 0x3F));
        s += (char)(0x80 | ((c >> 6) & 0x3F));
        s += (char)(0x80 | (c & 0x3F));
    }
}

nlohmann::json currentModifiers() {
    const ImGuiIO& io = ImGui::GetIO();
    nlohmann::json m  = nlohmann::json::array();
    if (io.KeyCtrl)  m.push_back("Control");
    if (io.KeyShift) m.push_back("Shift");
    if (io.KeyAlt)   m.push_back("Alt");
    if (io.KeySuper) m.push_back("Meta");
    return m;
}

}  // namespace

// ---- Inner: shared mutable state (outlives the widget until callbacks drain) ----------

struct WebView::Inner {
    std::string id, url, loginState = "unknown", lastError, siteLabel;
    std::string openArg;          // for the Retry button
    bool openArgIsUrl = false;

    bool owned    = true;         // we started the session → close() stops it
    bool active   = false;        // a worker-side session id is live
    bool starting = false;        // browser_embed_start in flight
    bool dead     = false;        // worker said no_such_session — stop polling
    bool detached = false;        // WebView destroyed; callbacks must not touch GL

    unsigned  tex = 0;
    int       texW = 0, texH = 0;
    long long seq = -1;

    bool   framePending = false, inputPending = false, focused = false;
    double lastFrameAt = 0.0, lastInputAt = 0.0;
    int    errStreak = 0;

    nlohmann::json evQueue = nlohmann::json::array();

    // GL upload — callbacks run in drain() on the GL thread only.
    void uploadFrame(const std::string& b64, int w, int h) {
        if (detached || w <= 0 || h <= 0) return;
        std::vector<unsigned char> bytes = b64decode(b64);
        if (bytes.empty()) return;
        int iw = 0, ih = 0, n = 0;
        unsigned char* px =
            stbi_load_from_memory(bytes.data(), (int)bytes.size(), &iw, &ih, &n, 4);
        if (!px) return;
        if (!tex) glGenTextures(1, &tex);
        glBindTexture(GL_TEXTURE_2D, tex);
        if (iw == texW && ih == texH) {
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, iw, ih, GL_RGBA, GL_UNSIGNED_BYTE, px);
        } else {
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, iw, ih, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
            texW = iw;
            texH = ih;
        }
        stbi_image_free(px);
    }

    void freeTexture() {
        if (tex) {
            glDeleteTextures(1, &tex);
            tex = 0;
        }
        texW = texH = 0;
        seq  = -1;
    }

    void resetSession() {
        id.clear();
        url.clear();
        loginState = "unknown";
        active = starting = dead = false;
        framePending = inputPending = focused = false;
        seq       = -1;
        errStreak = 0;
        evQueue   = nlohmann::json::array();
    }
};

// ---- lifecycle -------------------------------------------------------------------------

WebView::WebView() : in_(std::make_shared<Inner>()) {}

WebView::~WebView() {
    in_->detached = true;   // in-flight callbacks: skip GL / keep quiet
    in_->freeTexture();     // GL thread (UI-owned object)
}

void WebView::open(AppState& st, const std::string& siteOrUrl, bool isUrl) {
    close(st);
    auto in = in_;
    in->lastError.clear();
    in->starting     = true;
    in->owned        = true;
    in->siteLabel    = siteOrUrl;
    in->openArg      = siteOrUrl;
    in->openArgIsUrl = isUrl;

    nlohmann::json p;
    p[isUrl ? "url" : "site"] = siteOrUrl;
    p["max_width"]  = 900;
    p["max_height"] = 700;
    p["quality"]    = 60;
    st.rpc.call("browser_embed_start", p, [in](const core::RpcResult& r) {
        in->starting = false;
        const bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (!ok) {
            in->lastError = !r.ok ? r.err
                                  : (r.result.is_object()
                                         ? r.result.value("reason",
                                                          std::string("could not start browser"))
                                         : std::string("bad response"));
            return;
        }
        in->id         = r.result.value("id", "");
        in->url        = r.result.value("url", "");
        in->loginState = r.result.value("login_state", "unknown");
        in->active     = !in->id.empty();
        in->dead       = false;
        in->seq        = -1;
        in->errStreak  = 0;
        in->lastFrameAt = 0.0;   // poll immediately
    });
}

void WebView::attach(AppState& st, const std::string& embedId) {
    if (embedId.empty()) return;
    if (in_->active && in_->id == embedId) return;
    if (in_->active && in_->owned) close(st);
    auto& in = *in_;
    in.resetSession();
    in.id     = embedId;
    in.owned  = false;
    in.active = true;
    in.lastError.clear();
    in.lastFrameAt = 0.0;   // poll immediately (stale-callback guard: id compare)
}

void WebView::close(AppState& st) {
    auto& in = *in_;
    if (in.active && in.owned && !in.id.empty()) {
        st.rpc.call("browser_embed_stop", nlohmann::json{{"id", in.id}},
                    [](const core::RpcResult&) {});
    }
    in.resetSession();
    in.freeTexture();
}

// ---- polling ----------------------------------------------------------------------------

void WebView::requestFrame(AppState& st) {
    auto in = in_;
    in->framePending = true;
    in->lastFrameAt  = ImGui::GetTime();
    nlohmann::json p;
    p["id"]        = in->id;
    p["since_seq"] = in->seq;
    const std::string id = in->id;
    st.rpc.call("browser_embed_frame", p, [in, id](const core::RpcResult& r) {
        in->framePending = false;
        if (id != in->id) return;   // stale: attach()/open() switched sessions meanwhile
        if (!r.ok) {
            ++in->errStreak;
            in->lastError = r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            const std::string reason =
                r.result.is_object() ? r.result.value("reason", "") : std::string();
            if (reason == "no_such_session") {
                in->dead = true;
                return;
            }
            ++in->errStreak;
            in->lastError = reason.empty() ? "frame request failed" : reason;
            return;
        }
        in->errStreak = 0;
        in->lastError.clear();
        in->seq        = r.result.value("seq", in->seq);
        in->url        = r.result.value("url", in->url);
        in->loginState = r.result.value("login_state", in->loginState);
        if (r.result.contains("frame_b64"))   // omitted when since_seq is current
            in->uploadFrame(r.result["frame_b64"].get<std::string>(),
                            r.result.value("w", 0), r.result.value("h", 0));
    });
}

void WebView::flushInput(AppState& st) {
    auto in = in_;
    if (in->evQueue.empty() || in->inputPending) return;
    const double now = ImGui::GetTime();
    if (now - in->lastInputAt < 0.05) return;   // ~20 Hz cap
    in->inputPending = true;
    in->lastInputAt  = now;
    nlohmann::json p;
    p["id"]     = in->id;
    p["events"] = std::move(in->evQueue);
    in->evQueue = nlohmann::json::array();
    st.rpc.call("browser_embed_input", p,
                [in](const core::RpcResult&) { in->inputPending = false; });
}

void WebView::update(AppState& st) {
    auto& in = *in_;
    if (!in.active || in.dead) return;
    if (!in.framePending) {
        double interval = in.focused ? 0.10 : 0.30;
        if (in.errStreak > 0)
            interval = std::min(2.0, 0.25 * std::pow(2.0, (double)in.errStreak));
        if (ImGui::GetTime() - in.lastFrameAt >= interval) requestFrame(st);
    }
    flushInput(st);
}

// ---- input collection (called from draw when the image is on screen) ---------------------

void WebView::collectInput(AppState& st, ImVec2 img0, float scale, ImVec2 imgSize) {
    (void)st;
    auto& in     = *in_;
    ImGuiIO& io  = ImGui::GetIO();
    const bool hovered = ImGui::IsItemHovered();

    auto pagePos = [&](ImVec2 m) {
        float x = (m.x - img0.x) / scale;
        float y = (m.y - img0.y) / scale;
        x = std::max(0.0f, std::min(x, imgSize.x / scale));
        y = std::max(0.0f, std::min(y, imgSize.y / scale));
        return ImVec2(x, y);
    };
    auto push = [&](nlohmann::json ev) { in.evQueue.push_back(std::move(ev)); };

    const ImVec2 pg = pagePos(io.MousePos);

    // Focus: click on the page grabs it, click anywhere else releases it.
    for (int b = 0; b < 3; ++b) {
        if (ImGui::IsMouseClicked((ImGuiMouseButton)b)) {
            if (hovered) {
                in.focused = true;
                push({{"type", "mousedown"}, {"x", pg.x}, {"y", pg.y}, {"button", b}});
            } else {
                in.focused = false;
            }
        }
        if (hovered && ImGui::IsMouseReleased((ImGuiMouseButton)b))
            push({{"type", "mouseup"}, {"x", pg.x}, {"y", pg.y}, {"button", b}});
    }

    if (hovered && (io.MouseDelta.x != 0.0f || io.MouseDelta.y != 0.0f)) {
        // Coalesce: replace a pending trailing mousemove instead of queueing a burst.
        nlohmann::json mv = {{"type", "mousemove"}, {"x", pg.x}, {"y", pg.y}};
        if (!in.evQueue.empty() && in.evQueue.back().value("type", "") == "mousemove")
            in.evQueue.back() = std::move(mv);
        else
            push(std::move(mv));
    }

    if (hovered && (io.MouseWheel != 0.0f || io.MouseWheelH != 0.0f)) {
        push({{"type", "wheel"},
              {"x", pg.x},
              {"y", pg.y},
              {"delta_x", -io.MouseWheelH * 120.0f},
              {"delta_y", -io.MouseWheel * 120.0f}});
    }

    // ---- keyboard (only while the page holds focus) ------------------------------------
    if (!in.focused) return;
    ImGui::SetNextFrameWantCaptureKeyboard(true);

    // Ctrl shortcuts first (their chars must not double-fire as text).
    static const struct { ImGuiKey k; const char* name; } kCtrl[] = {
        {ImGuiKey_A, "a"}, {ImGuiKey_C, "c"}, {ImGuiKey_V, "v"}, {ImGuiKey_X, "x"}};
    if (io.KeyCtrl) {
        for (const auto& c : kCtrl)
            if (ImGui::IsKeyPressed(c.k))
                push({{"type", "key"}, {"key", c.name}, {"modifiers", currentModifiers()}});
    } else {
        for (int i = 0; i < io.InputQueueCharacters.Size; ++i) {
            unsigned int ch = (unsigned int)io.InputQueueCharacters[i];
            if (ch == 0 || ch < 32) continue;
            std::string text;
            appendUtf8(text, ch);
            push({{"type", "char"}, {"text", text}});
        }
    }

    static const struct { ImGuiKey k; const char* name; } kSpecial[] = {
        {ImGuiKey_Enter, "Enter"},        {ImGuiKey_KeypadEnter, "Enter"},
        {ImGuiKey_Backspace, "Backspace"},{ImGuiKey_Tab, "Tab"},
        {ImGuiKey_Escape, "Escape"},      {ImGuiKey_Delete, "Delete"},
        {ImGuiKey_LeftArrow, "ArrowLeft"},{ImGuiKey_RightArrow, "ArrowRight"},
        {ImGuiKey_UpArrow, "ArrowUp"},    {ImGuiKey_DownArrow, "ArrowDown"},
        {ImGuiKey_Home, "Home"},          {ImGuiKey_End, "End"},
        {ImGuiKey_PageUp, "PageUp"},      {ImGuiKey_PageDown, "PageDown"}};
    for (const auto& s : kSpecial)
        if (ImGui::IsKeyPressed(s.k))
            push({{"type", "key"}, {"key", s.name}, {"modifiers", currentModifiers()}});
}

// ---- draw --------------------------------------------------------------------------------

bool WebView::draw(AppState& st, ImVec2 avail) {
    auto& in       = *in_;
    const float sc = ui::uiScale;
    if (avail.x < 16.0f || avail.y < 16.0f) return in.active || in.starting;

    ImDrawList* dl   = ImGui::GetWindowDrawList();
    const ImVec2 p0  = ImGui::GetCursorScreenPos();
    const ImVec2 p1  = ImVec2(p0.x + avail.x, p0.y + avail.y);
    dl->AddRectFilled(p0, p1, Palette::toU32(palette.panelBg), 6.0f * sc);

    auto centeredText = [&](const char* txt, float yOff, const float* col) {
        const ImVec2 ts = ImGui::CalcTextSize(txt);
        dl->AddText(ImVec2(p0.x + (avail.x - ts.x) * 0.5f,
                           p0.y + avail.y * 0.5f + yOff),
                    Palette::toU32(col), txt);
    };

    // ---- no-session states --------------------------------------------------------------
    if (!in.active) {
        if (in.starting) {
            char buf[160];
            int dots = 1 + (int)(ImGui::GetTime() * 2.0) % 3;
            std::snprintf(buf, sizeof(buf), "Opening %s%.*s",
                          in.siteLabel.empty() ? "page" : in.siteLabel.c_str(), dots, "...");
            centeredText(buf, -8.0f * sc, palette.textDisabled);
        } else if (!in.lastError.empty()) {
            centeredText(in.lastError.c_str(), -18.0f * sc, palette.statusErr);
            ImGui::SetCursorScreenPos(
                ImVec2(p0.x + avail.x * 0.5f - 34.0f * sc, p0.y + avail.y * 0.5f + 4.0f * sc));
            if (ImGui::Button("Retry", ImVec2(68.0f * sc, 0)) && in.owned &&
                !in.openArg.empty())
                open(st, in.openArg, in.openArgIsUrl);
        } else {
            centeredText("No page open", -8.0f * sc, palette.textDisabled);
        }
        ImGui::SetCursorScreenPos(p0);
        ImGui::Dummy(avail);
        return in.active || in.starting;
    }

    // ---- dead session (worker restarted / wizard advanced away) --------------------------
    if (in.dead) {
        centeredText("Browser session ended", -18.0f * sc, palette.statusWarn);
        if (in.owned && !in.openArg.empty()) {
            ImGui::SetCursorScreenPos(
                ImVec2(p0.x + avail.x * 0.5f - 46.0f * sc, p0.y + avail.y * 0.5f + 4.0f * sc));
            if (ImGui::Button("Reconnect", ImVec2(92.0f * sc, 0)))
                open(st, in.openArg, in.openArgIsUrl);
        } else {
            centeredText("Waiting for the wizard to reconnect", 6.0f * sc,
                         palette.textDisabled);
        }
        ImGui::SetCursorScreenPos(p0);
        ImGui::Dummy(avail);
        return true;
    }

    // ---- live page ------------------------------------------------------------------------
    if (!in.tex || in.texW <= 0 || in.texH <= 0) {
        // Session live but first frame not here yet.
        char buf[160];
        int dots = 1 + (int)(ImGui::GetTime() * 2.0) % 3;
        std::snprintf(buf, sizeof(buf), "Loading %s%.*s",
                      in.siteLabel.empty() ? "page" : in.siteLabel.c_str(), dots, "...");
        centeredText(buf, -8.0f * sc, palette.textDisabled);
        ImGui::SetCursorScreenPos(p0);
        ImGui::Dummy(avail);
        return true;
    }

    if (in.errStreak >= 5)
        centeredText("Connection trouble — retrying…", -avail.y * 0.5f + 14.0f * sc,
                     palette.statusWarn);

    const float scale = std::min(avail.x / (float)in.texW, avail.y / (float)in.texH);
    const ImVec2 imgSize((float)in.texW * scale, (float)in.texH * scale);
    const ImVec2 img0(p0.x + (avail.x - imgSize.x) * 0.5f,
                      p0.y + (avail.y - imgSize.y) * 0.5f);
    dl->AddImage((ImTextureID)(intptr_t)in.tex, img0,
                 ImVec2(img0.x + imgSize.x, img0.y + imgSize.y));

    // Input overlay exactly on the image; mouse maps to PAGE pixels (never × uiScale).
    ImGui::SetCursorScreenPos(img0);
    ImGui::InvisibleButton("##webview_input", imgSize,
                           ImGuiButtonFlags_MouseButtonLeft |
                               ImGuiButtonFlags_MouseButtonRight |
                               ImGuiButtonFlags_MouseButtonMiddle);
    collectInput(st, img0, scale, imgSize);

    // Subtle focus ring so the user knows keys go to the page.
    if (in.focused)
        dl->AddRect(img0, ImVec2(img0.x + imgSize.x, img0.y + imgSize.y),
                    Palette::toU32(palette.accentMid), 2.0f * sc, 0, 1.5f * sc);

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(avail);
    return true;
}

// ---- accessors ----------------------------------------------------------------------------

const std::string& WebView::url() const        { return in_->url; }
const std::string& WebView::loginState() const { return in_->loginState; }
bool WebView::active() const                   { return in_->active; }
bool WebView::starting() const                 { return in_->starting; }
const std::string& WebView::lastError() const  { return in_->lastError; }

}  // namespace ui
