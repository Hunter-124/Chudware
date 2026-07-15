#!/usr/bin/env python3
"""Grade mega-14 primary: axes S/T/U + A–Q regression; emit criterion_*.json + findings inputs."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent


def _events(led: dict) -> list:
    return list(led.get("events") or [])


def _payload(ev: dict) -> dict:
    p = ev.get("payload")
    return p if isinstance(p, dict) else {}


def main() -> int:
    led = json.loads((OUT / "ledger.json").read_text())
    status = json.loads((OUT / "status.json").read_text()) if (OUT / "status.json").exists() else {}
    mission = json.loads((OUT / "mission.json").read_text()) if (OUT / "mission.json").exists() else {}
    events = _events(led)
    state = led.get("state") or {}

    # --- criterion_results ---
    crs = []
    for ev in events:
        if ev.get("kind") == "criterion_result":
            pl = _payload(ev)
            if pl:
                crs.append(pl)
            else:
                # sometimes flat
                crs.append({k: ev.get(k) for k in ("id", "result", "check_tool", "pass_rule", "detail", "evidence") if k in ev or True})
    # prefer payload form
    crs_clean = []
    for ev in events:
        if ev.get("kind") != "criterion_result":
            continue
        pl = _payload(ev)
        if not pl and "id" in ev:
            pl = ev
        if pl.get("id"):
            crs_clean.append(pl)
    crs = crs_clean

    by_id = {}
    for c in crs:
        by_id[str(c.get("id"))] = c

    for acid in ["BATTERY"] + [f"AC{i}" for i in range(1, 10)]:
        c = by_id.get(acid)
        if c:
            (OUT / f"criterion_{acid}.json").write_text(json.dumps(c, indent=2, default=str))
            print(f"wrote {acid} {c.get('result')}")
        else:
            print(f"missing {acid}")

    # --- battery_result ---
    batt = None
    for ev in reversed(events):
        if ev.get("kind") == "battery_result" and (ev.get("actor") == "reviewer" or True):
            pl = _payload(ev)
            if pl:
                # prefer reviewer
                if ev.get("actor") == "reviewer" or batt is None:
                    batt = {"actor": ev.get("actor"), "kind": "battery_result", "payload": pl}
                if ev.get("actor") == "reviewer":
                    break
    if batt:
        (OUT / "battery_result.json").write_text(json.dumps(batt, indent=2, default=str))
        print(
            "battery",
            batt["payload"].get("verdict"),
            "doc",
            batt["payload"].get("doc_id"),
            "nchecks",
            len(batt["payload"].get("checks") or []),
        )

    # --- fw artifact ---
    fw = None
    for ev in events:
        kind = ev.get("kind")
        pl = _payload(ev)
        # ledger artifact events
        if kind == "artifact":
            body = pl or ev
            if any(
                k in json.dumps(body, default=str).lower()
                for k in ("firmware.elf", "fw_build", "elf_path", "project_id")
            ):
                fw = body
        if kind in ("tool_result", "ledger_tool_call", "agent.tool_call") or True:
            # scan tool outcomes for fw_build success
            pass
    # deeper scan: any payload with elf_path/success
    for ev in events:
        blob = json.dumps(ev, default=str)
        if "firmware.elf" in blob or "elf_path" in blob:
            pl = _payload(ev) or ev
            if not fw or "elf_path" in json.dumps(pl, default=str):
                fw = pl if isinstance(pl, dict) else {"raw": pl}
    if fw:
        (OUT / "fw_artifact.json").write_text(json.dumps(fw, indent=2, default=str))
        print("fw_artifact written")

    # --- AC live pass count (N/L) ---
    live = {}
    for i in range(1, 10):
        c = by_id.get(f"AC{i}")
        live[f"AC{i}"] = (c or {}).get("result")
    pass_ids = [k for k, v in live.items() if str(v).upper() == "PASS"]
    skip_ids = [k for k, v in live.items() if str(v).upper() == "SKIP"]
    print("live results", live)
    print("PASS", pass_ids, "count", len(pass_ids))
    print("SKIP", skip_ids)

    # --- O: ECAD board_from_schematic / DRC ---
    tool_names = Counter()
    board_path = {
        "board_from_schematic": 0,
        "ecad_run_drc": 0,
        "ecad_run_erc": 0,
        "ecad_new_schematic": 0,
        "ecad_place_symbol": 0,
        "ecad_simulate": 0,
        "ecad_connect": 0,
        "ecad_add_net": 0,
        "ecad_wire": 0,
        "fw_build": 0,
        "fw_new_project": 0,
        "new_part": 0,
        "add_features": 0,
        "delegate": 0,
        "check_interference": 0,
        "measure_part": 0,
        "verify_battery": 0,
        "ecad_antenna_analyze": 0,
        "ecad_antenna_design": 0,
        "make_report": 0,
        "report_card": 0,
    }
    add_features_ok = 0
    add_features_fail = 0
    new_part_ok = 0
    delegate_n = 0
    spawn_n = 0
    oneshot_ok = 0
    oneshot_miss = 0
    model_assignment = Counter()
    drc_errors = None
    board_drc_ok = False
    board_drc_honest_fail = False

    for ev in events:
        kind = ev.get("kind")
        pl = _payload(ev)
        # tool call shapes vary
        name = (
            pl.get("tool_name")
            or pl.get("tool")
            or ev.get("tool_name")
            or ev.get("tool")
            or ""
        )
        if not name and kind in ("agent.tool_call", "tool_call"):
            # event may be dual-arg style from sink — look in whole event
            name = str(ev.get("arg1", {}).get("tool_name") if isinstance(ev.get("arg1"), dict) else "") or name
        if name:
            tool_names[name] += 1
            if name in board_path:
                board_path[name] += 1
            if name == "add_features":
                ok = pl.get("ok")
                if ok is None and isinstance(ev.get("arg1"), dict):
                    ok = ev["arg1"].get("ok")
                if ok is True:
                    add_features_ok += 1
                elif ok is False:
                    add_features_fail += 1
            if name == "new_part" and (pl.get("ok") is True or (isinstance(ev.get("arg1"), dict) and ev["arg1"].get("ok") is True)):
                new_part_ok += 1
            if name == "delegate":
                delegate_n += 1
            if name == "ecad_run_drc":
                # try to read errors
                res = pl.get("result") or pl
                if isinstance(ev.get("arg1"), dict):
                    res = ev["arg1"].get("result") or res
                # search nested
                blob = json.dumps(pl if pl else ev, default=str)
                m = re.search(r'"errors"\s*:\s*(\d+)', blob)
                if m:
                    drc_errors = int(m.group(1))
                if '"ok": true' in blob.lower() or '"ok":true' in blob.lower():
                    if drc_errors == 0 or (drc_errors is None and "errors" not in blob):
                        board_drc_ok = True
                if pl.get("ok") is False or (isinstance(ev.get("arg1"), dict) and ev["arg1"].get("ok") is False):
                    board_drc_honest_fail = True

    # also scan events.jsonl dual-arg form
    ev_path = OUT / "events.jsonl"
    if ev_path.exists():
        for line in ev_path.open():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            a0 = rec.get("arg0")
            a1 = rec.get("arg1") if isinstance(rec.get("arg1"), dict) else {}
            if a0 == "agent.tool_call" or rec.get("kind") == "agent.tool_call":
                name = a1.get("tool_name") or a1.get("tool") or ""
                if name:
                    tool_names[name] += 1
                    if name in board_path:
                        board_path[name] += 1
                    ok = a1.get("ok")
                    if name == "add_features":
                        if ok is True:
                            add_features_ok += 1
                        elif ok is False:
                            add_features_fail += 1
                    if name == "new_part" and ok is True:
                        new_part_ok += 1
                    if name == "delegate":
                        delegate_n += 1
                    if name == "ecad_run_drc":
                        blob = json.dumps(a1, default=str)
                        m = re.search(r'"errors"\s*:\s*(\d+)', blob)
                        if m:
                            drc_errors = int(m.group(1))
                        if ok is True and (drc_errors == 0 or drc_errors is None):
                            board_drc_ok = True
                        if ok is False:
                            board_drc_honest_fail = True
            if a0 == "agent.spawn" or "spawn" in str(a0):
                spawn_n += 1
            if "leaf_oneshot_ok" in json.dumps(rec, default=str):
                oneshot_ok += 1
            if "leaf_oneshot_miss" in json.dumps(rec, default=str):
                oneshot_miss += 1
            if a0 == "model_assignment" or rec.get("kind") == "model_assignment":
                prov = (a1 or rec).get("provider") or (a1 or rec).get("pick_provider")
                if prov:
                    model_assignment[str(prov)] += 1
            # spawn detection
            if a0 in ("agent.spawn", "spawn", "delegation.spawn") or (
                isinstance(a1, dict) and a1.get("event") == "spawn"
            ):
                spawn_n += 1

    # scan ledger for oneshot / model_assignment / spawn
    for ev in events:
        blob = json.dumps(ev, default=str)
        if "leaf_oneshot_ok" in blob:
            oneshot_ok += 1
        if "leaf_oneshot_miss" in blob:
            oneshot_miss += 1
        if ev.get("kind") in ("spawn", "agent.spawn", "delegation.spawn") or "spawned" in blob[:200]:
            if "spawn" in (ev.get("kind") or "") or "worker" in blob and "budget_tokens" in blob:
                spawn_n += 1
        if ev.get("kind") == "model_assignment":
            pl = _payload(ev) or ev
            prov = pl.get("provider") or pl.get("pick_provider")
            if prov:
                model_assignment[str(prov)] += 1

    # O axis
    o_pass = False
    o_detail = ""
    if board_path["board_from_schematic"] > 0 and board_path["ecad_run_drc"] > 0:
        if drc_errors == 0 or board_drc_ok:
            o_pass = True
            o_detail = f"board_from_schematic + ecad_run_drc errors=={drc_errors}"
        elif board_drc_honest_fail:
            o_pass = False
            o_detail = "board_from_schematic + DRC ran with honest FAIL (errors!=0 or ok=false)"
        else:
            o_detail = f"board+drc calls present; errors={drc_errors}"
    else:
        o_detail = (
            f"board_from_schematic={board_path['board_from_schematic']} "
            f"ecad_run_drc={board_path['ecad_run_drc']} "
            f"schematic={board_path['ecad_new_schematic']}"
        )

    # N axis (regression): live AC pass >= 4 with AC3+AC6 held + at least AC7 and one of AC2/AC4/AC5
    n_pass = len(pass_ids) >= 4
    held_36 = "AC3" in pass_ids and "AC6" in pass_ids
    ac7_pass = "AC7" in pass_ids
    one_of_245 = any(x in pass_ids for x in ("AC2", "AC4", "AC5"))
    n_contract = n_pass and held_36 and ac7_pass and one_of_245

    # P axis: board_from_schematic + ecad_run_drc both executed (DRC errors==0 or honest FAIL)
    p_executed = board_path["board_from_schematic"] > 0 and board_path["ecad_run_drc"] > 0
    p_pass = p_executed and (drc_errors == 0 or board_drc_ok or board_drc_honest_fail)
    # Q axis: live AC pass count >= 6
    q_pass = len(pass_ids) >= 6
    # R axis: AC5 SPICE against wired netlist (not empty-netlist error)
    ac5 = by_id.get("AC5") or {}
    ac5_detail = str(ac5.get("detail") or "") + " " + str(ac5.get("evidence") or "")
    empty_netlist = any(
        s in ac5_detail.lower()
        for s in (
            "nothing to simulate",
            "no simulatable",
            "empty netlist",
            "no devices",
            "netlist has no",
        )
    )
    r_pass = str(ac5.get("result") or "").upper() == "PASS" or (
        str(ac5.get("result") or "").upper() == "FAIL" and not empty_netlist and bool(ac5)
    )
    # count connect/wire tools if present
    for extra in ("ecad_connect", "ecad_add_net", "ecad_wire", "ecad_place_wire"):
        if extra not in board_path:
            board_path[extra] = tool_names.get(extra, 0)

    # S axis: same as P upgrade — board_from_schematic + ecad_run_drc executed live
    s_pass = p_pass
    s_executed = p_executed
    # T axis: same as R upgrade — AC5 wired SPICE
    t_pass = r_pass
    # U axis: AC1 battery PASS, or per-check diagnosis when FAIL
    ac1 = by_id.get("AC1") or {}
    ac1_result = str(ac1.get("result") or "").upper()
    batt_payload = (batt or {}).get("payload") or {}
    batt_checks = list(batt_payload.get("checks") or [])
    ac1_fail_checks = []
    for ch in batt_checks:
        if not isinstance(ch, dict):
            continue
        v = str(ch.get("verdict") or ch.get("result") or ch.get("status") or "").upper()
        if v in ("FAIL", "ERROR", "ZERO", "SOFT_FAIL"):
            ac1_fail_checks.append({
                "name": ch.get("name") or ch.get("check") or ch.get("id"),
                "verdict": v,
                "detail": str(ch.get("detail") or ch.get("message") or ch.get("evidence") or "")[:300],
                "evidence": str(ch.get("evidence") or ch.get("geometry") or "")[:300],
            })
        elif v in ("WARN",) and any(
            k in str(ch.get("name") or ch.get("check") or "").lower()
            for k in ("sketch", "dim", "coverage", "measure", "ecad")
        ):
            ac1_fail_checks.append({
                "name": ch.get("name") or ch.get("check") or ch.get("id"),
                "verdict": v,
                "detail": str(ch.get("detail") or ch.get("message") or "")[:300],
            })
    # also mine AC1 criterion detail for soft zeros language
    ac1_detail = str(ac1.get("detail") or "") + " " + str(ac1.get("evidence") or "")
    u_pass = ac1_result == "PASS"
    u_diagnosis = {
        "ac1_result": ac1_result or None,
        "battery_verdict": batt_payload.get("verdict"),
        "battery_doc_id": batt_payload.get("doc_id"),
        "n_checks": len(batt_checks),
        "failing_or_soft_checks": ac1_fail_checks,
        "ac1_detail_snip": ac1_detail[:500],
        "contract": "AC1 PASS, or FAIL with per-check diagnosis (which checks, geometry evidence, brief next-step)",
    }
    # Fix8 live evidence: ecad_chain_gate / tool_denied events
    chain_gate_n = 0
    tool_denied_ecad_n = 0
    for ev in events:
        pl = _payload(ev) or {}
        blob = json.dumps(ev, default=str)
        if pl.get("event") == "ecad_chain_gate" or '"event": "ecad_chain_gate"' in blob or '"event":"ecad_chain_gate"' in blob:
            chain_gate_n += 1
        if pl.get("event") == "tool_denied" or "tool_denied" in (ev.get("kind") or ""):
            if "ECAD chain" in blob or "ecad_" in blob:
                tool_denied_ecad_n += 1
    if (OUT / "events.jsonl").exists():
        for line in (OUT / "events.jsonl").open():
            if "ecad_chain_gate" in line:
                chain_gate_n += 1
            if "tool_denied" in line and "ECAD chain" in line:
                tool_denied_ecad_n += 1

    # A–M quick
    stop_reason = mission.get("stop_reason") or (status.get("result") or {}).get("stop_reason")
    terminal_status = mission.get("status") or (status.get("result") or {}).get("status")
    phases = mission.get("phases_completed") or []

    grade = {
        "run_id": status.get("run_id") or mission.get("run_id"),
        "wall_s": mission.get("wall_s") or status.get("wall_s"),
        "terminal_status": terminal_status,
        "stop_reason": stop_reason,
        "phases_completed": phases,
        "live_ac": live,
        "pass_ids": pass_ids,
        "pass_count": len(pass_ids),
        "axes": {
            "N_live_ac_pass_ge_4": {
                "verdict": "PASS" if n_contract else ("PARTIAL" if n_pass else "FAIL"),
                "pass_count": len(pass_ids),
                "held_AC3_AC6": held_36,
                "AC7": ac7_pass,
                "one_of_AC2_4_5": one_of_245,
                "contract": "AC3+AC6 held + AC7 + one of AC2/AC4/AC5 + count>=4",
            },
            "O_ecad_board_drc": {
                "verdict": "PASS" if o_pass else "FAIL",
                "detail": o_detail,
                "counts": {
                    k: board_path[k]
                    for k in (
                        "ecad_new_schematic",
                        "ecad_place_symbol",
                        "board_from_schematic",
                        "ecad_run_drc",
                        "ecad_run_erc",
                        "ecad_simulate",
                    )
                },
                "drc_errors": drc_errors,
            },
            "P_board_from_schematic_and_drc": {
                "verdict": "PASS" if p_pass else ("PARTIAL" if p_executed else "FAIL"),
                "executed": p_executed,
                "board_from_schematic": board_path["board_from_schematic"],
                "ecad_run_drc": board_path["ecad_run_drc"],
                "drc_errors": drc_errors,
                "board_drc_ok": board_drc_ok,
                "board_drc_honest_fail": board_drc_honest_fail,
                "contract": "both tools executed live; DRC errors==0 or honest FAIL",
            },
            "Q_live_ac_pass_ge_6": {
                "verdict": "PASS" if q_pass else "FAIL",
                "pass_count": len(pass_ids),
                "pass_ids": pass_ids,
                "contract": "live AC PASS count >= 6",
            },
            "R_AC5_SPICE_wired_netlist": {
                "verdict": "PASS" if r_pass else "FAIL",
                "ac5_result": (ac5 or {}).get("result"),
                "empty_netlist_error": empty_netlist,
                "detail": ac5_detail[:400],
                "contract": "V(3V3) measured PASS or honest FAIL (not empty-netlist)",
            },
            "S_board_from_schematic_and_drc": {
                "verdict": "PASS" if s_pass else ("PARTIAL" if s_executed else "FAIL"),
                "executed": s_executed,
                "board_from_schematic": board_path["board_from_schematic"],
                "ecad_run_drc": board_path["ecad_run_drc"],
                "drc_errors": drc_errors,
                "board_drc_ok": board_drc_ok,
                "board_drc_honest_fail": board_drc_honest_fail,
                "upgrades": "P",
                "contract": "both tools executed live; DRC errors==0 or honest FAIL (P upgrade)",
            },
            "T_AC5_SPICE_wired_netlist": {
                "verdict": "PASS" if t_pass else "FAIL",
                "ac5_result": (ac5 or {}).get("result"),
                "empty_netlist_error": empty_netlist,
                "wire_tools": {
                    k: board_path.get(k, 0)
                    for k in ("ecad_wire", "ecad_add_power", "ecad_connect", "ecad_add_net")
                },
                "detail": ac5_detail[:400],
                "upgrades": "R",
                "contract": "V(3V3) measured PASS or honest FAIL (not empty-netlist) — R upgrade",
            },
            "U_AC1_battery_or_diagnosis": {
                "verdict": "PASS" if (u_pass or bool(ac1_fail_checks) or bool(ac1_detail.strip())) else "FAIL",
                "ac1_pass": u_pass,
                "diagnosis": u_diagnosis,
                "contract": "AC1 PASS, or FAIL with per-check diagnosis in findings",
            },
            "Fix8_hard_ecad_chain_gate_live": {
                "verdict": "PASS" if (chain_gate_n > 0 or (s_executed and board_path.get("ecad_wire", 0) > 0)) else "UNKNOWN",
                "ecad_chain_gate_events": chain_gate_n,
                "tool_denied_ecad": tool_denied_ecad_n,
                "contract": "live ecad_chain_gate deny events OR wire+board path advanced past place thrash",
            },
            "K_gradeable": {
                "verdict": "PASS" if sum(1 for v in live.values() if v is not None) >= 6 else "FAIL",
                "n_results": sum(1 for v in live.values() if v is not None),
            },
            "L_live_pass_ge_2": {
                "verdict": "PASS" if len(pass_ids) >= 2 else "FAIL",
                "pass_count": len(pass_ids),
            },
            "M_non_cad_artifact": {
                "verdict": "PASS"
                if (fw or board_path["fw_build"] or board_path["ecad_new_schematic"])
                else "FAIL",
                "fw": bool(fw),
                "fw_build_calls": board_path["fw_build"],
                "schematic_calls": board_path["ecad_new_schematic"],
            },
            "A_accepted_ops": {
                "verdict": "PASS" if new_part_ok and add_features_ok > 0 else "FAIL",
                "new_part_ok": new_part_ok,
                "add_features_ok": add_features_ok,
                "add_features_fail": add_features_fail,
            },
            "B_delegate": {
                "verdict": "PASS" if delegate_n > 0 or spawn_n > 0 else "FAIL",
                "delegate": delegate_n,
                "spawn": spawn_n,
            },
            "C_oneshot": {
                "verdict": "PASS" if oneshot_ok > 0 else "UNKNOWN",
                "oneshot_ok": oneshot_ok,
                "oneshot_miss": oneshot_miss,
            },
            "D_terminal": {
                "verdict": "PASS"
                if terminal_status
                and terminal_status not in ("running", "starting", None, "")
                else "FAIL",
                "status": terminal_status,
                "stop_reason": stop_reason,
            },
            "E_battery": {
                "verdict": "PASS" if batt else "FAIL",
                "doc_id": (batt or {}).get("payload", {}).get("doc_id") if batt else None,
                "verdict_batt": (batt or {}).get("payload", {}).get("verdict") if batt else None,
            },
            "F_clean_stop": {
                "verdict": "PASS"
                if stop_reason
                and "blob" not in str(stop_reason).lower()
                and "sqlite" not in str(stop_reason).lower()
                else "FAIL",
                "stop_reason": stop_reason,
            },
            "I_battery_doc_id": {
                "verdict": "PASS"
                if batt and (batt.get("payload") or {}).get("doc_id")
                else "FAIL",
                "doc_id": (batt or {}).get("payload", {}).get("doc_id") if batt else None,
            },
        },
        "tool_counts_top": tool_names.most_common(30),
        "board_path": board_path,
        "model_assignment": dict(model_assignment),
    }
    (OUT / "grade_summary.json").write_text(json.dumps(grade, indent=2, default=str))
    print(json.dumps(grade["axes"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
