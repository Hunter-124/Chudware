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
//   cards.cpp              plan/objectives section, plan-approve card,
//                          exec-mode (swarm/eng-firm/advisor) cards, AI-VC checkpoint UI
//   ask_answers.cpp        GL-free ask_user question batches (AskBatch): suggested-default prefill,
//                          RAW `answer` JSON per kind, session-file form (cards.cpp draws them)
//   feed.cpp               feedTurnActivity: stream-event -> ActivityItem folding (orchestrator
//                          events, ops-JSON detection), chat-history dirty flag
//   conversations.cpp      conversation tabs + chat-history/prompt-draft persistence (incl.
//                          WASM shared-chat sync), chat-triggered OpenFOAM poll
//   settings.cpp           AI settings sections (provider/model/auth, generation, privacy,
//                          manage-models), searchable model dropdown, provider usability
//   composer.cpp           wrapping chat input, attachments/context refs, run launch, slash
//                          commands + slash-menu autocomplete (session cmds + skills),
//                          submitTurn send path, composer prelude/toolbar/send-stop
//   pending_stack.cpp      the pinned stack closing the chat log: pending steers (sent, unread) and
//                          queued messages (edit / send now / cancel back to the composer)
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
// copilot: the plan checklist and the agent overview both read it.
ui::kit::Status chatRowStatus(const std::string& status);
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
// For a chat input other than the composer's (the queued-message editor): fill its buffer for a
// FRESH widget, and read back what the user wrote (soft wraps flattened, typed breaks kept).
void chatInputLoad(std::vector<char>& buf, const std::string& text);
std::string chatInputText(const std::vector<char>& buf);

// ---- composer spell check + word completion (composer_spell) ----
// Load the word lists on first use and keep the completion vocabulary in step with the active
// conversation. Once per frame, before the composer input draws.
void composerSpellRefresh(AppState& st);
// The completion drawn at the caret last frame ("" = none); Tab inserts it.
const std::string& composerSpellGhost();
// A correction picked from the right-click menu: replace bytes [begin, end) of the buffer.
struct ComposerSpellFix {
    int begin = 0, end = 0;
    std::string text;
};
// Right after the composer's InputTextMultiline (it must be the last item): squiggle the
// misspelled words, draw the ghost completion when `allowGhost`, run the right-click
// correction menu. True when the user picked a correction (`fix` filled).
bool composerSpellPaint(ImGuiID wid, const std::vector<char>& buf, const std::vector<int>& hard,
                        bool allowGhost, ComposerSpellFix& fix);

// ---- chat copy/paste affordances (ui_common) ----
extern bool gShowThinking;   // session-only "Show assistant thoughts" toggle (not persisted)
void        chatDrawCopyFlash();
std::string chatInputSelection(ImGuiID id);
void        chatCopyContext(const ImRect& rect, const char* popupId,
                            const std::function<std::string()>& copyText,
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
void        chatHoverCopyGlyph(const ImRect& rect, const std::function<std::string()>& text);
std::string chatActivityCopyText(const ActivityItem& a);

// ---- plan progress one-liner (cards) ----
std::string planProgressLine(const ChatMsg& message);

// ---- activity feed finalize + chat-history dirty tracking (feed) ----
void finalizeThoughtBox(ActivityItem& a);
void markChatHistoryDirty();
extern std::atomic<bool> s_chatHistoryDirty;
extern std::atomic<uint64_t> s_chatHistoryRevision;
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
    explicit ChatRawActivityCapture(ChatMsg& message, bool appendOnly = false) noexcept;
    ~ChatRawActivityCapture();

    ChatRawActivityCapture(const ChatRawActivityCapture&) = delete;
    ChatRawActivityCapture& operator=(const ChatRawActivityCapture&) = delete;

private:
    ChatMsg& message_;
    std::string conversationId_;
    size_t messageIndex_ = 0;
    std::vector<uint64_t> before_;
    bool appendOnly_ = false;
};

// ---- model label / token formatting (ui_common) ----
std::string shortModelName(const std::string& id);
std::string fmtTokens(int n);
int         effectiveContextWindow(const Settings& s);

// ---- transcript clock (chat_log) ----
// "just now", "3m ago", "2h ago", "4d ago"; empty for an unknown (<= 0) stamp.
std::string chatAgeLabel(double ts);
// Local wall clock with seconds ("2:26:07 PM"); empty for an unknown stamp.
std::string chatClockSeconds(double ts);
// "3m ago · Sep 27, 2026, 2:26:07 PM" — the hover twin of a visible stamp.
std::string chatTimeTooltip(double ts);

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
// Horizontal inset of the assistant's prose = the timeline rows' text column (the row pad).
float       chatProseInsetX();
void        drawActionCard(ActivityItem& a, const ui::Fonts& fonts, bool liveFrontier);
void        chatArtifactReveal(AppState& st, const std::string& path);
void        chatArtifactOpenSmart(AppState& st, const std::string& path);
void        chatArtifactActions(AppState& st, const std::string& path);
// One "<kind>\t<path>" line of an ART blob. False for the trailing META/stamp lines the feed
// appends and for corrupted continuation lines, so every artifact collector agrees.
bool        artifactLine(const std::string& line, std::string* kindOut, std::string* pathOut);
void        drawToolCallRow(AppState& st, ActivityItem& a, const ui::Fonts& fonts);
void        drawAgentTimeline(AppState& st, ChatMsg& m, const ui::Fonts& fonts, bool showThinking);
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
const std::string& timelineInlineProse(const ChatMsg& m);
// m.text minus the prose the timeline drew inline: a whole reply that never streamed, or a
// note appended after the fact ("(Stopped by you.)"). Empty when the timeline covered it all.
const std::string& timelineProseTail(const ChatMsg& m);

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

// ---- run launch helpers (composer; shared with the plan-approve card) ----
nlohmann::json agentProviderOverride(const Settings& s);
nlohmann::json composerTurnPayload(const Settings& s, const nlohmann::json& extra);
// Launch ONE Omp general_agent turn for `request` on the active conversation. Never creates
// or replaces a document — whether to new_part or edit the active doc is the agent's decision.
// `extra` is merged on top of the provider override (attachments / context_refs / options…);
// extra.options.mode defaults to the composer's current mode name ("agent"|"plan"|"ask").
void launchAgentTurn(AppState& st, const std::string& request,
                     nlohmann::json extra = nlohmann::json::object());
// Send an ask_user answer (or a dismissal) for `batchId` over the agent control lane. Off the GL
// thread; the result lands back on the batch through the composer's per-frame drain (composer).
void agentAnswerDispatch(AppState& st, const std::string& runId, const std::string& batchId,
                         nlohmann::json answers, bool declined);
// Queue `b`'s answers as the active conversation's next message (askAnswersMessage) when no
// run is left to take them over the control lane. The composer's staged attachments stay put.
void queueAskAnswersTurn(AppState& st, const AskBatch& b);

// ---- cards (cards) ----
void drawPlanApproveCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts);
// The online agent's ask_user question batches on this turn: interactive while pending,
// a compact read-only summary once resolved (cards.cpp). drawAskCards draws only batches with
// no timeline marker (turns saved before markers existed); a marked batch draws at its marker
// through drawAskMarker, i.e. where the agent asked.
void drawAskCards(AppState& st, ChatMsg& m, const ui::Fonts& fonts);
void drawAskMarker(AppState& st, ChatMsg& m, const ActivityItem& marker, const ui::Fonts& fonts);
// ActivityItem::stage of batch `batchId`'s rowKind-20 timeline marker.
std::string askMarkerStage(const std::string& batchId);
// Inter-agent message (rowKind 7): OMP's IRC card — header with sender, recipient and age over
// the body's first lines on a quote gutter; click expands the whole body.
void drawAgentMessageLine(const ActivityItem& a, const ui::Fonts& fonts);
// One finished provider request (rowKind 21): time, elapsed since the prompt, tokens in/out,
// prompt-cache reads, time to first token and output rate, as one dim line.
void drawUsageLine(const ActivityItem& a, const ui::Fonts& fonts);
void drawDelegationCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawSwarmWaveCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmBidsCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmAwardCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmTeamCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmCommCard(ActivityItem& a, const ui::Fonts& fonts);
void drawAdvisorCard(ActivityItem& a, const ui::Fonts& fonts);
// `live`: the row is running inside a running turn — shimmer + elapsed instead of a static title.
// `body` (optional) draws inside the card under the title: the calls this sub-agent made.
void drawSubRunCard(const ActivityItem& a, const ui::Fonts& fonts, bool live,
                    const std::function<void()>& body = nullptr);

// ---- AI-VC (agent version control) rollback (cards) ----
// Shared with the AI workspace's checkpoint strip (ai/plan_board.cpp); the chat transcript
// itself carries no restore controls.
struct AiVcUi {
    bool available = true;     // false after unknown-method (hide all AI-VC chrome)
    bool busy      = false;    // a plm_rollback is in flight
};
extern AiVcUi s_aiVc;

// Restore the active document to checkpoint `to` (a plm_timeline id). Geometry only — the chat
// is never touched — and refused while a run is live. Toasts the outcome.
void aiVcRollback(AppState& st, const std::string& to, const std::string& okToast);

// Header navigation: submit native hit zones, resolve clicks, then paint the final selection.
struct HeaderBtnHit {
    ImRect rect;
    ImGuiID id = 0;
    bool   clicked = false;
    bool   hovered = false;
    bool   held    = false;
};
HeaderBtnHit headerTextButtonHit(const char* id, float w);
// Rounded, theme-native chrome; outlined actions retain a quiet edge at rest.
// `withLabel=false` centres the icon and the caller provides its named tooltip.
// `ownActiveFill=false`: the caller paints the selected face (a sliding tab indicator); the
// button keeps only the active ink and hover wash.
void headerTextButtonPaint(const HeaderBtnHit& h, const char* label, bool active,
                           const char* iconName = nullptr, bool withLabel = true,
                           bool outlined = false, bool ownActiveFill = true);

// ---- chat log (chat_log) ----
void drawUserBlock(AppState& st, const ChatMsg& m, const ui::Fonts& fonts,
                   float panelW);
void drawChatLogWindowed(AppState& st);
void chatLogMessageChanged(const ChatMsg& message) noexcept;
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
const std::string& convProjectKey(const Conversation& conversation);
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
// Omp session-recovery service latch (conversations): true until the first
// unknown-method response proves the worker slice absent.
bool&       ompSessionServiceAvailable();
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
const std::string& composerText(const AppState& st);
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

// ---- queued messages + the pinned stack (composer queue ops, pending_stack UI) ----
// Retype one queued message (its attachment draft follows the index).
void setQueuedPromptText(AppState& st, size_t index, const std::string& text);
// Cancel: take a queued message out of the queue and hand it back to the composer, text and
// draft (attachments, inline images, context chips), merged with whatever the composer holds.
void cancelQueuedPromptToComposer(AppState& st, size_t index);
// Send now: the live run takes it as a steer when it accepts one, otherwise the message becomes
// the next to launch and the live run is stopped. Returns true when it went out as a steer.
bool queuedPromptSendNow(AppState& st, size_t index);
// Would Send now deliver as a steer (vs stop the run and launch it next)? For tooltips.
bool queuedSendNowSteers(AppState& st, size_t index);
// True while the user is editing a queued message in the pinned stack: the composer's queue
// drain waits so a message is never sent out from under its editor.
bool queuedPromptEditHoldsDrain();
// Open the pinned stack's in-place editor on queued message `index` (Enter saves, Esc reverts).
void beginQueuedPromptEdit(AppState& st, size_t index);
// Edit / Cancel on a pending steer (sent, not yet read): ask the run to take it back unread.
// Taken back, its bubble leaves the stack and the exact draft it was sent from heads the chat's
// queue, opened in the in-place editor (`edit`) or handed back to the composer (Cancel). If the
// agent already read it, the bubble stays and a toast says so.
void withdrawPendingSteer(AppState& st, const std::string& steerId, bool edit);
// A withdraw request for this steer is in flight.
bool steerWithdrawPending(const std::string& steerId);
// One withdraw outcome (the control channel's GL-thread drain; scenarios drive it directly).
// `error` names why the call failed or was refused; empty when the run simply kept it.
void applySteerWithdrawal(AppState& st, const std::string& convId, const std::string& steerId,
                          bool withdrawn, bool edit, const std::string& error);
// The pinned stack under the live turn: pending steers (sent, not yet read by the agent) and the
// queued messages (not yet sent), as dimmed bubbles with per-message actions (Edit / Cancel on
// both, Send now on queued ones). Drawn by the chat log INSIDE its scrolling child, after the
// last message; `width` is the log's width.
void drawPendingMessageStack(AppState& st, float width);
void drawComposer(AppState& st, bool busy, bool canRun);

// ---- sessions/tasks/artifacts panels (panel) ----
void drawSessionsPanel(AppState& st, bool busy);

}  // namespace ui
