#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "socket_test_support.h"
#include "device/grbl_adapter.h"
#include "manufacturing/gateway/device_allowlist.h"
#include "manufacturing/gateway/transport_tcp.h"
#include "manufacturing/gateway/transport_ws.h"

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <functional>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

using namespace std::chrono_literals;
using chudware::device::Endpoint;
using chudware::device::MachineClass;
using chudware::device::TransportEvent;
using chudware::device::TransportKind;
using chudware::mfg::gateway::AllowlistEntry;
using chudware::mfg::gateway::DeviceAllowlist;
using chudware::mfg::gateway::EndpointIdentity;
using chudware::mfg::gateway::TcpTransport;
using chudware::mfg::gateway::WebSocketTransport;

namespace test_net = chudware::test::net;
using Socket = test_net::Socket;
using chudware::mfg::gateway::WebSocketTransportConfig;

namespace {

std::span<const std::byte> bytes(std::string_view text) {
    return std::as_bytes(std::span(text));
}

std::string text(std::span<const std::byte> value) {
    return {reinterpret_cast<const char*>(value.data()), value.size()};
}
void send_all(Socket socket, std::span<const std::byte> data) {
    std::size_t offset = 0;
    while (offset < data.size()) {
        const auto count =
            test_net::sendSocket(socket, data.data() + offset, data.size() - offset);
        if (count < 0 && test_net::socketInterrupted(test_net::lastSocketError())) continue;
        if (count <= 0) throw std::runtime_error("fake server send failed");
        offset += static_cast<std::size_t>(count);
    }
}

void send_all(Socket socket, std::string_view data) { send_all(socket, bytes(data)); }

std::vector<std::byte> recv_exact(Socket socket, std::size_t size) {
    std::vector<std::byte> result(size);
    std::size_t offset = 0;
    while (offset < size) {
        const auto count =
            test_net::receiveSocket(socket, result.data() + offset, size - offset);
        if (count < 0 && test_net::socketInterrupted(test_net::lastSocketError())) continue;
        if (count <= 0) throw std::runtime_error("fake server receive failed");
        offset += static_cast<std::size_t>(count);
    }
    return result;
}

std::string recv_until(Socket socket, std::string_view marker, std::size_t limit = 16384) {
    std::string result;
    while (result.find(marker) == std::string::npos) {
        char buffer[1024];
        const auto count = test_net::receiveSocket(socket, buffer, sizeof(buffer));
        if (count < 0 && test_net::socketInterrupted(test_net::lastSocketError())) continue;
        if (count <= 0) throw std::runtime_error("fake server receive-until failed");
        result.append(buffer, static_cast<std::size_t>(count));
        if (result.size() > limit) throw std::runtime_error("fake server request too large");
    }
    return result;
}

std::string header_value(const std::string& request, std::string_view name) {
    const std::string needle = std::string("\r\n") + std::string(name) + ":";
    const auto begin = request.find(needle);
    if (begin == std::string::npos) throw std::runtime_error("missing fake request header");
    const auto value_begin = request.find_first_not_of(" \t", begin + needle.size());
    const auto end = request.find("\r\n", value_begin);
    return request.substr(value_begin, end - value_begin);
}

struct ClientFrame {
    bool masked = false;
    bool fin = false;
    std::uint8_t opcode = 0;
    std::vector<std::byte> payload;
};

ClientFrame recv_client_frame(Socket socket) {
    const auto first = recv_exact(socket, 2);
    ClientFrame result;
    const auto a = std::to_integer<std::uint8_t>(first[0]);
    const auto b = std::to_integer<std::uint8_t>(first[1]);
    result.fin = (a & 0x80) != 0;
    result.opcode = a & 0x0f;
    result.masked = (b & 0x80) != 0;
    std::uint64_t length = b & 0x7f;
    if (length == 126) {
        const auto extended = recv_exact(socket, 2);
        length = (static_cast<std::uint64_t>(std::to_integer<std::uint8_t>(extended[0])) << 8) |
                 std::to_integer<std::uint8_t>(extended[1]);
    } else if (length == 127) {
        const auto extended = recv_exact(socket, 8);
        length = 0;
        for (const auto byte : extended)
            length = (length << 8) | std::to_integer<std::uint8_t>(byte);
    }
    if (length > 1024 * 1024) throw std::runtime_error("unexpected large client frame");
    std::vector<std::byte> mask;
    if (result.masked) mask = recv_exact(socket, 4);
    result.payload = recv_exact(socket, static_cast<std::size_t>(length));
    if (result.masked) {
        for (std::size_t i = 0; i < result.payload.size(); ++i)
            result.payload[i] ^= mask[i % 4];
    }
    return result;
}

std::vector<std::byte> server_frame(std::uint8_t opcode, std::span<const std::byte> payload,
                                    bool fin = true, bool masked = false) {
    std::vector<std::byte> result;
    result.push_back(static_cast<std::byte>((fin ? 0x80 : 0) | opcode));
    if (payload.size() <= 125) {
        result.push_back(static_cast<std::byte>((masked ? 0x80 : 0) | payload.size()));
    } else {
        result.push_back(static_cast<std::byte>((masked ? 0x80 : 0) | 126));
        result.push_back(static_cast<std::byte>(payload.size() >> 8));
        result.push_back(static_cast<std::byte>(payload.size()));
    }
    if (masked) {
        const std::array<std::byte, 4> mask{std::byte{1}, std::byte{2}, std::byte{3}, std::byte{4}};
        result.insert(result.end(), mask.begin(), mask.end());
        for (std::size_t i = 0; i < payload.size(); ++i) result.push_back(payload[i] ^ mask[i % 4]);
    } else {
        result.insert(result.end(), payload.begin(), payload.end());
    }
    return result;
}

class FakeServer {
public:
    using Handler = std::function<void(Socket)>;

    explicit FakeServer(Handler handler) {
        listener_ = test_net::openTcpSocket();
        if (!test_net::socketValid(listener_)) throw std::runtime_error("socket failed");
        if (!test_net::setReuseAddress(listener_)) {
            test_net::closeSocket(listener_);
            listener_ = test_net::kInvalidSocket;
            throw std::runtime_error("setsockopt failed");
        }
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        address.sin_port = 0;
        if (test_net::bindSocket(listener_, reinterpret_cast<sockaddr*>(&address),
                                 sizeof(address)) != 0 ||
            test_net::listenSocket(listener_, 1) != 0) {
            test_net::closeSocket(listener_);
            listener_ = test_net::kInvalidSocket;
            throw std::runtime_error("bind/listen failed");
        }
        test_net::AddressLength size = sizeof(address);
        if (test_net::getSocketName(listener_, reinterpret_cast<sockaddr*>(&address), &size) != 0)
            throw std::runtime_error("getsockname failed");
        port_ = ntohs(address.sin_port);
        thread_ = std::thread([this, handler = std::move(handler)] {
            try {
                const Socket peer = test_net::acceptSocket(listener_);
                if (!test_net::socketValid(peer)) throw std::runtime_error("accept failed");
                if (!test_net::suppressBrokenPipe(peer))
                    throw std::runtime_error("setsockopt failed");
                handler(peer);
                test_net::closeSocket(peer);
            } catch (...) {
                failure_ = std::current_exception();
            }
            test_net::closeSocket(listener_);
            listener_ = test_net::kInvalidSocket;
        });
    }

    ~FakeServer() {
        if (thread_.joinable()) thread_.join();
        if (test_net::socketValid(listener_)) test_net::closeSocket(listener_);
    }

    std::uint16_t port() const { return port_; }

    void finish() {
        if (thread_.joinable()) thread_.join();
        if (failure_) std::rethrow_exception(failure_);
    }

private:
    test_net::SocketRuntime socket_runtime_;
    Socket listener_ = test_net::kInvalidSocket;
    std::uint16_t port_ = 0;
    std::thread thread_;
    std::exception_ptr failure_;
};

class CommissionedEndpoint {
private:
    static std::atomic<unsigned> counter_;
    std::filesystem::path file_;

public:
    CommissionedEndpoint(TransportKind kind, std::uint16_t port, std::string path = {},
                         std::string host = "127.0.0.1",
                         std::string address_class = {})
        : file_(std::filesystem::temp_directory_path() /
                ("chudware-net-allowlist-" + std::to_string(test_net::processId()) + "-" +
                 std::to_string(counter_.fetch_add(1)))),
          allowlist(file_) {
        AllowlistEntry entry;
        entry.endpoint = EndpointIdentity::network(kind, std::move(host), port);
        entry.machine_class = MachineClass::OpenFrameMill;
        if (!path.empty()) entry.connection["path"] = std::move(path);
        if (!address_class.empty())
            entry.connection["address_class"] = std::move(address_class);
        entry.operator_note = "in-process transport fixture";
        std::string why;
        if (!allowlist.upsert(entry, &why)) throw std::runtime_error(why);
    }

    ~CommissionedEndpoint() {
        std::error_code ignored;
        std::filesystem::remove(file_, ignored);
    }

    DeviceAllowlist allowlist;
};
std::atomic<unsigned> CommissionedEndpoint::counter_{0};

std::string ws_upgrade(Socket peer, std::string_view accept_override = {},
                       std::string_view extra_header = {}) {
    const std::string request = recv_until(peer, "\r\n\r\n");
    const std::string key = header_value(request, "Sec-WebSocket-Key");
    const std::string accept = accept_override.empty()
                                   ? WebSocketTransport::handshakeAcceptForTest(key)
                                   : std::string(accept_override);
    send_all(peer, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Accept: " +
                       accept + "\r\n" + std::string(extra_header) + "\r\n");
    return request;
}

Endpoint tcp_endpoint(std::uint16_t port, bool physical = false) {
    return {"tcp-fixture", "tcp://127.0.0.1:" + std::to_string(port), physical};
}

Endpoint ws_endpoint(std::uint16_t port, bool physical = false) {
    return {"ws-fixture", "ws://127.0.0.1:" + std::to_string(port) + "/fluidnc", physical};
}

std::vector<TransportEvent> poll_until(chudware::device::IReadTransport& transport,
                                       std::size_t minimum = 1) {
    std::vector<TransportEvent> result;
    const auto end = std::chrono::steady_clock::now() + 2s;
    while (result.size() < minimum && std::chrono::steady_clock::now() < end) {
        auto batch = transport.poll(std::chrono::steady_clock::now() + 100ms);
        result.insert(result.end(), std::make_move_iterator(batch.events.begin()),
                      std::make_move_iterator(batch.events.end()));
    }
    return result;
}

}  // namespace

TEST_CASE("RFC 6455 SHA-1 and Accept vectors are exact") {
    CHECK(WebSocketTransport::sha1Base64ForTest("") == "2jmj7l5rSw0yVb/vlWAYkK/YBwk=");
    CHECK(WebSocketTransport::sha1Base64ForTest("abc") == "qZk+NkcGgWq6PiVxeFDCbJzQ2J0=");
    CHECK(WebSocketTransport::handshakeAcceptForTest("dGhlIHNhbXBsZSBub25jZQ==") ==
          "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=");
}

TEST_CASE("TCP round trip is non-blocking, commissioned, simulated, and capped at transport") {
    FakeServer server([](Socket peer) {
        const auto request = recv_exact(peer, 5);
        send_all(peer, request);
        // Round-trip completion, not an arbitrary teardown timer, owns this socket.
        if (recv_exact(peer, 1).front() != std::byte{'!'})
            throw std::runtime_error("echo receipt acknowledgement missing");
    });
    CommissionedEndpoint commissioned(TransportKind::Tcp, server.port());
    TcpTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill, 1s);
    const Endpoint endpoint = tcp_endpoint(server.port(), false);

    chudware::device::GrblAdapter adapter(transport, [] { return std::uint64_t{1}; });
    REQUIRE(adapter.open(endpoint).ok);
    CHECK(adapter.snapshot().src == chudware::mfg::FreshSource::Simulated);
    const auto capabilities = adapter.capabilityEvidence();
    CHECK(capabilities.max_advertised == chudware::device::CapabilityLevel::Transport);
    CHECK(capabilities.levels.at(chudware::device::CapabilityLevel::Execute).truth !=
          chudware::device::CapabilityTruthValue::Verified);

    const auto written = transport.write(bytes("hello"));
    REQUIRE(written.ok);
    REQUIRE(written.bytes_written == 5);
    std::string reply;
    const auto deadline = std::chrono::steady_clock::now() + 2s;
    while (reply.size() < 5 && std::chrono::steady_clock::now() < deadline) {
        const auto batch = transport.poll(deadline);
        for (const auto& event : batch.events) {
            CAPTURE(event.why);
            CAPTURE(event.raw);
            REQUIRE(event.kind == TransportEvent::Kind::Data);
            CHECK(event.raw.empty());
            reply.append(reinterpret_cast<const char*>(event.data.data()), event.data.size());
        }
    }
    CHECK(reply == "hello");
    const auto acknowledged = transport.write(bytes("!"));
    REQUIRE(acknowledged.ok);
    REQUIRE(acknowledged.bytes_written == 1);
    transport.close();
    server.finish();
}

TEST_CASE("Grbl service consumes TCP and WebSocket identity and status within a bounded poll") {
    bool websocket = false;
    SUBCASE("TCP") {}
    SUBCASE("WebSocket") { websocket = true; }
    std::atomic<bool> opened{false};
    std::atomic<bool> sent{false};
    std::atomic<bool> finished{false};
    const std::string reply =
        "Grbl 1.1h ['$' for help]\r\n"
        "[VER:1.1h:fixture]\r\n[OPT:V,15,128]\r\nok\r\n"
        "$100=250\r\n$101=250\r\n$102=250\r\nok\r\n"
        "<Idle|MPos:1,2,3|WCO:0,0,0|FS:0,0>\r\n";
    FakeServer server([&](Socket peer) {
        if (websocket) ws_upgrade(peer);
        const auto end = std::chrono::steady_clock::now() + 2s;
        while (!opened.load() && std::chrono::steady_clock::now() < end)
            std::this_thread::sleep_for(1ms);
        if (!opened.load()) throw std::runtime_error("adapter never opened fixture");
        if (websocket) send_all(peer, server_frame(0x1, bytes(reply)));
        else send_all(peer, reply);
        sent = true;
        while (!finished.load() && std::chrono::steady_clock::now() < end)
            std::this_thread::sleep_for(1ms);
    });
    const auto kind = websocket ? TransportKind::WebSocket : TransportKind::Tcp;
    CommissionedEndpoint commissioned(kind, server.port(), websocket ? "/fluidnc" : "");
    TcpTransport tcp(commissioned.allowlist, MachineClass::OpenFrameMill, 1s);
    WebSocketTransport ws(commissioned.allowlist, MachineClass::OpenFrameMill);
    chudware::device::ITransport& transport = websocket
        ? static_cast<chudware::device::ITransport&>(ws)
        : static_cast<chudware::device::ITransport&>(tcp);
    chudware::device::GrblAdapter adapter(transport, [] { return std::uint64_t{1}; });
    REQUIRE(adapter.open(websocket ? ws_endpoint(server.port()) : tcp_endpoint(server.port())).ok);
    opened = true;
    const auto end = std::chrono::steady_clock::now() + 1s;
    while (!sent.load() && std::chrono::steady_clock::now() < end)
        std::this_thread::sleep_for(1ms);
    REQUIRE(sent.load());

    // Data is ready, but an exhausted transport deadline must leave it untouched.
    CHECK(transport.poll(std::chrono::steady_clock::now()).events.empty());
    for (unsigned i = 0; i < 64; ++i) {
        adapter.service(17);
        const auto snapshot = adapter.snapshot();
        if (adapter.fingerprint() && snapshot.v &&
            snapshot.v->state == chudware::device::DeviceState::Idle) break;
    }
    CHECK(adapter.fingerprint().has_value());
    const auto& identity = adapter.identity();
    CHECK(identity.has_value());
    if (identity) {
        CHECK(identity->firmware_banner == "Grbl 1.1h ['$' for help]");
        CHECK(identity->axis_count == 3);
        CHECK(identity->transport_kind == (websocket ? "websocket" : "tcp"));
    }
    const auto snapshot = adapter.snapshot();
    CHECK(snapshot.v.has_value());
    if (snapshot.v) {
        CHECK(snapshot.v->state == chudware::device::DeviceState::Idle);
        CHECK(snapshot.v->mpos == std::array<double, 3>{1, 2, 3});
        CHECK(snapshot.seq == 17);
    }
    // A quiet socket waits briefly rather than turning the lane's service loop
    // into an immediate retry spin; it never inherits the 200 ms status interval.
    const auto quiet_started = std::chrono::steady_clock::now();
    adapter.service(18);
    const auto quiet_elapsed = std::chrono::steady_clock::now() - quiet_started;
    CHECK(quiet_elapsed >= 1ms);
    CHECK(quiet_elapsed < 100ms);
    finished = true;
    adapter.close();
    server.finish();
}

TEST_CASE("TCP flood is capped per poll, carries no raw copy, and honors an expired deadline") {
    FakeServer server([](Socket peer) {
        std::array<std::byte, 16 * 1024> chunk{};
        std::size_t sent = 0;
        while (sent < 2 * 1024 * 1024) {
            const auto count = test_net::sendSocket(peer, chunk.data(), chunk.size());
            if (count < 0 && test_net::socketInterrupted(test_net::lastSocketError())) continue;
            if (count <= 0) break;
            sent += static_cast<std::size_t>(count);
        }
    });
    CommissionedEndpoint commissioned(TransportKind::Tcp, server.port());
    TcpTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill, 1s);
    REQUIRE(transport.open(tcp_endpoint(server.port())).ok);
    std::this_thread::sleep_for(20ms);

    const auto first = transport.poll(std::chrono::steady_clock::now() + 1s);
    std::size_t first_bytes = 0;
    for (const auto& event : first.events) {
        if (event.kind != TransportEvent::Kind::Data) continue;
        first_bytes += event.data.size();
        CHECK(event.raw.empty());
    }
    CHECK(first_bytes == TcpTransport::kMaxPollBytes);

    const auto started = std::chrono::steady_clock::now();
    const auto expired = transport.poll(started);
    const auto elapsed = std::chrono::steady_clock::now() - started;
    std::size_t expired_bytes = 0;
    for (const auto& event : expired.events)
        if (event.kind == TransportEvent::Kind::Data) expired_bytes += event.data.size();
    CHECK(expired_bytes == 0);
    CHECK(elapsed < 100ms);
    transport.close();
    server.finish();
}

TEST_CASE("TCP refuses hostile DNS resolution outside the commissioned address class") {
    CommissionedEndpoint commissioned(TransportKind::Tcp, 45678, {}, "printer.internal",
                                      "local");
    TcpTransport transport(
        commissioned.allowlist, MachineClass::OpenFrameMill, 100ms, TransportKind::Tcp,
        [](std::string_view host) {
            CHECK(host == "printer.internal");
            return std::vector<std::string>{"203.0.113.10"};
        });
    const auto result =
        transport.open({"hostile-dns", "tcp://printer.internal:45678", true});
    CHECK_FALSE(result.ok);
    CHECK(result.why.find("TCP resolution refused") != std::string::npos);
    CHECK(result.why.find("public") != std::string::npos);
    CHECK(result.why.find("local") != std::string::npos);
}

TEST_CASE("TCP refuses uncommissioned endpoints before any connection attempt") {
    const auto file = std::filesystem::temp_directory_path() /
                      ("chudware-net-deny-" + std::to_string(test_net::processId()));
    std::error_code ignored;
    std::filesystem::remove(file, ignored);
    DeviceAllowlist allowlist(file);
    TcpTransport transport(allowlist, MachineClass::OpenFrameMill, 100ms);
    const auto result = transport.open(tcp_endpoint(23, true));
    CHECK_FALSE(result.ok);
    CHECK(result.why.find("commissioning refused") != std::string::npos);
    std::filesystem::remove(file, ignored);
}

TEST_CASE("TCP preserves final payload until a fresh poll and reports EOF after its bytes") {
    std::string payload = "final payload";
    bool close_before_poll = true;
    SUBCASE("closed peer has final bytes ready") {}
    SUBCASE("final bytes span the per-poll budget") {
        payload.append(TcpTransport::kMaxPollBytes, 'x');
        close_before_poll = false;
    }
    FakeServer server([&payload](Socket peer) { send_all(peer, payload); });
    CommissionedEndpoint commissioned(TransportKind::Tcp, server.port());
    TcpTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill, 1s);
    REQUIRE(transport.open(tcp_endpoint(server.port())).ok);
    if (close_before_poll) server.finish();  // Closed before the first receive.
    const auto expired = std::chrono::steady_clock::now();
    CHECK(transport.poll(expired).events.empty());
    CHECK(transport.poll(expired - 1s).events.empty());

    std::string received;
    bool closed = false;
    const auto deadline = std::chrono::steady_clock::now() + 2s;
    while (!closed && std::chrono::steady_clock::now() < deadline) {
        const auto batch = transport.poll(deadline);
        std::size_t batch_bytes = 0;
        for (const auto& event : batch.events) {
            CAPTURE(event.why);
            CHECK_FALSE(closed);
            if (event.kind == TransportEvent::Kind::Data) {
                CHECK(event.raw.empty());
                batch_bytes += event.data.size();
                received.append(reinterpret_cast<const char*>(event.data.data()), event.data.size());
            } else {
                REQUIRE(event.kind == TransportEvent::Kind::Closed);
                CHECK(event.why.find("device_lost") != std::string::npos);
                CHECK(received == payload);
                closed = true;
            }
        }
        CHECK(batch_bytes <= TcpTransport::kMaxPollBytes);
    }
    CHECK(received == payload);
    CHECK(closed);
    CHECK_FALSE(transport.write(bytes("resume")).ok);
    server.finish();
}

TEST_CASE("TCP detects a peer half-close as terminal device_lost") {
    FakeServer server([](Socket peer) {
        (void)recv_exact(peer, 1);
        if (test_net::shutdownSend(peer) != 0) throw std::runtime_error("shutdown failed");
        std::this_thread::sleep_for(50ms);
    });
    CommissionedEndpoint commissioned(TransportKind::Tcp, server.port());
    TcpTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill, 1s);
    REQUIRE(transport.open(tcp_endpoint(server.port())).ok);
    REQUIRE(transport.write(bytes("x")).ok);
    const auto events = poll_until(transport);
    REQUIRE_FALSE(events.empty());
    CHECK(events.back().kind == TransportEvent::Kind::Closed);
    CHECK(events.back().why.find("device_lost") != std::string::npos);
    CHECK_FALSE(transport.write(bytes("resume")).ok);
    server.finish();
}

TEST_CASE("TCP peer reset is terminal device_lost and never reconnects itself") {
    FakeServer server([](Socket peer) {
        const auto request = recv_exact(peer, 1);
        (void)request;
        if (!test_net::setAbortiveClose(peer)) throw std::runtime_error("setsockopt failed");
    });
    CommissionedEndpoint commissioned(TransportKind::Tcp, server.port());
    TcpTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill, 1s);
    REQUIRE(transport.open(tcp_endpoint(server.port())).ok);
    REQUIRE(transport.write(bytes("x")).ok);
    const auto events = poll_until(transport);
    REQUIRE_FALSE(events.empty());
    CHECK(events.back().kind == TransportEvent::Kind::Error);
    CHECK(events.back().why.find("device_lost") != std::string::npos);
    CHECK_FALSE(transport.write(bytes("resume")).ok);
    server.finish();
}

TEST_CASE("WebSocket handshake accepts exact Accept and refuses bad Accept or extensions") {
    SUBCASE("accepted") {
        FakeServer server([](Socket peer) {
            ws_upgrade(peer);
            (void)recv_client_frame(peer);
        });
        CommissionedEndpoint commissioned(TransportKind::WebSocket, server.port(), "/fluidnc");
        WebSocketTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill);
        REQUIRE(transport.open(ws_endpoint(server.port())).ok);
        transport.close();
        server.finish();
    }
    SUBCASE("bad Accept") {
        FakeServer server([](Socket peer) {
            ws_upgrade(peer, "AAAAAAAAAAAAAAAAAAAAAAAAAAAA");
            std::this_thread::sleep_for(20ms);
        });
        CommissionedEndpoint commissioned(TransportKind::WebSocket, server.port(), "/fluidnc");
        WebSocketTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill);
        const auto result = transport.open(ws_endpoint(server.port()));
        CHECK_FALSE(result.ok);
        CHECK(result.why.find("bad Sec-WebSocket-Accept") != std::string::npos);
        server.finish();
    }
    SUBCASE("unsupported extension") {
        FakeServer server([](Socket peer) {
            ws_upgrade(peer, {}, "Sec-WebSocket-Extensions: permessage-deflate\r\n");
            std::this_thread::sleep_for(20ms);
        });
        CommissionedEndpoint commissioned(TransportKind::WebSocket, server.port(), "/fluidnc");
        WebSocketTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill);
        const auto result = transport.open(ws_endpoint(server.port()));
        CHECK_FALSE(result.ok);
        CHECK(result.why.find("unsupported extension") != std::string::npos);
        server.finish();
    }
}

TEST_CASE("WebSocket client text and binary frames are masked") {
    std::atomic<bool> text_masked{false};
    std::atomic<bool> binary_masked{false};
    FakeServer server([&](Socket peer) {
        ws_upgrade(peer);
        const auto first = recv_client_frame(peer);
        const auto second = recv_client_frame(peer);
        text_masked = first.masked && first.fin && first.opcode == 0x1 && text(first.payload) == "hello";
        binary_masked = second.masked && second.fin && second.opcode == 0x2 &&
                        second.payload == std::vector<std::byte>{std::byte{0}, std::byte{0xff}};
    });
    CommissionedEndpoint commissioned(TransportKind::WebSocket, server.port(), "/fluidnc");
    WebSocketTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill);
    REQUIRE(transport.open(ws_endpoint(server.port())).ok);
    REQUIRE(transport.writeText("hello").ok);
    const std::array<std::byte, 2> binary{std::byte{0}, std::byte{0xff}};
    REQUIRE(transport.writeBinary(binary).ok);
    server.finish();
    CHECK(text_masked.load());
    CHECK(binary_masked.load());
    transport.close();
}

TEST_CASE("WebSocket reassembles text, answers ping, and echoes server close status") {
    std::atomic<bool> pong_ok{false};
    std::atomic<bool> close_echo_ok{false};
    FakeServer server([&](Socket peer) {
        ws_upgrade(peer);
        const auto first = server_frame(0x1, bytes("hel"), false);
        const auto ping = server_frame(0x9, bytes("probe"));
        send_all(peer, first);
        send_all(peer, ping);
        const auto pong = recv_client_frame(peer);
        pong_ok = pong.masked && pong.fin && pong.opcode == 0xa && text(pong.payload) == "probe";
        send_all(peer, server_frame(0x0, bytes("lo")));
        const std::array<std::byte, 2> close_payload{std::byte{0x03}, std::byte{0xe9}};
        send_all(peer, server_frame(0x8, close_payload));
        const auto close = recv_client_frame(peer);
        close_echo_ok = close.masked && close.opcode == 0x8 &&
                        std::equal(close.payload.begin(), close.payload.end(),
                                   close_payload.begin(), close_payload.end());
    });
    CommissionedEndpoint commissioned(TransportKind::WebSocket, server.port(), "/fluidnc");
    WebSocketTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill);
    REQUIRE(transport.open(ws_endpoint(server.port())).ok);
    const auto events = poll_until(transport, 2);
    REQUIRE(events.size() == 2);
    CHECK(events[0].kind == TransportEvent::Kind::Data);
    CHECK(text(events[0].data) == "hello");
    CHECK(events[0].raw == "text");
    CHECK(events[1].kind == TransportEvent::Kind::Closed);
    CHECK(events[1].why.find("1001") != std::string::npos);
    CHECK(transport.lastCloseCode() == 1001);
    server.finish();
    CHECK(pong_ok.load());
    CHECK(close_echo_ok.load());
}

TEST_CASE("WebSocket refuses oversized and invalid frames with protocol close") {
    SUBCASE("oversized") {
        std::atomic<std::uint16_t> close_code{0};
        FakeServer server([&](Socket peer) {
            ws_upgrade(peer);
            std::this_thread::sleep_for(20ms);
            send_all(peer, server_frame(0x2, bytes("123456789")));
            const auto close = recv_client_frame(peer);
            if (close.payload.size() >= 2)
                close_code = (std::to_integer<std::uint8_t>(close.payload[0]) << 8) |
                             std::to_integer<std::uint8_t>(close.payload[1]);
        });
        CommissionedEndpoint commissioned(TransportKind::WebSocket, server.port(), "/fluidnc");
        WebSocketTransportConfig config;
        config.max_frame_bytes = 8;
        config.max_message_bytes = 16;
        WebSocketTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill, config);
        REQUIRE(transport.open(ws_endpoint(server.port())).ok);
        const auto events = poll_until(transport);
        REQUIRE_FALSE(events.empty());
        CHECK(events.back().kind == TransportEvent::Kind::Error);
        CHECK(events.back().why.find("refusal") != std::string::npos);
        server.finish();
        CHECK(close_code.load() == 1009);
    }
    SUBCASE("masked server frame") {
        std::atomic<std::uint16_t> close_code{0};
        FakeServer server([&](Socket peer) {
            ws_upgrade(peer);
            std::this_thread::sleep_for(20ms);
            send_all(peer, server_frame(0x1, bytes("bad"), true, true));
            const auto close = recv_client_frame(peer);
            if (close.payload.size() >= 2)
                close_code = (std::to_integer<std::uint8_t>(close.payload[0]) << 8) |
                             std::to_integer<std::uint8_t>(close.payload[1]);
        });
        CommissionedEndpoint commissioned(TransportKind::WebSocket, server.port(), "/fluidnc");
        WebSocketTransport transport(commissioned.allowlist, MachineClass::OpenFrameMill);
        REQUIRE(transport.open(ws_endpoint(server.port())).ok);
        const auto events = poll_until(transport);
        REQUIRE_FALSE(events.empty());
        CHECK(events.back().why.find("server frame is masked") != std::string::npos);
        server.finish();
        CHECK(close_code.load() == 1002);
    }
}
