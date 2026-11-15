SOLIDWORKS interop: open teammates' parts and assemblies, send parts and assemblies back as
native SolidWorks files.

IMPORT `import_part(path)` (.sldprt, .sldasm, .x_t, .x_b). Lanes, best first:
1. SolidWorks installed: the COM feature-tree import rebuilds sketches and features
   (`feature_resolved=true`). For a PART a feature it cannot rebuild refuses the import with
   `unsupported`; `resolve_features=False` imports the exact solid instead. For an ASSEMBLY
   every part that can be rebuilt gets its feature tree, the others come in as their exact
   solid (`parts_as_exact_solid` names them and why), sub-assemblies come in as nested
   assemblies, every component at its SolidWorks position (mates are not converted).
   Every rebuilt tree is checked against SolidWorks' volume for that part; one that
   rebuilds differently is replaced by the exact solid (named with the reason).
2. No SolidWorks, or COM failing: the part's own Parasolid body is read natively
   (`import_fidelity="exact_native_brep"`, SolidWorks 2011+ parts and Parasolid files;
   `solidworks_source.saved` names the release that saved it). Recon feature recognition then
   rebuilds an editable tree. It becomes current only when it matches the source
   (`feature_recovery.match_pct >= 99.9`, `feature_source="recognized"`). Otherwise the exact
   body stays current and `feature_recovery.doc_id` holds the partial rebuild to finish by hand.
3. Assemblies without SolidWorks, or parts the native reader rejects: the saved display
   tessellation (`saved_display_tessellation`, approximate).

EXPORT `export_solidworks(doc_id, path, method)`, a part or an assembly:
- The export is ONE VBA macro (.swb). method="com" (SolidWorks on this machine) runs it here
  and saves the .SLDPRT/.SLDASM; method="macro" only writes it, for a teammate to run
  (Tools > Macro > Run) in THEIR SolidWorks, which saves the file in THEIR release. Use it for
  a teammate on an older SolidWorks: SolidWorks cannot save to an older release. The macro is
  also left beside every COM export.
- Check `verified` and `checks` (volume, centre of mass, body count, inertia against
  Chudware's geometry). `notes` lists cuts that removed nothing (as in Chudware).
- Part features exported: sketches on any plane or face (default planes, offsets,
  point-normal datums, face queries via the engine's frame); 3D sketches (lines, arcs,
  splines); extrude/cut; revolve about a sketch centerline or a world axis; sweep along a
  sketch, 3D sketch or helix; loft; helix; fillet; equal chamfer; shell; linear and circular
  patterns; feature mirror; box/cylinder/cone/sphere/torus primitives (any axis); transform
  (Move/Copy Body); boolean (Combine). Multibody parts keep their bodies; a cut hits the one
  body Chudware cuts and a boss fuses only with the bodies it touches.
- Not exported (exact solid instead): `import` features (a part that IS imported geometry),
  and anything SolidWorks cannot build, e.g. a sweep whose solid intersects itself (the
  sweep feature warns about that in Chudware too).
- Anything else is listed in `unsupported` and a part export refuses.
  `allow_body_fallback=True` sends the exact solid as one imported body instead
  (`fidelity="imported_body"`, no feature tree). `degraded`: sketch relations and dimensions
  are not re-created yet.
- ASSEMBLY: writes a folder (one macro + .step per part, one per sub-assembly, the top
  macro). Each part is a feature tree when it maps and matches, else its exact solid
  (`parts[].fidelity`, `fidelity_counts`); components are placed at their Chudware poses and
  fixed (Chudware mates are not re-created). Share the whole folder; the teammate runs only
  the top macro.

3 common mistakes:
1. Calling an exact native import "parametric": only `feature_resolved=true` is a feature tree.
2. Telling the user a macro export is verified: it is checked when the teammate runs it.
3. Sending a .step+ or STEP when the teammate asked for SolidWorks: use export_solidworks.
