"""End-to-end trial pipeline, offline: a stand-in executor takes the EXACT run_mission
argument list, drops a REAL part (built with the CAD tools) into the trial workspace and
writes a current-format ``--result-json`` record; the runner then grades it in a fresh
grader process and writes the manifest. No model, no daemon.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from benchmarks.artifact_validity import provenance as prov
from benchmarks.artifact_validity import runner
from benchmarks.artifact_validity.evidence import gate
from benchmarks.artifact_validity.tasks.schema import CheckResult, Status, Task, check

HERE = Path(__file__).resolve().parent
REC = HERE / "fixtures" / "recorded"


def _box(length, width, height):
    return [{"op": "add_feature", "feature": {"type": "box", "name": "Slab", "params": {
        "length": length, "width": width, "height": height}}}]


@check("one_sound_body")
def _one_body(ev):
    if (g := gate(ev, "one_sound_body")) is not None:
        return g
    ok = len(ev.bodies) == 1 and all(b["valid"] and b["closed"] for b in ev.bodies)
    return CheckResult("one_sound_body", Status.PASS if ok else Status.FAIL,
                       measured=len(ev.bodies), expected=1)


@check("bbox_60x40x20")
def _bbox(ev):
    if (g := gate(ev, "bbox_60x40x20")) is not None:
        return g
    size = sorted(ev.rpc("doc_bbox")["size"])
    ok = all(abs(a - b) <= 0.05 for a, b in zip(size, [20, 40, 60]))
    return CheckResult("bbox_60x40x20", Status.PASS if ok else Status.FAIL,
                       measured=size, expected=[20, 40, 60], tolerance=0.05)


@check("volume_48000")
def _volume(ev):
    if (g := gate(ev, "volume_48000")) is not None:
        return g
    vol = sum(float(b["volume_mm3"] or 0) for b in ev.bodies)
    ok = abs(vol - 48000.0) <= 48.0
    return CheckResult("volume_48000", Status.PASS if ok else Status.FAIL,
                       measured=vol, expected=48000.0, tolerance=48.0)


_PROMPT = "Model a 60 x 40 x 20 mm block and save it as `deliverable.step+` in the working directory."
BOX_TASK = Task(id="TBOX", version=1, domain="cad", prompt=_PROMPT,
                checks=(_one_body, _bbox, _volume), budget_s=60)
SEEDED_TASK = Task(id="TSEED", version=1, domain="cad", prompt=_PROMPT,
                   checks=(_one_body, _bbox, _volume), budget_s=60,
                   fixtures=("seed/deliverable.step+",))

_FAKE = r'''
import argparse, json, os, shutil, sys, time
ap = argparse.ArgumentParser()
for a in ("--workspace", "--log", "--result-json", "--prompt-file", "--sessions-dir"):
    ap.add_argument(a)
args, _ = ap.parse_known_args()
mode, src, rec = os.environ["FAKE_MODE"], os.environ.get("FAKE_SRC", ""), os.environ["FAKE_REC"]
assert open(args.prompt_file).read().strip()
shutil.copyfile(os.path.join(rec, "turn_ok.session.jsonl"),
                os.path.join(args.sessions_dir, "turn_ok.session.jsonl"))
if mode == "crash":
    sys.exit(3)
dst = os.path.join(args.workspace, "deliverable.step+")
if mode in ("ok", "budget", "stale"):
    shutil.copyfile(src, dst)
if mode == "stale":
    os.utime(dst, (time.time() - 86400, time.time() - 86400))
if mode == "empty":
    open(dst, "wb").close()
shutil.copyfile(os.path.join(rec, "turn_ok.events.jsonl"), args.log)
r = json.load(open(os.path.join(rec, "turn_ok.result.json")))
if mode == "budget":
    r["verdict"], r["exit_code"] = "not_ok", 1
    r["response"] = {"ok": False, "stop_reason": "turn_budget",
                     "reason": "This turn used its whole budget", "usage": None}
json.dump(r, open(args.result_json, "w"))
'''


@pytest.fixture
def harness(tmp_path, monkeypatch, build_part):
    runs, fixtures = tmp_path / "runs", tmp_path / "fixtures"
    monkeypatch.setattr(runner, "RUNS", runs)
    monkeypatch.setattr(runner, "WORKSPACES", tmp_path / "live")
    monkeypatch.setattr(runner, "FIXTURES", fixtures)
    monkeypatch.setattr(prov, "FIXTURES", fixtures)
    fake = tmp_path / "fake_executor.py"
    fake.write_text(_FAKE)
    good = build_part(_box(60, 40, 20), name="good", path=tmp_path / "good.step+")
    wrong = build_part(_box(60, 40, 25), name="wrong", path=tmp_path / "wrong.step+")
    (fixtures / "seed").mkdir(parents=True)
    (fixtures / "seed" / "deliverable.step+").write_bytes(good.read_bytes())
    monkeypatch.setenv("FAKE_REC", str(REC))

    def run(mode, *, src=good, task=BOX_TASK, rep=1):
        monkeypatch.setenv("FAKE_MODE", mode)
        monkeypatch.setenv("FAKE_SRC", str(src))
        return runner.run_trial(tag="t", task=task, model=runner.parse_model(
            "anthropic:claude-opus-5-5:high"), rep=rep, hostd=str(fake),
            executor_program=[sys.executable, str(fake)], grade_timeout_s=300)

    run.good, run.wrong = good, wrong
    return run


def _checks(m):
    return {c["id"]: c["status"] for c in m["grade"]["checks"]}


def test_correct_deliverable_passes_with_a_complete_manifest(harness):
    m = harness("ok")
    assert m["verdict"]["status"] == "pass", m["verdict"]
    assert _checks(m) == {"one_sound_body": "pass", "bbox_60x40x20": "pass",
                          "volume_48000": "pass"}
    trial = Path(m["trial_dir"])
    on_disk = json.loads((trial / "manifest.json").read_text())
    assert on_disk["verdict"] == m["verdict"]
    # Manifest completeness: every provenance field the comparison needs.
    assert on_disk["schema"] == runner.MANIFEST_SCHEMA and on_disk["replicate"] == 1
    t = on_disk["task"]
    assert len(t["task_sha256"]) == 64 and len(t["scorer"]["sha256"]) == 64
    assert t["check_ids"] == ["one_sound_body", "bbox_60x40x20", "volume_48000"]
    assert any(f.endswith("test_runner.py") for f in t["scorer"]["files"])
    assert on_disk["model"] == {"label": "anthropic:claude-opus-5-5:high",
                                "provider": "anthropic", "model": "claude-opus-5-5",
                                "effort": "high"}
    ex = on_disk["executor"]
    live = trial.parents[4] / "live" / "t" / "TBOX" / "anthropic__claude-opus-5-5__high" / "01"
    assert ex["command"][2:4] == ["--workspace", str(live)]
    assert ex["live_workspace"] == str(live) and not live.exists()  # bundled, then removed
    assert "--result-json" in ex["command"] and ex["driver_returncode"] == 0
    assert ex["env"]["CHUDWARE_HOSTD_LINGER_SECS"] == "20"
    assert {"head", "dirty", "dirty_fingerprint"} <= set(on_disk["repo"])
    assert len(on_disk["binaries"]["hostd"]["sha256"]) == 64
    assert {"started", "ended", "wall_s", "started_epoch"} <= set(on_disk["timing"])
    assert on_disk["turn"]["status"] == "pass"
    assert on_disk["usage"]["worker_tokens"]["total_tokens"] == 56339
    assert on_disk["usage"]["session"]["total_tokens"] == 56344
    assert on_disk["usage"]["session"]["messages"] == 3
    assert on_disk["usage"]["cost_usd"] == pytest.approx(0.059741, abs=1e-6)
    assert on_disk["artifact"]["sha256"] == prov.sha256_path(trial / "work" / "deliverable.step+")
    assert on_disk["events"]["tool_calls"] == 3
    assert on_disk["grade"]["grader"]["pid"] != os.getpid()
    assert on_disk["grade"]["grader"]["engine"]["daemon"] == "0"
    for c in on_disk["grade"]["checks"]:
        assert {"id", "status", "measured", "expected", "tolerance", "detail"} <= set(c)
    assert (trial / "prompt.txt").read_text().strip() == BOX_TASK.prompt


def test_wrong_dimension_fails(harness):
    m = harness("ok", src=harness.wrong)
    assert m["verdict"]["status"] == "fail"
    c = _checks(m)
    assert c["one_sound_body"] == "pass" and c["bbox_60x40x20"] == "fail"
    assert c["volume_48000"] == "fail"


@pytest.mark.parametrize("mode", ["none", "empty"])
def test_missing_or_empty_artifact_fails_every_check(harness, mode):
    m = harness(mode)
    assert m["verdict"]["status"] == "fail"
    assert set(_checks(m).values()) == {"fail"} and m["verdict"]["checks_total"] == 3


def test_stale_artifact_fails_even_though_its_geometry_is_right(harness):
    m = harness("stale")
    assert set(_checks(m).values()) == {"pass"}
    assert m["verdict"]["status"] == "fail" and not m["verdict"]["artifact_fresh"]
    assert any("predates" in r for r in m["verdict"]["reasons"])


def test_untouched_input_fixture_is_not_a_deliverable(harness):
    m = harness("none", task=SEEDED_TASK)
    assert (Path(m["trial_dir"]) / "work" / "deliverable.step+").is_file()
    assert m["verdict"]["status"] == "fail"
    assert any("unmodified input fixture" in r for r in m["verdict"]["reasons"])


def test_budget_exhaustion_with_a_correct_leftover_file_never_passes(harness):
    m = harness("budget")
    assert set(_checks(m).values()) == {"pass"}
    assert m["turn"]["status"] == "fail" and m["verdict"]["status"] == "fail"
    assert m["usage"]["worker_tokens"] is None


def test_executor_crash_is_error_not_fail(harness):
    m = harness("crash")
    assert m["turn"]["status"] == "error" and m["verdict"]["status"] == "error"
    assert m["executor"]["driver_returncode"] == 3


def test_trial_dirs_are_never_reused(harness):
    harness("ok")
    with pytest.raises(SystemExit, match="refusing to reuse"):
        harness("ok")


def test_workspace_under_a_project_marker_is_refused(harness, tmp_path, monkeypatch):
    marked = tmp_path / "proj"
    (marked / ".plm").mkdir(parents=True)
    monkeypatch.setattr(runner, "WORKSPACES", marked / "live")
    with pytest.raises(SystemExit, match="resolves to project root"):
        harness("ok")
    assert not (marked / "live" / "t" / "TBOX" / "anthropic__claude-opus-5-5__high"
                / "01").exists()


def test_default_live_workspaces_are_outside_the_repo():
    assert runner._REPO.resolve() not in runner.WORKSPACES.resolve().parents
    # A workspace inside the repo would hand the agent the repo root (.plm/.aicadvault).
    assert "resolves to project root" in runner.workspace_root_fault(runner.HERE / "runs")


def test_dry_run_prints_the_exact_run_mission_command(monkeypatch, capsys, tmp_path):
    from benchmarks.artifact_validity import tasks

    monkeypatch.setattr(tasks, "_REGISTRY", {"TBOX": BOX_TASK})
    monkeypatch.setattr(runner, "WORKSPACES", tmp_path / "live")
    monkeypatch.setattr(runner, "RUNS", tmp_path / "runs")
    rc = runner.main(["run", "--tag", "dry", "--tasks", "TBOX", "--model",
                      "openai-codex:gpt-5.5:xhigh", "--model", "anthropic:claude-opus-5-5",
                      "--replicates", "2", "--hostd-bin", "/opt/hostd", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "scripts/run_mission.py --workspace" in out
    assert "--provider openai-codex --model gpt-5.5 --reasoning-effort xhigh" in out
    assert "--provider anthropic --model claude-opus-5-5 --mode agent" in out
    assert "--instance-key avbench-dry-TBOX-" in out and "--hostd-bin /opt/hostd" in out
    assert "--timeout-s 60 --quiet-worker --result-json" in out
    assert out.count("# TBOX") == 4 and not (tmp_path / "runs").exists()
    # Model order rotates per replicate.
    headers = [ln for ln in out.splitlines() if ln.startswith("# ")]
    assert "openai-codex" in headers[0] and "anthropic" in headers[2]


def test_model_spec_parsing():
    assert runner.parse_model("openrouter:anthropic/claude:sonnet").model == \
        "anthropic/claude:sonnet"
    m = runner.parse_model("openrouter:anthropic/claude:sonnet:low")
    assert (m.provider, m.model, m.effort) == ("openrouter", "anthropic/claude:sonnet", "low")
    with pytest.raises(Exception):
        runner.parse_model("nomodel")
