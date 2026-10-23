#pragma once

#include <string>
#include <string_view>

namespace ui {
namespace copilot {

// Structured tool-call outcome carried by agent.tool_call events. Legacy also represents a
// missing, malformed, or future value: callers must preserve their pre-taxonomy fallback for it.
enum class ToolOutcomeKind {
    Legacy,
    Ok,
    PolicyRefusal,
    GuardBlock,
    ValidationError,
    TimeoutUnknown,
    InternalError,
    ToolError,
};

inline ToolOutcomeKind parseToolOutcomeKind(std::string_view value) noexcept {
    if (value == "ok")               return ToolOutcomeKind::Ok;
    if (value == "policy_refusal")   return ToolOutcomeKind::PolicyRefusal;
    if (value == "guard_block")      return ToolOutcomeKind::GuardBlock;
    if (value == "validation_error") return ToolOutcomeKind::ValidationError;
    if (value == "timeout_unknown")  return ToolOutcomeKind::TimeoutUnknown;
    if (value == "internal_error")   return ToolOutcomeKind::InternalError;
    if (value == "tool_error")       return ToolOutcomeKind::ToolError;
    return ToolOutcomeKind::Legacy;
}

inline const char* toolOutcomeWireName(ToolOutcomeKind kind) noexcept {
    switch (kind) {
        case ToolOutcomeKind::Ok:              return "ok";
        case ToolOutcomeKind::PolicyRefusal:   return "policy_refusal";
        case ToolOutcomeKind::GuardBlock:      return "guard_block";
        case ToolOutcomeKind::ValidationError: return "validation_error";
        case ToolOutcomeKind::TimeoutUnknown:  return "timeout_unknown";
        case ToolOutcomeKind::InternalError:   return "internal_error";
        case ToolOutcomeKind::ToolError:       return "tool_error";
        case ToolOutcomeKind::Legacy:
        default:                               return "";
    }
}

struct ToolOutcomeInfo {
    ToolOutcomeKind kind = ToolOutcomeKind::Legacy;
    bool executed = false;
};

// ActivityItem is a shared core type without taxonomy fields. Keep the structured values in
// the same private detail-trailer channel already used by ERR/ART/META, so live and persisted rows
// retain the distinction without exposing marker bytes to copy/render paths.
inline constexpr std::string_view kToolOutcomeMark = "\n\x1eOUT\x1e\n";
inline constexpr std::string_view kToolOutcomeMarkBare = "\x1eOUT\x1e\n";

inline void appendToolOutcomeTrailer(std::string& detail, ToolOutcomeKind kind, bool executed) {
    const char* name = toolOutcomeWireName(kind);
    if (!name || !*name) return;
    detail.append(kToolOutcomeMark.data(), kToolOutcomeMark.size());
    detail += name;
    detail += '\t';
    detail += executed ? '1' : '0';
}

inline ToolOutcomeInfo toolOutcomeFromDetail(const std::string& detail) noexcept {
    ToolOutcomeInfo out;
    size_t payload = std::string::npos;
    const size_t mark = detail.rfind(kToolOutcomeMark);
    if (mark != std::string::npos) {
        payload = mark + kToolOutcomeMark.size();
    } else if (detail.rfind(kToolOutcomeMarkBare, 0) == 0) {
        payload = kToolOutcomeMarkBare.size();
    }
    if (payload == std::string::npos || payload >= detail.size()) return out;

    const size_t end = detail.find('\n', payload);
    const std::string_view line(detail.data() + payload,
                                (end == std::string::npos ? detail.size() : end) - payload);
    const size_t tab = line.find('\t');
    const std::string_view name = line.substr(0, tab);
    out.kind = parseToolOutcomeKind(name);
    if (out.kind == ToolOutcomeKind::Legacy) return out;
    if (tab != std::string_view::npos) {
        const std::string_view ran = line.substr(tab + 1);
        out.executed = ran == "1" || ran == "true";
    }
    return out;
}

// Remove only the private OUT trailer. Unknown future values are stripped too, then rendered via
// legacy ok/fail state; they must never leak as control bytes or manufacture an "unknown" chip.
inline void stripToolOutcomeTrailer(std::string& detail) {
    size_t mark = detail.rfind(kToolOutcomeMark);
    size_t payload = std::string::npos;
    if (mark != std::string::npos) {
        payload = mark + kToolOutcomeMark.size();
    } else if (detail.rfind(kToolOutcomeMarkBare, 0) == 0) {
        mark = 0;
        payload = kToolOutcomeMarkBare.size();
    }
    if (payload == std::string::npos) return;
    const size_t end = detail.find('\n', payload);
    detail.erase(mark, end == std::string::npos ? std::string::npos : end - mark);
}

}  // namespace copilot
}  // namespace ui
