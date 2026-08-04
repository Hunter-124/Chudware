# T-C3 questions — sonnet-rill
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Q1 — fw_sim_run signature: contract §5 vs work order

**Status: self-answered (non-blocking, proceeding)**

**Conflict:** `contracts/firmware_rpc.md §5` (frozen) specifies `fw_sim_run | {project_id, elf_path?, scenario?}`. The task steps say `fw_sim_run(project_id, assertions: list[str])`. These don't match.

**Recommended answer (implemented):** Accept all parameters — `{project_id, elf_path?, scenario?, assertions?: [str]}`. The `assertions?` field is purely additive; it removes nothing the frozen contract promised. This is exactly the `mcu_family required→optional` pattern from T-C2 Q1 — additive extension, not a schema break. The §5 row should append `assertions?:[str]` in a future contract amendment.

**Implemented as:** `fw_sim_run(params)` accepts `project_id, elf_path?, scenario?, assertions?` with `assertions` defaulting to `[]` if omitted.

---

## Q2 — Emulator-absent posture: graceful SimResult vs error envelope

**Status: self-answered (non-blocking, proceeding)**

**Question:** When neither `renode` nor `qemu-system-arm` is on PATH, should `fw_sim_run` return the error envelope `{"code", "message", "detail"}` or a graceful `SimResult(passed=False)`?

**Recommended answer (implemented):** Mirror T-C2's posture exactly — tool-absent situations return graceful `SimResult(passed=False)`, never an error envelope. Distinction:
- **Request errors** (missing `project_id`, unknown project, no ELF) → error envelope `_err(...)`, same as `fw_build`.
- **Tool-absent** (no renode/qemu binary) → `SimResult(passed=False, uart_log="emulator_not_found: ...")`, exactly as `BuildResult(success=False, returncode=-1, stderr="toolchain_not_found: ...")`.

The `SimResult` shape has no `stderr`/`returncode` field, so the "not found" signal lives in `uart_log`, and `assertion_results` is empty. This convention makes the distinction clear: populated `assertion_results` + `passed=False` = ran but failed; empty `assertion_results` + `passed=False` = couldn't run.

**Implemented in:** `emulator.py::_emulator_not_found()` and `run_simulation()`.
