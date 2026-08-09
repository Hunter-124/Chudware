#include "transport_tcp.h"

#include "device_allowlist.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <limits>
#include <string>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace chudware::mfg::gateway {
namespace {

#ifdef _WIN32
using Socket = SOCKET;
constexpr Socket kInvalidSocket = INVALID_SOCKET;
int socket_error() { return WSAGetLastError(); }
bool interrupted(int error) { return error == WSAEINTR; }
bool would_block(int error) { return error == WSAEWOULDBLOCK; }
bool connect_pending(int error) {
    return error == WSAEWOULDBLOCK || error == WSAEINPROGRESS || error == WSAEINVAL;
}
bool peer_reset(int error) {
    return error == WSAECONNRESET || error == WSAECONNABORTED || error == WSAENETRESET ||
           error == WSAENOTCONN || error == WSAESHUTDOWN;
}
void close_socket(Socket socket) { closesocket(socket); }
bool set_nonblocking(Socket socket) {
    u_long enabled = 1;
    return ioctlsocket(socket, FIONBIO, &enabled) == 0;
}
std::string error_text(int error) { return "socket error " + std::to_string(error); }
#else
using Socket = int;
constexpr Socket kInvalidSocket = -1;
int socket_error() { return errno; }
bool interrupted(int error) { return error == EINTR; }
bool would_block(int error) { return error == EAGAIN || error == EWOULDBLOCK; }
bool connect_pending(int error) { return error == EINPROGRESS || would_block(error); }
bool peer_reset(int error) {
    return error == ECONNRESET || error == ECONNABORTED || error == ENETRESET ||
           error == ENOTCONN || error == EPIPE;
}
void close_socket(Socket socket) { ::close(socket); }
bool set_nonblocking(Socket socket) {
    const int flags = fcntl(socket, F_GETFL, 0);
    return flags >= 0 && fcntl(socket, F_SETFL, flags | O_NONBLOCK) == 0;
}
std::string error_text(int error) { return std::strerror(error); }
#ifdef MSG_NOSIGNAL
constexpr int kSendFlags = MSG_NOSIGNAL;
#else
constexpr int kSendFlags = 0;
#endif
#endif

struct HostPort {
    std::string host;
    std::string port_text;
    std::uint16_t port = 0;
};

bool parse_host_port(std::string address, HostPort& result, std::string& why) {
    const auto scheme = address.find("://");
    if (scheme != std::string::npos) {
        const std::string name = address.substr(0, scheme);
        if (name != "tcp" && name != "ws") {
            why = "network endpoint scheme must be tcp or ws";
            return false;
        }
        address.erase(0, scheme + 3);
    }
    if (const auto slash = address.find('/'); slash != std::string::npos) {
        address.resize(slash);
    }
    if (address.empty()) {
        why = "TCP endpoint address is empty";
        return false;
    }

    if (address.front() == '[') {
        const auto end = address.find(']');
        if (end == std::string::npos || end + 1 >= address.size() || address[end + 1] != ':') {
            why = "bracketed TCP endpoint requires [host]:port";
            return false;
        }
        result.host = address.substr(1, end - 1);
        result.port_text = address.substr(end + 2);
    } else {
        const auto colon = address.rfind(':');
        if (colon == std::string::npos || address.find(':') != colon) {
            why = "TCP endpoint requires host:port (IPv6 addresses must be bracketed)";
            return false;
        }
        result.host = address.substr(0, colon);
        result.port_text = address.substr(colon + 1);
    }
    if (result.host.empty() || result.port_text.empty() ||
        !std::all_of(result.port_text.begin(), result.port_text.end(), [](unsigned char c) {
            return c >= '0' && c <= '9';
        })) {
        why = "TCP endpoint has an invalid host or port";
        return false;
    }
    unsigned long port = 0;
    try {
        port = std::stoul(result.port_text);
    } catch (...) {
        why = "TCP endpoint port is invalid";
        return false;
    }
    if (port == 0 || port > 65535) {
        why = "TCP endpoint port must be in 1..65535";
        return false;
    }
    result.port = static_cast<std::uint16_t>(port);
    return true;
}

int deadline_timeout_ms(device::Deadline deadline) {
    if (deadline == device::Deadline::max()) return 0;
    const auto now = std::chrono::steady_clock::now();
    if (deadline <= now) return 0;
    const auto remaining =
        std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now).count();
    return static_cast<int>(std::clamp<long long>(remaining, 1, std::numeric_limits<int>::max()));
}

bool wait_socket(Socket socket, bool writable, int timeout_ms, int& error) {
#ifdef _WIN32
    WSAPOLLFD descriptor{};
    descriptor.fd = socket;
    descriptor.events = writable ? POLLWRNORM : POLLRDNORM;
    const int ready = WSAPoll(&descriptor, 1, timeout_ms);
#else
    pollfd descriptor{};
    descriptor.fd = socket;
    descriptor.events = writable ? POLLOUT : POLLIN;
    const int ready = ::poll(&descriptor, 1, timeout_ms);
#endif
    if (ready < 0) {
        error = socket_error();
        return false;
    }
    if (ready == 0) {
        error = 0;
        return false;
    }
    error = 0;
    return true;
}

}  // namespace

struct TcpTransport::Impl {
    DeviceAllowlist& allowlist;
    std::chrono::milliseconds connect_timeout;
    device::MachineClass machine_class;
    device::TransportKind commissioning_kind;
    Socket socket = kInvalidSocket;
    bool terminal_event_pending = false;
    device::TransportEvent terminal_event;

#ifdef _WIN32
    struct WinsockLifetime {
        bool ok = false;
        WinsockLifetime() {
            WSADATA data{};
            ok = WSAStartup(MAKEWORD(2, 2), &data) == 0;
        }
        ~WinsockLifetime() {
            if (ok) WSACleanup();
        }
    } winsock;
#endif

    Impl(DeviceAllowlist& list, device::MachineClass machine,
         std::chrono::milliseconds timeout, device::TransportKind kind)
        : allowlist(list), connect_timeout(timeout), machine_class(machine),
          commissioning_kind(kind) {}

    void drop_socket() {
        if (socket != kInvalidSocket) {
            close_socket(socket);
            socket = kInvalidSocket;
        }
    }

    void terminal(device::TransportEvent::Kind kind, std::string why) {
        drop_socket();
        if (terminal_event_pending) return;
        terminal_event = {};
        terminal_event.kind = kind;
        terminal_event.why = std::move(why);
        terminal_event.raw = terminal_event.why;
        terminal_event_pending = true;
    }
};

TcpTransport::TcpTransport(DeviceAllowlist& allowlist, device::MachineClass machine_class,
                           std::chrono::milliseconds connect_timeout,
                           device::TransportKind commissioning_kind)
    : impl_(std::make_unique<Impl>(allowlist, machine_class, connect_timeout,
                                   commissioning_kind)) {}
TcpTransport::~TcpTransport() = default;
TcpTransport::TcpTransport(TcpTransport&&) noexcept = default;
TcpTransport& TcpTransport::operator=(TcpTransport&&) noexcept = default;

device::OpenResult TcpTransport::open(const device::Endpoint& endpoint) {
    impl_->drop_socket();
    impl_->terminal_event_pending = false;
    if (impl_->connect_timeout.count() <= 0)
        return {false, "TCP connect timeout must be positive"};

    HostPort endpoint_address;
    std::string why;
    if (!parse_host_port(endpoint.address, endpoint_address, why)) return {false, std::move(why)};
    CommissioningCandidate candidate;
    candidate.endpoint = EndpointIdentity::network(
        impl_->commissioning_kind, endpoint_address.host, endpoint_address.port);
    candidate.machine_class = impl_->machine_class;
    if (impl_->commissioning_kind == device::TransportKind::WebSocket) {
        const auto scheme = endpoint.address.find("://");
        const auto authority_begin = scheme == std::string::npos ? 0 : scheme + 3;
        const auto slash = endpoint.address.find('/', authority_begin);
        candidate.connection["path"] =
            slash == std::string::npos ? "/" : endpoint.address.substr(slash);
    }
    const Authorization authorization = impl_->allowlist.authorize(candidate);
    if (!authorization.allowed)
        return {false, authorization.why.empty() ? "endpoint is not commissioned" : authorization.why};
#ifdef _WIN32
    if (!impl_->winsock.ok) return {false, "Winsock initialization failed"};
#endif

    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    addrinfo* addresses = nullptr;
    const int lookup = getaddrinfo(endpoint_address.host.c_str(), endpoint_address.port_text.c_str(),
                                   &hints, &addresses);
    if (lookup != 0)
        return {false, std::string("TCP endpoint resolution failed: ") + gai_strerror(lookup)};

    std::string last_error = "no compatible address";
    const auto deadline = std::chrono::steady_clock::now() + impl_->connect_timeout;
    for (addrinfo* candidate = addresses; candidate != nullptr; candidate = candidate->ai_next) {
        Socket socket = ::socket(candidate->ai_family, candidate->ai_socktype, candidate->ai_protocol);
        if (socket == kInvalidSocket) {
            last_error = error_text(socket_error());
            continue;
        }
        if (!set_nonblocking(socket)) {
            last_error = "could not make TCP socket non-blocking: " + error_text(socket_error());
            close_socket(socket);
            continue;
        }
#ifndef _WIN32
#ifdef SO_NOSIGPIPE
        int one = 1;
        setsockopt(socket, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
#endif
        int connected = ::connect(socket, candidate->ai_addr,
                                  static_cast<decltype(candidate->ai_addrlen)>(candidate->ai_addrlen));
        if (connected != 0) {
            const int connect_error = socket_error();
            if (!connect_pending(connect_error)) {
                last_error = error_text(connect_error);
                close_socket(socket);
                continue;
            }
            int wait_error = 0;
            const int wait_ms = deadline_timeout_ms(deadline);
            if (!wait_socket(socket, true, wait_ms, wait_error)) {
                last_error = wait_error == 0 ? "connect timed out" : error_text(wait_error);
                close_socket(socket);
                continue;
            }
            int completion_error = 0;
#ifdef _WIN32
            int completion_size = sizeof(completion_error);
#else
            socklen_t completion_size = sizeof(completion_error);
#endif
            if (getsockopt(socket, SOL_SOCKET, SO_ERROR,
                           reinterpret_cast<char*>(&completion_error), &completion_size) != 0 ||
                completion_error != 0) {
                last_error = completion_error == 0 ? error_text(socket_error())
                                                    : error_text(completion_error);
                close_socket(socket);
                continue;
            }
        }

        int enabled = 1;
        if (setsockopt(socket, SOL_SOCKET, SO_KEEPALIVE, reinterpret_cast<const char*>(&enabled),
                       sizeof(enabled)) != 0) {
            last_error = "could not enable TCP keepalive: " + error_text(socket_error());
            close_socket(socket);
            continue;
        }
#ifndef _WIN32
#ifdef TCP_KEEPIDLE
        int idle = 30;
        setsockopt(socket, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
#endif
#ifdef TCP_KEEPINTVL
        int interval = 10;
        setsockopt(socket, IPPROTO_TCP, TCP_KEEPINTVL, &interval, sizeof(interval));
#endif
#ifdef TCP_KEEPCNT
        int count = 3;
        setsockopt(socket, IPPROTO_TCP, TCP_KEEPCNT, &count, sizeof(count));
#endif
#endif
        impl_->socket = socket;
        break;
    }
    freeaddrinfo(addresses);
    if (impl_->socket == kInvalidSocket) return {false, "TCP connect failed: " + last_error};
    return {true, {}};
}

void TcpTransport::close() {
    if (!impl_) return;
    impl_->drop_socket();
    impl_->terminal_event_pending = false;
}

device::WriteResult TcpTransport::write(std::span<const std::byte> bytes) {
    if (impl_->socket == kInvalidSocket) return {false, 0, "device_lost: TCP transport is closed"};
    std::size_t written = 0;
    while (written < bytes.size()) {
        const auto remaining = bytes.size() - written;
#ifdef _WIN32
        const int chunk = static_cast<int>(std::min<std::size_t>(remaining, INT_MAX));
        const int count = ::send(impl_->socket,
                                 reinterpret_cast<const char*>(bytes.data() + written), chunk, 0);
#else
        const ssize_t count = ::send(impl_->socket, bytes.data() + written, remaining, kSendFlags);
#endif
        if (count > 0) {
            written += static_cast<std::size_t>(count);
            continue;
        }
        if (count == 0) {
            impl_->terminal(device::TransportEvent::Kind::Closed,
                            "device_lost: peer half-closed TCP connection");
            return {false, written, impl_->terminal_event.why};
        }
        const int error = socket_error();
        if (interrupted(error)) continue;
        if (would_block(error)) return {true, written, {}};
        const std::string why = peer_reset(error) ? "device_lost: TCP peer reset"
                                                  : "device_lost: TCP write failed: " + error_text(error);
        impl_->terminal(device::TransportEvent::Kind::Error, why);
        return {false, written, why};
    }
    return {true, written, {}};
}

device::PollResult TcpTransport::poll(device::Deadline deadline) {
    device::PollResult result;
    if (impl_->terminal_event_pending) {
        result.events.push_back(std::move(impl_->terminal_event));
        impl_->terminal_event_pending = false;
        return result;
    }
    if (impl_->socket == kInvalidSocket) return result;

#ifdef _WIN32
    WSAPOLLFD descriptor{};
    descriptor.fd = impl_->socket;
    descriptor.events = POLLRDNORM;
    const int ready = WSAPoll(&descriptor, 1, deadline_timeout_ms(deadline));
#else
    pollfd descriptor{};
    descriptor.fd = impl_->socket;
    descriptor.events = POLLIN | POLLERR | POLLHUP;
#ifdef POLLRDHUP
    descriptor.events |= POLLRDHUP;
#endif
    const int ready = ::poll(&descriptor, 1, deadline_timeout_ms(deadline));
#endif
    if (ready < 0) {
        const int error = socket_error();
        if (interrupted(error)) return result;
        impl_->terminal(device::TransportEvent::Kind::Error,
                        "device_lost: TCP poll failed: " + error_text(error));
    } else if (ready > 0) {
        bool saw_eof = false;
        for (;;) {
            std::array<std::byte, 16 * 1024> buffer{};
#ifdef _WIN32
            const int count = ::recv(impl_->socket, reinterpret_cast<char*>(buffer.data()),
                                     static_cast<int>(buffer.size()), 0);
#else
            const ssize_t count = ::recv(impl_->socket, buffer.data(), buffer.size(), 0);
#endif
            if (count > 0) {
                device::TransportEvent event;
                event.kind = device::TransportEvent::Kind::Data;
                event.data.assign(buffer.begin(), buffer.begin() + count);
                event.raw.assign(reinterpret_cast<const char*>(event.data.data()), event.data.size());
                result.events.push_back(std::move(event));
                continue;
            }
            if (count == 0) {
                saw_eof = true;
                break;
            }
            const int error = socket_error();
            if (interrupted(error)) continue;
            if (would_block(error)) break;
            impl_->terminal(device::TransportEvent::Kind::Error,
                            peer_reset(error) ? "device_lost: TCP peer reset"
                                              : "device_lost: TCP read failed: " + error_text(error));
            break;
        }
        if (saw_eof)
            impl_->terminal(device::TransportEvent::Kind::Closed,
                            "device_lost: peer half-closed TCP connection");
#ifndef _WIN32
        if (impl_->socket != kInvalidSocket &&
            (descriptor.revents & (POLLERR | POLLNVAL)) != 0) {
            int error = 0;
            socklen_t size = sizeof(error);
            getsockopt(impl_->socket, SOL_SOCKET, SO_ERROR, &error, &size);
            impl_->terminal(device::TransportEvent::Kind::Error,
                            peer_reset(error) ? "device_lost: TCP peer reset"
                                              : "device_lost: TCP socket error: " + error_text(error));
        }
        if (impl_->socket != kInvalidSocket && (descriptor.revents & POLLHUP) != 0)
            impl_->terminal(device::TransportEvent::Kind::Closed,
                            "device_lost: peer half-closed TCP connection");
#endif
    }
    if (impl_->terminal_event_pending) {
        result.events.push_back(std::move(impl_->terminal_event));
        impl_->terminal_event_pending = false;
    }
    return result;
}

}  // namespace chudware::mfg::gateway
