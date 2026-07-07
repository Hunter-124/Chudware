// Parts Library workspace — full-viewport body for the PartsLibrary title-bar tab.
// Federated catalog browse/search over engine_oracle/parts_hub (fasteners, bearings,
// sections, fittings, hydraulics, ECAD + live vendor catalogs), price/material/source
// filters, thumbnail previews, per-vendor sign-in (headed browser via the worker), API
// provider credentials, and the background scrape/download queue. All RPC calls are
// async (st.rpc) — never blocks the GL thread. Degrades gracefully to empty-state when
// services are not running.
#include "parts_workspace.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <vector>

#include <glad/gl.h>

#include "imgui.h"
#include "stb_image.h"

#include "core/app_state.h"
#include "ui_theme.h"

namespace ui {

// ---- local state ----------------------------------------------------------------
namespace {

struct Category {
    std::string id;
    std::string label;
    std::string parentId;
};

struct PartsItem {
    std::string id;           // part_id, e.g. "fastener:ISO 4762:M8" or "catalog:mcmaster::https://…"
    std::string name;
    std::string vendor;
    std::string partNumber;
    std::string description;
    std::string unit;
    std::string material;
    std::string domain;
    std::string standard;
    std::string source;       // "local" | "live"
    std::string datasheet;
    std::string imageUrl;
    std::string license;
    double      priceUsd   = 0.0;
    bool        downloaded = false;
    bool        placeable  = false;
};

struct VendorEntry {
    std::string id;
    std::string label;
    bool loggedIn        = false;
    bool loginInProgress = false;
    std::string loginError;
    std::string lastLogin;
};

struct ApiProvider {
    std::string id;
    std::string label;
    std::vector<std::string> fields;
    bool configured = false;
    bool editing    = false;
    bool saving     = false;
    char fieldBuf[2][192] = {{0}, {0}};  // supports up to 2 credential fields
};

struct QueueJob {
    std::string id;
    std::string site;
    std::string query;
    std::string status;       // pending | running | done | error | cancelled
    std::string artifactPath;
    std::string error;
    bool importing = false;
};

struct Thumb {
    unsigned int tex = 0;
    int w = 0, h = 0;
};

struct PartsState {
    bool categoriesLoaded   = false;
    bool categoriesLoading  = false;
    std::vector<Category>  categories;
    std::string            selectedCategory;

    char searchBuf[256] = {};
    bool searching      = false;
    bool searchDone     = false;
    std::vector<PartsItem> results;
    std::string            searchErr;
    bool hasLive = false;

    // ---- filters (sent with parts_search) ----
    float priceMin = 0.0f;
    float priceMax = 0.0f;      // 0 = no cap
    char  materialBuf[64] = {};
    char  vendorBuf[64]   = {};
    int   sourceFilter = 0;     // 0=All 1=Local 2=Live
    int   sortMode     = 0;     // 0=Relevance 1=Price up 2=Price down 3=Name

    bool gridView = false;

    // ---- vendors (right pane) ----
    bool vendorsLoaded   = false;
    bool vendorsLoading  = false;
    std::vector<VendorEntry> vendors;
    std::vector<ApiProvider> apiProviders;
    double lastVendorPoll = 0.0;

    // ---- download queue ----
    std::vector<QueueJob> queue;
    bool   queuePolling  = false;
    double lastQueuePoll = 0.0;
    bool   queueActive   = false;  // any pending/running job → keep polling

    // ---- thumbnails ----
    std::map<std::string, Thumb> thumbs;      // part_id → GL texture (loaded)
    std::set<std::string>        thumbBusy;   // requests in flight
    std::set<std::string>        thumbFailed; // don't retry this session

    bool firstFrame = true;
};

static PartsState s_parts;

// ---- helpers -------------------------------------------------------------------

static void partRowPill(bool selected = false, float rows = 1.0f) {
    const float rowH = ImGui::GetFrameHeight() * rows;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    float  w  = ImGui::GetContentRegionAvail().x;
    ImVec2 p1(p0.x + w, p0.y + rowH);
    ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 4.0f,
                           false, selected, selected ? 0.42f : 0.20f);
}

static void sectionHeader(const char* label) {
    const float S = ui::uiScale;
    ImGui::PushFont(nullptr);
    ImGui::TextUnformatted(label);
    ImGui::PopFont();
    ImVec2 p0 = ImGui::GetItemRectMin();
    ImVec2 p1 = ImGui::GetItemRectMax();
    ImGui::GetWindowDrawList()->AddLine(
        ImVec2(p0.x, p1.y + 1.0f * S), ImVec2(p1.x, p1.y + 1.0f * S),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.60f)),
        1.0f * S);
    ImGui::Spacing();
}

static void emptyState(const char* icon, const char* msg, const char* sub = nullptr) {
    const float S = ui::uiScale;
    float avail = ImGui::GetContentRegionAvail().y;
    ImGui::Dummy(ImVec2(0, avail * 0.35f));

    ImGui::PushFont(nullptr);
    float tw = ImGui::CalcTextSize(icon).x;
    float cx = ImGui::GetCursorPosX() + (ImGui::GetContentRegionAvail().x - tw) * 0.5f;
    ImGui::SetCursorPosX(cx);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 0.5f));
    ImGui::TextUnformatted(icon);
    ImGui::PopStyleColor();
    ImGui::PopFont();
    ImGui::Spacing();

    {
        float mw = ImGui::CalcTextSize(msg).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - mw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }
    if (sub) {
        float sw = ImGui::CalcTextSize(sub).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - sw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.8f));
        ImGui::TextUnformatted(sub);
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 8.0f * S));
}

static void spinner(const char* prefix) {
    static const char* kFrames[] = {". . .", "o . .", ". o .", ". . o"};
    int f = (int)(ImGui::GetTime() * 4.0) & 3;
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::Text("%s %s", prefix, kFrames[f]);
    ImGui::PopStyleColor();
}

// Minimal base64 decode for parts_thumbnail image_b64 payloads.
static std::vector<unsigned char> b64decode(const std::string& in) {
    static const int T[256] = { /* built below on first use */ };
    static int table[256];
    static bool init = false;
    if (!init) {
        for (int i = 0; i < 256; ++i) table[i] = -1;
        const char* al = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        for (int i = 0; i < 64; ++i) table[(unsigned char)al[i]] = i;
        init = true;
    }
    (void)T;
    std::vector<unsigned char> out;
    out.reserve(in.size() * 3 / 4);
    int val = 0, bits = -8;
    for (unsigned char c : in) {
        int d = table[c];
        if (d == -1) { if (c == '=') break; continue; }
        val = (val << 6) + d;
        bits += 6;
        if (bits >= 0) {
            out.push_back((unsigned char)((val >> bits) & 0xFF));
            bits -= 8;
        }
    }
    return out;
}

// Runs inside rpc.drain() on the GL thread — safe to create GL textures here.
static void storeThumb(const std::string& id, const std::vector<unsigned char>& bytes) {
    int w = 0, h = 0, n = 0;
    unsigned char* px = stbi_load_from_memory(bytes.data(), (int)bytes.size(), &w, &h, &n, 4);
    if (!px) { s_parts.thumbFailed.insert(id); return; }
    Thumb t;
    t.w = w; t.h = h;
    glGenTextures(1, &t.tex);
    glBindTexture(GL_TEXTURE_2D, t.tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
    stbi_image_free(px);
    s_parts.thumbs[id] = t;
}

static void requestThumb(AppState& st, const PartsItem& item) {
    if (s_parts.thumbs.count(item.id) || s_parts.thumbBusy.count(item.id) ||
        s_parts.thumbFailed.count(item.id))
        return;
    if (item.imageUrl.empty()) { s_parts.thumbFailed.insert(item.id); return; }
    s_parts.thumbBusy.insert(item.id);
    nlohmann::json p;
    p["item_id"]   = item.id;
    p["image_url"] = item.imageUrl;
    st.rpc.call("parts_thumbnail", p, [id = item.id](const core::RpcResult& r) {
        s_parts.thumbBusy.erase(id);
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            s_parts.thumbFailed.insert(id);
            return;
        }
        std::string b64 = r.result.value("image_b64", "");
        if (b64.empty()) { s_parts.thumbFailed.insert(id); return; }
        storeThumb(id, b64decode(b64));
    });
}

// Flat vector glyph per part domain — the offline preview fallback.
static void domainGlyph(ImDrawList* dl, ImVec2 c, float r, const std::string& domain) {
    ImU32 col = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.75f));
    ImU32 dim = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.55f));
    const float th = std::max(1.5f, r * 0.12f);
    if (domain == "fastener") {
        // Bolt: hex head + shank.
        ImVec2 head(c.x, c.y - r * 0.45f);
        dl->AddNgon(head, r * 0.42f, col, 6, th);
        dl->AddLine(ImVec2(c.x, head.y + r * 0.42f), ImVec2(c.x, c.y + r * 0.8f), col, th);
        for (int i = 0; i < 3; ++i) {
            float y = c.y + r * (0.25f + 0.22f * i);
            dl->AddLine(ImVec2(c.x - r * 0.22f, y), ImVec2(c.x + r * 0.22f, y), dim, th * 0.8f);
        }
    } else if (domain == "bearing") {
        dl->AddCircle(c, r * 0.85f, col, 0, th);
        dl->AddCircle(c, r * 0.38f, col, 0, th);
        for (int i = 0; i < 6; ++i) {
            float a = (float)i / 6.0f * 6.2831853f;
            dl->AddCircleFilled(ImVec2(c.x + cosf(a) * r * 0.61f, c.y + sinf(a) * r * 0.61f),
                                r * 0.10f, dim);
        }
    } else if (domain == "section") {
        // I-beam.
        float w = r * 0.8f, hh = r * 0.85f;
        dl->AddLine(ImVec2(c.x - w, c.y - hh), ImVec2(c.x + w, c.y - hh), col, th);
        dl->AddLine(ImVec2(c.x - w, c.y + hh), ImVec2(c.x + w, c.y + hh), col, th);
        dl->AddLine(ImVec2(c.x, c.y - hh), ImVec2(c.x, c.y + hh), col, th);
    } else if (domain == "fitting") {
        // Pipe elbow.
        dl->PathArcTo(ImVec2(c.x - r * 0.5f, c.y + r * 0.5f), r * 0.75f, -1.5708f, 0.0f);
        dl->PathStroke(col, 0, th * 1.6f);
        dl->AddLine(ImVec2(c.x - r * 0.5f, c.y - r * 0.25f), ImVec2(c.x - r * 0.9f, c.y - r * 0.25f), col, th * 1.6f);
        dl->AddLine(ImVec2(c.x + r * 0.25f, c.y + r * 0.5f), ImVec2(c.x + r * 0.25f, c.y + r * 0.9f), col, th * 1.6f);
    } else if (domain == "hydraulic") {
        // Cylinder + rod.
        dl->AddRect(ImVec2(c.x - r * 0.8f, c.y - r * 0.35f), ImVec2(c.x + r * 0.25f, c.y + r * 0.35f), col, 2.0f, 0, th);
        dl->AddLine(ImVec2(c.x + r * 0.25f, c.y), ImVec2(c.x + r * 0.85f, c.y), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.35f, c.y - r * 0.35f), ImVec2(c.x - r * 0.35f, c.y + r * 0.35f), dim, th);
    } else if (domain == "ecad") {
        // IC chip.
        dl->AddRect(ImVec2(c.x - r * 0.5f, c.y - r * 0.5f), ImVec2(c.x + r * 0.5f, c.y + r * 0.5f), col, 2.0f, 0, th);
        for (int i = 0; i < 3; ++i) {
            float y = c.y - r * 0.3f + i * r * 0.3f;
            dl->AddLine(ImVec2(c.x - r * 0.8f, y), ImVec2(c.x - r * 0.5f, y), dim, th);
            dl->AddLine(ImVec2(c.x + r * 0.5f, y), ImVec2(c.x + r * 0.8f, y), dim, th);
        }
    } else if (domain == "catalog") {
        // Shopping tag.
        dl->AddTriangleFilled(ImVec2(c.x - r * 0.6f, c.y - r * 0.6f),
                              ImVec2(c.x + r * 0.1f, c.y - r * 0.6f),
                              ImVec2(c.x - r * 0.6f, c.y + r * 0.1f), dim);
        dl->AddRect(ImVec2(c.x - r * 0.6f, c.y - r * 0.6f), ImVec2(c.x + r * 0.6f, c.y + r * 0.6f), col, 3.0f, 0, th);
    } else {
        // Generic gear-ish circle.
        dl->AddCircle(c, r * 0.6f, col, 0, th);
        dl->AddCircle(c, r * 0.22f, dim, 0, th);
    }
}

// ---- async loaders -------------------------------------------------------------

static void loadCategories(AppState& st) {
    if (s_parts.categoriesLoaded || s_parts.categoriesLoading) return;
    s_parts.categoriesLoading = true;
    st.rpc.call("parts_list_categories", {}, [](const core::RpcResult& r) {
        s_parts.categoriesLoading = false;
        s_parts.categoriesLoaded  = true;
        if (!r.ok || !r.result.is_object()) return;
        if (!r.result.value("ok", false) || !r.result.contains("categories")) return;
        s_parts.categories.clear();
        for (auto& c : r.result["categories"]) {
            Category cat;
            cat.id       = c.value("id",        "");
            cat.label    = c.value("label",     "");
            cat.parentId = c.value("parent_id", "");
            s_parts.categories.push_back(std::move(cat));
        }
    });
}

static void loadVendors(AppState& st, bool force = false) {
    if (s_parts.vendorsLoading) return;
    if (s_parts.vendorsLoaded && !force) return;
    s_parts.vendorsLoading = true;
    st.rpc.call("vendor_status", {}, [](const core::RpcResult& r) {
        s_parts.vendorsLoading = false;
        s_parts.vendorsLoaded  = true;
        s_parts.lastVendorPoll = ImGui::GetTime();
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) return;

        std::vector<VendorEntry> vs;
        if (r.result.contains("sources"))
            for (auto& v : r.result["sources"]) {
                VendorEntry e;
                e.id              = v.value("id", "");
                e.label           = v.value("label", e.id);
                e.loggedIn        = v.value("logged_in", false);
                e.loginInProgress = v.value("login_in_progress", false);
                e.loginError      = v.value("login_error", "");
                e.lastLogin       = v.value("last_login", "");
                vs.push_back(std::move(e));
            }
        s_parts.vendors = std::move(vs);

        // API providers: preserve open editors across refreshes.
        std::map<std::string, ApiProvider> old;
        for (auto& a : s_parts.apiProviders) old[a.id] = a;
        std::vector<ApiProvider> as;
        if (r.result.contains("api_providers"))
            for (auto& v : r.result["api_providers"]) {
                ApiProvider a;
                a.id         = v.value("id", "");
                a.label      = v.value("label", a.id);
                a.configured = v.value("configured", false);
                if (v.contains("fields"))
                    for (auto& f : v["fields"]) a.fields.push_back(f.get<std::string>());
                auto it = old.find(a.id);
                if (it != old.end()) {
                    a.editing = it->second.editing;
                    a.saving  = it->second.saving;
                    std::memcpy(a.fieldBuf, it->second.fieldBuf, sizeof(a.fieldBuf));
                }
                as.push_back(std::move(a));
            }
        s_parts.apiProviders = std::move(as);
    });
}

static void doSearch(AppState& st) {
    if (s_parts.searching) return;
    s_parts.searching  = true;
    s_parts.searchDone = false;
    s_parts.searchErr.clear();
    s_parts.results.clear();
    nlohmann::json p;
    p["query"]    = s_parts.searchBuf;
    p["category"] = s_parts.selectedCategory;
    p["limit"]    = 100;
    if (s_parts.priceMin > 0.0f) p["price_min"] = s_parts.priceMin;
    if (s_parts.priceMax > 0.0f) p["price_max"] = s_parts.priceMax;
    if (s_parts.materialBuf[0])  p["material"]  = s_parts.materialBuf;
    if (s_parts.vendorBuf[0])    p["vendor"]    = s_parts.vendorBuf;
    if (s_parts.sourceFilter == 1) p["source"] = "local";
    if (s_parts.sourceFilter == 2) p["source"] = "live";
    st.rpc.call("parts_search", p, [](const core::RpcResult& r) {
        s_parts.searching  = false;
        s_parts.searchDone = true;
        if (!r.ok) { s_parts.searchErr = r.err; return; }
        if (!r.result.is_object()) return;
        if (!r.result.value("ok", false)) {
            s_parts.searchErr = r.result.value("reason", "search failed");
            return;
        }
        s_parts.hasLive = r.result.value("has_live", false);
        s_parts.results.clear();
        if (!r.result.contains("results")) return;
        for (auto& it : r.result["results"]) {
            PartsItem item;
            item.id          = it.value("id",          "");
            item.name        = it.value("name",        "");
            item.vendor      = it.value("vendor",      "");
            item.partNumber  = it.value("part_number", "");
            item.description = it.value("description", "");
            item.unit        = it.value("unit",        "ea");
            item.material    = it.value("material",    "");
            item.domain      = it.value("domain",      "");
            item.standard    = it.value("standard",    "");
            item.source      = it.value("source",      "local");
            item.datasheet   = it.value("datasheet",   "");
            item.imageUrl    = it.value("image_url",   "");
            item.license     = it.value("license",     "");
            item.priceUsd    = it.value("price_usd",   0.0);
            item.downloaded  = it.value("downloaded",  false);
            item.placeable   = it.value("placeable",   false);
            s_parts.results.push_back(std::move(item));
        }
        // Client-side sort (relevance = backend order).
        auto& v = s_parts.results;
        switch (s_parts.sortMode) {
            case 1: std::stable_sort(v.begin(), v.end(),
                        [](const PartsItem& a, const PartsItem& b) {
                            if ((a.priceUsd > 0) != (b.priceUsd > 0)) return a.priceUsd > 0;
                            return a.priceUsd < b.priceUsd; });
                    break;
            case 2: std::stable_sort(v.begin(), v.end(),
                        [](const PartsItem& a, const PartsItem& b) { return a.priceUsd > b.priceUsd; });
                    break;
            case 3: std::stable_sort(v.begin(), v.end(),
                        [](const PartsItem& a, const PartsItem& b) { return a.name < b.name; });
                    break;
            default: break;
        }
    });
}

static void pollQueue(AppState& st) {
    if (s_parts.queuePolling) return;
    s_parts.queuePolling  = true;
    s_parts.lastQueuePoll = ImGui::GetTime();
    st.rpc.call("parts_queue_status", {}, [](const core::RpcResult& r) {
        s_parts.queuePolling = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) return;
        // Preserve per-job "importing" spinners across refreshes.
        std::set<std::string> importing;
        for (auto& j : s_parts.queue)
            if (j.importing) importing.insert(j.id);
        std::vector<QueueJob> jobs;
        bool active = false;
        if (r.result.contains("jobs"))
            for (auto& j : r.result["jobs"]) {
                QueueJob q;
                q.id           = j.value("id", "");
                q.site         = j.value("site", "");
                q.query        = j.value("query", "");
                q.status       = j.value("status", "");
                q.artifactPath = j.value("artifact_path", "");
                q.error        = j.value("error", "");
                q.importing    = importing.count(q.id) > 0;
                if (q.status == "pending" || q.status == "running") active = true;
                jobs.push_back(std::move(q));
            }
        s_parts.queue       = std::move(jobs);
        s_parts.queueActive = active;
    });
}

static void queueDownload(AppState& st, const PartsItem& item) {
    nlohmann::json p;
    p["item_id"] = item.id;
    p["name"]    = item.name;
    st.rpc.call("parts_download", p, [&st, name = item.name](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok && r.result.value("downloaded", false)) {
            st.toast(ui::ToastLevel::Info, name + ": local library part — use Insert to place it");
            return;
        }
        if (ok) {
            st.toast(ui::ToastLevel::Success, "Queued download: " + name);
            s_parts.queueActive = true;
            pollQueue(st);
        } else {
            std::string why = r.ok ? r.result.value("reason", "download failed") : r.err;
            st.toast(ui::ToastLevel::Error, "Download failed: " + why);
        }
    });
}

static void insertItem(AppState& st, const PartsItem& item) {
    nlohmann::json p;
    p["item_id"] = item.id;
    st.rpc.call("parts_insert_into_doc", p, [&st, name = item.name](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            std::string doc = r.result.value("doc_id", "");
            st.toast(ui::ToastLevel::Success,
                     "Inserted " + name + (doc.empty() ? "" : (" → " + doc)));
        } else {
            std::string why = r.ok
                ? r.result.value("reason", std::string("insert failed"))
                : r.err;
            std::string hint = r.ok ? r.result.value("hint", "") : "";
            st.toast(ui::ToastLevel::Error,
                     "Insert failed: " + why + (hint.empty() ? "" : ("  (" + hint + ")")));
        }
    });
}

static void startVendorLogin(AppState& st, VendorEntry& v) {
    v.loginInProgress = true;
    nlohmann::json p;
    p["vendor"] = v.id;
    st.rpc.call("vendor_login", p, [&st, id = v.id](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            st.toast(ui::ToastLevel::Info,
                     "Browser window opened — sign into " + id + ", then close the window.");
        } else {
            std::string why = r.ok ? r.result.value("reason", "login failed") : r.err;
            st.toast(ui::ToastLevel::Error, "Login start failed: " + why);
            for (auto& vv : s_parts.vendors)
                if (vv.id == id) vv.loginInProgress = false;
        }
    });
}

static void saveApiCreds(AppState& st, ApiProvider& a) {
    nlohmann::json creds;
    for (size_t i = 0; i < a.fields.size() && i < 2; ++i)
        if (a.fieldBuf[i][0]) creds[a.fields[i]] = a.fieldBuf[i];
    if (creds.empty()) {
        st.toast(ui::ToastLevel::Warn, "Enter the credential fields first.");
        return;
    }
    a.saving = true;
    nlohmann::json p;
    p["provider"] = a.id;
    p["creds"]    = creds;
    st.rpc.call("vendor_creds_set", p, [&st, id = a.id](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        for (auto& aa : s_parts.apiProviders) {
            if (aa.id != id) continue;
            aa.saving = false;
            if (ok) {
                aa.editing = false;
                std::memset(aa.fieldBuf, 0, sizeof(aa.fieldBuf));
            }
        }
        if (ok) {
            st.toast(ui::ToastLevel::Success, "Saved credentials for " + id);
            loadVendors(st, /*force=*/true);
        } else {
            std::string why = r.ok ? r.result.value("reason", "save failed") : r.err;
            st.toast(ui::ToastLevel::Error, "Credential save failed: " + why);
        }
    });
}

// ---- sub-panels ----------------------------------------------------------------

static void drawCategoryPane(AppState& st, float paneW) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##parts_cats", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Categories");

    if (s_parts.categoriesLoading) {
        spinner("Loading");
    } else if (s_parts.categories.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextWrapped("No catalog data. Start the parts service or check the network.");
        ImGui::PopStyleColor();
    } else {
        auto pick = [&](const std::string& id) {
            s_parts.selectedCategory = id;
            doSearch(st);  // browse-on-click, even with an empty query
        };
        {
            bool sel = s_parts.selectedCategory.empty();
            partRowPill(sel);
            if (ImGui::Selectable("All Parts", sel,
                    ImGuiSelectableFlags_SpanAllColumns,
                    ImVec2(0, ImGui::GetFrameHeight())))
                pick("");
        }
        ImGui::Spacing();
        for (auto& c : s_parts.categories) {
            if (!c.parentId.empty()) continue;  // top-level only in outer loop
            bool sel = (s_parts.selectedCategory == c.id);
            partRowPill(sel);
            if (ImGui::Selectable(c.label.c_str(), sel,
                    ImGuiSelectableFlags_SpanAllColumns,
                    ImVec2(0, ImGui::GetFrameHeight())))
                pick(sel ? "" : c.id);

            for (auto& ch : s_parts.categories) {
                if (ch.parentId != c.id) continue;
                bool csel = (s_parts.selectedCategory == ch.id);
                ImGui::Indent(14.0f * S);
                partRowPill(csel);
                std::string clbl = "  " + ch.label;
                if (ImGui::Selectable(clbl.c_str(), csel,
                        ImGuiSelectableFlags_SpanAllColumns,
                        ImVec2(0, ImGui::GetFrameHeight())))
                    pick(csel ? "" : ch.id);
                ImGui::Unindent(14.0f * S);
            }
        }
    }

    // ---- Filters --------------------------------------------------------------
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Filters");

    ImGui::TextDisabled("Price (USD)");
    ImGui::SetNextItemWidth((paneW - 40.0f * S) * 0.5f);
    if (ImGui::InputFloat("##pmin", &s_parts.priceMin, 0, 0, "%.2f"))
        s_parts.priceMin = std::max(0.0f, s_parts.priceMin);
    ImGui::SameLine();
    ImGui::TextDisabled("-");
    ImGui::SameLine();
    ImGui::SetNextItemWidth((paneW - 40.0f * S) * 0.5f);
    if (ImGui::InputFloat("##pmax", &s_parts.priceMax, 0, 0, "%.2f"))
        s_parts.priceMax = std::max(0.0f, s_parts.priceMax);

    ImGui::TextDisabled("Material");
    ImGui::SetNextItemWidth(paneW - 22.0f * S);
    ImGui::InputTextWithHint("##fmat", "steel, brass, nylon…",
                             s_parts.materialBuf, sizeof(s_parts.materialBuf));

    ImGui::TextDisabled("Vendor");
    ImGui::SetNextItemWidth(paneW - 22.0f * S);
    ImGui::InputTextWithHint("##fven", "any vendor",
                             s_parts.vendorBuf, sizeof(s_parts.vendorBuf));

    ImGui::TextDisabled("Source");
    ImGui::SetNextItemWidth(paneW - 22.0f * S);
    ImGui::Combo("##fsrc", &s_parts.sourceFilter, "All\0Local library\0Live vendors\0");

    ImGui::Spacing();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.75f));
    if (ImGui::Button("Apply Filters", ImVec2(paneW - 22.0f * S, 0)))
        doSearch(st);
    ImGui::PopStyleColor();
    if (ImGui::SmallButton("Reset")) {
        s_parts.priceMin = s_parts.priceMax = 0.0f;
        s_parts.materialBuf[0] = 0;
        s_parts.vendorBuf[0]   = 0;
        s_parts.sourceFilter   = 0;
        doSearch(st);
    }

    ImGui::EndChild();
}

static void itemActions(AppState& st, const PartsItem& item) {
    const bool isCatalog = item.id.rfind("catalog:", 0) == 0;
    if (isCatalog && !item.downloaded && ImGui::SmallButton("Download"))
        queueDownload(st, item);
    if (isCatalog) ImGui::SameLine();
    if (ImGui::SmallButton("Insert"))
        insertItem(st, item);
}

static void itemTooltip(const PartsItem& item) {
    if (!ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) return;
    ImGui::BeginTooltip();
    ImGui::TextUnformatted(item.name.c_str());
    if (!item.description.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::PushTextWrapPos(360.0f * ui::uiScale);
        ImGui::TextUnformatted(item.description.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }
    if (!item.material.empty()) ImGui::Text("Material: %s", item.material.c_str());
    if (!item.standard.empty()) ImGui::Text("Standard: %s", item.standard.c_str());
    if (!item.license.empty())  ImGui::TextDisabled("License: %s", item.license.c_str());
    if (!item.datasheet.empty()) ImGui::TextDisabled("Datasheet: %s", item.datasheet.c_str());
    ImGui::TextDisabled("%s", item.source == "live" ? "Live vendor result" : "Local library");
    ImGui::EndTooltip();
}

static void drawResultsList(AppState& st) {
    const float S = ui::uiScale;

    if (s_parts.searching) { spinner("Searching"); return; }
    if (!s_parts.searchErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("Error: %s", s_parts.searchErr.c_str());
        ImGui::PopStyleColor();
        return;
    }
    if (s_parts.results.empty()) {
        if (s_parts.searchDone)
            emptyState("(x)", "No results",
                       "Try a different search term, category, or loosen the filters.");
        else
            emptyState("[P]", "Search for parts",
                       "Type in the search bar or pick a category to browse.");
        return;
    }

    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    ImGui::Text("%-36s  %-14s  %-12s  %-12s  %s", "Name", "Vendor", "Part #", "Material", "Price");
    ImGui::PopStyleColor();
    ImGui::Separator();
    ImGui::Spacing();

    for (auto& item : s_parts.results) {
        ImGui::PushID(item.id.c_str());

        partRowPill(false, 1.1f);
        ImGui::BeginGroup();

        if (item.downloaded) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusOk[0], palette.statusOk[1],
                       palette.statusOk[2], 1.0f));
            ImGui::TextUnformatted("[v]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        if (item.source == "live") {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.9f));
            ImGui::TextUnformatted("[live]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        ImGui::Text("%-36.36s", item.name.c_str());
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::Text("  %-14.14s  %-12.12s  %-12.12s",
                    item.vendor.c_str(), item.partNumber.c_str(),
                    item.material.empty() ? "-" : item.material.c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (item.priceUsd > 0.0)
            ImGui::Text("  $%.2f/%s", item.priceUsd, item.unit.c_str());
        else
            ImGui::TextDisabled("  —");
        ImGui::EndGroup();

        if (ImGui::IsItemHovered()) {
            float bx = ImGui::GetContentRegionAvail().x + ImGui::GetCursorPosX()
                       - 170.0f * S;
            ImGui::SameLine();
            ImGui::SetCursorPosX(bx);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], palette.button[3]));
            itemActions(st, item);
            ImGui::PopStyleColor();
        }
        itemTooltip(item);

        ImGui::Spacing();
        ImGui::PopID();
    }
}

static void drawResultsGrid(AppState& st) {
    const float S     = ui::uiScale;
    const float cardW = 168.0f * S;
    const float cardH = 168.0f * S;
    const float imgH  = 88.0f * S;
    float availW      = ImGui::GetContentRegionAvail().x;
    int   cols        = std::max(1, (int)(availW / (cardW + 8.0f * S)));

    if (s_parts.searching) { spinner("Searching"); return; }
    if (s_parts.results.empty()) {
        if (s_parts.searchDone)
            emptyState("(x)", "No results", "Try a different search term or category.");
        else
            emptyState("[P]", "Search for parts", "Type in the search bar or pick a category.");
        return;
    }

    int i = 0;
    for (auto& item : s_parts.results) {
        if (i % cols != 0) ImGui::SameLine();
        ImGui::PushID(item.id.c_str());

        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImVec2 p1(p0.x + cardW, p0.y + cardH);
        bool hov = ImGui::IsMouseHoveringRect(p0, p1);
        ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 8.0f,
                               false, hov, hov ? 0.32f : 0.20f);

        ImGui::BeginGroup();

        // ---- preview: real thumbnail when available, domain glyph otherwise ----
        requestThumb(st, item);
        auto th = s_parts.thumbs.find(item.id);
        if (th != s_parts.thumbs.end() && th->second.tex) {
            // Fit the image into the preview band, centred, preserving aspect.
            float iw = (float)th->second.w, ih = (float)th->second.h;
            float sc = std::min((cardW - 16.0f * S) / iw, imgH / ih);
            float dw = iw * sc, dh = ih * sc;
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (cardW - dw) * 0.5f);
            ImGui::SetCursorPosY(ImGui::GetCursorPosY() + (imgH - dh) * 0.5f);
            ImGui::Image((ImTextureID)(intptr_t)th->second.tex, ImVec2(dw, dh));
            float used = (imgH - dh) * 0.5f + dh;
            if (used < imgH) ImGui::Dummy(ImVec2(1, imgH - used));
        } else {
            ImVec2 c(p0.x + cardW * 0.5f, p0.y + imgH * 0.5f + 4.0f * S);
            domainGlyph(ImGui::GetWindowDrawList(), c, 26.0f * S, item.domain);
            ImGui::Dummy(ImVec2(cardW, imgH));
        }

        auto centre = [&](const char* t) {
            float tw = ImGui::CalcTextSize(t).x;
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (cardW - tw) * 0.5f);
            ImGui::TextUnformatted(t);
        };
        char nbuf[40];
        std::snprintf(nbuf, sizeof(nbuf), "%.34s", item.name.c_str());
        centre(nbuf);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        centre(item.partNumber.c_str());
        ImGui::PopStyleColor();

        char pbuf[48];
        if (item.priceUsd > 0.0)
            std::snprintf(pbuf, sizeof(pbuf), "$%.2f%s", item.priceUsd,
                          item.source == "live" ? "  [live]" : "");
        else
            std::snprintf(pbuf, sizeof(pbuf), "%s",
                          item.source == "live" ? "[live]" : "—");
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        centre(pbuf);
        ImGui::PopStyleColor();

        // Hover action row inside the card.
        if (hov) {
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + 10.0f * S);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], palette.button[3]));
            itemActions(st, item);
            ImGui::PopStyleColor();
        }
        ImGui::EndGroup();
        itemTooltip(item);

        // Keep card cells a fixed height so the flow layout stays aligned.
        ImVec2 cur = ImGui::GetItemRectMax();
        if (cur.y < p1.y) ImGui::Dummy(ImVec2(cardW, p1.y - cur.y));

        ImGui::PopID();
        ++i;
    }
}

static void drawVendorsPane(AppState& st, float paneW) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##parts_vendors", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Vendor Sites");

    bool anyLoginBusy = false;
    for (auto& v : s_parts.vendors)
        if (v.loginInProgress) anyLoginBusy = true;

    if (s_parts.vendorsLoading && s_parts.vendors.empty()) {
        spinner("Checking");
    } else if (s_parts.vendors.empty()) {
        ImGui::TextDisabled("No vendor adapters registered.");
    } else {
        for (auto& v : s_parts.vendors) {
            ImGui::PushID(v.id.c_str());
            partRowPill(false, 2.0f);
            ImGui::BeginGroup();

            ImVec4 dot = v.loggedIn
                ? ImVec4(palette.statusOk[0],  palette.statusOk[1],
                         palette.statusOk[2],  1.0f)
                : ImVec4(palette.statusErr[0], palette.statusErr[1],
                         palette.statusErr[2], 0.85f);
            if (v.loginInProgress)
                dot = ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f);
            ImGui::PushStyleColor(ImGuiCol_Text, dot);
            ImGui::TextUnformatted(v.loggedIn ? "[+]" : (v.loginInProgress ? "[~]" : "[ ]"));
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextUnformatted(v.label.c_str());
            ImGui::SameLine();
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                                 ImGui::GetContentRegionAvail().x - 80.0f * S);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], 0.80f));
            const char* lbl = v.loginInProgress ? "..." : (v.loggedIn ? "Re-login" : "Sign in");
            if (!v.loginInProgress && ImGui::SmallButton(lbl))
                startVendorLogin(st, v);
            ImGui::PopStyleColor();

            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            if (v.loginInProgress)
                ImGui::TextUnformatted("  Waiting for browser sign-in...");
            else if (!v.loginError.empty())
                ImGui::TextUnformatted("  Login failed (hover)");
            else
                ImGui::TextUnformatted(v.loggedIn ? "  Signed in" : "  Not signed in");
            ImGui::PopStyleColor();
            if (!v.loginError.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", v.loginError.c_str());

            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }

    // While a headed login window is open, poll status so the dot flips green
    // the moment the user closes the window. Edge-triggered + backed off (2 s).
    if (anyLoginBusy && !s_parts.vendorsLoading &&
        ImGui::GetTime() - s_parts.lastVendorPoll > 2.0)
        loadVendors(st, /*force=*/true);

    // ---- API pricing providers --------------------------------------------------
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("API Providers");

    if (s_parts.apiProviders.empty()) {
        ImGui::TextDisabled("No API providers available.");
    } else {
        for (auto& a : s_parts.apiProviders) {
            ImGui::PushID(a.id.c_str());
            partRowPill(false, a.editing ? (2.2f + (float)a.fields.size()) : 1.6f);
            ImGui::BeginGroup();

            ImVec4 dot = a.configured
                ? ImVec4(palette.statusOk[0],  palette.statusOk[1],
                         palette.statusOk[2],  1.0f)
                : ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                         palette.textDisabled[2], 0.9f);
            ImGui::PushStyleColor(ImGuiCol_Text, dot);
            ImGui::TextUnformatted(a.configured ? "[+]" : "[ ]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextUnformatted(a.label.c_str());
            ImGui::SameLine();
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                                 ImGui::GetContentRegionAvail().x - 80.0f * S);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], 0.80f));
            if (ImGui::SmallButton(a.editing ? "Close" : (a.configured ? "Edit" : "Set up")))
                a.editing = !a.editing;
            ImGui::PopStyleColor();

            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::TextUnformatted(a.configured ? "  Configured" : "  Not configured");
            ImGui::PopStyleColor();

            if (a.editing) {
                for (size_t i = 0; i < a.fields.size() && i < 2; ++i) {
                    ImGui::SetNextItemWidth(paneW - 24.0f * S);
                    ImGui::InputTextWithHint(
                        (std::string("##cred_") + a.fields[i]).c_str(),
                        a.fields[i].c_str(), a.fieldBuf[i], sizeof(a.fieldBuf[i]),
                        ImGuiInputTextFlags_Password);
                }
                if (a.saving) {
                    spinner("Saving");
                } else if (ImGui::SmallButton("Save")) {
                    saveApiCreds(st, a);
                }
            }

            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }

    // ---- Download queue ----------------------------------------------------------
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Download Queue");

    // Poll while active jobs exist (backed off; async — never blocks the frame).
    if (s_parts.queueActive && ImGui::GetTime() - s_parts.lastQueuePoll > 2.5)
        pollQueue(st);

    if (s_parts.queue.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("No downloads pending.");
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
    } else {
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
        ImGui::Spacing();
        for (auto& q : s_parts.queue) {
            ImGui::PushID(q.id.c_str());
            partRowPill(false, 1.8f);
            ImGui::BeginGroup();
            std::string label = q.query;
            if (label.size() > 34) label = label.substr(0, 31) + "...";
            ImGui::Text("%-34.34s", label.c_str());
            ImGui::SameLine();
            ImGui::TextDisabled("%s", q.site.c_str());
            if (q.status == "done") {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusOk[0], palette.statusOk[1],
                           palette.statusOk[2], 1.0f));
                ImGui::TextUnformatted("  Done");
                ImGui::PopStyleColor();
                if (!q.artifactPath.empty()) {
                    ImGui::SameLine();
                    if (q.importing) {
                        spinner("Importing");
                    } else if (ImGui::SmallButton("Import into doc")) {
                        q.importing = true;
                        nlohmann::json p;
                        p["artifact_path"] = q.artifactPath;
                        st.rpc.call("parts_import_artifact", p,
                            [&st, id = q.id](const core::RpcResult& r) {
                                for (auto& j : s_parts.queue)
                                    if (j.id == id) j.importing = false;
                                bool ok = r.ok && r.result.is_object() &&
                                          r.result.value("ok", false);
                                if (ok) {
                                    st.toast(ui::ToastLevel::Success,
                                        "Imported → " + r.result.value("doc_id", std::string("doc")));
                                } else {
                                    std::string why = r.ok
                                        ? r.result.value("reason", std::string("import failed"))
                                        : r.err;
                                    st.toast(ui::ToastLevel::Error, "Import failed: " + why);
                                }
                            });
                    }
                }
            } else if (q.status == "error") {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusErr[0], palette.statusErr[1],
                           palette.statusErr[2], 1.0f));
                ImGui::TextUnformatted("  Failed");
                ImGui::PopStyleColor();
                if (!q.error.empty() && ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", q.error.c_str());
            } else if (q.status == "cancelled") {
                ImGui::TextDisabled("  Cancelled");
            } else {
                float ph = (float)(0.5 + 0.5 * sin(ImGui::GetTime() * 2.5));
                ImGui::PushStyleColor(ImGuiCol_PlotHistogram,
                    ImVec4(palette.accent[0], palette.accent[1],
                           palette.accent[2], 0.80f));
                ImGui::ProgressBar(ph, ImVec2(-1.0f, 4.0f * S),
                                   q.status == "running" ? "downloading" : "queued");
                ImGui::PopStyleColor();
            }
            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }

    ImGui::EndChild();
}

} // anonymous namespace

// ---- public entry point -------------------------------------------------------

void drawPartsLibraryWorkspace(AppState& st) {
    const float S = ui::uiScale;

    if (s_parts.firstFrame) {
        s_parts.firstFrame = false;
        s_parts.gridView = st.settings.partsGridView;
        if (st.workerStarted) {
            loadCategories(st);
            loadVendors(st);
            pollQueue(st);
        }
    }
    // Re-try if worker came up after first frame.
    if (st.workerStarted) {
        if (!s_parts.categoriesLoaded && !s_parts.categoriesLoading) loadCategories(st);
        if (!s_parts.vendorsLoaded   && !s_parts.vendorsLoading)    loadVendors(st);
    }

    if (!st.workerStarted) {
        emptyState("[P]", "Parts Library unavailable",
                   "The AI-CAD worker service is not running.");
        return;
    }

    // Layout: [categories+filters 210px] | [results flex] | [vendors+queue 250px]
    float totalW = ImGui::GetContentRegionAvail().x;
    const float catW  = 210.0f * S;
    const float vendW = 250.0f * S;
    const float midW  = totalW - catW - vendW - 12.0f * S;

    // ---- search bar (above results column only) --------------------------------
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8 * S, 5 * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,  ImVec2(6 * S, 6 * S));
    ImGui::Dummy(ImVec2(catW + 6.0f * S, 0.0f));  // skip category pane width
    ImGui::SameLine();
    ImGui::SetNextItemWidth(midW - 250.0f * S);
    bool hit = ImGui::InputTextWithHint("##parts_q",
                                "search fasteners, bearings, sections, electronics, vendors…",
                                s_parts.searchBuf, sizeof(s_parts.searchBuf),
                                ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
    bool go = ImGui::Button("Search");
    ImGui::PopStyleColor(2);
    ImGui::SameLine();
    ImGui::SetNextItemWidth(90.0f * S);
    if (ImGui::Combo("##sort", &s_parts.sortMode,
                     "Relevance\0Price v\0Price ^\0Name\0"))
        doSearch(st);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.80f));
    if (ImGui::Button(s_parts.gridView ? "List" : "Grid")) {
        s_parts.gridView = !s_parts.gridView;
        st.settings.partsGridView = s_parts.gridView;
        saveSettings(st.settings);
    }
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(2);

    if ((hit || go) && !s_parts.searching)
        doSearch(st);

    ImGui::Spacing();

    // ---- three-column body ----------------------------------------------------
    drawCategoryPane(st, catW);
    ImGui::SameLine();

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##parts_results", ImVec2(midW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    {
        char hdr[96];
        if (!s_parts.results.empty())
            std::snprintf(hdr, sizeof(hdr), "Parts  (%d%s)", (int)s_parts.results.size(),
                          s_parts.hasLive ? ", incl. live vendor results" : "");
        else
            std::snprintf(hdr, sizeof(hdr), "Parts");
        sectionHeader(hdr);
    }
    if (s_parts.gridView) drawResultsGrid(st);
    else                  drawResultsList(st);
    ImGui::EndChild();

    ImGui::SameLine();
    drawVendorsPane(st, vendW);
}

}  // namespace ui
