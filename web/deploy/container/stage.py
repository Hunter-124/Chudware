"""Build-time staging only; never consult the host's user data or installed runtime."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def public_requirements(source: Path, destination: Path) -> None:
    lines = source.read_text().splitlines(keepends=True)
    sdk = (
        "chudcode-rpc @ https://github.com/Hunter-124/Chudware/releases/download/"
        "chudcode-runtime-v0.4.4/chudcode-source-8f70d216879be0cfe6bfeb5520f2370521224330.tar.gz"
        "#sha256=0951e7be9e32eb83019941c432cb38c02addd086ff35c112aac6e71b1ced8997"
        "&subdirectory=python/chudcode-rpc"
    )
    omitted = [index for index, line in enumerate(lines)
               if re.sub(r"(^|\s+)#.*$", "", line).strip() == sdk]
    if len(omitted) != 1:
        raise ValueError("expected exactly the optional hash-pinned chudcode-rpc archive requirement")
    # Online Copilot already refuses credential-bearing execution in browser cells.
    # Its SDK imports lazily; neither the SDK nor the native agent runtime is needed
    # for native CAD or general Python workers. Omit ONLY this exact declaration.
    del lines[omitted[0]]
    destination.write_text("".join(lines))


def copy_tree(source: Path, destination: Path) -> None:
    # Match generated build directories, not runtime sources such as buildgraph.
    shutil.copytree(source, destination, symlinks=False, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "test_*", "tests",
                                                  "build", "build-*", "dist"))


def dependency_packages(seeds: list[Path], embedded: Path, fetched: Path | None = None) -> set[str]:
    packages: set[str] = set()
    visited: set[Path] = set()
    environment = {**os.environ, "LC_ALL": "C"}
    for seed in seeds:
        # Standalone ldd loses the parent extension's inherited RPATH when a
        # wheel-private child DSO is inspected directly. Restore only this seed's
        # colocated libraries, never a shared search path across installed wheels.
        library_dirs = [str(embedded)]
        if seed.is_relative_to(embedded) and seed.parent != embedded:
            library_dirs.insert(0, str(seed.parent))
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(library_dirs)
        result = subprocess.run(["ldd", str(seed)], env=environment, text=True,
                                capture_output=True, check=True)
        if "not found" in result.stdout:
            raise RuntimeError(f"incomplete native dependency closure for {seed}:\n{result.stdout}")
        for line in result.stdout.splitlines():
            match = re.search(r"(?:=>\s+)?(/[^\s]+)\s+\(", line)
            if not match:
                continue  # linux-vdso has no filesystem object.
            dependency = Path(match.group(1)).resolve()
            if dependency.is_relative_to(embedded) or dependency in visited:
                continue
            visited.add(dependency)
            owner = subprocess.run(["rpm", "-qf", "--qf", "%{NAME}-%{EVR}.%{ARCH}\n", str(dependency)],
                                   text=True, capture_output=True)
            if owner.returncode == 0:
                packages.update(owner.stdout.splitlines())
            elif fetched is not None and dependency.is_relative_to(fetched):
                # Preserve the loader's requested name for fetched, non-RPM SDKs.
                soname = line.strip().split()[0]
                if not re.fullmatch(r"[A-Za-z0-9_.+-]+", soname):
                    raise ValueError(f"invalid dependency SONAME: {line}")
                shutil.copy2(dependency, embedded / soname)
            else:
                raise RuntimeError(f"unowned native dependency: {dependency}")
    if not packages:
        raise RuntimeError("native package closure is empty")
    return packages


def python_packages(venv: Path, destination: Path) -> None:
    # Wheels bundle many libraries via RPATH, but not every OS dependency.
    # Include every installed extension, not just whatever startup imports.
    seeds = [venv / "bin/python"]
    seeds.extend(path for path in venv.rglob("*.so*") if path.is_file())
    packages = dependency_packages(seeds, venv)
    destination.write_text("\n".join(sorted(packages)) + "\n")


def copy_notices(bases: list[Path], notices: Path) -> None:
    for base in bases:
        if not base.is_dir():
            continue
        for source in base.rglob("*"):
            if (source.is_file() and ".git" not in source.parts
                    and re.match(r"(?i)^(licen[cs]e|copying|notice|copyright)([._-].*)?$", source.name)):
                destination = notices / base.name / source.relative_to(base)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)


def stage_payload(root: Path, build: Path, output: Path) -> None:
    payload = output / "chudware"
    binary = payload / "bin"
    binary.mkdir(parents=True)
    # General workers use the ordinary installed native resolver without adding
    # loader overrides to the actor's sanitized environment. Both views point at
    # the same immutable payload; no host paths or duplicate engine copies.
    (payload / "dist").mkdir()
    (payload / "dist/Chudware").symlink_to("../bin", target_is_directory=True)
    # CMake stages dynamic lib3mf, ONNX Runtime and the advisor models beside the
    # engine. Keep their exact SONAMEs, including every versioned sibling.
    for name in ("chudware-hostd", "libchudware.so", "lib3mf.so", "polymesh"):
        source = build / "bin" / name
        if not source.is_file():
            raise FileNotFoundError(f"required full-engine artifact is missing: {source}")
        shutil.copy2(source, binary / name)
    for source in (build / "bin").glob("*.so*"):
        shutil.copy2(source.resolve(), binary / source.name)
    advisor = build / "bin/polymesh-advisor"
    for name in ("model.onnx", "normalization.json", "clamps.json", "ood.json"):
        if not (advisor / name).is_file():
            raise FileNotFoundError(f"required mesh advisor artifact is missing: {advisor / name}")
    copy_tree(advisor, binary / "polymesh-advisor")

    # Ask the distribution to install the COMPLETE owning RPMs for system native
    # dependencies, rather than copying bare .so files and losing dlopen plugins,
    # BLAS configuration, license texts or package/source identification. Include
    # the interpreter in this closure; both image stages use Fedora 44.
    seeds = [binary / "chudware-hostd", binary / "polymesh", Path("/usr/bin/python3.13")]
    seeds.extend(binary.glob("*.so*"))
    packages = dependency_packages(seeds, binary, root)
    (output / "runtime-packages.txt").write_text("\n".join(sorted(packages)) + "\n")

    for directory in ("services", "contracts", "assets"):
        copy_tree(root / directory, payload / directory)
    copy_tree(root / "core/data", payload / "core/data")
    copy_tree(root / "services/agent/prompts", payload / "agent/prompts")
    copy_tree(root / "web/gateway", payload / "web/gateway")
    copy_tree(root / "web/deploy/container", payload / "web/deploy/container")
    # Fixed launchers import their sibling modules. Do not replace actor bwrap or
    # egress isolation with an ordinary worker subprocess in the container path.
    for source in (root / "web/deploy/cell").iterdir():
        if source.is_file():
            shutil.copy2(source, binary / source.name)
            if source.name.startswith("chudware-"):
                (binary / source.name).chmod(0o755)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(root / name, payload / name)
    # Preserve notices from authenticated fetched sources and binary SDKs, in
    # addition to Fedora's installed /usr/share/licenses and Python dist-info.
    copy_notices([build / "authenticated-source", build / "authenticated-sdk",
                  build / "_deps", root / "third_party"], payload / "third-party-licenses")
    (payload / "container-runtime-v1").write_text("chudware-container-v1\n")
    (payload / "container-runtime-v1").chmod(0o444)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "requirements":
        public_requirements(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 4 and sys.argv[1] == "python-packages":
        python_packages(Path(sys.argv[2]).resolve(), Path(sys.argv[3]))
    elif len(sys.argv) == 4 and sys.argv[1] == "wasm-notices":
        copy_notices([Path(sys.argv[2]) / "_deps", Path("/emsdk/upstream/emscripten")],
                     Path(sys.argv[3]))
    elif len(sys.argv) == 5 and sys.argv[1] == "payload":
        stage_payload(*(Path(value).resolve() for value in sys.argv[2:]))
    else:
        raise SystemExit("usage: stage.py requirements SOURCE OUTPUT | python-packages VENV OUTPUT | payload SOURCE_ROOT BUILD_ROOT OUTPUT")
