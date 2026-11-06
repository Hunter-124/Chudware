# Chat Context References — copilot "context cube" picker

Current payload ownership is `host/src/core/context_ref.{h,cpp}` and
`host/src/ui/copilot/composer.cpp::submitTurn`; online engine-resolved context comes
from `services/agent/worker_rpc.py::_picked_context_text` / `_picked_context_block`.
The explicit offline route uses `services/agent/agent_turn.py::apply_context_refs`.

Goal: let the user attach live model entities (parts/components, faces, edges, sketches,
features) from the 3D viewport or the feature tree into the chat prompt as first-class
context references, so the AI targets and reasons about exactly those entities when editing
or explaining. A powerful, intuitive "point at what you mean" affordance.

## Interaction model (intuitive)
1. Click the cube -> arms `AppState::contextPickActive = true`. A viewport banner/HUD hint
   appears ("Pick parts, faces, edges, sketches, or features to add to the chat — Esc to
   finish"), same visual language as a PropertyManager selection well. The cube button shows
   an active/highlighted state.
2. While armed: clicking an entity in the VIEWPORT (component/body/face/edge) OR a row in the
   FEATURE TREE (sketch/feature/component) appends an `AppState::ContextRef` to
   `pendingContextRefs` and shows a chip above the input (beside file-attachment chips).
   Multi-select accumulates chips; picking an already-added entity toggles it off.
3. Esc, clicking the cube again, or sending the message disarms pick mode.
4. Each chip: small kind icon + short label + `x` to remove. Chips persist until send.
5. No-mode alternative: right-click a current selection in viewport/tree -> "Add to AI
   context" pushes the same ContextRef(s). (Reuses the existing selection-dependent RMB
   menus.)

## Data model (in host/src/core/app_state.h)
```cpp
struct ContextRef { std::string kind, id, docId, label; };
std::vector<ContextRef> pendingContextRefs;  // consumed+cleared by submitTurn
bool contextPickActive = false;              // cube armed
```
kind ∈ {component, body, face, edge, sketch, feature}. id = stable selector (feature id,
component id, or face/edge sid from describe_part/query_geometry). docId empty = active doc.

## Payload wiring
submitTurn serializes pendingContextRefs into the turn RPC payload under `context_refs`:
```json
"context_refs": [ {"kind":"face","id":"<sid>","doc_id":"<id>","label":"Front face of Wheel"} ]
```
The agent turn includes a "Context references" block so the model can resolve/measure/target
them (feed to describe_part/query_geometry/measure_* by id). Chips clear on send.

