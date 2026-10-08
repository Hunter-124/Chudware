// sim_common implementation — see sim_common.h.
#include "sim_common.h"

#include <algorithm>
#include <cstdio>
#include <utility>

#include "../../core/app_state.h"

namespace ui {
namespace sim {

using json = nlohmann::json;

namespace {
const char* const kEmDash = "\xe2\x80\x94";
}  // namespace

// ---- JSON accessors -----------------------------------------------------------------------------

bool jhas(const json& o, const char* k) {
    if (!o.is_object()) return false;
    const auto it = o.find(k);
    return it != o.end() && it->is_number();
}

double jnum(const json& o, const char* k, double fb) {
    if (!o.is_object()) return fb;
    const auto it = o.find(k);
    return it != o.end() && it->is_number() ? it->get<double>() : fb;
}

std::string jstr(const json& o, const char* k, const std::string& fb) {
    if (!o.is_object()) return fb;
    const auto it = o.find(k);
    return it != o.end() && it->is_string() ? it->get<std::string>() : fb;
}

bool jbool(const json& o, const char* k, bool fb) {
    if (!o.is_object()) return fb;
    const auto it = o.find(k);
    return it != o.end() && it->is_boolean() ? it->get<bool>() : fb;
}

const json& jobj(const json& o, const char* k) {
    static const json empty = json::object();
    if (!o.is_object()) return empty;
    const auto it = o.find(k);
    return it != o.end() && it->is_object() ? *it : empty;
}

const json& jarr(const json& o, const char* k) {
    static const json empty = json::array();
    if (!o.is_object()) return empty;
    const auto it = o.find(k);
    return it != o.end() && it->is_array() ? *it : empty;
}

bool jvec3(const json& v, double out[3]) {
    if (!v.is_array() || v.size() != 3) return false;
    for (int k = 0; k < 3; ++k) {
        if (!v[k].is_number()) return false;
        out[k] = v[k].get<double>();
    }
    return true;
}

// ---- formatting ---------------------------------------------------------------------------------

std::string fmtNum(double v, int prec) {
    char b[64];
    std::snprintf(b, sizeof b, "%.*f", prec, v);
    return b;
}

std::string fmtKey(const json& o, const char* k, int prec) {
    return jhas(o, k) ? fmtNum(jnum(o, k), prec) : std::string(kEmDash);
}

std::string fmtText(const json& o, const char* k) {
    if (!o.is_object()) return kEmDash;
    const auto it = o.find(k);
    if (it == o.end() || it->is_null()) return kEmDash;
    const json& v = *it;
    if (v.is_string()) return v.get<std::string>();
    if (v.is_boolean()) return v.get<bool>() ? "yes" : "no";
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number()) return fmtNum(v.get<double>(), 2);
    return v.dump();
}

int indexOf(const char* const* items, int n, const std::string& key) {
    for (int i = 0; i < n; ++i)
        if (key == items[i]) return i;
    return -1;
}

std::string countNoun(long long n, const char* one, const char* many) {
    return std::to_string(n) + " " + (n == 1 ? std::string(one) : many ? std::string(many) : std::string(one) + "s");
}

// ---- engine RPC envelopes -----------------------------------------------------------------------

bool protocolError(const json& res) { return res.contains("code") && !res.contains("ok"); }

bool envelopeOk(const core::RpcResult& r) {
    return r.ok && r.result.is_object() && !protocolError(r.result) && jbool(r.result, "ok", false);
}

bool studyRpcOk(const core::RpcResult& r) {
    return r.ok && r.result.is_object() && !protocolError(r.result) && jbool(r.result, "ok", true);
}

std::string envelopeWhy(const json& res, const std::string& transportErr) {
    if (res.is_object()) {
        std::string reason = jstr(res, "reason");
        if (reason.empty()) reason = jstr(res, "message", jstr(res, "error"));
        const std::string hint = jstr(res, "hint");
        if (!reason.empty()) return hint.empty() ? reason : reason + " " + kEmDash + " " + hint;
    }
    return transportErr.empty() ? std::string("the engine gave no reply") : transportErr;
}

json stripAdvice(json r) {
    if (r.is_object()) {
        r.erase("next_actions");
        r.erase("hint");
    }
    return r;
}

// ---- tones / small UI mappings ------------------------------------------------------------------

kit::Tone toneOf(int tone) {
    switch (tone) {
        case 1: return kit::Tone::Ok;
        case 2: return kit::Tone::Warn;
        case 3: return kit::Tone::Err;
        default: return kit::Tone::Neutral;
    }
}

void warningsList(const json& res) {
    for (const auto& w : jarr(res, "warnings"))
        if (w.is_string()) kit::inlineStatus(w.get<std::string>().c_str(), kit::Tone::Warn);
}

// ---- document -----------------------------------------------------------------------------------

void markDocDirty(AppState& st, const std::string& docId) {
    for (auto& tab : st.docTabs) {
        if (tab.docId != docId) continue;
        tab.dirty = true;
        tab.historyCleared = false;
        if (tab.aiHandoff) tab.userAdopted = true;
        return;
    }
}

// ---- StudyRecordStore ---------------------------------------------------------------------------

void StudyRecordStore::Slot::bind(const json& rec) {
    *this = Slot{};
    id = jstr(rec, "id");
    synced = rec.dump();
}

StudyRecordStore::StudyRecordStore(Config cfg)
    : cfg_(std::move(cfg)), alive_(std::make_shared<int>(0)) {}

void StudyRecordStore::setRecords(const std::string& docId, const std::vector<json>& records) {
    docId_ = docId;
    records_ = records;
}

const json* StudyRecordStore::find(const std::string& id) const {
    if (id.empty()) return nullptr;
    for (const auto& r : records_)
        if (jstr(r, "id") == id) return &r;
    return nullptr;
}

const json* StudyRecordStore::firstOfType(const char* type) const {
    for (const auto& r : records_)
        if (jstr(r, "type") == type) return &r;
    return nullptr;
}

std::string StudyRecordStore::activeDocId(const AppState& st) const {
    if (!docId_.empty()) return docId_;
    return st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
}

StudyRecordStore::Sync StudyRecordStore::sync(Slot& slot, const char* type, bool localEmpty,
                                              const std::function<void(const json&)>& adopt) {
    if (slot.addPending) return Sync::Unchanged;
    if (!slot.id.empty()) {
        const json* rec = find(slot.id);
        if (!rec) {
            // Deleted elsewhere: the local state stays as unsaved work.
            slot.id.clear();
            slot.synced.clear();
            return Sync::Forgotten;
        }
        if (rec->dump() == slot.synced) return Sync::Unchanged;
        adopt(*rec);
        return Sync::Adopted;
    }
    if (!localEmpty || !type) return Sync::Unchanged;
    const json* rec = firstOfType(type);
    if (!rec) return Sync::Unchanged;
    adopt(*rec);
    return Sync::Adopted;
}

void StudyRecordStore::upsertLocal(const json& saved) {
    const std::string id = jstr(saved, "id");
    for (auto& r : records_)
        if (jstr(r, "id") == id) {
            r = saved;
            return;
        }
    records_.push_back(saved);
}

void StudyRecordStore::saveRecord(AppState& st, Slot& slot, json record) {
    const std::string docId = activeDocId(st);
    if (docId.empty()) {
        setStatus(cfg_.noDocStatus, 2);
        return;
    }
    if (slot.addPending) {
        slot.pending = std::move(record);
        slot.resave = true;
        return;
    }
    std::weak_ptr<int> alive = alive_;
    if (!slot.id.empty()) {
        record["id"] = slot.id;
        slot.synced = record.dump();
        upsertLocal(record);
        const std::string studyId = slot.id;
        st.rpc.call("sim_study_edit", {{"doc_id", docId}, {"study_id", studyId}, {"study", record}},
                    [this, alive, &st, docId, studyId](const core::RpcResult& r) {
                        if (alive.expired()) return;
                        if (!studyRpcOk(r)) {
                            st.addLog(cfg_.logTag + " could not save study " + studyId + ": " +
                                      envelopeWhy(r.result, r.err));
                            setStatus(cfg_.saveFailedStatus, 3);
                            return;
                        }
                        markDocDirty(st, docId);
                    },
                    15000);
        return;
    }
    slot.addPending = true;
    Slot* sp = &slot;
    st.rpc.call("sim_study_add", {{"doc_id", docId}, {"study", record}},
                [this, alive, &st, docId, sp, record](const core::RpcResult& r) {
                    if (alive.expired()) return;
                    sp->addPending = false;
                    if (!studyRpcOk(r)) {
                        st.addLog(cfg_.logTag + " could not add study: " + envelopeWhy(r.result, r.err));
                        setStatus(cfg_.saveFailedStatus, 3);
                        return;
                    }
                    sp->id = jstr(r.result, "study_id");
                    json saved = record;
                    saved["id"] = sp->id;
                    sp->synced = saved.dump();
                    upsertLocal(saved);
                    markDocDirty(st, docId);
                    if (sp->resave) {
                        sp->resave = false;
                        json next = std::move(sp->pending);
                        sp->pending = json();
                        saveRecord(st, *sp, std::move(next));
                    }
                },
                15000);
}

void StudyRecordStore::deleteRecord(AppState& st, Slot& slot) {
    const std::string docId = activeDocId(st);
    if (slot.id.empty() || docId.empty() || slot.addPending) return;
    const std::string studyId = slot.id;
    slot = Slot{};
    records_.erase(std::remove_if(records_.begin(), records_.end(),
                                  [&](const json& r) { return jstr(r, "id") == studyId; }),
                   records_.end());
    std::weak_ptr<int> alive = alive_;
    st.rpc.call("sim_study_delete", {{"doc_id", docId}, {"study_id", studyId}},
                [this, alive, &st, docId, studyId](const core::RpcResult& r) {
                    if (alive.expired()) return;
                    if (!studyRpcOk(r)) {
                        st.addLog(cfg_.logTag + " could not delete study " + studyId + ": " +
                                  envelopeWhy(r.result, r.err));
                        setStatus(cfg_.deleteFailedStatus, 3);
                        return;
                    }
                    markDocDirty(st, docId);
                },
                15000);
}

void StudyRecordStore::call(AppState& st, const char* method, json params, Done done, int timeoutMs) {
    ++inflight_;
    lastError_.clear();
    std::weak_ptr<int> alive = alive_;
    st.rpc.call(
        method, std::move(params),
        [this, alive, &st, done = std::move(done)](const core::RpcResult& r) {
            if (alive.expired()) return;
            --inflight_;
            const bool ok = envelopeOk(r);
            const json res = r.result.is_object() ? r.result : json::object();
            done(st, ok, res, ok ? std::string() : envelopeWhy(r.result, r.err));
        },
        timeoutMs < 0 ? cfg_.callTimeoutMs : timeoutMs);
}

void StudyRecordStore::setStatus(const std::string& text, int tone) {
    status_ = text;
    statusTone_ = tone;
    if (tone == 3) lastError_ = text;
}

void StudyRecordStore::clearStatus() {
    status_.clear();
    statusTone_ = 0;
}

void StudyRecordStore::drawStatus() const {
    // The status lives in the pinned footer: one elided line, full text on hover (no-op when empty).
    kit::footerStatus(status_.c_str(), toneOf(statusTone_));
}

}  // namespace sim
}  // namespace ui
