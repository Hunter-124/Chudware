"""Consumer-visible offline provisioning checks, using only inert fixture wheels.

The pip smoke creates a temporary venv, serves a synthetic public candidate on
loopback, and never invokes a build backend or installs the real private SDK.
Run in a disposable network-disabled environment; loopback must remain available.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import zipfile

import pytest

from services.installer import provision
from services.installer.wheelhouse import (
    WheelhouseError, _packaging, install_command, pip_environment, target_key,
    verified_wheelhouse,
)


def fixture_wheel(directory, name="chudcode-rpc", version="0.3.1", dependencies=(),
                  module_value="approved fixture", tag="py3-none-any", requires_python=">=3.11",
                  extra_files=None):
    normalized = name.replace("-", "_")
    filename = f"{normalized}-{version}-{tag}.whl"
    info = f"{normalized}-{version}.dist-info"
    metadata = (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                f"Requires-Python: {requires_python}\n")
    metadata += "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
    members = {
        f"{normalized}/__init__.py": f"IDENTITY = {module_value!r}\n",
        f"{info}/METADATA": metadata,
        f"{info}/WHEEL": f"Wheel-Version: 1.0\nGenerator: inert-fixture\nRoot-Is-Purelib: true\nTag: {tag}\n",
    }
    members.update(extra_files or {})
    members[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in [*members, f"{info}/RECORD"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    with zipfile.ZipFile(path, "w") as archive:
        for member, contents in members.items():
            archive.writestr(member, contents)
    return {"filename": filename, "name": name, "version": version,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def reviewed(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("--no-index\n--only-binary=:all:\nchudcode-rpc==0.3.1\n")
    source = {"repository": "https://github.com/Hunter-124/chudcode.git", "repository_commit": "1" * 40}
    native = tmp_path / "CHUDCODE_ARTIFACT.lock"
    native.write_text(json.dumps(source))
    wheelhouse = tmp_path / "wheelhouse"
    vendored = {"chudcode_rpc/_vendor/inert_child.dist-info/INSTALLER": "reviewed vendored metadata\n"}
    private = fixture_wheel(wheelhouse, dependencies=("inert-dependency==1.0",),
                            extra_files=vendored)
    dependency = fixture_wheel(wheelhouse, "inert-dependency", "1.0")
    markers, _, _, _, _, _ = _packaging()
    environment = markers.default_environment()
    lock = tmp_path / "RUNTIME_WHEELS.lock"
    document = {"schema": "chudware-runtime-wheels-v1",
                "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
                "chudcode_source": source,
                "targets": {target_key(environment): {"wheels": [private, dependency]}}}
    lock.write_text(json.dumps(document))
    return {"wheelhouse": wheelhouse, "requirements": requirements, "lock_path": lock,
            "chudcode_lock": native, "document": document, "environment": environment}


def verify(inputs):
    return verified_wheelhouse(**{key: inputs[key] for key in
                                 ("wheelhouse", "requirements", "lock_path", "chudcode_lock")})


def rewrite_lock(inputs):
    inputs["lock_path"].write_text(json.dumps(inputs["document"]))


def bind_provisioner(monkeypatch, inputs):
    monkeypatch.setattr(provision, "REQUIREMENTS", inputs["requirements"])
    monkeypatch.setattr(provision, "WHEEL_LOCK", inputs["lock_path"])
    monkeypatch.setattr(provision, "CHUDCODE_LOCK", inputs["chudcode_lock"])


@contextmanager
def synthetic_public_index(tmp_path):
    candidate = fixture_wheel(tmp_path / "public", module_value="public candidate")
    payload = (tmp_path / "public" / candidate["filename"]).read_bytes()
    requests = []
    control_requests = []
    control_token = secrets.token_hex(16)
    phase = {"name": "index-startup"}
    started = time.monotonic()

    class Handler(BaseHTTPRequestHandler):
        def parse_request(self):
            if not super().parse_request():
                return False
            contact = {
                "method": self.command, "path": self.path,
                "elapsed_s": round(time.monotonic() - started, 3), "phase": phase["name"],
                # Only inert request metadata; never log cookies or credentials.
                "headers": {name: self.headers[name] for name in
                            ("User-Agent", "Accept", "Content-Type") if name in self.headers},
            }
            if self.headers.get("X-Chudware-Test-Control") == control_token:
                control_requests.append(contact)
            else:
                requests.append(contact)
            return True

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            if self.path.endswith(".whl"):
                self.wfile.write(payload)
            else:
                self.wfile.write(f'<a href="/{candidate["filename"]}">public candidate</a>'.encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from urllib.request import build_opener, ProxyHandler, Request
        index = f"http://127.0.0.1:{server.server_port}/simple"
        # Attest the synthetic index using one uniquely tagged control request.
        # Keep every other contact, including any unexpected startup probes.
        control = Request(index, headers={"X-Chudware-Test-Control": control_token})
        with build_opener(ProxyHandler({})).open(control, timeout=5) as response:
            assert b"public candidate" in response.read()
        assert len(control_requests) == 1 and control_requests[0]["path"] == "/simple"
        phase["name"] = "provisioning"
        yield index, requests, phase
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/simple"), ("HEAD", "/")])
def test_synthetic_index_records_every_untagged_contact(tmp_path, method, path):
    from urllib.error import HTTPError
    from urllib.request import build_opener, ProxyHandler, Request

    with synthetic_public_index(tmp_path) as (index, requests, phase):
        assert requests == []
        request = Request(index.removesuffix("/simple") + path, method=method,
                          headers={"User-Agent": "public-contact-proof"})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                response.read()
        except HTTPError as exc:
            assert method == "HEAD" and exc.code == 501
        assert len(requests) == 1
        assert requests[0]["method"] == method
        assert requests[0]["path"] == path


@pytest.mark.parametrize("failure", ["absent", "tampered", "public-replacement"])
def test_private_candidate_rejected_before_any_interpreter_or_install(reviewed, tmp_path, monkeypatch, failure):
    bind_provisioner(monkeypatch, reviewed)
    wheel = reviewed["wheelhouse"] / reviewed["document"]["targets"][target_key(reviewed["environment"])]["wheels"][0]["filename"]
    if failure == "absent":
        wheel.unlink()
    elif failure == "tampered":
        with wheel.open("ab") as stream:
            stream.write(b"tampered")
    else:
        fixture_wheel(reviewed["wheelhouse"], dependencies=("inert-dependency==1.0",),
                      module_value="public candidate")
    untouched = tmp_path / "existing-venv" / "retained-user-file"
    untouched.parent.mkdir()
    untouched.write_text("leave existing environments alone")
    with synthetic_public_index(tmp_path) as (index, requests, phase):
        phase["name"] = "preflight-refusal"
        monkeypatch.setenv("PIP_INDEX_URL", index)
        def unexpected_process(*_args, **_kwargs):
            pytest.fail("invalid wheel inputs must fail before creating/executing a venv or pip")
        monkeypatch.setattr(provision.subprocess, "run", unexpected_process)
        with pytest.raises(WheelhouseError, match="approved wheel"):
            provision.ensure_runtime(sys._base_executable, reviewed["wheelhouse"], force=True,
                                     venv_dir=untouched.parent)
        assert untouched.read_text() == "leave existing environments alone"
        assert requests == []


def test_missing_trusted_lock_does_not_accept_wheelhouse_supplied_lock(reviewed, tmp_path, monkeypatch):
    bind_provisioner(monkeypatch, reviewed)
    (reviewed["wheelhouse"] / reviewed["lock_path"].name).write_bytes(reviewed["lock_path"].read_bytes())
    reviewed["lock_path"].unlink()
    monkeypatch.setattr(provision.subprocess, "run", lambda *_a, **_k: pytest.fail("no process expected"))
    assert provision.main(["--base-python", sys._base_executable, "--wheelhouse", str(reviewed["wheelhouse"]),
                           "--venv-dir", str(tmp_path / "venv")]) == 1
    assert not (tmp_path / "venv").exists()


@pytest.mark.parametrize("failure", ["missing-transitive", "changed-declarations", "source-mismatch",
                                      "unsupported-target", "metadata-mismatch", "unsupported-python",
                                      "duplicate-wheel", "direct-dependency", "unsupported-tags"])
def test_incomplete_or_mismatched_managed_inputs_are_rejected(reviewed, failure):
    target = reviewed["document"]["targets"][target_key(reviewed["environment"])]
    if failure == "missing-transitive":
        target["wheels"].pop()
    elif failure == "changed-declarations":
        reviewed["requirements"].write_text("chudcode-rpc==0.3.1\nnew-runtime-dependency==1.0\n")
    elif failure == "source-mismatch":
        reviewed["document"]["chudcode_source"]["repository_commit"] = "2" * 40
    elif failure == "unsupported-target":
        reviewed["document"]["targets"] = {}
    elif failure == "metadata-mismatch":
        entry = target["wheels"][0]
        path = reviewed["wheelhouse"] / entry["filename"]
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        for name in members:
            if name.endswith("/METADATA"):
                members[name] = members[name].replace(b"Name: chudcode-rpc", b"Name: other-package")
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif failure == "unsupported-python":
        target["wheels"][0] = fixture_wheel(reviewed["wheelhouse"], requires_python="<3.0")
    elif failure == "duplicate-wheel":
        target["wheels"].append(target["wheels"][0].copy())
    elif failure == "direct-dependency":
        target["wheels"][0] = fixture_wheel(reviewed["wheelhouse"], dependencies=("remote @ https://invalid.example/remote.whl",))
    else:
        target["wheels"][0] = fixture_wheel(reviewed["wheelhouse"], tag="cp27-cp27m-win32")
    rewrite_lock(reviewed)
    with pytest.raises(WheelhouseError):
        with verify(reviewed):
            pytest.fail("unreviewed/incomplete input was accepted")


def test_selected_extras_dependency_closure_is_required(reviewed):
    reviewed["requirements"].write_text("chudcode-rpc[feature]==0.3.1\n")
    reviewed["document"]["requirements_sha256"] = hashlib.sha256(reviewed["requirements"].read_bytes()).hexdigest()
    target = reviewed["document"]["targets"][target_key(reviewed["environment"])]
    target["wheels"][0] = fixture_wheel(reviewed["wheelhouse"], dependencies=(
        'absent-extra==1.0; extra == "feature"', 'absent-platform==1.0; python_version < "3.0"'))
    rewrite_lock(reviewed)
    with pytest.raises(WheelhouseError, match="absent-extra"):
        with verify(reviewed):
            pass


def test_verified_snapshot_is_independent_of_later_wheelhouse_changes(reviewed):
    with verify(reviewed) as identity:
        for wheel in reviewed["wheelhouse"].iterdir():
            wheel.write_bytes(b"replaced after verification")
        for line in identity["requirements"].read_text().splitlines():
            uri, expected = line.split(" --hash=sha256:")
            from urllib.parse import unquote, urlsplit
            snapshot = Path(unquote(urlsplit(uri).path))
            if sys.platform == "win32":
                snapshot = Path(str(snapshot).lstrip("\\/"))
            assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == expected


def test_real_pip_installs_approved_fixture_offline_and_rejects_public_candidate(reviewed, tmp_path, monkeypatch):
    # Actual pip smoke, not a subprocess-argument/source-text assertion.
    venv = tmp_path / "isolated-venv"
    subprocess.run([sys._base_executable, "-m", "venv", str(venv)], check=True, env=pip_environment(),
                   capture_output=True, text=True)
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    with synthetic_public_index(tmp_path) as (index, requests, phase):
        monkeypatch.setenv("PIP_INDEX_URL", index)
        monkeypatch.setenv("PIP_EXTRA_INDEX_URL", index)
        with verify(reviewed) as identity:
            # Even an explicit index candidate cannot override --no-index or the
            # authenticated file URI; unlisted poisoned wheelhouse files are ignored.
            fixture_wheel(reviewed["wheelhouse"], "unlisted-public-package", "1.0")
            command = install_command(python, identity["requirements"]) + ["--index-url", index]
            phase["name"] = "offline-install"
            subprocess.run(command, check=True, env=pip_environment(), capture_output=True, text=True)
            phase["name"] = "installed-identity"
            assert provision._installed_identity_ok(python, identity)
            phase["name"] = "fixture-import"
            result = subprocess.run([str(python), "-I", "-c", "import chudcode_rpc; print(chudcode_rpc.IDENTITY)"],
                                    check=True, capture_output=True, text=True, env=pip_environment())
            assert result.stdout.strip() == "approved fixture"
            # Installed source bytes cannot spoof authenticity with copied PEP 610 metadata.
            phase["name"] = "site-query"
            site = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                  check=True, capture_output=True, text=True).stdout.strip()
            phase["name"] = "identity-tamper-refusals"
            module = Path(site) / "chudcode_rpc" / "__init__.py"
            approved = module.read_bytes()
            vendored_metadata = module.parent / "_vendor/inert_child.dist-info/INSTALLER"
            vendored_metadata.write_text("unapproved substituted metadata\n")
            assert not provision._installed_identity_ok(python, identity)
            vendored_metadata.write_text("reviewed vendored metadata\n")
            assert provision._installed_identity_ok(python, identity)
            module.write_text("IDENTITY = 'public candidate'\n")
            assert not provision._installed_identity_ok(python, identity)
            module.write_bytes(approved)
            assert provision._installed_identity_ok(python, identity)
            extra = module.parent / "unapproved_module.py"
            extra.write_text("IDENTITY = 'unapproved extra'\n")
            assert not provision._installed_identity_ok(python, identity)
            extra.unlink()
            # A forged generated cache must not override authenticated source.
            import marshal
            import struct
            from importlib.util import cache_from_source, MAGIC_NUMBER
            cache = Path(cache_from_source(str(module)))
            cache.parent.mkdir(exist_ok=True)
            approved_cache = (cache.read_bytes() if cache.exists() else
                              MAGIC_NUMBER + struct.pack("<III", 0, int(module.stat().st_mtime) % (2 ** 32), len(approved)) +
                              marshal.dumps(compile(approved, str(module), "exec", dont_inherit=True, optimize=0)))
            forged = compile("IDENTITY = 'forged bytecode'", str(module), "exec", dont_inherit=True, optimize=0)
            cache.write_bytes(approved_cache[:16] + marshal.dumps(forged))
            assert not provision._installed_identity_ok(python, identity)
            cache.write_bytes(approved_cache)
            assert provision._installed_identity_ok(python, identity)
            # Reject startup hooks without executing them, even when all package
            # metadata/bytes still claim the reviewed distribution.
            marker = tmp_path / "startup-hook-ran"
            pth = Path(site) / "unapproved-runtime-hook.pth"
            pth.write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n")
            assert not provision._installed_identity_ok(python, identity)
            assert not marker.exists()
            pth.unlink()
            customization = Path(site) / "sitecustomize.py"
            customization.write_text(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')\n")
            assert not provision._installed_identity_ok(python, identity)
            assert not marker.exists()
            customization.unlink()
            # A bare matching version with editable provenance is rejected.
            origin = next(Path(site).glob("chudcode_rpc-*.dist-info")) / "direct_url.json"
            origin.write_text(json.dumps({"url": "file:///synthetic-editable", "dir_info": {"editable": True}}))
            assert not provision._installed_identity_ok(python, identity)
        assert requests == []


def test_freshness_requires_full_lock_and_target_not_just_requirements(reviewed, tmp_path, monkeypatch):
    bind_provisioner(monkeypatch, reviewed)
    stamp = tmp_path / ".ready.json"
    with verify(reviewed) as identity:
        provision._write_stamp(stamp, tmp_path / "python", identity)
        assert provision._stamp_matches(stamp, identity)
        assert not provision._stamp_matches(stamp, {**identity, "target": "another-target"})
        assert not provision._stamp_matches(stamp, {**identity, "lock_sha256": "2" * 64})


@pytest.mark.parametrize("failure", ["absent", "tampered"])
def test_real_pip_hash_gate_cannot_fall_back_to_synthetic_public_index(reviewed, tmp_path, failure):
    venv = tmp_path / "hash-gate-venv"
    subprocess.run([sys._base_executable, "-m", "venv", str(venv)], check=True,
                   env=pip_environment(), capture_output=True, text=True)
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    with synthetic_public_index(tmp_path) as (index, requests, phase):
        with verify(reviewed) as identity:
            private = identity["requirements"].parent / "chudcode_rpc-0.3.1-py3-none-any.whl"
            if failure == "absent":
                private.unlink()
            else:
                with private.open("ab") as stream:
                    stream.write(b"changed after preflight")
            phase["name"] = "pip-hash-refusal"
            result = subprocess.run(install_command(python, identity["requirements"]) + ["--index-url", index],
                                    env=pip_environment(), capture_output=True, text=True)
            assert result.returncode != 0
            assert requests == []


def test_compiler_tool_requirements_must_be_in_reviewed_complete_target(reviewed, tmp_path):
    additional = tmp_path / "compiler-requirements.txt"
    additional.write_text("inert-dependency==1.0\n")
    inputs = {key: reviewed[key] for key in ("wheelhouse", "requirements", "lock_path", "chudcode_lock")}
    with verified_wheelhouse(**inputs, additional_requirements=additional) as identity:
        assert identity["packages"]["inert-dependency"] == "1.0"
    additional.write_text("unreviewed-compiler==1.0\n")
    with pytest.raises(WheelhouseError, match="unreviewed-compiler"):
        with verified_wheelhouse(**inputs, additional_requirements=additional):
            pass


def _temporary_venv(directory):
    base = str(sys._base_executable)
    subprocess.run([base, "-I", "-S", "-m", "venv", str(directory)], check=True,
                   env=pip_environment(), capture_output=True, text=True)
    return directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def test_reuse_rejects_fake_candidate_executable_without_running_it(reviewed, tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX executable marker fixture; Windows launcher bytes require platform verification")
    candidate = _temporary_venv(tmp_path / "candidate")
    marker = tmp_path / "fake-candidate-ran"
    candidate.unlink()  # Never overwrite the symlink's authenticated base target.
    candidate.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\nexit 0\n")
    candidate.chmod(0o755)
    with verify(reviewed) as identity:
        assert not provision._installed_identity_ok(candidate, identity, str(sys._base_executable))
        assert not provision._installed_identity_ok(candidate, identity, str(candidate))
        (candidate.parent.parent / "pyvenv.cfg").unlink()
        assert not provision._installed_identity_ok(candidate, identity, str(candidate))
        alias = tmp_path / "base-alias"
        alias.symlink_to(candidate)
        assert not provision._installed_identity_ok(candidate, identity, str(alias))
    assert not marker.exists()


@pytest.mark.parametrize("change", ["home", "executable", "system-site", "startup-path", "stdlib-shadow"])
def test_runtime_platform_configuration_is_rejected_offline(reviewed, tmp_path, change):
    directory = tmp_path / "candidate"
    candidate = _temporary_venv(directory)
    configuration = directory / "pyvenv.cfg"
    contents = configuration.read_text()
    if change == "home":
        contents = "\n".join("home = " + str(tmp_path) if line.startswith("home =") else line
                             for line in contents.splitlines()) + "\n"
    elif change == "executable":
        contents = "\n".join("executable = " + str(tmp_path / "unapproved-python")
                             if line.startswith("executable =") else line for line in contents.splitlines()) + "\n"
    elif change == "system-site":
        contents = contents.replace("include-system-site-packages = false", "include-system-site-packages = true")
    elif change == "startup-path":
        (candidate.parent / "python._pth").write_text(str(tmp_path) + "\nimport site\n")
    else:
        library = directory / ("Lib" if sys.platform == "win32" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}")
        (library / "os.py").write_text("raise RuntimeError('unapproved stdlib')\n")
    configuration.write_text(contents)
    with verify(reviewed) as identity:
        assert not provision._installed_identity_ok(candidate, identity, str(sys._base_executable))


def test_site_disabled_pip_ignores_startup_hooks_and_ambient_import_paths(reviewed, tmp_path, monkeypatch):
    directory = tmp_path / "pip-runtime"
    candidate = _temporary_venv(directory)
    site = directory / ("Lib/site-packages" if sys.platform == "win32" else
                        f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    marker = tmp_path / "unapproved-startup-ran"
    payload = f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')\n"
    (site / "unapproved.pth").write_text(f"import pathlib;pathlib.Path({str(marker)!r}).write_text('ran')\n")
    (site / "sitecustomize.py").write_text(payload)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "sitecustomize.py").write_text(payload)
    (ambient / "pip.py").write_text(payload + "raise RuntimeError('ambient pip')\n")
    monkeypatch.setenv("PYTHONPATH", str(ambient))
    monkeypatch.setenv("PYTHONHOME", str(ambient))
    monkeypatch.setenv("LD_UNAPPROVED_OVERRIDE", "fixture")
    monkeypatch.setenv("DYLD_UNAPPROVED_OVERRIDE", "fixture")
    with verify(reviewed) as identity:
        subprocess.run(install_command(candidate, identity["requirements"]), check=True,
                       env=pip_environment(), capture_output=True, text=True)
        assert not provision._installed_identity_ok(candidate, identity, str(sys._base_executable))
    assert not marker.exists()


@pytest.mark.parametrize("failure", ["fake-executable", "startup-hook"])
def test_linux_builder_rejects_runtime_before_asset_generation_or_compilation(reviewed, tmp_path, failure):
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux shell builder scenario; corresponding platform builders require platform verification")
    import shutil
    root = tmp_path / "builder-source"
    original_root = Path(__file__).resolve().parents[2]
    for relative in ("services/__init__.py", "services/paths.py", "services/installer/__init__.py",
                     "services/installer/provision.py", "services/installer/wheelhouse.py",
                     "packaging/linux/build_appimage.sh"):
        source = original_root / relative
        if source.is_file():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    shutil.copyfile(reviewed["requirements"], root / "requirements.txt")
    for relative, source in (("vendor/python/RUNTIME_WHEELS.lock", reviewed["lock_path"]),
                             ("vendor/chudcode/CHUDCODE_ARTIFACT.lock", reviewed["chudcode_lock"])):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    (root / "packaging/requirements-release.txt").write_text("chudcode-rpc==0.3.1\n")
    directory = tmp_path / "compiler-runtime"
    candidate = _temporary_venv(directory)
    with verify(reviewed) as identity:
        subprocess.run(install_command(candidate, identity["requirements"]), check=True,
                       env=pip_environment(), capture_output=True, text=True)
        (directory / ".ready.json").write_text(json.dumps({
            "req_hash": hashlib.sha256(reviewed["requirements"].read_bytes()).hexdigest()[:16],
            "wheel_lock_sha256": identity["lock_sha256"], "wheel_target": identity["target"],
            "python": str(candidate),
        }))
    marker = tmp_path / "candidate-or-startup-ran"
    if failure == "fake-executable":
        candidate.unlink()
        candidate.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\nexit 0\n")
        candidate.chmod(0o755)
    else:
        site = directory / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        (site / "unapproved.pth").write_text(f"import pathlib;pathlib.Path({str(marker)!r}).write_text('ran')\n")
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "sitecustomize.py").write_text(f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')\n")
    (root / "build.sh").write_text("chudware_read_version() {\nCHUDWARE_VERSION=1.0.0\n}\n")
    host = root / "host/build-linux/bin/chudware"
    host.parent.mkdir(parents=True)
    host.write_text("#!/bin/sh\nexit 0\n")
    host.chmod(0o755)
    (host.parent.parent / "CMakeCache.txt").write_text("CHUDWARE_DEVELOPER_BUILD:BOOL=OFF\n")
    output = tmp_path / "output"
    environment = pip_environment()
    environment.update({
        "CHUDWARE_BASE_PYTHON": str(sys._base_executable), "CHUDWARE_RELEASE_PYTHON": str(candidate),
        "CHUDWARE_WHEELHOUSE": str(reviewed["wheelhouse"]), "PYTHONPATH": str(ambient),
        "PYTHONHOME": str(ambient), "LD_UNAPPROVED_OVERRIDE": "fixture", "DYLD_UNAPPROVED_OVERRIDE": "fixture",
        "HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "data"),
    })
    result = subprocess.run(["bash", str(root / "packaging/linux/build_appimage.sh"), "--prepare-signing",
                             "--key-id", "fixture", "--chud-bin", str(host), "--host-bin", str(host),
                             "--hostd-bin", str(host), "--protected-assets-dir", str(root / "generated"),
                             "--out", str(output)], env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert not marker.exists()
    assert not (output / "AppDir").exists()


def test_runtime_digest_caps_file_that_grows_after_admissible_open(tmp_path, monkeypatch):
    import os
    from services.installer import wheelhouse

    candidate = tmp_path / "growing-wheel-input"
    candidate.write_bytes(b"safe")
    maximum = 8
    monkeypatch.setattr(wheelhouse, "MAX_WHEEL_BYTES", maximum)
    original_regular_file = wheelhouse._regular_file
    read_boundary = threading.Event()
    growth_finished = threading.Event()
    opening_sizes = []
    returned_bytes = []
    outcomes = []

    class SynchronizedReader:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def before_read(self):
            if not read_boundary.is_set():
                opening_sizes.append(os.fstat(self.stream.fileno()).st_size)
                read_boundary.set()
                if not growth_finished.wait(timeout=5):
                    raise RuntimeError("writer did not release the actual file read")

        def read(self, size=-1):
            self.before_read()
            value = self.stream.read(size)
            returned_bytes.append(len(value))
            return value

        def readinto(self, buffer):
            # Exercise the same real stream if hashing regresses to file_digest.
            self.before_read()
            count = self.stream.readinto(buffer)
            returned_bytes.append(count)
            return count

    @contextmanager
    def synchronized_regular_file(path, limit):
        with original_regular_file(path, limit) as stream:
            # The original regular-file/type/initial-size admission has completed.
            yield SynchronizedReader(stream)

    monkeypatch.setattr(wheelhouse, "_regular_file", synchronized_regular_file)

    def hash_candidate():
        try:
            outcomes.append(wheelhouse._digest(candidate))
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=hash_candidate)
    worker.start()
    try:
        assert read_boundary.wait(timeout=5), "hashing never reached the actual read boundary"
        assert opening_sizes == [4]
        with candidate.open("ab") as writer:
            writer.write(b"grew-past-cap")
            writer.flush()
            os.fsync(writer.fileno())
    finally:
        growth_finished.set()
        worker.join(timeout=5)

    assert not worker.is_alive(), "bounded hashing did not stop after growth"
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], WheelhouseError)
    assert "runtime input exceeds hash size bound" in str(outcomes[0])
    # Reject after at most cap+1 actual bytes, not after hashing the grown file
    # and noticing changed stat metadata on context-manager exit.
    assert returned_bytes == [maximum + 1]
