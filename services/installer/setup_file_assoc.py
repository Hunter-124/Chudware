"""Register Billet as a handler for the document types it owns (task 05).

Makes a double-clicked file (or right-click -> Open with) open in Billet, and lets Billet be set
as the default app. The native **.step+** document is the priority; the interchange formats Billet
natively loads are added to "Open with" without hijacking the user's existing handlers for them.

The user-facing identity is "Billet" (2026-07 rename wave); the executable is still named
AI-CAD this wave, so the Open commands keep pointing at the AI-CAD binary. Register/unregister
also clean up the pre-rename AICAD.* ProgIDs / ai-cad.* XDG files so a machine migrates cleanly.

Idempotent, **per-user (no elevation)**, and reversible (`--unregister`) — modelled on
installer/setup_mcp_clients.py (stdlib only, `--dry-run`, backups).

  Windows: HKCU\\Software\\Classes — a ProgID with an Open command (`"AI-CAD.exe" "%1"`) + a
           DefaultIcon pointing at the exe's SECOND icon group (the branded document icon from
           host/app.rc), the `.step+` extension defaulted to it, and every other extension wired
           into OpenWithProgids so Billet shows in "Open with".
  Linux:   a custom shared-mime-info type for `.step+` + a `.desktop` launcher in XDG_DATA_HOME,
           plus assets/doc_icon.png installed into hicolor/*/mimetypes as the type's icon,
           then update-mime-database / update-desktop-database / xdg-mime so the file manager
           routes a double-click to Billet and shows the branded doc icon. Interchange formats are listed in the launcher's
           MimeType via their standard freedesktop types (Open-with), not redefined.

    python installer/setup_file_assoc.py [--exe <path>] [--unregister] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Vendor identifiers for the native document type.
APP_NAME = "Billet"
PROGID_NATIVE = "Billet.stepplus"      # Windows ProgID for the native .step+ document
PROGID_DOC = "Billet.document"         # Windows ProgID used for the interchange formats (Open-with)
MIME_NATIVE = "application/vnd.billet.stepplus"  # custom MIME for .step+ (no system collision)
MIME_ICON = MIME_NATIVE.replace("/", "-")        # freedesktop icon name for the MIME type
DESKTOP_FILE = "billet.desktop"
MIME_PACKAGE = "billet.xml"
ICON_SIZES = (16, 32, 48, 64, 128, 256)          # hicolor sizes for the document MIME icon

# Pre-rename (AI-CAD) identifiers — register/unregister remove these so a machine that
# had the old associations migrates cleanly to the Billet names.
LEGACY_PROGIDS = ("AICAD.stepplus", "AICAD.document")
LEGACY_MIME_ICON = "application-vnd.aicad.stepplus"
LEGACY_DESKTOP_FILE = "ai-cad.desktop"
LEGACY_MIME_PACKAGE = "ai-cad.xml"

# Extensions AI-CAD owns / natively loads. `native` marks the priority .step+ document we DEFAULT to;
# the rest are added to "Open with" only (so we never steal the user's STL/STEP viewer). `mime` is the
# standard freedesktop type used in the Linux launcher's MimeType (None -> skip on Linux Open-with).
ASSOCIATIONS = [
    {"ext": ".step+", "native": True,  "desc": "Billet Document",       "mime": MIME_NATIVE},
    {"ext": ".step",  "native": False, "desc": "STEP 3D Model",         "mime": "model/step"},
    {"ext": ".stp",   "native": False, "desc": "STEP 3D Model",         "mime": "model/step"},
    {"ext": ".iges",  "native": False, "desc": "IGES 3D Model",         "mime": "model/iges"},
    {"ext": ".igs",   "native": False, "desc": "IGES 3D Model",         "mime": "model/iges"},
    {"ext": ".brep",  "native": False, "desc": "OpenCASCADE BREP",      "mime": None},
    {"ext": ".stl",   "native": False, "desc": "STL Mesh",              "mime": "model/stl"},
    {"ext": ".3mf",   "native": False, "desc": "3MF Mesh",              "mime": "model/3mf"},
]


def default_exe() -> str:
    """Best-guess path to the executable for this platform (overridable with --exe).
    Still the AI-CAD binary name — the Billet rename is user-facing identity only."""
    repo = Path(__file__).resolve().parent.parent
    if sys.platform.startswith("win"):
        return str(repo / "dist" / "AI-CAD" / "AI-CAD.exe")
    # Linux/macOS: the host build output (the porter's `host/build-linux/bin/AI-CAD`).
    return str(repo / "host" / "build-linux" / "bin" / "AI-CAD")


# ----------------------------------------------------------------- Windows (pure plan + winreg apply)

def windows_registry_plan(exe: str) -> list[dict]:
    """Pure description of the HKCU\\Software\\Classes entries to create — list of
    {subkey, name, value}. `name` "" is a key's (Default) value. Kept pure so it unit-tests with no
    registry access; register_windows() just walks it. Reverse = delete each listed subkey."""
    exe = str(exe)
    # ",1" = the exe's SECOND icon group (zero-based, resource order): the branded
    # document icon embedded via host/app.rc (`2 ICON "assets/doc.ico"`). Group 0
    # stays the app tile for the exe itself — document types get the doc icon.
    icon = f"{exe},1"
    plan: list[dict] = []

    def progid(pid: str, desc: str) -> None:
        plan.append({"subkey": fr"Software\Classes\{pid}", "name": "", "value": desc})
        plan.append({"subkey": fr"Software\Classes\{pid}", "name": "FriendlyTypeName", "value": desc})
        plan.append({"subkey": fr"Software\Classes\{pid}\DefaultIcon", "name": "", "value": icon})
        plan.append({"subkey": fr"Software\Classes\{pid}\shell\open\command",
                     "name": "", "value": f'"{exe}" "%1"'})

    progid(PROGID_NATIVE, "Billet Document")
    progid(PROGID_DOC, "Billet Model")

    for a in ASSOCIATIONS:
        ext, pid = a["ext"], (PROGID_NATIVE if a["native"] else PROGID_DOC)
        # Always advertise in "Open with".
        plan.append({"subkey": fr"Software\Classes\{ext}\OpenWithProgids",
                     "name": pid, "value": ""})
        # Default ONLY the native .step+ to Billet (don't hijack the user's STEP/STL viewer).
        if a["native"]:
            plan.append({"subkey": fr"Software\Classes\{ext}", "name": "", "value": pid})
    return plan


def windows_legacy_cleanup_plan() -> list[dict]:
    """Pure description of the PRE-RENAME (AICAD.*) registry state to remove — list of
    {"kind": "tree", "subkey": ...} (delete whole key tree) and
    {"kind": "value", "subkey": ..., "name": ...} (delete one value). Applied on BOTH
    register (migration) and unregister (full cleanup)."""
    plan: list[dict] = [{"kind": "tree", "subkey": fr"Software\Classes\{pid}"}
                        for pid in LEGACY_PROGIDS]
    native_legacy, doc_legacy = LEGACY_PROGIDS
    for a in ASSOCIATIONS:
        pid = native_legacy if a["native"] else doc_legacy
        plan.append({"kind": "value",
                     "subkey": fr"Software\Classes\{a['ext']}\OpenWithProgids", "name": pid})
    return plan


def register_windows(exe: str, unregister: bool, dry_run: bool) -> list[str]:
    import winreg  # noqa: WPS433  (Windows-only import, inside the function on purpose)

    def apply_legacy_cleanup() -> None:
        for e in windows_legacy_cleanup_plan():
            if e["kind"] == "tree":
                _winreg_delete_tree(winreg, winreg.HKEY_CURRENT_USER, e["subkey"])
            else:
                _winreg_delete_value(winreg, winreg.HKEY_CURRENT_USER, e["subkey"], e["name"])

    plan = windows_registry_plan(exe)
    msgs: list[str] = []
    if unregister:
        # Delete the ProgID trees + our OpenWithProgids values (current AND pre-rename
        # AICAD.* names). Deepest keys first.
        progid_roots = [fr"Software\Classes\{PROGID_NATIVE}", fr"Software\Classes\{PROGID_DOC}"]
        if not dry_run:
            for root in progid_roots:
                _winreg_delete_tree(winreg, winreg.HKEY_CURRENT_USER, root)
            for a in ASSOCIATIONS:
                pid = PROGID_NATIVE if a["native"] else PROGID_DOC
                _winreg_delete_value(winreg, winreg.HKEY_CURRENT_USER,
                                     fr"Software\Classes\{a['ext']}\OpenWithProgids", pid)
            apply_legacy_cleanup()
        msgs.append(f"{'would remove' if dry_run else 'removed'} {len(progid_roots)} ProgIDs "
                    f"+ {len(ASSOCIATIONS)} associations (+ legacy AICAD.* names)")
        return msgs

    if not dry_run:
        # Migration: drop the pre-rename AICAD.* ProgIDs/values FIRST, then write the
        # Billet plan (which re-defaults .step+ to the new native ProgID).
        apply_legacy_cleanup()
    for e in plan:
        if not dry_run:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, e["subkey"])
            winreg.SetValueEx(key, e["name"], 0, winreg.REG_SZ, e["value"])
            winreg.CloseKey(key)
    msgs.append(f"{'would write' if dry_run else 'wrote'} {len(plan)} HKCU\\Software\\Classes entries"
                " (legacy AICAD.* entries removed)")
    return msgs


def _winreg_delete_tree(winreg, hive, subkey: str) -> None:
    try:
        key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    while True:
        try:
            child = winreg.EnumKey(key, 0)
        except OSError:
            break
        _winreg_delete_tree(winreg, hive, subkey + "\\" + child)
    winreg.CloseKey(key)
    try:
        winreg.DeleteKey(hive, subkey)
    except FileNotFoundError:
        pass


def _winreg_delete_value(winreg, hive, subkey: str, name: str) -> None:
    try:
        key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS)
        winreg.DeleteValue(key, name)
        winreg.CloseKey(key)
    except FileNotFoundError:
        pass


# ----------------------------------------------------------------- Linux (pure plan + xdg apply)

def linux_desktop_entry(exe: str) -> str:
    """The .desktop launcher. MimeType lists the native type first, then the interchange types'
    standard freedesktop MIME (so AI-CAD shows in Open-with for them); %f is the file to open."""
    mimes = [MIME_NATIVE]
    for a in ASSOCIATIONS:
        if not a["native"] and a["mime"] and a["mime"] not in mimes:
            mimes.append(a["mime"])
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Parametric CAD\n"
        f"Exec=\"{exe}\" %f\n"
        "Terminal=false\n"
        "Categories=Graphics;Engineering;Science;\n"
        f"MimeType={';'.join(mimes)};\n"
    )


def linux_mime_xml() -> str:
    """shared-mime-info package defining ONLY the native .step+ type (the interchange formats reuse
    the system's existing MIME types, so we don't clobber how a .stl/.step is identified)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
        f'  <mime-type type="{MIME_NATIVE}">\n'
        "    <comment>Billet Document</comment>\n"
        f'    <icon name="{MIME_ICON}"/>\n'
        '    <glob pattern="*.step+"/>\n'
        "  </mime-type>\n"
        "</mime-info>\n"
    )


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def _doc_icon_source() -> Path | None:
    """The branded 256px document icon shipped in the repo (assets/doc_icon.png), or None
    when this checkout/deployment doesn't carry it — the icon install is then skipped and
    the associations still work (the file manager falls back to a generic page icon)."""
    src = Path(__file__).resolve().parents[2] / "assets" / "doc_icon.png"
    return src if src.is_file() else None


def _mime_icon_paths(data: Path, icon_name: str = MIME_ICON) -> list[Path]:
    """hicolor mimetype icon paths for every size — the freedesktop lookup for a file of
    type application/vnd.billet.stepplus is <theme>/<S>x<S>/mimetypes/<MIME '/'→'-'>.png."""
    return [data / "icons" / "hicolor" / f"{s}x{s}" / "mimetypes" / f"{icon_name}.png"
            for s in ICON_SIZES]


def _legacy_linux_paths(data: Path) -> list[Path]:
    """Pre-rename (AI-CAD) XDG artifacts removed on both register and unregister."""
    return [data / "applications" / LEGACY_DESKTOP_FILE,
            data / "mime" / "packages" / LEGACY_MIME_PACKAGE,
            *_mime_icon_paths(data, LEGACY_MIME_ICON)]


def _install_mime_icons(data: Path) -> bool:
    """Resize assets/doc_icon.png into the hicolor mimetypes dirs (PIL when available,
    else a plain 256px copy). Returns False when the source icon isn't present."""
    src = _doc_icon_source()
    if src is None:
        return False
    try:
        from PIL import Image  # noqa: WPS433  (optional dependency, degrade gracefully)
        img = Image.open(src).convert("RGBA")
        for size, dest in zip(ICON_SIZES, _mime_icon_paths(data)):
            dest.parent.mkdir(parents=True, exist_ok=True)
            img.resize((size, size), Image.LANCZOS).save(dest)
    except ImportError:
        import shutil
        dest = data / "icons" / "hicolor" / "256x256" / "mimetypes" / f"{MIME_ICON}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    return True


def register_linux(exe: str, unregister: bool, dry_run: bool) -> list[str]:
    data = _xdg_data_home()
    apps = data / "applications"
    mimedir = data / "mime"
    desktop_path = apps / DESKTOP_FILE
    mime_path = mimedir / "packages" / MIME_PACKAGE
    msgs: list[str] = []

    def run(cmd: list[str]) -> None:
        if dry_run:
            return
        try:
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass  # the xdg tool isn't installed — the written files still take effect on next login

    if unregister:
        if not dry_run:
            for p in (desktop_path, mime_path, *_mime_icon_paths(data), *_legacy_linux_paths(data)):
                p.unlink(missing_ok=True)
            run(["update-mime-database", str(mimedir)])
            run(["update-desktop-database", str(apps)])
            run(["gtk-update-icon-cache", "-f", "-t", str(data / "icons" / "hicolor")])
        msgs.append(f"{'would remove' if dry_run else 'removed'} {desktop_path.name} + {mime_path.name}"
                    " + mimetype icons (+ legacy ai-cad.* files)")
        return msgs

    if not dry_run:
        apps.mkdir(parents=True, exist_ok=True)
        (mimedir / "packages").mkdir(parents=True, exist_ok=True)
        # Migration: drop the pre-rename ai-cad.desktop / ai-cad.xml / old mime icons so
        # the shell doesn't keep a duplicate "AI-CAD" handler next to Billet.
        for p in _legacy_linux_paths(data):
            p.unlink(missing_ok=True)
        desktop_path.write_text(linux_desktop_entry(exe), encoding="utf-8")
        mime_path.write_text(linux_mime_xml(), encoding="utf-8")
        # Branded document icon for the native MIME type so Dolphin/Nautilus show it
        # on .step+ files (icon name declared in linux_mime_xml's <icon>).
        if _install_mime_icons(data):
            run(["gtk-update-icon-cache", "-f", "-t", str(data / "icons" / "hicolor")])
            msgs.append(f"installed {MIME_ICON}.png mimetype icons "
                        f"({'/'.join(str(s) for s in ICON_SIZES)})")
        run(["update-mime-database", str(mimedir)])
        run(["update-desktop-database", str(apps)])
        run(["xdg-mime", "default", DESKTOP_FILE, MIME_NATIVE])  # default ONLY for native .step+
    msgs.append(f"{'would write' if dry_run else 'wrote'} {desktop_path} + {mime_path}")
    return msgs


# ----------------------------------------------------------------- entry point

def register(exe: str, unregister: bool = False, dry_run: bool = False) -> list[str]:
    if sys.platform.startswith("win"):
        return register_windows(exe, unregister, dry_run)
    return register_linux(exe, unregister, dry_run)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", default=default_exe(), help="Path to the AI-CAD executable")
    ap.add_argument("--unregister", action="store_true", help="Remove the associations")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change")
    args = ap.parse_args(argv)

    action = "Unregistering" if args.unregister else "Registering"
    print(f"{action} {APP_NAME} file associations ({sys.platform}, per-user)")
    print(f"  exe : {args.exe}")
    for m in register(args.exe, args.unregister, args.dry_run):
        print(f"  {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
