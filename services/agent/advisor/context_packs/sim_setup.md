# Simulation setup context pack
# Tools: run_fea_static/modal/fatigue/thermal/buckling/contact, run_cfd_flow/accurate,
#        simulate_motion, describe_results, set_material, research_eng_calc, research_material_lookup

## Discipline
1. Anchor FIRST — closed-form order-of-magnitude (research_eng_calc beam/plate) before any solve. Ledger the estimate.
2. One load case per study. Mixed cases hide which load broke it.
3. Fixtures and loads on NAMED faces that exist (describe_part). Fix every pre-sim advisory lint before running.
4. Mesh from feature size; halve once near stress concentrations if first result is singular.
5. After run: describe_results; compare to anchor (~2–3× credible; >5× = setup bug first).
6. Verdict per criterion with margin vs pass rule; FAIL names face/feature in plain words.

## Common traps
- All-fixed body / missing loads → meaningless stress field.
- Hand-typed material props — use set_material / research_material_lookup.
- Modal without excitation comparison is trivia.
- CFD: check inlet/outlet BCs and units before trusting drag/pressure.
