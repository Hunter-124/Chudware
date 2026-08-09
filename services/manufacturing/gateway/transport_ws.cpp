#include "transport_ws.h"

#include "transport_tcp.h"

#include <algorithm>
#include <array>
#include <bit>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <random>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace chudware::mfg::gateway {
namespace {

constexpr std::string_view kWebSocketGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
constexpr std::size_t kMaxHandshakeBytes = 16 * 1024;

// RFC 6455 mandates SHA-1 for Sec-WebSocket-Accept. This deliberately private,
// narrowly tested implementation exists only for that compatibility handshake;
// it is not a general-purpose or security-grade digest API.
std::array<std::uint8_t, 20> rfc6455_sha1(std::string_view input) {
    std::uint64_t bit_length = static_cast<std::uint64_t>(input.size()) * 8;
    std::vector<std::uint8_t> padded(input.begin(), input.end());
    padded.push_back(0x80);
    while (padded.size() % 64 != 56) padded.push_back(0);
    for (int shift = 56; shift >= 0; shift -= 8)
        padded.push_back(static_cast<std::uint8_t>(bit_length >> shift));

    std::uint32_t h0 = 0x67452301;
    std::uint32_t h1 = 0xefcdab89;
    std::uint32_t h2 = 0x98badcfe;
    std::uint32_t h3 = 0x10325476;
    std::uint32_t h4 = 0xc3d2e1f0;
    for (std::size_t offset = 0; offset < padded.size(); offset += 64) {
        std::array<std::uint32_t, 80> words{};
        for (std::size_t i = 0; i < 16; ++i) {
            const auto p = offset + i * 4;
            words[i] = (static_cast<std::uint32_t>(padded[p]) << 24) |
                       (static_cast<std::uint32_t>(padded[p + 1]) << 16) |
                       (static_cast<std::uint32_t>(padded[p + 2]) << 8) |
                       static_cast<std::uint32_t>(padded[p + 3]);
        }
        for (std::size_t i = 16; i < 80; ++i)
            words[i] = std::rotl(words[i - 3] ^ words[i - 8] ^ words[i - 14] ^ words[i - 16], 1);

        std::uint32_t a = h0;
        std::uint32_t b = h1;
        std::uint32_t c = h2;
        std::uint32_t d = h3;
        std::uint32_t e = h4;
        for (std::size_t i = 0; i < 80; ++i) {
            std::uint32_t f = 0;
            std::uint32_t k = 0;
            if (i < 20) {
                f = (b & c) | ((~b) & d);
                k = 0x5a827999;
            } else if (i < 40) {
                f = b ^ c ^ d;
                k = 0x6ed9eba1;
            } else if (i < 60) {
                f = (b & c) | (b & d) | (c & d);
                k = 0x8f1bbcdc;
            } else {
                f = b ^ c ^ d;
                k = 0xca62c1d6;
            }
            const std::uint32_t next = std::rotl(a, 5) + f + e + k + words[i];
            e = d;
            d = c;
            c = std::rotl(b, 30);
            b = a;
            a = next;
        }
        h0 += a;
        h1 += b;
        h2 += c;
        h3 += d;
        h4 += e;
    }

    std::array<std::uint8_t, 20> digest{};
    const std::array<std::uint32_t, 5> words{h0, h1, h2, h3, h4};
    for (std::size_t i = 0; i < words.size(); ++i) {
        digest[i * 4] = static_cast<std::uint8_t>(words[i] >> 24);
        digest[i * 4 + 1] = static_cast<std::uint8_t>(words[i] >> 16);
        digest[i * 4 + 2] = static_cast<std::uint8_t>(words[i] >> 8);
        digest[i * 4 + 3] = static_cast<std::uint8_t>(words[i]);
    }
    return digest;
}

std::string base64(std::span<const std::uint8_t> bytes) {
    static constexpr char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve(((bytes.size() + 2) / 3) * 4);
    for (std::size_t i = 0; i < bytes.size(); i += 3) {
        const std::uint32_t a = bytes[i];
        const std::uint32_t b = i + 1 < bytes.size() ? bytes[i + 1] : 0;
        const std::uint32_t c = i + 2 < bytes.size() ? bytes[i + 2] : 0;
        const std::uint32_t value = (a << 16) | (b << 8) | c;
        out.push_back(alphabet[(value >> 18) & 63]);
        out.push_back(alphabet[(value >> 12) & 63]);
        out.push_back(i + 1 < bytes.size() ? alphabet[(value >> 6) & 63] : '=');
        out.push_back(i + 2 < bytes.size() ? alphabet[value & 63] : '=');
    }
    return out;
}

std::string trim(std::string value) {
    const auto first = std::find_if_not(value.begin(), value.end(), [](unsigned char c) {
        return c == ' ' || c == '\t';
    });
    const auto last = std::find_if_not(value.rbegin(), value.rend(), [](unsigned char c) {
        return c == ' ' || c == '\t';
    }).base();
    if (first >= last) return {};
    return std::string(first, last);
}

std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

bool has_token(std::string value, std::string_view expected) {
    std::size_t begin = 0;
    while (begin <= value.size()) {
        const auto comma = value.find(',', begin);
        const auto end = comma == std::string::npos ? value.size() : comma;
        if (lower(trim(value.substr(begin, end - begin))) == expected) return true;
        if (comma == std::string::npos) break;
        begin = comma + 1;
    }
    return false;
}

bool valid_utf8(std::span<const std::byte> bytes) {
    std::size_t i = 0;
    while (i < bytes.size()) {
        const auto first = std::to_integer<std::uint8_t>(bytes[i++]);
        if (first <= 0x7f) continue;
        std::uint32_t codepoint = 0;
        std::size_t continuation = 0;
        std::uint32_t minimum = 0;
        if ((first & 0xe0) == 0xc0) {
            codepoint = first & 0x1f;
            continuation = 1;
            minimum = 0x80;
        } else if ((first & 0xf0) == 0xe0) {
            codepoint = first & 0x0f;
            continuation = 2;
            minimum = 0x800;
        } else if ((first & 0xf8) == 0xf0) {
            codepoint = first & 0x07;
            continuation = 3;
            minimum = 0x10000;
        } else {
            return false;
        }
        if (bytes.size() - i < continuation) return false;
        for (std::size_t n = 0; n < continuation; ++n) {
            const auto next = std::to_integer<std::uint8_t>(bytes[i++]);
            if ((next & 0xc0) != 0x80) return false;
            codepoint = (codepoint << 6) | (next & 0x3f);
        }
        if (codepoint < minimum || codepoint > 0x10ffff ||
            (codepoint >= 0xd800 && codepoint <= 0xdfff))
            return false;
    }
    return true;
}

bool valid_close_code(std::uint16_t code) {
    switch (code) {
        case 1000:
        case 1001:
        case 1002:
        case 1003:
        case 1007:
        case 1008:
        case 1009:
        case 1010:
        case 1011:
        case 1012:
        case 1013:
        case 1014: return true;
        default: return code >= 3000 && code <= 4999;
    }
}

struct WsAddress {
    std::string host;
    std::string authority;
    std::string target = "/";
};

bool parse_ws_address(std::string address, WsAddress& out, std::string& why) {
    if (address.starts_with("ws://")) address.erase(0, 5);
    else if (address.find("://") != std::string::npos) {
        why = "WebSocket endpoint scheme must be ws";
        return false;
    }
    const auto slash = address.find('/');
    out.authority = address.substr(0, slash);
    if (slash != std::string::npos) out.target = address.substr(slash);
    if (out.target.empty() || out.target.front() != '/' || out.target.find('#') != std::string::npos) {
        why = "WebSocket endpoint has an invalid request target";
        return false;
    }
    if (out.authority.empty()) {
        why = "WebSocket endpoint authority is empty";
        return false;
    }
    if (out.authority.front() == '[') {
        const auto close = out.authority.find(']');
        if (close == std::string::npos || close + 1 >= out.authority.size() ||
            out.authority[close + 1] != ':') {
            why = "bracketed WebSocket endpoint requires [host]:port";
            return false;
        }
        out.host = out.authority.substr(1, close - 1);
    } else {
        const auto colon = out.authority.rfind(':');
        if (colon == std::string::npos || out.authority.find(':') != colon) {
            why = "WebSocket endpoint requires host:port";
            return false;
        }
        out.host = out.authority.substr(0, colon);
    }
    return !out.host.empty();
}

std::vector<std::byte> random_bytes(std::size_t count) {
    std::random_device random;
    std::vector<std::byte> result(count);
    for (auto& value : result) value = static_cast<std::byte>(random() & 0xff);
    return result;
}

std::string as_string(std::span<const std::byte> bytes) {
    return {reinterpret_cast<const char*>(bytes.data()), bytes.size()};
}

}  // namespace

struct WebSocketTransport::Impl {
    TcpTransport tcp;
    WebSocketTransportConfig config;
    bool open = false;
    bool close_sent = false;
    std::optional<std::uint16_t> last_close_code;
    std::vector<std::byte> wire;
    std::vector<std::byte> fragmented;
    std::optional<std::uint8_t> fragmented_opcode;
    std::vector<device::TransportEvent> pending;

    Impl(DeviceAllowlist& allowlist, device::MachineClass machine_class,
         WebSocketTransportConfig value)
        : tcp(allowlist, machine_class, value.connect_timeout,
              device::TransportKind::WebSocket),
          config(value) {}

    device::WriteResult send_frame(std::uint8_t opcode, std::span<const std::byte> payload,
                                   bool fin = true) {
        if (!open) return {false, 0, "device_lost: WebSocket transport is closed"};
        if (close_sent)
            return {false, 0, "device_lost: WebSocket close handshake is already in progress"};
        if (opcode >= 0x8 && payload.size() > 125)
            return {false, 0, "WebSocket control payload exceeds 125 bytes"};

        std::vector<std::byte> frame;
        frame.reserve(payload.size() + 14);
        frame.push_back(static_cast<std::byte>((fin ? 0x80 : 0) | opcode));
        if (payload.size() <= 125) {
            frame.push_back(static_cast<std::byte>(0x80 | payload.size()));
        } else if (payload.size() <= 0xffff) {
            frame.push_back(static_cast<std::byte>(0x80 | 126));
            frame.push_back(static_cast<std::byte>((payload.size() >> 8) & 0xff));
            frame.push_back(static_cast<std::byte>(payload.size() & 0xff));
        } else {
            frame.push_back(static_cast<std::byte>(0x80 | 127));
            const auto length = static_cast<std::uint64_t>(payload.size());
            for (int shift = 56; shift >= 0; shift -= 8)
                frame.push_back(static_cast<std::byte>((length >> shift) & 0xff));
        }
        const auto mask = random_bytes(4);
        frame.insert(frame.end(), mask.begin(), mask.end());
        for (std::size_t i = 0; i < payload.size(); ++i)
            frame.push_back(payload[i] ^ mask[i % 4]);

        std::size_t sent = 0;
        const auto deadline = std::chrono::steady_clock::now() + config.handshake_timeout;
        while (sent < frame.size()) {
            const auto result = tcp.write(std::span<const std::byte>(frame).subspan(sent));
            sent += result.bytes_written;
            if (!result.ok) return {false, 0, result.why};
            if (sent == frame.size()) break;
            if (std::chrono::steady_clock::now() >= deadline)
                return {false, 0, "device_lost: WebSocket frame write timed out"};
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        return {true, payload.size(), {}};
    }

    void fail_protocol(std::string why, std::uint16_t close_code = 1002) {
        if (!open) return;
        std::vector<std::byte> close_payload{
            static_cast<std::byte>(close_code >> 8), static_cast<std::byte>(close_code & 0xff)};
        (void)send_frame(0x8, close_payload);
        close_sent = true;
        open = false;
        tcp.close();
        device::TransportEvent event;
        event.kind = device::TransportEvent::Kind::Error;
        event.why = "device_lost: WebSocket protocol refusal: " + std::move(why);
        event.raw = event.why;
        pending.push_back(std::move(event));
    }

    void receive_frame(std::uint8_t opcode, bool fin, std::vector<std::byte> payload) {
        if (opcode == 0x8) {
            if (payload.size() == 1) {
                fail_protocol("close payload has a one-byte status", 1002);
                return;
            }
            std::uint16_t code = 1005;
            if (payload.size() >= 2) {
                code = (static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(payload[0])) << 8) |
                       std::to_integer<std::uint8_t>(payload[1]);
                if (!valid_close_code(code)) {
                    fail_protocol("close payload has a forbidden status code", 1002);
                    return;
                }
                if (!valid_utf8(std::span<const std::byte>(payload).subspan(2))) {
                    fail_protocol("close reason is not valid UTF-8", 1007);
                    return;
                }
                last_close_code = code;
            }
            if (!close_sent) (void)send_frame(0x8, payload);
            close_sent = true;
            open = false;
            tcp.close();
            device::TransportEvent event;
            event.kind = device::TransportEvent::Kind::Closed;
            event.why = "device_lost: WebSocket peer closed with status " + std::to_string(code);
            event.raw = event.why;
            pending.push_back(std::move(event));
            return;
        }
        if (opcode == 0x9) {
            const auto pong = send_frame(0xa, payload);
            if (!pong.ok) {
                fail_protocol("could not answer ping: " + pong.why);
            }
            return;
        }
        if (opcode == 0xa) return;
        if (payload.size() > config.max_message_bytes) {
            fail_protocol("message exceeds configured limit", 1009);
            return;
        }

        if (opcode == 0x0) {
            if (!fragmented_opcode) {
                fail_protocol("continuation frame without an open fragmented message");
                return;
            }
            if (fragmented.size() > config.max_message_bytes - payload.size()) {
                fail_protocol("fragmented message exceeds configured limit", 1009);
                return;
            }
            fragmented.insert(fragmented.end(), payload.begin(), payload.end());
            if (!fin) return;
            opcode = *fragmented_opcode;
            payload = std::move(fragmented);
            fragmented.clear();
            fragmented_opcode.reset();
        } else {
            if (fragmented_opcode) {
                fail_protocol("new data frame arrived before fragmented message completed");
                return;
            }
            if (!fin) {
                fragmented_opcode = opcode;
                fragmented = std::move(payload);
                return;
            }
        }
        // Size was checked before fragmentation so the subtraction above cannot wrap.
        if (opcode == 0x1 && !valid_utf8(payload)) {
            fail_protocol("text message is not valid UTF-8", 1007);
            return;
        }
        device::TransportEvent event;
        event.kind = device::TransportEvent::Kind::Data;
        event.data = std::move(payload);
        event.raw = opcode == 0x1 ? "text" : "binary";
        pending.push_back(std::move(event));
    }

    void parse_frames() {
        while (open) {
            if (wire.size() < 2) return;
            const auto first = std::to_integer<std::uint8_t>(wire[0]);
            const auto second = std::to_integer<std::uint8_t>(wire[1]);
            const bool fin = (first & 0x80) != 0;
            const std::uint8_t rsv = first & 0x70;
            const std::uint8_t opcode = first & 0x0f;
            const bool masked = (second & 0x80) != 0;
            std::uint64_t length = second & 0x7f;
            std::size_t header = 2;
            if (rsv != 0) {
                fail_protocol("RSV bit set without a negotiated extension");
                return;
            }
            if (masked) {
                fail_protocol("server frame is masked");
                return;
            }
            if (opcode != 0x0 && opcode != 0x1 && opcode != 0x2 && opcode != 0x8 &&
                opcode != 0x9 && opcode != 0xa) {
                fail_protocol("reserved or unknown opcode");
                return;
            }
            const bool control = opcode >= 0x8;
            if (control && !fin) {
                fail_protocol("fragmented control frame");
                return;
            }
            if (length == 126) {
                if (wire.size() < 4) return;
                length = (static_cast<std::uint64_t>(std::to_integer<std::uint8_t>(wire[2])) << 8) |
                         std::to_integer<std::uint8_t>(wire[3]);
                header = 4;
                if (length < 126) {
                    fail_protocol("non-canonical 16-bit frame length");
                    return;
                }
            } else if (length == 127) {
                if (wire.size() < 10) return;
                if ((std::to_integer<std::uint8_t>(wire[2]) & 0x80) != 0) {
                    fail_protocol("64-bit frame length has its high bit set");
                    return;
                }
                length = 0;
                for (std::size_t i = 2; i < 10; ++i)
                    length = (length << 8) | std::to_integer<std::uint8_t>(wire[i]);
                header = 10;
                if (length < 65536) {
                    fail_protocol("non-canonical 64-bit frame length");
                    return;
                }
            }
            if (control && length > 125) {
                fail_protocol("control frame exceeds 125 bytes");
                return;
            }
            if (length > config.max_frame_bytes || length > std::numeric_limits<std::size_t>::max()) {
                fail_protocol("frame exceeds configured limit", 1009);
                return;
            }
            const auto size = static_cast<std::size_t>(length);
            if (wire.size() < header + size) return;
            std::vector<std::byte> payload(wire.begin() + static_cast<std::ptrdiff_t>(header),
                                           wire.begin() + static_cast<std::ptrdiff_t>(header + size));
            wire.erase(wire.begin(), wire.begin() + static_cast<std::ptrdiff_t>(header + size));
            receive_frame(opcode, fin, std::move(payload));
        }
    }
};

WebSocketTransport::WebSocketTransport(DeviceAllowlist& allowlist,
                                       device::MachineClass machine_class,
                                       WebSocketTransportConfig config)
    : impl_(std::make_unique<Impl>(allowlist, machine_class, config)) {}
WebSocketTransport::~WebSocketTransport() = default;
WebSocketTransport::WebSocketTransport(WebSocketTransport&&) noexcept = default;
WebSocketTransport& WebSocketTransport::operator=(WebSocketTransport&&) noexcept = default;

std::string WebSocketTransport::sha1Base64ForTest(std::string_view bytes) {
    const auto digest = rfc6455_sha1(bytes);
    return base64(digest);
}

std::string WebSocketTransport::handshakeAcceptForTest(std::string_view client_key) {
    std::string joined(client_key);
    joined += kWebSocketGuid;
    return sha1Base64ForTest(joined);
}

device::OpenResult WebSocketTransport::open(const device::Endpoint& endpoint) {
    impl_->tcp.close();
    impl_->open = false;
    impl_->close_sent = false;
    impl_->last_close_code.reset();
    impl_->wire.clear();
    impl_->fragmented.clear();
    impl_->fragmented_opcode.reset();
    impl_->pending.clear();
    if (impl_->config.handshake_timeout.count() <= 0 || impl_->config.max_frame_bytes == 0 ||
        impl_->config.max_message_bytes == 0)
        return {false, "WebSocket timeout and frame/message limits must be positive"};

    WsAddress address;
    std::string why;
    if (!parse_ws_address(endpoint.address, address, why)) return {false, std::move(why)};
    auto connected = impl_->tcp.open(endpoint);
    if (!connected.ok) return connected;

    const auto nonce = random_bytes(16);
    std::array<std::uint8_t, 16> nonce_u8{};
    for (std::size_t i = 0; i < nonce.size(); ++i)
        nonce_u8[i] = std::to_integer<std::uint8_t>(nonce[i]);
    const std::string key = base64(nonce_u8);
    const std::string request =
        "GET " + address.target + " HTTP/1.1\r\nHost: " + address.authority +
        "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + key +
        "\r\nSec-WebSocket-Version: 13\r\n\r\n";
    std::size_t sent = 0;
    const auto deadline = std::chrono::steady_clock::now() + impl_->config.handshake_timeout;
    while (sent < request.size()) {
        const auto bytes = std::as_bytes(std::span(request)).subspan(sent);
        const auto result = impl_->tcp.write(bytes);
        sent += result.bytes_written;
        if (!result.ok) {
            impl_->tcp.close();
            return {false, "WebSocket handshake write failed: " + result.why};
        }
        if (sent < request.size()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        if (std::chrono::steady_clock::now() >= deadline) {
            impl_->tcp.close();
            return {false, "WebSocket handshake write timed out"};
        }
    }

    std::vector<std::byte> response;
    std::size_t header_end = std::string::npos;
    while (std::chrono::steady_clock::now() < deadline) {
        const auto polled = impl_->tcp.poll(deadline);
        for (const auto& event : polled.events) {
            if (event.kind != device::TransportEvent::Kind::Data) {
                impl_->tcp.close();
                return {false, "WebSocket handshake lost device: " + event.why};
            }
            response.insert(response.end(), event.data.begin(), event.data.end());
            if (response.size() > kMaxHandshakeBytes) {
                impl_->tcp.close();
                return {false, "WebSocket handshake headers exceed 16384 bytes"};
            }
        }
        const std::string text = as_string(response);
        header_end = text.find("\r\n\r\n");
        if (header_end != std::string::npos) break;
    }
    if (header_end == std::string::npos) {
        impl_->tcp.close();
        return {false, "WebSocket handshake timed out before complete headers"};
    }

    const std::string headers_text = as_string(response).substr(0, header_end);
    const auto status_end = headers_text.find("\r\n");
    const std::string status = headers_text.substr(0, status_end);
    if (!status.starts_with("HTTP/1.1 101") ||
        (status.size() > 12 && status[12] != ' ' && status[12] != '\t')) {
        impl_->tcp.close();
        return {false, "WebSocket upgrade refused: expected HTTP/1.1 101"};
    }

    std::map<std::string, std::string> headers;
    std::size_t cursor = status_end == std::string::npos ? headers_text.size() : status_end + 2;
    while (cursor < headers_text.size()) {
        const auto end = headers_text.find("\r\n", cursor);
        const auto line_end = end == std::string::npos ? headers_text.size() : end;
        const std::string line = headers_text.substr(cursor, line_end - cursor);
        if (line.empty() || line.front() == ' ' || line.front() == '\t') {
            impl_->tcp.close();
            return {false, "WebSocket handshake contains an invalid folded header"};
        }
        const auto colon = line.find(':');
        if (colon == std::string::npos) {
            impl_->tcp.close();
            return {false, "WebSocket handshake contains a malformed header"};
        }
        const std::string name = lower(trim(line.substr(0, colon)));
        const std::string value = trim(line.substr(colon + 1));
        if (name.empty()) {
            impl_->tcp.close();
            return {false, "WebSocket handshake contains an empty header name"};
        }
        if (auto found = headers.find(name); found != headers.end()) found->second += "," + value;
        else headers.emplace(name, value);
        cursor = line_end + 2;
    }

    if (!has_token(headers["upgrade"], "websocket") || !has_token(headers["connection"], "upgrade")) {
        impl_->tcp.close();
        return {false, "WebSocket handshake lacks Upgrade/Connection tokens"};
    }
    const auto accept = headers.find("sec-websocket-accept");
    if (accept == headers.end() || accept->second.find(',') != std::string::npos ||
        trim(accept->second) != handshakeAcceptForTest(key)) {
        impl_->tcp.close();
        return {false, "WebSocket handshake has a bad Sec-WebSocket-Accept"};
    }
    if (const auto extension = headers.find("sec-websocket-extensions");
        extension != headers.end() && !trim(extension->second).empty()) {
        impl_->tcp.close();
        return {false, "WebSocket server selected an unsupported extension"};
    }
    if (headers.contains("sec-websocket-protocol")) {
        impl_->tcp.close();
        return {false, "WebSocket server selected an unrequested subprotocol"};
    }

    impl_->open = true;
    const auto payload_begin = header_end + 4;
    impl_->wire.assign(response.begin() + static_cast<std::ptrdiff_t>(payload_begin), response.end());
    impl_->parse_frames();
    if (!impl_->open) {
        const std::string refusal = impl_->pending.empty() ? "invalid first frame"
                                                          : impl_->pending.front().why;
        impl_->pending.clear();
        return {false, refusal};
    }
    return {true, {}};
}

void WebSocketTransport::close() {
    if (!impl_ || !impl_->open) {
        if (impl_) impl_->tcp.close();
        return;
    }
    (void)closeWithStatus(1000, {});
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(100);
    while (impl_->open && std::chrono::steady_clock::now() < deadline) (void)poll(deadline);
    impl_->open = false;
    impl_->tcp.close();
}

device::WriteResult WebSocketTransport::write(std::span<const std::byte> bytes) {
    return writeBinary(bytes);
}

device::WriteResult WebSocketTransport::writeText(std::string_view text) {
    const auto bytes = std::as_bytes(std::span(text));
    if (!valid_utf8(bytes)) return {false, 0, "WebSocket text payload is not valid UTF-8"};
    if (bytes.size() > impl_->config.max_frame_bytes)
        return {false, 0, "WebSocket frame exceeds configured limit"};
    if (bytes.size() > impl_->config.max_message_bytes)
        return {false, 0, "WebSocket message exceeds configured limit"};
    return impl_->send_frame(0x1, bytes);
}

device::WriteResult WebSocketTransport::writeBinary(std::span<const std::byte> bytes) {
    if (bytes.size() > impl_->config.max_frame_bytes)
        return {false, 0, "WebSocket frame exceeds configured limit"};
    if (bytes.size() > impl_->config.max_message_bytes)
        return {false, 0, "WebSocket message exceeds configured limit"};
    return impl_->send_frame(0x2, bytes);
}

device::WriteResult WebSocketTransport::sendPing(std::span<const std::byte> payload) {
    return impl_->send_frame(0x9, payload);
}

device::WriteResult WebSocketTransport::closeWithStatus(std::uint16_t status_code,
                                                         std::string_view reason) {
    if (!valid_close_code(status_code)) return {false, 0, "WebSocket close status is forbidden"};
    const auto reason_bytes = std::as_bytes(std::span(reason));
    if (!valid_utf8(reason_bytes)) return {false, 0, "WebSocket close reason is not valid UTF-8"};
    if (reason_bytes.size() > 123) return {false, 0, "WebSocket close reason exceeds 123 bytes"};
    std::vector<std::byte> payload{static_cast<std::byte>(status_code >> 8),
                                   static_cast<std::byte>(status_code & 0xff)};
    payload.insert(payload.end(), reason_bytes.begin(), reason_bytes.end());
    const auto result = impl_->send_frame(0x8, payload);
    if (result.ok) impl_->close_sent = true;
    return result.ok ? device::WriteResult{true, reason_bytes.size(), {}} : result;
}

std::optional<std::uint16_t> WebSocketTransport::lastCloseCode() const {
    return impl_->last_close_code;
}

device::PollResult WebSocketTransport::poll(device::Deadline deadline) {
    device::PollResult result;
    if (!impl_->pending.empty()) {
        result.events = std::move(impl_->pending);
        impl_->pending.clear();
        return result;
    }
    if (!impl_->open) return result;
    const auto polled = impl_->tcp.poll(deadline);
    for (const auto& event : polled.events) {
        if (event.kind == device::TransportEvent::Kind::Data) {
            impl_->wire.insert(impl_->wire.end(), event.data.begin(), event.data.end());
            impl_->parse_frames();
        } else {
            impl_->open = false;
            device::TransportEvent lost = event;
            if (lost.why.find("device_lost") == std::string::npos)
                lost.why = "device_lost: " + lost.why;
            impl_->pending.push_back(std::move(lost));
        }
    }
    if (!impl_->pending.empty()) {
        result.events = std::move(impl_->pending);
        impl_->pending.clear();
    }
    return result;
}

}  // namespace chudware::mfg::gateway
