// core/agent/http.cpp — blocking HTTPS POST/GET (see http.h).
//
// Two backends behind one interface so the agent/LLM layer is cross-platform:
//   * Windows: native WinHTTP (no libcurl/OpenSSL dependency on the dev platform).
//   * Other (Linux/macOS): libcurl, which uses the system TLS + CA store.
// Both map onto the same HttpResponse contract: a transport-level failure (DNS,
// TLS, refused) yields status==0 with a human-readable `error`; an HTTP-level
// response yields the numeric status + full body.
#include "agent/http.h"

#include <atomic>
#include <string>
#include <vector>

#ifdef _WIN32

#include <windows.h>
#include <winhttp.h>

namespace aicad {
namespace agent {

namespace {

thread_local const std::atomic<bool>* g_http_cancel = nullptr;

// UTF-8 <-> UTF-16 conversion (WinHTTP is wide-char throughout).
std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), &w[0], n);
    return w;
}

std::string narrow(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0,
                                nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), &s[0], n, nullptr,
                        nullptr);
    return s;
}

// RAII for the WinHTTP HINTERNET handles so every error path cleans up.
struct Handle {
    HINTERNET h = nullptr;
    Handle() = default;
    explicit Handle(HINTERNET v) : h(v) {}
    ~Handle() {
        if (h) WinHttpCloseHandle(h);
    }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    operator HINTERNET() const { return h; }
    explicit operator bool() const { return h != nullptr; }
};

std::string last_error(const char* where) {
    DWORD code = GetLastError();
    return std::string(where) + " failed (GetLastError=" + std::to_string(code) + ")";
}

}  // namespace

void set_http_cancel_flag(const std::atomic<bool>* flag) { g_http_cancel = flag; }
const std::atomic<bool>* http_cancel_flag() { return g_http_cancel; }
bool http_cancel_requested() {
    return g_http_cancel && g_http_cancel->load(std::memory_order_acquire);
}

HttpResponse http_post(const std::string& url,
                       const std::vector<std::pair<std::string, std::string>>& headers,
                       const std::string& body, int timeout_ms) {
    HttpResponse out;

    std::wstring wurl = widen(url);

    // --- crack the URL into scheme/host/port/path ----------------------------
    URL_COMPONENTS uc;
    ZeroMemory(&uc, sizeof(uc));
    uc.dwStructSize = sizeof(uc);
    // Ask WinHttpCrackUrl to hand back pointers + lengths into wurl for the
    // dynamic-length fields (host + path); scheme/port are scalar.
    uc.dwSchemeLength = (DWORD)-1;
    uc.dwHostNameLength = (DWORD)-1;
    uc.dwUrlPathLength = (DWORD)-1;
    uc.dwExtraInfoLength = (DWORD)-1;
    if (!WinHttpCrackUrl(wurl.c_str(), static_cast<DWORD>(wurl.size()), 0, &uc)) {
        out.error = last_error("WinHttpCrackUrl");
        return out;
    }

    std::wstring host(uc.lpszHostName, uc.dwHostNameLength);
    // Path + query (extra info) together form the object path of the request.
    std::wstring path(uc.lpszUrlPath, uc.dwUrlPathLength);
    if (uc.dwExtraInfoLength > 0) path.append(uc.lpszExtraInfo, uc.dwExtraInfoLength);
    if (path.empty()) path = L"/";
    bool secure = (uc.nScheme == INTERNET_SCHEME_HTTPS);
    INTERNET_PORT port = uc.nPort;

    // --- session / connect / request ----------------------------------------
    Handle session(WinHttpOpen(L"aicad-agent/1.0", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                               WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0));
    if (!session) {
        out.error = last_error("WinHttpOpen");
        return out;
    }
    // Bound the whole exchange by timeout_ms (resolve/connect/send/receive).
    WinHttpSetTimeouts(session, timeout_ms, timeout_ms, timeout_ms, timeout_ms);

    Handle connect(WinHttpConnect(session, host.c_str(), port, 0));
    if (!connect) {
        out.error = last_error("WinHttpConnect");
        return out;
    }

    DWORD req_flags = secure ? WINHTTP_FLAG_SECURE : 0;
    Handle request(WinHttpOpenRequest(connect, L"POST", path.c_str(), nullptr,
                                      WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                      req_flags));
    if (!request) {
        out.error = last_error("WinHttpOpenRequest");
        return out;
    }

    // --- headers --------------------------------------------------------------
    for (const auto& h : headers) {
        std::wstring line = widen(h.first + ": " + h.second);
        if (!WinHttpAddRequestHeaders(request, line.c_str(), static_cast<DWORD>(line.size()),
                                      WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE)) {
            out.error = last_error("WinHttpAddRequestHeaders");
            return out;
        }
    }

    // --- send body ------------------------------------------------------------
    if (!WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                            const_cast<char*>(body.data()),
                            static_cast<DWORD>(body.size()),
                            static_cast<DWORD>(body.size()), 0)) {
        out.error = last_error("WinHttpSendRequest");
        return out;
    }

    if (!WinHttpReceiveResponse(request, nullptr)) {
        out.error = last_error("WinHttpReceiveResponse");
        return out;
    }

    // --- status code ----------------------------------------------------------
    DWORD status = 0;
    DWORD status_size = sizeof(status);
    if (!WinHttpQueryHeaders(request,
                             WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size,
                             WINHTTP_NO_HEADER_INDEX)) {
        out.error = last_error("WinHttpQueryHeaders");
        return out;
    }
    // --- read the full body ---------------------------------------------------
    std::string buf;
    for (;;) {
        if (http_cancel_requested()) {
            out.body.clear();
            out.status = 0;
            out.error = "cancelled";
            return out;
        }
        DWORD avail = 0;
        if (!WinHttpQueryDataAvailable(request, &avail)) {
            out.error = last_error("WinHttpQueryDataAvailable");
            return out;
        }
        if (avail == 0) break;
        std::string chunk(avail, '\0');
        DWORD read = 0;
        if (!WinHttpReadData(request, &chunk[0], avail, &read)) {
            out.error = last_error("WinHttpReadData");
            return out;
        }
        if (read == 0) break;
        buf.append(chunk.data(), read);
    }
    out.body = std::move(buf);
    return out;
}

HttpResponse http_get(const std::string& url,
                      const std::vector<std::pair<std::string, std::string>>& headers,
                      int timeout_ms) {
    HttpResponse out;
    std::wstring wurl = widen(url);

    URL_COMPONENTS uc;
    ZeroMemory(&uc, sizeof(uc));
    uc.dwStructSize = sizeof(uc);
    uc.dwSchemeLength = (DWORD)-1;
    uc.dwHostNameLength = (DWORD)-1;
    uc.dwUrlPathLength = (DWORD)-1;
    uc.dwExtraInfoLength = (DWORD)-1;
    if (!WinHttpCrackUrl(wurl.c_str(), static_cast<DWORD>(wurl.size()), 0, &uc)) {
        out.error = last_error("WinHttpCrackUrl");
        return out;
    }
    std::wstring host(uc.lpszHostName, uc.dwHostNameLength);
    std::wstring path(uc.lpszUrlPath, uc.dwUrlPathLength);
    if (uc.dwExtraInfoLength > 0) path.append(uc.lpszExtraInfo, uc.dwExtraInfoLength);
    if (path.empty()) path = L"/";
    bool secure = (uc.nScheme == INTERNET_SCHEME_HTTPS);
    INTERNET_PORT port = uc.nPort;

    Handle session(WinHttpOpen(L"aicad-agent/1.0", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                               WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0));
    if (!session) {
        out.error = last_error("WinHttpOpen");
        return out;
    }
    WinHttpSetTimeouts(session, timeout_ms, timeout_ms, timeout_ms, timeout_ms);

    Handle connect(WinHttpConnect(session, host.c_str(), port, 0));
    if (!connect) {
        out.error = last_error("WinHttpConnect");
        return out;
    }
    DWORD req_flags = secure ? WINHTTP_FLAG_SECURE : 0;
    Handle request(WinHttpOpenRequest(connect, L"GET", path.c_str(), nullptr,
                                      WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                      req_flags));
    if (!request) {
        out.error = last_error("WinHttpOpenRequest");
        return out;
    }
    for (const auto& h : headers) {
        std::wstring line = widen(h.first + ": " + h.second);
        if (!WinHttpAddRequestHeaders(request, line.c_str(), static_cast<DWORD>(line.size()),
                                      WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE)) {
            out.error = last_error("WinHttpAddRequestHeaders");
            return out;
        }
    }
    if (!WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0,
                            0, 0)) {
        out.error = last_error("WinHttpSendRequest");
        return out;
    }
    if (!WinHttpReceiveResponse(request, nullptr)) {
        out.error = last_error("WinHttpReceiveResponse");
        return out;
    }
    DWORD status = 0;
    DWORD status_size = sizeof(status);
    if (!WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size,
                             WINHTTP_NO_HEADER_INDEX)) {
        out.error = last_error("WinHttpQueryHeaders");
        return out;
    }
    out.status = static_cast<long>(status);
    std::string buf;
    for (;;) {
        DWORD avail = 0;
        if (!WinHttpQueryDataAvailable(request, &avail)) {
            out.error = last_error("WinHttpQueryDataAvailable");
            return out;
        }
        if (avail == 0) break;
        std::string chunk(avail, '\0');
        DWORD read = 0;
        if (!WinHttpReadData(request, &chunk[0], avail, &read)) {
            out.error = last_error("WinHttpReadData");
            return out;
        }
        if (read == 0) break;
        buf.append(chunk.data(), read);
    }
    out.body = std::move(buf);
    return out;
}

HttpResponse http_post_stream(const std::string& url,
                              const std::vector<std::pair<std::string, std::string>>& headers,
                              const std::string& body,
                              const HttpChunkCb& on_chunk,
                              int idle_timeout_ms) {
    // WinHTTP path: no incremental SSE parsing here (Linux/daemon is the streaming
    // target). Do a blocking POST, then deliver the whole body once so callers that
    // parse SSE still receive it. idle_timeout_ms maps to the receive timeout.
    HttpResponse r = http_post(url, headers, body,
                               idle_timeout_ms > 0 ? idle_timeout_ms : 120000);
    if (on_chunk && !r.body.empty()) on_chunk(r.body.data(), r.body.size());
    return r;
}

}  // namespace agent
}  // namespace aicad

#else  // ---------------------------------------------------------------------
       // POSIX (Linux/macOS): libcurl backend.

#include <curl/curl.h>

namespace aicad {
namespace agent {

namespace {

thread_local const std::atomic<bool>* g_http_cancel = nullptr;

}  // namespace

void set_http_cancel_flag(const std::atomic<bool>* flag) { g_http_cancel = flag; }
const std::atomic<bool>* http_cancel_flag() { return g_http_cancel; }
bool http_cancel_requested() {
    return g_http_cancel && g_http_cancel->load(std::memory_order_acquire);
}

namespace {

// Process-wide one-time curl_global_init. libcurl will lazy-init on the first
// easy handle, but doing it explicitly once (and never curl_global_cleanup, since
// these handles are created ad hoc across the process lifetime) is the documented,
// thread-safe pattern.
struct CurlGlobal {
    CurlGlobal() { curl_global_init(CURL_GLOBAL_DEFAULT); }
};
const CurlGlobal& curl_global() {
    static CurlGlobal g;
    return g;
}

size_t write_cb(char* ptr, size_t size, size_t nmemb, void* userdata) {
    if (http_cancel_requested()) return 0;  // abort transfer
    auto* buf = static_cast<std::string*>(userdata);
    const size_t n = size * nmemb;
    buf->append(ptr, n);
    return n;
}

// Progress/xfer callback: return nonzero to abort. Polled frequently by libcurl
// even while waiting on a slow response body (the 192s provider-call dead zone).
int xferinfo_cb(void* /*clientp*/, curl_off_t /*dltotal*/, curl_off_t /*dlnow*/,
                curl_off_t /*ultotal*/, curl_off_t /*ulnow*/) {
    return http_cancel_requested() ? 1 : 0;
}

// Shared request path for POST (body != nullptr) and GET (body == nullptr).
HttpResponse curl_request(const std::string& url,
                          const std::vector<std::pair<std::string, std::string>>& headers,
                          const std::string* body, int timeout_ms) {
    HttpResponse out;
    (void)curl_global();  // ensure curl_global_init ran before curl_easy_init.

    CURL* curl = curl_easy_init();
    if (!curl) {
        out.error = "curl_easy_init failed";
        return out;
    }

    struct curl_slist* hdrs = nullptr;
    for (const auto& h : headers) {
        const std::string line = h.first + ": " + h.second;
        hdrs = curl_slist_append(hdrs, line.c_str());
    }
    // Suppress libcurl's automatic "Expect: 100-continue" on large POSTs (WinHTTP
    // does not send it; some endpoints stall waiting on it).
    hdrs = curl_slist_append(hdrs, "Expect:");

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    if (hdrs) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, hdrs);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "aicad-agent/1.0");
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &out.body);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, static_cast<long>(timeout_ms));
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, static_cast<long>(timeout_ms));
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);  // mirror WinHTTP's default redirect follow.
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);        // thread-safe DNS timeouts (no SIGALRM).
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, xferinfo_cb);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, nullptr);
    if (body) {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body->data());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body->size()));
    }

    if (http_cancel_requested()) {
        if (hdrs) curl_slist_free_all(hdrs);
        curl_easy_cleanup(curl);
        out.error = "cancelled";
        return out;
    }

    const CURLcode res = curl_easy_perform(curl);
    if (res == CURLE_ABORTED_BY_CALLBACK || http_cancel_requested()) {
        out.body.clear();
        out.status = 0;
        out.error = "cancelled";
    } else if (res != CURLE_OK) {
        // Transport-level failure: status stays 0, body may be partial — clear it.
        out.body.clear();
        out.error = std::string("curl_easy_perform failed: ") + curl_easy_strerror(res);
    } else {
        long status = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
        out.status = status;
    }


    if (hdrs) curl_slist_free_all(hdrs);
    curl_easy_cleanup(curl);
    return out;
}

// Streaming variant of curl_request: emits each received buffer via on_chunk as it
// arrives, and uses an IDLE timeout (low-speed) instead of a hard total cap so a
// long-but-active LLM generation is never killed mid-stream.
struct StreamCtx {
    std::string* body;
    const HttpChunkCb* cb;
};
size_t stream_write_cb(char* ptr, size_t size, size_t nmemb, void* userdata) {
    if (http_cancel_requested()) return 0;  // abort transfer
    auto* ctx = static_cast<StreamCtx*>(userdata);
    const size_t n = size * nmemb;
    ctx->body->append(ptr, n);
    if (ctx->cb && *ctx->cb) (*ctx->cb)(ptr, n);
    return n;
}
HttpResponse curl_stream_request(const std::string& url,
                                 const std::vector<std::pair<std::string, std::string>>& headers,
                                 const std::string& body, const HttpChunkCb& on_chunk,
                                 int idle_timeout_ms) {
    HttpResponse out;
    (void)curl_global();
    CURL* curl = curl_easy_init();
    if (!curl) { out.error = "curl_easy_init failed"; return out; }
    struct curl_slist* hdrs = nullptr;
    for (const auto& h : headers) {
        const std::string line = h.first + ": " + h.second;
        hdrs = curl_slist_append(hdrs, line.c_str());
    }
    hdrs = curl_slist_append(hdrs, "Expect:");
    StreamCtx ctx{&out.body, &on_chunk};
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    if (hdrs) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, hdrs);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "aicad-agent/1.0");
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, stream_write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
    // IDLE timeout: abort only when < 1 byte/sec is sustained for idle_timeout_ms
    // (a true stall). A live token stream resets this continuously.
    const long idle_s = idle_timeout_ms > 0 ? (idle_timeout_ms + 999) / 1000 : 120;
    curl_easy_setopt(curl, CURLOPT_LOW_SPEED_LIMIT, 1L);
    curl_easy_setopt(curl, CURLOPT_LOW_SPEED_TIME, idle_s);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 30000L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 0L);  // no hard total-transfer cap
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, xferinfo_cb);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, nullptr);
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.data());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
    if (http_cancel_requested()) {
        if (hdrs) curl_slist_free_all(hdrs);
        curl_easy_cleanup(curl);
        out.error = "cancelled";
        return out;
    }
    const CURLcode res = curl_easy_perform(curl);
    if (res == CURLE_ABORTED_BY_CALLBACK || http_cancel_requested()) {
        // Keep any partial body (accumulated SSE) so the caller can salvage text.
        out.error = "cancelled";
    } else if (res != CURLE_OK) {
        // Keep any partial body (accumulated SSE) so the caller can salvage text.
        out.error = std::string("curl_easy_perform failed: ") + curl_easy_strerror(res);
    } else {
        long status = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
        out.status = status;
    }
    if (hdrs) curl_slist_free_all(hdrs);
    curl_easy_cleanup(curl);
    return out;
}

}  // namespace

HttpResponse http_post(const std::string& url,
                       const std::vector<std::pair<std::string, std::string>>& headers,
                       const std::string& body, int timeout_ms) {
    return curl_request(url, headers, &body, timeout_ms);
}

HttpResponse http_get(const std::string& url,
                      const std::vector<std::pair<std::string, std::string>>& headers,
                      int timeout_ms) {
    return curl_request(url, headers, nullptr, timeout_ms);
}

HttpResponse http_post_stream(const std::string& url,
                              const std::vector<std::pair<std::string, std::string>>& headers,
                              const std::string& body, const HttpChunkCb& on_chunk,
                              int idle_timeout_ms) {
    return curl_stream_request(url, headers, body, on_chunk, idle_timeout_ms);
}

}  // namespace agent
}  // namespace aicad

#endif  // _WIN32
