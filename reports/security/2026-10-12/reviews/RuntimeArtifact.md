## Yield — findings

Read-only source review completed. **No files changed; no commands, tests, scans, downloads, runtime binaries, or package installations executed.** Severity below assumes the stated attacker prerequisites—not an unauthenticated customer-to-host exploit.

### SC-1 — High: Private RPC package can resolve from the public package index

**Classification:** Confirmed configuration defect; public namespace ownership was not queried.  
**CWE:** CWE-829, Inclusion of Functionality from Untrusted Control Sphere.

**Locations**
- `requirements.txt:36-42` explicitly identifies `chudcode-rpc` as private/not on PyPI, but declares ordinary `chudcode-rpc==0.3.1`.
- `scripts/provision_venv.sh:17-26` installs requirements using `--find-links` when local source exists and normal index resolution when it does not.
- `services/installer/provision.py:304-313` upgrades pip and installs requirements from the index; supplying a wheelhouse adds `--find-links --prefer-binary`, **not** `--no-index`.
- `services/agent/chudcode_host.py:2223` imports `RpcClient` from the resolved package.

**Source → sink**
An attacker able to publish the matching private-package name/version to a consulted public index can supply the dependency when the trusted local package is unavailable. Ordinary pip resolution accepts it; subsequently importing `chudcode_rpc` executes its Python code inside the services worker. A matching source distribution can also execute build-backend code during installation. `--find-links` adds candidates; it is not a source-authority restriction.

**Customer impact**
Worker-code execution permits access to whatever documents, provider credentials, daemon interfaces, and customer data that worker identity possesses. Compromising a build/provisioning environment can propagate into distributable artifacts.

**Nearby controls considered**
- Exact version `0.3.1` narrows the required malicious version, but does not authenticate its publisher.
- A successfully supplied trusted wheel mitigates the straightforward missing-package path, but public-index fallback remains explicit.
- The dependency audit runs **after** installation and validates installed package presence/version, not its provenance.
- This does **not** establish that the signed native installer currently takes this Python provisioning path, nor that the public namespace is presently available.

**Minimal remediation**
Bind the private package to an authenticated, immutable wheel/source identity; install its reviewed wheel with `--no-index --no-deps`, and use a complete hash-locked offline wheelhouse for managed provisioning. Fail closed when the private artifact is absent rather than resolving its name publicly.

**Safe isolated reproduction for integrator**
In a temporary directory, create a metadata-only `chudcode_rpc-0.3.1-py3-none-any.whl` with no Python modules, plus a local PEP 503 `simple/chudcode-rpc/index.html` linking it. In a network-disabled disposable environment, point pip’s index exclusively at that `file://` fixture and run **`pip download`**, not install, for `chudcode-rpc==0.3.1` with an empty `--find-links` directory. The candidate is accepted despite not originating from the private checkout. Disable all inherited pip indexes/configuration. Separately capture the installer’s `subprocess.run` arguments with a stub to demonstrate the absence of `--no-index`/`--require-hashes`; execute no installer subprocesses.

---

### SC-2 — High: Native dependencies and runtime bootstrap tools are consumed without immutable content authentication

**Classification:** Confirmed-by-source supply-chain integrity defect.  
**CWE:** CWE-494, Download of Code Without Integrity Check.

**Locations and execution paths**

| Input | Missing control | Sensitive sink |
|---|---|---|
| OCCT Windows archive | `core/cmake/ProvisionOCCT.cmake:35-49`: versioned release URL; no `EXPECTED_HASH`; existing cached ZIP also accepted unchecked | Extracted `OpenCASCADEConfig.cmake` executes through `find_package` at `:57`; libraries are staged at `:80-89` |
| lib3mf SDK | `core/cmake/ProvisionLib3mf.cmake:48-62`: download/cache accepted without digest | Headers consumed and runtime library selected/staged at `:69-104` |
| nlohmann/json and miniz | `core/CMakeLists.txt:129-143`: archive URLs without `URL_HASH` | Downloaded CMake/source becomes build input |
| Eigen, Spectra, doctest, Clipper2 | `core/CMakeLists.txt:121-125,154-182`: mutable Git tags, not commit identities | Downloaded build/source/header inputs |
| OpenNURBS | `core/cmake/ProvisionOpenNURBS.cmake:42-60`: shallow clone of mutable rhino3dm tag; existing source accepted based on file existence | `add_subdirectory` executes downloaded CMake at `:81` |
| Chudcode Windows build tools—traced bootstrap dependency | `.github/workflows/chudcode-runtime-windows-build.yml:105-121,133-138`: Bun ZIP and rustup installer downloaded without expected hash/signature validation | Bun is executed for its self-reported version; downloaded `rustup-init.exe` executes immediately |

**Attacker prerequisites**
Control over an upstream release asset/tag, its delivery infrastructure, or an untrusted dependency cache. A network attacker additionally needs a transport-verification weakness; this review does **not** assume TLS can be bypassed universally.

**Customer impact**
Substituted build scripts execute on the builder; substituted libraries/toolchains enter the shipped engine or embedded agent. That can compromise confidentiality and integrity, including geometry, simulation results, and manufacturing output. No specific unsafe physical output was reproduced.

**Nearby controls considered**
- HTTPS and version-looking URLs identify origins/releases, not immutable bytes.
- Download status checks reject failed transfers, not substituted valid archives.
- No explicit `TLS_VERIFY` setting was present in the inspected core definitions. Effective TLS behavior depends on CMake version/environment; it should not be represented as universally disabled.
- OCCT/lib3mf extractors’ handling of malicious archive paths was not established; **no separate archive-traversal finding is claimed**.
- Polyhedral-FEA and ONNX Runtime provide materially stronger counterexamples: commit/digest pins are present.

**Minimal remediation**
Use reviewed commit hashes for Git dependencies and checked-in `EXPECTED_HASH`/`URL_HASH` values for every archive/tool bootstrap. Verify cached artifacts before extraction/use. Make TLS verification explicit. Record toolchain and source identities separately from hashes computed after building.

**Safe isolated reproduction for integrator**
No network or compiler is needed:
1. Create a temporary OCCT root containing `occt-combined.zip`.
2. Put only `opencascade-7.9.3-vc14-64/cmake/OpenCASCADEConfig.cmake` inside it. Its entire contents should set `OpenCASCADE_VERSION` and write a harmless marker **inside that temporary root**.
3. A temporary `cmake -P` harness sets `WIN32` true and `OCCT_ROOT` to the fixture, then includes the current `ProvisionOCCT.cmake`.
4. The cached ZIP skips download, is extracted without a digest check, and its configuration writes the marker.
5. Change the fixture contents and repeat in a fresh temporary root: there is no trusted expected-byte identity to reject the substitution.

This demonstrates the unchecked-cache-to-CMake execution path without malicious payloads or product changes. Do not invoke any generated build target.

## Yield — bootstrap trust, positive controls, and unresolved boundaries

### Chudcode pinning: meaningful integrity after trust is established

`vendor/chudcode/CHUDCODE_ARTIFACT.lock:1` records:

- Product **0.4.3**, engine provenance **18.4.4**, RPC protocol **2**.
- Repository commit **`2eaba2f4606984172f31f108459d9529d4ee5e38`**.
- Linux SHA-256: `740c6633bcfd4d994265d64eb5cdb0d32ce7afcf80837eddfb1d5aae1d2b90c0`
- macOS SHA-256: `126ecef341cb03a143b2cbe84585b615cf1b0e359f390de736dc774ad7623ad2`
- Windows SHA-256: `6627829b406aa431331be4dd8ec12c15928eb68030136cfb624f243600eb9fca`

These are **observed declarations**, not hashes recomputed against runtime files.

Positive controls in `scripts/pin_chudcode.py`:
- Canonical JSON, exact schema/key sets, target/canonical filename restrictions, HTTPS URLs without embedded credentials, and notice/source-commit consistency.
- Binary target and SHA-256 verified **before execution** of `--version` (`:367-387`).
- Source and copied binary verified before atomic publication (`:400-420`).
- Notice content hashes checked before and after copying.
- Digest-keyed cache refuses tampered existing content; no silent replacement.
- Authenticated GitHub fetch restricts the API/CDN flow and strips Authorization before the CDN request (`:430-464`).
- `stage` is offline. The artifact is a raw executable, so this path has no archive extraction.

**Trust-bootstrap limitation—not independently reported as a vulnerability:**  
`pin_lock` computes hashes from caller-supplied binaries (`:544-596`), writes a prospective lock, and verifies against that newly generated identity. That proves consistency, not publisher authenticity or correspondence to `repository_commit`. `--version` is self-reported. The maintainer must authenticate the inputs **before** pinning.

Windows source acquisition checks a separately supplied SHA-256 before extraction (`scripts/build_chudcode_windows.ps1:34-49`), and uses frozen Bun/locked Cargo dependencies (`:85,95`). The repository does not establish how that source hash was independently authenticated. An archive directory named after a commit is not proof of that commit’s source contents.

**Staging limits:** predictable `chud.tmp`/notice `.tmp` paths and pathname-based check/use sequences assume trusted staging directories. I found no concrete lower-trust staging-directory entry point in this assignment; therefore no speculative local race finding.

**Transport availability:** unauthenticated fetch checks redirects only after `urlopen` follows them and refuses a changed final URL (`:494-497`). The checked-in GitHub release URLs ordinarily require redirects **[INFERENCE]**; authenticated fetch or explicit offline inputs avoid that path. No live availability was tested.

### Embedded runtime isolation cannot be certified from these artifacts

`vendor/chudcode` contains the lock and two notices, **not the runtime implementation or RPC client source**. The private source archive, runtime binary, and dependency locks were not downloaded.

Current host-facing evidence:
- `services/agent/chudcode_host.py:100-103,2306-2313` exposes `read`, `bash`, `edit`, `write`, `glob`, `grep`, `eval`, `task`, `wait`, `todo`, and `web_search` to ordinary sessions.
- The inspected launch path supplies cwd, environment, tools, and configuration—not an observable OS sandbox (`:2231-2324`).
- Web credential scope provides fresh HOME/config directories, clears inherited environment values, and separates session directories (`:155-240`). **These are credential/configuration controls, not evidence of filesystem/process isolation.**
- Host tools reapply policy and Ask/Plan restrictions at dispatch (`services/agent/chudcode_tools.py:1470-1515,1571-1590`).
- Ordinary builtin tools are a separate execution path. The configured builtin list does not vary with Ask/Plan mode in `_make_client`.
- Private helper completions explicitly request zero tools and no skills/rules (`chudcode_host.py:1841-1879`).

**Unresolved high-impact hypothesis:** a web model turn might use builtin shell/eval/filesystem tools outside CAD’s host-tool authorization or workspace jail. **Do not classify this as a confirmed cross-tenant exploit based on tool names alone.** Exactly unavailable for audit: builtin path confinement, shell/eval child privileges, process/network access, subagent restriction inheritance, RPC `tools=[]` semantics, and any runtime-internal web/Ask/Plan enforcement. The parent should reconcile this boundary with gateway/worker findings before asserting tenant isolation.

A safe future runtime check would require the exact authenticated runtime, a disposable network-disabled container, synthetic sibling tenant directories, and fake credentials—never the shared production daemon.

### Shared UI manifest

`assets/ui-shared-manifest.json` contains vocabulary, thresholds, static asset provenance, and six font hashes; it is not executable configuration.

`scripts/verify-ui-shared.py:38-40,147-161,240-257` independently freezes the manifest hash and checks vendored font bytes/symlinks. Its declared manifest digest is:

`6badb552d943981c97e852c7ecfaf885bbbfa465f2e3f3e7278cf4676f09301f`

No current-byte hash computation or comparison against the private Chudcode checkout was performed.

## Yield — native dependency inventory for advisory correlation

| Dependency | Current source declaration/provenance | Important qualification |
|---|---|---|
| OCCT | **7.9.3**, official Open-Cascade-SAS Windows release; `ProvisionOCCT.cmake:21,35-37` | Linux/macOS use unconstrained system `find_package`, not verified installed 7.9.3 |
| OCCT Windows bundled libraries | jemalloc, TBB, FreeType, FreeImage, FFmpeg, OpenVR; `ProvisionOCCT.cmake:61-76` | Exact bundled versions unavailable without archive |
| lib3mf | **2.5.0**, 3MFConsortium release; `ProvisionLib3mf.cmake:26,50` | Python requirement is **`>=2.5.0`**, not equivalent fixed provenance |
| OpenNURBS/rhino3dm | rhino3dm tag **8.17.0**, mcneel; `ProvisionOpenNURBS.cmake:27,47-58` | Comment’s abbreviated `6c91d229` is not an enforced commit pin |
| Vendored OpenNURBS | **8.17.25066.07000**; `opennurbs_public_version.h:38-51` | Git revision macros empty |
| **Vendored zlib** | **1.2.3**; `.../opennurbs/zlib/zlib.h:40-41` | Actually linked into `opennurbsStatic`: `.../opennurbs/CMakeLists.txt:603,607,611`; account for possible downstream patches before assigning CVEs |
| **Vendored FreeType** | **2.6.3**; `.../freetype263/include/freetype/freetype.h:4173-4175` | Linux/Android build/link at OpenNURBS `CMakeLists.txt:472-474,598-603`; inspect downstream patches/reachability |
| android_uuid | Android e2fsprogs origin; `.../android_uuid/README.md:1-4` | No reliable package version identified; config’s `VERSION "0.14.1"` is explicitly “version for gettext,” not UUID version |
| Eigen | **3.4.0**, libeigen GitLab tag; `core/CMakeLists.txt:121-125` | Mutable tag |
| nlohmann/json | **3.11.3**, GitHub release; `:129-131` | No archive hash |
| miniz | **3.0.2**, richgel999 release; `:137-143` | No archive hash |
| Spectra | **1.0.1**, yixuan tag; `:154-159` | Mutable tag |
| doctest | **2.4.11**, doctest tag; `:165-170` | Test dependency |
| Clipper2 | **1.5.4**, AngusJohnson tag; `:177-186` | CAM library; mutable tag |
| Polyhedral-FEA | **`a9fe080daf6e15187ab9d31ce7acedb93d6d7fe7`**; `ProvisionPolymesh.cmake:20-23` | Managed fetch pinned; explicit local-source overrides intentionally bypass |
| ONNX Runtime | **1.28.0**; `third_party/polymesh/src/src/advisor/CMakeLists.txt:25-74` | Linux/Windows SHA-256 enforced; CAD disables advisor on unsupported platforms |
| Other system dependencies | CURL, OpenMP, optional CUDA; optional CHOLMOD through Polyhedral-FEA | Installed versions not established |

**Priority advisory handoff:** bundled **zlib 1.2.3 and FreeType 2.6.3** are real nested source dependencies, not merely stale license references. No CVE applicability claim was made without patch/reachability analysis.

Root `requirements.txt` also leaves `anyio`, `langgraph`, `langgraph-checkpoint-sqlite`, `pydantic`, `qrcode`, and `lib3mf` open-ended (`:97-102`), and contains no distribution hashes. This is a reproducibility/provenance gap distinct from proven advisory applicability. Parent-reported advisory results were not rerun.

## Yield — reviewed paths, limits, and checks

**Reviewed**
1. All seven `core/cmake` files.
2. Entire `scripts/pin_chudcode.py` and `scripts/test_pin_chudcode.py`.
3. Artifact lock, first-party notice, third-party notice scope/provenance opening section.
4. Entire root requirements and shared UI manifest; relevant verifier/test sections.
5. Core dependency declarations and feature gates in `core/CMakeLists.txt`.
6. Polyhedral-FEA top-level dependency declarations and advisor CMake.
7. OpenNURBS submodule declaration, version markers, nested library linkage, FreeType build definition, UUID provenance.
8. Traced private-package provisioning and Windows runtime build/bootstrap callers.
9. Host launch/credential/helper/mode-enforcement sections and relevant integration-test assertions.

**Explicit limits**
- Not a full audit of vendored parser implementations, the 27,023-line notice aggregate, the embedded runtime, or installed RPC client.
- No live release availability, installed-version inventory, advisory lookup, signature verification, binary hashing, extraction behavior probe, or deployment configuration verification.
- Existing tests were read as intended contracts, not proof they currently pass.
- Working-tree files were reviewed as encountered; no immutable Git snapshot was created.

**Suggested exact existing checks for integrator, not executed**
- `scripts/test_pin_chudcode.py`
- `scripts/test_verify_ui_shared.py`
- `services/agent/test_chudcode_host.py::test_byok_process_isolated_and_live_session_rejects_credential_switch`
- `services/agent/test_chudcode_host.py::test_cad_session_process_drops_chudcode_dev_prompt_but_keeps_cad_tools`
- `services/agent/test_chudcode_host.py::test_helper_completion_is_private_and_disposed_without_touching_saved_sessions`

Those checks do not establish builtin tenant isolation. Also note that `pin_chudcode.py verify`, `stage`, and `ensure` **execute the binary** after hash verification; they are not static-only audit commands.

**Coverage summary:** two substantiated supply-chain findings, stronger Chudcode/ONNX integrity controls documented, native dependency provenance inventoried, and the unavailable embedded-runtime isolation implementation explicitly left unresolved rather than presumed safe.
