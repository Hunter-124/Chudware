#include "worker_client.h"

#include <iostream>
#include <string>

using chudware::worker::detail::ControlInfo;
using chudware::worker::detail::ControlTransport;
using nlohmann::json;

namespace {

int failures = 0;

void check(bool condition, const char* message) {
    if (condition) return;
    std::cerr << "FAIL: " << message << '\n';
    ++failures;
}

std::string token(char digit = 'a') {
    return std::string(64, digit);
}

void endpointParsing() {
    std::string err;
    ControlInfo parsed;
    json posix = {
        {"transport", "unix"},
        {"endpoint", "/tmp/chudware-control.sock"},
        {"token", token()},
    };
    check(chudware::worker::detail::parseControlInfo(posix, false, parsed, err),
          "valid POSIX endpoint parses");
    check(parsed.transport == ControlTransport::Unix,
          "POSIX endpoint selects Unix transport");
    check(parsed.endpoint == "/tmp/chudware-control.sock",
          "POSIX endpoint is preserved");
    check(parsed.token == token(), "validated token is cached");
    check(posix["token"].get<std::string>().empty(),
          "token ownership moves out of the stdio response");

    json relative = {
        {"transport", "unix"}, {"endpoint", "relative.sock"}, {"token", token()},
    };
    err.clear();
    check(!chudware::worker::detail::parseControlInfo(relative, false, parsed, err),
          "relative Unix endpoint is rejected");

    json windows = {
        {"transport", "tcp"}, {"endpoint", "127.0.0.1:49152"}, {"token", token('b')},
    };
    err.clear();
    check(chudware::worker::detail::parseControlInfo(windows, true, parsed, err),
          "valid Windows loopback endpoint parses");
    check(parsed.transport == ControlTransport::Tcp,
          "Windows endpoint selects TCP transport");

    for (const char* endpoint : {"localhost:49152", "0.0.0.0:49152",
                                 "127.0.0.1:0", "127.0.0.1:65536",
                                 "127.0.0.1:not-a-port"}) {
        json invalid = {
            {"transport", "tcp"}, {"endpoint", endpoint}, {"token", token()},
        };
        err.clear();
        check(!chudware::worker::detail::parseControlInfo(invalid, true, parsed, err),
              "non-private or malformed TCP endpoint is rejected");
    }

    json uppercaseToken = {
        {"transport", "unix"},
        {"endpoint", "/tmp/chudware-control.sock"},
        {"token", token('A')},
    };
    err.clear();
    check(!chudware::worker::detail::parseControlInfo(
              uppercaseToken, false, parsed, err),
          "control token must be canonical lowercase hex");
}

void requestFraming() {
    const std::string secret = token('c');
    std::string frame;
    std::string err;
    const json params = {{"run_id", "run-17"}, {"text", "steer now"}};
    check(chudware::worker::detail::buildControlFrame(
              secret, "steer", params, frame, err),
          "steer request frame builds");
    check(!frame.empty() && frame.back() == '\n' &&
              frame.find('\n') == frame.size() - 1,
          "request is exactly one newline-terminated frame");
    const json decoded = json::parse(frame);
    check(decoded == json{{"token", secret},
                          {"action", "steer"},
                          {"params", params}},
          "request preserves authenticated action and correlated run id");

    const json answerParams = {
        {"run_id", "run-17"},
        {"batch_id", "b_0a1b"},
        {"answers", {{"q1", "M4"}, {"q2", {{"value", 12.5}, {"unit", "mm"}}}}},
    };
    std::string answerFrame;
    err.clear();
    check(chudware::worker::detail::buildControlFrame(
              secret, "answer", answerParams, answerFrame, err),
          "answer request frame builds");
    check(json::parse(answerFrame) == json{{"token", secret},
                                           {"action", "answer"},
                                           {"params", answerParams}},
          "answer request carries batch id and typed answers verbatim");
    err.clear();
    check(!chudware::worker::detail::buildControlFrame(
              secret, "answer", json::array({"q1"}), answerFrame, err),
          "answer params must be an object");

    std::string oversized;
    err.clear();
    check(!chudware::worker::detail::buildControlFrame(
              secret, "steer",
              json{{"run_id", "run-17"},
                   {"text", std::string(
                                chudware::worker::detail::kControlRequestLimit,
                                'x')}},
              oversized, err),
          "oversized request frame is rejected");
    check(oversized.empty(), "rejected request frame is wiped");

    for (const char* action : {"arbitrary", "answers", "Answer", "eval"}) {
        err.clear();
        check(!chudware::worker::detail::buildControlFrame(
                  secret, action, json::object(), frame, err),
              "only landed control actions can be framed");
    }
}

void responseFramingAndRedaction() {
    const std::string secret = token('d');
    json result;
    std::string err;
    check(chudware::worker::detail::parseControlResponse(
              R"({"ok":true,"result":{"cancelled":true,"run_id":"run-17"}})",
              secret, result, err),
          "successful control response parses");
    check(result == json{{"cancelled", true}, {"run_id", "run-17"}},
          "successful response result maps directly");

    const std::string reflected =
        std::string("{\"ok\":false,\"error\":{\"code\":\"unauthorized\",\"") +
        "message\":\"wrong token " + secret + "\\nretry\"}}";
    err.clear();
    check(!chudware::worker::detail::parseControlResponse(
              reflected, secret, result, err),
          "wrong-token response remains a method failure");
    check(err.find(secret) == std::string::npos,
          "control token is redacted from surfaced errors");
    check(err.find('\n') == std::string::npos,
          "surfaced control errors stay on one log-safe line");

    err.clear();
    check(!chudware::worker::detail::parseControlResponse(
              std::string(chudware::worker::detail::kControlResponseLimit + 1,
                          'x'),
              secret, result, err),
          "oversized response frame is rejected before JSON parsing");
    check(err.find(secret) == std::string::npos,
          "framing errors never disclose the control token");

    err.clear();
    check(!chudware::worker::detail::parseControlResponse(
              R"({"ok":true,"result":"not-an-object"})", secret, result,
              err),
          "non-object control result is rejected");
}

void lifecycleAndFallbackSelection() {
    check(chudware::worker::detail::matchesActiveRun("run-17", "run-17"),
          "matching active worker run selects the private control lane");
    check(!chudware::worker::detail::matchesActiveRun("run-17", "run-other"),
          "different run id preserves native core control fallback");
    check(!chudware::worker::detail::matchesActiveRun("", "run-17"),
          "no active worker run preserves native core control fallback");
    check(chudware::worker::detail::kControlTimeoutMs > 0 &&
              chudware::worker::detail::kControlTimeoutMs <= 5000,
          "control connect/send/read deadline is bounded");

    std::string cachedToken = token('e');
    chudware::worker::detail::secureClear(cachedToken);
    check(cachedToken.empty(), "worker lifecycle clearing removes cached token");
}

} // namespace

int main() {
    endpointParsing();
    requestFraming();
    responseFramingAndRedaction();
    lifecycleAndFallbackSelection();
    if (failures != 0)
        std::cerr << failures << " worker-control checks failed\n";
    return failures == 0 ? 0 : 1;
}
