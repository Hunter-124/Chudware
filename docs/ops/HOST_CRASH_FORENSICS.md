# Host crash forensics — `omp` (bun 1.3.14) aborts, 2026-09-05

Root cause found and proven: **`ulimit -v <N>` typed into an `omp` bash tool call sets `RLIMIT_AS`
on the `omp` process itself** (the bash tool is an in-process shell, `$$` == the omp pid). omp/bun
holds ~75 GiB of *virtual* reservations, so any cap of 4–21 GB instantly makes every later
`mmap`/`brk` in omp — and in every child it spawns — fail with `ENOMEM` while RAM is free.

## 1. Evidence

### 1.1 rlimits at crash time (systemd-coredump journal metadata, `COREDUMP_PROC_LIMITS`)

| crashed pid | signal | `Max address space` (soft) | equals | VmSize at crash | cmd |
|---|---|---|---|---|---|
| 1506255 12:41:50 | ABRT | 21 504 000 000 | `ulimit -v 21000000` | 79 650 712 kB (76 GiB) | `bun …/omp` |
| 2273288 13:03:23 | ABRT |  7 168 000 000 | `ulimit -v 7000000`  | 77 915 340 kB (74 GiB) | `bun …/omp --resume` |
| 2471896 13:32:52 | SEGV |  4 096 000 000 | `ulimit -v 4000000`  | 78 186 112 kB (75 GiB) | `bun …/omp --resume` |
| native children `prove` / `mem_calib` ×2 / `chudware-hostd` | all ABRT | 21 504 / 9 216 / 6 144 / 4 096 ×10^6 | inherited from the same shell | 2.5 / 8.8 / 2.8 / 4.0 GB | 12:41, 12:57, 13:03, 13:32 |

Every value is exactly `<N> × 1024` of a `ulimit -v <N>` (KB) issued in this repo's sessions, e.g.
`2026-09-05T16:36:23Z ulimit -v 21000000` (= 12:36 EDT, 5 min before the 12:41 pair) and
`17:03:03Z ulimit -v 6000000`. Live omp today: `Max address space unlimited` — hence the stability.

### 1.2 The in-process-shell proof (non-destructive, reversible)

```
$ echo $$; grep 'core file' /proc/$$/limits      -> 2550592  ... unlimited unlimited
$ tr '\0' ' ' </proc/2550592/cmdline             -> bun /home/hunter/.bun/bin/omp --resume
$ ulimit -c 0; grep 'core file' /proc/2550592/limits -> Max core file size 0 unlimited
$ ulimit -c unlimited                            -> restored
```
`$$` is the omp pid and the builtin mutates `/proc/<omp>/limits`. `pi_natives` imports
`setrlimit64`/`getrlimit64` (`nm -D`), so the `ulimit` builtin is served by the native layer.
No `bash` process exists for tool calls (parent chain: konsole → bash → **omp** → children).

### 1.3 Symbolized aborting thread — core 1 (1506255, 286 MB stored)

```
Thread 1 (LWP 2125103, thread name "tokio-rt-worker"):
#0 __pthread_kill_implementation   #1 raise   #2 abort            (libc.so.6)
#3 pi_natives.linux-x64-modern.node +0x9d9501a      <- Rust alloc-error handler
#4 …+0x9d8f909  (call *GOT[0xaba4520] -> local fn 0x9d94540)
#5-#9 …+0x8fd9601/0x9d73216/0x9d67c29/0x9d8f7ca/0x9d74ae3   (panic/abort runtime)
#10 …+0x7241d6f  = lea msg,%rdi; mov $0x23,%esi; call panic  @ alloc/src/raw_vec/mod.rs:28:5
#30-#33 tokio worker entry -> start_thread -> __clone3
regs: rax 0x0 rdi 0x16fbcf(=1506255) rdx 0x6(SIGABRT); other 75 threads idle in futex/syscall
```
Stack strings of that thread: `~/.omp/logs/native-alloc-1506255-1788626510777.log`,
`tokio-rt-worker`, ` glibc: pthread stack: 2125103`, plus `CMakeFiles/aicad_sim.dir/…` paths
(an `FS_SCAN_*`/`PI_WALK_WORKERS` file-walk was running). That log file says it all:
`pi-natives alloc crash / pid: 1506255 / thread: tokio-rt-worker / size: 47775744 bytes /
alignment: 1 / backtrace: 0..30: <unknown>` —
**malloc(45.6 MiB) returned NULL** with >15 GB free, because VmSize (76 GiB) already exceeded
the 20 GiB `RLIMIT_AS`. Core 2 (2273288): main thread, `abort` called from a one-int-arg noreturn
thunk at `bun+0x3a7cae0` (`call abort@plt` at `+0x3a7cafb`) beneath JIT frames — same NULL-alloc
class on the JS side; core 3 was the unchecked-NULL SIGSEGV (not stored: cores are dropped once
storage/rate limits hit; `drkonqi-coredump-launcher.socket: Too many incoming connections (16)`).

### 1.4 The glibc message is a *different*, non-core-dumping death

`cannot allocate memory for thread-local data: ABORT` exists only in `ld-linux-x86-64.so.2`, called
from **`tls_get_addr_tail`** (`0x12326`) and **`_dl_resize_dtv`** (`0x12417`), both → `_dl_fatal_printf`
→ `_Exit(127)` (`0xe56e: mov $0x7f,%edi; call _Exit`). So that message never produces a signal or a
core: those episodes were omp (or a child) dying silently with status 127 the moment glibc tried to
malloc a per-thread TLS block. `pi_natives` has `PT_TLS memsz 0x109b8` = **68 024 B of dynamic TLS
per thread**, allocated lazily by `__tls_get_addr` — the most frequent malloc victim under an AS cap.

### 1.5 Ruled out

* **Subreaper: NO.** `bun`'s only `prctl` calls are `PR_SET_VMA`/`PR_SET_NAME`/`PR_*_THP_DISABLE`
  (strace of a fresh `bun -e`); `pi_natives` does not import `prctl`. An orphaned grandchild
  reparented to **pid 1713 (`systemd --user`)**: `{"bunpid":2991714,"ppid":1713,"subreaper":false}`.
* **Reproduction: no crash.** Under `systemd-run --user -p MemoryMax=4G … ulimit -c 0`, a bun script
  ran 8× child→orphaned-grandchild `SIGABRT`, 2× orphaned grandchild allocating 1 GiB then aborting,
  and 200 Workers: `{"spawned":8,"exited":[0 ×10],"survived":true,"rss":204952 kB,"threads":16}`,
  `BUN_EXIT=0`. Bun does not die from reaping aborting children/grandchildren.
* **mimalloc interposition: no.** `bun` exports no `malloc`/`mi_*` dynamic symbols (558 dyn syms,
  0 matches), `pi_natives` links no mimalloc/jemalloc → ld.so and Rust both use glibc malloc.
* **Kernel/cgroup pressure: no.** `vm.overcommit_memory=0`, `CommitLimit 158 GB` vs `Committed_AS
  68 GB`, `vm.max_map_count=1048576` vs **5876 / 3734 VMAs** in the two cores (`readelf -l` PT_LOAD
  count), user-slice `memory.max=max`, no oomd/page-alloc-failure lines. EDR only logs exec/scan
  events; coredump/drkonqi socket saturation is a consequence of the abort storm, not its cause.

## 2. Verdict

**Confidence: high (~0.95).** All three omp deaths and all four native-child deaths are one bug:
`ulimit -v` in an omp bash tool call lowers `RLIMIT_AS` on the agent host, whose VmSize is ~75 GiB
of reservations, so subsequent `mmap` returns `ENOMEM` → Rust alloc-error abort (`native-alloc-*.log`),
glibc TLS `_Exit(127)` with the thread-local-data message, or an unchecked-NULL SIGSEGV. Children
inherit the cap and abort first — hence omp dying ~30 s *after* a native child. `systemd-run` "fixed"
it because units get fresh rlimits from `systemd --user` **and** because it replaced `ulimit -v`
with `MemoryMax=` — the latter is the actual fix.

## 3. Fixes, ranked

1. **Never run `ulimit -v` (or `-m`, `-d`) in an omp bash tool call — ever.** It caps the agent host.
   Cap memory only via `systemd-run --user -p MemoryMax=…`, which cannot touch omp. (Free, total.)
2. **Wrap every native run**: `systemd-run --user --pipe --wait -q --collect -p MemoryMax=12G bash -c
   'ulimit -c 0; …'`. Keep `ulimit -c 0` *inside* the wrapped `bash -c` (a real child) — it is
   harmless there and stops multi-GB cores; issued bare in a tool call it would only disable omp's cores.
3. **If a per-process AS backstop is truly wanted, apply it to the child only**: `prlimit --as=… -- cmd`
   or `systemd-run -p LimitAS=…`. Never `prlimit --pid <omp>`. `benchmarks/simsuite/resguard.py`
   already scopes its `RLIMIT_AS` to the bench process — that pattern is fine.
4. **Report upstream to omp/pi-natives** (not bun — 1.3.14 is not implicated; no matching bun issue
   exists): the in-process shell should apply `ulimit` to spawned children (or refuse `-v`/`-d`/`-m`),
   and the Rust allocator hook should surface `ENOMEM` as a tool error instead of `abort()`ing the
   host. Its 68 KB dynamic TLS/thread × ~70 threads compounds the fragility.
5. **Coredump hygiene** (cosmetic): `systemd-coredump` drop-in `ProcessSizeMax=0`, or `ulimit -c 0`
   in wrappers. **Not needed**: sysctls, bun upgrade, EDR exclusions, detached children.

## 4. Resource policy for this box (31 GB RAM / 6 cores + SMT / 135 GB swap)

* Heavy native runs: `systemd-run … -p MemoryMax=12G` (solves/meshes/ccx/ctest), `-p MemoryMax=16G`
  (ninja). One >4 GB run per agent at a time, announced on `hub`, aggregate target ≤ 24 GB.
* Engine env: `CHUDWARE_MEM_FRAC=0.5`, `CHUDWARE_HEADROOM_GB=6`, `CHUDWARE_SIM_THREADS=4`
  (4 threads keeps 2 cores for omp + EDR; `MemoryMax` is the real backstop, not the fraction).
* Concurrent agents: 8–12 comfortably (omp is ~0.5–1 GB RSS regardless of 75 GB VmSize); the
  binding constraint is the sum of *native* runs, not agent count.
* Never lower any rlimit on the omp pid; never `ulimit -v`; leave `vm.overcommit_memory=0` and
  `vm.max_map_count=1048576` as they are — neither was ever the limiting factor.
