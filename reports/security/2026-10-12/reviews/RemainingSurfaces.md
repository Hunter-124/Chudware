## yield — findings

**Read-only audit completed for the assigned scope. No files changed, commands executed, tests/scanners run, network requests made, or actual datasets/credentials inspected.** Findings below are confirmed by current source; reproduction outcomes are **not observed**.

### 1. High — DAG verification predicates escape their “restricted” Python namespace

- **CWE:** CWE-95, Eval Injection.
- **Locations:**
  - `services/dag/verify.py:262-289` — evaluates an expression with `eval`; exposes Python builtin functions.
  - `services/dag/verify.py:379-382,514-523` — obtains the expression from caller-supplied `end_condition`.
  - `services/dag/graph_loader.py:83-138` — graph validation does not restrict predicate syntax.
  - `services/dag/context_pack.py:103-127` — forwards `end_condition` unchanged.
  - `services/dag/mcp_tools.py:711-739` — submitted graphs reach `executor.run`/`resume`.
- **Attacker-controlled source → sink:** A malicious imported/model-authored graph supplies a verify node’s `end_condition`. Once the node runs and its tool succeeds, that string reaches Python `eval`.
- **Broken control:** Removing ordinary builtins is insufficient. The explicitly exposed builtin `abs` has `__self__` pointing to the complete `builtins` module. Attribute access is unrestricted, allowing access to `open`, `__import__`, and subsequently arbitrary process capabilities.
- **Prerequisites:** Victim executes or approves the malicious declarative DAG. This is not a claim that an authorized desktop shell tool is privilege escalation: the unexpected execution occurs inside a purportedly restricted verification expression, without invoking a shell/code tool.
- **Customer impact:** Worker-account code execution; customer files, credentials accessible to that account, and model/artifact integrity are exposed. On the shared web daemon, finding 2 supplies a separate unauthorized graph-resume path.
- **Nearby controls considered:** Default approval gates can postpone execution; `ok:false` tool results skip predicate evaluation; exceptions fail closed. None makes a successfully evaluated hostile expression safe.
- **Minimal remediation:** Replace Python `eval` with a small AST-interpreted expression language: explicitly permitted comparisons, boolean operations, literals, bounded arithmetic, and controlled dictionary lookups. Reject arbitrary attributes, comprehensions, lambdas, and unrestricted function calls.

**Safe isolated repro for integrator:**
```python
from pathlib import Path
from tempfile import TemporaryDirectory
from services.dag.verify import _eval_predicate

with TemporaryDirectory() as td:
    marker = Path(td) / "marker.txt"
    marker.write_text("fixture-only", encoding="utf-8")
    expression = (
        f"abs.__self__.open({str(marker)!r}).read() == 'fixture-only'"
    )
    print(_eval_predicate(expression, {}))
```
Expected by source: `(True, None)`, despite file I/O being outside the advertised comparison-only namespace. Reads only the temporary marker; no process creation or networking.

---

### 2. High — Shared-daemon DAG controls lack account ownership enforcement

- **CWE:** CWE-639 / CWE-862, authorization bypass through user-controlled identifiers.
- **Locations:**
  - `services/dag/worker_rpc.py:43-82` — `dag_status` accepts arbitrary `run_id` and `runs_dir`.
  - `services/dag/worker_rpc.py:85-138` — approve/abort forward arbitrary run ID, replacement graph, and autonomy setting.
  - `services/dag/executor.py:32-46,226-262` — one process-user run directory; existence and schema checks, but no tenant ownership check.
  - `services/worker_main.py:149-150,353-363` — registers and invokes these RPCs without an authenticated actor argument.
  - `Webservice/gateway/engine_bridge.py:215-219,1020-1056,1072-1082` — document/path controls and specific method denials do not authorize DAG run IDs; `runs_dir` is not a recognized path key.
  - `Webservice/gateway/engine_bridge.py:654-711` — replies for these methods are returned without run-owner filtering.
  - `core/hostd/doc_registry.cpp:433-454` — no-document-ID calls pass registry preflight.
  - `core/hostd/server.cpp:1125-1182` — unknown engine methods reach the worker and successful responses return verbatim.
- **Attacker-controlled source → sink:** An authenticated web account sends `dag_status`, `dag_approve`, or `dag_abort` naming another account’s run. The shared worker looks it up under its global run directory and resumes/rejects it, returning node state. Approval also accepts a replacement `graph_json`.
- **Prerequisites:** An existing DAG run and knowledge of its ID. IDs are not universally secret/random: callers may choose safe identifiers, and `services/dag/mcp_tools.py:715-718` explicitly describes a fresh mission using `"mission"`. No claim is made that every deployed run is guessable.
- **Customer impact:** Cross-account disclosure of DAG topology/results, unauthorized rejection or approval of another customer’s workflow, and graph replacement. Replacement verification predicates can combine with finding 1.
- **Nearby controls considered:** Run IDs reject traversal (`services/dag/run_ids.py:10-30`), but syntactic safety is not ownership. The gateway’s document capabilities do not cover a request containing only a run ID. Native writer-group protections are document coordination, not DAG tenant authorization.
- **Minimal remediation:** At the gateway’s tenant boundary, deny DAG RPCs until owner/workspace-scoped run capabilities exist. Bind persisted runs to authenticated ownership; authorize read, resume, and abort independently. Do not accept replacement graph/autonomy changes as an ordinary approval without separate authorization.

**Safe isolated repro recipe:**
1. Use a temporary `XDG_DATA_HOME`/`APPDATA`; do not connect to a daemon.
2. In a temporary fixture, create `victim.db` as an empty file and `victim.graph.json` containing a benign graph name and node IDs.
3. Call `worker_rpc.WORKER_METHODS["dag_status"]` with that `run_id` and temporary `runs_dir`; no principal is required.
4. Check `engine_bridge.path_violation` using a different temporary workspace root. A request containing only `run_id` and `runs_dir` has no recognized path violation.
5. For the mutation half, create a real **temporary** approval-paused graph using the existing executor-test pattern, replace the gate’s ledger factory with `MemoryLedger`, and point `executor.default_runs_dir` to that fixture. Invoke `dag_abort` by run ID. The worker accepts the rejection without an owner identity. Do not approve a live or real-agent graph.

**Integrator note:** This overlaps the gateway auditor’s generic method-authorization surface; merge overlapping presentation, but retain DAG-specific sinks and replacement-graph behavior.

---

### 3. Medium — Plain-text PDF fields are interpreted as markup and can embed unrelated local images

- **CWE:** CWE-74 / CWE-200, markup injection leading to unintended disclosure.
- **Locations:**
  - `services/deliverables/spec.py:188-214` — preserves heading/body/template prose verbatim.
  - `services/deliverables/templates/smoke.py:66-83` — passes these strings directly to ReportLab `Paragraph`.
  - `services/deliverables/templates/_doc.py:143-162,177-178` — headings, body text, table cells, labels, values, and captions are likewise unescaped.
  - Inspected installed dependency corroboration:
    - `.venv-mcp/lib/python3.11/site-packages/reportlab/platypus/paraparser.py:2705-2722` — `<img src=...>` becomes `ImageReader`.
    - `.venv-mcp/lib/python3.11/site-packages/reportlab/lib/utils.py:522-526,655-665` — opens local paths before URL trust checks and reads image bytes.
- **Attacker-controlled source → sink:** Malicious prose in a supplied report spec, or supplier/model text copied into the report’s prose/table fields, includes an inline image tag. Generating PDF interprets it and embeds an accessible image rather than printing the text literally.
- **Prerequisites:** Attacker influences a PDF prose field, knows an accessible image path, and the resulting report is returned/shared. This is not arbitrary-text-file disclosure, and direct remote invocation of `make_report` through the native worker table was **not** established.
- **Customer impact:** An unrelated customer rendering, screenshot, scan, or drawing image can be included in a report intended for someone else; document content can also be silently altered.
- **Nearby controls considered:** Provenance validation does not escape text. Explicit image fields are a separate intended capability; prose is not an explicit image request. The DOCX path uses text APIs instead.
- **Important exclusion:** **Do not report default ReportLab SSRF here.** Current inspected ReportLab defaults have `trustedHosts=None`, and current URL loading rejects when no hosts are trusted. Local file opening still precedes that check.
- **Minimal remediation:** Escape all plain text before passing it to `Paragraph`, preserving only markup deliberately constructed by the renderer. Resolve actual image fields through an explicit resource policy.

**Safe isolated repro recipe:**
- Create a small PNG under `TemporaryDirectory()/outside-project/`.
- Set both `CHUDWARE_PROJECT_DIR` and `CHUDWARE_ARTIFACTS_DIR` to other directories inside the same temporary fixture.
- Call `api.make_report` with `template="smoke_report"`, `format="pdf"`, and a section body containing:
  ```python
  f'<img src="{temporary_png}" width="12" height="12"/>'
  ```
- Inspect the generated PDF’s image objects using `pypdf`, without opening any real documents. Source predicts the fixture image is embedded, not rendered as literal prose. Repeat with a common `_doc.Doc` report template to cover both renderers.

---

### 4. Medium — Documented MLflow compose service exposes unauthenticated experiment storage on all host interfaces

- **CWE:** CWE-306, Missing Authentication for Critical Function.
- **Locations:**
  - `tools/flywheel/docker-compose.mlflow.yml:16-31` — host port `"5000:5000"` plus `--host 0.0.0.0`; no authentication application or protecting proxy configured.
  - `tools/flywheel/runner.py:716-738` — logs job/model/dataset identifiers, metrics, and attempts adapter-artifact logging.
  - `tools/flywheel/runner.py:854-855` — training invokes that logging path.
- **Attacker-controlled source → sink:** A network peer can reach the host-published tracking service launched by the documented compose command. The server configuration provides no authentication boundary over the experiment APIs.
- **Prerequisites:** Operator starts this optional development/training service and host/network policy permits access. This is a shipped insecure default, **not evidence that a production/customer server currently exposes port 5000**.
- **Customer impact:** Disclosure and tampering/deletion of experiments and model-training metadata; possible access to successfully stored artifacts. I do not claim that every adapter upload succeeds or that raw customer corpora are automatically stored in MLflow.
- **Nearby controls considered:** “Human-gated hosting” comments and localhost URLs in usage examples do not limit the published socket. The container volume persists the tracking data but does not authorize access.
- **Minimal remediation:** Bind the published port to `127.0.0.1` by default. Shared deployments need authenticated access and TLS, with an explicitly configured trusted ingress.

**Safe isolated repro recipe:**
- First statically parse the compose file and confirm host publication is not loopback-bound and authentication is absent.
- If runtime verification is desired, the integrator can use an already available image in an isolated environment, temporary storage, and an override publishing only `127.0.0.1:<ephemeral>:5000`.
- Create one synthetic experiment locally; issue an unauthenticated search request against that isolated instance. Never target the documented workstation, production service, or existing tracking data.

---

## yield — positive controls and non-findings

### Current controls supported by source

- **Dataset label deserialization:** `training/viewpoint/datasets/importers.py:306-320` rejects every pickle class/function reference through `_PlainDataUnpickler.find_class`.
- **Dataset extraction:** `training/viewpoint/datasets/fetch.py:452-477` resolves destinations beneath the cache and verifies extracted ZIP members against CRC; `:590-602` verifies publisher hashes when available, otherwise records a first-download hash. **TOFU/CRC are not publisher authentication.**
- **Runtime utility weights:** `services/viewpoint/utility/model.py:84-112` uses `allow_pickle=False`, validates feature schema, required arrays, and shapes.
- **Explicit publication review:** `training/perception/manifest.py:95-114` and `services/viewpoint/feedback/aggregation.py:58-71` require approved license/consent references, not merely nonempty reference strings. The viewpoint publication filter is conditional on `for_publication=True` (`:111-114,156-158`); it is not a universal training-data filter.
- **Live viewpoint scoring:** `services/viewpoint/flywheel/scorers.py:261-266` requires both configuration and caller opt-in and refuses execution under pytest.
- **Flywheel export:** `tools/flywheel/export.py:610-650` gates offline/global-off/missing-envelope/session-off cases. Valid meta consent strips supported sidecar-reference fields.
- **Provisioning:** `scripts/provision_libredwg.py:53-101` and `scripts/provision_reveng.py:35-64` implement checksum/size/path/link protections rather than blindly unpacking downloaded archives.
- **Safety-related validation sampled:** DFAM rejects nonfinite vertices, invalid indices, and all-degenerate meshes (`services/dfam/mesh.py:28-46`). ECAD design resolves component/pin references and validates analysis kinds (`services/ecad_design/orchestrator.py:276-351`). DAG tool failures and predicate errors do not automatically pass.
- **DAG persistence:** Run-ID traversal rejection is present. Event journaling explicitly flushes and fsyncs (`services/dag/event_journal.py:73-86`). Neither establishes encryption, retention limits, secure backup, or tenant isolation.

### Policy gaps / hypotheses — not promoted to exploitable findings

1. **Training checkpoint pickle loads:** `training/viewpoint/pixel_train.py:434`, `observer/train.py:555,680`, and `utility/train.py:427` explicitly use `weights_only=False`. Schema/signature checks follow deserialization. These remain unsafe for untrusted checkpoints, but the traced callers use local training/resume artifacts; no customer-facing upload/download-to-load path was established. Do not label them remote RCE on this evidence.
2. **Developer provisioning trust:** `scripts/fetch_cef_linux.sh:32-46`, its Windows counterpart, and the compatibility fallback in `tools/provision_renode.py:244-249` retain unsafe archive defaults on some Python versions. `tools/provision_openfoam.py:55-60` intentionally executes an upstream installer with sudo in WSL. These are explicit developer installations, not remotely supplied customer shell commands. Release pin/supply-chain closure belongs to the other assignment.
3. **Malformed session consent:** `tools/flywheel/export.py:643-657` only specially rejects `"off"` and clamps the exact `"full"`→`"meta"` combination. Unknown/null session values can avoid meta stripping. Valid-tier tests do not prove malformed-input fail-closed behavior. No upstream customer-controlled producer of malformed envelopes was established.
4. **Consent/approval is not an end-to-end upload gate:** Argilla submission (`tools/flywheel/argilla_loader.py:90-196`) accepts supplied shards without rechecking application consent. Training (`tools/flywheel/runner.py:456-477`) recursively loads all JSONL under the chosen directory rather than enforcing approved snapshots. Its DVC push (`:863-870`) is repository-wide. These are operator-controlled workflows; do not claim all uploads/training necessarily revalidate consent or human approval.
5. **Promotion signoff is process convention, not authentication:** `services/serving/promotion.py:180-240` checks a nonempty approver/report reference, report existence, and supplied evidence. It does not independently authenticate the approver. No untrusted remote entry point was found.
6. **Argilla deployment:** The compose file publishes its UI broadly and supplies a predictable fallback signing-secret value (`tools/flywheel/docker-compose.argilla.yml:37-44`). Authentication/token-forgery impact was not established against current Argilla dependency source. Elasticsearch is security-disabled but **not host-published**; do not call it publicly exposed from this file alone.
7. **Optional SVG renderer:** `services/deliverables/engines.py:147-169` uses optional CairoSVG without a supplied resource policy, but CairoSVG was not found in the inspected runtime. External-resource behavior remains dependency/deployment-dependent.
8. **Artifact roots:** Deliverables deliberately allow absolute desktop paths and keep artifacts in a hidden per-project store. That alone is not a same-user privilege escalation. Shared-web exposure of a configured global artifact root needs deployment context beyond this audit.

---

## yield — reviewed_paths and coverage inventory

This was a **sink-oriented source review, not a line-by-line certification** of every file in the large target trees.

### Assigned and extended source areas reviewed

1. **`training/`**
   - Inventory of viewpoint, perception, simulation, recipes, jobs, benches, and corpus subareas.
   - Dataset downloader/importer controls and existing importer tests.
   - Viewpoint pixel/observer/utility checkpoint-loading sites and campaign orchestration.
   - Perception manifest/publication/reproducibility paths.
   - Simulation registry governance and source-path/hash verification.
   - Job schema and training dependency inventory.
   - Actual data, weights, generated reports, and corpus records were not opened.

2. **`tools/`**
   - Full top-level inventory and dangerous-sink searches.
   - Detailed flywheel export, Argilla submission/snapshot, DVC helpers, training runner, compose services, README, and relevant test source.
   - Provisioning paths for Renode/OpenFOAM/EM; translator/VLM model-serving and loopback defaults; GNN dependency declarations.
   - Benchmark/dev-driver code received sink-oriented search coverage, not complete behavioral review.

3. **`scripts/`**
   - Full inventory and searches for downloads, archive extraction, shell execution, process launch, and listening sockets.
   - CEF, LibreDWG, reverse-engineering provisioning, and root build entrypoints inspected.
   - Developer mission/gate/smoke/device-qualification scripts classified as explicit operator tooling.
   - CI/release pin validation intentionally left to its assigned reviewer.

4. **`services/dag/`**
   - Graph validation, context propagation, verify dispatch/evaluation, autonomy gates, executor persistence/resume, run IDs, event journal, MCP entrypoints, worker RPCs, and relevant test source.
   - Native/gateway forwarding was narrowly traced to confirm the tenant-boundary finding.

5. **`services/deliverables/`**
   - API, project roots, schema/provenance resolution, render engines, worker/MCP surfaces, shared PDF/DOCX rendering, workbook/report templates, machining work-instruction controls, and test fixtures.
   - Installed ReportLab implementation checked specifically to distinguish local-image inclusion from blocked-by-default URL fetching.

6. **Extensions warranted by the requested patterns**
   - `services/serving/{routing,promotion}.py`.
   - `services/viewpoint/{feedback,flywheel,utility}` plus source searches across its other runtime areas.
   - `services/ecad_design/orchestrator.py`.
   - `services/requirements/requirements.py`.
   - Selected DFAM input checks and verify-battery result handling.
   - Narrow corroboration in `services/worker_main.py`, gateway engine bridge, and hostd worker/registry dispatch.

7. **Root inventory/dependencies**
   - `README.md`, `requirements.txt`, `config.example.json`, `CMakeLists.txt`, `build.sh`, `repomix.config.json`.
   - No dependency advisory execution or release-artifact pin verification.

### Remaining root directories classified

| Classification | Root directories | Coverage disposition |
|---|---|---|
| Active product code | `core/`, `host/`, `services/`, `Webservice/` | Other audit assignments; only narrowly related forwarding/source paths examined here |
| Active tooling/code | `tools/`, `scripts/`, `training/` | Assigned coverage above |
| Other active tooling | `work/tools/`, `benchmarks/`, `samples/`, `assets/` | Inventoried; benchmark runners, sample regeneration, task/issue tooling, and asset generators are **not fully audited here** |
| Build/release configuration | `cmake/`, `packaging/`, `.github/` | Separate release/build assignment |
| Dependencies/vendor SDKs | `vendor/`, `third_party/`, `extern/`, `.venv-mcp/` | Not recursively audited; only identified ReportLab source inspected |
| Fixture/model/data trees | `corpora/`, `fixtures/`, `parts/`; corpus/fixture subtrees elsewhere | Contents excluded, except discovered source/test files discussed above |
| Documentation/contracts | `docs/`, `contracts/`, most of `work/` | Select documents only; not executable authorization guarantees |
| Generated/output/state | `scratch/`, `runs/`, `reports/`, `graphify-out/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.plm/`, `.aicadvault/` | No customer/model/state contents inspected |
| Agent/editor configuration | `.claude/`, `.omp/`, `.cursor/`, `.kimi-code/`, `.vscode/` | Relevant skill instructions only; no recursive security review |

**Gap warning for integrator:** Inventory closure is not audit closure. Ensure the assignment matrix explicitly covers active `work/tools`, benchmark runners, sample regeneration, asset generators, and remaining service families; do not silently classify all of them as inert data.

---

## yield — verification, limitations, and handoff

**Exercised verification:** Source/directory/dependency-source inspection only. All repro recipes are proposed, not executed. No operational claim is made about live listeners, current stored corpora, production approvals, backup retention/encryption, or deployed dependency versions.

**Suggested existing isolated checks for integrator—not run here:**

```text
pytest services/dag/test_verify.py -q -k "not real_interference"
pytest services/dag/test_event_journal.py -q
pytest services/deliverables/test_deliverables.py services/deliverables/test_worker_rpc.py -q
pytest services/viewpoint/feedback/test_feedback.py services/viewpoint/flywheel/test_flywheel.py -q
pytest tools/flywheel/test_export.py::TestConsentGate tools/flywheel/test_export.py::TestMetaTierStripping tools/flywheel/test_export.py::TestScrubbing -q
pytest training/viewpoint/datasets/test_importers.py::test_extracted_members_are_crc_verified_and_repaired training/viewpoint/datasets/test_importers.py::test_git_members_are_blob_verified_and_repaired -q
```

Use temporary appdata/project/artifact roots. Existing tests do **not** establish closure of predicate escaping, DAG tenant ownership, PDF prose-resource inclusion, or network-service authentication; the isolated reproductions address those specific gaps.

**Coverage summary:** Four source-backed findings survive: unsafe DAG predicate evaluation, cross-account DAG control, PDF prose-triggered local image inclusion, and an unauthenticated broadly published training-tracking default. Developer-only execution paths, optional dependency behavior, privacy policy limitations, and unavailable deployment evidence are separated above rather than overstated as proven customer exploitation.
