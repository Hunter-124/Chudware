"""Adversarial audit of the ASSEMBLY path through the full doc engine (py-vs-C++).

cpp_asm_parity (10) + cpp_audit2_parity's ASM block cover the base; this stresses the
assembly compiler (core/doc/assembly.cpp: component resolution -> mate solve -> placement
-> multi-body tess -> patterns/explode/interference) with deeper/coupled cases. Unlike
cpp_mate_audit (which drives the mate solver in isolation via mate_cli), this goes through
doc_apply_ops / doc_solve_mates, so it exercises selector resolution + body placement +
tess, and benefits from the solve_pos lstsq fix on longer in-assembly mate chains.

Equivalence (mirrors cpp_audit2_parity.asm_mate_case): fixed components match tightly;
dof==0 -> all transforms tight; dof>0 -> residual-invariant (BOTH poses null every active
mate, computed with the oracle's residual blocks) + dof/over_defined/mate_status parity.
A DIFF is a SUCCESS (a real divergence).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402
from tools.cpp_asm_parity import (_write_box_step, _insert, _mate, _face_selector,  # noqa: E402
                                  _comp_transforms, mate_residuals, _vec_close)
from tools.cpp_audit2_parity import py_asm_session, CppAsmS, compare_asm_status  # noqa: E402

box20 = _write_box_step("box20", 20, 20, 20)
box10 = _write_box_step("box10", 10, 10, 10)
XP = _face_selector([20, 10, 10], [1, 0, 0], 400)
XM = _face_selector([0, 10, 10], [-1, 0, 0], 400)


def ident(tx=0.0, ty=0.0, tz=0.0):
    return [1, 0, 0, tx, 0, 1, 0, ty, 0, 0, 1, tz, 0, 0, 0, 1]


def chain_coincident(n):
    """n boxes, each -X coincident to previous +X — the in-assembly analog of the mate chain."""
    ops = [_insert(box20, "k1", fixed=True)]
    for i in range(2, n + 1):
        ops.append(_insert(box20, f"k{i}", transform=ident(30.0 * (i - 1))))
    for i in range(2, n + 1):
        ops.append(_mate("coincident", [{"component": f"k{i-1}", "selector": XP},
                                        {"component": f"k{i}", "selector": XM}]))
    return ops


def mixed_network():
    """k1 fixed; k2 coincident to k1; k3 coincident to k2 + distance to k1 (coupled mixed)."""
    return [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=ident(30)),
        _insert(box20, "k3", transform=ident(60, 5, 0)),
        _mate("coincident", [{"component": "k1", "selector": XP}, {"component": "k2", "selector": XM}]),
        _mate("coincident", [{"component": "k2", "selector": XP}, {"component": "k3", "selector": XM}]),
        _mate("parallel", [{"component": "k1", "selector": _face_selector([10, 20, 10], [0, 1, 0], 400)},
                           {"component": "k3", "selector": _face_selector([10, 20, 10], [0, 1, 0], 400)}]),
    ]


def lin_pattern(count, spacing=30):
    return [
        _insert(box10, "k1", fixed=True),
        _insert(box10, "k2", transform=ident(spacing), fixed=True),
        {"op": "add_feature", "feature": {"type": "asm_linear_pattern", "name": "LPat",
            "params": {"count1": {"value": count, "expr": None},
                       "spacing1": {"value": spacing, "expr": None}},
            "refs": {"dir1": "axis.x", "components": ["k2"]}}},
    ]


def circ_pattern(count):
    return [
        _insert(box10, "k1", fixed=True),
        _insert(box10, "k2", transform=ident(40, 0, 0), fixed=True),
        {"op": "add_feature", "feature": {"type": "asm_circular_pattern", "name": "CPat",
            "params": {"count": {"value": count, "expr": None},
                       "angle": {"value": 360, "expr": None}},
            "refs": {"axis": "axis.z", "components": ["k2"]}}},
    ]


def over_defined():
    """Two coincident mates pinning the SAME pair to conflicting planes -> over-defined."""
    return [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=ident(30)),
        _mate("coincident", [{"component": "k1", "selector": XP}, {"component": "k2", "selector": XM}]),
        _mate("coincident", [{"component": "k1", "selector": _face_selector([0, 10, 10], [-1, 0, 0], 400)},
                             {"component": "k2", "selector": XM}]),
    ]


def explode_many():
    ops = [_insert(box20, "k1", fixed=True)]
    steps = []
    for i in range(2, 5):
        ops.append(_insert(box20, f"k{i}", transform=ident(30.0 * (i - 1)), fixed=True))
        steps.append({"component": f"k{i}", "vector": [0, 0, 1], "distance": 10.0 * i})
    ops.append({"op": "asm_set_explode", "steps": steps})
    return ops


def compare_solved(label, ops, mism_box):
    """Run ops on both engines; assert assembly-status + pose equivalence."""
    try:
        py = py_asm_session(); pres = py.apply(0, ops)
        cpp = CppAsmS(core); cres = cpp.apply(0, ops)
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<34} DIFF: raised {type(exc).__name__}: {exc}"); mism_box[0] += 1; return
    diffs = compare_asm_status(pres, cres)   # dof / over_defined / mate_status / body count
    pt, ct = _comp_transforms(pres), _comp_transforms(cres)
    dof = pres["rebuild"].get("dof", 0)
    note = ""
    if dof == 0:
        for cid in pt:
            if cid in ct and not _vec_close(pt[cid], ct[cid], 1e-4):
                diffs.append(f"{cid} transform differs")
    else:
        # Residual-invariant ONLY when both engines actually solved (poses null all mates).
        # The doc-path assembly solve passes a time-budget that SKIPS the antipodal-escape
        # restart, so long rigid chains get cut off under-converged on BOTH engines; an
        # under-converged pose is implementation-dependent (basin-sensitive) and not a parity
        # signal, so for that case we assert structural parity only (mirrors cpp_mate_audit).
        try:
            pr, crr = mate_residuals(py, pt), mate_residuals(py, ct)
            py_ok = (max(pr.values()) < 1e-5) if pr else True
            cpp_ok = (max(crr.values()) < 1e-5) if crr else True
            if py_ok and cpp_ok:
                pass  # both poses satisfy every mate — clean parity
            elif py_ok != cpp_ok:  # one solved, the other did not -> real divergence
                diffs.append(f"convergence split: py_ok={py_ok} cpp_ok={cpp_ok} "
                             f"(py|r|={max(pr.values()):.2e} cpp|r|={max(crr.values()):.2e})")
            else:  # both under-converged (budget cut) -> structural-only, just note it
                note = f" [both under-converged @ py~{max(pr.values()):.1e}/cpp~{max(crr.values()):.1e}]"
        except Exception as exc:  # noqa: BLE001
            diffs.append(f"residual-invariant raised: {exc}")
    if diffs:
        print(f"  {label:<34} DIFF: {'; '.join(diffs)[:90]}"); mism_box[0] += 1
    else:
        print(f"  {label:<34} MATCH  (dof={dof}, {len(pres['tess']['bodies'])} bodies){note}")


def main():
    global core
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[asm-audit] libaicad.dll not found at {dll}"); return 2
    core = CppCore(dll)
    print(f"[asm-audit] libaicad ABI={core.abi()}")
    mism = [0]
    total = 0
    for label, ops in [
        ("chain coincident 3 (doc path)", chain_coincident(3)),
        ("chain coincident 4 (doc path)", chain_coincident(4)),
        ("chain coincident 5 (doc path)", chain_coincident(5)),
        ("mixed coincident+parallel net", mixed_network()),
        ("linear pattern 5x", lin_pattern(5)),
        ("linear pattern 8x", lin_pattern(8)),
        ("circular pattern 6x", circ_pattern(6)),
        ("over-defined (conflict blame)", over_defined()),
        ("explode 4 components", explode_many()),
    ]:
        total += 1
        compare_solved(label, ops, mism)
    print(f"\n[asm-audit] {total - mism[0]}/{total} MATCH ; {mism[0]} divergence(s)")
    return 1 if mism[0] else 0


if __name__ == "__main__":
    raise SystemExit(main())
