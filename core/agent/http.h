// core/agent/http.h — minimal HTTPS POST over native WinHTTP.
//
// The agent/LLM port (P51) talks to the Anthropic / OpenAI-compatible REST
// endpoints. To avoid pulling in libcurl / OpenSSL we use the Windows-native
// WinHTTP stack for TLS. This header is OCCT-free and pure std — it just exposes
// a single blocking POST. Streaming is NOT needed (the Python providers are all
// non-streaming `messages.create` / `chat.completions.create` calls).
#pragma once
#include <atomic>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace chudware {
namespace agent {

// Thread-local cooperative-cancel for in-flight HTTP. complete()/run_design set this
// from the run's RunControl so CURLOPT_XFERINFOFUNCTION (and WinHTTP read loops) can
// abort mid-transfer within ~1s of agent_cancel. nullptr = no cancel (default).
void set_http_cancel_flag(const std::atomic<bool>* flag);
const std::atomic<bool>* http_cancel_flag();
bool http_cancel_requested();
// The result of an http_post. On a transport-level failure (DNS, TLS, refused),
// `status` is 0 and `error` carries a human-readable reason (incl. the WinHTTP
// GetLastError code). On an HTTP-level response `status` is the HTTP status code
// and `body` holds the full response payload (which may itself be an API error
// envelope the caller parses).
struct HttpResponse {
    long status = 0;        // HTTP status code, or 0 on a transport failure.
    std::string body;       // Full response body (UTF-8).
    std::string error;      // Non-empty only on a transport failure.
};

// Blocking HTTPS (or HTTP) POST. `headers` are added verbatim ("Name: value").
// `body` is sent as the request payload. UTF-8 throughout (converted to wide for
// the WinHTTP APIs internally). `timeout_ms` bounds the receive.
HttpResponse http_post(const std::string& url,
                       const std::vector<std::pair<std::string, std::string>>& headers,
                       const std::string& body,
                       int timeout_ms = 120000);

// Blocking HTTPS (or HTTP) GET. Same semantics as http_post without a request body
// (used for availability probes like the OpenAI-compatible /v1/models endpoint).
HttpResponse http_get(const std::string& url,
                      const std::vector<std::pair<std::string, std::string>>& headers,
                      int timeout_ms = 8000);

// Streaming HTTPS POST. `on_chunk(ptr,len)` is invoked for each received buffer as
// it arrives (e.g. Server-Sent-Events `data:` frames from an LLM `stream:true`
// completion). The full concatenated body is ALSO returned in HttpResponse.body so
// callers can fall back to a whole-body parse. Instead of a hard total-transfer cap
// (which kills a long-but-active generation), this uses an IDLE timeout: the request
// is aborted only when NO bytes arrive for `idle_timeout_ms` (a true stall). A live
// token stream resets the idle timer continuously, so long completions survive.
using HttpChunkCb = std::function<void(const char* data, size_t len)>;
HttpResponse http_post_stream(const std::string& url,
                              const std::vector<std::pair<std::string, std::string>>& headers,
                              const std::string& body,
                              const HttpChunkCb& on_chunk,
                              int idle_timeout_ms = 120000);

}  // namespace agent
}  // namespace chudware
