// core/agent/firm_bid.cpp — see firm_bid.h.
#include "agent/firm_bid.h"

#include <algorithm>

namespace chudware {
namespace agent {

namespace {

int word_count(const std::string& s) {
    int n = 0;
    bool in = false;
    for (char c : s) {
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            in = false;
        } else if (!in) {
            in = true;
            ++n;
        }
    }
    return n;
}

// Per-tier baseline OUTPUT tokens (ops/plan/review the model generates).
long tier_output_base(Complexity cx) {
    switch (cx) {
        case Complexity::Solo: return 1500;       // one model loop, one part
        case Complexity::Standard: return 6000;   // plan + a few feature steps
        case Complexity::Complex: return 22000;   // multi-part / assembly decomposition
    }
    return 6000;
}

// Per-domain fixed context/overhead the input carries (cheatsheets, guides, checks).
long domain_input_overhead(Domain dom) {
    switch (dom) {
        case Domain::Modeling: return 4000;     // ops cheatsheet
        case Domain::Sheetmetal: return 4200;
        case Domain::Assembly: return 5000;     // mates + component context
        case Domain::Cam: return 4500;          // machining guide
        case Domain::Ecad: return 6000;         // schematic/PCB rules
        case Domain::Drawing: return 3500;
        case Domain::Simulation: return 4000;   // material + load setup
        case Domain::Sourcing: return 3000;     // catalog lookups
        case Domain::Research: return 1500;     // no geometry
    }
    return 4000;
}

}  // namespace

TokenEstimate estimate_task_tokens(const std::string& request, Complexity cx, Domain dom) {
    const long words = static_cast<long>(word_count(request));
    // Input: fixed domain overhead + the request itself (~1.4 tokens/word) + a tier
    // scratchpad allowance that grows with complexity.
    const long tier_in = tier_output_base(cx) / 2;  // reasoning/context scales with tier
    long input = domain_input_overhead(dom) + static_cast<long>(words * 1.4) + tier_in;
    // Output: tier baseline, nudged up for a wordy (feature-rich) request.
    long output = tier_output_base(cx) + static_cast<long>(words * 2.0);
    TokenEstimate e;
    e.input_tokens = input;
    e.output_tokens = output;
    e.total_tokens = input + output;
    return e;
}

double tokens_to_usd(long tokens, double usd_per_1k) {
    if (usd_per_1k < 0.0) usd_per_1k = 0.0;
    return (static_cast<double>(tokens) / 1000.0) * usd_per_1k;
}

const std::vector<Firm>& firm_roster() {
    // Specialists are leaner (efficiency < 1) IN their domain; generalists take any
    // job at baseline. IDs are stable for dispatch/ledger references.
    static const std::vector<Firm> kRoster = {
        {"mech_precision", "Precision Mechanical Co.", Domain::Modeling, false, 0.80},
        {"sheet_fab", "SheetFab Works", Domain::Sheetmetal, false, 0.82},
        {"assembly_sys", "Assembly Systems Group", Domain::Assembly, false, 0.85},
        {"machine_shop", "Vanguard Machine Shop", Domain::Cam, false, 0.83},
        {"circuit_labs", "Circuit Labs EE", Domain::Ecad, false, 0.80},
        {"draft_house", "Drafting House", Domain::Drawing, false, 0.85},
        {"sim_dynamics", "Sim Dynamics FEA/CFD", Domain::Simulation, false, 0.82},
        {"sourcing_partners", "Sourcing Partners", Domain::Sourcing, false, 0.88},
        {"gen_engineering", "General Engineering LLP", Domain::Modeling, true, 1.00},
        {"gen_rapid", "Rapid Generalists", Domain::Modeling, true, 1.10},
    };
    return kRoster;
}

std::vector<Bid> collect_bids(const std::string& request, Complexity cx, Domain dom,
                              double usd_per_1k, double budget_usd) {
    const TokenEstimate base = estimate_task_tokens(request, cx, dom);
    std::vector<Bid> bids;
    for (const Firm& f : firm_roster()) {
        Bid b;
        b.firm_id = f.id;
        b.firm_name = f.name;
        // In scope if generalist, or the firm's specialty matches the task domain.
        b.in_scope = f.generalist || f.specialty == dom;
        // A specialist bids with its efficiency; a generalist covering a non-native
        // domain pays a spread-thin penalty (+15%).
        double eff = f.efficiency;
        if (f.generalist && dom != f.specialty) eff *= 1.15;
        b.tokens = static_cast<long>(base.total_tokens * eff);
        b.usd = tokens_to_usd(b.tokens, usd_per_1k);
        b.within_budget = (budget_usd <= 0.0) || (b.usd <= budget_usd);
        bids.push_back(b);
    }
    // Cheapest in-scope first; out-of-scope sink to the bottom.
    std::sort(bids.begin(), bids.end(), [](const Bid& a, const Bid& b) {
        if (a.in_scope != b.in_scope) return a.in_scope;
        return a.tokens < b.tokens;
    });
    return bids;
}

}  // namespace agent
}  // namespace chudware
