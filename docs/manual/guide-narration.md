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
    Narrates an assembly or multi-body part: per-component world-frame bounding
    boxes and origins (from asm_world_bodies), interference/clearance pairs
    (volumes + gap distances from eval_interference), and mate solve status.
    Use INSTEAD of dumping asm_world_bodies JSON.
    Call solve_mates first for the latest placement.

  describe_board([doc_id])
    Narrates an active schematic or PCB: component roster (ref/value/footprint),
    net list with members, ERC/DRC violation counts, and unconnected pins.
    Promotes ecad_status into the GSL narration layer.
    Open a design first with ecad_new_schematic or ecad_new_pcb.

  describe_project([run])
    Narrates a DAG project run: nodes done/doing/blocked/pending, budget usage,
    and open questions. Feeds WS-A dag_status and the T-F17 mission-control UI.
    Pass run=dag_status()['run'] from your WS-A driver.
    See contracts/dag_schema.md for the run dict shape.

All tools narrate EXISTING numbers only — they never recompute values.
See contracts/narration.md for the full style guide (rounding, attribution,
graceful-degradation phrases, section order).
```

