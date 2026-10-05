# Python services worker (worker_main / kernel_exec / formats / parts)

Scope: services/worker_main.py, services/compiled_entry.py, services/kernel_exec.py, services/formats/, services/sw_import/, services/parts/, services/paths.py, services/sim/tessellate.py
Contracts: contracts/worker_rpc.md (incl. the hostd worker-lane table), contracts/doc_schema.md, contracts/doc_ops.md, contracts/mesh_format.md, contracts/stepplus.md
Canonical guidance: .claude/skills/chudware-dev/SKILL.md (worker lanes, venv, gate); engine routing lives host-side (work/context/worker-bridge.md)

## Entry points
- `services/worker_main.py` — newline-delimited JSON-RPC services worker.
  - `_METHODS` holds the core methods: `ping`, `run_model`, `list_editable_prompts`,
    `worker_control_info`.
  - `_register(label, import_path, attr, required=…)` merges the service tables:
    - vendor_parts (structured errors), deliverables, parts hub, reveng, DAG, PLM, EM,
      multiphysics and mfg printer adapters degrade with a warning;
    - `services.agent.worker_rpc` (agent_gui, the whole Chudcode/agent admin surface) and
      `services.agent.subagent_rpc` are REQUIRED, and a failed import fails startup.
  - `serve()` honors `CHUDWARE_HOSTD_WORKER_LANE` (`agent`|`general`; unset = full
    lifecycle). Agent-only services, such as the `ControlServer` control lane and the
    prompt-cache/Chudcode warmers, start only off the general lane.
- `services/compiled_entry.py` — the release-only Nuitka entry: `--module worker|mcp|…`.
  Packaging builds it into `chudware-worker` / `chudware-mcp`.
- `services/kernel_exec.py` — `run_code_multi` / `run_code_isolated` execute build123d code.
  It backs only the frozen legacy `run_model` method (host `--build` CLI, tests); it is not
  an agent path.
- `services/formats/formats.py` — Python import/export helpers (`import_path`,
  `export_document`, `list_formats()` → C++ `doc_formats`). The format matrix itself is C++
  (`core/doc/formats.cpp`).
- `services/formats/mesh_body.py` `MeshBody` — the numpy mesh-body type.
- `services/sim/tessellate.py` — the Python AICM v1 writer/reader (`write_aicm`,
  `read_aicm`, `tessellate_mesh_body`) used by MCP/sim/icon scripts. The GUI parser is
  `host/src/render/aicm_loader.*`.
- `services/formats/formats_solidworks.py`, `services/sw_import/{sw_translate,sw_feature_import}.py` —
  SolidWorks COM/Parasolid interop. It is Windows-COM-only (`solidworks_available`).
- `services/parts/` — `worker_rpc.py` (`part_search`/`part_get`/`part_place` + GUI and embed
  seams), hub/index/registry/taxonomy, `parts_library.py`, `vendor_parts.py`.
- `services/paths.py` — appdata roots (`ROOT`, `RUNTIME`, `CACHE`, `CONFIG`, `MODELS`, …),
  `model_dir(name)`, and legacy Billet/AI-CAD root migration.

## How it connects
- Native methods selected by `WorkerClient`'s doc/sim/ECAD/CAM/firmware/agent route
  predicates execute in libchudware. Service extensions are separate worker methods;
  a family prefix alone does not mean every method in that family is native.
- Everything else goes from hostd's `WorkerBridge` (`core/hostd/worker_bridge.cpp`) to one
  of two services-worker processes, chosen by method prefix (`WorkerBridge::forMethod`):
  - the agent lane serves Chudcode sessions, prompts and subagents;
  - the general lane serves PLM, parts, reveng, sim and deliverables.
- Launch:
  - Developer builds: `<CHUDWARE_PYTHON> -m services.worker_main`.
  - Packaged builds: the adjacent `chudware-worker --module worker`. `CHUDWARE_WORKER_BIN`
    must name that adjacent file.


## Debugging playbook
- Interpreter: `scripts/gate.sh::resolve_gate_python` probes `CHUDWARE_GATE_PYTHON`,
  Chudware/legacy appdata venvs, `.venv-mcp`, then other repo-local venvs.
- `<venv>/python -X utf8 -m pytest services/test_worker_stream.py services/test_kernel_exec_aliases.py -q`
  — a real `python -m services.worker_main` subprocess (ping, run_model, shutdown), plus
  kernel aliases.
- `<venv>/python -X utf8 -m pytest services/formats -q`, then `services/parts` — run one
  package per invocation, as the gate does.
- `services/mcp/test_sw_feature_import.py` covers sw_import. It needs Windows COM and owner
  samples.

## Graphify starters
- graphify query "services worker_main _register kernel_exec run_code_multi"
- graphify explain "WorkerBridge"
- graphify path "worker_main.py" "kernel_exec.py"

## Related tasks/docs
- ARCHITECTURE.md (component map), contracts/worker_rpc.md (worker lanes, legacy `run_model`)
- work/context/worker-bridge.md (host routing), work/context/services-agent.md (agent lane)
