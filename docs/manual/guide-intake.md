# Guide: Intake

```text
INTAKE / FIRST-TRY-FIT — get real-world parts to fit on the first print.

NORTH STAR: never silently guess a dimension that decides whether parts FIT. Spend questions only
on the dims that gate a mating interface; default everything else and flag it.

ISO 286 FIT CALCULATOR — call fit_calc BEFORE setting any bore or shaft diameter:
  fit_calc(nominal, fit)          ISO 286 tolerance bands + fit character. Always call this
                                  BEFORE hole_sizing or setting a bore dimension. Example:
                                  fit_calc(20.0, "H7/g6") → hole 20.000–20.021,
                                  shaft 19.980–19.993, clearance 0.007–0.041 mm.
  recommend_fit(function)         not sure which fit class? "sliding", "press fit", "bearing",
                                  "dowel pin" → recommended ISO 286 classes + rationale.
                                  Call recommend_fit FIRST, then fit_calc with the class it gives.
  suggest_dimension_scheme(doc, sketch, process)
                                  ordered dimension plan for a sketch: which dims to drive,
                                  default IT-grade tolerances by process, bolt-pattern datum rules,
                                  and needs_user_confirm flags for function-critical dims.
                                  CNC/lathe → IT7, FDM → IT12, sheet/laser → IT10.

WHEN the design must incorporate a real part you have NO model/datasheet for:
  1. LOOK IT UP FIRST — list_standard_parts / make_standard_part. Only a real miss triggers photos.
  2. resolve_process(request) — pick the manufacturing process (FDM 3D-printing is the default).
     If it returns ambiguous, ASK the user the one `clarification` question (e.g. sheet-metal vs
     CNC for a flat metal plate); then set_process(process). The process sets the fit clearances.
  3. request_part_photos(label, critical_features) — ask for photos WITH a scale reference (a
     printable scale card / calipers-in-frame / ruler) in the SAME PLANE as the feature, plus the
     ortho views + close-ups it lists. The user replies with photos next turn.
  4. submit_part_photos(claims) — report the dims YOU read from the photos as structured claims
     (with scale_source / in_plane / agrees_across_views / edge_quality). It scores each and
     returns `open_demands`: the fit-critical dims whose confidence is too low for their fit.
  5. confirm_dimensions([{key,value_mm}]) — get the user to TYPE a caliper number for each demand
     (a press/slip fit won't work off a photo guess). When the gate clears, build the geometry.
  6. Build the RECEPTACLE to fit, not a copy — the process clearance is already in the numbers.
  7. fit_report() to review modeled-vs-measured + clearances before export; dfm_check() for wall/
     draft vs the process minimums; make_fit_coupon(key) to print a tiny test of one interface
     first. All of this persists in the .step+ (reopens in-app intact).
```

