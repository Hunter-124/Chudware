// The command registry — the new, coherent command taxonomy (app/DESIGN.md §3).
//
// ONE data table re-homes every existing cmd_id into verb-based groups across
// doc-type-aware workspaces. The context toolbar, the ⌘K palette, the app menu,
// and right-click menus are ALL generated from this table (the old code defined
// the ribbon twice). cmd_ids are byte-for-byte the engine's existing ids — we
// only re-group them, so the dispatch + agent tool names are unchanged.
#pragma once
#include <vector>
#include <cstddef>

namespace cmd {

enum class Workspace {
    Model, Sketch, Sketch3D, SheetMetal, Weldment, Surface, Assemble, Drafting,
    Simulate, Schematic, Board,
    // T-DP15: full-viewport global workspaces (doc-type independent)
    AiAgent,          // AI-Agent full-screen workspace (body: T-DP16)
    PlmManagement,    // PLM project hub full-screen workspace (body: R-stack)
    // workspace-framework: global always-visible workspaces (tab strip step 1)
    Manufacture,      // CAM / machining workspace — OverlayHosting (delegates to drawCamManager)
    PartsLibrary,     // Standard parts / catalog browser — FullViewport
    _count
};

// T-DP15: workspace render mode — whether the workspace owns the full screen
// (no glass overlay columns) or hosts the viewport with pinned glass columns.
enum class WorkspaceKind {
    OverlayHosting,  // viewport + pinned glass FeatureManager (left) + Copilot (right)
    FullViewport,    // takes the full screen; no glass overlay columns
};

enum class Group {
    // 3D verb groups
    Sketch, Create, Modify, Pattern, Construct, Inspect, Output, Appearance,
    // sketch
    Draw, Dimension, Relations, Finish,
    // simulate
    Study, Setup, Run,
    // assembly
    Insert, Position, Display,
    // drafting
    Views, Annotate, Tables, Sheet,
    // ecad
    New, Verify, Place, Auto, Analyze, AI,
    _count
};

// Top  = a primary button on the toolbar (split-button if it has flyout children).
// Flyout = a sub-tool shown in its parent's flyout menu.
// Variant = an operation variant of its parent (Add/Cut/Surface) — hidden from the
//           toolbar, reachable via the ⌘K palette and the parent tool's op selector.
enum class Kind { Top, Flyout, Variant };

struct Command {
    const char* id;        // stable engine dispatch id (unchanged)
    const char* label;     // display
    const char* icon;      // IconId string (see ICONS.md)
    Workspace   ws;
    Group       group;
    Kind        kind;
    const char* parent;    // flyout/variant parent id, or nullptr
    const char* keywords;  // extra ⌘K search terms (may be nullptr)
};

enum class DocType { Part, Assembly, Drawing, Schematic, Pcb };

// ---- queries (all O(N) over the static table; N≈170, called per frame — fine) ----
const std::vector<Command>& all();

const char* workspaceLabel(Workspace w);
const char* workspaceIcon (Workspace w);
const char* groupLabel    (Group g);

// Workspaces offered for a document type, in display order.
std::vector<Workspace> workspacesFor(DocType d);

// T-DP15: render kind for a workspace (OverlayHosting or FullViewport).
WorkspaceKind workspaceKind(Workspace w);

// T-DP15: global workspaces available regardless of doc type (AiAgent, PlmManagement).
// Rendered as a second cluster in the workspace bar, separated by a divider.
std::vector<Workspace> globalWorkspaces();

// Top-level commands in a workspace (excludes Flyout + Variant), grouped, in table order.
std::vector<const Command*> topCommands(Workspace w);
// Groups present in a workspace, in first-appearance (authored) order.
std::vector<Group> groupsFor(Workspace w);
// Flyout children of a Top command (by parent id), in table order.
std::vector<const Command*> flyoutChildren(const char* parentId);

// Lookup by id (nullptr if absent). First match wins (ids may appear in >1 workspace).
const Command* find(const char* id);

// Everything searchable by the palette: every command (incl. variants/flyouts), deduped by id.
std::vector<const Command*> palette();

} // namespace cmd
