// Copilot activity feed: feedTurnActivity — folds streamed
// orchestrator events (stage/agent/plan/mission/corporate kinds) into a turn's ActivityItem
// timeline, plus the stream:out ops-JSON detection and the chat-history dirty flag the
// persistence debounce (conversations.cpp) reads. Pure state folding, no drawing.

#include "copilot_internal.h"
#include "agent_text.h"
#include "stream_rows.h"
#include "tool_outcome.h"

// ============================ transparent activity timeline ============================
// Map a raw orchestrator stage key to a plain-language heading the user can follow.
static std::string friendlyStageTitle(const std::string& stage) {
    if (stage == "intake")  return "Understanding the request";
    if (stage == "plan")    return "Planning the build";
    if (stage == "model")   return "Building the model";
    if (stage == "edit")    return "Editing the model";
    if (stage == "review")  return "Reviewing the result";
    if (stage == "fix")     return "Fixing issues";
    if (stage == "memory")  return "Naming part";
    std::string t = stage;            // Title-case fallback for any unknown stage
    if (!t.empty()) t[0] = (char)std::toupper((unsigned char)t[0]);
    return t.empty() ? std::string("\xe2\x80\xa6") : t;  // ellipsis — never a fake "Working"
}

// ============================ Corporate Roles (aicad.corporate/1) ============================
// Additive kind="corporate" events (corp_event ∈ org_built|hire|team_lead|meeting|steering|
// route_rejected|wind_down). Stored as ActivityItem rows with rowKind 4–8 so we never touch
// the shared app_state.h. Org chart updates in place; meetings/steering are compact cards;
// route rejections are muted one-liners. Missing fields degrade silently.
//
// rowKind 4 = org chart (stage "corp:org", detail = JSON snapshot)
// rowKind 5 = meeting digest (stage "corp:meet:<n>", detail = JSON digest)
// rowKind 6 = steering note (stage "corp:steer:<n>", reason = text)
// rowKind 7 = route rejected (stage "corp:rej:<n>", reason = muted line); also inter-agent
//             messages (stage "msg:<channel>:<id>", title = from, reason = to, detail = body)
// rowKind 8 = wind-down summary (stage "corp:wind", reason = final line)

static const nlohmann::json& corpPayloadObj(const nlohmann::json& data) {
    static const nlohmann::json kEmpty = nlohmann::json::object();
    if (data.is_object() && data.contains("payload") && data["payload"].is_object())
        return data["payload"];
    // Tolerate flat envelopes (fields at top level) — additive streams may omit nesting.
    return data.is_object() ? data : kEmpty;
}

static std::string corpDeptLabel(const nlohmann::json& data, const nlohmann::json& payload) {
    if (data.is_object() && data.contains("dept")) {
        const auto& d = data["dept"];
        if (d.is_string()) return d.get<std::string>();
        if (d.is_object()) {
            std::string n = jstr(d, "name");
            if (n.empty()) n = jstr(d, "id");
            return n;
        }
    }
    std::string n = jstr(payload, "name");
    if (n.empty()) n = jstr(payload, "dept_id");
    if (n.empty()) n = jstr(payload, "to_dept");
    if (n.empty()) n = jstr(payload, "digest_dept");
    return n;
}

static std::string corpAgentId(const nlohmann::json& data, const nlohmann::json& payload) {
    std::string a = jstr(data, "agent");
    if (a.empty()) a = jstr(payload, "agent");
    if (a.empty()) a = jstr(payload, "agent_id");
    if (a.empty()) a = jstr(payload, "worker_id");
    if (a.empty()) a = jstr(payload, "from");
    return a;
}

// Short seat id for display (drop run-id suffixes when long).
std::string corpShortId(const std::string& id) {
    if (id.size() <= 28) return id;
    return id.substr(0, 24) + "\xe2\x80\xa6";
}

int corpWorkerCount(const nlohmann::json& dept) {
    if (!dept.is_object()) return 0;
    if (dept.contains("workers") && dept["workers"].is_array())
        return (int)dept["workers"].size();
    if (dept.contains("worker_count") && dept["worker_count"].is_number_integer())
        return dept["worker_count"].get<int>();
    if (dept.contains("workers") && dept["workers"].is_number_integer())
        return dept["workers"].get<int>();
    return 0;
}

std::string corpDeptHead(const nlohmann::json& dept) {
    std::string h = jstr(dept, "head");
    if (h.empty()) h = jstr(dept, "head_id");
    return h;
}

std::string corpDeptLead(const nlohmann::json& dept) {
    std::string l = jstr(dept, "lead");
    if (l.empty()) l = jstr(dept, "team_lead");
    if (l.empty()) l = jstr(dept, "team_lead_id");
    return l;
}

std::string corpDeptNameOf(const nlohmann::json& dept) {
    std::string n = jstr(dept, "name");
    if (n.empty()) n = jstr(dept, "id");
    if (n.empty()) n = "department";
    return n;
}

// Normalize any payload departments array into a compact chart JSON for the org card.
static nlohmann::json corpNormalizeChart(const nlohmann::json& payload) {
    nlohmann::json chart = nlohmann::json::object();
    std::string dir = jstr(payload, "director_id");
    if (dir.empty()) dir = jstr(payload, "director");
    chart["director"] = dir;
    const nlohmann::json* src = nullptr;
    if (payload.contains("departments") && payload["departments"].is_array())
        src = &payload["departments"];
    else if (payload.contains("chart") && payload["chart"].is_object()) {
        const auto& c = payload["chart"];
        if (dir.empty()) {
            dir = jstr(c, "director_id");
            if (dir.empty()) dir = jstr(c, "director");
            chart["director"] = dir;
        }
        if (c.contains("departments") && c["departments"].is_array())
            src = &c["departments"];
    }
    nlohmann::json depts = nlohmann::json::array();
    if (src) {
        for (const auto& d : *src) {
            if (!d.is_object()) continue;
            nlohmann::json row = nlohmann::json::object();
            row["id"] = jstr(d, "id");
            row["name"] = corpDeptNameOf(d);
            row["head"] = corpDeptHead(d);
            row["lead"] = corpDeptLead(d);
            int wc = corpWorkerCount(d);
            row["workers"] = wc;
            // Keep worker ids when present so hire can de-dupe.
            if (d.contains("workers") && d["workers"].is_array())
                row["worker_ids"] = d["workers"];
            depts.push_back(std::move(row));
        }
    }
    chart["departments"] = std::move(depts);
    return chart;
}

static std::string corpOrgReason(const nlohmann::json& chart) {
    int nDepts = 0, nWorkers = 0;
    if (chart.contains("departments") && chart["departments"].is_array()) {
        nDepts = (int)chart["departments"].size();
        for (const auto& d : chart["departments"])
            nWorkers += corpWorkerCount(d);
    }
    std::string dir = jstr(chart, "director");
    char buf[160];
    if (!dir.empty())
        std::snprintf(buf, sizeof(buf), "Director \xc2\xb7 %d dept%s \xc2\xb7 %d worker%s",
                      nDepts, nDepts == 1 ? "" : "s", nWorkers, nWorkers == 1 ? "" : "s");
    else
        std::snprintf(buf, sizeof(buf), "%d dept%s \xc2\xb7 %d worker%s",
                      nDepts, nDepts == 1 ? "" : "s", nWorkers, nWorkers == 1 ? "" : "s");
    return std::string(buf);
}

static ActivityItem* corpFindOrg(ChatMsg& m) {
    for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
        if (it->rowKind == 4 && it->stage == "corp:org") return &*it;
    return nullptr;
}

static ActivityItem& corpEnsureOrg(ChatMsg& m) {
    if (ActivityItem* a = corpFindOrg(m)) return *a;
    ActivityItem ni;
    ni.rowKind = 4;
    ni.stage = "corp:org";
    ni.title = "Corporate org";
    ni.status = "done";
    m.activity.push_back(std::move(ni));
    return m.activity.back();
}

nlohmann::json corpParseChart(const ActivityItem& a) {
    if (a.detail.empty()) return nlohmann::json::object({{"departments", nlohmann::json::array()}});
    try {
        auto j = nlohmann::json::parse(a.detail);
        if (j.is_object()) return j;
    } catch (...) {}
    return nlohmann::json::object({{"departments", nlohmann::json::array()}});
}

static void corpApplyOrgBuilt(ChatMsg& m, const nlohmann::json& payload) {
    ActivityItem& a = corpEnsureOrg(m);
    nlohmann::json chart = corpNormalizeChart(payload);
    a.detail = chart.dump();
    a.reason = corpOrgReason(chart);
    a.title = "Corporate org";
    a.status = "done";
}

// Patch the in-place org card on hire / team_lead when a full departments[] is absent.
static void corpPatchOrgSeat(ChatMsg& m, const nlohmann::json& data, const nlohmann::json& payload,
                             bool asTeamLead) {
    // Prefer full chart replacement when the backend includes departments.
    if ((payload.contains("departments") && payload["departments"].is_array()) ||
        (payload.contains("chart") && payload["chart"].is_object())) {
        corpApplyOrgBuilt(m, payload);
        return;
    }
    ActivityItem& a = corpEnsureOrg(m);
    nlohmann::json chart = corpParseChart(a);
    if (!chart.contains("departments") || !chart["departments"].is_array())
        chart["departments"] = nlohmann::json::array();

    std::string did = jstr(payload, "dept_id");
    std::string dname = corpDeptLabel(data, payload);
    if (did.empty() && !dname.empty()) did = dname;
    std::string agent = corpAgentId(data, payload);
    std::string role = jstr(payload, "role");
    if (agent.empty() && !role.empty()) agent = role;

    nlohmann::json* row = nullptr;
    for (auto& d : chart["departments"]) {
        if (!d.is_object()) continue;
        if ((!did.empty() && (jstr(d, "id") == did || jstr(d, "name") == did)) ||
            (!dname.empty() && (jstr(d, "name") == dname || jstr(d, "id") == dname))) {
            row = &d;
            break;
        }
    }
    if (!row) {
        nlohmann::json nd = nlohmann::json::object();
        nd["id"] = did;
        nd["name"] = dname.empty() ? (did.empty() ? "department" : did) : dname;
        nd["head"] = jstr(payload, "parent_id");
        nd["lead"] = "";
        nd["workers"] = 0;
        nd["worker_ids"] = nlohmann::json::array();
        chart["departments"].push_back(std::move(nd));
        row = &chart["departments"].back();
    }

    if (!agent.empty()) {
        if (!row->contains("worker_ids") || !(*row)["worker_ids"].is_array())
            (*row)["worker_ids"] = nlohmann::json::array();
        bool found = false;
        for (const auto& w : (*row)["worker_ids"])
            if (w.is_string() && w.get<std::string>() == agent) { found = true; break; }
        if (!found) {
            (*row)["worker_ids"].push_back(agent);
            (*row)["workers"] = (int)(*row)["worker_ids"].size();
        } else {
            (*row)["workers"] = (int)(*row)["worker_ids"].size();
        }
        if (asTeamLead || jstr(payload, "rank") == "team_lead")
            (*row)["lead"] = agent;
    } else if (asTeamLead) {
        std::string lead = jstr(payload, "worker_id");
        if (lead.empty()) lead = jstr(payload, "team_lead_id");
        if (!lead.empty()) (*row)["lead"] = lead;
    }

    a.detail = chart.dump();
    a.reason = corpOrgReason(chart);
    a.status = "done";
}

static int corpListCount(const nlohmann::json& o, const char* key) {
    if (!o.is_object() || !o.contains(key)) return 0;
    const auto& v = o[key];
    if (v.is_array()) return (int)v.size();
    if (v.is_number_integer()) return v.get<int>();
    if (v.is_number_float()) return (int)v.get<double>();
    return 0;
}

static std::string corpJoinRisks(const nlohmann::json& payload) {
    if (!payload.contains("risks")) return {};
    const auto& r = payload["risks"];
    if (r.is_string()) return r.get<std::string>();
    if (!r.is_array()) return {};
    std::string out;
    for (const auto& item : r) {
        std::string s;
        if (item.is_string()) s = item.get<std::string>();
        if (s.empty()) continue;
        if (!out.empty()) out += "; ";
        out += s;
        if (out.size() > 160) { out.resize(157); out += "\xe2\x80\xa6"; break; }
    }
    return out;
}

static void corpApplyMeeting(ChatMsg& m, const nlohmann::json& data, const nlohmann::json& payload) {
    ActivityItem ni;
    ni.rowKind = 5;
    ni.stage = "corp:meet:" + std::to_string(m.activity.size());
    std::string dept = corpDeptLabel(data, payload);
    if (dept.empty()) dept = jstr(payload, "dept_id");
    ni.title = dept.empty() ? "Meeting" : ("Meeting \xc2\xb7 " + dept);

    int done = corpListCount(payload, "done");
    int prog = corpListCount(payload, "in_progress");
    if (prog == 0) prog = corpListCount(payload, "in-progress");
    int blocked = corpListCount(payload, "blocked");
    // Prefer explicit counts when backend sends them alongside lists.
    if (payload.contains("done_count") && payload["done_count"].is_number_integer())
        done = payload["done_count"].get<int>();
    if (payload.contains("in_progress_count") && payload["in_progress_count"].is_number_integer())
        prog = payload["in_progress_count"].get<int>();
    if (payload.contains("blocked_count") && payload["blocked_count"].is_number_integer())
        blocked = payload["blocked_count"].get<int>();

    char line[192];
    std::snprintf(line, sizeof(line), "done %d \xc2\xb7 in progress %d \xc2\xb7 blocked %d",
                  done, prog, blocked);
    ni.reason = line;
    std::string risks = corpJoinRisks(payload);
    if (!risks.empty()) {
        ni.reason += "\nRisks: " + risks;
    }
    // Pack counts for the card renderer (no app_state fields).
    ni.tokensIn = done;
    ni.tokensOut = prog;
    ni.opsCount = blocked;
    ni.status = blocked > 0 ? "warn" : "done";
    ni.expanded = false;

    nlohmann::json dig = nlohmann::json::object();
    dig["dept"] = dept;
    dig["done"] = done;
    dig["in_progress"] = prog;
    dig["blocked"] = blocked;
    dig["risks"] = risks;
    dig["trigger"] = jstr(payload, "trigger");
    dig["worker_completions"] = jint(payload, "worker_completions");
    // Preserve list bodies for expand.
    if (payload.contains("done") && payload["done"].is_array()) dig["done_items"] = payload["done"];
    if (payload.contains("in_progress") && payload["in_progress"].is_array())
        dig["in_progress_items"] = payload["in_progress"];
    if (payload.contains("blocked") && payload["blocked"].is_array())
        dig["blocked_items"] = payload["blocked"];
    ni.detail = dig.dump();
    m.activity.push_back(std::move(ni));
}

static void corpApplySteering(ChatMsg& m, const nlohmann::json& data, const nlohmann::json& payload) {
    ActivityItem ni;
    ni.rowKind = 6;
    ni.stage = "corp:steer:" + std::to_string(m.activity.size());
    std::string dept = corpDeptLabel(data, payload);
    ni.title = dept.empty() ? "Director steering" : ("Director steering \xc2\xb7 " + dept);
    std::string text = jstr(payload, "text");
    if (text.empty()) text = jstr(payload, "steering");
    if (text.empty()) text = jstr(payload, "note");
    if (text.empty() && payload.contains("notes") && payload["notes"].is_array()) {
        for (const auto& n : payload["notes"]) {
            if (!n.is_string()) continue;
            if (!text.empty()) text += " ";
            text += n.get<std::string>();
        }
    }
    ni.reason = text;
    ni.detail = text;
    ni.status = "done";
    m.activity.push_back(std::move(ni));
}

static void corpApplyRouteRejected(ChatMsg& m, const nlohmann::json& data,
                                   const nlohmann::json& payload) {
    ActivityItem ni;
    ni.rowKind = 7;
    ni.stage = "corp:rej:" + std::to_string(m.activity.size());
    std::string from = jstr(payload, "from");
    if (from.empty()) from = corpAgentId(data, payload);
    std::string to = jstr(payload, "to");
    if (to.empty()) to = jstr(payload, "channel");
    std::string reason = jstr(payload, "reason");
    // Prefer a backend-provided line; else compose the design-doc muted form.
    std::string line = jstr(payload, "line");
    if (line.empty()) line = jstr(payload, "message");
    if (line.empty()) {
        if (!from.empty() && (to.find("director") != std::string::npos ||
                              reason.find("director") != std::string::npos ||
                              reason.find("heads") != std::string::npos ||
                              to.empty())) {
            line = "worker " + corpShortId(from) +
                   " \xe2\x86\x92 director blocked \xe2\x80\x94 routed to head";
        } else if (!from.empty()) {
            line = corpShortId(from) + " route blocked";
            if (!to.empty()) line += " \xe2\x86\x92 " + to;
            if (!reason.empty()) line += " \xe2\x80\x94 " + reason;
        } else {
            line = reason.empty() ? "route rejected" : reason;
        }
    }
    ni.reason = line;
    ni.title = line;
    ni.status = "done";
    m.activity.push_back(std::move(ni));
}

static void corpApplyWindDown(ChatMsg& m, const nlohmann::json& data,
                              const nlohmann::json& payload) {
    (void)data;
    ActivityItem ni;
    ni.rowKind = 8;
    ni.stage = "corp:wind";
    ni.title = "Org wind-down";
    std::string summary = jstr(payload, "summary");
    if (summary.empty()) summary = jstr(payload, "text");
    if (summary.empty()) summary = jstr(payload, "message");
    int done = 0;
    if (payload.contains("departments_done")) {
        if (payload["departments_done"].is_array())
            done = (int)payload["departments_done"].size();
        else if (payload["departments_done"].is_number_integer())
            done = payload["departments_done"].get<int>();
    }
    if (summary.empty()) {
        if (done > 0) {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "Corporate org finished \xc2\xb7 %d department%s",
                          done, done == 1 ? "" : "s");
            summary = buf;
        } else {
            summary = "Corporate org finished";
        }
    }
    // Also refresh the org card summary line if present.
    if (ActivityItem* org = corpFindOrg(m)) {
        if (!summary.empty()) org->reason = summary;
        org->status = "done";
    }
    ni.reason = summary;
    ni.detail = summary;
    ni.status = "done";
    m.activity.push_back(std::move(ni));
}

static void feedCorporateEvent(ChatMsg& m, const nlohmann::json& data) {
    const nlohmann::json& payload = corpPayloadObj(data);
    std::string ce = jstr(data, "corp_event");
    if (ce.empty()) ce = jstr(data, "event");
    if (ce.empty()) ce = jstr(payload, "corp_event");
    if (ce.empty()) return;  // unknown / incomplete — silent no-op

    if (ce == "org_built") {
        corpApplyOrgBuilt(m, payload);
    } else if (ce == "hire") {
        corpPatchOrgSeat(m, data, payload, /*asTeamLead=*/false);
    } else if (ce == "team_lead") {
        corpPatchOrgSeat(m, data, payload, /*asTeamLead=*/true);
    } else if (ce == "meeting") {
        corpApplyMeeting(m, data, payload);
    } else if (ce == "steering") {
        corpApplySteering(m, data, payload);
    } else if (ce == "route_rejected") {
        corpApplyRouteRejected(m, data, payload);
    } else if (ce == "wind_down") {
        corpApplyWindDown(m, data, payload);
    }
    // Unknown corp_event values: ignore (forward-compatible).
}

// ---- stream:out ops-JSON detection (F1) ----
// Classify the accumulated Output stream as model ops/tool JSON once the first non-whitespace
// char is '{' or '[', OR a ``` / ```json fence appears whose body starts with { or [.
// Returns byte offset where the ops blob begins (0 = whole stream is ops; npos = prose).
// No streaming JSON parser — line-based prefix / fence scan only.
static size_t findOpsJsonStart(const std::string& s) {
    size_t i = 0;
    while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n')) ++i;
    if (i >= s.size()) return std::string::npos;
    if (s[i] == '{' || s[i] == '[') return i;
    // Scan for a markdown fence (at line start) whose body opens with { or [.
    // Handles mixed content: prose before the fence stays as prose.
    for (size_t p = i; p + 3 <= s.size(); ++p) {
        if (s.compare(p, 3, "```") != 0) continue;
        // Fence must be at start or after a newline (allow spaces before fence on its line).
        if (p > 0) {
            size_t q = p;
            while (q > 0 && (s[q - 1] == ' ' || s[q - 1] == '\t')) --q;
            if (q > 0 && s[q - 1] != '\n' && s[q - 1] != '\r') continue;
        }
        size_t j = p + 3;
        while (j < s.size() && s[j] != '\n' && s[j] != '\r') ++j;  // skip lang tag
        while (j < s.size() && (s[j] == '\n' || s[j] == '\r' || s[j] == ' ' || s[j] == '\t')) ++j;
        if (j < s.size() && (s[j] == '{' || s[j] == '[')) return p;
    }
    return std::string::npos;
}

// Best-effort op count from a JSON prefix: count top-level objects in an array, or 1 for object.
// Falls back to -1 when not parseable yet (caller shows byte count).
static int countOpsInJsonPrefix(const std::string& s) {
    size_t i = 0;
    while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n')) ++i;
    if (i >= s.size()) return -1;
    // Strip optional fence wrapper for counting.
    std::string body = s;
    if (s.compare(i, 3, "```") == 0) {
        size_t j = i + 3;
        while (j < s.size() && s[j] != '\n' && s[j] != '\r') ++j;
        while (j < s.size() && (s[j] == '\n' || s[j] == '\r')) ++j;
        size_t end = s.rfind("```");
        if (end != std::string::npos && end > j)
            body = s.substr(j, end - j);
        else
            body = s.substr(j);
        i = 0;
        while (i < body.size() && (body[i] == ' ' || body[i] == '\t' || body[i] == '\r' || body[i] == '\n')) ++i;
    } else {
        body = s;
    }
    if (i >= body.size()) return -1;
    try {
        auto j = nlohmann::json::parse(body, nullptr, false);
        if (j.is_discarded()) {
            // Incomplete stream: count rough object starts in an array prefix.
            if (body[i] == '[') {
                int n = 0;
                int depth = 0;
                bool inStr = false, esc = false;
                for (size_t k = i; k < body.size(); ++k) {
                    char c = body[k];
                    if (inStr) {
                        if (esc) esc = false;
                        else if (c == '\\') esc = true;
                        else if (c == '"') inStr = false;
                        continue;
                    }
                    if (c == '"') { inStr = true; continue; }
                    if (c == '{' ) {
                        if (depth == 1) ++n;  // top-level array element object
                        ++depth;
                    } else if (c == '}') {
                        if (depth > 0) --depth;
                    } else if (c == '[') {
                        ++depth;
                    } else if (c == ']') {
                        if (depth > 0) --depth;
                    }
                }
                return n > 0 ? n : -1;
            }
            return -1;
        }
        if (j.is_array()) return (int)j.size();
        if (j.is_object()) {
            // Prefer an "ops" array field when present.
            if (j.contains("ops") && j["ops"].is_array()) return (int)j["ops"].size();
            if (j.contains("tool_calls") && j["tool_calls"].is_array())
                return (int)j["tool_calls"].size();
            return 1;
        }
    } catch (...) {}
    return -1;
}

// Best-effort label of the most recent op in a streamed ops-JSON prefix: the last
// "op"/"tool" string field. Powers the live "Building ops…" summary so the user sees
// WHAT is being built in real time without opening the raw JSON expander.
static std::string lastOpLabelInJsonPrefix(const std::string& s) {
    static const char* keys[] = {"\"op\"", "\"tool\""};
    for (const char* k : keys) {
        size_t pos = s.rfind(k);
        while (pos != std::string::npos) {
            size_t p = pos + std::strlen(k);
            while (p < s.size() && (s[p] == ' ' || s[p] == '\t' || s[p] == '\r' || s[p] == '\n')) ++p;
            if (p < s.size() && s[p] == ':') {
                ++p;
                while (p < s.size() && (s[p] == ' ' || s[p] == '\t')) ++p;
                if (p < s.size() && s[p] == '"') {
                    size_t e = s.find('"', p + 1);
                    if (e != std::string::npos && e > p + 1 && e - p - 1 <= 64)
                        return s.substr(p + 1, e - p - 1);
                    if (e == std::string::npos && s.size() > p + 1 && s.size() - p - 1 <= 64)
                        return s.substr(p + 1) + "\xe2\x80\xa6";   // value still streaming
                }
            }
            pos = (pos == 0) ? std::string::npos : s.rfind(k, pos - 1);
        }
    }
    return {};
}

// Finalize a thought/output box: stop streaming. Ops JSON stays collapsed unless
// the user pinned it. A finished Thinking block AUTO-COLLAPSES to its one-line
// preview: while autoscrolling, a block that stays open after its stream ended
// floods the tail of the transcript — the user re-opens it with one click when
// they want the audit (userToggled always wins, both directions).
// Prose Output keeps the user's last expanded state.
void finalizeThoughtBox(ActivityItem& a) {
    a.streaming = false;
    if (a.status == "running" || a.status.empty()) a.status = "done";
    const bool isOps = (a.stage == "stream:out" && a.opsCount > 0) ||
                       (a.title.find("Building ops") != std::string::npos);
    const bool isProseOut = !isOps && (a.stage == "stream:out" || a.title == "Output");
    if (isProseOut) return;  // leave expanded as-is
    if (isOps) {
        if (!a.userToggled) a.expanded = false;
        return;
    }
    // Thinking / plan reasoning — done filling out, so close it unless the user
    // explicitly pinned it open.
    if (!a.userToggled) a.expanded = false;
}

// Finalize the live streamed-thinking box ("thought:stream") when a visible
// timeline boundary arrives — a context chip, tool call, stage boundary, or
// completed agent.thought block. A contentless channel flush is not a visible
// timeline row, so it never fragments the box (see ui::streamTextVisible).
// Once finalized, the next reasoning burst opens below the intervening row.
static void finalizeLiveStreamThought(ChatMsg& m) {
    for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it) {
        if (it->rowKind == 2 && it->stage == "thought:stream" && it->streaming) {
            finalizeThoughtBox(*it);
            return;
        }
    }
}

// The prose Output box is a VISIBLE row drawn at its arrival position, so the same
// boundaries close it: otherwise the next prose burst grows a box already drawn above the
// tool card or chip that just landed, and the transcript reads out of order. Ops-JSON boxes
// are exempt — consecutive stage bursts deliberately share one "Building ops…" row
// (stream_rows.h, streamDeltaTarget's tail rule).
static void finalizeLiveStreamProse(ChatMsg& m) {
    for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it) {
        if (it->rowKind != 2 || it->stage != "stream:out" || !it->streaming) continue;
        if (it->opsCount > 0 || it->title.find("Building ops") != std::string::npos) return;
        finalizeThoughtBox(*it);
        return;
    }
}

// A visible timeline row ends BOTH live streams.
static void finalizeLiveStreamBoxes(ChatMsg& m) {
    finalizeLiveStreamThought(m);
    finalizeLiveStreamProse(m);
}

// agent.context is emitted after each provider completion. It is not a visible
// row, but it is the original boundary between two reasoning buffers; retain
// that boundary as paragraph whitespace when the next completion continues the
// same visible Thinking box.
static void separateLiveStreamThoughtParagraph(ChatMsg& m) {
    for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it) {
        if (it->rowKind != 2 || it->stage != "thought:stream" || !it->streaming) continue;
        if (!it->detail.empty() && it->detail.back() != '\n') it->detail += "\n\n";
        return;
    }
}

// Mid-turn chat_history flush (resume-safe). feedTurnActivity sets the dirty flag;
// the GL draw path calls maybePersistChatHistoryDebounced with AppState.
std::atomic<bool> s_chatHistoryDirty{false};
double s_chatHistoryLastPersist = -1e9;

void markChatHistoryDirty() {
    s_chatHistoryDirty.store(true, std::memory_order_relaxed);
}

void feedTurnActivity(ChatMsg& m, const std::string& kind, const nlohmann::json& data) {
    ui::ChatRawActivityCapture rawCapture(m);
    m.lastEventTs = chatNowEpoch();
    // Plan-section position marker (rowKind 18): the plan/objectives section renders at the
    // point in the timeline where the plan FIRST arrived, not pinned to the top of the turn.
    // Without this a late plan used to pop in ABOVE thinking blocks the user had already read,
    // looking out-of-order. Idempotent — one marker per turn.
    auto ensurePlanMarker = [&]() {
        for (const auto& a : m.activity)
            if (a.rowKind == 18) return;
        ActivityItem ni;
        ni.rowKind = 18;
        ni.stage   = "__plan__";
        ni.status  = "done";
        m.activity.push_back(std::move(ni));
    };
    // Inside a mission, sub-run stage keys (intake/model/...) repeat per task — namespace them by
    // the current task index so task 2's "model" row doesn't reopen task 1's. Outside a mission
    // (curMissionTask < 0) the key is the bare stage, preserving the single-run behavior exactly.
    auto nsKey = [&](const std::string& s) {
        return m.curMissionTask >= 0 ? (std::to_string(m.curMissionTask) + ":" + s) : s;
    };
    auto findStage = [&](const std::string& s) -> ActivityItem* {
        for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
            if (it->stage == s) return &*it;
        return nullptr;
    };
    // ---- Plan / Objectives: multi-shape plan_state + derived normalizers ----
    // Explicit plan_state events drive the visible todo list on EVERY non-Ask run path
    // (agent / edit / swarm / eng_firm / mission). Items are namespaced by origin
    // ("edit:…", "mission:…", "firm:…", stage names for Solo) so multiple writers
    // mid-run NEVER clobber each other — we UPSERT by id, never wholesale-replace.
    // Edit-mode plans guide execution only (no approve gate).
    auto applyPlanStateEvent = [&](const nlohmann::json& src) {
        PlanState incoming = parsePlanState(src);
        if (!incoming.present) return;

        // First plan_state of the turn — take wholesale (still mark present/fromEvent).
        if (!m.planState.present || m.planState.items.empty()) {
            m.planState = std::move(incoming);
            ensurePlanMarker();
        } else {
            // Multi-writer merge: upsert items by id; keep foreign-namespace rows.
            // Top-level title/summary/shape update when incoming revision is >= current
            // OR when the field is still empty/generic.
            const bool revOk = incoming.revision >= m.planState.revision;
            if (revOk) {
                if (!incoming.shape.empty()) m.planState.shape = incoming.shape;
                if (!incoming.title.empty() &&
                    (m.planState.title.empty() || m.planState.title == "Plan" ||
                     m.planState.title == "Build plan" || m.planState.title == "Mission" ||
                     revOk)) {
                    // Prefer a more specific title when the incoming one isn't generic-only,
                    // or always accept when revision advances.
                    if (incoming.title != "Plan" || m.planState.title.empty())
                        m.planState.title = incoming.title;
                }
                if (!incoming.summary.empty() &&
                    (m.planState.summary.empty() || revOk))
                    m.planState.summary = incoming.summary;
                if (incoming.revision > m.planState.revision)
                    m.planState.revision = incoming.revision;
            } else {
                // Stale snapshot for chrome — still allow per-item status upserts below
                // (a lagging writer must not erase a newer writer's statuses, but its
                // own ids still need live status).
            }
            m.planState.fromEvent = true;
            m.planState.present = true;

            // Optional writer scope: when every incoming id shares a common prefix
            // ("edit:", "mission:", "firm:", "solo:", "stage:"), drop prior items under
            // that same prefix that are absent from this payload (writer republished
            // its full list). Unscoped / mixed-prefix payloads only upsert.
            auto idPrefix = [](const std::string& id) -> std::string {
                size_t c = id.find(':');
                if (c == std::string::npos || c == 0) return {};
                // Require a non-numeric prefix so bare "0:model" mission-stage keys
                // aren't treated as a writer namespace for prune.
                std::string pre = id.substr(0, c);
                bool allDigit = !pre.empty();
                for (char ch : pre)
                    if (ch < '0' || ch > '9') { allDigit = false; break; }
                if (allDigit) return {};
                return pre + ":";
            };
            std::string scope;
            bool scoped = !incoming.items.empty();
            for (const auto& it : incoming.items) {
                std::string pre = idPrefix(it.id);
                if (pre.empty()) { scoped = false; break; }
                if (scope.empty()) scope = pre;
                else if (scope != pre) { scoped = false; break; }
            }
            if (scoped && !scope.empty()) {
                // Collect incoming ids for this scope.
                std::vector<std::string> keepIds;
                keepIds.reserve(incoming.items.size());
                for (const auto& it : incoming.items) keepIds.push_back(it.id);
                auto stillPresent = [&](const std::string& id) {
                    for (const auto& k : keepIds) if (k == id) return true;
                    return false;
                };
                std::vector<PlanItem> kept;
                kept.reserve(m.planState.items.size());
                for (auto& it : m.planState.items) {
                    if (it.id.rfind(scope, 0) == 0) {
                        if (!stillPresent(it.id)) continue;  // dropped by this writer
                    }
                    kept.push_back(std::move(it));
                }
                m.planState.items = std::move(kept);
            }

            // Upsert each incoming item by id (append if new).
            for (auto& inc : incoming.items) {
                if (inc.id.empty()) continue;
                PlanItem* existing = nullptr;
                for (auto& it : m.planState.items)
                    if (it.id == inc.id) { existing = &it; break; }
                if (existing) {
                    if (!inc.title.empty()) existing->title = std::move(inc.title);
                    if (!inc.desc.empty()) existing->desc = std::move(inc.desc);
                    if (!inc.status.empty()) existing->status = std::move(inc.status);
                    if (!inc.kind.empty()) existing->kind = std::move(inc.kind);
                    if (!inc.phase.empty()) existing->phase = std::move(inc.phase);
                    if (!inc.parent_id.empty()) existing->parent_id = std::move(inc.parent_id);
                } else {
                    m.planState.items.push_back(std::move(inc));
                }
            }
            // Merge dag edges by (from,to) pair when present.
            for (auto& e : incoming.edges) {
                bool found = false;
                for (const auto& x : m.planState.edges)
                    if (x.from == e.from && x.to == e.to) { found = true; break; }
                if (!found) m.planState.edges.push_back(std::move(e));
            }
            // Prefer a richer shape when merging checklist into build_plan etc.
            // If we now have mixed part+step items, keep build_plan / phased_todo chrome.
            if (m.planState.shape == "checklist") {
                bool hasPart = false, hasTask = false;
                for (const auto& it : m.planState.items) {
                    if (it.kind == "part") hasPart = true;
                    if (it.kind == "task") hasTask = true;
                }
                if (hasPart) m.planState.shape = "build_plan";
                else if (hasTask) m.planState.shape = "phased_todo";
            }
        }

        // Mirror build_plan into legacy PlanInfo so old callers of m.plan still see it.
        // Rebuild parts list from current planState items (id-stable).
        if (m.planState.shape == "build_plan") {
            m.plan.present = true;
            if (!m.planState.title.empty()) m.plan.name = m.planState.title;
            if (!m.planState.summary.empty()) m.plan.summary = m.planState.summary;
            // Only seed parts when empty — avoid thrashing order mid-run.
            if (m.plan.parts.empty()) {
                for (const auto& it : m.planState.items) {
                    if (it.kind != "part" && it.kind != "step" && !it.kind.empty()) continue;
                    PlanPart pp;
                    pp.name = it.title;
                    if (!it.desc.empty()) {
                        size_t nl = it.desc.find('\n');
                        if (nl == std::string::npos) pp.purpose = it.desc;
                        else {
                            pp.purpose = it.desc.substr(0, nl);
                            std::string rest = it.desc.substr(nl + 1);
                            size_t nl2 = rest.find('\n');
                            std::string line2 = (nl2 == std::string::npos) ? rest : rest.substr(0, nl2);
                            if (line2.find("mm") != std::string::npos ||
                                line2.find_first_of("0123456789") != std::string::npos)
                                pp.dims = line2;
                        }
                    }
                    m.plan.parts.push_back(std::move(pp));
                }
            }
        }
    };
    auto ensureDerivedPlan = [&]() -> PlanState& {
        // Derived path may create a shell. Once an event plan_state exists we still allow
        // status-only tweaks via findPlanItem — but ensureDerivedPlan won't wipe it.
        if (!m.planState.present) {
            m.planState.present = true;
            m.planState.fromEvent = false;
            m.planState.shape = "checklist";
            m.planState.title = "Plan";
            m.planState.revision = 0;
            ensurePlanMarker();
        }
        return m.planState;
    };
    auto findPlanItem = [&](const std::string& id) -> PlanItem* {
        for (auto& it : m.planState.items)
            if (it.id == id) return &it;
        return nullptr;
    };
    // Upsert a single derived checklist item by id (plan_steps / model_step / mission_*).
    auto upsertDerivedItem = [&](PlanItem item) {
        if (item.id.empty()) return;
        if (PlanItem* ex = findPlanItem(item.id)) {
            if (!item.title.empty()) ex->title = std::move(item.title);
            if (!item.desc.empty()) ex->desc = std::move(item.desc);
            if (!item.status.empty()) ex->status = std::move(item.status);
            if (!item.kind.empty()) ex->kind = std::move(item.kind);
            if (!item.phase.empty()) ex->phase = std::move(item.phase);
            if (!item.parent_id.empty()) ex->parent_id = std::move(item.parent_id);
        } else {
            m.planState.items.push_back(std::move(item));
        }
        m.planState.present = true;
    };

    if (kind == "plan_state") {
        // SHARED CONTRACT: full multi-shape snapshot.
        nlohmann::json src = data;
        // Tolerate nested {plan_state:{...}} or top-level fields.
        if (data.contains("plan_state") && data["plan_state"].is_object())
            src = data["plan_state"];
        else if (data.contains("plan") && data["plan"].is_object() &&
                 (data["plan"].contains("items") || data["plan"].contains("shape")))
            src = data["plan"];
        applyPlanStateEvent(src);
    } else if (kind == "plan") {
        // Legacy kind:plan — may carry PlanInfo under "plan" or top-level, OR a full plan_state.
        if (data.contains("shape") || data.contains("items")) {
            applyPlanStateEvent(data);
        } else {
            nlohmann::json planObj = data;
            if (data.contains("plan") && data["plan"].is_object()) planObj = data["plan"];
            // Prefer parsePlan (reuse existing) → planStateFromPlanInfo, then MERGE by id
            // so a legacy plan mid-mission doesn't wipe mission:/edit: rows.
            PlanInfo pi = parsePlan(planObj);
            if (pi.present) {
                m.plan = pi;
                PlanState derived = planStateFromPlanInfo(pi);
                if (!m.planState.present || m.planState.items.empty()) {
                    m.planState = std::move(derived);
                    m.planState.fromEvent = false;
                    ensurePlanMarker();
                } else {
                    // Upsert part:* items only; leave foreign namespaces intact.
                    for (auto& inc : derived.items)
                        upsertDerivedItem(std::move(inc));
                    if (m.planState.shape.empty() || m.planState.shape == "checklist")
                        m.planState.shape = "build_plan";
                    if (m.planState.title.empty() || m.planState.title == "Plan")
                        m.planState.title = derived.title.empty() ? "Build plan" : derived.title;
                    if (m.planState.summary.empty() && !derived.summary.empty())
                        m.planState.summary = derived.summary;
                }
            }
        }
    } else if (kind == "plan_steps") {
        // C++ model checklist: {steps: string[], total}. Prefer plan_state (model:N ids)
        // as the single source of truth — if an event-driven checklist already arrived
        // for this turn, ignore legacy plan_steps so we don't double the task list as
        // step:N beside model:N.
        if (m.planState.present && m.planState.fromEvent) {
            // Still allow status-only model_step updates later; just don't synthesize rows.
            return;
        }
        // C++ model checklist: {steps: string[], total}. Merge step:* items by id —
        // never wipe foreign namespaces (edit:/mission:/firm:) even when fromEvent.
        PlanState& ps = ensureDerivedPlan();
        if (ps.shape != "build_plan" && ps.shape != "phased_todo") {
            if (ps.shape.empty() || ps.shape == "checklist")
                ps.shape = "checklist";
        }
        if (ps.title.empty() || ps.title == "Plan") ps.title = "Build checklist";
        const nlohmann::json* steps = nullptr;
        if (data.contains("steps") && data["steps"].is_array()) steps = &data["steps"];
        if (steps) {
            // Prune prior bare step:* / step:N items that this list no longer carries,
            // but keep namespaced ids (edit:/mission:/firm:/solo:) and subagents.
            std::vector<std::string> newIds;
            int i = 0;
            for (const auto& s : *steps) {
                std::string id;
                if (s.is_object()) id = jstr(s, "id");
                if (id.empty()) id = "step:" + std::to_string(i);
                newIds.push_back(id);
                ++i;
            }
            auto isBareStepId = [](const std::string& id) {
                // "step:0" / "step:foo" — not "edit:step:0"
                return id.rfind("step:", 0) == 0;
            };
            std::vector<PlanItem> kept;
            for (auto& it : ps.items) {
                if (isBareStepId(it.id)) {
                    bool found = false;
                    for (const auto& nid : newIds) if (nid == it.id) { found = true; break; }
                    if (!found) continue;
                }
                kept.push_back(std::move(it));
            }
            ps.items = std::move(kept);
            i = 0;
            for (const auto& s : *steps) {
                PlanItem it;
                it.kind = "step";
                it.status = "pending";
                it.phase = "Model";
                if (s.is_string()) {
                    it.title = s.get<std::string>();
                } else if (s.is_object()) {
                    it.title = jstr(s, "title");
                    if (it.title.empty()) it.title = jstr(s, "step");
                    if (it.title.empty()) it.title = jstr(s, "name");
                    it.desc = jstr(s, "desc");
                    it.status = jstr(s, "status");
                    if (it.status.empty()) it.status = "pending";
                    it.id = jstr(s, "id");
                }
                if (it.title.empty()) it.title = "Step " + std::to_string(i + 1);
                if (it.id.empty()) it.id = "step:" + std::to_string(i);
                // Truncate title to ~10 words for tile density.
                {
                    int words = 0;
                    size_t cut = it.title.size();
                    for (size_t c = 0; c < it.title.size(); ++c) {
                        if (it.title[c] == ' ') {
                            ++words;
                            if (words >= 10) { cut = c; break; }
                        }
                    }
                    if (cut < it.title.size()) {
                        if (it.desc.empty()) it.desc = it.title;
                        it.title = it.title.substr(0, cut);
                    }
                }
                upsertDerivedItem(std::move(it));
                ++i;
            }
            ps.present = true;
            ++ps.revision;
        }
    } else if (kind == "model_step") {
        // Live step progress: {index, total, step}. Prefer model:N (plan_state checklist)
        // then bare step:N. Safe alongside event plan_state.
        int idx = jint(data, "index");
        std::string stepTxt = jstr(data, "step");
        std::string idHint = jstr(data, "id");
        if (idHint.empty()) {
            // Prefer model:N when plan_state checklist is the source of truth.
            if (m.planState.present && m.planState.fromEvent)
                idHint = "model:" + std::to_string(idx);
            else
                idHint = "step:" + std::to_string(idx);
        }
        PlanItem* byId = findPlanItem(idHint);
        if (!byId && idHint.rfind("model:", 0) == 0)
            byId = findPlanItem("step:" + std::to_string(idx));
        if (!byId && idHint.rfind("step:", 0) == 0)
            byId = findPlanItem("model:" + std::to_string(idx));
        if (byId) {
            byId->status = "running";
            if (!stepTxt.empty() && byId->title.empty()) byId->title = stepTxt;
            // Mark earlier bare steps / model:N steps done.
            for (auto& it : m.planState.items) {
                if (it.id == idHint || it.id == byId->id) continue;
                bool bareStep = it.id.rfind("step:", 0) == 0;
                bool modelStep = it.id.rfind("model:", 0) == 0;
                if (!bareStep && !modelStep) continue;
                const size_t prefix = bareStep ? 5 : 6;
                if (it.id.size() <= prefix) continue;
                bool num = true;
                for (size_t k = prefix; k < it.id.size(); ++k)
                    if (it.id[k] < '0' || it.id[k] > '9') { num = false; break; }
                if (!num) continue;
                int n = 0;
                try { n = std::stoi(it.id.substr(prefix)); } catch (...) { n = -1; }
                if (n >= 0 && n < idx &&
                    (it.status == "pending" || it.status == "running" || it.status.empty()))
                    it.status = "done";
            }
        } else {
            int stepOrd = 0;
            bool touched = false;
            if (m.planState.present) {
                for (auto& it : m.planState.items) {
                    if (it.kind != "step" && it.kind != "task" && it.kind != "part") continue;
                    // Skip namespaced foreign writers for ordinal walk.
                    if (!it.id.empty()) {
                        size_t c = it.id.find(':');
                        if (c != std::string::npos) {
                            std::string pre = it.id.substr(0, c);
                            if (pre == "edit" || pre == "mission" || pre == "firm" ||
                                pre == "solo" || pre == "spawn")
                                continue;
                        }
                    }
                    if (stepOrd < idx) {
                        if (it.status == "pending" || it.status == "running" || it.status.empty())
                            it.status = "done";
                    } else if (stepOrd == idx) {
                        it.status = "running";
                        if (!stepTxt.empty() && it.title.empty()) it.title = stepTxt;
                        touched = true;
                    }
                    ++stepOrd;
                }
            }
            if (!touched && !stepTxt.empty()) {
                PlanState& ps = ensureDerivedPlan();
                if (ps.shape.empty()) ps.shape = "checklist";
                PlanItem it;
                it.id = idHint;
                it.title = stepTxt;
                it.kind = "step";
                it.status = "running";
                it.phase = "Model";
                upsertDerivedItem(std::move(it));
            }
        }
        m.planState.present = true;
    } else if (kind == "agent.spawn") {
        // Subagent dispatch. Never invent Plan chrome (present/title/checklist) for
        // spawn-only runs — that produced an empty Plan card with only Subagents.
        // Attach a kind=subagent row only when a real plan already exists; the
        // Delegating card below is always the spawn-only surface.
        auto planHasRealContent = [&]() -> bool {
            if (!m.planState.present) return false;
            for (const auto& pi : m.planState.items) {
                if (pi.kind == "step" || pi.kind == "task" || pi.kind == "part" ||
                    pi.kind == "objective" || pi.kind == "node")
                    return true;
            }
            return m.planState.fromEvent && !m.planState.summary.empty();
        };

        PlanItem it;
        it.id = jstr(data, "subagent_id");
        if (it.id.empty()) it.id = jstr(data, "id");
        if (it.id.empty()) it.id = "spawn:" + std::to_string(m.planState.items.size());
        // Ensure spawn: namespace when bare.
        if (it.id.find(':') == std::string::npos) it.id = "spawn:" + it.id;
        it.title = jstr(data, "name");
        if (it.title.empty()) it.title = jstr(data, "role");
        if (it.title.empty()) it.title = jstr(data, "agent");
        if (it.title.empty()) it.title = "subagent";
        it.desc = jstr(data, "brief");
        if (it.desc.empty()) it.desc = jstr(data, "task");
        if (it.desc.empty()) it.desc = jstr(data, "assignment");
        it.kind = "subagent";
        it.status = "running";
        // Capture before move — upsertDerivedItem(std::move(it)) empties it.title.
        const std::string roleTitle = it.title;
        std::string brief = jstr(data, "brief");
        if (brief.empty()) brief = jstr(data, "task");
        if (brief.empty()) brief = jstr(data, "assignment");
        if (planHasRealContent())
            upsertDerivedItem(std::move(it));

        // Delegation card (rowKind 19): transparency view of subagent dispatches. One card
        // per turn, upserted as spawns land; entries packed into detail JSON
        // {"delegates":[{role,brief,spawn_kind}]}.
        ActivityItem* dc = nullptr;
        for (auto& a : m.activity)
            if (a.rowKind == 19) { dc = &a; break; }
        if (!dc) {
            ActivityItem ni;
            ni.rowKind = 19;
            ni.stage   = "deleg:0";
            ni.title   = "0 subagents";
            ni.status  = "running";
            ni.detail  = "{\"delegates\":[]}";
            m.activity.push_back(std::move(ni));
            dc = &m.activity.back();
        }
        try {
            nlohmann::json meta = nlohmann::json::parse(dc->detail, nullptr, false);
            if (meta.is_discarded() || !meta.is_object()) meta = nlohmann::json::object();
            if (!meta.contains("delegates") || !meta["delegates"].is_array())
                meta["delegates"] = nlohmann::json::array();
            nlohmann::json entry;
            entry["role"] = roleTitle;
            entry["brief"] = brief;
            entry["spawn_kind"] = jstr(data, "spawn_kind");
            meta["delegates"].push_back(std::move(entry));
            dc->detail = meta.dump();
            char tb[40];
            std::snprintf(tb, sizeof(tb), "%d subagent%s",
                          (int)meta["delegates"].size(),
                          meta["delegates"].size() == 1 ? "" : "s");
            dc->title = tb;
        } catch (...) {}
    }


    if (kind == "run_started") {
        // Instant chrome for the first frame of a run; the live stream replaces it as
        // thinking / tool rows / assistant deltas arrive.
        if (m.running && m.liveStatus.empty())
            m.liveStatus = "Working\xe2\x80\xa6";
    } else
    if (kind == "mission_started") {
        m.curMissionTask = -1;  // task rows arrive via mission_task_started
        // Merge mission task list into the shared PlanState by mission:* ids.
        // Never wipe edit:/firm:/step: rows — multi-writer safe even when plan_state
        // events already landed (fromEvent).
        PlanState& ps = ensureDerivedPlan();
        if (ps.shape.empty() || ps.shape == "checklist")
            ps.shape = "phased_todo";
        if (ps.title.empty() || ps.title == "Plan" || ps.title == "Build checklist")
            ps.title = "Mission";
        std::string req = jstr(data, "request");
        if (!req.empty() && ps.summary.empty()) ps.summary = req;
        if (data.contains("tasks") && data["tasks"].is_array()) {
            // Prune mission:* rows absent from this list; keep everything else.
            std::vector<std::string> newIds;
            int i = 0;
            for (const auto& t : data["tasks"]) {
                std::string id = "mission:" + std::to_string(i);
                if (t.is_object()) {
                    std::string tid = jstr(t, "id");
                    if (!tid.empty()) {
                        if (tid.rfind("mission:", 0) == 0) id = tid;
                        else id = "mission:" + tid;
                    }
                }
                newIds.push_back(id);
                ++i;
            }
            std::vector<PlanItem> kept;
            for (auto& it : ps.items) {
                if (it.id.rfind("mission:", 0) == 0) {
                    bool found = false;
                    for (const auto& nid : newIds) if (nid == it.id) { found = true; break; }
                    if (!found) continue;
                }
                kept.push_back(std::move(it));
            }
            ps.items = std::move(kept);
            i = 0;
            for (const auto& t : data["tasks"]) {
                PlanItem it;
                it.id = newIds[(size_t)i];
                it.kind = "task";
                it.phase = "Mission";
                it.status = "pending";
                if (t.is_object()) {
                    it.title = jstr(t, "title");
                    if (it.title.empty()) it.title = jstr(t, "capability");
                    it.desc = jstr(t, "req");
                    if (it.desc.empty()) it.desc = jstr(t, "target");
                    if (it.desc.empty()) it.desc = jstr(t, "capability");
                    std::string stt = jstr(t, "status");
                    if (!stt.empty()) it.status = stt;
                } else if (t.is_string()) {
                    it.title = t.get<std::string>();
                }
                if (it.title.empty()) it.title = "Task " + std::to_string(i + 1);
                upsertDerivedItem(std::move(it));
                ++i;
            }
        }
        ps.present = true;
        ++ps.revision;
    } else if (kind == "mission_task_started") {
        int idx = jint(data, "index");
        m.curMissionTask = idx;
        std::string key = "task" + std::to_string(idx);
        ActivityItem* a = findStage(key);
        if (!a) {
            ActivityItem ni;
            ni.stage = key;
            ni.title = jstr(data, "title");
            if (ni.title.empty()) ni.title = jstr(data, "capability");
            ni.missionHeader = true;
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        a->status = "running";
        // Mirror into planState mission checklist by id — works with event plan_state too.
        {
            std::string id = jstr(data, "id");
            if (id.empty()) id = "mission:" + std::to_string(idx);
            else if (id.rfind("mission:", 0) != 0) id = "mission:" + id;
            PlanItem ni;
            ni.id = id;
            ni.title = a->title;
            ni.kind = "task";
            ni.phase = "Mission";
            ni.status = "running";
            upsertDerivedItem(std::move(ni));
        }
    } else if (kind == "mission_task_result") {
        int midx = jint(data, "index");
        ActivityItem* a = findStage("task" + std::to_string(midx));
        if (a) {
            std::string s = jstr(data, "status");
            a->status = (s == "done") ? "done" : (s.empty() ? "done" : s);
            std::string summary = jstr(data, "summary");
            if (!summary.empty()) a->reason = summary;
        }
        // Live status mirror — id-matched only (mission:N or explicit id).
        {
            std::string id = jstr(data, "id");
            if (id.empty()) id = "mission:" + std::to_string(midx);
            else if (id.rfind("mission:", 0) != 0) id = "mission:" + id;
            std::string s = jstr(data, "status");
            std::string summary = jstr(data, "summary");
            if (PlanItem* it = findPlanItem(id)) {
                it->status = (s == "done" || s.empty()) ? "done" : s;
                if (!summary.empty()) it->desc = summary;
            } else {
                // Late result without a prior started — still surface the row.
                PlanItem ni;
                ni.id = id;
                ni.title = a ? a->title : ("Task " + std::to_string(midx + 1));
                ni.kind = "task";
                ni.phase = "Mission";
                ni.status = (s == "done" || s.empty()) ? "done" : s;
                if (!summary.empty()) ni.desc = summary;
                upsertDerivedItem(std::move(ni));
            }
        }
    } else if (kind == "mission_finished") {
        m.curMissionTask = -1;
        for (auto& a : m.activity) {
            if (a.rowKind == 2) {
                if (a.streaming || a.status == "running" || a.status.empty())
                    finalizeThoughtBox(a);
                continue;
            }
            if (a.status == "running" || a.status.empty()) {
                a.status = "done";
                a.streaming = false;
            }
        }
        // Close only mission:* task rows — leave edit:/firm:/step: writers alone.
        if (m.planState.present) {
            for (auto& it : m.planState.items) {
                if (it.id.rfind("mission:", 0) != 0 && it.kind != "task") continue;
                // Prefer id namespace; fall back to kind=task + phase Mission.
                bool isMission = (it.id.rfind("mission:", 0) == 0) ||
                                 (it.kind == "task" && it.phase == "Mission");
                if (!isMission) continue;
                if (it.status == "running" || it.status == "pending" || it.status.empty())
                    it.status = "done";
            }
        }
    } else if (kind == "stage_started") {
        // A stage boundary ends the reasoning burst AND the prose burst from the previous call.
        finalizeLiveStreamBoxes(m);
        std::string s = nsKey(jstr(data, "stage"));
        ActivityItem* a = findStage(s);
        if (!a) {
            ActivityItem ni;
            ni.stage = s;
            ni.title = friendlyStageTitle(jstr(data, "stage"));
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        a->status = "running";
        // Compact live "Saying" narration under the spinner (stage step text / role).
        {
            std::string say = jstr(data, "text");
            if (say.empty()) say = jstr(data, "step_desc");
            if (say.empty()) {
                std::string role = jstr(data, "role");
                std::string stg = jstr(data, "stage");
                if (!role.empty() && !stg.empty()) say = stg + " \xc2\xb7 " + role;
                else if (!stg.empty()) say = stg;
            }
            if (!say.empty() && m.running) m.liveStatus = say;
        }
    } else if (kind == "agent_output") {
        ActivityItem* a = findStage(nsKey(jstr(data, "stage")));
        if (a) {
            std::string raw = jstr(data, "raw");
            if (!raw.empty()) a->detail = raw;     // keep the latest attempt's thoughts/output
            // Pack sent prompt after a delimiter so drawActionCard can expand it without new
            // ActivityItem fields (app_state.h stays unchanged). Same pattern as ART packing.
            std::string sent = jstr(data, "sent");
            if (!sent.empty()) {
                // Strip any previous SENT trailer before re-packing (latest attempt wins).
                const char* kSentMark = "\n\x1e" "SENT\x1e\n";
                const char* kSentMarkBare = "\x1e" "SENT\x1e\n";
                size_t mark = a->detail.find(kSentMark);
                if (mark != std::string::npos) a->detail.resize(mark);
                else if (a->detail.rfind(kSentMarkBare, 0) == 0) a->detail.clear();
                if (!a->detail.empty()) a->detail += "\n\x1e" "SENT\x1e\n";
                else a->detail = "\x1e" "SENT\x1e\n";
                a->detail += sent;
            }
            a->durationMs = jint(data, "duration_ms");
            a->opsCount   = jint(data, "ops_count");
            if (data.contains("tokens") && data["tokens"].is_object()) {
                a->tokensIn  = jint(data["tokens"], "in");
                a->tokensOut = jint(data["tokens"], "out");
            }
        }
        // C++ path (batch 2a): provider reasoning rides on agent_output.thinking when a
        // dedicated agent.thought event was not emitted — surface as rowKind 2 thought box.
        {
            std::string thinking = jstr(data, "thinking");
            if (!thinking.empty()) {
                std::string pid = jstr(data, "stage");
                if (pid.empty()) pid = "out";
                const std::string key = "thought:out:" + nsKey(pid);
                ActivityItem* ta = nullptr;
                for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
                    if (it->rowKind == 2 && it->stage == key) { ta = &*it; break; }
                // Dedupe: the same reasoning often already streamed live into a "Thinking"
                // box (assistant_delta channel=thinking). Fold into that box instead of
                // stacking a second, identical display under it.
                if (!ta) {
                    for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it) {
                        if (it->rowKind != 2 || it->stage == key ||
                            it->stage == "stream:out") continue;
                        if (it->detail == thinking ||
                            (!it->detail.empty() &&
                             thinking.find(it->detail.substr(0, std::min<size_t>(it->detail.size(), 64))) == 0)) {
                            ta = &*it;
                            break;
                        }
                        if (it->stage.rfind("thought:out:", 0) == 0) break;  // only scan this stage's burst
                    }
                }
                if (!ta) {
                    ActivityItem ni;
                    ni.rowKind = 2;
                    ni.stage   = key;
                    ni.title   = "Thinking";
                    ni.expanded = true;   // open while the turn may still be running
                    m.activity.push_back(std::move(ni));
                    ta = &m.activity.back();
                }
                ta->detail = thinking;
                finalizeThoughtBox(*ta);
            }
        }
    } else if (kind == "stage_result") {
        ActivityItem* a = findStage(nsKey(jstr(data, "stage")));
        if (a) {
            a->status = jstr(data, "verdict");
            if (a->status.empty()) a->status = "done";
            a->reason = jstr(data, "reason");
            if (!a->reason.empty() && m.running) m.liveStatus = a->reason;
        }
    } else if (kind == "run_finished") {
        // A sub-run finished (one mission task) OR a single-run completed. Close only the rows
        // for the CURRENT task; mission_finished does the final sweep. Outside a mission this
        // closes everything, as before.
        for (auto& a : m.activity) {
            if (a.missionHeader) continue;
            if (a.rowKind == 2) {
                // Thought/output boxes: stop streaming; auto-collapse once unless user pinned.
                if (a.streaming || a.status == "running" || a.status.empty())
                    finalizeThoughtBox(a);
                // Promote streamed prose into the final bubble once (no double while running).
                if (a.stage == "stream:out" && a.opsCount <= 0 && !a.detail.empty() &&
                    m.text.empty()) {
                    m.text = a.detail;
                    m.uiCachedH = -1.0f;
                }
                continue;
            }
            if (a.status == "running" || a.status.empty()) {
                a.status = "done";
                a.streaming = false;
            }
        }
        // Capture additive early-stop fields (ErrorDetail / contracts): stop_reason +
        // error_detail + error_human. Reason/detail are stashed as a meta activity row so the
        // error card can render "stopped: <reason> — <detail>"; error_human is the short
        // actionable sentence and lands on the message itself as the card's headline.
        {
            std::string sr = jstr(data, "stop_reason");
            std::string ed = jstr(data, "error_detail");
            if (ed.empty()) ed = jstr(data, "error");  // soft alias
            const std::string eh = jstr(data, "error_human");
            if (!eh.empty()) m.errorHuman = eh;
            if (!sr.empty() || !ed.empty()) {
                ActivityItem* meta = nullptr;
                for (auto& a : m.activity)
                    if (a.stage == "__stop_meta__") { meta = &a; break; }
                if (!meta) {
                    ActivityItem ni;
                    ni.stage = "__stop_meta__";
                    ni.rowKind = 0;
                    ni.status = "done";
                    ni.missionHeader = false;
                    m.activity.push_back(std::move(ni));
                    meta = &m.activity.back();
                }
                if (!sr.empty()) meta->title = sr;
                if (!ed.empty()) meta->detail = ed;
                if (!ed.empty()) meta->reason = ed;
            }
            // Prefer a model id carried on the terminal event over the stamp at run start.
            {
                std::string mid = jstr(data, "model");
                if (mid.empty()) mid = jstr(data, "model_id");
                if (!mid.empty()) m.model = std::move(mid);
            }
        }
    } else if (kind == "status") {
        // Live status/phase envelope: phase=done|error may carry the same additive fields.
        // Non-terminal phases also refresh the compact "Saying" line under the spinner.
        const std::string phase = jstr(data, "phase");
        const std::string statusText = jstr(data, "text");
        if (!statusText.empty() && m.running &&
            phase != "done" && phase != "error" && phase != "failed" && phase != "cancelled") {
            m.liveStatus = statusText;
        }
        if (phase == "done" || phase == "error" || phase == "failed" || phase == "cancelled") {
            std::string sr = jstr(data, "stop_reason");
            std::string ed = jstr(data, "error_detail");
            if (ed.empty()) ed = jstr(data, "error");
            const std::string eh = jstr(data, "error_human");
            if (!eh.empty()) m.errorHuman = eh;
            if (!sr.empty() || !ed.empty()) {
                ActivityItem* meta = nullptr;
                for (auto& a : m.activity)
                    if (a.stage == "__stop_meta__") { meta = &a; break; }
                if (!meta) {
                    ActivityItem ni;
                    ni.stage = "__stop_meta__";
                    ni.rowKind = 0;
                    ni.status = "done";
                    m.activity.push_back(std::move(ni));
                    meta = &m.activity.back();
                }
                if (!sr.empty()) meta->title = sr;
                if (!ed.empty()) { meta->detail = ed; meta->reason = ed; }
            }
        }
    }
    // ---- M12-M14: typed transparency rows from the aicad.agent_events/2 stream ----
    // These arrive through the SAME app.cpp drain as the stage events above (kind already has the
    // "orch_" prefix stripped; `data` is the parsed /2 record with envelope + payload fields at
    // top level). Each event appends a typed row at its ARRIVAL POSITION in the running turn's
    // timeline, interleaving naturally with the stage rows. Span-id keys ("ctx:"/"thought:"/
    // "tool:" + id) can never collide with the orchestrator stage keys.
    else if (kind == "agent.context_injected") {                     // M12 — context chip
        // A context chip is a visible timeline row.  Close any thinking that
        // arrived before it so later deltas cannot be appended into a box that
        // renders above the chip.
        finalizeLiveStreamBoxes(m);
        ActivityItem ni;
        ni.rowKind = 1;
        ni.stage   = "ctx:" + jstr(data, "id");
        ni.title   = jstr(data, "label");
        if (ni.title.empty()) ni.title = "context";
        ni.reason    = jstr(data, "source");
        ni.ctxTokens = jint(data, "token_est");
        ni.status    = "done";
        m.activity.push_back(std::move(ni));
    } else if (kind == "agent.thought.delta" || kind == "agent.thought") {  // M13 — thought box
        // Deltas sharing a parent span coalesce into ONE growing block, finalized by the matching
        // completed agent.thought (contract §3.2). The provider seam is non-streaming today, so
        // in practice whole agent.thought blocks land per-block on arrival; the delta path is
        // coded so live token streaming lights up the moment a streaming provider ships.
        std::string pid = jstr(data, "parent_id");
        if (pid.empty()) pid = jstr(data, "node_id");
        const std::string key = "thought:" + pid;
        ActivityItem* a = nullptr;
        for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
            if (it->rowKind == 2 && it->stage == key && it->streaming) { a = &*it; break; }
        if (!a && kind == "agent.thought") {
            // Dedupe vs the live stream: this completed block usually carries the
            // SAME reasoning assistant_delta channel=thinking already rendered
            // (possibly truncated per contract). Fold into that box instead of
            // stacking a second, identical Thinking block that also keeps pulsing.
            const std::string blk = jstr(data, "text");
            for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it) {
                if (it->rowKind != 2 || it->stage.rfind("thought:", 0) != 0) continue;
                const std::string& d = it->detail;
                if (d.empty() || blk.empty()) continue;
                const size_t n = std::min<size_t>(std::min(d.size(), blk.size()), 64);
                if (d.compare(0, n, blk, 0, n) == 0) { a = &*it; break; }
            }
        }
        // A completed thought block = the call's reasoning is done — stop the
        // live box from pulsing even when the dedupe above found no twin.
        if (kind == "agent.thought") finalizeLiveStreamBoxes(m);
        if (!a) {
            ActivityItem ni;
            ni.rowKind = 2;
            ni.stage   = key;
            ni.title   = "Thinking";
            ni.expanded = true;  // open + populating live
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        if (kind == "agent.thought.delta") {
            a->detail += jstr(data, "text");
            a->streaming = true;
            a->status    = "running";
            if (!a->userToggled) a->expanded = true;
        } else {
            // Completed block: authoritative text replaces any delta accumulation (same content,
            // possibly truncated per contract), then finalize — collapse once unless user pinned.
            std::string txt = jstr(data, "text");
            if (!txt.empty() || a->detail.empty()) a->detail = txt;
            finalizeThoughtBox(*a);
        }
    } else if (kind == "assistant_delta") {  // live provider token stream
        // A turn is ONE ordered column, and the prose box is now a visible row in it, so the
        // only question here is which box this delta joins. The rules — and the counter-
        // pressure that a contentless channel flush must NOT split one reasoning stream —
        // live in stream_rows.h, where a ctest pins them.
        const std::string chan = jstr(data, "channel");
        const std::string txt = jstr(data, "text");
        const bool think = (chan == "thinking");
        const bool visible = ui::streamTextVisible(txt);
        const std::string key = think ? "thought:stream" : "stream:out";

        // Ordered view of this turn's live-stream boxes (indices back into m.activity).
        std::vector<ui::StreamRowState> rows;
        std::vector<size_t> rowAt;
        rows.reserve(8);
        rowAt.reserve(8);
        for (size_t i = 0; i < m.activity.size(); ++i) {
            const ActivityItem& r = m.activity[i];
            if (r.rowKind != 2) continue;
            const bool isThink = (r.stage == "thought:stream");
            if (!isThink && r.stage != "stream:out") continue;
            ui::StreamRowState s;
            s.thinking  = isThink;
            s.ops       = !isThink && (r.opsCount > 0 ||
                                       r.title.find("Building ops") != std::string::npos);
            s.streaming = r.streaming;
            s.tail      = (i + 1 == m.activity.size());
            rows.push_back(s);
            rowAt.push_back(i);
        }
        // A channel switch carrying real content IS a visible boundary: close the other
        // channel's live box so this burst, and the next one on that channel, land in
        // arrival order instead of growing a box already drawn above.
        if (visible) {
            const int close = ui::streamChannelBoundary(rows, think);
            if (close >= 0) {
                finalizeThoughtBox(m.activity[rowAt[(size_t)close]]);
                rows[(size_t)close].streaming = false;
            }
        }
        const int target = ui::streamDeltaTarget(rows, think);
        ActivityItem* a = (target >= 0) ? &m.activity[rowAt[(size_t)target]] : nullptr;
        // tokensIn on stream:out = byte offset into m.text where THIS segment began (for
        // stripping JSON that already leaked into the prose bubble when classification flips).
        // opsCount > 0  => classified as OPS JSON (Building ops… mode).
        const size_t mTextBefore = m.text.size();
        if (!a && visible) {
            ActivityItem ni;
            ni.rowKind = 2;
            ni.stage   = key;
            ni.title   = think ? "Thinking" : "Output";
            // Thinking opens while streaming (auto-collapses on complete unless userToggled).
            // A prose box renders as the message bubble itself, so `expanded` is irrelevant
            // to it; an ops-JSON box collapses to its summary once the flip is detected.
            ni.expanded = think;
            if (!think) ni.tokensIn = (int)mTextBefore;  // segment start in m.text
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        // No box and nothing to add: a contentless flush must not manufacture an empty row
        // between the two halves of one burst.
        if (a && think) {
            if (!txt.empty()) a->detail += txt;
            a->streaming = true;
            a->status    = "running";
            if (!a->userToggled) a->expanded = true;
            m.uiCachedH = -1.0f;
        } else if (a) {
            // Text channel: `detail` is the box's own copy of this prose segment — the
            // timeline renders it in place — and m.text stays the turn's canonical full
            // reply for copy, persistence and the context estimate. The two are kept byte
            // -identical so chat_log can tell what the timeline already showed.
            if (!txt.empty()) a->detail += txt;
            a->streaming = true;   // re-opens a finalized (coalesced) ops box
            a->status    = "running";
            const bool wasOps = (a->opsCount > 0);
            const size_t opsAt = wasOps ? 0 : findOpsJsonStart(a->detail);
            const bool isOps = wasOps || (opsAt != std::string::npos);
            if (isOps) {
                if (!wasOps) {
                    // Classification just flipped. Drop any prose already in m.text for
                    // this segment (legacy dual-write); keep the ops box as sole surface.
                    const size_t segStart = (a->tokensIn > 0) ? (size_t)a->tokensIn : 0;
                    if (segStart <= m.text.size()) m.text.resize(segStart);
                    a->title = "Building ops\xe2\x80\xa6";  // "Building ops…"
                    a->expanded = false;  // summary collapsed; raw only in expander
                    a->opsCount = 1;      // flag: ops-JSON mode (updated with real count below)
                    // If detail still holds leading prose, drop it from the ops box body so the
                    // expander shows JSON only.
                    if (opsAt != std::string::npos && opsAt > 0 && opsAt <= a->detail.size())
                        a->detail.erase(0, opsAt);
                }
                // Refresh one-line summary into reason; keep raw JSON in detail.
                int nOps = countOpsInJsonPrefix(a->detail);
                if (nOps > 0) a->opsCount = nOps;
                const std::string opLbl = lastOpLabelInJsonPrefix(a->detail);
                if (nOps > 0) {
                    char buf[64];
                    std::snprintf(buf, sizeof(buf), "%d op%s", nOps, nOps == 1 ? "" : "s");
                    a->reason = buf;
                } else {
                    char buf[64];
                    std::snprintf(buf, sizeof(buf), "%zu bytes", a->detail.size());
                    a->reason = buf;
                }
                if (!opLbl.empty()) { a->reason += " \xc2\xb7 "; a->reason += opLbl; }
                // Do NOT append this delta (or any further ops JSON) to m.text.
                m.uiCachedH = -1.0f;
            } else {
                // Prose: also accumulate into m.text, the turn's canonical reply. The box
                // above is what the user SEES (drawn at this segment's arrival position);
                // m.text is what Copy, the persisted transcript and the context estimate
                // read. run_finished's promote-once is guarded by m.text.empty() so it
                // never doubles, and the ops-classification flip above strips this segment
                // back out of m.text if the stream turns out to be ops JSON.
                if (!txt.empty()) {
                    m.text += txt;
                    m.uiCachedH = -1.0f;
                }
            }
        }
    } else if (kind == "agent.tool_call") {                          // M14 — tool call row
        // A tool call is an action — the reasoning burst that preceded it is over.
        finalizeLiveStreamBoxes(m);
        // Upsert by span id so re-delivered/duplicate events update in place.
        std::string tid = jstr(data, "id");
        if (tid.empty()) tid = jstr(data, "span_id");
        const std::string stageKey = "tool:" + tid;
        ActivityItem* existingCard = nullptr;
        if (!tid.empty()) {
            for (auto& a : m.activity)
                if (a.rowKind == 3 && a.stage == stageKey) { existingCard = &a; break; }
        }
        ActivityItem scratch;
        ActivityItem& ni = existingCard ? *existingCard : scratch;
        ni.rowKind = 3;
        ni.stage   = stageKey;
        ni.title   = jstr(data, "tool_name");
        if (ni.title.empty()) ni.title = "tool";
        // ni.reason / ni.detail are authored by the Python harness or by the model, so they can
        // carry codepoints the font atlas has no glyph for — agent_events.py joins tool targets
        // with U+2192, which rendered as "DevicePocket ? MouthpieceSlot ?+2 more". Folded HERE,
        // at ingest, so the persisted transcript is clean too. The companion doc-id -> part-name
        // substitution runs at draw time (activity_timeline.cpp): it needs AppState, which this
        // TU does not have. Both passes are idempotent.
        ni.reason     = ui::foldToAtlasGlyphs(jstr(data, "target"));  // human "what it acts on"
        // Doing drawer: args_preview from batch 2a (capped JSON/text). Fall back to args dump.
        ni.detail     = ui::foldToAtlasGlyphs(jstr(data, "args_preview"));
        if (ni.detail.empty() && data.contains("args")) {
            if (data["args"].is_string()) ni.detail = data["args"].get<std::string>();
            else if (data["args"].is_object() || data["args"].is_array())
                ni.detail = data["args"].dump(2);
        }
        ni.durationMs = jint(data, "latency_ms");
        const bool legacyOk = !data.is_object() || data.value("ok", true);
        const ui::copilot::ToolOutcomeKind outcome =
            ui::copilot::parseToolOutcomeKind(jstr(data, "outcome_kind"));
        const bool executed = data.is_object() && data.contains("executed") &&
                              data["executed"].is_boolean() && data["executed"].get<bool>();
        // The structured taxonomy is authoritative when present. Missing, malformed, and future
        // values take the exact legacy ok -> done/fail path so replayed sessions never acquire a
        // fabricated UNKNOWN state.
        switch (outcome) {
            case ui::copilot::ToolOutcomeKind::Ok:
                ni.status = "done";
                break;
            case ui::copilot::ToolOutcomeKind::PolicyRefusal:
                ni.status = "skipped";
                break;
            case ui::copilot::ToolOutcomeKind::GuardBlock:
                ni.status = "skipped";
                break;
            case ui::copilot::ToolOutcomeKind::ValidationError:
                ni.status = "fail";
                break;
            case ui::copilot::ToolOutcomeKind::TimeoutUnknown:
                ni.status = "retry";
                break;
            case ui::copilot::ToolOutcomeKind::InternalError:
                ni.status = "fail";
                break;
            case ui::copilot::ToolOutcomeKind::ToolError:
                ni.status = "fail";
                break;
            case ui::copilot::ToolOutcomeKind::Legacy:
            default:
                ni.status = legacyOk ? "done" : "fail";
                break;
        }
        // Preserve the taxonomy ahead of potentially long error/artifact payloads so ActivityItem
        // persistence caps retain it. Unknown/missing values get no trailer and remain legacy.
        ui::copilot::appendToolOutcomeTrailer(ni.detail, outcome, executed);
        // Optional error_preview (capped failure text) — packed after the args body with the
        // same delimiter convention as artifacts below so drawToolCallRow can split it back
        // out without a new ActivityItem field (app_state.h stays unchanged).
        if (ni.status != "done") {
            const std::string errText = jstr(data, "error_preview");
            if (!errText.empty()) {
                if (!ni.detail.empty()) ni.detail += "\n\x1e" "ERR\x1e\n";
                else ni.detail = "\x1e" "ERR\x1e\n";
                ni.detail += errText;
            }
        }
        // Optional artifacts:[{path,kind}] — pack image paths into detail after a delimiter so
        // drawToolCallRow can expand them without new ActivityItem fields (app_state.h
        // stays unchanged). Delimiter is unlikely in args JSON: "\n\x1eART\x1e\n".
        // image_caption — the text the tool returned WITH its images (what the agent read beside
        // the pixels) — rides just ahead of ART after "\n\x1eCAP\x1e\n". It precedes ART so the
        // session-artifact scan (conversations.cpp, ART to end of detail) never reads caption
        // lines as file paths.
        if (data.contains("artifacts") && data["artifacts"].is_array()) {
            std::string arts;
            int n = 0;
            bool anyImage = false;
            for (const auto& art : data["artifacts"]) {
                if (!art.is_object() || n >= 8) break;
                std::string path = jstr(art, "path");
                std::string kindA = jstr(art, "kind");
                if (path.empty()) continue;
                if (kindA != "image" && kindA != "mesh" && kindA != "file") kindA = "file";
                if (kindA == "image") anyImage = true;
                if (!arts.empty()) arts += "\n";
                arts += kindA;
                arts += '\t';
                arts += path;
                ++n;
            }
            if (!arts.empty()) {
                const std::string caption =
                    anyImage ? ui::foldToAtlasGlyphs(jstr(data, "image_caption")) : std::string();
                if (!caption.empty()) {
                    if (!ni.detail.empty()) ni.detail += "\n\x1e" "CAP\x1e\n";
                    else ni.detail = "\x1e" "CAP\x1e\n";
                    ni.detail += caption;
                }
                if (!ni.detail.empty()) ni.detail += "\n\x1e" "ART\x1e\n";
                else ni.detail = "\x1e" "ART\x1e\n";
                ni.detail += arts;
            }
        }
        // Optional per-call META trailer: wall-clock stamp (ingest time — the
        // backend ships no clock) + the prompt-cache read of the completion that
        // produced this call (usage.cache_read, when the provider reports cache
        // counts). Appended LAST so splitToolRowDetail can lift it off the tail.
        {
            int cacheRead = 0;
            if (data.contains("usage") && data["usage"].is_object())
                cacheRead = jint(data["usage"], "cache_read");
            char meta[64];
            std::snprintf(meta, sizeof(meta), "\n\x1e" "META\x1e\n%lld\t%d",
                          (long long)chatNowEpoch(), cacheRead);
            ni.detail += meta;
        }
        // Pretty Doing card lands — close any live "Building ops…" stream:out so the
        // transcript shows the card, not both actively (F1 companion).
        for (auto& existing : m.activity) {
            if (existing.rowKind != 2) continue;
            if (existing.stage != "stream:out") continue;
            if (existing.opsCount <= 0 && existing.title.find("Building ops") == std::string::npos)
                continue;
            existing.streaming = false;
            existing.status = "done";
            existing.expanded = false;
        }
        if (!existingCard) m.activity.push_back(std::move(scratch));
    } else if (kind == "provider_call") {
        // In-depth provider HTTP call log (InAppDiag / core agent). One row per attempt.
        // rowKind 9; fields packed into existing ActivityItem slots so we never touch
        // the shared app_state.h:
        //   title      = "provider · model" (display)
        //   reason     = single-line summary (status + latency + endpoint snippet)
        //   detail     = full copyable line (+ error_excerpt when present)
        //   durationMs = latency_ms
        //   tokensIn   = http_status (0 = transport fail)
        //   opsCount   = attempt (1-based)
        //   status     = "done" | "fail" from ok / http_status
        ActivityItem ni;
        ni.rowKind = 9;
        const std::string prov = jstr(data, "provider");
        const std::string model = jstr(data, "model");
        const std::string endpoint = jstr(data, "endpoint");
        const std::string reqId = jstr(data, "request_id");
        const std::string errEx = jstr(data, "error_excerpt");
        int httpStatus = jint(data, "http_status");
        // Tolerate float-ish JSON numbers (some emitters use double).
        if (httpStatus == 0 && data.contains("http_status") && data["http_status"].is_number())
            httpStatus = (int)data["http_status"].get<double>();
        int latency = jint(data, "latency_ms");
        if (latency == 0 && data.contains("latency_ms") && data["latency_ms"].is_number())
            latency = (int)data["latency_ms"].get<double>();
        int attempt = jint(data, "attempt");
        if (attempt == 0 && data.contains("attempt") && data["attempt"].is_number())
            attempt = (int)data["attempt"].get<double>();
        if (attempt <= 0) attempt = 1;
        bool ok = true;
        if (data.contains("ok") && data["ok"].is_boolean())
            ok = data["ok"].get<bool>();
        else if (httpStatus != 0)
            ok = (httpStatus >= 200 && httpStatus < 300);
        else if (!errEx.empty())
            ok = false;

        std::string head;
        if (!prov.empty() && !model.empty()) head = prov + " \xc2\xb7 " + model;
        else if (!prov.empty()) head = prov;
        else if (!model.empty()) head = model;
        else head = "provider";
        ni.title = head;

        char statusBuf[32];
        if (httpStatus > 0)
            std::snprintf(statusBuf, sizeof(statusBuf), "HTTP %d", httpStatus);
        else
            std::snprintf(statusBuf, sizeof(statusBuf), "transport");

        std::string summary = statusBuf;
        if (latency > 0) {
            char lb[32];
            std::snprintf(lb, sizeof(lb), " %dms", latency);
            summary += lb;
        }
        if (attempt > 1) {
            char ab[24];
            std::snprintf(ab, sizeof(ab), " try#%d", attempt);
            summary += ab;
        }
        if (!endpoint.empty()) {
            summary += " ";
            // Keep endpoint short so the one-liner fits the chat column.
            if (endpoint.size() > 48) summary += endpoint.substr(0, 45) + "...";
            else summary += endpoint;
        }
        ni.reason = summary;

        // Full line for copy / expanded body.
        std::string full = head + "  " + summary;
        if (!reqId.empty()) { full += "  id="; full += reqId; }
        if (!errEx.empty()) { full += "\n  "; full += errEx; }
        ni.detail = full;

        ni.durationMs = latency;
        ni.tokensIn = httpStatus;
        ni.opsCount = attempt;
        ni.status = ok ? "done" : "fail";
        ni.stage = "prov:" + std::to_string(m.activity.size());
        m.activity.push_back(std::move(ni));
    } else if (kind == "corporate") {                               // Corporate Roles live UI
        // Additive aicad.corporate/1 stream: corp_event + optional dept/agent/payload.
        // Same drain as stage/tool events (app.cpp strips orch_ prefix).
        feedCorporateEvent(m, data);
    } else if (kind == "swarm_wave") {
        // Wave header: "Wave N/M" + task titles with status. One card per wave start.
        ActivityItem ni;
        ni.rowKind = 11;
        int wave = jint(data, "wave");
        int total = jint(data, "total_waves");
        if (wave <= 0 && data.contains("wave") && data["wave"].is_number())
            wave = (int)data["wave"].get<double>();
        if (total <= 0 && data.contains("total_waves") && data["total_waves"].is_number())
            total = (int)data["total_waves"].get<double>();
        char title[80];
        if (total > 0)
            std::snprintf(title, sizeof(title), "Wave %d/%d", wave, total);
        else
            std::snprintf(title, sizeof(title), "Wave %d", wave > 0 ? wave : 1);
        ni.title = title;
        ni.stage = "swarm:wave:" + std::to_string(wave > 0 ? wave : (int)m.activity.size());
        ni.status = "running";
        ni.tokensIn = wave;
        ni.tokensOut = total;
        // Pack tasks into detail JSON for the card renderer.
        nlohmann::json pack = nlohmann::json::object();
        pack["wave"] = wave;
        pack["total_waves"] = total;
        nlohmann::json tasks = nlohmann::json::array();
        std::string reason;
        if (data.contains("tasks") && data["tasks"].is_array()) {
            for (const auto& t : data["tasks"]) {
                if (!t.is_object()) continue;
                nlohmann::json row = nlohmann::json::object();
                row["id"] = jstr(t, "id");
                std::string tt = jstr(t, "title");
                if (tt.empty()) tt = jstr(t, "name");
                if (tt.empty()) tt = jstr(t, "capability");
                row["title"] = tt;
                std::string stt = jstr(t, "status");
                if (stt.empty()) stt = "pending";
                row["status"] = stt;
                tasks.push_back(std::move(row));
                if (!reason.empty()) reason += " \xc2\xb7 ";
                reason += tt.empty() ? "?" : tt;
            }
        }
        pack["tasks"] = std::move(tasks);
        ni.detail = pack.dump();
        // Cap reason length for the one-liner under the title.
        if (reason.size() > 120) { reason.resize(117); reason += "\xe2\x80\xa6"; }
        ni.reason = reason;
        m.activity.push_back(std::move(ni));
        if (m.running) m.liveStatus = ni.title;
    } else if (kind == "firm_bids") {
        // Contractor bidding card: firm names and estimated token counts.
        ActivityItem ni;
        ni.rowKind = 12;
        ni.stage = "firm:bids";
        ni.title = "Firm bids";
        ni.status = "done";
        std::string domain = jstr(data, "domain");
        std::string complexity = jstr(data, "complexity");
        char reason[160];
        if (!domain.empty() || !complexity.empty())
            std::snprintf(reason, sizeof(reason), "%s%s%s",
                          domain.c_str(),
                          (!domain.empty() && !complexity.empty()) ? " \xc2\xb7 " : "",
                          complexity.c_str());
        else
            std::snprintf(reason, sizeof(reason), "collecting bids");
        ni.reason = reason;
        // Store a compact structured payload (not raw event dump) for the card.
        nlohmann::json pack = nlohmann::json::object();
        pack["domain"] = domain;
        pack["complexity"] = complexity;
        nlohmann::json bids = nlohmann::json::array();
        if (data.contains("bids") && data["bids"].is_array()) {
            for (const auto& b : data["bids"]) {
                if (!b.is_object()) continue;
                nlohmann::json row = nlohmann::json::object();
                std::string firm = jstr(b, "firm");
                if (firm.empty()) firm = jstr(b, "firm_name");
                if (firm.empty()) firm = jstr(b, "firm_id");
                row["firm"] = firm;
                if (b.contains("tokens") && b["tokens"].is_number())
                    row["tokens"] = b["tokens"];
                else if (b.contains("tokens_est") && b["tokens_est"].is_number())
                    row["tokens"] = b["tokens_est"];
                if (b.contains("in_scope")) row["in_scope"] = b["in_scope"];
                bids.push_back(std::move(row));
            }
        }
        pack["bids"] = std::move(bids);
        // Also surface the award when the emitter packs it on the same event.
        if (data.contains("awarded_firm")) pack["awarded_firm"] = data["awarded_firm"];
        if (data.contains("awarded_tokens")) pack["awarded_tokens"] = data["awarded_tokens"];
        ni.detail = pack.dump();
        // Upsert single bids card (replace prior if re-emitted).
        bool replaced = false;
        for (auto& a : m.activity) {
            if (a.rowKind == 12 && a.stage == "firm:bids") {
                a = ni;
                replaced = true;
                break;
            }
        }
        if (!replaced) m.activity.push_back(std::move(ni));
        if (m.running) m.liveStatus = "Collecting firm bids...";
    } else if (kind == "firm_award") {
        ActivityItem ni;
        ni.rowKind = 13;
        ni.stage = "firm:award:" + std::to_string(m.activity.size());
        std::string firm = jstr(data, "firm");
        if (firm.empty()) firm = jstr(data, "firm_name");
        if (firm.empty()) firm = jstr(data, "awarded_firm");
        ni.title = firm.empty() ? "Firm awarded" : ("Awarded " + firm);
        long bidTokens = 0;
        if (data.contains("tokens") && data["tokens"].is_number_integer())
            bidTokens = data["tokens"].get<long>();
        else if (data.contains("tokens") && data["tokens"].is_number())
            bidTokens = (long)data["tokens"].get<double>();
        std::string reason = jstr(data, "reason");
        char line[192];
        if (bidTokens > 0 && !reason.empty())
            std::snprintf(line, sizeof(line), "%ld estimated tokens \xc2\xb7 %s", bidTokens,
                          reason.c_str());
        else if (bidTokens > 0)
            std::snprintf(line, sizeof(line), "%ld estimated tokens", bidTokens);
        else if (!reason.empty())
            std::snprintf(line, sizeof(line), "%s", reason.c_str());
        else
            std::snprintf(line, sizeof(line), "prime contractor selected");
        ni.reason = line;
        ni.detail = reason;
        ni.status = "done";
        m.activity.push_back(std::move(ni));
        if (m.running) m.liveStatus = firm.empty() ? "Firm awarded" : ("Awarded " + firm);
    } else if (kind == "firm_team") {
        ActivityItem ni;
        ni.rowKind = 14;
        ni.stage = "firm:team";
        ni.title = "Firm team";
        ni.status = "done";
        nlohmann::json pack = nlohmann::json::object();
        nlohmann::json members = nlohmann::json::array();
        int nLead = 0, nMem = 0;
        if (data.contains("members") && data["members"].is_array()) {
            for (const auto& mem : data["members"]) {
                if (!mem.is_object()) continue;
                nlohmann::json row = nlohmann::json::object();
                std::string role = jstr(mem, "role");
                if (role.empty()) role = jstr(mem, "role_title");
                std::string name = jstr(mem, "name");
                if (name.empty()) name = jstr(mem, "firm");
                if (name.empty()) name = jstr(mem, "firm_name");
                row["role"] = role;
                row["name"] = name;
                if (mem.contains("firm") && mem["firm"].is_string())
                    row["firm"] = mem["firm"];
                else if (mem.contains("firm_name") && mem["firm_name"].is_string())
                    row["firm"] = mem["firm_name"];
                bool lead = false;
                if (mem.contains("lead") && mem["lead"].is_boolean())
                    lead = mem["lead"].get<bool>();
                row["lead"] = lead;
                if (lead) ++nLead; else ++nMem;
                members.push_back(std::move(row));
            }
        }
        pack["members"] = std::move(members);
        ni.detail = pack.dump();
        char reason[96];
        std::snprintf(reason, sizeof(reason), "%d member%s%s",
                      nLead + nMem, (nLead + nMem) == 1 ? "" : "s",
                      nLead > 0 ? " \xc2\xb7 lead assigned" : "");
        ni.reason = reason;
        // Upsert single team card.
        bool replaced = false;
        for (auto& a : m.activity) {
            if (a.rowKind == 14 && a.stage == "firm:team") {
                a = ni;
                replaced = true;
                break;
            }
        }
        if (!replaced) m.activity.push_back(std::move(ni));
        if (m.running) m.liveStatus = "Assembling firm team...";
    } else if (kind == "firm_comm") {
        // Internal contractor comms — ONE collapsed-by-default expander; append lines.
        ActivityItem* thr = nullptr;
        for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
            if (it->rowKind == 15 && it->stage == "firm:comm") { thr = &*it; break; }
        if (!thr) {
            ActivityItem ni;
            ni.rowKind = 15;
            ni.stage = "firm:comm";
            ni.title = "Internal comms";
            ni.status = "done";
            ni.expanded = false;  // collapsed by default
            ni.detail = "";
            m.activity.push_back(std::move(ni));
            thr = &m.activity.back();
        }
        std::string from = jstr(data, "from");
        std::string to = jstr(data, "to");
        std::string text = jstr(data, "text");
        if (text.empty()) text = jstr(data, "message");
        if (text.empty()) return;  // nothing to append
        std::string line;
        if (!from.empty() && !to.empty())
            line = from + " \xe2\x86\x92 " + to + ": " + text;
        else if (!from.empty())
            line = from + ": " + text;
        else
            line = text;
        if (!thr->detail.empty()) thr->detail += "\n";
        thr->detail += line;
        // Cap stored thread to ~32 KB so a chatty firm can't bloat the turn forever.
        if (thr->detail.size() > 32768) {
            size_t cut = thr->detail.size() - 30000;
            size_t nl = thr->detail.find('\n', cut);
            if (nl != std::string::npos) thr->detail.erase(0, nl + 1);
            else thr->detail.erase(0, cut);
        }
        // Count lines into opsCount for the header chip.
        int nLines = 1;
        for (char c : thr->detail) if (c == '\n') ++nLines;
        thr->opsCount = nLines;
        char reason[64];
        std::snprintf(reason, sizeof(reason), "%d message%s", nLines, nLines == 1 ? "" : "s");
        thr->reason = reason;
        thr->status = "done";
        // Do NOT bump liveStatus on every comm — too noisy.
    } else if (kind == "advisor") {
        // Advisor card. Two flavors:
        //  1) Mid-run consult (stage/question/advice) — expanded default.
        //  2) Agent-decided plan approval verdict (approved/approve/verdict) — informational
        //     "Advisor-approved plan" card, collapsed-by-default, NO action buttons.
        ActivityItem ni;
        ni.rowKind = 16;
        ni.stage = "advisor:" + std::to_string(m.activity.size());
        std::string stg = jstr(data, "stage");
        if (stg.empty()) stg = jstr(data, "parent_stage");
        std::string question = jstr(data, "question");
        std::string advice = jstr(data, "advice");
        if (advice.empty()) advice = jstr(data, "text");
        if (advice.empty()) advice = jstr(data, "note");
        if (advice.empty()) advice = jstr(data, "verdict");
        std::string verdict = jstr(data, "verdict");
        if (verdict.empty()) verdict = jstr(data, "decision");
        // Detect plan-approval flavor: explicit flag, stage name, or verdict keywords.
        std::string vlow = verdict;
        for (char& c : vlow) c = (char)std::tolower((unsigned char)c);
        std::string stglow = stg;
        for (char& c : stglow) c = (char)std::tolower((unsigned char)c);
        const bool planApproval =
            jbool(data, "plan_approved") ||
            jbool(data, "approved") ||
            stglow.find("plan") != std::string::npos ||
            vlow == "approved" || vlow == "approve" || vlow == "ok" ||
            vlow.find("approv") != std::string::npos;
        if (planApproval) {
            ni.title = "Advisor-approved plan";
            ni.reason = !verdict.empty() ? verdict : (!advice.empty() ? advice : "approved");
            // Prefer a short plan summary in detail when present.
            std::string summary = jstr(data, "summary");
            if (summary.empty() && data.contains("plan")) {
                if (data["plan"].is_string()) summary = data["plan"].get<std::string>();
                else if (data["plan"].is_object()) {
                    summary = jstr(data["plan"], "summary");
                    if (summary.empty()) summary = jstr(data["plan"], "title");
                    if (summary.empty()) summary = jstr(data["plan"], "name");
                }
            }
            if (summary.empty()) summary = advice;
            if (!question.empty() && summary.find(question) == std::string::npos) {
                if (!summary.empty()) summary = question + "\n" + summary;
                else summary = question;
            }
            ni.detail = summary;
            ni.status = "done";
            ni.expanded = false;  // collapsed-by-default informational
            ni.opsCount = 1;      // flag: plan-approval flavor for the drawer
        } else {
            ni.title = stg.empty() ? "Advisor" : ("Advisor \xc2\xb7 " + stg);
            ni.reason = question;
            ni.detail = advice;
            ni.status = "done";
            ni.expanded = true;  // consult: expanded by default
            ni.opsCount = 0;
        }
        m.activity.push_back(std::move(ni));
        if (m.running) {
            if (planApproval) {
                m.liveStatus = "Advisor approved plan \xe2\x80\x94 executing\xe2\x80\xa6";
            } else if (!advice.empty()) {
                std::string ls = advice;
                if (ls.size() > 80) { ls.resize(77); ls += "\xe2\x80\xa6"; }
                m.liveStatus = "Advisor: " + ls;
            }
        }
    } else if (kind == "sub_run") {
        // Compact card: purpose + status. Upsert by id when present.
        std::string id = jstr(data, "id");
        std::string purpose = jstr(data, "purpose");
        if (purpose.empty()) purpose = jstr(data, "title");
        if (purpose.empty()) purpose = jstr(data, "name");
        std::string parent = jstr(data, "parent_stage");
        std::string stt = jstr(data, "status");
        if (stt.empty()) stt = "running";
        ActivityItem* existing = nullptr;
        if (!id.empty()) {
            const std::string key = "subrun:" + id;
            for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
                if (it->rowKind == 17 && it->stage == key) { existing = &*it; break; }
        }
        const std::string norm = (stt == "done" || stt == "pass") ? "done"
                               : (stt == "fail" || stt == "failed" || stt == "error") ? "fail"
                               : (stt == "running" || stt == "started") ? "running"
                               : stt;
        // The status line names live work only. A terminal sub-run used to leave
        // "Agent · start (done)" behind as the run's status for the rest of a silent wait.
        std::string closedLive;
        if (!existing) {
            ActivityItem ni;
            ni.rowKind = 17;
            ni.stage = id.empty() ? ("subrun:" + std::to_string(m.activity.size()))
                                 : ("subrun:" + id);
            ni.title = purpose.empty() ? "Sub-run" : purpose;
            if (!parent.empty()) {
                ni.reason = parent;
                if (!stt.empty()) ni.reason += " \xc2\xb7 " + stt;
            } else {
                ni.reason = stt;
            }
            ni.status = norm;
            if (norm == "running") ni.startedTs = chatNowEpoch();
            ni.detail = purpose;
            m.activity.push_back(std::move(ni));
        } else {
            if (existing->status == "running" && norm != "running") closedLive = existing->title;
            if (existing->status != "running" && norm == "running")
                existing->startedTs = chatNowEpoch();
            if (!purpose.empty()) existing->title = purpose;
            if (!parent.empty()) {
                existing->reason = parent;
                if (!stt.empty()) existing->reason += " \xc2\xb7 " + stt;
            } else if (!stt.empty()) {
                existing->reason = stt;
            }
            existing->status = norm;
        }
        if (m.running) {
            if (norm == "running" && !purpose.empty()) m.liveStatus = purpose;
            else if (!closedLive.empty() && m.liveStatus == closedLive) m.liveStatus = "Working";
        }
    } else if (kind == "agent.message") {
        // Inter-agent traffic (IRC between agents, workpool dispatch cards, coordination-board
        // posts): ONE compact transcript line per message, drawn by drawCorpMutedLine's
        // agent-message branch (stage "msg:<channel>:<id>"). title = sender, reason =
        // recipient, detail = body. Replayed frames carry the same id and are dropped.
        std::string text = jstr(data, "text");
        if (text.find_first_not_of(" \t\r\n") == std::string::npos) return;
        std::string channel = jstr(data, "channel");
        if (channel != "pool" && channel != "board") channel = "irc";
        std::string id = jstr(data, "message_id");
        if (id.empty()) id = jstr(data, "id");
        if (id.empty()) id = std::to_string(m.activity.size());
        const std::string key = "msg:" + channel + ":" + id;
        for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
            if (it->rowKind == 7 && it->stage == key) return;
        // A message is an interjection: the reasoning burst it lands in is over.
        finalizeLiveStreamBoxes(m);
        ActivityItem ni;
        ni.rowKind = 7;
        ni.stage = key;
        ni.title = ui::foldToAtlasGlyphs(jstr(data, "from"));
        if (ni.title.empty()) ni.title = "agent";
        ni.reason = ui::foldToAtlasGlyphs(jstr(data, "to"));
        ni.detail = ui::foldToAtlasGlyphs(text);
        ni.status = "done";
        m.activity.push_back(std::move(ni));
    } else if (kind == "agent.context") {
        separateLiveStreamThoughtParagraph(m);
        // Per-completion usage telemetry (Python general lane). app.cpp feeds the
        // composer meter from the same event; HERE we keep the turn's prompt-cache
        // reads for the assistant bubble's meta row. The DISPLAYED figure is the
        // per-completion PEAK, not the running sum: prompt caching re-reads the same
        // prefix on every call, so a 12-call turn on a 23k context summed to "150.8k
        // cached" — a number no reader can reconcile with the meter beside it. The
        // sum survives in cacheReadTotTok for the tooltip, where it reads as cost.
        const int cr = jint(data, "cache_read");
        if (cr > 0) {
            m.cacheReadTok = std::max(m.cacheReadTok, cr);
            m.cacheReadTotTok += cr;
            ++m.cacheReadCalls;
        }
    }
    if (kind == "mission_finished" || (kind == "run_finished" && m.curMissionTask < 0)) {
        for (auto& item : m.planState.items)
            if (item.status == "running" ||
                (item.kind == "subagent" && (item.status.empty() || item.status == "pending")))
                item.status = "stopped";
        m.uiCachedH = -1.0f;
    }
    // Resume-safe: mark history dirty on activity/text/plan changes; force on terminal events.
    if (m.running || kind == "run_finished" || kind == "mission_finished" ||
        kind == "agent.thought" || kind == "agent.thought.delta" || kind == "assistant_delta" ||
        kind == "agent.tool_call" || kind == "agent.role" ||
        kind == "agent.spawn" || kind == "plan_state" || kind == "plan" ||
        kind == "agent.message") {
        markChatHistoryDirty();
        if (kind == "run_finished" || kind == "mission_finished")
            s_chatHistoryLastPersist = -1e9;  // force next draw flush
    }

}

// ---- interactive ask_user batches (agent.question / agent.answer) ----------------------------
AskBatch* findAskBatch(std::vector<ChatMsg>& msgs, const std::string& batchId, ChatMsg** owner) {
    if (batchId.empty()) return nullptr;
    for (auto it = msgs.rbegin(); it != msgs.rend(); ++it)
        for (auto& b : it->asks)
            if (b.batchId == batchId) {
                if (owner) *owner = &*it;
                return &b;
            }
    return nullptr;
}

void anchorLegacyAsks(ChatMsg& m) {
    // Batches saved before rowKind-20 markers existed: the n-th unmarked batch was asked by the
    // n-th ask_user tool call, so its card goes right after that call's row. A batch with no
    // such row keeps drawing after the turn (drawAskCards).
    size_t searchFrom = 0;
    for (const AskBatch& b : m.asks) {
        if (b.batchId.empty()) continue;
        const std::string stage = ui::askMarkerStage(b.batchId);
        const bool marked = std::any_of(m.activity.begin(), m.activity.end(),
                                        [&](const ActivityItem& a) {
                                            return a.rowKind == 20 && a.stage == stage;
                                        });
        if (marked) continue;
        size_t at = searchFrom;
        while (at < m.activity.size() &&
               !(m.activity[at].rowKind == 3 && m.activity[at].title == "ask_user"))
            ++at;
        if (at >= m.activity.size()) return;
        ActivityItem marker;
        marker.rowKind = 20;
        marker.stage = stage;
        marker.status = "done";
        m.activity.insert(m.activity.begin() + (std::ptrdiff_t)(at + 1), std::move(marker));
        searchFrom = at + 2;
    }
}

bool feedAskEvent(std::vector<ChatMsg>& msgs, ChatMsg* active, const std::string& kind,
                  const nlohmann::json& data) {
    if (kind == "agent.question") {
        AskBatch incoming = askBatchFromEvent(data);
        if (incoming.batchId.empty() || incoming.questions.empty()) return true;
        if (findAskBatch(msgs, incoming.batchId)) return true;   // replayed event
        ChatMsg* host = active;
        if (!host)
            for (auto it = msgs.rbegin(); it != msgs.rend() && !host; ++it)
                if (!it->fromUser) host = &*it;
        if (!host) return true;
        // Position marker (rowKind 20): the card draws HERE in the timeline, where the agent
        // asked — not after everything the turn did later (answered cards used to trail the
        // whole run, far below the point the question came up).
        ActivityItem marker;
        marker.rowKind = 20;
        marker.stage = ui::askMarkerStage(incoming.batchId);
        marker.status = "done";
        host->activity.push_back(std::move(marker));
        host->asks.push_back(std::move(incoming));
        host->uiCachedH = -1.0f;
        markChatHistoryDirty();
        s_chatHistoryLastPersist = -1e9;   // a question is worth an immediate flush
        return true;
    }
    if (kind == "agent.answer") {
        ChatMsg* owner = nullptr;
        if (AskBatch* b = findAskBatch(msgs, jstr(data, "batch_id"), &owner)) {
            applyAskAnswerEvent(*b, data);
            owner->uiCachedH = -1.0f;
            markChatHistoryDirty();
            s_chatHistoryLastPersist = -1e9;
        }
        return true;
    }
    return false;
}

bool closeOpenAsks(std::vector<ChatMsg>& msgs) {
    bool changed = false;
    for (auto& m : msgs)
        for (auto& b : m.asks)
            if (askStatusOpen(b.status)) {
                b.status = AskStatus::Cancelled;
                b.error.clear();
                m.uiCachedH = -1.0f;
                changed = true;
            }
    if (changed) markChatHistoryDirty();
    return changed;
}

bool hasOpenAsk(const std::vector<ChatMsg>& msgs) {
    for (const auto& m : msgs)
        for (const auto& b : m.asks)
            if (askStatusOpen(b.status)) return true;
    return false;
}

// ---- mid-run user messages (steers) ----------------------------------------------------------
bool setSteerState(std::vector<ChatMsg>& msgs, const std::string& steerId, int state) {
    if (steerId.empty()) return false;
    for (auto it = msgs.rbegin(); it != msgs.rend(); ++it) {
        if (!it->fromUser || it->steerId != steerId) continue;
        // Streamed consumption is authoritative over every late control outcome,
        // including a transport failure after the runtime already read the message.
        if (it->steerState == state || it->steerState == STEER_DELIVERED ||
            (it->steerState == STEER_CARRIED && state != STEER_DELIVERED))
            return false;
        it->steerState = state;
        it->uiCachedH = -1.0f;
        markChatHistoryDirty();
        return true;
    }
    return false;
}

bool feedSteerEvent(std::vector<ChatMsg>& msgs, const std::string& kind,
                    const nlohmann::json& data) {
    if (kind != "agent.steer") return false;
    const std::string status = jstr(data, "status");
    if (status == "delivered")
        setSteerState(msgs, jstr(data, "steer_id"), STEER_DELIVERED);
    else if (status == "carried")
        setSteerState(msgs, jstr(data, "steer_id"), STEER_CARRIED);
    return true;
}

bool settleOpenSteers(std::vector<ChatMsg>& msgs) {
    bool changed = false;
    for (auto& m : msgs)
        if (m.fromUser && (m.steerState == STEER_SENDING || m.steerState == STEER_QUEUED)) {
            m.steerState = STEER_FAILED;
            m.uiCachedH = -1.0f;
            changed = true;
        }
    if (changed) markChatHistoryDirty();
    return changed;
}

bool ui::reconcileResolvedAskReplies(std::vector<ChatMsg>& msgs) {
    bool changed = false;
    size_t turnBegin = 0;
    while (turnBegin < msgs.size()) {
        size_t turnEnd = turnBegin + 1;
        while (turnEnd < msgs.size() &&
               !(msgs[turnEnd].fromUser && msgs[turnEnd].steerId.empty()))
            ++turnEnd;
        for (size_t i = turnBegin; i < turnEnd; ++i) {
            for (const AskBatch& b : msgs[i].asks) {
                if (b.status != AskStatus::Reply || b.reply.empty()) continue;
                ChatMsg* match = nullptr;
                bool ambiguous = false;
                for (size_t j = turnBegin; j < turnEnd; ++j) {
                    ChatMsg& m = msgs[j];
                    // A resolved text reply says nothing about separately queued images.
                    // Do not normalize/substring-match: old bounded replies and duplicate
                    // text cannot establish which exact composer payload was consumed.
                    if (!m.fromUser || m.steerId.empty() || m.text != b.reply)
                        continue;
                    if (match) { ambiguous = true; break; }
                    match = &m;
                }
                if (!match || ambiguous || !match->imagePaths.empty() ||
                    (match->steerState != STEER_SENDING && match->steerState != STEER_QUEUED &&
                     match->steerState != STEER_FAILED))
                    continue;
                match->steerState = STEER_DELIVERED;
                match->uiCachedH = -1.0f;
                changed = true;
            }
        }
        turnBegin = turnEnd;
    }
    if (changed) markChatHistoryDirty();
    return changed;
}
