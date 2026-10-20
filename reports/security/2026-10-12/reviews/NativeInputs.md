## Yield — findings

**Read-only review completed. No files changed; no builds, tests, scanners, reproductions, network calls, or git commands executed.** Findings below are confirmed by current source, not by observed runtime results. Reproduction recipes are proposals for the integrator and use temporary fixtures only.

### 1. High — Document-contained filesystem references bypass the web tenant boundary

**CWE:** CWE-863, CWE-22  
**Impact:** An authenticated customer can load another customer's CAD geometry into an authorized document, then inspect/export it. Native compilation can also rebuild and write cached build state into referenced files outside the customer's workspace.

**Source → sink**
- Gateway checks only strings in the submitted RPC: `Webservice/gateway/engine_bridge.py:215-232,250-275,1072-1084`.
- `source_path` is absent from `_PATH_KEYS`; an import feature submitted directly through document operations consequently escapes that check.
- Opening an allowed `.step+` reads its `document.json` and compiles it without tenant-aware filesystem authorization: `core/doc/session.cpp:16629-16655,16742-16756`.
- Assembly references accept absolute paths and resolve relative traversal: `core/doc/assembly.cpp:127-134,526-554`.
- Referenced native documents are read and compiled: `core/doc/assembly.cpp:215-233,315-332`.
- Legacy import features pass document-controlled `source_path` to `import_bodies`: `core/doc/document.cpp:4383-4395`.

**Nearby controls considered:** Gateway document-ID capabilities and outer-path containment are present. Canonical assembly cycle detection prevents cycles, not cross-tenant access. Embedded BREP normally makes imports self-contained, but attackers can omit it. These are legitimate same-user desktop features; the defect is their exposure through a shared daemon without propagating the gateway's authorization context.

**Minimal remediation:** Enforce caller-scoped filesystem authorization at every native file-open/write sink, including recursively discovered dependencies and import features. Include `source_path` in request validation, but do not treat that alone as fixing references stored inside documents.

**Safe isolated repro:** Create temporary `alice/` and `bob/` directories. Save a benign 10-mm box in `bob/box.step+`. In `alice/assembly.step+`, store:
```json
{"schema":"aicad.asm/1","components":[
  {"id":"leaf","fixed":true,"source":{"path":"../bob/box.step+"}}
]}
```
Check `path_violation` on a synthetic `doc_open` RPC naming only Alice's file: it admits the request. Open that fixture in a standalone native harness and inspect/export the resulting box. Separately check a synthetic `doc_apply_ops` request containing an import feature with `params.source_path` naming Bob's temporary STEP: the collector does not collect that field. Do not use a running shared daemon or actual account data.

### 2. High — SAT counted-string arithmetic permits a nonadvancing allocation loop

**CWE:** CWE-190, CWE-835, CWE-400  
**Impact:** A tiny uploaded SAT, or SAT embedded in DXF/DWG, can consume CPU and grow memory indefinitely inside native parsing, affecting the shared daemon.

**Evidence**
- Decimal length is accumulated without overflow checking: `core/doc/acis_read.cpp:264-267`.
- The substring length is bounded, but the cursor uses the original length: `core/doc/acis_read.cpp:271-273`.
- Parsing runs synchronously: `core/doc/acis_read.cpp:2352-2356`; standalone SAT import reaches it at `core/doc/formats.cpp:1608-1616`.

On the assigned 64-bit platform, the following token has a 22-byte prefix through its space:
```text
@18446744073709551594 
```
Its count is $2^{64}-22$. Thus `q + count` wraps to the token's starting position. Each iteration appends another token and returns to the same input position.

**Nearby controls considered:** The substring itself is clamped to remaining input. Exception handlers do not interrupt a loop that never reaches an error. The daemon watchdog eventually exits the **whole daemon**, rather than isolating the offending parser (`core/hostd/server.cpp:1269-1314`).

**Minimal remediation:** Checked decimal accumulation; reject lengths exceeding `n - q`; advance only using a validated length.

**Safe tiny repro:** Do **not** run the complete looping fixture without containment. A bounded arithmetic witness reproduces the actual transition:
```cpp
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <string>
int main() {
    static_assert(sizeof(std::size_t) == 8);
    const std::string token = "@18446744073709551594 x";
    std::size_t p = 0, q = 1, count = 0;
    while (q < token.size() && token[q] >= '0' && token[q] <= '9')
        count = count * 10 + static_cast<std::size_t>(token[q++] - '0');
    if (token[q] == ' ') ++q;
    assert(std::min(token.size(), q + count) == p);
}
```
A parser fixture, if needed in a separately hard-bounded child, is:
```text
700 0 1 0
1 1e-6 1e-10
body @18446744073709551594 x #
```

### 3. High — Binary PLY reads past the supplied buffer

**CWE:** CWE-125  
**Impact:** Untrusted mesh import reaches native out-of-bounds reads. Shared-daemon availability is at risk. No code execution or reliable information-disclosure claim is made.

**Evidence**
- Scalar reader dereferences `p[i]` for widths up to eight without an end pointer: `core/doc/formats.cpp:2490-2513`.
- Vertex parsing checks only `p >= end`, not remaining scalar width: `core/doc/formats.cpp:2623-2628`.
- Face-list parsing has no check for the list's complete payload or each element: `core/doc/formats.cpp:2638-2649`.
- Native routing: `core/doc/formats.cpp:2712,3967-3974`; raw-read RPC: `core/doc/session.cpp:15904-15916`.

**Minimal remediation:** Make scalar reads accept the end pointer and validate the complete width using subtraction. Validate supported property types, list counts, and the complete list byte extent before reading.

**Safe isolated repro:** In a sanitizer-instrumented standalone harness, write this header followed by **one byte** to a temporary file, then call `read_mesh(path, "ply")`:
```text
ply
format binary_little_endian 1.0
element vertex 1
property float x
property float y
property float z
element face 0
property list uchar int vertex_indices
end_header
```
The first four-byte scalar read crosses the one-byte body. Keep the fixture and harness separate from hostd.

### 4. High — PLY face indices reach unchecked mesh indexing

**CWE:** CWE-129, CWE-125  
**Impact:** A syntactically complete ASCII PLY can trigger out-of-bounds native accesses during mesh conversion. This is independent of the binary byte-boundary defect above.

**Evidence**
- ASCII face indices are accepted without range or extraction-success checks: `core/doc/formats.cpp:2608-2617`.
- Fan triangulation casts indices to unsigned without validation: `core/doc/formats.cpp:2588-2594`.
- `mesh_to_shape` immediately computes smooth normals: `core/doc/formats.cpp:3293-3295`.
- Smooth-normal construction uses those indices in unchecked nested vector indexing: `core/doc/formats.cpp:2865-2876`.

**Nearby controls considered:** Vertex-line length is checked. Triangle index validity is not checked before the first dangerous access. Later solid-validity analysis runs too late.

**Minimal remediation:** Before accepting faces or constructing mesh geometry, require successful parsing, bounded nonnegative counts, finite coordinates, and every index in `[0, vertex_count)`.

**Safe isolated repro:** Temporary complete fixture:
```text
ply
format ascii 1.0
element vertex 3
property float x
property float y
property float z
element face 1
property list uchar int vertex_indices
end_header
0 0 0
1 0 0
0 1 0
3 0 1 3
```
In a standalone ASan harness, call `import_bodies(path, "ply")`. Index `3` is exactly one past the three-vertex array. An even safer preliminary check is `read_mesh`, asserting that it returns an out-of-range triangle instead of rejecting the input.

### 5. High — DXF array INSERT expansion has no work/output limit

**CWE:** CWE-400  
**Impact:** A small drawing can cause excessive native geometry copying, CPU use, and memory growth.

**Evidence**
- Integer tag parsing accepts `long` values through `std::stol`: `core/doc/formats.cpp:781-784`.
- Caller-controlled columns and rows have only a lower bound: `core/doc/formats.cpp:1216`.
- Both loops run their full product and copy geometry: `core/doc/formats.cpp:1233-1253,1265-1279`.
- Block depth is capped at 16 (`core/doc/formats.cpp:1196,1207`), but that does not bound array multiplicity or accumulated output.

Even ordinary positive row/column values within signed 16-bit DXF ranges can request hundreds of millions of copies.

**Minimal remediation:** Checked array-product admission and a cumulative import budget covering instances, edges, vertices, triangles, and nested expansion. Reject excessive requests before copying.

**Safe isolated repro:** Adapt the temporary block fixture in `core/tests/test_import_roundtrip.cpp:415-439`: retain one LINE in block `B`, one INSERT, and add tags `70=4`, `71=4`, `44=2`, `45=2`. A standalone import should produce 16 placed lines. This bounded witness exercises the exact uncapped path; do not execute huge counts.

### 6. High — SolidWorks DAG expansion can grow exponentially below the depth limit

**CWE:** CWE-400  
**Impact:** A compact modern `.sldasm` can request exponentially many output instances. Native import consumes daemon resources; Python import additionally duplicates mesh arrays per instance.

**Evidence**
- Native recursion enforces depth/cycle checks but appends an output for every path: `core/doc/solidworks_tessellation.cpp:596-623`.
- Python follows the same path expansion and copies mesh arrays: `services/formats/formats_solidworks.py:411-446`.
- Native geometry instantiation then iterates all outputs: `core/doc/formats.cpp:3938-3961`.

An acyclic chain where every assembly model references the next twice yields $2^d$ leaf instances. Depth 30 remains below the 64-level guard.

**Minimal remediation:** Count expanded instances with checked arithmetic before expansion and enforce a cumulative output/work limit during traversal. Cycle/depth limits are still useful but insufficient.

**Safe isolated repro:** Reuse `_chunk` and `_tetra` from `services/formats/test_formats_solidworks.py:23-39`. Construct one part tessellation and an XML tree with **eight** assembly models, each referencing the next twice; the last references the part. `_portable_tessellations` returns 256 instances. Native `read_solidworks_tessellation` exercises the same bounded graph without performing sewing. Do not increase depth to denial-of-service scale.

### 7. High — SolidWorks stream inflation has no aggregate budget

**CWE:** CWE-400, CWE-409  
**Impact:** A compact assembly can accumulate many individually admissible expanded streams before geometry validation, exhausting memory.

**Evidence**
- Native per-stream allocation is allowed up to 512 MiB: `core/doc/solidworks_tessellation.cpp:121-129`.
- Every distinct wanted `FaceTessellations/…` name is retained in the map, with no aggregate limit: `core/doc/solidworks_tessellation.cpp:113-118,190-194`.
- Python similarly retains all distinct wanted streams: `services/formats/formats_solidworks.py:186-192,222-235`.
- Native assembly parsing begins only after `read_modern_streams` returns: `core/doc/solidworks_tessellation.cpp:533-556`.

**Nearby controls considered:** Native inflation correctly bounds each destination and rejects size mismatch. Neither port bounds the number or total expanded bytes of retained streams. Invalid tessellation syntax does not help because all streams inflate first.

**Minimal remediation:** Enforce checked aggregate expanded-byte and stream-count limits before allocating each destination, plus an input-file admission limit.

**Safe isolated repro:** Create two distinct wanted `FaceTessellations/` chunks containing 1 MiB of repeated benign bytes each, using the existing `_chunk` helper. Call Python `_modern_streams(path, True)` and assert the returned total is 2 MiB. A native standalone harness can use the same fixture under a modest memory ceiling. The larger exhaustion consequence follows from the inspected absence of aggregate admission; it was not exercised.

### 8. Medium — Python SolidWorks inflater trusts claimed size until after unrestricted decompression

**CWE:** CWE-409, CWE-400  
**Impact:** A malicious file bypasses the advertised per-stream expanded-size bound in Python import/preview consumers.

**Evidence:** `services/formats/formats_solidworks.py:227-232` checks the claimed `expanded` field, calls unrestricted `zlib.decompress`, then compares the actual length. Actual output can therefore exceed both the claimed size and 512-MiB policy before rejection.

**Nearby controls considered:** Compressed size and claimed expanded size are checked. Native `tinfl_decompress_mem_to_mem` uses a bounded output buffer and does **not** have this specific defect.

**Minimal remediation:** Use incremental inflation with a hard output cap tied to both advertised size and policy; reject surplus output before allocating it.

**Safe isolated repro:** Use `_chunk("Contents/DisplayLists", b"A"*4096)`, overwrite the chunk's `<I` expanded-size field at offset `0x16` with `1`, prefix the normal 15-byte container header with key byte `4`, and call `_modern_streams`. It reports that 4096 bytes were expanded despite a claim of one byte. The fixture is only 4 KiB expanded.

### 9. Medium — Metadata inspection bypasses archive limits through case-insensitive lookup

**CWE:** CWE-180, CWE-409  
**Impact:** Native inspect/relocation preflight can inflate an otherwise inadmissible JSON entry from an untrusted archive.

**Evidence**
- Metadata preflight matches only exact lowercase names: `core/doc/stepplus.cpp:462-464`.
- Subsequent lookup uses flags `0`, then sizes the buffer directly from ZIP metadata: `core/doc/stepplus.cpp:495-503`.
- The current local miniz dependency performs case-insensitive lookup by default: `core/build-occt-linux/_deps/miniz-src/miniz.c:4341-4348,4430-4434`.
- Reachable native inspect entrypoint: `core/doc/stepplus.cpp:941-952`; relocation metadata preflight: `core/doc/stepplus.cpp:1045-1046`.

An uppercase `DOCUMENT.JSON` is skipped during metadata admission but later found and extracted as `document.json`.

**Nearby controls considered:** Ordinary editable open validates all required/unknown entries, so this specific bypass concerns Metadata mode. The web bridge currently blocks `doc_file_operations`; do not present this as a direct browser RPC exploit.

**Minimal remediation:** Use exact case-sensitive lookup consistently and reject ambiguous normalized names, or validate the actual located entry before allocating/extracting.

**Safe isolated repro:** Write a temporary ZIP with lowercase `meta.json` and uppercase `DOCUMENT.JSON` containing 64 KiB of spaces followed by `{"schema":"aicad.part/1"}`, compressed at level 9. Compare ordinary `stepplus_open` with `native_file_operations({"action":"inspect","paths":[absolute_fixture_path]})`: ordinary open's ratio policy applies, while Metadata mode skips the uppercase entry's admission checks. Keep both calls in a standalone harness.

### 10. Medium — Attacker-authored solved caches can contradict the editable feature history

**CWE:** CWE-345  
**Impact:** A supplied CAD file can show trustworthy-looking dimensions/history while serving different solved geometry, volume, and feature-success state. This creates design-integrity and potential physical-safety risk downstream; no physical execution was exercised.

**Evidence**
- Cache acceptance relies on attacker-writable engine key, document hash, and `solved` flag: `core/doc/session.cpp:16388-16404`.
- Cached response is returned and compiled geometry hydrated without feature execution: `core/doc/session.cpp:16439-16470`.
- Hydration trusts cached feature-status and volume: `core/doc/session.cpp:16318-16343`.
- Assembly component loading directly accepts cached BREP after the same gates: `core/doc/assembly.cpp:306-318`.
- Document hash is public, deterministic FNV-1a: `core/doc/stepplus.cpp:1155-1175`.

**Nearby controls considered:** These gates correctly detect accidental staleness. Body counts/names and binary framing are checked. They do not establish that supplied cache geometry was computed from supplied features; no hash collision is needed.

**Minimal remediation:** Distinguish trusted locally produced cached state from externally supplied state. Recompute or independently validate untrusted caches before treating dimensions/statuses/geometry as mutually authoritative. Merely replacing FNV with SHA-256 does not fix an attacker-controlled cache/header pair.

**Safe isolated repro:** In a temporary standalone C++ harness, save two benign box documents with the same feature identifiers but different dimensions. Copy box B's `cache.json`, `cache.brep`, and `cache.aicm` into box A; remove `cache.ckpt`; set the copied header's `doc_hash` to `stepplus_doc_hash(A.doc)`. Save the hybrid fixture. Compare `doc_open`'s returned volume with a fresh uncached `compile_document(A.doc, …)` result. No credentials, malformed native binary, or physical device is needed.

### 11. Medium — Ordinary native save follows a predictable staging-file symlink

**CWE:** CWE-59, CWE-377  
**Impact:** An untrusted collaborator able to create entries beside a document can cause saving it to truncate another file writable by the saving process. This is a filesystem-collaboration risk, **not** evidence of remote privilege escalation by an authorized same-user desktop tool.

**Evidence**
- Ordinary save stages at the fixed name `<destination>.tmp`: `core/doc/stepplus.cpp:256-259`.
- Exclusive private staging is used only for relocation: `core/doc/stepplus.cpp:260-278`.
- Ordinary writer opens that path without no-follow/exclusive protections: `core/doc/stepplus.cpp:306-308`.
- Current miniz opens it with `"wb"`: `core/build-occt-linux/_deps/miniz-src/miniz.c:5858-5873`.
- Final rename occurs after the truncating open: `core/doc/stepplus.cpp:379-383`.

**Minimal remediation:** Use safely reserved unique staging for ordinary saves too; open without following symlinks and retain the reserved file identity through writing/publication.

**Safe isolated repro:** Within one `TemporaryDirectory`, create a sentinel file and a symlink `model.step+.tmp` pointing to it. Call `stepplus_save` with a benign empty part document to `model.step+`. Compare the sentinel's original bytes with its contents afterward. All files must remain inside the temporary directory.

## Yield — positive controls and classifications

- Normal native archives enforce **4096 entries, 64-MiB per-entry, 256-MiB aggregate, and compression-ratio admission** before required payload extraction: `core/doc/stepplus.cpp:62-68,439-491`. Optional oversized caches are skipped with diagnostics rather than preventing editable open.
- Archive members are retained in memory; the reviewed native open path does **not** extract member names onto the filesystem. No ZIP-slip finding survives for that path.
- DWG byte/bit readers perform explicit boundary checks; decompression/page extents are bounded and checksummed: `core/doc/dwg_native.cpp:38-100,329-459`. Current tests contain corresponding corrupt-input cases.
- Solved-BREP wrapper checks magic, body count, and outer byte lengths: `core/doc/stepplus.cpp:1200-1232`. This is not proof of third-party BinTools parser safety.
- Geometry-cache engine epochs, document hashes, dependency fingerprints, and writeback stamps provide genuine stale-cache controls. They are not authentication of untrusted caches.
- Native converter execution uses argument vectors, not a shell, and has a deadline: `core/doc/dwg.cpp:133-190`. Python DWG converter calls likewise use argv and deadlines: `services/formats/formats_dwg.py:65-102,135-145`.
- Expression evaluation uses a restricted arithmetic parser and function allowlist, not Python `eval` or general executable code: `core/util/expr.cpp:113-215,247-312`. No executable-document RCE finding was established.
- SVG text escaping was present: `core/doc/svgvec.cpp:31-48`.
- **Policy/operational gap, not a confirmed exploit:** ordinary save/writeback uses rename without reviewed file-and-directory fsync durability. Crash/power-loss guarantees require deployment/filesystem evidence.
- **Unverified hypotheses, not findings:** deep expression nesting/CBOR nesting causing stack exhaustion; hostile internal OCCT/BinTools lengths; build-state writeback races after the final stamp check. No runtime proof or concrete additional corruption path was established here.

## Yield — reviewed paths and limits

1. **Native persistence and compilation:** `core/doc/stepplus.{cpp,h}`, relevant sections of `session.cpp`, `document.cpp`, `assembly.cpp`, `validate.cpp`.
2. **Native format parsing/conversion:** relevant sections of `formats.{cpp,h}`, `acis_read.{cpp,h}`, `dwg.cpp`, `dwg_native.cpp`, `dxf.{cpp,h}`, `solidworks_tessellation.cpp`; SVG/PDF output escaping and file-write sections.
3. **Geometry/utilities:** inventories and filesystem/execution searches over `core/geom`; focused reads of `aicm.cpp`, `airfoil.cpp`, `text.cpp`; `core/util/process_isolation.cpp`, `aicm_handoff.cpp`, `expr.cpp`, resource-governor interface.
4. **Python format services:** focused import/export, parser, converter, mesh-carrier sections of `formats.py`, `formats_solidworks.py`, `formats_dwg.py`, `formats_dxf.py`, `libredwg.py`, `mesh_body.py`.
5. **SolidWorks feature services:** assembly extraction/COM lifecycle in `sw_feature_import.py`; translation/entrypoint discovery in `sw_translate.py`.
6. **Boundary dependencies:** relevant path-validation sections of `Webservice/gateway/engine_bridge.py`; marketplace native-open callsites; hostd watchdog behavior.
7. **Existing tests read:** relevant sections of `test_sim_cache.cpp`, `test_appearance.cpp`, `test_import_roundtrip.cpp`, `test_export_assembly.cpp`, `test_formats_registry.cpp`, `test_dwg_native.cpp`, `test_acis_read.cpp`; Python format tests, `services/mcp/test_sw_feature_import.py`, gateway `test_engine_path_jail.py`.
8. **Third-party dependency evidence:** current local miniz source/header lookup and writer-open behavior; CMake dependency configuration.

**Explicit limits:** Large compilation/session files were reviewed selectively, not line-by-line. Pure geometry algorithms were primarily searched for relevant input/sink surfaces. OCCT, BinTools, lib3mf, openNURBS, FreeType, ezdxf, trimesh, OLE, and SolidWorks COM internals were not fuzzed or exhaustively audited. No advisory scan was run. Actual deployment permissions, cgroups, watchdog configuration, gateway/daemon filesystem co-location, and recovery durability remain integrator/deployment evidence. Secret-store/runtime-security internals and actual credential files were not inspected. LSP was not available among this worker's exposed tools.

## Suggested existing checks — not executed

For integrator-selected existing builds:
```text
ctest --test-dir <existing-core-build> \
  -R '^(sim_cache|appearance|import_roundtrip|export_assembly|formats_registry)$' \
  --output-on-failure
```

Python:
```text
pytest services/formats/test_formats_solidworks.py \
       services/formats/test_formats_dwg.py \
       services/formats/test_formats_dxf.py \
       Webservice/gateway/tests/test_engine_path_jail.py
```

Use isolated sanitizer harnesses for the two PLY findings and bounded temporary-fixture reproductions above. Existing happy-path, stale-cache, converter-timeout, and archive-limit tests do not by themselves establish closure of these findings.
