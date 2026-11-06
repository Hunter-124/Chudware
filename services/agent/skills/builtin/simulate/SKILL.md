---
name: simulate
description: Run FEA, thermal, vibration, CFD, motion, EM, and topology optimization studies with actual setup units, backend checks, convergence, and analytic anchors.
inject: auto
triggers:
  keywords: [fea, cfd, simulate, simulation, stress, modal, thermal, vibration, wind tunnel, drag, motion, eigenmode, s-parameter, antenna, contact, load case, strong enough, will this hold, can this hold, factor of safety, deflection, buckling, von mises, topology, thermomechanical, conjugate heat transfer]
  tools: [run_fea_static, run_fea_adaptive, run_fea_modal, run_fea_fatigue, run_fea_frequency_response, run_fea_thermal, run_fea_buckling, run_fea_contact, run_fea_convergence, sim_evidence_packet, verify_packet, polymesh_cli, run_cfd_flow, run_cfd_accurate, simulate_motion, sweep_envelope, steering_turning_radius, motion_fea_handoff_sweep, em_eigenmode, em_driven, em_timedomain, ecad_antenna_design, ecad_antenna_analyze, topology_capabilities, run_topology_optimization]
  phases: [sim]
---

# Simulate

Start with `cad_skill(name="simulate", path="execution.md")` for study setup, motion-cost warnings, release evidence and current solver surfaces. Fetch the relevant additional procedures:

- `cad_skill(name="simulate", path="topology.md")` for topology optimization and verdicts.
- `cad_skill(name="simulate", path="mechanism.md")` for mechanism proof, sampled clearance, steering and motion-to-FEA handoff.

If not already available, call `enable_tools(tools=["em_status", "wait_job", "get_job", "sim_cancel", "render_animation", "slice_part", "import_part", "query_geometry", "set_material"])` and wait for it to succeed before using them. Mode and role restrictions still apply.
