#pragma once
// Simulation geometry gestures carry captured selectors, never the pane-time hover.
#include <cstdint>
#include <cmath>
#include <cstring>
#include <limits>
#include <string>
#include <imgui.h>
#include <nlohmann/json.hpp>

namespace ui::sim {
inline constexpr const char* kFaceDragPayload = "CHUDWARE_SIM_FACE";
inline constexpr const char* kSetupToolDragPayload = "CHUDWARE_SIM_TOOL";
inline std::string setupToolDocId;
inline void scopeSetupToolDrag(const std::string& docId) {
    const ImGuiPayload* payload = ImGui::GetDragDropPayload();
    if (!payload || !payload->IsDataType(kSetupToolDragPayload)) setupToolDocId = docId;
}
inline const std::string& setupToolDragDocument() { return setupToolDocId; }
struct FaceDrop {
    nlohmann::json selector;
    std::uint32_t body = 0, topo = 0;
};
inline bool validFace(const nlohmann::json& selector, std::uint32_t body, std::uint32_t topo) {
    if (!body || body > 0xfffu || !topo || topo > 0x3ffffu || !selector.is_object()) return false;
    const auto kind = selector.find("kind");
    if (kind == selector.end() || !kind->is_string() || *kind != "face") return false;
    const auto centroid = selector.find("centroid");
    if (centroid == selector.end() || !centroid->is_array() || centroid->size() != 3) return false;
    for (const auto& coordinate : *centroid)
        if (!coordinate.is_number() || !std::isfinite(coordinate.get<double>())) return false;
    const auto pick = selector.find("_pick");
    if (pick != selector.end() && (!pick->is_object() || !pick->contains("body") ||
        !(*pick)["body"].is_number_integer() || (*pick)["body"] != body ||
        !pick->contains("topo") || !(*pick)["topo"].is_number_integer() || (*pick)["topo"] != topo))
        return false;
    return true;
}
inline void faceDragSource(const std::string& docId, const nlohmann::json& selector,
                           std::uint32_t body, std::uint32_t topo, const char* label) {
    if (docId.empty() || !validFace(selector, body, topo) ||
        !ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceNoDisableHover |
                                    ImGuiDragDropFlags_SourceAllowNullID)) return;
    const std::string payload = nlohmann::json{{"doc_id", docId}, {"selector", selector},
                                              {"body", body}, {"topo", topo}}.dump();
    ImGui::SetDragDropPayload(kFaceDragPayload, payload.c_str(), payload.size() + 1);
    ImGui::TextUnformatted(label ? label : "Model face");
    ImGui::EndDragDropSource();
}
inline bool acceptFaceDrop(const std::string& docId, FaceDrop& out) {
    if (docId.empty() || !ImGui::BeginDragDropTarget()) return false;
    bool accepted = false;
    if (const ImGuiPayload* payload = ImGui::AcceptDragDropPayload(kFaceDragPayload)) {
        if (payload->IsDelivery() && payload->Data && payload->DataSize > 1) {
            const char* bytes = static_cast<const char*>(payload->Data);
            if (bytes[payload->DataSize - 1] == '\0') {
                const auto value = nlohmann::json::parse(bytes, bytes + payload->DataSize - 1, nullptr, false);
                if (value.is_object() && value.contains("doc_id") && value["doc_id"] == docId &&
                    value.contains("selector") && value.contains("body") && value["body"].is_number_unsigned() &&
                    value.contains("topo") && value["topo"].is_number_unsigned()) {
                    const auto body = value["body"].get<std::uint64_t>();
                    const auto topo = value["topo"].get<std::uint64_t>();
                    if (body <= std::numeric_limits<std::uint32_t>::max() &&
                        topo <= std::numeric_limits<std::uint32_t>::max() &&
                        validFace(value["selector"], static_cast<std::uint32_t>(body), static_cast<std::uint32_t>(topo))) {
                        out.selector = value["selector"];
                        out.body = static_cast<std::uint32_t>(body);
                        out.topo = static_cast<std::uint32_t>(topo);
                        out.selector["_pick"] = {{"body", out.body}, {"topo", out.topo}};
                        accepted = true;
                    }
                }
            }
        }
    }
    ImGui::EndDragDropTarget();
    return accepted;
}
} // namespace ui::sim
