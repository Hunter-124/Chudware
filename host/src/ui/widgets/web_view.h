// ui::WebView — reusable embedded-browser widget (Parts Library V2 phase C).
//
// Instance-based: any workspace can own one. Streams JPEG frames from a worker-side
// headless browser session (browser_embed_* RPCs) into a GL texture and forwards
// mouse/keyboard input back. The parts vendor sign-in wizard is the first client.
//
// Threading contract (GL-freeze history — see MEMORY gl-thread-rpc-freeze):
//   * All methods are GL-thread-only. Every RPC goes through st.rpc.call (async);
//     callbacks run inside st.rpc.drain() on the GL thread, so GL texture
//     create/update is safe there and nowhere else.
//   * Polls are single-flight with time backoff (0.10 s focused / 0.30 s unfocused,
//     exponential up to 2 s on errors).
//   * Callback lifetime: all mutable state lives in a shared_ptr<Inner> copied by
//     value into every RPC lambda — a WebView may be destroyed with calls in
//     flight; the Inner outlives it until the callbacks drain.
//
// Typical use (any workspace):
//   static ui::WebView web;
//   web.open(st, "https://example.com", /*isUrl=*/true);   // once
//   web.update(st);                                        // every frame while visible
//   web.draw(st, ImGui::GetContentRegionAvail());          // letterboxed page + input
#pragma once

#include <memory>
#include <string>

#include "imgui.h"

struct AppState;

namespace ui {

class WebView {
public:
    WebView();
    ~WebView();  // frees the GL texture (GL thread); detaches in-flight callbacks
    WebView(const WebView&)            = delete;
    WebView& operator=(const WebView&) = delete;

    // Begin a session: site (vendor key, opens its login page) or full url.
    void open(AppState& st, const std::string& siteOrUrl, bool isUrl);
    // Adopt an existing worker-side session (e.g. the login wizard's embed_id).
    // Not owned: close() will not stop it on the worker.
    void attach(AppState& st, const std::string& embedId);
    // Stop the session (only when owned), free the texture, reset state.
    void close(AppState& st);
    // Frame + input polling — call once per frame while the widget is visible.
    void update(AppState& st);
    // Draws the letterboxed page + forwards input inside the given content size.
    // Returns true while a session is alive (starting counts). Draw any top bar
    // (URL, status, buttons) yourself around it.
    bool draw(AppState& st, ImVec2 avail);

    const std::string& url() const;
    const std::string& loginState() const;   // unknown|login_page|logged_in|challenge
    bool active() const;
    bool starting() const;
    const std::string& lastError() const;

private:
    struct Inner;                 // all mutable state; shared with RPC callbacks
    std::shared_ptr<Inner> in_;

    void requestFrame(AppState& st);
    void flushInput(AppState& st);
    void collectInput(AppState& st, ImVec2 img0, float scale, ImVec2 imgSize);
};

}  // namespace ui
