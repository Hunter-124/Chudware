// core/agent/http.cpp — WinHTTP implementation of http_post (see http.h).
#include "agent/http.h"

#include <windows.h>
#include <winhttp.h>

#include <string>
#include <vector>

namespace aicad {
namespace agent {

namespace {

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
    out.status = static_cast<long>(status);

    // --- read the full body ---------------------------------------------------
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

}  // namespace agent
}  // namespace aicad
