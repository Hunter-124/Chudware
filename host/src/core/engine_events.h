// Phase 4 (shared engine host daemon): per-frame drain of hostd push events.
//
// Called from the app.cpp main loop right after st.rpc.drain(), only in daemon
// mode (engine::daemonMode()). Translates daemon events into the SAME tab UX
// the legacy pollAiCadHandoff gave mirror tabs:
//   doc.opened       -> open a background mirror tab (first reveal takes focus,
//                       later parts open in the background + aiPulse), attached
//                       read-only (attach_doc mode=ro).
//   doc.changed      -> foreign edit: refresh the active tab's mirror + mesh
//                       from the event's aicm_path; background tabs just pulse
//                       (their switch-in rebuild picks up the new revision).
//   doc.closed       -> close the matching mirror tab (reason "closed"); a
//                       "last_writer_detached" keeps the tab as a final view.
//   doc.lease_change -> toasts; an expired hold on the doc our PropertyManager
//                       is previewing cancels the preview (revision-parentage
//                       checked before the undo — see engine_events.cpp).
#pragma once

#include <string>
#include <vector>

struct AppState;

namespace core {

// No-op unless engine::daemonMode(). Safe to call every frame (cheap try-lock
// poll of the daemon socket).
void engineEventsTick(AppState& st);

// Follow a reveng reconstruction's live progress (contracts/reveng.md §4).
//
// ui::reveng::kickReconstruct() calls this the moment it fires reveng_reconstruct. Until the
// launch RPC lands, engineEventsTick() tails the mission's EventJournal (events.jsonl) and
// translates each aicad.reveng_events/1 record into ui::reveng::feedRevEngEvent(), so the
// Reconstruct stage's progress bar / sub-step track / elapsed clock advance DURING the run
// instead of only when the coarse stage-badge poll next fires. See engine_events.cpp for why
// the journal — and not the agent.event push or WorkerClient::EventFn streaming — is the only
// channel that can carry these events today.
//
// An empty sessionId stops the tail. Cheap when idle (one empty-string test per frame).
void watchRevEngReconstruct(const std::string& sessionId);

// Reveal read-only mirror tabs for docs the GUI itself just caused the daemon to
// open (recovery_open). The daemon's doc.opened fanout deliberately EXCLUDES the
// calling client — the RPC return value is how the caller learns what it opened —
// so those docs would otherwise only surface on the next app launch (via the
// bootstrap doc_list). Call this with the recovered doc_ids to mirror them in the
// current session. Same tab semantics as an agent-minted doc: the first reveal
// focuses, the rest open in the background with the aiPulse glow. No-op off
// daemon mode.
void mirrorRecoveredDocs(AppState& st, const std::vector<std::string>& docIds);

}  // namespace core
