#include "agent_dag.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <deque>
#include <iterator>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include "imgui.h"
#include "imgui_internal.h"
#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"
#include "ui/copilot/copilot.h"
#include "ui/copilot/copilot_internal.h"
#include "ui/copilot/agent_text.h"
namespace ui {
namespace {
// Execution trace = one agent tree per conversation. Agent "main" is always index 0; every
// sub-agent is keyed by its `subagent_id` and nests under the agent that spawned it. Each agent
// owns a bounded step deque (oldest dropped, counted in `dropped`, shown as one summary row).

constexpr size_t kMaxStepsPerAgent   = 2000;
constexpr size_t kMaxStepText        = 64 * 1024;   // thinking / prose accumulation cap
constexpr size_t kMaxAgents          = 256;
constexpr size_t kMaxSpanIndex       = 100000;
constexpr size_t kPersistStepsPerAgent = 400;
constexpr size_t kPersistText        = 4000;
constexpr size_t kSpanScan           = 512;         // upsert lookback per agent
constexpr const char* kTimelineRegion = "aiws.trace_timeline";

enum class StepKind : int {
    Thinking, Reply, Tool, User, Steer, Spawn, SubagentEnd, Context, Compaction, Retry, Plan,
    Note, Error, RunStart, RunEnd, Event, Count
};

struct Step {
    unsigned long long uid = 0, rev = 1;
    StepKind    kind = StepKind::Event;
    std::string spanId;     // tool call / sub-run / spawned sub-agent id, for in-place updates
    std::string title;      // tool name, or a short heading
    std::string target;     // what it acts on / source / counts
    std::string detail;     // full body: thinking, prose, args preview, prompt text
    std::string error;      // failure text (tool error_preview, run error)
    std::string status;     // running | ok | failed | skipped | retry | stopped | cancelled | ""
    int         durationMs = -1;
    double      ts = 0.0;
    bool        open = false;  // a thinking/prose block still receiving deltas
};

struct Agent {
    std::string id, parent, name, brief, status, model, note;
    double startTs = 0.0, endTs = 0.0;
    int toolCount = 0, failCount = 0;
    long long dropped = 0;
    bool named = false;   // `name` is the spawner-assigned task name; role/purpose never replace it
    std::deque<Step> steps;
};

struct TraceStore {
    std::string runId;
    std::vector<Agent> agents;
    std::unordered_map<std::string, int> agentIdx;
    std::unordered_map<std::string, std::string> spanAgent;  // step span id -> agent id
    unsigned long long uidCtr = 1;
    int thoughtCount = 0, ctxCount = 0;
    double userSyncTs = 0.0;
    double lastEventTs = 0.0;   // persists the last delta too, not just the start of its text block
    bool settled = false;      // ended traces are normalized once, never on every display frame
    bool userSeeded = false;
    std::string selAgent = "main";

    TraceStore() { reset(std::string()); }
    void reset(const std::string& rid) {
        runId = rid;
        agents.clear(); agentIdx.clear(); spanAgent.clear();
        thoughtCount = ctxCount = 0;
        userSyncTs = 0.0; userSeeded = false;
        lastEventTs = 0.0; settled = false;
        selAgent = "main";
        Agent m;
        m.id = "main"; m.name = "Main agent"; m.status = "idle";
        agents.push_back(std::move(m));
        agentIdx["main"] = 0;
    }
    bool hasContent() const {
        if (agents.size() > 1) return true;
        return !agents.empty() && (!agents[0].steps.empty() || agents[0].dropped > 0);
    }
};

static std::unordered_map<std::string, TraceStore> s_traces;   // conversation id -> live trace

struct FallbackTrace {
    TraceStore store;
    size_t sig = 0;
    double builtAt = -1.0;
    bool built = false;
};
static std::unordered_map<std::string, FallbackTrace> s_fallback;  // conv id -> transcript trace

static std::string jstr(const nlohmann::json& d, const char* k) {
    auto it = d.find(k);
    return (it != d.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

static bool genericAgentName(const std::string& s) {
    return s.empty() || s == "task" || s == "scout" || s == "worker" ||
           s == "reviewer" || s == "security-reviewer" || s == "sonic" ||
           s == "Subagent" || s == "Sub-agent" || s == "sub-agent";
}

static bool readableTaskId(const std::string& id) {
    // Chudcode's task handles are readable identifiers, including dotted nesting.
    // Never replace a human label with an opaque legacy span id.
    return !id.empty() && id[0] >= 'A' && id[0] <= 'Z' &&
        std::all_of(id.begin(), id.end(), [](unsigned char c) {
            return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                   (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
        });
}

static int jint(const nlohmann::json& d, const char* k, int def = 0) {
    auto it = d.find(k);
    if (it == d.end() || !it->is_number()) return def;
    return it->is_number_integer() ? it->get<int>() : (int)it->get<double>();
}

static void appendCapped(std::string& dst, const std::string& src) {
    if (dst.size() >= kMaxStepText || src.empty()) return;
    dst.append(src, 0, std::min(src.size(), kMaxStepText - dst.size()));
}
static bool blankText(const std::string& s) {
    return s.find_first_not_of(" \t\r\n") == std::string::npos;
}
static bool stoppedReason(const std::string& reason) {
    return reason == "confirmed" || reason == "cancelled" ||
           reason == "stopped" || reason == "interrupted";
}

static bool turnWasStopped(const ChatMsg& m) {
    return m.resultPartial ||
        std::any_of(m.activity.begin(), m.activity.end(), [](const ActivityItem& a) {
            return a.stage == "__stop_meta__" && stoppedReason(a.title);
        });
}
static bool terminalStatus(const std::string& s) {
    return s == "done" || s == "failed" || s == "cancelled" || s == "stopped";
}
// Sub-run / sub-agent status vocabulary -> the agent vocabulary.
static std::string agentStatusFrom(const std::string& raw) {
    if (raw == "done" || raw == "complete" || raw == "completed" || raw == "ok" ||
        raw == "pass" || raw == "passed" || raw == "success" || raw == "succeeded")
        return "done";
    if (raw == "fail" || raw == "failed" || raw == "error") return "failed";
    if (raw == "cancel" || raw == "cancelled" || raw == "canceled" || raw == "aborted")
        return "cancelled";
    if (raw == "stopped" || raw == "interrupted") return "stopped";
    if (raw == "pending" || raw == "waiting") return "pending";
    return "running";
}
// Activity-row / sub-run status -> the step vocabulary.
static std::string stepStatusFrom(const std::string& raw) {
    if (raw == "done" || raw == "pass" || raw == "ok" || raw == "complete" || raw == "completed")
        return "ok";
    if (raw == "fail" || raw == "failed" || raw == "error") return "failed";
    if (raw == "skipped") return "skipped";
    if (raw == "retry") return "retry";
    if (raw == "cancelled" || raw == "canceled") return "cancelled";
    if (raw == "stopped" || raw == "interrupted") return "stopped";
    if (raw == "running" || raw == "started" || raw == "pending") return "running";
    return raw.empty() ? std::string() : std::string("ok");
}

static int findAgent(const TraceStore& t, const std::string& id) {
    auto it = t.agentIdx.find(id);
    return it == t.agentIdx.end() ? -1 : it->second;
}

// Agent that owns span `pid`: an agent id itself, or the agent a recorded step span belongs to.
static int spanOwner(const TraceStore& t, const std::string& pid) {
    if (pid.empty()) return 0;
    const int direct = findAgent(t, pid);
    if (direct >= 0) return direct;
    auto s = t.spanAgent.find(pid);
    if (s == t.spanAgent.end()) return 0;
    const int owner = findAgent(t, s->second);
    return owner >= 0 ? owner : 0;
}

// True when `ancestor` is `id` or sits above it — guards a re-parent from forming a cycle.
static bool isAncestor(const TraceStore& t, const std::string& ancestor, const std::string& id) {
    std::string cur = id;
    for (size_t guard = 0; guard <= t.agents.size() && !cur.empty(); ++guard) {
        if (cur == ancestor) return true;
        const int i = findAgent(t, cur);
        if (i < 0) return false;
        cur = t.agents[(size_t)i].parent;
    }
    return false;
}

static int ensureAgent(TraceStore& t, const std::string& id, int parentIdx, double now) {
    const int have = findAgent(t, id);
    if (have >= 0) return have;
    if (t.agents.size() >= kMaxAgents) return 0;   // overflow folds into the main agent
    Agent a;
    a.id = id;
    const std::string& pid = t.agents[(size_t)std::max(parentIdx, 0)].id;
    a.parent = pid == id ? std::string("main") : pid;
    a.status = "running";
    a.startTs = now;
    t.agentIdx[id] = (int)t.agents.size();
    t.agents.push_back(std::move(a));
    return (int)t.agents.size() - 1;
}

static void closeOpen(Agent& a) {
    if (a.steps.empty() || !a.steps.back().open) return;
    Step& s = a.steps.back();
    s.open = false;
    s.status.clear();
    ++s.rev;
}

static Step& pushStep(TraceStore& t, int ai, StepKind k, double ts) {
    Agent& a = t.agents[(size_t)ai];
    t.lastEventTs = std::max(t.lastEventTs, ts);
    closeOpen(a);
    if (a.steps.size() >= kMaxStepsPerAgent) { a.steps.pop_front(); ++a.dropped; }
    Step s;
    s.uid = t.uidCtr++;
    s.kind = k;
    s.ts = ts;
    a.steps.push_back(std::move(s));
    return a.steps.back();
}

static void indexSpan(TraceStore& t, const std::string& span, int ai) {
    if (span.empty()) return;
    if (t.spanAgent.size() >= kMaxSpanIndex) t.spanAgent.clear();
    t.spanAgent[span] = t.agents[(size_t)ai].id;
}

static Step* findSpanStep(Agent& a, const std::string& span, StepKind k) {
    if (span.empty()) return nullptr;
    size_t scanned = 0;
    for (auto it = a.steps.rbegin(); it != a.steps.rend() && scanned < kSpanScan; ++it, ++scanned)
        if (it->kind == k && it->spanId == span) return &*it;
    return nullptr;
}

// An agent heard from without a start marker is running from its first event onward.
static void touchAgent(Agent& a, double now) {
    if (a.status == "idle" || a.status == "pending" || a.status.empty()) a.status = "running";
    if (a.startTs <= 0.0) a.startTs = now;
}

// The owning agent of an event: `subagent_id` when present, else the agent `parent_id` joins
// to, else the main agent.
static int eventOwner(TraceStore& t, const nlohmann::json& d, double now) {
    const std::string sub = jstr(d, "subagent_id");
    const int ai = sub.empty() ? spanOwner(t, jstr(d, "parent_id"))
                               : ensureAgent(t, sub, spanOwner(t, jstr(d, "parent_id")), now);
    touchAgent(t.agents[(size_t)ai], now);
    return ai;
}

// Coalesce a streamed thinking/prose delta into the agent's open block of the same kind.
static void appendStream(TraceStore& t, int ai, StepKind k, const std::string& txt, double now) {
    t.lastEventTs = std::max(t.lastEventTs, now);
    Agent& a = t.agents[(size_t)ai];
    if (!a.steps.empty() && a.steps.back().open && a.steps.back().kind == k) {
        appendCapped(a.steps.back().detail, txt);
        ++a.steps.back().rev;
        return;
    }
    // A contentless flush never opens (or splits) a block.
    if (blankText(txt)) return;
    Step& s = pushStep(t, ai, k, now);
    s.open = true;
    s.status = "running";
    appendCapped(s.detail, txt);
    if (k == StepKind::Thinking) ++t.thoughtCount;
}

static std::string toolStatus(const nlohmann::json& d) {
    const std::string outcome = jstr(d, "outcome_kind");
    if (outcome == "ok") return "ok";
    if (outcome == "policy_refusal" || outcome == "guard_block") return "skipped";
    if (outcome == "timeout_unknown") return "retry";
    if (!outcome.empty()) return "failed";
    auto it = d.find("ok");
    const bool ok = (it == d.end() || !it->is_boolean()) ? true : it->get<bool>();
    return ok ? "ok" : "failed";
}

static void subagentLifecycle(TraceStore& t, const nlohmann::json& d, const std::string& raw,
                              double now) {
    const std::string sub = jstr(d, "subagent_id");
    std::string parentSpan = jstr(d, "parent_id");
    if (parentSpan.empty()) parentSpan = jstr(d, "parent_stage");
    int parentIdx = spanOwner(t, parentSpan);
    if (t.agents[(size_t)parentIdx].id == sub) parentIdx = 0;
    const int ai = ensureAgent(t, sub, parentIdx, now);
    if (ai == 0) return;   // agent table full
    const std::string purpose = foldToAtlasGlyphs(jstr(d, "purpose"));
    const std::string assigned = foldToAtlasGlyphs(jstr(d, "name"));
    const std::string description = foldToAtlasGlyphs(jstr(d, "description"));
    const std::string detail = jstr(d, "detail");
    const std::string st = agentStatusFrom(raw);
    {
        Agent& a = t.agents[(size_t)ai];
        if (!assigned.empty()) {
            a.name = assigned;
            a.named = true;
        } else if (!a.named && !genericAgentName(purpose)) {
            a.name = purpose;
        }
        // The label lands late (after the start frame) and is the agent's one-line task.
        if (!description.empty()) a.brief = description;
        else if (a.brief.empty() && !purpose.empty() && purpose != a.name &&
                 !genericAgentName(purpose)) a.brief = purpose;
        if (!parentSpan.empty() && parentIdx != ai) {
            const std::string& pid = t.agents[(size_t)parentIdx].id;
            if (a.parent != pid && !isAncestor(t, sub, pid)) a.parent = pid;
        }
        const int pi = std::max(findAgent(t, a.parent), 0);
        if (Step* sp = findSpanStep(t.agents[(size_t)pi], sub, StepKind::Spawn)) {
            sp->title = "Spawned " + a.name;
            sp->target = a.brief;
            ++sp->rev;
        }
        if (!detail.empty()) a.note = detail;
        if (!terminalStatus(st)) {
            a.status = st;
            if (a.startTs <= 0.0) a.startTs = now;
            return;
        }
        if (terminalStatus(a.status) && a.status != "stopped") return;   // repeated terminal frame
        a.status = st;
        a.endTs = now;
        if (a.startTs <= 0.0) a.startTs = now;
    }
    // The sub-agent's own timeline ends with its result.
    {
        Step& s = pushStep(t, ai, StepKind::RunEnd, now);
        s.title = st == "done" ? "Finished" : st == "failed" ? "Failed" : "Cancelled";
        s.status = st == "done" ? "ok" : st;
        s.detail = detail;
        if (st == "failed") s.error = detail;
    }
    // The parent's spawn row turns into the outcome; without one, add an explicit marker.
    const Agent& a = t.agents[(size_t)ai];
    const int pi = std::max(findAgent(t, a.parent), 0);
    const std::string name = a.name.empty() ? std::string("Sub-agent") : a.name;
    const int ms = (int)std::max(0.0, (a.endTs - a.startTs) * 1000.0);
    const std::string stepStatus = st == "done" ? std::string("ok") : st;
    if (Step* sp = findSpanStep(t.agents[(size_t)pi], sub, StepKind::Spawn)) {
        sp->status = stepStatus;
        sp->durationMs = ms;
        if (st == "failed") sp->error = detail;
        ++sp->rev;
        return;
    }
    Step& s = pushStep(t, pi, StepKind::SubagentEnd, now);
    s.spanId = sub;
    s.title = name + (st == "done" ? " finished" : st == "failed" ? " failed" : " cancelled");
    s.status = stepStatus;
    s.durationMs = ms;
    s.detail = detail;
    if (st == "failed") s.error = detail;
}

// Close the whole tree, not only the main agent. A stream can disappear without run_finished
// (Stop, transport loss, crash recovery). Its last recorded event is the cutoff, never wall now.
static void settleTrace(TraceStore& t) {
    if (t.settled) return;
    double end = t.lastEventTs;
    for (const Agent& a : t.agents) {
        end = std::max(end, a.endTs);
        for (const Step& s : a.steps) {
            const bool timed = s.kind == StepKind::Tool || s.kind == StepKind::Spawn ||
                               s.kind == StepKind::Compaction || s.kind == StepKind::Retry;
            end = std::max(end, s.ts + (timed && s.durationMs > 0 ? s.durationMs / 1000.0 : 0.0));
        }
    }
    for (Agent& a : t.agents) {
        if (!terminalStatus(a.status) && (a.status != "idle" || !a.steps.empty())) {
            a.status = "stopped";
            a.endTs = std::max(end, a.startTs);
        }
        for (Step& s : a.steps) {
            if (s.status != "running" && !s.open) continue;
            s.open = false;
            s.status = "stopped";
            if (s.durationMs < 0 && s.ts > 0.0 &&
                (s.kind == StepKind::Tool || s.kind == StepKind::Spawn ||
                 s.kind == StepKind::Compaction || s.kind == StepKind::Retry))
                s.durationMs = (int)std::max(0.0, (end - s.ts) * 1000.0);
            ++s.rev;
        }
    }
    if (!t.agents.empty()) t.agents[0].endTs = std::max(t.agents[0].endTs, end);
    t.lastEventTs = end;
    t.settled = true;
}

static const char* kindName(StepKind k) {
    static const char* names[] = {"thinking", "reply", "tool", "user", "steer", "spawn",
                                  "subagent_end", "context", "compaction", "retry", "plan",
                                  "note", "error", "run_start", "run_end", "event"};
    const int i = (int)k;
    return (i >= 0 && i < (int)StepKind::Count) ? names[i] : "event";
}
static StepKind kindFromName(const std::string& s) {
    for (int i = 0; i < (int)StepKind::Count; ++i)
        if (s == kindName((StepKind)i)) return (StepKind)i;
    return StepKind::Event;
}

static std::string capText(const std::string& s, size_t n) {
    if (s.size() <= n) return s;
    size_t cut = n;
    while (cut > 0 && ((unsigned char)s[cut] & 0xC0) == 0x80) --cut;
    return s.substr(0, cut);
}

}  // namespace

// Feed one drained orchestrator event into the OWNING conversation's execution trace (GL thread
// only). `convId` is the conversation whose run emitted the event, so concurrent runs in two
// chats never reset or hijack each other's trace.
void feedAgentDagEvent(const std::string& convId, const std::string& kind,
                       const nlohmann::json& data) {
    if (!data.is_object()) return;
    TraceStore& t = s_traces[convId];
    const double now = chatNowEpoch();
    t.settled = false;
    t.lastEventTs = std::max(t.lastEventTs, now);
    const std::string sub = jstr(data, "subagent_id");

    if (kind == "run_started") {
        if (!sub.empty()) {   // a nested agent's own start frame
            const int ai = ensureAgent(t, sub, spanOwner(t, jstr(data, "parent_id")), now);
            Agent& a = t.agents[(size_t)ai];
            a.status = "running";
            if (a.startTs <= 0.0) a.startTs = now;
            const std::string model = jstr(data, "model");
            if (!model.empty()) a.model = model;
            return;
        }
        // A fresh top-level run replaces this conversation's previous trace.
        t.reset(jstr(data, "run_id"));
        Agent& m = t.agents[0];
        m.status = "running";
        m.startTs = now;
        m.model = jstr(data, "model");
        Step& s = pushStep(t, 0, StepKind::RunStart, now);
        s.title = "Run started";
        s.target = m.model;
        return;
    }
    if (t.runId.empty() && sub.empty()) t.runId = jstr(data, "run_id");

    if (kind == "assistant_delta") {
        const int ai = eventOwner(t, data, now);
        appendStream(t, ai, jstr(data, "channel") == "thinking" ? StepKind::Thinking
                                                                : StepKind::Reply,
                     jstr(data, "text"), now);
    } else if (kind == "agent.thought.delta") {
        appendStream(t, eventOwner(t, data, now), StepKind::Thinking, jstr(data, "text"), now);
    } else if (kind == "agent.thought") {
        const int ai = eventOwner(t, data, now);
        Agent& a = t.agents[(size_t)ai];
        const std::string txt = jstr(data, "text");
        if (!a.steps.empty() && a.steps.back().open && a.steps.back().kind == StepKind::Thinking) {
            if (!txt.empty()) a.steps.back().detail = capText(txt, kMaxStepText);
            closeOpen(a);
        } else if (!blankText(txt)) {
            Step& s = pushStep(t, ai, StepKind::Thinking, now);
            s.detail = capText(txt, kMaxStepText);
            ++t.thoughtCount;
        }
    } else if (kind == "status") {
        const std::string phase = jstr(data, "phase");
        if (phase == "tool") {
            std::string id = jstr(data, "tool_call_id");
            if (id.empty()) id = jstr(data, "id");
            const int ai = eventOwner(t, data, now);
            Agent& a = t.agents[(size_t)ai];
            if (Step* s = findSpanStep(a, id, StepKind::Tool)) {
                if (s->title.empty()) { s->title = foldToAtlasGlyphs(jstr(data, "tool_name")); ++s->rev; }
                return;
            }
            Step& s = pushStep(t, ai, StepKind::Tool, now);
            s.spanId = id;
            s.title = foldToAtlasGlyphs(jstr(data, "tool_name"));
            if (s.title.empty()) s.title = "tool";
            s.status = "running";
            ++t.agents[(size_t)ai].toolCount;
            indexSpan(t, id, ai);
        } else if (phase == "error") {
            const int ai = eventOwner(t, data, now);
            Step& s = pushStep(t, ai, StepKind::Error, now);
            s.title = foldToAtlasGlyphs(jstr(data, "text"));
            if (s.title.empty()) s.title = "Agent error";
            s.error = jstr(data, "error_detail");
            if (s.error.empty()) s.error = s.title;
            s.target = jstr(data, "stop_reason");
            s.status = "failed";
        } else if (phase == "notice" || phase == "progress_nudge") {
            const int ai = eventOwner(t, data, now);
            Step& s = pushStep(t, ai, StepKind::Note, now);
            s.title = foldToAtlasGlyphs(jstr(data, "text"));
            s.detail = jstr(data, "text");
        }
    } else if (kind == "agent.tool_call") {
        std::string id = jstr(data, "id");
        if (id.empty()) id = jstr(data, "span_id");
        const int ai = eventOwner(t, data, now);
        Step* s = findSpanStep(t.agents[(size_t)ai], id, StepKind::Tool);
        const bool wasFailed = s && s->status == "failed";
        if (!s) {
            s = &pushStep(t, ai, StepKind::Tool, now);
            s->spanId = id;
            ++t.agents[(size_t)ai].toolCount;
            indexSpan(t, id, ai);
        } else {
            closeOpen(t.agents[(size_t)ai]);
        }
        s->title = foldToAtlasGlyphs(jstr(data, "tool_name"));
        if (s->title.empty()) s->title = "tool";
        s->target = foldToAtlasGlyphs(jstr(data, "target"));
        s->detail = jstr(data, "args_preview");
        if (s->detail.empty() && data.contains("args")) {
            const auto& args = data["args"];
            if (args.is_string()) s->detail = args.get<std::string>();
            else if (args.is_object() || args.is_array()) s->detail = capText(args.dump(2), kPersistText);
        }
        const std::string intent = jstr(data, "intent");
        if (!intent.empty()) s->detail = intent + (s->detail.empty() ? "" : "\n\n") + s->detail;
        s->status = toolStatus(data);
        s->error = s->status == "ok" ? std::string() : jstr(data, "error_preview");
        const int lat = jint(data, "latency_ms", -1);
        s->durationMs = lat > 0 ? lat : (int)std::max(0.0, (now - s->ts) * 1000.0);
        ++s->rev;
        if (s->status == "failed" && !wasFailed) ++t.agents[(size_t)ai].failCount;
    } else if (kind == "agent.spawn") {
        std::string child = sub;
        if (child.empty()) child = jstr(data, "id");
        if (child.empty()) return;
        std::string spawner = jstr(data, "parent_id");
        int pi = spanOwner(t, spawner);
        if (t.agents[(size_t)pi].id == child) pi = 0;
        const int ci = ensureAgent(t, child, pi, now);
        if (ci == 0) return;
        const std::string role = foldToAtlasGlyphs(jstr(data, "role"));
        const std::string brief = foldToAtlasGlyphs(jstr(data, "brief"));
        const std::string assigned = foldToAtlasGlyphs(jstr(data, "name"));
        {
            Agent& c = t.agents[(size_t)ci];
            if (!assigned.empty()) { c.name = assigned; c.named = true; }
            else if (!c.named && genericAgentName(c.name)) {
                if (!brief.empty() && !genericAgentName(brief)) c.name = brief;
                else if (c.name.empty() && !role.empty()) c.name = role;
            }
            if (!brief.empty() && !genericAgentName(brief)) c.brief = brief;
            if (c.status.empty() || c.status == "idle") c.status = "running";
            if (c.startTs <= 0.0) c.startTs = now;
            // A frame that arrived before the spawn parented it under main; the spawn knows better.
            const std::string& pid = t.agents[(size_t)pi].id;
            if (c.parent != pid && !isAncestor(t, child, pid)) c.parent = pid;
        }
        if (findSpanStep(t.agents[(size_t)pi], child, StepKind::Spawn)) return;
        Step& s = pushStep(t, pi, StepKind::Spawn, now);
        s.spanId = child;
        const Agent& childAgent = t.agents[(size_t)ci];
        s.title = "Spawned " + (childAgent.name.empty() ? std::string("sub-agent") : childAgent.name);
        s.target = childAgent.brief;
        s.detail = jstr(data, "brief");
        s.status = "running";
        indexSpan(t, jstr(data, "id"), pi);
    } else if (kind == "sub_run") {
        const std::string status = jstr(data, "status");
        if (!sub.empty()) { subagentLifecycle(t, data, status, now); return; }
        const int ai = eventOwner(t, data, now);
        const std::string id = jstr(data, "id");
        std::string purpose = foldToAtlasGlyphs(jstr(data, "purpose"));
        StepKind k = StepKind::Event;
        if (id.rfind("compaction", 0) == 0 || purpose.rfind("Compact", 0) == 0)
            k = StepKind::Compaction;
        else if (purpose.rfind("Retry", 0) == 0 || purpose.rfind("Fallback", 0) == 0 ||
                 id.rfind("fallback", 0) == 0)
            k = StepKind::Retry;
        Step* s = findSpanStep(t.agents[(size_t)ai], id, k);
        if (!s) {
            s = &pushStep(t, ai, k, now);
            s->spanId = id;
            indexSpan(t, id, ai);
        }
        if (!purpose.empty()) s->title = purpose;
        if (s->title.empty()) s->title = "Sub-run";
        s->status = stepStatusFrom(status.empty() ? std::string("running") : status);
        const std::string detail = jstr(data, "detail");
        if (!detail.empty()) s->detail = capText(detail, kMaxStepText);
        s->error = s->status == "failed" ? s->detail : std::string();
        if (s->status != "running") s->durationMs = (int)std::max(0.0, (now - s->ts) * 1000.0);
        ++s->rev;
    } else if (kind == "run_finished") {
        const std::string raw = jstr(data, "status");
        if (!sub.empty()) { subagentLifecycle(t, data, raw.empty() ? "done" : raw, now); return; }
        const std::string reason = jstr(data, "stop_reason");
        const std::string st = stoppedReason(reason) ? "stopped"
                                  : agentStatusFrom(raw.empty() ? std::string("done") : raw);
        Agent& m = t.agents[0];
        m.status = st == "running" || st == "pending" ? std::string("done") : st;
        m.endTs = now;
        if (m.startTs <= 0.0) m.startTs = now;
        const std::string model = jstr(data, "model");
        if (!model.empty()) m.model = model;
        Step& s = pushStep(t, 0, StepKind::RunEnd, now);
        s.title = m.status == "done" ? "Run finished" : m.status == "failed" ? "Run failed"
                       : m.status == "stopped" ? "Run stopped" : "Run cancelled";
        s.status = m.status == "done" ? "ok" : m.status;
        s.target = jstr(data, "stop_reason");
        std::string err = jstr(data, "error_human");
        const std::string ed = jstr(data, "error_detail");
        if (!ed.empty()) err += (err.empty() ? "" : "\n\n") + ed;
        s.error = err;
        s.durationMs = (int)std::max(0.0, (m.endTs - m.startTs) * 1000.0);
        settleTrace(t);
    } else if (kind == "plan_state") {
        const int ai = eventOwner(t, data, now);
        int total = 0, done = 0;
        auto it = data.find("items");
        if (it != data.end() && it->is_array())
            for (const auto& item : *it) {
                if (!item.is_object()) continue;
                ++total;
                const std::string s = jstr(item, "status");
                if (s == "done" || s == "completed" || s == "complete") ++done;
            }
        Agent& a = t.agents[(size_t)ai];
        Step* s = (!a.steps.empty() && a.steps.back().kind == StepKind::Plan) ? &a.steps.back()
                                                                              : nullptr;
        if (!s) s = &pushStep(t, ai, StepKind::Plan, now);
        s->title = "Plan updated";
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%d/%d done", done, total);
        s->target = buf;
        s->detail = foldToAtlasGlyphs(jstr(data, "summary"));
        ++s->rev;
    } else if (kind == "provider_call") {
        const int ai = eventOwner(t, data, now);
        Step& s = pushStep(t, ai, StepKind::Retry, now);
        const int attempt = jint(data, "attempt", 1), maxA = jint(data, "max_attempts", 0);
        char buf[64];
        if (maxA > 0) std::snprintf(buf, sizeof(buf), "Retry attempt %d/%d", attempt, maxA);
        else          std::snprintf(buf, sizeof(buf), "Retry attempt %d", attempt);
        s.title = buf;
        const std::string prov = jstr(data, "provider"), model = jstr(data, "model");
        s.target = prov + (!prov.empty() && !model.empty() ? " \xC2\xB7 " : "") + model;
        s.error = jstr(data, "error_excerpt");
        s.status = "retry";
        s.durationMs = jint(data, "delay_ms", -1);
    } else if (kind == "agent.context_injected") {
        const int ai = eventOwner(t, data, now);
        Step& s = pushStep(t, ai, StepKind::Context, now);
        s.title = foldToAtlasGlyphs(jstr(data, "label"));
        if (s.title.empty()) s.title = "context";
        s.target = foldToAtlasGlyphs(jstr(data, "source"));
        const int tok = jint(data, "token_est");
        if (tok > 0) s.target += (s.target.empty() ? "~" : "  ~") + std::to_string(tok) + " tok";
        ++t.ctxCount;
    } else if (kind == "agent.advisor") {
        const int ai = eventOwner(t, data, now);
        Step& s = pushStep(t, ai, StepKind::Note, now);
        s.title = "Advisor \xC2\xB7 " + foldToAtlasGlyphs(jstr(data, "advisor_kind"));
        s.detail = jstr(data, "verdict_summary");
        s.target = foldToAtlasGlyphs(s.detail);
    } else if (kind == "agent.message") {
        // Inter-agent traffic: a Note on the SENDER when the trace knows it (Chudcode sub-agent
        // ids are their names), else on the event's owner.
        const std::string from = jstr(data, "from");
        int ai = findAgent(t, from);
        if (ai < 0) ai = eventOwner(t, data, now);
        const std::string to = jstr(data, "to");
        const std::string channel = jstr(data, "channel");
        Step& s = pushStep(t, ai, StepKind::Note, now);
        s.title = foldToAtlasGlyphs(channel == "board" ? "Board post"
                                  : to.empty()         ? "Message"
                                                       : "Message to " + to);
        s.detail = capText(jstr(data, "text"), kMaxStepText);
        s.target = foldToAtlasGlyphs(capText(s.detail.substr(0, s.detail.find('\n')), 160));
    }
}

// Serialize one conversation's trace: every agent and its newest persisted steps. Null when the
// conversation never ran. "nodes" is the flat step list (conversations.cpp saves only when it is
// non-empty).
nlohmann::json agentDagToJson(const std::string& convId) {
    auto sit = s_traces.find(convId);
    if (sit == s_traces.end() || !sit->second.hasContent()) return nlohmann::json();
    const TraceStore& t = sit->second;
    nlohmann::json j;
    j["v"] = 2;
    j["runId"] = t.runId;
    j["uidCtr"] = t.uidCtr;
    j["userSyncTs"] = t.userSyncTs;
    j["userSeeded"] = t.userSeeded;
    j["lastEventTs"] = t.lastEventTs;
    j["thoughtCount"] = t.thoughtCount;
    j["ctxCount"] = t.ctxCount;
    nlohmann::json agents = nlohmann::json::array(), nodes = nlohmann::json::array();
    for (size_t ai = 0; ai < t.agents.size(); ++ai) {
        const Agent& a = t.agents[ai];
        const size_t skip = a.steps.size() > kPersistStepsPerAgent
            ? a.steps.size() - kPersistStepsPerAgent : 0;
        agents.push_back({{"id", a.id}, {"parent", a.parent}, {"name", a.name}, {"named", a.named},
                          {"brief", capText(a.brief, kPersistText)}, {"status", a.status},
                          {"model", a.model}, {"note", capText(a.note, kPersistText)},
                          {"startTs", a.startTs}, {"endTs", a.endTs},
                          {"toolCount", a.toolCount}, {"failCount", a.failCount},
                          {"dropped", a.dropped + (long long)skip}});
        for (size_t i = skip; i < a.steps.size(); ++i) {
            const Step& s = a.steps[i];
            nodes.push_back({{"a", (int)ai}, {"k", kindName(s.kind)}, {"u", s.uid},
                             {"sp", s.spanId}, {"t", capText(s.title, kPersistText)},
                             {"g", capText(s.target, kPersistText)},
                             {"d", capText(s.detail, kPersistText)},
                             {"e", capText(s.error, kPersistText)},
                             {"s", s.status}, {"ms", s.durationMs}, {"ts", s.ts}});
        }
    }
    j["agents"] = std::move(agents);
    j["nodes"] = std::move(nodes);
    return j;
}

// Restore agentDagToJson output. Pre-v2 graph records are ignored: the transcript fallback
// reconstructs those sessions from their persisted activity rows instead.
void agentDagFromJson(const std::string& convId, const nlohmann::json& j) {
    if (!j.is_object() || j.value("v", 0) != 2) return;
    auto ja = j.find("agents");
    if (ja == j.end() || !ja->is_array() || ja->empty()) return;
    TraceStore& t = s_traces[convId];
    t.reset(j.value("runId", std::string()));
    t.agents.clear();
    t.agentIdx.clear();
    for (const auto& x : *ja) {
        if (!x.is_object()) continue;
        Agent a;
        a.id = x.value("id", std::string());
        if (a.id.empty() || t.agentIdx.count(a.id)) continue;
        a.parent = x.value("parent", std::string());
        a.name = x.value("name", std::string());
        a.brief = x.value("brief", std::string());
        a.named = x.value("named", false);
        if (!x.contains("named") && a.id != "main" && genericAgentName(a.name) &&
            readableTaskId(a.id)) {
            if (genericAgentName(a.brief)) a.brief.clear();
            a.name = a.id;
            a.named = true;
        }
        a.status = x.value("status", std::string());
        a.model = x.value("model", std::string());
        a.note = x.value("note", std::string());
        a.startTs = x.value("startTs", 0.0);
        a.endTs = x.value("endTs", 0.0);
        a.toolCount = x.value("toolCount", 0);
        a.failCount = x.value("failCount", 0);
        a.dropped = x.value("dropped", (long long)0);
        t.agentIdx[a.id] = (int)t.agents.size();
        t.agents.push_back(std::move(a));
    }
    if (t.agents.empty() || t.agents[0].id != "main") { t.reset(std::string()); return; }
    unsigned long long maxUid = 0;
    auto jn = j.find("nodes");
    if (jn != j.end() && jn->is_array())
        for (const auto& x : *jn) {
            if (!x.is_object()) continue;
            const int ai = x.value("a", -1);
            if (ai < 0 || ai >= (int)t.agents.size()) continue;
            Step s;
            s.kind = kindFromName(x.value("k", std::string()));
            s.uid = x.value("u", (unsigned long long)0);
            s.spanId = x.value("sp", std::string());
            s.title = x.value("t", std::string());
            if (s.kind == StepKind::Spawn) {
                const int child = findAgent(t, jstr(x, "sp"));
                if (child > 0 && t.agents[(size_t)child].named)
                    s.title = "Spawned " + t.agents[(size_t)child].name;
            }
            s.target = x.value("g", std::string());
            s.detail = x.value("d", std::string());
            s.error = x.value("e", std::string());
            s.status = x.value("s", std::string());
            s.durationMs = x.value("ms", -1);
            s.ts = x.value("ts", 0.0);
            maxUid = std::max(maxUid, s.uid);
            indexSpan(t, s.spanId, ai);
            Agent& a = t.agents[(size_t)ai];
            if (a.steps.size() >= kMaxStepsPerAgent) { a.steps.pop_front(); ++a.dropped; }
            a.steps.push_back(std::move(s));
        }
    t.uidCtr = std::max(j.value("uidCtr", (unsigned long long)1), maxUid + 1);
    t.userSyncTs = j.value("userSyncTs", 0.0);
    t.lastEventTs = j.value("lastEventTs", 0.0);
    t.userSeeded = j.value("userSeeded", false);
    t.thoughtCount = j.value("thoughtCount", 0);
    t.ctxCount = j.value("ctxCount", 0);
}

AgentDagSummary agentDagSummary(const std::string& convId) {
    const auto it = s_traces.find(convId);
    if (it == s_traces.end()) return {};
    const TraceStore& t = it->second;
    AgentDagSummary s;
    s.runId = t.runId;
    for (const Agent& a : t.agents) s.toolCount += a.toolCount;
    s.thoughtCount = t.thoughtCount;
    s.ctxCount = t.ctxCount;
    s.empty = !t.hasContent();
    return s;
}

// ---- transcript fallback + drawing ---------------------------------------------------------------
namespace {

// First line-ish preview: whitespace runs collapsed, capped on a UTF-8 boundary, atlas-folded.
static std::string oneLine(const std::string& s, size_t cap = 280) {
    std::string out;
    out.reserve(std::min(s.size(), cap));
    bool space = false;
    for (char c : s) {
        if (out.size() >= cap) break;
        if (c == '\n' || c == '\r' || c == '\t' || c == ' ') { space = !out.empty(); continue; }
        if (space) { out.push_back(' '); space = false; }
        out.push_back(c);
    }
    return foldToAtlasGlyphs(capText(out, cap));
}

static int wordCount(const std::string& s) {
    int n = 0;
    bool in = false;
    for (char c : s) {
        const bool ws = c == ' ' || c == '\n' || c == '\t' || c == '\r';
        if (!ws && !in) ++n;
        in = !ws;
    }
    return n;
}

// Insert a user prompt / steer into the main timeline at its send time.
static void insertUserStep(TraceStore& t, const ChatMsg& m, StepKind k) {
    Agent& a = t.agents[0];
    if (a.steps.size() >= kMaxStepsPerAgent) { a.steps.pop_front(); ++a.dropped; }
    Step s;
    s.uid = t.uidCtr++;
    s.kind = k;
    s.ts = m.ts;
    s.title = oneLine(m.text);
    s.detail = m.text;
    auto pos = a.steps.end();
    while (pos != a.steps.begin() && std::prev(pos)->ts > m.ts) --pos;
    a.steps.insert(pos, std::move(s));
}

// Prompts and steers are chat messages, not orch events: mirror the ones sent during this run.
// Only while the run is live — a prompt typed after it ended opens the NEXT run (run_started
// resets the trace and re-seeds it as that run's prompt).
static void syncUserMarkers(AppState& st, const std::string& convId, TraceStore& t) {
    const std::vector<ChatMsg>* msgs = conversationMsgs(st, convId);
    if (!msgs || msgs->empty()) return;
    const double runStart = t.agents[0].startTs;
    if (!t.userSeeded) {
        t.userSeeded = true;
        const ChatMsg* seed = nullptr;
        for (const ChatMsg& m : *msgs)
            if (m.fromUser && m.ts > 0.0 && (runStart <= 0.0 || m.ts <= runStart + 2.0)) seed = &m;
        if (seed && !blankText(seed->text)) insertUserStep(t, *seed, StepKind::User);
        t.userSyncTs = seed ? seed->ts : runStart;
        if (t.userSyncTs <= 0.0) t.userSyncTs = chatNowEpoch();
    }
    size_t first = msgs->size();
    if (!st.convRunning(convId)) return;
    while (first > 0 && (*msgs)[first - 1].ts > t.userSyncTs) --first;
    for (size_t i = first; i < msgs->size(); ++i) {
        const ChatMsg& m = (*msgs)[i];
        if (!m.fromUser || blankText(m.text)) continue;
        insertUserStep(t, m, StepKind::Steer);
        t.userSyncTs = std::max(t.userSyncTs, m.ts);
    }
}

static std::string rowStatus(const ActivityItem& a) {
    if (a.status == "running") return "running";
    return stepStatusFrom(a.status);
}

// Rebuild a trace from the conversation's persisted activity rows (restored sessions).
static void buildFallback(const std::vector<ChatMsg>& msgs, TraceStore& t) {
    const std::string keepSel = t.selAgent;
    t.reset(std::string());
    double first = 0.0, last = 0.0;
    bool running = false, failed = false;
    int delegate = 0;
    for (const ChatMsg& m : msgs) {
        if (m.ts > 0.0) {
            first = first > 0.0 ? std::min(first, m.ts) : m.ts;
            last = std::max(last, m.ts);
        }
        if (m.fromUser) {
            if (blankText(m.text)) continue;
            Step& s = pushStep(t, 0, StepKind::User, m.ts);
            s.title = oneLine(m.text);
            s.detail = m.text;
            continue;
        }
        running = running || m.running;
        for (const ActivityItem& a : m.activity) {
            switch (a.rowKind) {
            case 2: {
                const bool prose = a.stage == "stream:out";
                if (blankText(a.detail)) break;
                Step& s = pushStep(t, 0, prose ? StepKind::Reply : StepKind::Thinking, m.ts);
                s.detail = capText(a.detail, kMaxStepText);
                if (prose && a.opsCount > 0) s.title = "Building ops";
                if (a.streaming && m.running) { s.open = true; s.status = "running"; }
                if (!prose) ++t.thoughtCount;
                break;
            }
            case 3: {
                const ToolRowDetail d = splitToolRowDetail(a.detail);
                Step& s = pushStep(t, 0, StepKind::Tool, d.ts > 0.0 ? d.ts : m.ts);
                s.title = a.title.empty() ? std::string("tool") : a.title;
                s.target = a.reason;
                s.detail = d.args;
                s.error = d.err;
                s.status = rowStatus(a);
                if (s.status == "running" && !m.running) s.status = "stopped";
                s.durationMs = a.durationMs > 0 ? a.durationMs : -1;
                ++t.agents[0].toolCount;
                if (s.status == "failed") ++t.agents[0].failCount;
                if (d.ts > 0.0) last = std::max(last, d.ts);
                break;
            }
            case 1: {
                Step& s = pushStep(t, 0, StepKind::Context, m.ts);
                s.title = a.title;
                s.target = a.reason;
                if (a.ctxTokens > 0)
                    s.target += (s.target.empty() ? "~" : "  ~") + std::to_string(a.ctxTokens) + " tok";
                ++t.ctxCount;
                break;
            }
            case 9: {
                if (a.status != "fail") break;   // only failed provider attempts are steps
                Step& s = pushStep(t, 0, StepKind::Retry, m.ts);
                s.title = a.title;
                s.error = a.reason;
                s.detail = a.detail;
                s.status = "retry";
                s.durationMs = a.durationMs > 0 ? a.durationMs : -1;
                break;
            }
            case 17: {
                StepKind k = StepKind::Event;
                if (a.title.rfind("Compact", 0) == 0) k = StepKind::Compaction;
                else if (a.title.rfind("Retry", 0) == 0 || a.title.rfind("Fallback", 0) == 0)
                    k = StepKind::Retry;
                Step& s = pushStep(t, 0, k, m.ts);
                s.title = a.title;
                s.target = a.reason;
                s.status = rowStatus(a);
                if (s.status == "running" && !m.running) s.status = "stopped";
                if (s.status == "failed") s.error = a.detail;
                break;
            }
            case 19: {
                const nlohmann::json meta = nlohmann::json::parse(a.detail, nullptr, false);
                if (!meta.is_object() || !meta.contains("delegates") ||
                    !meta["delegates"].is_array())
                    break;
                for (const auto& dlg : meta["delegates"]) {
                    if (!dlg.is_object()) continue;
                    const std::string id = "transcript:" + std::to_string(delegate++);
                    const int ci = ensureAgent(t, id, 0, m.ts);
                    if (ci == 0) break;
                    Agent& c = t.agents[(size_t)ci];
                    c.name = jstr(dlg, "name");
                    if (c.name.empty()) c.name = jstr(dlg, "title");
                    c.named = !c.name.empty();
                    c.brief = jstr(dlg, "brief");
                    if (c.name.empty()) c.name = !c.brief.empty() && !genericAgentName(c.brief)
                        ? c.brief : jstr(dlg, "role");
                    // Dispatch is not proof of completion. Only a terminal lifecycle event can
                    // say "done"; an ended transcript without one is an interrupted delegate.
                    c.status = m.running ? "running" : "stopped";
                    Step& s = pushStep(t, 0, StepKind::Spawn, m.ts);
                    s.spanId = id;
                    s.title = "Spawned " + (c.name.empty() ? std::string("sub-agent") : c.name);
                    s.target = c.brief;
                    s.status = m.running ? "running" : "stopped";
                }
                break;
            }
            default:
                if (a.stage == "__stop_meta__" && (!a.detail.empty() || !a.title.empty())) {
                    const bool stopped = stoppedReason(a.title);
                    Step& s = pushStep(t, 0, stopped ? StepKind::RunEnd : StepKind::Error, m.ts);
                    s.title = a.title.empty() ? std::string("Stopped") : "Stopped: " + a.title;
                    if (stopped) s.detail = a.detail;
                    else s.error = a.detail;
                    s.status = stopped ? "stopped" : "failed";
                }
                break;
            }
        }
        if (!timelineHasInlineProse(m) && !blankText(m.text)) {
            Step& s = pushStep(t, 0, StepKind::Reply, m.ts);
            s.detail = capText(m.text, kMaxStepText);
        }
        failed = m.errKind != ERR_NONE && m.errKind != ERR_PARTIAL;
        const bool stopped = turnWasStopped(m);
        if (stopped) failed = false;
        if (failed) {
            Step& s = pushStep(t, 0, StepKind::Error, m.ts);
            s.title = m.errorHuman.empty() ? std::string("Run failed") : m.errorHuman;
            s.error = m.errorFull.empty() ? m.text : m.errorFull;
            s.status = "failed";
        }
    }
    for (Agent& a : t.agents) if (!running) closeOpen(a);
    Agent& main = t.agents[0];
    const ChatMsg* latest = nullptr;
    for (auto it = msgs.rbegin(); it != msgs.rend(); ++it)
        if (!it->fromUser) { latest = &*it; break; }
    const bool stopped = latest && turnWasStopped(*latest);
    main.status = running ? "running" : stopped ? "stopped" : failed ? "failed"
                                                    : main.steps.empty() ? "idle" : "done";
    main.startTs = first;
    main.endTs = running ? 0.0 : last;
    if (!running) settleTrace(t);
    t.selAgent = findAgent(t, keepSel) >= 0 ? keepSel : std::string("main");
}

static size_t transcriptSig(const std::vector<ChatMsg>& msgs) {
    size_t h = msgs.size();
    for (const ChatMsg& m : msgs)
        h = h * 1315423911u ^ (m.activity.size() * 131u + m.text.size() * 7u +
                               (m.running ? 3u : 0u) + (size_t)m.errKind);
    return h;
}

// The trace to show: live events when any were recorded, else the transcript reconstruction.
static TraceStore* activeTrace(AppState& st, const std::string& convId, bool* fromTranscript) {
    *fromTranscript = false;
    auto it = s_traces.find(convId);
    if (it != s_traces.end() && it->second.hasContent()) {
        if (!st.convRunning(convId)) settleTrace(it->second);
        return &it->second;
    }
    const std::vector<ChatMsg>* msgs = conversationMsgs(st, convId);
    if (!msgs || msgs->empty()) return nullptr;
    FallbackTrace& fb = s_fallback[convId];
    const size_t sig = transcriptSig(*msgs);
    const double now = ImGui::GetTime();
    const bool live = !msgs->empty() && msgs->back().running;
    if (!fb.built || fb.sig != sig || (live && now - fb.builtAt > 1.0)) {
        buildFallback(*msgs, fb.store);
        fb.sig = sig;
        fb.builtAt = now;
        fb.built = true;
    }
    if (!fb.store.hasContent()) return nullptr;
    if (!st.convRunning(convId)) settleTrace(fb.store);
    *fromTranscript = true;
    return &fb.store;
}

static const char* kindLabel(StepKind k) {
    switch (k) {
        case StepKind::Thinking:    return "Thinking";
        case StepKind::Reply:       return "Reply";
        case StepKind::Tool:        return "Tool";
        case StepKind::User:        return "Prompt";
        case StepKind::Steer:       return "Steer";
        case StepKind::Spawn:
        case StepKind::SubagentEnd: return "Sub-agent";
        case StepKind::Context:     return "Context";
        case StepKind::Compaction:  return "Compaction";
        case StepKind::Retry:       return "Retry";
        case StepKind::Plan:        return "Plan";
        case StepKind::Note:        return "Note";
        case StepKind::Error:       return "Error";
        case StepKind::RunStart:
        case StepKind::RunEnd:      return "Run";
        default:                    return "Event";
    }
}

static kit::Tone kindTone(const Step& s) {
    if (s.status == "failed") return kit::Tone::Err;
    switch (s.kind) {
        case StepKind::Tool:        return kit::Tone::Accent;
        case StepKind::Thinking:
        case StepKind::Plan:
        case StepKind::Spawn:
        case StepKind::SubagentEnd: return kit::Tone::Info;
        case StepKind::User:
        case StepKind::Steer:       return kit::Tone::Accent;
        case StepKind::Compaction:
        case StepKind::Retry:       return kit::Tone::Warn;
        case StepKind::Error:       return kit::Tone::Err;
        case StepKind::RunEnd:      return s.status == "ok" ? kit::Tone::Ok : kit::Tone::Neutral;
        default:                    return kit::Tone::Neutral;
    }
}

// A status glyph for steps that have an outcome; text blocks only show one while streaming.
static bool stepGlyph(const Step& s, kit::Status* out) {
    const std::string& v = s.status;
    if (v.empty()) return false;
    if (v == "running")   *out = kit::Status::Running;
    else if (v == "ok")   *out = kit::Status::Done;
    else if (v == "failed") *out = kit::Status::Failed;
    else if (v == "retry")  *out = kit::Status::Retry;
    else                  *out = kit::Status::Skipped;   // skipped / stopped / cancelled
    return true;
}

static kit::Status agentGlyph(const std::string& status) {
    if (status == "running") return kit::Status::Running;
    if (status == "done")    return kit::Status::Done;
    if (status == "failed")  return kit::Status::Failed;
    if (status == "pending" || status == "idle") return kit::Status::Pending;
    return kit::Status::Skipped;   // cancelled / stopped
}


static std::string stepDuration(const Step& s, double now) {
    if (s.status == "running" && s.ts > 0.0 &&
        (s.kind == StepKind::Tool || s.kind == StepKind::Spawn || s.kind == StepKind::Compaction))
        return kit::fmtDuration(now - s.ts);
    if (s.durationMs < 0) return std::string();
    return kit::fmtDuration(s.durationMs / 1000.0);
}

static std::string clockTime(double ts) {
    if (ts <= 0.0) return std::string();
    const time_t tt = (time_t)ts;
    std::tm tm{};
#ifdef _WIN32
    localtime_s(&tm, &tt);
#else
    localtime_r(&tt, &tm);
#endif
    char buf[16];
    std::strftime(buf, sizeof(buf), "%H:%M:%S", &tm);
    return buf;
}

static std::string agentLabel(const Agent& a) {
    if (!a.name.empty()) return a.name;
    return a.id == "main" ? std::string("Main agent") : std::string("Sub-agent");
}

// Display status: any agent still marked running after its run ended was interrupted (no event
// can arrive for it once the run's stream is gone).
static std::string agentDisplayStatus(const Agent& a, bool convRunning, bool fromTranscript) {
    (void)fromTranscript;
    if (!convRunning && (a.status == "running" || a.status == "pending")) return "stopped";
    if (a.status == "idle" && !a.steps.empty()) return "done";
    return a.status.empty() ? std::string("idle") : a.status;
}

// End of an agent's elapsed span for display. A running agent counts to now. A finished one uses
// its recorded endTs; an interrupted agent never received one (its stream died mid-run), so its
// span ends at the last thing it did — never at "now", which grew a stopped run to days.
// Returns 0 when nothing is known, so callers omit the elapsed figure.
static double agentEndTs(const Agent& a, const std::string& displayStatus) {
    if (displayStatus == "running") return chatNowEpoch();
    if (a.endTs > 0.0) return a.endTs;
    double last = 0.0;
    for (const Step& s : a.steps) {
        if (s.ts <= 0.0) continue;
        last = std::max(last, s.ts + (s.durationMs > 0 ? s.durationMs / 1000.0 : 0.0));
    }
    return last;
}

// Tool reasons and argument previews arrive as prose with a JSON object appended
// ('Collecting cancelled convergence status {"job_id":"jo…'), often cut mid-object. A step row
// must read as text, so every flat "key": value pair becomes "key value" (underscores spaced),
// joined with middots after the prose. Nested objects/arrays contribute their own flat pairs.
// Text without a '{' is returned unchanged.
static std::string humanizeInlineJson(const std::string& in) {
    const size_t brace = in.find('{');
    if (brace == std::string::npos) return in;
    std::string out = in.substr(0, brace);
    while (!out.empty() && (out.back() == ' ' || out.back() == ':' || out.back() == '-'))
        out.pop_back();
    const size_t n = in.size();
    size_t i = brace;
    int pairs = 0;
    while (i < n && pairs < 4) {
        const size_t kq = in.find('"', i);
        if (kq == std::string::npos) break;
        const size_t ke = in.find('"', kq + 1);
        if (ke == std::string::npos) break;
        size_t c = ke + 1;
        while (c < n && in[c] == ' ') ++c;
        if (c >= n || in[c] != ':') { i = ke + 1; continue; }
        ++c;
        while (c < n && in[c] == ' ') ++c;
        std::string val;
        if (c < n && in[c] == '"') {
            size_t ve = c + 1;
            while (ve < n && in[ve] != '"') ve += (in[ve] == '\\' && ve + 1 < n) ? 2 : 1;
            val = in.substr(c + 1, std::min(ve, n) - c - 1);
            i = ve + 1;
        } else if (c < n && (in[c] == '{' || in[c] == '[')) {
            i = c + 1;   // descend: the nested object's own pairs follow
            continue;
        } else {
            size_t ve = c;
            while (ve < n && in[ve] != ',' && in[ve] != '}' && in[ve] != ']') ++ve;
            val = in.substr(c, ve - c);
            while (!val.empty() && val.back() == ' ') val.pop_back();
            i = ve;
        }
        std::string key = in.substr(kq + 1, ke - kq - 1);
        for (char& ch : key) if (ch == '_') ch = ' ';
        if (key.empty() || val.empty()) continue;
        if (!out.empty()) out += " \xC2\xB7 ";
        out += key + " " + val;
        ++pairs;
    }
    return out.empty() ? in : out;
}

static AgentTraceStep stepView(const std::string& convId, const Agent& a, const Step& s) {
    AgentTraceStep v;
    v.uid = s.uid;
    v.rev = s.rev;
    v.convId = convId;
    v.agent = agentLabel(a);
    v.kind = s.kind == StepKind::Tool ? "Tool call" : kindLabel(s.kind);
    v.title = s.title;
    if (v.title.empty() && (s.kind == StepKind::Thinking || s.kind == StepKind::Reply))
        v.title = oneLine(s.detail, 120);
    v.target = humanizeInlineJson(s.target);   // full arguments stay raw under "Arguments"
    v.detail = foldToAtlasGlyphs(s.detail);
    v.error = foldToAtlasGlyphs(s.error);
    v.status = s.status;
    v.durationMs = s.durationMs;
    v.ts = s.ts;
    return v;
}

static ImU32 inkU32(const ImVec4& c) { return ImGui::ColorConvertFloat4ToU32(c); }

// Pre-order walk of the agent tree: (agent index, depth). Unknown parents attach to main.
static void treeOrder(const TraceStore& t, std::vector<std::pair<int, int>>& out) {
    out.clear();
    std::vector<std::vector<int>> kids(t.agents.size());
    for (size_t i = 1; i < t.agents.size(); ++i) {
        int p = findAgent(t, t.agents[i].parent);
        if (p < 0 || p == (int)i) p = 0;
        kids[(size_t)p].push_back((int)i);
    }
    std::vector<char> seen(t.agents.size(), 0);
    std::vector<std::pair<int, int>> stack{{0, 0}};
    while (!stack.empty()) {
        const auto cur = stack.back();
        stack.pop_back();
        if (seen[(size_t)cur.first]) continue;
        seen[(size_t)cur.first] = 1;
        out.push_back(cur);
        const auto& k = kids[(size_t)cur.first];
        for (auto it = k.rbegin(); it != k.rend(); ++it) stack.push_back({*it, cur.second + 1});
    }
    // Anything left out by a malformed parent cycle still gets a row.
    for (size_t i = 0; i < t.agents.size(); ++i)
        if (!seen[i]) out.push_back({(int)i, 1});
}

static void drawAgentRow(AppState& st, TraceStore& t, int ai, int depth, bool convRunning,
                         bool fromTranscript, const AdaptiveSurface& surf,
                         AgentTraceSelectAgentFn onAgent) {
    const Agent& a = t.agents[(size_t)ai];
    const float lineH = ImGui::GetTextLineHeight();
    const float h = lineH + kit::padY() * 2.0f;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float w = std::max(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    ImGui::PushID(ai);
    const bool clicked = kit::rowBegin(a.id.c_str(), h, t.selAgent == a.id);
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float gap = ui::metrics::U(1.5f);
    float x = p0.x + kit::padX() + depth * ui::metrics::U(4.0f);
    const float y = p0.y + kit::padY();
    const float right = p0.x + w - kit::padX();

    const std::string status = agentDisplayStatus(a, convRunning, fromTranscript);
    kit::statusGlyph(dl, ImVec2(x + lineH * 0.5f, y + lineH * 0.5f), lineH * 0.30f,
                     agentGlyph(status), surf);
    x += lineH + gap;

    const double end = agentEndTs(a, status);
    std::string meta;
    if (a.startTs > 0.0 && end >= a.startTs)
        meta += "  \xE2\x80\xA2  " + kit::fmtDuration(end - a.startTs);
    meta += "  \xE2\x80\xA2  " + std::to_string(a.toolCount) +
            (a.toolCount == 1 ? " tool" : " tools");
    std::string failTxt;
    if (a.failCount > 0) failTxt = "  \xE2\x80\xA2  " + std::to_string(a.failCount) + " failed";
    const std::string statusTxt = "  \xE2\x80\xA2  " + status;

    const float wStatus = ImGui::CalcTextSize(statusTxt.c_str()).x;
    const float wMeta = ImGui::CalcTextSize(meta.c_str()).x;
    const float wFail = ImGui::CalcTextSize(failTxt.c_str()).x;
    const std::string name = kit::elide(agentLabel(a).c_str(),
        std::max(right - x - wStatus - wMeta - wFail, ui::metrics::U(6.0f)));
    dl->AddText(ImVec2(x, y), inkU32(surf.textPrimary), name.c_str());
    x += ImGui::CalcTextSize(name.c_str()).x;
    const kit::Tone tone = kit::statusTone(agentGlyph(status));
    dl->AddText(ImVec2(x, y), inkU32(kit::toneOn(tone, surf)), statusTxt.c_str());
    x += wStatus;
    if (x + wMeta <= right) {
        dl->AddText(ImVec2(x, y), inkU32(surf.textSecondary), meta.c_str());
        x += wMeta;
    }
    if (!failTxt.empty() && x + wFail <= right)
        dl->AddText(ImVec2(x, y), inkU32(kit::toneOn(kit::Tone::Err, surf)), failTxt.c_str());
    kit::rowEnd();
    ImGui::PopID();

    if (hovered) {
        std::string tip = agentLabel(a);
        if (!a.brief.empty()) tip += "\n" + a.brief;
        if (!a.model.empty()) tip += "\nModel: " + a.model;
        tip += ai == 0 ? "\nClick to inspect the main agent and its steps"
                       : "\nClick to show its steps and inspect it";
        ImGui::SetTooltip("%s", tip.c_str());
    }
    if (clicked) {
        if (t.selAgent != a.id) chatLogJumpToBottom(kTimelineRegion);
        t.selAgent = a.id;
        if (onAgent) onAgent(st, a.id, a.name);
    }
}

static void drawStepRow(AppState& st, const std::string& convId, const Agent& a, const Step& s,
                        float h, bool selected, const AdaptiveSurface& surf,
                        AgentTraceSelectAgentFn onAgent, AgentTraceSelectStepFn onStep) {
    const float lineH = ImGui::GetTextLineHeight();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float w = std::max(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    char id[32];
    std::snprintf(id, sizeof(id), "s%llu", s.uid);
    const bool clicked = kit::rowBegin(id, h, selected);
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float gap = ui::metrics::U(1.5f);
    const float x0 = p0.x + kit::padX();
    const float y1 = p0.y + kit::padY();
    const float y2 = y1 + lineH + ui::metrics::U(0.5f);
    const float right = p0.x + w - kit::padX();
    const double now = chatNowEpoch();

    kit::Status gs;
    if (stepGlyph(s, &gs))
        kit::statusGlyph(dl, ImVec2(x0 + lineH * 0.5f, y1 + lineH * 0.5f), lineH * 0.30f, gs, surf);
    float x = x0 + lineH + gap;

    // Line 1: kind label, title, duration.
    const kit::Tone tone = kindTone(s);
    const char* label = kindLabel(s.kind);
    dl->AddText(ImVec2(x, y1), inkU32(kit::toneOn(tone, surf)), label);
    x += ImGui::CalcTextSize(label).x + gap * 2.0f;
    const std::string dur = stepDuration(s, now);
    float titleRight = right;
    if (!dur.empty()) {
        const float dw = ImGui::CalcTextSize(dur.c_str()).x;
        dl->AddText(ImVec2(right - dw, y1), inkU32(surf.textSecondary), dur.c_str());
        titleRight = right - dw - gap * 2.0f;
    }
    const bool textKind = s.kind == StepKind::Thinking || s.kind == StepKind::Reply ||
                          s.kind == StepKind::User || s.kind == StepKind::Steer;
    std::string title = s.title;
    if (title.empty() && textKind) title = oneLine(s.detail);
    const std::string t1 = kit::elide(title.c_str(), std::max(titleRight - x, gap));
    dl->AddText(ImVec2(x, y1), inkU32(surf.textPrimary), t1.c_str());

    // Line 2: error / target / preview, clock time on the right.
    const std::string clock = clockTime(s.ts);
    float secondRight = right;
    if (!clock.empty()) {
        const float cw = ImGui::CalcTextSize(clock.c_str()).x;
        dl->AddText(ImVec2(right - cw, y2), inkU32(surf.textTertiary), clock.c_str());
        secondRight = right - cw - gap * 2.0f;
    }
    std::string second;
    ImVec4 secondInk = surf.textSecondary;
    if (!s.error.empty() && s.status != "ok") {
        second = oneLine(s.error);
        secondInk = kit::toneOn(kit::Tone::Err, surf);
    } else if (textKind) {
        const int words = wordCount(s.detail);
        second = std::to_string(words) + (words == 1 ? " word" : " words");
        if (s.open) second += " \xC2\xB7 streaming";
        if (s.kind == StepKind::User || s.kind == StepKind::Steer) second = "You \xC2\xB7 " + second;
    } else if (!s.target.empty()) {
        second = humanizeInlineJson(oneLine(s.target));
    } else if (!s.detail.empty()) {
        second = humanizeInlineJson(oneLine(s.detail));
    } else if (s.status == "running") {
        second = "in progress";
    }
    const float x2 = x0 + lineH + gap;
    if (!second.empty()) {
        const std::string t2 = kit::elide(second.c_str(), std::max(secondRight - x2, gap));
        dl->AddText(ImVec2(x2, y2), inkU32(secondInk), t2.c_str());
    }
    kit::rowEnd();
    if (hovered) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (t1 != title) ImGui::SetTooltip("%s", title.c_str());
    }
    if (clicked && onStep) {
        if (onAgent) onAgent(st, a.id, a.name);
        onStep(st, stepView(convId, a, s));
    }
}

}  // namespace

void drawAgentTrace(AppState& st, const std::string& convId, AgentTraceSelectAgentFn onAgent,
                    AgentTraceSelectStepFn onStep, unsigned long long selectedStepUid) {
    bool fromTranscript = false;
    TraceStore* tp = activeTrace(st, convId, &fromTranscript);
    const bool convRunning = st.convRunning(convId);
    if (!tp) {
        kit::emptyState("activity", convRunning ? "Agent is starting" : "No steps yet",
                        "The main agent, its sub-agents and every step they take appear here "
                        "as the run streams.");
        return;
    }
    TraceStore& t = *tp;
    if (!fromTranscript) syncUserMarkers(st, convId, t);
    int sel = findAgent(t, t.selAgent);
    if (sel < 0) { t.selAgent = "main"; sel = 0; }

    // ---- agent tree ----
    kit::sectionHeader("Agents", nullptr, "Select an agent to inspect its steps");
    static std::vector<std::pair<int, int>> order;
    treeOrder(t, order);
    const float spacingY = ImGui::GetStyle().ItemSpacing.y;
    const float agentRowH = ImGui::GetTextLineHeight() + kit::padY() * 2.0f;
    const float avail = ImGui::GetContentRegionAvail().y;
    const float pitch = agentRowH + spacingY;
    const float padding = ImGui::GetStyle().WindowPadding.y * 2.0f;
    const float treeContent = order.size() * pitch - spacingY + padding;
    // Account for the steps header, both child gaps and navigation footer before spending
    // height on the tree. Small trees used to bypass the budget entirely; the two-row minimum
    // on larger trees could also push the timeline/footer out of a short inspector.
    const float stepH = ImGui::GetTextLineHeight() * 2.0f + ui::metrics::U(0.5f) +
                        kit::padY() * 2.0f;
    const float stepsHeaderH = kit::sectionGap() + ImGui::GetTextLineHeight() +
                              kit::headGap() + spacingY * 3.0f;
    const float timelineMinH = stepH * 2.0f + spacingY + padding;
    const float treeLimit = std::max(ui::metrics::stroke(1.0f),
        avail - stepsHeaderH - chatJumpToLatestFooterHeight() - spacingY * 2.0f - timelineMinH);
    const float treeBudget = std::min(treeLimit, order.size() <= 5 ? treeContent
        : std::max(pitch * 2.0f + padding, std::min(avail * 0.5f, treeContent)));
    const int visibleRows = std::max(1, (int)std::floor((treeBudget - padding + spacingY) / pitch));
    const float treeH = std::min(treeBudget,
        std::min(treeContent, visibleRows * pitch - spacingY + padding));
    ImGui::BeginChild("##trace_tree", ImVec2(0, treeH), ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar);
    {
        const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
        const AdaptiveSurface& surf = adaptivePanel(panelId("ai_ws_trace_tree"),
            ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), GlassGroup::Global);
        for (const auto& [ai, depth] : order)
            drawAgentRow(st, t, ai, depth, convRunning, fromTranscript, surf, onAgent);
        drawWindowPillScrollbarV(surf);
    }
    ImGui::EndChild();

    // ---- selected agent's step timeline ----
    sel = std::max(findAgent(t, t.selAgent), 0);
    const Agent& a = t.agents[(size_t)sel];
    const std::string header = "Steps \xC2\xB7 " + agentLabel(a);
    kit::sectionHeader(header.c_str(), nullptr,
                       fromTranscript ? "from saved transcript" : nullptr,
                       (int)std::min<long long>(a.dropped + (long long)a.steps.size(), 1 << 30));
    const float timelineH = std::max(ui::metrics::stroke(1.0f),
        ImGui::GetContentRegionAvail().y - chatJumpToLatestFooterHeight() - spacingY);
    ImGui::BeginChild("##trace_timeline", ImVec2(0, timelineH), ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar);
    const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
    const AdaptiveSurface& surf = adaptivePanel(panelId("ai_ws_trace_timeline"),
        ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), GlassGroup::Global);
    const float lineH = ImGui::GetTextLineHeight();
    const int offset = a.dropped > 0 ? 1 : 0;
    const int count = (int)a.steps.size() + offset;
    if (count == 0) {
        kit::dimText("No steps recorded for this agent yet.");
    } else {
        ImGuiListClipper clip;
        clip.Begin(count, stepH + spacingY);
        while (clip.Step()) {
            for (int i = clip.DisplayStart; i < clip.DisplayEnd; ++i) {
                if (i < offset) {
                    const std::string earlier = kit::fmtCount(a.dropped) +
                        (a.dropped == 1 ? " earlier step" : " earlier steps") +
                        " not shown (oldest dropped)";
                    kit::rowBegin("##earlier", stepH, false);
                    ImGui::GetWindowDrawList()->AddText(
                        ImVec2(ImGui::GetCursorScreenPos().x + lineH + ui::metrics::U(1.5f),
                               ImGui::GetCursorScreenPos().y + lineH * 0.5f),
                        inkU32(surf.textTertiary), earlier.c_str());
                    kit::rowEnd();
                    continue;
                }
                const Step& s = a.steps[(size_t)(i - offset)];
                drawStepRow(st, convId, a, s, stepH, s.uid == selectedStepUid, surf, onAgent, onStep);
            }
        }
        clip.End();
        // Tail pad so the bottom-pinned scroll (chatLogStickToBottom clamps to ScrollMax) is a
        // whole number of rows: ScrollMax = rows + tail + 2*padY - viewH, so tail =
        // (viewH - 2*padY) mod pitch puts the top visible row flush under the header instead of
        // half-cut. Only while the list actually scrolls; a list that fits needs no tail.
        const float pitch = stepH + spacingY;
        const float padY = ImGui::GetStyle().WindowPadding.y;
        if (count * pitch - spacingY + padY * 2.0f > ws.y)
            ImGui::Dummy(ImVec2(1.0f, std::fmod(std::max(ws.y - padY * 2.0f, 0.0f), pitch)));
    }
    chatLogStickToBottom(kTimelineRegion);
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
    chatDrawJumpToLatest(kTimelineRegion);
}

bool agentTraceRefreshStep(AppState& st, AgentTraceStep& io) {
    if (io.uid == 0) return false;
    bool fromTranscript = false;
    TraceStore* t = activeTrace(st, io.convId, &fromTranscript);
    if (!t) return false;
    for (const Agent& a : t->agents) {
        // uids grow along each deque except around inserted user markers: scan from the back.
        for (auto it = a.steps.rbegin(); it != a.steps.rend(); ++it) {
            if (it->uid != io.uid) continue;
            if (it->rev != io.rev) io = stepView(io.convId, a, *it);
            return true;
        }
    }
    return false;
}

void agentTraceSubagents(AppState& st, const std::string& convId,
                         std::vector<AgentTraceAgent>& out) {
    out.clear();
    bool fromTranscript = false;
    TraceStore* t = activeTrace(st, convId, &fromTranscript);
    if (!t || t->agents.size() < 2) return;
    const bool convRunning = st.convRunning(convId);
    static std::vector<std::pair<int, int>> order;
    treeOrder(*t, order);
    out.reserve(order.size());
    for (const auto& entry : order) {
        if (entry.first == 0) continue;
        const Agent& a = t->agents[(size_t)entry.first];
        AgentTraceAgent v;
        v.id = a.id;
        v.parent = a.parent;
        v.name = agentLabel(a);
        v.brief = a.brief;
        v.model = a.model;
        v.note = a.note;
        v.status = agentDisplayStatus(a, convRunning, fromTranscript);
        v.startTs = a.startTs;
        v.endTs = v.status == "running" ? a.endTs : agentEndTs(a, v.status);
        v.toolCount = a.toolCount;
        v.failCount = a.failCount;
        v.depth = std::max(1, entry.second);
        if (!a.steps.empty()) {
            const Step& s = a.steps.back();
            std::string title = s.title;
            if (title.empty()) title = humanizeInlineJson(oneLine(s.detail, 120));
            v.lastStep = s.kind == StepKind::Tool ? std::string("Tool call") : kindLabel(s.kind);
            if (!title.empty()) v.lastStep += " \xC2\xB7 " + title;
            v.lastStepTs = s.ts;
        }
        out.push_back(std::move(v));
    }
}

bool agentTraceSelectAgent(AppState& st, const std::string& convId, const std::string& agentId) {
    bool fromTranscript = false;
    TraceStore* t = activeTrace(st, convId, &fromTranscript);
    if (!t || findAgent(*t, agentId) < 0) return false;
    if (t->selAgent != agentId) chatLogJumpToBottom(kTimelineRegion);
    t->selAgent = agentId;
    return true;
}

void agentTraceRecentSteps(AppState& st, const std::string& convId, const std::string& agentId,
                           size_t maxSteps, std::vector<AgentTraceStep>& out) {
    out.clear();
    bool fromTranscript = false;
    TraceStore* t = activeTrace(st, convId, &fromTranscript);
    if (!t || maxSteps == 0) return;
    const int ai = findAgent(*t, agentId);
    if (ai < 0) return;
    const Agent& a = t->agents[(size_t)ai];
    const size_t n = std::min(maxSteps, a.steps.size());
    out.reserve(n);
    for (size_t i = a.steps.size() - n; i < a.steps.size(); ++i)
        out.push_back(stepView(convId, a, a.steps[i]));
}

}  // namespace ui
