// Compact PLM overview — see plm_overview.h.
//
// Self-contained: does not mutate the full PLM hub workspace state (g_plmHub).
// Feeds from existing plm_* RPCs (plm_item_list, plm_history, plm_timeline,
// plm_branch_*) and degrades to a filesystem listing of the project folder when
// those methods are missing or the vault is absent.
#include "plm_overview.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <string>
#include <system_error>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"

#include "feature_tree.h"   // fmRowPill / fmTreeIcon / fmSectionDivider
#include "icons.h"
#include "layout.h"

#include "../core/app_state.h"
#include "../core/async_rpc.h"
#include "../ui_theme.h"

namespace fs = std::filesystem;

namespace ui {
namespace {

// ---- liquid-glass surface (same id as the left manager column) -----------------
static const ui::AdaptiveSurface& plmOvSurf() {
    ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
    ImRect r(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
    return ui::adaptivePanel(ui::panelId("host_left_glass"), r, ui::GlassGroup::FeatureManager);
}

// ---- row shapes ---------------------------------------------------------------
struct AssetRow {
    std::string itemId;
    std::string path;       // project-relative
    std::string kind;       // part | assembly | drawing | ecad | cam | ...
    std::string rev;
    std::string lifecycle;
    bool        dirty = false;
};

struct FeedRow {
    std::string id;         // commit hash or checkpoint id
    std::string when;       // ISO-ish timestamp
    std::string title;      // primary line
    std::string sub;        // secondary line (author · kind)
    std::string source;     // "plm" | "ai"
    std::string itemId;     // optional PLM item filter key
    std::string pathHint;   // optional path for client-side filter
};

// ---- module state -------------------------------------------------------------
struct PlmOverviewState {
    // availability: hide PLM-specific chrome after permanent unknown_method
    bool plmItemsAvail  = true;
    bool plmHistAvail   = true;
    bool aiTimelineAvail = true;
    bool plmBranchAvail  = true;

    bool itemsLoading = false;
    bool histLoading  = false;
    bool aiLoading    = false;
    bool branchLoading = false;

    std::string itemsErr;
    std::string histErr;
    std::string aiErr;

    std::string projectPath;          // last path we fetched against
    std::string selectedPath;         // project-relative path ("" = all)
    std::string selectedItemId;       // PLM item_id when known

    std::vector<AssetRow> assets;
    std::vector<FeedRow>  history;    // plm_history
    std::vector<FeedRow>  aiFeed;     // plm_timeline

    // lightweight design branches (plm_branch_list)
    std::vector<std::string> branches;
    std::string activeBranch;
    char        newBranchName[64] = {};
    bool        showCreateBranch  = false;

    // directory listing fallback (project folder CAD files)
    std::vector<std::string> dirFiles;  // basenames
    bool dirListed = false;

    double lastItemsFetch = 0.0;
    double lastHistFetch  = 0.0;
    double lastAiFetch    = 0.0;
    double lastBranchFetch = 0.0;
    int    histGen        = 0;        // bumped when selection filter changes
};
static PlmOverviewState S;

// ---- helpers ------------------------------------------------------------------
static std::string jstr(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return "";
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

static std::string jactor(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return "";
    if (it->is_object()) return jstr(*it, "display");
    if (it->is_string()) return it->get<std::string>();
    return "";
}

static bool isAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos || e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (!r.result.is_object()) return false;
    if (r.result.contains("code")) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented" ||
            c == "unknown" || c.find("unknown") != std::string::npos)
            return true;
    }
    if (!r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        if (reason == "unknown_method" || reason == "method_not_found" ||
            reason == "not_implemented")
            return true;
    }
    return false;
}

static bool rpcOk(const core::RpcResult& r, std::string& hint, bool* availFlag) {
    if (isAbsent(r)) {
        if (availFlag) *availFlag = false;
        hint = "unavailable";
        return false;
    }
    if (!r.ok) {
        hint = r.err.empty() ? "the service did not respond" : r.err;
        return false;
    }
    if (r.result.is_object() && !r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        // no_vault is a soft degrade — not a permanent method absence
        hint = r.result.value("hint", reason.empty() ? std::string("that could not be done")
                                                     : reason);
        return false;
    }
    return true;
}

static std::string basenameOf(const std::string& p) {
    size_t s = p.find_last_of("/\\");
    return s == std::string::npos ? p : p.substr(s + 1);
}

static std::string projectDisplayName(const std::string& p) {
    if (p.empty()) return "(no project)";
    std::string t = p;
    while (!t.empty() && (t.back() == '/' || t.back() == '\\')) t.pop_back();
    return basenameOf(t);
}

static std::string niceTime(const std::string& iso) {
    if (iso.size() >= 16 && iso[10] == 'T')
        return iso.substr(0, 10) + "  " + iso.substr(11, 5);
    if (iso.size() >= 16) return iso.substr(0, 16);
    return iso;
}

static const char* kindIcon(const std::string& kind) {
    if (kind == "assembly") return "assembly";
    if (kind == "drawing")  return "section_props";
    if (kind == "ecad" || kind == "cam") return "ai_panel";
    if (kind == "part")     return "part";
    return "new_doc";
}

static const char* kindFallback(const std::string& kind) {
    if (kind == "assembly") return "A";
    if (kind == "drawing")  return "D";
    if (kind == "ecad")     return "B";
    if (kind == "cam")      return "M";
    if (kind == "part")     return "P";
    return "F";
}

// Status-dot color: dirty → warn, released → ok, superseded → dim, else secondary.
static ImU32 statusDotColor(const AssetRow& a, const ui::AdaptiveSurface& surf) {
    const auto& pal = ui::palette;
    if (a.dirty)
        return ui::Palette::toU32(pal.statusWarn);
    if (a.lifecycle == "Released")
        return ui::Palette::toU32(pal.statusOk);
    if (a.lifecycle == "Superseded")
        return ImGui::ColorConvertFloat4ToU32(surf.textTertiary);
    return ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
}

static bool isAssetKind(const std::string& kind) {
    return kind == "part" || kind == "assembly" || kind == "drawing" ||
           kind == "ecad" || kind == "cam";
}

static bool isCadExt(const std::string& e) {
    return e == ".step+" || e == ".step" || e == ".stp" || e == ".iges" || e == ".igs" ||
           e == ".stl"   || e == ".3mf"  || e == ".ecad+" || e == ".ecad" ||
           e == ".kicad_sch" || e == ".kicad_pcb";
}

static std::string lowerExt(const fs::path& p) {
    std::string e = p.extension().string();
    std::transform(e.begin(), e.end(), e.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return e;
}

static std::string kindFromExt(const std::string& ext) {
    if (ext == ".ecad+" || ext == ".ecad" || ext == ".kicad_pcb" || ext == ".kicad_sch")
        return "ecad";
    // Heuristic: assemblies often named *asm* — otherwise part.
    return "part";
}

// ---- directory fallback -------------------------------------------------------
static void refreshDirListing(const std::string& projectPath) {
    S.dirFiles.clear();
    S.dirListed = true;
    if (projectPath.empty()) return;
    std::error_code ec;
    fs::path root(projectPath);
    if (!fs::is_directory(root, ec)) return;
    for (auto it = fs::directory_iterator(root, ec); !ec && it != fs::directory_iterator();
         it.increment(ec)) {
        if (!it->is_regular_file(ec)) continue;
        const std::string ext = lowerExt(it->path());
        if (!isCadExt(ext)) continue;
        S.dirFiles.push_back(it->path().filename().string());
    }
    std::sort(S.dirFiles.begin(), S.dirFiles.end(),
              [](const std::string& a, const std::string& b) {
                  return std::lexicographical_compare(
                      a.begin(), a.end(), b.begin(), b.end(),
                      [](char c1, char c2) {
                          return std::tolower((unsigned char)c1) <
                                 std::tolower((unsigned char)c2);
                      });
              });
}

// ---- async fetchers -----------------------------------------------------------
static void fetchItems(AppState& st, bool force) {
    if (!S.plmItemsAvail || S.itemsLoading) return;
    const std::string path = st.plmProjectPath();
    const double now = ImGui::GetTime();
    if (!force && (now - S.lastItemsFetch) < 2.0 && path == S.projectPath && !S.assets.empty())
        return;
    S.itemsLoading = true;
    S.lastItemsFetch = now;
    S.projectPath = path;
    nlohmann::json p;
    p["path"] = path;
    st.rpc.call("plm_item_list", p, [path](const core::RpcResult& r) {
        S.itemsLoading = false;
        std::string hint;
        if (!rpcOk(r, hint, &S.plmItemsAvail)) {
            S.itemsErr = S.plmItemsAvail ? hint : std::string();
            if (!S.plmItemsAvail) S.assets.clear();
            // Soft fail (no vault / empty): keep dir listing path.
            refreshDirListing(path);
            return;
        }
        S.itemsErr.clear();
        S.assets.clear();
        auto it = r.result.find("items");
        if (it != r.result.end() && it->is_array()) {
            for (const auto& row : *it) {
                if (!row.is_object()) continue;
                AssetRow a;
                a.itemId    = jstr(row, "item_id");
                a.path      = jstr(row, "path");
                a.kind      = jstr(row, "kind");
                if (a.kind.empty()) a.kind = jstr(row, "doc_type");
                a.rev       = jstr(row, "rev");
                if (a.rev.empty()) a.rev = jstr(row, "working_rev");
                a.lifecycle = jstr(row, "lifecycle");
                a.dirty     = row.value("dirty", false);
                if (!isAssetKind(a.kind) && a.kind != "requirements" &&
                    a.kind != "deliverable") {
                    // still show unknown tracked files under Assets
                }
                S.assets.push_back(std::move(a));
            }
        }
        // Prefer PLM items; if none, fall back to directory listing.
        if (S.assets.empty()) refreshDirListing(path);
        else                  S.dirListed = false;
    });
}

static void fetchHistory(AppState& st, bool force) {
    if (!S.plmHistAvail || S.histLoading) return;
    const std::string path = st.plmProjectPath();
    const double now = ImGui::GetTime();
    if (!force && (now - S.lastHistFetch) < 1.5 && path == S.projectPath)
        return;
    S.histLoading = true;
    S.lastHistFetch = now;
    const int gen = ++S.histGen;
    nlohmann::json p;
    p["path"] = path;
    p["limit"] = 80;
    p["scope"] = "design";
    if (!S.selectedItemId.empty()) p["item_id"] = S.selectedItemId;
    st.rpc.call("plm_history", p, [gen](const core::RpcResult& r) {
        if (gen != S.histGen) return;  // stale response after filter change
        S.histLoading = false;
        std::string hint;
        if (!rpcOk(r, hint, &S.plmHistAvail)) {
            S.histErr = S.plmHistAvail ? hint : std::string();
            if (!S.plmHistAvail) S.history.clear();
            return;
        }
        S.histErr.clear();
        S.history.clear();
        auto e = r.result.find("entries");
        if (e == r.result.end() || !e->is_array()) return;
        for (const auto& en : *e) {
            if (!en.is_object()) continue;
            FeedRow fr;
            fr.id     = jstr(en, "commit");
            fr.when   = jstr(en, "authored_at");
            fr.source = "plm";
            fr.itemId = jstr(en, "item_id");
            fr.pathHint = jstr(en, "path");
            std::string kind = jstr(en, "kind");
            std::string msg  = jstr(en, "message");
            std::string sum  = jstr(en, "summary");
            if (!msg.empty())       fr.title = msg;
            else if (!sum.empty())  fr.title = sum;
            else if (!kind.empty()) fr.title = kind;
            else                    fr.title = "Save";
            std::string author = jactor(en, "author");
            if (!author.empty() && !kind.empty())
                fr.sub = author + "  ·  " + kind;
            else if (!author.empty())
                fr.sub = author;
            else
                fr.sub = kind;
            S.history.push_back(std::move(fr));
        }
    });
}

static void fetchAiTimeline(AppState& st, bool force) {
    if (!S.aiTimelineAvail || S.aiLoading) return;
    const double now = ImGui::GetTime();
    if (!force && (now - S.lastAiFetch) < 1.5) return;
    S.aiLoading = true;
    S.lastAiFetch = now;

    // Prefer the selected file's open doc when it matches an active tab; else the
    // active document; else global (no doc_id).
    std::string docId;
    if (st.docMirror.hasDoc()) docId = st.docMirror.docId();
    else if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        docId = st.docTabs[st.activeTab].docId;

    nlohmann::json p = nlohmann::json::object();
    if (!docId.empty()) p["doc_id"] = docId;
    // Bind AI-VC store to the active design branch when a project is open.
    const std::string proj = st.plmProjectPath();
    if (!proj.empty()) p["path"] = proj;
    st.rpc.call("plm_timeline", p, [](const core::RpcResult& r) {
        S.aiLoading = false;
        std::string hint;
        if (!rpcOk(r, hint, &S.aiTimelineAvail)) {
            S.aiErr = S.aiTimelineAvail ? hint : std::string();
            if (!S.aiTimelineAvail) S.aiFeed.clear();
            return;
        }
        S.aiErr.clear();
        S.aiFeed.clear();
        auto it = r.result.find("checkpoints");
        if (it == r.result.end() || !it->is_array()) return;
        for (const auto& c : *it) {
            if (!c.is_object()) continue;
            FeedRow fr;
            fr.id     = jstr(c, "id");
            fr.when   = c.value("when", c.value("time", std::string()));
            fr.source = "ai";
            fr.pathHint = jstr(c, "path");
            int turn = -1;
            if (c.contains("turn") && c["turn"].is_number())
                turn = (int)c["turn"].get<double>();
            std::string label = jstr(c, "label");
            if (turn >= 0 && !label.empty()) {
                char buf[192];
                std::snprintf(buf, sizeof(buf), "AI · turn %d · %s", turn, label.c_str());
                fr.title = buf;
            } else if (turn >= 0) {
                char buf[64];
                std::snprintf(buf, sizeof(buf), "AI · turn %d", turn);
                fr.title = buf;
            } else if (!label.empty()) {
                fr.title = std::string("AI · ") + label;
            } else {
                fr.title = "AI checkpoint";
            }
            int feats = 0;
            if (c.contains("features") && c["features"].is_number())
                feats = (int)c["features"].get<double>();
            else if (c.contains("features_count") && c["features_count"].is_number())
                feats = (int)c["features_count"].get<double>();
            if (feats > 0) {
                char buf[48];
                std::snprintf(buf, sizeof(buf), "%d features", feats);
                fr.sub = buf;
            } else {
                fr.sub = "checkpoint";
            }
            S.aiFeed.push_back(std::move(fr));
        }
    });
}

static void fetchBranches(AppState& st, bool force) {
    if (!S.plmBranchAvail || S.branchLoading) return;
    const std::string path = st.plmProjectPath();
    if (path.empty()) return;
    const double now = ImGui::GetTime();
    if (!force && (now - S.lastBranchFetch) < 2.0) return;
    S.branchLoading = true;
    S.lastBranchFetch = now;
    nlohmann::json p;
    p["path"] = path;
    st.rpc.call("plm_branch_list", p, [](const core::RpcResult& r) {
        S.branchLoading = false;
        std::string hint;
        if (!rpcOk(r, hint, &S.plmBranchAvail)) {
            if (!S.plmBranchAvail) {
                S.branches.clear();
                S.activeBranch.clear();
            }
            return;
        }
        S.branches.clear();
        S.activeBranch = jstr(r.result, "active");
        auto it = r.result.find("branches");
        if (it != r.result.end() && it->is_array()) {
            for (const auto& b : *it) {
                if (!b.is_object()) continue;
                std::string name = jstr(b, "name");
                if (name.empty()) continue;
                S.branches.push_back(name);
                if (b.value("current", false)) S.activeBranch = name;
            }
        }
        if (S.activeBranch.empty() && !S.branches.empty())
            S.activeBranch = S.branches.front();
    });
}

static void switchBranch(AppState& st, const std::string& name) {
    if (name.empty() || name == S.activeBranch) return;
    const std::string path = st.plmProjectPath();
    if (path.empty()) return;
    nlohmann::json p;
    p["path"] = path;
    p["name"] = name;
    st.rpc.call("plm_branch_switch", p, [name](const core::RpcResult& r) {
        std::string hint;
        if (!rpcOk(r, hint, &S.plmBranchAvail)) {
            // soft fail — toast so the user sees why
            return;
        }
        S.activeBranch = name;
        S.lastBranchFetch = 0.0;
        S.lastHistFetch = 0.0;
        S.lastAiFetch = 0.0;
        S.lastItemsFetch = 0.0;
    });
}

static void createBranch(AppState& st, const std::string& name) {
    if (name.empty()) return;
    const std::string path = st.plmProjectPath();
    if (path.empty()) return;
    nlohmann::json p;
    p["path"] = path;
    p["name"] = name;
    st.rpc.call("plm_branch_create", p, [name](const core::RpcResult& r) {
        std::string hint;
        if (!rpcOk(r, hint, &S.plmBranchAvail)) return;
        S.branches.push_back(name);
        S.lastBranchFetch = 0.0;
        S.showCreateBranch = false;
        S.newBranchName[0] = '\0';
    });
}

static void mergeBranchIntoActive(AppState& st, const std::string& src) {
    if (src.empty() || S.activeBranch.empty() || src == S.activeBranch) return;
    const std::string path = st.plmProjectPath();
    if (path.empty()) return;
    const std::string dst = S.activeBranch;
    nlohmann::json p;
    p["path"] = path;
    p["src"] = src;
    p["dst"] = dst;
    st.rpc.call("plm_branch_merge", p, [&st, src, dst](const core::RpcResult& r) {
        std::string hint;
        if (!rpcOk(r, hint, &S.plmBranchAvail)) {
            st.toast(ui::ToastLevel::Warn,
                     hint.empty() ? "Could not merge branches" : hint);
            return;
        }
        int n = 0;
        if (r.result.contains("conflict_count") && r.result["conflict_count"].is_number())
            n = (int)r.result["conflict_count"].get<double>();
        else if (r.result.contains("conflicts") && r.result["conflicts"].is_array())
            n = (int)r.result["conflicts"].size();
        char buf[160];
        if (n > 0) {
            std::snprintf(buf, sizeof(buf),
                          "Merged %s into %s — %d conflict%s",
                          src.c_str(), dst.c_str(), n, n == 1 ? "" : "s");
            st.toast(ui::ToastLevel::Warn, buf);
        } else {
            std::snprintf(buf, sizeof(buf), "Merged %s into %s",
                          src.c_str(), dst.c_str());
            st.toast(ui::ToastLevel::Success, buf);
        }
        S.lastBranchFetch = 0.0;
        S.lastHistFetch = 0.0;
        S.lastItemsFetch = 0.0;
        S.lastAiFetch = 0.0;
    });
}


// ---- selection ----------------------------------------------------------------
static void selectAsset(AppState& st, const std::string& path, const std::string& itemId) {
    if (S.selectedPath == path && S.selectedItemId == itemId) {
        // Toggle off → show global feed again.
        S.selectedPath.clear();
        S.selectedItemId.clear();
    } else {
        S.selectedPath   = path;
        S.selectedItemId = itemId;
    }
    S.lastHistFetch = 0.0;  // force history refetch with new filter
    fetchHistory(st, /*force=*/true);
}

// ---- drawing ------------------------------------------------------------------
static void drawProjectHeader(AppState& st) {
    const float Scl = ui::uiScale;
    const auto& surf = plmOvSurf();
    const std::string path = st.plmProjectPath();
    const std::string name = projectDisplayName(path);

    ui::FmRowRect rr = ui::fmRowPill(false);
    ui::fmTreeIcon("body_folder", "P");
    ImGui::SameLine(0, 4.0f * Scl);
    ImGui::AlignTextToFramePadding();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::TextUnformatted(name.c_str());
    ImGui::PopStyleColor();
    ui::fmRowPillEnd(rr, false);
    if (ImGui::IsItemHovered() && !path.empty())
        ImGui::SetTooltip("%s", path.c_str());

    // Branch dropdown — hidden when plm_branch_* RPCs are unavailable.
    if (S.plmBranchAvail && !path.empty()) {
        ImGui::Dummy(ImVec2(0, 2.0f * Scl));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("Branch");
        ImGui::PopStyleColor();
        ImGui::SameLine(0, 6.0f * Scl);

        const std::string preview =
            S.activeBranch.empty() ? std::string("main") : S.activeBranch;
        ImGui::SetNextItemWidth(std::max(80.0f * Scl,
                                         ImGui::GetContentRegionAvail().x * 0.55f));
        if (ImGui::BeginCombo("##plm_ov_branch", preview.c_str())) {
            for (const auto& b : S.branches) {
                const bool sel = (b == S.activeBranch);
                if (ImGui::Selectable(b.c_str(), sel)) {
                    if (!sel) switchBranch(st, b);
                }
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::Separator();
            if (ImGui::Selectable("Create branch\xe2\x80\xa6")) {
                S.showCreateBranch = true;
                S.newBranchName[0] = '\0';
            }
            // Merge another branch into the active one.
            if (!S.activeBranch.empty() && S.branches.size() > 1) {
                if (ImGui::BeginMenu("Merge into current\xe2\x80\xa6")) {
                    for (const auto& b : S.branches) {
                        if (b == S.activeBranch) continue;
                        if (ImGui::MenuItem(b.c_str()))
                            mergeBranchIntoActive(st, b);
                    }
                    ImGui::EndMenu();
                }
            }
            ImGui::EndCombo();
        }

        if (S.showCreateBranch) {
            ImGui::SetNextItemWidth(-1.0f);
            bool enter = ImGui::InputTextWithHint(
                "##plm_ov_new_branch", "new-branch-name",
                S.newBranchName, sizeof(S.newBranchName),
                ImGuiInputTextFlags_EnterReturnsTrue);
            if (enter || ImGui::Button("Create")) {
                std::string n = S.newBranchName;
                while (!n.empty() && (n.back() == ' ' || n.back() == '\t')) n.pop_back();
                size_t i = 0;
                while (i < n.size() && (n[i] == ' ' || n[i] == '\t')) ++i;
                n = n.substr(i);
                if (!n.empty()) createBranch(st, n);
            }
            ImGui::SameLine();
            if (ImGui::Button("Cancel")) {
                S.showCreateBranch = false;
                S.newBranchName[0] = '\0';
            }
        }
    }
}

static void drawStatusDot(ImDrawList* dl, ImVec2 center, ImU32 col, float r) {
    dl->AddCircleFilled(center, r, col);
    // soft ring so the dot reads on frosted glass
    dl->AddCircle(center, r + 0.5f, IM_COL32(0, 0, 0, 40), 0, 1.0f);
}

static void drawAssetRow(AppState& st, const AssetRow& a) {
    const float Scl = ui::uiScale;
    const auto& surf = plmOvSurf();
    const bool sel = (!S.selectedPath.empty() && S.selectedPath == a.path) ||
                     (!S.selectedItemId.empty() && !a.itemId.empty() &&
                      S.selectedItemId == a.itemId);

    ImGui::PushID(a.itemId.empty() ? a.path.c_str() : a.itemId.c_str());
    ui::FmRowRect rr = ui::fmRowPill(sel);
    ui::fmTreeIcon(kindIcon(a.kind), kindFallback(a.kind));
    ImGui::SameLine(0, 4.0f * Scl);

    // Leave room for the status dot on the right.
    const float dotR = 3.5f * Scl;
    const float rightPad = 12.0f * Scl + dotR * 2.0f;
    const float labelW = std::max(1.0f, ImGui::GetContentRegionAvail().x - rightPad);

    std::string label = basenameOf(a.path);
    if (label.empty()) label = a.path.empty() ? a.itemId : a.path;
    if (!a.rev.empty()) label += "  ·  " + a.rev;

    ImGui::PushStyleColor(ImGuiCol_Text, sel ? surf.textPrimary : surf.textSecondary);
    ImGui::SetNextItemWidth(labelW);
    // Selectable for click hit-testing; transparent so the pill shows through.
    ImGui::PushStyleColor(ImGuiCol_Header,        ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(0, 0, 0, 0));
    if (ImGui::Selectable(label.c_str(), sel, ImGuiSelectableFlags_SpanAllColumns,
                          ImVec2(labelW, 0))) {
        selectAsset(st, a.path, a.itemId);
    }
    ImGui::PopStyleColor(4);

    // Status dot at the row's right edge.
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 c(rr.mx.x - 8.0f * Scl - dotR, (rr.mn.y + rr.mx.y) * 0.5f);
    drawStatusDot(dl, c, statusDotColor(a, surf), dotR);
    if (ImGui::IsItemHovered()) {
        std::string tip = a.path;
        if (!a.lifecycle.empty()) tip += "\n" + a.lifecycle;
        if (a.dirty) tip += "  ·  unsaved";
        if (!a.itemId.empty()) tip += "\n" + a.itemId;
        ImGui::SetTooltip("%s", tip.c_str());
    }

    ui::fmRowPillEnd(rr, sel);
    ImGui::PopID();
}

static void drawDirFileRow(AppState& st, const std::string& name) {
    const float Scl = ui::uiScale;
    const auto& surf = plmOvSurf();
    const bool sel = (S.selectedPath == name);

    ImGui::PushID(name.c_str());
    ui::FmRowRect rr = ui::fmRowPill(sel);
    const std::string ext = lowerExt(fs::path(name));
    const std::string kind = kindFromExt(ext);
    ui::fmTreeIcon(kindIcon(kind), kindFallback(kind));
    ImGui::SameLine(0, 4.0f * Scl);

    ImGui::PushStyleColor(ImGuiCol_Text, sel ? surf.textPrimary : surf.textSecondary);
    ImGui::PushStyleColor(ImGuiCol_Header,        ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(0, 0, 0, 0));
    if (ImGui::Selectable(name.c_str(), sel)) {
        // Directory fallback has no item_id — client-side path filter only.
        selectAsset(st, name, /*itemId=*/"");
    }
    ImGui::PopStyleColor(4);
    ui::fmRowPillEnd(rr, sel);
    ImGui::PopID();
}

static void drawFeedRow(const FeedRow& fr, int idx) {
    const float Scl = ui::uiScale;
    const auto& surf = plmOvSurf();
    const auto& pal  = ui::palette;
    ImDrawList* dl   = ImGui::GetWindowDrawList();

    ImGui::PushID(idx);
    const ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float  rowW   = ImGui::GetContentRegionAvail().x;
    const float  padY   = 5.0f * Scl;
    const float  lineH  = ImGui::GetTextLineHeight();
    const float  rowH   = lineH * 2.0f + padY * 2.0f + 2.0f * Scl;
    const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);

    ImGui::InvisibleButton("##feed", ImVec2(std::max(1.0f, rowW), rowH));
    const bool hov = ImGui::IsItemHovered();
    ui::glassRowBackground(dl, rowMin, rowMax, 6.0f * Scl, (idx % 2) == 1, false,
                           hov ? 0.40f : 0.24f);

    // Source accent bar (AI = accent, PLM save = secondary).
    const ImU32 accent = (fr.source == "ai")
        ? ui::Palette::toU32(pal.accent)
        : ImGui::ColorConvertFloat4ToU32(surf.textTertiary);
    const float barW = 2.5f * Scl;
    dl->AddRectFilled(ImVec2(rowMin.x + 3.0f * Scl, rowMin.y + 5.0f * Scl),
                      ImVec2(rowMin.x + 3.0f * Scl + barW, rowMax.y - 5.0f * Scl),
                      accent, 1.0f * Scl);

    const float tx = rowMin.x + 10.0f * Scl;
    const float ty = rowMin.y + padY;
    dl->AddText(ImVec2(tx, ty), ImGui::ColorConvertFloat4ToU32(surf.textPrimary),
                fr.title.c_str());

    std::string sub = fr.sub;
    const std::string when = niceTime(fr.when);
    if (!when.empty()) {
        if (!sub.empty()) sub += "  ·  ";
        sub += when;
    }
    if (!sub.empty())
        dl->AddText(ImVec2(tx, ty + lineH + 1.0f * Scl),
                    ImGui::ColorConvertFloat4ToU32(surf.textTertiary), sub.c_str());

    if (hov && !fr.id.empty())
        ImGui::SetTooltip("%s", fr.id.c_str());

    ImGui::PopID();
}

static bool feedMatchesFilter(const FeedRow& fr) {
    if (S.selectedPath.empty() && S.selectedItemId.empty()) return true;
    if (!S.selectedItemId.empty() && fr.itemId == S.selectedItemId) return true;
    if (!S.selectedPath.empty() && !fr.pathHint.empty()) {
        // Match basename or full relative path.
        if (fr.pathHint == S.selectedPath) return true;
        if (basenameOf(fr.pathHint) == basenameOf(S.selectedPath)) return true;
        if (fr.pathHint.find(S.selectedPath) != std::string::npos) return true;
    }
    // AI feed is doc-scoped server-side; when a file is selected and we have no
    // path hint, still show AI rows (they belong to the active/open doc).
    if (fr.source == "ai" && S.selectedItemId.empty()) return true;
    // When filtering by item_id, server already filtered history; AI rows still
    // show if they lack path (active doc context).
    if (fr.source == "ai" && !S.selectedItemId.empty()) return true;
    // History without item/path when a filter is active: drop (server should have
    // filtered; keep only explicit matches above).
    if (fr.source == "plm") return S.selectedItemId.empty() && S.selectedPath.empty();
    return false;
}

static void drawChangeFeed(AppState& st) {
    const auto& surf = plmOvSurf();
    const float Scl  = ui::uiScale;

    // Section header with optional filter chip.
    if (!S.selectedPath.empty() || !S.selectedItemId.empty()) {
        char hdr[160];
        const std::string leaf = basenameOf(S.selectedPath);
        std::snprintf(hdr, sizeof(hdr), "History · %s",
                      leaf.empty() ? S.selectedItemId.c_str() : leaf.c_str());
        ui::fmSectionDivider(hdr);
        // Clear-filter affordance.
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        if (ImGui::Selectable("Show all files", false)) {
            S.selectedPath.clear();
            S.selectedItemId.clear();
            S.lastHistFetch = 0.0;
            fetchHistory(st, /*force=*/true);
        }
        ImGui::PopStyleColor();
    } else {
        ui::fmSectionDivider("History");
    }

    if (S.histLoading && S.history.empty() && S.aiLoading && S.aiFeed.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted("Loading\xe2\x80\xa6");
        ImGui::PopStyleColor();
        return;
    }

    // Merge + sort by `when` descending (empty when sorts last).
    std::vector<const FeedRow*> rows;
    rows.reserve(S.history.size() + S.aiFeed.size());
    for (const auto& fr : S.history)
        if (feedMatchesFilter(fr)) rows.push_back(&fr);
    for (const auto& fr : S.aiFeed)
        if (feedMatchesFilter(fr)) rows.push_back(&fr);

    std::stable_sort(rows.begin(), rows.end(),
                     [](const FeedRow* a, const FeedRow* b) {
                         // ISO strings sort chronologically; empty → bottom.
                         if (a->when.empty() != b->when.empty())
                             return b->when.empty();  // non-empty first
                         return a->when > b->when;
                     });

    if (rows.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        if (!S.plmHistAvail && !S.aiTimelineAvail) {
            ImGui::TextWrapped("No version history available for this project.");
        } else if (!S.selectedPath.empty() || !S.selectedItemId.empty()) {
            ImGui::TextWrapped("No changes for this file yet.");
        } else {
            ImGui::TextWrapped("No changes yet \xe2\x80\x94 saves and AI edits "
                               "appear here.");
        }
        ImGui::PopStyleColor();
        if (!S.histErr.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  ImVec4(ui::palette.statusErr[0], ui::palette.statusErr[1],
                                         ui::palette.statusErr[2], 1.0f));
            ImGui::TextWrapped("%s", S.histErr.c_str());
            ImGui::PopStyleColor();
        }
        return;
    }

    ImGui::BeginChild("##plm_ov_feed", ImVec2(0, 0), ImGuiChildFlags_None);
    for (int i = 0; i < (int)rows.size(); ++i)
        drawFeedRow(*rows[(size_t)i], i);
    ImGui::Dummy(ImVec2(0, 4.0f * Scl));
    ImGui::EndChild();
}

}  // namespace

void drawPlmOverviewBody(AppState& st) {
    ui::fmRowStripeReset();
    const auto& surf = plmOvSurf();
    const float Scl  = ui::uiScale;

    // Compact vertical rhythm (the FeatureManager convention): tighten ItemSpacing.y only.
    // RAII so it unwinds on any early return.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 4.0f * Scl));
    struct SpacingPop { ~SpacingPop() { ImGui::PopStyleVar(); } } spacingPop;

    // Kick async refreshes (throttled inside).
    const std::string path = st.plmProjectPath();
    if (path != S.projectPath) {
        // Project folder changed — drop caches.
        S.assets.clear();
        S.history.clear();
        S.aiFeed.clear();
        S.branches.clear();
        S.activeBranch.clear();
        S.dirFiles.clear();
        S.dirListed = false;
        S.selectedPath.clear();
        S.selectedItemId.clear();
        S.projectPath = path;
        S.lastItemsFetch = 0.0;
        S.lastHistFetch  = 0.0;
        S.lastAiFetch    = 0.0;
        S.lastBranchFetch = 0.0;
        S.showCreateBranch = false;
        S.newBranchName[0] = '\0';
    }

    fetchItems(st, /*force=*/false);
    if (S.plmHistAvail)  fetchHistory(st, /*force=*/false);
    if (S.aiTimelineAvail) fetchAiTimeline(st, /*force=*/false);
    if (S.plmBranchAvail) fetchBranches(st, /*force=*/false);

    // If PLM items never arrived, ensure a directory listing exists.
    if (S.assets.empty() && !S.itemsLoading && !S.dirListed)
        refreshDirListing(path);

    drawProjectHeader(st);
    ImGui::Dummy(ImVec2(0, 2.0f * Scl));

    // ---- Assets ---------------------------------------------------------------
    ui::fmSectionDivider("Assets");

    if (S.itemsLoading && S.assets.empty() && S.dirFiles.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted("Loading\xe2\x80\xa6");
        ImGui::PopStyleColor();
    } else if (!S.assets.empty()) {
        // Group lightly: parts, assemblies, drawings, boards, other.
        auto drawGroup = [&](const char* label, auto pred) {
            bool any = false;
            for (const auto& a : S.assets) if (pred(a)) { any = true; break; }
            if (!any) return;
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted(label);
            ImGui::PopStyleColor();
            for (const auto& a : S.assets)
                if (pred(a)) drawAssetRow(st, a);
        };
        drawGroup("Parts",       [](const AssetRow& a) { return a.kind == "part"; });
        drawGroup("Assemblies",  [](const AssetRow& a) { return a.kind == "assembly"; });
        drawGroup("Drawings",    [](const AssetRow& a) { return a.kind == "drawing"; });
        drawGroup("Boards",      [](const AssetRow& a) {
            return a.kind == "ecad" || a.kind == "cam";
        });
        drawGroup("Other",       [](const AssetRow& a) {
            return a.kind != "part" && a.kind != "assembly" && a.kind != "drawing" &&
                   a.kind != "ecad" && a.kind != "cam";
        });
    } else if (!S.dirFiles.empty()) {
        // Degrade: plain project directory listing (no PLM vault / empty items).
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted("Project folder");
        ImGui::PopStyleColor();
        for (const auto& name : S.dirFiles)
            drawDirFileRow(st, name);
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("No tracked assets in this project.");
        ImGui::PopStyleColor();
        if (!S.itemsErr.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  ImVec4(ui::palette.statusErr[0], ui::palette.statusErr[1],
                                         ui::palette.statusErr[2], 1.0f));
            ImGui::TextWrapped("%s", S.itemsErr.c_str());
            ImGui::PopStyleColor();
        }
    }

    ImGui::Dummy(ImVec2(0, 4.0f * Scl));

    // ---- Change feed ----------------------------------------------------------
    drawChangeFeed(st);
}

}  // namespace ui
