// core/agent/logging_provider.h — thin C++ logging wrapper for provider calls.
//
// Provides LoggingProviderState — a lightweight struct the caller uses to:
//   1. Receive per-call timing (last_latency_ms).
//   2. Receive sidecar paths written at "full" consent (last_input_ref / last_output_ref).
//
// The actual wrapping is done inline in run.cpp using the wrap_logging() free function
// defined here, which captures a pointer to this state.  ProvReply / ProviderFn are
// defined locally in run.cpp and forwarded via template so this header stays
// self-contained.
//
// Offline + sidecar write policy (mirrors contracts/providers.md §5):
//   - consent == "full" : request + response bodies written to run_dir/sidecars/
//   - consent == "meta" : timing recorded, no bodies written
//   - consent == "off"  : timing recorded, no bodies written
//
// The wrapper never makes network calls and never changes the return value.
// GUI target must NOT link core/agent directly (C-ABI bridge only).
#pragma once

#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <string>

#include <nlohmann/json.hpp>

#include "agent/events.h"   // run_dir(), cap_raw()

namespace chudware {
namespace agent {

using nlohmann::json;

// ---------------------------------------------------------------------------
// LoggingProviderState
// Caller-owned; the wrap_logging() lambda writes into it on every call.
// Read last_input_ref / last_output_ref after the run to pass into
// emit_ai_artifact() as the per-run sidecar refs.
// ---------------------------------------------------------------------------
struct LoggingProviderState {
    std::string last_input_ref;
    std::string last_output_ref;
    double      last_latency_ms = 0.0;
    std::atomic<int> call_count{0};

    // Prevent copy (atomic member is not copyable).
    LoggingProviderState() = default;
    LoggingProviderState(const LoggingProviderState&) = delete;
    LoggingProviderState& operator=(const LoggingProviderState&) = delete;
};

// ---------------------------------------------------------------------------
// wrap_logging<ProviderFn>
//
// Wraps any callable matching ProviderFn = std::function<Reply(system, messages, max_tokens)>
// with per-call timing + optional sidecar writes.  Uses a template so ProvReply / ProviderFn
// don't need to be declared in this header.
//
// Parameters:
//   inner   — the inner ProviderFn (moved in)
//   run_id  — used to resolve run_dir(run_id)/sidecars/
//   consent — "off" | "meta" | "full"
//   state   — pointer to a LoggingProviderState owned by the call site
// ---------------------------------------------------------------------------
template <typename ProviderFn>
ProviderFn wrap_logging(
    ProviderFn inner,
    const std::string& run_id,
    const std::string& consent,
    LoggingProviderState* state)
{
    return [inner = std::move(inner), run_id, consent, state]
           (const std::string& system, const json& messages, int max_tokens)
           -> decltype(inner(system, messages, max_tokens))
    {
        const auto t0 = std::chrono::steady_clock::now();
        auto reply = inner(system, messages, max_tokens);
        const double latency_ms =
            std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - t0).count();

        if (!state) return reply;

        state->last_latency_ms = latency_ms;
        const int idx = state->call_count.fetch_add(1, std::memory_order_relaxed);

        std::string input_ref, output_ref;

        if (consent == "full") {
            try {
                std::filesystem::path dir = run_dir(run_id) / "sidecars";
                std::filesystem::create_directories(dir);

                const std::string stem =
                    run_id + "_" + std::to_string(idx);

                // Request sidecar (system + messages)
                std::filesystem::path req_p = dir / (stem + "_req.jsonl");
                {
                    json req = {{"system", system}, {"messages", messages}};
                    std::ofstream rf(req_p, std::ios::trunc);
                    rf << req.dump() << "\n";
                }
                input_ref = req_p.string();

                // Response sidecar (text + thinking, both capped).
                std::filesystem::path resp_p = dir / (stem + "_resp.jsonl");
                {
                    json resp = {{"text", cap_raw(reply.text)},
                                 {"thinking", cap_raw(reply.thinking)}};
                    std::ofstream sf(resp_p, std::ios::trunc);
                    sf << resp.dump() << "\n";
                }
                output_ref = resp_p.string();
            } catch (...) {
                // Never break the provider call due to a logging failure.
                input_ref.clear();
                output_ref.clear();
            }
        }

        state->last_input_ref  = std::move(input_ref);
        state->last_output_ref = std::move(output_ref);
        return reply;
    };
}

}  // namespace agent
}  // namespace chudware
