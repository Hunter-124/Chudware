// Native Windows transport regressions: real pipes, no engine or provider runtime.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include "../../services/ipc/ipc_transport.h"
#include <chrono>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

using namespace chudware_ipc;
using namespace std::chrono_literals;
namespace {
void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
struct Pair {
    IpcListener listener;
    IpcConn server, client;
    HANDLE clientHandle = INVALID_HANDLE_VALUE; // borrowed from client
    Pair() {
        static unsigned serial = 0;
        const auto name = "\\\\.\\pipe\\chudware-transport-test-" +
            std::to_string(GetCurrentProcessId()) + "-" + std::to_string(++serial);
        std::string error;
        require(listener.listen(name, error), error.c_str());
        const std::wstring wide(name.begin(), name.end());
        clientHandle = CreateFileW(wide.c_str(), GENERIC_READ | GENERIC_WRITE, 0,
            nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED |
            SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, nullptr);
        require(clientHandle != INVALID_HANDLE_VALUE, "client pipe open failed");
        client = IpcConn(clientHandle);
        server = listener.accept(2000);
        require(client.valid() && server.valid(), "native connection failed");
    }
    void fragment(const std::string& bytes) {
        OVERLAPPED operation{};
        operation.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        require(operation.hEvent != nullptr, "fragment event creation failed");
        const BOOL submitted = WriteFile(clientHandle, bytes.data(),
            static_cast<DWORD>(bytes.size()), nullptr, &operation);
        const bool pending = !submitted && GetLastError() == ERROR_IO_PENDING;
        DWORD count = 0;
        const bool complete = (submitted || pending) &&
            GetOverlappedResult(clientHandle, &operation, &count, TRUE);
        CloseHandle(operation.hEvent);
        require(complete && count == bytes.size(), "fragment write failed");
    }
};
void shutdownIdle(bool beforeRead) {
    Pair pipe;
    if (beforeRead) pipe.server.shutdown();
    auto result = std::async(std::launch::async, [&] {
        std::string line;
        return pipe.server.readLine(line, -1);
    });
    if (!beforeRead) {
        std::this_thread::sleep_for(20ms);
        pipe.server.shutdown();
    }
    const bool woke = result.wait_for(2s) == std::future_status::ready;
    if (!woke) pipe.client.close(); // fail cleanly even with the old polling reader
    const bool read = result.get();
    require(woke && !read && pipe.server.eof(),
        beforeRead ? "shutdown before read was lost" : "shutdown did not wake idle reader");
}
void framingAndTimeout() {
    Pair pipe;
    std::string line;
    require(!pipe.server.readLine(line, 0) && !pipe.server.eof(), "empty poll is not EOF");
    pipe.fragment("partial");
    require(!pipe.server.readLine(line, 20) && !pipe.server.eof(), "partial frame did not time out");
    pipe.fragment("-tail\nsecond\n\n");
    require(pipe.server.readLine(line, 0) && line == "partial-tail", "timeout lost partial bytes");
    require(pipe.server.readLine(line, 0) && line == "second", "buffered second frame lost");
    require(pipe.server.readLine(line, 0) && line.empty(), "empty frame lost");
    pipe.fragment("final\n");
    pipe.client.close();
    require(pipe.server.readLine(line, 2000) && line == "final", "EOF discarded final frame");
    require(!pipe.server.readLine(line, 2000) && pipe.server.eof(), "peer close not reported");
}
void cancelBothDirections() {
    Pair pipe;
    const std::string payload(4 * 1024 * 1024, 'x');
    auto reader = std::async(std::launch::async, [&] {
        std::string line;
        return pipe.server.readLine(line, -1);
    });
    auto writer = std::async(std::launch::async, [&] { return pipe.server.writeLine(payload); });
    std::this_thread::sleep_for(20ms);
    pipe.server.shutdown();
    const bool readReady = reader.wait_for(2s) == std::future_status::ready;
    const bool writeReady = writer.wait_for(2s) == std::future_status::ready;
    if (!readReady || !writeReady) pipe.client.close();
    const bool read = reader.get(), written = writer.get();
    require(readReady && writeReady && !read && !written,
            "shutdown failed to cancel simultaneous blocked read/write");
}
void duplex() {
    Pair pipe;
    const std::string payload(4 * 1024 * 1024, 'd');
    auto request = std::async(std::launch::async, [&] {
        std::string line;
        return pipe.server.readLine(line, 5000) && line == "request";
    });
    auto response = std::async(std::launch::async, [&] { return pipe.server.writeLine(payload); });
    auto received = std::async(std::launch::async, [&] {
        std::string line;
        return pipe.client.readLine(line, 5000) && line == payload;
    });
    const bool sent = pipe.client.writeLine("request");
    const bool ready = request.wait_for(6s) == std::future_status::ready &&
        response.wait_for(6s) == std::future_status::ready &&
        received.wait_for(6s) == std::future_status::ready;
    if (!ready) { pipe.server.shutdown(); pipe.client.shutdown(); }
    const bool requestOk = request.get(), responseOk = response.get(), receivedOk = received.get();
    require(sent && ready && requestOk && responseOk && receivedOk,
            "concurrent read/write completion events interfered");
}
void lineLimit(bool oversized) {
    Pair pipe;
    const std::string payload(64u * 1024u * 1024u + (oversized ? 1u : 0u), 'l');
    auto writer = std::async(std::launch::async, [&] { return pipe.client.writeLine(payload); });
    std::string line;
    const bool read = pipe.server.readLine(line, 30000);
    const bool closed = pipe.server.eof();
    pipe.server.close();
    const bool ready = writer.wait_for(2s) == std::future_status::ready;
    if (!ready) pipe.client.shutdown();
    const bool written = writer.get();
    require(ready, "line-limit writer did not drain");
    if (oversized) require(!read && closed, "oversized frame was not rejected closed");
    else require(read && written && line == payload, "64 MiB boundary frame was rejected or corrupted");
}
}
int main() {
    try {
        shutdownIdle(false);
        shutdownIdle(true);
        framingAndTimeout();
        cancelBothDirections();
        duplex();
        lineLimit(false);
        lineLimit(true);
        std::cout << "PASS native Windows transport cancellation/duplex/framing/64MiB limit\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
