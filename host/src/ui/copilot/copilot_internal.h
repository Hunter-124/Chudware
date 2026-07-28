// Copilot internals shared across the ui/copilot/* translation units (T-RF6 split of the old
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
#include <thread>
#include <map>
#include <vector>
#include <unordered_map>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect — Liquid Glass v2 host port (contract H5: glass bubbles)

#include "copilot.h"
#include "ui/layout.h"          // ui::overlayBand / takeOverlayColumnReset — pinned-column geometry
#include "ui/panel_collapse.h"  // ui::PanelCollapseAnim — shared vertical-collapse animation (T-G21)
#include "ui/doc_tabs.h"        // ui::docTabStripHeight — top inset so the column clears the tab band
#include "ui/model_registry.h"  // ui::ModelInfo / capability flags / provider mapping
#include "ui/metrics.h"         // ui::metrics::px/U/asmHeaderIconGap
#include "ui/console_panel.h"   // drawConsoleBody (shared console helper)
#include "ui/file_dialog.h"     // openFileDialog — composer paperclip attachments (M23)
#include "ui/icons.h"           // getIcon / IconUV — composer settings glyph

#include "core/app_state.h"
#include "ui/property_manager.h"  // Edit-mode scope: PM targetId
#include "core/cpp_core.h"        // detect the in-process C++ core (libchudware) backend
#include "core/sim_client.h"      // chat-triggered OpenFOAM CFD on the SimClient channel
#include "core/jobs.h"
#include "ui_theme.h"

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
ImU32  modeBorderColor(int mode);
void   modeAccentRgb(int mode, float out[3]);
void   drawStatusGlyph(const std::string& status, float box);
void   drawBusyMark(float box);
GlassBubble beginGlassBubble(float width, ui::GlassLevel level, float rounding);
ImRect  endGlassBubble(const GlassBubble& b);

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
ImGuiID     chatDrawSelectableText(const char* id, const std::string& plain,
                                   const ImVec4& col);
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

namespace ui {

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
void        drawAgentStep(AgentStep& s, const ui::Fonts& fonts, float logW);
std::string stripMdMarks(const std::string& in);
// Fence-aware assistant prose: ``` spans render via drawCodeBox, everything else through
// stripMdMarks + chatDrawSelectableText. Returns the last prose segment's InputText id.
ImGuiID     drawMdProse(const char* id, const std::string& in, const ImVec4& col, ImFont* mono);
void        drawActionCard(ActivityItem& a, AgentStep* step, const ui::Fonts& fonts, float logW,
                           bool liveFrontier = false);
void        chatArtifactReveal(AppState& st, const std::string& path);
void        chatArtifactOpenSmart(AppState& st, const std::string& path);
void        chatArtifactActions(AppState& st, const std::string& path);
void        drawToolCallRow(AppState& st, ActivityItem& a, const ui::Fonts& fonts);
void        drawAgentTimeline(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW,
                              bool showThinking);

// ---- run launch helpers (composer; shared with the clarify/plan-approve cards) ----
nlohmann::json agentProviderOverride(const Settings& s);
void launchDesignRun(AppState& st, const std::string& request,
                     nlohmann::json extra = nlohmann::json::object());
void launchMissionRunChat(AppState& st, const std::string& request, bool needFreshDoc,
                          nlohmann::json extra = nlohmann::json::object());

// ---- cards (cards) ----
void drawClarifyCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts);
void drawPlanApproveCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts);
void drawCorpOrgCard(ActivityItem& a, const ui::Fonts& fonts);
void drawCorpMeetingCard(ActivityItem& a, const ui::Fonts& fonts);
void drawCorpSteeringCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawCorpMutedLine(const ActivityItem& a, const ui::Fonts& fonts);
void drawClassificationCard(ActivityItem& a, const ui::Fonts& fonts,
                            bool turnRunning = false);
void drawDelegationCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawSwarmWaveCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmBidsCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmAwardCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmTeamCard(const ActivityItem& a, const ui::Fonts& fonts);
void drawFirmCommCard(ActivityItem& a, const ui::Fonts& fonts);
void drawAdvisorCard(ActivityItem& a, const ui::Fonts& fonts);
void drawSubRunCard(const ActivityItem& a, const ui::Fonts& fonts);

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
    int         callI = -1;    // call.i for kind=tool
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

// Staged "Edit message" — rollback is deferred until the user actually sends. Esc restores
// the pre-edit chat tail + composer without any plm_rollback.
struct EditStage {
    bool active = false;
    int  msgIdx = -1;
    std::string savedPrompt;                 // composer contents before edit
    std::vector<ChatMsg> chatTail;           // messages [msgIdx..) parked for Esc restore
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
bool drawHeaderTextButton(const char* id, const char* label, bool active,
                          float minW = 0.0f, const char* iconName = nullptr);
void cancelEditStage(AppState& st);
void beginEditStage(AppState& st, int msgIdx);
void commitEditStageOnSend(AppState& st);
// Restore/rewind actions for an assistant turn. alwaysShow=true draws them as a bottom
// action row (the chat-log default, matching the user bubble's own meta bar); false keeps
// the hover-latched variant that anchors on a preceding label item.
void drawAiVcTurnRestore(AppState& st, int msgIdx, int turnNum, bool alwaysShow = true);

// ---- chat log (chat_log) ----
void drawUserBlock(AppState& st, const ChatMsg& m, const ui::Fonts& fonts,
                   float panelW, int msgIdx);
void drawChatLogWindowed(AppState& st);

// ---- conversations + persistence (conversations) ----
// Sessions are first-class + document-independent (v2 flat store); Conversation::docLink is
// only a reference. st.chatDocKey = the CURRENT document's key (run launches, prompt drafts).
std::string chatHistoryPath();
void        savePromptDraft(AppState& st, bool force = false);
void        clearPromptDraft(AppState& st);
std::string convDocKey(AppState& st);
std::string newConvId(AppState& st);
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

// ---- AI settings (settings) ----
// Compact AI-settings popover body (provider + model + effort + run toggles + "All settings...");
// hosted by the header Settings button.
void drawAISettingsBody(AppState& st, bool busy);
void drawModelDropdown(AppState& st, bool busy);

// ---- composer + send path (composer) ----
void drawBtwPanel(AppState& st);
void submitTurn(AppState& st, const std::string& text);
void drawRuntimeBanner(AppState& st);
bool aiTurnActive(AppState& st);
void drawComposer(AppState& st, bool busy, bool canRun);

// ---- sessions/tasks/artifacts panels (panel) ----
void drawSessionsPanel(AppState& st, bool busy);

}  // namespace ui
