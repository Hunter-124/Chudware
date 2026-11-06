// sources_panel.cpp — the Sources stage of the Reverse Engineering wizard (RE-40 rebuild).
//
// This is where a part enters the pipeline. Two intake lanes, chosen by the selector in the
// workspace shell:
//
//   Scan           — webcam console, file picker, video ingest and phone capture, plus the gallery.
//   From reference — part number / name / URL. FIRST an existing CAD model is searched for
//                    (reveng_find_models over GrabCAD, TraceParts, Printables, ...) and adopted with
//                    reveng_use_model; only when nothing fits is one built from documents
//                    (reveng_research_source / reveng_ingest_datasheet), which costs real work.
//
// Everything is drawn with ui::reveng::kit so all six stages read as one wizard, and every card
// that depends on a worker backend consults RevEngState::caps and disables with a REASON rather
// than failing after the click.
//
// GL-thread only. All service calls are ASYNC via st.rpc.call — a sync worker call in a draw path
// is a known freeze bug. Panel-local state lives in the file-static SourcesUi below, never in
// reveng_internal.h.
#include "sources_panel.h"

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"
#include "platform/camera.h"

#include "reveng_internal.h"
#include "reveng_widgets.h"          // ui::reveng::kit — the RE visual vocabulary
#include "webcam_capture.h"          // drawWebcamCapture
#include "image_gallery.h"           // drawImageGallery / galleryLoadPendingThumbs
#include "img_tex.h"                 // datasheet / QR previews -> GL textures
#include "../file_dialog.h"          // openFileDialog
#include "ui/parts/parts_widgets.h"  // b64decode for QR data URLs
#include "ui/web_capabilities.h"     // openExternalHttpUrl — "View page" on a model candidate
#include "ui/widgets/kit.h"          // ui::kit::elide

namespace ui {
namespace reveng {

namespace {

namespace mx = ui::metrics;

// ---------------------------------------------------------------------------------------------
// Panel-local state. File-static, GL-thread only — the shared store (reveng_internal.h) is owned
// by the workspace shell and only carries state more than one panel reads.
// ---------------------------------------------------------------------------------------------

enum class AddSlot { Photos, Drop };

struct SourcesUi {
    // Scan-lane capture method. Deep-linked by the three ribbon buttons via S().sourceFocus.
    int  captureTab = 0;              // 0 = Webcam, 1 = Photos, 2 = Video, 3 = Phone
    bool captureOpen = true;          // capture card expanded; starts shut for a session that already has images
    std::string settledSid;           // session whose open/closed default was already chosen
    bool focusPulse = false;          // one-shot: scroll the capture card into view
    uint64_t sessionGeneration = 0;
    bool webcamVisible = false;

    // reveng_add_images (file picker)
    bool        addPhotosPending = false;
    std::string addPhotosMsg;
    kit::Level  addPhotosLvl = kit::Level::Neutral;

    // reveng_add_images (drag-drop out of the gallery)
    std::string dropMsg;
    kit::Level  dropLvl = kit::Level::Neutral;

    bool        addVideoPending = false;
    std::string addVideoMsg;
    kit::Level  addVideoLvl = kit::Level::Neutral;

    // Datasheet lane detail (the shared store only keeps path / err / pending).
    int                      dsTables = 0;
    std::vector<std::string> dsCrops;
    std::vector<std::string> dsMpns;
    unsigned int             dsTex  = 0;
    int                      dsTexW = 0;
    int                      dsTexH = 0;
    std::string              dsTexPath;      // which file dsTex was decoded from

    // Web lane detail.
    std::vector<std::string> webMpns;
    std::vector<std::string> webSources;
    std::vector<std::pair<std::string, std::string>> webDims;   // label -> formatted value
    bool                     webRan = false;

    // Phone capture live poll.
    bool   phonePollPending = false;
    double phonePollT = -100.0;
};

SourcesUi g_ui;

// ---- small shared helpers --------------------------------------------------------------------

std::string activeSessionId() {
    const RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return {};
    return s.sessions[(size_t)s.selected].session_id;
}

// Provisioning lookup against the reveng_status() mirror. Returns true (available) when the probe
// has not landed yet: a card must not be dead on first paint just because the probe is in flight.
bool capPresent(const char* key, const char** hintOut = nullptr) {
    const RevEngState& s = S();
    if (hintOut) *hintOut = nullptr;
    if (!s.caps.loaded) return true;
    for (const CapabilityState::Artifact& a : s.caps.artifacts) {
        if (a.key == key) {
            if (hintOut && !a.hint.empty()) *hintOut = a.hint.c_str();
            return a.present;
        }
    }
    return true;   // the probe does not know this backend — do not invent a block
}

// Width hint for kit::CardHeaderRight.
float chipWidth(const char* label) {
    return ImGui::CalcTextSize(label).x + mx::reCardPadX() * 2.5f;
}

// ---------------------------------------------------------------------------------------------
// reveng_add_images — shared by the file picker and the gallery's drag-drop.
//
// The old code fired this with an empty callback in three places, so a failed add looked exactly
// like a successful one. Every path now reports its outcome.
// ---------------------------------------------------------------------------------------------

void kickAddImages(AppState& st, const std::string& sid,
                   std::vector<std::string> paths, AddSlot slot) {
    if (sid.empty() || paths.empty()) return;

    // Optimistically show the files in the contact sheet; the response decides their badge.
    RevEngState& s = S();
    for (const std::string& p : paths) {
        bool known = false;
        for (const GalleryImage& g : s.galleryImages)
            if (g.path == p) { known = true; break; }
        if (known) continue;
        GalleryImage g;
        g.path = p;
        s.galleryImages.push_back(std::move(g));
    }

    if (slot == AddSlot::Photos) {
        g_ui.addPhotosPending = true;
        g_ui.addPhotosMsg = "Adding to session\xe2\x80\xa6";
        g_ui.addPhotosLvl = kit::Level::Busy;
    } else {
        g_ui.dropMsg = "Adding dropped files\xe2\x80\xa6";
        g_ui.dropLvl = kit::Level::Busy;
    }

    nlohmann::json args;
    args["session_id"] = sid;
    args["paths"]      = nlohmann::json(paths);
    const uint64_t generation = webcamCaptureSessionGeneration();
    st.rpc.call("reveng_add_images", args, [paths, slot, sid, generation](const core::RpcResult& r) {
        if (generation != webcamCaptureSessionGeneration() || sid != activeSessionId()) return;
        std::string msg;
        kit::Level  lvl = kit::Level::Ok;
        bool        accepted = false;

        if (!r.ok) {
            msg = r.err.empty() ? "reveng_add_images failed (transport)." : r.err;
            lvl = kit::Level::Error;
        } else if (!r.result.is_object()) {
            msg = "reveng_add_images returned a non-object.";
            lvl = kit::Level::Error;
        } else if (!r.result.value("ok", true)) {
            msg = r.result.value("reason", std::string("reveng_add_images failed."));
            const auto hint = r.result.value("hint", std::string());
            if (!hint.empty()) msg += " — " + hint;
            lvl = kit::Level::Error;
        } else {
            const int added = r.result.value("added", 0);
            accepted = added > 0;
            char buf[128];
            if (accepted) {
                std::snprintf(buf, sizeof(buf), "Added %d image%s to the session.",
                              added, added == 1 ? "" : "s");
                lvl = kit::Level::Ok;
            } else {
                std::snprintf(buf, sizeof(buf), "%s",
                              "No new images added; matching inputs may already be in this session.");
                lvl = kit::Level::Warn;
            }
            msg = buf;
        }

        // The ingest response maps user paths onto durable content-addressed images. Honour
        // per-image successes even for a mixed-failure envelope, and never invent a quality score.
        if (r.ok && r.result.is_object() && r.result.contains("images") &&
            r.result["images"].is_array()) {
            auto& gallery = S().galleryImages;
            for (const auto& image : r.result["images"]) {
                if (!image.is_object()) continue;
                const auto persisted = image.value("path", std::string());
                if (persisted.empty()) continue;
                const auto source = image.value("source_path", std::string());
                const double sharpness = image.contains("blur_score") && image["blur_score"].is_number()
                    ? image["blur_score"].get<double>() : -1.0;
                for (const auto& input : paths) {
                    bool matches = input == source;
                    if (image.contains("source_paths") && image["source_paths"].is_array())
                        for (const auto& alias : image["source_paths"])
                            if (alias.is_string() && alias.get<std::string>() == input) matches = true;
                    if (!matches) continue;
                    const bool known = std::any_of(gallery.begin(), gallery.end(),
                        [&persisted](const GalleryImage& g) { return g.path == persisted; });
                    for (auto it = gallery.begin(); it != gallery.end(); ++it) {
                        if (it->path != input) continue;
                        if (known && input != persisted) {
                            gallery.erase(it);
                            S().selectedImage = -1;
                        } else {
                            it->path = persisted;
                            it->texId = 0;
                            it->texFailed = false;
                            it->sharpness = sharpness;
                            it->quality = sharpness < 0.0 ? "" :
                                sharpness < chudware::platform::kSharpnessBlurThreshold ? "blurred" : "ok";
                        }
                        break;
                    }
                }
            }
        }

        if (slot == AddSlot::Photos) {
            g_ui.addPhotosPending = false;
            g_ui.addPhotosMsg = msg;
            g_ui.addPhotosLvl = lvl;
        } else {
            g_ui.dropMsg = msg;
            g_ui.dropLvl = lvl;
        }
    });
}

// ---------------------------------------------------------------------------------------------
// Reference lane, step 1: find an existing CAD model (reveng_find_models / reveng_use_model)
// ---------------------------------------------------------------------------------------------

// Fill `r` from a reveng_find_models result or a mirrored session.reference — same shape.
void readReferenceSearch(const nlohmann::json& j, ReferenceState& r) {
    r.candidates.clear();
    r.standardPart.clear();
    r.sitesSearched = 0;
    if (j.contains("candidates") && j["candidates"].is_array()) {
        r.searched = true;
        for (const auto& c : j["candidates"]) {
            if (!c.is_object()) continue;
            ModelCandidate m;
            m.url = c.value("url", std::string());
            if (m.url.empty()) continue;
            m.title = c.contains("title") && c["title"].is_string() ? c["title"].get<std::string>() : m.url;
            m.site = c.contains("site") && c["site"].is_string() ? c["site"].get<std::string>() : std::string();
            m.confidence = c.contains("confidence") && c["confidence"].is_string()
                               ? c["confidence"].get<std::string>() : std::string("weak");
            r.candidates.push_back(std::move(m));
        }
    }
    if (j.contains("standard_part") && j["standard_part"].is_object()) {
        const auto& sp = j["standard_part"];
        r.standardPart = sp.contains("title") && sp["title"].is_string()
                             ? sp["title"].get<std::string>() : std::string("standard hardware");
    }
    if (j.contains("coverage") && j["coverage"].is_object())
        r.sitesSearched = j["coverage"].value("sites_searched", 0);
}

void kickFindModels(AppState& st) {
    RevEngState& s = S();
    ReferenceState& r = s.ref;
    const std::string sid = activeSessionId();
    // A running search blocks only a repeat of the same query; a new query supersedes it.
    if ((r.searchPending && r.searchQuery == r.queryBuf) || sid.empty() || r.queryBuf[0] == '\0')
        return;
    r.searchPending = true;
    r.searchQuery = r.queryBuf;
    r.searchErr.clear();
    r.useErr.clear();

    nlohmann::json params = {{"session_id", sid}, {"query", r.searchQuery}};
    const uint64_t generation = webcamCaptureSessionGeneration();
    st.rpc.call("reveng_find_models", params, [sid, generation](const core::RpcResult& res) {
        if (generation != webcamCaptureSessionGeneration() || sid != activeSessionId()) return;
        RevEngState& s2 = S();
        ReferenceState& r2 = s2.ref;
        if (!res.ok || !res.result.is_object() || !res.result.value("ok", false)) {
            r2.searchPending = false;
            const std::string reason = res.ok && res.result.is_object()
                ? res.result.value("reason", std::string("Model search failed."))
                : (res.err.empty() ? std::string("Worker unreachable.") : res.err);
            const std::string hint =
                res.ok && res.result.is_object() ? res.result.value("hint", std::string()) : "";
            r2.searchErr = hint.empty() ? reason : reason + " \xe2\x80\x94 " + hint;
            return;
        }
        if (res.result.value("pending", false)) {
            s2.lastDetailT = -100.0;   // the result arrives through session.reference
            return;
        }
        r2.searchPending = false;
        readReferenceSearch(res.result, r2);
        r2.searched = true;
        r2.hint = res.result.value("hint", std::string());
    });
}

// Acquire `url` (download into <project>/reference/) or adopt a local CAD `path`, record it as
// the session's model, and open it — the user asked for exactly this model.
void kickUseModel(AppState& st, const std::string& url, const std::string& path) {
    RevEngState& s = S();
    ReferenceState& r = s.ref;
    const std::string sid = activeSessionId();
    if (!r.usePendingKey.empty() || sid.empty() || (url.empty() && path.empty())) return;
    r.usePendingKey = url.empty() ? path : url;
    r.useErr.clear();

    nlohmann::json params = {{"session_id", sid}};
    if (!url.empty()) params["url"] = url;
    else              params["path"] = path;
    const uint64_t generation = webcamCaptureSessionGeneration();
    const bool fromUrl = !url.empty();
    st.rpc.call("reveng_use_model", params, [&st, sid, generation, fromUrl](const core::RpcResult& res) {
        if (generation != webcamCaptureSessionGeneration() || sid != activeSessionId()) return;
        RevEngState& s2 = S();
        ReferenceState& r2 = s2.ref;
        r2.usePendingKey.clear();
        if (!res.ok || !res.result.is_object() || !res.result.value("ok", false)) {
            if (!res.ok || !res.result.is_object()) {
                r2.useErr = res.err.empty() ? "Worker unreachable." : res.err;
                return;
            }
            // Many libraries (GrabCAD, MakerWorld, ...) need the user's own login to download;
            // the way through is the page itself, then the drop zone / Browse below.
            const std::string detail = res.result.value("detail", std::string());
            const std::string hint = res.result.value("hint", std::string());
            r2.useErr = !detail.empty() ? detail
                      : res.result.value("reason", std::string("Could not get the model.")) +
                            (hint.empty() ? "" : " \xe2\x80\x94 " + hint);
            if (fromUrl && r2.useErr.find("ownload it") == std::string::npos)
                r2.useErr += "  Download it from the page (View page), then drop the file below "
                             "or use Browse.";
            return;
        }
        r2.modelPath = res.result.value("path", std::string());
        const auto src = res.result.find("source_url");
        r2.modelSource = src != res.result.end() && src->is_string() ? src->get<std::string>()
                                                                     : std::string("local file");
        s2.lastDetailT = -100.0;   // stepper badges + status follow the new manifest
        s2.lastListT = -100.0;
        if (!r2.modelPath.empty()) st.requestOpenPath = r2.modelPath;
    }, core::kLongEngineCallTimeoutMs);
}

// ---------------------------------------------------------------------------------------------
// Reference lane, step 2 (fallback): reveng_research_source
// ---------------------------------------------------------------------------------------------

void kickResearch(AppState& st) {
    RevEngState& s = S();
    const std::string query = s.ref.queryBuf;
    if (s.researchPending || query.empty()) return;
    s.researchPending = true;
    s.researchErr.clear();
    s.researchResult.clear();
    g_ui.webMpns.clear();
    g_ui.webSources.clear();
    g_ui.webDims.clear();

    nlohmann::json params = nlohmann::json::object();
    const std::string sid = activeSessionId();
    if (!sid.empty()) params["session_id"] = sid;
    params["query"] = query;

    const uint64_t generation = webcamCaptureSessionGeneration();
    st.rpc.call("reveng_research_source", params, [sid, generation](const core::RpcResult& r) {
        if (generation != webcamCaptureSessionGeneration() || sid != activeSessionId()) return;
        RevEngState& s2 = S();
        s2.researchPending = false;
        g_ui.webRan = true;
        if (!r.ok) {
            s2.researchErr = r.err.empty() ? "reveng_research_source failed (transport)." : r.err;
            return;
        }
        if (!r.result.is_object()) {
            s2.researchErr = "reveng_research_source returned a non-object.";
            return;
        }
        if (!r.result.value("ok", true)) {
            const std::string reason = r.result.value("reason",
                                           std::string("reveng_research_source failed."));
            const std::string hint = r.result.value("hint", std::string());
            s2.researchErr = hint.empty() ? reason : (reason + " \xe2\x80\x94 " + hint);
            return;
        }
        s2.researchErr.clear();

        if (r.result.contains("summary") && r.result["summary"].is_string())
            s2.researchResult = r.result["summary"].get<std::string>();

        if (r.result.contains("candidate_mpns") && r.result["candidate_mpns"].is_array()) {
            for (const auto& m : r.result["candidate_mpns"])
                if (m.is_string()) g_ui.webMpns.push_back(m.get<std::string>());
        }
        if (r.result.contains("sources") && r.result["sources"].is_array()) {
            for (const auto& src : r.result["sources"]) {
                if (src.is_string()) {
                    g_ui.webSources.push_back(src.get<std::string>());
                } else if (src.is_object()) {
                    std::string title = src.value("title", std::string());
                    std::string url   = src.value("url", std::string());
                    if (title.empty()) title = url;
                    if (url.empty())   url = title;
                    g_ui.webSources.push_back(title == url ? url : (title + "  \xe2\x80\x94  " + url));
                }
            }
        }
        if (r.result.contains("candidate_dims") && r.result["candidate_dims"].is_array()) {
            for (const auto& d : r.result["candidate_dims"]) {
                if (!d.is_object()) continue;
                std::string label = d.value("label", d.value("name", std::string("dimension")));
                std::string value;
                if (d.contains("value_mm") && d["value_mm"].is_number())
                    value = kit::fmtMm(d["value_mm"].get<double>());
                else if (d.contains("value") && d["value"].is_number())
                    value = kit::fmtMm(d["value"].get<double>());
                else
                    value = d.value("value", std::string("?"));
                const std::string prov = d.value("provenance", std::string());
                if (!prov.empty()) value += "  (" + prov + ")";
                g_ui.webDims.emplace_back(std::move(label), std::move(value));
            }
        }
        if (s2.researchResult.empty()) {
            char buf[160];
            std::snprintf(buf, sizeof(buf),
                          "%zu candidate part number(s), %zu dimension(s), %zu source(s) pulled "
                          "into the session.",
                          g_ui.webMpns.size(), g_ui.webDims.size(), g_ui.webSources.size());
            s2.researchResult = buf;
        }
    });
}

// ---------------------------------------------------------------------------------------------
// reveng_ingest_datasheet (Datasheet lane)
// ---------------------------------------------------------------------------------------------

void releaseDatasheetTex() {
    if (g_ui.dsTex) {
        ::reveng::freeTexture(g_ui.dsTex);
        g_ui.dsTex  = 0;
        g_ui.dsTexW = 0;
        g_ui.dsTexH = 0;
    }
    g_ui.dsTexPath.clear();
}

// Decode a preview for the datasheet lane. Prefers the first extracted page crop (an actual
// picture of what was read) and falls back to the dropped file itself when it is an image. A PDF
// with no crops yields no texture, and HeroPreview shows a labelled placeholder rather than a hole.
void ensureDatasheetPreview() {
    const RevEngState& s = S();
    std::string want = g_ui.dsCrops.empty() ? s.datasheetPath : g_ui.dsCrops.front();
    if (want == g_ui.dsTexPath) return;
    releaseDatasheetTex();
    g_ui.dsTexPath = want;
    if (want.empty()) return;
    int w = 0, h = 0;
    const unsigned int tex = ::reveng::loadTexFromFile(want, &w, &h);
    if (tex) {
        g_ui.dsTex  = tex;
        g_ui.dsTexW = w;
        g_ui.dsTexH = h;
    }
}

void kickDatasheetIngest(AppState& st, const std::string& path) {
    RevEngState& s = S();
    if (s.datasheetPending || path.empty()) return;
    s.datasheetPending = true;
    s.datasheetErr.clear();
    s.datasheetPath = path;
    g_ui.dsTables = 0;
    g_ui.dsCrops.clear();
    g_ui.dsMpns.clear();
    releaseDatasheetTex();

    nlohmann::json params = nlohmann::json::object();
    const std::string sid = activeSessionId();
    if (!sid.empty()) params["session_id"] = sid;
    params["path"] = path;

    const uint64_t generation = webcamCaptureSessionGeneration();
    st.rpc.call("reveng_ingest_datasheet", params, [sid, generation](const core::RpcResult& r) {
        if (generation != webcamCaptureSessionGeneration() || sid != activeSessionId()) return;
        RevEngState& s2 = S();
        s2.datasheetPending = false;
        if (!r.ok) {
            s2.datasheetErr = r.err.empty() ? "reveng_ingest_datasheet failed (transport)." : r.err;
            return;
        }
        if (!r.result.is_object()) {
            s2.datasheetErr = "reveng_ingest_datasheet returned a non-object.";
            return;
        }
        if (!r.result.value("ok", true)) {
            const std::string reason = r.result.value("reason",
                                           std::string("reveng_ingest_datasheet failed."));
            const std::string hint = r.result.value("hint", std::string());
            s2.datasheetErr = hint.empty() ? reason : (reason + " \xe2\x80\x94 " + hint);
            return;
        }
        s2.datasheetErr.clear();
        if (r.result.contains("tables") && r.result["tables"].is_array())
            g_ui.dsTables = (int)r.result["tables"].size();
        if (r.result.contains("crops") && r.result["crops"].is_array()) {
            for (const auto& c : r.result["crops"])
                if (c.is_string()) g_ui.dsCrops.push_back(c.get<std::string>());
        }
        if (r.result.contains("mpn_candidates") && r.result["mpn_candidates"].is_array()) {
            for (const auto& m : r.result["mpn_candidates"])
                if (m.is_string()) g_ui.dsMpns.push_back(m.get<std::string>());
        }
    });
}

// ---------------------------------------------------------------------------------------------
// Phone capture (start_capture_session / stop_capture_session)
// ---------------------------------------------------------------------------------------------

void updatePhoneQrTexture(RevEngState& s) {
    if (s.phoneQrTex) {
        ::reveng::freeTexture(s.phoneQrTex);
        s.phoneQrTex = 0;
        s.phoneQrW = s.phoneQrH = 0;
    }
    if (s.phoneQr.empty()) return;
    const std::string prefix = "data:image/png;base64,";
    if (s.phoneQr.rfind(prefix, 0) != 0) return;   // bare URL fallback: the UI shows the string
    const std::vector<unsigned char> bytes = ui::parts::b64decode(s.phoneQr.substr(prefix.size()));
    if (bytes.empty()) return;
    int w = 0, h = 0;
    const unsigned int tex = ::reveng::loadTexFromMemory(bytes.data(), (int)bytes.size(), &w, &h);
    if (tex) {
        s.phoneQrTex = tex;
        s.phoneQrW = w;
        s.phoneQrH = h;
    }
}

// Adopt the {upload_url, qr, photo_count} envelope shared by the fresh-start and
// already-running responses.
void adoptPhoneSession(RevEngState& s, const nlohmann::json& res) {
    s.phoneActive = true;
    s.phonePort   = res.value("port", s.phonePort);
    s.phoneUrl    = res.value("upload_url", s.phoneUrl);
    s.phoneCount  = res.value("photo_count", s.phoneCount);
    if (res.contains("qr") && res["qr"].is_string()) {
        const std::string qr = res["qr"].get<std::string>();
        if (qr != s.phoneQr) {
            s.phoneQr = qr;
            updatePhoneQrTexture(s);
        }
    }
}

void kickStartPhoneCapture(AppState& st) {
    RevEngState& s = S();
    if (s.phonePending || s.phoneActive) return;
    s.phonePending = true;
    s.phoneErr.clear();
    s.phoneStatus = "Starting the upload server\xe2\x80\xa6";
    s.phoneUrl.clear();
    s.phoneQr.clear();
    s.phoneCount = 0;
    if (s.phoneQrTex) {
        ::reveng::freeTexture(s.phoneQrTex);
        s.phoneQrTex = 0;
    }

    nlohmann::json params = nlohmann::json::object();
    params["port"] = s.phonePort > 0 ? s.phonePort : 8742;

    const std::string sid = activeSessionId();
    const uint64_t generation = webcamCaptureSessionGeneration();
    st.rpc.call("start_capture_session", params, [sid, generation](const core::RpcResult& r) {
        if (generation != webcamCaptureSessionGeneration() || sid != activeSessionId()) return;
        RevEngState& s2 = S();
        s2.phonePending = false;
        if (!r.ok) {
            s2.phoneActive = false;
            s2.phoneErr = r.err.empty()
                ? "start_capture_session failed (is the worker running?)" : r.err;
            s2.phoneStatus.clear();
            return;
        }
        if (!r.result.is_object()) {
            s2.phoneActive = false;
            s2.phoneErr = "start_capture_session returned a non-object.";
            s2.phoneStatus.clear();
            return;
        }
        if (!r.result.value("ok", false)) {
            // A session already running on this port is a usable outcome, not a failure.
            if (r.result.contains("upload_url")) {
                adoptPhoneSession(s2, r.result);
                s2.phoneStatus = r.result.value("reason",
                    std::string("A capture session was already running on this port."));
                s2.phoneErr.clear();
                return;
            }
            const std::string reason = r.result.value("reason",
                                           std::string("start_capture_session failed."));
            const std::string hint = r.result.value("hint", std::string());
            s2.phoneActive = false;
            s2.phoneErr = hint.empty() ? reason : (reason + " \xe2\x80\x94 " + hint);
            s2.phoneStatus.clear();
            return;
        }
        adoptPhoneSession(s2, r.result);
        s2.phoneStatus = "Live. Open the URL on your phone, or scan the QR.";
        s2.phoneErr.clear();
    });
}

// Live received-count poll. start_capture_session on a port that is already serving returns the
// current photo_count without disturbing the session, which is the only status read the worker
// exposes — so it doubles as the poll. Only ever fired while a session is known to be active, so
// the "start" side effect can at worst re-establish a server we already believe is up.
void pollPhoneCount(AppState& st) {
    RevEngState& s = S();
    if (!s.phoneActive || s.phonePending || g_ui.phonePollPending) return;
    const double now = ImGui::GetTime();
    if (now - g_ui.phonePollT < 2.0) return;
    g_ui.phonePollT = now;
    g_ui.phonePollPending = true;

    nlohmann::json params = nlohmann::json::object();
    params["port"] = s.phonePort > 0 ? s.phonePort : 8742;
    const std::string sid = activeSessionId();
    const uint64_t generation = webcamCaptureSessionGeneration();
    st.rpc.call("start_capture_session", params, [sid, generation](const core::RpcResult& r) {
        if (generation != webcamCaptureSessionGeneration() || sid != activeSessionId()) return;
        g_ui.phonePollPending = false;
        if (!r.ok || !r.result.is_object()) return;
        RevEngState& s2 = S();
        if (r.result.contains("photo_count") || r.result.contains("upload_url"))
            adoptPhoneSession(s2, r.result);
    });
}

// `attach` false = stop the server and keep the photos in the gallery only.
void kickStopPhoneCapture(AppState& st, bool attach) {
    RevEngState& s = S();
    if (s.phonePending) return;
    s.phonePending = true;
    s.phoneStatus = attach ? "Pulling photos\xe2\x80\xa6" : "Stopping the upload server\xe2\x80\xa6";

    nlohmann::json params = nlohmann::json::object();
    params["port"] = s.phonePort > 0 ? s.phonePort : 8742;
    params["run_reconstruction"] = false;

    const std::string sid = attach ? activeSessionId() : std::string();
    AppState* stp = &st;   // AppState is process-lifetime for the host

    const auto origin = activeSessionId();
    const uint64_t generation = webcamCaptureSessionGeneration();
    st.rpc.call("stop_capture_session", params, [sid, stp, origin, generation](const core::RpcResult& r) {
        if (generation != webcamCaptureSessionGeneration() || origin != activeSessionId()) return;
        RevEngState& s2 = S();
        s2.phonePending = false;
        s2.phoneActive  = false;

        if (!r.ok) {
            s2.phoneErr = r.err.empty() ? "stop_capture_session failed (transport)." : r.err;
            s2.phoneStatus.clear();
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            const std::string reason = r.result.value("reason",
                                           std::string("stop_capture_session failed."));
            const std::string hint = r.result.value("hint", std::string());
            s2.phoneErr = hint.empty() ? reason : (reason + " \xe2\x80\x94 " + hint);
            s2.phoneStatus.clear();
            return;
        }

        s2.phoneErr.clear();
        int count = 0;
        std::vector<std::string> photos;
        if (r.result.is_object()) {
            count = r.result.value("photo_count", 0);
            if (r.result.contains("photos") && r.result["photos"].is_array()) {
                for (const auto& p : r.result["photos"])
                    if (p.is_string()) photos.push_back(p.get<std::string>());
            }
        }
        s2.phoneCount = count;
        s2.phoneUrl.clear();
        s2.phoneQr.clear();
        if (s2.phoneQrTex) {
            ::reveng::freeTexture(s2.phoneQrTex);
            s2.phoneQrTex = 0;
            s2.phoneQrW = s2.phoneQrH = 0;
        }

        if (photos.empty()) {
            s2.phoneStatus = "Capture stopped. No photos were received.";
            return;
        }
        for (const std::string& p : photos) {
            bool known = false;
            for (const GalleryImage& g : s2.galleryImages)
                if (g.path == p) { known = true; break; }
            if (known) continue;
            GalleryImage g;
            g.path = p;
            s2.galleryImages.push_back(std::move(g));
        }
        if (sid.empty() || !stp) {
            char buf[176];
            std::snprintf(buf, sizeof(buf),
                          "%d photo%s downloaded to the gallery. Select a session to attach them.",
                          (int)photos.size(), photos.size() == 1 ? "" : "s");
            s2.phoneStatus = buf;
            return;
        }
        s2.phoneStatus = "Photos downloaded \xe2\x80\x94 attaching to the session\xe2\x80\xa6";
        kickAddImages(*stp, sid, photos, AddSlot::Drop);
    });
}

// ---------------------------------------------------------------------------------------------
// Scan lane
// ---------------------------------------------------------------------------------------------

void drawPhotosTab(AppState& st, const std::string& sid) {
    RevEngState& s = S();

    kit::SectionHeader("Add photos from disk");
    ImGui::TextWrapped(
        "Pick photos you already shot, or drag them onto the gallery below from your file "
        "manager. For a dimensionally-useful reconstruction, aim for 20-40 sharp, well-lit "
        "photos that orbit the part with generous overlap between neighbours.");
    kit::GapControl();

    const bool canAdd = !sid.empty() && !g_ui.addPhotosPending;
    const char* why = sid.empty() ? "No active session — create or select one first."
                                  : "An add is already in flight.";
    if (kit::PrimaryButton("Add photos\xe2\x80\xa6", canAdd, canAdd ? nullptr : why,
                           ImVec2(0, mx::reBtnH()))) {
        const std::string path = ui::openFileDialog(
            "Images (*.png *.jpg *.jpeg *.bmp)\0*.png;*.jpg;*.jpeg;*.bmp\0"
            "All files (*.*)\0*.*\0");
        if (!path.empty()) kickAddImages(st, sid, { path }, AddSlot::Photos);
    }
    if (g_ui.addPhotosPending) kit::ProgressBar(-1.0f, "adding", kit::Level::Busy);
    kit::InlineStatus(g_ui.addPhotosMsg.c_str(), g_ui.addPhotosLvl);

    if (s.galleryImages.empty() && g_ui.addPhotosMsg.empty()) {
        kit::GapControl();
        kit::EmptyState(nullptr, "No photos in this session yet",
                        "Photos from the file picker, the webcam, your phone, or a drag-and-drop "
                        "all land in the same gallery below and feed the same reconstruction.");
    }
}

void drawVideoTab(AppState& st, const std::string& sid) {
    RevEngState& s = S();
    kit::SectionHeader("Video capture");
    ImGui::TextWrapped(
        "Move slowly with overlapping views of a stationary object. Choose whether to extract "
        "frames into the session or train a Gaussian splat from the clip.");
    kit::GapControl();

    const auto& gaussian = s.gaussian;
    const bool idle = !gaussian.pending &&
        (gaussian.status.empty() || gaussian.status == "done" || gaussian.status == "failed" ||
         gaussian.status == "cancelled" || gaussian.status == "interrupted");
    const bool canChoose = !g_ui.addVideoPending;
    if (kit::SecondaryButton("Choose video...", canChoose,
                             "Wait for frame extraction to finish.")) {
        const std::string path = ui::openFileDialog(
            "Video (*.mp4 *.mov *.avi *.mkv *.webm)\0*.mp4;*.mov;*.avi;*.mkv;*.webm\0"
            "All files (*.*)\0*.*\0");
        if (!path.empty()) {
            s.sourceVideoPath = path;
            g_ui.addVideoMsg.clear();
        }
    }
    if (!s.sourceVideoPath.empty()) {
        kit::GapControl();
        kit::KeyValueRow("clip", s.sourceVideoPath.c_str());
    }
    const bool canRun = st.workerStarted && !sid.empty() && !s.sourceVideoPath.empty() &&
                        idle && !g_ui.addVideoPending && !s.recon.active && !s.recon.kickPending;
    const char* why = !st.workerStarted ? "The worker is not connected."
                    : sid.empty() ? "Create or select a session first."
                    : s.sourceVideoPath.empty() ? "Choose a video first."
                    : "Wait for the active reconstruction or extraction to finish.";
    if (kit::PrimaryButton("Extract frames", canRun, canRun ? nullptr : why,
                           ImVec2(0, mx::reBtnH()))) {
        const std::string path = s.sourceVideoPath;
        const uint64_t generation = webcamCaptureSessionGeneration();
        g_ui.addVideoPending = true;
        g_ui.addVideoMsg = "Extracting frames...";
        g_ui.addVideoLvl = kit::Level::Busy;
        st.rpc.call("reveng_add_video", {{"session_id", sid}, {"path", path}},
                    [sid, generation](const core::RpcResult& r) {
            if (generation != webcamCaptureSessionGeneration() || sid != activeSessionId()) return;
            g_ui.addVideoPending = false;
            if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                g_ui.addVideoLvl = kit::Level::Error;
                g_ui.addVideoMsg = r.ok && r.result.is_object()
                    ? r.result.value("reason", std::string("Frame extraction failed."))
                    : (r.err.empty() ? std::string("Frame extraction failed.") : r.err);
                return;
            }
            const int count = r.result.value("added", 0);
            g_ui.addVideoMsg = "Added " + std::to_string(count) + " frames to this session.";
            g_ui.addVideoLvl = count ? kit::Level::Ok : kit::Level::Warn;
            S().lastDetailT = -100.0;
            S().lastListT = -100.0;
        }, core::kLongEngineCallTimeoutMs);
    }
#ifdef __EMSCRIPTEN__
    kit::SecondaryButton("Train splat", false,
                         "Splat training requires a GPU, which the browser server sandbox does not expose.");
#else
    if (kit::SecondaryButton("Train splat", canRun, canRun ? nullptr : why))
        startGaussian(st, sid, false, s.sourceVideoPath);
#endif
    if (g_ui.addVideoPending) kit::ProgressBar(-1.0f, "extracting", kit::Level::Busy);
    kit::InlineStatus(g_ui.addVideoMsg.c_str(), g_ui.addVideoLvl);
}

void drawPhoneTab(AppState& st, const std::string& sid) {
    RevEngState& s = S();
    pollPhoneCount(st);

    kit::SectionHeader("Capture with your phone");
    ImGui::TextWrapped(
        "This uses your phone's camera over your local network \xe2\x80\x94 nothing leaves your "
        "Wi-Fi and nothing is uploaded to the internet. The app starts a small web server on this "
        "machine; you open its page on the phone and every photo you take is sent straight back "
        "here. A modern phone camera usually out-resolves a USB webcam, so this is often the best "
        "image quality available to you. Both devices must be on the same network.");
    kit::GapControl();

    if (!s.phoneActive && !s.phonePending) {
        kit::FieldLabel("Port", "The local port the upload server listens on. Change it only if "
                                "something else already owns 8742.");
        ImGui::SetNextItemWidth(mx::reFieldW());
        ImGui::InputInt("##re_phone_port", &s.phonePort);
        if (s.phonePort < 1024) s.phonePort = 1024;
        if (s.phonePort > 65535) s.phonePort = 65535;
        kit::GapControl();
        if (kit::PrimaryButton("Start phone capture", true, nullptr, ImVec2(0, mx::reBtnH())))
            kickStartPhoneCapture(st);
        kit::GapControl();
        kit::EmptyState(nullptr, "Phone capture is not running",
                        "Start the server and a QR code appears here. Scan it with the phone's "
                        "camera to open the guided upload page.");
    } else if (s.phonePending) {
        kit::ProgressBar(-1.0f, s.phoneStatus.empty() ? "working" : s.phoneStatus.c_str(),
                         kit::Level::Busy);
    } else {
        // ---- QR hero ----
        const float side = std::min(mx::reThumbLg(), ImGui::GetContentRegionAvail().x);
        kit::HeroPreview("re_phone_qr", s.phoneQrTex, s.phoneQrW, s.phoneQrH,
                         ImVec2(side, side),
                         "Scan with your phone's camera",
                         "QR unavailable on this worker \xe2\x80\x94 type the URL below instead");
        kit::GapControl();

        if (!s.phoneUrl.empty()) {
            kit::KeyValueRow("open on phone", s.phoneUrl.c_str(), kit::Level::Ok);
            if (kit::SecondaryButton("Copy URL")) ImGui::SetClipboardText(s.phoneUrl.c_str());
            ImGui::SameLine(0.0f, mx::reCtlGap());
        }
        char cnt[32];
        std::snprintf(cnt, sizeof(cnt), "%d", s.phoneCount);
        kit::ValueChip("photos received", cnt,
                       s.phoneCount > 0 ? kit::Level::Ok : kit::Level::Neutral,
                       "Live count from the upload server, refreshed every couple of seconds "
                       "while this tab is open.");
        if (g_ui.phonePollPending) {
            ImGui::SameLine(0.0f, mx::reCtlGap());
            kit::StatusChip("refreshing", kit::Level::Busy);
        }

        kit::GapControl();
        const bool canPull = s.phoneCount > 0;
        if (kit::PrimaryButton("Done \xe2\x80\x94 pull photos into the session", canPull,
                               canPull ? nullptr
                                       : "No photos have been received yet.",
                               ImVec2(0, mx::reBtnH()))) {
            kickStopPhoneCapture(st, /*attach=*/true);
        }
        ImGui::SameLine(0.0f, mx::reCtlGap());
        if (kit::DangerButton("Stop without adding")) kickStopPhoneCapture(st, /*attach=*/false);

        if (sid.empty()) {
            kit::InlineStatus("No session is selected — pulled photos land in the gallery and can "
                              "be attached once you pick one.", kit::Level::Warn);
        }
    }

    kit::InlineStatus(s.phoneStatus.c_str(), kit::Level::Neutral);
    kit::InlineStatus(s.phoneErr.c_str(), kit::Level::Error);
}

void drawScanEntry(AppState& st) {
    RevEngState& s = S();
    const std::string sid = activeSessionId();

    // ---- capture-method card ----
    const bool hasRefs = !sid.empty() && S().selected >= 0 &&
                         S().selected < (int)S().sessions.size() &&
                         (S().sessions[(size_t)S().selected].images > 0 || !S().galleryImages.empty());
    if (kit::CardBegin("re_src_capture", hasRefs ? "Add more images or video" : "Capture an environment or object",
                       nullptr, true, &g_ui.captureOpen)) {
        const char* head = sid.empty() ? "no session" : "session ready";
        kit::CardHeaderRight(chipWidth(head));
        kit::StatusChip(head, sid.empty() ? kit::Level::Error : kit::Level::Ok,
                        sid.empty()
                            ? "Photos can be gathered without a session, but nothing can be "
                              "attached until one exists."
                            : "Captures will be written into the selected session.");

        static const char* kTabs[4] = { "Webcam", "Photos", "Video", "Phone" };
        kit::SegmentedControl("re_src_method", kTabs, 4, &g_ui.captureTab);
        if (g_ui.focusPulse) {
            ImGui::SetScrollHereY(0.0f);
            g_ui.focusPulse = false;
        }
        kit::GapSection();

        switch (g_ui.captureTab) {
            case 1:  drawPhotosTab(st, sid); break;
            case 2:  drawVideoTab(st, sid);  break;
            case 3:  drawPhoneTab(st, sid);  break;
            default: drawWebcamCapture(st, sid, /*session_dir=*/""); break;
        }
    }
    kit::CardEnd();
    kit::GapCards();
    drawGaussianPanel(st);

    kit::GapCards();

    // ---- gallery ----
    if (sid.empty() && s.galleryImages.empty()) {
        if (kit::CardBegin("re_src_gallery", "Captured images")) {
            kit::EmptyState(nullptr, "No session selected",
                            "Create or pick a session in the rail on the left. Then add photos "
                            "with the webcam, your phone, a video orbit, the file picker, or by "
                            "dropping files onto this panel. Scale and Reconstruct unlock once "
                            "the session holds images.");
        }
        kit::CardEnd();
        return;
    }

    galleryLoadPendingThumbs(s.galleryImages);
    GalleryActions actions;
    drawImageGallery(st, sid, s.galleryImages, actions);
    if (!actions.dropped.empty() && !sid.empty())
        kickAddImages(st, sid, actions.dropped, AddSlot::Drop);
    if (!actions.removed.empty()) {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "Removed %d image%s from the contact sheet.",
                      (int)actions.removed.size(), actions.removed.size() == 1 ? "" : "s");
        g_ui.dropMsg = buf;
        g_ui.dropLvl = kit::Level::Neutral;
    }
    kit::InlineStatus(g_ui.dropMsg.c_str(), g_ui.dropLvl);
}

// ---------------------------------------------------------------------------------------------
// From-reference lane: existing model first, documents second
// ---------------------------------------------------------------------------------------------

// Path of a file dragged out of the in-app Explorer onto the item drawn just before this call.
std::string acceptDroppedFile() {
    std::string path;
    if (ImGui::BeginDragDropTarget()) {
        if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload("FILES")) {
            const char* data = static_cast<const char*>(pl->Data);
            if (data && pl->DataSize > 0)
                path.assign(data, std::find(data, data + pl->DataSize, '\0'));
        }
        ImGui::EndDragDropTarget();
    }
    return path;
}

// A bordered drop target with a one-line caption. Returns a dropped path, or "".
std::string dropZone(const char* id, const char* caption) {
    const ImVec2 zone(ImGui::GetContentRegionAvail().x, ImGui::GetFrameHeight() * 2.0f);
    ImGui::BeginChild(id, zone, ImGuiChildFlags_Border, ImGuiWindowFlags_NoScrollbar);
    {
        const ImVec2 avail = ImGui::GetContentRegionAvail();
        const std::string text = ui::kit::elide(caption, avail.x);
        const ImVec2 ts = ImGui::CalcTextSize(text.c_str());
        ImGui::SetCursorPos(ImVec2(std::max(0.0f, (avail.x - ts.x) * 0.5f) + ImGui::GetCursorPosX(),
                                   std::max(0.0f, (avail.y - ts.y) * 0.5f) + ImGui::GetCursorPosY()));
        ImGui::TextDisabled("%s", text.c_str());
    }
    ImGui::EndChild();
    return acceptDroppedFile();   // must follow the child so the target region IS the zone
}

kit::Level confidenceLevel(const std::string& c) {
    return c == "exact" ? kit::Level::Ok : c == "strong" ? kit::Level::Warn : kit::Level::Neutral;
}

const char* confidenceLabel(const std::string& c) {
    return c == "exact" ? "part number match" : c == "strong" ? "likely match" : "similar";
}

void drawModelCandidates(AppState& st, ReferenceState& r) {
    const bool busy = !r.usePendingKey.empty();
    for (int i = 0; i < (int)r.candidates.size(); ++i) {
        const ModelCandidate& m = r.candidates[(size_t)i];
        ImGui::PushID(i);
        if (i > 0) kit::Rule();
        const float rightX = ImGui::GetCursorScreenPos().x + ImGui::GetContentRegionAvail().x;
        const std::string title = ui::kit::elide(m.title.c_str(), ImGui::GetContentRegionAvail().x);
        ImGui::TextUnformatted(title.c_str());
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s\n%s", m.title.c_str(), m.url.c_str());
        if (!m.site.empty()) {
            kit::StatusChip(m.site.c_str(), kit::Level::Neutral);
            ImGui::SameLine(0.0f, mx::reCtlGap());
        }
        kit::StatusChip(confidenceLabel(m.confidence), confidenceLevel(m.confidence),
                        m.confidence == "exact"
                            ? "The model's part number equals the one you searched for."
                            : "Matched on brand / name only \xe2\x80\x94 check it is the same part.");
        // Actions share the chip row, right-aligned, while they fit; a narrow card wraps them.
        const bool thisOne = r.usePendingKey == m.url;
        const char* useLabel = thisOne ? "Downloading\xe2\x80\xa6###use" : "Use this model###use";
        const float pad = ImGui::GetStyle().FramePadding.x * 2.0f;
        const float actionsW = ImGui::CalcTextSize("View page").x + pad + mx::reCtlGap() +
                               ImGui::CalcTextSize(useLabel, nullptr, true).x + pad;
        const float chipsEnd = ImGui::GetItemRectMax().x;
        if (chipsEnd + mx::reCtlGap() + actionsW <= rightX)
            ImGui::SameLine(0.0f, rightX - actionsW - chipsEnd);
        else
            kit::GapControl();
        if (kit::SecondaryButton("View page", true, nullptr))
            ui::openExternalHttpUrl(m.url);
        ImGui::SameLine(0.0f, mx::reCtlGap());
        if (kit::PrimaryButton(useLabel, !busy, "A model is already being downloaded."))
            kickUseModel(st, m.url, {});
        ImGui::PopID();
    }
}

void drawFindModelCard(AppState& st) {
    RevEngState& s = S();
    ReferenceState& r = s.ref;
    if (!kit::CardBegin("re_src_find", "Find an existing CAD model", "vendor_globe")) {
        kit::CardEnd();
        return;
    }
    ImGui::TextWrapped("Searches GrabCAD, TraceParts, Printables, 3D ContentCentral, McMaster and "
                       "other CAD libraries before anything is modelled. Paste a model page or "
                       "file URL to use it directly.");
    kit::GapControl();

    const float btnW = mx::reBtnW();
    ImGui::SetNextItemWidth(std::max(btnW, ImGui::GetContentRegionAvail().x - btnW -
                                               ImGui::GetStyle().ItemSpacing.x));
    const bool enter = ImGui::InputTextWithHint("##re_ref_query",
                                                "Part number, product name or model URL",
                                                r.queryBuf, sizeof(r.queryBuf),
                                                ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    const bool sameAsRunning = r.searchPending && r.searchQuery == r.queryBuf;
    const bool canSearch = !sameAsRunning && r.queryBuf[0] != '\0';
    const bool clicked = kit::PrimaryButton(sameAsRunning ? "\xe2\x80\xa6###re_ref_find"
                                                          : "Search###re_ref_find",
                                            canSearch,
                                            sameAsRunning ? "This search is already running."
                                                          : "Type a part number, name or URL.",
                                            ImVec2(btnW, 0.0f));
    if ((enter || clicked) && canSearch) kickFindModels(st);

    if (r.searchPending)
        kit::ProgressBar(-1.0f, "searching CAD libraries \xe2\x80\x94 this can take a minute or two",
                         kit::Level::Busy);
    kit::InlineStatus(r.searchErr.c_str(), kit::Level::Error);

    if (!r.modelPath.empty()) {
        kit::GapSection();
        const std::string file = std::filesystem::path(r.modelPath).filename().string();
        kit::InlineStatus(("Using " + file).c_str(), kit::Level::Ok);
        if (kit::SecondaryButton("Open model")) st.requestOpenPath = r.modelPath;
    }

    if (r.searched && !r.searchPending) {
        kit::GapSection();
        if (!r.standardPart.empty()) {
            kit::InlineStatus(("Standard hardware: " + r.standardPart +
                               " \xe2\x80\x94 insert it from the Parts Library.").c_str(),
                              kit::Level::Ok);
            if (kit::SecondaryButton("Open Parts Library"))
                st.activeWorkspace = AppState::Workspace::PartsLibrary;
            kit::GapControl();
        }
        if (!r.candidates.empty()) {
            char head[64];
            std::snprintf(head, sizeof(head), "%d model%s found", (int)r.candidates.size(),
                          r.candidates.size() == 1 ? "" : "s");
            kit::SectionHeader(head);
            drawModelCandidates(st, r);
        } else if (r.standardPart.empty()) {
            kit::InlineStatus(r.hint.empty() ? "No existing model found \xe2\x80\x94 build one "
                                               "from documents below."
                                             : r.hint.c_str(),
                              kit::Level::Warn);
        }
    }
    kit::InlineStatus(r.useErr.c_str(), kit::Level::Error);

    kit::GapSection();
    const std::string dropped =
        dropZone("##re_ref_drop", "Already downloaded one? Drop a STEP / IGES / STL file here");
    if (!dropped.empty()) kickUseModel(st, {}, dropped);
    kit::GapControl();
    if (kit::SecondaryButton("Browse\xe2\x80\xa6###re_ref_browse", r.usePendingKey.empty(),
                             "A model is already being added.")) {
        const std::string path = ui::openFileDialog(
            "CAD model (*.step *.stp *.iges *.igs *.stl *.3mf *.obj)\0"
            "*.step;*.stp;*.iges;*.igs;*.stl;*.3mf;*.obj\0All files (*.*)\0*.*\0");
        if (!path.empty()) kickUseModel(st, {}, path);
    }
    kit::CardEnd();
}

// Building our own model costs research and modelling work, so it sits below the search and says
// so when the search already found a usable model.
void drawBuildFromDocsCard(AppState& st) {
    RevEngState& s = S();
    const ReferenceState& r = s.ref;
    if (!kit::CardBegin("re_src_docs", "No usable model? Build one from documents", "file_doc")) {
        kit::CardEnd();
        return;
    }
    if (!r.modelPath.empty())
        kit::InlineStatus("This session already uses an existing model \xe2\x80\x94 building one is "
                          "unnecessary.", kit::Level::Neutral);
    else if (!r.candidates.empty())
        kit::InlineStatus("Models were found above \xe2\x80\x94 check them before building one.",
                          kit::Level::Neutral);
    else if (!r.searched)
        kit::InlineStatus("Search for an existing model first; this builds one from specs and "
                          "datasheets instead.", kit::Level::Neutral);
    kit::GapControl();

    // ---- web specs: same query box as the model search ----
    kit::SectionHeader("Look up specs online");
    const bool canResearch = !s.researchPending && r.queryBuf[0] != '\0';
    if (kit::SecondaryButton(s.researchPending ? "Researching\xe2\x80\xa6###re_research"
                                               : "Research specs###re_research",
                             canResearch,
                             s.researchPending ? "Research is already running."
                                               : "Type a part number or name above first."))
        kickResearch(st);
    kit::InlineStatus(s.researchErr.c_str(), kit::Level::Error);

    // ---- datasheet ----
    kit::GapSection();
    kit::SectionHeader("Datasheet or drawing");
    const char* doclingHint = nullptr;
    if (!capPresent("docling", &doclingHint)) {
        kit::InlineStatus(doclingHint ? doclingHint
                                      : "The datasheet reader is not installed (pip install "
                                        "docling, or pdfplumber for text-only tables).",
                          kit::Level::Warn);
    } else {
        const std::string dropped = dropZone(
            "##re_ds_drop", s.datasheetPath.empty() ? "Drop a PDF or image here"
                                                    : s.datasheetPath.c_str());
        if (!dropped.empty()) kickDatasheetIngest(st, dropped);
        kit::GapControl();
        if (kit::SecondaryButton("Browse\xe2\x80\xa6###re_ds_browse", !s.datasheetPending,
                                 "A datasheet is already being read.")) {
            const std::string path = ui::openFileDialog(
                "Spec sheet (*.pdf *.png *.jpg *.jpeg)\0*.pdf;*.png;*.jpg;*.jpeg\0"
                "All files (*.*)\0*.*\0");
            if (!path.empty()) kickDatasheetIngest(st, path);
        }
        if (s.datasheetPending) kit::ProgressBar(-1.0f, "reading the document", kit::Level::Busy);
        kit::InlineStatus(s.datasheetErr.c_str(), kit::Level::Error);
    }
    kit::CardEnd();
}

// What research and the datasheet reader actually produced. Only drawn once one of them ran.
void drawDocsResultCard() {
    const RevEngState& s = S();
    const bool research = g_ui.webRan || !s.researchResult.empty();
    const bool datasheet = !s.datasheetPath.empty() && !s.datasheetPending;
    if (!research && !datasheet) return;

    kit::GapCards();
    if (!kit::CardBegin("re_src_docs_result", "What was found", "results")) {
        kit::CardEnd();
        return;
    }
    if (research) {
        if (!s.researchResult.empty()) ImGui::TextWrapped("%s", s.researchResult.c_str());
        if (!g_ui.webDims.empty()) {
            kit::GapSection();
            kit::SectionHeader("Catalogue dimensions");
            for (const auto& d : g_ui.webDims)
                kit::KeyValueRow(d.first.c_str(), d.second.c_str(), kit::Level::Warn);
            kit::GapControl();
            kit::HonestyTag("low", "Catalogue values, not measured off your part.");
        }
        if (!g_ui.webMpns.empty()) {
            kit::GapSection();
            kit::SectionHeader("Part numbers");
            for (const std::string& m : g_ui.webMpns) kit::KeyValueRow("MPN", m.c_str());
        }
        if (!g_ui.webSources.empty()) {
            kit::GapSection();
            kit::SectionHeader("Sources");
            for (const std::string& src : g_ui.webSources) ImGui::TextWrapped("%s", src.c_str());
        }
    }
    if (datasheet) {
        if (research) kit::GapSection();
        ensureDatasheetPreview();
        const float w = std::min(mx::rePreviewMaxW(), ImGui::GetContentRegionAvail().x);
        kit::HeroPreview("re_ds_hero", g_ui.dsTex, g_ui.dsTexW, g_ui.dsTexH,
                         ImVec2(w, w * 9.0f / 16.0f),
                         g_ui.dsCrops.empty() ? "datasheet" : "extracted page",
                         "No page image \xe2\x80\x94 PDFs show a crop once the reader has run");
        kit::GapControl();
        char nb[24];
        std::snprintf(nb, sizeof(nb), "%d", g_ui.dsTables);
        kit::ValueChip("tables", nb, g_ui.dsTables > 0 ? kit::Level::Ok : kit::Level::Warn,
                       "Dimension tables read from the document.");
        for (const std::string& m : g_ui.dsMpns) kit::KeyValueRow("MPN", m.c_str());
    }
    kit::CardEnd();
}

void drawReferenceEntry(AppState& st) {
    drawFindModelCard(st);
    kit::GapCards();
    drawBuildFromDocsCard(st);
    drawDocsResultCard();
}

}  // namespace

// ---------------------------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------------------------

void drawSourcesPanel(AppState& st) {
    RevEngState& s = S();
    if (g_ui.sessionGeneration != webcamCaptureSessionGeneration()) {
        releaseDatasheetTex();
        const int tab = g_ui.captureTab;
        g_ui = SourcesUi{};
        g_ui.captureTab = tab;
        g_ui.sessionGeneration = webcamCaptureSessionGeneration();
    }

    // One-shot deep link from the three ribbon buttons (reveng_add_photos / reveng_add_video /
    // reveng_webcam). Consume-and-clear, same contract as focusNewName.
    if (s.sourceFocus != SourceFocus::None) {
        switch (s.sourceFocus) {
            case SourceFocus::Photos: g_ui.captureTab = 1; break;
            case SourceFocus::Video:  g_ui.captureTab = 2; break;
            case SourceFocus::Webcam: g_ui.captureTab = 0; break;
            default: break;
        }
        g_ui.focusPulse = true;
        g_ui.captureOpen = true;
        s.sourceFocus = SourceFocus::None;
    }
    // A session that already holds images (usually one phone video) opens with the capture card
    // shut and never auto-starts the webcam: the user is reviewing, not capturing. Chosen once per
    // session, so a later expand/collapse sticks.
    {
        const std::string sid = activeSessionId();
        if (!sid.empty() && g_ui.settledSid != sid && s.selected >= 0 &&
            s.selected < (int)s.sessions.size()) {
            g_ui.settledSid = sid;
            if (s.sessions[(size_t)s.selected].images > 0) {
                g_ui.captureOpen = false;
                if (g_ui.captureTab == 0) g_ui.captureTab = 2;
            }
        }
    }
    // Capture is owned by the visible webcam console, never by a hidden source lane.
    const bool showWebcam = s.sourceMode == SourceMode::Scan && g_ui.captureTab == 0 &&
                            g_ui.captureOpen;
    if (g_ui.webcamVisible && !showWebcam) webcamCaptureShutdown();
    g_ui.webcamVisible = showWebcam;

    if (s.sourceMode == SourceMode::Scan) drawScanEntry(st);
    else                                  drawReferenceEntry(st);
}

void mirrorReference(const nlohmann::json& reference) {
    ReferenceState& r = S().ref;
    if (!reference.is_object()) return;
    auto text = [&](const char* key) {
        const auto it = reference.find(key);
        return it != reference.end() && it->is_string() ? it->get<std::string>() : std::string();
    };
    const std::string query = text("query");
    const std::string status = text("status");   // empty for a search that ran synchronously
    if (r.queryBuf[0] == '\0' && !query.empty())
        std::snprintf(r.queryBuf, sizeof(r.queryBuf), "%s", query.c_str());

    if (reference.contains("model") && reference["model"].is_object()) {
        const auto& m = reference["model"];
        r.modelPath = m.contains("path") && m["path"].is_string() ? m["path"].get<std::string>() : "";
        r.modelSource = m.contains("source_url") && m["source_url"].is_string()
                            ? m["source_url"].get<std::string>() : std::string("local file");
    }

    if (status == "searching") {
        // Ours, or one started before this tab was opened (or by an agent): show it running.
        r.searchPending = true;
        r.searchQuery = query;
        return;
    }
    if (r.searchPending && query != r.searchQuery) return;   // manifest predates our search
    r.searchPending = false;
    if (status == "failed") {
        r.searchErr = text("error").empty() ? std::string("Model search failed.") : text("error");
        return;
    }
    if (reference.contains("candidates")) {
        readReferenceSearch(reference, r);
        r.hint = text("hint");
    }
}

}  // namespace reveng
}  // namespace ui
