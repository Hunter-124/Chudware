# engine_oracle — Python parity oracle (frozen)

**Owner:** P5 kill-Python nodes. **Deleted at N6b.**

The legacy Python CAD engine kept as a differential oracle while `core/` (libaicad)
reaches parity subsystem-by-subsystem. Each ported subsystem flips routing in
`services/worker/` and deletes its Python module here in the same commit.

## Entry points
- `python -m engine.worker` (JSON-RPC subprocess spawned by host)
- `python -m engine.doc.test_*` (pytest suites)

## Compat symlink
Root `engine -> engine_oracle` preserves existing `python -m engine.*` paths.

## Contract
- `contracts/worker_rpc.md` — method table this worker implements.
