"""Project roots across the real OMP Eval/child bridge, without a provider."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import time
import threading
from types import SimpleNamespace
import uuid

import pytest


_RUNTIME_FIXTURE = r'''
import { initializeExtensions } from "@oh-my-pi/pi-coding-agent/modes/runtime-init";
import { getBundledModels } from "@oh-my-pi/pi-catalog/models";

export default function (pi) {
  pi.on("session_start", async (_event, ctx) => {
    if (ctx.agent.kind !== "main") return;
    let child;
    try {
      const root = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
      if (!root?.preparedExtensions) throw new Error("root extension factories unavailable");
      const model = getBundledModels("anthropic").find(m => m.id === "claude-opus-5-5");
      if (!model) throw new Error("fixture model unavailable");
      const created = await pi.pi.createAgentSession({
        cwd: ctx.cwd, taskDepth: 1, agentId: "root-propagation-child",
        parentAgentId: ctx.agent.id, sessionManager: pi.pi.SessionManager.inMemory(ctx.cwd),
        model, modelRegistry: ctx.modelRegistry, settings: root.settings.overlay(),
        preloadedPreparedExtensions: root.preparedExtensions,
        disableExtensionDiscovery: true, bindProcessState: false,
      });
      child = created.session;
      const errors = [];
      await initializeExtensions(child, {
        reportSendError: (_action, error) => errors.push(String(error)),
        reportRuntimeError: error => errors.push(String(error.error)),
      });
      if (errors.length) throw new Error(errors.join("; "));
      const results = [];
      for (const [label, session, language] of [["root", root, "py"], ["child", child, "js"]]) {
        const evalTool = session.getToolByName("eval");
        if (!evalTool) throw new Error("Eval unavailable for " + label);
        const part = `parts/${label}.draw+`;
        const figure = `figures/${label}.png`;
        const py = `import json\na = await tool.write({"path": "xd://export_part", "content": json.dumps({"fmt": "draw+", "doc_id": "dwg", "path": ${JSON.stringify(part)}})})\nassert a["details"]["xdev"]["inner"]["ok"], a\nb = await tool.write({"path": "xd://render_part", "content": json.dumps({"doc_id": "dwg", "save_path": ${JSON.stringify(figure)}})})\nassert b["details"]["xdev"]["inner"]["ok"], b\nprint("root-eval-ok")`;
        const js = `const a = await tool.write({path: "xd://export_part", content: JSON.stringify({fmt: "draw+", doc_id: "dwg", path: ${JSON.stringify(part)}})});\nif (!a.details.xdev.inner.ok) throw new Error(JSON.stringify(a));\nconst b = await tool.write({path: "xd://render_part", content: JSON.stringify({doc_id: "dwg", save_path: ${JSON.stringify(figure)}})});\nif (!b.details.xdev.inner.ok) throw new Error(JSON.stringify(b));\nprint("child-eval-ok");`;
        const result = await evalTool.execute(`${label}-eval`, {
          language, code: language === "py" ? py : js, timeout: 60,
        });
        results.push({ label, agent: session.extensionRunner.createContext().agent, result });
      }
      await Bun.write(process.env.OMP_ROOT_TEST_REPORT, JSON.stringify({results}));
    } catch (error) {
      await Bun.write(process.env.OMP_ROOT_TEST_REPORT, JSON.stringify({error: String(error)}));
    } finally {
      if (child) await child.dispose();
    }
  });
}
'''


def test_installed_omp_root_and_child_eval_keep_selected_project(tmp_path, monkeypatch):
    configured = os.environ.get("CHUDWARE_OMP_INSTALLED_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_INSTALLED_TEST_BIN to the official installed OMP")
    rpc = pytest.importorskip("omp_rpc")
    from mcp.server.fastmcp import FastMCP, Image
    from PIL import Image as PILImage
    from services.agent import config
    from services.agent.omp_bridge import ExtensionBridge
    from services.agent.omp_host import (
        CAD_SESSION_TOOLS, _binary_supports_flag, _CAD_MEMORY_SETTINGS, _CAD_SESSION_SETTINGS,
    )
    from services.agent.omp_tools import build_host_tools
    from services.mcp import cad_exchange_tools, cad_render_tools, core_bridge

    root = tmp_path / "project" / "selected"
    root.mkdir(parents=True)
    (root.parent / ".git").mkdir()
    (tmp_path / "sessions").mkdir()
    monkeypatch.chdir(tmp_path)
    for key in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR", "CHUDWARE_FS_ROOT",
                "CHUDWARE_RENDER_SAVE_ANYWHERE"):
        monkeypatch.delenv(key, raising=False)
    for key, directory in (("HOME", tmp_path), ("USERPROFILE", tmp_path),
                           ("XDG_CONFIG_HOME", tmp_path / "config"),
                           ("XDG_DATA_HOME", tmp_path / "data"),
                           ("XDG_CACHE_HOME", tmp_path / "cache")):
        monkeypatch.setenv(key, str(directory))
    monkeypatch.setattr(config, "get_tool_options", lambda *_: {})
    monkeypatch.setattr(config, "tool_timeout_s_default", lambda: 0)
    monkeypatch.setattr(cad_exchange_tools, "_doc", lambda doc_id=None: doc_id or "dwg")
    monkeypatch.setattr(cad_render_tools, "_doc", lambda doc_id=None: doc_id or "dwg")
    monkeypatch.setattr(cad_render_tools, "_render_stem", lambda *_: "fixture")
    monkeypatch.setattr(cad_render_tools, "render_output_path",
                        lambda *_: str(tmp_path / f"inline-{uuid.uuid4().hex}.png"))
    monkeypatch.setattr(cad_render_tools, "_captioned_render",
                        lambda png, *_: [Image(path=png), "Fixture drawing"])

    def engine(method, params):
        if method == "doc_bbox":
            return {}
        if method == "drawing_render_png":
            PILImage.new("RGB", (8, 8), "white").save(params["out_path"])
            return {"ok": True}
        if method == "drawing_save_standalone":
            destination = Path(params["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"fixture drawing")
            return {"path": str(destination)}
        raise AssertionError(f"unexpected engine request: {method}")

    monkeypatch.setattr(core_bridge, "call", engine)
    mcp = FastMCP("root-propagation")
    cad_exchange_tools.register_cad_exchange_tools(mcp)
    cad_render_tools.register_cad_render_tools(mcp)
    entries = mcp._tool_manager._tools
    context = {
        "tool_entries": {name: entries[name] for name in ("export_part", "render_part")},
        "resolver": lambda name: entries[name].fn if name in entries else None,
        "omp_builtin_names": (), "project_dir": str(root), "workspace_dir": str(root),
        "save_dir": str(root), "run_id": "root-propagation-run", "mode": "agent",
    }
    selected = {"export_part", "render_part"}
    bindings = {
        binding.name: replace(binding, active=True, load_mode="discoverable")
        for binding in build_host_tools(context) if binding.name in selected
    }
    assert set(bindings) == selected
    calls = []

    def dispatch(name, args, host_context):
        calls.append((name, host_context.agent_id, host_context.parent_agent_id,
                      host_context.tool_call_id))
        return bindings[name].to_host_tool(lambda **kwargs: kwargs["execute"])(args, host_context)

    fixture = tmp_path / "root_fixture.ts"
    fixture.write_text(_RUNTIME_FIXTURE, encoding="utf-8")
    report = tmp_path / "report.json"
    overlay = tmp_path / "settings.yml"
    overlay.write_text(_CAD_SESSION_SETTINGS + _CAD_MEMORY_SETTINGS["off"], encoding="utf-8")
    binary = str(Path(configured).expanduser().resolve(strict=True))
    extra = ["--no-lsp"]
    if _binary_supports_flag(binary, "--no-mcp"):
        extra.append("--no-mcp")
    if _binary_supports_flag(binary, "--config"):
        extra.extend(["--config", str(overlay)])
    extra.extend(["--no-extensions", "--trusted-extension",
                  str(Path(__file__).with_name("omp_extension.ts")),
                  "--trusted-extension", str(fixture)])
    bridge = ExtensionBridge(str(tmp_path / "bridge"), dispatch).start()
    bridge.publish_tools([binding.as_dict() for binding in bindings.values()])
    info = bridge.info()
    client = rpc.RpcClient(
        executable=binary, cwd=root, session_dir=tmp_path / "sessions", no_skills=True,
        tools=CAD_SESSION_TOOLS, extra_args=extra, startup_timeout=90,
        env={"OMP_CAD_BRIDGE_ENDPOINT": info["endpoint"], "OMP_CAD_BRIDGE_TOKEN": info["token"],
             "OMP_CAD_BRIDGE_TRANSPORT": info["transport"], "OMP_ROOT_TEST_REPORT": str(report)},
    )
    try:
        client.start()
        deadline = time.monotonic() + 100
        while not report.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert report.is_file(), "OMP fixture did not finish"
        result = json.loads(report.read_text(encoding="utf-8"))
        assert "error" not in result, result
        rows = result["results"]
        assert [row["agent"]["kind"] for row in rows] == ["main", "sub"]
        assert rows[1]["agent"]["id"] == "root-propagation-child"
        assert rows[1]["agent"]["parentId"] == rows[0]["agent"]["id"]
        for row in rows:
            assert not row["result"].get("isError"), row
            assert f'{row["label"]}-eval-ok' in json.dumps(row["result"]), row
            assert (root / "parts" / f'{row["label"]}.draw+').read_bytes() == b"fixture drawing"
            assert (root / "figures" / f'{row["label"]}.png').is_file()
        assert [call[0] for call in calls] == ["export_part", "render_part"] * 2
        assert all(call[3].startswith("js-write-") for call in calls)
        assert [call[1] for call in calls] == [rows[0]["agent"]["id"]] * 2 + [rows[1]["agent"]["id"]] * 2
        assert calls[2][2] == calls[3][2] == rows[0]["agent"]["id"]
        assert not (tmp_path / "parts").exists()
    finally:
        client.stop()
        bridge.stop()


def test_background_cad_jobs_keep_each_turn_root_until_completion(tmp_path, monkeypatch):
    from services.agent import config, request_context
    from services.agent.omp_tools import build_host_tools
    from services.mcp import cad_common, cad_doc_tools, cad_render_tools

    ambient = tmp_path / "ambient"
    projects = [tmp_path / "first" / "selected", tmp_path / "second" / "selected"]
    for root in [ambient, *projects]:
        root.mkdir(parents=True)
    for name in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR", "CHUDWARE_FS_ROOT"):
        monkeypatch.setenv(name, str(ambient))
    monkeypatch.delenv("CHUDWARE_RENDER_SAVE_ANYWHERE", raising=False)
    monkeypatch.setattr(config, "get_tool_options", lambda *_: {})
    monkeypatch.setattr(config, "tool_timeout_s_default", lambda: 0)
    release = threading.Event()
    started = {str(root): threading.Event() for root in projects}

    def start_background():
        initiating_root = request_context.root_value("CHUDWARE_PROJECT_DIR")

        def work():
            started[initiating_root].set()
            if not release.wait(10):
                raise RuntimeError("test did not release the background work")
            return {
                "ok": True,
                "project": request_context.root_value("CHUDWARE_PROJECT_DIR"),
                "save": request_context.root_value("CHUDWARE_PROJECT_SAVE_DIR"),
                "fs": request_context.root_value("CHUDWARE_FS_ROOT"),
                "output": cad_common.resolve_agent_path("parts/background.step+", "output path"),
                "render": cad_render_tools._render_save_target("figures/background.png"),
            }

        return {"ok": True, "job_id": cad_common._start_job("project-root-test", work)}

    entry = {
        "fn": start_background, "description": "Background root probe",
        "input_schema": {"type": "object", "properties": {}},
    }
    bindings = []
    for root in projects:
        context = {
            "tool_entries": {"background_root_probe": entry},
            "resolver": lambda name: start_background if name == "background_root_probe" else None,
            "omp_builtin_names": (), "project_dir": str(root), "workspace_dir": str(root),
            "save_dir": str(root / "saved"), "run_id": root.parent.name, "mode": "agent",
        }
        bindings.append(next(item for item in build_host_tools(context)
                             if item.name == "background_root_probe"))

    class Registry:
        def __init__(self):
            self.tools = {}

        def tool(self, *args, **kwargs):
            def register(fn):
                self.tools[kwargs.get("name") or fn.__name__] = fn
                return fn
            return register

    registry = Registry()
    cad_doc_tools.register_cad_doc_tools(registry)
    host_context = SimpleNamespace(cancelled=False, tool_call_id="js-write-background",
                                   agent_id="builder", parent_agent_id="main")
    try:
        accepted = [binding.to_host_tool(lambda **kw: kw["execute"])({}, host_context)["details"]
                    for binding in bindings]
        assert all(item["ok"] for item in accepted)
        assert all(event.wait(5) for event in started.values())
        # Neither the caller's binding nor the surrounding process environment is changed.
        assert request_context.root_value("CHUDWARE_PROJECT_DIR") == str(ambient)
        release.set()
        for root, item in zip(projects, accepted):
            completed = json.loads(registry.tools["wait_job"](
                job_id=item["job_id"], timeout_s=10, poll_s=0.5))
            assert completed["status"] == "done", completed
            assert completed["result"] == {
                "ok": True, "project": str(root), "save": str(root / "saved"), "fs": str(root),
                "output": str(root / "parts" / "background.step+"),
                "render": str(root / "figures" / "background.png"),
            }
    finally:
        release.set()
