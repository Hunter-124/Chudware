// TessCatchup: single-flight "catch the viewport up" bookkeeping for tess-less revisions.
//
// A doc.changed published by a `with_tess:false` writer (asm_add_component, asm_mate, the
// animation/motion pose writers, parts_tools, the PLM live server) carries the new tree but
// no aicm_path. The active tab keeps drawing its previous mesh, so an assembly's components
// stayed where they were after every mate/insert/pose until some later mesh-bearing revision
// (or the end of the turn) — on 2026-09-27 a 36-revision mate+sweep stretch showed nothing
// for ten minutes. engine_events.cpp asks the daemon for the current tessellation instead
// (doc_activate: warm read-cache, assembly AICM from cached local meshes) and this class
// decides, per document, when to ask and whether the answer may be drawn:
//   * at most ONE fetch in flight per doc; revisions that land meanwhile only raise the
//     wanted revision (a pose sweep never queues one RPC per frame);
//   * an answer is drawn only when it matches the tree the viewport shows (mirrorRev) and is
//     not older than the newest tess-less revision seen; an older one refetches once;
//   * a mesh-bearing revision, a tab switch or a failed fetch drops the want.
// Pure and GL-free so it unit-tests without AppState (host/tests/test_tess_catchup.cpp).
#pragma once

#include <map>
#include <string>

namespace core {

class TessCatchup {
public:
    enum class Outcome { Commit, Refetch, Drop };

    // A tess-less revision `rev` of `docId` was applied to the active view. True when the
    // caller must issue the fetch now; false while one is already in flight for the doc.
    bool wantMesh(const std::string& docId, long rev) {
        State& s = docs_[docId];
        if (rev > s.wanted) s.wanted = rev;
        if (s.inFlight) return false;
        s.inFlight = true;
        return true;
    }

    // A mesh-bearing revision reached the viewport: nothing is owed any more. An in-flight
    // fetch still completes, and onFetched() then drops it.
    void satisfied(const std::string& docId) {
        auto it = docs_.find(docId);
        if (it == docs_.end()) return;
        if (it->second.inFlight) it->second.wanted = kNone;
        else docs_.erase(it);
    }

    // The fetch for `docId` returned. `usable` = transport ok, no method error, a mesh path;
    // `rev` = the answer's revision; `viewing` = the doc is still the active, editable view;
    // `mirrorRev` = revision of the tree the viewport currently shows. Refetch leaves the doc
    // in flight — the caller must issue the next fetch.
    Outcome onFetched(const std::string& docId, bool usable, long rev, bool viewing,
                      long mirrorRev) {
        auto it = docs_.find(docId);
        if (it == docs_.end()) return Outcome::Drop;
        State& s = it->second;
        s.inFlight = false;
        if (!usable || !viewing || s.wanted == kNone) {
            docs_.erase(it);
            return Outcome::Drop;
        }
        if (rev < s.wanted) {  // a newer tess-less revision landed while this one was served
            s.inFlight = true;
            return Outcome::Refetch;
        }
        docs_.erase(it);
        // rev > mirrorRev: the push for that revision is still queued; its own handling
        // (mesh-bearing load, or a new want) takes over. rev < mirrorRev cannot be drawn.
        return rev == mirrorRev ? Outcome::Commit : Outcome::Drop;
    }

    bool inFlight(const std::string& docId) const {
        auto it = docs_.find(docId);
        return it != docs_.end() && it->second.inFlight;
    }

private:
    static constexpr long kNone = -1;
    struct State {
        long wanted = kNone;
        bool inFlight = false;
    };
    std::map<std::string, State> docs_;
};

}  // namespace core
