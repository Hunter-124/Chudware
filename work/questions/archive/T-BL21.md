# T-BL21 — questions / escalations
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Q1 (resolved — see A1, foreman 2026-07-05) — canonical HSS `section_name` grammar: mm vs. the imperial catalog

Context: `brep.py`'s new hollow-section parser (RHS/HSS/CHS/PIPE) is mm-based, matching every
other form in `section_profile` (`WxH`, `I HxBxTWxTF`). But the codebase's own *existing* HSS
designations — `estimate._AISC_LBS_PER_FT` (`"HSS4X4X1/4"`, `"HSS5X5X1/4"`, `"HSS6X6X1/4"`) — are
the real-world AISC catalog format: imperial, fractional wall thickness. Those two grammars
disagree on what an `HSS` `section_name` *is*. Naively running the fractional string through the
mm digit-regex would silently mis-tokenize `"1/4"` into `[1, 4]` and produce a plausible-but-wrong
4mm×4mm×1mm-wall tube — worse than an error. I closed that specific hole (a literal `/` in the key
now returns unparseable → safe per-type default, see `test_fractional_imperial_hss_designation_does_not_fabricate_geometry`
in `test_elements.py`), but an element named `"HSS4X4X1/4"` still gets **correct takeoff mass**
(exact string match against the lbs/ft table) and a **generic default-rectangle solid** (not its
real hollow geometry) — geometry and cost disagree about the same member.

Recommendation: keep `section_profile`/`element_to_brep` mm-only (rejecting the case for pulling
an imperial fraction/schedule catalog into the mm geometry engine — that's a research-tool concern,
see `mcp__ai-cad__research_standard_section`, not this module). Treat `_AISC_LBS_PER_FT`'s
`"HSS4X4X1/4"`-style keys as a **takeoff-only** designation namespace, separate from
`section_name`'s geometry grammar; a future task converts the imperial designations to their mm
outer×wall equivalents (e.g. `HSS4X4X1/4` → `HSS101.6x101.6x6.35`) so a single `section_name` drives
both geometry and (if the mm string is also added to the lbs/ft table, or looked up via
`research_standard_section`) mass.

Alternatives:
- A. Teach the mm parser to also accept fractional-inch dims (`1/4` → 0.25) and auto-convert
  in×in specs when a `/` or trailing `IN`/`"` marker is present. More parser complexity, and still
  wouldn't reconcile with `_AISC_LBS_PER_FT`'s square-only inches-not-mm keys without a unit flag.
- B. Do nothing further — leave the takeoff/geometry designation namespaces permanently disjoint
  (accept that `section_name` means different things to `estimate.py` vs `brep.py` for legacy `HSS`
  strings). Cheapest, but leaves a latent trap for whoever writes the first real HSS demo doc.

## ANSWER (Q1)

(awaiting advisor)

## A1 (foreman, 2026-07-05)

**Accept the recommendation (mm-only geometry grammar; AISC imperial keys are a
takeoff-only namespace).** Rationale: the driver already closed the dangerous
hole — a fractional imperial string can no longer silently fabricate a
plausible-but-wrong mm solid (it returns a safe per-type default, test committed).
Pulling an imperial fraction/schedule catalog into the mm geometry engine is a
research-tool concern (research_standard_section), not this module's job. Option A
(teach the mm parser fractional inches) adds parser complexity without reconciling
the lbs/ft table's inch-not-mm keys — net worse. Option B (permanent disjoint
namespaces) leaves geometry and cost disagreeing about the same named member —
not acceptable long-term.

Cut **T-BL52** (deferred, low priority) to converge the two: convert
`_AISC_LBS_PER_FT`'s imperial designations to mm outer×wall equivalents
(HSS4X4X1/4 -> HSS101.6x101.6x6.35) so one section_name drives both geometry and
mass, adding the mm string to the lbs/ft table or routing mass through
research_standard_section. Not blocking; BL21 ships as-is.
