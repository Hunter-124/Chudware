// switch_hold.h — keeps an async tab switch selected until its activation lands.
//
// AppState::ensureActiveTab runs every frame and re-points activeTab at the tab of the doc the
// DocMirror holds (a doc-creating path may re-point the mirror without going through
// switchToTab). An ASYNC switch-in (switchToTab's doc_activate branch) selects the clicked tab
// at once, but the mirror only follows when the activation drains on st.rpc. Whenever that took
// longer than one frame the reconcile snapped the strip back to the previous part — 2026-09-27:
// clicking an AI mirror tab during an agent turn "flashed Loading for a frame, then kicked me
// back to the last part and deselected the tab" (and the late activation then landed
// off-screen). SwitchHold records the doc an async switch waits on and the doc the mirror held
// when it began; while the active tab is that doc and the mirror has not moved elsewhere, the
// reconcile leaves the selection alone. Header-only, GL-free (host/tests/test_switch_hold.cpp).
#pragma once

#include <string>

namespace core {

class SwitchHold {
public:
    // switchToTab's async branch (dispatched or coalesced): the strip now shows `target`'s
    // shell while the mirror still holds `from`.
    void begin(const std::string& target, const std::string& from) {
        target_ = target;
        from_ = from;
    }
    // `target`'s newest activation finished (adopted or failed). A later hold for another doc
    // is untouched. After a failure the reconcile returns the strip to the mirror's tab.
    void end(const std::string& target) {
        if (target == target_) {
            target_.clear();
            from_.clear();
        }
    }
    // ensureActiveTab: true = leave activeTab on `activeDocId` although the mirror holds
    // `mirrorDocId`. A mirror that moved to a THIRD doc (new part / open) is followed as usual.
    bool keepsActiveTab(const std::string& activeDocId, const std::string& mirrorDocId) const {
        return !target_.empty() && activeDocId == target_ && mirrorDocId != target_ &&
               mirrorDocId == from_;
    }

private:
    std::string target_;
    std::string from_;
};

}  // namespace core
