// Chudware Code ACP transport.
//
// Owns the stdio NDJSON/JSON-RPC connection to `chudware-code acp`. This layer
// knows ACP wire JSON and NOTHING about ImGui, AppState, ChatMsg, or CAD. The
// reader thread assigns arrival order by pushing complete frames into one FIFO;
// the GL thread drains that FIFO and performs every state mutation.
#pragma once

#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui::code {

using AcpRequestId = std::uint64_t;

enum class AcpFrameKind { Response, Request, Notification };

struct AcpFrame {
    AcpFrameKind kind = AcpFrameKind::Notification;
    AcpRequestId id = 0;
    std::string method;
    nlohmann::json payload;
    nlohmann::json error;
};

class AcpClient {
public:
    AcpClient();
    ~AcpClient();
    AcpClient(const AcpClient&) = delete;
    AcpClient& operator=(const AcpClient&) = delete;

    // Spawn `executable acp` in cwd. stdout is protocol-only; child stderr is
    // inherited so diagnostics never contaminate JSON-RPC framing.
    bool start(const std::string& executable, const std::string& cwd, std::string& err);
    void stop();
    bool running() const;

    // Non-blocking request. The response arrives later through drain().
    bool request(const std::string& method, const nlohmann::json& params,
                 AcpRequestId& id, std::string& err);
    bool notify(const std::string& method, const nlohmann::json& params,
                std::string& err);
    bool respond(AcpRequestId id, const nlohmann::json& result,
                 std::string& err);
    bool respondError(AcpRequestId id, int code, const std::string& message,
                      std::string& err);

    // GL-thread only. Preserves the exact line arrival order from the reader.
    std::vector<AcpFrame> drain();

private:
    bool writeFrame(const nlohmann::json& frame, std::string& err);
    void readerMain();
    void queueTransportError(const std::string& message);

#ifdef _WIN32
    void* childStdinWr_ = nullptr;
    void* childStdoutRd_ = nullptr;
    void* process_ = nullptr;
#else
    int childStdinWr_ = -1;
    int childStdoutRd_ = -1;
    int process_ = -1;
#endif
    mutable std::mutex stateMtx_;
    std::mutex writeMtx_;
    std::mutex queueMtx_;
    std::vector<AcpFrame> queue_;
    std::thread reader_;
    AcpRequestId nextId_ = 1;
    bool running_ = false;
    bool stopping_ = false;
};

} // namespace ui::code
