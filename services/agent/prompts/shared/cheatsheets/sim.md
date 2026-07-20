SIMULATION — set up, anchor, run, and interpret FEA/CFD/motion honestly.

STUDY KINDS:
  run_fea_static     linear static stress/disp under loads+fixtures
  run_fea_modal      natural frequencies / mode shapes
  run_fea_thermal    steady/transient thermal
  run_fea_buckling   eigenvalue buckling
  run_fea_fatigue    cyclic life estimate (needs stress history)
  run_fea_contact    nonlinear contact
  run_cfd_flow       steady flow (drag/pressure/velocity)
  run_cfd_accurate   higher-fidelity CFD when coarse pass is done
  simulate_motion    rigid-body / mechanism motion

BC / MESH RULES:
  - Fixtures + loads on NAMED faces (describe_part first). One load case per study.
  - Mesh from feature size; refine once at concentrations. Coarse first for sanity.
  - Material via set_material / research_material_lookup — never hand-type E, ν, ρ.

SANITY:
  - Closed-form anchor (research_eng_calc) BEFORE solve; ledger it.
  - Result vs anchor within ~2–3× → credible; >5× → fix BCs/units/material first.
  - Reaction balance / free-body check after static; first modes vs known excitation for modal.
  - describe_results after every run; FAIL names face/feature in plain words.
