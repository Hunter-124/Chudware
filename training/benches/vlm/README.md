# vlm render-review bench (IP-21)

**Measures:** a reviewer's *claims* about geometry against the **known geometry** behind a
synthetic render — precision and recall of claims, not image quality.

**Locked design rule (survey Appendix A) — never invert:** a VLM proposes COARSE claims
about what a render shows; deterministic code verifies/measures those claims against known
geometry. This bench *is* that verification step, run offline against committed fixtures.
It never lets a model grade itself.

**Metric:** per example,

```
precision = correct_claims / total_claims   (0 hallucinations -> precision == 1.0)
recall    = matched_facts   / total_facts
score     = F1(precision, recall)
passed    = recall >= 0.8  AND  precision == 1.0
```

**Precision is king.** A single hallucinated / ungrounded claim fails the example
regardless of how much else it got right — recall never masks a hallucination. This is
the concrete enforcement of the locked division of labor: the model's job is coarse
recall, code's job is to catch anything it got wrong before a human sees it.

Claim matching (`_matches` in `tools/flywheel/bench/benches/vlm.py`): same `kind`, and
every attribute the claim asserts must be compatible with the fact — CONTINUOUS numeric
attributes (bbox dims, diameters, depths) within tolerance `max(1.0mm abs, 5% rel)`;
everything else, **including discrete counts**, by exact equality. A claim need not
restate every attribute a fact carries (coarse claims are fine), but nothing it *does*
assert may be wrong — and a miscounted claim (e.g. "6 holes" when there are 8) is scored
as ungrounded, not "close enough", so it sinks precision like any other hallucination.

**Occlusion caveat:** `describe_scene()` (`services/mcp/gsl_tools.py`) does **not** model
occlusion — a face behind another still lists if it faces the camera. This bench does not
penalize a claim purely for naming something that might be occluded; matching is by
geometry-fact equality only. Visibility (`"visible": false` on a fact) only gates what the
*deterministic fallback* would propose, never what a claim is allowed to say.

**Dataset** — 10 committed fixtures under `scenes/*.json`, 5 part families (`bracket`,
`flange`, `shaft`, `plate`, `enclosure`), each `{known_geometry: [facts], claims: [...],
structure: "<family>"}`:

| fixture | what it exercises |
|---|---|
| `bracket_hallucinated_thread` | a claim with no matching fact (thread) — hallucination |
| `flange_missed_boss`, `shaft_missed_thread`, `enclosure_internal_ribs` | a known fact the claim set never mentions — recall < 1 |
| `plate_miscounted_holes` | a claim with the right `kind` but a wrong attribute (hole count) — sinks both precision and recall |
| the remaining 5 fixtures | perfect claim sets (precision == recall == 1.0) |

`spec.json` freezes the content hash of `scenes/` (`tools/flywheel/bench/dataset.py`); a
second machine gets the same hash from the same bytes (DoD).

**Baselines:**
- `deterministic` (measured) — `deterministic_claims()`, the `describe_scene`-style
  non-vision fallback: echoes every fact marked visible (default true) verbatim as a
  claim. Near-perfect precision by construction (it only ever restates a known fact) but
  modest recall (it never proposes anything a face-normal read couldn't see — internal
  ribs, thread pitch, etc. are `"visible": false` in fixtures). This is the ceiling a
  trained VLM must beat.
- `frontier` (**pending**) — the real offline-VLM candidate. No credentials/network
  offline/CI, so this is an honest `pending_frontier()` stub filled once T-H7 logging +
  provider credentials exist. Its residual over `deterministic` **feeds T-H10**.

**Live path (guarded):** `score_example(..., live_check=...)` and `_core_available()` are
the extension points for cross-checking a fixture's known geometry against a real
`describe_scene()` call once a fixture carries a live `doc_id` — none do today (rendered
PNGs are optional; this bench scores claims-vs-facts, not pixels). `_core_available()`
probes `services.mcp.core_bridge` import only (never a hardcoded sibling `.so` path) and
records skip-with-reason in `aggregate.live_core` without gating the offline score.

**Run:**
```
python -m tools.flywheel.bench.runner vlm          # full (10)
python -m tools.flywheel.bench.runner vlm --smoke  # gate-size (4)
```
(Register `vlm.DRIVER` in `runner.REGISTRY` to wire the CLI entry — this bench module
does not modify `runner.py` itself.)
