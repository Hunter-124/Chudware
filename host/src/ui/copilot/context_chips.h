// context_chips.h — the composer's context picker ("context cube") on the GUI side: adding a
// picked model entity as a chip, keeping chips bound to open documents, drawing a chip (pending
// above the composer, and on the sent user bubble), and focusing the entity a chip names.
// The record itself is core::ContextRef (core/context_ref.h, GL-free).
#pragma once

#include <string>

#include "core/context_ref.h"

struct AppState;
namespace render { struct PickHit; }

namespace ui {

// Stamp the active document onto `ref` (id, name, path), toggle it into
// st.pendingContextRefs, and for a newly added viewport pick start the async doc_resolve_pick
// that fills its engine selector, stable sid, owner feature and component.
void addContextRef(AppState& st, core::ContextRef ref);

// Tab index of the open Part/Assembly document a ref names (by doc id, or by saved path for a
// ref restored from another process); -1 when that document is not open.
int contextRefTabIndex(const AppState& st, const core::ContextRef& ref);

// Rebind pending chips to their open documents (a draft restored after a restart carries only a
// path) and refresh doc names. Cheap; call once per frame from the composer.
void reconcileContextRefDocs(AppState& st);

// One chip: kind icon + label (+ the document's name when it is not the active one). Clicking
// the chip focuses its entity; `removable` adds a trailing × that detaches it.
enum class ContextChipAction { None, Focus, Remove };
float contextRefChipWidth(const AppState& st, const core::ContextRef& ref, bool removable);
ContextChipAction drawContextRefChip(AppState& st, const core::ContextRef& ref, bool removable);

// Select / highlight the chip's entity in the viewport, switching to its document tab first
// when it is open elsewhere (the selection lands once that tab's activation completes).
void focusContextRef(AppState& st, const core::ContextRef& ref);
// Applies a focus that waited on a tab switch. Once per frame.
void pumpContextRefFocus(AppState& st);

}  // namespace ui
