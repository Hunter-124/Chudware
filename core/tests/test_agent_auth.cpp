// test_agent_auth.cpp — unit tests for the subscription-auth port (core/agent/auth.cpp).
//
// Everything runs against a SCRATCH config dir (XDG_DATA_HOME redirect, POSIX /
// APPDATA on Windows) — never the live store, never real network. The refresh
// HTTP layer is replaced via auth::set_http_post_for_testing.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/auth.h"
#include "agent/paths.h"

#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;
using nlohmann::json;
using aicad::agent::HttpResponse;
using aicad::agent::auth::EnsureFreshResult;

namespace {

long long now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

// Redirect the app-data root into a per-test scratch dir. paths.cpp reads the
// env live, so this fully isolates the store (fresh dir → <root>/Billet).
struct ScratchConfig {
    fs::path root;
    ScratchConfig() {
        static std::atomic<int> seq{0};
        root = fs::temp_directory_path() /
               ("aicad-auth-test-" + std::to_string(
#ifdef _WIN32
                    _getpid()
#else
                    getpid()
#endif
                        ) +
                "-" + std::to_string(seq++));
        fs::create_directories(root);
#ifdef _WIN32
        _putenv_s("APPDATA", root.string().c_str());
#else
        setenv("XDG_DATA_HOME", root.c_str(), 1);
#endif
    }
    ~ScratchConfig() {
#ifdef _WIN32
        _putenv_s("APPDATA", "");
#else
        unsetenv("XDG_DATA_HOME");
#endif
        std::error_code ec;
        fs::remove_all(root, ec);
    }
    fs::path cfg_dir() const { return root / "Billet"; }
    fs::path cfg_path() const { return cfg_dir() / "config.json"; }
    void write_cfg(const json& j) const {
        fs::create_directories(cfg_dir());
        std::ofstream fh(cfg_path(), std::ios::binary | std::ios::trunc);
        fh << j.dump(2);
    }
    void write_raw(const std::string& s) const {
        fs::create_directories(cfg_dir());
        std::ofstream fh(cfg_path(), std::ios::binary | std::ios::trunc);
        fh << s;
    }
    json read_cfg() const {
        std::ifstream fh(cfg_path(), std::ios::binary);
        std::ostringstream ss;
        ss << fh.rdbuf();
        return json::parse(ss.str());
    }
};

// Injectable fake transport: records requests, answers through a handler.
struct FakeHttp {
    struct Request {
        std::string url;
        std::vector<std::pair<std::string, std::string>> headers;
        std::string body;
    };
    std::mutex m;
    std::vector<Request> requests;
    std::function<HttpResponse(const Request&)> handler;
    std::atomic<int> calls{0};

    HttpResponse operator()(const std::string& url,
                            const std::vector<std::pair<std::string, std::string>>& headers,
                            const std::string& body, int /*timeout_ms*/) {
        Request req{url, headers, body};
        {
            std::lock_guard<std::mutex> lk(m);
            requests.push_back(req);
        }
        calls.fetch_add(1);
        if (handler) return handler(req);
        return HttpResponse{200, "{}", ""};
    }
    std::string header(const Request& r, const std::string& name) const {
        for (const auto& h : r.headers)
            if (h.first == name) return h.second;
        return "";
    }
};

HttpResponse json_response(long status, const json& body) {
    return HttpResponse{status, body.dump(), ""};
}

std::string b64url_encode(const std::string& in) {
    static const char* alpha =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    int val = 0, bits = -6;
    for (unsigned char c : in) {
        val = (val << 8) + c;
        bits += 8;
        while (bits >= 0) {
            out.push_back(alpha[(val >> bits) & 0x3F]);
            bits -= 6;
        }
    }
    if (bits > -6) out.push_back(alpha[((val << 8) >> (bits + 8)) & 0x3F]);
    return out;
}

std::string make_jwt(const json& payload) {
    return b64url_encode(R"({"alg":"none","typ":"JWT"})") + "." +
           b64url_encode(payload.dump()) + ".sig";
}

json xai_account(const std::string& access, const std::string& rt, long long expires) {
    return json{{"access_token", access},
                {"refresh_token", rt},
                {"expires_at_ms", expires}};
}

json xai_store(const json& accounts, const std::string& active) {
    return json{{"subscription_auth",
                 {{"xai-sub", {{"active", active}, {"accounts", accounts}}}}}};
}

}  // namespace

TEST_CASE("fresh token skips the network entirely") {
    ScratchConfig scratch;
    FakeHttp fake;
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    scratch.write_cfg(xai_store({{"default", xai_account("at-live", "rt-1", now_ms() + 3600'000)}},
                                "default"));

    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("xai");
    CHECK(r.ok);
    CHECK_FALSE(r.refreshed);
    CHECK(r.expires_in_s > 3500);
    CHECK(fake.calls.load() == 0);

    // Alias resolution: xai-sub / XAI reach the same credential.
    CHECK(aicad::agent::auth::ensure_fresh("xai-sub").ok);
    CHECK(aicad::agent::auth::ensure_fresh("XAI").ok);
    CHECK(fake.calls.load() == 0);

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("expired xai token refreshes and the rotated refresh token is written back") {
    ScratchConfig scratch;
    FakeHttp fake;
    fake.handler = [](const FakeHttp::Request& req) {
        CHECK(req.url == "https://auth.x.ai/oauth2/token");
        return json_response(200, {{"access_token", "at-new"},
                                   {"refresh_token", "rt-new"},
                                   {"expires_in", 3600}});
    };
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    scratch.write_cfg(xai_store({{"default", xai_account("at-old", "rt-old", now_ms() - 1000)}},
                                "default"));

    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("xai");
    REQUIRE(r.ok);
    CHECK(r.refreshed);
    CHECK(fake.calls.load() == 1);

    // Store-compat proof: same multi-account shape, only the touched slot patched.
    json disk = scratch.read_cfg();
    const json& acct = disk["subscription_auth"]["xai-sub"]["accounts"]["default"];
    CHECK(acct["access_token"] == "at-new");
    CHECK(acct["refresh_token"] == "rt-new");  // xAI rotation rule
    CHECK(acct["expires_at_ms"].get<long long>() > now_ms());
    CHECK(acct["token_endpoint"] == "https://auth.x.ai/oauth2/token");
    CHECK(disk["subscription_auth"]["xai-sub"]["active"] == "default");

    // Atomic write-back: tmp file gone, advisory lock file present (same file
    // Python's sub_auth._lock_path uses).
    std::error_code ec;
    CHECK_FALSE(fs::exists(scratch.cfg_dir() / "config.json.tmp", ec));
    CHECK(fs::exists(scratch.cfg_dir() / "config.json.lock", ec));

    // The C++ reader now sees the rotated token.
    json cred = aicad::agent::auth::load_credential("xai-sub");
    CHECK(cred["access_token"] == "at-new");

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("codex refresh keeps the old RT when the response omits it; legacy flat store migrates") {
    ScratchConfig scratch;
    FakeHttp fake;
    fake.handler = [](const FakeHttp::Request& req) {
        CHECK(req.url == "https://auth.openai.com/oauth/token");
        return json_response(200, {{"access_token", "at-codex-new"},
                                   {"expires_in", 3600}});  // no refresh_token
    };
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    // Legacy FLAT credential (pre-multi-account shape) under the codex cred id.
    scratch.write_cfg(json{{"subscription_auth",
                            {{"openai-codex",
                              {{"access_token", "at-codex-old"},
                               {"refresh_token", "rt-codex-old"},
                               {"expires_at_ms", now_ms() - 5000},
                               {"account_id", "acct-123"}}}}}});

    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("codex");
    REQUIRE(r.ok);
    CHECK(r.refreshed);
    REQUIRE(fake.calls.load() == 1);

    // Form-encoded request mirrors codex_sub_provider.refresh().
    const auto& req = fake.requests[0];
    CHECK(fake.header(req, "Content-Type") == "application/x-www-form-urlencoded");
    CHECK(req.body.find("grant_type=refresh_token") != std::string::npos);
    CHECK(req.body.find("refresh_token=rt-codex-old") != std::string::npos);
    CHECK(req.body.find("client_id=app_EMoamEEZ73f0CkXaXp7hrann") != std::string::npos);

    // Written back as multi-account; refresh token preserved, identity kept.
    json disk = scratch.read_cfg();
    const json& entry = disk["subscription_auth"]["openai-codex"];
    CHECK(entry["active"] == "acct-123");
    const json& acct = entry["accounts"]["acct-123"];
    CHECK(acct["access_token"] == "at-codex-new");
    CHECK(acct["refresh_token"] == "rt-codex-old");

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("anthropic refresh request shape and skew-subtracted expiry") {
    ScratchConfig scratch;
    FakeHttp fake;
    fake.handler = [](const FakeHttp::Request&) {
        return json_response(200, {{"access_token", "at-claude-new"},
                                   {"refresh_token", "rt-claude-new"},
                                   {"expires_in", 3600}});
    };
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    scratch.write_cfg(json{{"subscription_auth",
                            {{"anthropic-sub",
                              {{"active", "default"},
                               {"accounts",
                                {{"default",
                                  {{"access_token", "at-claude-old"},
                                   {"refresh_token", "rt-claude-old"},
                                   {"expires_at_ms", now_ms() - 100},
                                   {"plan", "max"},
                                   {"display_name", "Claude (max)"}}}}}}}}}});

    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("claude");
    REQUIRE(r.ok);
    REQUIRE(fake.calls.load() == 1);

    const auto& req = fake.requests[0];
    CHECK(req.url == "https://api.anthropic.com/v1/oauth/token");
    CHECK(fake.header(req, "anthropic-beta") == "oauth-2025-04-20");
    CHECK(fake.header(req, "User-Agent") == "anthropic-sdk-typescript/0.94.0 userOAuthProvider");
    json body = json::parse(req.body);
    CHECK(body["grant_type"] == "refresh_token");
    CHECK(body["refresh_token"] == "rt-claude-old");
    CHECK(body["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e");

    json disk = scratch.read_cfg();
    const json& acct = disk["subscription_auth"]["anthropic-sub"]["accounts"]["default"];
    CHECK(acct["access_token"] == "at-claude-new");
    CHECK(acct["refresh_token"] == "rt-claude-new");
    // Skew subtracted at store time: expires_in 3600s - 300s skew.
    long long exp = acct["expires_at_ms"].get<long long>();
    CHECK(exp <= now_ms() + 3300'000);
    CHECK(exp > now_ms() + 3000'000);
    // Identity preserved across the rotation.
    CHECK(acct["plan"] == "max");
    CHECK(acct["display_name"] == "Claude (max)");

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("dead active account rotates to a fresh sibling and is marked revoked") {
    ScratchConfig scratch;
    FakeHttp fake;
    fake.handler = [](const FakeHttp::Request& req) {
        CHECK(req.body.find("rt-dead") != std::string::npos);
        return json_response(400, {{"error", "invalid_grant"},
                                   {"error_description", "Refresh token has been revoked"}});
    };
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    scratch.write_cfg(xai_store(
        {{"acct-a", xai_account("at-a", "rt-dead", now_ms() - 1000)},
         {"acct-b", xai_account("at-b", "rt-b", now_ms() + 3600'000)}},
        "acct-a"));

    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("xai");
    REQUIRE(r.ok);
    CHECK_FALSE(r.refreshed);  // sibling was already fresh — promoted, not refreshed
    CHECK(fake.calls.load() == 1);  // one dead attempt on the active account

    json disk = scratch.read_cfg();
    const json& entry = disk["subscription_auth"]["xai-sub"];
    CHECK(entry["active"] == "acct-b");
    // invalid_grant persisted on the dead account (skip-marker for later scans).
    CHECK(entry["accounts"]["acct-a"]["refresh_revoked"] == true);
    CHECK(entry["accounts"]["acct-a"].contains("auth_error"));

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("every stored account revoked -> hard failure with re-auth message") {
    ScratchConfig scratch;
    FakeHttp fake;
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    json acct = xai_account("at-a", "rt-a", now_ms() - 1000);
    acct["refresh_revoked"] = true;
    acct["auth_error"] = "invalid_grant";
    scratch.write_cfg(xai_store({{"acct-a", acct}}, "acct-a"));

    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("xai");
    CHECK_FALSE(r.ok);
    CHECK(r.error.find("revoked") != std::string::npos);
    CHECK(r.error.find("Re-authenticate") != std::string::npos);
    CHECK(fake.calls.load() == 0);  // revoked markers never hit the network

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("concurrent refreshes serialize on the store lock (single network exchange)") {
    ScratchConfig scratch;
    FakeHttp fake;
    std::atomic<int> posts{0};
    fake.handler = [&posts](const FakeHttp::Request&) {
        posts.fetch_add(1);
        std::this_thread::sleep_for(std::chrono::milliseconds(150));
        return json_response(200, {{"access_token", "at-raced"},
                                   {"refresh_token", "rt-raced"},
                                   {"expires_in", 3600}});
    };
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    scratch.write_cfg(xai_store({{"default", xai_account("at-old", "rt-old", now_ms() - 1000)}},
                                "default"));

    std::vector<std::thread> threads;
    std::vector<char> oks(4, 0);
    for (int i = 0; i < 4; ++i)
        threads.emplace_back([i, &oks] {
            oks[i] = aicad::agent::auth::ensure_fresh("xai").ok ? 1 : 0;
        });
    for (auto& t : threads) t.join();

    for (int i = 0; i < 4; ++i) CHECK(oks[i] == 1);
    // The first refresher holds the lock through the POST; the others re-read
    // under the same lock and see the already-rotated token (xAI rule).
    CHECK(posts.load() == 1);
    json disk = scratch.read_cfg();
    CHECK(disk["subscription_auth"]["xai-sub"]["accounts"]["default"]["refresh_token"] ==
          "rt-raced");

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("malformed store is tolerated (no credential -> graceful no-op)") {
    ScratchConfig scratch;
    FakeHttp fake;
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    scratch.write_raw("{ not valid json !!!");
    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("xai");
    CHECK(r.ok);  // no stored credential is NOT a refresh failure
    CHECK_FALSE(r.refreshed);

    scratch.write_cfg(json{{"subscription_auth", "a string, not an object"}});
    r = aicad::agent::auth::ensure_fresh("xai");
    CHECK(r.ok);
    CHECK_FALSE(r.refreshed);

    // Unknown provider id is the only hard pre-flight failure.
    r = aicad::agent::auth::ensure_fresh("not-a-provider");
    CHECK_FALSE(r.ok);
    CHECK(r.error.find("unknown subscription provider") != std::string::npos);
    CHECK(fake.calls.load() == 0);

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("expiry margin: 5-minute ensure-fresh skew") {
    ScratchConfig scratch;
    FakeHttp fake;
    fake.handler = [](const FakeHttp::Request&) {
        return json_response(200, {{"access_token", "at-new"},
                                   {"refresh_token", "rt-new"},
                                   {"expires_in", 3600}});
    };
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    SUBCASE("expiring in 60s is stale (inside the skew)") {
        scratch.write_cfg(xai_store(
            {{"default", xai_account("at", "rt", now_ms() + 60'000)}}, "default"));
        CHECK(aicad::agent::auth::ensure_fresh("xai").ok);
        CHECK(fake.calls.load() == 1);
    }
    SUBCASE("expiring in 10min is fresh (outside the skew)") {
        scratch.write_cfg(xai_store(
            {{"default", xai_account("at", "rt", now_ms() + 600'000)}}, "default"));
        CHECK(aicad::agent::auth::ensure_fresh("xai").ok);
        CHECK(fake.calls.load() == 0);
    }
    SUBCASE("is_expired matches sub_auth semantics") {
        json c = json{{"expires_at_ms", now_ms() + 100'000}};
        CHECK(aicad::agent::auth::is_expired(c, 0) == false);
        CHECK(aicad::agent::auth::is_expired(c, 200'000) == true);
        CHECK(aicad::agent::auth::is_expired(json::object(), 0) == true);
    }

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("cursor refresh: bearer exchange with JWT exp claim expiry") {
    ScratchConfig scratch;
    FakeHttp fake;
    const long long jwt_exp = now_ms() / 1000 + 7200;
    fake.handler = [jwt_exp](const FakeHttp::Request& req) {
        CHECK(req.url == "https://api2.cursor.sh/auth/exchange_user_api_key");
        return json_response(
            200, {{"accessToken", make_jwt({{"sub", "user-9"}, {"exp", jwt_exp}})}});
    };
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    json cursor_acct = {{"access_token", "at-cursor-old"},
                        {"refresh_token", "rt-cursor-old"},
                        {"expires_at_ms", now_ms() - 10}};
    json cursor_entry = {{"active", "default"}, {"accounts", {{"default", cursor_acct}}}};
    scratch.write_cfg(json{{"subscription_auth", {{"cursor", cursor_entry}}}});

    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("cursor");
    REQUIRE(r.ok);
    REQUIRE(fake.calls.load() == 1);
    CHECK(fake.header(fake.requests[0], "Authorization") == "Bearer rt-cursor-old");

    json disk = scratch.read_cfg();
    const json& acct = disk["subscription_auth"]["cursor"]["accounts"]["user-9"];
    CHECK(acct["refresh_token"] == "rt-cursor-old");  // response omitted it -> keep
    // exp claim (s) * 1000 - 5min skew.
    CHECK(acct["expires_at_ms"].get<long long>() == jwt_exp * 1000 - 300'000);

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("ensure_fresh_for_run reloads cfg with the rotated token; failure is a refusal") {
    ScratchConfig scratch;
    FakeHttp fake;
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    SUBCASE("stale snapshot triggers refresh and cfg is reloaded") {
        fake.handler = [](const FakeHttp::Request&) {
            return json_response(200, {{"access_token", "at-rotated"},
                                       {"refresh_token", "rt-rotated"},
                                       {"expires_in", 3600}});
        };
        json cfg = xai_store({{"default", xai_account("at-stale", "rt-stale", now_ms() - 1000)}},
                             "default");
        scratch.write_cfg(cfg);
        std::string err = aicad::agent::auth::ensure_fresh_for_run("xai-sub", cfg);
        CHECK(err.empty());
        CHECK(fake.calls.load() == 1);
        const json& acct = cfg["subscription_auth"]["xai-sub"]["accounts"]["default"];
        CHECK(acct["access_token"] == "at-rotated");
    }

    SUBCASE("hard refresh failure returns the re-auth error") {
        fake.handler = [](const FakeHttp::Request&) {
            return json_response(400, {{"error", "invalid_grant"}});
        };
        json cfg = xai_store({{"default", xai_account("at-stale", "rt-stale", now_ms() - 1000)}},
                             "default");
        scratch.write_cfg(cfg);
        std::string err = aicad::agent::auth::ensure_fresh_for_run("xai-sub", cfg);
        CHECK_FALSE(err.empty());
        CHECK(err.find("re-authenticate") != std::string::npos);
        CHECK(err.find("token refresh failed") != std::string::npos);
    }

    SUBCASE("fresh snapshot is a no-op") {
        json cfg = xai_store({{"default", xai_account("at-live", "rt", now_ms() + 3600'000)}},
                             "default");
        scratch.write_cfg(cfg);
        int before = fake.calls.load();
        std::string err = aicad::agent::auth::ensure_fresh_for_run("xai-sub", cfg);
        CHECK(err.empty());
        CHECK(fake.calls.load() == before);
    }

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}

TEST_CASE("api-key fallback: a stale leftover subscription credential does not brick xai key mode") {
    ScratchConfig scratch;
    FakeHttp fake;
    fake.handler = [](const FakeHttp::Request&) {
        return HttpResponse{500, "server error", ""};  // non-auth failure
    };
    aicad::agent::auth::set_http_post_for_testing(
        [&fake](const std::string& u, const std::vector<std::pair<std::string, std::string>>& h,
                const std::string& b, int t) { return fake(u, h, b, t); });

    json cfg = xai_store({{"default", xai_account("at-stale", "rt-stale", now_ms() - 1000)}},
                         "default");
    cfg["provider"] = "xai";
    cfg["api_key"] = "xai-console-key-abc";
    scratch.write_cfg(cfg);

    EnsureFreshResult r = aicad::agent::auth::ensure_fresh("xai");
    CHECK(r.ok);  // API-key path soft-passes (mirrors _has_api_key_fallback)
    CHECK_FALSE(r.refreshed);
    CHECK(fake.calls.load() == 1);

    aicad::agent::auth::set_http_post_for_testing(nullptr);
}
