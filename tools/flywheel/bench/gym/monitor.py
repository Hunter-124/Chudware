"""Live / offline cycle monitor (WS5).

Tails ``events.jsonl`` and per-cell ``ledger.json`` under a gym cycle dir and
detects tooling-oriented anomalies:

  * repeated tool errors   — same (tool, error) ≥ 3 times (cell-local or cycle)
  * phase stalls           — no new events for >120s while a cell is open
  * guardrail blocks       — hard_guardrail outcome=blocked
  * retry storms           — ≥3 identical tool+args fingerprints in a short window
  * missing-tool requests  — tools_slot unresolved names, or model asks for a tool
                             not in the offered tools_slot

CLI:
  python3 -m tools.flywheel.bench.gym.monitor <cycle> [--follow]
  python3 -m tools.flywheel.bench.gym.monitor tools/flywheel/bench/gym/reports/runs/grok45-1

``<cycle>`` may be a bare cycle id (resolved under reports/runs/) or an absolute/
relative path to a cycle directory.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

_HERE = Path(__file__).resolve().parent
REPORTS_RUNS = _HERE / "reports" / "runs"

STALL_SECONDS = 120.0
REPEAT_ERROR_THRESHOLD = 3
RETRY_STORM_THRESHOLD = 3
POLL_INTERVAL = 2.0


# ---------------------------------------------------------------------------
# Finding records
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    kind: str                       # repeated_tool_error | phase_stall | guardrail_block
                                    # | retry_storm | missing_tool
    severity: str                   # info | warn | error
    detail: str
    cell: str = ""                  # cell dir name, or "" for cycle-level
    evidence_seqs: list[int] = field(default_factory=list)
    count: int = 1
    tool: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def line(self) -> str:
        loc = f"[{self.cell}] " if self.cell else ""
        seqs = f" seqs={self.evidence_seqs}" if self.evidence_seqs else ""
        cnt = f" x{self.count}" if self.count > 1 else ""
        return f"{self.severity.upper():5} {self.kind:22} {loc}{self.detail}{cnt}{seqs}"


# ---------------------------------------------------------------------------
# Path / IO helpers
# ---------------------------------------------------------------------------

def resolve_cycle_dir(cycle: str | Path) -> Path:
    """Resolve a cycle id or path to an absolute cycle directory."""
    p = Path(cycle)
    if p.is_dir():
        return p.resolve()
    cand = REPORTS_RUNS / str(cycle)
    if cand.is_dir():
        return cand.resolve()
    raise FileNotFoundError(
        f"cycle dir not found: {cycle!r} (tried {cand})"
    )


def _parse_ts(ts: Any) -> float | None:
    """Parse ISO timestamp → epoch seconds (UTC). Returns None if unparseable."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if not s:
        return None
    # Accept trailing Z / offset
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _now() -> float:
    return time.time()


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _iter_jsonl(path: Path) -> Iterator[dict]:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            yield obj


def _cell_dirs(cycle_dir: Path) -> list[Path]:
    """Per-cell dirs: subdirs containing ledger.json (skip nested non-cells)."""
    out: list[Path] = []
    if not cycle_dir.is_dir():
        return out
    for child in sorted(cycle_dir.iterdir()):
        if child.is_dir() and (child / "ledger.json").is_file():
            out.append(child)
        elif child.is_dir():
            # one level of nesting (e.g. tuning subtrees)
            for grand in sorted(child.iterdir()):
                if grand.is_dir() and (grand / "ledger.json").is_file():
                    out.append(grand)
    return out


def _tool_error_message(payload: dict) -> str | None:
    """Extract a stable error string from a tool_call payload, if any."""
    if payload.get("error"):
        return str(payload["error"]).strip()[:200]
    res = payload.get("result")
    if res is None:
        return None
    obj = res
    if isinstance(res, str):
        try:
            obj = json.loads(res)
        except Exception:
            # bare string result — only treat as error if looks like one
            low = res.lower()
            if "error" in low or "unknown" in low or "not found" in low:
                return res.strip()[:200]
            return None
    if not isinstance(obj, dict):
        return None
    if obj.get("ok") is False or obj.get("error") or obj.get("reason") in (
        "offline", "rate_limited", "transport",
    ):
        msg = obj.get("error") or obj.get("reason") or obj.get("detail") or "ok=false"
        return str(msg).strip()[:200]
    return None


def _is_tool_call(event: dict) -> tuple[bool, dict]:
    """Normalise scripted tool_call vs agent_turn tool_call."""
    kind = event.get("kind")
    p = event.get("payload") or {}
    if kind == "tool_call":
        return True, p
    if kind == "agent_turn" and p.get("event") == "tool_call":
        return True, p
    return False, p


def _fingerprint(tool: str, args: Any) -> str:
    try:
        args_s = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        args_s = str(args)
    # Cap so storms compare on shape, not huge payloads
    if len(args_s) > 400:
        args_s = args_s[:400]
    return f"{tool}|{args_s}"


# ---------------------------------------------------------------------------
# Detectors (pure over event lists)
# ---------------------------------------------------------------------------

def analyze_ledger_events(
    events: list[dict],
    *,
    cell: str = "",
    stall_seconds: float = STALL_SECONDS,
    now: float | None = None,
    cell_complete: bool = True,
) -> list[Finding]:
    """Run all detectors on one cell's ledger events."""
    findings: list[Finding] = []
    if not events:
        return findings

    now = now if now is not None else _now()

    # --- track tools offered (last tools_slot wins as "current surface") ---
    offered: set[str] = set()
    # also accumulate any tools_slot.rendered seen
    all_offered: set[str] = set()

    error_counts: Counter[tuple[str, str]] = Counter()
    error_seqs: dict[tuple[str, str], list[int]] = defaultdict(list)
    fp_counts: Counter[str] = Counter()
    fp_seqs: dict[str, list[int]] = defaultdict(list)
    missing_tool_hits: dict[str, list[int]] = defaultdict(list)

    last_ts: float | None = None
    last_seq: int | None = None
    phase_hint = ""

    for e in events:
        seq = e.get("seq")
        if isinstance(seq, int):
            last_seq = seq
        ts = _parse_ts(e.get("ts"))
        if ts is not None:
            last_ts = ts

        kind = e.get("kind")
        p = e.get("payload") or {}
        actor = str(e.get("actor") or "")

        # phase hint from stage events / actor labels
        if kind == "stage" and p.get("stage"):
            phase_hint = str(p.get("stage"))
        elif actor and ":" in actor:
            phase_hint = actor.split(":", 1)[0].strip() or phase_hint

        # tools_slot — offered surface + unresolved (missing tools)
        if kind == "agent_turn" and p.get("event") == "tools_slot":
            rendered = p.get("rendered") or []
            if isinstance(rendered, list):
                offered = {str(x) for x in rendered if x}
                all_offered |= offered
            unresolved = p.get("unresolved") or []
            if isinstance(unresolved, list):
                for name in unresolved:
                    if not name:
                        continue
                    name = str(name)
                    missing_tool_hits[name].append(seq if isinstance(seq, int) else -1)
            # omitted is intentional trim — not missing-tool; skip

        # hard guardrail blocks
        if kind == "hard_guardrail" and p.get("outcome") == "blocked":
            findings.append(Finding(
                kind="guardrail_block",
                severity="error",
                detail=(
                    f"hard_guardrail blocked name={p.get('name')!r} "
                    f"reason={p.get('reason')!r}"
                ),
                cell=cell,
                evidence_seqs=[seq] if isinstance(seq, int) else [],
                tool=str(p.get("name") or ""),
                error=str(p.get("reason") or ""),
                extra={"name": p.get("name"), "reason": p.get("reason")},
            ))

        # provider_error as a soft signal (not tool error, but useful)
        if kind == "agent_turn" and p.get("event") == "provider_error":
            findings.append(Finding(
                kind="provider_error",
                severity="warn",
                detail=f"provider_error: {str(p.get('error') or '')[:160]}",
                cell=cell,
                evidence_seqs=[seq] if isinstance(seq, int) else [],
                error=str(p.get("error") or "")[:200],
            ))

        is_tc, call = _is_tool_call(e)
        if not is_tc:
            continue

        tool = str(call.get("tool") or call.get("name") or "?")
        args = call.get("args") if "args" in call else call.get("arguments")
        err = _tool_error_message(call)

        # missing-tool: model called something never offered in any tools_slot
        # (only flag when we have seen at least one tools_slot)
        if all_offered and tool and tool not in all_offered and tool != "?":
            # Some tools are injected outside the slot (system); only flag if
            # the result itself says unknown/not found/not offered.
            if err and any(
                k in err.lower()
                for k in ("unknown tool", "not found", "not offered", "not available",
                          "no such tool", "unknown function")
            ):
                missing_tool_hits[tool].append(seq if isinstance(seq, int) else -1)

        if err:
            key = (tool, err)
            error_counts[key] += 1
            if isinstance(seq, int):
                error_seqs[key].append(seq)

        # retry storm fingerprint (all calls, success or fail — identical retry)
        fp = _fingerprint(tool, args)
        fp_counts[fp] += 1
        if isinstance(seq, int):
            fp_seqs[fp].append(seq)

    # repeated tool errors
    for (tool, err), n in error_counts.items():
        if n >= REPEAT_ERROR_THRESHOLD:
            findings.append(Finding(
                kind="repeated_tool_error",
                severity="error",
                detail=f"tool={tool!r} error={err!r}",
                cell=cell,
                evidence_seqs=error_seqs[(tool, err)][:20],
                count=n,
                tool=tool,
                error=err,
            ))

    # retry storms (identical tool+args ≥ threshold)
    for fp, n in fp_counts.items():
        if n >= RETRY_STORM_THRESHOLD:
            tool = fp.split("|", 1)[0]
            findings.append(Finding(
                kind="retry_storm",
                severity="warn",
                detail=f"identical call repeated: tool={tool!r} fingerprint={fp[:120]!r}",
                cell=cell,
                evidence_seqs=fp_seqs[fp][:20],
                count=n,
                tool=tool,
                extra={"fingerprint": fp[:300]},
            ))

    # missing tools
    for name, seqs in missing_tool_hits.items():
        clean = [s for s in seqs if isinstance(s, int) and s >= 0]
        findings.append(Finding(
            kind="missing_tool",
            severity="warn",
            detail=f"model requested tool not offered / unresolved: {name!r}",
            cell=cell,
            evidence_seqs=clean[:20],
            count=len(seqs),
            tool=name,
        ))

    # phase stall: only if cell looks incomplete (no terminal stage exit / still open)
    if not cell_complete and last_ts is not None:
        gap = now - last_ts
        if gap > stall_seconds:
            findings.append(Finding(
                kind="phase_stall",
                severity="error",
                detail=(
                    f"no events for {gap:.0f}s (threshold {stall_seconds:.0f}s); "
                    f"last_seq={last_seq} phase≈{phase_hint!r}"
                ),
                cell=cell,
                evidence_seqs=[last_seq] if isinstance(last_seq, int) else [],
                extra={"gap_seconds": gap, "phase": phase_hint},
            ))

    return findings


def _cell_looks_complete(cell_dir: Path, events: list[dict]) -> bool:
    """Heuristic: cell finished if graded.json exists or stage exited / mission done."""
    if (cell_dir / "graded.json").is_file():
        return True
    if (cell_dir / "classifications.json").is_file():
        return True
    for e in reversed(events):
        p = e.get("payload") or {}
        if e.get("kind") == "stage" and p.get("exited") not in (None, "None", ""):
            # last stage has an exit — weak signal of progress; not sufficient alone
            pass
        if e.get("kind") == "agent_turn" and p.get("event") == "mission_done":
            return True
    # cycle events.jsonl cell record is stronger; caller may pass complete=True for static
    return False


def analyze_cycle_events(events_jsonl: Path) -> list[Finding]:
    """Lightweight scan of the cycle-level events.jsonl (summary stream)."""
    findings: list[Finding] = []
    for obj in _iter_jsonl(events_jsonl):
        typ = obj.get("type") or obj.get("kind")
        if typ == "gym.cell":
            fcs = obj.get("failure_classes") or []
            for fc in fcs:
                if not isinstance(fc, dict):
                    continue
                # surface grind / execution_stall as monitor-relevant
                if fc.get("failure_class") in ("grind", "execution_stall", "tool_misuse"):
                    findings.append(Finding(
                        kind="cycle_failure_class",
                        severity="info",
                        detail=(
                            f"{obj.get('challenge')}/{obj.get('tier')}/s{obj.get('seed')}: "
                            f"{fc.get('failure_class')}: {fc.get('detail')}"
                        ),
                        cell=(
                            f"{obj.get('challenge')}_{obj.get('tier')}_s{obj.get('seed')}"
                        ),
                        evidence_seqs=list(fc.get("evidence_seqs") or [])[:20],
                        extra={"failure_class": fc.get("failure_class")},
                    ))
    return findings


# ---------------------------------------------------------------------------
# Cycle scan (static + follow)
# ---------------------------------------------------------------------------

@dataclass
class MonitorSnapshot:
    cycle: str
    cycle_dir: str
    findings: list[Finding] = field(default_factory=list)
    cells_scanned: int = 0
    events_scanned: int = 0
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "cycle_dir": self.cycle_dir,
            "cells_scanned": self.cells_scanned,
            "events_scanned": self.events_scanned,
            "generated_at": self.generated_at,
            "findings": [f.to_dict() for f in self.findings],
            "summary": summarize_findings(self.findings),
        }


def summarize_findings(findings: Iterable[Finding]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for f in findings:
        c[f.kind] += 1
    return dict(c)


def scan_cycle(
    cycle_dir: Path,
    *,
    stall_seconds: float = STALL_SECONDS,
    assume_complete: bool | None = None,
    now: float | None = None,
) -> MonitorSnapshot:
    """Full static (or one-shot) scan of a cycle directory."""
    cycle_dir = cycle_dir.resolve()
    now = now if now is not None else _now()
    findings: list[Finding] = []
    cells = _cell_dirs(cycle_dir)
    events_scanned = 0

    # cycle-level stream
    events_path = cycle_dir / "events.jsonl"
    if events_path.is_file():
        cyc_findings = analyze_cycle_events(events_path)
        findings.extend(cyc_findings)

    for cell_dir in cells:
        lg_path = cell_dir / "ledger.json"
        doc = _read_json(lg_path)
        if not isinstance(doc, dict):
            continue
        events = list(doc.get("events") or [])
        events_scanned += len(events)
        if assume_complete is None:
            complete = _cell_looks_complete(cell_dir, events)
        else:
            complete = assume_complete
        findings.extend(analyze_ledger_events(
            events,
            cell=cell_dir.name,
            stall_seconds=stall_seconds,
            now=now,
            cell_complete=complete,
        ))

    # For completed cycles, also surface cycle-wide repeated errors (same tool+error
    # across cells) when per-cell count was under threshold but aggregate ≥ 3.
    _merge_cross_cell_repeats(findings)

    return MonitorSnapshot(
        cycle=cycle_dir.name,
        cycle_dir=str(cycle_dir),
        findings=findings,
        cells_scanned=len(cells),
        events_scanned=events_scanned,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _merge_cross_cell_repeats(findings: list[Finding]) -> None:
    """No-op placeholder: per-cell detector already uses threshold 3.

    Cross-cell aggregation is left to findings.md writers; keep scan pure.
    """
    return


def follow_cycle(
    cycle_dir: Path,
    *,
    stall_seconds: float = STALL_SECONDS,
    poll_interval: float = POLL_INTERVAL,
    max_seconds: float | None = None,
    print_fn=print,
) -> MonitorSnapshot:
    """Poll a running cycle until the cycle is complete (or max_seconds).

    Quiet-exit only fires when every known cell looks complete (graded.json /
    classifications.json) *and* event growth has stopped for ``quiet_exit_after``
    seconds. Mid-cell model pauses no longer abort the follow loop.
    """
    cycle_dir = cycle_dir.resolve()
    cycle_dir.mkdir(parents=True, exist_ok=True)
    start = _now()
    seen: set[tuple] = set()
    last_activity = start
    last_event_count = -1
    quiet_exit_after = max(stall_seconds, 30.0)
    saw_work = False

    print_fn(f"monitor: following {cycle_dir} (stall>{stall_seconds:.0f}s, poll={poll_interval}s)")

    while True:
        snap = scan_cycle(
            cycle_dir,
            stall_seconds=stall_seconds,
            assume_complete=False,  # live: stall detection on
            now=_now(),
        )
        if snap.cells_scanned or snap.events_scanned:
            saw_work = True

        if snap.events_scanned != last_event_count:
            last_event_count = snap.events_scanned
            last_activity = _now()

        for f in snap.findings:
            key = (f.kind, f.cell, f.detail, tuple(f.evidence_seqs[:5]), f.count)
            if key in seen:
                continue
            seen.add(key)
            print_fn(f.line())

        # exit conditions
        elapsed = _now() - start
        if max_seconds is not None and elapsed >= max_seconds:
            print_fn(f"monitor: max_seconds={max_seconds} reached")
            break

        cells = _cell_dirs(cycle_dir)
        all_complete = bool(cells) and all(
            _cell_looks_complete(
                c,
                list((_read_json(c / "ledger.json") or {}).get("events") or []),
            )
            for c in cells
        )
        # also accept cycle-level events.jsonl cycle_summary as done signal
        events_path = cycle_dir / "events.jsonl"
        has_cycle_summary = False
        if events_path.is_file():
            for obj in _iter_jsonl(events_path):
                if (obj.get("type") or obj.get("kind")) == "gym.cycle_summary":
                    has_cycle_summary = True
                    break

        if saw_work and (all_complete or has_cycle_summary) and (
            _now() - last_activity
        ) >= quiet_exit_after:
            final = scan_cycle(cycle_dir, stall_seconds=stall_seconds, assume_complete=True)
            print_fn(
                f"monitor: cycle complete + quiet {quiet_exit_after:.0f}s — done "
                f"({final.cells_scanned} cells, {len(final.findings)} findings)"
            )
            return final

        time.sleep(poll_interval)

    return scan_cycle(cycle_dir, stall_seconds=stall_seconds, assume_complete=True)


def format_report(snap: MonitorSnapshot) -> str:
    lines = [
        f"# Monitor: {snap.cycle}",
        f"",
        f"- cycle_dir: `{snap.cycle_dir}`",
        f"- generated_at: {snap.generated_at}",
        f"- cells_scanned: {snap.cells_scanned}",
        f"- events_scanned: {snap.events_scanned}",
        f"- findings: {len(snap.findings)}",
        f"- by_kind: {summarize_findings(snap.findings)}",
        f"",
        f"## Findings",
        f"",
    ]
    if not snap.findings:
        lines.append("_No anomalies detected._")
    else:
        # group by kind
        by: dict[str, list[Finding]] = defaultdict(list)
        for f in snap.findings:
            by[f.kind].append(f)
        for kind in sorted(by.keys()):
            lines.append(f"### {kind} ({len(by[kind])})")
            lines.append("")
            for f in by[kind]:
                lines.append(f"- **{f.severity}** {f.detail}")
                if f.cell:
                    lines.append(f"  - cell: `{f.cell}`")
                if f.evidence_seqs:
                    lines.append(f"  - evidence_seqs: {f.evidence_seqs}")
                if f.count > 1:
                    lines.append(f"  - count: {f.count}")
            lines.append("")
    return "\n".join(lines) + "\n"


def write_monitor_snapshot(snap: MonitorSnapshot, out_dir: Path | None = None) -> Path:
    """Write monitor.json (+ optional .md) under the cycle dir."""
    out_dir = Path(out_dir) if out_dir else Path(snap.cycle_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jp = out_dir / "monitor.json"
    jp.write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")
    mp = out_dir / "monitor.md"
    mp.write_text(format_report(snap), encoding="utf-8")
    return jp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Tail/scan gym cycle events+ledgers for tooling anomalies (WS5)",
    )
    ap.add_argument(
        "cycle",
        help="cycle id under reports/runs/ or path to a cycle directory",
    )
    ap.add_argument(
        "--follow", action="store_true",
        help="poll until the cycle goes quiet (live monitor)",
    )
    ap.add_argument(
        "--stall-seconds", type=float, default=STALL_SECONDS,
        help=f"phase-stall threshold in seconds (default {STALL_SECONDS:.0f})",
    )
    ap.add_argument(
        "--poll-interval", type=float, default=POLL_INTERVAL,
        help=f"follow poll interval seconds (default {POLL_INTERVAL})",
    )
    ap.add_argument(
        "--max-seconds", type=float, default=None,
        help="follow: hard stop after N seconds",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="print JSON snapshot to stdout instead of text lines",
    )
    ap.add_argument(
        "--write", action="store_true",
        help="write monitor.json + monitor.md into the cycle dir",
    )
    ap.add_argument(
        "--live-stalls", action="store_true",
        help="static mode: treat cells as incomplete so stall detector can fire "
             "(default static assumes completed cells)",
    )
    args = ap.parse_args(argv)

    try:
        cycle_dir = resolve_cycle_dir(args.cycle)
    except FileNotFoundError as exc:
        # --follow may start before the dir exists
        if args.follow:
            p = Path(args.cycle)
            cycle_dir = p if p.is_absolute() or "/" in str(args.cycle) else (REPORTS_RUNS / args.cycle)
            cycle_dir.mkdir(parents=True, exist_ok=True)
        else:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.follow:
        snap = follow_cycle(
            cycle_dir,
            stall_seconds=args.stall_seconds,
            poll_interval=args.poll_interval,
            max_seconds=args.max_seconds,
        )
    else:
        snap = scan_cycle(
            cycle_dir,
            stall_seconds=args.stall_seconds,
            assume_complete=not args.live_stalls,
        )
        if args.json:
            print(json.dumps(snap.to_dict(), indent=2))
        else:
            print(format_report(snap))
            for f in snap.findings:
                print(f.line())

    if args.write or args.follow:
        path = write_monitor_snapshot(snap)
        print(f"wrote {path}", file=sys.stderr)

    if args.follow and args.json:
        print(json.dumps(snap.to_dict(), indent=2))

    # non-zero if any error-severity findings (useful for CI hooks)
    if any(f.severity == "error" for f in snap.findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
