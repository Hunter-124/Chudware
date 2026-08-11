#pragma once

#include <algorithm>
#include <climits>
#include <cstddef>
#include <cstdint>
#include <stdexcept>

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <process.h>
#else
#include <arpa/inet.h>
#include <cerrno>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace chudware::test::net {

#if defined(_WIN32)
using Socket = SOCKET;
using AddressLength = int;
inline constexpr Socket kInvalidSocket = INVALID_SOCKET;

class SocketRuntime {
public:
    SocketRuntime() {
        WSADATA data{};
        if (::WSAStartup(MAKEWORD(2, 2), &data) != 0)
            throw std::runtime_error("Winsock initialization failed");
        initialized_ = true;
    }
    ~SocketRuntime() {
        if (initialized_) ::WSACleanup();
    }
    SocketRuntime(const SocketRuntime&) = delete;
    SocketRuntime& operator=(const SocketRuntime&) = delete;

private:
    bool initialized_ = false;
};

inline bool socketValid(Socket socket) { return socket != kInvalidSocket; }
inline void closeSocket(Socket socket) { ::closesocket(socket); }
inline int lastSocketError() { return ::WSAGetLastError(); }
inline bool socketInterrupted(int error) { return error == WSAEINTR; }
inline long long processId() { return static_cast<long long>(::_getpid()); }
inline int shutdownSend(Socket socket) { return ::shutdown(socket, SD_SEND); }
inline bool suppressBrokenPipe(Socket) { return true; }

inline std::ptrdiff_t sendSocket(Socket socket, const void* data, std::size_t size) {
    const int chunk = static_cast<int>(std::min<std::size_t>(size, INT_MAX));
    return ::send(socket, static_cast<const char*>(data), chunk, 0);
}

inline std::ptrdiff_t receiveSocket(Socket socket, void* data, std::size_t size) {
    const int chunk = static_cast<int>(std::min<std::size_t>(size, INT_MAX));
    return ::recv(socket, static_cast<char*>(data), chunk, 0);
}

inline bool setReuseAddress(Socket socket) {
    const int enabled = 1;
    return ::setsockopt(socket, SOL_SOCKET, SO_REUSEADDR,
                        reinterpret_cast<const char*>(&enabled), sizeof(enabled)) == 0;
}

inline bool setAbortiveClose(Socket socket) {
    linger reset{1, 0};
    return ::setsockopt(socket, SOL_SOCKET, SO_LINGER,
                        reinterpret_cast<const char*>(&reset), sizeof(reset)) == 0;
}

#else
using Socket = int;
using AddressLength = socklen_t;
inline constexpr Socket kInvalidSocket = -1;

class SocketRuntime {
public:
    SocketRuntime() = default;
    SocketRuntime(const SocketRuntime&) = delete;
    SocketRuntime& operator=(const SocketRuntime&) = delete;
};

inline bool socketValid(Socket socket) { return socket != kInvalidSocket; }
inline void closeSocket(Socket socket) { ::close(socket); }
inline int lastSocketError() { return errno; }
inline bool socketInterrupted(int error) { return error == EINTR; }
inline long long processId() { return static_cast<long long>(::getpid()); }
inline int shutdownSend(Socket socket) { return ::shutdown(socket, SHUT_WR); }

inline bool suppressBrokenPipe(Socket socket) {
#if defined(SO_NOSIGPIPE)
    const int enabled = 1;
    return ::setsockopt(socket, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled)) == 0;
#else
    (void)socket;
    return true;
#endif
}

inline std::ptrdiff_t sendSocket(Socket socket, const void* data, std::size_t size) {
#if defined(MSG_NOSIGNAL)
    constexpr int flags = MSG_NOSIGNAL;
#else
    constexpr int flags = 0;
#endif
    return ::send(socket, data, size, flags);
}

inline std::ptrdiff_t receiveSocket(Socket socket, void* data, std::size_t size) {
    return ::recv(socket, data, size, 0);
}

inline bool setReuseAddress(Socket socket) {
    const int enabled = 1;
    return ::setsockopt(socket, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled)) == 0;
}

inline bool setAbortiveClose(Socket socket) {
    linger reset{1, 0};
    return ::setsockopt(socket, SOL_SOCKET, SO_LINGER, &reset, sizeof(reset)) == 0;
}
#endif

inline Socket openTcpSocket() { return ::socket(AF_INET, SOCK_STREAM, 0); }
inline int bindSocket(Socket socket, const sockaddr* address, AddressLength size) {
    return ::bind(socket, address, size);
}
inline int listenSocket(Socket socket, int backlog) { return ::listen(socket, backlog); }
inline int getSocketName(Socket socket, sockaddr* address, AddressLength* size) {
    return ::getsockname(socket, address, size);
}
inline Socket acceptSocket(Socket socket) { return ::accept(socket, nullptr, nullptr); }

}  // namespace chudware::test::net
