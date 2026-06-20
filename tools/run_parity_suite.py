"""run_parity_suite.py — one command for the whole C++-core differential-parity status.

Runs every doc-engine / solver / agent parity gate AND the adversarial audits against a
single libaicad.dll (+ the non-OCCT mate_cli/sketch_cli), then prints a consolidated
PASS/FAIL table. Exit code is 0 iff every gate passes — the integration gate for the flip.

    & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools/run_parity_suite.py
    # options: --dll <libaicad.dll>  --mate-cli <exe>  --sketch-cli <exe>  --only <substr>

Each gate is a sibling tool run as a subprocess; PASS = exit 0. A missing tool (e.g. a
not-yet-landed audit) is reported as SKIP, not a failure.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

# kind: "noarg" (tool auto-finds the DLL) | "dll" (pass --dll) |
#       "mate_cli" / "sketch_cli" (pass --cli <that exe>)
GATES = [
    # ---- doc engine: curated parity corpora -------------------------------
    ("doc",    "cpp_session_parity",      "noarg"),
    ("doc",    "cpp_eval_parity",         "dll"),
    ("doc",    "cpp_export_parity",       "dll"),
    ("doc",    "cpp_drawing_parity",      "dll"),
    ("doc",    "cpp_sketch_parity",       "dll"),
    ("doc",    "cpp_weld_parity",         "dll"),
    ("doc",    "cpp_simbridge_parity",    "dll"),
    ("doc",    "cpp_asm_parity",          "dll"),
    ("doc",    "cpp_derived_parity",      "noarg"),
    # ---- doc engine: adversarial audits -----------------------------------
    ("audit",  "cpp_audit_parity",        "noarg"),
    ("audit",  "cpp_audit2_parity",       "dll"),
    ("audit",  "cpp_audit3_parity",       "dll"),
    ("audit",  "cpp_eval_audit",          "dll"),
    ("audit",  "cpp_derived_audit",       "noarg"),
    ("audit",  "cpp_weld_audit",          "noarg"),
    ("audit",  "cpp_asm_audit",           "noarg"),
    # ---- solver core (fast, non-OCCT CLIs) --------------------------------
    ("solver", "mate_diff",               "mate_cli"),
    ("solver", "solver_diff",             "sketch_cli"),
    ("solver", "cpp_mate_audit",          "mate_cli"),
    ("solver", "cpp_sketch_audit",        "sketch_cli"),
    # ---- agent orchestration (mock provider, no key) ----------------------
    ("agent",  "cpp_agent_parity",        "dll"),
    ("agent",  "cpp_agent_support_parity","dll"),
    ("agent",  "cpp_assist_parity",       "dll"),
    ("agent",  "cpp_micro_parity",        "dll"),
    ("agent",  "cpp_orch_machine_parity", "dll"),
    ("agent",  "cpp_orch_run_parity",     "dll"),
]


def last_summary(text):
    """Best-effort one-line summary: the last non-empty stdout line."""
    for ln in reversed((text or "").splitlines()):
        s = ln.strip()
        if s:
            return s[:88]
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    ap.add_argument("--mate-cli", default=str(REPO / "core" / "build" / "bin" / "Release" / "mate_cli.exe"))
    ap.add_argument("--sketch-cli", default=str(REPO / "core" / "build" / "bin" / "Release" / "sketch_cli.exe"))
    ap.add_argument("--only", default=None, help="run only gates whose name contains this substring")
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        sys.stderr.write(f"run_parity_suite: libaicad.dll not found at {dll}\n")
        return 2

    rows = []
    npass = nfail = nskip = 0
    cur_group = None
    for group, name, kind in GATES:
        if args.only and args.only not in name:
            continue
        tool = REPO / "tools" / f"{name}.py"
        if not tool.exists():
            rows.append((group, name, "SKIP", "tool not present"))
            nskip += 1
            continue
        cmd = [PY, str(tool)]
        if kind == "dll":
            cmd += ["--dll", str(dll)]
        elif kind == "mate_cli":
            cmd += ["--cli", args.mate_cli]
        elif kind == "sketch_cli":
            cmd += ["--cli", args.sketch_cli]
        if group != cur_group:
            print(f"\n=== {group.upper()} ===")
            cur_group = group
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=900)
            ok = proc.returncode == 0
            summary = last_summary(proc.stdout) or last_summary(proc.stderr)
        except Exception as exc:  # noqa: BLE001
            ok, summary = False, f"runner raised {type(exc).__name__}: {exc}"
        dt = time.perf_counter() - t0
        status = "PASS" if ok else "FAIL"
        npass += ok
        nfail += (not ok)
        rows.append((group, name, status, summary))
        print(f"  [{status}] {name:<26} ({dt:5.1f}s)  {summary}")

    print("\n" + "=" * 78)
    print(f"PARITY SUITE: {npass} passed, {nfail} failed, {nskip} skipped "
          f"(of {npass + nfail + nskip} run)")
    if nfail:
        print("FAILED gates:")
        for g, n, s, summ in rows:
            if s == "FAIL":
                print(f"  - {n}: {summ}")
    print("=" * 78)
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(main())
