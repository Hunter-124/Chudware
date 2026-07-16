// oauth_capture.h — in-app multi-account xAI (SuperGrok) OAuth via embedded CEF.
//
// Settings → AI → Accounts "Add account" for xAI opens an ImGui modal hosting the
// same IWebViewBackend / CEF seam as the Web workspace tab. The token exchange and
// persistence stay in Python (subscription_login / xai_sub_provider) — C++ only:
//   1. kicks subscription_login{provider:xai, device:true, open_browser:false}
//   2. polls subscription_status for user_code + verification_url
//   3. navigates the embedded CEF to that URL so the user can approve
//   4. watches status (and URL heuristics) until the Python poll completes
//   5. offers "Add another account" with a cookie wipe so the next login is isolated
//
// Fallback: when makeWebView()->available() is false (SDK-less build / CEF init
// failed), callers keep the external-browser subscription_login path.
#pragma once

#include <functional>
#include <memory>
#include <string>

struct AppState;

namespace ui {

class IWebViewBackend;

// Modal host for one in-flight xAI device-code login (and consecutive multi-adds).
// All public methods are GL/UI-thread only (same contract as IWebViewBackend).
class OauthCapture {
public:
    OauthCapture();
    ~OauthCapture();

    OauthCapture(const OauthCapture&)            = delete;
    OauthCapture& operator=(const OauthCapture&) = delete;

    // True when CEF (or any embedded backend) can host the approve page.
    // Callers fall back to the external-browser flow when false.
    static bool cefAvailable();

    // Start (or restart) an xAI device-code login inside the modal.
    // open_browser=false so Python does not also spawn an external tab.
    void beginXai(AppState& st);

    // Draw the modal while active (no-op when idle). Call every frame from
    // drawSubscriptionAccounts / the Settings AI tab.
    void draw(AppState& st, float sc);

    bool active() const { return phase_ != Phase::Idle; }

    // Cancel / dismiss; parks the browser and clears progress UI.
    void close(AppState& st);

private:
    enum class Phase {
        Idle,
        StartingLogin,   // subscription_login RPC in flight
        AwaitingCode,    // waiting for user_code / verification_url from status
        Browsing,        // CEF showing the approve page; Python polling tokens
        Success,         // credential persisted; offer Add another / Done
        Error,
    };

    void startLoginRpc(AppState& st);
    void pollStatus(AppState& st, bool force);
    void openVerifyInCef(AppState& st);
    void resetBrowserSession(AppState& st, std::function<void()> then);
    void onLoginSucceeded(AppState& st, const std::string& email,
                          const std::string& accountId);
    void onLoginFailed(const std::string& reason, const std::string& hint);

    // URL heuristics for the accounts.x.ai approve page (extra signal alongside
    // subscription_status; token handoff still happens in Python).
    static bool looksApproved(const std::string& url);
    static bool looksAuthHost(const std::string& url);

    std::unique_ptr<IWebViewBackend> web_;
    Phase phase_ = Phase::Idle;

    std::string userCode_;
    std::string verifyUrl_;
    std::string lastError_;
    std::string lastHint_;
    std::string connectedEmail_;
    std::string connectedAccountId_;

    bool   loginRpcInFlight_ = false;
    bool   statusPollInFlight_ = false;
    bool   browserOpened_ = false;
    bool   isolationPending_ = false;  // wipe cookies before next add
    bool   pendingWipeThenLogin_ = false;  // open about:blank first, then wipe+login
    double lastStatusPoll_ = -1e9;
    int    sessionGen_ = 0;            // bumps on each begin / add-another
};

// Process-wide singleton used by Settings → Accounts (function-static in .cpp
// would also work; a free accessor keeps the draw site one-liners).
OauthCapture& oauthCapture();

}  // namespace ui
