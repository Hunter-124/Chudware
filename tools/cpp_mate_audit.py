"""Adversarial mate-solver audit (non-OCCT, fast path via mate_cli).

The corpus (mate_diff, 28 fixtures) is hand-curated; this generates harder,
*coupled* topologies the corpus underweights — long chains, trees, closed loops,
and mixed mate-type networks where one component participates in several mates.
Each generated network is run through BOTH the Python oracle (engine.doc.
mate_solver.solve_mates) and the C++ mate_cli, then compared with mate_diff's own
equivalence check (dof / converged / over_defined / mate_status + the
satisfied-residual invariant for under-constrained gauges). A DIFF here is a
SUCCESS: a real divergence the curated corpus missed.

    & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools/cpp_mate_audit.py \
        --cli core/build/bin/Release/mate_cli.exe
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.mate_diff import run_python, run_cpp, compare, _status_set  # noqa: E402


def compare_audit(fx, py, cpp, tol, res_tol):
    """Equivalence for adversarial networks. A CONVERGED system must agree on the full
    mate_diff equivalence (dof/blame + satisfied-residual invariant). An inconsistent
    system both engines fail to converge (e.g. a geometrically-impossible rigid loop, or
    a near-degenerate mixed network) has an implementation-dependent least-squares
    minimum, so its raw pose/residual is NOT a meaningful parity signal — there we assert
    only STRUCTURAL agreement (converged flag, dof, over_defined, mate_status blame)."""
    structural = []
    if py["converged"] != cpp["converged"]:
        structural.append(f"converged: py={py['converged']} cpp={cpp['converged']}")
    if py["dof"] != cpp["dof"]:
        structural.append(f"dof: py={py['dof']} cpp={cpp['dof']}")
    if py["over_defined"] != cpp["over_defined"]:
        structural.append(f"over_defined: py={py['over_defined']} cpp={cpp['over_defined']}")
    if _status_set(py["mate_status"]) != _status_set(cpp["mate_status"]):
        structural.append("mate_status differs")
    if not (py["converged"] and cpp["converged"]):
        return structural  # inconsistent system: structural parity only
    return structural + [d for d in compare(fx, py, cpp, tol, res_tol)
                         if d not in structural]

BOX = 20.0  # edge length of every test box


def ident(tx=0.0, ty=0.0, tz=0.0):
    return [1, 0, 0, tx, 0, 1, 0, ty, 0, 0, 1, tz, 0, 0, 0, 1]


def comp(cid, tx=0.0, ty=0.0, tz=0.0, fixed=False):
    return {"id": cid, "transform": ident(tx, ty, tz), "fixed": fixed,
            "suppressed": False, "name": cid}


def mate(mid, mtype, refs, align="aligned", value=None):
    m = {"id": mid, "type": mtype, "align": align, "suppressed": False,
         "refs": [{"component": c} for c in refs]}
    if value is not None:
        m["value"] = {"value": value, "expr": None}
    return m


def geom(mid, ri, kind, point, normal=(0, 0, 1), direction=(0, 0, 1), radius=0.0):
    return {"mate_id": mid, "ref_index": ri,
            "geom": {"kind": kind, "point": list(point), "normal": list(normal),
                     "direction": list(direction), "radius": float(radius)}}


# local-frame face descriptors for a [0,BOX]^3 box
def xp_face():  # +X face
    return ("plane", (BOX, BOX / 2, BOX / 2), (1, 0, 0), (1, 0, 0))


def xm_face():  # -X face
    return ("plane", (0, BOX / 2, BOX / 2), (-1, 0, 0), (1, 0, 0))


def yp_face():
    return ("plane", (BOX / 2, BOX, BOX / 2), (0, 1, 0), (0, 1, 0))


def zaxis(at=(BOX / 2, BOX / 2, 0)):  # local Z axis through box centre
    return ("line", at, (0, 0, 1), (0, 0, 1))


def g_from(mid, ri, desc, radius=0.0):
    kind, pt, nrm, dr = desc
    return geom(mid, ri, kind, pt, nrm, dr, radius)


def fixture(name, components, mates, rgeom):
    return {"name": name, "components": components, "mates": mates,
            "resolved_geometry": rgeom}


# --------------------------------------------------------------------------- networks
def chain_coincident(n):
    """n boxes in a row, each -X face coincident with previous +X face."""
    comps = [comp("k1", 0, fixed=True)]
    mates, rg = [], []
    for i in range(2, n + 1):
        comps.append(comp(f"k{i}", 30.0 * (i - 1)))
        mid = f"m{i-1}"
        mates.append(mate(mid, "coincident", [f"k{i-1}", f"k{i}"]))
        rg.append(g_from(mid, 0, xp_face()))
        rg.append(g_from(mid, 1, xm_face()))
    return fixture(f"chain_coincident_{n}", comps, mates, rg)


def chain_distance(n, d=5.0):
    comps = [comp("k1", 0, fixed=True)]
    mates, rg = [], []
    for i in range(2, n + 1):
        comps.append(comp(f"k{i}", 50.0 * (i - 1)))
        mid = f"m{i-1}"
        mates.append(mate(mid, "distance", [f"k{i-1}", f"k{i}"], align="anti", value=d))
        rg.append(g_from(mid, 0, xp_face()))
        rg.append(g_from(mid, 1, xm_face()))
    return fixture(f"chain_distance_{n}", comps, mates, rg)


def star_coincident(n):
    """One fixed hub; n satellites each coincident to the hub's +X face."""
    comps = [comp("k1", 0, fixed=True)]
    mates, rg = [], []
    for i in range(2, n + 1):
        comps.append(comp(f"k{i}", 40.0 * (i - 1), ty=10.0 * i))
        mid = f"m{i-1}"
        mates.append(mate(mid, "coincident", ["k1", f"k{i}"]))
        rg.append(g_from(mid, 0, xp_face()))
        rg.append(g_from(mid, 1, xm_face()))
    return fixture(f"star_coincident_{n}", comps, mates, rg)


def loop_coincident_3():
    """Closed loop k1-k2-k3-k1: over-constrained, exercises redundancy blame."""
    comps = [comp("k1", 0, fixed=True), comp("k2", 30.0), comp("k3", 60.0)]
    mates = [
        mate("m1", "coincident", ["k1", "k2"]),
        mate("m2", "coincident", ["k2", "k3"]),
        mate("m3", "coincident", ["k3", "k1"]),
    ]
    rg = []
    for mid in ("m1", "m2", "m3"):
        rg.append(g_from(mid, 0, xp_face()))
        rg.append(g_from(mid, 1, xm_face()))
    return fixture("loop_coincident_3", comps, mates, rg)


def concentric_chain(n, r=4.0):
    """Peg through n-1 plates: each pair concentric on Z axes."""
    comps = [comp("k1", 0, fixed=True)]
    mates, rg = [], []
    for i in range(2, n + 1):
        comps.append(comp(f"k{i}", 5.0 * i, ty=5.0, tz=10.0 * (i - 1)))
        mid = f"m{i-1}"
        mates.append(mate(mid, "concentric", [f"k{i-1}", f"k{i}"]))
        rg.append(g_from(mid, 0, zaxis(), radius=r))
        rg.append(g_from(mid, 1, zaxis(), radius=r))
    return fixture(f"concentric_chain_{n}", comps, mates, rg)


def mixed_chain():
    """k1 fixed; k2 coincident to k1; k3 parallel + distance to k2 (mixed types)."""
    comps = [comp("k1", 0, fixed=True), comp("k2", 30.0), comp("k3", 70.0, ty=5.0)]
    mates = [
        mate("m1", "coincident", ["k1", "k2"]),
        mate("m2", "parallel", ["k2", "k3"]),
        mate("m3", "distance", ["k2", "k3"], align="anti", value=8.0),
    ]
    rg = [
        g_from("m1", 0, xp_face()), g_from("m1", 1, xm_face()),
        g_from("m2", 0, xp_face()), g_from("m2", 1, xp_face()),
        g_from("m3", 0, xp_face()), g_from("m3", 1, xm_face()),
    ]
    return fixture("mixed_coincident_parallel_distance", comps, mates, rg)


def coincident_plus_redundant_parallel():
    """coincident already pins orientation; an added parallel on the same faces is
    REDUNDANT (no new DOF removed) — both engines must blame it identically."""
    comps = [comp("k1", 0, fixed=True), comp("k2", 30.0)]
    mates = [
        mate("m1", "coincident", ["k1", "k2"]),
        mate("m2", "parallel", ["k1", "k2"]),
    ]
    rg = [
        g_from("m1", 0, xp_face()), g_from("m1", 1, xm_face()),
        g_from("m2", 0, xp_face()), g_from("m2", 1, xm_face()),
    ]
    return fixture("coincident_plus_redundant_parallel", comps, mates, rg)


def perpendicular_then_coincident():
    comps = [comp("k1", 0, fixed=True), comp("k2", 30.0, tz=5.0)]
    mates = [
        mate("m1", "perpendicular", ["k1", "k2"]),
        mate("m2", "coincident", ["k1", "k2"]),
    ]
    rg = [
        g_from("m1", 0, xp_face()), g_from("m1", 1, yp_face()),
        g_from("m2", 0, xp_face()), g_from("m2", 1, xm_face()),
    ]
    return fixture("perpendicular_then_coincident", comps, mates, rg)


def two_fixed_floating_middle():
    """k1 and k3 fixed; k2 floats coincident to BOTH (mid-plane-ish, may conflict)."""
    comps = [comp("k1", 0, fixed=True), comp("k2", 30.0), comp("k3", 80.0, fixed=True)]
    mates = [
        mate("m1", "coincident", ["k1", "k2"]),
        mate("m2", "coincident", ["k3", "k2"]),
    ]
    rg = [
        g_from("m1", 0, xp_face()), g_from("m1", 1, xm_face()),
        g_from("m2", 0, xm_face()), g_from("m2", 1, xp_face()),
    ]
    return fixture("two_fixed_floating_middle", comps, mates, rg)


def build_all():
    out = []
    # long coincident chains stress the solve_pos lstsq robustness fix hardest:
    # more coupling => more ill-conditioned normal matrix near convergence.
    for n in (3, 4, 5, 6, 8, 10):
        out.append(chain_coincident(n))
    for n in (3, 4, 5):
        out.append(chain_distance(n))
    for n in (3, 4, 5):
        out.append(star_coincident(n))
    for n in (3, 4, 6):  # concentric chains carry a free rotational DOF per pair
        out.append(concentric_chain(n))
    out.append(loop_coincident_3())
    out.append(mixed_chain())
    out.append(coincident_plus_redundant_parallel())
    out.append(perpendicular_then_coincident())
    out.append(two_fixed_floating_middle())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", default=str(REPO / "core" / "build" / "bin" / "Release" / "mate_cli.exe"))
    ap.add_argument("--tol", type=float, default=1e-6)
    ap.add_argument("--res-tol", type=float, default=1e-6)
    args = ap.parse_args()
    cli = Path(args.cli)
    if not cli.exists():
        sys.stderr.write(f"mate_cli not found at {cli}\n")
        return 2

    fixtures = build_all()
    print(f"[mate-audit] {len(fixtures)} adversarial network(s) | cli={cli.name}")
    print("-" * 78)
    mism = 0
    for fx in fixtures:
        name = fx["name"]
        try:
            py = run_python(fx)
            cpp = run_cpp(cli, fx)
        except Exception as exc:  # noqa: BLE001
            print(f"DIFF  {name:<38} raised {type(exc).__name__}: {exc}")
            mism += 1
            continue
        diffs = compare_audit(fx, py, cpp, args.tol, args.res_tol)
        if diffs:
            print(f"DIFF  {name:<38} {'; '.join(diffs)}")
            mism += 1
        else:
            print(f"ok    {name:<38} dof={py['dof']} conv={py['converged']} "
                  f"over={py['over_defined']}")
    print("-" * 78)
    print(f"[mate-audit] {len(fixtures) - mism}/{len(fixtures)} MATCH ; {mism} divergence(s)")
    return 1 if mism else 0


if __name__ == "__main__":
    raise SystemExit(main())
