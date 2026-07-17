// PLM Project Hub — Graph tab body (contracts/cad_graph.md).
//
// Draws the project node/edge web from cad_graph_get (project_dir = active
// project). Force-ish static layout (layered rings by kind, deterministic),
// pan/zoom, hover tooltip, click → side panel + blast radius, search filter,
// refresh + auto-refresh on OS app focus regain. Degrades: unknown_method
// permanently hides the tab via plmGraphAvailable().
#include "graph_tab.h"

#include "plm_hub.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstring>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/icons.h"
#include "ui_theme.h"

namespace ui {
namespace {

// ---- row shapes --------------------------------------------------------------

struct GNode {
    std::string id, kind, label, path, docId, origin;
    std::string metaSummary;   // short key=val dump for tooltip/panel
    float lx = 0.0f, ly = 0.0f;  // layout coords (graph space)
    float radius = 14.0f;
};

struct GEdge {
    std::string src, dst, kind, origin;
    std::string metaSummary;   // short key=val dump for label/panel
    std::string dimLabel;      // compact value label for dimensional edges
    nlohmann::json meta;       // full meta for the side panel
};

struct GraphTabState {
    bool available = true;     // false after permanent unknown_method
    bool loading = false;
    bool blastLoading = false;
    bool layoutDirty = true;
    bool centerPending = false;
    std::string projectDir;
    std::string err;
    std::string generatedAt;
    std::vector<GNode> nodes;
    std::vector<GEdge> edges;
    std::unordered_set<std::string> blast;  // highlighted node ids
    std::string selectedId;
    std::string hoverId;
    char search[128] = {};
    float panX = 0.0f, panY = 0.0f, zoom = 1.0f;
    bool wasAppFocusLost = false;
    double lastFetch = -1e9;
    int layoutToken = 0;  // bumps when nodes/edges replace → re-layout
    // edge kind filter — hidden kinds skip drawing (all visible by default)
    std::unordered_set<std::string> hiddenKinds;
    // edge selection (when no node is under the cursor)
    int selectedEdge = -1;
    // link authoring
    std::string linkArmSrc;   // node id armed as the link source
    bool linkPopupOpen = false;
    std::string linkPopupDst; // node id chosen as the target
    int linkKindIdx = 4;      // default REFERENCES (see kEdgeKinds)
    char linkNote[192] = {};
    std::string focusPending; // plmGraphFocus request to apply on next draw
};

GraphTabState& S() {
    static GraphTabState s;
    return s;
}

// ---- helpers -----------------------------------------------------------------

bool isAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos ||
            e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos ||
            e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (!r.result.is_object()) return false;
    if (r.result.contains("code")) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" ||
            c == "not_implemented" || c == "unknown" ||
            c.find("unknown") != std::string::npos)
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

std::string jstr(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return "";
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

std::string summarizeMeta(const nlohmann::json& meta) {
    if (!meta.is_object() || meta.empty()) return "";
    std::string out;
    int n = 0;
    for (auto it = meta.begin(); it != meta.end() && n < 6; ++it, ++n) {
        if (!out.empty()) out += " · ";
        out += it.key();
        out += "=";
        if (it.value().is_string()) out += it.value().get<std::string>();
        else if (it.value().is_number_integer()) out += std::to_string(it.value().get<long long>());
        else if (it.value().is_number_float()) {
            char b[32];
            std::snprintf(b, sizeof(b), "%.4g", it.value().get<double>());
            out += b;
        } else if (it.value().is_boolean()) out += it.value().get<bool>() ? "true" : "false";
        else out += it.value().dump();
    }
    if ((int)meta.size() > n) out += " …";
    return out;
}

// Kind → layout ring (0 = centre). Deterministic order from contracts/cad_graph.md.
int kindRing(const std::string& k) {
    if (k == "project") return 0;
    if (k == "assembly" || k == "doc") return 1;
    if (k == "part") return 2;
    if (k == "body" || k == "bom_item") return 3;
    if (k == "feature" || k == "sketch" || k == "mate" || k == "equation") return 4;
    if (k == "drawing" || k == "sim_study" || k == "board" || k == "firmware") return 5;
    return 3;
}

// Theme-token colours only (gui_lint theme rule).
ImU32 kindColor(const std::string& k, float a = 1.0f) {
    const float* c = palette.accent;
    if (k == "project")                              c = palette.accent;
    else if (k == "assembly" || k == "doc")          c = palette.axisZ;
    else if (k == "part" || k == "body")             c = palette.axisY;
    else if (k == "feature" || k == "sketch")        c = palette.sketchUnder;
    else if (k == "equation")                        c = palette.statusOk;
    else if (k == "mate")                            c = palette.axisX;
    else if (k == "bom_item")                        c = palette.statusWarn;
    else if (k == "drawing")                         c = palette.diffHunk;
    else if (k == "sim_study")                       c = palette.simMesh;
    else if (k == "board" || k == "firmware")        c = palette.sketchExternal;
    else                                             c = palette.textDim;
    return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
}

ImU32 edgeColor(const std::string& k, float a = 0.70f) {
    const float* c = palette.textDim;
    if (k == "CONTAINS")      c = palette.textDim;
    else if (k == "DEPENDS_ON") c = palette.statusWarn;
    else if (k == "MATES")      c = palette.axisX;
    else if (k == "INSTANCE_OF") c = palette.axisY;
    else if (k == "REFERENCES")  c = palette.diffHunk;
    else if (k == "DERIVES_FROM") c = palette.sketchExternal;
    else if (k == "DOCUMENTS")   c = palette.accent;
    else if (k == "SIMULATES")   c = palette.simMesh;
    else if (k == "MANUFACTURES") c = palette.statusOk;
    return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
}

const char* kindShort(const std::string& k) {
    if (k == "project") return "PROJ";
    if (k == "assembly") return "ASM";
    if (k == "doc") return "DOC";
    if (k == "part") return "PART";
    if (k == "body") return "BODY";
    if (k == "feature") return "FEAT";
    if (k == "sketch") return "SK";
    if (k == "mate") return "MATE";
    if (k == "bom_item") return "BOM";
    if (k == "drawing") return "DWG";
    if (k == "sim_study") return "SIM";
    if (k == "board") return "PCB";
    if (k == "firmware") return "FW";
    if (k == "equation") return "EQ";
    return "NODE";
}

// Canonical edge-kind vocabulary (contracts/cad_graph.md) — filter chips + the
// link-authoring combo iterate this. Index 4 == REFERENCES (linkKindIdx default).
static const char* kEdgeKinds[] = {
    "CONTAINS", "DEPENDS_ON", "MATES", "INSTANCE_OF", "REFERENCES",
    "DERIVES_FROM", "DOCUMENTS", "SIMULATES", "MANUFACTURES",
};

// Compact value label for a dimensional edge, e.g. "width=12mm" or "25mm".
std::string edgeDimLabel(const nlohmann::json& meta) {
    if (!meta.is_object()) return "";
    std::string dim;
    if (auto it = meta.find("dim"); it != meta.end() && it->is_string())
        dim = it->get<std::string>();
    std::string val;
    if (auto it = meta.find("value"); it != meta.end() && !it->is_null()) {
        if (it->is_number_integer()) val = std::to_string(it->get<long long>());
        else if (it->is_number_float()) {
            char b[32];
            std::snprintf(b, sizeof(b), "%.4g", it->get<double>());
            val = b;
        } else if (it->is_string()) val = it->get<std::string>();
    }
    std::string unit;
    if (auto it = meta.find("unit"); it != meta.end() && it->is_string())
        unit = it->get<std::string>();
    if (val.empty() && dim.empty()) return "";
    std::string out;
    if (!dim.empty()) {
        // trim a "fid." prefix so the label reads as the dimension name
        auto dot = dim.rfind('.');
        out = (dot == std::string::npos) ? dim : dim.substr(dot + 1);
    }
    if (!val.empty()) {
        if (!out.empty()) out += "=";
        out += val;
        out += unit;
    }
    return out;
}

// Deterministic layered-ring layout. Same input → same positions.
void computeLayout(GraphTabState& G) {
    const float S = ui::uiScale;
    if (G.nodes.empty()) {
        G.layoutDirty = false;
        return;
    }

    // Stable order by id so layout is deterministic across refreshes.
    std::vector<int> order(G.nodes.size());
    for (int i = 0; i < (int)order.size(); ++i) order[i] = i;
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        return G.nodes[a].id < G.nodes[b].id;
    });

    // Bucket by ring.
    std::vector<std::vector<int>> rings(6);
    for (int i : order) {
        int r = kindRing(G.nodes[i].kind);
        if (r < 0) r = 0;
        if (r > 5) r = 5;
        rings[r].push_back(i);
    }

    const float baseR = 90.0f * S;
    const float ringGap = 110.0f * S;
    const float nodeR = 16.0f * S;

    for (int ring = 0; ring < 6; ++ring) {
        auto& bucket = rings[ring];
        if (bucket.empty()) continue;
        if (ring == 0) {
            // Centre cluster: small grid if multiple project-level nodes.
            int n = (int)bucket.size();
            int cols = (int)std::ceil(std::sqrt((float)n));
            float cell = 48.0f * S;
            for (int i = 0; i < n; ++i) {
                int row = i / cols, col = i % cols;
                float ox = (col - (cols - 1) * 0.5f) * cell;
                float oy = (row - ((n + cols - 1) / cols - 1) * 0.5f) * cell;
                G.nodes[bucket[i]].lx = ox;
                G.nodes[bucket[i]].ly = oy;
                G.nodes[bucket[i]].radius = nodeR * 1.15f;
            }
            continue;
        }
        const float rad = baseR + (ring - 1) * ringGap;
        const int n = (int)bucket.size();
        // Evenly spaced; start angle hash from first id for stability, not random.
        float start = 0.0f;
        if (!bucket.empty()) {
            // FNV-ish hash of first id → angle offset
            unsigned h = 2166136261u;
            for (unsigned char c : G.nodes[bucket[0]].id) {
                h ^= c; h *= 16777619u;
            }
            start = (float)(h % 360) * (3.14159265f / 180.0f);
        }
        for (int i = 0; i < n; ++i) {
            float a = start + (float)i * (2.0f * 3.14159265f / (float)std::max(1, n));
            G.nodes[bucket[i]].lx = std::cos(a) * rad;
            G.nodes[bucket[i]].ly = std::sin(a) * rad;
            G.nodes[bucket[i]].radius = nodeR;
        }
    }
    G.layoutDirty = false;
}

void drawDashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float thick,
                    float dash = 6.0f, float gap = 4.0f) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float len = std::sqrt(dx * dx + dy * dy);
    if (len < 0.5f) return;
    float ux = dx / len, uy = dy / len;
    float t = 0.0f;
    bool on = true;
    while (t < len) {
        float seg = on ? dash : gap;
        float t1 = std::min(t + seg, len);
        if (on) {
            ImVec2 p0(a.x + ux * t, a.y + uy * t);
            ImVec2 p1(a.x + ux * t1, a.y + uy * t1);
            dl->AddLine(p0, p1, col, thick);
        }
        t = t1;
        on = !on;
    }
}

// ---- fetch -------------------------------------------------------------------

void requestGraph(AppState& st, bool force) {
    auto& G = S();
    if (!G.available) return;
    if (G.loading) return;
    const std::string proj = st.plmProjectPath();
    if (proj.empty()) return;
    const double now = ImGui::GetTime();
    if (!force && (now - G.lastFetch) < 1.5 && proj == G.projectDir && !G.nodes.empty())
        return;

    G.loading = true;
    G.err.clear();
    G.projectDir = proj;
    G.lastFetch = now;

    nlohmann::json p;
    p["project_dir"] = proj;
    // Also accept path-style callers; harmless extra key for robust workers.
    p["path"] = proj;

    st.rpc.call("cad_graph_get", p, [](const core::RpcResult& r) {
        auto& G = S();
        G.loading = false;
        if (isAbsent(r)) {
            G.available = false;
            G.nodes.clear();
            G.edges.clear();
            G.err.clear();
            return;
        }
        if (!r.ok) {
            G.err = r.err.empty() ? "the service did not respond" : r.err;
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            G.err = r.result.value("hint", r.result.value("reason",
                        std::string("that could not be done")));
            return;
        }

        G.nodes.clear();
        G.edges.clear();
        G.blast.clear();
        G.generatedAt = jstr(r.result, "generated_at");

        if (auto it = r.result.find("nodes"); it != r.result.end() && it->is_array()) {
            G.nodes.reserve(it->size());
            for (const auto& jn : *it) {
                if (!jn.is_object()) continue;
                GNode n;
                n.id     = jstr(jn, "id");
                n.kind   = jstr(jn, "kind");
                n.label  = jstr(jn, "label");
                n.path   = jstr(jn, "path");
                n.docId  = jstr(jn, "doc_id");
                n.origin = jstr(jn, "origin");
                if (n.origin.empty()) n.origin = "extracted";
                if (auto m = jn.find("meta"); m != jn.end() && m->is_object())
                    n.metaSummary = summarizeMeta(*m);
                if (n.id.empty()) continue;
                if (n.label.empty()) n.label = n.id;
                G.nodes.push_back(std::move(n));
            }
        }
        if (auto it = r.result.find("edges"); it != r.result.end() && it->is_array()) {
            G.edges.reserve(it->size());
            for (const auto& je : *it) {
                if (!je.is_object()) continue;
                GEdge e;
                e.src    = jstr(je, "src");
                e.dst    = jstr(je, "dst");
                e.kind   = jstr(je, "kind");
                e.origin = jstr(je, "origin");
                if (e.origin.empty()) e.origin = "extracted";
                if (auto m = je.find("meta"); m != je.end() && m->is_object()) {
                    e.meta = *m;
                    e.metaSummary = summarizeMeta(*m);
                    if (e.kind == "DERIVES_FROM" || e.kind == "REFERENCES" ||
                        e.kind == "MATES")
                        e.dimLabel = edgeDimLabel(*m);
                }
                if (e.src.empty() || e.dst.empty()) continue;
                G.edges.push_back(std::move(e));
            }
        }
        G.layoutDirty = true;
        G.layoutToken++;
        G.selectedEdge = -1;  // edge indices are invalidated by the reload
        // Keep selection if it still exists.
        if (!G.selectedId.empty()) {
            bool found = false;
            for (const auto& n : G.nodes)
                if (n.id == G.selectedId) { found = true; break; }
            if (!found) { G.selectedId.clear(); G.blast.clear(); }
        }
    });
}

void requestBlast(AppState& st, const std::string& nodeId) {
    auto& G = S();
    if (!G.available || nodeId.empty() || G.blastLoading) return;
    G.blastLoading = true;
    G.blast.clear();
    nlohmann::json p;
    p["node_id"] = nodeId;
    p["project_dir"] = st.plmProjectPath();
    p["path"] = st.plmProjectPath();
    st.rpc.call("cad_graph_blast", p, [nodeId](const core::RpcResult& r) {
        auto& G = S();
        G.blastLoading = false;
        if (isAbsent(r)) {
            G.available = false;
            return;
        }
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            // Soft fail — leave previous highlight empty, keep selection.
            return;
        }
        G.blast.clear();
        G.blast.insert(nodeId);
        if (auto it = r.result.find("affected"); it != r.result.end() && it->is_array()) {
            for (const auto& a : *it) {
                if (a.is_string()) G.blast.insert(a.get<std::string>());
            }
        }
    });
}

void requestLinkAdd(AppState& st, const std::string& src, const std::string& dst,
                    const std::string& kind, const std::string& note) {
    auto& G = S();
    if (!G.available || src.empty() || dst.empty()) return;
    nlohmann::json p;
    p["project_dir"] = st.plmProjectPath();
    p["path"] = st.plmProjectPath();
    p["src"] = src;
    p["dst"] = dst;
    p["kind"] = kind;
    p["note"] = note;
    p["origin"] = "manual";
    st.rpc.call("cad_graph_link_add", p, [](const core::RpcResult& r) {
        if (isAbsent(r)) { S().available = false; return; }
        // Rebuild on the next frame so the new edge appears.
        S().lastFetch = -1e9;
    });
}

void requestLinkRemove(AppState& st, const std::string& linkId) {
    auto& G = S();
    if (!G.available || linkId.empty()) return;
    nlohmann::json p;
    p["project_dir"] = st.plmProjectPath();
    p["path"] = st.plmProjectPath();
    p["link_id"] = linkId;
    st.rpc.call("cad_graph_link_remove", p, [](const core::RpcResult& r) {
        if (isAbsent(r)) { S().available = false; return; }
        S().selectedEdge = -1;
        S().lastFetch = -1e9;
    });
}

// Local glass action button (mirrors plmGlassButton without coupling to pages TU).
bool graphGlassButton(const char* id, const char* label, bool accent = false,
                      float widthOverride = 0.0f) {
    const float S = ui::uiScale;
    const float padX = 12 * S;
    const float textW = (label && label[0]) ? ImGui::CalcTextSize(label).x : 0.0f;
    const float w = widthOverride > 0 ? widthOverride : (padX * 2 + textW);
    const float h = ImGui::GetFrameHeight();
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + w, p0.y + h);
    bool clicked = ImGui::InvisibleButton(id, ImVec2(w, h));
    bool hov = ImGui::IsItemHovered(), held = ImGui::IsItemActive();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassBackground(dl, p0, p1, 9.0f);
    if (accent) {
        float a = held ? 0.92f : hov ? 0.80f : 0.66f;
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.accent[0], palette.accent[1], palette.accent[2], a)), 9.0f * S);
    } else if (hov) {
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.text[0], palette.text[1], palette.text[2], held ? 0.10f : 0.06f)),
            9.0f * S);
    }
    ImU32 tcol = accent ? IM_COL32(255, 255, 255, 255) : Palette::toU32(palette.text);
    if (textW > 0)
        dl->AddText(ImVec2(p0.x + (w - textW) * 0.5f,
                           p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f), tcol, label);
    return clicked;
}

void fitView(GraphTabState& G, ImVec2 canvasSz) {
    if (G.nodes.empty() || canvasSz.x < 1.0f || canvasSz.y < 1.0f) return;
    float minX = 1e9f, minY = 1e9f, maxX = -1e9f, maxY = -1e9f;
    for (const auto& n : G.nodes) {
        minX = std::min(minX, n.lx - n.radius);
        minY = std::min(minY, n.ly - n.radius);
        maxX = std::max(maxX, n.lx + n.radius);
        maxY = std::max(maxY, n.ly + n.radius);
    }
    float gw = std::max(40.0f, maxX - minX);
    float gh = std::max(40.0f, maxY - minY);
    float pad = 40.0f * ui::uiScale;
    float zx = (canvasSz.x - pad * 2) / gw;
    float zy = (canvasSz.y - pad * 2) / gh;
    G.zoom = std::clamp(std::min(zx, zy), 0.15f, 4.0f);
    float cx = (minX + maxX) * 0.5f;
    float cy = (minY + maxY) * 0.5f;
    G.panX = canvasSz.x * 0.5f - cx * G.zoom;
    G.panY = canvasSz.y * 0.5f - cy * G.zoom;
}

void centerOnNode(GraphTabState& G, const GNode& n, ImVec2 canvasSz) {
    G.panX = canvasSz.x * 0.5f - n.lx * G.zoom;
    G.panY = canvasSz.y * 0.5f - n.ly * G.zoom;
}

bool matchesSearch(const GNode& n, const char* q) {
    if (!q || !q[0]) return true;
    // case-insensitive substring on label / kind / id / path
    auto has = [q](const std::string& s) {
        if (s.empty()) return false;
        std::string a = s, b = q;
        for (char& c : a) c = (char)std::tolower((unsigned char)c);
        for (char& c : b) c = (char)std::tolower((unsigned char)c);
        return a.find(b) != std::string::npos;
    };
    return has(n.label) || has(n.kind) || has(n.id) || has(n.path);
}

void drawLegend(ImDrawList* dl, ImVec2 origin) {
    const float S = ui::uiScale;
    // Compact legend: edge styles + a few node kinds.
    struct Row { const char* label; ImU32 col; bool dashed; bool isNode; };
    const Row rows[] = {
        {"extracted", edgeColor("CONTAINS", 0.85f), false, false},
        {"inferred",  edgeColor("DEPENDS_ON", 0.85f), true,  false},
        {"manual",    kindColor("project", 0.90f), false, false},
        {"ai link",   kindColor("project", 0.90f), true,  false},
        {"part",      kindColor("part"), false, true},
        {"assembly",  kindColor("assembly"), false, true},
        {"feature",   kindColor("feature"), false, true},
        {"equation",  kindColor("equation"), false, true},
        {"mate",      kindColor("mate"), false, true},
        {"drawing",   kindColor("drawing"), false, true},
    };
    float y = origin.y;
    float x = origin.x;
    ImU32 tdim = Palette::toU32(palette.textDim);
    for (const auto& r : rows) {
        if (r.isNode) {
            dl->AddCircleFilled(ImVec2(x + 6 * S, y + 6 * S), 5 * S, r.col);
            dl->AddCircle(ImVec2(x + 6 * S, y + 6 * S), 5 * S,
                          ImGui::GetColorU32(ImVec4(palette.border[0], palette.border[1],
                                                    palette.border[2], 0.9f)), 0, 1.0f);
        } else if (r.dashed) {
            drawDashedLine(dl, ImVec2(x, y + 6 * S), ImVec2(x + 18 * S, y + 6 * S),
                           r.col, 1.5f * S, 4 * S, 3 * S);
        } else {
            dl->AddLine(ImVec2(x, y + 6 * S), ImVec2(x + 18 * S, y + 6 * S), r.col, 1.5f * S);
        }
        dl->AddText(ImVec2(x + 24 * S, y), tdim, r.label);
        y += 16 * S;
    }
}

void drawSidePanel(AppState& st, float panelW) {
    auto& G = S();
    const float S = ui::uiScale;
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * S, 10 * S));
    ImGui::BeginChild("##graph_side", ImVec2(panelW, 0), ImGuiChildFlags_Borders);
    ui::popGlassChildStyle();

    auto row = [](const char* k, const std::string& v) {
        if (v.empty()) return;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted(k);
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextWrapped("%s", v.c_str());
        ImGui::PopStyleColor();
    };
    auto nodeLabel = [&](const std::string& id) -> std::string {
        for (const auto& n : G.nodes) if (n.id == id) return n.label;
        return id;
    };

    // --- an edge is selected: the "properties that link the parts" readout ---
    if (G.selectedEdge >= 0 && G.selectedEdge < (int)G.edges.size()) {
        const GEdge& e = G.edges[G.selectedEdge];
        plmSectionHeader("Link", "mesh");
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImGui::ColorConvertU32ToFloat4(edgeColor(e.kind, 1.0f)));
        ImGui::Text("[%s]", e.kind.c_str());
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("from");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (ImGui::SmallButton((nodeLabel(e.src) + "##lnksrc").c_str())) {
            G.selectedId = e.src; G.selectedEdge = -1; G.blast.clear();
        }
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("to");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (ImGui::SmallButton((nodeLabel(e.dst) + "##lnkdst").c_str())) {
            G.selectedId = e.dst; G.selectedEdge = -1; G.blast.clear();
        }
        ImGui::Dummy(ImVec2(0, 4 * S));
        row("origin", e.origin);
        if (e.meta.is_object()) {
            for (auto it = e.meta.begin(); it != e.meta.end(); ++it) {
                std::string v = it->is_string() ? it->get<std::string>()
                    : (it->is_null() ? std::string() : it->dump());
                row(it.key().c_str(), v);
            }
        }
        ImGui::Dummy(ImVec2(0, 8 * S));
        bool authored = (e.origin == "manual" || e.origin == "ai");
        if (authored) {
            std::string linkId = e.meta.is_object()
                ? e.meta.value("link_id", std::string()) : "";
            if (graphGlassButton("##rmlink", "Remove link", true, -1.0f)
                    && !linkId.empty())
                requestLinkRemove(st, linkId);
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1],
                       palette.textDim[2], 1.0f));
            ImGui::TextWrapped("Derived from the model — edit the model to change it.");
            ImGui::PopStyleColor();
        }
        ImGui::EndChild();
        ImGui::PopStyleVar();
        return;
    }

    plmSectionHeader("Node", "mesh");
    ImGui::Dummy(ImVec2(0, 4 * S));

    const GNode* sel = nullptr;
    for (const auto& n : G.nodes)
        if (n.id == G.selectedId) { sel = &n; break; }

    if (!sel) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        if (!G.linkArmSrc.empty())
            ImGui::TextWrapped("Click the target node to link it — or Esc to cancel.");
        else
            ImGui::TextWrapped("Click a node to inspect it, or a link to see its "
                               "properties.");
        ImGui::PopStyleColor();
        ImGui::EndChild();
        ImGui::PopStyleVar();
        return;
    }

    // Kind pill
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(kindColor(sel->kind)));
    ImGui::Text("[%s]", kindShort(sel->kind));
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f));
    ImGui::TextWrapped("%s", sel->label.c_str());
    ImGui::PopStyleColor();

    ImGui::Dummy(ImVec2(0, 4 * S));
    row("kind", sel->kind);
    row("id", sel->id);
    row("origin", sel->origin);
    row("path", sel->path);
    row("doc", sel->docId);
    row("meta", sel->metaSummary);

    ImGui::Dummy(ImVec2(0, 8 * S));
    if (graphGlassButton("##blast", G.blastLoading ? "Blasting…" : "Blast radius",
                         true, -1.0f) && !G.blastLoading) {
        requestBlast(st, sel->id);
    }
    // Link authoring: arm this node as a link source.
    ImGui::Dummy(ImVec2(0, 4 * S));
    if (G.linkArmSrc == sel->id) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        ImGui::TextWrapped("Click the target node…");
        ImGui::PopStyleColor();
        if (graphGlassButton("##armcancel", "Cancel", false, -1.0f))
            G.linkArmSrc.clear();
    } else if (graphGlassButton("##armlink", "Link from here…", false, -1.0f)) {
        G.linkArmSrc = sel->id;
    }
    if (!G.blast.empty()) {
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                   palette.statusWarn[2], 1.0f));
        ImGui::Text("%d node%s in blast", (int)G.blast.size(),
                    G.blast.size() == 1 ? "" : "s");
        ImGui::PopStyleColor();
    }

    // Outgoing / incoming edge counts for orientation.
    int outE = 0, inE = 0;
    for (const auto& e : G.edges) {
        if (e.src == sel->id) ++outE;
        if (e.dst == sel->id) ++inE;
    }
    ImGui::Dummy(ImVec2(0, 6 * S));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 1.0f));
    ImGui::Text("edges  out %d · in %d", outE, inE);
    ImGui::PopStyleColor();

    ImGui::EndChild();
    ImGui::PopStyleVar();
}

void drawCanvas(AppState& st, ImVec2 canvasSz) {
    auto& G = S();
    const float S = ui::uiScale;

    ImGui::InvisibleButton("##graph_canvas", canvasSz,
                           ImGuiButtonFlags_MouseButtonLeft |
                           ImGuiButtonFlags_MouseButtonMiddle |
                           ImGuiButtonFlags_MouseButtonRight);
    const bool hovered = ImGui::IsItemHovered();
    const ImVec2 origin = ImGui::GetItemRectMin();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(origin, ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y), true);

    // Frosted canvas bed.
    ui::glassBackground(dl, origin,
                        ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y), 10.0f);

    if (G.layoutDirty) computeLayout(G);

    // First layout after data lands → fit the view once.
    static int s_fitToken = -1;
    if (s_fitToken != G.layoutToken && !G.nodes.empty()) {
        fitView(G, canvasSz);
        s_fitToken = G.layoutToken;
    }

    // Zoom (wheel) around cursor.
    if (hovered) {
        float wheel = ImGui::GetIO().MouseWheel;
        if (wheel != 0.0f) {
            ImVec2 mp = ImGui::GetIO().MousePos;
            float lx = (mp.x - origin.x - G.panX) / G.zoom;
            float ly = (mp.y - origin.y - G.panY) / G.zoom;
            float old = G.zoom;
            G.zoom = std::clamp(G.zoom * (wheel > 0 ? 1.12f : 1.0f / 1.12f), 0.12f, 6.0f);
            G.panX = (mp.x - origin.x) - lx * G.zoom;
            G.panY = (mp.y - origin.y) - ly * G.zoom;
            (void)old;
        }
    }

    // Pan: middle-drag or left-drag on empty background.
    if (hovered && (ImGui::IsMouseDragging(ImGuiMouseButton_Middle) ||
                    (ImGui::IsMouseDragging(ImGuiMouseButton_Left) &&
                     G.hoverId.empty() && !ImGui::GetIO().KeyCtrl))) {
        ImVec2 d = ImGui::GetIO().MouseDelta;
        G.panX += d.x;
        G.panY += d.y;
    }

    auto toScreen = [&](float lx, float ly) -> ImVec2 {
        return ImVec2(origin.x + G.panX + lx * G.zoom,
                      origin.y + G.panY + ly * G.zoom);
    };

    // Search filter set.
    const bool filtering = G.search[0] != 0;
    std::unordered_set<std::string> matchIds;
    if (filtering) {
        for (const auto& n : G.nodes)
            if (matchesSearch(n, G.search)) matchIds.insert(n.id);
    }

    // Index for edge endpoints.
    std::unordered_map<std::string, int> idx;
    idx.reserve(G.nodes.size() * 2);
    for (int i = 0; i < (int)G.nodes.size(); ++i) idx[G.nodes[i].id] = i;

    // Edges under nodes.
    for (int ei = 0; ei < (int)G.edges.size(); ++ei) {
        const GEdge& e = G.edges[ei];
        if (G.hiddenKinds.count(e.kind)) continue;
        auto si = idx.find(e.src), di = idx.find(e.dst);
        if (si == idx.end() || di == idx.end()) continue;
        const GNode& a = G.nodes[si->second];
        const GNode& b = G.nodes[di->second];
        bool dimf = filtering && !matchIds.count(a.id) && !matchIds.count(b.id);
        bool blastEdge = !G.blast.empty() &&
                         (G.blast.count(a.id) || G.blast.count(b.id));
        bool selEdge = (ei == G.selectedEdge);
        bool authored = (e.origin == "manual" || e.origin == "ai");
        float alpha = dimf ? 0.12f : (selEdge ? 1.0f : blastEdge ? 0.95f : 0.55f);
        ImU32 col = edgeColor(e.kind, alpha);
        if (authored && !dimf)
            col = ImGui::GetColorU32(ImVec4(palette.accent[0], palette.accent[1],
                                            palette.accent[2], selEdge ? 1.0f : 0.85f));
        else if (blastEdge && !dimf)
            col = ImGui::GetColorU32(ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                           palette.statusWarn[2], 0.90f));
        ImVec2 p0 = toScreen(a.lx, a.ly);
        ImVec2 p1 = toScreen(b.lx, b.ly);
        float thick = (selEdge ? 3.0f : (blastEdge && !dimf ? 2.2f : 1.3f)) * S;
        // extracted = solid, inferred = dashed, manual = solid accent, ai = dashed accent
        if (e.origin == "inferred" || e.origin == "ai")
            drawDashedLine(dl, p0, p1, col, thick, 7 * S, 4 * S);
        else
            dl->AddLine(p0, p1, col, thick);
        // Dimensional value label at the edge midpoint when zoomed in.
        if (!dimf && !e.dimLabel.empty() && G.zoom >= 0.8f) {
            ImVec2 midp((p0.x + p1.x) * 0.5f, (p0.y + p1.y) * 0.5f);
            ImVec2 ts = ImGui::CalcTextSize(e.dimLabel.c_str());
            dl->AddText(ImVec2(midp.x - ts.x * 0.5f, midp.y - ts.y * 0.5f),
                        Palette::toU32(palette.textDim), e.dimLabel.c_str());
        }
    }

    // Hit-test nearest node under cursor.
    G.hoverId.clear();
    ImVec2 mp = ImGui::GetIO().MousePos;
    float bestD = 1e9f;
    if (hovered) {
        for (const auto& n : G.nodes) {
            ImVec2 c = toScreen(n.lx, n.ly);
            float r = n.radius * G.zoom;
            float dx = mp.x - c.x, dy = mp.y - c.y;
            float d = dx * dx + dy * dy;
            if (d <= r * r && d < bestD) {
                bestD = d;
                G.hoverId = n.id;
            }
        }
    }

    // Click select (nodes) — also completes an armed link.
    if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !G.hoverId.empty()) {
        if (!G.linkArmSrc.empty() && G.hoverId != G.linkArmSrc) {
            G.linkPopupDst = G.hoverId;
            G.linkPopupOpen = true;
        } else {
            G.selectedId = G.hoverId;
            G.selectedEdge = -1;
            G.blast.clear();
        }
    }

    // Nearest-edge pick when no node is under the cursor (< 6px, screen space).
    if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && G.hoverId.empty()
        && G.linkArmSrc.empty()) {
        int bestEdge = -1;
        float bestEd = 6.0f * 6.0f;
        for (int ei = 0; ei < (int)G.edges.size(); ++ei) {
            const GEdge& e = G.edges[ei];
            if (G.hiddenKinds.count(e.kind)) continue;
            auto si = idx.find(e.src), di = idx.find(e.dst);
            if (si == idx.end() || di == idx.end()) continue;
            ImVec2 p0 = toScreen(G.nodes[si->second].lx, G.nodes[si->second].ly);
            ImVec2 p1 = toScreen(G.nodes[di->second].lx, G.nodes[di->second].ly);
            float vx = p1.x - p0.x, vy = p1.y - p0.y;
            float wx = mp.x - p0.x, wy = mp.y - p0.y;
            float len2 = vx * vx + vy * vy;
            float t = len2 > 0 ? std::clamp((wx * vx + wy * vy) / len2, 0.0f, 1.0f) : 0.0f;
            float ex = p0.x + t * vx, ey = p0.y + t * vy;
            float dx = mp.x - ex, dy = mp.y - ey;
            float d2 = dx * dx + dy * dy;
            if (d2 < bestEd) { bestEd = d2; bestEdge = ei; }
        }
        G.selectedEdge = bestEdge;
        if (bestEdge >= 0) { G.selectedId.clear(); G.blast.clear(); }
    }

    // Nodes.
    for (const auto& n : G.nodes) {
        ImVec2 c = toScreen(n.lx, n.ly);
        float r = n.radius * G.zoom;
        bool dim = filtering && !matchIds.count(n.id);
        bool sel = (n.id == G.selectedId);
        bool hov = (n.id == G.hoverId);
        bool inBlast = G.blast.count(n.id) > 0;
        float a = dim ? 0.18f : 1.0f;
        ImU32 fill = kindColor(n.kind, 0.85f * a);
        ImU32 ring = Palette::toU32(palette.border);
        if (sel) {
            ring = Palette::toU32(palette.accent);
            r += 2.0f * S;
        } else if (inBlast) {
            ring = Palette::toU32(palette.statusWarn);
            r += 1.5f * S;
        } else if (hov) {
            ring = Palette::toU32(palette.accentDim);
        }
        dl->AddCircleFilled(c, r, fill, 24);
        dl->AddCircle(c, r, ring, 24, (sel || inBlast ? 2.4f : 1.4f) * S);
        if (hov && !dim)
            dl->AddCircleFilled(c, r * 0.35f,
                ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                          palette.text[2], 0.55f)), 12);

        // Label when zoomed in enough or selected/hovered.
        if (!dim && (G.zoom >= 0.75f || sel || hov)) {
            const char* lab = n.label.c_str();
            ImVec2 ts = ImGui::CalcTextSize(lab);
            // Cap width with ellipsis-ish: just clip long labels.
            float maxW = 120.0f * S * std::max(0.8f, G.zoom);
            ImU32 tc = ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                                palette.text[2], sel || hov ? 1.0f : 0.85f));
            ImVec2 tp(c.x - std::min(ts.x, maxW) * 0.5f, c.y + r + 3 * S);
            if (ts.x > maxW) {
                // Draw clipped via PushClipRect around the label area.
                dl->PushClipRect(tp, ImVec2(tp.x + maxW, tp.y + ts.y), true);
                dl->AddText(tp, tc, lab);
                dl->PopClipRect();
            } else {
                dl->AddText(tp, tc, lab);
            }
        }
    }

    // Tooltip.
    if (!G.hoverId.empty() && hovered) {
        const GNode* hn = nullptr;
        for (const auto& n : G.nodes)
            if (n.id == G.hoverId) { hn = &n; break; }
        if (hn) {
            ImGui::BeginTooltip();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImGui::ColorConvertU32ToFloat4(kindColor(hn->kind)));
            ImGui::Text("[%s]", kindShort(hn->kind));
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextUnformatted(hn->label.c_str());
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::Text("kind  %s", hn->kind.c_str());
            ImGui::Text("origin  %s", hn->origin.c_str());
            if (!hn->path.empty()) ImGui::Text("path  %s", hn->path.c_str());
            if (!hn->metaSummary.empty())
                ImGui::TextWrapped("meta  %s", hn->metaSummary.c_str());
            ImGui::PopStyleColor();
            ImGui::EndTooltip();
        }
    }

    // Empty / loading overlays.
    if (G.nodes.empty() && !G.loading) {
        const char* msg = G.err.empty() ? "No graph nodes yet" : G.err.c_str();
        const char* sub = G.err.empty()
            ? "Open or save parts in this project to populate the graph."
            : nullptr;
        ImVec2 ts = ImGui::CalcTextSize(msg);
        dl->AddText(ImVec2(origin.x + (canvasSz.x - ts.x) * 0.5f,
                           origin.y + canvasSz.y * 0.45f),
                    Palette::toU32(palette.textDim), msg);
        if (sub) {
            ImVec2 ss = ImGui::CalcTextSize(sub);
            dl->AddText(ImVec2(origin.x + (canvasSz.x - ss.x) * 0.5f,
                               origin.y + canvasSz.y * 0.45f + ts.y + 6 * S),
                        Palette::toU32(palette.textDisabled), sub);
        }
    }
    if (G.loading && G.nodes.empty()) {
        const char* msg = "Loading graph…";
        ImVec2 ts = ImGui::CalcTextSize(msg);
        dl->AddText(ImVec2(origin.x + (canvasSz.x - ts.x) * 0.5f,
                           origin.y + canvasSz.y * 0.5f),
                    Palette::toU32(palette.accent), msg);
    }

    // Legend bottom-left of canvas.
    drawLegend(dl, ImVec2(origin.x + 12 * S, origin.y + canvasSz.y - 186 * S));

    dl->PopClipRect();

    // Search-driven center: when the user types a filter, center on first match once.
    if (G.centerPending && filtering && !matchIds.empty()) {
        for (const auto& n : G.nodes) {
            if (matchIds.count(n.id)) {
                centerOnNode(G, n, canvasSz);
                break;
            }
        }
        G.centerPending = false;
    }
}

}  // namespace

// ---- public ------------------------------------------------------------------

bool plmGraphAvailable() { return S().available; }

void plmGraphOnProjectChanged() {
    auto& G = S();
    G.nodes.clear();
    G.edges.clear();
    G.blast.clear();
    G.selectedId.clear();
    G.hoverId.clear();
    G.err.clear();
    G.generatedAt.clear();
    G.layoutDirty = true;
    G.loading = false;
    G.blastLoading = false;
    G.projectDir.clear();
    G.lastFetch = -1e9;
    G.panX = G.panY = 0.0f;
    G.zoom = 1.0f;
    G.selectedEdge = -1;
    G.hiddenKinds.clear();
    G.linkArmSrc.clear();
    G.linkPopupOpen = false;
    G.linkPopupDst.clear();
    G.linkNote[0] = 0;
    // Keep available flag — method absence is session-wide, not per-project.
}

void plmGraphFocus(const std::string& query) {
    auto& G = S();
    std::snprintf(G.search, sizeof(G.search), "%s", query.c_str());
    G.focusPending = query;
    G.centerPending = !query.empty();
    G.lastFetch = -1e9;  // force a refetch on the next draw
}

void plmDrawGraphPage(AppState& st) {
    auto& G = S();
    const float S = ui::uiScale;

    if (!G.available) {
        // Should not be reached (rail hides us); belt-and-suspenders.
        plmEmptyState("[ ]", "Graph unavailable",
                      "This Chudware build does not expose the CAD graph service.");
        return;
    }

    // Project change → drop caches.
    const std::string proj = st.plmProjectPath();
    if (proj != G.projectDir && !G.loading) {
        // Only wipe when we have a previous project; first paint just fetches.
        if (!G.projectDir.empty()) plmGraphOnProjectChanged();
        G.projectDir = proj;
    }

    // Auto-refresh when the OS app regains focus.
    const bool lost = ImGui::GetIO().AppFocusLost;
    if (G.wasAppFocusLost && !lost)
        requestGraph(st, /*force=*/true);
    G.wasAppFocusLost = lost;

    requestGraph(st, /*force=*/false);

    // ---- toolbar -------------------------------------------------------------
    ImGui::SetNextItemWidth(220 * S);
    if (ImGui::InputTextWithHint("##graph_search", "Search nodes…",
                                 G.search, sizeof(G.search))) {
        G.centerPending = G.search[0] != 0;
    }
    ImGui::SameLine();
    if (graphGlassButton("##graph_refresh", G.loading ? "Refreshing…" : "Refresh",
                         false, 110 * S) && !G.loading) {
        requestGraph(st, /*force=*/true);
    }
    ImGui::SameLine();
    if (graphGlassButton("##graph_fit", "Fit", false, 70 * S)) {
        // Fit is applied next canvas draw via token bump.
        G.layoutToken++;
    }
    if (!G.generatedAt.empty()) {
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::Text("generated %s", G.generatedAt.c_str());
        ImGui::PopStyleColor();
    }
    if (!G.err.empty() && !G.nodes.empty()) {
        ImGui::SameLine();
        plmError(G.err);
    }

    // ---- edge-kind filter row -----------------------------------------------
    if (graphGlassButton("##links_only", "Links only", false, 0.0f)) {
        G.hiddenKinds.clear();
        G.hiddenKinds.insert("CONTAINS");  // hide containment → the link web
    }
    ImGui::SameLine();
    if (graphGlassButton("##all_kinds", "All", false, 0.0f))
        G.hiddenKinds.clear();
    for (const char* k : kEdgeKinds) {
        ImGui::SameLine();
        bool vis = !G.hiddenKinds.count(k);
        ImU32 tc = vis ? edgeColor(k, 1.0f) : Palette::toU32(palette.textDisabled);
        ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(tc));
        if (ImGui::SmallButton(k)) {
            if (vis) G.hiddenKinds.insert(k); else G.hiddenKinds.erase(k);
        }
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, 4 * S));

    // ---- body: canvas + side panel ------------------------------------------
    const float gap = 10.0f * S;
    const float sideW = 260.0f * S;
    const float bodyH = ImGui::GetContentRegionAvail().y;
    const float canvasW = std::max(120.0f * S,
                                   ImGui::GetContentRegionAvail().x - sideW - gap);

    ImGui::BeginChild("##graph_body", ImVec2(0, bodyH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

    ImGui::BeginChild("##graph_canvas_host", ImVec2(canvasW, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImVec2 csz = ImGui::GetContentRegionAvail();
    drawCanvas(st, csz);
    ImGui::EndChild();

    ImGui::SameLine(0, gap);
    drawSidePanel(st, sideW);

    ImGui::EndChild();

    // ---- link authoring popup (outside the canvas child) --------------------
    if (G.linkPopupOpen) {
        ImGui::OpenPopup("Add link##graph_link");
        G.linkPopupOpen = false;  // consume: the popup now owns its lifetime
    }
    if (ImGui::BeginPopup("Add link##graph_link")) {
        ImGui::TextUnformatted("New link");
        ImGui::SetNextItemWidth(180 * S);
        ImGui::Combo("kind##lk", &G.linkKindIdx, kEdgeKinds,
                     IM_ARRAYSIZE(kEdgeKinds));
        ImGui::SetNextItemWidth(220 * S);
        ImGui::InputTextWithHint("##lknote", "note (optional)",
                                 G.linkNote, sizeof(G.linkNote));
        if (ImGui::Button("Create")) {
            int ki = std::clamp(G.linkKindIdx, 0,
                                (int)IM_ARRAYSIZE(kEdgeKinds) - 1);
            requestLinkAdd(st, G.linkArmSrc, G.linkPopupDst, kEdgeKinds[ki],
                           G.linkNote);
            G.linkArmSrc.clear(); G.linkPopupDst.clear(); G.linkNote[0] = 0;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel")) {
            G.linkArmSrc.clear(); G.linkPopupDst.clear();
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    // Esc cancels an armed link when no popup is open.
    if (!G.linkArmSrc.empty() && !ImGui::IsPopupOpen("Add link##graph_link") &&
        ImGui::IsKeyPressed(ImGuiKey_Escape))
        G.linkArmSrc.clear();
}

}  // namespace ui
