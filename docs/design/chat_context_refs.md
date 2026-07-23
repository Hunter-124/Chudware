# Chat Context References — copilot "context cube" picker

Status: design + Wave-4 implementation (2026-07-16 session).
Goal: let the user attach live model entities (parts/components, faces, edges, sketches,
features) from the 3D viewport or the feature tree into the chat prompt as first-class
context references, so the AI targets and reasons about exactly those entities when editing
or explaining. A powerful, intuitive "point at what you mean" affordance.

## Composer toolbar (bottom row of the copilot input)
Row order, filling the full width, evenly aligned (uiScale-aware): 
`[ model picker ] [ thinking effort ] ......... [ paperclip ] [ context-cube ] [ new ]`
- Paperclip icon: REMADE (cleaner glyph) — file attachments (unchanged behavior).
- Context-cube button (NEW): the Chudware logo cube drawn with BLACK outline lines on a
  TRANSPARENT background. Toggles context-pick mode.
- Buttons distribute so there is no dead gap: left group (model, thinking) left-aligned;
  right group (paperclip, cube, new) right-aligned; spacer between.

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
   context" pushes the same ContextRef(s). (Reuses the selection-dependent RMB menus from
   Wave 1.)

## Data model (added to host/src/core/app_state.h — DONE pre-dispatch)
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

## Scope split (Wave 4 workers, disjoint write-sets)
- R_Icons: remade paperclip glyph + new logo-cube icon (black lines, transparent bg). Icon
  draw files per gui-icons; returns icon id(s) -> integrator appends contracts/icons.md.
- S_Composer: host/src/ui/legacy_chat.cpp(+.h) — cube button, chips row, arm/disarm, chip x,
  submit `context_refs`, toolbar fill/align. Consumes R's icon ids + app_state fields.
- T_PickCapture: host/src/ui/viewport_panel.cpp + host/src/ui/feature_tree.cpp — when
  contextPickActive, route viewport picks / tree-row clicks into pendingContextRefs; RMB
  "Add to AI context". Uses app_state fields (declared already).
- U_AgentPayload: services/agent/ — accept + surface context_refs in the agent turn.

## Verification
- Cube button renders (both themes, toolbar + no dead gap); paperclip remade.
- Arm cube -> pick a face in viewport + a sketch in tree -> two chips appear; x removes one;
  Esc disarms.
- Send with chips -> payload carries context_refs; agent can reference them; chips clear.
