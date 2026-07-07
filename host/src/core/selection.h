// Selection model (C15). Holds the current set of picked topology (PickHits), with click=replace /
// ctrl-click=toggle semantics, kind filters, clear, and an observer callback that C16 (feature
// tree) and C17 (PropertyManager) subscribe to. Each entry carries a `pending_resolve` slot for
// the typed selector the worker returns from doc_resolve_pick (filled in C16) — left empty here.
#pragma once

#include <functional>
#include <string>
#include <vector>

#include "../render/id_pass.h"

namespace core {

// One selected item: the raw pick plus a slot for its resolved selector (worker round-trip, C16).
struct SelItem {
    render::PickHit hit;
    bool pending_resolve = true;  // true until doc_resolve_pick fills `selector`
    std::string selector;         // JSON selector ref (empty until resolved by C16)
};

class Selection {
public:
    using Observer = std::function<void()>;

    // Which kinds may be selected. A pick whose kind is filtered out is ignored.
    bool allowFaces = true, allowEdges = true, allowVertices = true;

    bool allows(render::PickKind k) const {
        switch (k) {
            case render::PickKind::Face: return allowFaces;
            case render::PickKind::Edge: return allowEdges;
            case render::PickKind::Vertex: return allowVertices;
            default: return false;
        }
    }

    const std::vector<SelItem>& items() const { return items_; }
    // Non-const access for C16's resolve round-trip (fills pending_resolve / selector slots).
    std::vector<SelItem>& mutableItems() { return items_; }
    bool empty() const { return items_.empty(); }
    size_t size() const { return items_.size(); }

    // Replace the whole selection with this single pick (or clear if invalid/filtered). The
    // standard left-click behavior.
    void selectOne(const render::PickHit& hit) {
        items_.clear();
        if (hit.valid() && allows(hit.kind)) items_.push_back(SelItem{hit, true, {}});
        notify();
    }

    // Toggle membership of this pick (ctrl-click): add if absent, remove if present.
    void toggle(const render::PickHit& hit) {
        if (!hit.valid() || !allows(hit.kind)) return;
        for (size_t i = 0; i < items_.size(); ++i) {
            if (items_[i].hit == hit) {
                items_.erase(items_.begin() + i);
                notify();
                return;
            }
        }
        items_.push_back(SelItem{hit, true, {}});
        notify();
    }

    void clear() {
        if (items_.empty()) return;
        items_.clear();
        notify();
    }

    bool contains(const render::PickHit& hit) const {
        for (const auto& it : items_)
            if (it.hit == hit) return true;
        return false;
    }

    // Subscribe to selection changes. Single observer slot is enough for the panels we have;
    // chain in the callback if more are ever needed.
    void setObserver(Observer obs) { observer_ = std::move(obs); }

private:
    void notify() {
        if (observer_) observer_();
    }
    std::vector<SelItem> items_;
    Observer observer_;
};

}  // namespace core
