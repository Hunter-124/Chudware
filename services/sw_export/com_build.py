"""Run Chudware's SolidWorks macros in an installed SolidWorks (COM), and read what they built.

The rebuild itself lives in ONE place, the VBA macro (:mod:`services.sw_export.macro`): this
module writes it beside the output, runs it with ``ISldWorks.RunMacro2`` and reads the
``<stem>.report.txt`` it leaves. A teammate who runs the same .swb file in an older SolidWorks
gets the same part, so every COM export also exercises the macro they would run.

The part's mass properties are compared with Chudware's (``expected`` in the plan); a mismatch
is reported (``verified: false``), never hidden.
"""

from __future__ import annotations

import os
from typing import Any

from services.sw_export.macro import (CLOSE_FACTOR, COM_TOL_MM, INERTIA_RTOL, VOLUME_RTOL,
                                      write_body_macro, write_macro)
from services.sw_import.sw_feature_import import SolidWorksUnavailable, _cast


class BuildError(RuntimeError):
    pass


def _app(visible: bool = False):
    try:
        import pythoncom
        import win32com.client as win32
    except Exception as e:  # noqa: BLE001
        raise SolidWorksUnavailable(f"pywin32 is not installed: {e}") from e
    pythoncom.CoInitialize()
    try:
        app = _cast(win32.Dispatch("SldWorks.Application"), "ISldWorks")
    except Exception as e:  # noqa: BLE001
        raise SolidWorksUnavailable(f"could not start SolidWorks over COM: {e}") from e
    app.Visible = visible
    return app


def mass_props(model) -> tuple[float, list[float]]:
    """(volume_mm3, centre of mass in SolidWorks model mm)."""
    mp = model.Extension.CreateMassProperty()
    c = mp.CenterOfMass
    return float(mp.Volume) * 1e9, [c[0] * 1000, c[1] * 1000, c[2] * 1000]


def load_step(app, step_path: str):
    """Open a STEP file as a new SolidWorks document (IModelDoc2, or None).

    The typed ISldWorks.LoadFile4 takes the by-reference error code as a plain int and
    returns ``(model, errors)``; it needs a native (backslash) path."""
    ret = app.LoadFile4(os.path.normpath(os.path.abspath(step_path)), "r", None, 0)
    return _cast(ret[0] if isinstance(ret, tuple) else ret, "IModelDoc2")


def _floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",")]


def read_report(path: str) -> dict:
    """A macro's key=value report as typed values."""
    out: dict[str, Any] = {}
    with open(path, encoding="cp1252", errors="replace") as fh:
        for line in fh:
            key, sep, val = line.rstrip("\r\n").partition("=")
            if not sep:
                continue
            if key in ("com_sw_mm", "inertia_sw_mm5"):
                out[key] = _floats(val)
            elif key in ("volume_mm3",):
                out[key] = float(val)
            elif key in ("built", "body_count"):
                out[key] = int(val)
            elif key in ("ok", "match", "save_ok", "close"):
                out[key] = val.strip() == "1"
            else:
                out[key] = val
    return out


def _recycle(app) -> None:
    """Close an automation-only SolidWorks after a failed build. A feature SolidWorks refused
    can leave a hidden instance unable to sketch until it restarts (SolidWorks 2026); a
    visible session or one with open documents is the user's, and is left alone."""
    try:
        if app.Visible or app.GetDocumentCount() != 0:
            return
        pid = app.GetProcessID()
        app.ExitApp()
    except Exception:  # noqa: BLE001
        return
    # Wait for it to go: a COM call during shutdown reaches the dying process and fails.
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    if handle:
        kernel32.WaitForSingleObject(handle, 60_000)
        kernel32.CloseHandle(handle)


def run_macro(macro_path: str, *, visible: bool = False) -> dict:
    """Run a Chudware macro silently in SolidWorks and return its report."""
    stem = os.path.splitext(os.path.abspath(macro_path))[0]
    report, marker = stem + ".report.txt", stem + ".silent"
    for p in (report,):
        if os.path.exists(p):
            os.remove(p)
    with open(marker, "w", encoding="ascii") as fh:
        fh.write("run by Chudware\n")
    try:
        app = _app(visible)
        try:
            ret = app.RunMacro2(os.path.normpath(os.path.abspath(macro_path)), "", "main", 1, 0)
        except Exception as e:  # noqa: BLE001
            # A SolidWorks that died since the last call: start a fresh one, once.
            if getattr(e, "hresult", None) not in (-2147023170, -2147417848, -2147023174):
                raise
            app = _app(visible)
            ret = app.RunMacro2(os.path.normpath(os.path.abspath(macro_path)), "", "main", 1, 0)
        ok, err = (ret if isinstance(ret, tuple) else (ret, 0))
        if not os.path.isfile(report):
            _recycle(app)
            raise BuildError(f"SolidWorks did not run the macro (RunMacro2={ok}, error {err})")
        rep = read_report(report)
        if not rep.get("ok"):
            _recycle(app)
        return rep
    finally:
        for p in (marker, report):
            try:
                os.remove(p)
            except OSError:
                pass


def verify(expected: dict, got: dict) -> dict:
    """Compare a macro's measured values with Chudware's."""
    out: dict[str, Any] = {k: got.get(k) for k in ("volume_mm3", "com_sw_mm", "body_count",
                                                   "inertia_sw_mm5")}
    ev, ec = expected.get("volume_mm3"), expected.get("com_sw_mm")
    vol, com = got.get("volume_mm3"), got.get("com_sw_mm")
    if ev is None or vol is None:
        out["verified"] = None
        return out
    errors: dict[str, tuple[float, float]] = {}   # check -> (error, tolerance)
    dv = abs(vol - ev) / max(abs(ev), 1e-9)
    out.update({"expected_volume_mm3": ev, "volume_rel_error": dv})
    errors["volume"] = (dv, VOLUME_RTOL)
    if ec and com:
        dc = max(abs(com[i] - ec[i]) for i in range(3))
        out.update({"expected_com_sw_mm": ec, "com_error_mm": dc})
        errors["centre_of_mass"] = (dc, COM_TOL_MM)
    if expected.get("body_count") is not None and got.get("body_count") is not None:
        out["expected_body_count"] = expected["body_count"]
        errors["body_count"] = (abs(got["body_count"] - expected["body_count"]), 0.0)
    ei, gi = expected.get("inertia_sw_mm5"), got.get("inertia_sw_mm5")
    if ei and gi:
        imax = max(abs(x) for x in ei) or 1.0
        di = max(abs(gi[i] - ei[i]) for i in range(3)) / imax
        out.update({"expected_inertia_sw_mm5": ei, "inertia_rel_error": di})
        errors["inertia"] = (di, INERTIA_RTOL)
    checks = {k: e <= t for k, (e, t) in errors.items()}
    # Within CLOSE_FACTOR x the tolerances: the size of a kernel difference between Chudware
    # and SolidWorks booleans, not a modelling difference.
    out["close"] = all(e <= t * CLOSE_FACTOR for e, t in errors.values())
    out["checks"] = checks
    out["verified"] = all(checks.values())
    return out


def build(plan: dict, out_path: str, *, visible: bool = False) -> dict:
    """Write ``plan`` as a macro beside ``out_path`` (.SLDPRT), run it in SolidWorks and check
    the part it saved. The macro stays beside the part for teammates on older releases."""
    out = os.path.abspath(out_path)
    macro = os.path.splitext(out)[0] + ".swb"
    write_macro(plan, macro)
    rep = run_macro(macro, visible=visible)
    saved = rep.get("saved")
    result = {"ok": bool(rep.get("ok")) and bool(rep.get("save_ok")) and os.path.isfile(out),
              "path": out, "macro": macro, "built_steps": rep.get("built"),
              "failed_step": None if rep.get("ok") else rep.get("stage"),
              **verify(plan.get("expected") or {}, rep)}
    if rep.get("error"):
        result["error"] = rep["error"]
    if saved and os.path.normcase(os.path.abspath(saved)) != os.path.normcase(out):
        result["saved"] = saved
    return result


def import_body(step_path: str, out_path: str, *, expected: dict | None = None,
                reason: str = "", visible: bool = False) -> dict:
    """Body-only export: SolidWorks opens Chudware's exact STEP, turns it into SolidWorks axes
    and saves it as a part (an imported body; FeatureWorks can recognize features on it).
    Runs the same body macro a teammate would, written beside the part."""
    out = os.path.abspath(out_path)
    macro = os.path.splitext(out)[0] + ".swb"
    stem_step = os.path.splitext(out)[0] + ".step"
    if os.path.normcase(os.path.abspath(step_path)) != os.path.normcase(stem_step):
        import shutil
        shutil.copyfile(step_path, stem_step)
    write_body_macro(os.path.basename(os.path.splitext(out)[0]), stem_step, macro,
                     expected=expected, reason=reason or "exported as its exact solid")
    rep = run_macro(macro, visible=visible)
    result = {"ok": bool(rep.get("ok")) and bool(rep.get("save_ok")) and os.path.isfile(out),
              "path": out, "macro": macro, "step": stem_step, "fidelity": "imported_body",
              **verify(expected or {}, rep)}
    if rep.get("error"):
        result["error"] = rep["error"]
    return result
