#pragma once
// graph_changes.h — "what changed since I last looked?" for the PLM Graph tab.
//
// WHY THIS EXISTS
// ---------------
// Eighteen cad_graph_* worker RPCs are registered; the Graph tab called six. The most valuable
// of the missing twelve is `cad_graph_changes` (contracts/cad_graph.md, "Change diff"), which
// answers the one question the tab could not answer at all: which nodes and edges appeared or
// vanished since the cached build. The tab's only temporal signal was a raw
// `generated <ISO stamp>` string in its status line. This module owns that diff, the persisted
// watch overlay that flags the parts you actually care about, and the side rail that lists both.
//
// THE SCHEDULING RULE — READ THIS BEFORE ADDING A CALL SITE
// --------------------------------------------------------
// `_cad_graph_changes` diffs the on-disk cache against a FRESH FULL REBUILD
// (`cg.get_graph(project_dir, force=True, ...)`, worker_rpc.py). On a large project that is
// seconds of work, and it invalidates nothing, so it is pure cost. It MUST therefore be
// user-initiated, always, with a visible spinner.
//
// The API is shaped so that cannot be got wrong: there is exactly ONE fetch entry point,
// requestChanges(), and it has no "refresh if stale" sibling. toggleChanges() deliberately does
// NOT fetch — opening the overlay shows an empty state whose action button runs the diff, so the
// user is told what it costs before paying it. NEVER call requestChanges() from the graph's poll
// path, from the app-focus-regain auto-refresh, from a project-changed hook, or from any painter:
// that turns a 20 Hz idle app into a rebuild treadmill.
//
// The watch overlay is the opposite: `.plm/graph_watch.json` is a small JSON read/write that
// never touches the graph cache, so watchAdd/watchRemove/requestWatchList are cheap and may be
// called from an interaction handler freely.
//
// HOUSE RULES OBSERVED HERE
//   * Colors via ui::palette / ui::kit::Tone only; sizes via ui::metrics / the kit's density
//     accessors only; icons by name from contracts/icons.md only.
//   * Motion via ui::anim (the rail enters as a stagger wave; the kit's spinner pumps frames).
//
// GL/GUI thread only. State is a single file-static in graph_changes.cpp, exactly like
// GraphTabState — nothing to construct, nothing to own.

#include <string>

struct AppState;

namespace ui {
namespace plmgraph {

// ---- the diff -----------------------------------------------------------------------------------

// True while the Changes overlay is on: the canvas should paint node/edge verdicts and the host
// should give the rail its column. Purely a view flag — it never implies a diff has been run.
bool changesActive();

// False after the worker answered unknown_method / not_implemented for cad_graph_changes (an older
// services tree). The toolbar should DISABLE its Changes control rather than hide it; the rail
// explains the situation if opened anyway.
bool changesAvailable();

// True while a diff is in flight. The toolbar must show its own busy affordance or disable the
// re-run control; the rail draws a spinner on its own.
bool changesBusy();

// Node changes in the last diff (added + removed). Suitable as a toolbar badge count
// (kit::popoverBegin's activeCount); 0 before the first diff.
int changesCount();

// Flip the overlay. Does NOT fetch — see THE SCHEDULING RULE above. `projectDir`/`linked` are the
// values a subsequent diff would use; passing them here lets the module drop a diff that belongs
// to a different project (or a different linked-projects policy) instead of showing stale verdicts.
void toggleChanges(AppState& st, const std::string& projectDir, bool linked);

// Run the diff. The ONLY fetch entry point, and only ever from an explicit user gesture (the rail's
// action button, or a toolbar "Diff" item). Re-entrant calls while busy are ignored.
// `linked` must match the graph's own linked-projects setting or the two node sets will not line up.
void requestChanges(AppState& st, const std::string& projectDir, bool linked);

// Per-element verdict for the canvas painter. Both are a hash lookup over the last diff and
// allocate nothing, so they are safe to call per node / per edge / per frame.
//
// Precedence: Watched outranks Added and Removed — a change to something the user explicitly asked
// to be told about is the strongest signal on the canvas and must not hide behind the generic
// "added" treatment. `Watched` here means "changed AND watched"; the persistent, unchanged watch
// marker is isWatched().
//
// Both return None whenever the overlay is off, so toggling it off clears every highlight without
// the painter needing a second condition.
enum class Delta { None, Added, Removed, Watched };
Delta nodeDelta(const std::string& id);
// Edges are keyed (src, dst, kind), the worker's own edge identity (cad_graph.graph_diff `_ekey`).
// Never returns Watched: the worker flags watched NODES only.
Delta edgeDelta(const std::string& src, const std::string& dst, const std::string& kind);

// The side rail: Added / Removed / Watched, plus the diff's edge counts and the run controls.
// Removed nodes exist ONLY here — they are gone from the graph, so no canvas treatment can show
// them; each gets a real row carrying its id, kind and label out of the cached graph.
//
// Returns the node id of a clicked row so the caller can centre the camera on it (flyToNode), else
// an empty string. Removed rows return empty — there is nothing to fly to; they offer a copy-id
// action instead.
//
// The caller owns the window/child, its scroll and its kit density. Draws its own empty state when
// the diff came back clean and its own blocked state when the RPC failed: an error never renders as
// an empty rail. Lazily issues ONE cad_graph_watch_list per project on first draw (a small JSON
// read — never a rebuild).
std::string drawChangesRail(AppState& st);

// ---- the watch overlay --------------------------------------------------------------------------
// `.plm/graph_watch.json` (schema aicad.cad_graph_watch/1): node ids and keywords of interest.
// The next diff flags matching added/removed nodes, which is how nodeDelta() reports Watched.

// True when `id` is watched by an explicit node-id entry — the inspector's toggle state, and a
// persistent canvas marker independent of any diff. Keyword watches are matched server-side
// against node metadata this TU does not hold, so they are reported through the diff, not here.
bool isWatched(const std::string& id);

// Add / remove a node-id watch. Optimistically updates isWatched() so the inspector's toggle is
// live, then reconciles with the worker's reply; a failure is surfaced in the rail's Watched
// section rather than swallowed. watchRemove takes a NODE id — the module resolves the overlay
// entry id itself, fetching the list first if it has to.
void watchAdd(AppState& st, const std::string& projectDir, const std::string& id);
void watchRemove(AppState& st, const std::string& projectDir, const std::string& id);

// Refresh the overlay from disk. Cheap; call it when the project folder changes.
void requestWatchList(AppState& st, const std::string& projectDir);

}  // namespace plmgraph
}  // namespace ui
