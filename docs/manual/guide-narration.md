# Guide: Narration

```text
NARRATION TOOLS — compact text summaries of every domain state:

  describe_toolpaths([job_id])
    Narrates an active CAM job: per-op strategy, tool, stepdown/stepover/stock,
    cycle time + gouge/rapid counts (from cam_simulate), and a G-code digest
    (bounds, feed range, tool changes) from cam_post.
    Use INSTEAD of dumping cam_job_tree JSON.
    Call cam_generate + cam_simulate first for full output.

  describe_results([doc_id], [study_name])
    Narrates the latest (or named) FEA/CFD study on a part: max von Mises stress,
    max displacement, factor of safety, margin vs yield, the fixed/loaded faces
    named by sid, modal frequencies, and mesh convergence caveat.
    Use INSTEAD of dumping run_fea_static / run_fea_modal JSON.
    Returns a graceful 'no results attached' message when no study exists.

  describe_assembly([doc_id])
    Queries pre-solve world-frame bounding boxes and origins, runs a live mate
    solve, then eval_interference with clearance_mode='threshold',
    min_clearance_mm=0 and include_coincident=False. Exact clash volumes remain;
    clearance bounds are not measured gaps. No minimum measured gap is reported.
    Returns raw fit completeness, checked/total pairs and failures; incomplete
    or no-pair reports cannot qualify collision-free. Use INSTEAD of raw JSON.
    Call solve_mates first if placement rows must show the solved pose. To validate
    that mating interfaces LINE UP (hole patterns, coaxial bores, flush contact faces,
    fastener pass-through) — not just that nothing clashes — run interface_check.

  describe_board([doc_id])
    Narrates an active schematic or PCB: component roster (ref/value/footprint),
    net list with members, ERC/DRC violation counts, and unconnected pins.
    Promotes ecad_status into the GSL narration layer.
    Open a design first with ecad_new_schematic or ecad_new_pcb.

  describe_project([run])
    Narrates a DAG project run: nodes done/doing/blocked/pending, budget usage,
    and open questions. Pass run=dag_status()['run'] or run_id=<id>.
    See contracts/dag_schema.md for the run dict shape.

All tools narrate EXISTING numbers only — they never recompute values.
See contracts/narration.md for the full style guide (rounding, attribution,
graceful-degradation phrases, section order).
```

