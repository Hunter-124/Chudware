# tools/flywheel — benchmarks and agent-run analysis

Developer tooling behind the desktop app's **Settings > Benchmarks** tab and the AI
workspace leaderboard, plus two reports over recorded agent runs.

## `bench/` — benchmark harness

| Path | Purpose |
|---|---|
| `categories.py` | Benchmark category registry (modeling, ecad, simulation, reveng, parts_research, roundtrip). Launched by the `bench_*` RPCs in `services/agent/worker_rpc.py`. |
| `gym/` | Design gym: graded challenges (`challenges/`, C1–C15), failure taxonomy, per-cycle reports, and diagnostic suites (`diag/`: tool_validity, vision_fix, feature_id, vc_hygiene). |
| `gym/leaderboard.py` | Builds `benchmarks/leaderboard/leaderboard.json` (schema `aicad.leaderboard/1`, `contracts/leaderboard.md`), read by `host/src/ui/leaderboard_ui.h`. |
| `benches/regen_fix.py` | Regen-fix bench: scores repairs of corrupted documents (rebuild passes, geometry unchanged). Its live verifier is also used by `services/mcp/regen_tools.py`. |
| `corpus/regen_corpus.py` | Generates the regen-fix corruption corpus from `contracts/fixtures/part_bracket.json` (`build_corpus`). |
| `roundtrip_pipeline.py` | Round-trip bench: parts-library research → reverse-engineering build → parts-library store. |
| `schema.py`, `dataset.py`, `baseline.py`, `metrics.py` | Shared result schema (`aicad.bench/1`), dataset hashing, baseline rows, metrics. |

```
python3 -m tools.flywheel.bench.categories list [--json]
python3 -m tools.flywheel.bench.categories run <id> [--mode diag|smoke]

python3 -m tools.flywheel.bench.gym.cli run --smoke            # C1, local tier, seed 1
python3 -m tools.flywheel.bench.gym.cli run --real --tiers local-24k --challenges C1
python3 -m tools.flywheel.bench.gym.cli diag --tiers xai:grok-4-5
python3 -m tools.flywheel.bench.gym.leaderboard build [--out PATH] [--mock]

python3 -m tools.flywheel.bench.roundtrip_pipeline [--out DIR]
```

Gym reports are written under `bench/gym/reports/`. Mock cycles replay scripted
turns and are labelled mock everywhere; `--real` needs the C++ core, a provisioned
model, and the DAG dependencies (the CLI re-execs into the shared mcp-venv when
the current interpreter lacks them).

## `shadow_report.py`

Summarises a JSONL shadow-comparison log (incumbent vs challenger provider:
verifier scores, latency, cost, accept rate).

```
python3 tools/flywheel/shadow_report.py shadow_log.jsonl [--min-calls 100] [--title TEXT]
```

## `replay_ledger_failures.py`

Replays every tool call that failed in recorded runs against the current code and
reports how many now succeed. Reads gym ledgers by default, or live desktop runs
with `--live`.

```
python3 tools/flywheel/replay_ledger_failures.py [--live] [--since-days N] \
    [--tool NAME] [--detail] [--json out.json]
```

## Tests

```
python3 -m pytest tools/flywheel -q
```
