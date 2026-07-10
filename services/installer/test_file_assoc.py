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
    # Document ProgIDs use the exe's SECOND icon group (",1" = branded doc icon from
    # host/app.rc), NOT ",0" (the app tile, which stays on the exe itself).
    for pid in (fa.PROGID_NATIVE, fa.PROGID_DOC):
        check(has(fr"Software\Classes\{pid}\DefaultIcon", "", lambda v: v == f"{exe},1"),
              f"windows: {pid} DefaultIcon = <exe>,1 (doc icon group)")
    # Billet identity (2026-07 rename): ProgIDs + user-visible type names say Billet, not AI-CAD.
    check(fa.PROGID_NATIVE == "Billet.stepplus" and fa.PROGID_DOC == "Billet.document",
          "windows: ProgIDs use the Billet identity")
    check(has(fr"Software\Classes\{fa.PROGID_NATIVE}", "", lambda v: v == "Billet Document"),
          "windows: native ProgID type name is 'Billet Document'")
    check(not any("AI-CAD" in e["value"] or "AICAD" in e["subkey"]
                  for e in plan if e["subkey"].count("\\") == 2 and e["name"] == ""),
          "windows: no AI-CAD branding left in ProgID type names")
    # .step+ defaults to the native ProgID; all listed extensions advertise in Open-with.
    check(has(r"Software\Classes\.step+", "", lambda v: v == fa.PROGID_NATIVE),
          "windows: .step+ defaults to the native ProgID")
    for a in fa.ASSOCIATIONS:
        pid = fa.PROGID_NATIVE if a["native"] else fa.PROGID_DOC
        check(has(fr"Software\Classes\{a['ext']}\OpenWithProgids", pid, lambda v: v == ""),
              f"windows: {a['ext']} -> OpenWithProgids {pid}")
    # A non-native interchange type must NOT be forced as the default handler.
    check(not has(r"Software\Classes\.stl", "", lambda v: v in (fa.PROGID_NATIVE, fa.PROGID_DOC)),
          "windows: .stl is NOT defaulted to Billet (Open-with only)")
    # Every plan value is a string (REG_SZ) and every subkey is under Software\Classes.
    check(all(isinstance(e["value"], str) and e["subkey"].startswith(r"Software\Classes")
              for e in plan),
          "windows: plan is well-formed (REG_SZ values under Software\\Classes)")


def test_windows_legacy_cleanup_plan() -> None:
    plan = fa.windows_legacy_cleanup_plan()
    # Both pre-rename ProgID trees are removed...
    for pid in fa.LEGACY_PROGIDS:
        check(any(e["kind"] == "tree" and e["subkey"] == fr"Software\Classes\{pid}" for e in plan),
              f"windows-legacy: {pid} ProgID tree is deleted")
    # ...and every extension's old OpenWithProgids value is dropped.
    for a in fa.ASSOCIATIONS:
        pid = fa.LEGACY_PROGIDS[0] if a["native"] else fa.LEGACY_PROGIDS[1]
        check(any(e["kind"] == "value" and e["name"] == pid
                  and e["subkey"] == fr"Software\Classes\{a['ext']}\OpenWithProgids" for e in plan),
              f"windows-legacy: {a['ext']} old OpenWithProgids {pid} is deleted")
    # The cleanup plan never touches the NEW Billet ProgIDs.
    check(not any(fa.PROGID_NATIVE in e["subkey"] or fa.PROGID_DOC in e["subkey"] for e in plan),
          "windows-legacy: cleanup plan leaves the Billet ProgIDs alone")


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
    check(f'<icon name="{fa.MIME_ICON}"/>' in xml,
          "linux: MIME package declares the branded doc icon")
    check(fa.MIME_ICON == fa.MIME_NATIVE.replace("/", "-"),
          "linux: icon name is the MIME type with '/' -> '-'")

    # Billet identity (2026-07 rename): launcher name, MIME vendor tree and file names.
    check("Name=Billet" in desk, "linux: launcher Name is Billet")
    check(fa.MIME_NATIVE == "application/vnd.billet.stepplus",
          "linux: native MIME is application/vnd.billet.stepplus")
    check("Billet Document" in xml and "AI-CAD" not in xml,
          "linux: MIME comment says Billet Document (no AI-CAD branding)")
    check(fa.DESKTOP_FILE == "billet.desktop" and fa.MIME_PACKAGE == "billet.xml",
          "linux: XDG file names use the billet identity")


def test_linux_register_roundtrip() -> None:
    exe = "/opt/ai-cad/AI-CAD"
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_DATA_HOME"] = tmp
        desktop = Path(tmp) / "applications" / fa.DESKTOP_FILE
        mime = Path(tmp) / "mime" / "packages" / fa.MIME_PACKAGE

        # Pre-seed the PRE-RENAME (AI-CAD) artifacts so register can prove the migration cleanup.
        legacy_desktop = Path(tmp) / "applications" / fa.LEGACY_DESKTOP_FILE
        legacy_mime = Path(tmp) / "mime" / "packages" / fa.LEGACY_MIME_PACKAGE
        legacy_icon = (Path(tmp) / "icons" / "hicolor" / "256x256" / "mimetypes"
                       / f"{fa.LEGACY_MIME_ICON}.png")
        for p in (legacy_desktop, legacy_mime, legacy_icon):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("stale pre-rename artifact")

        # Dry-run writes nothing (and removes nothing).
        fa.register_linux(exe, unregister=False, dry_run=True)
        check(not desktop.exists() and not mime.exists(), "linux: --dry-run writes no files")
        check(legacy_desktop.exists(), "linux: --dry-run leaves legacy files untouched")

        # Register writes both files with the expected content.
        fa.register_linux(exe, unregister=False, dry_run=False)
        check(desktop.exists() and mime.exists(), "linux: register writes .desktop + MIME package")
        check(not legacy_desktop.exists() and not legacy_mime.exists() and not legacy_icon.exists(),
              "linux: register removes the pre-rename ai-cad.* files (migration)")
        check(exe in desktop.read_text(), "linux: written .desktop points at the exe")
        check("*.step+" in mime.read_text(), "linux: written MIME package has the .step+ glob")

        # Branded doc icon lands in hicolor/*/mimetypes (only checkable when this
        # checkout carries assets/doc_icon.png — deployments without it skip cleanly).
        icon256 = Path(tmp) / "icons" / "hicolor" / "256x256" / "mimetypes" / f"{fa.MIME_ICON}.png"
        if fa._doc_icon_source() is not None:
            check(icon256.exists(), "linux: register installs the 256px mimetype icon")
        else:
            check(not icon256.exists(), "linux: no doc icon source -> icon install skipped cleanly")

        # Idempotent: a second register leaves identical content.
        before = desktop.read_text()
        fa.register_linux(exe, unregister=False, dry_run=False)
        check(desktop.read_text() == before, "linux: register is idempotent")

        # Unregister removes both files + any installed mimetype icons + legacy leftovers.
        legacy_desktop.parent.mkdir(parents=True, exist_ok=True)
        legacy_desktop.write_text("stale pre-rename artifact")  # re-seed one legacy file
        fa.register_linux(exe, unregister=True, dry_run=False)
        check(not desktop.exists() and not mime.exists(), "linux: unregister removes the files")
        check(not icon256.exists(), "linux: unregister removes the mimetype icons")
        check(not legacy_desktop.exists(), "linux: unregister also removes legacy ai-cad.* files")
        os.environ.pop("XDG_DATA_HOME", None)


def main() -> int:
    test_windows_plan()
    test_windows_legacy_cleanup_plan()
    test_linux_content()
    test_linux_register_roundtrip()
    if _fail:
        print(f"\n{_fail} check(s) FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
