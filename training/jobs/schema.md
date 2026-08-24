---
id: schema
title: Training job spec schema
status: meta
---

# Training Job Spec — Schema Reference (T-H6)

A training job is a Markdown file in `training/jobs/` with flat YAML front matter
(same parser as `work/tools/work.py`). No nested YAML — multi-key sub-sections like
`requires:` are expanded to flat keys with a `requires_` prefix.

## Front-matter fields

```
---
id: <string>              # unique job id, e.g. smoke-sft-001
job_kind: lora_adapter|simulation_optimizer  # optional; omitted = lora_adapter
title: <string>           # human-readable name
dataset_hash: <string>    # DVC path or hash, e.g. corpus/IP-13/v1
recipe: <path>            # path to training/recipes/*.yaml
base_model: <string>      # HuggingFace model id or local path
adapter_out: <path>       # LoRA only: output directory for the adapter
eval_bench: <string>      # optional benchmark name (empty string = skip eval)
requires_os: any|linux|windows   # OS constraint; any = no restriction
requires_vram_gb: <float>        # minimum VRAM in GB (0.0 = no GPU required)
requires_cuda: true|false        # whether CUDA is required
owner: unassigned|<agent-name>
status: todo|doing|review|done
simulation_family: <string>       # simulation_optimizer only; canonical registry family
registry_track: <string>          # simulation_optimizer only; track_id in registry.json
trainer_entrypoint: <path>        # simulation_optimizer only; executable training entry point
artifact_out: <path>              # simulation_optimizer only; model artifact output
corpus_manifest: <path>           # simulation_optimizer only; immutable corpus manifest
corpus_manifest_sha256: <sha256>  # simulation_optimizer only; lowercase 64-hex digest
model_card_out: <path>            # simulation_optimizer only; required before review
split_policy: group_holdout       # simulation_optimizer only; row/random splits are forbidden
baseline_id: <string>             # simulation_optimizer only; registered deterministic baseline
optimization_target: <string>     # simulation_optimizer only; registered objective identifier
safety_veto_profile: <string>     # simulation_optimizer only; non-empty veto-set identifier
decision_metric: <string>         # simulation_optimizer only; registered promotion metric
promotion_evidence_out: <path>    # simulation_optimizer only; hashed held-out evaluation output
---
```

## Notes

- **Flat encoding** — `requires_os`, `requires_vram_gb`, `requires_cuda` instead of
  a nested `requires:` block. This keeps the file compatible with the same regex-based
  front-matter parser used by `work/tools/work.py`.
- `dataset_hash` is a DVC target — passed verbatim to `dvc pull <target>`. It may be
  a `.dvc` file path, a directory, or a DVC cache hash string.
- `recipe` is a repo-relative path, e.g. `training/recipes/sft_lora.yaml`.
- `adapter_out` is a repo-relative or absolute output path. The runner creates it with
  `mkdir -p` before training.
- `eval_bench` is optional. Leave empty or omit if no eval is configured yet.
- `job_kind` is additive. Existing job files omit it and continue to mean
  `lora_adapter`; their `recipe`, `base_model`, and `adapter_out` behavior is
  unchanged.
- A `simulation_optimizer` job is a non-LoRA job. It requires
  `simulation_family`, `registry_track`, `trainer_entrypoint`, `artifact_out`,
  `corpus_manifest`, `corpus_manifest_sha256`, `model_card_out`,
  `split_policy`, `baseline_id`, `optimization_target`,
  `safety_veto_profile`, `decision_metric`, and
  `promotion_evidence_out`. It does not require `base_model` or `adapter_out`;
  `recipe` may name a non-LoRA optimizer recipe.
- `simulation_family` and `registry_track` must match
  `training/simulation/registry.json`. The canonical families are
  `fea_static_adaptive`, `modal`, `thermal`, `fatigue`, `buckling`, `contact`,
  `frequency_response`, `lbm_cfd`, `openfoam_cfd`, and `motion`.
- Simulation-optimizer splits must hold out a whole `geometry_family`,
  `cad_template`, `physical_asset`, or `case_family`. Row-, action-, candidate-,
  and run-level random splits are rejected because they leak related geometry
  into evaluation.
- The training runner may produce an artifact, but only the simulation registry
  can record promotion. Candidate, validated, or promoted states require the
  artifact hash, model-card hash, and held-out evidence hashes; deterministic
  solver results and safety vetoes remain authoritative.
- `owner` is set atomically by the runner at claim time (mirrors `work.py` pattern).
- `status` lifecycle: `todo` → `doing` (claimed) → `review` (training done, waiting
  for human merge/promotion) → `done` (merged/accepted).

## Capability matching

The runner calls `detect_capability()` and matches against the job's `requires_*`
fields via `_can_claim()`:

| job field         | cap field   | rule                                                  |
|---|---|---|
| `requires_os`     | `cap["os"]` | job value must be `"any"` or equal `cap["os"]`        |
| `requires_vram_gb`| `cap["vram_gb"]` | `cap["vram_gb"] >= requires_vram_gb`             |
| `requires_cuda`   | `cap["cuda"]`| if `true`, runner must have `cap["cuda"] == True`    |

WSL2 runners report `os: linux` (they ARE running Linux). Native Windows runners
report `os: windows`.
