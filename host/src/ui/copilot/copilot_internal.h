// Copilot internals shared across the ui/copilot/* translation units (split out of the old
// legacy_chat.cpp monolith). NOT a public header: everything below was a file-static of the
// monolith and is now defined in exactly one copilot TU and declared here for the sibling TUs.
// Public API lives in copilot.h (and core/app_state.h for the shared small-UI helpers).
//
// Module map (one concern per TU):
//   ui_common.cpp          shared small-UI helpers, Liquid-Glass bubble toolkit, code/diff/prose
//                          boxes, fit readout, copy/paste affordances, model-name/token helpers
//   chat_log.cpp           message list rendering: user block, provider log, assistant turn,
//                          windowed log + stick-to-bottom
//   activity_timeline.cpp  per-turn typed-block timeline: agent steps, thinking/action cards,
//                          transparency rows, tool-call rows, artifact actions
//   cards.cpp              plan/objectives section, clarify + plan-approve cards, corporate-org
//                          cards, exec-mode (swarm/eng-firm/advisor) cards, AI-VC checkpoint UI
//   ask_answers.cpp        GL-free ask_user question batches (AskBatch): suggested-default prefill,
//                          RAW `answer` JSON per kind, session-file form (cards.cpp draws them)
//   feed.cpp               feedTurnActivity: stream-event -> ActivityItem folding (orchestrator
//                          events, corporate events, ops-JSON detection), chat-history dirty flag
//   conversations.cpp      conversation tabs + chat-history/prompt-draft persistence (incl.
//                          WASM shared-chat sync), chat-triggered OpenFOAM poll
//   settings.cpp           AI settings sections (provider/model/auth, generation, privacy,
//                          manage-models), searchable model dropdown, provider usability
//   composer.cpp           wrapping chat input, attachments/context refs, run launch, slash
//                          commands + slash-menu autocomplete (session cmds + skills),
//                          submitTurn send path, composer prelude/toolbar/send-stop
//   panel.cpp              the docked Copilot pane host (header, sessions/tasks/artifacts
//                          panels, drawAIPanel) + the ai_workspace reuse hooks
#pragma once

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cfloat>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <map>
#include <vector>
#include <unordered_map>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect — Liquid Glass v2 host port (contract H5: glass bubbles)

#include "copilot.h"
#include "ui/layout.h"          // ui::overlayBand / takeOverlayColumnReset — pinned-column geometry
#include "ui/panel_collapse.h"  // ui::PanelCollapseAnim — shared vertical-collapse animation
#include "ui/doc_tabs.h"        // ui::docTabStripHeight — top inset so the column clears the tab band
#include "ui/model_registry.h"  // ui::ModelInfo / capability flags / provider mapping
#include "ui/metrics.h"         // ui::metrics::px/U/asmHeaderIconGap
#include "ui/widgets/kit.h"     // ui::kit — the app-wide widget vocabulary (status, rows, cards)
#include "ui/anim.h"            // ui::anim — duration tokens, easing, the frame pump
#include "ui/console_panel.h"   // drawConsoleBody (shared console helper)
#include "ui/file_dialog.h"     // openFileDialog — composer paperclip attachments (M23)
#include "ui/icons.h"           // getIcon / IconUV — composer settings glyph

#include "core/app_state.h"
#include "ui/property_manager.h"  // Edit-mode scope: PM targetId
#include "core/cpp_core.h"        // detect the in-process C++ core (libchudware) backend
#include "core/sim_client.h"      // chat-triggered OpenFOAM CFD on the SimClient channel
#include "core/jobs.h"
#include "ui_theme.h"
#include "ui/copilot/magic_keywords.h"   // ui::copilot::magicKeywordRanges — keyword presentation

#include "worker_client.h"

#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#include <emscripten/fetch.h>
#endif

// ============================ cross-module declarations ============================
// Everything below was file-static in the monolith; linkage is now external but each symbol is
// still defined exactly once (see the module map above). Keep this header in sync when moving
// code between copilot TUs.

// ---- Liquid Glass v2 host port (contract H5): glass surface + bubble helpers (ui_common) ----
struct GlassBubble {
    ImVec2         origin;
    float          width;
    ui::GlassLevel level;
    float          rounding;   // LOGICAL dp — ui::glassPanel scales by uiScale internally
};
const ui::AdaptiveSurface& chatSurf();
ImVec4 semanticOnGlass(const float role[4], const ui::AdaptiveSurface& surf);
// RGB (0..1) of a message's mode role (Agent=accent, Plan=amber, Ask=neutral).
void   modeAccentRgb(int mode, float out[3]);
// Map a raw ActivityItem status string ("running"/"pass"/"done"/"retry"/"fail"/
// "skipped"/"") onto the kit's status vocabulary. ONE spelling of that mapping for the whole
// copilot: the timeline spine, the action cards and the plan checklist all read it.
ui::kit::Status chatRowStatus(const std::string& status);
void   drawStatusGlyph(const std::string& status, float box);
void   drawBusyMark(float box);
GlassBubble beginGlassBubble(float width, ui::GlassLevel level, float rounding);
ImRect  endGlassBubble(const GlassBubble& b);
// Soft role-tinted bubble (the user's messages): begin with beginGlassBubble, end here. A flat
// `rgb` wash at `fillAlpha` plus an optional same-hue hairline — no frost, no specular rim.
ImRect  endTintBubble(const GlassBubble& b, const float rgb[3], float fillAlpha, float edgeAlpha);
// Unboxed bubble (the assistant's prose): same begin/end shape, but nothing is painted — the
// text flows on the pane's glass. Pair with a child whose horizontal inset is
// ui::chatProseInsetX() so prose lines up with the timeline's text column.
GlassBubble beginOpenBubble(float width);
ImRect      endOpenBubble(const GlassBubble& b);

// ---- auto-growing, wrapping chat input (composer) ----
bool rewrapInPlace(char* buf, int len, float wrapW);
// rightReserve narrows the widget (and its wrap width) so composer overlays anchored at the
// input's right edge — the in-box send button — never sit over the text.
bool drawChatInput(const char* id, std::vector<char>& buf, const char* placeholder,
                   int maxLines, float rightReserve = 0.0f);

// ---- chat copy/paste affordances (ui_common) ----
extern bool gShowThinking;   // session-only "Show assistant thoughts" toggle (not persisted)
void        chatDrawCopyFlash();
std::string chatInputSelection(ImGuiID id);
void        chatCopyContext(const ImRect& rect, const char* popupId,
                            const std::string& copyText,
                            const std::string& copyAll = {},
                            const std::string& selText = {});
// `keywords`: magic-keyword byte ranges into `plain` to paint with their static gradient
// (sent user bubbles — the composer animates the same words; see chatPaintMagicKeywords).
ImGuiID     chatDrawSelectableText(const char* id, const std::string& plain,
                                   const ImVec4& col,
                                   const std::vector<ui::copilot::KeywordRange>* keywords = nullptr);
// Recolor the magic-keyword glyphs of the InputTextMultiline submitted IMMEDIATELY before this
// call, in place: the widget's own glyph quads get the keyword's hue gradient (vertex colors,
// alpha kept), so there is no second image to hide and selection/caret drawing is untouched.
// `shown` is the exact buffer the widget displayed and `ranges` index into it. `shimmer` < 0
// paints the static gradient; a phase in [0,1) slides the gradient and sweeps a glint across the
// word. `glow` adds a soft hue wash behind each visible word. Returns true when it painted.
bool        chatPaintMagicKeywords(std::string_view shown,
                                   const std::vector<ui::copilot::KeywordRange>& ranges,
                                   float shimmer, bool glow);
// Text with a travelling light sweep (the "working" label): `text` in `base` ink with a soft
// band of `glint` ink moving across it once per `period` seconds. Advances the cursor like
// ImGui::TextUnformatted; requests frames (it is ambient motion — only call it while shown).
void        chatShimmerText(const char* text, const ImVec4& base, const ImVec4& glint,
                            float period);
// Narrowest wrap width at which chatDrawSelectableText keeps every hard line of `text` whole
// (measured exactly as its soft-wrapper measures) — for shrink-to-fit bubbles.
float       chatSoftWrapNaturalWidth(const std::string& text);
void        chatHoverCopyGlyph(const ImRect& rect, const std::string& text);
std::string chatActivityCopyText(const ActivityItem& a);

// ---- plan progress one-liner (cards) ----
std::string planProgressLine(const PlanState& plan,
                             const std::vector<ActivityItem>& activity);

// ---- corporate chart field helpers (feed; shared with the corporate cards in cards.cpp) ----
std::string    corpShortId(const std::string& id);
int            corpWorkerCount(const nlohmann::json& dept);
std::string    corpDeptHead(const nlohmann::json& dept);
std::string    corpDeptLead(const nlohmann::json& dept);
std::string    corpDeptNameOf(const nlohmann::json& dept);
nlohmann::json corpParseChart(const ActivityItem& a);

// ---- activity feed finalize + chat-history dirty tracking (feed) ----
void finalizeThoughtBox(ActivityItem& a);
void markChatHistoryDirty();
extern std::atomic<bool> s_chatHistoryDirty;
extern double            s_chatHistoryLastPersist;
// Batch `batchId` anywhere in `msgs` (newest message first); `owner` receives its message.
AskBatch* findAskBatch(std::vector<ChatMsg>& msgs, const std::string& batchId,
                       ChatMsg** owner = nullptr);
// Restored turns saved before question markers existed: anchor each unmarked ask_user batch
// right after its ask_user tool row so the card draws where the agent asked.
void anchorLegacyAsks(ChatMsg& m);


namespace ui {
// ---- append-only lossless main-chat archive (conversations) ----
// Message construction sites call appendChatRawMessage after insertion. feedTurnActivity wraps
// its one folding seam in ChatRawActivityCapture, which records every added or changed row.
void appendChatRawMessage(AppState& st, const ChatMsg& message) noexcept;
class ChatRawActivityCapture {
public:
    explicit ChatRawActivityCapture(ChatMsg& message) noexcept;
    ~ChatRawActivityCapture();

    ChatRawActivityCapture(const ChatRawActivityCapture&) = delete;
    ChatRawActivityCapture& operator=(const ChatRawActivityCapture&) = delete;

private:
    ChatMsg& message_;
    std::string conversationId_;
    std::vector<std::string> before_;
};

// ---- model label / token formatting (ui_common) ----
std::string shortModelName(const std::string& id);
std::string fmtTokens(int n);
int         effectiveContextWindow(const Settings& s);

// ---- copilot MENU chrome (ui_common) — Sessions / Tasks / Artifacts / Quick settings --------
// One title/section/label treatment for all four header views (they share ONE scroll region,
// so they must share one rhythm). Sizes from ui::metrics::cpMenu*; tones from chatSurf().
void cpMenuHeader(const ui::Fonts& fonts, const char* title, const char* subtitle = nullptr,
                  const std::function<void()>& accessory = nullptr);
void cpMenuSection(const char* label, int count = -1);
void cpMenuLabel(const char* label);
void cpMenuHint(const char* text);

// ---- activity timeline (activity_timeline) ----
// `srcOf` (optional): per output byte, the index of the input byte it came from.
std::string stripMdMarks(const std::string& in, std::vector<size_t>* srcOf = nullptr);
// Truncate to fit `maxW` in the CURRENT font, appending "…" when it does not (UTF-8-safe).
// The one truncation primitive for chat chrome — call it inside the PushFont whose metrics
// the label will be drawn with. Returns the input unchanged when it already fits.
std::string chatEllipsize(const std::string& s, float maxW);
// The transcript's one disclosure chevron (activity_timeline.cpp): a stroked right-pointing
// chevron rotating to point down as `openT` goes 0 -> 1. Shared with chat_log.cpp.
void chatPaintDisclosure(ImDrawList* dl, ImVec2 c, float r, float openT, ImU32 col);
// Fence-aware assistant prose: ``` spans render via drawCodeBox, everything else through
// stripMdMarks + chatDrawSelectableText. Returns the last prose segment's InputText id.
ImGuiID     drawMdProse(const char* id, const std::string& in, const ImVec4& col, ImFont* mono);
// Horizontal inset of the assistant's prose = the timeline rows' text column (rail + row pad).
float       chatProseInsetX();
void        drawActionCard(ActivityItem& a, const ui::Fonts& fonts, bool liveFrontier,
                           bool onSpine);
void        chatArtifactReveal(AppState& st, const std::string& path);
void        chatArtifactOpenSmart(AppState& st, const std::string& path);
void        chatArtifactActions(AppState& st, const std::string& path);
void        drawToolCallRow(AppState& st, ActivityItem& a, const ui::Fonts& fonts);
void        drawAgentTimeline(AppState& st, ChatMsg& m, const ui::Fonts& fonts, bool showThinking,
                              bool isLastTurn = false);
// ---- transcript scroll anchors (activity_timeline) ----
// The ONE anchor mechanism shared by the run receipt's failure list and the cost-attribution
// strip: both need "take me to that action card". Keyed the way the timeline keys its rows —
// activityAnchorId() must be called inside the SAME ImGui id scope drawAgentTimeline runs in
// (chat_log pushes the message index; the id below adds the activity index), so a request made
// from the turn footer resolves to the row the timeline will draw.
//
// The scroll is DEFERRED, not applied at click time: the render-windowing cache
// (ChatMsg::uiCachedH) means the target row may not have been submitted yet this frame. The
// request survives until the row draws, which is also what makes it correct for a message that
// is currently collapsed to a spacer.
ImGuiID     activityAnchorId(int activityIdx);
void        timelineScrollTo(ImGuiID anchor);
// True when a FAILED tool row is the harness's own loop-guard refusing a repeated call rather
// than a tool that ran and broke. The run receipt counts those separately — a turn that burned
// six calls on refusals must not read like a turn that did six units of work.
bool        activityIsGuardRefusal(const ActivityItem& a);
// The FINISHED turn's footer: a one-line receipt ("14 tool calls · 2 retried · 1 skipped · 3 files
// written · 41 s · 12.4k in / 3.1k out") that expands to the failures, the files the turn wrote,
// and a per-stage cost-attribution strip. It REPLACES the old plain "N tokens in / M out" line —
// those numbers are the receipt headline's last clause. Must be called from the message id scope
// (chat_log's per-message PushID) so its scroll anchors resolve to the timeline's own rows.
void        drawTurnReceipt(AppState& st, ChatMsg& m);
// Doc-id + atlas-glyph fold for agent-authored text, gated on ui::chatHumanizeDocIds().
// `jsonBody` additionally strips the resolved-id parenthetical (tool-row args blobs).
// Idempotent, with a no-allocation fast path.
std::string sanitizeAgentText(AppState& st, const std::string& s, bool jsonBody = false);
// Same, in place, with an allocation-free pre-scan (ui::agentTextMayChange). Prefer this
// anywhere the string is re-sanitized every frame.
void        sanitizeAgentTextInPlace(AppState& st, std::string& s, bool jsonBody = false);
// A turn renders as ONE ordered column: the model's prose is a row in the timeline, drawn where
// it arrived, not a card appended after everything else. These let chat_log tell what the
// timeline already showed so nothing prints twice.
bool        isProseOutputRow(const ActivityItem& a);
bool        timelineHasInlineProse(const ChatMsg& m);
std::string timelineInlineProse(const ChatMsg& m);
// m.text minus the prose the timeline drew inline: a whole reply that never streamed, or a
// note appended after the fact ("(Stopped by you.)"). Empty when the timeline covered it all.
std::string timelineProseTail(const ChatMsg& m);

// Packed tool-row detail: feedTurnActivity writes args_preview, then the capped failure text
// after "\n\x1eERR\x1e\n", the image caption after "\n\x1eCAP\x1e\n", artifact paths after
// "\n\x1eART\x1e\n", and a per-call META trailer after "\n\x1eMETA\x1e\n" ("<epoch-ts>\t
// <cache-read-tokens>" — the timestamp + prompt-cache read of the completion that produced the
// call). ActivityItem gains no fields — app_state.h stays unchanged. chatActivityCopyText
// (ui_common) splits the same way so a clipboard copy never carries the raw \x1e marker bytes.
struct ToolRowDetail {
    std::string args;
    std::string err;
    std::string cap;     // CAP trailer: the text the tool returned beside its images
    std::string arts;
    double ts = 0.0;     // META trailer: when the call ran (epoch s); 0 = unknown
    int cacheRead = 0;   // META trailer: tokens the producing completion read from cache
};
ToolRowDetail splitToolRowDetail(const std::string& detail);

// "12.3k" / "482" token counts — shared by the tool-row cache chip and the
// assistant bubble's meta row (both in k-token prompt-cache readouts).
std::string chatTokCount(int n);

// ---- run launch helpers (composer; shared with the clarify/plan-approve cards) ----
nlohmann::json agentProviderOverride(const Settings& s);
// Launch ONE Chudcode general_agent turn for `request` on the active conversation. Never creates
// or replaces a document — whether to new_part or edit the active doc is the agent's decision.
// `extra` is merged on top of the provider override (attachments / context_refs / options…);
// extra.options.mode defaults to the composer's current mode name ("agent"|"plan"|"ask").
void launchAgentTurn(AppState& st, const std::string& request,
                     nlohmann::json extra = nlohmann::json::object());
// Send an ask_user answer (or a dismissal) for `batchId` over the agent control lane. Off the GL
// thread; the result lands back on the batch through the composer's per-frame drain (composer).
void agentAnswerDispatch(AppState& st, const std::string& runId, const std::string& batchId,
                         nlohmann::json answers, bool declined);

// ---- cards (cards) ----
void drawClarifyCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts);
void drawPlanApproveCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts);
// The online agent's ask_user question batches on this turn: interactive while pending,
// a compact read-only summary once resolved (cards.cpp). drawAskCards draws only batches with
// no timeline marker (turns saved before markers existed); a marked batch draws at its marker
// through drawAskMarker, i.e. where the agent asked.
void drawAskCards(AppState& st, ChatMsg& m, const ui::Fonts& fonts);
void drawAskMarker(AppState& st, ChatMsg& m, const ActivityItem& marker, const ui::Fonts& fonts);
// ActivityItem::stage of batch `batchId`'s rowKind-20 timeline marker.
std::string askMarkerStage(const std::string& batchId);
void drawCorpOrgCard(ActivityItem& a, const ui::Fonts& fonts);
void drawCorpMeetingCard(ActivityItem& a, const ui::Fonts& fonts);
void drawCorpSteeringCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawCorpMutedLine(const ActivityItem& a, const ui::Fonts& fonts);
void drawDelegationCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawSwarmWaveCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmBidsCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmAwardCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmTeamCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmCommCard(ActivityItem& a, const ui::Fonts& fonts);
void drawAdvisorCard(ActivityItem& a, const ui::Fonts& fonts);
// `live`: the row is running inside a running turn — shimmer + elapsed instead of a static title.
void drawSubRunCard(const ActivityItem& a, const ui::Fonts& fonts, bool live);

// ---- AI-VC (agent version control) state + cards (cards) ----
struct AiVcCheckpoint {
    std::string id;
    int         turn = -1;     // agent turn index, or -1 if unset
    std::string when;          // ISO timestamp
    std::string label;         // tool/op-batch summary
    int         features = 0;  // feature count at checkpoint
    // v2 prompt/tool granularity (absent => -1 / empty → turn-level UI only)
    std::string kind;          // "prompt" | "tool" | ""
    int         msgIdx = -1;   // chat message ordinal for kind=prompt
    int         callTurn = -1; // call.turn for kind=tool
    int         callI = -1;    // call.i for kind=tool — 0-based tool ordinal WITHIN a run,
                               // so it only identifies a call together with sessionId.
    std::string sessionId;     // run that authored this checkpoint (call.i restarts per run)
};

struct AiVcUi {
    bool available = true;     // false after unknown-method (hide all AI-VC chrome)
    bool loading   = false;
    bool busy      = false;
    std::string err;
    std::vector<AiVcCheckpoint> checkpoints;
    double      lastFetch = 0.0;
    std::string lastDocId;
    // v2 capability flags — latched from the last successful timeline payload.
    bool hasMsgGranularity  = false;  // any row carried msg_idx
    bool hasCallGranularity = false;  // any row carried call:{turn,i}
    // Inline confirm (click again) for per-turn / per-msg / per-call restore — never a modal.
    int    confirmMsgIdx = -1;  // chat index pending confirm, -1 = none
    int    confirmKind   = 0;   // 1 = before, 2 = after, 3 = user-msg rewind
    int    confirmCallI  = -1;  // tool-call index within turn when confirmKind is call-scoped
    double confirmUntil  = 0.0; // auto-clear after a few seconds

    // Pre-rollback confirmation dialog (the warning ladder). Filled by the
    // plm_rollback_check preflight RPC, shown in ONE modal before aiVcRollback runs.
    struct Confirm {
        bool open = false;          // request the modal next frame
        bool busy = false;          // preflight RPC in flight
        std::string to;             // rollback target awaiting confirmation
        std::string toast;          // success toast after restore
        // Preflight result (ok==false -> err/notFound describe why).
        bool ok = false;
        bool notFound = false;      // no checkpoint at that point (chat-only rewind)
        std::string err;
        std::string cpLabel;
        std::string cpWhen;
        int discardedCheckpoints = -1;  // -1 = unknown
        int discardedTurns = -1;
        int discardedMessages = -1;
        bool geometryRollback = false;
        // Backing-file provenance (externallyModified: -1 unknown, 0 no, 1 yes).
        std::string filePath;
        bool provenanceKnown = false;
        int  externallyModified = -1;
        double recordedMtime = 0.0;
        double currentMtime = 0.0;
    } confirm;
};
extern AiVcUi s_aiVc;

// Staged "Edit message" — a conversation-scoped transaction. Nothing in the transcript moves
// while the edit is staged: the tail [msgIdx..) is only HIDDEN by the chat log (and only once
// the conversation's run is truly over), so a running reply, the run's chat indices and the
// persisted history are never touched by staging. Esc / switching sessions just drops the
// stage and restores the composer.
// Sending runs, strictly in order and never concurrently with a turn:
//   1. wait for the conversation's run to reach its actual terminal (editing during a run
//      stops it first);
//   2. chudcode_rewind_session — branch the runtime session to before the edited message;
//   3. plm_rollback msg:<idx> — geometry (a missing checkpoint is a legitimate chat-only edit);
//   4. commit: adopt the branch link, park the old tail as a ChatBranch, truncate, persist;
//   5. exactly one submitTurn of the edited text (deferred until the conversation is viewed).
// Any failure before 4 adopts nothing, keeps the tail and the edited composer text, launches no
// run and says why.
struct EditStage {
    enum class Phase {
        Off,
        Staged,       // composer prefilled; sendRequested = waiting for the run to settle
        Rewinding,    // chudcode_rewind_session in flight
        RollingBack,  // plm_rollback msg:<idx> in flight
        AwaitSubmit,  // committed; edited turn launches when its conversation is viewed
    };
    Phase phase = Phase::Off;
    uint64_t gen = 0;             // transaction generation; stale callbacks compare it
    std::string convId;           // the conversation being edited — never "whatever is viewed"
    int  msgIdx = -1;             // edited user message index in that conversation
    std::string targetText;       // typed text of the edited message (runtime match key)
    int  occurrence = 0;          // earlier matching user messages (runtime match ordinal)
    std::string savedPrompt;      // composer contents before edit (Esc restores)
    std::vector<std::string> savedAttachments, savedImages;
    std::vector<core::ContextRef> savedContextRefs;
    bool sendRequested = false;   // Enter pressed; transaction starts once the run settled
    std::string editedText;       // composer text frozen at send
    std::vector<std::string> editedAttachments, editedImages;
    std::vector<core::ContextRef> editedContextRefs;
    std::string docId;            // document whose AI-VC chain rolls back
    std::string newSessionId;     // rewind result, adopted only at commit
    std::string newSessionFile;
    bool geometryRolledBack = false;
};
extern EditStage s_edit;

bool chatMsgMutatedDoc(const ChatMsg& m);
void aiVcRollback(AppState& st, const std::string& to, const std::string& okToast);
// Request a rollback: runs the plm_rollback_check preflight, then shows the
// single confirmation dialog (warning ladder) before anything destructive runs.
void aiVcRequestRollback(AppState& st, const std::string& to, const std::string& okToast);
// Draw the confirmation modal; call once per frame inside the chat window.
void drawAiVcRollbackConfirm(AppState& st);
// Checkpoint coverage for a msg:<idx> rewind target: 0 = no geometry checkpoint exists
// for that message (chat-only rewind), 1 = a checkpoint exists (geometry rolls back too),
// 2 = unknown (timeline not loaded yet — kicks a throttled fetch).
int aiVcMsgCheckpointState(AppState& st, int msgIdx);
// Shared wording so the user-message rewind and the assistant chat-only rewind never
// diverge: the tip is shown BEFORE the click, the toast AFTER the rewind.
const char* aiVcRewindTip(int cpState);
std::string aiVcRewindToast(int cpState);
void drawDoubleRewindGlyph(ImDrawList* dl, const ImVec2& mn, const ImVec2& mx,
                           ImU32 col);
void drawPencilGlyph(ImDrawList* dl, const ImVec2& mn, const ImVec2& mx, ImU32 col);
// Header navigation: submit native hit zones, resolve clicks, then paint the final selection.
struct HeaderBtnHit {
    ImRect rect;
    bool   clicked = false;
    bool   hovered = false;
    bool   held    = false;
};
HeaderBtnHit headerTextButtonHit(const char* id, float w);
// Flat at rest; `withLabel=false` centres the icon and the caller provides its named tooltip.
void headerTextButtonPaint(const HeaderBtnHit& h, const char* label, bool active,
                           const char* iconName = nullptr, bool withLabel = true);
// ---- staged edit transaction (cards.cpp; see EditStage) ----
bool editStageInFlight();                    // rewind/rollback RPC outstanding (not cancellable)
// The stage belongs to convId: its queue drain must not launch a turn under the edit.
bool editStageHoldsConversation(const std::string& convId);
// First message index the chat log hides for the viewed conversation, or -1. Only a staged
// edit of a SETTLED conversation hides anything — a running tail always stays visible.
int  editStageHideFrom(AppState& st);
// Start editing user message msgIdx of the viewed conversation: prefill the composer. When
// that conversation's run is live this also stops it (Stop-and-edit); nothing is hidden until
// the run's actual terminal.
void beginEditStage(AppState& st, int msgIdx);
// Esc: drop the stage and restore the composer (staged only — an in-flight transaction cannot
// be abandoned halfway). Returns false when it refused.
bool cancelEditStage(AppState& st);
// Called before the viewed conversation is parked (switch / new session): a merely staged edit
// is cancelled while its conversation is still the viewed one, so its composer text and draft
// are restored; an in-flight or committed transaction keeps running for ITS conversation.
void editStageBeforeConversationPark(AppState& st);
// Enter while staged: freeze `text` and start the transaction once the run has settled.
void editStageSubmit(AppState& st, const std::string& text);
// Per-frame driver (composer): starts a requested transaction once the run settled, and
// launches a committed edit when its conversation is the viewed one.
void editStageTick(AppState& st);
// One-line composer status for the viewed conversation's edit, or nullptr.
const char* editStageStatus(AppState& st);
// RPC completions of the two transaction steps. Public so a harness can drive the exact
// transaction with synthetic replies; a stale gen is ignored.
void editStageOnRewindResult(AppState& st, uint64_t gen, const core::RpcResult& r);
void editStageOnRollbackResult(AppState& st, uint64_t gen, const core::RpcResult& r);
// Restore/rewind actions for an assistant turn. alwaysShow=true draws them as a bottom
// action row (the chat-log default, matching the user bubble's own meta bar); false keeps
// the hover-latched variant that anchors on a preceding label item.
void drawAiVcTurnRestore(AppState& st, int msgIdx, int turnNum, bool alwaysShow = true);
// Per-tool-call "before" rewind glyph, drawn on each tool-call row. Draws nothing unless
// exactly one checkpoint matches (call ordinal + tool name), so it never offers a restore
// the backend would refuse or, worse, resolve to the wrong state. There is no "after"
// counterpart by design — "after call i" is "before call i+1", so before-only already
// reaches every point in the turn.
void drawAiVcCallRewind(AppState& st, int callI, const std::string& toolName);
std::string aiVcCallCheckpointId(int callI, const std::string& toolName);

// ---- chat log (chat_log) ----
void drawUserBlock(AppState& st, const ChatMsg& m, const ui::Fonts& fonts,
                   float panelW, int msgIdx);
void drawChatLogWindowed(AppState& st);
// Drop stick-to-bottom on EVERY chat surface. A deferred scroll-to-anchor lands one frame after
// the click; chatLogStickToBottom runs after the log every frame and would snap straight back to
// the tail, so the jump has to un-stick first.
void chatLogBreakStick();

// ---- conversations + persistence (conversations) ----
// Sessions are first-class + document-independent (v2 flat store); Conversation::docLink is
// only a reference. st.chatDocKey = the CURRENT document's key (run launches, prompt drafts).
std::string chatHistoryPath();
std::string chatSessionsDir();
void        savePromptDraft(AppState& st, bool force = false);
void        clearPromptDraft(AppState& st);
std::string convDocKey(AppState& st);
std::string newConvId(AppState& st);
// Project scoping (conversations). A session starts under the current project but follows
// the workspace used by its latest launched turn; Sessions filters on this stored truth.
// lexicalPath/comparablePath are the ONE normalization used for every path comparison in the
// copilot (project keys AND artifact paths).
std::filesystem::path lexicalPath(std::string_view raw);
std::string comparablePath(const std::filesystem::path& path);
std::string convProjectKey(std::string_view rawDir);   // comparable key for a project folder
std::string convProjectDir(AppState& st);              // the selected root (st.workspaceDir) only
void        bindActiveConversationProject(AppState& st, const std::string& workspace);
void        ensureConversations(AppState& st);
void        maybePersistChatHistoryDebounced(AppState& st, bool force);
void        loadChatHistory(AppState& st);
void        syncChatToDoc(AppState& st);
void        parkActiveConversation(AppState& st);
bool        activeConversationRunning(AppState& st);
void        newConversation(AppState& st);
void        switchConversation(AppState& st, int idx);
void        closeConversation(AppState& st, int idx);
void        archiveConversation(AppState& st, int idx, bool archived);
int         forkConversation(AppState& st, int idx);   // -> new conversation index, or -1
// Chudcode session-recovery service latch (conversations): true until the first
// unknown-method response proves the worker slice absent; the Sessions panel and the
// post-turn health capture both honor (and may flip) it.
bool&       chudcodeSessionServiceAvailable();
// Sessions list scope as last drawn (scenario probe): the project key it shows (empty for
// Unassigned/All) and the root key it last snapped to. `drawn` = the list has drawn at least once.
struct SessionsScopeProbe {
    std::string viewedKey, followedKey;
    bool        drawn = false;
};
SessionsScopeProbe sessionsScopeProbe();

// ---- AI settings (settings) ----
// Compact AI-settings popover body (provider + model + effort + run toggles + "All settings...");
// hosted by the header Settings button.
void drawAISettingsBody(AppState& st, bool busy);
void drawModelDropdown(AppState& st, bool busy);

// ---- user-facing service errors (shared by every copilot / AI-workspace control) ----
// A running agent turn holds the agent service lane; calls queued behind it fail fast with a
// transient, internal code. Controls that refused a user action say when it comes back instead.
inline constexpr const char* kAvailableAfterRun = "Available when the current run finishes.";
// The ONE mapping from a failed service call to text a user may see: the busy lane becomes
// kAvailableAfterRun; a message naming internals (worker, lane, RPC, raw method ids, tracebacks)
// collapses to `fallback`, which callers write in user terms. Only the first line is kept.
inline std::string userFacingRpcError(const core::RpcResult& r, const char* fallback) {
    if (core::workerLaneBusy(r)) return kAvailableAfterRun;
    std::string msg = r.err;
    if (msg.empty() && r.result.is_object()) {
        for (const char* key : {"reason", "message", "error"}) {
            const auto it = r.result.find(key);
            if (it != r.result.end() && it->is_string() &&
                !it->get_ref<const std::string&>().empty()) {
                msg = it->get<std::string>();
                break;
            }
        }
    }
    if (const size_t nl = msg.find('\n'); nl != std::string::npos) msg.resize(nl);
    if (msg.empty()) return fallback;
    std::string low = msg;
    for (char& c : low) c = (char)std::tolower((unsigned char)c);
    for (const char* marker : {"worker", "lane", "rpc", "hostd", "method", "traceback",
                               "exception", "config.json", "errno", "json"})
        if (low.find(marker) != std::string::npos) return fallback;
    // snake_case identifiers (session_btw, rule_create, ...) are wire names, not user copy.
    for (size_t i = 1; i + 1 < low.size(); ++i)
        if (low[i] == '_' && std::isalpha((unsigned char)low[i - 1]) &&
            std::isalpha((unsigned char)low[i + 1]))
            return fallback;
    return msg;
}

// ---- composer + send path (composer) ----
std::string composerText(const AppState& st);
void drawBtwPanel(AppState& st);
void submitTurn(AppState& st, const std::string& text);
// Consume only referenced inline images, compact their numbering, and preserve ordinary files.
nlohmann::json consumeComposerAttachments(AppState& st, std::string& requestText,
                                           std::string* bubbleText = nullptr);
void drawRuntimeBanner(AppState& st);
// Queue a steer (non-empty `steer`) or a cancel (empty) for the active run on the agent control
// channel — the one path that reaches a running turn while it holds the agent lane.
// `steerId` names the steer's user bubble so its delivery state can follow (empty = none).
void agentControlDispatch(AppState& st, const std::string& runId, const std::string& steer,
                          nlohmann::json attachments = nlohmann::json::array(),
                          const std::string& steerId = std::string(),
                          const std::string& convId = std::string());
bool aiTurnActive(AppState& st);
// Stop the VIEWED conversation's run (agent_cancel + soft-finalize its bubble). The run's
// JobState stays running until the worker's actual terminal drains.
void requestStopRun(AppState& st);
// Replace the composer text from outside the InputText (fresh widget state so an active
// widget does not keep its stale buffer). Empty text also drops the session's saved draft.
void composerSetText(AppState& st, const std::string& text);
// Reset the restored buffer's widget/caret/hard-break state without deleting a saved draft.
void composerResetInputState(AppState& st);
void drawComposer(AppState& st, bool busy, bool canRun);

// ---- sessions/tasks/artifacts panels (panel) ----
void drawSessionsPanel(AppState& st, bool busy);

}  // namespace ui
