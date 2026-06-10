# Scratch: live end-to-end ATX-case run against the local LM Studio model.
# Run with the runtime venv python, cwd = dist\AI-CAD\app (the worker's view of the world).
import json
import sys
import time

from agent import config, loop

REQUEST = "make me an ATX computer case with standard ATX spacing for standoffs"


def on_event(e):
    ts = time.strftime("%H:%M:%S")
    kind = e.get("kind")
    if kind == "status":
        print(f"[{ts}] status i={e.get('index')} {e.get('phase','')}: "
              f"{str(e.get('text',''))[:140]}", flush=True)
    elif kind == "iteration":
        err = (e.get("error") or "").strip().splitlines()
        err_tail = err[-1][:160] if err else ""
        print(f"[{ts}] ITER {e.get('index')} ok={e.get('ok')} done={e.get('done')} "
              f"dims={e.get('dims')} asm={e.get('is_assembly')} "
              f"notes={str(e.get('notes',''))[:120]!r} err={err_tail!r}", flush=True)
        fit = e.get("fit")
        if fit:
            print(f"          fit: {fit.get('summary','')}", flush=True)


def main():
    provider = config.get_provider()
    print(f"provider={provider.name} model={getattr(provider, 'model', '?')} "
          f"base={getattr(provider, 'base_url', '?')} vision={provider.supports_vision}",
          flush=True)
    t0 = time.time()
    run = loop.run(REQUEST, provider, name="atx_case_live", on_event=on_event)
    dt = time.time() - t0
    print("\n==== RESULT ====", flush=True)
    print(f"success={run.success} converged={run.converged} stop={run.stop_reason} "
          f"iters={run.iterations} elapsed={dt:.0f}s", flush=True)
    print(f"contact_sheet={run.contact_sheet}", flush=True)
    if run.result:
        keep = {k: run.result.get(k) for k in ("ok", "volume", "triangles", "is_assembly")}
        bb = run.result.get("bbox") or {}
        keep["size"] = bb.get("size")
        print("result:", json.dumps(keep, default=str), flush=True)
    sys.exit(0 if (run.success and run.converged) else 1)


if __name__ == "__main__":
    main()
