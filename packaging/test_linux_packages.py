"""Tests for packaging/linux/build_linux_packages.sh.

Everything here is hermetic: the AppDir fixture is built from synthesised ELF objects
rather than the host's binaries, so DT_NEEDED, the glibc symbol-version floor and the
bundled-versus-host split are all controlled inputs and the assertions do not change
with whatever is installed on the machine running the suite. No network.

What is actually pinned:
  * the outer `ar` member order and the deb control/md5sums/maintainer-script contents;
  * filename and version-field sanitisation for a release AND a prerelease, cross-checked
    against packaging/artifact_names.py, so the script can never hand-format a name;
  * rpm metadata (queried through rpm, skipped when rpmbuild is absent);
  * byte-for-byte determinism across two builds of the same AppDir;
  * payload preservation: the signed manifest still verifies against the tree unpacked
    out of the built package.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging/linux/build_linux_packages.sh"
sys.path.insert(0, str(REPO))

import artifact_names  # noqa: E402  (packaging/ is this test's import root)
from services.installer.payload_manifest import (  # noqa: E402
    build_manifest,
    verify_manifest_payload,
    write_manifest,
)

PRERELEASE = "0.1.0-beta.21"
RELEASE = "0.1.1"
GLIBC_FLOOR = "2.38"

# One host-provided soname (must appear in Depends) and one bundled soname (must not).
HOST_SONAME = "libc.so.6"
BUNDLED_SONAME = "libchudtest.so.1"


# --------------------------------------------------------------------- synthetic ELF


def _elf64_dynamic_object(soname: str, needed: list[str], glibc_versions: list[str]) -> bytes:
    """A minimal valid ELF64 ET_DYN object carrying exactly the given dynamic info.

    Only the parts build_linux_packages.sh reads are real: the program headers, a PT_LOAD
    that maps the file 1:1 at vaddr 0, PT_DYNAMIC, the string table, and a .gnu.version_r
    block naming the glibc symbol versions. There is no code — nothing executes it.
    """
    strings = bytearray(b"\0")
    offsets: dict[str, int] = {}

    def intern(value: str) -> int:
        if value not in offsets:
            offsets[value] = len(strings)
            strings.extend(value.encode("ascii") + b"\0")
        return offsets[value]

    soname_index = intern(soname)
    needed_indexes = [intern(name) for name in needed]
    version_owner = intern(needed[0] if needed else "libc.so.6")
    version_indexes = [intern(name) for name in glibc_versions]

    dynstr_offset = 176  # 64-byte header + two 56-byte program headers, rounded up
    dynstr = bytes(strings)
    verneed_offset = dynstr_offset + len(dynstr)
    verneed_offset += -verneed_offset % 8

    verneed = bytearray()
    if version_indexes:
        verneed += (1).to_bytes(2, "little")                      # vn_version
        verneed += len(version_indexes).to_bytes(2, "little")     # vn_cnt
        verneed += version_owner.to_bytes(4, "little")            # vn_file
        verneed += (16).to_bytes(4, "little")                     # vn_aux
        verneed += (0).to_bytes(4, "little")                      # vn_next (only entry)
        for position, index in enumerate(version_indexes):
            last = position == len(version_indexes) - 1
            verneed += (0).to_bytes(4, "little")                  # vna_hash
            verneed += (0).to_bytes(2, "little")                  # vna_flags
            verneed += (position + 2).to_bytes(2, "little")       # vna_other
            verneed += index.to_bytes(4, "little")                # vna_name
            verneed += (0 if last else 16).to_bytes(4, "little")  # vna_next

    dynamic_offset = verneed_offset + len(verneed)
    dynamic_offset += -dynamic_offset % 8

    entries: list[tuple[int, int]] = [(14, soname_index)]              # DT_SONAME
    entries += [(1, index) for index in needed_indexes]                # DT_NEEDED
    entries += [(5, dynstr_offset), (10, len(dynstr))]                 # DT_STRTAB/DT_STRSZ
    if verneed:
        entries += [(0x6FFFFFFE, verneed_offset), (0x6FFFFFFF, 1)]     # DT_VERNEED(NUM)
    entries.append((0, 0))                                             # DT_NULL
    dynamic = b"".join(
        tag.to_bytes(8, "little") + value.to_bytes(8, "little") for tag, value in entries
    )
    total = dynamic_offset + len(dynamic)

    def phdr(kind: int, flags: int, offset: int, size: int, align: int) -> bytes:
        return (
            kind.to_bytes(4, "little")
            + flags.to_bytes(4, "little")
            + offset.to_bytes(8, "little")   # p_offset
            + offset.to_bytes(8, "little")   # p_vaddr  (identity mapping)
            + offset.to_bytes(8, "little")   # p_paddr
            + size.to_bytes(8, "little")     # p_filesz
            + size.to_bytes(8, "little")     # p_memsz
            + align.to_bytes(8, "little")
        )

    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2   # ELFCLASS64
    header[5] = 1   # ELFDATA2LSB
    header[6] = 1   # EV_CURRENT
    header[16:18] = (3).to_bytes(2, "little")      # ET_DYN
    header[18:20] = (0x3E).to_bytes(2, "little")   # EM_X86_64
    header[20:24] = (1).to_bytes(4, "little")
    header[32:40] = (64).to_bytes(8, "little")     # e_phoff
    header[52:54] = (64).to_bytes(2, "little")     # e_ehsize
    header[54:56] = (56).to_bytes(2, "little")     # e_phentsize
    header[56:58] = (2).to_bytes(2, "little")      # e_phnum
    header[58:60] = (64).to_bytes(2, "little")     # e_shentsize

    image = bytearray(total)
    image[0:64] = header
    image[64:120] = phdr(1, 5, 0, total, 0x1000)                          # PT_LOAD
    image[120:176] = phdr(2, 6, dynamic_offset, len(dynamic), 8)          # PT_DYNAMIC
    image[dynstr_offset:dynstr_offset + len(dynstr)] = dynstr
    if verneed:
        image[verneed_offset:verneed_offset + len(verneed)] = verneed
    image[dynamic_offset:dynamic_offset + len(dynamic)] = dynamic
    return bytes(image)


# -------------------------------------------------------------------------- fixtures


def _make_appdir(root: Path, version: str) -> Path:
    """A minimal AppDir with the shape build_appimage.sh produces."""
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/lib").mkdir(parents=True)

    binary = _elf64_dynamic_object(
        "chudware", [HOST_SONAME, BUNDLED_SONAME], [f"GLIBC_{GLIBC_FLOOR}", "GLIBC_2.17"]
    )
    (root / "usr/bin/chudware").write_bytes(binary)
    os.chmod(root / "usr/bin/chudware", 0o755)
    (root / "usr/bin/chudware-hostd").write_bytes(binary)
    os.chmod(root / "usr/bin/chudware-hostd", 0o755)
    # Bundled beside the engine: its soname must be subtracted from the external set.
    library = _elf64_dynamic_object(BUNDLED_SONAME, [HOST_SONAME], ["GLIBC_2.17"])
    (root / f"usr/lib/{BUNDLED_SONAME}").write_bytes(library)
    os.chmod(root / f"usr/lib/{BUNDLED_SONAME}", 0o755)

    (root / "AppRun").write_text(
        '#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
        'export LD_LIBRARY_PATH="$HERE/usr/lib:${LD_LIBRARY_PATH:-}"\n'
        'exec "$HERE/usr/bin/chudware" "$@"\n',
        encoding="utf-8",
    )
    os.chmod(root / "AppRun", 0o755)

    # Comments and X-AppImage-Version are what the packager has to strip out.
    (root / "chudware.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Chudware\n"
        "Exec=chudware %F\n"
        "Icon=chudware\n"
        "Categories=Graphics;Engineering;\n"
        "Terminal=false\n"
        "StartupWMClass=chudware\n"
        f"X-AppImage-Version={version}\n"
        "# developer note that must not ship\n",
        encoding="utf-8",
    )
    os.chmod(root / "chudware.desktop", 0o644)
    for name in ("chudware.png", ".DirIcon"):
        (root / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
        os.chmod(root / name, 0o644)
    (root / "README").write_text("Chudware AppImage\n", encoding="utf-8")
    os.chmod(root / "README", 0o644)

    _, canonical, _ = build_manifest(
        root, version=version, platform="linux", arch="x86_64", key_id="release-ci-v1"
    )
    write_manifest(root, canonical)
    verify_manifest_payload(root, canonical)
    # build_appimage.sh's finalize step chmods the manifest pair to 0644; the packager
    # copies modes verbatim, so the fixture has to be realistic about it.
    os.chmod(root / "payload-manifest.v1.json", 0o644)
    (root / "payload-manifest.v1.sig").write_text("A" * 88 + "\n", encoding="utf-8")
    os.chmod(root / "payload-manifest.v1.sig", 0o644)
    return root


def _build(appdir: Path, out: Path, fmt: str, *extra: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--appdir", str(appdir), "--out", str(out), "--format", fmt, *extra],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"build failed:\n{result.stdout}\n{result.stderr}"
    return out


@pytest.fixture(scope="module")
def appdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_appdir(tmp_path_factory.mktemp("appdir"), PRERELEASE)


@pytest.fixture(scope="module")
def built(appdir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(appdir, tmp_path_factory.mktemp("out") / "packages", "all")


@pytest.fixture(scope="module")
def deb(built: Path) -> Path:
    return built / artifact_names.canonical_artifact(PRERELEASE, "linux", "x86_64", "deb")


# ------------------------------------------------------------------------- ar / deb


def _ar_members(path: Path) -> list[tuple[str, bytes]]:
    raw = path.read_bytes()
    assert raw[:8] == b"!<arch>\n", "a .deb is an ar archive"
    members: list[tuple[str, bytes]] = []
    offset = 8
    while offset + 60 <= len(raw):
        header = raw[offset:offset + 60]
        assert header[58:60] == b"`\n", "ar header magic"
        name = header[0:16].decode("ascii").rstrip()
        size = int(header[48:58].decode("ascii").strip())
        start = offset + 60
        members.append((name, raw[start:start + size]))
        offset = start + size + (size % 2)
    return members


def _control_files(payload: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        return {member.name.lstrip("./"): member for member in archive.getmembers()}


def _control_text(payload: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        extracted = archive.extractfile(f"./{name}")
        assert extracted is not None
        return extracted.read().decode("utf-8")


def _control_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    return fields


def test_deb_ar_member_order_is_exactly_what_dpkg_requires(deb: Path) -> None:
    members = _ar_members(deb)
    assert [name for name, _ in members] == ["debian-binary", "control.tar.gz", "data.tar.xz"]
    assert members[0][1] == b"2.0\n"


def test_deb_ar_headers_carry_no_build_host_state(deb: Path) -> None:
    raw = deb.read_bytes()
    offset = 8
    while offset + 60 <= len(raw):
        header = raw[offset:offset + 60]
        assert header[16:28].decode("ascii").strip() == "0", "mtime must be zeroed"
        assert header[28:34].decode("ascii").strip() == "0", "uid must be zeroed"
        assert header[34:40].decode("ascii").strip() == "0", "gid must be zeroed"
        assert header[40:48].decode("ascii").strip() == "100644"
        size = int(header[48:58].decode("ascii").strip())
        offset = offset + 60 + size + (size % 2)


def test_deb_control_fields(deb: Path) -> None:
    control = dict(_ar_members(deb))["control.tar.gz"]
    fields = _control_fields(_control_text(control, "control"))
    assert fields["Package"] == "chudware"
    assert fields["Architecture"] == "amd64"
    assert fields["Version"] == artifact_names.deb_version(PRERELEASE) == "0.1.0~beta.21"
    assert int(fields["Installed-Size"]) > 0
    assert "@" in fields["Maintainer"]

    depends = [item.strip() for item in fields["Depends"].split(",")]
    assert f"libc6 (>= {GLIBC_FLOOR})" in depends, "the glibc floor must be a real dependency"
    assert not any(BUNDLED_SONAME in item for item in depends), (
        "a library bundled inside /opt/chudware must never become a system dependency"
    )
    assert all(item for item in depends), "no empty dependency entries"


def test_deb_md5sums_covers_every_regular_file(deb: Path) -> None:
    members = dict(_ar_members(deb))
    listed: dict[str, str] = {}
    for line in _control_text(members["control.tar.gz"], "md5sums").splitlines():
        digest, _, name = line.partition("  ")
        listed[name] = digest

    with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"]), mode="r:xz") as data:
        regular = {}
        for member in data.getmembers():
            if member.isfile():
                extracted = data.extractfile(member)
                assert extracted is not None
                regular[member.name.lstrip("./")] = hashlib.md5(extracted.read()).hexdigest()
    assert listed == regular


def test_deb_maintainer_scripts_refresh_the_desktop_caches(deb: Path) -> None:
    control = dict(_ar_members(deb))["control.tar.gz"]
    infos = _control_files(control)
    for name in ("postinst", "postrm"):
        assert name in infos, f"{name} must ship"
        assert infos[name].mode == 0o755, f"{name} must be executable"
        body = _control_text(control, name)
        assert "update-desktop-database" in body
        assert "gtk-update-icon-cache" in body
        assert "command -v" in body, "both tools are optional and must be probed first"


def test_deb_data_layout(deb: Path) -> None:
    members = dict(_ar_members(deb))
    with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"]), mode="r:xz") as data:
        names = {member.name.lstrip("./") for member in data.getmembers()}
        by_name = {member.name.lstrip("./"): member for member in data.getmembers()}

    assert "opt/chudware/AppRun" in names
    assert "opt/chudware/payload-manifest.v1.json" in names
    assert by_name["usr/bin/chudware"].issym()
    assert by_name["usr/bin/chudware"].linkname == "../../opt/chudware/AppRun"
    assert "usr/share/applications/chudware.desktop" in names
    assert "usr/share/doc/chudware/copyright" in names
    for size in (64, 128, 256):
        assert f"usr/share/icons/hicolor/{size}x{size}/apps/chudware.png" in names
    # /opt/chudware is the manifest's territory: nothing may be added there.
    assert not any(
        name.startswith("opt/chudware/") and name.endswith(".desktop") and "/" not in name[13:]
        for name in names - {"opt/chudware/chudware.desktop"}
    )
    assert all(member.uid == 0 and member.gid == 0 for member in by_name.values())


def test_shipped_desktop_entry_drops_appimage_and_developer_lines(deb: Path) -> None:
    members = dict(_ar_members(deb))
    with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"]), mode="r:xz") as data:
        extracted = data.extractfile("./usr/share/applications/chudware.desktop")
        assert extracted is not None
        entry = extracted.read().decode("utf-8")
    assert "X-AppImage-Version" not in entry
    assert "#" not in entry, "developer comments must not reach an installed system"
    assert f"X-Chudware-Version={PRERELEASE}" in entry
    assert "StartupWMClass=chudware" in entry, "the taskbar icon binding must survive"


# ----------------------------------------------------------------------- filenames


@pytest.mark.parametrize(
    ("version", "fmt", "expected"),
    [
        (RELEASE, "deb", "chudware_0.1.1_amd64.deb"),
        (RELEASE, "rpm", "chudware-0.1.1-1.x86_64.rpm"),
        (RELEASE, "tar", "Chudware-0.1.1-linux-x86_64.tar.zst"),
        (PRERELEASE, "deb", "chudware_0.1.0~beta.21_amd64.deb"),
        (PRERELEASE, "rpm", "chudware-0.1.0-0.beta.21.x86_64.rpm"),
        (PRERELEASE, "tar", "Chudware-0.1.0-beta.21-linux-x86_64.tar.zst"),
    ],
)
def test_filename_sanitisation_matches_artifact_names(version: str, fmt: str, expected: str) -> None:
    """Release files are looked up by this exact name, so the script may not
    hand-format it and the two implementations may not drift apart."""
    printed = subprocess.run(
        ["bash", str(SCRIPT), "--print-name", "--version", version, "--format", fmt],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert printed == expected
    assert printed == artifact_names.canonical_artifact(version, "linux", "x86_64", fmt)


def test_prerelease_version_fields_are_ordered_before_the_release() -> None:
    assert artifact_names.deb_version(PRERELEASE) == "0.1.0~beta.21"
    assert artifact_names.rpm_version_release(PRERELEASE) == ("0.1.0", "0.beta.21")
    assert artifact_names.rpm_version_release(RELEASE) == ("0.1.1", "1")


def test_built_artifacts_use_the_canonical_names(built: Path) -> None:
    for fmt in ("deb", "rpm", "tar"):
        name = artifact_names.canonical_artifact(PRERELEASE, "linux", "x86_64", fmt)
        assert (built / name).is_file(), f"{name} was not produced"


# ----------------------------------------------------------------------------- rpm

rpm_tooling = pytest.mark.skipif(
    shutil.which("rpmbuild") is None or shutil.which("rpm") is None,
    reason="rpmbuild/rpm are not installed on this host",
)


def _rpm_query(package: Path, *args: str) -> str:
    return subprocess.run(
        ["rpm", "-qp", "--nosignature", *args, str(package)],
        capture_output=True, text=True, check=True,
    ).stdout


@rpm_tooling
def test_rpm_metadata(built: Path, appdir: Path) -> None:
    package = built / artifact_names.canonical_artifact(PRERELEASE, "linux", "x86_64", "rpm")
    version, release = artifact_names.rpm_version_release(PRERELEASE)
    fields = _rpm_query(
        package, "--qf",
        "%{NAME}\n%{VERSION}\n%{RELEASE}\n%{ARCH}\n%{BUILDHOST}\n%{BUILDTIME}\n%{LICENSE}\n",
    ).splitlines()
    name, got_version, got_release, arch, buildhost, buildtime, license_name = fields[:7]
    assert (name, got_version, got_release, arch) == ("chudware", version, release, "x86_64")
    assert license_name == "MIT"
    assert buildhost == "chudware-release", "the build host must not leak into the package"

    created = json.loads((appdir / "payload-manifest.v1.json").read_text(encoding="utf-8"))
    assert created["version"] == PRERELEASE
    assert int(buildtime) == _epoch_of(created["created_at"]), (
        "the rpm build time must come from SOURCE_DATE_EPOCH, not the clock"
    )


def _epoch_of(created_at: str) -> int:
    from datetime import datetime, timezone

    return int(
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


@rpm_tooling
def test_rpm_requires_sonames_and_provides_nothing_bundled(built: Path) -> None:
    package = built / artifact_names.canonical_artifact(PRERELEASE, "linux", "x86_64", "rpm")
    requires = set(_rpm_query(package, "--requires").split())
    assert f"{HOST_SONAME}()(64bit)" in requires, "host libraries must be real requirements"
    assert f"{BUNDLED_SONAME}()(64bit)" not in requires

    provides = _rpm_query(package, "--provides").splitlines()
    assert any(line.startswith("chudware = ") for line in provides)
    assert not any(BUNDLED_SONAME in line for line in provides), (
        "a private copy of a library must not be offered to the rest of the system"
    )


@rpm_tooling
def test_rpm_file_list(built: Path) -> None:
    package = built / artifact_names.canonical_artifact(PRERELEASE, "linux", "x86_64", "rpm")
    files = set(_rpm_query(package, "--list").splitlines())
    assert "/opt/chudware/AppRun" in files
    assert "/opt/chudware/payload-manifest.v1.json" in files
    assert "/usr/bin/chudware" in files
    assert "/usr/share/applications/chudware.desktop" in files
    assert "/usr/share/doc/chudware/copyright" in files
    for size in (64, 128, 256):
        assert f"/usr/share/icons/hicolor/{size}x{size}/apps/chudware.png" in files


# -------------------------------------------------------------------- determinism


def test_two_builds_from_one_appdir_are_byte_identical(
    appdir: Path, built: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The whole point of pinning mtimes, sort order, ownership and compressor threads.

    A release build that produced different bytes from identical inputs would make the
    published sha256 unverifiable by anyone rebuilding it.
    """
    second = _build(appdir, tmp_path_factory.mktemp("out2") / "packages", "all")
    formats = ["deb", "tar"] + (["rpm"] if shutil.which("rpmbuild") else [])
    for fmt in formats:
        name = artifact_names.canonical_artifact(PRERELEASE, "linux", "x86_64", fmt)
        first_bytes = (built / name).read_bytes()
        second_bytes = (second / name).read_bytes()
        assert first_bytes == second_bytes, f"{fmt} is not reproducible"
        assert len(first_bytes) > 0


def test_source_date_epoch_defaults_to_the_manifest(appdir: Path, deb: Path) -> None:
    created = json.loads((appdir / "payload-manifest.v1.json").read_text(encoding="utf-8"))
    expected = _epoch_of(created["created_at"])
    members = dict(_ar_members(deb))
    with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"]), mode="r:xz") as data:
        stamps = {member.mtime for member in data.getmembers()}
    assert stamps == {expected}, "every entry's mtime is pinned to SOURCE_DATE_EPOCH"


# ------------------------------------------------------------- payload preservation


def test_payload_bytes_and_modes_survive_packaging(appdir: Path, deb: Path, tmp_path: Path) -> None:
    """The signed manifest must still verify against the tree taken out of the package.

    This is the invariant the whole layout exists to protect: if packaging rewrote one
    byte or one mode bit, an installed Chudware would fail its own startup integrity
    check, and the auto-updater would reject the release.
    """
    members = dict(_ar_members(deb))
    extracted = tmp_path / "root"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"]), mode="r:xz") as data:
        data.extractall(extracted, filter="tar")

    payload = extracted / "opt/chudware"
    manifest, _ = verify_manifest_payload(
        payload, (payload / "payload-manifest.v1.json").read_bytes()
    )
    assert manifest["version"] == PRERELEASE

    # Belt and braces: compare against the AppDir directly, not just the manifest.
    for row in manifest["files"]:
        relative = str(row["path"])
        source, target = appdir / relative, payload / relative
        assert target.read_bytes() == source.read_bytes(), f"{relative} changed"
        assert target.stat().st_mode & 0o7777 == source.stat().st_mode & 0o7777


def test_tarball_is_the_same_sysroot_tree(built: Path, deb: Path) -> None:
    """`tar -C / --zstd -xf` must install exactly what the deb installs."""
    tarball = built / artifact_names.canonical_artifact(PRERELEASE, "linux", "x86_64", "tar")
    uncompressed = subprocess.check_output(["zstd", "-dc", str(tarball)])
    with tarfile.open(fileobj=io.BytesIO(uncompressed), mode="r:") as archive:
        from_tar = {
            (member.name.lstrip("./"), member.mode, member.linkname)
            for member in archive.getmembers()
            if member.name not in (".", "./")
        }
    members = dict(_ar_members(deb))
    with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"]), mode="r:xz") as data:
        from_deb = {
            (member.name.lstrip("./"), member.mode, member.linkname)
            for member in data.getmembers()
            if member.name not in (".", "./")
        }
    assert from_tar == from_deb
