# Model history evaluation and simplification

`eval_model_history({doc_id})` inspects the authored history of a part and returns
`{ok, doc_id, revision, complete, summary, metrics, findings, safe_fixes,
blocked_fixes, limitations}`. `complete` describes this document-history rule set
only: the report is **not** a geometry, constraint-solver, manufacturing or
engineering certification. Assemblies are refused. The same inexpensive report
is included in part operation responses as `rebuild.doc_status.model_history`
and `model_history`; it does not run a sketch solver, compile geometry, or run
pairwise booleans.

Each finding has `code`, `severity`, actual `feature_ids`, `message`, `suggestion`,
`auto_fixable`, and optional `evidence`. The static rules cover unreferenced
datums; used sketches with only coincidence relations; structural work after
dress-ups; standalone extrude/transform/boolean-cut chains; repeated paired
cylinder/cone cuts suggestive of countersinks; literal identity transforms;
repeated literal placements; and repeated literal dimensions. Countersink
guidance requires both primitives to have effective cut mode (`merge:false`,
including an active configuration override, instead creates a new body).
Signed axis words and normalized vectors use the primitive compiler's direction
semantics. Only matching placements, directions, dimensions and cone orientation
are grouped; opposite directions do not match. Suggestions are advisory: equal
sizes or placements do not prove a shared relationship or a pattern, and
coincidence-only evidence is not a solver DOF result. Suppressed, rolled-back and
active-configuration states are respected. Feature count and face ownership are
not evidence that a feature is unnecessary.

A `safe_fixes` item contains `feature_ids`, `description`, and native `ops`,
currently a `simplify_history` operation with those IDs. `blocked_fixes` identifies
candidates that are not safe to remove and gives the reason. Eligibility is
conservative: unused active datum features or exactly literal zero, in-place
transforms with a proven subsequent body owner. All authored references,
selector owners, parameters, expressions, metadata, configurations and open
consumer documents are considered. Suppressed features, active rollback, named
configurations, unknown metadata, scripted dependencies, externally observable
body identities, copies, and expression-driven transforms are not removed.
Face finishes block transform cleanup when stable face identity cannot be proven.
Saved, embedded, detached, or otherwise public documents may have persisted
external consumers that the process cannot enumerate, so feature-ID deletion is
blocked even when their local history has no references. Live drawings register
their source as an external consumer while open. Successfully saving a standalone
`.draw+` containing the part's source history also publishes that history, so
cleanup stays blocked after the drawing closes. This does not save the source
`.step+`, change its revision or mark it clean. A busy open document or another
registered external consumer also blocks cleanup; no absence of evidence is
treated as proof of safety.

`doc_apply_ops` accepts `{op:"simplify_history", features:[id,...]}`. Omit
`features` to select all currently eligible candidates; an explicit empty list is
a no-op. Every explicit ID must still be eligible, otherwise the entire batch is
refused. The native engine rechecks eligibility under the document lock and the
normal `base_revision` guard; a changed live-consumer registry, stale revision,
new reference, invalid candidate or failed rebuild leaves the original tree and
history intact. No cascading deletion or automatic reorder occurs. Removing a
solid-history transform requires scratch compilation, matching surviving body
IDs and kinds, valid geometry and valid, warning-free non-destructive directional
material differences in both directions. Inconclusive comparison blocks deletion.
A successful batch is one undo step; no selected candidates means no revision,
dirty-state or undo change. Other operations may share the normal atomic batch,
including an explicitly requested editable `refine` feature.
