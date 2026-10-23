// core/agent/auth.cpp — see auth.h. Ports services/agent/providers/sub_auth.py
// (credential store) + subscription_login.py ensure_fresh + the five providers'
// refresh() HTTP exchanges. Python remains a reader/writer of the same store,
// so every mutation mirrors sub_auth.py: process lock + advisory
// file lock on <config.json>.lock, re-read disk inside the lock, patch only the
// touched (provider, account) slot, atomic tmp+replace write-back.
#include "agent/auth.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <memory>
#include <mutex>
#include <random>
#include <sstream>
#include <system_error>

#include "agent/paths.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/file.h>
#include <sys/utsname.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace fs = std::filesystem;

namespace chudware {
namespace agent {
namespace auth {

namespace {

// ── Constants (mirror the Python modules) ───────────────────────────────────

constexpr long long kEnsureFreshSkewMs = 5 * 60 * 1000;  // _ENSURE_FRESH_SKEW_MS
constexpr int kMaxRefreshAttempts = 8;  // _ENSURE_FRESH_MAX_REFRESH_ATTEMPTS
constexpr int kMaxScan = 64;            // _ENSURE_FRESH_MAX_SCAN
const char* kAuthKey = "subscription_auth";
const char* kDefaultAccountId = "default";

// Per-provider OAuth constants (from the *_sub_provider.py modules).
const char* kCodexClientId = "app_EMoamEEZ73f0CkXaXp7hrann";
const char* kCodexTokenUrl = "https://auth.openai.com/oauth/token";
const char* kAnthropicClientId = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";
const char* kAnthropicTokenUrl = "https://api.anthropic.com/v1/oauth/token";
const char* kCursorRefreshUrl = "https://api2.cursor.sh/auth/exchange_user_api_key";
const char* kXaiClientId = "b1a00492-073a-47ea-816f-4c329264a828";
const char* kXaiTokenUrlFallback = "https://auth.x.ai/oauth2/token";
const char* kKimiClientId = "17e5f671-d194-4dfb-9706-5516cb48c098";
const char* kKimiDefaultOauthHost = "https://auth.kimi.com";
const char* kKimiCliVersion = "17.0.5";

long long now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

// ── Provider id resolution (subscription_login._resolve) ────────────────────

struct ProviderSpec {
    std::string friendly;  // codex | claude | cursor | xai | kimi
    std::string cred_id;   // key inside subscription_auth
};

bool resolve_provider_id(const std::string& provider, ProviderSpec& out) {
    std::string key = lower(trim(provider));
    if (key == "anthropic") key = "claude";
    if (key == "codex" || key == "codex-sub" || key == "openai-codex") {
        out = {"codex", "openai-codex"};
        return true;
    }
    if (key == "claude" || key == "anthropic-sub") {
        out = {"claude", "anthropic-sub"};
        return true;
    }
    if (key == "cursor" || key == "cursor-sub") {
        out = {"cursor", "cursor"};
        return true;
    }
    if (key == "xai" || key == "xai-sub") {
        out = {"xai", "xai-sub"};
        return true;
    }
    if (key == "kimi" || key == "kimi-sub") {
        out = {"kimi", "kimi-sub"};
        return true;
    }
    return false;
}

// ── Credential store (sub_auth.py) ──────────────────────────────────────────

fs::path config_path() { return paths_config(); }
fs::path lock_path() { return config_path().parent_path() / (config_path().filename().string() + ".lock"); }
// Per-process tmp name: every writer of config.json takes lock_path(), but if that lock is
// ever unavailable a shared ".tmp" lets two writers publish each other's half-written file.
fs::path tmp_path() {
    return config_path().parent_path() /
           (config_path().filename().string() + ".tmp." + std::to_string(
#ifdef _WIN32
                (long long)GetCurrentProcessId()
#else
                (long long)::getpid()
#endif
                ));
}

json load_cfg() {
    std::error_code ec;
    if (!fs::exists(config_path(), ec)) return json::object();
    try {
        std::ifstream fh(config_path(), std::ios::binary);
        if (!fh) return json::object();
        std::ostringstream ss;
        ss << fh.rdbuf();
        json j = json::parse(ss.str());
        return j.is_object() ? j : json::object();
    } catch (...) {
        return json::object();
    }
}

// Atomic replace (sub_auth._write_cfg): tmp file + rename so concurrent readers
// never see a partial JSON document.
void write_cfg(const json& cfg) {
    std::error_code ec;
    fs::create_directories(config_path().parent_path(), ec);
#ifndef _WIN32
    (void)::chmod(config_path().parent_path().c_str(), 0700);
    const int fd = ::open(tmp_path().c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;
    const std::string data = cfg.dump(2);
    size_t written = 0;
    while (written < data.size()) {
        const ssize_t n = ::write(fd, data.data() + written, data.size() - written);
        if (n <= 0) { ::close(fd); fs::remove(tmp_path(), ec); return; }
        written += static_cast<size_t>(n);
    }
    if (::fsync(fd) != 0 || ::close(fd) != 0) { fs::remove(tmp_path(), ec); return; }
#else
    {
        std::ofstream fh(tmp_path(), std::ios::binary | std::ios::trunc);
        fh << cfg.dump(2);
        if (!fh) return;
    }
#endif
    fs::rename(tmp_path(), config_path(), ec);
    if (ec) {
        // Windows rename refuses an existing target; os.replace semantics instead.
        std::error_code ec2;
        fs::remove(config_path(), ec2);
        fs::rename(tmp_path(), config_path(), ec);
    }
#ifndef _WIN32
    if (!ec) (void)::chmod(config_path().c_str(), 0600);
#endif
}

// Advisory exclusive lock on <config.json>.lock — the SAME file sub_auth._lock_path
// uses, so C++ and Python refreshes serialize against each other during the
// transition window. flock on POSIX, LockFileEx on Windows.
class FileLock {
public:
    explicit FileLock(const fs::path& p) {
        std::error_code ec;
        fs::create_directories(p.parent_path(), ec);
#ifndef _WIN32
        (void)::chmod(p.parent_path().c_str(), 0700);
#endif
#ifdef _WIN32
        h_ = CreateFileW(p.c_str(), GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS,
                         FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h_ == INVALID_HANDLE_VALUE) {
            h_ = nullptr;
            return;
        }
        OVERLAPPED ov{};
        if (!LockFileEx(h_, LOCKFILE_EXCLUSIVE_LOCK, 0, 1, 0, &ov)) {
            CloseHandle(h_);
            h_ = nullptr;
        }
#else
        fd_ = ::open(p.c_str(), O_CREAT | O_RDWR, 0644);
        if (fd_ < 0) return;
        if (flock(fd_, LOCK_EX) != 0) {
            ::close(fd_);
            fd_ = -1;
        }
#endif
    }
    ~FileLock() {
#ifdef _WIN32
        if (h_) {
            OVERLAPPED ov{};
            UnlockFileEx(h_, 0, 1, 0, &ov);
            CloseHandle(h_);
        }
#else
        if (fd_ >= 0) {
            flock(fd_, LOCK_UN);
            ::close(fd_);
        }
#endif
    }
    FileLock(const FileLock&) = delete;
    FileLock& operator=(const FileLock&) = delete;

private:
#ifdef _WIN32
    HANDLE h_ = nullptr;
#else
    int fd_ = -1;
#endif
};

// sub_auth._with_cfg_lock: process lock (recursive, mirrors threading.RLock) +
// file lock. The FILE lock is depth-aware: nested with_cfg_lock calls in the
// same process (e.g. save_credential_tokens inside the refresh critical
// section, or patch_credential inside refresh_xai) must not re-flock the same
// file — a second flock(LOCK_EX) on a separate open description would
// self-deadlock. In-process serialization is already guaranteed by the
// recursive mutex, so only the outermost acquisition touches the file.
class StoreLock {
public:
    StoreLock() {
        mutex().lock();
        if (++depth() == 1) file_lock().reset(new FileLock(lock_path()));
    }
    ~StoreLock() {
        if (--depth() == 0) file_lock().reset();
        mutex().unlock();
    }
    StoreLock(const StoreLock&) = delete;
    StoreLock& operator=(const StoreLock&) = delete;

private:
    static std::recursive_mutex& mutex() {
        static std::recursive_mutex m;
        return m;
    }
    static int& depth() {
        static int d = 0;
        return d;
    }
    static std::unique_ptr<FileLock>& file_lock() {
        static std::unique_ptr<FileLock> fl;
        return fl;
    }
};

template <typename Fn>
auto with_cfg_lock(Fn&& fn) -> decltype(fn()) {
    StoreLock lk;
    return fn();
}

json load_all() {
    json cfg = load_cfg();
    if (cfg.contains(kAuthKey) && cfg[kAuthKey].is_object()) return cfg[kAuthKey];
    return json::object();
}

// sub_auth._normalize_entry: {active, accounts} from legacy flat or multi storage.
json normalize_entry(const json& entry) {
    if (!entry.is_object()) return json{{"active", kDefaultAccountId}, {"accounts", json::object()}};
    if (entry.contains("accounts") && entry["accounts"].is_object()) {
        json accounts = json::object();
        for (auto& [k, v] : entry["accounts"].items())
            if (v.is_object()) accounts[k] = v;
        std::string active;
        if (entry.contains("active") && entry["active"].is_string())
            active = entry["active"].get<std::string>();
        if (active.empty() || !accounts.contains(active)) {
            active = accounts.empty() ? std::string(kDefaultAccountId) : accounts.begin().key();
        }
        return json{{"active", active}, {"accounts", accounts}};
    }
    // Legacy single-account credential (flat token dict).
    std::string aid = kDefaultAccountId;
    if (entry.contains("account_id") && entry["account_id"].is_string() &&
        !trim(entry["account_id"].get<std::string>()).empty())
        aid = trim(entry["account_id"].get<std::string>());
    return json{{"active", aid}, {"accounts", json{{aid, entry}}}};
}

// sub_auth._commit_provider_entry: write one provider's multi-account entry
// into cfg (caller holds the lock); drop the provider key when no accounts remain.
void commit_provider_entry(const std::string& cred_id, const json& entry, json& cfg) {
    if (!cfg.contains(kAuthKey) || !cfg[kAuthKey].is_object()) cfg[kAuthKey] = json::object();
    json& auth = cfg[kAuthKey];
    json accounts = entry.value("accounts", json::object());
    if (!accounts.is_object() || accounts.empty()) {
        auth.erase(cred_id);
        return;
    }
    std::string active;
    if (entry.contains("active") && entry["active"].is_string())
        active = entry["active"].get<std::string>();
    if (active.empty() || !accounts.contains(active)) active = accounts.begin().key();
    auth[cred_id] = json{{"active", active}, {"accounts", accounts}};
}

std::string get_active_account_id(const std::string& cred_id) {
    json entry = normalize_entry(load_all().value(cred_id, json()));
    if (entry["accounts"].empty()) return "";
    return entry["active"].get<std::string>();
}

bool set_active_account(const std::string& cred_id, const std::string& aid) {
    return with_cfg_lock([&] {
        json cfg = load_cfg();
        json auth = cfg.value(kAuthKey, json::object());
        if (!auth.is_object()) auth = json::object();
        json entry = normalize_entry(auth.value(cred_id, json()));
        if (!entry["accounts"].contains(aid)) return false;
        entry["active"] = aid;
        commit_provider_entry(cred_id, entry, cfg);
        write_cfg(cfg);
        return true;
    });
}

// sub_auth._merge_saved_cred (replace_tokens=True path): install token fields,
// preserve everything already on disk that the refresh response does not carry,
// and clear a prior revoked marker on a successful token write.
json merge_saved_cred(const json& prev, const json& incoming) {
    json base = prev.is_object() ? prev : json::object();
    for (auto& [k, v] : incoming.items()) base[k] = v;
    bool has_tokens = (incoming.contains("access_token") && !incoming["access_token"].is_null()) ||
                      (incoming.contains("refresh_token") && !incoming["refresh_token"].is_null());
    if (has_tokens) {
        if (!incoming.contains("refresh_revoked")) base.erase("refresh_revoked");
        if (!incoming.contains("auth_error")) base.erase("auth_error");
    }
    return base;
}

// sub_auth.save_credential (refresh path: set_active=True, replace_tokens=True).
// Under the lock, re-read disk and patch ONLY this (provider, account) slot so a
// parallel writer's rotated refresh token on another account is never clobbered.
// Slot resolution mirrors _resolve_save_account_id: an explicit account_id wins
// (xAI binds the candidate slot), else cred["account_id"], else the active slot.
// Returns the account_id actually written.
std::string save_credential_tokens(const std::string& cred_id, const json& cred,
                                   const std::string& account_id) {
    return with_cfg_lock([&] {
        json cfg = load_cfg();
        json auth = cfg.value(kAuthKey, json::object());
        if (!auth.is_object()) auth = json::object();
        json entry = normalize_entry(auth.value(cred_id, json()));
        std::string aid = account_id;
        if (aid.empty()) {
            if (cred.contains("account_id") && cred["account_id"].is_string() &&
                !trim(cred["account_id"].get<std::string>()).empty())
                aid = trim(cred["account_id"].get<std::string>());
            else if (!entry["accounts"].empty() && entry["accounts"].contains(entry["active"]))
                aid = entry["active"].get<std::string>();
            else
                aid = kDefaultAccountId;
        }
        json prev = entry["accounts"].value(aid, json::object());
        json stored = merge_saved_cred(prev, cred);
        if (!stored.contains("account_id") && aid != kDefaultAccountId)
            stored["account_id"] = aid;
        entry["accounts"][aid] = stored;
        entry["active"] = aid;
        commit_provider_entry(cred_id, entry, cfg);
        write_cfg(cfg);
        return aid;
    });
}

// sub_auth.patch_credential: patch explicit fields on one account under lock
// (null value erases the key). Used to mark/clear refresh_revoked.
bool patch_credential(const std::string& cred_id, const std::string& aid_in,
                      const json& fields) {
    if (!fields.is_object() || fields.empty()) return false;
    return with_cfg_lock([&] {
        json cfg = load_cfg();
        json auth = cfg.value(kAuthKey, json::object());
        if (!auth.is_object()) auth = json::object();
        json entry = normalize_entry(auth.value(cred_id, json()));
        if (entry["accounts"].empty()) return false;
        std::string aid = aid_in.empty() ? entry["active"].get<std::string>() : aid_in;
        if (!entry["accounts"].contains(aid)) return false;
        json prev = entry["accounts"][aid];
        for (auto& [k, v] : fields.items()) {
            if (v.is_null())
                prev.erase(k);
            else
                prev[k] = v;
        }
        entry["accounts"][aid] = prev;
        commit_provider_entry(cred_id, entry, cfg);
        write_cfg(cfg);
        return true;
    });
}

// sub_auth.list_accounts order: active first, then account_id.
std::vector<std::string> list_account_ids(const std::string& cred_id) {
    json entry = normalize_entry(load_all().value(cred_id, json()));
    std::vector<std::string> out;
    std::string active = entry["active"].get<std::string>();
    if (entry["accounts"].contains(active)) out.push_back(active);
    for (auto& [k, v] : entry["accounts"].items())
        if (k != active) out.push_back(k);
    return out;
}

// ── Small helpers ───────────────────────────────────────────────────────────

std::string str_field(const json& j, const char* key) {
    if (j.is_object() && j.contains(key) && j[key].is_string())
        return j[key].get<std::string>();
    return "";
}

double num_field(const json& j, const char* key, double dflt = 0.0) {
    if (j.is_object() && j.contains(key) && j[key].is_number()) return j[key].get<double>();
    return dflt;
}

bool truthy(const json& j, const char* key) {
    if (!j.is_object() || !j.contains(key)) return false;
    const json& v = j[key];
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number()) return v.get<double>() != 0.0;
    if (v.is_string()) return !v.get<std::string>().empty();
    return !v.is_null();
}

long long expires_at_ms(const json& cred) {
    return static_cast<long long>(num_field(cred, "expires_at_ms", 0.0));
}

// base64url decode (JWT payloads; no padding required).
std::string b64url_decode(const std::string& in) {
    static const std::string alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    int val = 0, bits = -8;
    for (unsigned char c : in) {
        size_t pos = alpha.find(c);
        if (pos == std::string::npos) break;
        val = (val << 6) + static_cast<int>(pos);
        bits += 6;
        if (bits >= 0) {
            out.push_back(static_cast<char>((val >> bits) & 0xFF));
            bits -= 8;
        }
    }
    return out;
}

// sub_auth.decode_jwt_payload: claims without signature verification.
json decode_jwt_payload(const std::string& token) {
    size_t d1 = token.find('.');
    if (d1 == std::string::npos) return json::object();
    size_t d2 = token.find('.', d1 + 1);
    std::string payload = token.substr(d1 + 1, d2 == std::string::npos ? d2 : d2 - d1 - 1);
    try {
        json j = json::parse(b64url_decode(payload));
        return j.is_object() ? j : json::object();
    } catch (...) {
        return json::object();
    }
}

// urllib.parse.urlencode (quote_via=quote_plus): unreserved [A-Za-z0-9_.-~] stay,
// space -> '+', everything else %XX.
std::string url_encode(const std::string& s) {
    std::string out;
    char buf[4];
    for (unsigned char c : s) {
        if (std::isalnum(c) || c == '_' || c == '.' || c == '-' || c == '~')
            out.push_back(static_cast<char>(c));
        else if (c == ' ')
            out.push_back('+');
        else {
            std::snprintf(buf, sizeof(buf), "%%%02X", c);
            out += buf;
        }
    }
    return out;
}

std::string form_encode(const std::vector<std::pair<std::string, std::string>>& fields) {
    std::string out;
    for (size_t i = 0; i < fields.size(); ++i) {
        if (i) out.push_back('&');
        out += url_encode(fields[i].first);
        out.push_back('=');
        out += url_encode(fields[i].second);
    }
    return out;
}

// ── HTTP seam (injectable for tests) ────────────────────────────────────────

HttpPostFn g_test_post;

HttpResponse do_post(const std::string& url,
                     const std::vector<std::pair<std::string, std::string>>& headers,
                     const std::string& body, int timeout_ms) {
    if (g_test_post) return g_test_post(url, headers, body, timeout_ms);
    return http_post(url, headers, body, timeout_ms);
}

json parse_json_body(const HttpResponse& r) {
    try {
        json j = json::parse(r.body);
        return j;
    } catch (...) {
        return json();
    }
}

// ── Per-provider refresh exchanges (the *_sub_provider.refresh() ports) ─────

struct RefreshOutcome {
    bool ok = false;
    json cred;        // stored credential on success
    std::string error;
};

// Preserve identity / usage metadata across refresh (every Python _store_tokens
// does this so a token rotation never wipes the Settings-panel identity).
void carry_identity(const json& oldc, json& cred) {
    static const char* kKeys[] = {"plan", "display_name", "email", "account_id",
                                  "last_usage", "last_usage_fetched_at"};
    for (const char* k : kKeys) {
        if (oldc.contains(k) && !oldc[k].is_null() && !cred.contains(k)) cred[k] = oldc[k];
    }
}

std::string http_error_prefix(const char* label, const HttpResponse& r) {
    if (r.status == 0)
        return std::string(label) + " token refresh failed: " + r.error;
    return std::string(label) + " token refresh failed (HTTP " + std::to_string(r.status) + ")";
}

RefreshOutcome refresh_codex(const json& cred) {
    RefreshOutcome out;
    std::string rt = str_field(cred, "refresh_token");
    HttpResponse r = do_post(
        kCodexTokenUrl, {{"Content-Type", "application/x-www-form-urlencoded"}},
        form_encode({{"grant_type", "refresh_token"},
                     {"refresh_token", rt},
                     {"client_id", kCodexClientId}}),
        30000);
    if (r.status < 200 || r.status >= 300) {
        out.error = http_error_prefix("Codex", r);
        return out;
    }
    json resp = parse_json_body(r);
    std::string access = str_field(resp, "access_token");
    if (access.empty()) {
        out.error = "Codex token response missing access_token";
        return out;
    }
    // Some token responses omit refresh_token; preserve the existing one.
    std::string new_rt = str_field(resp, "refresh_token");
    if (new_rt.empty()) new_rt = rt;
    long long exp = now_ms() + static_cast<long long>(num_field(resp, "expires_in", 3600.0)) * 1000;
    json claims = decode_jwt_payload(access);
    json nc = {{"access_token", access}, {"refresh_token", new_rt}, {"expires_at_ms", exp}};
    // account/email from the JWT when present, else keep the previous identity
    // (Python raises when chatgpt_account_id is absent; keeping the stored id
    // is strictly safer for a refresh of an already-validated account).
    json auth_claims = claims.value("https://api.openai.com/auth", json::object());
    std::string acct = str_field(auth_claims, "chatgpt_account_id");
    if (!acct.empty()) nc["account_id"] = acct;
    json profile = claims.value("https://api.openai.com/profile", json::object());
    std::string email = lower(trim(str_field(profile, "email")));
    if (!email.empty()) nc["email"] = email;
    carry_identity(cred, nc);
    out.ok = true;
    out.cred = nc;
    return out;
}

RefreshOutcome refresh_anthropic(const json& cred) {
    RefreshOutcome out;
    std::string rt = str_field(cred, "refresh_token");
    json body = {{"grant_type", "refresh_token"},
                 {"refresh_token", rt},
                 {"client_id", kAnthropicClientId}};
    HttpResponse r = do_post(kAnthropicTokenUrl,
                             {{"Content-Type", "application/json"},
                              {"anthropic-beta", "oauth-2025-04-20"},
                              {"User-Agent", "anthropic-sdk-typescript/0.94.0 userOAuthProvider"}},
                             body.dump(), 30000);
    if (r.status < 200 || r.status >= 300) {
        out.error = http_error_prefix("Claude", r);
        return out;
    }
    json resp = parse_json_body(r);
    std::string access = str_field(resp, "access_token");
    if (access.empty()) {
        out.error = "Claude token response missing access_token";
        return out;
    }
    std::string new_rt = str_field(resp, "refresh_token");
    if (new_rt.empty()) new_rt = rt;  // keep old refresh_token if none issued
    // Anthropic subtracts its REFRESH_SKEW_MS at store time.
    long long exp = now_ms() +
                    static_cast<long long>(num_field(resp, "expires_in", 3600.0)) * 1000 -
                    kEnsureFreshSkewMs;
    json nc = {{"access_token", access}, {"refresh_token", new_rt}, {"expires_at_ms", exp}};
    carry_identity(cred, nc);
    out.ok = true;
    out.cred = nc;
    return out;
}

RefreshOutcome refresh_cursor(const json& cred) {
    RefreshOutcome out;
    std::string bearer = str_field(cred, "refresh_token");
    if (bearer.empty()) bearer = str_field(cred, "access_token");
    HttpResponse r = do_post(kCursorRefreshUrl,
                             {{"Authorization", "Bearer " + bearer},
                              {"Content-Type", "application/json"}},
                             "{}", 30000);
    if (r.status < 200 || r.status >= 300) {
        out.error = http_error_prefix("Cursor", r);
        return out;
    }
    json resp = parse_json_body(r);
    std::string access = str_field(resp, "accessToken");
    if (access.empty()) {
        out.error = "Cursor token response missing accessToken";
        return out;
    }
    std::string new_rt = str_field(resp, "refreshToken");
    if (new_rt.empty()) new_rt = str_field(cred, "refresh_token");
    // Expiry from the JWT exp claim minus the skew, else a 1h fallback.
    json claims = decode_jwt_payload(access);
    long long exp;
    if (claims.contains("exp") && claims["exp"].is_number())
        exp = static_cast<long long>(claims["exp"].get<double>()) * 1000 - kEnsureFreshSkewMs;
    else
        exp = now_ms() + 3600 * 1000;
    json nc = {{"access_token", access}, {"expires_at_ms", exp}};
    if (!new_rt.empty()) nc["refresh_token"] = new_rt;
    for (const char* k : {"sub", "user_id", "userId", "id"}) {
        std::string v = str_field(claims, k);
        if (!trim(v).empty()) {
            nc["account_id"] = trim(v);
            break;
        }
    }
    for (const char* k : {"email", "preferred_username", "unique_name", "upn"}) {
        std::string v = str_field(claims, k);
        if (v.find('@') != std::string::npos) {
            nc["email"] = lower(trim(v));
            break;
        }
    }
    carry_identity(cred, nc);
    out.ok = true;
    out.cred = nc;
    return out;
}

// Kimi CLI identification headers (get_kimi_common_headers), best-effort and
// sanitized to printable ASCII like _sanitize_header.
std::string sanitize_header(const std::string& value, const std::string& fallback) {
    std::string out;
    for (unsigned char c : value)
        if (c >= 0x20 && c <= 0x7E) out.push_back(static_cast<char>(c));
    out = trim(out);
    return out.empty() ? fallback : out;
}

std::string random_hex32() {
    std::random_device rd;
    std::mt19937_64 gen(rd());
    std::string s;
    char buf[17];
    for (int i = 0; i < 2; ++i) {
        std::snprintf(buf, sizeof(buf), "%016llx",
                      static_cast<unsigned long long>(gen()));
        s += buf;
    }
    return s;
}

// Install-stable device id persisted next to config.json (kimi _get_device_id).
std::string kimi_device_id() {
    fs::path p = config_path().parent_path() / "kimi-device-id";
    try {
        std::ifstream fh(p);
        std::string existing;
        std::getline(fh, existing);
        existing = trim(existing);
        if (!existing.empty()) return existing;
    } catch (...) {
    }
    std::string id = random_hex32();
    try {
        std::error_code ec;
        fs::create_directories(p.parent_path(), ec);
        std::ofstream fh(p, std::ios::trunc);
        fh << id << "\n";
    } catch (...) {
    }
    return id;
}

std::string kimi_os_label() {
#ifdef _WIN32
    return "Windows";
#else
    struct utsname u {};
    if (uname(&u) == 0) {
        std::string label = u.sysname;
        std::string s = label + " " + u.release + " " + u.machine;
        return trim(s);
    }
    return "unknown";
#endif
}

RefreshOutcome refresh_kimi(const json& cred) {
    RefreshOutcome out;
    std::string rt = str_field(cred, "refresh_token");
    const char* host_env = std::getenv("KIMI_CODE_OAUTH_HOST");
    if (!host_env || !*host_env) host_env = std::getenv("KIMI_OAUTH_HOST");
    std::string host = (host_env && *host_env) ? host_env : kKimiDefaultOauthHost;
    std::string ua = std::string("KimiCLI/") + kKimiCliVersion;
    HttpResponse r = do_post(
        host + "/api/oauth/token",
        {{"Content-Type", "application/x-www-form-urlencoded"},
         {"Accept", "application/json"},
         {"User-Agent", ua},
         {"X-Msh-Platform", "kimi_cli"},
         {"X-Msh-Version", kKimiCliVersion},
         {"X-Msh-Device-Id", sanitize_header(kimi_device_id(), "unknown")},
         {"X-Msh-Device-Model", sanitize_header(kimi_os_label(), "unknown")}},
        form_encode({{"grant_type", "refresh_token"},
                     {"refresh_token", rt},
                     {"client_id", kKimiClientId}}),
        30000);
    if (r.status < 200 || r.status >= 300) {
        // Mirror the Python message: HTTPError + error_description / error detail
        // (the detail is what lets ensure_fresh classify invalid_grant as dead).
        std::string detail;
        json err = parse_json_body(r);
        if (err.is_object()) {
            detail = str_field(err, "error_description");
            if (detail.empty()) detail = str_field(err, "error");
        }
        out.error = http_error_prefix("Kimi", r);
        if (!detail.empty()) out.error += ": " + detail;
        return out;
    }
    json resp = parse_json_body(r);
    std::string access = str_field(resp, "access_token");
    if (access.empty() || !resp.contains("expires_in") || !resp["expires_in"].is_number()) {
        out.error = "Kimi token response missing required fields";
        return out;
    }
    // Kimi omits refresh_token on refresh responses — keep the old one.
    std::string new_rt = str_field(resp, "refresh_token");
    if (new_rt.empty()) new_rt = rt;
    // Skew is subtracted at store time.
    long long exp = now_ms() +
                    static_cast<long long>(num_field(resp, "expires_in", 3600.0)) * 1000 -
                    kEnsureFreshSkewMs;
    json nc = {{"access_token", access}, {"refresh_token", new_rt}, {"expires_at_ms", exp}};
    json claims = decode_jwt_payload(access);
    for (const char* k : {"sub", "account_id", "user_id"}) {
        std::string v = str_field(claims, k);
        if (!v.empty()) {
            nc["account_id"] = v;
            break;
        }
    }
    std::string email = str_field(claims, "email");
    if (email.empty()) {
        json profile = claims.value("profile", json::object());
        email = str_field(profile, "email");
    }
    if (!email.empty()) nc["email"] = lower(trim(email));
    carry_identity(cred, nc);
    out.ok = true;
    out.cred = nc;
    return out;
}

// xAI: rotates the refresh token on EVERY exchange — the write-back below must
// land atomically in the same critical section as the POST (the caller holds
// the store lock for the whole refresh_candidate call).
RefreshOutcome refresh_xai(const json& cred, const std::string& aid) {
    RefreshOutcome out;
    std::string rt = str_field(cred, "refresh_token");
    std::string endpoint = str_field(cred, "token_endpoint");
    if (endpoint.empty()) endpoint = kXaiTokenUrlFallback;
    HttpResponse r = do_post(
        endpoint,
        {{"Content-Type", "application/x-www-form-urlencoded"},
         {"Accept", "application/json"}},
        form_encode({{"grant_type", "refresh_token"},
                     {"client_id", kXaiClientId},
                     {"refresh_token", rt}}),
        30000);
    if (r.status < 200 || r.status >= 300) {
        std::string body = r.body.substr(0, 400);
        std::string body_l = lower(body);
        if (r.status == 403) {
            out.error = "xAI OAuth refresh 403-tier: " + body;
            return out;
        }
        if (r.status == 400 || r.status == 401) {
            bool definitive = body_l.find("invalid_grant") != std::string::npos ||
                              body_l.find("revoked") != std::string::npos ||
                              body_l.find("invalid_token") != std::string::npos ||
                              body_l.find("expired") != std::string::npos;
            if (definitive && !aid.empty()) {
                // mark_refresh_revoked: persist the invalid_grant so later scans
                // skip this account without burning the network budget.
                patch_credential("xai-sub", aid,
                                 {{"refresh_revoked", true},
                                  {"auth_error", body.substr(0, 240)}});
            }
            out.error = "xAI OAuth refresh failed — re-authenticate via "
                        "Settings → Sign in with xAI. (HTTP " +
                        std::to_string(r.status) + ": " + body + ")";
            return out;
        }
        if (r.status == 429) {
            out.error = "xAI token refresh rate-limited (HTTP 429): " + body;
            return out;
        }
        out.error = http_error_prefix("xAI", r);
        if (!body.empty()) out.error += ": " + body;
        return out;
    }
    json resp = parse_json_body(r);
    std::string access = str_field(resp, "access_token");
    if (access.empty()) {
        out.error = "xAI token response missing access_token";
        return out;
    }
    // xAI rotates RTs — prefer the rotated one, never drop a missing new RT.
    std::string new_rt = str_field(resp, "refresh_token");
    if (new_rt.empty()) new_rt = rt;
    long long exp = now_ms() + static_cast<long long>(num_field(resp, "expires_in", 3600.0)) * 1000;
    json claims = decode_jwt_payload(access);
    json nc = {{"access_token", access},
               {"refresh_token", new_rt},
               {"expires_at_ms", exp},
               {"token_endpoint", endpoint}};
    if (!str_field(resp, "id_token").empty()) nc["id_token"] = resp["id_token"];
    std::string email = str_field(claims, "email");
    if (email.empty()) {
        json profile = claims.value("profile", json::object());
        email = str_field(profile, "email");
    }
    if (!email.empty()) nc["email"] = lower(trim(email));
    for (const char* k : {"sub", "account_id", "user_id"}) {
        std::string v = str_field(claims, k);
        if (!v.empty()) {
            nc["account_id"] = v;
            break;
        }
    }
    carry_identity(cred, nc);
    out.ok = true;
    out.cred = nc;
    return out;
}

RefreshOutcome refresh_account(const ProviderSpec& spec, const json& cred,
                               const std::string& aid) {
    if (spec.friendly == "codex") return refresh_codex(cred);
    if (spec.friendly == "claude") return refresh_anthropic(cred);
    if (spec.friendly == "cursor") return refresh_cursor(cred);
    if (spec.friendly == "kimi") return refresh_kimi(cred);
    return refresh_xai(cred, aid);
}

// xAI is the one provider whose Python _store_tokens binds the candidate slot
// explicitly; the others resolve the slot from cred["account_id"] on save.
std::string save_account_id_for(const ProviderSpec& spec, const std::string& aid) {
    return spec.friendly == "xai" ? aid : std::string();
}

// ── ensure_fresh (subscription_login.ensure_fresh) ──────────────────────────

bool is_dead_refresh(const std::string& error_text) {
    std::string t = lower(error_text);
    return t.find("invalid_grant") != std::string::npos ||
           t.find("revoked") != std::string::npos ||
           t.find("re-authenticate") != std::string::npos ||
           t.find("refresh_token previously revoked") != std::string::npos ||
           t.find("no refresh_token") != std::string::npos;
}

// subscription_login._has_api_key_fallback: a stale leftover subscription
// credential must not block a classic API-key run (xai / anthropic dual-path).
bool has_api_key_fallback(const std::string& friendly) {
    if (friendly != "xai" && friendly != "claude") return false;
    json cfg = load_cfg();
    std::string prov = lower(trim(str_field(cfg, "provider")));
    std::string key = trim(str_field(cfg, "api_key"));
    if (key.empty()) return false;
    if (friendly == "claude") {
        if (prov != "anthropic") return false;
        if (key.rfind("sk-ant-oat", 0) == 0) return false;
        return key.rfind("sk-ant-", 0) == 0;
    }
    return prov == "xai";
}

long expires_in_s(const json& cred) {
    if (!cred.is_object() || !cred.contains("expires_at_ms") ||
        !cred["expires_at_ms"].is_number())
        return -1;
    long long d = expires_at_ms(cred) - now_ms();
    return d > 0 ? static_cast<long>(d / 1000) : 0;
}

EnsureFreshResult ok_result(bool refreshed, const json& cred) {
    EnsureFreshResult r;
    r.ok = true;
    r.refreshed = refreshed;
    r.expires_in_s = expires_in_s(cred);
    return r;
}

}  // namespace

bool is_subscription_name(const std::string& name) {
    ProviderSpec spec;
    return resolve_provider_id(name, spec);
}

bool is_expired(const json& cred, long long skew_ms) {
    return now_ms() >= (expires_at_ms(cred) - skew_ms);
}

json load_credential(const std::string& cred_id, const std::string& account_id) {
    json entry = normalize_entry(load_all().value(cred_id, json()));
    const json& accounts = entry["accounts"];
    if (accounts.empty()) return json();
    if (!account_id.empty()) return accounts.value(account_id, json());
    std::string active = entry["active"].get<std::string>();
    json cred = accounts.value(active, json());
    if (cred.is_null()) cred = accounts.value(kDefaultAccountId, json());
    if (cred.is_null() && !accounts.empty()) cred = accounts.begin().value();
    return cred;
}

void set_http_post_for_testing(HttpPostFn fn) { g_test_post = std::move(fn); }

EnsureFreshResult ensure_fresh(const std::string& provider) {
    ProviderSpec spec;
    if (!resolve_provider_id(provider, spec)) {
        EnsureFreshResult r;
        r.error = "unknown subscription provider '" + provider + "'";
        return r;
    }

    json cred;
    try {
        cred = load_credential(spec.cred_id);
    } catch (...) {
        EnsureFreshResult r;
        r.error = "load credential failed";
        return r;
    }
    if (cred.is_null() ||
        (str_field(cred, "access_token").empty() && str_field(cred, "refresh_token").empty())) {
        // No stored subscription credential — not a refresh failure (API-key
        // callers still proceed; the provider layer refuses later if both are
        // missing). Mirrors the Python ok:true / refreshed:false return.
        return ok_result(false, json());
    }
    if (!is_expired(cred, kEnsureFreshSkewMs)) return ok_result(false, cred);

    // Multi-account candidates: active first, then stored accounts (stable order).
    std::vector<std::string> candidates;
    {
        std::string active = get_active_account_id(spec.cred_id);
        if (!active.empty()) candidates.push_back(active);
        for (const auto& aid : list_account_ids(spec.cred_id))
            if (std::find(candidates.begin(), candidates.end(), aid) == candidates.end())
                candidates.push_back(aid);
        if (candidates.size() > static_cast<size_t>(kMaxScan)) candidates.resize(kMaxScan);
    }

    std::string last_error;
    int tried = 0;
    int saw_revoked = 0;
    int saw_candidates = 0;
    for (const std::string& aid : candidates) {
        json c = load_credential(spec.cred_id, aid);
        if (c.is_null() ||
            (str_field(c, "access_token").empty() && str_field(c, "refresh_token").empty()))
            continue;
        saw_candidates += 1;
        // Definitive prior invalid_grant: skip without burning the network budget.
        if (truthy(c, "refresh_revoked")) {
            saw_revoked += 1;
            if (!is_expired(c, 0) && !str_field(c, "access_token").empty()) {
                // Access JWT still live — promote and use until it dies.
                set_active_account(spec.cred_id, aid);
                return ok_result(false, c);
            }
            continue;
        }
        if (!is_expired(c, kEnsureFreshSkewMs)) {
            // Another account is already fresh — promote it.
            set_active_account(spec.cred_id, aid);
            return ok_result(false, c);
        }
        if (str_field(c, "refresh_token").empty()) {
            last_error = spec.friendly + " access token expired and no usable refresh_token "
                                         "for account " +
                         aid.substr(0, 12);
            continue;
        }
        if (tried >= kMaxRefreshAttempts) break;
        tried += 1;

        // Refresh serialized under the SAME store lock the save uses: re-read the
        // credential inside the lock so a peer process/thread that already rotated
        // the refresh token is reused instead of burning the old (now-revoked) RT
        // a second time — the xAI rotation rule. The POST runs inside the critical
        // section so a Python writer cannot interleave a stale read-modify-write
        // between our re-check and our write-back.
        RefreshOutcome ro;
        std::string saved_aid = aid;
        bool short_circuited = with_cfg_lock([&] {
            json fresh = load_credential(spec.cred_id, aid);
            if (!fresh.is_null() && !is_expired(fresh, kEnsureFreshSkewMs) &&
                !str_field(fresh, "access_token").empty()) {
                ro.ok = true;
                ro.cred = fresh;
                return true;
            }
            if (!fresh.is_null()) c = fresh;
            ro = refresh_account(spec, c, aid);
            if (ro.ok) saved_aid = save_credential_tokens(
                spec.cred_id, ro.cred, save_account_id_for(spec, aid));
            return false;
        });
        if (ro.ok) {
            // save_credential_tokens already pointed active at the written slot;
            // ensure the pointer even on the short-circuit path.
            set_active_account(spec.cred_id, saved_aid);
            return ok_result(!short_circuited, ro.cred);
        }
        last_error = ro.error;
        if (is_dead_refresh(ro.error)) continue;  // scan siblings for multi-account pools
        // Non-auth failure (network / 5xx / tier) — don't burn more accounts
        // unless this is xAI multi-account and the error looks per-account.
        if (spec.friendly == "xai") {
            std::string e = lower(ro.error);
            if (e.find("403") != std::string::npos || e.find("429") != std::string::npos ||
                e.find("tier") != std::string::npos || e.find("rate-limit") != std::string::npos ||
                e.find("rate limited") != std::string::npos)
                continue;
        }
        break;
    }

    // Dual-path providers (xai / anthropic) may run on a classic API key while a
    // leftover subscription credential is stale. Don't brick the API-key path.
    if (has_api_key_fallback(spec.friendly)) return ok_result(false, cred);

    EnsureFreshResult r;
    if (last_error.empty() && tried == 0) {
        if (saw_revoked && saw_candidates == saw_revoked) {
            r.error = spec.friendly + " refresh tokens are revoked on every stored account (" +
                      std::to_string(saw_revoked) +
                      "). Re-authenticate via Settings → AI → Accounts.";
        } else {
            r.error = spec.friendly +
                      " access token expired and no usable refresh_token is stored. "
                      "Re-authenticate via Settings → AI → Accounts.";
        }
    } else {
        r.error = spec.friendly +
                  " token refresh failed — re-authenticate via Settings → AI → Accounts. (" +
                  last_error + ")";
    }
    r.expires_in_s = expires_in_s(cred);
    return r;
}

std::string ensure_fresh_for_run(const std::string& name, json& cfg_inout) {
    ProviderSpec spec;
    if (!resolve_provider_id(name, spec)) return "";
    // Inspect the caller's snapshot: a fresh active credential means no disk work
    // at all (resolve_provider is called from status/probe paths too).
    json entry = json();
    if (cfg_inout.is_object() && cfg_inout.contains(kAuthKey) &&
        cfg_inout[kAuthKey].is_object())
        entry = cfg_inout[kAuthKey].value(spec.cred_id, json());
    json norm = normalize_entry(entry);
    json cred;
    const json& accounts = norm["accounts"];
    if (!accounts.empty()) {
        std::string active = norm["active"].get<std::string>();
        cred = accounts.value(active, json());
        if (cred.is_null()) cred = accounts.value(kDefaultAccountId, json());
        if (cred.is_null()) cred = accounts.begin().value();
    }
    if (cred.is_null() ||
        (str_field(cred, "access_token").empty() && str_field(cred, "refresh_token").empty()))
        return "";  // no subscription credential — descriptor refusal handles it
    if (!is_expired(cred, kEnsureFreshSkewMs)) return "";  // still fresh

    EnsureFreshResult r = ensure_fresh(spec.friendly);
    if (!r.ok) return r.error.empty() ? spec.friendly + " token refresh failed" : r.error;
    json fresh = load_cfg();
    if (fresh.is_object()) cfg_inout = fresh;
    return "";
}

}  // namespace auth
}  // namespace agent
}  // namespace chudware
