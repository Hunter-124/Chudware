"""The podman argument list and the CLI wrapper (no real podman)."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from demo.config import ConfigError
from demo.podman import (
    NAME_PREFIX, PodmanError, PodmanRuntime, SessionSpec, build_run_args, run_command)

from .support import make_cfg, sid_for

SID = sid_for(0xABCDEF)


def spec(tmp_path, **kwargs):
    values = dict(sid=SID, port=18321, run_dir=tmp_path / "sessions" / SID / "run", started=1_700_000_000,
                  ip_tag="a" * 24)
    values.update(kwargs)
    return SessionSpec(**values)


def pairs(args):
    """Option -> list of values, for assertions that do not depend on ordering."""
    out: dict[str, list[str]] = {}
    for index, arg in enumerate(args[:-1]):
        if arg.startswith("-"):
            key, _, inline = arg.partition("=")
            value = inline if inline else (args[index + 1] if not args[index + 1].startswith("-") else "")
            out.setdefault(key, []).append(value)
    return out


def test_run_args_use_the_measured_working_hardening_set(tmp_path):
    cfg = make_cfg(tmp_path)
    args = build_run_args(cfg, spec(tmp_path))
    opts = pairs(args)
    assert args[:2] == ["run", "-d"] and "--rm" in args and args[-1] == cfg.image
    assert opts["--name"] == [NAME_PREFIX + SID]
    assert opts["--user"] == ["10001:10001"] and "--read-only" in args
    assert "--cap-drop=ALL" in args and "--security-opt=no-new-privileges" in args
    secopts = opts["--security-opt"]
    assert "systempaths=unconfined" in secopts and "label=disable" in secopts
    # The default seccomp profile stays: neither seccomp nor privileged relaxations appear.
    assert not any("seccomp" in a for a in args) and "--privileged" not in args
    assert not any(a.startswith("--cap-add") or a.startswith("--device") for a in args)
    assert not any(a in ("--network=host", "--net=host", "--pid=host", "--ipc=host", "--userns=host") for a in args)
    assert opts["--cpus"] == ["2"] and opts["--memory"] == ["3500m"] and opts["--memory-swap"] == ["3500m"]
    assert opts["--pids-limit"] == ["512"]
    assert opts["--pull"] == ["never"] and "--no-healthcheck" in args
    assert opts["--network"] == ["pasta"]
    assert opts["--publish"] == ["127.0.0.1:18321:8300/tcp"]        # loopback only, fixed inner port


def test_run_args_tmpfs_for_every_writable_path_and_one_bind_mount(tmp_path):
    cfg = make_cfg(tmp_path)
    args = build_run_args(cfg, spec(tmp_path))
    tmpfs = dict(v.split(":", 1) for v in pairs(args)["--tmpfs"])
    assert set(tmpfs) == {"/data", "/work", "/state", "/run", "/tmp"}
    for path, options in tmpfs.items():
        assert "U" in options.split(",") and "nosuid" in options and "nodev" in options
        assert "uid=" not in options and "gid=" not in options and "noexec" not in options
        assert any(o.startswith("size=") for o in options.split(","))
    assert "mode=0700" in tmpfs["/work"] and "size=1g" in tmpfs["/work"] and "size=256m" in tmpfs["/data"]
    assert "mode=1777" in tmpfs["/tmp"]
    # The only host path in the container is the per-session provider directory.
    volumes = pairs(args)["--volume"]
    assert volumes == [f"{tmp_path}/sessions/{SID}/run:/run/chudware-demo:rw,nosuid,nodev,noexec"]
    assert "-v" not in args and not any(a.startswith("--mount") for a in args)


def test_run_args_env_labels_and_no_secrets(tmp_path):
    cfg = make_cfg(tmp_path, openrouter_key="sk-or-owner-key")
    args = build_run_args(cfg, spec(tmp_path))
    env = dict(v.split("=", 1) for v in pairs(args)["--env"])
    assert env == {
        "CHUDWARE_WEB_CELL_BACKEND": "container", "CHUDWARE_CONTAINER_RUNTIME": "1",
        "CHUDWARE_WEB_OPERATOR": "demo", "CHUDWARE_WEB_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "CHUDWARE_WEB_HOME": "/data", "CHUDWARE_WORKSPACE_ROOT": "/work",
        "CHUDWARE_WEB_APP": "/opt/chudware/app", "CHUDWARE_WEB_DEMO": "1"}
    joined = "\0".join(args)
    assert "sk-or-owner-key" not in joined and "OPENROUTER" not in joined and "kkkkkkkk" not in joined
    labels = dict(v.split("=", 1) for v in pairs(args)["--label"])
    assert labels == {"chudware.demo": "1", "chudware.demo.sid": SID, "chudware.demo.port": "18321",
                      "chudware.demo.started": "1700000000", "chudware.demo.ip": "a" * 24}
    assert "-e" not in args                                           # no host environment passthrough


def test_run_args_follow_configuration(tmp_path):
    cfg = make_cfg(tmp_path, cpus="1.5", memory="2g", pasta_options=("--ipv4-only",),
                   extra_security_opts=("apparmor=chudware-selfhost",), image="ghcr.io/hunter-124/chudware-web:sha-1")
    args = build_run_args(cfg, spec(tmp_path))
    opts = pairs(args)
    assert opts["--cpus"] == ["1.5"] and opts["--memory"] == ["2g"] and opts["--memory-swap"] == ["2g"]
    assert opts["--network"] == ["pasta:--ipv4-only"] and args[-1].endswith(":sha-1")
    assert "apparmor=chudware-selfhost" in opts["--security-opt"]
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, extra_security_opts=("seccomp=unconfined",))


def test_session_spec_refuses_ids_that_could_break_out_of_names_and_paths(tmp_path):
    for bad in ("../../x", "a b", "UPPERCASE00000000000000", "short", ""):
        with pytest.raises(ValueError):
            spec(tmp_path, sid=bad)


class Recorder:
    def __init__(self, results):
        self.calls, self.results = [], list(results)

    async def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        return self.results.pop(0)


def test_start_runs_podman_run_and_reports_failures(tmp_path):
    async def main():
        cfg = make_cfg(tmp_path, podman_bin="/usr/bin/podman")
        ok = Recorder([(0, "deadbeefcafe0000\n", "")])
        await PodmanRuntime(cfg, exec_fn=ok).start(spec(tmp_path))
        argv, timeout = ok.calls[0]
        assert argv[0] == "/usr/bin/podman" and argv[1:3] == ["run", "-d"] and timeout >= 60
        bad = Recorder([(125, "", "Error: crun: setrlimit failed\n")])
        with pytest.raises(PodmanError, match="rc=125.*setrlimit"):
            await PodmanRuntime(cfg, exec_fn=bad).start(spec(tmp_path))
    asyncio.run(main())


def test_remove_forces_and_tolerates_missing_containers(tmp_path):
    async def main():
        cfg = make_cfg(tmp_path)
        rec = Recorder([(0, "", ""), (1, "", "Error: no such container chudware-demo-x\n"),
                        (125, "", "Error: cannot remove container: busy\n")])
        runtime = PodmanRuntime(cfg, exec_fn=rec)
        await runtime.remove("chudware-demo-a")
        await runtime.remove("chudware-demo-x")
        with pytest.raises(PodmanError):
            await runtime.remove("chudware-demo-b")
        assert rec.calls[0][0] == ["podman", "rm", "-f", "--time", "3", "chudware-demo-a"]
    asyncio.run(main())


def test_list_labelled_parses_ps_output(tmp_path):
    async def main():
        out = "\n".join([
            f"{NAME_PREFIX}{SID}\trunning\t{SID}\t18321\t1700000000\t{'a' * 24}",
            f"{NAME_PREFIX}{sid_for(2)}\texited\t\t\t\t",
            "unrelated-container\trunning\t\t\t\t",
            "malformed line",
        ]) + "\n"
        rec = Recorder([(0, out, "")])
        infos = await PodmanRuntime(make_cfg(tmp_path), exec_fn=rec).list_labelled()
        assert [i.name for i in infos] == [NAME_PREFIX + SID, NAME_PREFIX + sid_for(2)]
        first, second = infos
        assert (first.sid, first.port, first.started, first.ip_tag, first.running) == (SID, 18321, 1700000000, "a" * 24, True)
        assert second.port is None and second.started is None and not second.running
        argv = rec.calls[0][0]
        assert argv[:4] == ["podman", "ps", "-a", "--filter"] and f"name=^{NAME_PREFIX}" in argv
        failing = Recorder([(125, "", "boom")])
        with pytest.raises(PodmanError):
            await PodmanRuntime(make_cfg(tmp_path), exec_fn=failing).list_labelled()
    asyncio.run(main())


FAKE_PODMAN = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time
    if sys.argv[1:2] == ["sleep"]:
        time.sleep(30)
    print(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ)}}))
    print("to-stderr", file=sys.stderr)
    sys.exit(3 if sys.argv[1:2] == ["fail"] else 0)
    """)


def test_run_command_passes_argv_verbatim_without_a_shell_and_scrubs_the_environment(tmp_path, monkeypatch):
    fake = tmp_path / "podman"
    fake.write_text(FAKE_PODMAN.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CHUDWARE_DEMO_OPENROUTER_KEY", "sk-or-must-not-leak")
    monkeypatch.setenv("CHUDWARE_DEMO_COOKIE_KEY", "cookie-key-must-not-leak")
    monkeypatch.setenv("SOME_OTHER_SECRET", "nope")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CONTAINERS_CONF", "/etc/containers/containers.conf")
    nasty = ["a b", "$(touch pwned)", "; rm -rf /", "`id`", "--flag=x y", ""]

    async def main():
        rc, out, err = await run_command([str(fake), *nasty], 10)
        assert rc == 0 and "to-stderr" in err
        seen = json.loads(out)
        assert seen["argv"] == nasty                               # untouched by any shell
        assert not (Path.cwd() / "pwned").exists()
        assert "XDG_RUNTIME_DIR" in seen["env"] and "CONTAINERS_CONF" in seen["env"]
        for name in ("CHUDWARE_DEMO_OPENROUTER_KEY", "CHUDWARE_DEMO_COOKIE_KEY", "SOME_OTHER_SECRET"):
            assert name not in seen["env"]
        rc, _, _ = await run_command([str(fake), "fail"], 10)
        assert rc == 3
        with pytest.raises(PodmanError, match="timed out"):
            await run_command([str(fake), "sleep"], 0.5)
    asyncio.run(main())


def test_a_missing_podman_binary_is_a_podman_error_not_a_crash():
    async def main():
        with pytest.raises(PodmanError, match="cannot run"):
            await run_command(["/nonexistent/podman", "ps"], 5)
    asyncio.run(main())
