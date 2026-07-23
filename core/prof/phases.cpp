// Implementation of the per-phase simulation timers. See phases.h for the design rationale.

#include "prof/phases.h"
#include "prof/tree.h"

#include <algorithm>
#include <deque>
#include <map>
#include <mutex>
#include <utility>
#include <vector>

using nlohmann::json;

namespace chudware {
namespace prof {
namespace phases {

namespace {

// Newest-N completed studies kept for the report. 64 comfortably covers a multi-study
// session (a static+modal+thermal sweep is ~10 records) without unbounded growth.
constexpr size_t kRingCap = 64;

struct PhaseAgg {
    double ms = 0.0;
    unsigned long long count = 0;
};

struct StudyRec {
    const char* domain = nullptr;
    std::chrono::steady_clock::time_point start;
    unsigned long long seq = 0;
    // Ordered by first occurrence so the report reads in execution order. A study has a
    // handful of phases, so linear scan beats a map here. Names are COPIED (callers may
    // pass composed temporaries, e.g. fea_static's "solve.<label>").
    std::vector<std::pair<std::string, PhaseAgg>> phases;

    void record(const char* name, double ms, unsigned long long count) {
        for (auto& kv : phases) {
            if (kv.first == name) {
                kv.second.ms += ms;
                kv.second.count += count;
                return;
            }
        }
        phases.emplace_back(name, PhaseAgg{ms, count});
    }
};

struct CompletedStudy {
    std::string domain;
    unsigned long long seq = 0;
    double total_ms = 0.0;
    std::vector<std::pair<std::string, PhaseAgg>> phases;
};

// Thread-local stack of open studies (nested studies each get their own record; phases go
// to the innermost). Raw pointers: each StudyRec is owned by its StudyScope.
thread_local std::vector<StudyRec*> tl_stack;

std::mutex g_mtx;
std::deque<CompletedStudy> g_ring;                          // oldest -> newest
unsigned long long g_seq = 0;                               // completed-study counter
std::map<std::string, PhaseAgg> g_domain_totals;            // domain -> {total_ms, runs}
std::map<std::string, std::map<std::string, PhaseAgg>> g_phase_totals;  // domain -> phase
std::map<std::string, PhaseAgg> g_unscoped;                 // phases with no open study

void route(const char* name, double ms, unsigned long long count) {
    if (!tl_stack.empty()) {
        tl_stack.back()->record(name, ms, count);
        return;
    }
    std::lock_guard<std::mutex> lk(g_mtx);
    PhaseAgg& a = g_unscoped[name];
    a.ms += ms;
    a.count += count;
}

}  // namespace

StudyScope::StudyScope(const char* domain) {
    auto* rec = new StudyRec();
    rec->domain = domain ? domain : "?";
    rec->start = std::chrono::steady_clock::now();
    tl_stack.push_back(rec);
    rec_ = rec;
}

StudyScope::~StudyScope() {
    auto* rec = static_cast<StudyRec*>(rec_);
    if (!rec) return;
    // Pop THIS record (defensively search from the top in case of interleaved lifetimes).
    for (auto it = tl_stack.rbegin(); it != tl_stack.rend(); ++it) {
        if (*it == rec) {
            tl_stack.erase(std::next(it).base());
            break;
        }
    }
    const double total_ms = std::chrono::duration<double, std::milli>(
                                std::chrono::steady_clock::now() - rec->start)
                                .count();
    CompletedStudy done;
    done.domain = rec->domain;
    done.total_ms = total_ms;
    done.phases = std::move(rec->phases);
    delete rec;

    std::lock_guard<std::mutex> lk(g_mtx);
    done.seq = ++g_seq;
    PhaseAgg& dt = g_domain_totals[done.domain];
    dt.ms += total_ms;
    dt.count += 1;  // count = runs for the domain totals
    auto& pt = g_phase_totals[done.domain];
    for (const auto& kv : done.phases) {
        PhaseAgg& a = pt[kv.first];
        a.ms += kv.second.ms;
        a.count += kv.second.count;
    }
    g_ring.push_back(std::move(done));
    while (g_ring.size() > kRingCap) g_ring.pop_front();
}

PhaseScope::PhaseScope(const char* name)
    : name_(name ? name : "?"), t0_(std::chrono::steady_clock::now()) {}

PhaseScope::~PhaseScope() {
    const double ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0_)
            .count();
    route(name_, ms, 1);
}

void add(const char* name, double ms, unsigned long long count) {
    route(name ? name : "?", ms, count);
}

unsigned long long completed_count() {
    std::lock_guard<std::mutex> lk(g_mtx);
    return g_seq;
}

json report(bool do_reset) {
    std::lock_guard<std::mutex> lk(g_mtx);
    json studies = json::array();
    for (const auto& s : g_ring) {
        json ph = json::array();
        for (const auto& kv : s.phases) {
            ph.push_back(json{{"name", kv.first},
                              {"ms", kv.second.ms},
                              {"count", kv.second.count},
                              {"pct", s.total_ms > 0.0 ? 100.0 * kv.second.ms / s.total_ms
                                                       : 0.0}});
        }
        studies.push_back(json{{"domain", s.domain},
                               {"seq", s.seq},
                               {"total_ms", s.total_ms},
                               {"phases", std::move(ph)}});
    }
    json totals = json::object();
    for (const auto& kv : g_domain_totals) {
        json phases_j = json::object();
        auto it = g_phase_totals.find(kv.first);
        if (it != g_phase_totals.end()) {
            for (const auto& p : it->second)
                phases_j[p.first] = json{{"ms", p.second.ms}, {"count", p.second.count}};
        }
        totals[kv.first] = json{{"runs", kv.second.count},
                                {"total_ms", kv.second.ms},
                                {"phases", std::move(phases_j)}};
    }
    json unscoped = json::object();
    for (const auto& kv : g_unscoped)
        unscoped[kv.first] = json{{"ms", kv.second.ms}, {"count", kv.second.count}};

    json r{{"ok", true},
           {"completed", g_seq},
           {"kept", g_ring.size()},
           {"studies", std::move(studies)},
           {"totals", std::move(totals)},
           {"unscoped", std::move(unscoped)}};
    if (do_reset) {
        g_ring.clear();
        g_seq = 0;
        g_domain_totals.clear();
        g_phase_totals.clear();
        g_unscoped.clear();
    }
    return r;
}

json report_tree(const json& opts, bool do_reset) {
    // Build from the same locked snapshot as report() so flat + tree agree.
    std::lock_guard<std::mutex> lk(g_mtx);

    // Root children = one node per completed study. When a domain appears more than
    // once, suffix "@seq" so paths stay unique and stable.
    std::map<std::string, int> domain_hits;
    for (const auto& s : g_ring) domain_hits[s.domain] += 1;
    // (domain_hits used below for unique labels)

    tree::Node root;
    root.name = "";
    double root_total_ms = 0.0;

    for (const auto& s : g_ring) {
        std::vector<std::pair<std::string, std::pair<double, unsigned long long>>> phases;
        phases.reserve(s.phases.size());
        for (const auto& kv : s.phases) {
            phases.emplace_back(kv.first,
                                std::make_pair(kv.second.ms, kv.second.count));
        }
        tree::Node study = tree::build_phase_tree(phases, s.total_ms);
        // Name the study node.
        std::string label = s.domain;
        if (domain_hits[s.domain] > 1) {
            label += "@";
            label += std::to_string(s.seq);
        }
        study.name = label;
        // build_phase_tree leaves name=""; we overwrote it. total_ms already set.
        root.children.push_back(std::move(study));
        root_total_ms += s.total_ms;
        root.total_samples += 1;  // one "sample" per study for ordering
    }
    root.total_ms = root_total_ms;

    // Sort studies hot-first by total_ms (build_phase_tree already sorted each study).
    std::sort(root.children.begin(), root.children.end(),
              [](const tree::Node& a, const tree::Node& b) {
                  if (a.total_ms != b.total_ms) return a.total_ms > b.total_ms;
                  return a.name < b.name;
              });
    // pct_of_parent relative to root.
    for (auto& c : root.children) {
        c.pct_of_parent =
            root.total_ms > 0.0 ? 100.0 * c.total_ms / root.total_ms : 100.0;
    }
    root.pct_of_parent = 100.0;

    json tree_opts = json::object();
    if (opts.contains("path")) tree_opts["path"] = opts["path"];
    if (opts.contains("depth")) tree_opts["depth"] = opts["depth"];
    if (opts.contains("top")) tree_opts["top"] = opts["top"];
    tree_opts["mode"] = "ms";
    json out = tree::tree_to_json(root, tree_opts);
    out["completed"] = g_seq;
    out["kept"] = g_ring.size();

    if (do_reset) {
        g_ring.clear();
        g_seq = 0;
        g_domain_totals.clear();
        g_phase_totals.clear();
        g_unscoped.clear();
    }
    return out;
}

void reset() {
    std::lock_guard<std::mutex> lk(g_mtx);
    g_ring.clear();
    g_seq = 0;
    g_domain_totals.clear();
    g_phase_totals.clear();
    g_unscoped.clear();
}

}  // namespace phases
}  // namespace prof
}  // namespace chudware
