#include "ui/code/code_workspace.h"

#include "core/app_state.h"
#include "ui/clipboard_image.h"
#include "ui/code/code_acp.h"
#include "ui/code/code_mapping.h"

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace ui::code {
namespace {


std::atomic_bool g_requested{false};


std::string jsonString(const nlohmann::json& value, const char* key) {
    const auto it = value.find(key);
    return it != value.end() && it->is_string() ? it->get<std::string>() : std::string();
}

std::string configOptionLabel(const nlohmann::json& option,
                              const std::string& current) {
    const auto choices = option.find("options");
    if (choices != option.end() && choices->is_array()) {
        for (const auto& choice : *choices) {
            if (!choice.is_object() || jsonString(choice, "value") != current)
                continue;
            const std::string name = jsonString(choice, "name");
            if (!name.empty()) return name;
        }
    }
    return current;
}

void captureSessionConfiguration(const nlohmann::json& payload,
                                 SessionConfiguration& configuration) {
    if (!payload.is_object()) return;
    const auto options = payload.find("configOptions");
    if (options != payload.end() && options->is_array()) {
        for (const auto& option : *options) {
            if (!option.is_object()) continue;
            const std::string id = jsonString(option, "id");
            const std::string current = jsonString(option, "currentValue");
            const std::string label = configOptionLabel(option, current);
            if (id == "mode") configuration.mode = label;
            else if (id == "model") configuration.model = label;
            else if (id == "thinking") configuration.thinking = label;
        }
    }
    const auto modes = payload.find("modes");
    if (modes != payload.end() && modes->is_object()) {
        const std::string current = jsonString(*modes, "currentModeId");
        if (!current.empty()) configuration.mode = current;
    }
    if (jsonString(payload, "sessionUpdate") == "current_mode_update") {
        const std::string current = jsonString(payload, "currentModeId");
        if (!current.empty()) configuration.mode = current;
    }
}

std::string responseError(const nlohmann::json& error) {
    if (error.is_string()) return error.get<std::string>();
    if (error.is_object()) {
        std::string message = jsonString(error, "message");
        const auto data = error.find("data");
        if (data != error.end() && !data->is_null()) {
            const std::string detail = data->is_string() ? data->get<std::string>() : data->dump();
            if (!detail.empty() && detail != message) {
                if (!message.empty()) message += ": ";
                message += detail;
            }
        }
        if (!message.empty()) return message;
    }
    return error.is_null() ? std::string("ACP request failed") : error.dump();
}


std::string base64Encode(std::string_view bytes) {
    static constexpr char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve(((bytes.size() + 2) / 3) * 4);
    size_t i = 0;
    while (i + 3 <= bytes.size()) {
        const uint32_t v = (static_cast<uint32_t>(static_cast<unsigned char>(bytes[i])) << 16) |
                           (static_cast<uint32_t>(static_cast<unsigned char>(bytes[i + 1])) << 8) |
                           static_cast<uint32_t>(static_cast<unsigned char>(bytes[i + 2]));
        out.push_back(alphabet[(v >> 18) & 63]);
        out.push_back(alphabet[(v >> 12) & 63]);
        out.push_back(alphabet[(v >> 6) & 63]);
        out.push_back(alphabet[v & 63]);
        i += 3;
    }
    if (i < bytes.size()) {
        uint32_t v = static_cast<uint32_t>(static_cast<unsigned char>(bytes[i])) << 16;
        const bool two = i + 1 < bytes.size();
        if (two) v |= static_cast<uint32_t>(static_cast<unsigned char>(bytes[i + 1])) << 8;
        out.push_back(alphabet[(v >> 18) & 63]);
        out.push_back(alphabet[(v >> 12) & 63]);
        out.push_back(two ? alphabet[(v >> 6) & 63] : '=');
        out.push_back('=');
    }
    return out;
}

bool readImageBlock(const std::string& path, nlohmann::json& block, std::string& err) {
    const std::string mime = ui::imageMimeForPath(path);
    if (mime.empty()) {
        err = "Chudcode accepts image attachments only; remove the non-image file and retry.";
        return false;
    }
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        err = "Could not read attached image: " + path;
        return false;
    }
    input.seekg(0, std::ios::end);
    const std::streamoff length = input.tellg();
    if (length < 0) {
        err = "Could not determine attached image size: " + path;
        return false;
    }
    input.seekg(0, std::ios::beg);
    std::string bytes(static_cast<size_t>(length), '\0');
    if (length > 0 && !input.read(bytes.data(), length)) {
        err = "Could not read attached image: " + path;
        return false;
    }
    block = {{"type", "image"}, {"data", base64Encode(bytes)}, {"mimeType", mime}};
    return true;
}

std::string displayAttachmentName(const std::string& path) {
    const std::string name = std::filesystem::path(path).filename().string();
    return name.empty() ? path : name;
}

enum class PendingOp { Initialize, Authenticate, List, Load, Resume, NewSession, Fork, Prompt, Close };

struct PendingRequest {
    PendingOp op = PendingOp::Initialize;
    std::string sessionId;
    size_t placeholder = mapping::kNoMessage;
    bool cancelRequested = false;
};

class Controller {
public:
    bool start(AppState& state, const StartupOptions& startup, std::string& err) {
        shutdown(state);
        options_ = startup;
        try {
            if (options_.cwd.empty()) options_.cwd = std::filesystem::current_path().string();
            options_.cwd = std::filesystem::absolute(options_.cwd).lexically_normal().string();
        } catch (const std::exception& ex) {
            err = std::string("Could not resolve the Chudcode workspace: ") + ex.what();
            setFailureStatus(state, err);
            return false;
        }
        if (!client_.start(options_.executable, options_.cwd, err)) {
            setFailureStatus(state, err.empty() ? "Could not start Chudcode ACP." : err);
            return false;
        }
        phase_ = Phase::Starting;
        nlohmann::json params = {
            {"protocolVersion", 1},
            {"clientCapabilities", nlohmann::json::object()},
            {"clientInfo", {{"name", "chudcode-cad"}, {"title", "Chudcode"}, {"version", "0.1"}}},
        };
        if (!request("initialize", params, {PendingOp::Initialize}, err)) {
            setFailureStatus(state, err);
            client_.stop();
            return false;
        }
        state.status = "Starting Chudcode...";
        state.statusKind = 0;
        return true;
    }

    void tick(AppState& state) {
        for (const AcpFrame& frame : client_.drain()) {
            if (frame.kind == AcpFrameKind::Response) {
                handleResponse(state, frame);
            } else if (frame.kind == AcpFrameKind::Request) {
                handleRequest(state, frame);
            } else {
                handleNotification(state, frame);
            }
        }
    }

    void shutdown(AppState&) {
        pending_.clear();
        mappings_.clear();
        loadedSessions_.clear();
        listedSessions_.clear();
        sessionCwds_.clear();
        activeSessionId_.clear();
        capabilities_ = {};
        configuration_ = {};
        phase_ = Phase::Stopped;
        client_.stop();
    }

    bool connected() const { return phase_ == Phase::Ready && client_.running(); }
    const Capabilities& capabilities() const { return capabilities_; }
    const SessionConfiguration& configuration() const { return configuration_; }

    bool submit(AppState& state, const std::string& text, std::string& err) {
        if (!connected()) {
            err = "Chudcode is not connected yet.";
            return false;
        }
        if (activeSessionId_.empty()) {
            err = "No Chudcode session is selected.";
            return false;
        }
        if (!state.pendingContextRefs.empty()) {
            err = "CAD geometry context is not available in Chudcode v0.1; remove the context chips and retry.";
            return false;
        }
        if (text.empty() && state.pendingAttachments.empty()) {
            err = "Enter a message or attach an image.";
            return false;
        }
        if (!state.pendingAttachments.empty() && !capabilities_.images) {
            err = "This coding agent did not advertise ACP image support.";
            return false;
        }

        nlohmann::json prompt = nlohmann::json::array();
        if (!text.empty()) prompt.push_back({{"type", "text"}, {"text", text}});
        for (const std::string& path : state.pendingAttachments) {
            nlohmann::json image;
            if (!readImageBlock(path, image, err)) return false;
            prompt.push_back(std::move(image));
        }

        Conversation* conversation = findConversation(state, activeSessionId_);
        std::vector<ChatMsg>* messages = conversationMsgs(state, activeSessionId_);
        if (!conversation || !messages) {
            err = "The selected Chudcode session is no longer resident.";
            return false;
        }
        mapping::State& runtime = mappings_[activeSessionId_];
        if (runtime.pendingAssistant < messages->size() && (*messages)[runtime.pendingAssistant].running) {
            err = "Wait for the current Chudcode turn to finish or stop it first.";
            return false;
        }

        ChatMsg user;
        user.fromUser = true;
        user.text = text;
        user.copilotMode = state.aiInteraction;
        for (const std::string& path : state.pendingAttachments)
            user.text += "\n[attached image: " + displayAttachmentName(path) + "]";
        messages->push_back(std::move(user));

        ChatMsg assistant;
        assistant.fromUser = false;
        assistant.running = true;
        assistant.copilotMode = state.aiInteraction;
        assistant.liveStatus = "Chudcode is working...";
        messages->push_back(std::move(assistant));
        const size_t placeholder = messages->size() - 1;
        mapping::bindPendingAssistant(runtime, placeholder);

        AcpRequestId id = 0;
        if (!client_.request("session/prompt", {{"sessionId", activeSessionId_}, {"prompt", prompt}}, id, err)) {
            mapping::markPromptFailure(*messages, runtime, placeholder,
                                       err.empty() ? "Could not send the Chudcode prompt." : err);
            setFailureStatus(state, err);
            return false;
        }
        pending_.emplace(id, PendingRequest{PendingOp::Prompt, activeSessionId_, placeholder, false});
        state.pendingAttachments.clear();
        state.status = "Chudcode is working...";
        state.statusKind = 0;
        return true;
    }

    bool cancel(AppState& state, std::string& err) {
        if (!connected() || activeSessionId_.empty()) {
            err = "No active Chudcode session to stop.";
            return false;
        }
        mapping::State& runtime = mappings_[activeSessionId_];
        std::vector<ChatMsg>* messages = conversationMsgs(state, activeSessionId_);
        if (!messages || runtime.pendingAssistant >= messages->size() ||
            !(*messages)[runtime.pendingAssistant].running) {
            err = "The selected Chudcode session has no running turn.";
            return false;
        }
        if (!client_.notify("session/cancel", {{"sessionId", activeSessionId_}}, err)) return false;
        (*messages)[runtime.pendingAssistant].liveStatus = "Stopping...";
        for (auto& entry : pending_) {
            PendingRequest& pending = entry.second;
            if (pending.op == PendingOp::Prompt && pending.sessionId == activeSessionId_)
                pending.cancelRequested = true;
        }
        state.status = "Stopping Chudcode...";
        state.statusKind = 0;
        return true;
    }

    bool selectSession(AppState& state, const std::string& sessionId, std::string& err) {
        if (!connected()) {
            err = "Chudcode is not connected yet.";
            return false;
        }
        if (resumeSupported_ && loadedSessions_.count(sessionId) != 0) {
            const int index = findConversationIndex(state, sessionId);
            if (index < 0) {
                err = "Unknown Chudcode session: " + sessionId;
                return false;
            }
            activateConversation(state, index);
            const std::string cwd = sessionCwds_.count(sessionId)
                                        ? sessionCwds_[sessionId] : options_.cwd;
            if (!request("session/resume",
                         {{"sessionId", sessionId}, {"cwd", cwd},
                          {"mcpServers", nlohmann::json::array()}},
                         {PendingOp::Resume, sessionId}, err))
                return false;
            state.status = "Resuming Chudcode session...";
            state.statusKind = 0;
            return true;
        }
        if (!capabilities_.load) {
            err = "This coding agent does not support loading sessions.";
            return false;
        }
        return requestLoad(state, sessionId, PendingOp::Load, "session/load", err);
    }

    bool newSession(AppState&, std::string& err) {
        if (!connected()) {
            err = "Chudcode is not connected yet.";
            return false;
        }
        return request("session/new", {{"cwd", options_.cwd}, {"mcpServers", nlohmann::json::array()}},
                       {PendingOp::NewSession}, err);
    }

    bool forkSession(AppState&, const std::string& sessionId, std::string& err) {
        if (!connected()) {
            err = "Chudcode is not connected yet.";
            return false;
        }
        if (!capabilities_.fork) {
            err = "This coding agent does not support forking sessions.";
            return false;
        }
        const std::string cwd = sessionCwds_.count(sessionId) ? sessionCwds_[sessionId] : options_.cwd;
        return request("session/fork",
                       {{"sessionId", sessionId}, {"cwd", cwd}, {"mcpServers", nlohmann::json::array()}},
                       {PendingOp::Fork, sessionId}, err);
    }

private:
    enum class Phase { Stopped, Starting, Ready, Failed };

    bool request(const std::string& method, const nlohmann::json& params,
                 PendingRequest pending, std::string& err) {
        AcpRequestId id = 0;
        if (!client_.request(method, params, id, err)) return false;
        pending_.emplace(id, std::move(pending));
        return true;
    }

    Conversation* findConversation(AppState& state, const std::string& id) {
        for (auto& conversation : state.conversations)
            if (conversation.id == id) return &conversation;
        return nullptr;
    }

    int findConversationIndex(const AppState& state, const std::string& id) const {
        for (size_t i = 0; i < state.conversations.size(); ++i)
            if (state.conversations[i].id == id) return static_cast<int>(i);
        return -1;
    }

    void activateConversation(AppState& state, int index) {
        if (index < 0 || index >= static_cast<int>(state.conversations.size())) return;
        if (state.activeConv == index) return;
        if (state.activeConv >= 0 && state.activeConv < static_cast<int>(state.conversations.size())) {
            Conversation& old = state.conversations[state.activeConv];
            old.msgs = std::move(state.chat);
            old.lastContextTokens = state.lastContextTokens;
            old.queuedPrompts = std::move(state.queuedPrompts);
        }
        state.activeConv = index;
        Conversation& current = state.conversations[index];
        state.chat = std::move(current.msgs);
        state.lastContextTokens = current.lastContextTokens;
        state.queuedPrompts = std::move(current.queuedPrompts);
        current.msgs.clear();
        current.queuedPrompts.clear();
        activeSessionId_ = current.id;
    }

    bool requestLoad(AppState& state, const std::string& sessionId, PendingOp op,
                     const std::string& method, std::string& err) {
        const int index = findConversationIndex(state, sessionId);
        if (index < 0) {
            err = "Unknown Chudcode session: " + sessionId;
            return false;
        }
        activateConversation(state, index);
        std::vector<ChatMsg>* messages = conversationMsgs(state, sessionId);
        if (!messages) {
            err = "Could not activate Chudcode session: " + sessionId;
            return false;
        }
        freeChatTextures(*messages);
        messages->clear();
        mapping::State& runtime = mappings_[sessionId];
        mapping::reset(runtime);
        mapping::setHistoryReplay(runtime, true);
        const std::string cwd = sessionCwds_.count(sessionId) ? sessionCwds_[sessionId] : options_.cwd;
        if (!request(method,
                     {{"sessionId", sessionId}, {"cwd", cwd}, {"mcpServers", nlohmann::json::array()}},
                     {op, sessionId}, err)) {
            mapping::setHistoryReplay(runtime, false);
            return false;
        }
        state.status = "Loading Chudcode session...";
        state.statusKind = 0;
        return true;
    }

    void captureCapabilities(const nlohmann::json& result) {
        capabilities_ = {};
        const auto capsIt = result.find("agentCapabilities");
        if (capsIt == result.end() || !capsIt->is_object()) return;
        const nlohmann::json& caps = *capsIt;
        capabilities_.load = caps.value("loadSession", false);
        const auto prompt = caps.find("promptCapabilities");
        if (prompt != caps.end() && prompt->is_object()) {
            capabilities_.images = prompt->value("image", false);
            capabilities_.embeddedContext = prompt->value("embeddedContext", false);
        }
        const auto sessions = caps.find("sessionCapabilities");
        if (sessions != caps.end() && sessions->is_object()) {
            capabilities_.list = sessions->contains("list");
            capabilities_.fork = sessions->contains("fork");
            resumeSupported_ = sessions->contains("resume");
            capabilities_.close = sessions->contains("close");
        }
    }

    void requestListOrNew(AppState& state) {
        std::string err;
        if (capabilities_.list) {
            listedSessions_.clear();
            if (!request("session/list", {{"cwd", options_.cwd}}, {PendingOp::List}, err))
                setFailureStatus(state, err);
        } else {
            listedSessions_.clear();
            replaceSessionList(state);
            if (!request("session/new",
                         {{"cwd", options_.cwd}, {"mcpServers", nlohmann::json::array()}},
                         {PendingOp::NewSession}, err))
                setFailureStatus(state, err);
        }
    }

    void replaceSessionList(AppState& state) {
        freeChatTextures(state.chat);
        state.chat.clear();
        for (auto& conversation : state.conversations) freeChatTextures(conversation.msgs);
        state.conversations.clear();
        state.activeConv = -1;
        state.lastContextTokens = 0;
        state.lastContextWindow = 0;
        state.queuedPrompts.clear();
        mappings_.clear();
        loadedSessions_.clear();
        sessionCwds_.clear();
        activeSessionId_.clear();

        for (const auto& item : listedSessions_) {
            if (!item.is_object()) continue;
            const std::string id = jsonString(item, "sessionId");
            if (id.empty()) continue;
            Conversation conversation;
            conversation.id = id;
            conversation.title = jsonString(item, "title");
            if (conversation.title.empty()) conversation.title = "Chudcode session";
            const std::string cwd = jsonString(item, "cwd");
            if (!cwd.empty()) {
                conversation.docLink = "file:" + cwd;
                sessionCwds_[id] = cwd;
            }
            state.conversations.push_back(std::move(conversation));
        }
    }

    void addAndActivateSession(AppState& state, const std::string& id, const std::string& cwd,
                               const std::string& title) {
        int index = findConversationIndex(state, id);
        if (index < 0) {
            Conversation conversation;
            conversation.id = id;
            conversation.title = title;
            conversation.docLink = cwd.empty() ? std::string() : "file:" + cwd;
            state.conversations.push_back(std::move(conversation));
            index = static_cast<int>(state.conversations.size()) - 1;
        }
        if (!cwd.empty()) sessionCwds_[id] = cwd;
        mappings_[id] = {};
        loadedSessions_.insert(id);
        activateConversation(state, index);
    }

    void handleResponse(AppState& state, const AcpFrame& frame) {
        const auto found = pending_.find(frame.id);
        if (found == pending_.end()) return;
        PendingRequest pending = found->second;
        pending_.erase(found);

        if (!frame.error.is_null() && !(frame.error.is_object() && frame.error.empty())) {
            const std::string error = responseError(frame.error);
            if (pending.op == PendingOp::Prompt) {
                std::vector<ChatMsg>* messages = conversationMsgs(state, pending.sessionId);
                if (messages)
                    mapping::markPromptFailure(*messages, mappings_[pending.sessionId],
                                               pending.placeholder, error);
            } else if (!pending.sessionId.empty()) {
                std::vector<ChatMsg>* messages = conversationMsgs(state, pending.sessionId);
                if (messages)
                    mapping::markPromptFailure(*messages, mappings_[pending.sessionId],
                                               mapping::kNoMessage, error);
            }
            const bool startupFailure = phase_ == Phase::Starting &&
                (pending.op == PendingOp::Initialize || pending.op == PendingOp::Authenticate ||
                 pending.op == PendingOp::List || pending.op == PendingOp::Load ||
                 pending.op == PendingOp::Resume || pending.op == PendingOp::NewSession);
            setErrorStatus(state, error, startupFailure);
            return;
        }

        std::string err;
        captureSessionConfiguration(frame.payload, configuration_);
        switch (pending.op) {
            case PendingOp::Initialize:
                captureCapabilities(frame.payload);
                if (!request("authenticate", {{"methodId", "agent"}}, {PendingOp::Authenticate}, err))
                    setFailureStatus(state, err);
                break;
            case PendingOp::Authenticate:
                requestListOrNew(state);
                break;
            case PendingOp::List: {
                const auto sessions = frame.payload.find("sessions");
                if (sessions != frame.payload.end() && sessions->is_array())
                    for (const auto& session : *sessions) listedSessions_.push_back(session);
                const std::string cursor = jsonString(frame.payload, "nextCursor");
                if (!cursor.empty()) {
                    if (!request("session/list", {{"cwd", options_.cwd}, {"cursor", cursor}},
                                 {PendingOp::List}, err))
                        setFailureStatus(state, err);
                    break;
                }
                replaceSessionList(state);
                if (state.conversations.empty()) {
                    if (!request("session/new",
                                 {{"cwd", options_.cwd}, {"mcpServers", nlohmann::json::array()}},
                                 {PendingOp::NewSession}, err))
                        setFailureStatus(state, err);
                } else if (!requestLoad(state, state.conversations.front().id, PendingOp::Load,
                                        "session/load", err)) {
                    setFailureStatus(state, err);
                }
                break;
            }
            case PendingOp::Load:
            case PendingOp::Resume:
                mapping::setHistoryReplay(mappings_[pending.sessionId], false);
                loadedSessions_.insert(pending.sessionId);
                phase_ = Phase::Ready;
                state.status = "Chudcode connected";
                state.statusKind = 0;
                break;
            case PendingOp::NewSession: {
                const std::string id = jsonString(frame.payload, "sessionId");
                if (id.empty()) {
                    setFailureStatus(state, "Chudcode returned a new session without an id.");
                    break;
                }
                addAndActivateSession(state, id, options_.cwd, "New Chudcode session");
                phase_ = Phase::Ready;
                state.status = "Chudcode connected";
                state.statusKind = 0;
                break;
            }
            case PendingOp::Fork: {
                const std::string id = jsonString(frame.payload, "sessionId");
                if (id.empty()) {
                    setFailureStatus(state, "Chudcode returned a fork without a session id.");
                    break;
                }
                const std::string cwd = sessionCwds_.count(pending.sessionId)
                                            ? sessionCwds_[pending.sessionId] : options_.cwd;
                addAndActivateSession(state, id, cwd, "Forked Chudcode session");
                phase_ = Phase::Ready;
                state.status = "Forked Chudcode session";
                state.statusKind = 0;
                break;
            }
            case PendingOp::Prompt: {
                Conversation* conversation = findConversation(state, pending.sessionId);
                std::vector<ChatMsg>* messages = conversationMsgs(state, pending.sessionId);
                if (conversation && messages)
                    mapping::applyPromptResponse(*conversation, *messages,
                                                 mappings_[pending.sessionId], frame.payload,
                                                 pending.cancelRequested);
                state.status = pending.cancelRequested ? "Chudcode turn cancelled"
                                                       : "Chudcode ready";
                state.statusKind = 0;
                break;
            }
            case PendingOp::Close:
                break;
        }
    }

    void handleNotification(AppState& state, const AcpFrame& frame) {
        if (frame.method == "$/transportError") {
            const std::string error = jsonString(frame.payload, "message");
            if (!activeSessionId_.empty()) {
                std::vector<ChatMsg>* messages = conversationMsgs(state, activeSessionId_);
                if (messages)
                    mapping::markPromptFailure(
                        *messages, mappings_[activeSessionId_], mapping::kNoMessage,
                        error.empty() ? "Chudcode transport failed." : error);
            }
            setFailureStatus(state, error.empty() ? "Chudcode transport failed." : error);
            return;
        }
        const auto configUpdate = frame.payload.find("update");
        if (configUpdate != frame.payload.end())
            captureSessionConfiguration(*configUpdate, configuration_);
        if (frame.method != "session/update" || !frame.payload.is_object()) return;
        const std::string sessionId = jsonString(frame.payload, "sessionId");
        Conversation* conversation = findConversation(state, sessionId);
        std::vector<ChatMsg>* messages = conversationMsgs(state, sessionId);
        if (!conversation || !messages) return;
        const auto update = frame.payload.find("update");
        if (update == frame.payload.end()) return;
        mapping::applyUpdate(*conversation, *messages, mappings_[sessionId], *update,
                             sessionId == activeSessionId_ ? &state.lastContextTokens : nullptr,
                             sessionId == activeSessionId_ ? &state.lastContextWindow : nullptr);
    }

    void handleRequest(AppState& state, const AcpFrame& frame) {
        const std::string sessionId = frame.payload.is_object()
                                          ? jsonString(frame.payload, "sessionId") : std::string();
        Conversation* conversation = findConversation(state, sessionId);
        std::vector<ChatMsg>* messages = conversationMsgs(state, sessionId);
        mapping::ServerRequestDecision decision;
        if (conversation && messages) {
            decision = mapping::applyServerRequest(*conversation, *messages,
                                                   mappings_[sessionId], frame.method,
                                                   frame.payload);
            if (frame.method == "session/request_permission") {
                state.status = "Chudcode permission rejected — no permission UI in v0.1";
                state.statusKind = 2;
            }
        } else if (frame.method == "session/request_permission") {
            decision.result = {{"outcome", {{"outcome", "cancelled"}}}};
            state.status = "Chudcode permission rejected — no permission UI in v0.1";
            state.statusKind = 2;
        } else {
            decision.error = true;
            decision.errorCode = -32601;
            decision.errorMessage = "Method not found: " + frame.method;
        }
        std::string err;
        const bool sent = decision.error
                              ? client_.respondError(frame.id, decision.errorCode,
                                                     decision.errorMessage, err)
                              : client_.respond(frame.id, decision.result, err);
        if (!sent) setFailureStatus(state, err);
    }

    void setErrorStatus(AppState& state, const std::string& message, bool fatal) {
        if (fatal) phase_ = Phase::Failed;
        state.status = message.empty() ? "Chudcode failed" : message;
        state.statusKind = 2;
    }

    void setFailureStatus(AppState& state, const std::string& message) {
        setErrorStatus(state, message, true);
    }

    AcpClient client_;
    StartupOptions options_;
    Phase phase_ = Phase::Stopped;
    Capabilities capabilities_;
    SessionConfiguration configuration_;
    bool resumeSupported_ = false;
    std::unordered_map<AcpRequestId, PendingRequest> pending_;
    std::unordered_map<std::string, mapping::State> mappings_;
    std::unordered_map<std::string, std::string> sessionCwds_;
    std::unordered_set<std::string> loadedSessions_;
    std::vector<nlohmann::json> listedSessions_;
    std::string activeSessionId_;
};

std::unique_ptr<Controller> g_controller;

Controller& controller() {
    if (!g_controller) g_controller = std::make_unique<Controller>();
    return *g_controller;
}

} // namespace

void setRequested(bool value) { g_requested.store(value, std::memory_order_relaxed); }
bool requested() { return g_requested.load(std::memory_order_relaxed); }

bool start(AppState& state, const StartupOptions& options, std::string& err) {
    return controller().start(state, options, err);
}

void tick(AppState& state) {
    if (g_controller) g_controller->tick(state);
}

void shutdown(AppState& state) {
    if (g_controller) g_controller->shutdown(state);
}

bool connected() { return g_controller && g_controller->connected(); }
const SessionConfiguration& sessionConfiguration() {
    static const SessionConfiguration empty;
    return g_controller ? g_controller->configuration() : empty;
}

bool submit(AppState& state, const std::string& text, std::string& err) {
    return controller().submit(state, text, err);
}

bool cancel(AppState& state, std::string& err) { return controller().cancel(state, err); }

bool selectSession(AppState& state, const std::string& sessionId, std::string& err) {
    return controller().selectSession(state, sessionId, err);
}

bool newSession(AppState& state, std::string& err) { return controller().newSession(state, err); }

bool forkSession(AppState& state, const std::string& sessionId, std::string& err) {
    return controller().forkSession(state, sessionId, err);
}

const Capabilities& capabilities() {
    static const Capabilities unavailable;
    return g_controller ? g_controller->capabilities() : unavailable;
}

} // namespace ui::code
