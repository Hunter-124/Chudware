#include "ui/code/code_acp.h"

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <iterator>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

namespace fs = std::filesystem;
using ui::code::AcpClient;
using ui::code::AcpFrame;
using ui::code::AcpFrameKind;
using ui::code::AcpRequestId;
using nlohmann::json;

static int g_failures = 0;

#define CHECK(cond)                                                             \
    do {                                                                        \
        if (!(cond)) {                                                          \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
            ++g_failures;                                                       \
        }                                                                       \
    } while (0)

#define CHECK_EQ(a, b)                                                          \
    do {                                                                        \
        const auto _a = (a);                                                    \
        const auto _b = (b);                                                    \
        if (!(_a == _b)) {                                                      \
            std::printf("FAIL %s:%d: %s == %s\n", __FILE__, __LINE__, #a, #b); \
            ++g_failures;                                                       \
        }                                                                       \
    } while (0)

static void childWrite(const std::string& bytes) {
    std::cout.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    std::cout.flush();
}

static void childFrame(const json& frame) { childWrite(frame.dump() + "\n"); }

static int fakeAcpChild() {
    // Force the parent reader to retain a partial frame across reads.
    const std::string partial =
        json{{"jsonrpc", "2.0"},
             {"method", "test/partial"},
             {"params", {{"piece", 1}}}}
            .dump() +
        "\n";
    const std::size_t split = partial.size() / 2;
    childWrite(partial.substr(0, split));
    std::this_thread::sleep_for(std::chrono::milliseconds(80));
    childWrite(partial.substr(split));

    childWrite("this is not json\n");
    childFrame({{"jsonrpc", "2.0"},
                {"id", 900},
                {"method", "server/question"},
                {"params", {{"answerAs", "result"}}}});
    childFrame({{"jsonrpc", "2.0"},
                {"id", 901},
                {"method", "server/question"},
                {"params", {{"answerAs", "error"}}}});

    std::string line;
    while (std::getline(std::cin, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        json incoming;
        try {
            incoming = json::parse(line);
        } catch (...) {
            return 20;
        }

        const auto method = incoming.find("method");
        const auto id = incoming.find("id");
        if (method != incoming.end() && method->is_string() && id != incoming.end()) {
            const std::string name = method->get<std::string>();
            if (name == "test/one") {
                childFrame({{"jsonrpc", "2.0"},
                            {"method", "test/beforeResponse"},
                            {"params", {{"for", *id}}}});
                const std::string response =
                    json{{"jsonrpc", "2.0"},
                         {"id", *id},
                         {"result", {{"method", name}}}}
                        .dump() +
                    "\n";
                childWrite(response.substr(0, 5));
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
                childWrite(response.substr(5));
            } else if (name == "test/two") {
                childFrame({{"jsonrpc", "2.0"},
                            {"id", *id},
                            {"result", {{"method", name}}}});
            }
            continue;
        }

        if (method != incoming.end() && method->is_string() && id == incoming.end()) {
            childFrame({{"jsonrpc", "2.0"},
                        {"method", "test/notificationSeen"},
                        {"params", {{"method", *method}}}});
            continue;
        }

        if (id != incoming.end() && *id == 900 && incoming.contains("result")) {
            childFrame({{"jsonrpc", "2.0"},
                        {"method", "test/resultSeen"},
                        {"params", incoming["result"]}});
            continue;
        }
        if (id != incoming.end() && *id == 901 && incoming.contains("error")) {
            childFrame({{"jsonrpc", "2.0"},
                        {"method", "test/errorSeen"},
                        {"params", incoming["error"]}});
        }
    }
    return 0;
}

static bool collectUntil(AcpClient& client, std::vector<AcpFrame>& frames,
                         std::size_t count,
                         std::chrono::milliseconds timeout =
                             std::chrono::milliseconds(3000)) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (frames.size() < count && std::chrono::steady_clock::now() < deadline) {
        std::vector<AcpFrame> batch = client.drain();
        frames.insert(frames.end(), std::make_move_iterator(batch.begin()),
                      std::make_move_iterator(batch.end()));
        if (frames.size() < count)
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return frames.size() >= count;
}

static void testTransport(const std::string& self) {
    AcpClient client;
    std::string error;
    CHECK(client.start(self, fs::current_path().u8string(), error));
    CHECK(error.empty());
    CHECK(client.running());

    std::vector<AcpFrame> frames;
    CHECK(collectUntil(client, frames, 4));
    CHECK_EQ(frames.size(), std::size_t{4});
    if (frames.size() >= 4) {
        CHECK_EQ(frames[0].kind, AcpFrameKind::Notification);
        CHECK_EQ(frames[0].method, std::string("test/partial"));
        CHECK_EQ(frames[0].payload.value("piece", 0), 1);

        CHECK_EQ(frames[1].kind, AcpFrameKind::Notification);
        CHECK_EQ(frames[1].method, std::string("$/transportError"));
        CHECK(frames[1].payload.value("message", std::string()).find("Malformed") !=
              std::string::npos);

        CHECK_EQ(frames[2].kind, AcpFrameKind::Request);
        CHECK_EQ(frames[2].id, AcpRequestId{900});
        CHECK_EQ(frames[2].method, std::string("server/question"));
        CHECK_EQ(frames[3].kind, AcpFrameKind::Request);
        CHECK_EQ(frames[3].id, AcpRequestId{901});
    }
    CHECK(client.drain().empty()); // drain swaps rather than replaying old rows.

    CHECK(client.respond(900, {{"answer", 42}}, error));
    CHECK(error.empty());
    CHECK(client.respondError(901, -32001, "not available", error));
    CHECK(error.empty());

    AcpRequestId first = 0;
    AcpRequestId second = 0;
    CHECK(client.request("test/one", {{"value", 1}}, first, error));
    CHECK(error.empty());
    CHECK(client.notify("test/note", {{"value", 2}}, error));
    CHECK(error.empty());
    CHECK(client.request("test/two", {{"value", 3}}, second, error));
    CHECK(error.empty());
    CHECK(first != 0);
    CHECK_EQ(second, first + 1);

    CHECK(collectUntil(client, frames, 10));
    CHECK_EQ(frames.size(), std::size_t{10});
    if (frames.size() >= 10) {
        // The fake child consumes the five serialized writes in their wire order.
        CHECK_EQ(frames[4].method, std::string("test/resultSeen"));
        CHECK_EQ(frames[4].payload.value("answer", 0), 42);
        CHECK_EQ(frames[5].method, std::string("test/errorSeen"));
        CHECK_EQ(frames[5].payload.value("code", 0), -32001);
        CHECK_EQ(frames[6].kind, AcpFrameKind::Notification);
        CHECK_EQ(frames[6].method, std::string("test/beforeResponse"));
        CHECK_EQ(frames[6].payload.value("for", AcpRequestId{0}), first);
        CHECK_EQ(frames[7].kind, AcpFrameKind::Response);
        CHECK_EQ(frames[7].id, first);
        CHECK_EQ(frames[7].payload.value("method", std::string()),
                 std::string("test/one"));
        CHECK(frames[7].error.is_null());
        CHECK_EQ(frames[8].kind, AcpFrameKind::Notification);
        CHECK_EQ(frames[8].method, std::string("test/notificationSeen"));
        CHECK_EQ(frames[9].kind, AcpFrameKind::Response);
        CHECK_EQ(frames[9].id, second);
        CHECK_EQ(frames[9].payload.value("method", std::string()),
                 std::string("test/two"));
    }

    const auto beforeStop = std::chrono::steady_clock::now();
    client.stop();
    const auto stopTime = std::chrono::steady_clock::now() - beforeStop;
    CHECK(!client.running());
    CHECK(stopTime < std::chrono::seconds(2));
    client.stop(); // Must remain safe after every handle and thread is gone.
    CHECK(!client.running());

    AcpRequestId afterStop = 0;
    CHECK(!client.request("test/afterStop", json::object(), afterStop, error));
    CHECK(!error.empty());
    CHECK(afterStop > second); // Failed sends still never reuse an allocated id.
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "acp") return fakeAcpChild();

    std::error_code canonicalError;
    fs::path self = fs::weakly_canonical(fs::absolute(argv[0]), canonicalError);
    if (canonicalError) self = fs::absolute(argv[0]);
    testTransport(self.u8string());

    if (g_failures != 0) {
        std::printf("test_code_acp_transport: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_code_acp_transport: all checks passed\n");
    return 0;
}
