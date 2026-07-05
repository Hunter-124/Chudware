# Guide: Reverse

```text
REVERSE-ENGINEERING — turn an imported STEP/mesh body into an EDITABLE parametric feature tree,
verified against the source solid by boolean comparison. Deterministic recognition first; a
learned model is considered only after a measured residual (GEOMETRY_LLM_REPRESENTATION_REVIEW
§3.4/§3.6) — and the heuristics NEVER hide evidence: unclaimed source faces are always listed.

THE ONE-CALL PATH
  reverse_engineer(source) — source is a file path (STEP/IGES/BREP/STL/OBJ/…) or the doc_id of
  an already-imported part. It imports (meshes are sewn to a solid), recognizes, rebuilds the
  recognized features into a NEW part, and verifies EVERY batch with a boolean compare against
  the source. The new part is made current; the imported source doc stays open as the reference.

  What the recognizer can claim deterministically:
  * prismatic base — largest planar face + its antiparallel partner at distance d, with the
    perpendicular/parallel lateral faces explaining >=80% of the remaining area. The profile is
    traced from the face's boundary wires; corner arcs that match a detected fillet radius are
    un-rounded first so the fillet stays a SEPARATE editable feature instead of being baked in.
  * turned body — every curved face coaxial on a principal axis through the origin: the revolve
    profile is computed ANALYTICALLY from the cylinder radii + spans (area/(2*pi*r)) — no
    sectioning, no tessellation.
  * holes — bore-concavity cylinders become real `hole` features {d=2r, blind depth | through},
    entered from the planar cap the bore opens onto.
  * patterns — same-radius hole groups at uniform angular spacing become ONE seed hole + a
    circular_pattern (collinear equal spacing → linear_pattern). Editing the seed re-drives
    every instance.
  * fillets/chamfers — small convex cylinders/tori and 45°±2° planar bridges are CANDIDATES:
    applied only when a plain-word edge query resolves on the rebuilt body AND the match does
    not regress; otherwise they are returned under `candidates` for you to apply by hand.

READING THE RESULT
  match_pct     common_volume / max(volume) of the two solids. 100 ≈ identical. null means the
                boolean itself failed (sick geometry) and the compare DEGRADED to volume/bbox
                agreement — trust it less, say so.
  features      what was rebuilt (ids are live feature ids in the new part — edit them).
  rejected      batches that built but made the match WORSE (or failed to build): they were
                REVERTED, with before/after numbers. Nothing rejected is silently kept.
  candidates    recognized-but-not-applied dress-ups/holes, with radius/length + a world-space
                `near` point. Apply one with add_features, e.g.
                {"type":"fillet","params":{"radius":{"value":3,"expr":null}},
                 "refs":{"edges":{"of":"edge","near":[50,30,0],"count":1}}}
                then re-run compare_parts to prove it helped.
  unexplained   missing_mm3 (source material the rebuild lacks), extra_mm3 (rebuild material
                the source never had), and up to 10 residual REGIONS with centers — go look
                exactly there.
  recognition.unclaimed  source faces no pass claimed — hover(sid) them on the ref doc.

MANUAL PRIMITIVES (when the one-call path is not enough)
  compare_parts(doc_a, doc_b)          the same boolean verdict for ANY two docs. doc_a is the
                                       reference. WORLD-FRAME: no alignment/ICP is attempted —
                                       a moved copy scores low, by design.
  convert_face_to_sketch(face, ...)    "convert entities": trace one planar face's wires into
                                       an editable sketch ON that face, then extrude/cut it.
                                       Creates a NEW sketch positioned on the face's own plane.
  project_into_sketch(sketch, refs)    "convert entities" INTO AN EXISTING sketch — appends
                                       projected geometry (lines/arcs/circles/ellipses) to a
                                       sketch you are already building, mirroring SolidWorks
                                       Convert Entities workflow.  Distinct from
                                       convert_face_to_sketch (which makes a new sketch).
                                       refs: a datum string ("front"/"top"/"right"), a face sid
                                       ("Fdb31d2"), a side word ("top" for the top face — note
                                       "front"/"top"/"right" resolve to DATUM PLANES not faces),
                                       an edge sid ("E4a7c9"), or a ref dict; or a list of any
                                       mix.  A face ref projects ALL its boundary wires.  Each
                                       entity is pinned with a fix relation.  ONE-SHOT: converted
                                       geometry does NOT update when the source model changes.
                                       Use construction=True for reference-only geometry.
  describe_part()/hover(sid)           read the imported geometry as text (cad_guide("gsl")).

WORKFLOW FOR THE RESIDUAL
  1. reverse_engineer(path) → note match_pct.
  2. For each unexplained region: hover the nearby unclaimed sids on the ref doc, model the
     feature by hand on the rebuilt part (convert_face_to_sketch helps), compare_parts again.
  3. Only call the job done when match_pct is high AND you can name every unclaimed face.

CAVEATS — BE HONEST ABOUT THESE
  * Mesh input (STL/OBJ/…) is faceted: the exact-surface passes claim little. Expect a low
    deterministic yield and say so; the value is the imported solid + the honest residual map.
  * The rebuild is authored in the SOURCE's coordinates; everything assumes world-frame match.
  * A cbore/csink stack is recovered as separate coaxial simple holes, not one wizard hole.
  * Cone faces (turned chamfers), splines and freeform faces are not claimed deterministically
    — they surface in `unclaimed`/`candidates`, never silently approximated.
```

