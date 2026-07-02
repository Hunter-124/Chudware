# services/worker — JSON-RPC seam, libaicad router, document mirror

Scope: services/worker/**, contracts/worker_rpc.md
Contracts: contracts/worker_rpc.md (protocol 2, newline-delimited JSON-RPC), contracts/doc_ops.md (ops envelope), contracts/doc_schema.md (aicad.part/1 | aicad.asm/1)

## Entry points
- services/worker/worker_command_dispatcher.cpp:L28 `dispatch(cmdId, params)` — stable command router; routes ui::ICommandDispatcher calls to WorkerClient or libaicad
- services/worker/worker_client.h:L29 `start(module)` and :L36 `call(method, params)` — spawns Python engine.worker subprocess (stdio JSON-RPC) and sends synchronous RPC; blocks until response or timeout (120s default)
- services/worker/cpp_core.h:L29 `ensureLoaded(err)` and :L38 `call(method, params)` — runtime DLL loader for libaicad (via LoadLibrary/dlopen); resolves aicad_call/aicad_free/aicad_abi_version C ABI exports and returns true iff core handled the method
- services/worker/worker_router.cpp:L16 `routeMethod(method, docId)` — routing policy: returns CppCore if AI_CAD_CPP_DOCS=1 AND (method=="doc_new" OR docId has "doc-cpp" prefix); else PythonOracle
- services/worker/doc_mirror.h:L68 `DocMirror` — stateful document model (tree + revision + feature_status); caches last known doc JSON and feeds feature tree UI; notified post-mutation
- services/worker/bootstrap.cpp:L1 — runtime provisioning (Python venv setup on first app launch)

## How it connects
This is the production strangler seam: every UI doc command (doc_new/doc_apply_ops/doc_undo/doc_redo/etc) flows through WorkerCommandDispatcher → WorkerClient → worker_router → either CppCore (libaicad.dll/.so) or Python engine_oracle over stdio JSON-RPC. CppCore lazily loads the native core (separate binary; zero dependency on OCCT/Python in host), dispatches via flat C ABI, and silently falls back to Python if the method is unhandled or the DLL is absent. Per README: **24/24 geometric-feature parity, 42/42 stateful doc-session parity, FEA static/modal 4/4, SPICE 35/35+10/10**. Still Python-only: CFD, ECAD, formats, assemblies, agent/MCP layer. Settings → General → Engine surfaces the live backend.

## Debugging playbook
- Build libaicad: `cmake -S core -B core/build-linux -DAICAD_WITH_OCCT=ON && cmake --build core/build-linux --config Release` (produces libaicad.so)
- Test core: `ctest --test-dir core/build-linux -R "test_doc|test_sketch" --output-on-failure` (constraint solver, geometry, session tests)
- Test Python worker: `python3 -m engine.doc.test_session` or `python3 -m engine.run_model mounting_plate`
- Gotcha: AI_CAD_CPP_DOCS env flag MUST be set to "1" (or any non-empty non-"0" value) to route to C++; default routes all to Python oracle
- Gotcha: libaicad.dll/.so must be in exe dir, AI_CAD_LIBAICAD env var, or system loader path; CppCore::ensureLoaded caches the load result, so first call failure silently disables C++ routing for the process lifetime (fallback to Python)
- Gotcha: Windows DLL transitive-dep resolution requires SetDllDirectoryW (cpp_core.cpp:L129); LOAD_WITH_ALTERED_SEARCH_PATH alone doesn't cover OCCT TK*.dll or gmsh siblings

## Graphify starters
- graphify query "WorkerClient call routing libaicad engine_oracle"
- graphify explain "routeMethod"
- graphify path "worker_command_dispatcher.cpp" "cpp_core.cpp"

## Related tasks/docs
- contracts/worker_rpc.md (protocol 2 handshake, doc_* method table, ops-response shape + owner_map)
- docs/CPP_MIGRATION.md (strangler cutover design, ABI versioning)
- docs/REFACTOR_LOG.md (C++ parity roadmap: what's landed vs pending)
- AGENTS.md §N6b (engine_oracle lifecycle: "frozen while core/ catches up... deleted LAST")
- work/tasks: grep `worker|routing|cpp_docs|libaicad` (integration tasks T-C*, T-G*, T-S* track cutover)
