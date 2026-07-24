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
title: <string>           # human-readable name
dataset_hash: <string>    # DVC path or hash, e.g. corpus/IP-13/v1
recipe: <path>            # path to training/recipes/*.yaml
base_model: <string>      # HuggingFace model id or local path
adapter_out: <path>       # output directory for the LoRA adapter
eval_bench: <string>      # optional benchmark name (empty string = skip eval)
requires_os: any|linux|windows   # OS constraint; any = no restriction
requires_vram_gb: <float>        # minimum VRAM in GB (0.0 = no GPU required)
requires_cuda: true|false        # whether CUDA is required
owner: unassigned|<agent-name>
status: todo|doing|review|done
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
