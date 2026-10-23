"""Evidence packets are never overwritten, and a packet is checkable before it is cited.

Both behaviours are regressions of a live failure (2026-09-05): a mission
re-ran FRAME-PEDAL-BRAKE into the folder that already held the released bonded packet, and
`result.json` / `inputs.json` / `provenance.json` from the earlier run were gone. The
never-overwrite rule lived only in the project's runbook, so the only thing enforcing it
was the agent's memory — these tests pin it in the tool instead.

Pure filesystem: `sim_evidence_packet` and `verify_packet` take a study result as data, so
nothing here needs an engine, a document or a solve.
"""
from __future__ import annotations

import json

from services.mcp.sim_evidence_tools import register_sim_evidence_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools() -> dict:
    stub = _StubMCP()
    register_sim_evidence_tools(stub)
    return stub.tools


def _call(tools: dict, name: str, **kw) -> dict:
    out = tools[name](**kw)
    assert isinstance(out, str), f"{name} must return a JSON string"
    return json.loads(out)


def _result(peak: float = 105.98, build: str = "eng-2026-09-05-abc") -> dict:
    """A static result in the shape sim_evidence_packet is documented to be handed."""
    return {
        "ok": True,
        "doc_id": "doc-1",
        "material": {"name": "steel", "E_mpa": 200000.0, "yield_mpa": 250.0},
        "summary": {
            "max_von_mises_mpa": peak,
            "max_von_mises_raw_mpa": peak * 3.8,
            "max_von_mises_reduction": "fixture_ring_excluded",
            "summary_nodes_excluded": 812,
            "summary_nodes_total": 41022,
            "min_fos": 2.36,
            "min_fos_raw": 0.62,
            "max_disp_mm": 0.42,
        },
        "mesh_quality": {"order": 2, "n_nodes": 41022, "n_elems": 24110,
                         "aspect_max": 12.4, "jacobian_min": 0.21, "pct_poor": 0.4},
        "reactions": {"applied_force_n": 1500.0, "reaction_force_n": 1499.8,
                      "equilibrium_error_pct": 0.013},
        "provenance": {"solver": "chudware-fea", "engine_build_id": build,
                       "polymesh_commit": "deadbeef", "mesher": "polymesh",
                       "timestamp_utc": "2026-09-05T22:14:00Z"},
        "n_dof": 123066,
        "monitor": "max_von_mises_mpa",
        "tol_pct": 5.0,
        "converged": True,
        "final_delta_pct": 1.9,
        "table": [
            {"size_mm": 4.0, "n_dof": 30000, "max_von_mises_mpa": 92.1},
            {"size_mm": 2.0, "n_dof": 71000, "max_von_mises_mpa": 103.9,
             "delta_pct_vs_prev": 12.8},
            {"size_mm": 1.0, "n_dof": 123066, "max_von_mises_mpa": peak,
             "delta_pct_vs_prev": 1.9},
        ],
    }


def _write(tools: dict, out_dir, geometry, result: dict, **kw) -> dict:
    return _call(
        tools,
        "sim_evidence_packet",
        case_id="FRAME-PEDAL-BRAKE",
        requirement_id="REQ-7",
        out_dir=str(out_dir),
        study_result=result,
        geometry_path=str(geometry),
        material_source="registry row: steel 1018",
        load_rationale="1500 N single-pedal stomp",
        notes="pedal force is an OPEN input",
        **kw,
    )


def _geometry(tmp_path, body: str = "solid frame"):
    step = tmp_path / "frame.step"
    step.write_text(body, encoding="utf-8")
    return step


def test_underresolved_ladder_wall_remains_a_packet_limitation(tmp_path):
    result = _result()
    caveat = "Underresolved wall: refine through the measured minimum wall before release."
    result["table"][-1]["underresolved_wall"] = {
        "min_wall_mm": 0.53, "elements_across": 0.09}
    result["table"][-1]["resolution_caveat"] = caveat
    written = _write(_tools(), tmp_path / "out", _geometry(tmp_path), result)
    assert written["ok"], written
    checklist = (tmp_path / "out" / "FRAME-PEDAL-BRAKE" / "checklist.md").read_text()
    assert caveat in checklist


def test_second_packet_archives_the_first_and_names_it(tmp_path):
    """A re-run into the same folder must not destroy the released packet.

    The first packet's bytes must still be readable afterwards, in a folder the reply
    names, and the new provenance.json must say which run it displaced.
    """
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    first = _write(tools, out, _geometry(tmp_path), _result(peak=105.98))
    assert first["ok"] is True
    assert first["archived_to"] is None, "nothing to archive on a first write"
    first_result = json.loads((out / "FRAME-PEDAL-BRAKE" / "result.json").read_text())
    first_sha = first["result_sha256"]

    second = _write(
        tools, out, _geometry(tmp_path, "solid frame rev B"),
        _result(peak=402.93, build="eng-2026-09-06-def"),
    )
    archived = second["archived_to"]
    assert archived, "the second write must report where the first packet went"
    archive_dir = out / "FRAME-PEDAL-BRAKE" / archived.rsplit("/", 1)[1]
    assert archive_dir.is_dir()
    assert archive_dir.name.startswith("superseded_")
    assert first_sha[:8] in archive_dir.name, "the archive is named by the old result hash"

    # The FIRST run survived, byte for byte, as a complete set.
    for name in ("inputs.json", "result.json", "provenance.json",
                 "convergence.md", "checklist.md"):
        assert (archive_dir / name).is_file(), f"{name} was not archived"
    assert json.loads((archive_dir / "result.json").read_text()) == first_result

    # And the live folder holds the SECOND run, which names what it replaced.
    live = json.loads((out / "FRAME-PEDAL-BRAKE" / "result.json").read_text())
    assert live["summary"]["max_von_mises_mpa"] == 402.93
    prov = json.loads((out / "FRAME-PEDAL-BRAKE" / "provenance.json").read_text())
    superseded = prov["superseded_from"]
    assert superseded["mode"] == "archived"
    assert superseded["result_sha256"] == first_sha
    assert superseded["engine_build_id"] == "eng-2026-09-05-abc"
    assert superseded["archived_to"] == archived
    assert second["superseded_from"]["result_sha256"] == first_sha


def test_third_packet_chains_the_lineage(tmp_path):
    """Each archive records the previous one, so a case's history is walkable."""
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    _write(tools, out, _geometry(tmp_path), _result(peak=1.0))
    second = _write(tools, out, _geometry(tmp_path), _result(peak=2.0))
    _write(tools, out, _geometry(tmp_path), _result(peak=3.0))
    prov = json.loads((out / "FRAME-PEDAL-BRAKE" / "provenance.json").read_text())
    assert prov["superseded_from"]["chained_from"] == second["archived_to"]
    archives = sorted(
        p.name for p in (out / "FRAME-PEDAL-BRAKE").iterdir()
        if p.is_dir() and p.name.startswith("superseded_")
    )
    assert len(archives) == 2, f"one archive per displaced run, got {archives}"


def test_a_render_inside_the_packet_survives_its_own_archiving(tmp_path):
    """Citing a packet's own annotated_render.png back at the tool must not lose it.

    The render is the only checklist item the packet closes with a FILE, and it is the
    one input that lives in the folder being archived.
    """
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    png = tmp_path / "view.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    first_res = _result()
    first_res["annotated_image_path"] = str(png)
    first = _write(tools, out, _geometry(tmp_path), first_res)
    assert "annotated_render.png" in first["files"]

    packet = out / "FRAME-PEDAL-BRAKE"
    again = _result(peak=402.93)
    again["annotated_image_path"] = str(packet / "annotated_render.png")
    second = _write(tools, out, _geometry(tmp_path), again)

    assert "annotated_render.png" in second["files"]
    assert (packet / "annotated_render.png").read_bytes() == b"\x89PNG\r\n\x1a\nfirst"
    archive = packet / second["archived_to"].rsplit("/", 1)[1]
    assert (archive / "annotated_render.png").is_file()


def test_overwrite_true_replaces_in_place_and_still_names_the_loss(tmp_path):
    """`overwrite=True` is the documented escape: no archive, but not silent either."""
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    first = _write(tools, out, _geometry(tmp_path), _result(peak=105.98))
    second = _write(
        tools, out, _geometry(tmp_path), _result(peak=402.93), overwrite=True,
    )
    packet = out / "FRAME-PEDAL-BRAKE"
    assert second["archived_to"] is None
    assert [p for p in packet.iterdir() if p.is_dir()] == []
    live = json.loads((packet / "result.json").read_text())
    assert live["summary"]["max_von_mises_mpa"] == 402.93
    prov = json.loads((packet / "provenance.json").read_text())
    assert prov["superseded_from"]["mode"] == "overwrite_in_place"
    assert prov["superseded_from"]["result_sha256"] == first["result_sha256"]


def test_verify_packet_passes_a_freshly_written_packet(tmp_path):
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    written = _write(tools, out, _geometry(tmp_path), _result())
    rep = _call(tools, "verify_packet", folder=written["packet_dir"])
    failed = [c for c in rep["checks"] if not c["ok"]]
    assert rep["consistent"] is True, failed
    assert rep["ok"] is True
    assert rep["problems"] == []
    assert {c["check"] for c in rep["checks"]} >= {
        "core_files_readable",
        "case_id_agrees",
        "input_hash_agrees",
        "engine_build_id_agrees",
        "provenance_cites_this_result",
        "convergence_cites_this_result",
        "reduction_fields_present",
        "checklist_is_the_runbook_list",
    }
    # The judgement items are still open — that is the release gate, not a consistency
    # failure, so it must not turn this check red.
    assert rep["open_checklist_items"]


def test_verify_packet_separates_unstamped_from_contradictory(tmp_path):
    """A packet written before the hash stamp is UNVERIFIABLE, not inconsistent.

    Every packet the mission released predates it, so lumping the two together would
    make the check cry wolf on all of them and get ignored — and the remedy differs:
    re-stamp versus do-not-cite.
    """
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    written = _write(tools, out, _geometry(tmp_path), _result())
    packet = out / "FRAME-PEDAL-BRAKE"

    prov = json.loads((packet / "provenance.json").read_text())
    prov.pop("result_sha256")
    (packet / "provenance.json").write_text(json.dumps(prov, indent=2), encoding="utf-8")
    text = (packet / "convergence.md").read_text()
    (packet / "convergence.md").write_text(
        "\n".join(l for l in text.splitlines() if "result_sha256" not in l),
        encoding="utf-8",
    )

    rep = _call(tools, "verify_packet", folder=written["packet_dir"])
    assert rep["consistent"] is True, rep["problems"]
    assert rep["ok"] is False
    assert rep["reason"] == "packet_unverifiable"
    assert len(rep["unverifiable"]) == 2
    assert rep["problems"] == []
    assert "Re-run sim_evidence_packet" in rep["note"]


def test_verify_packet_flags_a_result_swapped_under_the_convergence_table(tmp_path):
    """The 2026-09-05 shape: one file replaced, the rest left describing another run."""
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    written = _write(tools, out, _geometry(tmp_path), _result(peak=105.98))
    packet = out / "FRAME-PEDAL-BRAKE"

    swapped = _result(peak=402.93)
    (packet / "result.json").write_text(json.dumps(swapped, indent=2), encoding="utf-8")

    rep = _call(tools, "verify_packet", folder=written["packet_dir"])
    assert rep["ok"] is False
    assert rep["consistent"] is False
    assert rep["reason"] == "packet_inconsistent"
    failed = {c["check"] for c in rep["checks"] if not c["ok"]}
    assert "convergence_cites_this_result" in failed
    assert "provenance_cites_this_result" in failed
    assert any("DIFFERENT runs" in p for p in rep["problems"])


def test_verify_packet_flags_disagreeing_engine_builds(tmp_path):
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    written = _write(tools, out, _geometry(tmp_path), _result(build="eng-A"))
    packet = out / "FRAME-PEDAL-BRAKE"
    prov = json.loads((packet / "provenance.json").read_text())
    prov["solver_provenance"]["engine_build_id"] = "eng-B"
    (packet / "provenance.json").write_text(json.dumps(prov, indent=2), encoding="utf-8")

    rep = _call(tools, "verify_packet", folder=written["packet_dir"])
    assert rep["consistent"] is False
    failed = {c["check"] for c in rep["checks"] if not c["ok"]}
    assert "engine_build_id_agrees" in failed


def test_verify_packet_flags_a_result_with_no_reduction_disclosure(tmp_path):
    """Without the disclosure the reader cannot tell a masked peak from the field's max."""
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    stale = _result()
    for key in ("max_von_mises_reduction", "max_von_mises_raw_mpa", "min_fos_raw"):
        stale["summary"].pop(key)
    written = _write(tools, out, _geometry(tmp_path), stale)
    rep = _call(tools, "verify_packet", folder=written["packet_dir"])
    assert rep["consistent"] is False
    row = next(c for c in rep["checks"] if c["check"] == "reduction_fields_present")
    assert row["ok"] is False
    assert "max_von_mises_reduction" in row["detail"]


def test_verify_packet_flags_a_missing_geometry_hash(tmp_path):
    """A packet whose solved revision is unpinned cannot be tied to a geometry file."""
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    written = _call(
        tools,
        "sim_evidence_packet",
        case_id="FRAME-PEDAL-BRAKE",
        requirement_id="REQ-7",
        out_dir=str(out),
        study_result=_result(),
    )
    rep = _call(tools, "verify_packet", folder=written["packet_dir"])
    assert rep["consistent"] is False
    row = next(c for c in rep["checks"] if c["check"] == "input_hash_agrees")
    assert row["ok"] is False


def test_verify_packet_flags_a_vanished_vtu(tmp_path):
    """The field a released number came from must still be where provenance says."""
    tools = _tools()
    out = tmp_path / "Simulation_Output"
    vtu = tmp_path / "case.vtu"
    vtu.write_text("<VTKFile/>", encoding="utf-8")
    result = _result()
    result["vtu_path"] = str(vtu)
    written = _write(tools, out, _geometry(tmp_path), result)
    assert _call(tools, "verify_packet", folder=written["packet_dir"])["consistent"] is True

    vtu.unlink()
    rep = _call(tools, "verify_packet", folder=written["packet_dir"])
    assert rep["consistent"] is False
    row = next(c for c in rep["checks"] if c["check"] == "vtu_matches_provenance")
    assert row["ok"] is False


def test_verify_packet_refuses_a_folder_that_is_not_there(tmp_path):
    tools = _tools()
    rep = _call(tools, "verify_packet", folder=str(tmp_path / "nope"))
    assert rep["ok"] is False
    assert rep["reason"] == "packet_not_found"


def test_a_solve_never_overwrites_an_existing_vtu(tmp_path):
    """`_archive_existing_vtu` is what stands between a re-solve and the earlier field."""
    from services.mcp.cad_sim_tools import _archive_existing_vtu

    vtu = tmp_path / "FRAME-PEDAL-BRAKE.vtu"
    vtu.write_text("<VTKFile>first field</VTKFile>", encoding="utf-8")
    notes: list = []
    _archive_existing_vtu(str(vtu), notes)

    assert not vtu.exists(), "the path must be free for the solver to write"
    archives = [p for p in tmp_path.iterdir() if p.is_dir()
                and p.name.startswith("superseded_")]
    assert len(archives) == 1
    kept = archives[0] / "FRAME-PEDAL-BRAKE.vtu"
    assert kept.read_text() == "<VTKFile>first field</VTKFile>"
    assert notes and "was NOT overwritten" in notes[0]
    assert str(archives[0]) in notes[0]

    # Nothing there: nothing said, nothing created.
    notes2: list = []
    _archive_existing_vtu(str(tmp_path / "absent.vtu"), notes2)
    assert notes2 == []


def _stub_polymesh(tmp_path, tag: str):
    """A fake `polymesh` that writes whatever -o names, so the archive rule is testable
    without the real cross-check solver (which is a multi-minute solve)."""
    cli = tmp_path / "polymesh_stub.py"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "a = sys.argv\n"
        "out = a[a.index('-o') + 1] if '-o' in a else None\n"
        f"tag = {tag!r}\n"
        "if out:\n"
        "    open(out, 'w').write('<VTKFile>' + tag + '</VTKFile>')\n"
        "print('stub ' + tag)\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return cli


def test_polymesh_cli_archives_the_previous_cross_check(tmp_path, monkeypatch):
    """out_dir is normally the case's packet folder, and a cross-check field is evidence."""
    tools = _tools()
    case = tmp_path / "WHEEL-RADIAL"
    step = _geometry(tmp_path)
    args = dict(
        action="mesh",
        step_path=str(step),
        out_dir=str(case),
        h_m=0.003,
        fix_box=[0, 0, 0, 0.1, 0.05, 0.005],
        load_box=[0, 0, 0.015, 0.1, 0.05, 0.02],
    )

    monkeypatch.setenv("CHUDWARE_POLYMESH_CLI", str(_stub_polymesh(tmp_path, "first")))
    first = _call(tools, "polymesh_cli", **args)
    assert first["ok"] is True, first
    assert first["archived_to"] is None
    assert (case / "WHEEL-RADIAL_mesh.vtu").read_text() == "<VTKFile>first</VTKFile>"

    monkeypatch.setenv("CHUDWARE_POLYMESH_CLI", str(_stub_polymesh(tmp_path, "second")))
    second = _call(tools, "polymesh_cli", **args)
    assert second["ok"] is True, second
    archive = case / second["archived_to"].rsplit("/", 1)[1]
    assert (archive / "WHEEL-RADIAL_mesh.vtu").read_text() == "<VTKFile>first</VTKFile>"
    assert (archive / "mesh.log").is_file(), "the previous run's log travels with its .vtu"
    assert (case / "WHEEL-RADIAL_mesh.vtu").read_text() == "<VTKFile>second</VTKFile>"

    third = _call(tools, "polymesh_cli", overwrite=True, **args)
    assert third["archived_to"] is None
    assert len([p for p in case.iterdir() if p.is_dir()]) == 1, "overwrite adds no archive"


def test_polymesh_cli_cannot_pass_on_a_leftover_artifact(tmp_path, monkeypatch):
    """A stale .vtu from an earlier run must not satisfy the did-it-write check."""
    tools = _tools()
    case = tmp_path / "WHEEL-RADIAL"
    case.mkdir()
    (case / "WHEEL-RADIAL_mesh.vtu").write_text("<VTKFile>stale</VTKFile>", encoding="utf-8")
    silent = tmp_path / "silent.py"
    silent.write_text("#!/usr/bin/env python3\nprint('wrote nothing')\n", encoding="utf-8")
    silent.chmod(0o755)
    monkeypatch.setenv("CHUDWARE_POLYMESH_CLI", str(silent))

    rep = _call(
        tools,
        "polymesh_cli",
        action="mesh",
        step_path=str(_geometry(tmp_path)),
        out_dir=str(case),
        h_m=0.003,
        fix_box=[0, 0, 0, 0.1, 0.05, 0.005],
        load_box=[0, 0, 0.015, 0.1, 0.05, 0.02],
    )
    assert rep["ok"] is False
    assert "did not write" in rep["error"]
    assert rep["archived_to"], "the stale artifact was archived, not left to masquerade"


def test_polymesh_cli_unavailable_moves_nothing(tmp_path, monkeypatch):
    tools = _tools()
    case = tmp_path / "WHEEL-RADIAL"
    case.mkdir()
    (case / "WHEEL-RADIAL_mesh.vtu").write_text("<VTKFile>kept</VTKFile>", encoding="utf-8")
    monkeypatch.setenv("CHUDWARE_POLYMESH_CLI", str(tmp_path / "absent"))
    monkeypatch.setenv("CHUDWARE_HOSTD_BIN", str(tmp_path / "absent-hostd"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    rep = _call(
        tools,
        "polymesh_cli",
        action="check",
        step_path=str(_geometry(tmp_path)),
        out_dir=str(case),
    )
    assert rep["ok"] is False
    assert rep["reason"] == "polymesh_cli_unavailable"
    assert (case / "WHEEL-RADIAL_mesh.vtu").read_text() == "<VTKFile>kept</VTKFile>"
    assert [p for p in case.iterdir() if p.is_dir()] == []
