// oauth_capture.cpp — Settings → AI → Accounts in-app xAI OAuth (device-code + CEF).
//
// Token exchange / persistence: Python only (subscription_login → xai_sub_provider).
// This TU owns the ImGui modal + CEF approve surface and status polling.
#include "oauth_capture.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <string>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/widgets/web_view_backend.h"

namespace ui {
namespace {

static std::string optStr(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return {};
    if (it->is_string()) return it->get<std::string>();
    return {};
}

static std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return s;
}

// Accounts list refresh: Settings' pollSubscriptionAccounts sees the
// login_in_progress → false edge and forces a membership re-poll (same as the
// external-browser path). No separate accounts RPC needed here.

}  // namespace

OauthCapture& oauthCapture() {
    static OauthCapture inst;
    return inst;
}

bool OauthCapture::cefAvailable() {
    // Probe once with a throwaway backend — available() is cheap (init-failed flag).
    static int cached = -1;
    if (cached < 0) {
        auto probe = makeWebView();
        cached = (probe && probe->available()) ? 1 : 0;
    }
    return cached == 1;
}

OauthCapture::OauthCapture() = default;

OauthCapture::~OauthCapture() {
    // Backend owns CEF browser; destructor parks it (CefWebView keeps one browser).
    web_.reset();
}

bool OauthCapture::looksAuthHost(const std::string& url) {
    const std::string u = lower(url);
    return u.find("accounts.x.ai") != std::string::npos ||
           u.find("auth.x.ai") != std::string::npos ||
           u.find("x.ai/oauth") != std::string::npos ||
           u.find("x.ai/sign") != std::string::npos;
}

bool OauthCapture::looksApproved(const std::string& url) {
    // Device-code approval pages often land on a success / "you can close this"
    // URL. This is a soft signal only — Python token poll is authoritative.
    const std::string u = lower(url);
    if (u.find("accounts.x.ai") == std::string::npos &&
        u.find("auth.x.ai") == std::string::npos)
        return false;
    return u.find("success") != std::string::npos ||
           u.find("approved") != std::string::npos ||
           u.find("complete") != std::string::npos ||
           u.find("device/success") != std::string::npos ||
           u.find("activate/success") != std::string::npos ||
           u.find("you_can_close") != std::string::npos ||
           u.find("close-this") != std::string::npos;
}

void OauthCapture::onLoginFailed(const std::string& reason, const std::string& hint) {
    phase_ = Phase::Error;
    lastError_ = reason.empty() ? "xAI sign-in failed" : reason;
    lastHint_ = hint;
    loginRpcInFlight_ = false;
}

void OauthCapture::onLoginSucceeded(AppState& st, const std::string& email,
                                    const std::string& accountId) {
    phase_ = Phase::Success;
    connectedEmail_ = email;
    connectedAccountId_ = accountId;
    lastError_.clear();
    lastHint_.clear();
    loginRpcInFlight_ = false;
    // Next beginXai / Add account must wipe so multi-account stays isolated.
    needsIsolationBeforeLogin_ = true;
    // Toast only — membership re-poll is driven by Settings status edge.
    st.toast(ui::ToastLevel::Success,
             email.empty() ? "xAI account connected"
                           : ("xAI connected: " + email));
}

void OauthCapture::startLoginRpc(AppState& st) {
    loginRpcInFlight_ = true;
    lastError_.clear();
    lastHint_.clear();
    userCode_.clear();
    verifyUrl_.clear();
    browserOpened_ = false;
    connectedEmail_.clear();
    connectedAccountId_.clear();
    phase_ = Phase::StartingLogin;
    lastStatusPoll_ = -1e9;
    const int gen = sessionGen_;

    nlohmann::json p = {
        {"provider", "xai"},
        {"device", true},
        // Suppress external browser — CEF owns the approve surface.
        {"open_browser", false},
    };
    st.rpc.call("subscription_login", p, [this, gen](const core::RpcResult& r) {
        if (gen != sessionGen_) return;  // superseded by Add another / close
        loginRpcInFlight_ = false;
        if (!r.ok) {
            onLoginFailed(r.err.empty() ? "subscription_login failed" : r.err, {});
            return;
        }
        if (r.result.is_object() && r.result.value("ok", true) == false) {
            onLoginFailed(optStr(r.result, "reason"), optStr(r.result, "hint"));
            return;
        }
        // Start RPC returns immediately with in_progress; status poll drives the rest.
        if (phase_ == Phase::StartingLogin)
            phase_ = Phase::AwaitingCode;
    });
}

void OauthCapture::openVerifyInCef(AppState& st) {
    if (!web_) web_ = makeWebView();
    if (!web_ || !web_->available()) {
        onLoginFailed("Embedded browser unavailable",
                      "Fall back to external browser sign-in from Accounts.");
        return;
    }
    if (verifyUrl_.empty()) return;
    web_->open(st, verifyUrl_, /*isUrl=*/true);
    browserOpened_ = true;
    phase_ = Phase::Browsing;
}

void OauthCapture::resetBrowserSession(AppState& st, std::function<void()> then) {
    if (!web_) {
        isolationPending_ = false;
        isolationThen_ = nullptr;
        isolationStartedAt_ = -1.0;
        if (then) then();
        return;
    }
    isolationPending_ = true;
    isolationStartedAt_ = ImGui::GetTime();
    isolationThen_ = std::move(then);
    const int gen = sessionGen_;
    web_->clearSession(st, [this, gen]() {
        if (gen != sessionGen_) return;  // superseded by close / restart
        if (!isolationPending_) return;  // already finished via UI timeout
        isolationPending_ = false;
        isolationStartedAt_ = -1.0;
        auto cont = std::move(isolationThen_);
        isolationThen_ = nullptr;
        if (cont) cont();
    });
}

void OauthCapture::beginXai(AppState& st) {
    ++sessionGen_;
    if (!web_) web_ = makeWebView();
    if (!web_ || !web_->available()) {
        onLoginFailed("Embedded browser unavailable",
                      "Use the external browser sign-in path.");
        return;
    }
    lastError_.clear();
    lastHint_.clear();
    userCode_.clear();
    verifyUrl_.clear();
    browserOpened_ = false;
    connectedEmail_.clear();
    connectedAccountId_.clear();

    // First login: start device-code RPC immediately (no pre-wipe).
    // 2nd+ add (after a completed login, or explicit "Add another"): wipe first
    // with timeout guard so isolationPending_ cannot wedge the modal.
    if (!needsIsolationBeforeLogin_) {
        isolationPending_ = false;
        isolationStartedAt_ = -1.0;
        isolationThen_ = nullptr;
        startLoginRpc(st);
        return;
    }
    phase_ = Phase::StartingLogin;
    // clearSession needs a live CefBrowser when possible; seed about:blank if
    // none yet, then wipe. Timeout in draw()/clearSession always continues.
    if (!web_->active() && !web_->starting())
        web_->open(st, "about:blank", /*isUrl=*/true);
    const int gen = sessionGen_;
    resetBrowserSession(st, [this, &st, gen]() {
        if (gen != sessionGen_) return;
        startLoginRpc(st);
    });
}

void OauthCapture::close(AppState& st) {
    ++sessionGen_;  // invalidate in-flight RPC callbacks
    phase_ = Phase::Idle;
    loginRpcInFlight_ = false;
    statusPollInFlight_ = false;
    isolationPending_ = false;
    isolationStartedAt_ = -1.0;
    isolationThen_ = nullptr;
    userCode_.clear();
    verifyUrl_.clear();
    lastError_.clear();
    lastHint_.clear();
    connectedEmail_.clear();
    connectedAccountId_.clear();
    browserOpened_ = false;
    if (web_) web_->close(st);
}

void OauthCapture::pollStatus(AppState& st, bool force) {
    if (statusPollInFlight_) return;
    const double now = ImGui::GetTime();
    if (!force && (now - lastStatusPoll_) < 0.75) return;
    lastStatusPoll_ = now;
    statusPollInFlight_ = true;
    const int gen = sessionGen_;
    st.rpc.call("subscription_status", nlohmann::json::object(),
                [this, &st, gen](const core::RpcResult& r) {
        statusPollInFlight_ = false;
        if (gen != sessionGen_) return;
        if (!r.ok || !r.result.is_object()) return;
        auto providers = r.result.value("providers", nlohmann::json::object());
        if (!providers.is_object()) return;
        auto it = providers.find("xai");
        if (it == providers.end() || !it->is_object()) return;
        const auto& x = *it;

        const bool inProgress = x.value("login_in_progress", false);
        const std::string stage = optStr(x, "login_stage");
        const std::string err = optStr(x, "last_error");
        const std::string code = optStr(x, "user_code");
        std::string vurl = optStr(x, "verification_url");
        if (vurl.empty()) vurl = optStr(x, "verification_uri");
        if (vurl.empty() && !code.empty())
            vurl = "https://accounts.x.ai/oauth2/device?user_code=" + code;

        if (!code.empty()) userCode_ = code;
        if (!vurl.empty()) verifyUrl_ = vurl;

        // Open CEF once we have a verification URL.
        if ((phase_ == Phase::StartingLogin || phase_ == Phase::AwaitingCode) &&
            !verifyUrl_.empty() && !isolationPending_) {
            openVerifyInCef(st);
        }

        if (!inProgress) {
            if (stage == "connected" || stage == "done" || stage.empty()) {
                // Empty stage after a successful login also means idle-connected.
                // Prefer explicit connected / no error after we were browsing.
                if (phase_ == Phase::Browsing || phase_ == Phase::AwaitingCode ||
                    phase_ == Phase::StartingLogin) {
                    if (!err.empty() && stage == "error") {
                        onLoginFailed(err, optStr(x, "hint"));
                    } else if (stage == "error") {
                        onLoginFailed(err.empty() ? "xAI sign-in failed" : err,
                                      optStr(x, "hint"));
                    } else if (stage == "connected" ||
                               (err.empty() && phase_ == Phase::Browsing &&
                                !optStr(x, "email").empty())) {
                        onLoginSucceeded(st, optStr(x, "email"),
                                         optStr(x, "account_id"));
                    } else if (stage == "connected") {
                        onLoginSucceeded(st, optStr(x, "email"),
                                         optStr(x, "account_id"));
                    } else if (!err.empty()) {
                        onLoginFailed(err, optStr(x, "hint"));
                    }
                    // else: still starting, no code yet — keep waiting
                }
            } else if (stage == "error") {
                onLoginFailed(err.empty() ? "xAI sign-in failed" : err,
                              optStr(x, "hint"));
            }
        } else {
            // Still in progress — promote to AwaitingCode if we have fields.
            if (phase_ == Phase::StartingLogin &&
                (!userCode_.empty() || !verifyUrl_.empty()))
                phase_ = Phase::AwaitingCode;
        }
    });
}

void OauthCapture::draw(AppState& st, float sc) {
    if (phase_ == Phase::Idle) return;

    if (web_) web_->update(st);

    // Isolation wipe timeout escape hatch (UI-thread). clearSession also has a
    // CEF-side ~4s deadline; this catches the rare case where even that path
    // stalls before invoking done, so "Resetting…" can never stick forever.
    constexpr double kIsolationUiTimeoutSec = 4.5;
    if (isolationPending_ && isolationStartedAt_ >= 0.0 &&
        (ImGui::GetTime() - isolationStartedAt_) >= kIsolationUiTimeoutSec) {
        std::fprintf(stderr,
                     "[oauth] isolation wipe UI timeout (%.1fs) — proceeding with login\n",
                     kIsolationUiTimeoutSec);
        std::fflush(stderr);
        isolationPending_ = false;
        isolationStartedAt_ = -1.0;
        auto cont = std::move(isolationThen_);
        isolationThen_ = nullptr;
        if (cont) cont();
    }

    // Always poll while modal is open (except pure Success/Error idle).
    // isolationPending_ still allows poll so a concurrent wipe can't hide progress.
    if (phase_ == Phase::StartingLogin || phase_ == Phase::AwaitingCode ||
        phase_ == Phase::Browsing)
        pollStatus(st, /*force=*/false);

    // Soft URL intercept: approval-success page is a UX hint; Python remains source of truth.
    if (phase_ == Phase::Browsing && web_ && !web_->url().empty()) {
        if (looksApproved(web_->url())) {
            // Nudge a status poll; do NOT claim success from URL alone.
            pollStatus(st, /*force=*/true);
        }
    }

    ImGui::SetNextWindowSize(ImVec2(720.0f * sc, 560.0f * sc), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSizeConstraints(ImVec2(480.0f * sc, 360.0f * sc),
                                        ImVec2(1400.0f * sc, 1000.0f * sc));
    bool open = true;
    if (!ImGui::Begin("Sign in — xAI Grok", &open,
                      ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoDocking)) {
        ImGui::End();
        if (!open) close(st);
        return;
    }
    if (!open) {
        ImGui::End();
        close(st);
        return;
    }

    // ---- header: status + user code -----------------------------------------
    ImGui::TextUnformatted("xAI SuperGrok");
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.textDim));
    if (isolationPending_)
        ImGui::TextUnformatted("— resetting browser session\xe2\x80\xa6");
    else if (phase_ == Phase::StartingLogin)
        ImGui::TextUnformatted("— preparing sign-in\xe2\x80\xa6");
    else if (phase_ == Phase::AwaitingCode)
        ImGui::TextUnformatted("— waiting for device code\xe2\x80\xa6");
    else if (phase_ == Phase::Browsing)
        ImGui::TextUnformatted("— approve access in the page below");
    else if (phase_ == Phase::Success)
        ImGui::TextUnformatted("— connected");
    else if (phase_ == Phase::Error)
        ImGui::TextUnformatted("— failed");
    ImGui::PopStyleColor();

    if (!userCode_.empty() && phase_ != Phase::Success) {
        ImGui::Dummy(ImVec2(0, 4.0f * sc));
        ImGui::Text("Code: %s", userCode_.c_str());
        ImGui::SameLine();
        if (ImGui::SmallButton("Copy code"))
            ImGui::SetClipboardText(userCode_.c_str());
        if (!verifyUrl_.empty()) {
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.textDim));
            ImGui::TextWrapped("%s", verifyUrl_.c_str());
            ImGui::PopStyleColor();
        }
    }

    if (phase_ == Phase::Error) {
        ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.statusErr));
        ImGui::TextWrapped("%s", lastError_.c_str());
        ImGui::PopStyleColor();
        if (!lastHint_.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.textDim));
            ImGui::TextWrapped("%s", lastHint_.c_str());
            ImGui::PopStyleColor();
        }
        if (ImGui::Button("Retry", ImVec2(100.0f * sc, 0)))
            beginXai(st);
        ImGui::SameLine();
        if (ImGui::Button("Close", ImVec2(100.0f * sc, 0)))
            close(st);
        ImGui::End();
        return;
    }

    if (phase_ == Phase::Success) {
        ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.statusOk));
        if (!connectedEmail_.empty())
            ImGui::Text("Signed in as %s", connectedEmail_.c_str());
        else
            ImGui::TextUnformatted("Account connected.");
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 8.0f * sc));
        // Multi-account: wipe session then start a fresh device-code login.
        if (ImGui::Button("Add another account", ImVec2(180.0f * sc, 0))) {
            ++sessionGen_;
            resetBrowserSession(st, [this, &st]() {
                startLoginRpc(st);
            });
            phase_ = Phase::StartingLogin;
            userCode_.clear();
            verifyUrl_.clear();
            browserOpened_ = false;
            connectedEmail_.clear();
            connectedAccountId_.clear();
            lastError_.clear();
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Clears the embedded browser session so the next xAI login is not "
                "auto-signed into this account.");
        ImGui::SameLine();
        if (ImGui::Button("Done", ImVec2(100.0f * sc, 0)))
            close(st);
        ImGui::End();
        return;
    }

    // ---- body: embedded CEF -------------------------------------------------
    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const float footerH = 36.0f * sc;
    const ImVec2 body(avail.x, std::max(120.0f * sc, avail.y - footerH));

    if (!isolationPending_ && web_ &&
        (phase_ == Phase::Browsing || web_->starting() || web_->active())) {
        if (web_->starting() && !web_->active()) {
            ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.textDim));
            ImGui::TextUnformatted("Loading approve page\xe2\x80\xa6");
            ImGui::PopStyleColor();
            ImGui::Dummy(body);
        } else if (!web_->lastError().empty() && !web_->active()) {
            ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.statusErr));
            ImGui::TextWrapped("%s", web_->lastError().c_str());
            ImGui::PopStyleColor();
            ImGui::Dummy(body);
        } else {
            web_->draw(st, body);
        }
        // Live URL chip (callback intercept surface — OnAddressChange → url()).
        if (!web_->url().empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.textDim));
            ImGui::TextWrapped("%s", web_->url().c_str());
            ImGui::PopStyleColor();
        }
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.textDim));
        ImGui::TextUnformatted(
            isolationPending_
                ? "Resetting browser session\xe2\x80\xa6"
                : "Requesting device code from xAI\xe2\x80\xa6");
        ImGui::PopStyleColor();
        if (isolationPending_) {
            ImGui::Dummy(ImVec2(0, 8.0f * sc));
            if (ImGui::Button("Skip reset & continue", ImVec2(180.0f * sc, 0))) {
                isolationPending_ = false;
                isolationStartedAt_ = -1.0;
                auto cont = std::move(isolationThen_);
                isolationThen_ = nullptr;
                if (cont) cont();
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Proceed with sign-in without waiting for the browser session wipe.");
            ImGui::SameLine();
            if (ImGui::Button("Cancel reset", ImVec2(120.0f * sc, 0)))
                close(st);
        }
        ImGui::Dummy(body);
    }

    // ---- footer -------------------------------------------------------------
    if (ImGui::Button("Cancel", ImVec2(100.0f * sc, 0)))
        close(st);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.textDim));
    ImGui::TextUnformatted(
        "Tokens are stored by the Python subscription_login path — never in C++.");
    ImGui::PopStyleColor();

    ImGui::End();
}

}  // namespace ui
