#!/usr/bin/env bash
# packaging/linux/build_linux_packages.sh — turn a verified Chudware AppDir into distro
# packages (.deb, .rpm, .tar.zst) without touching one payload byte.
#
# The input is the SAME `squashfs-root` the release build already extracted from the finished
# AppImage and verified (ldd clean, chudware-hostd loads, signed payload manifest
# verifies). That tree is copied verbatim to /opt/chudware, so
# payload-manifest.v1.json still verifies after the package is installed: the manifest
# records path + size + sha256 + mode for every file and rejects extra files, missing
# files and symlinks, which is why nothing is added, removed, renamed or stripped
# inside /opt/chudware. Everything a distro package owes the desktop — the
# /usr/bin/chudware launcher symlink, the .desktop entry, the hicolor icons, the
# copyright file — lives OUTSIDE that directory for exactly that reason.
#
# Filenames and the deb/rpm version fields come from packaging/artifact_names.py, the
# single source of release file names; this script must never hand-format a name.
#
# Deterministic by construction: every mtime is pinned to SOURCE_DATE_EPOCH, tar entries
# are name-sorted with numeric root ownership, compressors are single-threaded at a fixed
# level, the ar member headers carry zeroed timestamps/ids, and the rpm is built with
# rpm's own source-date-epoch handling plus a fixed build host. Two runs from the same
# AppDir produce byte-identical packages.
#
# Usage:
#   packaging/linux/build_linux_packages.sh --appdir DIR --out DIR [options]
#
#   --appdir DIR      verified AppDir / extracted squashfs-root (required)
#   --out DIR         where the packages are written (required)
#   --version V       release version; default: the AppDir manifest's version
#   --format F        deb | rpm | tar | all   (default: all)
#   --print-name      print the canonical filename(s) for --version/--format and exit
#   --no-verify       skip re-verifying the signed payload manifest from inside the
#                     built package (the check that proves the payload survived)
#   --keep-work       keep the staging tree for inspection
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARTIFACT_NAMES="$REPO/packaging/artifact_names.py"
PYTHON="${CHUDWARE_PACKAGING_PYTHON:-python3}"

# Compression settings are part of the reproducibility contract, so they are constants,
# not knobs: a caller that could change them could produce two different "identical"
# builds. -T1 is mandatory (xz and zstd both vary their output with thread count).
# xz -6 rather than -9: on a ~600 MB payload -9 costs several extra minutes of release
# build wall clock and ~640 MB of compressor memory for a low single-digit size win.
DEB_XZ_LEVEL=6
TAR_ZSTD_LEVEL=19

APPDIR=""
OUT=""
VERSION=""
FORMAT=all
PRINT_NAME=0
VERIFY=1
KEEP_WORK=0

while [ $# -gt 0 ]; do
    case "$1" in
        --appdir) APPDIR="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --format) FORMAT="$2"; shift 2 ;;
        --print-name) PRINT_NAME=1; shift ;;
        --no-verify) VERIFY=0; shift ;;
        --keep-work) KEEP_WORK=1; shift ;;
        -h|--help) sed -n '2,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$FORMAT" in
    deb|rpm|tar|all) ;;
    *) echo "error: --format must be deb, rpm, tar, or all (got '$FORMAT')" >&2; exit 2 ;;
esac
[ -f "$ARTIFACT_NAMES" ] || {
    echo "error: $ARTIFACT_NAMES is missing; it is the single source of truth for release filenames" >&2
    exit 1
}

canonical_name() {  # canonical_name <format>
    "$PYTHON" "$ARTIFACT_NAMES" --version "$VERSION" --platform linux --arch x86_64 --format "$1"
}
name_field() {  # name_field <format> <field>
    "$PYTHON" "$ARTIFACT_NAMES" --version "$VERSION" --platform linux --arch x86_64 \
        --format "$1" --field "$2"
}

if [ "$PRINT_NAME" = 1 ]; then
    [ -n "$VERSION" ] || { echo "error: --print-name needs --version" >&2; exit 2; }
    if [ "$FORMAT" = all ]; then
        for f in deb rpm tar; do canonical_name "$f"; done
    else
        canonical_name "$FORMAT"
    fi
    exit 0
fi

[ -n "$APPDIR" ] && [ -n "$OUT" ] || { echo "error: --appdir and --out are required" >&2; exit 2; }
[ -d "$APPDIR" ] || { echo "error: --appdir is not a directory: $APPDIR" >&2; exit 1; }
APPDIR="$(cd "$APPDIR" && pwd)"
MANIFEST="$APPDIR/payload-manifest.v1.json"
[ -f "$MANIFEST" ] || {
    echo "error: $MANIFEST is missing; only a prepared/verified AppDir can be packaged" >&2
    exit 1
}

MANIFEST_VERSION="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$MANIFEST")"
if [ -z "$VERSION" ]; then
    VERSION="$MANIFEST_VERSION"
elif [ "$VERSION" != "$MANIFEST_VERSION" ]; then
    # A package whose filename claims one version while the signed payload inside it
    # claims another is an auto-update trap: it would be offered for a version the
    # client then refuses to run.
    echo "error: --version $VERSION disagrees with the AppDir manifest ($MANIFEST_VERSION)" >&2
    exit 1
fi
echo "==> version $VERSION from $(basename "$MANIFEST")"

# SOURCE_DATE_EPOCH decides every recorded mtime. Defaulting it to the manifest's
# created_at keeps the packages a pure function of the AppDir: a caller that passes
# nothing still gets the same bytes tomorrow.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH="$("$PYTHON" - "$MANIFEST" <<'PY'
import json, sys
from datetime import datetime, timezone
created = json.load(open(sys.argv[1], encoding="utf-8"))["created_at"]
stamp = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
print(int(stamp.timestamp()))
PY
)"
fi
export SOURCE_DATE_EPOCH
echo "==> SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="$OUT/.linux-packages"
rm -rf "$WORK"
mkdir -p "$WORK"
cleanup() { [ "$KEEP_WORK" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

PKGTOOL="$WORK/pkgtool.py"
cat > "$PKGTOOL" <<'PKGTOOL_PY'
"""Staging, dependency resolution, ar assembly and payload re-verification.

Kept in Python rather than shell for three reasons that each bit during development:
  * the AppDir must be copied with its mode bits and WITHOUT ACLs or xattrs (the signed
    manifest records st_mode only, and `cp -a` drags ACLs along on this repo's tree);
  * DT_NEEDED and the glibc symbol-version floor have to be read out of the ELFs, and
    the release runner's provisioned toolset has no objdump or readelf;
  * `ar` is not on that toolset either, and the deb archive format is 60 bytes of header
    per member, so writing it here removes a binutils dependency instead of adding one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath

PAYLOAD_PREFIX = "opt/chudware"
ICON_SIZES = (64, 128, 256)

# Debian has no soname-level dependency resolution the way rpm does, so an unbundled
# soname has to be named as a package. This table covers the AppImage excludelist (the
# libraries build_appimage.sh deliberately leaves host-provided) plus the CEF/GTK/NSS
# stack the GUI resolves from the host. `a | b` alternations exist because Debian renamed
# several of these packages for the 64-bit time_t transition; either name satisfies apt.
#
# An unmapped soname FAILS THE .deb, never a silently dropped dependency: a .deb that
# installs and then fails to start is worse than one that fails to build. The .rpm
# requires sonames directly and needs no table, so a gap here costs one format only.
DEB_PACKAGE_FOR_SONAME = {
    # glibc and the compiler runtimes
    "libc.so.6": "libc6",
    "libm.so.6": "libc6",
    "libdl.so.2": "libc6",
    "libpthread.so.0": "libc6",
    "librt.so.1": "libc6",
    "libresolv.so.2": "libc6",
    "libutil.so.1": "libc6",
    "libanl.so.1": "libc6",
    "libmvec.so.1": "libc6",
    "libthread_db.so.1": "libc6",
    "libBrokenLocale.so.1": "libc6",
    "libcidn.so.1": "libc6",
    # Some glibc-adjacent objects name the loader as a real DT_NEEDED entry, not just
    # as PT_INTERP, so it turns up in the external set. It ships in libc6.
    "ld-linux-x86-64.so.2": "libc6",
    "ld-linux.so.2": "libc6",
    "libstdc++.so.6": "libstdc++6",
    "libgcc_s.so.1": "libgcc-s1",
    "libatomic.so.1": "libatomic1",
    "libgomp.so.1": "libgomp1",
    # X11 and Wayland
    "libX11.so.6": "libx11-6",
    "libX11-xcb.so.1": "libx11-xcb1",
    "libXau.so.6": "libxau6",
    "libXdmcp.so.6": "libxdmcp6",
    "libXcomposite.so.1": "libxcomposite1",
    "libXcursor.so.1": "libxcursor1",
    "libXdamage.so.1": "libxdamage1",
    "libXext.so.6": "libxext6",
    "libXfixes.so.3": "libxfixes3",
    "libXi.so.6": "libxi6",
    "libXinerama.so.1": "libxinerama1",
    "libXrandr.so.2": "libxrandr2",
    "libXrender.so.1": "libxrender1",
    "libXtst.so.6": "libxtst6",
    "libXss.so.1": "libxss1",
    "libICE.so.6": "libice6",
    "libSM.so.6": "libsm6",
    "libxcb.so.1": "libxcb1",
    "libxcb-dri2.so.0": "libxcb1",
    "libxcb-dri3.so.0": "libxcb1",
    "libxcb-glx.so.0": "libxcb-glx0",
    "libxcb-present.so.0": "libxcb-present0",
    "libxcb-randr.so.0": "libxcb-randr0",
    "libxcb-render.so.0": "libxcb-render0",
    "libxcb-shm.so.0": "libxcb-shm0",
    "libxcb-sync.so.1": "libxcb-sync1",
    "libxcb-xfixes.so.0": "libxcb-xfixes0",
    "libxshmfence.so.1": "libxshmfence1",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libxkbfile.so.1": "libxkbfile1",
    "libwayland-client.so.0": "libwayland-client0",
    "libwayland-cursor.so.0": "libwayland-cursor0",
    "libwayland-egl.so.1": "libwayland-egl1",
    "libwayland-server.so.0": "libwayland-server0",
    # graphics drivers and GL dispatch
    "libGL.so.1": "libgl1",
    "libGLX.so.0": "libglx0",
    "libGLdispatch.so.0": "libglvnd0",
    "libOpenGL.so.0": "libopengl0",
    "libEGL.so.1": "libegl1",
    "libGLESv2.so.2": "libgles2",
    "libglapi.so.0": "libglapi-mesa",
    "libgbm.so.1": "libgbm1",
    "libdrm.so.2": "libdrm2",
    "libepoxy.so.0": "libepoxy0",
    "libvulkan.so.1": "libvulkan1",
    # text, fonts, imaging
    "libfontconfig.so.1": "libfontconfig1",
    "libfreetype.so.6": "libfreetype6",
    "libharfbuzz.so.0": "libharfbuzz0b",
    "libfribidi.so.0": "libfribidi0",
    "libgraphite2.so.3": "libgraphite2-3",
    "libpng16.so.16": "libpng16-16t64 | libpng16-16",
    "libjpeg.so.62": "libjpeg62-turbo",
    "libjpeg.so.8": "libjpeg-turbo8",
    "libtiff.so.6": "libtiff6",
    "libwebp.so.7": "libwebp7",
    "libcairo.so.2": "libcairo2",
    "libcairo-gobject.so.2": "libcairo-gobject2",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libpangocairo-1.0.so.0": "libpangocairo-1.0-0",
    "libpangoft2-1.0.so.0": "libpangoft2-1.0-0",
    "libpixman-1.so.0": "libpixman-1-0",
    # GLib / GTK / accessibility (CEF)
    "libglib-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgobject-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgio-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgmodule-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgthread-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgtk-3.so.0": "libgtk-3-0t64 | libgtk-3-0",
    "libgdk-3.so.0": "libgtk-3-0t64 | libgtk-3-0",
    "libgdk_pixbuf-2.0.so.0": "libgdk-pixbuf-2.0-0",
    "libatk-1.0.so.0": "libatk1.0-0t64 | libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0t64 | libatk-bridge2.0-0",
    "libatspi.so.0": "libatspi2.0-0t64 | libatspi2.0-0",
    "libcups.so.2": "libcups2t64 | libcups2",
    # NSS / NSPR (CEF)
    "libnss3.so": "libnss3",
    "libnssutil3.so": "libnss3",
    "libsmime3.so": "libnss3",
    "libssl3.so": "libnss3",
    "libnspr4.so": "libnspr4",
    "libplc4.so": "libnspr4",
    "libplds4.so": "libnspr4",
    # system services, crypto, compression, misc
    "libdbus-1.so.3": "libdbus-1-3",
    "libudev.so.1": "libudev1",
    "libsystemd.so.0": "libsystemd0",
    "libasound.so.2": "libasound2t64 | libasound2",
    "libjack.so.0": "libjack-jackd2-0 | libjack0",
    "libpipewire-0.3.so.0": "libpipewire-0.3-0",
    "libpulse.so.0": "libpulse0",
    "libusb-1.0.so.0": "libusb-1.0-0",
    "libexpat.so.1": "libexpat1",
    "libz.so.1": "zlib1g",
    "liblzma.so.5": "liblzma5",
    "libzstd.so.1": "libzstd1",
    "libbz2.so.1.0": "libbz2-1.0",
    "libbrotlidec.so.1": "libbrotli1",
    "libbrotlicommon.so.1": "libbrotli1",
    "libcrypto.so.3": "libssl3t64 | libssl3",
    "libssl.so.3": "libssl3t64 | libssl3",
    "libgmp.so.10": "libgmp10",
    "libgpg-error.so.0": "libgpg-error0",
    "libgcrypt.so.20": "libgcrypt20",
    "libuuid.so.1": "libuuid1",
    "libcom_err.so.2": "libcom-err2",
    "libselinux.so.1": "libselinux1",
    "libffi.so.8": "libffi8",
    "libpcre2-8.so.0": "libpcre2-8-0",
    "libcurl.so.4": "libcurl4t64 | libcurl4 | libcurl3-gnutls",
    "libtbb.so.12": "libtbb12",
    "libtbbmalloc.so.2": "libtbbmalloc2",
    "libnghttp2.so.14": "libnghttp2-14",
    "libidn2.so.0": "libidn2-0",
    "libpsl.so.5": "libpsl5",
    "libssh2.so.1": "libssh2-1t64 | libssh2-1",
    "libldap.so.2": "libldap2",
    "libkrb5.so.3": "libkrb5-3",
    "libk5crypto.so.3": "libk5crypto3",
    "libgssapi_krb5.so.2": "libgssapi-krb5-2",
    "libcap.so.2": "libcap2",
    "libmount.so.1": "libmount1",
    "libblkid.so.1": "libblkid1",
    # Reachable as a DIRECT link from the engine, libcef.so or libcurl-consuming code.
    # Second-order dependencies of these (libavahi's own tail, libcbor, libtinysparql)
    # are deliberately absent: apt resolves them through the packages named here, and
    # several carry a version-numbered Debian package name that differs per release, so
    # guessing one would produce an unsatisfiable dependency instead of a missing one.
    "libxml2.so.2": "libxml2",
    "libsqlite3.so.0": "libsqlite3-0",
    "liblcms2.so.2": "liblcms2-2",
    "libcrypt.so.2": "libcrypt1",
    "libbz2.so.1": "libbz2-1.0",
    "libevent-2.1.so.7": "libevent-2.1-7t64 | libevent-2.1-7",
    "libseccomp.so.2": "libseccomp2",
    "libgnutls.so.30": "libgnutls30t64 | libgnutls30",
    "libnettle.so.8": "libnettle8t64 | libnettle8",
    "libhogweed.so.6": "libhogweed6t64 | libhogweed6",
    "libtasn1.so.6": "libtasn1-6",
    "libunistring.so.5": "libunistring5",
    "libp11-kit.so.0": "libp11-kit0",
    "libjson-glib-1.0.so.0": "libjson-glib-1.0-0",
    "libthai.so.0": "libthai0",
    "libdatrie.so.1": "libdatrie1",
    "libkrb5support.so.0": "libkrb5support0",
    "libkeyutils.so.1": "libkeyutils1",
    "libsasl2.so.3": "libsasl2-2",
    "liblber.so.2": "libldap2",
    "libavahi-client.so.3": "libavahi-client3",
    "libavahi-common.so.3": "libavahi-common3",
    "libssh.so.4": "libssh-4",
    "libnghttp3.so.9": "libnghttp3-9",
    "libcloudproviders.so.0": "libcloudproviders0",
    "libfido2.so.1": "libfido2-1",
}

# glibc symbol versions that are not release versions and must not become a floor.
_NON_NUMERIC_GLIBC = {"GLIBC_PRIVATE", "GLIBC_ABI_DT_RELR"}


# --------------------------------------------------------------------------- ELF


def elf_dynamic(path: Path) -> tuple[set[str], set[str], str | None] | None:
    """``(needed, symbol_versions, soname)`` for an ELF64 LSB object, else ``None``.

    Read straight out of PT_DYNAMIC rather than the section headers so a fully
    stripped object still answers, and so nothing here depends on binutils.
    """
    with path.open("rb") as handle:
        head = handle.read(64)
        if len(head) < 64 or head[:4] != b"\x7fELF" or head[4] != 2 or head[5] != 1:
            return None

        def u(raw: bytes) -> int:
            return int.from_bytes(raw, "little")

        phoff, phentsize, phnum = u(head[32:40]), u(head[54:56]), u(head[56:58])
        if not phoff or not phentsize or not phnum:
            return set(), set(), None
        handle.seek(phoff)
        table = handle.read(phentsize * phnum)
        loads: list[tuple[int, int, int, int]] = []
        dynamic: tuple[int, int] | None = None
        for index in range(phnum):
            entry = table[index * phentsize:(index + 1) * phentsize]
            if len(entry) < 56:
                continue
            kind = u(entry[0:4])
            offset, vaddr = u(entry[8:16]), u(entry[16:24])
            filesz, memsz = u(entry[32:40]), u(entry[40:48])
            if kind == 1:  # PT_LOAD
                loads.append((vaddr, memsz, offset, filesz))
            elif kind == 2:  # PT_DYNAMIC
                dynamic = (offset, filesz)
        if dynamic is None:
            return set(), set(), None

        def to_offset(vaddr: int) -> int | None:
            for base, memsz, offset, filesz in loads:
                if base <= vaddr < base + memsz and vaddr - base < filesz:
                    return offset + (vaddr - base)
            return None

        handle.seek(dynamic[0])
        raw = handle.read(dynamic[1])
        tags: list[tuple[int, int]] = []
        for index in range(0, len(raw) - 15, 16):
            tag, value = u(raw[index:index + 8]), u(raw[index + 8:index + 16])
            if tag == 0:  # DT_NULL
                break
            tags.append((tag, value))

        def first(tag: int) -> int | None:
            return next((value for kind, value in tags if kind == tag), None)

        strtab, strsz = first(5), first(10) or 0
        if strtab is None:
            return set(), set(), None
        strtab_offset = to_offset(strtab)
        if strtab_offset is None:
            return set(), set(), None
        handle.seek(strtab_offset)
        strings = handle.read(strsz or 1 << 20)

        def text(index: int) -> str:
            end = strings.find(b"\0", index)
            return strings[index:end if end >= 0 else None].decode("utf-8", "replace")

        needed = {text(value) for kind, value in tags if kind == 1}      # DT_NEEDED
        soname_index = first(14)                                        # DT_SONAME
        soname = text(soname_index) if soname_index is not None else None

        versions: set[str] = set()
        verneed, verneednum = first(0x6FFFFFFE), first(0x6FFFFFFF) or 0
        if verneed is not None:
            cursor = to_offset(verneed)
            while cursor is not None and verneednum > 0:
                handle.seek(cursor)
                need = handle.read(16)
                if len(need) < 16:
                    break
                count, aux_offset, next_offset = u(need[2:4]), u(need[8:12]), u(need[12:16])
                aux = cursor + aux_offset
                for _ in range(count):
                    handle.seek(aux)
                    entry = handle.read(16)
                    if len(entry) < 16:
                        break
                    versions.add(text(u(entry[8:12])))
                    aux_next = u(entry[12:16])
                    if not aux_next:
                        break
                    aux += aux_next
                if not next_offset:
                    break
                cursor += next_offset
                verneednum -= 1
        return needed, versions, soname


def _glibc_floor(versions: set[str]) -> str | None:
    """Highest GLIBC_x.y[.z] symbol version the payload references."""
    best: tuple[int, ...] | None = None
    for name in versions:
        if not name.startswith("GLIBC_") or name in _NON_NUMERIC_GLIBC:
            continue
        parts = name[len("GLIBC_"):].split(".")
        if not all(part.isdigit() for part in parts):
            continue
        value = tuple(int(part) for part in parts)
        if best is None or value > best:
            best = value
    return ".".join(str(part) for part in best) if best else None


# ----------------------------------------------------------------------- staging


def _walk(root: Path) -> list[str]:
    found: list[str] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        prefix = os.path.relpath(directory, root)
        prefix = "" if prefix == "." else prefix
        for name in sorted(filenames):
            found.append(f"{prefix}/{name}" if prefix else name)
    return sorted(found)


def _copy_payload(appdir: Path, destination: Path) -> list[str]:
    """Copy the AppDir byte-for-byte with its mode bits, and nothing else.

    No ACLs, no xattrs, no timestamps: the signed manifest records path, size, sha256
    and st_mode, and those are exactly what is reproduced. A symlink or special file
    would already have failed the manifest, so finding one here is a hard error rather
    than something to paper over.
    """
    copied: list[str] = []
    for directory, dirnames, filenames in os.walk(appdir):
        dirnames.sort()
        relative = os.path.relpath(directory, appdir)
        target_dir = destination if relative == "." else destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(target_dir, 0o755)
        for name in sorted(filenames):
            source = Path(directory) / name
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SystemExit(f"error: {source} is not a regular file; the AppDir must be flat bytes")
            target = target_dir / name
            with source.open("rb") as reader, target.open("wb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            os.chmod(target, stat.S_IMODE(info.st_mode))
            copied.append(os.path.relpath(target, destination).replace(os.sep, "/"))
    return copied


def _desktop_entry(source: Path, version: str) -> str:
    """The shipped entry: the AppDir's entry minus AppImage-only and developer lines.

    The repository copy carries maintenance comments (the WM_CLASS warning) that belong
    in the repository, and build_appimage.sh injects X-AppImage-Version because
    appimagetool would otherwise rewrite the signed file. Neither is true of an
    installed package, so both are dropped and the real version is recorded instead.
    """
    lines: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.split("=", 1)[0].strip() == "X-AppImage-Version":
            continue
        lines.append(line)
    if "[Desktop Entry]" not in lines:
        raise SystemExit(f"error: {source} has no [Desktop Entry] group")
    lines.append(f"X-Chudware-Version={version}")
    return "\n".join(lines) + "\n"


def _copyright(license_path: Path) -> str:
    # Machine-readable debian/copyright (DEP-5).
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: Chudware\n"
        "Upstream-Contact: Chudware contributors <167375961+Hunter-124@users.noreply.github.com>\n"
        "Source: https://github.com/Hunter-124/Chudware\n"
        "Comment: Bundled third-party components and their licenses are listed in\n"
        " /opt/chudware/THIRD_PARTY_NOTICES.md and /opt/chudware/usr/bin/CHUDCODE-NOTICE.\n"
        "\n"
        "Files: *\n"
        "Copyright: 2026 Hunter Edmonds and Chudware contributors\n"
        "License: MIT\n"
        "\n"
        "License: MIT\n"
        + "".join(
            (" " + line if line.strip() else " .") + "\n"
            for line in license_path.read_text(encoding="utf-8").splitlines()
        )
    )


def command_stage(args: argparse.Namespace) -> int:
    appdir, root = Path(args.appdir).resolve(), Path(args.root)
    if root.exists():
        shutil.rmtree(root)
    payload = root / PAYLOAD_PREFIX
    _copy_payload(appdir, payload)

    # The launcher. AppRun exports LD_LIBRARY_PATH and pins the hostd instance key, so
    # it — not usr/bin/chudware — is the correct entry point for an installed tree. The
    # symlink is relative so the package relocates cleanly and so it stays OUTSIDE
    # /opt/chudware, where the signed manifest forbids symlinks.
    if not (payload / "AppRun").is_file():
        raise SystemExit("error: the AppDir has no AppRun; there is nothing to launch")
    bindir = root / "usr/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    os.symlink(f"../../{PAYLOAD_PREFIX}/AppRun", bindir / "chudware")

    applications = root / "usr/share/applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / "chudware.desktop"
    entry.write_text(_desktop_entry(payload / "chudware.desktop", args.version), encoding="utf-8")
    os.chmod(entry, 0o644)

    icon_set = Path(args.icon_set) if args.icon_set else None
    fallback = payload / "chudware.png"
    for size in ICON_SIZES:
        target_dir = root / f"usr/share/icons/hicolor/{size}x{size}/apps"
        target_dir.mkdir(parents=True, exist_ok=True)
        source = icon_set / f"{size}.png" if icon_set else None
        if source is None or not source.is_file():
            # assets/appicon carries art drawn natively at each size; without it the
            # 256 master is the only honest option (resampling here would make the
            # output depend on whatever Pillow the build host happens to have).
            if not fallback.is_file():
                raise SystemExit("error: no icon available: neither the icon set nor the AppDir icon exists")
            source = fallback
        target = target_dir / "chudware.png"
        shutil.copyfile(source, target)
        os.chmod(target, 0o644)

    docs = root / "usr/share/doc/chudware"
    docs.mkdir(parents=True, exist_ok=True)
    copyright_file = docs / "copyright"
    copyright_file.write_text(_copyright(Path(args.license)), encoding="utf-8")
    os.chmod(copyright_file, 0o644)

    # Pin every mtime. tar's --mtime covers the deb and the tarball, but rpmbuild reads
    # the buildroot's timestamps, so they have to be right on disk too.
    epoch = int(args.epoch)
    for directory, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            os.utime(os.path.join(directory, name), (epoch, epoch), follow_symlinks=False)
    os.utime(root, (epoch, epoch))

    installed = 0
    for relative in _walk(root):
        installed += (root / relative).lstat().st_size
    json.dump({"installed_size_kib": (installed + 1023) // 1024}, sys.stdout)
    return 0


# ------------------------------------------------------------------ dependencies


def command_deps(args: argparse.Namespace) -> int:
    payload = Path(args.root) / PAYLOAD_PREFIX
    provided: set[str] = set()
    needed: set[str] = set()
    versions: set[str] = set()
    elf_count = 0
    for relative in _walk(payload):
        path = payload / relative
        provided.add(path.name)
        try:
            parsed = elf_dynamic(path)
        except OSError as exc:
            raise SystemExit(f"error: cannot read {path}: {exc}")
        if parsed is None:
            continue
        elf_count += 1
        entry_needed, entry_versions, soname = parsed
        needed |= entry_needed
        versions |= entry_versions
        if soname:
            provided.add(soname)
    if not elf_count:
        raise SystemExit("error: the payload contains no ELF objects; this is not a Chudware AppDir")

    external = sorted(needed - provided)
    # Only DIRECT links matter here. An external soname's own dependency tail is
    # resolved by whatever package provides it, exactly as for any other package, so
    # this set is the DT_NEEDED entries of the payload and nothing beyond them.
    unmapped = [soname for soname in external if soname not in DEB_PACKAGE_FOR_SONAME]

    floor = _glibc_floor(versions)
    depends: list[str] = []
    for soname in external:
        package = DEB_PACKAGE_FOR_SONAME.get(soname)
        if package is None:
            continue  # reported through "unmapped_sonames"; only the deb leg is blocked
        if package == "libc6" and floor:
            # Without a floor apt installs happily on an older release and the binary
            # dies at load with "version `GLIBC_2.xx' not found".
            package = f"libc6 (>= {floor})"
        if package not in depends:
            depends.append(package)
    depends.sort()

    json.dump({
        "elf_count": elf_count,
        "external_sonames": external,
        "unmapped_sonames": unmapped,
        "glibc_floor": floor,
        "deb_depends": ", ".join(depends),
        # rpm resolves sonames directly, which is both more precise than a package name
        # and portable across Fedora/RHEL/openSUSE, so the rpm needs no mapping table —
        # and so a gap in the Debian table never costs the .rpm.
        "rpm_requires": [f"{soname}()(64bit)" for soname in external],
    }, sys.stdout)
    return 0


# ------------------------------------------------------------------------ md5sums


def command_md5sums(args: argparse.Namespace) -> int:
    root = Path(args.root)
    out: list[str] = []
    for relative in _walk(root):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue  # dpkg's md5sums covers regular files only
        digest = hashlib.md5()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        out.append(f"{digest.hexdigest()}  {relative}")
    sys.stdout.write("".join(line + "\n" for line in out))
    return 0


# ---------------------------------------------------------------------- deb (ar)


def command_ar(args: argparse.Namespace) -> int:
    """Write a deb's outer `ar` archive.

    Order is load-bearing: dpkg requires debian-binary, then control.tar.*, then
    data.tar.*. Headers carry zeroed mtime/uid/gid and a fixed 0644 mode, which is what
    `ar rcD` would produce; names are space-padded with no trailing slash, matching
    dpkg-deb's own writer.
    """
    target = Path(args.out)
    with target.open("wb") as archive:
        archive.write(b"!<arch>\n")
        for member in args.member:
            name, _, source = member.partition("=")
            if not source:
                raise SystemExit(f"error: --member expects NAME=PATH (got {member!r})")
            if len(name) > 16:
                raise SystemExit(f"error: ar member name is too long for the format: {name}")
            path = Path(source)
            size = path.stat().st_size
            archive.write(
                name.ljust(16).encode("ascii")
                + b"0".ljust(12)          # mtime
                + b"0".ljust(6)           # uid
                + b"0".ljust(6)           # gid
                + b"100644".ljust(8)      # mode
                + str(size).ljust(10).encode("ascii")
                + b"`\n"
            )
            # Streamed: data.tar.xz is hundreds of megabytes on a real release.
            with path.open("rb") as reader:
                shutil.copyfileobj(reader, archive, 1024 * 1024)
            if size % 2:
                archive.write(b"\n")
    os.chmod(target, 0o644)
    return 0


# ------------------------------------------------------------------ verification


def command_verify(args: argparse.Namespace) -> int:
    """Re-verify the signed payload manifest against a tree taken from a built package.

    This is the whole point of the exercise: if packaging altered one byte or one mode
    bit, the installed application would fail its own runtime integrity check.
    """
    sys.path.insert(0, args.repo)
    from services.installer.payload_manifest import read_manifest, verify_manifest_payload

    root = Path(args.root)
    manifest, canonical = read_manifest(root)
    verify_manifest_payload(root, canonical)
    print(
        "payload manifest verified from the package: %d files, key_id %s, version %s"
        % (len(manifest["files"]), manifest["key_id"], manifest["version"])
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage")
    stage.add_argument("--appdir", required=True)
    stage.add_argument("--root", required=True)
    stage.add_argument("--version", required=True)
    stage.add_argument("--epoch", required=True)
    stage.add_argument("--license", required=True)
    stage.add_argument("--icon-set")
    stage.set_defaults(handler=command_stage)

    deps = sub.add_parser("deps")
    deps.add_argument("--root", required=True)
    deps.set_defaults(handler=command_deps)

    sums = sub.add_parser("md5sums")
    sums.add_argument("--root", required=True)
    sums.set_defaults(handler=command_md5sums)

    archive = sub.add_parser("ar")
    archive.add_argument("--out", required=True)
    archive.add_argument("--member", action="append", required=True)
    archive.set_defaults(handler=command_ar)

    verify = sub.add_parser("verify")
    verify.add_argument("--root", required=True)
    verify.add_argument("--repo", required=True)
    verify.set_defaults(handler=command_verify)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
PKGTOOL_PY

pkgtool() { "$PYTHON" "$PKGTOOL" "$@"; }

# --------------------------------------------------------------------- staging

ROOT="$WORK/root"
PAYLOAD_PREFIX_LITERAL="opt/chudware"
echo "==> staging /opt/chudware + desktop integration from $APPDIR"
# assets/appicon carries the tesseract mark drawn natively at each size; pass it when
# it exists and let the stager fall back to the AppDir's 256 master when it does not.
STAGE_ARGS=(--appdir "$APPDIR" --root "$ROOT" --version "$VERSION"
            --epoch "$SOURCE_DATE_EPOCH" --license "$REPO/LICENSE")
[ -d "$REPO/assets/appicon" ] && STAGE_ARGS+=(--icon-set "$REPO/assets/appicon")
STAGE_META="$(pkgtool stage "${STAGE_ARGS[@]}")"
INSTALLED_SIZE_KIB="$("$PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["installed_size_kib"])' "$STAGE_META")"
echo "==> staged tree: ${INSTALLED_SIZE_KIB} KiB installed"

echo "==> resolving host-provided sonames"
DEPS_META="$(pkgtool deps --root "$ROOT")"
read_dep() {
    "$PYTHON" -c '
import json, sys
value = json.loads(sys.argv[1])[sys.argv[2]]
print("\n".join(value) if isinstance(value, list) else ("" if value is None else value))
' "$DEPS_META" "$1"
}
DEB_DEPENDS="$(read_dep deb_depends)"
GLIBC_FLOOR="$(read_dep glibc_floor)"
echo "==> $(read_dep elf_count) ELF objects, glibc floor ${GLIBC_FLOOR:-unknown}, $(read_dep external_sonames | wc -l) host-provided sonames"

# One tar of the staged tree, reused by the deb and the tarball. Everything that makes
# tar output vary is pinned: entry order, ownership, timestamps and archive format.
# --no-acls/--no-xattrs/--no-selinux are explicit because some distributions patch the
# defaults, and either would leak build-host state into the archive. "-" writes to
# stdout so a 600 MB payload never needs an uncompressed copy on disk.
build_payload_tar() {  # build_payload_tar <destination|->
    LC_ALL=C tar \
        --format=gnu --sort=name --numeric-owner --owner=0 --group=0 \
        --mtime="@$SOURCE_DATE_EPOCH" --no-acls --no-xattrs --no-selinux \
        -C "$ROOT" -cf "$1" .
}

DEB_OUT=""
RPM_OUT=""
TAR_OUT=""

# ------------------------------------------------------------------------- deb

build_deb() {
    local name debver control data control_tar unmapped
    # Debian has no soname-level resolution, so an unmapped soname would silently
    # become a missing Depends and ship a .deb that installs and then will not start.
    # This blocks the .deb ONLY: the .rpm requires sonames directly and is unaffected,
    # so a table gap costs one format rather than the whole Linux packaging leg.
    unmapped="$(read_dep unmapped_sonames)"
    if [ -n "$unmapped" ]; then
        echo "error: no Debian package is mapped for these host-provided sonames:" >&2
        printf '  %s\n' $unmapped >&2
        echo "       add them to DEB_PACKAGE_FOR_SONAME in $(basename "${BASH_SOURCE[0]}")." >&2
        echo "       Only direct DT_NEEDED entries need an entry; their own dependency" >&2
        echo "       tails are resolved by the packages named there." >&2
        exit 1
    fi
    name="$(canonical_name deb)"
    debver="$(name_field deb deb-version)"
    DEB_OUT="$OUT/$name"
    echo "==> building $name (Version: $debver)"

    control="$WORK/control"
    rm -rf "$control"
    mkdir -p "$control"

    # Field order is the one dpkg-deb itself emits, so `dpkg-deb --info` output reads
    # like any other package. Installed-Size is in KiB, as Debian policy requires.
    {
        printf 'Package: chudware\n'
        printf 'Version: %s\n' "$debver"
        printf 'Architecture: amd64\n'
        printf 'Maintainer: Chudware contributors <167375961+Hunter-124@users.noreply.github.com>\n'
        printf 'Installed-Size: %s\n' "$INSTALLED_SIZE_KIB"
        printf 'Depends: %s\n' "$DEB_DEPENDS"
        printf 'Section: graphics\n'
        printf 'Priority: optional\n'
        printf 'Homepage: https://chudware.com/\n'
        printf 'Description: AI-native parametric CAD\n'
        printf ' Chudware is a parametric CAD application with an integrated modelling\n'
        printf ' engine, assembly and drawing workspaces, simulation, CAM and an AI\n'
        printf ' assistant that drives the same tools a person does.\n'
        printf ' .\n'
        printf ' The application payload is installed under /opt/chudware and is covered by\n'
        printf ' a signed manifest that it verifies at startup. Chudware is released under\n'
        printf ' the MIT License; see /usr/share/doc/chudware/copyright.\n'
    } > "$control/control"

    pkgtool md5sums --root "$ROOT" > "$control/md5sums"

    cat > "$control/postinst" <<'SH'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor || true
    fi
fi
exit 0
SH
    cat > "$control/postrm" <<'SH'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor || true
    fi
fi
exit 0
SH
    chmod 0755 "$control/postinst" "$control/postrm"
    chmod 0644 "$control/control" "$control/md5sums"

    printf '2.0\n' > "$WORK/debian-binary"

    control_tar="$WORK/control.tar.gz"
    # gzip -n so no filename or timestamp reaches the stream.
    LC_ALL=C tar --format=gnu --sort=name --numeric-owner --owner=0 --group=0 \
        --mtime="@$SOURCE_DATE_EPOCH" --no-acls --no-xattrs --no-selinux \
        -C "$control" -cf - . | gzip -9n > "$control_tar"

    # Piped, not staged: an uncompressed intermediate of a ~600 MB payload is pure
    # disk churn on the release runner.
    data="$WORK/data.tar.xz"
    build_payload_tar - | xz "-$DEB_XZ_LEVEL" -T1 -c > "$data"

    pkgtool ar --out "$DEB_OUT" \
        --member "debian-binary=$WORK/debian-binary" \
        --member "control.tar.gz=$control_tar" \
        --member "data.tar.xz=$data"
    rm -f "$data" "$control_tar" "$WORK/debian-binary"

    if command -v dpkg-deb >/dev/null 2>&1; then
        echo "==> dpkg-deb --info $name"
        dpkg-deb --info "$DEB_OUT"
    else
        echo "==> dpkg-deb is not installed on this host; skipping the deb self-check" \
             "(the archive was written directly and does not need dpkg tooling)"
    fi
}

# ------------------------------------------------------------------------- rpm

build_rpm() {
    local name rpmver rpmrel top spec built
    name="$(canonical_name rpm)"
    rpmver="$(name_field rpm rpm-version)"
    rpmrel="$(name_field rpm rpm-release)"
    echo "==> building $name (Version: $rpmver Release: $rpmrel)"
    command -v rpmbuild >/dev/null 2>&1 || {
        echo "error: rpmbuild is required for --format rpm; install rpm-build" >&2
        exit 1
    }

    top="$WORK/rpmbuild"
    rm -rf "$top"
    mkdir -p "$top/BUILD" "$top/BUILDROOT" "$top/RPMS" "$top/SPECS" "$top/SOURCES" "$top/SRPMS"
    spec="$top/SPECS/chudware.spec"

    {
        # The payload is already stripped where it should be and is covered by a signed
        # manifest, so every post-install transformation rpm normally applies has to be
        # off: brp-strip would rewrite ELF bytes and break the manifest, and the
        # debuginfo package would be extracted from those same bytes.
        printf '%%global debug_package %%{nil}\n'
        printf '%%global __os_install_post %%{nil}\n'
        printf '%%global __strip /bin/true\n'
        printf '%%global _build_id_links none\n'
        # Reproducibility: fixed build host, build time taken from SOURCE_DATE_EPOCH,
        # single-threaded payload compression.
        printf '%%global _buildhost chudware-release\n'
        printf '%%global source_date_epoch_from_changelog 0\n'
        printf '%%global use_source_date_epoch_as_buildtime 1\n'
        printf '%%global clamp_mtime_to_source_date_epoch 1\n'
        printf '%%global _binary_payload w19T1.zstdio\n'
        printf '\n'
        printf 'Name:           chudware\n'
        printf 'Version:        %s\n' "$rpmver"
        printf 'Release:        %s\n' "$rpmrel"
        printf 'Summary:        AI-native parametric CAD\n'
        printf 'License:        MIT\n'
        printf 'URL:            https://github.com/Hunter-124/Chudware\n'
        printf 'BuildArch:      x86_64\n'
        # Explicit sonames instead of generated dependencies: the automatic generator
        # would also emit Requires for the ~130 libraries bundled under /opt/chudware
        # and Provides for them, offering private OCCT/CEF copies to the whole system.
        printf 'AutoReqProv:    no\n'
        # No explicit Requires: /bin/sh — rpm derives it from the %post/%postun
        # interpreters, and listing it as well produced three duplicate entries.
        read_dep rpm_requires | while IFS= read -r soname; do
            [ -n "$soname" ] && printf 'Requires:       %s\n' "$soname"
        done
        printf '\n'
        printf '%%description\n'
        printf 'Chudware is a parametric CAD application with an integrated modelling engine,\n'
        printf 'assembly and drawing workspaces, simulation, CAM and an AI assistant that drives\n'
        printf 'the same tools a person does.\n'
        printf '\n'
        printf 'The application payload is installed under /opt/chudware and is covered by a\n'
        printf 'signed manifest that it verifies at startup. Chudware is released under the MIT\n'
        printf 'License; see %%{_docdir}/chudware/copyright.\n'
        printf '\n'
        printf '%%install\n'
        # Hardlink rather than copy: the staged tree is hundreds of megabytes and rpm
        # reads the bytes through whichever name it is given. Each path is a single
        # extra link, so nothing looks like a hardlink set inside the package.
        printf 'cp -al %s/. %%{buildroot}/\n' "$ROOT"
        printf '\n'
        printf '%%files\n'
        printf '/opt/chudware\n'
        printf '%%{_bindir}/chudware\n'
        printf '%%{_datadir}/applications/chudware.desktop\n'
        printf '%%{_datadir}/icons/hicolor/*/apps/chudware.png\n'
        printf '%%{_docdir}/chudware/copyright\n'
        printf '\n'
        printf '%%post\n'
        printf 'if command -v update-desktop-database >/dev/null 2>&1; then\n'
        printf '    update-desktop-database -q %%{_datadir}/applications || :\n'
        printf 'fi\n'
        printf 'if command -v gtk-update-icon-cache >/dev/null 2>&1; then\n'
        printf '    gtk-update-icon-cache -q -f -t %%{_datadir}/icons/hicolor || :\n'
        printf 'fi\n'
        printf '\n'
        printf '%%postun\n'
        printf 'if [ "$1" = 0 ]; then\n'
        printf '    if command -v update-desktop-database >/dev/null 2>&1; then\n'
        printf '        update-desktop-database -q %%{_datadir}/applications || :\n'
        printf '    fi\n'
        printf '    if command -v gtk-update-icon-cache >/dev/null 2>&1; then\n'
        printf '        gtk-update-icon-cache -q -f -t %%{_datadir}/icons/hicolor || :\n'
        printf '    fi\n'
        printf 'fi\n'
    } > "$spec"

    rpmbuild -bb "$spec" \
        --define "_topdir $top" \
        --define "_rpmdir $top/RPMS" \
        --define "_rpmfilename %{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}.rpm" \
        > "$WORK/rpmbuild.log" 2>&1 || {
            echo "error: rpmbuild failed" >&2
            tail -n 40 "$WORK/rpmbuild.log" >&2
            exit 1
        }

    built="$top/RPMS/$name"
    [ -f "$built" ] || {
        echo "error: rpmbuild did not produce $name (see $WORK/rpmbuild.log)" >&2
        find "$top/RPMS" -name '*.rpm' >&2
        exit 1
    }
    RPM_OUT="$OUT/$name"
    mv "$built" "$RPM_OUT"
    chmod 0644 "$RPM_OUT"

    if command -v rpm >/dev/null 2>&1; then
        echo "==> rpm -qip $name"
        rpm -qip "$RPM_OUT"
    fi
}

# ------------------------------------------------------------------------- tar

build_tar() {
    local name
    name="$(canonical_name tar)"
    TAR_OUT="$OUT/$name"
    echo "==> building $name"
    # A sysroot tarball: members are ./opt/chudware/... and ./usr/..., so
    # `tar -C / --zstd -xf` installs exactly what the deb and the rpm install.
    build_payload_tar "$WORK/payload.tar"
    zstd "-$TAR_ZSTD_LEVEL" -T1 -q -f -o "$TAR_OUT" "$WORK/payload.tar"
    rm -f "$WORK/payload.tar"
    chmod 0644 "$TAR_OUT"
}

# ------------------------------------------------------------- payload integrity

verify_from_deb() {
    local extracted="$WORK/verify-deb"
    echo "==> re-verifying the signed payload manifest from inside $(basename "$DEB_OUT")"
    rm -rf "$extracted"
    mkdir -p "$extracted"
    "$PYTHON" - "$DEB_OUT" "$extracted/data.tar.xz" <<'PY'
import sys
from pathlib import Path
# Read data.tar.xz straight back out of the ar archive: no dpkg tooling involved, and it
# proves the member layout is what a reader sees rather than what the writer intended.
source, destination = Path(sys.argv[1]), Path(sys.argv[2])
order, found = [], None
with source.open("rb") as archive:
    if archive.read(8) != b"!<arch>\n":
        raise SystemExit("error: the deb is not an ar archive")
    while True:
        header = archive.read(60)
        if len(header) < 60:
            break
        name = header[0:16].decode("ascii").strip()
        size = int(header[48:58].decode("ascii").strip())
        order.append(name)
        start = archive.tell()
        if name == "data.tar.xz":
            with destination.open("wb") as writer:
                remaining = size
                while remaining:
                    chunk = archive.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise SystemExit("error: the deb is truncated")
                    writer.write(chunk)
                    remaining -= len(chunk)
            found = size
        archive.seek(start + size + (size % 2))
if order != ["debian-binary", "control.tar.gz", "data.tar.xz"]:
    raise SystemExit("error: unexpected ar member order: %s" % order)
if found is None:
    raise SystemExit("error: the deb has no data.tar.xz member")
PY
    tar -C "$extracted" -xf "$extracted/data.tar.xz" "./$PAYLOAD_PREFIX_LITERAL"
    pkgtool verify --root "$extracted/$PAYLOAD_PREFIX_LITERAL" --repo "$REPO"
    rm -rf "$extracted"
}

case "$FORMAT" in
    deb) build_deb ;;
    rpm) build_rpm ;;
    tar) build_tar ;;
    all) build_deb; build_rpm; build_tar ;;
esac

if [ "$VERIFY" = 1 ] && [ -n "$DEB_OUT" ]; then
    verify_from_deb
fi

echo "==> built:"
for artifact in "$DEB_OUT" "$RPM_OUT" "$TAR_OUT"; do
    if [ -n "$artifact" ]; then ls -l "$artifact"; fi
done
