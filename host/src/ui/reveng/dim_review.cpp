// dim_review.cpp — Reverse Engineering "Review" stage: the dimension-confirm ledger and the
// rebuild-vs-scan deviation report (RE-40 overhaul of RE-22).
//
// ─── What this stage is for ──────────────────────────────────────────────────────────────────
// This is the human gate. Everything upstream (photos → scale → reconstruct → fit) produces
// NUMBERS; here an engineer decides whether those numbers are good enough to print a
// replacement part from. Two questions get answered:
//
//   1. "Are these dimensions real?"          → the dimension ledger card.
//   2. "Did the rebuild actually match it?"  → the deviation card.
//
// ─── Honesty rules encoded here (contracts/reveng.md §0) ─────────────────────────────────────
//   • Demo rows exist ONLY under $CHUDWARE_SHOT. seedCannedDimPayload() hard-returns otherwise
//     and no failure path ever falls back to canned data, so a real session cannot show them.
//     While the fixture is live the card header carries a Warn chip naming the scenario.
//   • Every row shows its honesty tier (high ±2% / medium ±5% / low ±10% / approximate =
//     reference only) and its provenance method, always.
//   • A FIT-CRITICAL row is the reason a printed part fits or does not, so it is grouped first,
//     given a stronger row treatment, and is never swept up by the bulk confirm. A fit-critical
//     row whose provenance_method is `guessed` is an ALARM — §0 forbids silent guessing there.
//   • A fitted row on an unscaled session is NOT millimetres. It renders in reconstruction
//     units with the server's note attached and its confirm control demands a typed value.
//   • get_reveng_deviation's instructive refusals (no reference mesh; B-Rep auto-tessellation
//     not wired) are BlockedStates with the fix inline — a known limitation, not a red failure.
//
// ─── Threading ───────────────────────────────────────────────────────────────────────────────
// GL-thread only. Every RPC is async via st.rpc.call(..., callback); the callback is drained on
// the GL thread. A sync worker call in a draw path is a known freeze bug. All panel-local state
// lives in the file-static DRS() singleton — no mutex, never touched off the GL thread.
#include "dim_review.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui/metrics.h"
#include "ui_theme.h"
#include "reveng_internal.h"   // S() → RevEngState, SessionSummary, kickFitPrimitives
#include "reveng_widgets.h"    // ui::reveng::kit — the shared RE visual kit

namespace ui {
namespace reveng {
namespace {

namespace M = ui::metrics;
using kit::Level;

// ─── Tuning (ratios and counts only — every pixel comes from metrics::re*()) ─────────────────

constexpr float  kDimsShare    = 0.58f;   // ledger's share of the stage height before it scrolls
constexpr int    kProfileN     = 128;     // quantiles kept from vertex_distances_mm
constexpr double kTierTightMm  = 0.10;    // green : within tight tolerance
constexpr double kTierLooseMm  = 0.50;    // amber : within loose tolerance
constexpr double kDimReloadSec = 5.0;     // retry floor for a failed ledger fetch
constexpr double kDevReloadSec = 10.0;    // retry floor for a failed deviation fetch

// ─── Palette helpers (the ONLY way colour enters this file) ──────────────────────────────────

inline ImVec4 role4(const float c[4], float a = 1.0f) {
    return ImVec4(c[0], c[1], c[2], c[3] * a);
}
inline ImU32 roleU(const float c[4], float a = 1.0f) {
    return ImGui::GetColorU32(role4(c, a));
}

// Deviation tier for a distance in mm: 0 = ok (<0.1), 1 = warn (<0.5), 2 = error (>=0.5).
inline int devTier(double mm) {
    return mm < kTierTightMm ? 0 : (mm < kTierLooseMm ? 1 : 2);
}
inline const float* tierRole(int t) {
    return t == 0 ? palette.statusOk : (t == 1 ? palette.statusWarn : palette.statusErr);
}
inline Level tierLevel(int t) {
    return t == 0 ? Level::Ok : (t == 1 ? Level::Warn : Level::Error);
}

// ─── One ledger row ──────────────────────────────────────────────────────────────────────────

struct DimRow {
    // From list_reveng_dims. `key` (not `id`) is the confirm_dimensions ledger key.
    std::string key, id, label;
    double      value_mm    = 0.0;
    bool        hasValue    = false;
    double      tol_mm      = 0.0;
    bool        hasTol      = false;
    std::string source;          // datasheet | typed | intake | fitted | ...
    std::string method;          // provenance_method: user_confirmed|photo_fiducial|datasheet|fitted|guessed
    std::string tier;            // honesty_tier: high|medium|low|approximate
    std::string note;            // server degradation note (unscaled units, single-photo mesh, …)
    std::string fitSummary;      // "cylinder r=4.00 h=12.00 mm" on fitted rows
    std::string imagePath;       // source still, when the server could resolve one
    int         imageIndex  = -1;
    bool        confirmed   = false;   // server-side confirmed
    bool        fitCritical = false;
    bool        unscaled    = false;   // value is in reconstruction units, not mm

    // Panel-local.
    char        editBuf[32] = {};
    bool        localConfirm = false;  // confirmed by this GUI session
    bool        busy    = false;       // per-row confirm_dimensions in flight
    bool        justOk  = false;
    std::string err;                   // per-row sticky error

    bool guessed()  const { return method == "guessed" || source == "guessed"; }
    bool settled()  const { return confirmed || localConfirm; }
    bool alarm()    const { return fitCritical && !settled() && guessed(); }
};

// ─── Deviation result ────────────────────────────────────────────────────────────────────────

struct DeviationBucket {
    double      lo = 0.0, hi = 0.0;
    bool        openEnded = false;
    long long   count = 0;
    float       frac  = 0.0f;   // share of samples, 0..1
    int         tier  = 0;
    std::string label;          // "< 0.10 mm" | "0.10 - 0.50 mm" | ">= 0.50 mm"
};

struct DeviationResult {
    bool        ok = false;
    double      max_mm = 0.0, rms_mm = 0.0;
    std::string reason, hint, meshPath, brepDocId;
    std::vector<DeviationBucket> buckets;
    std::vector<std::string>     worst;    // named worst regions, when the response carries them
    std::vector<float>           profile;  // sorted quantiles of vertex_distances_mm
    long long   samples = 0;
};

// ─── Module state singleton (GL thread only) ─────────────────────────────────────────────────

struct DimReviewState {
    // ---- ledger ----
    std::vector<DimRow>      rows;
    std::vector<std::string> confirmedLocal;   // survives a refetch; fitted/datasheet rows are
                                               // not in the intake ledger so the server will not
                                               // echo their confirmation back.
    std::string sessionId;
    bool        loadPending = false, loadedOnce = false;
    bool        forceReload = false;
    double      lastLoadT = -1.0e9;
    std::string loadTransportErr;   // r.ok == false — the call never reached the tool
    std::string loadReason, loadHint;
    std::vector<std::string> notes; // degradation notes from the tool
    std::string emptyHint;          // what to do when dims:[] came back ok
    bool        scaleConfirmed = false;
    bool        canned = false;     // rows came from the CHUDWARE_SHOT fixture

    // ---- confirm ----
    bool        bulkPending = false;
    std::string bulkErr;
    bool        gateKnown = false, gateOk = false;
    std::vector<std::string> stillBlocking;

    // ---- deviation ----
    DeviationResult dev;
    bool        devLoaded = false, devPending = false, devForce = false;
    double      lastDevT = -1.0e9;
    std::string devTransportErr;
    std::string devSessionId;
    char        refPathBuf[512] = {};
    std::string refPathSent;

    // ---- layout ----
    bool  dimsOpen = true, devOpen = true;
    float stageH = 0.0f;        // Review body height published by drawReviewStage()
    float rowsMeasuredH = 0.0f; // measured content height of the row list
    int   selectedRow = -1;
};

DimReviewState& DRS() {
    static DimReviewState s;
    return s;
}

// ─── Small helpers ───────────────────────────────────────────────────────────────────────────

// The canned fixture may only exist inside a headless CHUDWARE_SHOT capture. This is the single
// gate; every other path in this file is live-data-only.
bool shotFixtureAllowed() {
    const char* s = std::getenv("CHUDWARE_SHOT");
    return s && *s;
}

const SessionSummary* selectedSession() {
    const RevEngState& rs = S();
    if (rs.selected < 0 || rs.selected >= (int)rs.sessions.size()) return nullptr;
    return &rs.sessions[rs.selected];
}

DimRow* findRow(DimReviewState& d, const std::string& key) {
    for (DimRow& r : d.rows)
        if (r.key == key) return &r;
    return nullptr;
}

bool inList(const std::vector<std::string>& v, const std::string& k) {
    return std::find(v.begin(), v.end(), k) != v.end();
}

// Null-safe JSON accessors. nlohmann's value() throws when a key is PRESENT but null or the
// wrong type, and these parsers run inside RPC callbacks on the GL thread where a thrown
// exception would take the frame loop with it. `tolerance_mm` and `value_mm` are legitimately
// null for an unscaled fit, so this is a real case, not paranoia.
std::string jsonStr(const nlohmann::json& j, const char* k, const char* dflt = "") {
    const auto it = j.find(k);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string(dflt);
}
bool jsonBool(const nlohmann::json& j, const char* k, bool dflt = false) {
    const auto it = j.find(k);
    return (it != j.end() && it->is_boolean()) ? it->get<bool>() : dflt;
}
int jsonInt(const nlohmann::json& j, const char* k, int dflt) {
    const auto it = j.find(k);
    return (it != j.end() && it->is_number_integer()) ? it->get<int>() : dflt;
}
long long jsonI64(const nlohmann::json& j, const char* k, long long dflt) {
    const auto it = j.find(k);
    return (it != j.end() && it->is_number_integer()) ? it->get<long long>() : dflt;
}
// Returns false (leaving `out` untouched) when the key is missing, null, or not finite.
bool jsonNum(const nlohmann::json& j, const char* k, double& out) {
    const auto it = j.find(k);
    if (it == j.end() || !it->is_number()) return false;
    const double v = it->get<double>();
    if (!std::isfinite(v)) return false;
    out = v;
    return true;
}

const GalleryImage* rowImage(const DimRow& r) {
    const RevEngState& rs = S();
    if (r.imageIndex >= 0 && r.imageIndex < (int)rs.galleryImages.size())
        return &rs.galleryImages[r.imageIndex];
    if (!r.imagePath.empty())
        for (const GalleryImage& g : rs.galleryImages)
            if (g.path == r.imagePath) return &g;
    return nullptr;
}

// The value that would be submitted for this row. False when there is nothing usable to send.
bool rowSubmitValue(const DimRow& r, double& out) {
    if (r.editBuf[0]) {
        char* end = nullptr;
        const double v = std::strtod(r.editBuf, &end);
        if (end && end != r.editBuf && std::isfinite(v) && v > 0.0) { out = v; return true; }
        return false;
    }
    if (r.hasValue && !r.unscaled && std::isfinite(r.value_mm) && r.value_mm > 0.0) {
        out = r.value_mm;
        return true;
    }
    return false;
}

Level methodLevel(const DimRow& r) {
    if (r.method == "user_confirmed" || r.source == "typed" || r.source == "caliper")
        return Level::Ok;
    if (r.method == "datasheet")      return Level::Ok;
    if (r.method == "photo_fiducial") return r.tier == "high" ? Level::Ok : Level::Warn;
    if (r.method == "guessed")        return Level::Error;
    return Level::Warn;   // fitted and anything unrecognised: evidence, not a measurement
}

const char* methodTip(const DimRow& r) {
    if (r.method == "user_confirmed") return "A human typed this value into the intake ledger.";
    if (r.method == "datasheet")      return "Read off a manufacturer datasheet - the strongest\n"
                                             "evidence this pipeline ever gets.";
    if (r.method == "photo_fiducial") return "Measured from a photo against a printed fiducial.\n"
                                             "In-plane only; out-of-plane reads are capped at the\n"
                                             "low tier.";
    if (r.method == "fitted")         return "RANSAC-fitted to the reconstructed mesh. Only as\n"
                                             "true as the scale solve behind it - treat as a\n"
                                             "starting point, not a caliper reading.";
    if (r.method == "guessed")        return "Nobody measured this. contracts/reveng.md section 0\n"
                                             "forbids a guessed value on a fit-critical dimension.";
    return "Provenance reported by list_reveng_dims.";
}

std::string rowValueText(const DimRow& r) {
    if (!r.hasValue) return std::string("--");
    if (r.unscaled) {
        char buf[48];
        std::snprintf(buf, sizeof(buf), "%.4g units", r.value_mm);
        return std::string(buf);
    }
    return kit::fmtMm(r.value_mm, 2);
}

// ─── Ledger loading ──────────────────────────────────────────────────────────────────────────

void parseDims(const nlohmann::json& j, DimReviewState& d) {
    // Preserve what the user TYPED — not the value we pre-filled for them. A stale pre-fill must
    // never win over a fresher server value (e.g. after a re-fit).
    std::vector<std::pair<std::string, std::string>> typed;
    for (const DimRow& r : d.rows) {
        if (!r.editBuf[0]) continue;
        char prefill[32] = {};
        if (r.hasValue && !r.unscaled)
            std::snprintf(prefill, sizeof(prefill), "%.4g", r.value_mm);
        if (std::strcmp(r.editBuf, prefill) != 0)
            typed.emplace_back(r.key, std::string(r.editBuf));
    }

    d.rows.clear();
    d.notes.clear();
    d.emptyHint.clear();
    d.scaleConfirmed = jsonBool(j, "scale_confirmed", false);

    const auto notesIt = j.find("notes");
    if (notesIt != j.end() && notesIt->is_array())
        for (const auto& n : *notesIt)
            if (n.is_string()) d.notes.push_back(n.get<std::string>());
    d.emptyHint = jsonStr(j, "hint");

    const auto dimsIt = j.find("dims");
    if (dimsIt == j.end() || !dimsIt->is_array()) return;
    d.rows.reserve(dimsIt->size());
    for (const auto& item : *dimsIt) {
        if (!item.is_object()) continue;
        DimRow r;
        r.id  = jsonStr(item, "id");
        r.key = jsonStr(item, "key", r.id.c_str());   // `key` is the confirm_dimensions ledger
        if (r.key.empty()) continue;                  // key; `id` is only positional.
        r.label = jsonStr(item, "label", r.key.c_str());

        r.hasValue = jsonNum(item, "value_mm", r.value_mm);
        r.hasTol   = jsonNum(item, "tolerance_mm", r.tol_mm) && r.tol_mm > 0.0;

        r.source      = jsonStr(item, "source", "unknown");
        r.method      = jsonStr(item, "provenance_method", "");
        if (r.method.empty()) r.method = jsonStr(item, "method", "guessed");
        r.tier        = jsonStr(item, "honesty_tier", "");
        if (r.tier.empty()) r.tier = jsonStr(item, "tier", "low");
        r.confirmed   = jsonBool(item, "confirmed", false);
        r.fitCritical = jsonBool(item, "fit_critical", jsonBool(item, "required", false));
        r.note        = jsonStr(item, "note");
        r.fitSummary  = jsonStr(item, "fit_summary");
        r.imagePath   = jsonStr(item, "image_path");
        r.imageIndex  = jsonInt(item, "image_index", -1);
        // A fitted value on an unscaled session is in reconstruction units, not millimetres —
        // the server says so in `note` and the row must not claim mm.
        r.unscaled     = r.note.find("unscaled") != std::string::npos;
        r.localConfirm = inList(d.confirmedLocal, r.key);

        // Pre-fill the caliper box only when the shown number is honestly in millimetres.
        // An unscaled fit value must be re-typed, never accepted as mm by a single click.
        if (r.hasValue && !r.unscaled)
            std::snprintf(r.editBuf, sizeof(r.editBuf), "%.4g", r.value_mm);
        for (const auto& t : typed)
            if (t.first == r.key)
                std::snprintf(r.editBuf, sizeof(r.editBuf), "%s", t.second.c_str());

        d.rows.push_back(std::move(r));
    }

    // Fit-critical first, alarms at the very top, then unconfirmed before confirmed.
    std::stable_sort(d.rows.begin(), d.rows.end(), [](const DimRow& a, const DimRow& b) {
        if (a.fitCritical != b.fitCritical) return a.fitCritical;
        if (a.alarm() != b.alarm())         return a.alarm();
        return a.settled() < b.settled();
    });
}

void loadDims(AppState& st) {
    DimReviewState& d = DRS();
    if (d.canned || d.loadPending) return;          // the shot fixture is never overwritten
    if (!st.workerStarted) return;
    const SessionSummary* sess = selectedSession();
    if (!sess) return;

    const bool switched = (sess->session_id != d.sessionId);
    if (!switched && d.loadedOnce && !d.forceReload) return;

    const double now = ImGui::GetTime();
    if (!switched && d.loadedOnce && now - d.lastLoadT < kDimReloadSec && !d.forceReload) return;

    if (switched) {
        d.rows.clear();
        d.confirmedLocal.clear();
        d.stillBlocking.clear();
        d.gateKnown = false;
        d.loadedOnce = false;
        d.selectedRow = -1;
    }
    d.forceReload = false;
    d.loadPending = true;
    d.lastLoadT   = now;
    d.sessionId   = sess->session_id;

    nlohmann::json args;
    args["session_id"] = d.sessionId;
    st.rpc.call("list_reveng_dims", args, [](const core::RpcResult& r) {
        DimReviewState& d2 = DRS();
        d2.loadPending = false;
        d2.loadedOnce  = true;
        d2.loadTransportErr.clear();
        d2.loadReason.clear();
        d2.loadHint.clear();

        // NOTE: no canned fallback lives on any of these paths. A failure is reported as a
        // failure; an empty ledger is reported as an empty ledger.
        if (!r.ok) {
            d2.rows.clear();
            d2.loadTransportErr = r.err.empty() ? std::string("worker unreachable") : r.err;
            return;
        }
        if (!r.result.is_object()) {
            d2.rows.clear();
            d2.loadReason = "list_reveng_dims returned a malformed response";
            d2.loadHint   = "Check the worker log (View > Console) for the raw payload.";
            return;
        }
        if (!jsonBool(r.result, "ok", false)) {
            d2.rows.clear();
            d2.loadReason = jsonStr(r.result, "reason", "list_reveng_dims returned ok:false");
            d2.loadHint   = jsonStr(r.result, "hint");
            return;
        }
        parseDims(r.result, d2);
    });
}

// ─── Confirm ─────────────────────────────────────────────────────────────────────────────────

void confirmKeys(AppState& st, const std::vector<std::string>& keys) {
    DimReviewState& d = DRS();
    if (keys.empty() || !st.workerStarted) return;

    nlohmann::json confirmations = nlohmann::json::array();
    std::vector<std::string> sent;
    sent.reserve(keys.size());
    for (const std::string& k : keys) {
        DimRow* row = findRow(d, k);
        if (!row || row->busy) continue;
        double v = 0.0;
        if (!rowSubmitValue(*row, v)) {
            row->err = "Type a caliper value in mm before confirming.";
            continue;
        }
        row->busy   = true;
        row->justOk = false;
        row->err.clear();
        nlohmann::json c;
        c["key"]      = row->key;
        c["value_mm"] = v;
        confirmations.push_back(std::move(c));
        sent.push_back(row->key);
    }
    if (sent.empty()) return;

    const bool bulk = sent.size() > 1;
    if (bulk) { d.bulkPending = true; d.bulkErr.clear(); }

    nlohmann::json args;
    args["confirmations"] = confirmations;
    st.rpc.call("confirm_dimensions", args, [sent, bulk](const core::RpcResult& r) {
        DimReviewState& d2 = DRS();
        if (bulk) d2.bulkPending = false;

        std::string err;
        if (!r.ok) {
            err = r.err.empty() ? std::string("worker unreachable") : r.err;
        } else if (!r.result.is_object()) {
            err = "confirm_dimensions returned a malformed response";
        } else if (!jsonBool(r.result, "ok", false)) {
            err = jsonStr(r.result, "error");
            if (err.empty()) err = jsonStr(r.result, "reason",
                                           "confirm_dimensions returned ok:false");
        } else {
            d2.gateKnown = true;
            d2.gateOk    = jsonBool(r.result, "gate_ok", false);
            d2.stillBlocking.clear();
            const auto sb = r.result.find("still_blocking");
            if (sb != r.result.end() && sb->is_array())
                for (const auto& s : *sb)
                    if (s.is_string()) d2.stillBlocking.push_back(s.get<std::string>());
        }

        for (const std::string& k : sent) {
            DimRow* row = findRow(d2, k);
            if (!row) continue;
            row->busy = false;
            if (err.empty()) {
                row->localConfirm = true;
                row->justOk = true;
                row->err.clear();
                if (!inList(d2.confirmedLocal, k)) d2.confirmedLocal.push_back(k);
            } else {
                row->err = err;
            }
        }
        if (bulk && !err.empty()) d2.bulkErr = err;
    });
}

// ─── Deviation loading ───────────────────────────────────────────────────────────────────────

void parseDeviation(const nlohmann::json& res, DeviationResult& dev) {
    dev = DeviationResult();
    dev.ok       = jsonBool(res, "ok", false);
    dev.reason   = jsonStr(res, "reason");
    dev.hint     = jsonStr(res, "hint");
    jsonNum(res, "max_mm", dev.max_mm);
    jsonNum(res, "rms_mm", dev.rms_mm);
    dev.meshPath = jsonStr(res, "mesh_path");
    dev.brepDocId = jsonStr(res, "brep_doc_id");

    // §5 buckets: {range_mm:[lo, hi|null], face_count, color}. The server's `color` is
    // deliberately ignored — tier colour comes from the palette so the whole app agrees.
    long long total = 0;
    const auto bucketsIt = res.find("buckets");
    if (bucketsIt != res.end() && bucketsIt->is_array()) {
        for (const auto& b : *bucketsIt) {
            if (!b.is_object()) continue;
            DeviationBucket bk;
            const auto rgIt = b.find("range_mm");
            if (rgIt != b.end() && rgIt->is_array() && rgIt->size() >= 2) {
                const auto& rg = *rgIt;
                if (rg[0].is_number()) bk.lo = rg[0].get<double>();
                if (rg[1].is_number()) bk.hi = rg[1].get<double>();
                else                   bk.openEnded = true;
            } else {
                bk.openEnded = true;
            }
            bk.count = jsonI64(b, "face_count", jsonI64(b, "count", 0));

            bk.tier = bk.openEnded ? devTier(std::max(bk.lo, kTierLooseMm)) : devTier(bk.hi);
            char lbl[64];
            if (bk.openEnded)      std::snprintf(lbl, sizeof(lbl), ">= %.2f mm", bk.lo);
            else if (bk.lo <= 0.0) std::snprintf(lbl, sizeof(lbl), "< %.2f mm", bk.hi);
            else                   std::snprintf(lbl, sizeof(lbl), "%.2f - %.2f mm", bk.lo, bk.hi);
            bk.label = lbl;
            total += bk.count;
            dev.buckets.push_back(std::move(bk));
        }
    }
    for (DeviationBucket& b : dev.buckets)
        b.frac = total > 0 ? (float)((double)b.count / (double)total) : 0.0f;

    // Optional: named worst regions. Rendered only when the response actually carries them —
    // we never invent a region name.
    const auto worstIt = res.find("worst_regions");
    if (worstIt != res.end() && worstIt->is_array()) {
        for (const auto& w : *worstIt) {
            if (w.is_string()) {
                dev.worst.push_back(w.get<std::string>());
            } else if (w.is_object()) {
                std::string name = jsonStr(w, "label");
                if (name.empty()) name = jsonStr(w, "name");
                if (name.empty()) name = jsonStr(w, "region");
                if (name.empty()) continue;
                double wm = 0.0;
                if (jsonNum(w, "max_mm", wm)) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf), "%s  (%.3f mm)", name.c_str(), wm);
                    dev.worst.push_back(buf);
                } else {
                    dev.worst.push_back(std::move(name));
                }
            }
        }
    }

    // Per-sample distances → a fixed-size sorted profile. The raw array (up to n_samples
    // doubles) is consumed here and dropped; only kProfileN floats survive into the frame loop.
    const auto vdIt = res.find("vertex_distances_mm");
    if (vdIt != res.end() && vdIt->is_array()) {
        const auto& arr = *vdIt;
        std::vector<float> v;
        v.reserve(arr.size());
        for (const auto& x : arr)
            if (x.is_number()) {
                const double dd = x.get<double>();
                if (std::isfinite(dd)) v.push_back((float)dd);
            }
        dev.samples = (long long)v.size();
        if (!v.empty()) {
            std::sort(v.begin(), v.end());
            dev.profile.resize(kProfileN);
            const double last = (double)(v.size() - 1);
            for (int i = 0; i < kProfileN; ++i) {
                const double q  = (double)i / (double)(kProfileN - 1);
                const double fi = q * last;
                const size_t i0 = (size_t)fi;
                const size_t i1 = std::min(i0 + 1, v.size() - 1);
                const double fr = fi - (double)i0;
                dev.profile[i] = (float)(v[i0] * (1.0 - fr) + v[i1] * fr);
            }
        }
    }

    // Some responses carry buckets but not the per-sample array. The bucket face_counts are the
    // same population, so the readout stays coherent instead of reporting "of 0".
    if (dev.samples == 0) dev.samples = total;
}

void loadDeviation(AppState& st) {
    DimReviewState& d = DRS();
    if (d.canned || d.devPending) return;
    if (!st.workerStarted) return;
    const SessionSummary* sess = selectedSession();
    if (!sess) return;

    const bool switched = (sess->session_id != d.devSessionId);
    if (switched) { d.devLoaded = false; d.dev = DeviationResult(); d.devTransportErr.clear(); }
    if (d.devLoaded && !d.devForce) return;

    const double now = ImGui::GetTime();
    if (!switched && !d.devForce && now - d.lastDevT < kDevReloadSec) return;

    d.devForce     = false;
    d.devPending   = true;
    d.lastDevT     = now;
    d.devSessionId = sess->session_id;

    nlohmann::json args;
    args["session_id"] = d.devSessionId;
    // A reference mesh is what makes the comparison possible. Prefer a path the user supplied;
    // otherwise name the built parametric doc so the tool answers with its own instructive
    // "B-Rep auto-tessellation not wired" hint rather than a bare "supply one of these".
    d.refPathSent = d.refPathBuf[0] ? std::string(d.refPathBuf) : std::string();
    if (!d.refPathSent.empty()) args["ref_path"] = d.refPathSent;
    else if (!S().model.descDocId.empty()) args["brep_doc_id"] = S().model.descDocId;

    st.rpc.call("get_reveng_deviation", args, [](const core::RpcResult& r) {
        DimReviewState& d2 = DRS();
        d2.devPending = false;
        d2.devLoaded  = true;
        d2.lastDevT   = ImGui::GetTime();
        d2.devTransportErr.clear();

        if (!r.ok) {
            d2.dev = DeviationResult();
            d2.devTransportErr = r.err.empty() ? std::string("worker unreachable") : r.err;
            return;
        }
        if (!r.result.is_object()) {
            d2.dev = DeviationResult();
            d2.dev.reason = "get_reveng_deviation returned a malformed response";
            d2.dev.hint   = "Check the worker log (View > Console) for the raw payload.";
            return;
        }
        parseDeviation(r.result, d2.dev);
    });
}

// ─── Row rendering ───────────────────────────────────────────────────────────────────────────

// Draws one ledger row as a self-contained band: source thumbnail, label + provenance chips,
// caliper field + confirm control, and the server's note. The band background is painted on a
// lower draw channel once the real height is known, so a fit-critical row can be emphasised
// without guessing its size up front.
void drawDimRow(AppState& st, DimReviewState& d, DimRow& row, int idx) {
    ImGui::PushID(idx);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    static ImDrawListSplitter splitter;   // GL thread only; balanced within this function
    splitter.Split(dl, 2);
    splitter.SetCurrentChannel(dl, 1);

    const float padX = M::reCardPadX();
    const float padY = M::reCardPadY();
    const ImVec2 mn  = ImGui::GetCursorScreenPos();
    const float  fullW = ImGui::GetContentRegionAvail().x;

    ImGui::Dummy(ImVec2(0.0f, padY));
    ImGui::Indent(padX);

    // ---- source thumbnail (what am I actually confirming?) --------------------------------
    const GalleryImage* img = rowImage(row);
    const float thumb = M::reThumbSm();
    {
        const char* badge = img ? "ok" : nullptr;
        const char* tag   = img ? "photo" : (row.fitSummary.empty() ? nullptr : "fit");
        if (kit::ThumbTile("##dimthumb", img ? img->texId : 0u, thumb,
                           d.selectedRow == idx, badge, tag))
            d.selectedRow = (d.selectedRow == idx) ? -1 : idx;
        if (ImGui::IsItemHovered()) {
            std::vector<std::string> meta;
            meta.push_back(row.label);
            meta.push_back("key: " + row.key);
            meta.push_back("source: " + row.source + " / " + row.method);
            meta.push_back("honesty: " + row.tier);
            if (row.hasTol) meta.push_back("tolerance: +/- " + kit::fmtMm(row.tol_mm, 3));
            if (!row.fitSummary.empty()) meta.push_back(row.fitSummary);
            if (!row.note.empty())       meta.push_back(row.note);
            if (img) {
                meta.push_back(img->path);
                kit::ThumbHoverPreview(img->texId, img->thumbW, img->thumbH, meta);
            } else if (ImGui::BeginTooltip()) {
                for (const std::string& m : meta) ImGui::TextUnformatted(m.c_str());
                ImGui::EndTooltip();
            }
        }
        ImGui::SameLine(0.0f, M::reThumbGap());
    }

    ImGui::BeginGroup();

    // ---- line 1: label + provenance -------------------------------------------------------
    if (row.fitCritical) {
        ImGui::PushStyleColor(ImGuiCol_Text, role4(row.alarm() ? palette.statusErr : palette.text));
        ImGui::TextUnformatted(row.label.c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine(0.0f, M::reCtlGap());
        kit::StatusChip("FIT-CRITICAL", row.alarm() ? Level::Error : Level::Warn,
                        "This dimension decides whether a printed replacement fits.\n"
                        "It must be confirmed individually with a typed caliper value -\n"
                        "the bulk confirm deliberately skips it.");
    } else {
        ImGui::TextUnformatted(row.label.c_str());
    }
    ImGui::SameLine(0.0f, M::reCtlGap());
    kit::HonestyTag(row.tier.c_str(), row.note.empty() ? nullptr : row.note.c_str());
    ImGui::SameLine(0.0f, M::reCtlGap());
    kit::StatusChip(row.source.c_str(), methodLevel(row), methodTip(row));
    if (row.alarm()) {
        ImGui::SameLine(0.0f, M::reCtlGap());
        kit::StatusChip("GUESSED", Level::Error,
                        "contracts/reveng.md section 0: no silent guessing on a fit-critical\n"
                        "dimension. Measure the part with calipers and type the value, or\n"
                        "read it off the datasheet - do not ship this number.");
    }
    if (row.settled()) {
        ImGui::SameLine(0.0f, M::reCtlGap());
        kit::StatusChip("confirmed", Level::Ok,
                        row.confirmed ? "Recorded in the intake ledger."
                                      : "Confirmed from this session.");
    }

    ImGui::Dummy(ImVec2(0.0f, M::reLabelGap()));

    // ---- line 2: reported value, caliper field, confirm ------------------------------------
    {
        const std::string shown = rowValueText(row);
        ImGui::PushStyleColor(ImGuiCol_Text,
                              role4(row.unscaled ? palette.statusWarn : palette.textDim));
        ImGui::TextUnformatted(shown.c_str());
        ImGui::PopStyleColor();
        if (row.hasTol) {
            ImGui::SameLine(0.0f, M::reCtlGap());
            ImGui::PushStyleColor(ImGuiCol_Text, role4(palette.textDim));
            const std::string tol = "+/- " + kit::fmtMm(row.tol_mm, 3);
            ImGui::TextUnformatted(tol.c_str());
            ImGui::PopStyleColor();
        }

        ImGui::SameLine(0.0f, M::reSectionGap());
        ImGui::SetNextItemWidth(M::reFieldW());
        ImGui::InputTextWithHint("##caliper", "caliper mm", row.editBuf, sizeof(row.editBuf),
                                 ImGuiInputTextFlags_CharsDecimal);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("The typed caliper value submitted to confirm_dimensions.\n"
                              "This is the number the model is built from.");

        ImGui::SameLine(0.0f, M::reCtlGap());
        double pending = 0.0;
        const bool canSend = rowSubmitValue(row, pending);
        if (row.busy) {
            kit::StatusChip("confirming", Level::Busy, nullptr);
        } else if (row.settled()) {
            if (kit::SecondaryButton("Re-confirm", canSend && st.workerStarted,
                                     canSend ? "Worker is not running."
                                             : "Type a caliper value in mm first."))
                confirmKeys(st, {row.key});
        } else {
            const char* why = !st.workerStarted ? "Worker is not running."
                            : !canSend          ? (row.unscaled
                                    ? "This value is in unscaled reconstruction units, not mm.\n"
                                      "Type the caliper reading."
                                    : "Type a caliper value in mm first.")
                            : nullptr;
            if (kit::PrimaryButton("Confirm", canSend && st.workerStarted, why))
                confirmKeys(st, {row.key});
        }
        if (row.justOk && !row.busy) {
            ImGui::SameLine(0.0f, M::reCtlGap());
            kit::StatusChip("recorded", Level::Ok, nullptr);
        }
    }

    // ---- line 3: server note / per-row error ----------------------------------------------
    if (!row.note.empty()) {
        ImGui::Dummy(ImVec2(0.0f, M::reLabelGap()));
        kit::InlineStatus(row.note.c_str(), Level::Warn);
    }
    if (!row.err.empty()) {
        ImGui::Dummy(ImVec2(0.0f, M::reLabelGap()));
        kit::InlineStatus(row.err.c_str(), Level::Error);
    }

    ImGui::EndGroup();
    ImGui::Unindent(padX);
    ImGui::Dummy(ImVec2(0.0f, padY));

    // ---- band background, now that the height is known -------------------------------------
    const ImVec2 mx(mn.x + fullW, ImGui::GetCursorScreenPos().y);
    const bool hovered = ImGui::IsMouseHoveringRect(mn, mx);

    const float* bandRole = row.alarm()       ? palette.statusErr
                          : row.fitCritical   ? palette.accent
                          : row.settled()     ? palette.statusOk
                                              : palette.textDim;
    float bandAlpha = row.alarm() ? 0.16f : (row.fitCritical ? 0.10f : 0.05f);
    if (hovered) bandAlpha += 0.05f;

    splitter.SetCurrentChannel(dl, 0);
    dl->AddRectFilled(mn, mx, roleU(bandRole, bandAlpha), M::reCardRound());
    if (row.fitCritical) {
        // Emphasis rail: a fit-critical row must be findable at a glance in a long ledger.
        const float rail = M::stroke(3.0f);
        dl->AddRectFilled(mn, ImVec2(mn.x + rail, mx.y),
                          roleU(row.alarm() ? palette.statusErr : palette.accent, 0.95f),
                          M::reChipRound());
    }
    if (d.selectedRow == idx)
        dl->AddRect(mn, mx, roleU(palette.accent, 0.75f), M::reCardRound(), 0, M::stroke(1.0f));
    splitter.Merge(dl);

    ImGui::PopID();
}

// ─── Deviation drawing helpers ───────────────────────────────────────────────────────────────

// Paints the sorted error profile inside the rect the 3D preview slot reserved. Every sample
// the tool measured is represented; the colour of each column is the tier that sample falls in,
// so "how much of this part is out of tolerance" is legible in one glance. When the GL pass
// eventually renders a spatial heatmap into PreviewSlot::Deviation it simply composites on top.
void paintErrorField(ImDrawList* dl, const ImRect& r, const DeviationResult& dev) {
    const float x0 = r.Min.x, y0 = r.Min.y, x1 = r.Max.x, y1 = r.Max.y;
    const float inset = M::U(1.0f);
    const float px0 = x0 + inset, px1 = x1 - inset;
    const float py1 = y1 - inset - ImGui::GetTextLineHeight() - M::reLabelGap();
    const float py0 = y0 + inset + ImGui::GetTextLineHeight() + M::reLabelGap();
    if (px1 - px0 < M::U(4.0f) || py1 - py0 < M::U(4.0f)) return;

    dl->PushClipRect(r.Min, r.Max, true);

    const double yMax = std::max({dev.max_mm, kTierLooseMm * 1.25, 1e-6});
    auto yFor = [&](double mm) {
        const double t = std::min(1.0, std::max(0.0, mm / yMax));
        return (float)(py1 - t * (py1 - py0));
    };

    // Tier guide lines at the two documented thresholds.
    for (int t = 0; t < 2; ++t) {
        const double thr = (t == 0) ? kTierTightMm : kTierLooseMm;
        if (thr > yMax) continue;
        const float gy = yFor(thr);
        dl->AddLine(ImVec2(px0, gy), ImVec2(px1, gy), roleU(tierRole(t), 0.45f), M::stroke(1.0f));
        char lbl[24];
        std::snprintf(lbl, sizeof(lbl), "%.2f mm", thr);
        dl->AddText(ImVec2(px0 + M::reLabelGap(), gy - ImGui::GetTextLineHeight()),
                    roleU(tierRole(t), 0.85f), lbl);
    }

    if (!dev.profile.empty()) {
        const int cols = std::min(256, std::max(16, (int)(px1 - px0)));
        const float cw = (px1 - px0) / (float)cols;
        const int   pn = (int)dev.profile.size();
        for (int i = 0; i < cols; ++i) {
            const float q  = (cols == 1) ? 0.0f : (float)i / (float)(cols - 1);
            const float fi = q * (float)(pn - 1);
            const int   i0 = (int)fi;
            const int   i1 = std::min(i0 + 1, pn - 1);
            const float fr = fi - (float)i0;
            const double v = (double)(dev.profile[i0] * (1.0f - fr) + dev.profile[i1] * fr);
            const float bx = px0 + cw * (float)i;
            dl->AddRectFilled(ImVec2(bx, yFor(v)), ImVec2(bx + cw + M::stroke(1.0f), py1),
                              roleU(tierRole(devTier(v)), 0.85f));
        }
        char cap[96];
        std::snprintf(cap, sizeof(cap), "%lld surface samples, sorted by error",
                      (long long)dev.samples);
        dl->AddText(ImVec2(px0, py1 + M::reLabelGap()), roleU(palette.textDim), cap);
    } else if (!dev.buckets.empty()) {
        // No per-sample array in this response — show the bucket shares as one honest band.
        float bx = px0;
        for (const DeviationBucket& b : dev.buckets) {
            const float w = (px1 - px0) * b.frac;
            if (w <= 0.0f) continue;
            dl->AddRectFilled(ImVec2(bx, py0), ImVec2(bx + w, py1), roleU(tierRole(b.tier), 0.85f));
            bx += w;
        }
        dl->AddText(ImVec2(px0, py1 + M::reLabelGap()), roleU(palette.textDim),
                    "share of surface per tier (per-sample distances not returned)");
    }

    dl->AddText(ImVec2(px0, y0 + inset), roleU(palette.textDim), "deviation vs. reference");
    dl->PopClipRect();
}

void drawBucketHistogram(const DeviationResult& dev, float w, float h) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImGui::InvisibleButton("##devhist", ImVec2(w, h));
    ImDrawList* dl = ImGui::GetWindowDrawList();

    const float lh      = ImGui::GetTextLineHeight();
    const float labelH  = lh * 2.0f + M::reLabelGap();
    const float plotH   = std::max(M::U(6.0f), h - labelH);
    const int   n       = (int)dev.buckets.size();
    if (n <= 0) return;

    const float gap  = M::reThumbGap();
    const float barW = std::max(M::U(2.0f), (w - gap * (float)(n - 1)) / (float)n);
    const float base = p.y + plotH;

    dl->AddLine(ImVec2(p.x, base), ImVec2(p.x + w, base), roleU(palette.border), M::stroke(1.0f));

    for (int i = 0; i < n; ++i) {
        const DeviationBucket& b = dev.buckets[i];
        const float bx = p.x + (barW + gap) * (float)i;
        const float bh = std::max(M::stroke(2.0f), plotH * b.frac);
        // Track behind every bar so a 0 % bucket still reads as "measured, empty".
        dl->AddRectFilled(ImVec2(bx, p.y), ImVec2(bx + barW, base),
                          roleU(palette.textDim, 0.10f), M::reChipRound());
        dl->AddRectFilled(ImVec2(bx, base - bh), ImVec2(bx + barW, base),
                          roleU(tierRole(b.tier), 0.90f), M::reChipRound());

        char pct[16];
        std::snprintf(pct, sizeof(pct), "%.0f%%", b.frac * 100.0f);
        const ImVec2 ps = ImGui::CalcTextSize(pct);
        dl->AddText(ImVec2(bx + (barW - ps.x) * 0.5f, base - bh - lh), roleU(palette.text), pct);

        const ImVec2 ls = ImGui::CalcTextSize(b.label.c_str());
        dl->AddText(ImVec2(bx + (barW - ls.x) * 0.5f, base + M::reLabelGap()),
                    roleU(tierRole(b.tier)), b.label.c_str());
        char cnt[32];
        std::snprintf(cnt, sizeof(cnt), "%lld", (long long)b.count);
        const ImVec2 cs = ImGui::CalcTextSize(cnt);
        dl->AddText(ImVec2(bx + (barW - cs.x) * 0.5f, base + M::reLabelGap() + lh),
                    roleU(palette.textDim), cnt);
    }
}

// Resume the card body after CardHeaderRight() widgets.
// kit::CardBegin's header row is one text line tall; a kit button is metrics::reBtnH() tall, so
// blindly restoring the saved body cursor lets a header button bleed onto the first body line.
// Take whichever is lower.
void resumeCardBody(const ImVec2& bodyPos) {
    const float bottomLocal = ImGui::GetItemRectMax().y - ImGui::GetWindowPos().y
                            + ImGui::GetScrollY() + M::reLabelGap();
    ImGui::SetCursorPos(ImVec2(bodyPos.x, std::max(bodyPos.y, bottomLocal)));
}

}  // namespace

// ═════════════════════════════════════════════════════════════════════════════════════════════
// Canned fixture — CHUDWARE_SHOT only
// ═════════════════════════════════════════════════════════════════════════════════════════════

void seedCannedDimPayload() {
    // HARD GATE. app.cpp calls this from the CHUDWARE_SHOT=reveng_review branch; if anything
    // else ever calls it in a real session it is a no-op, so demo dimensions cannot be mistaken
    // for a measurement of the user's part.
    if (!shotFixtureAllowed()) return;

    DimReviewState& d = DRS();
    d.rows.clear();
    d.confirmedLocal.clear();

    auto add = [&](const char* key, const char* label, double val, double tol, const char* src,
                   const char* method, const char* tier, bool critical, bool confirmed) {
        DimRow r;
        r.key = key;
        r.id  = key;
        r.label = label;
        r.value_mm = val;
        r.hasValue = true;
        r.tol_mm = tol;
        r.hasTol = tol > 0.0;
        r.source = src;
        r.method = method;
        r.tier = tier;
        r.fitCritical = critical;
        r.confirmed = confirmed;
        std::snprintf(r.editBuf, sizeof(r.editBuf), "%.4g", val);
        d.rows.push_back(std::move(r));
    };
    add("output_shaft.dia",    "output shaft diameter", 8.00,  0.16, "typed",     "user_confirmed", "high",   true,  true);
    add("mount_holes.spacing", "mount hole spacing",   18.00,  0.36, "datasheet", "datasheet",      "high",   true,  false);
    add("bore.dia",            "bore diameter",         5.00,  0.50, "intake",    "guessed",        "low",    true,  false);
    add("body.length",         "body length",          52.30,  2.62, "fitted",    "fitted",         "medium", false, false);
    add("wall.thickness",      "wall thickness",        2.00,  0.20, "fitted",    "fitted",         "medium", false, false);

    d.sessionId  = "re_canned00";
    d.loadedOnce = true;
    d.loadPending = false;
    d.canned     = true;
    d.lastLoadT  = 1.0e9;    // belt and braces: the live fetch is already blocked by `canned`
    d.loadTransportErr.clear();
    d.loadReason.clear();

    DeviationResult& dev = d.dev;
    dev = DeviationResult();
    dev.ok      = true;
    dev.max_mm  = 0.42;
    dev.rms_mm  = 0.11;
    dev.samples = 20000;
    dev.meshPath = "mesh/cleaned.ply";
    dev.buckets = {
        {0.0,  0.1,  false, 12200, 0.61f, 0, "< 0.10 mm"},
        {0.1,  0.5,  false,  7200, 0.36f, 1, "0.10 - 0.50 mm"},
        {0.5,  0.0,  true,    600, 0.03f, 2, ">= 0.50 mm"},
    };
    dev.profile.resize(kProfileN);
    for (int i = 0; i < kProfileN; ++i) {
        const double q = (double)i / (double)(kProfileN - 1);
        dev.profile[i] = (float)(dev.max_mm * q * q * q);   // long green tail, short red nose
    }
    d.devLoaded    = true;
    d.devSessionId = d.sessionId;
    d.lastDevT     = 1.0e9;
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// Dimension ledger
// ═════════════════════════════════════════════════════════════════════════════════════════════

void drawDimReviewPanel(AppState& st) {
    loadDims(st);
    DimReviewState& d = DRS();
    const SessionSummary* sess = selectedSession();

    // Tallies drive both the header chip and the progress summary.
    int total = 0, settled = 0, critical = 0, criticalOpen = 0, alarms = 0, nonCritOpen = 0;
    for (const DimRow& r : d.rows) {
        ++total;
        if (r.settled()) ++settled;
        if (r.fitCritical) {
            ++critical;
            if (!r.settled()) ++criticalOpen;
            if (r.alarm())    ++alarms;
        } else if (!r.settled()) {
            ++nonCritOpen;
        }
    }

    if (!kit::CardBegin("##re_dim_card", "Dimensions", "smart_dim", true, &d.dimsOpen)) {
        kit::CardEnd();
        return;
    }
    const ImVec2 bodyPos = ImGui::GetCursorPos();

    // ---- header right: gate state + refresh ---------------------------------------------
    {
        const char* gate = alarms > 0        ? "guessed fit-critical"
                         : criticalOpen > 0  ? "gate open"
                         : total == 0        ? "no dimensions"
                                             : "gate clear";
        const Level gl = alarms > 0       ? Level::Error
                       : criticalOpen > 0 ? Level::Warn
                       : total == 0       ? Level::Neutral
                                          : Level::Ok;
        const float hrW = ImGui::CalcTextSize(gate).x + ImGui::CalcTextSize("Refresh").x
                        + ImGui::GetStyle().FramePadding.x * 8.0f + M::reCtlGap();
        kit::CardHeaderRight(hrW);
        kit::StatusChip(gate, gl,
                        "The fit-critical gate: every dimension that decides whether a printed\n"
                        "replacement fits must carry a confirmed, non-guessed value.");
        ImGui::SameLine(0.0f, M::reCtlGap());
        if (kit::SecondaryButton("Refresh", sess != nullptr && st.workerStarted && !d.canned,
                                 d.canned ? "Showing the CHUDWARE_SHOT fixture."
                                          : (sess ? "Worker is not running." : "No session selected.")))
            d.forceReload = true;
        resumeCardBody(bodyPos);
    }

    if (d.canned) {
        kit::InlineStatus("CHUDWARE_SHOT=reveng_review fixture - these five rows are canned demo "
                          "data for the headless capture, not measurements of a real part.",
                          Level::Warn);
        kit::GapControl();
    }

    // ---- blocking states ------------------------------------------------------------------
    if (!sess && !d.canned) {
        kit::EmptyState("smart_dim", "No session selected",
                        "Pick a reverse-engineering session in the rail (or start one from the "
                        "ribbon) to review the dimensions the pipeline extracted for it.");
        kit::CardEnd();
        return;
    }
    if (!st.workerStarted && !d.canned) {
        kit::BlockedState("Worker not running",
                          "The dimension ledger lives in the reverse-engineering worker, and the "
                          "worker has not started.",
                          "Wait for the engine daemon to come up - the status bar shows a "
                          "reconnect chip while it is unreachable.");
        kit::CardEnd();
        return;
    }
    if (d.loadPending && !d.loadedOnce) {
        kit::InlineStatus("Reading the dimension ledger...", Level::Busy);
        kit::ProgressBar(-1.0f, nullptr, Level::Busy);
        kit::CardEnd();
        return;
    }
    if (!d.loadTransportErr.empty()) {
        if (kit::BlockedState("Dimension ledger unavailable", d.loadTransportErr.c_str(),
                              "list_reveng_dims did not answer. Check View > Console for the "
                              "worker log, then retry.", "Retry"))
            d.forceReload = true;
        kit::CardEnd();
        return;
    }
    if (!d.loadReason.empty()) {
        if (kit::BlockedState("No dimension ledger for this session", d.loadReason.c_str(),
                              d.loadHint.empty()
                                  ? "Re-select the session in the rail; the id may be stale."
                                  : d.loadHint.c_str(),
                              "Retry"))
            d.forceReload = true;
        kit::CardEnd();
        return;
    }

    // ---- legitimately empty ----------------------------------------------------------------
    if (d.rows.empty()) {
        const std::string body =
            d.emptyHint.empty()
                ? std::string("This session has no dimensions yet. Fit primitives to the "
                              "reconstructed mesh, ingest a datasheet, or measure a photo - each "
                              "writes rows into this ledger.")
                : d.emptyHint;
        const bool canFit = !S().artifactMeshPath.empty();
        if (kit::EmptyState("smart_dim", "No dimensions extracted yet", body.c_str(),
                            "Fit primitives to the mesh", canFit && st.workerStarted))
            kickFitPrimitives(st);
        if (!canFit) {
            kit::GapControl();
            kit::InlineStatus("No reconstructed mesh in this session yet - run Reconstruct first, "
                              "or ingest a datasheet from the Source stage.", Level::Warn);
        }
        kit::GapControl();
        if (kit::SecondaryButton("Ingest a datasheet", true, nullptr)) {
            S().stage      = Stage::Source;
            S().sourceMode = SourceMode::Datasheet;
        }
        for (const std::string& n : d.notes) {
            kit::GapControl();
            kit::InlineStatus(n.c_str(), Level::Warn);
        }
        kit::CardEnd();
        return;
    }

    // ---- progress summary -------------------------------------------------------------------
    {
        char cap[96];
        std::snprintf(cap, sizeof(cap), "%d of %d confirmed", settled, total);
        const Level pl = alarms > 0 ? Level::Error : (criticalOpen > 0 ? Level::Warn : Level::Ok);
        kit::ProgressBar(total > 0 ? (float)settled / (float)total : 0.0f, cap, pl);
        kit::GapControl();

        char critTxt[32];
        std::snprintf(critTxt, sizeof(critTxt), "%d of %d open", criticalOpen, critical);
        kit::ValueChip("fit-critical", critTxt,
                       criticalOpen > 0 ? Level::Warn : Level::Ok,
                       "Fit-critical dimensions must each be confirmed with a typed caliper\n"
                       "value. Until they are, a printed replacement is a gamble.");
        if (alarms > 0) {
            ImGui::SameLine(0.0f, M::reCtlGap());
            char alarmTxt[24];
            std::snprintf(alarmTxt, sizeof(alarmTxt), "%d", alarms);
            kit::ValueChip("guessed & critical", alarmTxt, Level::Error,
                           "contracts/reveng.md section 0 forbids a guessed value on a\n"
                           "fit-critical dimension. Measure these before building.");
        }
        if (!d.scaleConfirmed) {
            ImGui::SameLine(0.0f, M::reCtlGap());
            kit::StatusChip("scale unconfirmed", Level::Warn,
                            "session.scale.confirmed is false: every millimetre below inherits\n"
                            "the uncertainty of the scale solve. Confirm it in the Scale stage.");
        }
        ImGui::SameLine(0.0f, M::reCtlGap());
        kit::HelpMarker("Honesty tiers (contracts/reveng.md section 0):\n"
                        "  high        +/- 2 %  - datasheet or typed caliper\n"
                        "  medium      +/- 5 %  - good photo/fit evidence\n"
                        "  low         +/- 10 % - out-of-plane or weak evidence\n"
                        "  approximate reference / preview only, never a measurement");
    }

    for (const std::string& n : d.notes) {
        kit::GapControl();
        kit::InlineStatus(n.c_str(), Level::Warn);
    }

    // ---- the rows ---------------------------------------------------------------------------
    {
        const float slack  = M::reCardGap();
        const float budget = d.stageH > 0.0f ? d.stageH * kDimsShare : 0.0f;
        float listH = d.rowsMeasuredH > 0.0f ? d.rowsMeasuredH + slack
                                             : (float)d.rows.size() * (M::reThumbSm() + M::reCardPadY() * 2.0f);
        if (budget > 0.0f) listH = std::min(listH, budget);
        listH = std::max(listH, M::reThumbLg());

        kit::GapSection();
        ImGui::BeginChild("##re_dim_rows", ImVec2(0.0f, listH), ImGuiChildFlags_None);
        const float y0 = ImGui::GetCursorPosY();

        bool wroteCriticalHeader = false, wroteSupportHeader = false;
        for (int i = 0; i < (int)d.rows.size(); ++i) {
            DimRow& row = d.rows[i];
            if (row.fitCritical && !wroteCriticalHeader) {
                kit::SectionHeader("Fit-critical - confirm each one individually", "warning_badge");
                wroteCriticalHeader = true;
            } else if (!row.fitCritical && !wroteSupportHeader) {
                if (wroteCriticalHeader) kit::GapSection();
                kit::SectionHeader("Supporting dimensions", "measure");
                wroteSupportHeader = true;
            }
            drawDimRow(st, d, row, i);
            ImGui::Dummy(ImVec2(0.0f, M::reCardGap()));
        }
        d.rowsMeasuredH = ImGui::GetCursorPosY() - y0;
        ImGui::EndChild();
    }

    // ---- bulk confirm + gate feedback ---------------------------------------------------------
    kit::GapSection();
    {
        std::vector<std::string> bulk;
        for (const DimRow& r : d.rows)
            if (!r.fitCritical && !r.settled() && !r.busy) {
                double v = 0.0;
                if (rowSubmitValue(r, v)) bulk.push_back(r.key);
            }
        char label[64];
        std::snprintf(label, sizeof(label), "Confirm all non-critical (%d)", (int)bulk.size());
        const char* why = d.bulkPending      ? "A bulk confirm is already in flight."
                        : !st.workerStarted  ? "Worker is not running."
                        : bulk.empty()       ? (nonCritOpen > 0
                                ? "Those rows need a typed value first."
                                : "Every non-critical dimension is already confirmed.")
                                             : nullptr;
        if (kit::SecondaryButton(label, !bulk.empty() && st.workerStarted && !d.bulkPending, why))
            confirmKeys(st, bulk);
        ImGui::SameLine(0.0f, M::reCtlGap());
        kit::HelpMarker("Bulk confirm never touches a fit-critical dimension. Those are the "
                        "numbers a printed part fits or fails on, so each one is an explicit,\n"
                        "individual decision.");

        if (d.bulkPending) {
            ImGui::SameLine(0.0f, M::reCtlGap());
            kit::StatusChip("confirming", Level::Busy, nullptr);
        }
        if (!d.bulkErr.empty()) {
            kit::GapControl();
            kit::InlineStatus(d.bulkErr.c_str(), Level::Error);
        }
        if (d.gateKnown) {
            kit::GapControl();
            if (d.gateOk) {
                kit::InlineStatus("Intake gate clear - the ledger has every fit-critical value it "
                                  "demanded.", Level::Ok);
            } else {
                std::string msg = "Intake gate still blocking";
                if (!d.stillBlocking.empty()) {
                    msg += ": ";
                    for (size_t i = 0; i < d.stillBlocking.size(); ++i) {
                        if (i) msg += ", ";
                        msg += d.stillBlocking[i];
                    }
                }
                kit::InlineStatus(msg.c_str(), Level::Warn);
            }
        }
    }

    kit::CardEnd();
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// Deviation report
// ═════════════════════════════════════════════════════════════════════════════════════════════

void drawDeviationPanel(AppState& st) {
    loadDeviation(st);
    DimReviewState& d = DRS();
    const DeviationResult& dev = d.dev;
    const SessionSummary* sess = selectedSession();
    const int verdictTier = devTier(dev.max_mm);

    if (!kit::CardBegin("##re_dev_card", "Deviation - rebuild vs. scan", "geometry_analysis",
                        true, &d.devOpen)) {
        kit::CardEnd();
        return;
    }
    const ImVec2 bodyPos = ImGui::GetCursorPos();
    {
        const char* chip = dev.ok ? (verdictTier == 0 ? "within 0.1 mm"
                                   : verdictTier == 1 ? "within 0.5 mm" : "out of tolerance")
                                  : "not measured";
        const Level cl = dev.ok ? tierLevel(verdictTier) : Level::Neutral;
        const float hrW = ImGui::CalcTextSize(chip).x + ImGui::CalcTextSize("Recompute").x
                        + ImGui::GetStyle().FramePadding.x * 8.0f + M::reCtlGap();
        kit::CardHeaderRight(hrW);
        kit::StatusChip(chip, cl,
                        "Worst-point deviation between the reconstructed scan and the rebuilt\n"
                        "model. Tiers: under 0.1 mm tight, under 0.5 mm loose, above that the\n"
                        "rebuild does not represent the part.");
        ImGui::SameLine(0.0f, M::reCtlGap());
        if (kit::SecondaryButton("Recompute", sess != nullptr && st.workerStarted && !d.canned,
                                 d.canned ? "Showing the CHUDWARE_SHOT fixture."
                                          : (sess ? "Worker is not running." : "No session selected.")))
            d.devForce = true;
        resumeCardBody(bodyPos);
    }

    if (!sess && !d.canned) {
        kit::EmptyState("geometry_analysis", "No session selected",
                        "Select a session to compare its rebuilt model against the scanned mesh.");
        kit::CardEnd();
        return;
    }
    if (!st.workerStarted && !d.canned) {
        kit::BlockedState("Worker not running",
                          "The deviation solver runs in the reverse-engineering worker, and the "
                          "worker has not started.",
                          "Wait for the engine daemon to come up, then press Recompute.");
        kit::CardEnd();
        return;
    }
    if (d.devPending && !d.devLoaded) {
        kit::InlineStatus("Sampling the mesh against the reference...", Level::Busy);
        kit::ProgressBar(-1.0f, nullptr, Level::Busy);
        kit::CardEnd();
        return;
    }
    if (!d.devTransportErr.empty()) {
        if (kit::BlockedState("Deviation unavailable", d.devTransportErr.c_str(),
                              "get_reveng_deviation did not answer. Check View > Console for the "
                              "worker log, then recompute.", "Retry"))
            d.devForce = true;
        kit::CardEnd();
        return;
    }
    if (!dev.ok) {
        // Every refusal from this tool is instructive by contract ({ok, reason, hint}) — a
        // missing reference mesh or the unwired B-Rep tessellation path is a known limitation,
        // not something the user broke. Show the hint and put the fix right underneath it.
        const char* reason = dev.reason.empty()
            ? "No deviation has been computed for this session yet."
            : dev.reason.c_str();
        const char* hint = dev.hint.empty()
            ? "Export the rebuilt model to PLY or OBJ and give its path below, then recompute."
            : dev.hint.c_str();
        kit::BlockedState("Deviation not computed", reason, hint);

        kit::GapSection();
        kit::SectionHeader("Compare against a reference mesh", "mesh");
        kit::FieldLabel("Reference mesh (PLY / OBJ)",
                        "The rebuilt model exported to a mesh. Until B-Rep auto-tessellation is "
                        "wired, the solver needs an explicit file to sample against.");
        ImGui::SetNextItemWidth(std::max(M::reFieldW(),
                                         ImGui::GetContentRegionAvail().x - M::reBtnW()
                                             - M::reCtlGap()));
        ImGui::InputTextWithHint("##re_dev_ref", "/path/to/model.ply",
                                 d.refPathBuf, sizeof(d.refPathBuf));
        ImGui::SameLine(0.0f, M::reCtlGap());
        if (kit::PrimaryButton("Compare", d.refPathBuf[0] != '\0' && st.workerStarted,
                               d.refPathBuf[0] ? "Worker is not running."
                                               : "Enter the exported mesh path first."))
            d.devForce = true;
        if (!S().model.descDocId.empty()) {
            kit::GapControl();
            kit::KeyValueRow("Built doc", S().model.descDocId.c_str(), Level::Neutral);
        }
        kit::CardEnd();
        return;
    }

    // ---- the visual: error field beside the bucket histogram --------------------------------
    {
        const float availW = ImGui::GetContentRegionAvail().x;
        const float boxH   = M::reThumbLg();
        const float histW  = std::min(M::reThumbLg() * 1.4f, availW * 0.40f);
        const float fieldW = std::max(M::reThumbMd(), availW - histW - M::reCardGap());

        ImGui::BeginGroup();
        // The slot is published for the post-ImGui GL pass either way; paintErrorField fills it
        // with the CPU-side error field so the region is never a blank hole in the meantime.
        const char* slotEmpty = (dev.profile.empty() && dev.buckets.empty())
            ? "response carried no per-sample distances" : nullptr;
        const ImRect slot = kit::Viewport3DPreview("##re_dev_field", ImVec2(fieldW, boxH),
                                                   kit::PreviewSlot::Deviation, slotEmpty);
        paintErrorField(ImGui::GetWindowDrawList(), slot, dev);
        ImGui::EndGroup();

        ImGui::SameLine(0.0f, M::reCardGap());

        ImGui::BeginGroup();
        const float rightTopY = ImGui::GetCursorPosY();
        kit::ValueChip("worst point", kit::fmtMm(dev.max_mm, 3).c_str(), tierLevel(verdictTier),
                       "The single largest distance between the scan and the reference.\n"
                       "This is what decides whether a mating feature actually fits.");
        kit::GapControl();
        kit::ValueChip("rms", kit::fmtMm(dev.rms_mm, 3).c_str(), tierLevel(devTier(dev.rms_mm)),
                       "Root-mean-square deviation across every sample - the typical error,\n"
                       "as opposed to the worst one.");
        kit::GapControl();
        // The histogram takes whatever the two headline chips left of the preview's height, so
        // the right column always lines up with the error field beside it.
        const float histH = std::max(M::U(12.0f), boxH - (ImGui::GetCursorPosY() - rightTopY));
        drawBucketHistogram(dev, histW, histH);
        ImGui::EndGroup();
    }

    // ---- verdict -----------------------------------------------------------------------------
    kit::GapSection();
    kit::SectionHeader("Verdict", "results");
    {
        const char* verdict =
            verdictTier == 0
                ? "Within tight tolerance. The rebuild tracks the scan everywhere; press-fits and "
                  "mating features modelled from these dimensions should reproduce faithfully."
            : verdictTier == 1
                ? "Within loose tolerance. Good enough for a form-and-function replacement, but a "
                  "press-fit or bearing seat at the worst region will need its dimension typed "
                  "from a caliper rather than taken from the scan."
                : "Out of tolerance. At least one region is half a millimetre or more away from "
                  "the reference, so the rebuild does not represent the part yet. Re-check the "
                  "scale solve, then the primitive fits, before printing anything.";
        ImGui::PushStyleColor(ImGuiCol_Text, role4(tierRole(verdictTier)));
        ImGui::TextWrapped("%s", verdict);
        ImGui::PopStyleColor();

        kit::GapControl();
        if (!dev.worst.empty()) {
            kit::SectionHeader("Worst regions", "warning_badge");
            for (const std::string& w : dev.worst)
                kit::KeyValueRow("region", w.c_str(), Level::Error);
        } else if (!dev.buckets.empty()) {
            // The response carries no region labels, so name the worst BUCKET rather than
            // inventing a location.
            const DeviationBucket* worst = nullptr;
            for (const DeviationBucket& b : dev.buckets)
                if (b.count > 0 && (!worst || b.tier > worst->tier)) worst = &b;
            if (worst) {
                char line[160];
                std::snprintf(line, sizeof(line),
                              "%lld of %lld samples (%.1f%%) fall in the %s band. The response "
                              "does not label regions, so open the model and inspect that band "
                              "in the 3D view.",
                              (long long)worst->count, (long long)dev.samples,
                              worst->frac * 100.0f, worst->label.c_str());
                kit::InlineStatus(line, tierLevel(worst->tier));
            }
        }

        kit::GapControl();
        if (!dev.meshPath.empty())  kit::KeyValueRow("Scan mesh", dev.meshPath.c_str());
        if (!d.refPathSent.empty()) kit::KeyValueRow("Reference", d.refPathSent.c_str());
        else if (!dev.brepDocId.empty()) kit::KeyValueRow("Reference doc", dev.brepDocId.c_str());
        if (dev.samples > 0) {
            char s[48];
            std::snprintf(s, sizeof(s), "%lld", (long long)dev.samples);
            kit::KeyValueRow("Samples", s);
        }
    }

    kit::CardEnd();
}

// ═════════════════════════════════════════════════════════════════════════════════════════════
// Stage entry point
// ═════════════════════════════════════════════════════════════════════════════════════════════

void drawReviewStage(AppState& st) {
    DimReviewState& d = DRS();

    // Publish the stage height so the ledger can cap itself at kDimsShare of it and scroll
    // internally — the deviation card must always be reachable, however long the ledger is.
    // Collapsing either card frees its space for the other automatically, because a collapsed
    // kit card is just its header row.
    const float availY = ImGui::GetContentRegionAvail().y;
    if (availY > M::reThumbLg()) d.stageH = availY;

    drawDimReviewPanel(st);
    kit::GapCards();
    drawDeviationPanel(st);
}

}  // namespace reveng
}  // namespace ui
