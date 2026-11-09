#pragma once

// A task sub-agent's run totals — requests, context occupancy, spend, model, pending provider
// retry — as OMP's AgentProgress reports them on `subagent_progress` and the producer maps them
// onto the `sub_run` event (services/agent/omp_events.py::_subagent_usage). They ride the card's
// private detail trailer, the channel ERR/ART/META/OUT already use, so a restored session keeps
// them without a new persisted field. GL-free and header-only.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdio>
#include <limits>
#include <string>
#include <string_view>
#include <type_traits>

#include <nlohmann/json.hpp>

namespace ui {
namespace copilot {

struct SubRunUsage {
    int requests = 0;                         // assistant requests across the run
    long long tokens = 0;                     // input + output + cache writes over the run
    int contextTokens = 0, contextWindow = 0; // the latest request's prefix vs the model's window
    double costUsd = 0.0;
    int durationMs = 0;
    std::string model;                        // resolved "<provider>/<id>[:level]"
    bool retrying = false;                    // asleep between provider retries
    int retryAttempt = 0, retryMax = 0, retryDelayMs = 0;
    std::string retryError;
};

namespace usage_detail {
// A present, finite, non-negative JSON number; anything else (absent, string, bool, null) is not.
inline bool number(const nlohmann::json& data, const char* key, double& out) {
    const auto it = data.find(key);
    if (it == data.end() || !it->is_number()) return false;
    const double value = it->get<double>();
    if (!std::isfinite(value) || value < 0.0) return false;
    out = value;
    return true;
}

template <typename T>
inline T clampTo(double value) {
    // 9.0e18 stays inside long long; max() would round up to 2^63 as a double.
    constexpr double kMax = sizeof(T) >= 8 ? 9.0e18 : static_cast<double>(std::numeric_limits<T>::max());
    return static_cast<T>(std::min(value, kMax));
}
}  // namespace usage_detail

// Fold one `sub_run` event into `usage`. Only keys the event carries replace what is held, so a
// lifecycle frame (no totals) never blanks the latest progress values. `retry_state` is a
// snapshot: a progress frame (one carrying `requests`) without it means the retry resolved.
// Returns true when anything was taken from the event.
inline bool mergeSubRunUsage(SubRunUsage& usage, const nlohmann::json& data) {
    if (!data.is_object()) return false;
    bool touched = false;
    double value = 0.0;
    const auto take = [&](const char* key, auto& field) {
        if (!usage_detail::number(data, key, value)) return;
        field = usage_detail::clampTo<std::remove_reference_t<decltype(field)>>(value);
        touched = true;
    };
    take("requests", usage.requests);
    take("tokens", usage.tokens);
    take("context_tokens", usage.contextTokens);
    take("context_window", usage.contextWindow);
    take("duration_ms", usage.durationMs);
    if (usage_detail::number(data, "cost_usd", value)) {
        usage.costUsd = value;
        touched = true;
    }
    const auto model = data.find("resolved_model");
    if (model != data.end() && model->is_string() && !model->get<std::string>().empty()) {
        usage.model = model->get<std::string>();
        touched = true;
    }
    const auto retry = data.find("retry_state");
    if (retry != data.end() && retry->is_object()) {
        usage.retrying = true;
        usage.retryAttempt = usage.retryMax = usage.retryDelayMs = 0;
        usage.retryError.clear();
        if (usage_detail::number(*retry, "attempt", value)) usage.retryAttempt = usage_detail::clampTo<int>(value);
        if (usage_detail::number(*retry, "max_attempts", value)) usage.retryMax = usage_detail::clampTo<int>(value);
        if (usage_detail::number(*retry, "delay_ms", value)) usage.retryDelayMs = usage_detail::clampTo<int>(value);
        const auto error = retry->find("error");
        if (error != retry->end() && error->is_string()) usage.retryError = error->get<std::string>();
        touched = true;
    } else if (usage.retrying && data.contains("requests")) {
        usage.retrying = false;
        usage.retryAttempt = usage.retryMax = usage.retryDelayMs = 0;
        usage.retryError.clear();
        touched = true;
    }
    return touched;
}

// The trailer is appended after the card's purpose. Its leading newline is part of the mark, so
// a purpose-less detail still matches.
inline constexpr std::string_view kSubRunUsageMark = "\n\x1e" "USE\x1e\n";

inline void stripSubRunUsageTrailer(std::string& detail) {
    const size_t mark = detail.rfind(kSubRunUsageMark);
    if (mark != std::string::npos) detail.erase(mark);
}

// The usage as the `sub_run` event keys carry it, so one decoder (mergeSubRunUsage) reads the
// trailer and the persisted trace alike.
inline nlohmann::json subRunUsageJson(const SubRunUsage& usage) {
    nlohmann::json packed = {{"requests", usage.requests}, {"tokens", usage.tokens},
                             {"context_tokens", usage.contextTokens},
                             {"context_window", usage.contextWindow},
                             {"cost_usd", usage.costUsd}, {"duration_ms", usage.durationMs}};
    if (!usage.model.empty()) packed["resolved_model"] = usage.model;
    if (usage.retrying)
        packed["retry_state"] = {{"attempt", usage.retryAttempt}, {"max_attempts", usage.retryMax},
                                 {"delay_ms", usage.retryDelayMs}, {"error", usage.retryError}};
    return packed;
}

inline void appendSubRunUsageTrailer(std::string& detail, const SubRunUsage& usage) {
    detail.append(kSubRunUsageMark.data(), kSubRunUsageMark.size());
    detail += subRunUsageJson(usage).dump(-1, ' ', false, nlohmann::json::error_handler_t::replace);
}

// A damaged or truncated trailer (a tail-capped persisted row) reads as no usage.
inline SubRunUsage subRunUsageFromDetail(const std::string& detail) {
    SubRunUsage usage;
    const size_t mark = detail.rfind(kSubRunUsageMark);
    if (mark == std::string::npos) return usage;
    mergeSubRunUsage(usage, nlohmann::json::parse(
        detail.begin() + static_cast<std::ptrdiff_t>(mark + kSubRunUsageMark.size()), detail.end(),
        nullptr, false));
    return usage;
}

// "346k" / "1M" / "1.2M" / "482".
inline std::string subRunCount(long long n) {
    char text[32];
    if (n >= 999500) {
        std::snprintf(text, sizeof(text), "%.1f", static_cast<double>(n) / 1e6);
        std::string out = text;
        if (out.size() > 2 && out.compare(out.size() - 2, 2, ".0") == 0) out.erase(out.size() - 2);
        return out + "M";
    }
    if (n >= 1000) std::snprintf(text, sizeof(text), "%lldk", (n + 500) / 1000);
    else std::snprintf(text, sizeof(text), "%lld", n);
    return text;
}

// "160 requests · ctx 346k / 1M · $6.58 · sonnet-5-5". `model` is the caller's short label for
// usage.model. Cost shows only when it is above zero; an empty line means nothing to say yet.
inline std::string subRunUsageLine(const SubRunUsage& usage, const std::string& model) {
    std::string out;
    const auto add = [&](const std::string& part) {
        if (!out.empty()) out += " \xc2\xb7 ";
        out += part;
    };
    if (usage.requests > 0)
        add(std::to_string(usage.requests) + (usage.requests == 1 ? " request" : " requests"));
    if (usage.contextTokens > 0)
        add("ctx " + subRunCount(usage.contextTokens) +
            (usage.contextWindow > 0 ? " / " + subRunCount(usage.contextWindow) : std::string()));
    if (usage.costUsd > 0.0) {
        char text[32];
        if (usage.costUsd >= 0.01) std::snprintf(text, sizeof(text), "$%.2f", usage.costUsd);
        else std::snprintf(text, sizeof(text), "<$0.01");
        add(text);
    }
    if (!model.empty()) add(model);
    return out;
}

}  // namespace copilot
}  // namespace ui
