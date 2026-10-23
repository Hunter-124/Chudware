// sources_panel.cpp — the Sources stage of the Reverse Engineering wizard (RE-40 rebuild).
//
// This is the stage an engineer spends most of their time in: it is where a physical part on the
// bench becomes pixels the pipeline can reconstruct. Four lanes, chosen by the source-mode control
// in the workspace shell:
//
//   Scan        — webcam console, file picker, video ingest and phone capture, plus the gallery.
//   Datasheet   — PDF/image spec sheet -> reveng_ingest_datasheet -> straight to Model.
//   Web+Part#   — part number / URL / query -> reveng_research_source -> straight to Model.
//   FromLibrary — seeded externally by the Parts Library; shows the routed mode read-only.
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

    // The video path belongs to the asynchronous Gaussian job, not an extraction run token.
    std::string addVideoPath;

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
    std::string              webDatasheetDir;
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
// reveng_research_source (Web lane)
// ---------------------------------------------------------------------------------------------

void kickResearch(AppState& st) {
    RevEngState& s = S();
    if (s.researchPending || s.researchQuery.empty()) return;
    s.researchPending = true;
    s.researchErr.clear();
    s.researchResult.clear();
    g_ui.webMpns.clear();
    g_ui.webSources.clear();
    g_ui.webDims.clear();
    g_ui.webDatasheetDir.clear();

    nlohmann::json params = nlohmann::json::object();
    const std::string sid = activeSessionId();
    if (!sid.empty()) params["session_id"] = sid;
    params["query"] = s.researchQuery;

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
        g_ui.webDatasheetDir = r.result.value("datasheet_dir", std::string());

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
    kit::SectionHeader("Train a Gaussian splat from video");
    ImGui::TextWrapped(
        "Move slowly with overlapping views of a stationary scene or object. The local job "
        "extracts sharp frames, solves cameras, then trains a visible 3D Gaussian splat. "
        "Use the Gaussian panel below for environment/object mode, quality and cropping. "
        "Photos remain in this session for the separate mesh/CAD reconstruction workflow.");
    kit::GapControl();

    const auto& gaussian = S().gaussian;
    const bool idle = !gaussian.pending &&
        (gaussian.status.empty() || gaussian.status == "done" || gaussian.status == "failed" ||
         gaussian.status == "cancelled" || gaussian.status == "interrupted");
    const bool canAdd = st.workerStarted && !sid.empty() && idle;
    const char* why = sid.empty() ? "Create or select a session first."
        : !st.workerStarted ? "The worker is not connected." : "A Gaussian job is already active.";
    if (kit::PrimaryButton("Choose video and train splat...", canAdd, canAdd ? nullptr : why,
                           ImVec2(0, mx::reBtnH()))) {
        const std::string path = ui::openFileDialog(
            "Video (*.mp4 *.mov *.avi *.mkv *.webm)\0*.mp4;*.mov;*.avi;*.mkv;*.webm\0"
            "All files (*.*)\0*.*\0");
        if (!path.empty()) {
            g_ui.addVideoPath = path;
            startGaussian(st, sid, false, path);
        }
    }
    if (!g_ui.addVideoPath.empty()) {
        kit::GapControl();
        kit::KeyValueRow("clip", g_ui.addVideoPath.c_str());
    }
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
    if (kit::CardBegin("re_src_capture", "Capture an environment or object")) {
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
// Datasheet lane
// ---------------------------------------------------------------------------------------------

void drawDatasheetEntry(AppState& st) {
    RevEngState& s = S();
    const char* doclingHint = nullptr;
    const bool docling = capPresent("docling", &doclingHint);
    const std::string blockReason =
        doclingHint ? std::string(doclingHint)
                    : std::string("The datasheet reader backend is not provisioned on the worker "
                                  "(pip install docling, or pdfplumber for text-only tables).");

    if (kit::CardBegin("re_src_datasheet", "Datasheet / spec sheet")) {
        const char* head = docling ? "reader ready" : "reader missing";
        kit::CardHeaderRight(chipWidth(head));
        kit::StatusChip(head, docling ? kit::Level::Ok : kit::Level::Error,
                        docling ? "reveng_status() reports the datasheet reader is provisioned."
                                : blockReason.c_str());

        ImGui::TextWrapped(
            "Drop a PDF or an image of a spec sheet below. The reader extracts the dimension "
            "tables and routes them straight to Model \xe2\x80\x94 Scale and Reconstruct are "
            "skipped, because a printed dimension is already a measurement.");
        kit::GapControl();
        kit::HonestyTag("medium",
                        "Datasheet dimensions are as good as the document: nominal values, often "
                        "without tolerances. Fit-critical features still need confirmation "
                        "against the real part.");
        kit::GapSection();

        if (!docling) {
            kit::BlockedState("Datasheet reader not provisioned", blockReason.c_str(),
                              "Install the backend on the worker, then reopen this tab. You can "
                              "use the Scan lane in the meantime.");
            kit::CardEnd();
            return;
        }

        // ---- drop zone ---- (reThumbMd is the nearest token to a comfortable drop target; a
        // dedicated reDropZoneH() would be the honest name for it)
        const ImVec2 zone(ImGui::GetContentRegionAvail().x, mx::reThumbMd());
        ImGui::BeginChild("##re_ds_drop", zone, ImGuiChildFlags_Border);
        {
            const ImVec2 avail = ImGui::GetContentRegionAvail();
            ImGui::Dummy(ImVec2(0.0f, avail.y * 0.25f));
            const char* hint = s.datasheetPath.empty() ? "Drop a PDF or image here"
                                                       : s.datasheetPath.c_str();
            const ImVec2 ts = ImGui::CalcTextSize(hint);
            float cx = (avail.x - ts.x) * 0.5f;
            if (cx < mx::reCardPadX()) cx = mx::reCardPadX();
            ImGui::SetCursorPosX(cx);
            if (s.datasheetPath.empty()) ImGui::TextDisabled("%s", hint);
            else                         ImGui::TextUnformatted(hint);
        }
        ImGui::EndChild();
        // Must follow the drop-zone child immediately so the target region IS the drop zone.
        if (ImGui::BeginDragDropTarget()) {
            if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload("FILES")) {
                const char* data = static_cast<const char*>(pl->Data);
                if (data && pl->DataSize > 0) {
                    const std::string path(data, std::find(data, data + pl->DataSize, '\0'));
                    kickDatasheetIngest(st, path);
                }
            }
            ImGui::EndDragDropTarget();
        }

        kit::GapControl();
        if (kit::SecondaryButton("Browse\xe2\x80\xa6", !s.datasheetPending,
                                 s.datasheetPending ? "An ingest is already running." : nullptr)) {
            const std::string path = ui::openFileDialog(
                "Spec sheet (*.pdf *.png *.jpg *.jpeg)\0*.pdf;*.png;*.jpg;*.jpeg\0"
                "All files (*.*)\0*.*\0");
            if (!path.empty()) kickDatasheetIngest(st, path);
        }

        if (s.datasheetPending) kit::ProgressBar(-1.0f, "reading the document", kit::Level::Busy);
        kit::InlineStatus(s.datasheetErr.c_str(), kit::Level::Error);
    }
    kit::CardEnd();

    kit::GapCards();

    // ---- what was actually read ----
    if (kit::CardBegin("re_src_ds_result", "What was read")) {
        if (s.datasheetPath.empty()) {
            kit::EmptyState(nullptr, "Nothing ingested yet",
                            "Drop a spec sheet above and its extracted page crops and dimension "
                            "tables appear here.");
        } else {
            ensureDatasheetPreview();
            const float w = std::min(mx::rePreviewMaxW(), ImGui::GetContentRegionAvail().x);
            const float h = w * 9.0f / 16.0f;
            kit::HeroPreview("re_ds_hero", g_ui.dsTex, g_ui.dsTexW, g_ui.dsTexH, ImVec2(w, h),
                             g_ui.dsCrops.empty() ? "source document"
                                                  : "extracted page crop",
                             "No page image \xe2\x80\x94 PDFs render a crop only once the reader "
                             "has run");
            kit::GapControl();

            char nb[24];
            std::snprintf(nb, sizeof(nb), "%d", g_ui.dsTables);
            kit::ValueChip("tables", nb, g_ui.dsTables > 0 ? kit::Level::Ok : kit::Level::Warn,
                           "Dimension tables the reader recovered from the document.");
            ImGui::SameLine(0.0f, mx::reCtlGap());
            std::snprintf(nb, sizeof(nb), "%d", (int)g_ui.dsCrops.size());
            kit::ValueChip("page crops", nb,
                           g_ui.dsCrops.empty() ? kit::Level::Neutral : kit::Level::Ok);

            if (!g_ui.dsMpns.empty()) {
                kit::GapSection();
                kit::SectionHeader("Candidate part numbers");
                for (const std::string& m : g_ui.dsMpns) kit::KeyValueRow("MPN", m.c_str());
            }
            if (!g_ui.dsCrops.empty()) {
                kit::GapSection();
                kit::SectionHeader("Page crops");
                for (size_t i = 0; i < g_ui.dsCrops.size(); ++i) {
                    char key[24];
                    std::snprintf(key, sizeof(key), "crop %d", (int)i + 1);
                    kit::KeyValueRow(key, g_ui.dsCrops[i].c_str());
                }
            }
        }
    }
    kit::CardEnd();
}

// ---------------------------------------------------------------------------------------------
// Web / Part# lane
// ---------------------------------------------------------------------------------------------

void drawWebEntry(AppState& st) {
    RevEngState& s = S();

    if (kit::CardBegin("re_src_web", "Web / part number")) {
        ImGui::TextWrapped(
            "Enter a manufacturer part number, a vendor URL, or a plain search query. The "
            "researcher pulls specs, datasheets and images into the session and routes straight "
            "to Model \xe2\x80\x94 Scale and Reconstruct are skipped, because nothing here was "
            "measured off your bench.");
        kit::GapControl();
        kit::HonestyTag("low",
                        "Web-sourced dimensions describe the catalogue part, not the part in your "
                        "hand. Anything fit-critical must be confirmed with calipers before you "
                        "cut metal.");
        kit::GapSection();

        kit::FieldLabel("Query", "Part number, vendor URL, or free text, e.g. "
                                 "\"SKF 6204-2RS bearing specifications\".");
        ImGui::SetNextItemWidth(mx::reFieldW() * 3.0f);
        const bool enter = ImGui::InputText("##re_research_q", s.researchQueryBuf,
                                            sizeof(s.researchQueryBuf),
                                            ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::SameLine(0.0f, mx::reCtlGap());
        const bool canSearch = !s.researchPending && s.researchQueryBuf[0] != '\0';
        const bool clicked = kit::PrimaryButton(
            "Search", canSearch,
            s.researchPending ? "A search is already running." : "Type something to search for.",
            ImVec2(mx::reBtnW(), 0));
        if ((enter || clicked) && canSearch) {
            s.researchQuery = s.researchQueryBuf;
            kickResearch(st);
        }

        if (s.researchPending) kit::ProgressBar(-1.0f, "searching", kit::Level::Busy);
        kit::InlineStatus(s.researchErr.c_str(), kit::Level::Error);
    }
    kit::CardEnd();

    kit::GapCards();

    if (kit::CardBegin("re_src_web_result", "Research result")) {
        if (!g_ui.webRan && s.researchResult.empty()) {
            kit::EmptyState(nullptr, "Nothing searched yet",
                            "Candidate part numbers, catalogue dimensions and the sources they "
                            "came from will be listed here, each tagged with its provenance.");
        } else {
            if (!s.researchResult.empty()) {
                ImGui::TextWrapped("%s", s.researchResult.c_str());
                kit::GapControl();
            }
            if (!g_ui.webDatasheetDir.empty())
                kit::KeyValueRow("datasheet dir", g_ui.webDatasheetDir.c_str());

            if (!g_ui.webMpns.empty()) {
                kit::GapSection();
                kit::SectionHeader("Candidate part numbers");
                for (const std::string& m : g_ui.webMpns) kit::KeyValueRow("MPN", m.c_str());
            }
            if (!g_ui.webDims.empty()) {
                kit::GapSection();
                kit::SectionHeader("Catalogue dimensions");
                for (const auto& d : g_ui.webDims)
                    kit::KeyValueRow(d.first.c_str(), d.second.c_str(), kit::Level::Warn);
                kit::GapControl();
                kit::HonestyTag("low", "catalogue values, not measured off your part");
            }
            if (!g_ui.webSources.empty()) {
                kit::GapSection();
                kit::SectionHeader("Sources");
                for (size_t i = 0; i < g_ui.webSources.size(); ++i) {
                    char key[24];
                    std::snprintf(key, sizeof(key), "source %d", (int)i + 1);
                    kit::KeyValueRow(key, g_ui.webSources[i].c_str());
                }
            }
            if (g_ui.webMpns.empty() && g_ui.webDims.empty() && g_ui.webSources.empty() &&
                s.researchErr.empty()) {
                kit::GapControl();
                kit::InlineStatus("The search completed but returned no structured data. Try a "
                                  "manufacturer part number, or use the Datasheet lane with a PDF.",
                                  kit::Level::Warn);
            }
        }
    }
    kit::CardEnd();
}

// ---------------------------------------------------------------------------------------------
// From-Library lane
// ---------------------------------------------------------------------------------------------

void drawFromLibraryEntry() {
    RevEngState& s = S();
    if (kit::CardBegin("re_src_library", "Seeded from the Parts Library")) {
        ImGui::TextWrapped(
            "This session was created from a Parts Library record. The pipeline routes itself: "
            "the Scan lane if the record ships photos, otherwise the Datasheet or Web lane from "
            "its documents and specs.");
        kit::GapSection();
        if (kit::EmptyState(nullptr, "Waiting for the library seed",
                            "Nothing has arrived from the Parts Library yet. If you would rather "
                            "capture the part yourself, switch to the Scan lane.",
                            "Switch to Scan")) {
            s.sourceMode = SourceMode::Scan;
        }
    }
    kit::CardEnd();
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
        s.sourceFocus = SourceFocus::None;
    }
    // Capture is owned by the visible webcam console, never by a hidden source lane.
    const bool showWebcam = s.sourceMode == SourceMode::Scan && g_ui.captureTab == 0;
    if (g_ui.webcamVisible && !showWebcam) webcamCaptureShutdown();
    g_ui.webcamVisible = showWebcam;

    switch (s.sourceMode) {
        case SourceMode::Scan:        drawScanEntry(st);        break;
        case SourceMode::Datasheet:   drawDatasheetEntry(st);   break;
        case SourceMode::Web:         drawWebEntry(st);         break;
        case SourceMode::FromLibrary: drawFromLibraryEntry();   break;
    }
}

}  // namespace reveng
}  // namespace ui
