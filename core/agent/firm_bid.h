// core/agent/firm_bid.h — deterministic token-cost estimator + contractor-firm bids.
//
// The Eng. Firm org lets specialist/generalist contractor firms BID on a task in
// predicted TOKEN OUTPUT, converted to USD, against a budget ceiling. To keep bids
// honest and reproducible (no LLM guessing its own cost), the estimate is a pure
// deterministic function of the request + its complexity + domain. Firms then apply
// their efficiency (a specialist in-domain is cheaper/faster than a generalist).
//
// No LLM, no I/O -> deterministic + unit-testable.
#pragma once

#include <string>
#include <vector>

#include "agent/complexity.h"
#include "agent/domain.h"

namespace aicad {
namespace agent {

struct TokenEstimate {
    long input_tokens;   // system+context+request fed to the model
    long output_tokens;  // generated ops/plan/review
    long total_tokens;   // input + output
};

// Predict the token cost of completing a task. Deterministic: scales with the
// complexity tier (solo/standard/complex), the domain's per-task overhead, and the
// request length. Used both by the budgeter and by firms to size their bids.
TokenEstimate estimate_task_tokens(const std::string& request, Complexity cx, Domain dom);

// Convert a token count to USD at a blended rate (dollars per 1000 tokens).
double tokens_to_usd(long tokens, double usd_per_1k);

struct Firm {
    std::string id;
    std::string name;
    Domain specialty;    // the domain this firm is expert in
    bool generalist;     // true = bids on any domain (no specialty bonus)
    double efficiency;   // multiplier on predicted tokens; <1 = leaner, >1 = heavier
};

struct Bid {
    std::string firm_id;
    std::string firm_name;
    bool in_scope;       // firm can take this domain (specialist match or generalist)
    long tokens;         // this firm's predicted token cost (estimate * efficiency)
    double usd;          // tokens -> USD
    bool within_budget;  // usd <= budget_usd (0 budget = unlimited)
};

// The built-in contractor-firm roster (specialists per discipline + generalists).
const std::vector<Firm>& firm_roster();

// Collect bids from every roster firm for a task. Sorted cheapest-first; out-of-scope
// firms are included but flagged !in_scope so the orchestrator can see who declined.
std::vector<Bid> collect_bids(const std::string& request, Complexity cx, Domain dom,
                              double usd_per_1k, double budget_usd);

}  // namespace agent
}  // namespace aicad
