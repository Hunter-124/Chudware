"""Canonical Chudware release artifact filenames — the single source of truth.

Deliberately stdlib-only so a build runner can call it with a bare ``python3``.
Every producer and consumer of a release file name asks this module instead of
formatting one by hand: the Linux package builder
(packaging/linux/build_linux_packages.sh), the GitHub Releases update-manifest
writer (packaging/github_release_manifest.py), and the updater's own expectation
in host/src/update/updater.cpp, which this module must keep matching.

Debian and RPM both refuse '-' in a version field, so a prerelease has to be
re-spelled — and the two systems disagree about how:

    0.1.0-beta.21  ->  deb  Version 0.1.0~beta.21
                   ->  rpm  Version 0.1.0, Release 0.beta.21

Both spellings sort the prerelease BEFORE the final 0.1.0, which is the entire
point: somebody running the beta must be offered 0.1.0 as an upgrade rather than
told they already have something newer. The rpm form puts the prerelease in
Release instead of writing '~' into Version because rpmvercmp only understands
'~' from rpm 4.10 onward, while ``0.<prerelease>`` is the Fedora packaging
guideline and orders correctly everywhere.

AppImage and tarball names keep the RAW version, prerelease suffix and all:
nothing parses them as a package version, and rewriting them would make the
filename disagree with the release version it is published under.
"""

from __future__ import annotations

import argparse
import re
import sys

# Package formats. ``default`` is the platform's primary artifact: the AppImage on
# Linux, the Inno Setup installer on Windows and the zipped app bundle on macOS.
FORMATS: tuple[str, ...] = ("default", "deb", "rpm", "tar", "msi")

# A filename is derived from this string, so validating it here is what stops a
# hostile or malformed version from naming a file something else entirely.
_VERSION_RE = re.compile(
    r"(?P<triple>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
)

_FIELDS = ("filename", "deb-version", "rpm-version", "rpm-release")


def split_version(version: str) -> tuple[str, str]:
    """``("0.1.0", "beta.21")`` for ``0.1.0-beta.21``; prerelease may be ""."""
    match = _VERSION_RE.fullmatch(version or "")
    if match is None:
        raise ValueError(
            "version must be major.minor.patch with an optional -prerelease"
        )
    return match.group("triple"), match.group("prerelease") or ""


def deb_version(version: str) -> str:
    """The Debian ``Version:`` field. Every '-' becomes '~'."""
    triple, prerelease = split_version(version)
    if not prerelease:
        return triple
    # A prerelease may itself contain '-' (0.1.0-beta-21); each one has to
    # become '~' or dpkg rejects the field outright.
    return f"{triple}~{prerelease.replace('-', '~')}"


def rpm_version_release(version: str) -> tuple[str, str]:
    """The RPM ``Version:`` and ``Release:`` fields."""
    triple, prerelease = split_version(version)
    if not prerelease:
        return triple, "1"
    return triple, f"0.{prerelease}"


def canonical_artifact(
    version: str, platform: str, arch: str, fmt: str = "default"
) -> str:
    """The one filename a (version, platform, arch, format) may be published as.

    Raises ``ValueError`` for an unknown format, malformed version, unsupported
    platform/architecture pair, or package format requested for the wrong platform.
    """
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    split_version(version)
    if platform == "linux" and arch == "x86_64":
        if fmt == "msi":
            raise ValueError("msi is a Windows package format")
        if fmt == "deb":
            return f"chudware_{deb_version(version)}_amd64.deb"
        if fmt == "rpm":
            rpm_version, rpm_release = rpm_version_release(version)
            return f"chudware-{rpm_version}-{rpm_release}.x86_64.rpm"
        if fmt == "tar":
            return f"Chudware-{version}-linux-x86_64.tar.zst"
        return f"Chudware-{version}-linux-x86_64.AppImage"
    if platform == "windows" and arch == "x86_64":
        if fmt == "msi":
            return f"Chudware-{version}-x86_64.msi"
        if fmt != "default":
            raise ValueError(
                f"windows publishes only default and msi formats; {fmt} is not supported"
            )
        return f"Chudware-Setup-{version}-x86_64.exe"
    if platform == "macos" and arch in {"x86_64", "arm64"}:
        _require_default(platform, fmt)
        return f"Chudware-{version}-macos-{arch}.zip"
    raise ValueError("unsupported platform and architecture combination")


def _require_default(platform: str, fmt: str) -> None:
    if fmt != "default":
        raise ValueError(
            f"{platform} publishes only the default format; {fmt} is not supported"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="artifact_names.py",
        description=(
            "Print the canonical Chudware artifact filename, or the packaging "
            "version field, for one (version, platform, arch, format)."
        ),
    )
    parser.add_argument("--version", required=True, help="e.g. 0.1.1 or 0.1.0-beta.21")
    parser.add_argument("--platform", default="linux", help="default: linux")
    parser.add_argument("--arch", default="x86_64", help="default: x86_64")
    parser.add_argument(
        "--format", dest="fmt", default="default", choices=FORMATS,
        help="default: default (AppImage on Linux)",
    )
    parser.add_argument(
        "--field", default="filename", choices=_FIELDS,
        help="what to print; default: filename",
    )
    args = parser.parse_args(argv)
    try:
        if args.field == "filename":
            value = canonical_artifact(args.version, args.platform, args.arch, args.fmt)
        elif args.field == "deb-version":
            value = deb_version(args.version)
        else:
            rpm_version, rpm_release = rpm_version_release(args.version)
            value = rpm_version if args.field == "rpm-version" else rpm_release
    except ValueError as exc:
        print(f"artifact_names: {exc}", file=sys.stderr)
        return 2
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
