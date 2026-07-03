"""Tests for installer/setup_file_assoc.py (task 05 — file associations).

House style: a plain main()-runner (no pytest dependency), runnable on any platform with stdlib
only. The Windows registry PLAN and the Linux .desktop/MIME content are pure functions, so they are
checked everywhere; the Linux register/unregister side-effects are exercised against a TEMP
XDG_DATA_HOME so the run never touches the real MIME database.

    python installer/test_file_assoc.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# T-V3 fix: .resolve() first so this lands on repo root regardless of whether the file was
# reached via the installer/ symlink or its real services/installer/ path — the old
# dirname(dirname(...)) on the unresolved path landed on services/ when pytest collects the
# real path, which makes services/mcp/ resolve as a top-level `mcp` package, shadowing the
# real installed SDK.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from installer import setup_file_assoc as fa  # noqa: E402

_fail = 0


def check(cond: bool, msg: str) -> None:
    global _fail
    if cond:
        print(f"ok:   {msg}")
    else:
        print(f"FAIL: {msg}")
        _fail += 1


def test_windows_plan() -> None:
    exe = r"C:\Apps\AI-CAD\AI-CAD.exe"
    plan = fa.windows_registry_plan(exe)

    def has(subkey, name, value_pred):
        return any(e["subkey"] == subkey and e["name"] == name and value_pred(e["value"])
                   for e in plan)

    # Native ProgID carries the Open command with the exe + "%1".
    check(has(fr"Software\Classes\{fa.PROGID_NATIVE}\shell\open\command", "",
              lambda v: exe in v and "%1" in v),
          "windows: native ProgID open command = \"<exe>\" \"%1\"")
    # .step+ defaults to the native ProgID; all listed extensions advertise in Open-with.
    check(has(r"Software\Classes\.step+", "", lambda v: v == fa.PROGID_NATIVE),
          "windows: .step+ defaults to the native ProgID")
    for a in fa.ASSOCIATIONS:
        pid = fa.PROGID_NATIVE if a["native"] else fa.PROGID_DOC
        check(has(fr"Software\Classes\{a['ext']}\OpenWithProgids", pid, lambda v: v == ""),
              f"windows: {a['ext']} -> OpenWithProgids {pid}")
    # A non-native interchange type must NOT be forced as the default handler.
    check(not has(r"Software\Classes\.stl", "", lambda v: v in (fa.PROGID_NATIVE, fa.PROGID_DOC)),
          "windows: .stl is NOT defaulted to AI-CAD (Open-with only)")
    # Every plan value is a string (REG_SZ) and every subkey is under Software\Classes.
    check(all(isinstance(e["value"], str) and e["subkey"].startswith(r"Software\Classes")
              for e in plan),
          "windows: plan is well-formed (REG_SZ values under Software\\Classes)")


def test_linux_content() -> None:
    exe = "/opt/ai-cad/AI-CAD"
    desk = fa.linux_desktop_entry(exe)
    check("[Desktop Entry]" in desk and "Type=Application" in desk, "linux: .desktop is well-formed")
    check(f'Exec="{exe}" %f' in desk, "linux: Exec runs the exe with the file arg")
    check(fa.MIME_NATIVE in desk.split("MimeType=", 1)[1].splitlines()[0],
          "linux: native .step+ MIME is listed in MimeType")
    check("model/step" in desk and "model/stl" in desk,
          "linux: interchange formats appear in Open-with (standard MIME)")

    xml = fa.linux_mime_xml()
    check(fa.MIME_NATIVE in xml and "*.step+" in xml,
          "linux: MIME package defines the .step+ glob")


def test_linux_register_roundtrip() -> None:
    exe = "/opt/ai-cad/AI-CAD"
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_DATA_HOME"] = tmp
        desktop = Path(tmp) / "applications" / fa.DESKTOP_FILE
        mime = Path(tmp) / "mime" / "packages" / fa.MIME_PACKAGE

        # Dry-run writes nothing.
        fa.register_linux(exe, unregister=False, dry_run=True)
        check(not desktop.exists() and not mime.exists(), "linux: --dry-run writes no files")

        # Register writes both files with the expected content.
        fa.register_linux(exe, unregister=False, dry_run=False)
        check(desktop.exists() and mime.exists(), "linux: register writes .desktop + MIME package")
        check(exe in desktop.read_text(), "linux: written .desktop points at the exe")
        check("*.step+" in mime.read_text(), "linux: written MIME package has the .step+ glob")

        # Idempotent: a second register leaves identical content.
        before = desktop.read_text()
        fa.register_linux(exe, unregister=False, dry_run=False)
        check(desktop.read_text() == before, "linux: register is idempotent")

        # Unregister removes both files.
        fa.register_linux(exe, unregister=True, dry_run=False)
        check(not desktop.exists() and not mime.exists(), "linux: unregister removes the files")
        os.environ.pop("XDG_DATA_HOME", None)


def main() -> int:
    test_windows_plan()
    test_linux_content()
    test_linux_register_roundtrip()
    if _fail:
        print(f"\n{_fail} check(s) FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
