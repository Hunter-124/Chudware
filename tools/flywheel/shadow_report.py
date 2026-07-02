"""Shadow comparison report generator.

Reads a JSONL shadow log (produced by services/agent/shadow.ShadowExecutor) and
produces a human-readable comparison table: incumbent vs challenger verifier scores,
latency, cost, and accept-rate.

Usage:
    python tools/flywheel/shadow_report.py shadow_log.jsonl [--min-calls 100]

or as a library:
    from tools.flywheel.shadow_report import generate_report, load_shadow_log
    records = load_shadow_log(Path("shadow_log.jsonl"))
    print(generate_report(records))
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@dataclass
class ShadowRow:
    artifact_id: str
    insertion_point: str
    prompt_hash: str
    incumbent_provider: str
    incumbent_model: str
    incumbent_latency_ms: float
    incumbent_scores: dict[str, float]
    incumbent_accepted: bool
    challenger_provider: str
    challenger_model: str
    challenger_latency_ms: float
    challenger_scores: dict[str, float]
    challenger_accepted: bool
    mode: str = "replay"


def load_shadow_log(path: Path) -> list[ShadowRow]:
    """Load all ShadowRecord entries from a JSONL shadow log."""
    rows: list[ShadowRow] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"  [warn] line {lineno}: bad JSON — {exc}", file=sys.stderr)
                continue
            try:
                rows.append(ShadowRow(
                    artifact_id=str(d.get("artifact_id", "")),
                    insertion_point=str(d.get("insertion_point", "IP-?")),
                    prompt_hash=str(d.get("prompt_hash", "")),
                    incumbent_provider=str(d.get("incumbent_provider", "unknown")),
                    incumbent_model=str(d.get("incumbent_model", "unknown")),
                    incumbent_latency_ms=float(d.get("incumbent_latency_ms") or 0.0),
                    incumbent_scores=dict(d.get("incumbent_scores") or {}),
                    incumbent_accepted=bool(d.get("incumbent_accepted", True)),
                    challenger_provider=str(d.get("challenger_provider", "unknown")),
                    challenger_model=str(d.get("challenger_model", "unknown")),
                    challenger_latency_ms=float(d.get("challenger_latency_ms") or 0.0),
                    challenger_scores=dict(d.get("challenger_scores") or {}),
                    challenger_accepted=bool(d.get("challenger_accepted", True)),
                    mode=str(d.get("mode", "replay")),
                ))
            except (TypeError, ValueError) as exc:
                print(f"  [warn] line {lineno}: parse error — {exc}", file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class _Agg:
    latencies: list[float] = field(default_factory=list)
    accepts: list[bool] = field(default_factory=list)
    # score_name -> list[float]
    scores: dict[str, list[float]] = field(default_factory=dict)

    def add(self, latency_ms: float, accepted: bool, scores: dict[str, float]) -> None:
        self.latencies.append(latency_ms)
        self.accepts.append(accepted)
        for k, v in scores.items():
            self.scores.setdefault(k, []).append(v)

    def mean_latency_ms(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    def p95_latency_ms(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        idx = max(0, int(len(s) * 0.95) - 1)
        return s[idx]

    def accept_rate(self) -> float:
        if not self.accepts:
            return 0.0
        return sum(self.accepts) / len(self.accepts)

    def mean_scores(self) -> dict[str, float]:
        return {k: statistics.mean(v) for k, v in self.scores.items() if v}

    def n(self) -> int:
        return len(self.latencies)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    rows: list[ShadowRow],
    *,
    min_calls: int = 0,
    title: str = "Shadow Comparison Report",
) -> str:
    """Generate a plain-text comparison report from *rows*."""
    if not rows:
        return f"{title}\n{'='*len(title)}\n\nNo shadow records loaded.\n"

    n = len(rows)
    inc_agg = _Agg()
    ch_agg = _Agg()
    by_ip: dict[str, tuple[_Agg, _Agg]] = {}

    for row in rows:
        inc_agg.add(row.incumbent_latency_ms, row.incumbent_accepted, row.incumbent_scores)
        ch_agg.add(row.challenger_latency_ms, row.challenger_accepted, row.challenger_scores)

        if row.insertion_point not in by_ip:
            by_ip[row.insertion_point] = (_Agg(), _Agg())
        by_ip[row.insertion_point][0].add(
            row.incumbent_latency_ms, row.incumbent_accepted, row.incumbent_scores)
        by_ip[row.insertion_point][1].add(
            row.challenger_latency_ms, row.challenger_accepted, row.challenger_scores)

    # Header info
    inc_id = f"{rows[0].incumbent_provider}/{rows[0].incumbent_model}"
    ch_id = f"{rows[0].challenger_provider}/{rows[0].challenger_model}"
    modes = set(r.mode for r in rows)

    lines: list[str] = []
    lines.append(f"\n{'='*72}")
    lines.append(f"  {title}")
    lines.append(f"  N = {n} calls  |  mode: {', '.join(sorted(modes))}")
    lines.append(f"  Incumbent : {inc_id}")
    lines.append(f"  Challenger: {ch_id}")
    lines.append(f"{'='*72}\n")

    if min_calls and n < min_calls:
        lines.append(
            f"  [WARNING] Only {n} records — fewer than the required minimum of "
            f"{min_calls}. Results may not be statistically reliable.\n"
        )

    # Overall summary table
    lines.append(_section("OVERALL SUMMARY"))
    lines.append(_row_header())
    lines.append(_table_row("Incumbent",  inc_agg))
    lines.append(_table_row("Challenger", ch_agg))
    lines.append("")

    # Latency delta
    lat_delta = ch_agg.mean_latency_ms() - inc_agg.mean_latency_ms()
    lat_pct = (lat_delta / inc_agg.mean_latency_ms() * 100) if inc_agg.mean_latency_ms() else 0.0
    sign = "+" if lat_delta >= 0 else ""
    lines.append(f"  Latency delta:    {sign}{lat_delta:.1f} ms  ({sign}{lat_pct:.1f}%)")

    # Accept-rate delta
    ar_delta = ch_agg.accept_rate() - inc_agg.accept_rate()
    sign = "+" if ar_delta >= 0 else ""
    lines.append(f"  Accept-rate delta: {sign}{ar_delta:.3f}  ({sign}{ar_delta*100:.1f} pp)")
    lines.append("")

    # Per-score breakdown
    all_scores = sorted(
        set(list(inc_agg.mean_scores()) + list(ch_agg.mean_scores()))
    )
    if all_scores:
        lines.append(_section("VERIFIER SCORES"))
        col_w = max(len(s) for s in all_scores) + 2
        header = f"  {'Score':<{col_w}} {'Incumbent':>12} {'Challenger':>12} {'Delta':>10}"
        lines.append(header)
        lines.append("  " + "-" * (col_w + 38))
        inc_s = inc_agg.mean_scores()
        ch_s = ch_agg.mean_scores()
        for s in all_scores:
            iv = inc_s.get(s, float("nan"))
            cv = ch_s.get(s, float("nan"))
            delta = cv - iv if (iv == iv and cv == cv) else float("nan")
            sign = "+" if delta >= 0 else ""
            lines.append(
                f"  {s:<{col_w}} {iv:>12.4f} {cv:>12.4f} {sign}{delta:>9.4f}"
            )
        lines.append("")

    # Per insertion-point breakdown
    if len(by_ip) > 1:
        lines.append(_section("BY INSERTION POINT"))
        for ip in sorted(by_ip):
            ia, ca = by_ip[ip]
            lines.append(f"  {ip}  (n={ia.n()})")
            lines.append(_row_header("    "))
            lines.append(_table_row("Incumbent",  ia, indent="    "))
            lines.append(_table_row("Challenger", ca, indent="    "))
            lines.append("")

    lines.append("=" * 72)
    return "\n".join(lines) + "\n"


def _section(title: str) -> str:
    return f"  {title}\n  {'-'*len(title)}"


def _row_header(indent: str = "  ") -> str:
    return (
        f"{indent}{'':16} {'N':>6} {'Lat-mean':>10} {'Lat-p95':>10} {'Accept%':>9}"
    )


def _table_row(label: str, agg: _Agg, indent: str = "  ") -> str:
    return (
        f"{indent}{label:<16} {agg.n():>6} {agg.mean_latency_ms():>10.1f} "
        f"{agg.p95_latency_ms():>10.1f} {agg.accept_rate()*100:>9.1f}%"
    )


# ---------------------------------------------------------------------------
# Fixture generator (for smoke tests — synthesises ≥100 logged calls)
# ---------------------------------------------------------------------------

def generate_fixture_log(
    path: Path,
    n: int = 120,
    *,
    insertion_point: str = "IP-1",
    incumbent_provider: str = "mock",
    incumbent_model: str = "mock-v1",
    challenger_provider: str = "mock",
    challenger_model: str = "mock-v2",
    seed: int = 42,
) -> None:
    """Write *n* synthetic ShadowRecord JSONL lines to *path* for testing."""
    import random
    rng = random.Random(seed)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            artifact_id = f"{i:04x}{rng.randint(0, 0xffff):04x}"
            prompt_hash = f"{rng.randint(0, 2**64):016x}"
            inc_lat = rng.gauss(1200.0, 200.0)
            ch_lat = rng.gauss(900.0, 150.0)
            inc_score = rng.uniform(0.3, 1.0)
            ch_score = rng.uniform(0.3, 1.0)

            record = {
                "artifact_id": artifact_id,
                "insertion_point": insertion_point,
                "prompt_hash": prompt_hash,
                "incumbent_provider": incumbent_provider,
                "incumbent_model": incumbent_model,
                "incumbent_latency_ms": max(100.0, inc_lat),
                "incumbent_scores": {"verifier": round(inc_score, 4)},
                "incumbent_accepted": inc_score >= 0.5,
                "challenger_provider": challenger_provider,
                "challenger_model": challenger_model,
                "challenger_latency_ms": max(80.0, ch_lat),
                "challenger_scores": {"verifier": round(ch_score, 4)},
                "challenger_accepted": ch_score >= 0.5,
                "mode": "replay",
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Generate a shadow comparison report.")
    parser.add_argument("log", help="Path to JSONL shadow log file")
    parser.add_argument(
        "--min-calls", type=int, default=100,
        help="Warn if fewer than N records (default: 100)"
    )
    parser.add_argument(
        "--title", default="Shadow Comparison Report",
        help="Report title"
    )
    args = parser.parse_args()

    path = Path(args.log)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        sys.exit(1)

    rows = load_shadow_log(path)
    report = generate_report(rows, min_calls=args.min_calls, title=args.title)
    print(report)


if __name__ == "__main__":
    _cli()
