---
id: T-WIN2
title: Windows packaging skeleton + bundled llama.cpp Windows seam
workstream: WIN
owner: unassigned
status: todo
branch: task/T-WIN2-win-packaging
depends_on: [T-WIN1]
lane: platform-infra
kind: new
priority: 3
---

## Context

Follow-on to T-WIN1. T-W26 (in flight, another session) is bundling llama.cpp +
local-model provisioning with a DOCUMENTED Windows seam left unimplemented — you
implement the Windows side against whatever seam T-W26 lands (read its merged
code/notes first; do not start this task until T-W26 is merged).

## Steps

1. Read T-W26's merged local_runtime/model_manager + its Windows-seam notes.
2. Windows binary path: pinned llama.cpp Windows release fetch + checksum, app-data
   paths (%APPDATA%/Chudware), server lifecycle on Windows (process mgmt differences).
3. Packaging skeleton: installer or portable-zip layout that carries the app, the
   runtime fetcher, and first-run provisioning; document signing as future work.
4. Live smoke on your box: provision a model, run one bench turn against the
   bundled server (tools/flywheel/bench/benches/roles_local.py, CHUDWARE_LOCAL_ENDPOINT).

## Definition of done

- [ ] Bundled runtime works end-to-end on Windows 10 (real smoke, honest report)
- [ ] Packaging layout documented + scripted; license_debt.md updated if vendored
