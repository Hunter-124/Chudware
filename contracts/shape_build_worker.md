# Shape-build worker contract (v1)

This internal POSIX process boundary contains native fillet construction and its named
validation policy. It is not an agent tool, public RPC endpoint, interchange format,
security sandbox, second GUI, or persistent CAD engine. The document, transaction,
selected-edge and radius-correction contracts remain authoritative in
[feature parameters](feature_params.md) and [document operations](doc_ops.md).

## Ownership and runtime placement

- [`dress.cpp`](../core/geom/dress.cpp) owns fillet search, edge coverage, endpoint
  intent, probe construction and result classification.
- [`process_isolation.h`](../core/util/process_isolation.h) and
  [`process_isolation.cpp`](../core/util/process_isolation.cpp) own the local transport,
  executable discovery, descriptor lifetime, deadline, child termination and reaping.
- [`shape_build_worker.cpp`](../core/util/shape_build_worker.cpp) is the executable entry.
  It links the same [document solid validator](../core/doc/document.cpp) used by
  explicitly in-process document builds.
- `chudware-shape-build-worker` is required beside the actually loaded POSIX engine
  library, or beside a statically linked geometry/document executable. It comes from
  the same selected build tree. It is not discovered through `$PATH` or relative to
  an unrelated Python executable.
- Packaged Linux places the helper beside `usr/lib/libchudware.so`; macOS places it
  beside the engine in `Contents/Frameworks`. Containers and cells place both in
  `/opt/chudware/bin`. Windows retains its existing in-process geometry policy and
  does not require this POSIX executable.

## Invocation and input

The parent uses `posix_spawn`, never an OCCT callback executed after forking a
multithreaded host. Each probe gets a fresh executable and process group.

The private CLI is `--fillet-v1` followed by decimal encodings of the first and second
IEEE-754 binary64 bit patterns, flags, the absolute steady-clock deadline in nanoseconds,
and the owning parent PID. It is a same-build, same-machine protocol; it is not a
portable file format. Unknown arguments or flags fail unavailable.

| Field | Meaning |
|---|---|
| Flag 1 | A second radius is present. |
| Flag 2 | The authored variable endpoints were unequal; requires flag 1. |
| Flag 4 | Preserve operation-provenance recording. |
| Flag 8 | Apply the document-solid validation policy. |
| FD 3 | One source BinTools B-rep, then a native `uint64_t` edge count and ordered pairs of native `uint32_t` edge index and orientation. |
| FD 4 | Probe response, described below. |
| FD 5, Linux only | The held verified executable used to enter the worker; closed after exec. |

Edge indices are one-based `TopExp::MapShapes` indices of the deserialized source.
The ordered selections retain each original `TopAbs_Orientation`. Source encoding
is reused across the bounded search; it must not mutate authored source shapes or
replace the selected edges with an implicit or partial selection. The worker checks
parent identity, finite values, edge bounds and transport sizes before using them.
Only the reserved transport descriptors and standard streams survive the worker
descriptor-cleanup policy.

## Validation, response and failure

The basic policy retains kernel validation. The document-solid policy uses the same
closed-solid, BRepCheck, finite nonzero face-area and non-destructive per-solid
self-intersection checks as document compilation. An arbitrary host callback cannot
cross exec. Custom basic-policy callbacks are available only with an explicitly
in-process policy; strict isolation rejects them rather than silently ignoring them.

FD 4 contains a native `uint64_t` sidecar length, those diagnostic/provenance bytes,
and, on success, the BinTools result. Construction refusal can retain diagnostics
without a shape. The parent checks the file extent, declared length, decoding and
result consistency before admitting it. No partial transport is a usable result.

| Exit or condition | Parent classification |
|---|---|
| 0 with a valid complete payload | Success; the ordinary all-edge and radius-result checks still apply. |
| 101 with a valid diagnostic payload | Construction or geometry refusal, not success. |
| 103, missing helper, failed verification, malformed transport or spawn failure | Unavailable; never an inferred radius bound. |
| 104 or expired owning deadline | Timed out; terminal for the search. |
| Signal or unexpected exit | Crash; never an inferred radius bound. |

The existing limits remain at most 72 probes, a 30-second aggregate search and
at most five seconds for an isolated probe. The parent deadline covers exec,
construction, validation and result transfer, not just the kernel constructor.
Timeout kills the owned process group and reaps the direct child. Parent death
terminates the worker. Scheduling and caller-side serialization are not hard-real-time
guarantees. A prior legacy chamfer/fork timeout must never retire strict fillet
isolation, cause an in-process retry, or poison a later operation.

## Integrity and deployment

Each search opens the helper without following a symlink and keeps its descriptor
for all probes. Production authenticates that descriptor against the signed manifest
row of kind `worker`; there is no per-probe whole-payload rehash. Linux executes the
verified inode through `/proc/self/fd/5`. Other POSIX platforms retain the verified
canonical path while holding its descriptor, matching the native-loader policy;
this is not a claim of Linux-style inode-pinned execution on macOS. Developer builds
retain their explicit build-tree policy.

Missing, empty, nonexecutable or untrusted helpers must not silently enable an unsafe
fallback. Native bundles include the helper and its dynamic dependency closure
before signing and manifest generation. Updating mapped files atomically prevents
overwrite corruption, but does not migrate live daemon documents or holds: see
[hostd upgrades](../docs/reference/HOSTD.md). Do not deploy over sessions whose
continuity must be preserved.

## Search outcome clarification (v2, append-only)

An individual timeout remains terminal for the search and is never a radius
bound. If a previous probe already produced a fully validated all-edge result,
the parent returns that result with a conservative search resolution; otherwise
it returns the timeout. Aggregate budget exhaustion follows the same rule.
This does not change the v1 worker protocol or relax isolation and validation.
