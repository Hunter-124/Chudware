"""Simulation EVIDENCE tools — the release packet and the independent-solver cross-check.

Two tools, both about the same problem: a solved number is not a released number.

  sim_evidence_packet(...)  writes one self-contained directory per case (inputs, result,
                            convergence table, hashes, provenance, annotated render) and
                            pre-fills the runbook's evidence checklist, marking only the
                            items the packet itself proves.
  polymesh_cli(...)         drives the Polyhedral-FEA command-line binary directly — the
                            runbook's step 11 "cross-check the critical case in the other
                            solver with identical assumptions", outside the engine's own
                            in-process solver path.

The checklist items are copied VERBATIM from the project's Simulation Runbook (EVIDENCE
CHECKLIST FOR EVERY RELEASED RESULT). Editing their wording here silently un-couples the
packet from the document a reviewer holds, so keep them literal and add machine evidence
in the item's own fields instead.

Nothing here fabricates a satisfied item. An item is `auto_satisfied` only when the value
that closes it is present in the packet; everything else is returned in
`unsatisfied_checklist` with what is still missing, because a checklist that marks itself
complete is exactly the failure mode the runbook exists to prevent.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pathlib import Path
from typing import Optional, Union
import datetime
import hashlib
import json
import os
import shutil
import subprocess

from .cad_common import (
    coerce_json_or_repr,
    require_list,
    require_mapping,
    _dumps,
    _err,
)
from .daemon_bridge import resolve_hostd_bin


# --------------------------------------------------------------------------------- shared

def _utc_now() -> str:
    """The packet timestamp, ISO-8601 UTC with a Z suffix (same shape as provenance)."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256(path: Union[str, Path]) -> Optional[dict]:
    """{path, sha256, bytes} for a real file, or None — a hash of nothing is not evidence."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    size = 0
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return {"path": str(p.resolve()), "sha256": h.hexdigest(), "bytes": size}


def _require_abs_dir(path, arg: str) -> Path:
    """An absolute directory path, created if needed. Relative output is not releasable."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{arg} must be a non-empty absolute directory path.")
    p = Path(path.strip())
    if not p.is_absolute():
        raise ValueError(f"{arg} must be ABSOLUTE, got {str(p)!r}.")
    p.mkdir(parents=True, exist_ok=True)
    return p


# ----------------------------------------------------------------------- evidence packet

#: The runbook's EVIDENCE CHECKLIST FOR EVERY RELEASED RESULT, verbatim and in order.
_CHECKLIST = (
    "Case ID and requirement ID",
    "Geometry path, revision, hash, units, solid count",
    "Solver binary/revision and timestamp",
    "Material E, nu, density, yield, source",
    "Fixture/load faces and annotated image",
    "Load magnitude, direction, distribution, dynamic factor rationale",
    "Mesh family, size, order, element/DOF counts, quality",
    "Three-mesh convergence table or justified adaptive convergence",
    "Applied load vs nodal sum and reaction equilibrium",
    "Max displacement and location",
    "Non-singular monitored stress and location",
    "Peak stress singularity assessment",
    "Factor of safety with explicit allowable",
    "Hand calculation and/or independent solver comparison",
    "Limitations and what remains unverified",
    "VTU/JSON/log/plot artifact paths",
)

#: Keys a study/render result may carry an image path under. render_part and the measure
#: tools each name it differently, and the packet must not miss the annotated view because
#: of a spelling.
_IMAGE_KEYS = (
    "annotated_image_path",
    "annotated_path",
    "image_path",
    "render_path",
    "png_path",
    "path",
    "image",
    "png",
)


def _find_image(result: dict) -> Optional[str]:
    """The first existing image file the study result points at, searched one level deep."""
    candidates: list = []
    for key in _IMAGE_KEYS:
        value = result.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for nested in ("render", "image", "annotated_render", "measure", "screenshot"):
        sub = result.get(nested)
        if isinstance(sub, str):
            candidates.append(sub)
        elif isinstance(sub, dict):
            for key in _IMAGE_KEYS:
                if isinstance(sub.get(key), str):
                    candidates.append(sub[key])
    for cand in candidates:
        p = Path(cand)
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            return str(p)
    return None


def _convergence_table(result: dict) -> list:
    """The convergence rows a result carries, from run_fea_convergence or the adaptive passes."""
    table = result.get("table")
    if isinstance(table, list) and all(isinstance(r, dict) for r in table):
        return table
    passes = result.get("passes")
    if isinstance(passes, list) and all(isinstance(r, dict) for r in passes):
        return passes
    return []


def _convergence_md(case_id: str, result: dict, rows: list) -> str:
    """The convergence table as markdown — the artifact a reviewer reads, not JSON."""
    cols: list = []
    for row in rows:
        for key in row:
            if key not in cols and not isinstance(row[key], (dict, list)):
                cols.append(key)
    lines = [
        f"# Convergence — {case_id}",
        "",
        f"monitor: `{result.get('monitor', 'max_von_mises_mpa')}`  "
        f"tol_pct: `{result.get('tol_pct')}`  "
        f"converged: `{result.get('converged')}`  "
        f"final_delta_pct: `{result.get('final_delta_pct')}`",
        "",
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join("" if row.get(c) is None else str(row.get(c)) for c in cols) + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _checklist_state(
    *,
    case_id: str,
    requirement_id: str,
    result: dict,
    geometry: Optional[dict],
    vtu: Optional[dict],
    material_source: str,
    load_rationale: str,
    notes: str,
    image: Optional[str],
    rows: list,
    artifacts: list,
) -> list:
    """Each runbook item with the evidence the packet actually holds for it.

    `auto_satisfied` is True only where the packet closes the item on its own. Items whose
    remaining part is a human statement (a location, a singularity judgement, an allowable,
    a hand calculation) stay False with the missing piece named — they are the reviewer's
    work, and pre-ticking them would launder the guess the runbook is guarding against.
    """
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    mesh_quality = (
        result.get("mesh_quality") if isinstance(result.get("mesh_quality"), dict) else {}
    )
    reactions = result.get("reactions") if isinstance(result.get("reactions"), dict) else {}
    adapt = result.get("adapt") if isinstance(result.get("adapt"), dict) else {}
    # What the reported peak IS. The solver's summary max_von_mises_mpa skips the two
    # element rings around ideal fixtures (a clamp singularity), so on a case whose
    # fixture faces ARE the feature of interest the reported peak is not the peak
    # there — one released packet quoted 105.98 MPa as "the peak at the keyway
    # bolt-hole edge" while the same solve's field peaked at 402.93 MPa on that edge.
    # Every stress/FoS item below therefore carries the reduction and its unmasked twin.
    reduction = summary.get("max_von_mises_reduction")
    raw_peak = summary.get("max_von_mises_raw_mpa")
    if reduction is None:
        reduction_note = (
            "the result predates the reduction disclosure (no "
            "summary.max_von_mises_reduction), so whether it is masked is UNKNOWN — "
            "re-solve with the current engine before releasing this number"
        )
    elif reduction == "node_averaged":
        reduction_note = "reduction=node_averaged over ALL nodes (nothing excluded)"
    else:
        reduction_note = (
            f"reduction={reduction} — it EXCLUDES "
            f"{summary.get('summary_nodes_excluded')} of "
            f"{summary.get('summary_nodes_total')} nodes (two element rings around the "
            f"fixtures); unmasked peak max_von_mises_raw_mpa={raw_peak} MPa, "
            f"min_fos_raw={summary.get('min_fos_raw')}"
        )

    def item(text: str, ok: bool, evidence: str) -> dict:
        return {"item": text, "auto_satisfied": bool(ok), "evidence": evidence}

    state = [
        item(
            _CHECKLIST[0],
            bool(case_id and requirement_id),
            f"case_id={case_id!r}, requirement_id={requirement_id!r}",
        ),
        item(
            _CHECKLIST[1],
            False,
            (
                f"inputs.json records the geometry path + sha256 "
                f"({geometry['sha256'][:12] + '…' if geometry else 'NO FILE HASHED'}); "
                "revision, units and solid count are not machine-readable from the study "
                "result — state them in notes."
            ),
        ),
        item(
            _CHECKLIST[2],
            bool(provenance.get("solver") and provenance.get("timestamp_utc")),
            (
                f"provenance.json: solver={provenance.get('solver')!r}, "
                f"engine_build_id={provenance.get('engine_build_id')!r}, "
                f"polymesh_commit={provenance.get('polymesh_commit')!r}, "
                f"timestamp_utc={provenance.get('timestamp_utc')!r}"
            )
            if provenance
            else "the study result carries no `provenance` block",
        ),
        item(
            _CHECKLIST[3],
            bool(material_source),
            (
                f"material={result.get('material')!r}, source={material_source!r} "
                "(E/nu/density/yield as solved are in result.json)"
            ),
        ),
        item(
            _CHECKLIST[4],
            bool(image),
            f"annotated render copied into the packet: {Path(image).name}"
            if image
            else "the study result carries no image path — render the fixture/load faces "
            "and re-run the packet with that result",
        ),
        item(
            _CHECKLIST[5],
            bool(load_rationale),
            f"load_rationale={load_rationale!r}" if load_rationale else "load_rationale is empty",
        ),
        item(
            _CHECKLIST[6],
            bool(mesh_quality) and result.get("n_dof") is not None,
            (
                f"mesher={provenance.get('mesher')!r}, order={mesh_quality.get('order')}, "
                f"n_nodes={mesh_quality.get('n_nodes', result.get('n_nodes'))}, "
                f"n_elems={mesh_quality.get('n_elems', result.get('n_elems'))}, "
                f"n_dof={result.get('n_dof')}, aspect_max={mesh_quality.get('aspect_max')}, "
                f"jacobian_min={mesh_quality.get('jacobian_min')}, "
                f"pct_poor={mesh_quality.get('pct_poor')}"
            )
            if mesh_quality
            else "the study result carries no `mesh_quality` block",
        ),
        item(
            _CHECKLIST[7],
            (len(rows) >= 3 and result.get("converged") is True)
            or adapt.get("eta_target_met") is True,
            (
                f"convergence.md holds {len(rows)} mesh rows, converged="
                f"{result.get('converged')}"
                if rows
                else "no convergence table and no adaptive convergence metadata — run "
                "run_fea_convergence over three sizes or run_fea_adaptive"
            ),
        ),
        item(
            _CHECKLIST[8],
            isinstance(reactions.get("equilibrium_error_pct"), (int, float)),
            (
                f"applied_force_n={reactions.get('applied_force_n')}, "
                f"reaction_force_n={reactions.get('reaction_force_n')}, "
                f"equilibrium_error_pct={reactions.get('equilibrium_error_pct')}"
            )
            if reactions
            else "the study result carries no `reactions` block",
        ),
        item(
            _CHECKLIST[9],
            False,
            f"max_disp_mm={summary.get('max_disp_mm')}; its LOCATION is not in the result — "
            "state the face/node.",
        ),
        item(
            _CHECKLIST[10],
            False,
            f"max_von_mises_mpa={summary.get('max_von_mises_mpa')} is a REPORTED peak, "
            f"not a non-singular monitored value ({reduction_note}) — name the monitored "
            "location and its stress, and say which of the two peaks your monitored "
            "value is being compared against.",
        ),
        item(
            _CHECKLIST[11],
            False,
            "a peak that keeps growing with refinement is a singularity: judge it against "
            f"convergence.md and record the assessment. {reduction_note} — the solver "
            "already withheld the fixture-adjacent nodes from the reported peak, so this "
            "assessment is about the peak that REMAINS, and any statement about stress AT "
            "a fixture face must quote the unmasked value.",
        ),
        item(
            _CHECKLIST[12],
            False,
            f"min_fos={summary.get('min_fos')} is reported against the solver's yield and "
            f"is derived from max_von_mises_mpa ({reduction_note}); state the explicit "
            "allowable (yield/SF) you release against, and whether the released FoS is "
            "the masked one (min_fos) or the unmasked one (min_fos_raw).",
        ),
        item(
            _CHECKLIST[13],
            False,
            "record the hand calculation and/or the polymesh_cli cross-check result "
            "(runbook step 11); differences are explained, never averaged.",
        ),
        item(
            _CHECKLIST[14],
            bool(notes),
            f"notes={notes!r}" if notes else "notes is empty — state limitations and what "
            "remains unverified",
        ),
        item(
            _CHECKLIST[15],
            bool(artifacts) and vtu is not None,
            (
                f"packet files: {', '.join(artifacts)}; "
                f"vtu sha256={vtu['sha256'][:12] + '…' if vtu else 'NO VTU'}"
            ),
        ),
    ]
    return state


def _checklist_md(case_id: str, requirement_id: str, state: list) -> str:
    """The checklist as markdown, ticked only where the packet proves the item."""
    lines = [
        f"# Evidence checklist — {case_id} / {requirement_id}",
        "",
        "Items are verbatim from the Simulation Runbook (EVIDENCE CHECKLIST FOR EVERY",
        "RELEASED RESULT). `[x]` means the packet itself carries the evidence quoted",
        "beneath it; `[ ]` is yours to close before release.",
        "",
    ]
    for row in state:
        lines.append(f"- [{'x' if row['auto_satisfied'] else ' '}] {row['item']}")
        lines.append(f"      - {row['evidence']}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- polymesh CLI

#: Wall-clock ceiling for one CLI invocation. A `solve --adapt` on a real frame is a
#: multi-minute job, so the bound is generous — but it IS bounded: an unbounded subprocess
#: wedges the MCP transport, and a wedged transport drops open documents.
_POLYMESH_TIMEOUT_S = 3600.0

_POLYMESH_ACTIONS = ("check", "mesh", "solve", "diag")

#: The staged binary's basename. It is installed beside chudware-hostd/libchudware by
#: core/cmake/ProvisionPolymesh.cmake, so hostd resolution finds the whole family at once.
_POLYMESH_BIN = "polymesh.exe" if os.name == "nt" else "polymesh"


def _resolve_polymesh_cli() -> tuple:
    """(path | None, searched[]) — env override, then beside hostd, then PATH."""
    searched: list = []
    env = os.environ.get("CHUDWARE_POLYMESH_CLI")
    if env:
        searched.append(f"$CHUDWARE_POLYMESH_CLI={env}")
        p = Path(env)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve()), searched
    hostd = resolve_hostd_bin()
    if hostd:
        cand = Path(hostd).parent / _POLYMESH_BIN
        searched.append(str(cand))
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand.resolve()), searched
    else:
        searched.append("<beside chudware-hostd> (no hostd binary resolved)")
    which = shutil.which(_POLYMESH_BIN)
    searched.append(f"PATH ({_POLYMESH_BIN})")
    if which:
        return which, searched
    return None, searched


def _box(value, arg: str) -> list:
    """A six-number axis-aligned box X0 Y0 Z0 X1 Y1 Z1, in the CLI's own units (metres)."""
    vals = require_list(value, arg, "six numbers: X0 Y0 Z0 X1 Y1 Z1 (metres)")
    if len(vals) != 6:
        raise ValueError(f"{arg} needs exactly 6 numbers (X0 Y0 Z0 X1 Y1 Z1), got {len(vals)}.")
    out = [_num(v, f"{arg}[{i}]", positive=False) for i, v in enumerate(vals)]
    for i in range(3):
        if out[i + 3] <= out[i]:
            raise ValueError(
                f"{arg} is degenerate on axis {'XYZ'[i]}: max ({out[i + 3]}) must exceed "
                f"min ({out[i]})."
            )
    return out


def _dir3(value, arg: str) -> list:
    """A three-number direction; zero length cannot orient a load."""
    vals = require_list(value, arg, "three numbers: DX DY DZ")
    if len(vals) != 3:
        raise ValueError(f"{arg} needs exactly 3 numbers (DX DY DZ), got {len(vals)}.")
    out = [_num(v, f"{arg}[{i}]", positive=False) for i, v in enumerate(vals)]
    if not any(out):
        raise ValueError(f"{arg} must not be the zero vector.")
    return out


def _num(value, arg: str, *, positive: bool = True) -> float:
    """A required finite scalar, named in the error — the CLI argv has no defaults."""
    if value is None:
        raise ValueError(f"{arg} is required for this action.")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{arg} must be a finite number, got {value!r}.")
    try:
        v = float(value)
    except ValueError:
        raise ValueError(f"{arg} must be a finite number, got {value!r}.") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{arg} must be a finite number.")
    if positive and v <= 0.0:
        raise ValueError(f"{arg} must be > 0.")
    return v


def _fmt(v: float) -> str:
    """A plain decimal for argv — an exponent spelling ("2e+11") is not worth risking on
    someone else's argument parser when E is routinely 2e11 pascals. A value too small to
    render as a decimal keeps the exponent form rather than becoming a silent zero."""
    text = f"{v:.10g}"
    if "e" not in text and "E" not in text:
        return text
    plain = f"{v:.12f}".rstrip("0").rstrip(".")
    return plain if plain and float(plain) == float(text) else text


def register_sim_evidence_tools(mcp: FastMCP) -> None:
    """Release-evidence packets and the independent Polyhedral-FEA CLI cross-check."""

    @mcp.tool()
    def sim_evidence_packet(
        case_id: str,
        requirement_id: str,
        out_dir: str,
        study_result: Union[dict, str],
        geometry_path: str = "",
        material_source: str = "",
        load_rationale: str = "",
        notes: str = "",
    ) -> str:
        """Write ONE released-result evidence packet and pre-fill the runbook checklist.

        A solved number is not a released number: the runbook requires inputs, commands,
        logs, JSON/VTU, the convergence table, the hand calculation and the limitations to
        travel together. This writes that directory and tells you what is still missing.

        case_id : the load-case id, e.g. "WHEEL-RADIAL-3G". Also the packet's directory name.
        requirement_id : the requirement this case closes.
        out_dir : ABSOLUTE parent directory; the packet is written to <out_dir>/<case_id>/.
        study_result : the FULL result dict/JSON from run_fea_static / run_fea_adaptive /
                   run_fea_convergence / sim_results — pass it verbatim. Its "provenance",
                   "mesh_quality", "reactions", "vtu_path" and (for a convergence run)
                   "table" are what the checklist is scored against.
        geometry_path : the exact STEP/geometry revision that was solved; sha256'd here.
        material_source : where E/nu/density/yield came from (registry row, datasheet, MMPDS
                   table) — a material with no source cannot be released.
        load_rationale : magnitude, direction, distribution and dynamic-factor justification.
        notes : limitations and what remains unverified.

        The stress and FoS checklist items quote the result's OWN reduction disclosure
        (summary.max_von_mises_reduction / max_von_mises_raw_mpa / min_fos_raw): the
        solver's reported peak excludes two element rings around ideal fixtures, so a
        case whose fixture faces are the feature of interest has a higher real peak in
        its own .vtu, and the packet says so instead of letting the reader assume the
        reported number is the field's maximum. A result carrying no reduction key
        predates the disclosure and is flagged as UNKNOWN rather than assumed unmasked.

        Writes inputs.json, result.json, provenance.json, checklist.md, convergence.md
        (when the result carries a table) and a copy of the annotated render when the
        result points at one. Returns {ok, packet_dir, files, unsatisfied_checklist,
        checklist, geometry_sha256, vtu_sha256}. `unsatisfied_checklist` is the release
        gate: it is non-empty until a human closes the judgement items.
        """
        try:
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError("case_id must be a non-empty load-case id.")
            case_id = case_id.strip()
            if any(sep in case_id for sep in ("/", "\\", os.sep)) or case_id in (".", ".."):
                raise ValueError(
                    f"case_id {case_id!r} must be a single directory name, not a path."
                )
            requirement_id = (requirement_id or "").strip()
            result = require_mapping(
                coerce_json_or_repr(study_result),
                "study_result",
                "the full result dict from a run_fea_* / sim_results call",
            )
            packet = _require_abs_dir(out_dir, "out_dir") / case_id
            packet.mkdir(parents=True, exist_ok=True)

            geometry = _sha256(geometry_path) if geometry_path else None
            vtu_path = result.get("vtu_path")
            vtu = _sha256(vtu_path) if isinstance(vtu_path, str) else None
            rows = _convergence_table(result)
            image = _find_image(result)

            files: list = []

            def write(name: str, text: str) -> None:
                (packet / name).write_text(text, encoding="utf-8")
                files.append(name)

            write(
                "inputs.json",
                json.dumps(
                    {
                        "case_id": case_id,
                        "requirement_id": requirement_id,
                        "created_utc": _utc_now(),
                        "geometry": geometry,
                        "geometry_path_arg": geometry_path or None,
                        "material": result.get("material"),
                        "material_source": material_source or None,
                        "load_rationale": load_rationale or None,
                        "notes": notes or None,
                        "doc_id": result.get("doc_id"),
                        "monitor": result.get("monitor"),
                        "tol_pct": result.get("tol_pct"),
                    },
                    indent=2,
                ),
            )
            write("result.json", json.dumps(result, indent=2))
            write(
                "provenance.json",
                json.dumps(
                    {
                        "case_id": case_id,
                        "packet_written_utc": _utc_now(),
                        "solver_provenance": result.get("provenance"),
                        "mesh_quality": result.get("mesh_quality"),
                        "reactions": result.get("reactions"),
                        "geometry_sha256": geometry,
                        "vtu_sha256": vtu,
                    },
                    indent=2,
                ),
            )
            if rows:
                write("convergence.md", _convergence_md(case_id, result, rows))
            if image:
                dest = packet / f"annotated_render{Path(image).suffix.lower()}"
                shutil.copyfile(image, dest)
                files.append(dest.name)

            state = _checklist_state(
                case_id=case_id,
                requirement_id=requirement_id,
                result=result,
                geometry=geometry,
                vtu=vtu,
                material_source=(material_source or "").strip(),
                load_rationale=(load_rationale or "").strip(),
                notes=(notes or "").strip(),
                image=image,
                rows=rows,
                artifacts=list(files),
            )
            write("checklist.md", _checklist_md(case_id, requirement_id, state))

            unsatisfied = [row["item"] for row in state if not row["auto_satisfied"]]
            return _dumps(
                {
                    "ok": True,
                    "packet_dir": str(packet.resolve()),
                    "files": sorted(files),
                    "checklist": state,
                    "unsatisfied_checklist": unsatisfied,
                    "geometry_sha256": geometry["sha256"] if geometry else None,
                    "vtu_sha256": vtu["sha256"] if vtu else None,
                    "note": (
                        f"{len(unsatisfied)} of {len(state)} checklist items are NOT closed "
                        "by the packet itself — they are judgement calls (locations, "
                        "singularity assessment, allowable, hand calculation) and must be "
                        "written into checklist.md before release."
                    ),
                }
            )
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def polymesh_cli(
        action: str,
        step_path: str,
        out_dir: str,
        h_m: Optional[float] = None,
        fix_box: Optional[Union[list, str]] = None,
        load_box: Optional[Union[list, str]] = None,
        load_dir: Optional[Union[list, str]] = None,
        force_n: Optional[float] = None,
        E_pa: Optional[float] = None,
        nu: Optional[float] = None,
        adapt: int = 0,
        eta_target: Optional[float] = None,
        max_mem_gb: Optional[float] = None,
        max_dof: Optional[int] = None,
        max_elems: Optional[int] = None,
        model_units: str = "mm",
        extra_args: Optional[Union[list, str]] = None,
        timeout_s: float = _POLYMESH_TIMEOUT_S,
    ) -> str:
        """Run the Polyhedral-FEA CLI directly — the runbook's INDEPENDENT cross-check lane.

        The engine's run_fea_* tools call the same mesher in-process. This shells out to the
        standalone `polymesh` binary with explicitly stated boxes/loads instead, so a
        critical case can be re-solved outside the engine path and the two answers compared
        (step 11: explain differences, never average them).

        action : "check"  — geometry validity of step_path (no BCs needed)
                 "mesh"   — graded BC-aware mesh -> <out_dir>/<case>_mesh.vtu
                 "solve"  — linear static (optionally adaptive) -> <case>_result.vtu
                 "diag"   — setup diagnostics as JSON -> <case>_diag.json (parsed back into
                            the reply's "json" field)
        step_path : ABSOLUTE path to the exact STEP revision.
        model_units : units the STEP file itself is written in — "mm" (default; every
                   Chudware export) or "m". With "mm" the CLI is invoked with
                   `--scale 0.001`, which converts the loaded geometry to metres BEFORE
                   any check/mesh/solve/diag work. So EVERY value you pass here is
                   METRES in that scaled frame: h_m, fix_box and load_box are metres,
                   i.e. the model's mm coordinates divided by 1000. force_n is newtons;
                   E_pa is PASCALS, not MPa. Pass model_units="m" only for a STEP that
                   is already metre-based; then no scaling is applied.
        out_dir : ABSOLUTE output directory; its own basename is the artifact prefix, so
                   .../Simulation_Output/WHEEL-RADIAL writes WHEEL-RADIAL_mesh.vtu etc.
        h_m : characteristic element size in metres (required except for "check").
        fix_box / load_box : six numbers X0 Y0 Z0 X1 Y1 Z1 (metres, scaled frame)
                   selecting the fixed and loaded regions. FREEZE these before running —
                   the runbook forbids running the template with unfrozen boxes.
        load_dir : DX DY DZ load direction ("solve"/"diag").
        force_n : total applied force in newtons ("solve"/"diag").
        E_pa / nu : Young's modulus in PASCALS and Poisson's ratio ("solve").
        adapt : adaptive pass count ("solve"); 0 leaves the flag off.
        eta_target : adaptive error target ("solve"); requires adapt > 0.
        max_mem_gb / max_dof : solver ceilings ("solve"); omitted when unset.
        max_elems : element-count ceiling passed as --max-elems; the CLI's own built-in
                   ceiling is 589824 elements, so raise this to mesh finer than that.
        extra_args : additional argv strings appended verbatim.
        timeout_s : wall-clock bound, default 3600.

        Returns {ok, argv, exit_code, outputs, json, log_path}. stdout+stderr always land in
        <out_dir>/<action>.log. With no binary anywhere it refuses with
        {ok:false, reason:"polymesh_cli_unavailable", searched:[...]} — an absent
        cross-check solver is a SKIP to be reported, never a passed cross-check.
        """
        try:
            action = (action or "").strip().lower()
            if action not in _POLYMESH_ACTIONS:
                raise ValueError(
                    f"action must be one of {list(_POLYMESH_ACTIONS)}, got {action!r}."
                )
            step = Path(step_path or "")
            if not step.is_absolute():
                raise ValueError("step_path must be an ABSOLUTE path to the STEP revision.")
            if not step.is_file():
                raise ValueError(f"step_path does not exist: {str(step)!r}")
            out = _require_abs_dir(out_dir, "out_dir")
            prefix = out / out.name

            cli, searched = _resolve_polymesh_cli()
            if not cli:
                return _dumps(
                    {
                        "ok": False,
                        "reason": "polymesh_cli_unavailable",
                        "searched": searched,
                        "error": (
                            "no polymesh CLI binary found. Set CHUDWARE_POLYMESH_CLI to it, "
                            "or build the core with Polyhedral-FEA staged beside "
                            "chudware-hostd. This lane is the independent cross-check: "
                            "without it, report the cross-check as NOT performed."
                        ),
                    }
                )

            extra = (
                []
                if extra_args is None
                else [
                    str(a)
                    for a in require_list(extra_args, "extra_args", "a list of argv strings")
                ]
            )
            units = (model_units or "").strip().lower()
            if units not in ("mm", "m"):
                raise ValueError(f'model_units must be "mm" or "m", got {model_units!r}.')
            argv = [cli, action, str(step)]
            if units == "mm":
                # The STEP is millimetre-based; scale it to metres before any work so
                # h_m / fix_box / load_box are genuinely metres.
                argv += ["--scale", "0.001"]
            outputs: list = []
            if action != "check":
                argv += ["--mesher", "graded", "-h", _fmt(_num(h_m, "h_m"))]
                if max_elems is not None:
                    if (
                        isinstance(max_elems, bool)
                        or not isinstance(max_elems, int)
                        or max_elems <= 0
                    ):
                        raise ValueError("max_elems must be a positive integer.")
                    argv += ["--max-elems", str(max_elems)]
            if action == "mesh":
                argv.append("--bc-grade")
            if action != "check":
                argv += ["--fix-box"] + [_fmt(v) for v in _box(fix_box, "fix_box")]
                argv += ["--load-box"] + [_fmt(v) for v in _box(load_box, "load_box")]
            if action == "solve":
                if isinstance(adapt, bool) or not isinstance(adapt, int) or adapt < 0:
                    raise ValueError("adapt must be a non-negative integer pass count.")
                if adapt:
                    argv += ["--adapt", str(adapt)]
                    if eta_target is not None:
                        argv += ["--eta-target", _fmt(_num(eta_target, "eta_target"))]
                elif eta_target is not None:
                    raise ValueError("eta_target only applies with adapt > 0.")
                argv += ["-E", _fmt(_num(E_pa, "E_pa"))]
                poisson = _num(nu, "nu", positive=False)
                if not -1.0 < poisson < 0.5:
                    raise ValueError("nu must lie in (-1, 0.5).")
                argv += ["-nu", _fmt(poisson)]
            if action in ("solve", "diag"):
                argv += ["--load-dir"] + [_fmt(v) for v in _dir3(load_dir, "load_dir")]
                argv += ["--force", _fmt(_num(force_n, "force_n"))]
            if action == "solve":
                if max_mem_gb is not None:
                    argv += ["--max-mem", _fmt(_num(max_mem_gb, "max_mem_gb"))]
                if max_dof is not None:
                    if isinstance(max_dof, bool) or not isinstance(max_dof, int) or max_dof <= 0:
                        raise ValueError("max_dof must be a positive integer.")
                    argv += ["--max-dof", str(max_dof)]
                target = f"{prefix}_result.vtu"
                argv += ["-o", target]
                outputs.append(target)
            elif action == "mesh":
                target = f"{prefix}_mesh.vtu"
                argv += ["-o", target]
                outputs.append(target)
            elif action == "diag":
                target = f"{prefix}_diag.json"
                argv += ["--json", target]
                outputs.append(target)
            argv += extra

            log_path = out / f"{action}.log"
            timeout = _num(timeout_s, "timeout_s")
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(out),
                )
                exit_code = proc.returncode
                captured = (proc.stdout or "") + (proc.stderr or "")
                timed_out = False
            except subprocess.TimeoutExpired as exc:
                exit_code = None
                # TimeoutExpired carries the raw byte buffers even under text=True.
                captured = "".join(
                    part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part
                    for part in (exc.stdout, exc.stderr)
                    if part
                )
                timed_out = True
            log_path.write_text(
                f"$ {' '.join(argv)}\n\n{captured}"
                + ("\n\n*** TIMEOUT after "
                   f"{timeout:.0f}s — the process was killed ***\n" if timed_out else ""),
                encoding="utf-8",
            )
            written = [p for p in outputs if Path(p).is_file()]
            parsed = None
            if action == "diag" and written:
                try:
                    parsed = json.loads(Path(written[0]).read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    parsed = {"parse_error": str(exc)}
            reply = {
                "ok": (not timed_out) and exit_code == 0 and len(written) == len(outputs),
                "action": action,
                "cli": cli,
                "argv": argv,
                "exit_code": exit_code,
                "outputs": written,
                "log_path": str(log_path),
                "tail": captured[-2000:],
            }
            if parsed is not None:
                reply["json"] = parsed
            if timed_out:
                reply["error"] = (
                    f"polymesh {action} exceeded timeout_s={timeout:.0f} and was killed; "
                    "coarsen h_m, lower --adapt, or raise timeout_s."
                )
            elif exit_code != 0:
                reply["error"] = (
                    f"polymesh {action} exited {exit_code}; see {log_path} — the "
                    "cross-check did NOT run."
                )
            elif len(written) != len(outputs):
                missing = [p for p in outputs if p not in written]
                reply["error"] = (
                    f"polymesh {action} reported success but did not write {missing}; "
                    "refusing to report an artifact that does not exist."
                )
            return _dumps(reply)
        except Exception as e:  # noqa: BLE001
            return _err(e)
