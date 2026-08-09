#pragma once

#include <cstdint>
#include <cstddef>
#include <string>
#include <string_view>
#include <vector>

namespace ui {

struct MfgAttentionDelivery {
    std::string channel;
    std::string status;
    std::string error;
    std::uint64_t seq = 0;
};

struct MfgAttentionRow {
    std::string id;
    std::string severity;
    std::string device;
    std::string run;
    std::string reason;
    std::string why;
    std::uint64_t raisedSeq = 0;
    std::string acknowledgedBy;
    std::uint64_t acknowledgedSeq = 0;
    std::uint64_t resolvedSeq = 0;
    std::uint32_t occurrenceCount = 1;
    std::vector<MfgAttentionDelivery> deliveries;
};

using MfgAttentionAcknowledgeFn = void (*)(void* context, std::string_view itemId,
                                            std::string_view actor);

struct MfgAttentionPanelInput {
    // Plain pointer+count, not std::span: the host target compiles as C++17.
    const MfgAttentionRow* itemsData = nullptr;
    std::size_t itemsCount = 0;
    std::string_view actor;
    bool requestInFlight = false;
    std::string_view requestError;
    void* acknowledgeContext = nullptr;
    MfgAttentionAcknowledgeFn acknowledge = nullptr;

    const MfgAttentionRow* begin() const { return itemsData; }
    const MfgAttentionRow* end() const { return itemsData + itemsCount; }
    std::size_t size() const { return itemsCount; }
};

// Read-only renderer. The owner supplies journal-projected rows and an RPC sink;
// this panel never mutates, acknowledges, resolves, or expires an item locally.
void drawManufactureAttentionPanel(const MfgAttentionPanelInput& input);

}  // namespace ui
