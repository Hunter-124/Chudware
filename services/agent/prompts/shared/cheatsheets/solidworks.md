SOLIDWORKS interop: open teammates' parts, send parts back as native SolidWorks features.

IMPORT `import_part(path)` (.sldprt, .sldasm, .x_t, .x_b). Lanes, best first:
1. SolidWorks installed: the COM feature-tree import rebuilds sketches and features
   (`feature_resolved=true`). A feature it cannot rebuild refuses the import with
   `unsupported`. `resolve_features=False` imports the exact solid instead.
2. No SolidWorks, or COM failing: the part's own Parasolid body is read natively
   (`import_fidelity="exact_native_brep"`, SolidWorks 2011+ parts and Parasolid files;
   `solidworks_source.saved` names the release that saved it). Recon feature recognition then
   rebuilds an editable tree. It becomes current only when it matches the source
   (`feature_recovery.match_pct >= 99.9`, `feature_source="recognized"`). Otherwise the exact
   body stays current and `feature_recovery.doc_id` holds the partial rebuild to finish by hand.
3. Assemblies without SolidWorks, or parts the native reader rejects: the saved display
   tessellation (`saved_display_tessellation`, approximate).

EXPORT `export_solidworks(doc_id, path, method)`:
- method="com" (SolidWorks on this machine): SolidWorks builds each feature and saves the
  .SLDPRT. Check `verified` (volume and centre of mass against Chudware's).
- method="macro": writes a .swb macro. The teammate runs Tools > Macro > Run and their
  SolidWorks writes the part in THEIR release, then reports MATCHES / DOES NOT MATCH.
  Use it for a teammate on an older SolidWorks: SolidWorks cannot save to an older release.
- Exported: sketches on Top/Front/Right, an offset of one, or a principal-facing face (lines,
  arcs, circles, points, splines); extrude/cut (blind, mid-plane, through-all, two
  directions); revolve about the sketch's single centerline; constant fillet; equal chamfer;
  shell; linear pattern along X/Y/Z; circular pattern about a world axis; feature mirror
  across Top/Front/Right.
- Anything else is listed in `unsupported` and the export refuses.
  `allow_body_fallback=True` sends the exact solid as one imported body instead
  (`fidelity="imported_body"`, no feature tree). `degraded` notes exact geometry that carries
  less intent: sketch relations and dimensions are not re-created yet.

3 common mistakes:
1. Calling an exact native import "parametric": only `feature_resolved=true` is a feature tree.
2. Telling the user a macro export is verified: it is checked when the teammate runs it.
3. Sending a .step+ or STEP when the teammate asked for SolidWorks: use export_solidworks.
