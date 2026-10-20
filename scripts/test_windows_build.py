"""Run the real developer scripts under Windows PowerShell 5.1 with inert tools.

These tests exercise native exit/path/copy semantics, not compiler performance,
installer authenticity, runtime qualification, or publication authority.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32" and os.name == "nt"
pytestmark = pytest.mark.skipif(not WINDOWS, reason="requires genuine Windows PowerShell 5.1 and native file semantics")

# One disposable .NET executable supplies real native-program exit codes without
# changing installed tools, invoking a compiler build, or touching user sessions.
_NATIVE_TOOL = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
class Fixture {
    static string Env(string name) { return Environment.GetEnvironmentVariable("CHUDWARE_FIXTURE_" + name) ?? ""; }
    static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }
    static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        string name = Path.GetFileNameWithoutExtension(Process.GetCurrentProcess().MainModule.FileName);
        string step = name;
        if (name == "cmake") {
            step = args.Contains("--preset") ? (args.Contains("--build") ? "core-build" : "core-configure")
                : (args.Contains("--build") ? "host-build" : "host-configure");
        } else if (name == "python") {
            step = "mcp";
        } else if (name == "robocopy") {
            string source = Path.GetFileName(args[0]);
            step = source == "bin" ? (Path.GetFileName(args[1]) == "Chudware" ? "core-copy" : "host-copy") : source;
        } else if (name == "git") {
            step = args.Contains("ls-files") ? "git-index" : args.Contains("cat-file") ? "git-target" : "git-skip";
        }
        File.AppendAllText(Env("LOG"), step + "|" + String.Join("\t", args) + "\n", new UTF8Encoding(false));
        if (Env("FAIL_STEP") == step) return Int32.Parse(Env("FAIL_CODE"));
        if (name == "cmake" && Env("IMPORTED") != "developer") return 94;
        if (name == "cmake" && Env("UNICODE") != "profile caf\u00e9 \u6f22\u5b57 & 100%") return 95;
        if (name == "vswhere") Console.WriteLine(Env("VS"));
        if (step == "git-index") Console.WriteLine("120000 " + new string('a', 40) + " 0\t" + Env("LINK"));
        if (step == "git-target") Console.WriteLine(Env("TARGET"));
        if (name == "robocopy") {
            string real = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "robocopy.exe");
            var start = new ProcessStartInfo(real, String.Join(" ", args.Select(Quote).ToArray()));
            start.UseShellExecute = false;
            using (var child = Process.Start(start)) {
                child.WaitForExit();
                if (child.ExitCode < 0 || child.ExitCode >= 8) return child.ExitCode;
            }
            return Int32.Parse(Env("COPY_CODE"));
        }
        return 0;
    }
}
'''


def _ps_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def native_scripts(tmp_path):
    # Derive the trusted system inspector exactly as release verification does;
    # this also refuses Wine rather than accepting an executable named powershell.
    try:
        from packaging.platform_publishers import _windows_powershell_executable
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location("windows_build_publishers", ROOT / "packaging/platform_publishers.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _windows_powershell_executable = module._windows_powershell_executable
    powershell = _windows_powershell_executable()
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    assert compiler.is_file(), "Windows .NET Framework C# compiler is required for disposable native-command fixtures"
    tools = tmp_path / "tools"
    tools.mkdir()
    source = tools / "fixture.cs"
    source.write_text(_NATIVE_TOOL, encoding="ascii")
    binary = tools / "fixture.exe"
    compiled = subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(binary), str(source)],
                              capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    for name in ("cmake", "cl", "link", "ninja", "robocopy", "git", "vcvars"):
        shutil.copyfile(binary, tools / (name + ".exe"))
    programs = tmp_path / "programs"
    vswhere = programs / "Microsoft Visual Studio/Installer/vswhere.exe"
    vswhere.parent.mkdir(parents=True)
    shutil.copyfile(binary, vswhere)
    vs = tmp_path / "vs tools [literal] caf\u00e9 & O'Brien 100%"
    bundled_cmake = vs / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
    bundled_cmake.parent.mkdir(parents=True)
    shutil.copyfile(binary, bundled_cmake)
    vcvars = vs / "VC/Auxiliary/Build/vcvars64.bat"
    vcvars.parent.mkdir(parents=True)
    # cmd /u is required for the product's Unicode developer-environment capture.
    # Keep stage logging in the native fixture's UTF-8 writer, not cmd's UTF-16 echo.
    vcvars.write_text('@vcvars.exe\n'
                      '@if errorlevel 1 exit /b %errorlevel%\n'
                      '@set "CHUDWARE_FIXTURE_IMPORTED=developer"\n'
                      '@exit /b 0\n', encoding="ascii")
    repo = tmp_path / "workspace [literal] caf\u00e9 & O'Brien 100%"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in ("build_windows.ps1", "stage_windows.ps1", "setup_windows_links.ps1"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    log = tmp_path / "calls.log"
    environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                       CHUDWARE_FIXTURE_LOG=str(log), CHUDWARE_FIXTURE_VS=str(vs),
                       CHUDWARE_FIXTURE_FAIL_STEP="", CHUDWARE_FIXTURE_FAIL_CODE="37", CHUDWARE_FIXTURE_COPY_CODE="1")
    environment["CHUDWARE_FIXTURE_UNICODE"] = "profile caf\u00e9 \u6f22\u5b57 & 100%"
    environment["ProgramFiles(x86)"] = str(programs)
    environment["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    temporary = tmp_path / "process-temp"
    temporary.mkdir()
    environment["TEMP"] = str(temporary)
    environment["TMP"] = str(temporary)
    return repo, environment, log, powershell, binary


def _run(native_scripts, name, *, failure="", code=37, copy_code=1):
    repo, environment, log, powershell, _binary = native_scripts
    environment.update(CHUDWARE_FIXTURE_FAIL_STEP=failure, CHUDWARE_FIXTURE_FAIL_CODE=str(code),
                       CHUDWARE_FIXTURE_COPY_CODE=str(copy_code))
    # Dot-source in a disposable process to check that location is restored on
    # success AND throw. The product scripts still use real child PowerShell/native tools.
    harness = repo / "invoke.ps1"
    harness.write_text("$ErrorActionPreference = 'Stop'\n"
                       "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding\n"
                       "$before = (Get-Location).Path\n"
                       "$beforePath = $env:PATH\n"
                       "$beforeImported = $env:CHUDWARE_FIXTURE_IMPORTED\n"
                       "$beforeEncoding = [Console]::OutputEncoding.CodePage\n"
                       "try { . " + _ps_literal(repo / "scripts" / name) + "; $status = 0 }\n"
                       "catch { Write-Host $_; $status = 1 }\n"
                       "if ((Get-Location).Path -ne $before) { throw 'script leaked its working directory' }\n"
                       "if ($env:PATH -ne $beforePath -or $env:CHUDWARE_FIXTURE_IMPORTED -ne $beforeImported) { throw 'script leaked its developer environment' }\n"
                       "if ([Console]::OutputEncoding.CodePage -ne $beforeEncoding) { throw 'script leaked its console encoding' }\n"
                       "Write-Host 'LOCATION_RESTORED'\nexit $status\n", encoding="utf-8-sig")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                            cwd=repo.parent, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert "LOCATION_RESTORED" in result.stdout, result.stdout + result.stderr
    calls = [line.split("|", 1) for line in log.read_text(encoding="utf-8-sig").splitlines()] if log.exists() else []
    return result, calls


def _script_fixture(repo, name, step):
    (repo / "scripts" / name).write_text(
        "Add-Content -LiteralPath $env:CHUDWARE_FIXTURE_LOG -Value '" + step + "|' -Encoding UTF8\n"
        "if ($env:CHUDWARE_FIXTURE_FAIL_STEP -eq '" + step + "') { exit ([int]$env:CHUDWARE_FIXTURE_FAIL_CODE) }\nexit 0\n",
        encoding="ascii",
    )


@pytest.mark.parametrize("failure", ["links", "vswhere", "vcvars", "core-configure", "core-build", "host-configure", "host-build", "stage", "mcp", ""])
def test_build_stops_every_native_failure(native_scripts, failure):
    repo, _environment, _log, _powershell, binary = native_scripts
    _script_fixture(repo, "setup_windows_links.ps1", "links")
    _script_fixture(repo, "stage_windows.ps1", "stage")
    python = repo / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    shutil.copyfile(binary, python)
    result, calls = _run(native_scripts, "build_windows.ps1", failure=failure)
    expected = ["links", "vswhere", "vcvars", "core-configure", "core-build", "host-configure", "host-build", "stage", "mcp"]
    assert [call[0] for call in calls] == (expected[:expected.index(failure) + 1] if failure else expected)
    assert (result.returncode != 0) == bool(failure), result.stdout + result.stderr


def _stage_inputs(repo):
    for relative in ("build/windows-occt/bin/libchudware.dll", "build/windows-occt/bin/chudware-hostd.exe",
                     "host/build-win/bin/chudware.exe", "engine/runtime.py", "services/__init__.py", "services/agent/runtime.py",
                     "services/ipc/runtime.py", "requirements.txt", "vendor/python/RUNTIME_WHEELS.lock",
                     "vendor/chudcode/CHUDCODE_ARTIFACT.lock", "wheelhouse/fixture.whl", "assets/fixture.dat"):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"inert developer-stage fixture; not authenticated release inputs\n")


@pytest.mark.parametrize("failure", ["core-copy", "host-copy", "engine", "agent", "ipc", "wheelhouse", "assets"])
@pytest.mark.parametrize("code", [8, 16, -1])
def test_stage_stops_every_failed_native_copy(native_scripts, failure, code):
    repo = native_scripts[0]
    _stage_inputs(repo)
    result, calls = _run(native_scripts, "stage_windows.ps1", failure=failure, code=code)
    expected = ["core-copy", "host-copy", "engine", "agent", "ipc", "wheelhouse", "assets"]
    assert [call[0] for call in calls] == expected[:expected.index(failure) + 1]
    assert result.returncode != 0, result.stdout + result.stderr


@pytest.mark.parametrize("code", range(8))
def test_stage_accepts_all_robocopy_success_codes_and_literal_paths(native_scripts, code):
    repo = native_scripts[0]
    _stage_inputs(repo)
    result, calls = _run(native_scripts, "stage_windows.ps1", copy_code=code)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [call[0] for call in calls] == ["core-copy", "host-copy", "engine", "agent", "ipc", "wheelhouse", "assets"]
    for source, destination in (("build/windows-occt/bin/libchudware.dll", "dist/Chudware/libchudware.dll"),
                                ("build/windows-occt/bin/chudware-hostd.exe", "dist/Chudware/chudware-hostd.exe"),
                                ("host/build-win/bin/chudware.exe", "dist/Chudware/chudware.exe"),
                                ("services/ipc/runtime.py", "dist/Chudware/app/services/ipc/runtime.py")):
        assert (repo / source).read_bytes() == (repo / destination).read_bytes()


@pytest.mark.parametrize("missing", ["build/windows-occt/bin/libchudware.dll", "build/windows-occt/bin/chudware-hostd.exe",
                                     "host/build-win/bin/chudware.exe", "requirements.txt", "vendor/python/RUNTIME_WHEELS.lock",
                                     "vendor/chudcode/CHUDCODE_ARTIFACT.lock", "wheelhouse"])
def test_stage_missing_prerequisite_never_mutates_retained_dist(native_scripts, missing):
    repo = native_scripts[0]
    _stage_inputs(repo)
    removed = repo / missing
    if removed.is_dir():
        shutil.rmtree(removed)
    else:
        removed.unlink()
    retained = repo / "dist/Chudware/chudware.exe"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"retained operator bytes")
    result, calls = _run(native_scripts, "stage_windows.ps1")
    assert result.returncode != 0, result.stdout + result.stderr
    assert not calls
    assert retained.read_bytes() == b"retained operator bytes"


@pytest.mark.parametrize("failure", ["git-index", "git-target", "git-skip", ""])
def test_junction_setup_checks_git_and_nested_literal_targets(native_scripts, failure):
    repo, environment, _log, _powershell, _binary = native_scripts
    link = "nested/link [literal] caf\u00e9 & O'Brien 100%"
    target = "../target [literal] caf\u00e9 & O'Brien 100%"
    environment.update(CHUDWARE_FIXTURE_LINK=link, CHUDWARE_FIXTURE_TARGET=target)
    (repo / "nested").mkdir()
    destination = repo / "target [literal] caf\u00e9 & O'Brien 100%"
    destination.mkdir()
    (destination / "sentinel").write_bytes(b"target bytes")
    (repo / link).write_text(target, encoding="utf-8")
    result, calls = _run(native_scripts, "setup_windows_links.ps1", failure=failure)
    expected = ["git-index", "git-target", "git-skip"]
    assert [call[0] for call in calls] == (expected[:expected.index(failure) + 1] if failure else expected)
    assert (result.returncode != 0) == bool(failure), result.stdout + result.stderr
    assert (destination / "sentinel").read_bytes() == b"target bytes"
    if failure:
        assert (repo / link).is_file()
    else:
        assert (repo / link / "sentinel").read_bytes() == b"target bytes"
        repeated, repeated_calls = _run(native_scripts, "setup_windows_links.ps1")
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert [call[0] for call in repeated_calls] == expected + expected[:2]
        assert (repo / link / "sentinel").read_bytes() == b"target bytes"
